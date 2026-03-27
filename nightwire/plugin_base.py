"""Plugin base class and types for nightwire extensibility."""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

import structlog


# Type alias for command handlers: async (sender: str, args: str) -> str
CommandHandler = Callable[[str, str], Awaitable[str]]


@dataclass
class MessageMatcher:
    """A priority-ordered message interceptor registered by a plugin.

    Attributes:
        priority: Lower numbers are checked first (0-99).
        match_fn: Sync function (message: str) -> bool.
        handle_fn: Async function (sender: str, message: str) -> str.
        description: Human-readable label for logging.
        pre_command: If True, runs before builtin command dispatch.
    """
    priority: int
    match_fn: Callable[[str], bool]
    handle_fn: Callable[[str, str], Awaitable[str]]
    description: str = ""
    pre_command: bool = False


@dataclass
class HelpSection:
    """A block of help text contributed by a plugin.

    Attributes:
        title: Section heading (e.g. "Music Control").
        commands: Dict of command_name -> one-line description.
    """
    title: str
    commands: Dict[str, str] = field(default_factory=dict)


class PluginContext:
    """Interface exposed to plugins for interacting with the bot.

    Plugins receive this in their constructor. They should never
    import bot.py directly.
    """

    def __init__(
        self,
        plugin_name: str,
        send_message: Callable[[str, str], Awaitable[None]],
        settings: dict,
        allowed_numbers: List[str],
        data_dir: Path,
    ):
        self.plugin_name = plugin_name
        self._send_message = send_message
        self._settings = settings
        # Only expose the plugin's own config section, not full settings
        self._plugin_settings = settings.get("plugins", {}).get(plugin_name, {})
        self.allowed_numbers = list(allowed_numbers)  # Read-only copy
        self.data_dir = data_dir
        self.logger = structlog.get_logger(plugin=plugin_name)

    def get_config(self, key: str, default: Any = None) -> Any:
        """Read a value from plugins.<plugin_name>.<key> in settings.yaml."""
        return self._plugin_settings.get(key, default)

    def get_env(self, key: str) -> Optional[str]:
        """Read an environment variable."""
        return os.environ.get(key)

    @property
    def enabled(self) -> bool:
        """Whether this plugin is enabled in config (default True)."""
        return self._plugin_settings.get("enabled", True)

    @property
    def instance_name(self) -> str:
        """The instance name for this bot (e.g., 'nightwire-osx')."""
        return self._settings.get("instance_name", "nightwire")

    @property
    def signal_api_url(self) -> str:
        """The Signal CLI REST API URL."""
        return os.environ.get("SIGNAL_API_URL") or self._settings.get("signal_api_url", "http://127.0.0.1:8080")

    async def send_message(self, recipient: str, message: str) -> None:
        """Send a Signal message to a recipient."""
        await self._send_message(recipient, message)


class NightwirePlugin:
    """Base class for all nightwire plugins.

    Subclass this and override the methods you need.
    Place your plugin in plugins/<name>/plugin.py.
    """

    name: str = ""
    description: str = ""
    version: str = "1.0.0"

    def __init__(self, ctx: PluginContext):
        self.ctx = ctx

    def commands(self) -> Dict[str, CommandHandler]:
        """Return {command_name: async_handler} to register as /commands.

        Handler signature: async (sender: str, args: str) -> str
        """
        return {}

    def message_matchers(self) -> List[MessageMatcher]:
        """Return message matchers for priority-based interception.

        Lower priority numbers are checked first.
        """
        return []

    async def on_start(self) -> None:
        """Called after the bot connects. Initialize resources, start schedulers."""
        pass

    async def on_stop(self) -> None:
        """Called during shutdown. Clean up resources."""
        pass

    def help_sections(self) -> List[HelpSection]:
        """Return help text entries for the /help display."""
        return []


# Backwards compat alias
SidechannelPlugin = NightwirePlugin
