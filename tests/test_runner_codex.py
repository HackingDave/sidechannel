"""Tests for Codex CLI runner support."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from nightwire.claude_runner import ClaudeRunner


def _make_runner(model: str | None, reasoning_effort: str | None = None) -> ClaudeRunner:
    runner = ClaudeRunner()
    runner.config = SimpleNamespace(
        runner_type="codex",
        runner_path="/home/hackingdave/.npm-global/bin/codex",
        runner_model=model,
        runner_reasoning_effort=reasoning_effort,
        sandbox_config={},
        claude_path="/usr/bin/claude",
        claude_max_turns=25,
        config_dir=Path("/tmp"),
    )
    return runner


def test_codex_command_includes_model_when_configured():
    runner = _make_runner("gpt-5.4", "xhigh")
    cmd = runner._build_runner_command(Path("/tmp/project"), "Fix the bug")
    assert cmd[0] == "/home/hackingdave/.npm-global/bin/codex"
    assert cmd[1:3] == ["exec", "--json"]
    assert "-m" in cmd
    idx = cmd.index("-m")
    assert cmd[idx + 1] == "gpt-5.4"
    assert "-c" in cmd
    cfg_idx = cmd.index("-c")
    assert cmd[cfg_idx + 1] == 'model_reasoning_effort="xhigh"'
    assert "-C" in cmd
    assert str(Path("/tmp/project")) in cmd
    assert cmd[-1] == "Fix the bug"


def test_codex_command_omits_model_flag_when_not_configured():
    runner = _make_runner(None)
    cmd = runner._build_runner_command(Path("/tmp/project"), "Fix the bug")
    assert "-m" not in cmd
    assert cmd[-3:] == ["-C", str(Path("/tmp/project")), "Fix the bug"]


@pytest.mark.asyncio
async def test_codex_execution_closes_stdin_and_does_not_send_prompt_bytes():
    runner = _make_runner("gpt-5.4")

    class DummyProcess:
        def __init__(self):
            self.returncode = 0
            self.pid = 1234

        async def communicate(self, input=None):
            self.input = input
            return (
                b'{"type":"item.completed","item":{"type":"agent_message","text":"OK"}}\n',
                b"",
            )

    proc = DummyProcess()

    with patch("nightwire.claude_runner.asyncio.create_subprocess_exec", return_value=proc) as mock_exec:
        success, output, category = await runner._execute_claude_once(
            cmd=runner._build_runner_command(Path("/tmp/project"), "Fix the bug"),
            prompt="Fix the bug",
            timeout=5,
            project_path=Path("/tmp/project"),
        )

    assert success is True
    assert output == "OK"
    assert category.value == "permanent"
    assert proc.input is None
    assert mock_exec.call_args.kwargs["stdin"] == asyncio.subprocess.DEVNULL
