"""Tests for auto-update feature."""

import asyncio
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestAutoUpdateConfig:
    """Tests for auto_update configuration properties."""

    def test_auto_update_disabled_by_default(self):
        from nightwire.config import Config
        with patch.object(Config, '__init__', lambda self, **kw: None):
            config = Config.__new__(Config)
            config.settings = {}
            assert config.auto_update_enabled is False

    def test_auto_update_enabled_from_settings(self):
        from nightwire.config import Config
        with patch.object(Config, '__init__', lambda self, **kw: None):
            config = Config.__new__(Config)
            config.settings = {"auto_update": {"enabled": True}}
            assert config.auto_update_enabled is True

    def test_auto_update_check_interval_default(self):
        from nightwire.config import Config
        with patch.object(Config, '__init__', lambda self, **kw: None):
            config = Config.__new__(Config)
            config.settings = {}
            assert config.auto_update_check_interval == 21600

    def test_auto_update_check_interval_from_settings(self):
        from nightwire.config import Config
        with patch.object(Config, '__init__', lambda self, **kw: None):
            config = Config.__new__(Config)
            config.settings = {"auto_update": {"check_interval": 3600}}
            assert config.auto_update_check_interval == 3600

    def test_auto_update_branch_default(self):
        from nightwire.config import Config
        with patch.object(Config, '__init__', lambda self, **kw: None):
            config = Config.__new__(Config)
            config.settings = {}
            assert config.auto_update_branch == "main"

    def test_auto_update_branch_from_settings(self):
        from nightwire.config import Config
        with patch.object(Config, '__init__', lambda self, **kw: None):
            config = Config.__new__(Config)
            config.settings = {"auto_update": {"branch": "develop"}}
            assert config.auto_update_branch == "develop"


class TestAutoUpdater:
    """Tests for AutoUpdater class."""

    def _make_updater(self, send_message=None, branch="main"):
        """Create an AutoUpdater with mocked dependencies."""
        from nightwire.updater import AutoUpdater
        config = MagicMock()
        config.auto_update_enabled = True
        config.auto_update_check_interval = 21600
        config.auto_update_branch = branch
        config.allowed_numbers = ["+15551234567"]
        if send_message is None:
            send_message = AsyncMock()
        return AutoUpdater(
            config=config,
            send_message=send_message,
            repo_dir=Path("/fake/repo"),
        )

    # --- check_for_updates tests ---

    @pytest.mark.asyncio
    async def test_check_for_updates_no_update(self):
        """check_for_updates returns False when local matches remote."""
        updater = self._make_updater()
        async def fake_run_git(*args, **kwargs):
            return "abc1234"
        updater._run_git = fake_run_git
        result = await updater.check_for_updates()
        assert result is False
        assert updater.pending_update is False

    @pytest.mark.asyncio
    async def test_check_for_updates_has_update(self):
        """check_for_updates returns True and sets pending state when remote is ahead."""
        send = AsyncMock()
        updater = self._make_updater(send_message=send)
        call_count = 0
        async def fake_run_git(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return ""  # git fetch
            elif call_count == 2:
                return "abc1234"  # local HEAD
            elif call_count == 3:
                return "def5678"  # remote HEAD
            elif call_count == 4:
                return "3"  # commit count
            elif call_count == 5:
                return "feat: add cool thing"  # latest commit message
            return ""
        updater._run_git = fake_run_git
        result = await updater.check_for_updates()
        assert result is True
        assert updater.pending_update is True
        assert updater.pending_sha == "def5678"
        send.assert_called_once()

    @pytest.mark.asyncio
    async def test_check_for_updates_no_renotify_same_sha(self):
        """check_for_updates should not re-notify if pending_sha unchanged."""
        send = AsyncMock()
        updater = self._make_updater(send_message=send)
        updater.pending_update = True
        updater.pending_sha = "def5678"
        call_count = 0
        async def fake_run_git(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return ""  # git fetch
            elif call_count == 2:
                return "abc1234"  # local HEAD
            elif call_count == 3:
                return "def5678"  # remote HEAD (same as pending)
            return ""
        updater._run_git = fake_run_git
        result = await updater.check_for_updates()
        assert result is True
        send.assert_not_called()

    @pytest.mark.asyncio
    async def test_check_for_updates_git_fetch_fails(self):
        """check_for_updates returns False on git fetch failure (e.g. network down)."""
        updater = self._make_updater()
        async def fake_run_git(*args, **kwargs):
            raise subprocess.CalledProcessError(1, ["git", "fetch"], "", "network error")
        updater._run_git = fake_run_git
        result = await updater.check_for_updates()
        assert result is False

    # --- apply_update tests ---

    @pytest.mark.asyncio
    async def test_apply_update_no_pending(self):
        """apply_update returns message when no update pending."""
        updater = self._make_updater()
        result = await updater.apply_update()
        assert result == "No updates available."

    @pytest.mark.asyncio
    async def test_apply_update_success(self):
        """apply_update pulls, installs, and schedules restart."""
        send = AsyncMock()
        updater = self._make_updater(send_message=send)
        updater.pending_update = True
        updater.pending_sha = "def5678"

        async def fake_run_git(*args, **kwargs):
            if "rev-parse" in args:
                return "abc1234"
            return ""
        updater._run_git = fake_run_git

        with patch("nightwire.updater.subprocess.run") as mock_run, \
             patch("nightwire.updater.asyncio.create_task") as mock_create_task:
            mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")
            result = await updater.apply_update()

        assert "Update applied" in result
        assert updater.pending_update is False
        mock_create_task.assert_called()  # _delayed_exit task was created

    @pytest.mark.asyncio
    async def test_apply_update_git_pull_fails_triggers_rollback(self):
        """apply_update rolls back and resets state on git pull failure."""
        send = AsyncMock()
        updater = self._make_updater(send_message=send)
        updater.pending_update = True
        updater.pending_sha = "def5678"

        call_count = 0
        async def fake_run_git(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return "abc1234"  # rev-parse HEAD
            # git pull fails
            raise subprocess.CalledProcessError(1, ["git", "pull"], "", "merge conflict")
        updater._run_git = fake_run_git
        updater._rollback = AsyncMock()

        result = await updater.apply_update()
        assert "failed" in result.lower()
        updater._rollback.assert_called_once_with("abc1234")
        assert updater.pending_update is False  # State reset for re-check
        assert updater.pending_sha is None
        send.assert_called()

    @pytest.mark.asyncio
    async def test_apply_update_pip_fails_triggers_rollback(self):
        """apply_update rolls back on pip install failure."""
        send = AsyncMock()
        updater = self._make_updater(send_message=send)
        updater.pending_update = True
        updater.pending_sha = "def5678"

        async def fake_run_git(*args, **kwargs):
            return "abc1234"
        updater._run_git = fake_run_git
        updater._rollback = AsyncMock()

        with patch("nightwire.updater.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stderr="error", stdout="")
            result = await updater.apply_update()

        assert "rolled back" in result.lower()
        updater._rollback.assert_called_once_with("abc1234")
        assert updater.pending_update is False
        assert updater.pending_sha is None

    @pytest.mark.asyncio
    async def test_apply_update_timeout_triggers_rollback(self):
        """apply_update handles subprocess timeout and rolls back."""
        send = AsyncMock()
        updater = self._make_updater(send_message=send)
        updater.pending_update = True
        updater.pending_sha = "def5678"

        call_count = 0
        async def fake_run_git(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return "abc1234"  # rev-parse HEAD
            if call_count == 2:
                return ""  # git pull succeeds
            return ""
        updater._run_git = fake_run_git
        updater._rollback = AsyncMock()

        with patch("nightwire.updater.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(["pip"], 120)
            result = await updater.apply_update()

        assert "failed" in result.lower()
        updater._rollback.assert_called_once_with("abc1234")
        assert updater.pending_update is False

    # --- lifecycle tests ---

    @pytest.mark.asyncio
    async def test_start_stop_lifecycle(self):
        """start() creates task, stop() cancels it."""
        updater = self._make_updater()
        await updater.start()
        assert updater._check_task is not None
        assert not updater._check_task.done()
        await updater.stop()
        assert updater._check_task.done()

    @pytest.mark.asyncio
    async def test_start_without_admin_phone(self):
        """start() warns and does not create task if no admin phone."""
        updater = self._make_updater()
        updater.admin_phone = None
        await updater.start()
        assert updater._check_task is None

    # --- branch validation tests ---

    def test_rejects_branch_starting_with_dash(self):
        """Branch names starting with - are rejected (git flag injection)."""
        from nightwire.updater import AutoUpdater
        config = MagicMock()
        config.auto_update_branch = "--upload-pack=evil"
        config.allowed_numbers = ["+15551234567"]
        config.auto_update_check_interval = 21600
        with pytest.raises(ValueError, match="Invalid branch name"):
            AutoUpdater(config=config, send_message=AsyncMock(),
                        repo_dir=Path("/fake/repo"))

    def test_accepts_valid_branch_names(self):
        """Valid branch names like feature/foo and release-1.0 are accepted."""
        from nightwire.updater import AutoUpdater
        for branch in ["main", "develop", "feature/auto-update", "release-1.0",
                        "v2.0.0", "my_branch"]:
            config = MagicMock()
            config.auto_update_branch = branch
            config.allowed_numbers = ["+15551234567"]
            config.auto_update_check_interval = 21600
            updater = AutoUpdater(config=config, send_message=AsyncMock(),
                                  repo_dir=Path("/fake/repo"))
            assert updater.branch == branch

    # --- rollback tests ---

    @pytest.mark.asyncio
    async def test_rollback_failure_does_not_crash(self):
        """_rollback logs error but does not raise on git reset failure."""
        updater = self._make_updater()
        async def failing_git(*args, **kwargs):
            raise subprocess.CalledProcessError(1, ["git", "reset"], "", "error")
        updater._run_git = failing_git
        # Should not raise
        await updater._rollback("abc1234")

    # --- check loop error handling ---

    @pytest.mark.asyncio
    async def test_check_loop_continues_after_error(self):
        """_check_loop should continue running after check_for_updates raises."""
        updater = self._make_updater()
        updater.check_interval = 0.01  # Fast for testing
        call_count = 0

        async def counting_check():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("network error")
            # Second call succeeds, then we cancel
            if call_count >= 2:
                updater._check_task.cancel()
            return False
        updater.check_for_updates = counting_check

        await updater.start()
        try:
            await updater._check_task
        except asyncio.CancelledError:
            pass
        assert call_count >= 2  # Loop survived the first error
