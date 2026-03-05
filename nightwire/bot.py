"""Signal bot implementation for nightwire.

Connects to Signal CLI REST API via WebSocket, dispatches incoming
messages through the command handler registry, plugin system, and
default Claude handler. Manages lifecycle for all subsystems:
memory, autonomous, plugins, cooldown, and auto-updater.

Key classes:
    SignalBot: Main bot class -- owns all subsystem instances and
        the message processing pipeline.

Key functions:
    _make_memory_commands: Factory for memory command handlers
        with per-phone project context injection.
"""

import asyncio
import hashlib
import json
import time as _time
from collections import OrderedDict
from functools import partial
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import aiohttp
import structlog

from .attachments import (
    SUPPORTED_IMAGE_TYPES,
    cleanup_old_attachments,
    process_attachments,
)
from .claude_runner import get_runner
from .commands.base import BotContext, HandlerRegistry
from .commands.core import CoreCommandHandler, get_memory_context
from .config import get_config
from .memory import MemoryCommands, MemoryManager
from .plugin_loader import PluginLoader
from .project_manager import get_project_manager
from .rate_limit_cooldown import get_cooldown_manager
from .security import check_rate_limit, is_authorized, sanitize_input
from .task_manager import TaskManager, log_task_exception
from .updater import AutoUpdater

logger = structlog.get_logger("nightwire.bot")


def _make_memory_commands(memory_commands, project_manager):
    """Create command dict with project context injection for memory handlers."""
    def _with_project(handler):
        async def wrapper(sender, args):
            project = project_manager.get_current_project(sender)
            return await handler(sender, args, project=project)
        return wrapper

    return {
        "remember": _with_project(memory_commands.handle_remember),
        "recall": _with_project(memory_commands.handle_recall),
        "history": _with_project(memory_commands.handle_history),
        "forget": memory_commands.handle_forget,
        "memories": _with_project(memory_commands.handle_memories),
        "preferences": memory_commands.handle_preferences,
    }


class SignalBot:
    """Signal bot with command handler registry.

    Owns the full message lifecycle: WebSocket connection, message
    deduplication, authorization, rate limiting, command routing
    (via HandlerRegistry), plugin dispatch, and response delivery.

    Subsystems (memory, autonomous, plugins, cooldown, updater)
    are initialized in two phases: __init__ for sync setup and
    start() for async initialization requiring the event loop.
    """

    def __init__(self):
        self.config = get_config()
        self.runner = get_runner()
        self.project_manager = get_project_manager()

        # nightwire assistant runner is optional
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
        self._processed_messages = OrderedDict()  # Dedup: msg_hash -> timestamp
        self._attachment_cleanup_task: Optional[asyncio.Task] = None
        self._ws_frames_received: int = 0
        self._startup_notified: bool = False

        # Memory system
        memory_db_path = Path(self.config.config_dir).parent / "data" / "memory.db"
        self.memory = MemoryManager(
            db_path=memory_db_path,
            session_timeout_minutes=self.config.memory_session_timeout,
            max_context_tokens=self.config.memory_max_context_tokens,
        )
        self.memory_commands = MemoryCommands(self.memory)

        # Autonomous system (initialized after memory in start())
        self.autonomous_manager = None
        self.autonomous_commands = None

        # Auto-updater (initialized in start() if enabled)
        self.updater: Optional[AutoUpdater] = None

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

        # --- Command handler infrastructure ---

        # Standalone memory context function (no circular dep)
        get_mem_ctx = partial(
            get_memory_context, self.memory, self.config, self.project_manager
        )

        # TaskManager — background task lifecycle
        self.task_manager = TaskManager(
            runner=self.runner,
            project_manager=self.project_manager,
            memory=self.memory,
            config=self.config,
            send_message=self._send_message,
            get_memory_context=get_mem_ctx,
        )

        # BotContext — dependency container for handlers
        self._bot_context = BotContext(
            config=self.config,
            runner=self.runner,
            project_manager=self.project_manager,
            memory=self.memory,
            memory_commands=self.memory_commands,
            plugin_loader=self.plugin_loader,
            send_message=self._send_message,
            task_manager=self.task_manager,
            get_memory_context=get_mem_ctx,
            nightwire_runner=self.nightwire_runner,
        )

        # Handler registry — core + memory registered now, autonomous in start()
        self._registry = HandlerRegistry()
        self._bot_context.registry = self._registry
        self._core_handler = CoreCommandHandler(self._bot_context)
        self._registry.register(self._core_handler)
        self._registry.register_external(
            _make_memory_commands(self.memory_commands, self.project_manager)
        )

    async def start(self):
        """Start the bot and all subsystems.

        Initializes the HTTP session, memory system, autonomous
        manager, plugins, auto-updater, and cooldown manager.
        Registers deferred command handlers (autonomous).
        """
        self.session = aiohttp.ClientSession()
        self.running = True

        # Warn if non-localhost Signal API is not using HTTPS
        parsed = urlparse(self.config.signal_api_url)
        if (
            parsed.hostname not in ("127.0.0.1", "localhost", "::1")
            and parsed.scheme != "https"
        ):
            logger.warning(
                "insecure_signal_api_url", url=self.config.signal_api_url,
                msg="Non-localhost Signal API should use HTTPS",
            )

        # Get the registered account
        await self._get_account()

        # Initialize memory system
        await self.memory.initialize()

        # Initialize autonomous system (uses same DB connection)
        from .autonomous import AutonomousCommands, AutonomousManager

        async def autonomous_notify(phone: str, message: str):
            await self._send_message(phone, message)

        async def autonomous_usage_recorder(
            phone_number: str,
            project_name: str = None,
            source: str = "autonomous",
            usage_data: dict = None,
        ):
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
                )
            except Exception as e:
                logger.debug("autonomous_usage_record_failed", error=str(e))

        self.autonomous_manager = AutonomousManager(
            db_connection=self.memory.db._conn,
            progress_callback=autonomous_notify,
            poll_interval=self.config.autonomous_poll_interval,
            run_quality_gates=self.config.autonomous_quality_gates,
            max_parallel=self.config.autonomous_max_parallel,
            usage_recorder=autonomous_usage_recorder,
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
            )
            await self.updater.start()
            self.set_shutdown_callback()

        # Initialize rate-limit cooldown manager
        self.cooldown_manager = get_cooldown_manager()

        async def _cooldown_on_activate():
            if self.autonomous_manager:
                await self.autonomous_manager.pause_loop()
            state = self.cooldown_manager.get_state()
            for phone in self.config.allowed_numbers:
                try:
                    await self._send_message(
                        phone,
                        f"Rate limit cooldown activated. {state.user_message}",
                    )
                except Exception as e:
                    logger.warning("cooldown_notify_error", error=str(e))

        async def _cooldown_on_deactivate():
            if self.autonomous_manager:
                await self.autonomous_manager.start_loop()
            for phone in self.config.allowed_numbers:
                try:
                    await self._send_message(
                        phone,
                        "Rate limit cooldown expired. Claude operations resumed.",
                    )
                except Exception as e:
                    logger.warning("cooldown_notify_error", error=str(e))

        self.cooldown_manager.on_activate(_cooldown_on_activate)
        self.cooldown_manager.on_deactivate(_cooldown_on_deactivate)

        # Start attachment cleanup loop
        self._attachment_cleanup_task = asyncio.create_task(
            self._attachment_cleanup_loop()
        )

        # Update BotContext with deferred dependencies
        self._bot_context._autonomous_manager = self.autonomous_manager
        self._bot_context._autonomous_commands = self.autonomous_commands
        self._bot_context._updater = self.updater
        self._bot_context._cooldown_manager = self.cooldown_manager

        # Update TaskManager with autonomous_manager
        self.task_manager.autonomous_manager = self.autonomous_manager

        # Register autonomous commands (now that they exist)
        self._registry.register_external({
            "prd": self.autonomous_commands.handle_prd,
            "story": self.autonomous_commands.handle_story,
            "task": self.autonomous_commands.handle_task,
            "tasks": self.autonomous_commands.handle_tasks,
            "autonomous": self.autonomous_commands.handle_autonomous,
            "queue": self.autonomous_commands.handle_queue,
            "learnings": self.autonomous_commands.handle_learnings,
        })

        # Startup diagnostics — log feature availability
        try:
            from .diagnostics import run_all_checks

            diag_results = await run_all_checks(self.config)
            for name, (ok, detail, hint) in diag_results.items():
                if ok:
                    logger.info(
                        "startup_check_pass",
                        check=name, detail=detail,
                    )
                else:
                    logger.warning(
                        "startup_check_fail",
                        check=name, detail=detail, hint=hint,
                    )
        except Exception as exc:
            logger.warning("startup_diagnostics_error", error=str(exc))

        # Notify users about tasks interrupted by previous shutdown
        data_dir = Path(self.config.config_dir).parent / "data"
        await self.task_manager.notify_interrupted_tasks(data_dir)

        logger.info("bot_started", account=self.account)

    SHUTDOWN_GRACE_SECONDS = 90

    def set_shutdown_callback(self):
        """Wire this bot's stop() as the updater's shutdown callback."""
        if self.updater:
            self.updater._shutdown_callback = lambda: asyncio.create_task(
                self.stop()
            )

    async def stop(self):
        """Stop the bot and clean up all resources.

        Graceful shutdown: waits up to SHUTDOWN_GRACE_SECONDS for
        in-flight tasks to complete before force-killing. Persists
        interrupted tasks for restart notification.
        """
        if not self.running:
            return
        self.running = False
        await self.plugin_loader.stop_all()
        if self.cooldown_manager:
            self.cooldown_manager.cancel_timer()
        if self.updater:
            await self.updater.stop()
        if self.autonomous_manager:
            await self.autonomous_manager.stop_loop()
        if self.nightwire_runner:
            await self.nightwire_runner.close()
        if self._attachment_cleanup_task and not self._attachment_cleanup_task.done():
            self._attachment_cleanup_task.cancel()
            try:
                await self._attachment_cleanup_task
            except asyncio.CancelledError:
                pass
        # Cancel active Claude processes
        await self.runner.cancel()
        # Graceful shutdown: wait for in-flight tasks to finish naturally
        pending = [
            s["task"]
            for s in self.task_manager._sender_tasks.values()
            if s.get("task") and not s["task"].done()
        ]
        if pending:
            logger.info("shutdown_grace_period",
                        tasks=len(pending),
                        timeout=self.SHUTDOWN_GRACE_SECONDS)
            done, still_running = await asyncio.wait(
                pending, timeout=self.SHUTDOWN_GRACE_SECONDS,
            )
            if still_running:
                logger.warning("shutdown_force_kill",
                               tasks=len(still_running))
        # Save interrupted tasks BEFORE force-cancelling
        data_dir = Path(self.config.config_dir).parent / "data"
        self.task_manager.save_interrupted_tasks(data_dir)
        # Force-cancel any remaining tasks
        await self.task_manager.cancel_all_tasks(
            reason="service shutting down",
        )
        # Now safe to close network and database resources
        if self.session:
            await self.session.close()
        await self.runner.close()
        await self.memory.close()
        logger.info("bot_stopped")

    async def _attachment_cleanup_loop(self):
        """Periodically delete attachment files older than the configured TTL."""
        while True:
            try:
                await asyncio.sleep(3600)  # Check every hour
                max_age = self.config.attachment_max_age_hours
                if max_age <= 0:
                    continue
                deleted = await asyncio.to_thread(
                    cleanup_old_attachments,
                    self.config.attachments_dir,
                    max_age,
                )
                if deleted > 0:
                    logger.info("attachments_cleaned", deleted=deleted)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("attachment_cleanup_error", error=str(e))

    async def _get_account(self):
        """Get the registered Signal account with retry."""
        max_attempts = 12
        base_delay = 5
        max_delay = 15

        for attempt in range(1, max_attempts + 1):
            try:
                url = f"{self.config.signal_api_url}/v1/accounts"
                async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        accounts = await resp.json()
                        if accounts:
                            acct = accounts[0]
                            self.account = acct if isinstance(acct, str) else acct.get("number")
                            logger.info("account_found", account=self.account)
                            return
                        else:
                            logger.warning("no_accounts_registered")
                            return
                    else:
                        logger.warning("account_request_failed",
                                        status=resp.status, attempt=attempt)
                        if attempt < max_attempts:
                            delay = min(base_delay * attempt, max_delay)
                            await asyncio.sleep(delay)
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                delay = min(base_delay * attempt, max_delay)
                logger.warning(
                    "account_request_error", error=str(e),
                    attempt=attempt, retry_delay=delay,
                )
                if attempt < max_attempts:
                    await asyncio.sleep(delay)

        logger.error("account_request_failed_all_attempts", attempts=max_attempts)

    @staticmethod
    def _split_message(text: str, max_len: int = 5000) -> list:
        """Split long messages at paragraph/line boundaries.

        Args:
            text: Message text to split.
            max_len: Maximum characters per part.

        Returns:
            List of message parts, each within max_len.
        """
        if len(text) <= max_len:
            return [text]
        parts = []
        remaining = text
        while remaining:
            if len(remaining) <= max_len:
                parts.append(remaining)
                break
            # Try to split at paragraph boundary
            split_at = remaining.rfind("\n\n", 0, max_len)
            if split_at < max_len // 2:
                # Try single newline
                split_at = remaining.rfind("\n", 0, max_len)
            if split_at < max_len // 2:
                # Hard split at max_len
                split_at = max_len
            parts.append(remaining[:split_at])
            remaining = remaining[split_at:].lstrip("\n")
        return parts

    async def _send_message(self, recipient: str, message: str):
        """Send a message via Signal API, splitting long messages.

        Messages are prefixed with ``[instance_name]`` and split at
        paragraph/line boundaries if they exceed 5000 characters.
        """
        if not self.account:
            logger.warning("send_no_account", msg="Cannot send — no Signal account registered")
            return
        if not is_authorized(recipient):
            logger.warning("send_blocked_unauthorized", recipient="..." + recipient[-4:])
            return

        prefix = f"[{self.config.instance_name}]"
        parts = self._split_message(message)
        total = len(parts)
        for i, part in enumerate(parts):
            if total > 1:
                label = f"{prefix} [{i + 1}/{total}] "
            else:
                label = f"{prefix} "
            payload = {
                "message": label + part,
                "number": self.account,
                "recipients": [recipient],
            }
            try:
                url = f"{self.config.signal_api_url}/v2/send"
                async with self.session.post(url, json=payload) as resp:
                    if resp.status != 201:
                        body = await resp.text()
                        logger.warning("send_failed", status=resp.status,
                                        body=body[:200])
            except Exception as e:
                logger.error("send_error", error=str(e))

    async def _handle_command(
        self, command: str, args: str, sender: str, image_paths=None
    ) -> Optional[str]:
        """Route a /command to the handler registry or plugins.

        Args:
            command: Command name (without leading /).
            args: Everything after the command name.
            sender: Phone number or UUID of the sender.
            image_paths: Optional list of saved image file paths.
                Only forwarded to ask/do commands.

        Returns:
            Response string, or None if handled asynchronously.
        """
        command = command.lower()
        logger.debug("command_routing", command=command, has_args=bool(args))

        # Check registered command handlers
        handler = self._registry.get(command)
        if handler:
            # Only ask/do accept image_paths — all others keep (sender, args)
            if image_paths and command in ("ask", "do"):
                return await handler(sender, args, image_paths=image_paths)
            return await handler(sender, args)

        # Check plugin commands
        plugin_handler = self.plugin_loader.get_all_commands().get(command)
        if plugin_handler:
            return await plugin_handler(sender, args)

        return f"Unknown command: /{command}\nUse /help to see available commands."

    async def _process_message(
        self, sender: str, message: str, image_paths=None
    ):
        """Process an incoming message through the full routing chain.

        Routing order: /commands -> plugin matchers -> nightwire
        assistant -> default Claude handler. Stores both user and
        assistant messages to memory.

        Args:
            sender: Phone number or UUID of the sender.
            message: Raw message text.
            image_paths: Optional list of saved image file paths from
                attachments. Forwarded to ask/do commands and the
                default handler for Claude's agentic Read tool.
        """
        if not is_authorized(sender):
            logger.warning("unauthorized_message", sender="..." + sender[-4:])
            return

        if not check_rate_limit(sender):
            logger.warning("rate_limited", sender="..." + sender[-4:])
            await self._send_message(
                sender, "Rate limited. Please wait before sending more messages."
            )
            return

        message = sanitize_input(message.strip())
        if not message:
            return

        logger.info(
            "message_received",
            sender="..." + sender[-4:],
            length=len(message),
        )

        command_type = None
        if message.startswith("/"):
            parts = message[1:].split(maxsplit=1)
            command_type = parts[0].lower()

        project_name = self.project_manager.get_current_project(sender)
        t = asyncio.create_task(
            self.memory.store_message(
                phone_number=sender,
                role="user",
                content=message,
                project_name=project_name,
                command_type=command_type,
            )
        )
        t.add_done_callback(log_task_exception)

        # Route the message
        if message.startswith("/"):
            parts = message[1:].split(maxsplit=1)
            command = parts[0]
            args = parts[1] if len(parts) > 1 else ""

            logger.debug("message_routing", is_command=True, routing_path="command")
            response = await self._handle_command(
                command, args, sender, image_paths=image_paths
            )
        else:
            response = None
            for matcher in self.plugin_loader.get_sorted_matchers():
                matched = matcher.match_fn(message)
                logger.debug(
                    "plugin_matcher_evaluated",
                    matcher=type(matcher).__name__, matched=matched,
                )
                if matched:
                    response = await matcher.handle_fn(sender, message)
                    break

            if response is None and self._core_handler._is_nightwire_query(message):
                logger.debug("message_routing", is_command=False, routing_path="nightwire")
                response = await self._core_handler._nightwire_response(message)
            elif response is not None:
                logger.debug("message_routing", is_command=False, routing_path="matcher")
            else:
                logger.debug("message_routing", is_command=False, routing_path="default")
                if self.cooldown_manager and self.cooldown_manager.is_active:
                    response = self.cooldown_manager.get_state().user_message
                elif project_name:
                    busy = self.task_manager.check_busy(sender, project_name)
                    if busy:
                        response = busy
                    else:
                        await self._send_message(sender, "Working on it...")
                        self.task_manager.start_background_task(
                            sender, message, project_name,
                            image_paths=image_paths,
                        )
                        return
                elif image_paths:
                    response = (
                        "Image received but no project selected. "
                        "Use /select <project> first, then send images."
                    )
                else:
                    response = (
                        "No project selected. Use /projects to list"
                        " or /select <project> to choose one."
                    )

        if response is None:
            return

        t = asyncio.create_task(
            self.memory.store_message(
                phone_number=sender,
                role="assistant",
                content=response,
                project_name=project_name,
                command_type=command_type,
            )
        )
        t.add_done_callback(log_task_exception)

        await self._send_message(sender, response)

    async def poll_messages(self):
        """Connect via WebSocket to receive messages (json-rpc mode)."""
        if not self.account:
            logger.error("no_account_for_polling")
            return

        ws_base = self.config.signal_api_url.replace(
            "http://", "ws://"
        ).replace("https://", "wss://")
        ws_url = f"{ws_base}/v1/receive/{self.account}"

        reconnect_delay = 5
        MAX_RECONNECT_DELAY = 300

        while self.running:
            try:
                logger.info("websocket_connecting", url=ws_url)
                async with self.session.ws_connect(ws_url, heartbeat=30) as ws:
                    logger.info("websocket_connected")
                    reconnect_delay = 5
                    # Startup notification — send once on first WS connect
                    if not self._startup_notified:
                        self._startup_notified = True
                        from nightwire import __version__
                        for phone in self.config.allowed_numbers:
                            try:
                                await self._send_message(
                                    phone,
                                    f"Nightwire v{__version__} started"
                                    " and ready.",
                                )
                            except Exception:
                                pass

                    async for msg in ws:
                        self._ws_frames_received += 1
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            try:
                                data = json.loads(msg.data)
                                envelope = data.get("envelope", {})
                                envelope_type = (
                                    "dataMessage" if envelope.get("dataMessage")
                                    else "syncMessage" if envelope.get("syncMessage")
                                    else "receipt" if envelope.get("receiptMessage")
                                    else "typing" if envelope.get("typingMessage")
                                    else "other"
                                )
                                logger.debug(
                                    "ws_envelope",
                                    type=envelope_type,
                                    frames=self._ws_frames_received,
                                )
                                await self._handle_signal_message(data)
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
            source = (
                envelope.get("source")
                or envelope.get("sourceNumber")
                or envelope.get("sourceUuid")
            )
            message_text = None
            attachments = []

            data_message = envelope.get("dataMessage")
            if data_message:
                message_text = data_message.get("message", "")
                attachments = data_message.get("attachments", [])

            sync_message = envelope.get("syncMessage")
            if sync_message and not message_text:
                sent_message = sync_message.get("sentMessage")
                if sent_message:
                    destination = (
                        sent_message.get("destination")
                        or sent_message.get("destinationNumber")
                    )
                    if sent_message.get("groupInfo"):
                        return
                    if destination and destination == self.account:
                        message_text = sent_message.get("message", "")
                        source = self.account
                        if not attachments:
                            attachments = sent_message.get("attachments", [])

            # Process image attachments if present
            image_paths = []
            if attachments and source and is_authorized(source):
                image_attachments = [
                    a for a in attachments
                    if a.get("contentType", "") in SUPPORTED_IMAGE_TYPES
                ]
                if image_attachments and self.session:
                    image_paths = await process_attachments(
                        attachments=image_attachments,
                        sender=source,
                        session=self.session,
                        signal_api_url=self.config.signal_api_url,
                        attachments_dir=self.config.attachments_dir,
                    )
                    if image_paths:
                        logger.info(
                            "images_processed",
                            count=len(image_paths),
                            sender="..." + source[-4:],
                        )

            # Allow image-only messages (no text) — default to describe prompt
            if not message_text or not message_text.strip():
                if image_paths:
                    message_text = "Describe this image."
                else:
                    return
            if not source:
                return

            # Feedback loop prevention — ignore our own outgoing messages
            # that arrive back via linked devices (dataMessage or syncMessage)
            prefix = f"[{self.config.instance_name}]"
            if message_text.strip().startswith(prefix):
                return

            # Deduplication
            timestamp = envelope.get("timestamp", 0)
            msg_hash = hashlib.sha256(
                f"{timestamp}:{message_text.strip()}".encode()
            ).hexdigest()
            if msg_hash in self._processed_messages:
                logger.debug("duplicate_message_skipped", timestamp=timestamp)
                return
            self._processed_messages[msg_hash] = _time.time()

            cutoff = _time.time() - 60
            while self._processed_messages:
                oldest_key, oldest_time = next(
                    iter(self._processed_messages.items())
                )
                if oldest_time < cutoff:
                    self._processed_messages.pop(oldest_key)
                else:
                    break

            logger.info(
                "processing_message",
                source="..." + source[-4:], length=len(message_text),
                images=len(image_paths) if image_paths else 0,
            )
            await asyncio.wait_for(
                self._process_message(
                    source, message_text,
                    image_paths=image_paths if image_paths else None,
                ),
                timeout=120,
            )

        except asyncio.TimeoutError:
            logger.error(
                "message_handler_timeout",
                sender="..." + source[-4:] if source else "unknown",
                elapsed=120,
            )
        except Exception as e:
            logger.error(
                "message_handling_error", error=str(e), msg=str(msg)[:200]
            )

    async def run(self):
        """Main run loop: start, poll messages, stop on exit."""
        await self.start()

        try:
            await self.poll_messages()
        finally:
            await self.stop()
