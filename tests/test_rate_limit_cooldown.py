"""Tests for rate limit cooldown feature."""

import asyncio
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Patch config before importing modules under test
_mock_settings = {}


def _mock_get_config():
    config = MagicMock()
    config.settings = _mock_settings
    return config


# -------------------------------------------------------------------
# CooldownManager tests
# -------------------------------------------------------------------

class TestCooldownManager:
    """Tests for CooldownManager."""

    def _make_manager(self, **overrides):
        """Create a CooldownManager with optional config overrides."""
        settings = {"rate_limit_cooldown": {**overrides}}
        with patch("nightwire.rate_limit_cooldown.get_config") as mock_cfg:
            cfg = MagicMock()
            cfg.settings = settings
            mock_cfg.return_value = cfg
            from nightwire.rate_limit_cooldown import CooldownManager
            return CooldownManager()

    def test_default_state_inactive(self):
        mgr = self._make_manager()
        state = mgr.get_state()
        assert state.active is False
        assert state.remaining_minutes == 0
        assert state.user_message == ""

    def test_is_active_false_by_default(self):
        mgr = self._make_manager()
        assert mgr.is_active is False

    @pytest.mark.asyncio
    async def test_activate_sets_active(self):
        mgr = self._make_manager(cooldown_minutes=10)
        mgr.activate()
        assert mgr.is_active is True
        state = mgr.get_state()
        assert state.active is True
        assert state.remaining_minutes <= 10
        assert "cooldown" in state.user_message.lower()
        # Cleanup
        mgr.cancel_timer()

    @pytest.mark.asyncio
    async def test_deactivate_clears_state(self):
        mgr = self._make_manager()
        mgr.activate()
        assert mgr.is_active is True
        mgr.deactivate()
        assert mgr.is_active is False
        state = mgr.get_state()
        assert state.active is False

    @pytest.mark.asyncio
    async def test_activate_with_custom_minutes(self):
        mgr = self._make_manager(cooldown_minutes=30)
        mgr.activate(cooldown_minutes=5)
        state = mgr.get_state()
        assert state.active is True
        assert state.remaining_minutes <= 5
        mgr.cancel_timer()

    def test_enabled_false_blocks_activate(self):
        mgr = self._make_manager(enabled=False)
        mgr.activate()
        assert mgr.is_active is False

    def test_enabled_false_blocks_record_failure(self):
        mgr = self._make_manager(enabled=False, consecutive_threshold=1)
        mgr.record_rate_limit_failure()
        assert mgr.is_active is False

    # --- Consecutive failure threshold ---

    def test_failures_below_threshold_no_activation(self):
        mgr = self._make_manager(consecutive_threshold=3)
        mgr.record_rate_limit_failure()
        mgr.record_rate_limit_failure()
        assert mgr.is_active is False

    @pytest.mark.asyncio
    async def test_failures_at_threshold_activates(self):
        mgr = self._make_manager(consecutive_threshold=3, cooldown_minutes=5)
        mgr.record_rate_limit_failure()
        mgr.record_rate_limit_failure()
        mgr.record_rate_limit_failure()
        assert mgr.is_active is True
        mgr.cancel_timer()

    @pytest.mark.asyncio
    async def test_failures_outside_window_pruned(self):
        mgr = self._make_manager(
            consecutive_threshold=2,
            failure_window_seconds=10,
        )
        # Record a failure in the past (outside window)
        from nightwire.rate_limit_cooldown import _FailureRecord
        mgr._failures.append(_FailureRecord(timestamp=time.time() - 20))
        # Record one more within the window
        mgr.record_rate_limit_failure()
        # Should NOT activate — old failure was pruned
        assert mgr.is_active is False

    @pytest.mark.asyncio
    async def test_deactivate_clears_failures(self):
        mgr = self._make_manager(consecutive_threshold=3, cooldown_minutes=5)
        mgr.record_rate_limit_failure()
        mgr.record_rate_limit_failure()
        mgr.record_rate_limit_failure()
        assert mgr.is_active is True
        mgr.deactivate()
        assert len(mgr._failures) == 0

    # --- Auto-resume ---

    @pytest.mark.asyncio
    async def test_auto_resume(self):
        mgr = self._make_manager(cooldown_minutes=1)
        # Activate with very short cooldown for testing
        mgr._do_activate(cooldown_minutes=1)
        assert mgr.is_active is True

        # Manually trigger the resume by setting a very short delay
        mgr.cancel_timer()
        mgr._schedule_resume(0.05)  # 50ms
        await asyncio.sleep(0.15)
        assert mgr.is_active is False

    # --- Callbacks ---

    @pytest.mark.asyncio
    async def test_activate_fires_callbacks(self):
        mgr = self._make_manager(cooldown_minutes=5)
        activated = asyncio.Event()

        async def on_activate():
            activated.set()

        mgr.on_activate(on_activate)
        mgr.activate()
        await asyncio.sleep(0.05)
        assert activated.is_set()
        mgr.cancel_timer()

    @pytest.mark.asyncio
    async def test_deactivate_fires_callbacks(self):
        mgr = self._make_manager(cooldown_minutes=5)
        deactivated = asyncio.Event()

        async def on_deactivate():
            deactivated.set()

        mgr.on_deactivate(on_deactivate)
        mgr.activate()
        await asyncio.sleep(0.05)
        mgr.deactivate()
        await asyncio.sleep(0.05)
        assert deactivated.is_set()

    @pytest.mark.asyncio
    async def test_callback_error_does_not_propagate(self):
        mgr = self._make_manager(cooldown_minutes=5)

        async def bad_callback():
            raise RuntimeError("boom")

        mgr.on_activate(bad_callback)
        # Should not raise
        mgr.activate()
        await asyncio.sleep(0.05)
        assert mgr.is_active is True
        mgr.cancel_timer()

    # --- cancel_timer ---

    @pytest.mark.asyncio
    async def test_cancel_timer(self):
        mgr = self._make_manager(cooldown_minutes=5)
        mgr.activate()
        assert mgr._resume_task is not None
        mgr.cancel_timer()
        assert mgr._resume_task is None

    # --- Config overrides ---

    def test_config_overrides(self):
        mgr = self._make_manager(
            cooldown_minutes=120,
            consecutive_threshold=5,
            failure_window_seconds=600,
            enabled=True,
        )
        assert mgr.cooldown_minutes == 120
        assert mgr.consecutive_threshold == 5
        assert mgr.failure_window_seconds == 600
        assert mgr.enabled is True

    def test_default_config_values(self):
        mgr = self._make_manager()
        assert mgr.cooldown_minutes == 60
        assert mgr.consecutive_threshold == 3
        assert mgr.failure_window_seconds == 300
        assert mgr.enabled is True


# -------------------------------------------------------------------
# classify_error tests
# -------------------------------------------------------------------

class TestClassifyErrorRateLimited:
    """Test that classify_error returns RATE_LIMITED for subscription patterns."""

    def test_usage_limit(self):
        from nightwire.claude_runner import ErrorCategory, classify_error
        result = classify_error(1, "", "429 rate limit: usage limit exceeded")
        assert result == ErrorCategory.RATE_LIMITED

    def test_daily_limit(self):
        from nightwire.claude_runner import ErrorCategory, classify_error
        result = classify_error(1, "429 rate limit: daily limit reached", "")
        assert result == ErrorCategory.RATE_LIMITED

    def test_quota_exceeded(self):
        from nightwire.claude_runner import ErrorCategory, classify_error
        result = classify_error(1, "", "429: quota exceeded for this account")
        assert result == ErrorCategory.RATE_LIMITED

    def test_too_many_requests(self):
        from nightwire.claude_runner import ErrorCategory, classify_error
        result = classify_error(1, "429 too many requests", "")
        assert result == ErrorCategory.RATE_LIMITED

    def test_try_again_later(self):
        from nightwire.claude_runner import ErrorCategory, classify_error
        result = classify_error(1, "", "rate limit hit, try again later")
        assert result == ErrorCategory.RATE_LIMITED

    def test_capacity(self):
        from nightwire.claude_runner import ErrorCategory, classify_error
        result = classify_error(1, "429 rate limit: at capacity", "")
        assert result == ErrorCategory.RATE_LIMITED

    def test_overloaded(self):
        from nightwire.claude_runner import ErrorCategory, classify_error
        result = classify_error(1, "", "rate limit: overloaded")
        assert result == ErrorCategory.RATE_LIMITED

    def test_plain_rate_limit_stays_transient(self):
        """A plain rate limit without subscription patterns stays TRANSIENT."""
        from nightwire.claude_runner import ErrorCategory, classify_error
        result = classify_error(1, "", "rate limit exceeded")
        assert result == ErrorCategory.TRANSIENT

    def test_plain_429_stays_transient(self):
        from nightwire.claude_runner import ErrorCategory, classify_error
        result = classify_error(1, "HTTP 429", "")
        assert result == ErrorCategory.TRANSIENT


# -------------------------------------------------------------------
# run_claude cooldown integration
# -------------------------------------------------------------------

class TestRunClaudeCooldown:
    """Test that run_claude respects cooldown state."""

    @pytest.mark.asyncio
    async def test_run_claude_returns_early_when_cooldown_active(self):
        """run_claude should return False with cooldown message when active."""
        from nightwire.claude_runner import ClaudeRunner

        with patch("nightwire.claude_runner.get_config") as mock_cfg:
            cfg = MagicMock()
            cfg.config_dir = Path("/tmp")
            cfg.claude_timeout = 60
            cfg.claude_max_turns = 5
            cfg.claude_path = "/usr/bin/claude"
            mock_cfg.return_value = cfg

            runner = ClaudeRunner()
            runner.current_project = MagicMock()
            runner.current_project.exists.return_value = True

        # Mock cooldown as active
        mock_cooldown = MagicMock()
        mock_cooldown.is_active = True
        mock_cooldown.get_state.return_value = MagicMock(
            user_message="Cooldown active"
        )

        with patch(
            "nightwire.rate_limit_cooldown.get_cooldown_manager",
            return_value=mock_cooldown,
        ):
            success, output = await runner.run_claude("test prompt")

        assert success is False
        assert "Cooldown active" in output

    @pytest.mark.asyncio
    async def test_run_claude_proceeds_when_cooldown_inactive(self):
        """run_claude should proceed normally when cooldown is not active."""
        from nightwire.claude_runner import ClaudeRunner

        with patch("nightwire.claude_runner.get_config") as mock_cfg:
            cfg = MagicMock()
            cfg.config_dir = Path("/tmp")
            cfg.claude_timeout = 60
            cfg.claude_max_turns = 5
            cfg.claude_path = "/usr/bin/claude"
            mock_cfg.return_value = cfg

            runner = ClaudeRunner()

        # No project set — should get the normal error, not cooldown
        mock_cooldown = MagicMock()
        mock_cooldown.is_active = False

        with patch(
            "nightwire.rate_limit_cooldown.get_cooldown_manager",
            return_value=mock_cooldown,
        ):
            success, output = await runner.run_claude("test prompt")

        assert success is False
        assert "No project selected" in output


# -------------------------------------------------------------------
# get_cooldown_manager singleton
# -------------------------------------------------------------------

class TestGetCooldownManager:
    """Test the singleton factory."""

    def test_returns_same_instance(self):
        import nightwire.rate_limit_cooldown as mod
        # Reset singleton
        mod._manager = None
        with patch("nightwire.rate_limit_cooldown.get_config") as mock_cfg:
            cfg = MagicMock()
            cfg.settings = {}
            mock_cfg.return_value = cfg
            m1 = mod.get_cooldown_manager()
            m2 = mod.get_cooldown_manager()
        assert m1 is m2
        mod._manager = None  # cleanup
