"""Scheduler loop — polls for due schedules and executes them."""

import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Callable, Awaitable

import structlog

from ..claude_runner import ClaudeRunner, classify_error, ErrorCategory
from ..config import get_config
from .database import SchedulerDatabase
from .models import Schedule, ScheduleType, ScheduleStatus, RunOutcome

logger = structlog.get_logger()

# How often the loop checks for due schedules (seconds)
DEFAULT_POLL_INTERVAL = 30


def compute_next_run(schedule: Schedule, after: Optional[datetime] = None) -> datetime:
    """Compute the next run time for a schedule.

    For interval schedules, adds the interval to `after` (or now).
    For time-of-day schedules (daily/weekly/weekday/weekend), finds
    the next occurrence of the specified time(s).
    """
    now = after or datetime.now()
    params = schedule.schedule_params
    stype = schedule.schedule_type

    if stype == ScheduleType.INTERVAL:
        return now + timedelta(minutes=params["minutes"])

    if stype == ScheduleType.DAILY:
        times = sorted(params["times"])
        for t_str in times:
            h, m = map(int, t_str.split(":"))
            candidate = now.replace(hour=h, minute=m, second=0, microsecond=0)
            if candidate > now:
                return candidate
        # All times today have passed — first time tomorrow
        h, m = map(int, times[0].split(":"))
        return (now + timedelta(days=1)).replace(hour=h, minute=m, second=0, microsecond=0)

    if stype == ScheduleType.WEEKLY:
        target_day = params["day"]  # 0=monday
        h, m = map(int, params["time"].split(":"))
        candidate = now.replace(hour=h, minute=m, second=0, microsecond=0)
        days_ahead = (target_day - now.weekday()) % 7
        if days_ahead == 0 and candidate <= now:
            days_ahead = 7
        return (now + timedelta(days=days_ahead)).replace(
            hour=h, minute=m, second=0, microsecond=0
        )

    if stype == ScheduleType.WEEKDAY:
        h, m = map(int, params["time"].split(":"))
        candidate = now.replace(hour=h, minute=m, second=0, microsecond=0)
        # Find next weekday (mon-fri = 0-4)
        days = 0
        while True:
            check = candidate + timedelta(days=days)
            if check.weekday() < 5 and check > now:
                return check
            days += 1
            if days > 7:
                break
        return candidate + timedelta(days=1)

    if stype == ScheduleType.WEEKEND:
        h, m = map(int, params["time"].split(":"))
        candidate = now.replace(hour=h, minute=m, second=0, microsecond=0)
        days = 0
        while True:
            check = candidate + timedelta(days=days)
            if check.weekday() >= 5 and check > now:
                return check
            days += 1
            if days > 7:
                break
        return candidate + timedelta(days=1)

    # Fallback
    return now + timedelta(hours=1)


class SchedulerLoop:
    """Background loop that checks for and executes due schedules."""

    def __init__(
        self,
        db: SchedulerDatabase,
        runner: ClaudeRunner,
        notify_callback: Optional[Callable[[str, str], Awaitable[None]]] = None,
        task_semaphore: Optional[asyncio.Semaphore] = None,
        poll_interval: int = DEFAULT_POLL_INTERVAL,
    ):
        self.db = db
        self.runner = runner
        self.notify = notify_callback
        self._task_semaphore = task_semaphore
        self.poll_interval = poll_interval

        self._running = False
        self._paused = False
        self._loop_task: Optional[asyncio.Task] = None
        self._active_runs: dict[int, asyncio.Task] = {}  # schedule_id -> task

    @property
    def is_running(self) -> bool:
        return self._running and not self._paused

    @property
    def is_paused(self) -> bool:
        return self._running and self._paused

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._paused = False
        self._loop_task = asyncio.create_task(self._run_loop())
        logger.info("scheduler_loop_started")

    async def stop(self) -> None:
        self._running = False
        for sid, task in list(self._active_runs.items()):
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._active_runs.clear()
        if self._loop_task:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass
        self._loop_task = None
        logger.info("scheduler_loop_stopped")

    async def pause(self) -> None:
        self._paused = True
        logger.info("scheduler_loop_paused")

    async def resume(self) -> None:
        self._paused = False
        logger.info("scheduler_loop_resumed")

    async def trigger_now(self, schedule_id: int) -> str:
        """Manually trigger a schedule immediately (for /schedule run)."""
        schedule = await self.db.get_schedule(schedule_id)
        if not schedule:
            return f"Schedule #{schedule_id} not found."
        if schedule.id in self._active_runs:
            return f"Schedule #{schedule_id} is already running."
        task = asyncio.create_task(self._execute_schedule(schedule))
        self._active_runs[schedule.id] = task
        return f"Schedule #{schedule_id} triggered."

    async def _run_loop(self) -> None:
        while self._running:
            try:
                if self._paused:
                    await asyncio.sleep(self.poll_interval)
                    continue

                # Clean up finished runs
                finished = [
                    sid for sid, t in self._active_runs.items() if t.done()
                ]
                for sid in finished:
                    task = self._active_runs.pop(sid, None)
                    if task and task.done() and not task.cancelled():
                        exc = task.exception()
                        if exc:
                            logger.error(
                                "scheduler_run_exception",
                                schedule_id=sid,
                                error=str(exc),
                            )

                # Check for due schedules
                now = datetime.now()
                due = await self.db.get_due_schedules(now)

                for schedule in due:
                    if schedule.id in self._active_runs:
                        continue  # Already running
                    task = asyncio.create_task(self._execute_schedule(schedule))
                    self._active_runs[schedule.id] = task

                await asyncio.sleep(self.poll_interval)

            except asyncio.CancelledError:
                break
            except (OSError, RuntimeError, ValueError) as e:
                logger.error("scheduler_loop_error", error=str(e))
                await asyncio.sleep(self.poll_interval)

    async def _execute_schedule(self, schedule: Schedule) -> None:
        """Execute a single scheduled task."""
        started_at = datetime.now()
        run_id = await self.db.record_run_start(schedule.id, started_at)

        try:
            logger.info(
                "scheduler_executing",
                schedule_id=schedule.id,
                description=schedule.description,
                project=schedule.project_name or "global",
            )

            # Determine project path
            project_path = None
            if schedule.project_path:
                p = Path(schedule.project_path)
                if p.is_dir():
                    project_path = p

            # Build the prompt — instruct Claude to only report if noteworthy
            prompt = (
                f"{schedule.prompt}\n\n"
                "IMPORTANT: Only produce output if you find something noteworthy "
                "(errors, issues, failures, security concerns, or things that need "
                "attention). If everything looks fine, respond with exactly: "
                "ALL_CLEAR"
            )

            # Acquire semaphore slot if shared with bot
            if self._task_semaphore:
                async with self._task_semaphore:
                    success, output = await self.runner.run_claude(
                        prompt, project_path=project_path
                    )
            else:
                success, output = await self.runner.run_claude(
                    prompt, project_path=project_path
                )

            # Determine outcome
            if not success:
                outcome = RunOutcome.FAILED
                await self.db.complete_run(
                    run_id, outcome, claude_output=output, error=output[:500]
                )
                # Notify on failure
                if self.notify:
                    await self.notify(
                        schedule.phone_number,
                        f"Scheduled task failed: {schedule.description}\n"
                        f"Error: {output[:300]}",
                    )
            elif output.strip().upper() == "ALL_CLEAR" or "all_clear" in output.strip().lower():
                # Nothing noteworthy
                outcome = RunOutcome.SILENT
                await self.db.complete_run(run_id, outcome, claude_output=output)
            else:
                # Claude found something — notify user
                outcome = RunOutcome.NOTIFIED
                notification = (
                    f"Scheduled alert ({schedule.description}):\n\n{output}"
                )
                # Truncate for Signal message limits
                if len(notification) > 4000:
                    notification = notification[:3950] + "\n\n[truncated]"
                await self.db.complete_run(
                    run_id, outcome, claude_output=output,
                    notification_sent=notification,
                )
                if self.notify:
                    await self.notify(schedule.phone_number, notification)

            # Update schedule timing
            next_run = compute_next_run(schedule, after=started_at)
            await self.db.update_after_run(
                schedule.id, started_at, next_run, outcome
            )

            logger.info(
                "scheduler_completed",
                schedule_id=schedule.id,
                outcome=outcome.value,
                next_run=next_run.isoformat(),
            )

        except asyncio.CancelledError:
            await self.db.complete_run(run_id, RunOutcome.FAILED, error="Cancelled")
            raise
        except Exception as e:
            logger.error(
                "scheduler_execution_error",
                schedule_id=schedule.id,
                error=str(e),
            )
            await self.db.complete_run(run_id, RunOutcome.FAILED, error=str(e)[:500])
            # Still update next_run so the schedule doesn't get stuck
            next_run = compute_next_run(schedule, after=started_at)
            await self.db.update_after_run(
                schedule.id, started_at, next_run, RunOutcome.FAILED
            )
        finally:
            self._active_runs.pop(schedule.id, None)
