"""Comprehensive integration tests for OpenCode runner behavior."""

import asyncio
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

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


def test_sandbox_uses_config_runner_type_when_param_default_is_claude():
    config = SandboxConfig(enabled=True, runner_type="opencode")
    cmd = ["opencode", "run", "--format", "json"]
    result = build_sandbox_command(cmd, Path("/tmp/project"), config)

    env_vars = []
    for i, arg in enumerate(result):
        if arg == "-e" and i + 1 < len(result):
            env_vars.append(result[i + 1])

    assert "HOME" in env_vars
    assert "XDG_CONFIG_HOME" in env_vars
    assert "ANTHROPIC_API_KEY" not in env_vars


def test_sandbox_opencode_auth_mount_uses_dynamic_home(monkeypatch):
    monkeypatch.setattr("nightwire.sandbox.Path.home", lambda: Path("/custom/home"))
    config = SandboxConfig(enabled=True, runner_type="opencode")
    cmd = ["opencode", "run", "--format", "json"]

    result = build_sandbox_command(cmd, Path("/tmp/project"), config)

    assert "/custom/home/.local/share/opencode:/home/sandbox/.local/share/opencode:ro" in result


def test_sandbox_opencode_does_not_pass_opencode_config_dir_env():
    config = SandboxConfig(enabled=True, runner_type="opencode")
    cmd = ["opencode", "run", "--format", "json"]
    result = build_sandbox_command(cmd, Path("/tmp/project"), config)

    env_vars = []
    for i, arg in enumerate(result):
        if arg == "-e" and i + 1 < len(result):
            env_vars.append(result[i + 1])

    assert "OPENCODE_CONFIG_DIR" not in env_vars


def test_install_script_has_valid_bash_syntax():
    install_script = Path(__file__).resolve().parents[1] / "install.sh"
    result = subprocess.run(["bash", "-n", str(install_script)], capture_output=True, text=True)

    assert result.returncode == 0, result.stderr
