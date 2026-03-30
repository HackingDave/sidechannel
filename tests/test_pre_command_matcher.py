"""Tests for pre-command message matcher interception in bot."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from nightwire.plugin_base import MessageMatcher


@pytest.fixture
def bot_mocks():
    """Create a minimal mock bot with the _process_message dependencies."""
    bot = MagicMock()
    bot._send_message = AsyncMock()
    bot.config = MagicMock()
    bot.config.instance_name = "nightwire-test"
    bot.memory = MagicMock()
    bot.memory.store_message = AsyncMock()
    bot.project_manager = MagicMock()
    bot.project_manager.get_current_project.return_value = None
    bot.plugin_loader = MagicMock()
    bot._handle_command = AsyncMock(return_value="command response")
    bot.cooldown_manager = None
    bot.nightwire_runner = None
    return bot


@pytest.fixture(autouse=True)
def patch_security():
    """Bypass auth and rate-limit checks so tests reach the matcher logic."""
    with patch("nightwire.bot.is_authorized", return_value=True), \
         patch("nightwire.bot.check_rate_limit", return_value="allowed"):
        yield


class TestPreCommandMatcher:
    """Test that pre_command matchers intercept before command dispatch."""

    @pytest.mark.asyncio
    async def test_pre_command_matcher_blocks_do_command(self, bot_mocks):
        """A pre_command matcher returning a string should block /do dispatch."""
        handler = AsyncMock(return_value="blocked by matcher")
        matcher = MessageMatcher(
            priority=5,
            match_fn=lambda msg: msg.startswith("/do"),
            handle_fn=handler,
            description="test gate",
            pre_command=True,
        )
        bot_mocks.plugin_loader.get_sorted_matchers.return_value = [matcher]

        from nightwire.bot import SignalBot
        await SignalBot._process_message(bot_mocks, "+1234567890", "/do fix bug")

        handler.assert_called_once_with("+1234567890", "/do fix bug")
        bot_mocks._send_message.assert_called_with("+1234567890", "blocked by matcher")

    @pytest.mark.asyncio
    async def test_pre_command_matcher_silent_consume(self, bot_mocks):
        """A pre_command matcher returning empty string should silently consume."""
        handler = AsyncMock(return_value="")
        matcher = MessageMatcher(
            priority=5,
            match_fn=lambda msg: msg.startswith("/do"),
            handle_fn=handler,
            description="test gate",
            pre_command=True,
        )
        bot_mocks.plugin_loader.get_sorted_matchers.return_value = [matcher]

        from nightwire.bot import SignalBot
        await SignalBot._process_message(bot_mocks, "+1234567890", "/do fix bug")

        handler.assert_called_once()
        # Should NOT send any message (silently consumed)
        bot_mocks._send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_pre_command_matcher_passthrough(self, bot_mocks):
        """A pre_command matcher returning None should pass through to normal handling."""
        handler = AsyncMock(return_value=None)
        matcher = MessageMatcher(
            priority=5,
            match_fn=lambda msg: msg.startswith("/do"),
            handle_fn=handler,
            description="test gate",
            pre_command=True,
        )
        bot_mocks.plugin_loader.get_sorted_matchers.return_value = [matcher]

        from nightwire.bot import SignalBot
        await SignalBot._process_message(bot_mocks, "+1234567890", "/do fix bug")

        handler.assert_called_once()
        # Normal command processing should have continued — /do with no project
        # selected returns "No project selected" via _handle_command

    @pytest.mark.asyncio
    async def test_non_pre_command_matcher_ignored_for_commands(self, bot_mocks):
        """Regular (non-pre_command) matchers should NOT intercept / commands."""
        handler = AsyncMock(return_value="should not run")
        matcher = MessageMatcher(
            priority=5,
            match_fn=lambda msg: True,
            handle_fn=handler,
            description="regular matcher",
            pre_command=False,
        )
        bot_mocks.plugin_loader.get_sorted_matchers.return_value = [matcher]

        from nightwire.bot import SignalBot
        await SignalBot._process_message(bot_mocks, "+1234567890", "/help")

        handler.assert_not_called()
