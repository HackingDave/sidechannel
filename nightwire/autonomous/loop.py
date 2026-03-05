"""Autonomous task execution loop with parallel task support.

Runs as a background asyncio task, polling the task queue and
dispatching parallel workers up to a configurable concurrency
limit. Handles dependency resolution, circular dependency
detection, stale task recovery, story/PRD completion tracking,
retry logic, and user notifications via Signal.

Classes:
    AutonomousLoop: Background loop that polls the task queue,
        dispatches workers, and manages task lifecycle.
"""

import asyncio
from datetime import datetime, timedelta
from typing import Awaitable, Callable, List, Optional, Set

import structlog

from .database import AutonomousDatabase
from .exceptions import (
    AutonomousError,
)
from .executor import TaskExecutor
from .models import (
    LoopStatus,
    PRDStatus,
    StoryStatus,
    Task,
    TaskStatus,
)

logger = structlog.get_logger("nightwire.autonomous")

# Tasks stuck IN_PROGRESS longer than this are considered stale (crash recovery)
STALE_TASK_TIMEOUT_MINUTES = 60


class AutonomousLoop:
    """Background loop for autonomous task execution.

    Polls the task queue at a configurable interval and dispatches
    up to ``max_parallel`` concurrent workers. Workers are bounded
    by an ``asyncio.Semaphore`` and resource checks (CPU/memory).

    Features:
        - Dependency-aware parallel batching
        - Circular dependency detection (DFS cycle detection)
        - Stale task recovery on startup (crash recovery)
        - Automatic story/PRD completion tracking
        - Retry logic with configurable max retries
        - Daily task counter reset at midnight
    """

    def __init__(
        self,
        db: AutonomousDatabase,
        executor: TaskExecutor,
        progress_callback: Optional[Callable[[str, str], Awaitable[None]]] = None,
        poll_interval: int = 30,
        max_parallel: int = 3,
        usage_recorder: Optional[Callable[..., Awaitable[None]]] = None,
    ):
        """
        Initialize the autonomous loop.

        Args:
            db: Database for task management
            executor: Task executor instance
            progress_callback: Async callback(phone_number, message) for progress updates
            poll_interval: Seconds between queue polls
            max_parallel: Max concurrent task workers
            usage_recorder: Async callback to record usage data.
                Signature: (phone_number, project_name, source, usage_data) -> None.
        """
        self.db = db
        self.executor = executor
        self.progress_callback = progress_callback
        self.poll_interval = poll_interval
        self.max_parallel = max_parallel
        self._usage_recorder = usage_recorder

        self._running = False
        self._paused = False
        self._task: Optional[asyncio.Task] = None
        self._current_task_id: Optional[int] = None
        self._current_phone: Optional[str] = None
        self._started_at: Optional[datetime] = None
        self._tasks_completed_today = 0
        self._tasks_failed_today = 0
        self._counter_date = datetime.now().date()
        self._last_task_completed_at: Optional[datetime] = None

        # Parallel execution tracking
        self._active_workers: dict[int, asyncio.Task] = {}  # task_id -> asyncio.Task
        self._active_task_ids: Set[int] = set()
        self._worker_semaphore = asyncio.Semaphore(max_parallel)

    @property
    def is_running(self) -> bool:
        """Check if loop is active."""
        return self._running and not self._paused

    @property
    def is_paused(self) -> bool:
        """Check if loop is paused."""
        return self._running and self._paused

    def _reset_daily_counters_if_needed(self) -> None:
        """Reset daily task counters at midnight."""
        today = datetime.now().date()
        if today != self._counter_date:
            self._tasks_completed_today = 0
            self._tasks_failed_today = 0
            self._counter_date = today

    async def get_status(self) -> LoopStatus:
        """Get current loop status snapshot.

        Returns:
            LoopStatus with running/paused state, active
            workers, queue depth, daily counters, and uptime.
        """
        self._reset_daily_counters_if_needed()
        queued_count = await self.db.get_queued_task_count()
        uptime = 0.0
        if self._started_at and self._running:
            uptime = (datetime.now() - self._started_at).total_seconds()

        return LoopStatus(
            is_running=self._running,
            is_paused=self._paused,
            current_task_id=self._current_task_id,
            parallel_task_ids=list(self._active_task_ids),
            max_parallel=self.max_parallel,
            tasks_queued=queued_count,
            tasks_completed_today=self._tasks_completed_today,
            tasks_failed_today=self._tasks_failed_today,
            last_task_completed_at=self._last_task_completed_at,
            uptime_seconds=uptime,
        )

    async def start(self) -> None:
        """Start the autonomous loop.

        Recovers any stale tasks from a previous crash, then
        spawns the background polling task. No-op if already
        running.
        """
        if self._running:
            logger.warning("autonomous_loop_already_running")
            return

        # Recover stale tasks before starting
        recovered = await self._recover_stale_tasks()
        if recovered > 0:
            logger.info("stale_tasks_recovered", count=recovered)

        self._running = True
        self._paused = False
        self._started_at = datetime.now()
        self._worker_semaphore = asyncio.Semaphore(self.max_parallel)
        self._task = asyncio.create_task(self._run_loop())
        logger.info("autonomous_loop_started", max_parallel=self.max_parallel)

    async def stop(self) -> None:
        """Stop the autonomous loop and cancel all workers."""
        self._running = False

        # Cancel all active workers
        for task_id, worker in list(self._active_workers.items()):
            worker.cancel()
            try:
                await worker
            except asyncio.CancelledError:
                pass
        self._active_workers.clear()
        self._active_task_ids.clear()

        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        self._started_at = None
        logger.info("autonomous_loop_stopped")

    async def pause(self) -> None:
        """Pause processing (active tasks finish, no new dispatches)."""
        self._paused = True
        logger.info("autonomous_loop_paused")

    async def resume(self) -> None:
        """Resume processing."""
        self._paused = False
        logger.info("autonomous_loop_resumed")

    async def _run_loop(self) -> None:
        """Main processing loop - dispatches parallel workers."""
        while self._running:
            try:
                self._reset_daily_counters_if_needed()

                if self._paused:
                    await asyncio.sleep(self.poll_interval)
                    continue

                # Clean up finished workers
                self._cleanup_finished_workers()

                # Get batch of parallelizable tasks
                tasks = await self._get_parallel_batch()

                if not tasks:
                    # No tasks in queue, wait and check again
                    await asyncio.sleep(self.poll_interval)
                    continue

                # Dispatch workers for each task
                for task in tasks:
                    if not self._running:
                        break
                    # Spawn worker (semaphore limits concurrency)
                    worker = asyncio.create_task(
                        self._worker_wrapper(task)
                    )
                    self._active_workers[task.id] = worker
                    self._active_task_ids.add(task.id)

                # Small delay before checking for more work
                await asyncio.sleep(2)

            except asyncio.CancelledError:
                break
            except AutonomousError as e:
                logger.error("autonomous_loop_error", error=str(e), exc_type=type(e).__name__)
                await asyncio.sleep(self.poll_interval)
            except (OSError, RuntimeError, ValueError) as e:
                logger.error("autonomous_loop_error", error=str(e), exc_type=type(e).__name__)
                await asyncio.sleep(self.poll_interval)

    def _cleanup_finished_workers(self) -> None:
        """Remove completed worker tasks."""
        finished = [
            task_id for task_id, worker in self._active_workers.items()
            if worker.done()
        ]
        for task_id in finished:
            worker = self._active_workers.pop(task_id, None)
            self._active_task_ids.discard(task_id)
            # Check for uncaught exceptions
            if worker and worker.done() and not worker.cancelled():
                exc = worker.exception()
                if exc:
                    logger.error(
                        "worker_uncaught_exception",
                        task_id=task_id,
                        error=str(exc),
                    )

    async def _get_parallel_batch(self) -> List[Task]:
        """Get a batch of tasks that can run in parallel.

        Strategy:
        - Get all queued tasks for the current story
        - Filter out tasks that have unmet dependencies
        - Filter out tasks that share files with currently running tasks
        - Return up to (max_parallel - active_count) tasks
        """
        available_slots = self.max_parallel - len(self._active_task_ids)
        if available_slots <= 0:
            return []

        # Get next queued task to determine current story
        next_task = await self.db.get_next_queued_task()
        if not next_task:
            return []

        # If we can only run 1, just return it (original behavior)
        if self.max_parallel <= 1:
            return [next_task]

        # Get all queued tasks for this story
        story_tasks = await self.db.list_tasks(
            story_id=next_task.story_id,
            status=TaskStatus.QUEUED,
        )

        if not story_tasks:
            return [next_task]

        # Detect and fail circular dependencies
        cyclic_ids = await self._detect_circular_dependencies(next_task.story_id)
        for cid in cyclic_ids:
            ctask = await self.db.get_task(cid)
            if ctask and ctask.status == TaskStatus.QUEUED:
                await self.db.update_task_status(
                    cid, TaskStatus.FAILED,
                    error_message="Circular dependency detected - task cannot be scheduled",
                )
                logger.warning("circular_dependency_detected", task_id=cid)
                await self._notify(
                    ctask.phone_number,
                    f"Task FAILED (circular dependency): {ctask.title}",
                )

        # Filter tasks that can run now (dependencies met)
        runnable = []
        for task in story_tasks:
            if task.id in self._active_task_ids:
                continue

            # Check dependencies
            if task.depends_on:
                deps_met = await self._check_dependencies(task.depends_on)
                logger.debug(
                    "dependency_check",
                    task_id=task.id,
                    dependencies=task.depends_on,
                    all_satisfied=deps_met,
                )
                if not deps_met:
                    continue

            runnable.append(task)

            if len(runnable) >= available_slots:
                break

        # Only fall back to next_task if it's not already active
        if runnable:
            return runnable
        if next_task.id not in self._active_task_ids:
            return [next_task]
        return []

    async def _check_dependencies(self, dep_ids: List[int]) -> bool:
        """Check if all dependency tasks are completed."""
        for dep_id in dep_ids:
            dep_task = await self.db.get_task(dep_id)
            if dep_task is None:
                continue
            if dep_task.status != TaskStatus.COMPLETED:
                return False
        return True

    async def _detect_circular_dependencies(self, story_id: int) -> List[int]:
        """Detect tasks with circular dependencies in a story.

        Uses DFS cycle detection. Returns list of task IDs involved in cycles.
        """
        tasks = await self.db.list_tasks(story_id=story_id)
        task_map = {t.id: t for t in tasks}
        cyclic_ids: Set[int] = set()

        # States: 0=unvisited, 1=in_stack, 2=done
        state: dict[int, int] = {t.id: 0 for t in tasks}

        def dfs(tid: int, path: Set[int]) -> bool:
            """Returns True if a cycle is found involving tid."""
            if state[tid] == 2:
                return False
            if state[tid] == 1:
                # Cycle found - mark all tasks in the current path
                cyclic_ids.update(path)
                return True

            state[tid] = 1
            path.add(tid)
            task = task_map[tid]
            if task.depends_on:
                for dep_id in task.depends_on:
                    if dep_id in task_map:
                        if dfs(dep_id, path):
                            cyclic_ids.add(tid)
            path.discard(tid)
            state[tid] = 2
            return False

        for task in tasks:
            if state[task.id] == 0:
                dfs(task.id, set())

        return list(cyclic_ids)

    async def _worker_wrapper(self, task: Task) -> None:
        """Wrapper for worker that handles semaphore and error recovery."""
        from ..resource_guard import check_resources

        logger.debug("worker_dispatch", task_id=task.id, active_workers=len(self._active_task_ids))

        # Check resources before acquiring semaphore slot
        status = check_resources()
        if not status.ok:
            logger.warning(
                "worker_resource_limit",
                task_id=task.id,
                reason=status.reason,
                memory_percent=status.memory_percent,
            )
            # Re-queue the task instead of failing it
            await self.db.update_task_status(
                task.id, TaskStatus.QUEUED,
                error_message=f"Deferred: {status.reason}",
            )
            await self._notify(
                task.phone_number,
                f"Task deferred (resources): {task.title}\n{status.reason}",
            )
            return

        async with self._worker_semaphore:
            await self._process_task(task)

    async def _process_task(self, task: Task) -> None:
        """Process a single task."""
        # Note: _current_task_id is legacy for single-worker mode.
        # In parallel mode, use _active_task_ids instead.
        if len(self._active_task_ids) <= 1:
            self._current_task_id = task.id

        try:
            # Update status to in_progress
            logger.debug(
                "task_state_transition",
                task_id=task.id,
                from_status="queued",
                to_status="in_progress",
            )
            await self.db.update_task_status(
                task.id, TaskStatus.IN_PROGRESS, started_at=datetime.now()
            )

            # Get story and PRD info for context
            story = await self.db.get_story(task.story_id)
            prd_title = ""
            story_progress = ""
            if story:
                prd = await self.db.get_prd(story.prd_id)
                if prd:
                    prd_title = f" ({prd.title})"
                # Get task position in story
                all_tasks = await self.db.list_tasks(story_id=task.story_id)
                completed = sum(1 for t in all_tasks if t.completed_at is not None)
                total = len(all_tasks)
                story_progress = f"\nStory: {story.title} - Task {completed + 1}/{total}"

            # Parallel indicator
            parallel_info = ""
            if len(self._active_task_ids) > 1:
                parallel_info = f" [parallel: {len(self._active_task_ids)} workers]"

            # Notify user with context
            await self._notify(
                task.phone_number,
                f"Starting task{prd_title}: {task.title}{story_progress}{parallel_info}"
            )

            # Create a progress callback for this task
            async def task_progress(msg: str):
                await self._notify(task.phone_number, f"  [{task.id}] {msg}")

            # Execute the task
            result = await self.executor.execute(task, progress_callback=task_progress)

            # Record accumulated usage from task execution
            if result.usage_data and self._usage_recorder:
                project_name = task.project_name
                for usage_entry in result.usage_data:
                    try:
                        await self._usage_recorder(
                            phone_number=task.phone_number,
                            project_name=project_name,
                            source="autonomous",
                            usage_data=usage_entry,
                        )
                    except Exception:
                        pass  # Non-critical

            # Handle result
            if result.success:
                logger.debug(
                    "task_state_transition",
                    task_id=task.id,
                    from_status="in_progress",
                    to_status="completed",
                )
                await self._handle_success(task, result)
            else:
                logger.debug(
                    "task_state_transition",
                    task_id=task.id,
                    from_status="in_progress",
                    to_status="failed",
                )
                await self._handle_failure(task, result)

        except AutonomousError as e:
            logger.error(
                "task_processing_error",
                task_id=task.id,
                error=str(e),
                exc_type=type(e).__name__,
            )
            await self.db.update_task_status(
                task.id, TaskStatus.FAILED, error_message=f"[{type(e).__name__}] {e}"
            )
            self._tasks_failed_today += 1
            await self._notify(
                task.phone_number,
                f"Task FAILED: {task.title}\nCheck logs for details."
            )
        except (OSError, asyncio.TimeoutError, RuntimeError, ValueError) as e:
            logger.error(
                "task_processing_error",
                task_id=task.id,
                error=str(e),
                exc_type=type(e).__name__,
            )
            await self.db.update_task_status(
                task.id, TaskStatus.FAILED, error_message=f"[{type(e).__name__}] {e}"
            )
            self._tasks_failed_today += 1
            await self._notify(
                task.phone_number,
                f"Task FAILED: {task.title}\nCheck logs for details."
            )
        finally:
            if self._current_task_id == task.id:
                self._current_task_id = None
            # Always clean up from active set
            self._active_task_ids.discard(task.id)
            self._active_workers.pop(task.id, None)

    async def _handle_success(self, task: Task, result) -> None:
        """Handle successful task completion."""
        # Build update kwargs including verification results
        update_kwargs = dict(
            completed_at=datetime.now(),
            claude_output=result.claude_output,
            files_changed=result.files_changed,
            quality_gate_results=result.quality_gate,
        )

        await self.db.update_task_status(
            task.id,
            TaskStatus.COMPLETED,
            **update_kwargs,
        )

        # Store verification result if available
        if result.verification:
            await self.db.store_verification_result(task.id, result.verification)

        # Store extracted learnings
        for learning in result.learnings_extracted:
            await self.db.store_learning(learning)

        self._tasks_completed_today += 1
        self._last_task_completed_at = datetime.now()

        # Check if story is complete
        await self._check_story_completion(task.story_id)

        # Notify user
        files_info = (
            f"\nFiles changed: {len(result.files_changed)}"
            if result.files_changed else ""
        )
        learnings_info = (
            f"\nLearnings captured: {len(result.learnings_extracted)}"
            if result.learnings_extracted else ""
        )
        verification_info = ""
        if result.verification:
            if result.verification.passed:
                verification_info = "\nVerification: PASSED"
            else:
                issue_count = len(result.verification.issues)
                verification_info = f"\nVerification: {issue_count} issue(s) found"

        await self._notify(
            task.phone_number,
            f"Completed: {task.title}{files_info}{learnings_info}{verification_info}"
        )

    async def _handle_failure(self, task: Task, result) -> None:
        """Handle task failure with retry logic."""
        if task.retry_count < task.max_retries:
            # Queue for retry
            await self.db.increment_retry_count(task.id)
            await self.db.update_task_status(
                task.id,
                TaskStatus.QUEUED,
                error_message=result.error_message,
            )

            await self._notify(
                task.phone_number,
                f"Task failed, retrying ({task.retry_count + 1}/{task.max_retries}): "
                f"{task.title}\nError: "
                f"{result.error_message[:200] if result.error_message else 'Unknown'}"
            )
        else:
            # Max retries reached
            await self.db.update_task_status(
                task.id,
                TaskStatus.FAILED,
                error_message=result.error_message,
                claude_output=result.claude_output,
            )

            self._tasks_failed_today += 1

            # Store the failure as a learning
            if result.learnings_extracted:
                for learning in result.learnings_extracted:
                    await self.db.store_learning(learning)

            await self._notify(
                task.phone_number,
                f"Task FAILED (max retries): {task.title}\n"
                f"Error: {result.error_message[:300] if result.error_message else 'Unknown'}"
            )

            # Check if story should be marked as failed
            await self._check_story_completion(task.story_id)

    async def _check_story_completion(self, story_id: int) -> None:
        """Check if all tasks in a story are complete or failed."""
        story = await self.db.get_story(story_id)
        if not story:
            return

        # Check if all tasks are in a terminal state (completed or failed)
        tasks_done = story.completed_tasks + story.failed_tasks
        if story.total_tasks > 0 and tasks_done == story.total_tasks:
            if story.failed_tasks > 0:
                # At least one task failed - mark story as failed
                await self.db.update_story_status(story_id, StoryStatus.FAILED)

                await self._notify(
                    story.phone_number,
                    f"Story FAILED: {story.title}\n"
                    f"({story.completed_tasks} completed, {story.failed_tasks} failed)"
                )
            else:
                # All tasks completed successfully
                await self.db.update_story_status(story_id, StoryStatus.COMPLETED)

                await self._notify(
                    story.phone_number,
                    f"Story COMPLETED: {story.title}"
                )

            # Check PRD completion (whether story completed or failed)
            await self._check_prd_completion(story.prd_id, story.phone_number)

    async def _check_prd_completion(self, prd_id: int, phone_number: str) -> None:
        """Check if all stories in a PRD are in a terminal state (completed or failed)."""
        prd = await self.db.get_prd(prd_id)
        if not prd:
            return

        finished_stories = prd.completed_stories + prd.failed_stories
        if prd.total_stories > 0 and finished_stories == prd.total_stories:
            await self.db.update_prd_status(prd_id, PRDStatus.COMPLETED)

            # Build detailed completion summary
            stories = await self.db.list_stories(prd_id=prd_id)
            total_tasks = sum(s.total_tasks for s in stories)
            failed_tasks = sum(s.failed_tasks for s in stories)

            # Collect all files changed and story summaries
            all_files_changed = set()
            story_summaries = []

            for story in stories:
                tasks = await self.db.list_tasks(story_id=story.id)
                story_files = set()

                for task in tasks:
                    if task.files_changed:
                        story_files.update(task.files_changed)
                        all_files_changed.update(task.files_changed)

                # Build story summary line
                status_icon = "[x]" if story.status == StoryStatus.COMPLETED else "[!]"
                file_count = f" ({len(story_files)} files)" if story_files else ""
                story_summaries.append(f"  {status_icon} {story.title}{file_count}")

            # Calculate duration if we have timestamps
            duration_str = ""
            if prd.created_at:
                duration = datetime.now() - prd.created_at
                mins = int(duration.total_seconds() / 60)
                if mins >= 60:
                    hours = mins // 60
                    mins = mins % 60
                    duration_str = f"\nDuration: {hours}h {mins}m"
                else:
                    duration_str = f"\nDuration: {mins} min"

            # Build the summary message
            summary = (
                f"PRD COMPLETED: {prd.title}\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
            )

            # Add stories summary
            summary += f"\nStories Completed ({len(stories)}):\n"
            summary += "\n".join(story_summaries)

            # Add files changed summary
            if all_files_changed:
                summary += f"\n\nFiles Changed ({len(all_files_changed)}):\n"
                # Show up to 15 files, sorted for readability
                sorted_files = sorted(all_files_changed)
                for f in sorted_files[:15]:
                    # Shorten paths for readability
                    short_path = f.split('/')[-1] if '/' in f else f
                    summary += f"  - {short_path}\n"
                if len(sorted_files) > 15:
                    summary += f"  ... and {len(sorted_files) - 15} more files\n"

            # Add stats
            summary += "\nStats:\n"
            summary += f"  Tasks: {total_tasks - failed_tasks} completed"
            if failed_tasks > 0:
                summary += f", {failed_tasks} failed"
            summary += duration_str

            await self._notify(phone_number, summary)

    async def _recover_stale_tasks(self) -> int:
        """Recover tasks stuck in IN_PROGRESS state from a previous crash.

        Tasks that have been IN_PROGRESS for longer than STALE_TASK_TIMEOUT_MINUTES
        are re-queued so they can be retried. This handles the case where the bot
        crashed while a task was running.

        Returns:
            Number of tasks recovered.
        """
        try:
            stale_tasks = await self.db.list_tasks(status=TaskStatus.IN_PROGRESS)
            recovered = 0
            cutoff = datetime.now() - timedelta(minutes=STALE_TASK_TIMEOUT_MINUTES)

            for task in stale_tasks:
                # Only recover truly stale tasks (not ones we're actively running)
                if task.id in self._active_task_ids:
                    continue

                # Check if the task has been stuck long enough
                if task.started_at and task.started_at < cutoff:
                    if task.retry_count < task.max_retries:
                        await self.db.increment_retry_count(task.id)
                        await self.db.update_task_status(
                            task.id,
                            TaskStatus.QUEUED,
                            error_message=(
                                f"Recovered from stale state (was IN_PROGRESS"
                                f" for >{STALE_TASK_TIMEOUT_MINUTES}min)"
                            ),
                        )
                        logger.info(
                            "stale_task_requeued",
                            task_id=task.id,
                            title=task.title,
                            started_at=str(task.started_at),
                        )
                        await self._notify(
                            task.phone_number,
                            f"Recovered stale task (re-queued): {task.title}",
                        )
                    else:
                        await self.db.update_task_status(
                            task.id,
                            TaskStatus.FAILED,
                            error_message=(
                                "Failed: task was stuck IN_PROGRESS"
                                " and has no retries left"
                            ),
                        )
                        logger.warning(
                            "stale_task_failed",
                            task_id=task.id,
                            title=task.title,
                        )
                        await self._notify(
                            task.phone_number,
                            f"Stale task FAILED (no retries left): {task.title}",
                        )
                    recovered += 1

            return recovered

        except (OSError, RuntimeError, ValueError) as e:
            logger.error("stale_task_recovery_error", error=str(e), exc_type=type(e).__name__)
            return 0

    async def _notify(self, phone_number: str, message: str) -> None:
        """Send a notification to the user."""
        if self.progress_callback:
            try:
                await self.progress_callback(phone_number, message)
            except (OSError, RuntimeError, ValueError, asyncio.TimeoutError) as e:
                logger.warning("notification_error", error=str(e), exc_type=type(e).__name__)
