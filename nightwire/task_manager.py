"""Background task lifecycle management for Nightwire bot.

Manages per-sender background tasks: starting Claude tasks, checking busy
state, cancelling tasks, and PRD creation orchestration.
"""

import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable, Dict, List, Optional, Tuple

import structlog

from .prd_builder import parse_prd_json

logger = structlog.get_logger("nightwire.bot")


def log_task_exception(task: asyncio.Task):
    """Log exceptions from fire-and-forget tasks instead of silently swallowing them."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc:
        logger.error("background_task_failed", error=str(exc), exc_type=type(exc).__name__)


class TaskManager:
    """Manages per-sender background task lifecycle.

    Each sender can have one concurrent background task. Handles task
    creation, progress tracking, cancellation, and PRD creation.
    """

    def __init__(
        self,
        runner,
        project_manager,
        memory,
        config,
        send_message: Callable[[str, str], Awaitable[None]],
        get_memory_context: Callable[..., Awaitable[Optional[str]]],
    ):
        """Initialize the task manager.

        Args:
            runner: ClaudeRunner instance for executing tasks.
            project_manager: ProjectManager for path resolution.
            memory: MemoryManager for context and storage.
            config: Config instance for timeouts and settings.
            send_message: Async callback to send Signal messages.
            get_memory_context: Async callback to build memory
                context for a sender/prompt/project triple.
        """
        self.runner = runner
        self.project_manager = project_manager
        self.memory = memory
        self.config = config
        self._send_message = send_message
        self._get_memory_context = get_memory_context
        self._sender_tasks: Dict[Tuple[str, str], dict] = {}
        # Per-user+project session IDs for Claude CLI --resume.
        # Keys are "sender:project_name", values are CLI session_id.
        # Ephemeral — lost on bot restart.
        self._session_ids: Dict[str, str] = {}
        # Set after start() — deferred initialization
        self.autonomous_manager = None
        # Budget alert spam prevention: set of alert keys already sent today.
        # Keys are "{phone}:{period}:{threshold}" e.g. "+1234:daily:80"
        self._budget_alerts_sent: set = set()

    async def _record_usage(
        self,
        phone_number: str,
        project_name: Optional[str],
        source: str,
        usage_data: Optional[dict] = None,
        session_id: Optional[str] = None,
    ) -> None:
        """Record a usage entry from a runner invocation (fire-and-forget safe).

        Args:
            phone_number: User who triggered the invocation.
            project_name: Active project name (may be None).
            source: Source label (do, ask, complex, summary, etc.).
            usage_data: Dict with input_tokens, output_tokens, model, cost_usd.
            session_id: Optional CLI session ID.
        """
        if not usage_data:
            return
        try:
            await self.memory.db.record_usage(
                phone_number=phone_number,
                project_name=project_name,
                model=usage_data.get("model", "unknown"),
                input_tokens=usage_data.get("input_tokens", 0),
                output_tokens=usage_data.get("output_tokens", 0),
                cost_usd=usage_data.get("cost_usd", 0.0),
                source=source,
                session_id=session_id,
            )
            # Check budget thresholds
            await self._check_budget_alerts(phone_number)
        except Exception as e:
            logger.debug("usage_recording_failed", error=str(e), source=source)

    async def _check_budget_alerts(self, phone_number: str) -> None:
        """Check daily/weekly budget thresholds and send alerts once.

        Alerts at 80% and 100% of configured budget. Each alert is sent
        only once per phone/period/threshold until the period resets.
        """
        now = datetime.now()
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

        # Reset alerts at the start of each day
        day_key = today_start.isoformat()
        if not hasattr(self, "_budget_alert_day") or self._budget_alert_day != day_key:
            self._budget_alert_day = day_key
            self._budget_alerts_sent.clear()

        checks = []
        daily_budget = self.config.usage_daily_budget_usd
        if daily_budget and daily_budget > 0:
            daily_cost = await self.memory.db.get_usage_cost_since(
                phone_number, 0
            )
            checks.append(("daily", daily_budget, daily_cost))

        weekly_budget = self.config.usage_weekly_budget_usd
        if weekly_budget and weekly_budget > 0:
            weekly_cost = await self.memory.db.get_usage_cost_since(
                phone_number, 7
            )
            checks.append(("weekly", weekly_budget, weekly_cost))

        for period, budget, spent in checks:
            pct = (spent / budget) * 100 if budget > 0 else 0

            if pct >= 100:
                key = f"{phone_number}:{period}:100"
                if key not in self._budget_alerts_sent:
                    self._budget_alerts_sent.add(key)
                    await self._send_message(
                        phone_number,
                        f"Budget EXCEEDED: {period} spending ${spent:.2f}"
                        f" has reached ${budget:.2f} ({pct:.0f}%).",
                    )
            elif pct >= 80:
                key = f"{phone_number}:{period}:80"
                if key not in self._budget_alerts_sent:
                    self._budget_alerts_sent.add(key)
                    await self._send_message(
                        phone_number,
                        f"Budget WARNING: {period} spending ${spent:.2f}"
                        f" is approaching ${budget:.2f} ({pct:.0f}%).",
                    )

    def get_task_state(self, sender: str, project_name: Optional[str] = None) -> Optional[dict]:
        """Get the current task state for a sender, or None."""
        return self._sender_tasks.get((sender, project_name or ""))

    def check_busy(self, sender: str, project_name: Optional[str] = None) -> Optional[str]:
        """Return a busy message if a task is running for this sender, else None."""
        task_state = self._sender_tasks.get((sender, project_name or ""))
        if not task_state or not task_state.get("task") or task_state["task"].done():
            return None
        elapsed = ""
        if task_state.get("start"):
            mins = int((datetime.now() - task_state["start"]).total_seconds() / 60)
            elapsed = f" ({mins}m)"
        desc = task_state.get("description", "unknown")[:100]
        return f"Task in progress{elapsed}: {desc}\nUse /cancel to stop it."

    def start_background_task(
        self,
        sender: str,
        task_description: str,
        project_name: Optional[str],
        image_paths: Optional[List[Path]] = None,
        source: str = "do",
    ) -> None:
        """Start a Claude task in the background (non-blocking).

        Args:
            sender: Phone number of the requesting user.
            task_description: The user's prompt/task text.
            project_name: Currently selected project name.
            image_paths: Optional list of saved image file paths.
                When provided, file paths are appended to the prompt
                so Claude's agentic Read tool can view the images.
            source: Usage source label (do, ask, summary, complex).
        """
        # Build effective description with image paths appended
        effective_description = task_description
        if image_paths:
            paths_text = "\n".join(str(p) for p in image_paths)
            effective_description = (
                f"{task_description}\n\n"
                f"The user also sent {len(image_paths)} image(s). "
                f"Use the Read tool to view them:\n{paths_text}"
            )

        task_state = {
            "description": task_description,
            "start": datetime.now(),
            "step": "Preparing context...",
            "cancel_reason": None,
            "task": None,
        }
        self._sender_tasks[(sender, project_name or "")] = task_state

        async def run_task():
            try:
                async def progress_cb(msg: str):
                    task_state["step"] = msg
                    await self._send_message(sender, msg)

                task_state["step"] = "Loading memory context..."
                memory_context = await self._get_memory_context(
                    sender, task_description, project_name
                )

                task_state["step"] = "Claude executing task..."
                task_project_path = self.project_manager.get_current_path(sender)
                # Session key for --resume continuity
                session_key = (
                    f"{sender}:{project_name}"
                    if project_name
                    else None
                )
                resume_id = (
                    self._session_ids.get(session_key)
                    if session_key
                    else None
                )
                success, response = await self.runner.run_claude(
                    effective_description,
                    progress_callback=progress_cb,
                    memory_context=memory_context,
                    project_path=task_project_path,
                    stream=True,
                    resume_session_id=resume_id,
                )
                # Record usage (fire-and-forget)
                t_usage = asyncio.create_task(
                    self._record_usage(
                        phone_number=sender,
                        project_name=project_name,
                        source=source,
                        usage_data=self.runner.last_usage,
                        session_id=self.runner.last_session_id,
                    )
                )
                t_usage.add_done_callback(log_task_exception)

                # Store session_id for next invocation
                if (
                    success
                    and session_key
                    and self.runner.last_session_id
                ):
                    self._session_ids[session_key] = (
                        self.runner.last_session_id
                    )

                # Store response to memory (fire-and-forget)
                t = asyncio.create_task(
                    self.memory.store_message(
                        phone_number=sender,
                        role="assistant",
                        content=response,
                        project_name=project_name,
                        command_type="do",
                    )
                )
                t.add_done_callback(log_task_exception)

                if success:
                    await self._send_message(sender, "[Task complete]")
                else:
                    await self._send_message(sender, response)

            except asyncio.CancelledError:
                reason = task_state.get("cancel_reason", "user cancel")
                elapsed = ""
                if task_state.get("start"):
                    mins = int(
                        (datetime.now() - task_state["start"]).total_seconds() / 60
                    )
                    elapsed = f" after {mins}m"
                proj_label = f"[{project_name}] " if project_name else ""
                msg = (
                    f"{proj_label}Task cancelled{elapsed}: {reason}\n"
                    f"Task was: {task_description[:100]}"
                )
                await self._send_message(sender, msg)
                logger.info(
                    "background_task_cancelled",
                    task=task_description[:50],
                    reason=reason,
                )
            except Exception as e:
                logger.error(
                    "background_task_error", error=str(e), exc_type=type(e).__name__
                )
                await self._send_message(sender, "Task failed due to an internal error.")
            finally:
                self._sender_tasks.pop((sender, project_name or ""), None)

        task_state["task"] = asyncio.create_task(run_task())
        logger.info("background_task_started", task=task_description[:50], sender=sender)

    async def cancel_current_task(self, sender: str, project_name: Optional[str] = None) -> str:
        """Cancel the currently running task for this sender.

        Args:
            sender: Phone number of the requesting user.
            project_name: Currently selected project name.

        Returns:
            User-facing status message.
        """
        task_state = self._sender_tasks.get((sender, project_name or ""))
        if not task_state or not task_state.get("task") or task_state["task"].done():
            return "No task is currently running."

        task_desc = task_state.get("description", "unknown")
        elapsed = ""
        if task_state.get("start"):
            mins = int(
                (datetime.now() - task_state["start"]).total_seconds() / 60
            )
            elapsed = f" after {mins}m"

        task_state["cancel_reason"] = "user cancel"
        task_state["task"].cancel()
        await self.runner.cancel()

        logger.info("task_cancelled_by_user", task=task_desc[:50], sender=sender)
        return f"Cancelled{elapsed}: {task_desc[:100]}"

    async def cancel_all_tasks(self, reason: str = "service shutting down") -> None:
        """Cancel all pending background tasks during shutdown.

        Sets cancel_reason on each task before cancelling so the
        CancelledError handler can notify the user with context.
        Used by bot.stop() to drain tasks before closing the HTTP
        session.

        Args:
            reason: Reason string stored on each task's cancel_reason.
        """
        for key, task_state in list(self._sender_tasks.items()):
            task = task_state.get("task")
            if task and not task.done():
                task_state["cancel_reason"] = reason
                task.cancel()
        pending = [
            s["task"]
            for s in self._sender_tasks.values()
            if s.get("task") and not s["task"].done()
        ]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._sender_tasks.clear()

    def save_interrupted_tasks(self, data_dir: Path) -> None:
        """Persist in-flight tasks to JSON so users can be notified on restart.

        Args:
            data_dir: Directory to write interrupted_tasks.json into.
        """
        active = []
        for (sender, proj), state in self._sender_tasks.items():
            task = state.get("task")
            if task and not task.done():
                active.append({
                    "sender": sender,
                    "project": proj,
                    "description": state.get("description", ""),
                    "step": state.get("step", ""),
                })
        if not active:
            return
        data_dir.mkdir(parents=True, exist_ok=True)
        target = data_dir / "interrupted_tasks.json"
        import tempfile
        fd, tmp = tempfile.mkstemp(
            suffix=".json", dir=str(data_dir),
        )
        try:
            import os
            with os.fdopen(fd, "w") as f:
                json.dump(active, f)
            os.replace(tmp, str(target))
            logger.info("interrupted_tasks_saved", count=len(active))
        except Exception as e:
            logger.warning("interrupted_tasks_save_failed", error=str(e))

    async def notify_interrupted_tasks(self, data_dir: Path) -> None:
        """Read interrupted_tasks.json and notify users on startup.

        Args:
            data_dir: Directory containing interrupted_tasks.json.
        """
        target = data_dir / "interrupted_tasks.json"
        if not target.exists():
            return
        try:
            tasks = json.loads(target.read_text())
            for t in tasks:
                sender = t.get("sender", "")
                proj = t.get("project", "")
                desc = t.get("description", "unknown")
                step = t.get("step", "")
                proj_label = f"[{proj}] " if proj else ""
                msg = (
                    f"{proj_label}Service was restarted while a task was running.\n"
                    f"Interrupted task: {desc[:100]}\n"
                    f"Last step: {step[:100]}\n"
                    "You may need to re-run this task."
                )
                await self._send_message(sender, msg)
            target.unlink(missing_ok=True)
            logger.info("interrupted_tasks_notified", count=len(tasks))
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning("interrupted_tasks_read_failed", error=str(e))
            target.unlink(missing_ok=True)
        except Exception as e:
            logger.warning("interrupted_tasks_notify_error", error=str(e))

    def get_all_tasks_for_sender(self, sender: str) -> Dict[str, dict]:
        """Get all active tasks for a sender, keyed by project_name."""
        return {
            proj: state for (s, proj), state in self._sender_tasks.items()
            if s == sender and state.get("task") and not state["task"].done()
        }

    def start_prd_creation_task(
        self, sender: str, task_description: str, project_name: Optional[str] = None,
    ) -> None:
        """Start PRD creation in the background (non-blocking)."""
        task_state = {
            "description": f"Creating PRD: {task_description[:50]}...",
            "start": datetime.now(),
            "step": "Initializing...",
            "task": None,
        }
        self._sender_tasks[(sender, project_name or "")] = task_state

        async def run_prd_creation():
            try:
                result = await self.create_autonomous_prd(sender, task_description)
                await self._send_message(sender, result)
            except asyncio.CancelledError:
                await self._send_message(sender, "PRD creation cancelled.")
                logger.info("prd_creation_cancelled")
            except Exception as e:
                logger.error(
                    "prd_creation_error", error=str(e), exc_type=type(e).__name__
                )
                await self._send_message(
                    sender, "PRD creation failed. Check logs for details."
                )
            finally:
                self._sender_tasks.pop((sender, project_name or ""), None)

        task_state["task"] = asyncio.create_task(run_prd_creation())
        logger.info("prd_creation_started", task=task_description[:50], sender=sender)

    async def create_autonomous_prd(
        self, sender: str, task_description: str
    ) -> str:
        """Create a PRD with stories and tasks via Claude.

        Tries structured SDK output first (PRDBreakdown model),
        falls back to text mode + parse_prd_json on failure.

        Args:
            sender: Phone number of the requesting user.
            task_description: High-level feature description.

        Returns:
            User-facing summary of the created PRD.

        Raises:
            json.JSONDecodeError: If JSON parsing fails.
            ValueError: If breakdown is incomplete.
        """
        project_name = self.project_manager.get_current_project(sender)
        project_path = self.project_manager.get_current_path(sender)

        async def update_step(step: str, notify: bool = True):
            task_state = self._sender_tasks.get((sender, project_name or ""))
            if task_state:
                task_state["step"] = step
            if notify:
                await self._send_message(sender, step)

        await update_step("Analyzing task complexity...")

        from .autonomous.models import PRDBreakdown

        # Prompt describes WHAT to generate; API json_schema enforces HOW
        structured_prompt = f"""Analyze this task request and break it \
into a structured PRD (Product Requirements Document).

TASK REQUEST:
{task_description}

PROJECT: {project_name}

RULES:
1. Break into logical stories (features/components)
2. Each story should have 2-5 focused tasks
3. Tasks should be atomic - completable in one Claude session
4. Higher priority number = executed first
5. Order tasks by dependency (foundations first)
6. Include a final "Testing & Deployment" story if mentioned
7. Be specific in task descriptions - mention exact files/components
8. Keep tasks focused - if a task is too big, split it"""

        # Fallback prompt with explicit JSON format (used when structured fails)
        fallback_prompt = structured_prompt + """

Return a JSON structure with this EXACT format (no markdown, just JSON):
{
    "prd_title": "Brief title for the PRD",
    "prd_description": "One paragraph summary",
    "stories": [
        {
            "title": "Story title",
            "description": "What this story accomplishes",
            "tasks": [
                {
                    "title": "Task title",
                    "description": "Detailed task description",
                    "priority": 10
                }
            ]
        }
    ]
}

Return ONLY valid JSON, no markdown code blocks, no explanation."""

        try:
            # Primary: structured output via SDK
            await update_step("Breaking down task (structured)...")
            success, result = await self.runner.run_claude_structured(
                structured_prompt,
                response_model=PRDBreakdown,
                timeout=self.config.claude_timeout,
                project_path=project_path,
            )

            # Record structured call usage
            await self._record_usage(
                phone_number=sender,
                project_name=project_name,
                source="complex",
                usage_data=self.runner.last_usage,
            )

            if success and isinstance(result, PRDBreakdown):
                # Structured path — typed model access
                await update_step("Creating PRD structure...", notify=False)
                return await self._create_prd_from_breakdown(
                    sender, project_name, result, update_step,
                )

            # Fallback: text mode + parse_prd_json
            logger.info("prd_structured_fallback", reason=str(result)[:200])
            await update_step("Retrying with text mode...")
            success, response = await self.runner.run_claude(
                fallback_prompt,
                timeout=self.config.claude_timeout,
                project_path=project_path,
            )
            # Record fallback call usage
            await self._record_usage(
                phone_number=sender,
                project_name=project_name,
                source="complex",
                usage_data=self.runner.last_usage,
            )

            if not success:
                logger.error("prd_analyze_failed", response=response[:200])
                return "Failed to analyze task."

            await update_step("Parsing task breakdown...", notify=False)
            breakdown = await parse_prd_json(
                response, self.runner, update_step,
            )

            await update_step("Creating PRD structure...", notify=False)
            return await self._create_prd_from_dict(
                sender, project_name, breakdown, update_step,
            )

        except (json.JSONDecodeError, ValueError) as e:
            logger.error(
                "prd_json_parse_error", error=str(e), exc_type=type(e).__name__
            )
            return "Failed to parse the task breakdown. Please try again."
        except KeyError as e:
            logger.error("prd_missing_field", error=str(e))
            return "Task breakdown was incomplete. Please try again."
        except Exception as e:
            logger.error(
                "prd_creation_error", error=str(e), exc_type=type(e).__name__
            )
            return "PRD creation failed. Please try again or check logs."

    async def _create_prd_from_breakdown(
        self, sender, project_name, breakdown, update_step,
    ) -> str:
        """Create PRD/stories/tasks from a typed PRDBreakdown model."""
        prd = await self.autonomous_manager.create_prd(
            phone_number=sender,
            project_name=project_name,
            title=breakdown.prd_title,
            description=breakdown.prd_description,
        )

        total_tasks = 0
        story_summaries = []

        for story_idx, story_bd in enumerate(breakdown.stories, 1):
            await update_step(
                f"Creating story {story_idx}/{len(breakdown.stories)}...",
                notify=False,
            )
            story = await self.autonomous_manager.create_story(
                prd_id=prd.id,
                phone_number=sender,
                title=story_bd.title,
                description=story_bd.description,
            )

            for task_bd in story_bd.tasks:
                await self.autonomous_manager.create_task(
                    story_id=story.id,
                    phone_number=sender,
                    project_name=project_name,
                    title=task_bd.title,
                    description=task_bd.description,
                    priority=task_bd.priority,
                )
                total_tasks += 1

            story_summaries.append(
                f"  - {story.title} ({len(story_bd.tasks)} tasks)"
            )

        return await self._finalize_prd(
            prd, total_tasks, story_summaries, update_step,
        )

    async def _create_prd_from_dict(
        self, sender, project_name, breakdown, update_step,
    ) -> str:
        """Create PRD/stories/tasks from a parsed dict (fallback path)."""
        prd = await self.autonomous_manager.create_prd(
            phone_number=sender,
            project_name=project_name,
            title=breakdown["prd_title"],
            description=breakdown["prd_description"],
        )

        total_tasks = 0
        story_summaries = []
        total_stories = len(breakdown.get("stories", []))

        for story_idx, story_data in enumerate(
            breakdown.get("stories", []), 1,
        ):
            await update_step(
                f"Creating story {story_idx}/{total_stories}...",
                notify=False,
            )
            story = await self.autonomous_manager.create_story(
                prd_id=prd.id,
                phone_number=sender,
                title=story_data["title"],
                description=story_data["description"],
            )

            task_count = 0
            for task_data in story_data.get("tasks", []):
                await self.autonomous_manager.create_task(
                    story_id=story.id,
                    phone_number=sender,
                    project_name=project_name,
                    title=task_data["title"],
                    description=task_data["description"],
                    priority=task_data.get("priority", 5),
                )
                task_count += 1
                total_tasks += 1

            story_summaries.append(
                f"  - {story.title} ({task_count} tasks)"
            )

        return await self._finalize_prd(
            prd, total_tasks, story_summaries, update_step,
        )

    async def _finalize_prd(
        self, prd, total_tasks, story_summaries, update_step,
    ) -> str:
        """Queue tasks and return summary (shared by both paths)."""
        await update_step("Queuing tasks for execution...")

        await self.autonomous_manager.queue_prd(prd.id)

        status = await self.autonomous_manager.get_loop_status()
        if not status.is_running:
            await self.autonomous_manager.start_loop()

        loop_state = "Started" if not status.is_running else "Running"
        return (
            f"PRD #{prd.id}: {prd.title}\n\n"
            f"Stories:\n" + "\n".join(story_summaries) + "\n\n"
            f"{total_tasks} tasks queued | Loop: {loop_state}\n"
            f"Use /tasks or /autonomous status to monitor."
        )
