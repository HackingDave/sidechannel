"""Core command handler for Nightwire bot.

Handles: help, projects, select, status, add, remove, new, ask, do,
complex, cancel, summary, cooldown, update, nightwire/sidechannel, global.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import List, Optional

import structlog

from .base import BaseCommandHandler, HelpMetadata

logger = structlog.get_logger("nightwire.bot")


async def get_memory_context(
    memory, config, project_manager,
    sender: str, query: str, project_name: Optional[str] = None,
) -> Optional[str]:
    """Retrieve relevant past conversations for Claude prompts.

    Standalone function (not a method) to avoid circular dependency
    between CoreCommandHandler and TaskManager.
    """
    if project_name is None:
        project_name = project_manager.get_current_project(sender)
    try:
        context = await memory.get_relevant_context(
            phone_number=sender,
            query=query,
            project_name=project_name,
            max_results=5,
            max_tokens=config.memory_max_context_tokens,
        )
        return context if context else None
    except Exception as e:
        logger.warning("memory_context_error", error=str(e))
        return None


class CoreCommandHandler(BaseCommandHandler):
    """Handles core bot commands."""

    def get_commands(self):
        return {
            "help": self.handle_help,
            "projects": self.handle_projects,
            "select": self.handle_select,
            "status": self.handle_status,
            "add": self.handle_add,
            "remove": self.handle_remove,
            "new": self.handle_new,
            "ask": self.handle_ask,
            "do": self.handle_do,
            "complex": self.handle_complex,
            "cancel": self.handle_cancel,
            "summary": self.handle_summary,
            "cooldown": self.handle_cooldown,
            "update": self.handle_update,
            "nightwire": self.handle_nightwire,
            "sidechannel": self.handle_nightwire,
            "global": self.handle_global,
            "diagnose": self.handle_diagnose,
            "usage": self.handle_usage,
        }

    def get_help_metadata(self):
        return {
            "help": HelpMetadata(
                "Show available commands",
                "/help [command]",
                ["/help", "/help select"],
            ),
            "projects": HelpMetadata(
                "List all registered projects",
                "/projects",
                ["/projects"],
            ),
            "select": HelpMetadata(
                "Select a project to work on",
                "/select <project_name>",
                ["/select myproject"],
            ),
            "status": HelpMetadata(
                "Show current project, task, and system status",
                "/status",
                ["/status"],
            ),
            "add": HelpMetadata(
                "Register an existing directory as a project",
                "/add <name> [path] [description]",
                ["/add myproject", "/add myproject /home/user/code"],
            ),
            "remove": HelpMetadata(
                "Remove a project from the registry",
                "/remove <project_name>",
                ["/remove myproject"],
            ),
            "new": HelpMetadata(
                "Create a new project directory and register it",
                "/new <name> [description]",
                ["/new myproject", "/new myproject A REST API"],
            ),
            "ask": HelpMetadata(
                "Ask Claude a question about the current project",
                "/ask <question>",
                ["/ask How does auth work?"],
            ),
            "do": HelpMetadata(
                "Execute a coding task with Claude",
                "/do <task>",
                ["/do Add input validation to login"],
            ),
            "complex": HelpMetadata(
                "Break a large task into PRD with autonomous tasks",
                "/complex <task>",
                ["/complex Build user management API"],
            ),
            "cancel": HelpMetadata(
                "Cancel the currently running background task",
                "/cancel",
                ["/cancel"],
            ),
            "summary": HelpMetadata(
                "Generate a project summary",
                "/summary",
                ["/summary"],
            ),
            "cooldown": HelpMetadata(
                "Check or control rate-limit cooldown",
                "/cooldown [status|clear|test]",
                ["/cooldown", "/cooldown clear", "/cooldown test"],
            ),
            "update": HelpMetadata(
                "Apply a pending software update (admin only)",
                "/update",
                ["/update"],
            ),
            "nightwire": HelpMetadata(
                "Ask the optional AI assistant",
                "/nightwire <question>",
                ["/nightwire What is the capital of France?"],
            ),
            "global": HelpMetadata(
                "Run memory commands in cross-project scope",
                "/global <command> [args]",
                ["/global recall database", "/global memories"],
            ),
            "diagnose": HelpMetadata(
                "Run health checks on all dependencies",
                "/diagnose",
                ["/diagnose"],
            ),
            "usage": HelpMetadata(
                "View token usage and cost tracking",
                "/usage [project|all]",
                ["/usage", "/usage project", "/usage all"],
            ),
        }

    # --- Core commands ---

    async def handle_help(self, sender: str, args: str) -> str:
        """Show available commands, per-command help, and setup status.

        Signal usage::

            /help
            /help select
            /help diagnose

        Args:
            sender: Phone number or UUID of the message sender.
            args: Optional command name for detailed help.

        Returns:
            General help text, or detailed per-command help.
        """
        cmd = args.strip().lower() if args else ""
        if cmd:
            return self._get_command_help(cmd)
        text = self._build_help_text()
        text += "\n\n" + self._build_setup_status()
        return text

    async def handle_projects(self, sender: str, args: str) -> str:
        """List all registered projects and highlight the currently selected one.

        Signal usage::

            /projects

        Args:
            sender: Phone number or UUID of the message sender.
            args: Unused.

        Returns:
            Formatted project list with the active project marked.
        """
        return self.ctx.project_manager.list_projects(sender)

    async def handle_select(self, sender: str, args: str) -> str:
        """Select a project to work on. Sets the runner's working directory.

        Signal usage::

            /select myproject

        Args:
            sender: Phone number or UUID of the message sender.
            args: Project name to select.

        Returns:
            Success or error message.
        """
        if not args:
            return "Usage: /select <project_name>"
        success, msg = self.ctx.project_manager.select_project(args, sender)
        if success:
            self.ctx.runner.set_project(
                self.ctx.project_manager.get_current_path(sender)
            )
        return msg

    async def handle_status(self, sender: str, args: str) -> str:
        """Show current project, active task, autonomous loop, and cooldown state.

        Signal usage::

            /status

        Args:
            sender: Phone number or UUID of the message sender.
            args: Unused.

        Returns:
            Multi-section status string with project, task, loop, and cooldown info.
        """
        project_name = self.ctx.project_manager.get_current_project(sender)
        status = self.ctx.project_manager.get_status(sender)

        # Add running task info for current project
        task_state = self.ctx.task_manager.get_task_state(sender, project_name)
        if task_state and task_state.get("task") and not task_state["task"].done():
            elapsed = ""
            if task_state.get("start"):
                mins = int(
                    (datetime.now() - task_state["start"]).total_seconds() / 60
                )
                elapsed = f" ({mins}m)"
            desc = task_state.get("description", "unknown")[:120]
            status += f"\n\nActive Task{elapsed}: {desc}"
            if task_state.get("step"):
                status += f"\nStep: {task_state['step']}"

        # Show tasks running on other projects
        all_tasks = self.ctx.task_manager.get_all_tasks_for_sender(sender)
        other_tasks = {
            proj: state for proj, state in all_tasks.items()
            if proj != (project_name or "")
        }
        if other_tasks:
            status += "\n\nOther Active Tasks:"
            for proj, state in other_tasks.items():
                proj_label = proj if proj else "(no project)"
                desc = state.get("description", "unknown")[:80]
                elapsed = ""
                if state.get("start"):
                    mins = int(
                        (datetime.now() - state["start"]).total_seconds() / 60
                    )
                    elapsed = f" ({mins}m)"
                status += f"\n  [{proj_label}]{elapsed}: {desc}"

        # Add autonomous loop status
        try:
            am = self.ctx.autonomous_manager
            loop_status = await am.get_loop_status()
            if loop_status.is_running:
                auto_info = "\n\nAutonomous Loop: Running"
                if loop_status.current_task_id:
                    current_task = await am.db.get_task(
                        loop_status.current_task_id
                    )
                    if current_task:
                        elapsed_auto = ""
                        if current_task.started_at:
                            mins = int(
                                (datetime.now() - current_task.started_at)
                                .total_seconds() / 60
                            )
                            elapsed_auto = f" ({mins}m)"
                        auto_info += (
                            f"\nCurrent: {current_task.title[:50]}"
                            f"{elapsed_auto}"
                        )
                auto_info += f"\nQueued: {loop_status.tasks_queued}"
                auto_info += f" | Done: {loop_status.tasks_completed_today}"
                if loop_status.tasks_failed_today > 0:
                    auto_info += (
                        f" | Failed: {loop_status.tasks_failed_today}"
                    )
                status += auto_info
            elif loop_status.is_paused:
                status += "\n\nAutonomous Loop: Paused"
        except Exception as e:
            logger.warning("status_autonomous_error", error=str(e))

        # Add cooldown info
        if self.ctx.cooldown_active:
            state = self.ctx.cooldown_manager.get_state()
            status += (
                f"\n\nRate Limit Cooldown: Active"
                f" (~{state.remaining_minutes} min remaining)"
            )

        return status

    async def handle_add(self, sender: str, args: str) -> str:
        """Register an existing directory as a project.

        Signal usage::

            /add myproject
            /add myproject /home/user/code/myproject
            /add myproject /path A web application

        Args:
            sender: Phone number or UUID of the message sender.
            args: ``<name> [path] [description]`` — name is required,
                path and description are optional.

        Returns:
            Success or error message.
        """
        if not args:
            return "Usage: /add <project_name> [path] [description]"
        parts = args.split(maxsplit=2)
        name = parts[0]
        path = parts[1] if len(parts) > 1 else None
        desc = parts[2] if len(parts) > 2 else ""
        success, msg = self.ctx.project_manager.add_project(name, path, desc)
        return msg

    async def handle_remove(self, sender: str, args: str) -> str:
        """Remove a project from the registry (does not delete files).

        Signal usage::

            /remove myproject

        Args:
            sender: Phone number or UUID of the message sender.
            args: Project name to unregister.

        Returns:
            Success or error message.
        """
        if not args:
            return "Usage: /remove <project_name>"
        success, msg = self.ctx.project_manager.remove_project(args)
        if success and self.ctx.project_manager.get_current_project(sender) is None:
            self.ctx.runner.set_project(None)
        return msg

    async def handle_new(self, sender: str, args: str) -> str:
        """Create a new project directory and register it.

        Signal usage::

            /new myproject
            /new myproject A REST API backend

        Args:
            sender: Phone number or UUID of the message sender.
            args: ``<name> [description]`` — name is required.

        Returns:
            Success or error message. Auto-selects the new project on success.
        """
        if not args:
            return "Usage: /new <project_name> [description]"
        parts = args.split(maxsplit=1)
        name = parts[0]
        desc = parts[1] if len(parts) > 1 else ""
        success, msg = self.ctx.project_manager.create_project(
            name, sender, desc
        )
        if success:
            self.ctx.runner.set_project(
                self.ctx.project_manager.get_current_path(sender)
            )
        return msg

    async def handle_ask(
        self, sender: str, args: str,
        image_paths: Optional[List[Path]] = None,
    ) -> Optional[str]:
        """Ask Claude a question about the currently selected project.

        Runs in the background. Returns None once the task is started
        (the response is sent asynchronously via send_message).

        Signal usage::

            /ask How does the authentication middleware work?
            /ask What tests cover the payment module?

        Args:
            sender: Phone number or UUID of the message sender.
            args: The question to ask Claude.
            image_paths: Optional list of saved image file paths from
                Signal attachments.

        Returns:
            Error message string, or None if the background task started.
        """
        if not args:
            return "Usage: /ask <question about the project>"
        if self.ctx.cooldown_active:
            return self.ctx.cooldown_manager.get_state().user_message
        current_project = self.ctx.project_manager.get_current_project(sender)
        if not current_project:
            return "No project selected. Use /select <project> first."
        busy = self.ctx.task_manager.check_busy(sender, current_project)
        if busy:
            return busy

        await self.ctx.send_message(sender, "Analyzing project...")
        self.ctx.task_manager.start_background_task(
            sender,
            f"Answer this question about the codebase: {args}",
            current_project,
            image_paths=image_paths,
            source="ask",
        )
        return None

    async def handle_do(
        self, sender: str, args: str,
        image_paths: Optional[List[Path]] = None,
    ) -> Optional[str]:
        """Execute a coding task with Claude on the current project.

        Runs in the background with streaming output. The default handler
        when a message is sent without a command prefix.

        Signal usage::

            /do Add input validation to the login form
            /do Fix the failing test in test_auth.py

        Args:
            sender: Phone number or UUID of the message sender.
            args: Task description for Claude to execute.
            image_paths: Optional list of saved image file paths from
                Signal attachments.

        Returns:
            Error message string, or None if the background task started.
        """
        if not args:
            return "Usage: /do <task to perform>"
        if self.ctx.cooldown_active:
            return self.ctx.cooldown_manager.get_state().user_message
        current_project = self.ctx.project_manager.get_current_project(sender)
        if not current_project:
            return "No project selected. Use /select <project> first."
        busy = self.ctx.task_manager.check_busy(sender, current_project)
        if busy:
            return busy

        await self.ctx.send_message(sender, "Working on it...")
        self.ctx.task_manager.start_background_task(
            sender, args, current_project,
            image_paths=image_paths,
        )
        return None

    async def handle_complex(self, sender: str, args: str) -> Optional[str]:
        """Break a large task into a PRD with stories and autonomous tasks.

        Creates a structured PRD (Product Requirements Document), decomposes
        it into user stories and tasks, then queues them for autonomous execution.

        Signal usage::

            /complex Build a REST API for user management with auth
            /complex Refactor the database layer to use connection pooling

        Args:
            sender: Phone number or UUID of the message sender.
            args: High-level task description to decompose.

        Returns:
            Error message string, or None if the PRD creation task started.
        """
        if not args:
            return (
                "Usage: /complex <task>\n"
                "Breaks task into PRD with stories and autonomous tasks."
            )
        if self.ctx.cooldown_active:
            return self.ctx.cooldown_manager.get_state().user_message
        current_project = self.ctx.project_manager.get_current_project(sender)
        if not current_project:
            return "No project selected. Use /select <project> first."
        busy = self.ctx.task_manager.check_busy(sender, current_project)
        if busy:
            return busy

        await self.ctx.send_message(
            sender, "Creating PRD and breaking into autonomous tasks..."
        )
        self.ctx.task_manager.start_prd_creation_task(sender, args, current_project)
        return None

    async def handle_cancel(self, sender: str, args: str) -> str:
        """Cancel the currently running background task for this sender.

        Signal usage::

            /cancel

        Args:
            sender: Phone number or UUID of the message sender.
            args: Unused.

        Returns:
            Confirmation that the task was cancelled, or a message if no task is running.
        """
        project_name = self.ctx.project_manager.get_current_project(sender)
        return await self.ctx.task_manager.cancel_current_task(sender, project_name)

    async def handle_summary(self, sender: str, args: str) -> Optional[str]:
        """Generate a comprehensive summary of the current project.

        Asks Claude to describe the project structure, technologies, and
        recent git changes. Runs in the background.

        Signal usage::

            /summary

        Args:
            sender: Phone number or UUID of the message sender.
            args: Unused.

        Returns:
            Error message string, or None if the background task started.
        """
        current_project = self.ctx.project_manager.get_current_project(sender)
        if not current_project:
            return "No project selected. Use /select <project> first."
        busy = self.ctx.task_manager.check_busy(sender, current_project)
        if busy:
            return busy

        await self.ctx.send_message(sender, "Generating summary...")
        self.ctx.task_manager.start_background_task(
            sender,
            "Provide a comprehensive summary of this project including "
            "its structure, main technologies used, and any recent changes "
            "visible in git history.",
            current_project,
            source="summary",
        )
        return None

    async def handle_cooldown(self, sender: str, args: str) -> str:
        """Check, clear, or test the rate-limit cooldown state.

        Signal usage::

            /cooldown            — Show cooldown status (default)
            /cooldown status     — Same as above
            /cooldown clear      — Manually clear an active cooldown
            /cooldown test       — Activate a 2-minute test cooldown

        Args:
            sender: Phone number or UUID of the message sender.
            args: Subcommand — ``status``, ``clear``, or ``test``.

        Returns:
            Cooldown state description or confirmation of action taken.
        """
        if self.ctx._cooldown_manager is None:
            return "Cooldown manager not initialized."

        subcommand = args.strip().lower() if args else "status"

        if subcommand == "status":
            state = self.ctx.cooldown_manager.get_state()
            if state.active:
                return (
                    f"Cooldown ACTIVE"
                    f" (~{state.remaining_minutes} min remaining)"
                    f"\n{state.user_message}"
                )
            return "No active cooldown. Claude operations are running normally."

        elif subcommand == "clear":
            if not self.ctx.cooldown_manager.is_active:
                return "No active cooldown to clear."
            self.ctx.cooldown_manager.deactivate()
            return "Cooldown cleared. Claude operations resumed."

        elif subcommand == "test":
            if self.ctx.cooldown_manager.is_active:
                return "Cooldown is already active. Use /cooldown clear first."
            self.ctx.cooldown_manager.activate(cooldown_minutes=2)
            return (
                "Test cooldown activated (2 minutes)."
                " Use /cooldown clear to cancel."
            )

        else:
            return "Usage: /cooldown [status|clear|test]"

    async def handle_update(self, sender: str, args: str) -> str:
        """Apply a pending software update (admin only).

        Pulls the latest code from git and restarts. Only the first phone
        number in ``allowed_numbers`` (the admin) can trigger this.

        Signal usage::

            /update

        Args:
            sender: Phone number or UUID of the message sender.
            args: Unused.

        Returns:
            Update result or permission denied message.
        """
        allowed = self.ctx.config.allowed_numbers
        if not allowed or sender != allowed[0]:
            return "Only the admin can trigger updates."
        if self.ctx._updater is None:
            return (
                "Auto-update is not enabled."
                " Set auto_update.enabled: true in settings.yaml."
            )
        return await self.ctx.updater.apply_update()

    async def handle_nightwire(self, sender: str, args: str) -> str:
        """Ask the optional AI assistant (OpenAI/Grok-compatible provider).

        Aliases: ``/nightwire`` and ``/sidechannel`` both route here.
        Requires ``nightwire_assistant.enabled: true`` in settings.yaml.

        Signal usage::

            /nightwire What is the capital of France?
            /sidechannel Explain quantum computing briefly

        Args:
            sender: Phone number or UUID of the message sender.
            args: Question or prompt for the assistant.

        Returns:
            Assistant response text or error message.
        """
        if not self.ctx.nightwire_runner:
            return (
                "nightwire assistant is not enabled."
                " Set nightwire_assistant.enabled: true in settings.yaml"
                " and provide OPENAI_API_KEY or GROK_API_KEY."
            )
        if not args:
            return "Usage: /nightwire <question>\nAsk the AI assistant anything."
        response = await self._nightwire_response(args)
        # Record NightwireRunner usage
        if self.ctx.nightwire_runner and self.ctx.nightwire_runner.last_usage:
            try:
                project = self.ctx.project_manager.get_current_project(sender)
                await self.ctx.task_manager._record_usage(
                    phone_number=sender,
                    project_name=project,
                    source="nightwire",
                    usage_data=self.ctx.nightwire_runner.last_usage,
                )
            except Exception:
                pass  # Non-critical
        return response

    async def handle_global(self, sender: str, args: str) -> str:
        """Run memory commands in cross-project (global) scope.

        Wraps memory commands to bypass the per-project scoping, so
        memories, recall, and history span all projects.

        Signal usage::

            /global remember Always use UTC timestamps
            /global recall database migration
            /global memories
            /global history 20

        Args:
            sender: Phone number or UUID of the message sender.
            args: ``<subcommand> [args]`` — one of remember, recall,
                memories, or history.

        Returns:
            Result from the underlying memory command, or usage help.
        """
        if not args.strip():
            return (
                "Usage: /global <command> <args>\n\n"
                "  remember <text> - Store a global memory\n"
                "  recall <query> - Search all projects\n"
                "  memories - List all memories\n"
                "  history [count] - History across projects"
            )

        parts = args.strip().split(maxsplit=1)
        subcommand = parts[0].lower()
        subargs = parts[1] if len(parts) > 1 else ""
        mc = self.ctx.memory_commands

        if subcommand == "remember":
            return await mc.handle_remember(sender, subargs, project=None)
        elif subcommand == "recall":
            return await mc.handle_recall(sender, subargs, project=None)
        elif subcommand == "memories":
            return await mc.handle_memories(sender, subargs, project=None)
        elif subcommand == "history":
            return await mc.handle_history(sender, subargs, project=None)
        else:
            return (
                f"Unknown global command: {subcommand}\n\nUse /global for help."
            )

    async def handle_diagnose(self, sender: str, args: str) -> str:
        """Run health checks on all subsystem dependencies.

        Signal usage::

            /diagnose

        Args:
            sender: Phone number or UUID of the message sender.
            args: Unused.

        Returns:
            Formatted diagnostic results with pass/fail indicators.
        """
        from ..diagnostics import run_all_checks

        results = await run_all_checks(self.ctx.config)
        lines = ["Diagnostics:"]
        for name, (ok, detail, hint) in results.items():
            icon = "+" if ok else "-"
            line = f"  {icon} {name}: {detail}"
            if not ok and hint:
                line += f"\n      Hint: {hint}"
            lines.append(line)
        return "\n".join(lines)

    async def handle_usage(self, sender: str, args: str) -> str:
        """View token usage and cost tracking.

        Shows the requesting user's own usage summary by default.
        ``/usage project`` shows per-project breakdown.
        ``/usage all`` shows all users (admin only).

        Signal usage::

            /usage                — Your usage today/this week/all time
            /usage project        — Per-project breakdown
            /usage all            — All users (admin only)

        Args:
            sender: Phone number or UUID of the message sender.
            args: Optional subcommand (project, all).

        Returns:
            Formatted usage summary.
        """
        subcommand = args.strip().lower() if args else ""

        if subcommand == "all":
            # Admin-only: all users
            allowed = self.ctx.config.allowed_numbers
            if not allowed or sender not in allowed[:1]:
                return "Only the admin can view all-user usage."
            rows = await self.ctx.task_manager.memory.db.get_usage_all_users()
            if not rows:
                return "No usage data recorded yet."
            lines = ["Usage — All Users:"]
            for row in rows:
                phone = row["phone_number"]
                masked = f"...{phone[-4:]}" if len(phone) >= 4 else phone
                total_tokens = row["input_tokens"] + row["output_tokens"]
                cost = row["cost_usd"]
                lines.append(
                    f"  {masked}: {total_tokens:,} tokens, ${cost:.4f}"
                )
            return "\n".join(lines)

        if subcommand == "project":
            rows = await self.ctx.task_manager.memory.db.get_usage_by_project(
                sender
            )
            if not rows:
                return "No usage data recorded yet."
            lines = ["Usage — By Project:"]
            for row in rows:
                proj = row["project_name"] or "(no project)"
                total_tokens = row["input_tokens"] + row["output_tokens"]
                cost = row["cost_usd"]
                lines.append(
                    f"  {proj}: {total_tokens:,} tokens, ${cost:.4f}"
                )
            return "\n".join(lines)

        # Default: user's own summary (today, this week, all time)
        today_row = await self.ctx.task_manager.memory.db.get_usage_summary(
            sender, days=0
        )
        week_row = await self.ctx.task_manager.memory.db.get_usage_summary(
            sender, days=7
        )
        all_row = await self.ctx.task_manager.memory.db.get_usage_summary(
            sender
        )

        def fmt(row):
            if not row or (row["input_tokens"] == 0 and row["output_tokens"] == 0):
                return "0 tokens, $0.0000"
            total = row["input_tokens"] + row["output_tokens"]
            return f"{total:,} tokens, ${row['cost_usd']:.4f}"

        lines = [
            "Usage Summary:",
            f"  Today: {fmt(today_row)}",
            f"  This week: {fmt(week_row)}",
            f"  All time: {fmt(all_row)}",
        ]
        return "\n".join(lines)

    # --- Helper methods ---

    def _is_nightwire_query(self, message: str) -> bool:
        """Detect if a message is addressed to nightwire assistant."""
        if not self.ctx.nightwire_runner:
            return False
        msg_lower = message.lower().strip()
        for prefix in ("nightwire:", "nightwire,", "sidechannel:", "sidechannel,"):
            if msg_lower.startswith(prefix):
                return True
        for prefix in ("nightwire ", "sidechannel "):
            if msg_lower.startswith(prefix) and len(msg_lower) > len(prefix):
                return True
        if msg_lower in ("nightwire", "sidechannel"):
            return True
        return False

    async def _nightwire_response(self, message: str) -> str:
        """Generate a nightwire response using the configured provider."""
        if not self.ctx.nightwire_runner:
            return (
                "nightwire assistant is not enabled."
                " Set nightwire_assistant.enabled: true in settings.yaml"
                " and provide OPENAI_API_KEY or GROK_API_KEY."
            )
        try:
            logger.info("nightwire_query", length=len(message))
            success, response = await self.ctx.nightwire_runner.ask(message)
            if not response or not response.strip():
                logger.warning("nightwire_empty_response")
                return "The assistant returned an empty response. Please try again."
            return response
        except Exception as e:
            logger.error(
                "nightwire_response_error",
                error=str(e), exc_type=type(e).__name__,
            )
            return "The assistant encountered an error. Please try again later."

    def _get_command_help(self, command: str) -> str:
        """Return detailed help for a specific command."""
        # Strip leading / if user typed /help /select
        command = command.lstrip("/")
        registry = self.ctx.registry
        if registry:
            meta = registry.get_help(command)
            if meta:
                lines = [
                    f"/{command} — {meta.description}",
                    f"\nUsage: {meta.usage}",
                ]
                if meta.examples:
                    lines.append("\nExamples:")
                    for ex in meta.examples:
                        lines.append(f"  {ex}")
                return "\n".join(lines)
            # Command exists but no HelpMetadata (external command)
            handler = registry.get(command)
            if handler:
                return (
                    f"/{command} — Use /help for the full command list."
                )
        return f"Unknown command: /{command}\nUse /help to see available commands."

    def _build_setup_status(self) -> str:
        """Build a lightweight setup status section (sync, no network I/O)."""
        lines = ["Status:"]
        # Claude CLI — just check if path is configured or found
        import shutil

        claude_found = bool(shutil.which(self.ctx.config.claude_path))
        if claude_found:
            lines.append("  + Claude CLI: configured")
        else:
            lines.append("  - Claude CLI: not found")
        # Memory/embeddings — check if package is findable (no actual import)
        import importlib.util

        if importlib.util.find_spec("sentence_transformers"):
            lines.append("  + Embeddings: available")
        else:
            lines.append("  - Embeddings: not installed")
        # sqlite-vec
        if importlib.util.find_spec("sqlite_vec"):
            lines.append("  + sqlite-vec: available")
        else:
            lines.append("  - sqlite-vec: not installed")
        # Nightwire assistant
        if self.ctx.nightwire_runner:
            lines.append("  + Nightwire Assistant: enabled")
        else:
            lines.append("  - Nightwire Assistant: not configured")
        # Docker sandbox
        if self.ctx.config.sandbox_enabled:
            lines.append("  + Docker Sandbox: enabled")
        else:
            lines.append("  - Docker Sandbox: disabled")
        return "\n".join(lines)

    def _build_help_text(self) -> str:
        """Build the complete help text."""
        help_text = """nightwire Commands:

Project Management:
  /projects - List available projects
  /select <project> - Select a project
  /add <name> [path] [desc] - Add existing project
  /remove <project> - Remove a project from the list
  /new <name> [desc] - Create new project
  /status - Show current project and task status
  /summary - Generate project summary

Claude Tasks:
  /ask <question> - Ask about the current project
  /do <task> - Execute a task with Claude
  /complex <task> - Break into PRD with autonomous tasks
  /cancel - Stop the running task

Autonomous System:
  /prd <title> - Create a Product Requirements Doc
  /story <prd_id> <title> | <desc> - Add a user story
  /task <story_id> <title> | <desc> - Add a task
  /tasks [status] - List tasks
  /queue story|prd <id> - Queue tasks for execution
  /autonomous status|start|pause|stop - Control the loop
  /learnings [search] - View or search learnings

Memory:
  /remember <text> - Store a memory
  /recall <query> - Search past conversations
  /memories - List stored memories
  /history [count] - View recent messages
  /forget all|preferences|today - Delete data
  /preferences - View stored preferences
  /global <cmd> - Cross-project memory commands"""

        help_text += """

System:
  /cooldown [status|clear|test] - Rate limit cooldown info/control
  /update - Apply a pending update (admin only)
  /diagnose - Run health checks on all dependencies"""

        if self.ctx.nightwire_runner:
            help_text = """nightwire Commands:

AI Assistant:
  /nightwire <question> - Ask the AI assistant anything
  Or just: nightwire <question>

""" + help_text[len("nightwire Commands:\n\n"):]

        for section in self.ctx.plugin_loader.get_all_help():
            help_text += f"\n\n{section.title}:"
            for cmd, desc in section.commands.items():
                help_text += f"\n  /{cmd} - {desc}"

        return help_text
