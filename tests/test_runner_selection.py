"""Tests for runner command selection and OpenCode output parsing."""

import json
from pathlib import Path
from types import SimpleNamespace

from nightwire.claude_runner import ClaudeRunner


def _make_runner(monkeypatch, runner_type="claude", runner_path="opencode"):
    cfg = SimpleNamespace(
        config_dir=Path("/tmp"),
        runner_type=runner_type,
        runner_path=runner_path,
        claude_path="claude",
        claude_max_turns=8,
        claude_timeout=60,
    )
    monkeypatch.setattr("nightwire.claude_runner.get_config", lambda: cfg)
    return ClaudeRunner()


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


def test_opencode_json_events_extract_text(monkeypatch):
    runner = _make_runner(monkeypatch, runner_type="opencode")

    output = "\n".join(
        [
            json.dumps({"type": "text", "text": "first line"}),
            json.dumps(
                {
                    "type": "content",
                    "content": [
                        {"type": "text", "text": "second line"},
                        {"type": "image", "url": "https://example.invalid/img"},
                    ],
                }
            ),
            json.dumps(
                {
                    "type": "assistant_message",
                    "message": {
                        "content": [
                            {"type": "text", "text": "third line"},
                            {"type": "tool_use", "name": "noop"},
                        ]
                    },
                }
            ),
            "not-json",
        ]
    )

    extracted = runner._extract_opencode_text(output)

    assert extracted == "first line\nsecond line\nthird line"
