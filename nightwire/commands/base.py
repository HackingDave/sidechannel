"""Base classes for the command handler framework.

Defines the abstractions for registering and dispatching bot
commands. Command handlers are grouped into classes that extend
BaseCommandHandler, then registered with a HandlerRegistry that
maps command names to async callables.

Key classes:
    BotContext: Dependency container shared by all handlers.
    BaseCommandHandler: ABC that handler groups must implement.
    HandlerRegistry: Maps command names to handler callables.

Constants:
    BUILTIN_COMMANDS: Frozenset of reserved command names that
        plugins are not allowed to override.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Awaitable, Callable, Dict, List, Optional

import structlog

if TYPE_CHECKING:
    from ..autonomous import AutonomousCommands, AutonomousManager
    from ..claude_runner import ClaudeRunner
    from ..config import Config
    from ..memory import MemoryCommands, MemoryManager
    from ..nightwire_runner import NightwireRunner
    from ..plugin_loader import PluginLoader
    from ..project_manager import ProjectManager
    from ..rate_limit_cooldown import CooldownManager
    from ..task_manager import TaskManager
    from ..updater import AutoUpdater

logger = structlog.get_logger("nightwire.bot")

# Single source of truth for builtin command names.
# plugin_loader.py imports this to block plugin overrides.
BUILTIN_COMMANDS = frozenset({
    "help", "projects", "select", "add", "remove", "new", "status",
    "ask", "do", "complex", "cancel", "summary",
    "cooldown", "update", "nightwire", "sidechannel",
    "global", "diagnose",
    "remember", "recall", "history", "forget", "memories", "preferences",
    "prd", "story", "task", "tasks", "autonomous", "queue", "learnings",
})


@dataclass
class BotContext:
    """Dependency container for command handlers.

    Provides typed access to shared services without coupling handlers
    to SignalBot. Deferred fields (set during start()) use underscore
    storage with property getters that raise RuntimeError if accessed
    before initialization.
    """

    config: "Config"
    runner: "ClaudeRunner"
    project_manager: "ProjectManager"
    memory: "MemoryManager"
    memory_commands: "MemoryCommands"
    plugin_loader: "PluginLoader"
    send_message: Callable[[str, str], Awaitable[None]]
    task_manager: "TaskManager"
    get_memory_context: Callable[..., Awaitable[Optional[str]]]
    # Set after registry creation — used by /help <command>
    registry: Optional["HandlerRegistry"] = None
    # nightwire_runner is Optional — legitimately None when feature disabled
    nightwire_runner: Optional["NightwireRunner"] = None
    # Deferred fields — set in start(), guarded by properties
    _autonomous_manager: Optional["AutonomousManager"] = field(
        default=None, repr=False
    )
    _autonomous_commands: Optional["AutonomousCommands"] = field(
        default=None, repr=False
    )
    _updater: Optional["AutoUpdater"] = field(default=None, repr=False)
    _cooldown_manager: Optional["CooldownManager"] = field(
        default=None, repr=False
    )

    @property
    def autonomous_manager(self) -> "AutonomousManager":
        if self._autonomous_manager is None:
            raise RuntimeError("Bot not started — autonomous_manager not available")
        return self._autonomous_manager

    @property
    def autonomous_commands(self) -> "AutonomousCommands":
        if self._autonomous_commands is None:
            raise RuntimeError("Bot not started — autonomous_commands not available")
        return self._autonomous_commands

    @property
    def updater(self) -> "AutoUpdater":
        if self._updater is None:
            raise RuntimeError("Bot not started — updater not available")
        return self._updater

    @property
    def cooldown_manager(self) -> "CooldownManager":
        if self._cooldown_manager is None:
            raise RuntimeError("Bot not started — cooldown_manager not available")
        return self._cooldown_manager

    @property
    def cooldown_active(self) -> bool:
        """Check if cooldown is active. Safe to call before start() — returns False."""
        return self._cooldown_manager is not None and self._cooldown_manager.is_active


@dataclass
class HelpMetadata:
    """Per-command help information for ``/help <command>``.

    Attributes:
        description: One-line description of the command.
        usage: Usage pattern (e.g. ``/select <project>``).
        examples: List of Signal usage example strings.
    """

    description: str
    usage: str
    examples: List[str]


class BaseCommandHandler(ABC):
    """Abstract base class for command handler groups.

    Subclasses implement get_commands() to return a dict mapping
    command names to async handler functions. Each handler receives
    (sender, args) and returns an optional response string.

    Args:
        ctx: Shared BotContext dependency container.
    """

    def __init__(self, ctx: BotContext):
        self.ctx = ctx

    @abstractmethod
    def get_commands(self) -> Dict[str, Callable[..., Awaitable[Optional[str]]]]:
        """Return {command_name: async_handler} mapping.

        Handler signature: async (sender: str, args: str) -> Optional[str]
        Returning None means the response will be sent asynchronously.
        """
        ...

    def get_help_metadata(self) -> Dict[str, HelpMetadata]:
        """Return per-command help metadata for ``/help <command>``.

        Override in subclasses to provide rich help text.
        """
        return {}

    def get_help_lines(self) -> str:
        """Return help text section for this handler group."""
        return ""


class HandlerRegistry:
    """Maps command names to handler callables.

    Supports two registration modes: register() for
    BaseCommandHandler subclasses, and register_external() for
    plain dicts of command name -> async callable (used by
    autonomous and memory subsystems).
    """

    def __init__(self):
        self._handlers: Dict[str, Callable[..., Awaitable[Optional[str]]]] = {}
        self._help_metadata: Dict[str, HelpMetadata] = {}

    def register(self, handler: BaseCommandHandler) -> None:
        """Register all commands from a BaseCommandHandler subclass.

        Also merges any HelpMetadata from the handler.

        Args:
            handler: Handler instance whose get_commands() dict
                will be merged into the registry.
        """
        for cmd_name, method in handler.get_commands().items():
            if cmd_name in self._handlers:
                logger.warning(
                    "command_handler_conflict",
                    command=cmd_name,
                    handler=type(handler).__name__,
                )
            self._handlers[cmd_name] = method
        self._help_metadata.update(handler.get_help_metadata())

    def register_external(
        self, commands: Dict[str, Callable[..., Awaitable[Optional[str]]]]
    ) -> None:
        """Register commands from a plain dict.

        Used for subsystems that don't extend BaseCommandHandler
        (e.g., AutonomousCommands, memory commands).

        Args:
            commands: Mapping of command name to async handler.
        """
        for cmd_name, method in commands.items():
            if cmd_name in self._handlers:
                logger.warning(
                    "command_handler_conflict",
                    command=cmd_name,
                    source="external",
                )
            self._handlers[cmd_name] = method

    def get(
        self, command: str
    ) -> Optional[Callable[..., Awaitable[Optional[str]]]]:
        """Look up a handler for a command name."""
        return self._handlers.get(command)

    def get_help(self, command: str) -> Optional[HelpMetadata]:
        """Look up HelpMetadata for a command name."""
        return self._help_metadata.get(command)

    @property
    def command_names(self) -> frozenset:
        """All registered command names."""
        return frozenset(self._handlers.keys())
