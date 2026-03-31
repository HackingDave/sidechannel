"""Signal command handlers for the /schedule command."""

import re
from datetime import datetime
from typing import Callable, Optional, Tuple

import structlog

from .database import SchedulerDatabase
from .loop import SchedulerLoop, compute_next_run
from .models import ScheduleStatus, RunOutcome
from .parser import parse_schedule_expression

logger = structlog.get_logger()


class SchedulerCommands:
    """Handles /schedule subcommands."""

    def __init__(
        self,
        db: SchedulerDatabase,
        loop: SchedulerLoop,
        get_current_project: Callable[[str], Tuple[Optional[str], Optional[str]]],
    ):
        self.db = db
        self.loop = loop
        self._get_current_project = get_current_project

    async def handle(self, sender: str, args: str) -> str:
        """Route /schedule subcommands."""
        if not args:
            return self._usage()

        parts = args.strip().split(None, 1)
        subcommand = parts[0].lower()
        rest = parts[1] if len(parts) > 1 else ""

        if subcommand == "add":
            return await self._add(sender, rest)
        elif subcommand == "list":
            return await self._list(sender)
        elif subcommand == "remove":
            return await self._remove(sender, rest)
        elif subcommand == "pause":
            return await self._pause(sender, rest)
        elif subcommand == "resume":
            return await self._resume(sender, rest)
        elif subcommand == "run":
            return await self._run(sender, rest)
        elif subcommand == "history":
            return await self._history(sender, rest)
        else:
            return self._usage()

    def _usage(self) -> str:
        return (
            "Usage:\n"
            "  /schedule add <when> <prompt>\n"
            "  /schedule list\n"
            "  /schedule remove <id>\n"
            "  /schedule pause <id>\n"
            "  /schedule resume <id>\n"
            "  /schedule run <id>\n"
            "  /schedule history <id>\n\n"
            "Examples:\n"
            "  /schedule add daily at 5am Check production for errors\n"
            "  /schedule add every 6 hours Check API health\n"
            "  /schedule add every weekday at 9am --project nightwire Review PRs"
        )

    async def _add(self, sender: str, args: str) -> str:
        if not args:
            return "Usage: /schedule add <when> <prompt>\nExample: /schedule add daily at 5am Check production for errors"

        # Extract --project or --global flags
        project_name = None
        project_path = None
        explicit_project = False
        is_global = False

        # Check for --global flag
        if "--global" in args:
            args = args.replace("--global", "").strip()
            is_global = True

        # Check for --project flag
        project_match = re.search(r'--project\s+(\S+)', args)
        if project_match:
            project_name = project_match.group(1)
            args = args[:project_match.start()] + args[project_match.end():]
            args = args.strip()
            explicit_project = True

        if not is_global and not explicit_project:
            # Default to current project
            current = self._get_current_project(sender)
            project_name = current[0]
            project_path_str = current[1]
            if project_path_str:
                project_path = str(project_path_str)

        # Parse the schedule expression from the beginning of args
        # Strategy: try progressively longer prefixes until we find a valid schedule
        schedule_type = None
        schedule_params = None
        description = None
        prompt = None

        # Try progressively longer prefixes — take the first valid match
        # so the prompt gets as many words as possible
        words = args.split()
        first_match = None
        for i in range(2, len(words) + 1):
            candidate_expr = " ".join(words[:i])
            stype, sparams, result = parse_schedule_expression(candidate_expr)
            if stype is not None:
                first_match = (i, stype, sparams, result)
                break

        if first_match is None:
            _, _, error = parse_schedule_expression(args)
            return error

        idx, schedule_type, schedule_params, description = first_match
        prompt = " ".join(words[idx:])

        if not prompt:
            return "Missing prompt. What should Claude do on this schedule?"

        # Compute first run time
        now = datetime.now()
        from .models import Schedule, ScheduleType
        temp_schedule = Schedule(
            id=0, phone_number=sender, prompt=prompt,
            schedule_type=schedule_type, schedule_params=schedule_params,
            project_name=project_name, project_path=project_path,
        )
        next_run = compute_next_run(temp_schedule, after=now)

        # Create the schedule
        schedule = await self.db.create_schedule(
            phone_number=sender,
            prompt=prompt,
            schedule_type=schedule_type,
            schedule_params=schedule_params,
            project_name=project_name,
            project_path=project_path,
            description=description,
            next_run_at=next_run,
        )

        project_label = f"[{project_name}]" if project_name else "[global]"
        return (
            f"Schedule #{schedule.id} created {project_label}\n"
            f"When: {description}\n"
            f"Prompt: {prompt[:100]}{'...' if len(prompt) > 100 else ''}\n"
            f"Next run: {next_run.strftime('%Y-%m-%d %H:%M')}"
        )

    async def _list(self, sender: str) -> str:
        schedules = await self.db.list_schedules()
        if not schedules:
            return "No scheduled tasks. Use /schedule add to create one."

        lines = [f"Scheduled Tasks ({len(schedules)})"]
        for s in schedules:
            status_icon = "||" if s.status == ScheduleStatus.PAUSED else ""
            project_label = f"[{s.project_name}]" if s.project_name else "[global]"
            line = f"\n#{s.id} -- {s.description} {project_label} {status_icon}"
            prompt_preview = s.prompt[:60] + ("..." if len(s.prompt) > 60 else "")
            line += f'\n   "{prompt_preview}"'

            if s.last_run_at:
                outcome_icon = {
                    RunOutcome.SILENT: "(silent)",
                    RunOutcome.NOTIFIED: "(notified)",
                    RunOutcome.FAILED: "(FAILED)",
                    RunOutcome.QUEUED: "(queued)",
                    RunOutcome.SKIPPED: "(skipped)",
                }.get(s.last_outcome, "")
                line += f"\n   Last: {s.last_run_at.strftime('%m-%d %H:%M')} {outcome_icon}"

            if s.next_run_at and s.status == ScheduleStatus.ACTIVE:
                line += f"  Next: {s.next_run_at.strftime('%m-%d %H:%M')}"

            if s.total_runs > 0:
                line += f"\n   Runs: {s.total_runs}"
                if s.total_notifications > 0:
                    line += f" | Alerts: {s.total_notifications}"

            lines.append(line)

        return "\n".join(lines)

    async def _remove(self, sender: str, args: str) -> str:
        schedule_id = self._parse_id(args)
        if schedule_id is None:
            return "Usage: /schedule remove <id>"
        deleted = await self.db.delete_schedule(schedule_id)
        if deleted:
            return f"Schedule #{schedule_id} removed."
        return f"Schedule #{schedule_id} not found."

    async def _pause(self, sender: str, args: str) -> str:
        schedule_id = self._parse_id(args)
        if schedule_id is None:
            return "Usage: /schedule pause <id>"
        schedule = await self.db.get_schedule(schedule_id)
        if not schedule:
            return f"Schedule #{schedule_id} not found."
        if schedule.status == ScheduleStatus.PAUSED:
            return f"Schedule #{schedule_id} is already paused."
        await self.db.update_schedule_status(schedule_id, ScheduleStatus.PAUSED)
        return f"Schedule #{schedule_id} paused."

    async def _resume(self, sender: str, args: str) -> str:
        schedule_id = self._parse_id(args)
        if schedule_id is None:
            return "Usage: /schedule resume <id>"
        schedule = await self.db.get_schedule(schedule_id)
        if not schedule:
            return f"Schedule #{schedule_id} not found."
        if schedule.status == ScheduleStatus.ACTIVE:
            return f"Schedule #{schedule_id} is already active."
        await self.db.update_schedule_status(schedule_id, ScheduleStatus.ACTIVE)
        return f"Schedule #{schedule_id} resumed."

    async def _run(self, sender: str, args: str) -> str:
        schedule_id = self._parse_id(args)
        if schedule_id is None:
            return "Usage: /schedule run <id>"
        return await self.loop.trigger_now(schedule_id)

    async def _history(self, sender: str, args: str) -> str:
        schedule_id = self._parse_id(args)
        if schedule_id is None:
            return "Usage: /schedule history <id>"
        schedule = await self.db.get_schedule(schedule_id)
        if not schedule:
            return f"Schedule #{schedule_id} not found."

        runs = await self.db.get_recent_runs(schedule_id, limit=5)
        if not runs:
            return f"Schedule #{schedule_id} has no run history yet."

        lines = [f"History for #{schedule_id} ({schedule.description}):"]
        for run in runs:
            outcome_icon = {
                RunOutcome.SILENT: "(silent)",
                RunOutcome.NOTIFIED: "(notified)",
                RunOutcome.FAILED: "FAILED",
                RunOutcome.QUEUED: "(queued)",
            }.get(run.outcome, str(run.outcome))

            started = run.started_at.strftime("%m-%d %H:%M")
            duration = ""
            if run.completed_at:
                secs = int((run.completed_at - run.started_at).total_seconds())
                if secs >= 60:
                    duration = f" ({secs // 60}m{secs % 60}s)"
                else:
                    duration = f" ({secs}s)"

            line = f"  {started} {outcome_icon}{duration}"
            if run.error:
                line += f"\n    Error: {run.error[:100]}"
            lines.append(line)

        return "\n".join(lines)

    def _parse_id(self, args: str) -> Optional[int]:
        """Parse a schedule ID from args (e.g., '3' or '#3')."""
        if not args:
            return None
        cleaned = args.strip().lstrip("#")
        try:
            return int(cleaned)
        except ValueError:
            return None
