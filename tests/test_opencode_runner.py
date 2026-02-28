"""Tests for OpenCode runner support: config, command construction, JSON parsing, sandbox, and signal-path simulation."""

import asyncio
import json
import subprocess
from pathlib import Path
from types import MethodType, SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from nightwire.bot import SignalBot
from nightwire.claude_runner import ClaudeRunner, ErrorCategory
from nightwire.config import Config
from nightwire.sandbox import SandboxConfig, build_sandbox_command


def _make_config(monkeypatch, settings):
    def _fake_init(self, config_dir=None):
        self.settings = settings
        self.projects = {"projects": []}

    monkeypatch.setattr(Config, "__init__", _fake_init)
    return Config()


def _make_runner(monkeypatch, runner_type="claude", runner_path="claude"):
    cfg = SimpleNamespace(
        config_dir=Path("/tmp"),
        runner_type=runner_type,
        runner_path=runner_path,
        claude_path="claude",
        claude_max_turns=8,
        claude_timeout=60,
        sandbox_config={},
    )
    monkeypatch.setattr("nightwire.claude_runner.get_config", lambda: cfg)
    return ClaudeRunner()


# Section: Config properties
def test_runner_type_defaults_to_claude(monkeypatch):
    config = _make_config(monkeypatch, {})
    assert config.runner_type == "claude"


def test_runner_path_defaults_to_claude_path(monkeypatch):
    config = _make_config(monkeypatch, {"claude_path": "/custom/claude"})
    assert config.runner_path == "/custom/claude"


def test_runner_type_opencode(monkeypatch):
    config = _make_config(monkeypatch, {"runner": {"type": "opencode"}})
    assert config.runner_type == "opencode"


def test_legacy_claude_path_still_works_without_runner(monkeypatch):
    config = _make_config(monkeypatch, {"claude_path": "/legacy/claude"})
    assert config.claude_path == "/legacy/claude"


def test_runner_type_unknown_value_is_returned_as_is(monkeypatch):
    config = _make_config(monkeypatch, {"runner": {"type": "my-custom-runner"}})
    assert config.runner_type == "my-custom-runner"


def test_runner_path_opencode_without_binary_falls_back_to_claude_path(monkeypatch, tmp_path):
    config = _make_config(
        monkeypatch,
        {"runner": {"type": "opencode"}, "claude_path": "/fallback/claude"},
    )
    monkeypatch.setattr("nightwire.config.shutil.which", lambda _name: None)
    monkeypatch.setattr("nightwire.config.Path.home", lambda: tmp_path)

    assert config.runner_path == "/fallback/claude"


def test_runner_path_override_applies_even_when_runner_type_is_claude(monkeypatch):
    config = _make_config(
        monkeypatch,
        {"runner": {"type": "claude", "path": "/custom/runner"}, "claude_path": "/legacy/claude"},
    )

    assert config.runner_path == "/custom/runner"


def test_runner_path_wins_over_claude_path_when_both_set(monkeypatch):
    config = _make_config(
        monkeypatch,
        {"runner": {"path": "/custom/runner"}, "claude_path": "/legacy/claude"},
    )

    assert config.runner_path == "/custom/runner"


# Section: Command construction
def test_default_runner_keeps_claude_command(monkeypatch):
    runner = _make_runner(monkeypatch, runner_type="claude")

    cmd = runner._build_runner_command(Path("/tmp/project"))

    assert cmd == [
        "claude",
        "--print",
        "--dangerously-skip-permissions",
        "--verbose",
        "--max-turns",
        "8",
    ]


def test_opencode_runner_uses_json_command(monkeypatch):
    runner = _make_runner(
        monkeypatch,
        runner_type="opencode",
        runner_path="/usr/local/bin/opencode",
    )

    cmd = runner._build_runner_command(Path("/tmp/project"))

    assert cmd == [
        "/usr/local/bin/opencode",
        "run",
        "--format",
        "json",
        "--dir",
        "/tmp/project",
    ]


# Section: Subprocess environment
@pytest.mark.parametrize("runner_type", ["claude", "opencode"])
def test_subprocess_env_always_has_common_keys(monkeypatch, runner_type):
    monkeypatch.setenv("HOME", "/tmp/home")
    monkeypatch.setenv("PATH", "/usr/local/bin:/usr/bin")
    monkeypatch.setenv("USER", "nightwire")
    monkeypatch.setenv("LANG", "C.UTF-8")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-key")
    monkeypatch.setenv("XDG_CONFIG_HOME", "/tmp/xdg-config")
    monkeypatch.setenv("XDG_DATA_HOME", "/tmp/xdg-data")
    monkeypatch.setenv("XDG_STATE_HOME", "/tmp/xdg-state")
    monkeypatch.setenv("OPENCODE_CONFIG_DIR", "/tmp/opencode-config")

    runner = _make_runner(monkeypatch, runner_type=runner_type, runner_path="opencode")
    env = runner._build_subprocess_env()

    assert env["HOME"] == "/tmp/home"
    assert env["PATH"] == "/usr/local/bin:/usr/bin"
    assert env["USER"] == "nightwire"
    assert env["LANG"] == "C.UTF-8"


def test_subprocess_env_claude_has_anthropic_only(monkeypatch):
    monkeypatch.setenv("HOME", "/tmp/home")
    monkeypatch.setenv("PATH", "/usr/local/bin:/usr/bin")
    monkeypatch.setenv("USER", "nightwire")
    monkeypatch.setenv("LANG", "C.UTF-8")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-key")
    monkeypatch.setenv("XDG_CONFIG_HOME", "/tmp/xdg-config")
    monkeypatch.setenv("XDG_DATA_HOME", "/tmp/xdg-data")
    monkeypatch.setenv("XDG_STATE_HOME", "/tmp/xdg-state")
    monkeypatch.setenv("OPENCODE_CONFIG_DIR", "/tmp/opencode-config")

    runner = _make_runner(monkeypatch, runner_type="claude")
    env = runner._build_subprocess_env()

    assert env["ANTHROPIC_API_KEY"] == "anthropic-key"
    assert "XDG_CONFIG_HOME" not in env
    assert "XDG_DATA_HOME" not in env
    assert "XDG_STATE_HOME" not in env
    assert "OPENCODE_CONFIG_DIR" not in env


def test_subprocess_env_opencode_has_xdg_only(monkeypatch):
    monkeypatch.setenv("HOME", "/tmp/home")
    monkeypatch.setenv("PATH", "/usr/local/bin:/usr/bin")
    monkeypatch.setenv("USER", "nightwire")
    monkeypatch.setenv("LANG", "C.UTF-8")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-key")
    monkeypatch.setenv("XDG_CONFIG_HOME", "/tmp/xdg-config")
    monkeypatch.setenv("XDG_DATA_HOME", "/tmp/xdg-data")
    monkeypatch.setenv("XDG_STATE_HOME", "/tmp/xdg-state")
    monkeypatch.setenv("OPENCODE_CONFIG_DIR", "/tmp/opencode-config")

    runner = _make_runner(monkeypatch, runner_type="opencode", runner_path="opencode")
    env = runner._build_subprocess_env()

    assert env["XDG_CONFIG_HOME"] == "/tmp/xdg-config"
    assert env["XDG_DATA_HOME"] == "/tmp/xdg-data"
    assert env["XDG_STATE_HOME"] == "/tmp/xdg-state"
    assert env["OPENCODE_CONFIG_DIR"] == "/tmp/opencode-config"
    assert "ANTHROPIC_API_KEY" not in env


# Section: OpenCode JSON parsing
def test_extract_opencode_text_empty_input(monkeypatch):
    runner = _make_runner(monkeypatch, runner_type="opencode", runner_path="opencode")
    assert runner._extract_opencode_text("") == ""


def test_extract_opencode_text_all_non_json_lines(monkeypatch):
    runner = _make_runner(monkeypatch, runner_type="opencode", runner_path="opencode")
    output = "not json\n{oops\nstill not json"
    assert runner._extract_opencode_text(output) == ""


def test_extract_opencode_text_mixed_lines_and_nested_string_content(monkeypatch):
    runner = _make_runner(monkeypatch, runner_type="opencode", runner_path="opencode")
    output = "\n".join(
        [
            "garbage",
            "",
            json.dumps({"type": "text", "text": "line 1"}),
            json.dumps({"type": "content", "content": ["line 2", {"type": "text", "text": "line 3"}]}),
            "   ",
            json.dumps({"type": "assistant_message", "message": {"content": ["line 4"]}}),
        ]
    )

    assert runner._extract_opencode_text(output) == "line 1\nline 2\nline 3\nline 4"


def test_extract_opencode_text_assistant_message_non_dict_is_skipped(monkeypatch):
    runner = _make_runner(monkeypatch, runner_type="opencode", runner_path="opencode")
    output = "\n".join(
        [
            json.dumps({"type": "assistant_message", "message": "invalid"}),
            json.dumps({"type": "text", "text": "kept"}),
        ]
    )

    assert runner._extract_opencode_text(output) == "kept"


def test_extract_opencode_text_content_non_list_is_skipped(monkeypatch):
    runner = _make_runner(monkeypatch, runner_type="opencode", runner_path="opencode")
    output = "\n".join(
        [
            json.dumps({"type": "content", "content": "invalid"}),
            json.dumps({"type": "text", "text": "kept"}),
        ]
    )

    assert runner._extract_opencode_text(output) == "kept"


def test_extract_opencode_text_only_tool_use_events_returns_empty(monkeypatch):
    runner = _make_runner(monkeypatch, runner_type="opencode", runner_path="opencode")
    output = "\n".join(
        [
            json.dumps({"type": "content", "content": [{"type": "tool_use", "name": "a"}]}),
            json.dumps({"type": "assistant_message", "message": {"content": [{"type": "tool_use", "name": "b"}]}}),
        ]
    )

    assert runner._extract_opencode_text(output) == ""


# Section: Execution path
@pytest.mark.asyncio
async def test_execute_once_missing_binary_message_mentions_opencode(monkeypatch, tmp_path):
    runner = _make_runner(monkeypatch, runner_type="opencode", runner_path="opencode")

    async def _raise_not_found(*args, **kwargs):
        raise FileNotFoundError

    monkeypatch.setattr("nightwire.claude_runner.asyncio.create_subprocess_exec", _raise_not_found)

    success, message, category = await runner._execute_claude_once(
        cmd=["opencode", "run"],
        prompt="test",
        timeout=5,
        project_path=tmp_path,
    )

    assert not success
    assert category == ErrorCategory.INFRASTRUCTURE
    assert "OpenCode CLI not found" in message
    assert "Claude CLI not found" not in message


@pytest.mark.asyncio
async def test_execute_once_missing_binary_message_mentions_claude(monkeypatch, tmp_path):
    runner = _make_runner(monkeypatch, runner_type="claude", runner_path="claude")

    async def _raise_not_found(*args, **kwargs):
        raise FileNotFoundError

    monkeypatch.setattr("nightwire.claude_runner.asyncio.create_subprocess_exec", _raise_not_found)

    success, message, category = await runner._execute_claude_once(
        cmd=["claude", "--print"],
        prompt="test",
        timeout=5,
        project_path=tmp_path,
    )

    assert not success
    assert category == ErrorCategory.INFRASTRUCTURE
    assert "Claude CLI not found" in message


@pytest.mark.asyncio
async def test_execute_once_opencode_success_uses_extracted_output(monkeypatch, tmp_path):
    runner = _make_runner(monkeypatch, runner_type="opencode", runner_path="opencode")

    class _FakeProcess:
        returncode = 0

        async def communicate(self, input=None):
            return b'{"type":"text","text":"raw"}\n', b""

        def kill(self):
            self.returncode = -9

        async def wait(self):
            return self.returncode

    async def _fake_create_subprocess_exec(*args, **kwargs):
        return _FakeProcess()

    extract_mock = MagicMock(return_value="parsed output")
    monkeypatch.setattr(runner, "_extract_opencode_text", extract_mock)
    monkeypatch.setattr("nightwire.claude_runner.asyncio.create_subprocess_exec", _fake_create_subprocess_exec)

    success, message, category = await runner._execute_claude_once(
        cmd=["opencode", "run"],
        prompt="test",
        timeout=5,
        project_path=tmp_path,
    )

    assert success
    assert message == "parsed output"
    assert category == ErrorCategory.PERMANENT
    extract_mock.assert_called_once()


@pytest.mark.asyncio
async def test_execute_once_claude_success_returns_raw_output(monkeypatch, tmp_path):
    runner = _make_runner(monkeypatch, runner_type="claude", runner_path="claude")

    class _FakeProcess:
        returncode = 0

        async def communicate(self, input=None):
            return b'{"type":"text","text":"raw"}\n', b""

        def kill(self):
            self.returncode = -9

        async def wait(self):
            return self.returncode

    async def _fake_create_subprocess_exec(*args, **kwargs):
        return _FakeProcess()

    extract_mock = MagicMock(return_value="parsed output")
    monkeypatch.setattr(runner, "_extract_opencode_text", extract_mock)
    monkeypatch.setattr("nightwire.claude_runner.asyncio.create_subprocess_exec", _fake_create_subprocess_exec)

    success, message, category = await runner._execute_claude_once(
        cmd=["claude", "--print"],
        prompt="test",
        timeout=5,
        project_path=tmp_path,
    )

    assert success
    assert message == '{"type":"text","text":"raw"}\n'
    assert category == ErrorCategory.PERMANENT
    extract_mock.assert_not_called()


# Section: Signal-path simulation (end-to-end)
@pytest.mark.asyncio
@pytest.mark.parametrize("sandbox_enabled", [False, True])
async def test_signal_message_exec_path_uses_direct_or_sandbox_runner(monkeypatch, tmp_path, sandbox_enabled):
    cfg = SimpleNamespace(
        config_dir=tmp_path,
        runner_type="opencode",
        runner_path="/usr/local/bin/opencode",
        claude_path="claude",
        claude_max_turns=8,
        claude_timeout=60,
        sandbox_config={
            "enabled": sandbox_enabled,
            "image": "nightwire-sandbox:latest",
            "network": False,
            "memory_limit": "2g",
            "cpu_limit": 2.0,
            "tmpfs_size": "256m",
        },
        memory_max_context_tokens=1000,
    )
    monkeypatch.setattr("nightwire.claude_runner.get_config", lambda: cfg)

    captured_commands = []

    class _FakeProcess:
        returncode = 0

        async def communicate(self, input=None):
            return b'{"type":"text","text":"ok"}\n', b""

        def kill(self):
            self.returncode = -9

        async def wait(self):
            return self.returncode

    async def _fake_create_subprocess_exec(*cmd, **kwargs):
        captured_commands.append(list(cmd))
        return _FakeProcess()

    monkeypatch.setattr(
        "nightwire.claude_runner.asyncio.create_subprocess_exec",
        _fake_create_subprocess_exec,
    )
    monkeypatch.setattr("nightwire.sandbox.validate_docker_available", lambda: (True, ""))

    runner = ClaudeRunner()

    bot = cast(Any, SignalBot.__new__(SignalBot))
    bot.config = SimpleNamespace(memory_max_context_tokens=1000)
    bot.runner = runner
    bot.project_manager = SimpleNamespace(
        get_current_project=lambda _sender: "demo-project",
        get_current_path=lambda _sender: tmp_path,
    )
    bot.memory = SimpleNamespace(
        store_message=AsyncMock(return_value=None),
        get_relevant_context=AsyncMock(return_value=None),
    )
    bot.plugin_loader = SimpleNamespace(get_sorted_matchers=lambda: [])
    bot.cooldown_manager = None
    bot._sender_tasks = {}
    bot.nightwire_runner = None
    bot._send_message = AsyncMock(return_value=None)

    started_task = {}
    original_start_background_task = SignalBot._start_background_task

    def _capture_background_task(self, sender, task_description, project_name, image_paths=None):
        original_start_background_task(self, sender, task_description, project_name, image_paths=image_paths)
        started_task["task"] = self._sender_tasks[sender]["task"]

    bot._start_background_task = MethodType(_capture_background_task, bot)

    monkeypatch.setattr("nightwire.bot.is_authorized", lambda _sender: True)
    monkeypatch.setattr("nightwire.bot.check_rate_limit", lambda _sender: True)

    await SignalBot._process_message(bot, "+15555550123", "run this")
    await started_task["task"]

    assert captured_commands
    cmd = captured_commands[0]

    if not sandbox_enabled:
        assert cmd[0] == "/usr/local/bin/opencode"
        assert cmd[1:5] == ["run", "--format", "json", "--dir"]
        assert cmd[5] == str(tmp_path)
    else:
        assert cmd[:2] == ["docker", "run"]
        opencode_idx = cmd.index("/usr/local/bin/opencode")
        assert cmd[opencode_idx:opencode_idx + 5] == [
            "/usr/local/bin/opencode",
            "run",
            "--format",
            "json",
            "--dir",
        ]
        assert cmd[opencode_idx + 5] == str(tmp_path)


# Section: Install script validation
def test_install_script_has_valid_bash_syntax():
    install_script = Path(__file__).resolve().parents[1] / "install.sh"
    result = subprocess.run(["bash", "-n", str(install_script)], capture_output=True, text=True)

    assert result.returncode == 0, result.stderr
