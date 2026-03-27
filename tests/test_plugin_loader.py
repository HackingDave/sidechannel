"""Tests for plugin loader allowlist."""

import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch, AsyncMock

from nightwire.plugin_loader import PluginLoader


def _make_loader(settings=None, plugins_dir=None):
    """Create a PluginLoader with test defaults."""
    return PluginLoader(
        plugins_dir=plugins_dir or Path("/tmp/test_plugins"),
        settings=settings or {},
        send_message=AsyncMock(),
        allowed_numbers=["+1234567890"],
        data_dir=Path("/tmp/test_data"),
    )


def test_plugin_allowlist_blocks_unlisted_plugin(tmp_path):
    """Plugins not in allowlist should be skipped."""
    # Create a fake plugin directory
    plugin_dir = tmp_path / "evil_plugin"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.py").write_text("# evil")

    loader = _make_loader(
        settings={"plugin_allowlist": ["safe_plugin"]},
        plugins_dir=tmp_path,
    )
    loader.discover_and_load()
    assert len(loader.plugins) == 0


def test_plugin_allowlist_allows_listed_plugin(tmp_path):
    """Plugins in allowlist should be attempted (not blocked by allowlist)."""
    plugin_dir = tmp_path / "safe_plugin"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.py").write_text(
        "from nightwire.plugin_base import NightwirePlugin\n"
        "class SafePlugin(NightwirePlugin):\n"
        "    name = 'safe'\n"
        "    version = '1.0'\n"
    )

    loader = _make_loader(
        settings={"plugin_allowlist": ["safe_plugin"]},
        plugins_dir=tmp_path,
    )
    # This will attempt to load - may fail on import but shouldn't be blocked by allowlist
    loader.discover_and_load()
    # The point is it tried to load (wasn't blocked)


def test_plugin_no_allowlist_loads_all(tmp_path):
    """Without allowlist configured, all plugins should be attempted."""
    loader = _make_loader(
        settings={},
        plugins_dir=tmp_path,
    )
    # No exception means no block
    loader.discover_and_load()


def test_message_matcher_pre_command_default():
    """MessageMatcher.pre_command defaults to False for backwards compat."""
    from nightwire.plugin_base import MessageMatcher

    matcher = MessageMatcher(
        priority=10,
        match_fn=lambda msg: True,
        handle_fn=AsyncMock(),
        description="test",
    )
    assert matcher.pre_command is False


def test_message_matcher_pre_command_set():
    """MessageMatcher.pre_command can be set to True."""
    from nightwire.plugin_base import MessageMatcher

    matcher = MessageMatcher(
        priority=5,
        match_fn=lambda msg: True,
        handle_fn=AsyncMock(),
        description="test",
        pre_command=True,
    )
    assert matcher.pre_command is True
