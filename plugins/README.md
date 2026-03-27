# nightwire Plugins

Plugins extend nightwire without modifying core code. Drop a plugin into the `plugins/` directory, restart the bot, and it's live.

## Quick Start

```bash
# 1. Create your plugin directory
mkdir -p plugins/my_plugin
touch plugins/my_plugin/__init__.py

# 2. Write your plugin (see examples below)
vim plugins/my_plugin/plugin.py

# 3. Add config (optional)
# Edit config/settings.yaml, add plugins.my_plugin section

# 4. Restart the bot
systemctl --user restart nightwire
```

## Plugin Structure

```
plugins/my_plugin/
├── __init__.py        # Required (can be empty)
├── plugin.py          # Required — your NightwirePlugin subclass
├── helpers.py         # Optional — additional modules
└── README.md          # Optional — documentation
```

The plugin loader scans each subdirectory of `plugins/` for a `plugin.py` file, imports it, and finds the first `NightwirePlugin` subclass.

## Minimal Example

```python
# plugins/hello_world/plugin.py
from nightwire.plugin_base import NightwirePlugin, HelpSection


class HelloWorldPlugin(NightwirePlugin):
    name = "hello_world"
    description = "A simple greeting plugin"
    version = "1.0.0"

    def commands(self):
        """Register /hello as a command."""
        return {"hello": self._handle_hello}

    async def _handle_hello(self, sender: str, args: str) -> str:
        """Handle the /hello command."""
        name = args.strip() or "world"
        greeting = self.ctx.get_config("greeting", "Hello")
        return f"{greeting}, {name}!"

    def help_sections(self):
        """Add to /help output."""
        return [HelpSection(
            title="Hello World",
            commands={"hello": "Say hello (usage: /hello [name])"},
        )]
```

## Plugin Base Class

All plugins inherit from `NightwirePlugin`:

```python
class NightwirePlugin:
    name: str = ""            # Unique plugin identifier
    description: str = ""     # One-line description
    version: str = "1.0.0"    # Semantic version

    def __init__(self, ctx: PluginContext):
        self.ctx = ctx        # Bot interface

    def commands(self) -> dict[str, CommandHandler]:
        """Return {command_name: handler} to register as /commands.
        Handler signature: async (sender: str, args: str) -> str"""
        return {}

    def message_matchers(self) -> list[MessageMatcher]:
        """Return matchers for priority-based message interception."""
        return []

    async def on_start(self) -> None:
        """Called after bot connects. Start schedulers, open connections."""
        pass

    async def on_stop(self) -> None:
        """Called during shutdown. Cancel tasks, close sessions."""
        pass

    def help_sections(self) -> list[HelpSection]:
        """Return help text entries for /help display."""
        return []
```

## PluginContext API

Your plugin receives a `PluginContext` as `self.ctx`. This is your interface to the bot — plugins should **never** import `bot.py` directly.

| Method / Attribute | Type | Description |
|--------------------|------|-------------|
| `await ctx.send_message(recipient, text)` | async | Send a Signal message to any phone number |
| `ctx.get_config(key, default=None)` | any | Read `plugins.<name>.<key>` from settings.yaml |
| `ctx.get_env(key)` | str or None | Read an environment variable |
| `ctx.logger` | structlog | Structured logger tagged with your plugin name |
| `ctx.allowed_numbers` | list[str] | All authorized phone numbers |
| `ctx.data_dir` | Path | Persistent data directory for your plugin |
| `ctx.enabled` | bool | Whether plugin is enabled in config (default: True) |
| `ctx.plugin_name` | str | Your plugin's name |
| `ctx.instance_name` | str | This bot instance's name (from `instance_name` in settings.yaml) |
| `ctx.signal_api_url` | str | Signal CLI REST API base URL |

### Config Example

```yaml
# config/settings.yaml
plugins:
  my_plugin:
    enabled: true
    api_url: "https://api.example.com"
    max_results: 10
```

```python
# In your plugin
url = self.ctx.get_config("api_url")           # "https://api.example.com"
limit = self.ctx.get_config("max_results", 5)   # 10
missing = self.ctx.get_config("nonexistent")     # None
```

### Environment Variables

For secrets (API keys, tokens), use `.env` instead of settings.yaml:

```bash
# config/.env
MY_PLUGIN_API_KEY=secret123
```

```python
# In your plugin
key = self.ctx.get_env("MY_PLUGIN_API_KEY")  # "secret123"
```

## Commands

Register `/commands` that users can invoke:

```python
def commands(self):
    return {
        "weather": self._handle_weather,    # /weather New York
        "forecast": self._handle_forecast,  # /forecast 5-day
    }

async def _handle_weather(self, sender: str, args: str) -> str:
    """sender is the phone number, args is everything after the command."""
    city = args.strip() or "default"
    return f"Weather in {city}: Sunny, 72°F"
```

**Important:** Command names must be unique across all plugins. If two plugins register the same command, the first-loaded wins (alphabetical directory order) and a warning is logged.

## Message Matchers

Intercept messages based on content, without requiring a `/command` prefix:

```python
from nightwire.plugin_base import MessageMatcher

def message_matchers(self):
    return [
        MessageMatcher(
            priority=10,              # Lower = checked first (0-99)
            match_fn=self._is_match,  # Sync: (message: str) -> bool
            handle_fn=self._handle,   # Async: (sender, message) -> str
            description="My matcher",
        ),
    ]

def _is_match(self, message: str) -> bool:
    """Return True if this plugin should handle the message."""
    return message.lower().startswith("play ")

async def _handle(self, sender: str, message: str) -> str:
    """Handle the matched message."""
    return f"Playing: {message[5:]}"
```

**Priority order:** When multiple plugins register matchers, they're sorted by priority. The first matcher where `match_fn` returns `True` handles the message — no further matchers or default routing are checked.

**Routing order:** `/commands` are always checked before matchers. Matchers are checked before the nightwire assistant and default `/do` routing.

**Pre-command matchers:** Set `pre_command=True` on a `MessageMatcher` to intercept messages *before* the built-in command dispatcher. Use this when your plugin needs to gate or redirect commands (e.g., device targeting). Pre-command matchers fire before `/ask`, `/do`, `/complex`, and other built-in commands.

```python
MessageMatcher(
    priority=0,
    match_fn=self._should_intercept,
    handle_fn=self._intercept,
    description="Gate commands before dispatch",
    pre_command=True,   # Run before built-in /commands
)
```

## Lifecycle Hooks

Use `on_start()` and `on_stop()` for resource management:

```python
import asyncio
import aiohttp

class MyPlugin(NightwirePlugin):
    name = "my_plugin"

    def __init__(self, ctx):
        super().__init__(ctx)
        self._session = None
        self._task = None

    async def on_start(self):
        """Called once after the bot connects to Signal."""
        self._session = aiohttp.ClientSession()
        self._task = asyncio.create_task(self._background_work())
        self.ctx.logger.info("plugin_started")

    async def on_stop(self):
        """Called during bot shutdown. Clean up everything."""
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._session:
            await self._session.close()
        self.ctx.logger.info("plugin_stopped")

    async def _background_work(self):
        """Example background loop."""
        while True:
            try:
                await asyncio.sleep(3600)
                # Do periodic work...
            except asyncio.CancelledError:
                break
```

**Important:** Always cancel background tasks and close sessions in `on_stop()` to prevent resource leaks. Plugins are stopped in reverse load order (LIFO).

## Sending Messages

Plugins can proactively send messages to any authorized number:

```python
# Send to a specific recipient
await self.ctx.send_message("+15551234567", "Your report is ready!")

# Send to all authorized numbers
for number in self.ctx.allowed_numbers:
    await self.ctx.send_message(number, "System alert: server restarted")

# Send to configured recipients
recipients = self.ctx.get_config("recipients", self.ctx.allowed_numbers)
for number in recipients:
    await self.ctx.send_message(number, "Daily update...")
```

## Data Storage

Each plugin gets a persistent data directory at `data/plugins/`:

```python
import json

# Write data
data_file = self.ctx.data_dir / "cache.json"
data_file.write_text(json.dumps({"key": "value"}))

# Read data
if data_file.exists():
    data = json.loads(data_file.read_text())
```

## Disabling Plugins

Set `enabled: false` in config to disable a plugin without removing it:

```yaml
plugins:
  my_plugin:
    enabled: false
```

The plugin loader skips disabled plugins entirely — no import, no initialization.

## Error Handling

The plugin loader is designed to be resilient:

- **Import errors:** If your `plugin.py` raises an exception during import, it's logged and skipped. Other plugins still load normally.
- **Lifecycle errors:** If `on_start()` or `on_stop()` raises, it's caught and logged per-plugin.
- **Command errors:** Unhandled exceptions in command handlers are caught by the bot and reported to the user.
- **Matcher errors:** Unhandled exceptions in matcher handlers are caught and logged.

## Logging

Use `self.ctx.logger` for structured logging:

```python
self.ctx.logger.info("event_name", key="value", count=42)
self.ctx.logger.error("something_failed", error=str(e))
self.ctx.logger.warning("rate_limited", user=sender[:6] + "...")
```

Logs are automatically tagged with your plugin name. Follow the convention `plugin_action_detail` for event names.

## Testing Your Plugin

```python
# tests/test_plugin_my_plugin.py
import sys
from pathlib import Path
from unittest.mock import AsyncMock
import pytest
from nightwire.plugin_base import PluginContext

@pytest.fixture(autouse=True)
def _add_plugins_to_path():
    plugins_dir = str(Path(__file__).parent.parent / "plugins")
    if plugins_dir not in sys.path:
        sys.path.insert(0, plugins_dir)

def _make_ctx(tmp_path, config=None):
    return PluginContext(
        plugin_name="my_plugin",
        send_message=AsyncMock(),
        settings={"plugins": {"my_plugin": config or {}}},
        allowed_numbers=["+15551234567"],
        data_dir=tmp_path,
    )

class TestMyPlugin:
    def test_commands_registered(self, tmp_path):
        from my_plugin.plugin import MyPlugin
        plugin = MyPlugin(_make_ctx(tmp_path))
        assert "mycommand" in plugin.commands()

    @pytest.mark.asyncio
    async def test_command_handler(self, tmp_path):
        from my_plugin.plugin import MyPlugin
        plugin = MyPlugin(_make_ctx(tmp_path))
        result = await plugin.commands()["mycommand"]("+15551234567", "test args")
        assert "test args" in result
```

Run tests: `python -m pytest tests/test_plugin_my_plugin.py -v`

## Built-in Plugins

### Device Targeting (`device_target`)

Routes task commands (`/do`, `/ask`, `/complex`, `/summary`) to a specific nightwire instance when multiple instances share the same Signal account.

**Setup:**

1. Give each instance a unique `instance_name` in `config/settings.yaml`:

```yaml
instance_name: "nightwire-osx"
```

2. Add the plugin config:

```yaml
plugins:
  device_target:
    enabled: true
    signal_account: "+15551234567"  # Your Signal account number
    refresh_interval: 300           # Device list refresh interval in seconds
```

**Commands:**

| Command | Description |
|---------|-------------|
| `/target` | Show available instances and pick one |
| `/target <name>` | Set target (e.g., `/target osx` matches `nightwire-osx`) |
| `/target status` | Show current target |
| `/target clear` | Clear target (commands run on all instances again) |

**How it works:**

On startup, the plugin queries the Signal API for linked devices whose names contain "nightwire". When you `/target osx`, subsequent `/do`, `/ask`, `/complex`, and `/summary` commands only execute on the instance named `nightwire-osx`. Other instances receive the message but silently ignore gated commands. Non-task commands (`/help`, `/status`, `/projects`) always run on every instance.

