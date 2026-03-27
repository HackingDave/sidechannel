"""Tests for device_target plugin."""

import json
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from nightwire.plugin_base import PluginContext


def _make_ctx(tmp_path, instance_name="nightwire-osx", signal_api_url="http://127.0.0.1:8080"):
    """Create a PluginContext for testing."""
    data_dir = tmp_path / "device_target"
    data_dir.mkdir(parents=True, exist_ok=True)
    return PluginContext(
        plugin_name="device_target",
        send_message=AsyncMock(),
        settings={
            "instance_name": instance_name,
            "signal_api_url": signal_api_url,
            "plugins": {
                "device_target": {
                    "signal_account": "+15551234567",
                    "refresh_interval": 300,
                },
            },
        },
        allowed_numbers=["+15551234567"],
        data_dir=data_dir,
    )


def _make_plugin(ctx):
    """Import and instantiate the plugin."""
    import importlib.util
    import sys

    plugin_path = Path(__file__).parent.parent / "plugins" / "device_target" / "plugin.py"
    spec = importlib.util.spec_from_file_location("device_target.plugin", plugin_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["device_target.plugin"] = module
    spec.loader.exec_module(module)

    # Find the plugin class
    for attr in dir(module):
        cls = getattr(module, attr)
        if isinstance(cls, type) and hasattr(cls, 'name') and cls.__name__ != 'NightwirePlugin':
            from nightwire.plugin_base import NightwirePlugin
            if issubclass(cls, NightwirePlugin) and cls is not NightwirePlugin:
                return cls(ctx)
    raise RuntimeError("No plugin class found")


class TestTargetState:
    """Test per-sender target persistence."""

    def test_load_empty(self, tmp_path):
        ctx = _make_ctx(tmp_path)
        plugin = _make_plugin(ctx)
        assert plugin._targets == {}

    def test_save_and_load(self, tmp_path):
        ctx = _make_ctx(tmp_path)
        plugin = _make_plugin(ctx)
        plugin._targets["+15559999999"] = "nightwire-linux"
        plugin._save_targets()

        # Reload
        plugin2 = _make_plugin(ctx)
        plugin2._load_targets()
        assert plugin2._targets["+15559999999"] == "nightwire-linux"

    def test_clear_target(self, tmp_path):
        ctx = _make_ctx(tmp_path)
        plugin = _make_plugin(ctx)
        plugin._targets["+15559999999"] = "nightwire-linux"
        plugin._save_targets()
        del plugin._targets["+15559999999"]
        plugin._save_targets()

        plugin2 = _make_plugin(ctx)
        plugin2._load_targets()
        assert "+15559999999" not in plugin2._targets


class TestDeviceMatching:
    """Test flexible name matching for /target <name>."""

    def test_exact_match(self, tmp_path):
        ctx = _make_ctx(tmp_path)
        plugin = _make_plugin(ctx)
        plugin._devices = ["nightwire-osx", "nightwire-linux"]
        assert plugin._match_device("nightwire-osx") == "nightwire-osx"

    def test_suffix_match(self, tmp_path):
        ctx = _make_ctx(tmp_path)
        plugin = _make_plugin(ctx)
        plugin._devices = ["nightwire-osx", "nightwire-linux"]
        assert plugin._match_device("osx") == "nightwire-osx"
        assert plugin._match_device("linux") == "nightwire-linux"

    def test_case_insensitive(self, tmp_path):
        ctx = _make_ctx(tmp_path)
        plugin = _make_plugin(ctx)
        plugin._devices = ["nightwire-osx", "nightwire-linux"]
        assert plugin._match_device("OSX") == "nightwire-osx"
        assert plugin._match_device("Linux") == "nightwire-linux"

    def test_no_match(self, tmp_path):
        ctx = _make_ctx(tmp_path)
        plugin = _make_plugin(ctx)
        plugin._devices = ["nightwire-osx", "nightwire-linux"]
        assert plugin._match_device("windows") is None


class TestMessageGating:
    """Test pre-command matcher logic."""

    @pytest.mark.asyncio
    async def test_no_target_set_blocks(self, tmp_path):
        """With no target set, task commands return a prompt."""
        ctx = _make_ctx(tmp_path, instance_name="nightwire-osx")
        plugin = _make_plugin(ctx)
        plugin._devices = ["nightwire-osx", "nightwire-linux"]

        result = await plugin._handle_gate("+15559999999", "/do fix the bug")
        assert result is not None
        assert "No target set" in result

    @pytest.mark.asyncio
    async def test_single_instance_passes_through(self, tmp_path):
        """With fewer than 2 devices, gate always passes through."""
        ctx = _make_ctx(tmp_path, instance_name="nightwire-osx")
        plugin = _make_plugin(ctx)
        plugin._devices = ["nightwire-osx"]

        result = await plugin._handle_gate("+15559999999", "/do fix the bug")
        assert result is None

    @pytest.mark.asyncio
    async def test_target_matches_passes_through(self, tmp_path):
        """When target matches this instance, return None (pass through)."""
        ctx = _make_ctx(tmp_path, instance_name="nightwire-osx")
        plugin = _make_plugin(ctx)
        plugin._devices = ["nightwire-osx", "nightwire-linux"]
        plugin._targets["+15559999999"] = "nightwire-osx"

        result = await plugin._handle_gate("+15559999999", "/do fix the bug")
        assert result is None

    @pytest.mark.asyncio
    async def test_target_mismatch_silently_consumes(self, tmp_path):
        """When target doesn't match, return empty string (silent consume)."""
        ctx = _make_ctx(tmp_path, instance_name="nightwire-osx")
        plugin = _make_plugin(ctx)
        plugin._devices = ["nightwire-osx", "nightwire-linux"]
        plugin._targets["+15559999999"] = "nightwire-linux"

        result = await plugin._handle_gate("+15559999999", "/do fix the bug")
        assert result == ""

    @pytest.mark.asyncio
    async def test_gates_plain_text(self, tmp_path):
        """Plain text (implicit /do) should also be gated."""
        ctx = _make_ctx(tmp_path, instance_name="nightwire-osx")
        plugin = _make_plugin(ctx)
        plugin._devices = ["nightwire-osx", "nightwire-linux"]
        plugin._targets["+15559999999"] = "nightwire-linux"

        result = await plugin._handle_gate("+15559999999", "fix the bug please")
        assert result == ""

    @pytest.mark.asyncio
    async def test_does_not_gate_passthrough_commands(self, tmp_path):
        """Only /help and /target should pass through ungated."""
        ctx = _make_ctx(tmp_path, instance_name="nightwire-osx")
        plugin = _make_plugin(ctx)
        plugin._targets["+15559999999"] = "nightwire-linux"

        matchers = plugin.message_matchers()
        gate_matcher = [m for m in matchers if m.pre_command][0]
        assert gate_matcher.match_fn("/help") is False
        assert gate_matcher.match_fn("/target osx") is False

    @pytest.mark.asyncio
    async def test_gates_all_other_commands(self, tmp_path):
        """All commands except /help and /target should be gated."""
        ctx = _make_ctx(tmp_path, instance_name="nightwire-osx")
        plugin = _make_plugin(ctx)
        plugin._targets["+15559999999"] = "nightwire-linux"

        matchers = plugin.message_matchers()
        gate_matcher = [m for m in matchers if m.pre_command][0]
        assert gate_matcher.match_fn("/do fix bug") is True
        assert gate_matcher.match_fn("/ask what is this") is True
        assert gate_matcher.match_fn("/complex build auth") is True
        assert gate_matcher.match_fn("/summary") is True
        assert gate_matcher.match_fn("/select NightBeacon") is True
        assert gate_matcher.match_fn("/status") is True
        assert gate_matcher.match_fn("/projects") is True


class TestTargetCommand:
    """Test /target command handling."""

    @pytest.mark.asyncio
    async def test_target_set_responds_from_targeted_instance(self, tmp_path):
        """Only the newly targeted instance should respond."""
        ctx = _make_ctx(tmp_path, instance_name="nightwire-osx")
        plugin = _make_plugin(ctx)
        plugin._devices = ["nightwire-osx", "nightwire-linux"]

        result = await plugin._handle_target("+15559999999", "osx")
        assert "nightwire-osx" in result
        assert plugin._targets["+15559999999"] == "nightwire-osx"

    @pytest.mark.asyncio
    async def test_target_set_silent_on_other_instance(self, tmp_path):
        """Non-targeted instance should return None (no response)."""
        ctx = _make_ctx(tmp_path, instance_name="nightwire-linux")
        plugin = _make_plugin(ctx)
        plugin._devices = ["nightwire-osx", "nightwire-linux"]

        result = await plugin._handle_target("+15559999999", "osx")
        assert result is None
        assert plugin._targets["+15559999999"] == "nightwire-osx"

    @pytest.mark.asyncio
    async def test_target_clear_responds_from_first_device(self, tmp_path):
        """After clear, first device alphabetically responds."""
        ctx = _make_ctx(tmp_path, instance_name="nightwire-linux")
        plugin = _make_plugin(ctx)
        plugin._devices = ["nightwire-linux", "nightwire-osx"]
        plugin._targets["+15559999999"] = "nightwire-osx"

        result = await plugin._handle_target("+15559999999", "clear")
        assert "+15559999999" not in plugin._targets
        assert "cleared" in result.lower()

    @pytest.mark.asyncio
    async def test_target_clear_silent_on_other_instance(self, tmp_path):
        """After clear, non-first device returns None."""
        ctx = _make_ctx(tmp_path, instance_name="nightwire-osx")
        plugin = _make_plugin(ctx)
        plugin._devices = ["nightwire-linux", "nightwire-osx"]
        plugin._targets["+15559999999"] = "nightwire-osx"

        result = await plugin._handle_target("+15559999999", "clear")
        assert "+15559999999" not in plugin._targets
        assert result is None

    @pytest.mark.asyncio
    async def test_target_status(self, tmp_path):
        ctx = _make_ctx(tmp_path, instance_name="nightwire-osx")
        plugin = _make_plugin(ctx)
        plugin._targets["+15559999999"] = "nightwire-osx"
        plugin._devices = ["nightwire-osx", "nightwire-linux"]

        result = await plugin._handle_target("+15559999999", "status")
        assert "nightwire-osx" in result

    @pytest.mark.asyncio
    async def test_target_no_args_shows_picker(self, tmp_path):
        """With no target, first device alphabetically responds."""
        ctx = _make_ctx(tmp_path, instance_name="nightwire-linux")
        plugin = _make_plugin(ctx)
        plugin._devices = ["nightwire-linux", "nightwire-osx"]

        result = await plugin._handle_target("+15559999999", "")
        assert "nightwire-osx" in result
        assert "nightwire-linux" in result

    @pytest.mark.asyncio
    async def test_target_auto_select_single(self, tmp_path):
        ctx = _make_ctx(tmp_path, instance_name="nightwire-osx")
        plugin = _make_plugin(ctx)
        plugin._devices = ["nightwire-osx"]

        result = await plugin._handle_target("+15559999999", "")
        assert plugin._targets["+15559999999"] == "nightwire-osx"

    @pytest.mark.asyncio
    async def test_target_unknown_name(self, tmp_path):
        """Unknown name — responds from first device alphabetically when no target set."""
        ctx = _make_ctx(tmp_path, instance_name="nightwire-linux")
        plugin = _make_plugin(ctx)
        plugin._devices = ["nightwire-linux", "nightwire-osx"]

        result = await plugin._handle_target("+15559999999", "windows")
        assert "No matching" in result or "not found" in result.lower()
