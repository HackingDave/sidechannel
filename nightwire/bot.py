"""Signal bot implementation for nightwire."""

import asyncio
import hashlib
import json
import time as _time
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional
from urllib.parse import urlparse

import aiohttp
import structlog

from .attachments import process_attachments
from .config import get_config
from .security import is_authorized, sanitize_input, check_rate_limit
from .claude_runner import get_runner
from .project_manager import get_project_manager
from .memory import MemoryManager, MemoryCommands
from .autonomous import AutonomousManager, AutonomousCommands
from .plugin_loader import PluginLoader
from .updater import AutoUpdater
from .rate_limit_cooldown import get_cooldown_manager
from .prd_builder import clean_json_string, extract_balanced_json, parse_prd_json

logger = structlog.get_logger()


def _log_task_exception(task: asyncio.Task):
    """Log exceptions from fire-and-forget tasks instead of silently swallowing them."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc:
        logger.error("background_task_failed", error=str(exc), exc_type=type(exc).__name__)


class SignalBot:
    """Signal bot that interfaces with Claude."""

    def __init__(self):
        self.config = get_config()
        self.runner = get_runner()
        self.project_manager = get_project_manager()

        # nightwire assistant runner is optional (supports OpenAI and Grok providers)
        self.nightwire_runner = None
        if self.config.nightwire_assistant_enabled:
            try:
                from .nightwire_runner import NightwireRunner
                self.nightwire_runner = NightwireRunner(
                    api_url=self.config.nightwire_assistant_api_url,
                    api_key=self.config.nightwire_assistant_api_key,
                    model=self.config.nightwire_assistant_model,
                    max_tokens=self.config.nightwire_assistant_max_tokens,
                )
                logger.info(
                    "nightwire_runner_initialized",
                    provider=self.config.nightwire_assistant_provider,
                    model=self.config.nightwire_assistant_model,
                )
            except Exception as e:
                logger.warning("nightwire_runner_unavailable", error=str(e))

        self.session: Optional[aiohttp.ClientSession] = None
        self.running = False
        self.account: Optional[str] = None
        self._shutdown_callback: Optional[callable] = None  # Set by main.py
        self.restart_exit_code: Optional[int] = None  # Non-None = exit with this code after stop
        self._processed_messages = OrderedDict()  # Dedup: msg_hash -> timestamp
        self._last_ws_activity = 0.0  # monotonic timestamp of last websocket activity
        self._ws_frames_received = 0  # Total WebSocket frames received (for diagnostics)
        self._watchdog_task: Optional[asyncio.Task] = None
        self._startup_notified = False  # True after "ready" message sent on first WS connect

        # Per-(sender, project) task state tracking - allows concurrent tasks
        # across users AND across different projects for the same user.
        # Key: (sender_phone, project_name), Value: dict with task, description, start, step
        self._sender_tasks: Dict[tuple, dict] = {}

        # File to persist interrupted tasks across restarts
        self._interrupted_tasks_file = Path(self.config.config_dir).parent / "data" / "interrupted_tasks.json"

        # Memory system
        memory_db_path = Path(self.config.config_dir).parent / "data" / "memory.db"
        self.memory = MemoryManager(
            db_path=memory_db_path,
            session_timeout_minutes=self.config.memory_session_timeout,
            max_context_tokens=self.config.memory_max_context_tokens
        )
        self.memory_commands = MemoryCommands(self.memory)

        # Autonomous system (initialized after memory in start())
        self.autonomous_manager: Optional[AutonomousManager] = None
        self.autonomous_commands: Optional[AutonomousCommands] = None

        # Auto-updater (initialized in start() if enabled)
        self.updater: Optional[AutoUpdater] = None

    def set_shutdown_callback(self, callback: callable):
        """Set the callback that triggers graceful shutdown (called from main.py)."""
        self._shutdown_callback = callback

        # Cooldown manager (initialized in start())
        self.cooldown_manager = None

        # Plugin system
        plugins_data_dir = Path(self.config.config_dir).parent / "data" / "plugins"
        plugins_data_dir.mkdir(parents=True, exist_ok=True)
        self.plugin_loader = PluginLoader(
            plugins_dir=self.config.plugins_dir,
            settings=self.config.settings,
            send_message=self._send_message,
            allowed_numbers=self.config.allowed_numbers,
            data_dir=plugins_data_dir,
        )
        self.plugin_loader.discover_and_load()

    async def start(self):
        """Start the bot."""
        self.session = aiohttp.ClientSession()
        self.running = True

        # Warn if non-localhost Signal API is not using HTTPS
        parsed = urlparse(self.config.signal_api_url)
        if parsed.hostname not in ("127.0.0.1", "localhost", "::1") and parsed.scheme != "https":
            logger.warning("insecure_signal_api_url", url=self.config.signal_api_url,
                           msg="Non-localhost Signal API should use HTTPS")

        # Get the registered account (also runs signal-api health check)
        await self._get_account()
        await self._check_signal_api_health()

        # Initialize memory system
        await self.memory.initialize()

        # Initialize autonomous system (uses same DB connection)
        async def autonomous_notify(phone: str, message: str):
            await self._send_message(phone, message)

        self.autonomous_manager = AutonomousManager(
            db_connection=self.memory.db._conn,
            progress_callback=autonomous_notify,
            poll_interval=self.config.autonomous_poll_interval,
            run_quality_gates=self.config.autonomous_quality_gates,
            max_parallel=self.config.autonomous_max_parallel,
        )
        self.autonomous_commands = AutonomousCommands(
            manager=self.autonomous_manager,
            get_current_project=lambda phone: (
                self.project_manager.get_current_project(phone),
                self.project_manager.get_current_path(phone),
            ),
        )

        # Start plugins
        await self.plugin_loader.start_all()

        # Start auto-updater if enabled
        if self.config.auto_update_enabled:
            self.updater = AutoUpdater(
                config=self.config,
                send_message=self._send_message,
                shutdown_callback=self._shutdown_callback,
            )
            await self.updater.start()

        # Initialize rate-limit cooldown manager
        self.cooldown_manager = get_cooldown_manager()

        async def _cooldown_on_activate():
            """Pause autonomous loop and notify users on cooldown."""
            if self.autonomous_manager:
                await self.autonomous_manager.pause_loop()
            state = self.cooldown_manager.get_state()
            for phone in self.config.allowed_numbers:
                try:
                    await self._send_message(
                        phone,
                        f"Rate limit cooldown activated. {state.user_message}"
                    )
                except Exception as e:
                    logger.warning("cooldown_notify_error", error=str(e))

        async def _cooldown_on_deactivate():
            """Resume autonomous loop and notify users when cooldown ends."""
            if self.autonomous_manager:
                await self.autonomous_manager.start_loop()
            for phone in self.config.allowed_numbers:
                try:
                    await self._send_message(
                        phone,
                        "Rate limit cooldown expired. Claude operations resumed."
                    )
                except Exception as e:
                    logger.warning("cooldown_notify_error", error=str(e))

        self.cooldown_manager.on_activate(_cooldown_on_activate)
        self.cooldown_manager.on_deactivate(_cooldown_on_deactivate)

        # Start health watchdog
        self._watchdog_task = asyncio.create_task(self._health_watchdog())

        logger.info("bot_started", account=self.account)

        # Notify users about tasks interrupted by previous shutdown/restart
        await self._notify_interrupted_tasks()

    # How long to wait for Claude to finish during shutdown before force-killing
    SHUTDOWN_GRACE_SECONDS = 90

    async def stop(self):
        """Stop the bot, waiting for in-flight Claude responses before shutting down."""
        if not self.running:
            return
        self.running = False
        # Cancel watchdog
        if self._watchdog_task and not self._watchdog_task.done():
            self._watchdog_task.cancel()
            try:
                await self._watchdog_task
            except asyncio.CancelledError:
                pass
        # Stop plugins
        await self.plugin_loader.stop_all()
        if self.cooldown_manager:
            self.cooldown_manager.cancel_timer()
        if self.updater:
            await self.updater.stop()
        if self.autonomous_manager:
            await self.autonomous_manager.stop_loop()
        if self.nightwire_runner:
            await self.nightwire_runner.close()

        # --- Graceful shutdown: let in-flight tasks finish before killing ---
        active_tasks = [
            (key, s) for key, s in self._sender_tasks.items()
            if s.get("task") and not s["task"].done()
        ]
        if active_tasks:
            task_names = [f"{k[1]}:{s.get('description', '?')[:40]}" for k, s in active_tasks]
            logger.info("shutdown_waiting_for_tasks",
                        count=len(active_tasks), tasks=task_names,
                        grace_seconds=self.SHUTDOWN_GRACE_SECONDS)

            # Wait for tasks to finish naturally (Claude subprocess will
            # complete and the response will be sent to the user)
            pending = [s["task"] for _, s in active_tasks]
            done, still_running = await asyncio.wait(
                pending, timeout=self.SHUTDOWN_GRACE_SECONDS
            )

            if done:
                logger.info("shutdown_tasks_completed", count=len(done))

            if still_running:
                # Grace period expired — force-kill remaining Claude processes
                # and cancel the tasks that didn't finish in time
                logger.warning("shutdown_grace_expired",
                               still_running=len(still_running))
                await self.runner.cancel()
                for key, state in list(self._sender_tasks.items()):
                    task = state.get("task")
                    if task and not task.done():
                        state["cancel_reason"] = "Service is restarting (timed out waiting for response)"
                        task.cancel()
                remaining = [
                    s["task"] for s in self._sender_tasks.values()
                    if s.get("task") and not s["task"].done()
                ]
                if remaining:
                    await asyncio.gather(*remaining, return_exceptions=True)
            else:
                # All tasks finished — cancel Claude processes that may have
                # been started but have no associated task (shouldn't happen,
                # but clean up just in case)
                await self.runner.cancel()
        else:
            # No active tasks — just clean up any orphan processes
            await self.runner.cancel()

        # Save interrupted tasks (only tasks that didn't complete in time)
        await self._save_interrupted_tasks()

        # Now safe to close session and database
        if self.session:
            await self.session.close()
        await self.memory.close()
        logger.info("bot_stopped")

    async def _save_interrupted_tasks(self):
        """Persist in-flight tasks to disk so we can notify users after restart."""
        interrupted = []
        for (sender, project), state in self._sender_tasks.items():
            task = state.get("task")
            if task and not task.done():
                interrupted.append({
                    "sender": sender,
                    "project": project,
                    "description": state.get("description", "unknown"),
                    "start": state["start"].isoformat() if state.get("start") else None,
                    "step": state.get("step", ""),
                    "timestamp": datetime.now().isoformat(),
                })
        if interrupted:
            try:
                self._interrupted_tasks_file.parent.mkdir(parents=True, exist_ok=True)
                self._interrupted_tasks_file.write_text(json.dumps(interrupted, indent=2))
                logger.info("interrupted_tasks_saved", count=len(interrupted))
            except Exception as e:
                logger.error("interrupted_tasks_save_failed", error=str(e))
        else:
            # No tasks to save — clean up stale file
            if self._interrupted_tasks_file.exists():
                self._interrupted_tasks_file.unlink(missing_ok=True)

    async def _notify_interrupted_tasks(self):
        """On startup, notify users about tasks interrupted by the previous shutdown."""
        if not self._interrupted_tasks_file.exists():
            return
        try:
            data = json.loads(self._interrupted_tasks_file.read_text())
            if not data:
                return
            for entry in data:
                sender = entry.get("sender")
                project = entry.get("project", "unknown")
                desc = entry.get("description", "unknown task")
                step = entry.get("step", "")
                ts = entry.get("timestamp", "")
                msg = (
                    f"[{project}] Service was restarted while a task was running.\n"
                    f"Interrupted task: {desc[:150]}\n"
                    f"Last step: {step}\n"
                    f"You may need to re-run this task."
                )
                try:
                    await self._send_message(sender, msg)
                    logger.info("interrupted_task_notified", sender=sender,
                                project=project, task=desc[:50])
                except Exception as e:
                    logger.warning("interrupted_task_notify_failed",
                                   sender=sender, error=str(e))
            # Clean up the file after notifying
            self._interrupted_tasks_file.unlink(missing_ok=True)
            logger.info("interrupted_tasks_file_cleaned")
        except (json.JSONDecodeError, OSError) as e:
            logger.error("interrupted_tasks_read_failed", error=str(e))
            # Remove corrupt file
            self._interrupted_tasks_file.unlink(missing_ok=True)

    async def _get_account(self):
        """Get the registered Signal account with retry.

        Retries on connection errors and timeouts (signal-api may still be
        starting).  Does NOT retry when signal-api responds 200 with an
        empty account list (it's up but unconfigured).
        """
        url = f"{self.config.signal_api_url}/v1/accounts"
        max_attempts = 12
        base_delay = 5
        max_delay = 15

        for attempt in range(1, max_attempts + 1):
            try:
                async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        accounts = await resp.json()
                        if accounts:
                            self.account = accounts[0]
                            logger.info("account_found", account=self.account)
                            return
                        else:
                            # Signal API is up but no accounts registered — don't retry
                            logger.error("no_accounts_registered")
                            return
                    else:
                        logger.warning(
                            "accounts_request_failed",
                            status=resp.status,
                            attempt=attempt,
                            max_attempts=max_attempts,
                        )
                        if attempt < max_attempts:
                            delay = min(base_delay * attempt, max_delay)
                            await asyncio.sleep(delay)
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                if attempt < max_attempts:
                    delay = min(base_delay * attempt, max_delay)
                    logger.warning(
                        "accounts_request_retry",
                        error=str(e),
                        attempt=attempt,
                        max_attempts=max_attempts,
                        retry_in=delay,
                    )
                    await asyncio.sleep(delay)
                else:
                    logger.error(
                        "accounts_request_error",
                        error=str(e),
                        attempts_exhausted=max_attempts,
                    )
                    return
            except Exception as e:
                logger.error("accounts_request_error", error=str(e))
                return

    async def _check_signal_api_health(self):
        """Check signal-api health and log diagnostic info at startup.

        Queries /v1/about to verify mode, version, and connectivity.
        Warns loudly if mode is not json-rpc (a common misconfiguration).
        """
        url = f"{self.config.signal_api_url}/v1/about"
        try:
            async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    info = await resp.json()
                    mode = info.get("mode", "unknown")
                    version = info.get("version", "unknown")
                    logger.info(
                        "signal_api_health",
                        mode=mode,
                        version=version,
                        build=info.get("build"),
                    )
                    if mode != "json-rpc":
                        logger.error(
                            "signal_api_wrong_mode",
                            mode=mode,
                            expected="json-rpc",
                            msg="Signal API must run in json-rpc mode for WebSocket message delivery. "
                                "Set MODE=json-rpc in docker-compose.yml environment.",
                        )
                else:
                    logger.warning("signal_api_health_failed", status=resp.status)
        except Exception as e:
            logger.warning("signal_api_health_error", error=str(e))

    def _split_message(self, message: str, max_length: int = 5000) -> list:
        """Split a long message into chunks that fit within Signal's limits.

        Splits at paragraph boundaries first, then line boundaries, then
        hard-splits at max_length as a last resort.
        """
        if len(message) <= max_length:
            return [message]

        parts = []
        remaining = message

        while remaining:
            if len(remaining) <= max_length:
                parts.append(remaining)
                break

            # Try to split at a paragraph boundary (double newline)
            split_pos = remaining.rfind("\n\n", 0, max_length)
            if split_pos > max_length // 2:
                parts.append(remaining[:split_pos])
                remaining = remaining[split_pos + 2:]
                continue

            # Try to split at a single newline
            split_pos = remaining.rfind("\n", 0, max_length)
            if split_pos > max_length // 2:
                parts.append(remaining[:split_pos])
                remaining = remaining[split_pos + 1:]
                continue

            # Hard split at max_length
            parts.append(remaining[:max_length])
            remaining = remaining[max_length:]

        return parts

    async def _send_message(self, recipient: str, message: str):
        """Send a message via Signal, splitting long messages into parts."""
        if not self.account:
            logger.error("no_account_for_sending")
            return

        # SECURITY: Double-check recipient is authorized before sending
        if not is_authorized(recipient):
            logger.warning("blocked_send_to_unauthorized", recipient="..." + recipient[-4:])
            return

        # Add instance identifier to all messages
        message = f"[{self.config.instance_name}] {message}"

        # Split long messages into multiple parts
        parts = self._split_message(message)

        try:
            url = f"{self.config.signal_api_url}/v2/send"

            for i, part in enumerate(parts):
                # Add part indicator for multi-part messages
                if len(parts) > 1:
                    part = f"[{i + 1}/{len(parts)}] {part}" if i > 0 else part

                payload = {
                    "number": self.account,
                    "recipients": [recipient],
                    "message": part
                }

                async with self.session.post(url, json=payload) as resp:
                    if resp.status == 201:
                        logger.info(
                            "message_sent",
                            recipient="..." + recipient[-4:],
                            part=i + 1,
                            total_parts=len(parts),
                        )
                    else:
                        text = await resp.text()
                        logger.error("send_failed", status=resp.status, response=text)
                        break

        except Exception as e:
            logger.error("send_error", error=str(e))

    async def _handle_command(self, command: str, args: str, sender: str, image_paths: Optional[list] = None) -> str:
        """Handle a bot command."""
        command = command.lower()

        if command == "help":
            return self._get_help()

        elif command == "projects":
            return self.project_manager.list_projects(sender)

        elif command == "select":
            if not args:
                return "Usage: /select <project_name>"
            success, msg = self.project_manager.select_project(args, sender)
            if success:
                self.runner.set_project(self.project_manager.get_current_path(sender))
            return msg

        elif command == "status":
            status = self.project_manager.get_status(sender)
            # Add running task info (direct /do tasks) for this sender
            active_tasks = [
                (key, state) for key, state in self._sender_tasks.items()
                if key[0] == sender and state.get("task") and not state["task"].done()
            ]
            for (_, proj), task_state in active_tasks:
                elapsed = ""
                if task_state.get("start"):
                    mins = int((datetime.now() - task_state["start"]).total_seconds() / 60)
                    elapsed = f" ({mins}m)"
                desc = task_state.get("description", "unknown")[:120]
                status += f"\n\nActive Task [{proj}]{elapsed}: {desc}"
                if task_state.get("step"):
                    status += f"\nStep: {task_state['step']}"

            # Add autonomous loop status
            try:
                loop_status = await self.autonomous_manager.get_loop_status()
                if loop_status.is_running:
                    auto_info = "\n\nAutonomous Loop: Running"
                    if loop_status.current_task_id:
                        current_task = await self.autonomous_manager.db.get_task(loop_status.current_task_id)
                        if current_task:
                            elapsed_auto = ""
                            if current_task.started_at:
                                mins = int((datetime.now() - current_task.started_at).total_seconds() / 60)
                                elapsed_auto = f" ({mins}m)"
                            auto_info += f"\nCurrent: {current_task.title[:50]}{elapsed_auto}"
                    auto_info += f"\nQueued: {loop_status.tasks_queued}"
                    auto_info += f" | Done: {loop_status.tasks_completed_today}"
                    if loop_status.tasks_failed_today > 0:
                        auto_info += f" | Failed: {loop_status.tasks_failed_today}"
                    status += auto_info
                elif loop_status.is_paused:
                    status += "\n\nAutonomous Loop: Paused"
            except Exception as e:
                logger.warning("status_autonomous_error", error=str(e))

            # Add cooldown info if active
            if self.cooldown_manager and self.cooldown_manager.is_active:
                state = self.cooldown_manager.get_state()
                status += f"\n\nRate Limit Cooldown: Active (~{state.remaining_minutes} min remaining)"

            return status

        elif command == "add":
            if not args:
                return "Usage: /add <project_name> [path] [description]"
            parts = args.split(maxsplit=2)
            name = parts[0]
            path = parts[1] if len(parts) > 1 else None
            desc = parts[2] if len(parts) > 2 else ""
            success, msg = self.project_manager.add_project(name, path, desc)
            return msg

        elif command == "remove":
            if not args:
                return "Usage: /remove <project_name>"
            success, msg = self.project_manager.remove_project(args)
            if success and self.project_manager.get_current_project(sender) is None:
                self.runner.current_project = None
            return msg

        elif command == "new":
            if not args:
                return "Usage: /new <project_name> [description]"
            parts = args.split(maxsplit=1)
            name = parts[0]
            desc = parts[1] if len(parts) > 1 else ""
            success, msg = self.project_manager.create_project(name, sender, desc)
            if success:
                self.runner.set_project(self.project_manager.get_current_path(sender))
            return msg

        elif command == "ask":
            if not args and not image_paths:
                return "Usage: /ask <question about the project>"
            if self.cooldown_manager and self.cooldown_manager.is_active:
                return self.cooldown_manager.get_state().user_message
            current_project = self.project_manager.get_current_project(sender)
            if not current_project:
                return "No project selected. Use /select <project> first."
            busy = self._check_task_busy(sender, current_project)
            if busy:
                return busy

            await self._send_message(sender, "Analyzing project...")
            self._start_background_task(
                sender,
                f"Answer this question about the codebase: {args}",
                current_project,
                image_paths=image_paths,
            )
            return None  # Response will be sent when task completes

        elif command == "do":
            if not args and not image_paths:
                return "Usage: /do <task to perform>"
            if self.cooldown_manager and self.cooldown_manager.is_active:
                return self.cooldown_manager.get_state().user_message
            current_project = self.project_manager.get_current_project(sender)
            if not current_project:
                return "No project selected. Use /select <project> first."
            busy = self._check_task_busy(sender, current_project)
            if busy:
                return busy

            task_msg = args or "User sent an image attachment"
            await self._send_message(sender, f"[{current_project}] Working on it...")
            self._start_background_task(sender, task_msg, current_project, image_paths=image_paths)
            return None  # Response will be sent when task completes

        elif command == "complex":
            if not args:
                return "Usage: /complex <task>\nBreaks task into PRD with stories and autonomous tasks."
            if self.cooldown_manager and self.cooldown_manager.is_active:
                return self.cooldown_manager.get_state().user_message
            current_project = self.project_manager.get_current_project(sender)
            if not current_project:
                return "No project selected. Use /select <project> first."
            busy = self._check_task_busy(sender, current_project)
            if busy:
                return busy

            await self._send_message(sender, "Creating PRD and breaking into autonomous tasks...")
            # Run PRD creation in background (non-blocking)
            self._start_prd_creation_task(sender, args, current_project)
            return None  # Response sent when PRD creation completes

        elif command == "cancel":
            current_project = self.project_manager.get_current_project(sender)
            return await self._cancel_current_task(sender, current_project)

        elif command == "summary":
            current_project = self.project_manager.get_current_project(sender)
            if not current_project:
                return "No project selected. Use /select <project> first."
            busy = self._check_task_busy(sender, current_project)
            if busy:
                return busy

            await self._send_message(sender, "Generating summary...")
            self._start_background_task(
                sender,
                "Provide a comprehensive summary of this project including "
                "its structure, main technologies used, and any recent changes "
                "visible in git history.",
                current_project
            )
            return None  # Response will be sent when task completes

        # Memory commands - use current project by default
        elif command == "remember":
            return await self.memory_commands.handle_remember(
                sender, args, project=self.project_manager.get_current_project(sender)
            )

        elif command == "recall":
            return await self.memory_commands.handle_recall(
                sender, args, project=self.project_manager.get_current_project(sender)
            )

        elif command == "history":
            return await self.memory_commands.handle_history(
                sender, args, project=self.project_manager.get_current_project(sender)
            )

        elif command == "forget":
            return await self.memory_commands.handle_forget(sender, args)

        elif command == "memories":
            return await self.memory_commands.handle_memories(
                sender, args, project=self.project_manager.get_current_project(sender)
            )

        elif command == "preferences":
            return await self.memory_commands.handle_preferences(sender, args)

        # Global memory commands - explicitly cross-project
        elif command == "global":
            return await self._handle_global_command(sender, args)

        # Autonomous system commands
        elif command == "prd":
            return await self.autonomous_commands.handle_prd(sender, args)

        elif command == "story":
            return await self.autonomous_commands.handle_story(sender, args)

        elif command == "task":
            return await self.autonomous_commands.handle_task(sender, args)

        elif command == "tasks":
            return await self.autonomous_commands.handle_tasks(sender, args)

        elif command == "autonomous":
            return await self.autonomous_commands.handle_autonomous(sender, args)

        elif command == "queue":
            return await self.autonomous_commands.handle_queue(sender, args)

        elif command == "learnings":
            return await self.autonomous_commands.handle_learnings(sender, args)

        elif command in ("nightwire", "sidechannel"):
            if not self.nightwire_runner:
                return "nightwire assistant is not enabled. Set nightwire_assistant.enabled: true in settings.yaml and provide OPENAI_API_KEY or GROK_API_KEY."
            if not args:
                return "Usage: /nightwire <question>\nAsk the AI assistant anything."
            return await self._nightwire_response(args)

        elif command == "update":
            # Only admin (first allowed number) can trigger updates
            if not self.config.allowed_numbers or sender != self.config.allowed_numbers[0]:
                return "Only the admin can trigger updates."
            if not self.updater:
                return "Auto-update is not enabled. Set auto_update.enabled: true in settings.yaml."
            return await self.updater.apply_update()

        elif command == "cooldown":
            return await self._handle_cooldown_command(sender, args)

        elif command == "diagnose":
            return await self._handle_diagnose(sender)

        else:
            # Check plugin commands
            plugin_handler = self.plugin_loader.get_all_commands().get(command)
            if plugin_handler:
                return await plugin_handler(sender, args)
            return f"Unknown command: /{command}\nUse /help to see available commands."

    async def _handle_cooldown_command(self, sender: str, args: str) -> str:
        """Handle /cooldown [status|clear|test] command."""
        if not self.cooldown_manager:
            return "Cooldown manager not initialized."

        subcommand = args.strip().lower() if args else "status"

        if subcommand == "status":
            state = self.cooldown_manager.get_state()
            if state.active:
                return f"Cooldown ACTIVE (~{state.remaining_minutes} min remaining)\n{state.user_message}"
            return "No active cooldown. Claude operations are running normally."

        elif subcommand == "clear":
            if not self.cooldown_manager.is_active:
                return "No active cooldown to clear."
            self.cooldown_manager.deactivate()
            return "Cooldown cleared. Claude operations resumed."

        elif subcommand == "test":
            if self.cooldown_manager.is_active:
                return "Cooldown is already active. Use /cooldown clear first."
            self.cooldown_manager.activate(cooldown_minutes=2)
            return "Test cooldown activated (2 minutes). Use /cooldown clear to cancel."

        else:
            return "Usage: /cooldown [status|clear|test]"

    def _get_help(self) -> str:
        """Get help text."""
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
  /diagnose - Run system health diagnostics
  /cooldown [status|clear|test] - Rate limit cooldown info/control
  /update - Apply a pending update (admin only)"""

        if self.nightwire_runner:
            help_text = """nightwire Commands:

AI Assistant:
  /nightwire <question> - Ask the AI assistant anything
  Or just: nightwire <question>

""" + help_text[len("nightwire Commands:\n\n"):]

        # Append plugin help sections
        for section in self.plugin_loader.get_all_help():
            help_text += f"\n\n{section.title}:"
            for cmd, desc in section.commands.items():
                help_text += f"\n  /{cmd} - {desc}"

        return help_text

    async def _handle_diagnose(self, sender: str) -> str:
        """Run system health diagnostics and return results."""
        from . import __version__
        lines = [f"Nightwire Diagnostics (v{__version__})"]
        lines.append("=" * 35)

        # 1. Signal API health
        try:
            url = f"{self.config.signal_api_url}/v1/about"
            async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    info = await resp.json()
                    mode = info.get("mode", "unknown")
                    version = info.get("version", "unknown")
                    mode_ok = mode == "json-rpc"
                    lines.append(f"Signal API: OK (v{version}, mode={mode})")
                    if not mode_ok:
                        lines.append("  WARNING: Mode must be 'json-rpc' for message delivery!")
                else:
                    lines.append(f"Signal API: ERROR (HTTP {resp.status})")
        except Exception as e:
            lines.append(f"Signal API: UNREACHABLE ({e})")

        # 2. WebSocket status
        ws_frames = self._ws_frames_received
        idle_secs = int(_time.monotonic() - self._last_ws_activity) if self._last_ws_activity else -1
        lines.append(f"WebSocket frames received: {ws_frames}")
        if idle_secs >= 0:
            lines.append(f"Last WS activity: {idle_secs}s ago")
        else:
            lines.append("Last WS activity: never")

        # 3. Account
        lines.append(f"Account: {self.account or 'NOT SET'}")

        # 4. Docker container status
        try:
            result = await asyncio.to_thread(
                __import__('subprocess').run,
                ["docker", "inspect", "--format", "{{.State.Status}}", "signal-api"],
                capture_output=True, text=True, timeout=5
            )
            container_status = result.stdout.strip() if result.returncode == 0 else "not found"
            lines.append(f"Docker signal-api: {container_status}")
        except Exception:
            lines.append("Docker signal-api: unknown (docker not available)")

        # 5. Active tasks
        active = sum(1 for s in self._sender_tasks.values()
                     if s.get("task") and not s["task"].done())
        lines.append(f"Active tasks: {active}")

        return "\n".join(lines)

    async def _get_memory_context(self, sender: str, query: str,
                                   project_name: Optional[str] = None) -> Optional[str]:
        """Get memory context for a Claude prompt.

        Args:
            sender: User's phone number
            query: The current task/question
            project_name: Project to scope memory to (uses sender's current if None)

        Returns:
            Memory context string to inject, or None
        """
        if project_name is None:
            project_name = self.project_manager.get_current_project(sender)
        try:
            context = await self.memory.get_relevant_context(
                phone_number=sender,
                query=query,
                project_name=project_name,
                max_results=5,
                max_tokens=self.config.memory_max_context_tokens
            )
            return context if context else None
        except Exception as e:
            logger.warning("memory_context_error", error=str(e))
            return None

    def _start_background_task(self, sender: str, task_description: str, project_name: Optional[str],
                               image_paths: Optional[list] = None):
        """Start a Claude task in the background (non-blocking).

        This allows other commands to be processed while the task runs.
        Each sender can have one concurrent task per project.
        """
        task_key = (sender, project_name)
        task_state = {
            "description": task_description,
            "start": datetime.now(),
            "step": "Preparing context...",
            "task": None,  # Set after creation
            "project": project_name,
            "cancel_reason": None,  # Set before cancel() to explain why
        }
        self._sender_tasks[task_key] = task_state

        # Capture project path at creation time to avoid race conditions
        # when user switches projects while a task is running
        task_project_path = self.project_manager.get_project_path(project_name)

        async def run_task():
            try:
                async def progress_cb(msg: str):
                    task_state["step"] = msg
                    await self._send_message(sender, f"[{project_name}] {msg}")

                # Get memory context for this task (use project_name captured at creation)
                task_state["step"] = "Loading memory context..."
                memory_context = await self._get_memory_context(sender, task_description, project_name)

                # Append image attachment paths to the task description
                effective_description = task_description
                if image_paths:
                    paths_section = "\n\n## Attached Images\n" + "\n".join(
                        f"- {path}" for path in image_paths
                    )
                    effective_description = task_description + paths_section

                task_state["step"] = "Claude executing task..."
                # Use project_path captured at task creation to avoid race conditions
                # when user switches projects while a task is running
                success, response = await self.runner.run_claude(
                    effective_description,
                    progress_callback=progress_cb,
                    memory_context=memory_context,
                    project_path=task_project_path,
                )

                # Handle empty or failed responses
                if not success:
                    logger.error("claude_task_failed", response=response[:200])
                    await self._send_message(
                        sender,
                        f"[{project_name}] Task failed: {response}" if response.strip() else f"[{project_name}] Task failed. Please try again.",
                    )
                    return

                if not response or not response.strip():
                    logger.warning("claude_empty_response", output_length=len(response) if response else 0)
                    await self._send_message(
                        sender,
                        f"[{project_name}] Claude returned an empty response. This can happen with URLs or content it can't process. Please try rephrasing your request.",
                    )
                    return

                # Store response to memory
                t = asyncio.create_task(
                    self.memory.store_message(
                        phone_number=sender,
                        role="assistant",
                        content=response,
                        project_name=project_name,
                        command_type="do"
                    )
                )
                t.add_done_callback(_log_task_exception)

                # Send the response prefixed with project name
                await self._send_message(sender, f"[{project_name}] {response}")

            except asyncio.CancelledError:
                reason = task_state.get("cancel_reason") or "Unknown reason"
                elapsed = ""
                if task_state.get("start"):
                    mins = int((datetime.now() - task_state["start"]).total_seconds() / 60)
                    elapsed = f" after {mins}m"
                await self._send_message(
                    sender,
                    f"[{project_name}] Task cancelled{elapsed}: {reason}.\n"
                    f"Task was: {self._truncate_description(task_description)}"
                )
                logger.info("background_task_cancelled", task=task_description[:50],
                            reason=reason)
            except Exception as e:
                logger.error("background_task_error", error=str(e), exc_type=type(e).__name__)
                await self._send_message(sender, f"[{project_name}] Task failed due to an internal error.")
            finally:
                # Clear task state for this (sender, project) pair
                self._sender_tasks.pop(task_key, None)

        task_state["task"] = asyncio.create_task(run_task())
        logger.info("background_task_started", task=task_description[:50], sender=sender, project=project_name)

    async def _handle_global_command(self, sender: str, args: str) -> str:
        """Handle /global <subcommand> for cross-project memory operations."""
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

        if subcommand == "remember":
            return await self.memory_commands.handle_remember(sender, subargs, project=None)
        elif subcommand == "recall":
            return await self.memory_commands.handle_recall(sender, subargs, project=None)
        elif subcommand == "memories":
            return await self.memory_commands.handle_memories(sender, subargs, project=None)
        elif subcommand == "history":
            return await self.memory_commands.handle_history(sender, subargs, project=None)
        else:
            return f"Unknown global command: {subcommand}\n\nUse /global for help."

    @staticmethod
    def _truncate_description(desc: str, max_len: int = 100) -> str:
        """Truncate a task description at a word boundary with ellipsis."""
        if len(desc) <= max_len:
            return desc
        # Find last space before the limit to avoid cutting mid-word
        truncated = desc[:max_len]
        last_space = truncated.rfind(" ")
        if last_space > max_len // 2:
            truncated = truncated[:last_space]
        return truncated + "..."

    def _check_task_busy(self, sender: str, project: str) -> Optional[str]:
        """Return a busy message if a task is running for this sender+project, else None."""
        task_key = (sender, project)
        task_state = self._sender_tasks.get(task_key)
        if not task_state or not task_state.get("task") or task_state["task"].done():
            return None
        elapsed = ""
        if task_state.get("start"):
            mins = int((datetime.now() - task_state["start"]).total_seconds() / 60)
            elapsed = f" ({mins}m)"
        desc = self._truncate_description(task_state.get("description", "unknown"))
        return f"[{project}] Task in progress{elapsed}: {desc}\nUse /cancel to stop it."

    async def _cancel_current_task(self, sender: str, project: Optional[str] = None) -> str:
        """Cancel the running task for this sender on the current project.

        If no project is specified, cancels all tasks for this sender.
        """
        if project:
            # Cancel task for specific project
            task_key = (sender, project)
            task_state = self._sender_tasks.get(task_key)
            if not task_state or not task_state.get("task") or task_state["task"].done():
                # Check if there are tasks on other projects
                other_tasks = [
                    (key, state) for key, state in self._sender_tasks.items()
                    if key[0] == sender and state.get("task") and not state["task"].done()
                ]
                if other_tasks:
                    task_list = ", ".join(f"{k[1]}" for k, _ in other_tasks)
                    return f"No task running on [{project}]. Active tasks on: {task_list}\nSwitch project with /select to cancel."
                return "No task is currently running."

            task_desc = task_state.get("description", "unknown")
            elapsed = ""
            if task_state.get("start"):
                mins = int((datetime.now() - task_state["start"]).total_seconds() / 60)
                elapsed = f" after {mins}m"

            task_state["cancel_reason"] = "Cancelled by user"
            task_state["task"].cancel()
            logger.info("task_cancelled_by_user", task=task_desc[:50], sender=sender, project=project)
            return f"[{project}] Cancelled{elapsed}: {self._truncate_description(task_desc)}"
        else:
            # Cancel all tasks for this sender
            cancelled = []
            for key, state in list(self._sender_tasks.items()):
                if key[0] == sender and state.get("task") and not state["task"].done():
                    state["cancel_reason"] = "Cancelled by user"
                    state["task"].cancel()
                    cancelled.append(key[1])
            if not cancelled:
                return "No task is currently running."
            logger.info("all_tasks_cancelled_by_user", sender=sender, projects=cancelled)
            return f"Cancelled tasks on: {', '.join(str(p) for p in cancelled)}"

    def _start_prd_creation_task(self, sender: str, task_description: str, project_name: str):
        """Start PRD creation in the background (non-blocking)."""
        task_key = (sender, project_name)
        # Capture project_path now to avoid TOCTOU race if user switches projects
        project_path = self.project_manager.get_current_path(sender)
        task_state = {
            "description": f"Creating PRD: {task_description[:50]}...",
            "start": datetime.now(),
            "step": "Initializing...",
            "task": None,  # Set after creation
            "project": project_name,
            "cancel_reason": None,
        }
        self._sender_tasks[task_key] = task_state

        async def run_prd_creation():
            try:
                result = await self._create_autonomous_prd(
                    sender, task_description, project_name, project_path
                )
                await self._send_message(sender, f"[{project_name}] {result}")
            except asyncio.CancelledError:
                reason = task_state.get("cancel_reason") or "Unknown reason"
                await self._send_message(
                    sender,
                    f"[{project_name}] PRD creation cancelled: {reason}."
                )
                logger.info("prd_creation_cancelled", reason=reason)
            except Exception as e:
                logger.error("prd_creation_error", error=str(e), exc_type=type(e).__name__)
                await self._send_message(sender, f"[{project_name}] PRD creation failed. Check logs for details.")
            finally:
                self._sender_tasks.pop(task_key, None)

        task_state["task"] = asyncio.create_task(run_prd_creation())
        logger.info("prd_creation_started", task=task_description[:50], sender=sender, project=project_name)

    async def _create_autonomous_prd(
        self, sender: str, task_description: str,
        project_name: Optional[str] = None, project_path: Optional[Path] = None,
    ) -> str:
        """Create a PRD with stories and tasks from a complex task description.

        Uses Claude to intelligently break down the task into manageable pieces.
        project_name and project_path are captured at call time to avoid race
        conditions if the user switches projects while this runs.
        """
        if project_name is None:
            project_name = self.project_manager.get_current_project(sender)
        if project_path is None:
            project_path = self.project_manager.get_current_path(sender)
        task_key = (sender, project_name)

        # Helper to update step and notify user
        async def update_step(step: str, notify: bool = True):
            task_state = self._sender_tasks.get(task_key)
            if task_state:
                task_state["step"] = step
            if notify:
                await self._send_message(sender, step)

        await update_step("Analyzing task complexity...")

        # First, use Claude to analyze and break down the task
        breakdown_prompt = f"""Analyze this task request and break it into a structured PRD (Product Requirements Document).

TASK REQUEST:
{task_description}

PROJECT: {project_name}

Return a JSON structure with this EXACT format (no markdown, just JSON):
{{
    "prd_title": "Brief title for the PRD (max 50 chars)",
    "prd_description": "One paragraph summary of what we're building",
    "stories": [
        {{
            "title": "Story title (max 50 chars)",
            "description": "What this story accomplishes",
            "tasks": [
                {{
                    "title": "Task title (max 50 chars)",
                    "description": "Detailed description of what to do. Include specific files, functions, or components to modify. Be specific enough that Claude can execute this independently.",
                    "priority": 10
                }}
            ]
        }}
    ]
}}

RULES:
1. Break into logical stories (features/components)
2. Each story should have 2-5 focused tasks
3. Tasks should be atomic - completable in one Claude session
4. Higher priority number = executed first
5. Order tasks by dependency (foundations first)
6. Include a final "Testing & Deployment" story if mentioned
7. Be specific in task descriptions - mention exact files/components
8. Keep tasks focused - if a task is too big, split it

CRITICAL JSON FORMATTING:
- Use double quotes for all strings
- NO trailing commas
- NO comments
- Escape special characters in strings (use \\n for newlines)
- Keep descriptions on single lines (no literal newlines in strings)

Return ONLY valid JSON, no markdown code blocks, no explanation."""

        try:
            # Run Claude to get the breakdown (pass project_path directly to avoid race)
            await update_step("Breaking down task...")
            success, response = await self.runner.run_claude(
                breakdown_prompt,
                timeout=self.config.claude_timeout,
                project_path=project_path,
            )

            if not success:
                logger.error("prd_analyze_failed", response=response[:200])
                return "Failed to analyze task."

            await update_step("Parsing task breakdown...", notify=False)

            # Parse the JSON response with robust error handling
            breakdown = await parse_prd_json(response, self.runner, update_step)

            await update_step("Creating PRD structure...", notify=False)

            # Create the PRD
            prd = await self.autonomous_manager.create_prd(
                phone_number=sender,
                project_name=project_name,
                title=breakdown["prd_title"],
                description=breakdown["prd_description"]
            )

            total_tasks = 0
            story_summaries = []
            total_stories = len(breakdown.get("stories", []))

            # Create stories and tasks
            for story_idx, story_data in enumerate(breakdown.get("stories", []), 1):
                await update_step(f"Creating story {story_idx}/{total_stories}...", notify=False)
                story = await self.autonomous_manager.create_story(
                    prd_id=prd.id,
                    phone_number=sender,
                    title=story_data["title"],
                    description=story_data["description"]
                )

                task_count = 0
                for task_data in story_data.get("tasks", []):
                    await self.autonomous_manager.create_task(
                        story_id=story.id,
                        phone_number=sender,
                        project_name=project_name,
                        title=task_data["title"],
                        description=task_data["description"],
                        priority=task_data.get("priority", 5)
                    )
                    task_count += 1
                    total_tasks += 1

                story_summaries.append(f"  - {story.title} ({task_count} tasks)")

            await update_step("Queuing tasks for execution...")

            # Queue all tasks
            await self.autonomous_manager.queue_prd(prd.id)

            # Start the autonomous loop if not running
            status = await self.autonomous_manager.get_loop_status()
            was_running = status.is_running
            if not was_running:
                await self.autonomous_manager.start_loop()

            # Return summary
            loop_state = "Running" if was_running else "Started"
            return (
                f"PRD #{prd.id}: {prd.title}\n\n"
                f"Stories:\n" + "\n".join(story_summaries) + "\n\n"
                f"{total_tasks} tasks queued | Loop: {loop_state}\n"
                f"Use /tasks or /autonomous status to monitor."
            )

        except (json.JSONDecodeError, ValueError) as e:
            logger.error("prd_json_parse_error", error=str(e), exc_type=type(e).__name__)
            return "Failed to parse the task breakdown. Please try again."
        except KeyError as e:
            logger.error("prd_missing_field", error=str(e))
            return "Task breakdown was incomplete. Please try again."
        except Exception as e:
            logger.error("prd_creation_error", error=str(e), exc_type=type(e).__name__)
            return "PRD creation failed. Please try again or check logs."

    async def _process_message(self, sender: str, message: str, image_paths: Optional[list] = None):
        """Process an incoming message."""
        if image_paths is None:
            image_paths = []

        # Check authorization
        if not is_authorized(sender):
            logger.warning("unauthorized_message", sender="..." + sender[-4:])
            return  # Silently ignore unauthorized messages

        # Check rate limit
        if not check_rate_limit(sender):
            logger.warning("rate_limited", sender="..." + sender[-4:])
            await self._send_message(sender, "Rate limited. Please wait before sending more messages.")
            return

        # Sanitize input
        message = sanitize_input(message.strip())

        if not message and not image_paths:
            return

        logger.info(
            "message_received",
            sender="..." + sender[-4:],
            length=len(message)
        )

        # Determine command type for memory logging
        command_type = None
        if message.startswith("/"):
            parts = message[1:].split(maxsplit=1)
            command_type = parts[0].lower()

        # Store incoming message (fire and forget, don't block)
        project_name = self.project_manager.get_current_project(sender)
        t = asyncio.create_task(
            self.memory.store_message(
                phone_number=sender,
                role="user",
                content=message,
                project_name=project_name,
                command_type=command_type
            )
        )
        t.add_done_callback(_log_task_exception)

        # Check pre-command matchers (e.g., device targeting)
        if message:
            for matcher in self.plugin_loader.get_sorted_matchers():
                if matcher.pre_command and matcher.match_fn(message):
                    response = await matcher.handle_fn(sender, message)
                    if response is not None:
                        if response:
                            await self._send_message(sender, response)
                        return  # Consumed (silently if empty, with response if not)

        # Check if it's a command
        if message and message.startswith("/"):
            parts = message[1:].split(maxsplit=1)
            command = parts[0]
            args = parts[1] if len(parts) > 1 else ""

            response = await self._handle_command(command, args, sender, image_paths=image_paths)
        else:
            # Check plugin message matchers
            response = None
            if message:
                for matcher in self.plugin_loader.get_sorted_matchers():
                    if matcher.match_fn(message):
                        response = await matcher.handle_fn(sender, message)
                        break

            if response is None and message and self._is_nightwire_query(message):
                # Addressed to nightwire - general AI assistant mode
                response = await self._nightwire_response(message)
            elif response is None:
                # Treat non-command messages as /do commands if a project is selected
                if self.cooldown_manager and self.cooldown_manager.is_active:
                    response = self.cooldown_manager.get_state().user_message
                elif project_name:
                    busy = self._check_task_busy(sender, project_name)
                    if busy:
                        response = busy
                    else:
                        task_msg = message or "User sent an image attachment"
                        await self._send_message(sender, f"[{project_name}] Working on it...")
                        self._start_background_task(sender, task_msg, project_name, image_paths=image_paths)
                        return  # Response will be sent when task completes
                elif image_paths:
                    response = "I received your image(s), but no project is selected. Use /select <project> first."
                else:
                    response = "No project selected. Use /projects to list or /select <project> to choose one."


        # If response is None, the task is running in background
        if response is None:
            return

        # Store outgoing response (fire and forget)
        t = asyncio.create_task(
            self.memory.store_message(
                phone_number=sender,
                role="assistant",
                content=response,
                project_name=project_name,
                command_type=command_type
            )
        )
        t.add_done_callback(_log_task_exception)

        await self._send_message(sender, response)

    def _is_nightwire_query(self, message: str) -> bool:
        """Detect if a message is addressed to nightwire assistant."""
        if not self.nightwire_runner:
            return False
        msg_lower = message.lower().strip()
        # Match: "nightwire:" / "sidechannel:" variants, followed by text, or just the name
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
        if not self.nightwire_runner:
            return "nightwire assistant is not enabled. Set nightwire_assistant.enabled: true in settings.yaml and provide OPENAI_API_KEY or GROK_API_KEY."
        try:
            logger.info("nightwire_query", length=len(message))
            success, response = await self.nightwire_runner.ask_nightwire(message)
            if not response or not response.strip():
                logger.warning("nightwire_empty_response")
                return "The assistant returned an empty response. Please try again."
            return response
        except Exception as e:
            logger.error("nightwire_response_error", error=str(e), exc_type=type(e).__name__)
            return "The assistant encountered an error. Please try again later."

    async def _health_watchdog(self):
        """Periodic health check that monitors bot responsiveness.

        Logs warnings if the websocket hasn't seen activity in a while,
        and verifies the event loop is responsive.
        """
        WATCHDOG_INTERVAL = 60  # check every 60 seconds
        WS_STALE_THRESHOLD = 600  # warn if no WS activity for 10 minutes

        try:
            while self.running:
                await asyncio.sleep(WATCHDOG_INTERVAL)

                if self._last_ws_activity > 0:
                    idle_secs = _time.monotonic() - self._last_ws_activity
                    if idle_secs > WS_STALE_THRESHOLD:
                        logger.warning(
                            "watchdog_ws_idle",
                            idle_seconds=int(idle_secs),
                            threshold=WS_STALE_THRESHOLD,
                            ws_frames_total=self._ws_frames_received,
                        )
        except asyncio.CancelledError:
            pass

    async def poll_messages(self):
        """Connect via WebSocket to receive messages (json-rpc mode)."""
        if not self.account:
            logger.error("no_account_for_polling")
            return

        # Convert http:// to ws:// for websocket connection
        ws_base = self.config.signal_api_url.replace("http://", "ws://").replace("https://", "wss://")
        ws_url = f"{ws_base}/v1/receive/{self.account}"

        reconnect_delay = 5
        MAX_RECONNECT_DELAY = 300
        MESSAGE_HANDLING_TIMEOUT = 120  # seconds

        while self.running:
            try:
                logger.info("websocket_connecting", url=ws_url)
                async with self.session.ws_connect(ws_url, heartbeat=30) as ws:
                    logger.info("websocket_connected")
                    self._last_ws_activity = _time.monotonic()
                    reconnect_delay = 5  # Reset on successful connection

                    # Notify users that the bot is ready on first connect
                    if not self._startup_notified:
                        self._startup_notified = True
                        from . import __version__
                        for phone in self.config.allowed_numbers:
                            try:
                                await self._send_message(
                                    phone,
                                    f"Nightwire v{__version__} started and ready."
                                )
                            except Exception as e:
                                logger.warning("startup_notify_error", error=str(e))

                    async for msg in ws:
                        self._last_ws_activity = _time.monotonic()
                        self._ws_frames_received += 1
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            try:
                                data = json.loads(msg.data)
                                await asyncio.wait_for(
                                    self._handle_signal_message(data),
                                    timeout=MESSAGE_HANDLING_TIMEOUT,
                                )
                            except asyncio.TimeoutError:
                                logger.error(
                                    "message_handling_timeout",
                                    timeout=MESSAGE_HANDLING_TIMEOUT,
                                    msg=data.get("envelope", {}).get("source", "unknown")[:20]
                                    if isinstance(data, dict) else "unknown",
                                )
                            except json.JSONDecodeError:
                                logger.warning("invalid_json", data=msg.data[:100])
                        elif msg.type == aiohttp.WSMsgType.ERROR:
                            logger.error("websocket_error", error=str(ws.exception()))
                            break
                        elif msg.type == aiohttp.WSMsgType.CLOSED:
                            logger.info("websocket_closed")
                            break

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("websocket_exception", error=str(e))
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, MAX_RECONNECT_DELAY)

    async def _handle_signal_message(self, msg: dict):
        """Handle a message from Signal API."""
        try:
            envelope = msg.get("envelope", {})
            source = envelope.get("source") or envelope.get("sourceNumber") or envelope.get("sourceUuid")
            message_text = None
            attachments_list = []

            # Log envelope type for diagnostics (helps debug message delivery issues)
            envelope_types = [k for k in envelope if k.endswith("Message") or k == "dataMessage"]
            if envelope_types:
                logger.debug("envelope_received", types=envelope_types,
                             source="..." + source[-4:] if source else "unknown")

            # Check for regular data message (from others TO us)
            data_message = envelope.get("dataMessage")
            if data_message:
                message_text = data_message.get("message", "")
                attachments_list = data_message.get("attachments") or []

            # Check for sync message (our own messages sent from another device)
            sync_message = envelope.get("syncMessage")
            if sync_message and not message_text:
                sent_message = sync_message.get("sentMessage")
                if sent_message:
                    # Check destination - only process messages sent to our own number
                    destination = sent_message.get("destination") or sent_message.get("destinationNumber")

                    # Ignore group messages
                    if sent_message.get("groupInfo"):
                        return

                    # Only process if sent to ourselves (the bot's number)
                    if destination and destination == self.account:
                        message_text = sent_message.get("message", "")
                        attachments_list = sent_message.get("attachments") or []
                        source = self.account

            # Download and save any image attachments
            image_paths = []
            if attachments_list and source and self.session:
                image_paths = await process_attachments(
                    attachments=attachments_list,
                    sender=source,
                    session=self.session,
                    signal_api_url=self.config.signal_api_url,
                    attachments_dir=self.config.attachments_dir,
                )
                if image_paths:
                    logger.info("attachments_processed", count=len(image_paths),
                                sender="..." + source[-4:])

            # Ignore bot-generated messages (prevents feedback loop between
            # multiple linked instances — each instance's responses arrive as
            # dataMessages on other instances sharing the same account)
            # Matches any instance name variant: [nightwire], [nightwire-mac], etc.
            if message_text and message_text.strip().startswith("[nightwire"):
                return

            # Ignore receipts, typing indicators, and other message types
            # Allow messages through if they have attachments even without text
            has_text = message_text and message_text.strip()
            if not has_text and not image_paths:
                return

            # SECURITY: Only process messages from authorized sources
            if not source:
                return

            # Deduplication: Signal sends both dataMessage and syncMessage for self-messages
            timestamp = envelope.get("timestamp", 0)
            dedup_text = (message_text or "").strip()
            msg_hash = hashlib.sha256(f"{timestamp}:{dedup_text}".encode()).hexdigest()
            if msg_hash in self._processed_messages:
                logger.debug("duplicate_message_skipped", timestamp=timestamp)
                return
            self._processed_messages[msg_hash] = _time.time()

            # Evict entries older than 60 seconds
            cutoff = _time.time() - 60
            while self._processed_messages:
                oldest_key, oldest_time = next(iter(self._processed_messages.items()))
                if oldest_time < cutoff:
                    self._processed_messages.pop(oldest_key)
                else:
                    break

            # Default empty text for attachment-only messages
            if not message_text:
                message_text = ""

            logger.info("processing_message", source="..." + source[-4:],
                        length=len(message_text), attachments=len(image_paths))
            await self._process_message(source, message_text, image_paths=image_paths)

        except Exception as e:
            logger.error("message_handling_error", error=str(e), msg=str(msg)[:200])

    async def run(self):
        """Main run loop."""
        await self.start()

        try:
            await self.poll_messages()
        finally:
            await self.stop()
