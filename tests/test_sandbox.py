"""Tests for Docker sandbox module."""

import subprocess

import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch

from nightwire.sandbox import build_sandbox_command, SandboxConfig


def test_build_sandbox_command_wraps_with_docker():
    """Should wrap command in docker run with proper mounts."""
    config = SandboxConfig(
        enabled=True,
        image="python:3.11-slim",
        network=False,
    )
    cmd = ["claude", "--print", "--verbose"]
    project_path = Path("/home/user/projects/myapp")

    result = build_sandbox_command(cmd, project_path, config)

    assert result[0] == "docker"
    assert "run" in result
    assert "--rm" in result
    assert "--network=none" in result  # no network
    assert any("/home/user/projects/myapp" in arg for arg in result)


def test_build_sandbox_command_disabled():
    """When disabled, should return original command unchanged."""
    config = SandboxConfig(enabled=False)
    cmd = ["claude", "--print"]
    project_path = Path("/home/user/projects/myapp")

    result = build_sandbox_command(cmd, project_path, config)
    assert result == cmd


def test_build_sandbox_command_with_network():
    """When network=True, should not add --network=none."""
    config = SandboxConfig(enabled=True, network=True)
    cmd = ["claude", "--print"]
    project_path = Path("/home/user/projects/myapp")

    result = build_sandbox_command(cmd, project_path, config)
    assert "--network=none" not in result


def test_default_image_is_nightwire_sandbox():
    """Default image should be nightwire-sandbox:latest."""
    config = SandboxConfig(enabled=True)
    assert config.image == "nightwire-sandbox:latest"


def test_env_vars_exclude_path():
    """Sandbox should not pass through PATH (container has its own)."""
    config = SandboxConfig(enabled=True)
    cmd = ["claude", "--print"]
    project_path = Path("/home/user/projects/myapp")

    result = build_sandbox_command(cmd, project_path, config)

    # Find all -e flags and their values
    env_vars = []
    for i, arg in enumerate(result):
        if arg == "-e" and i + 1 < len(result):
            env_vars.append(result[i + 1])

    assert "PATH" not in env_vars
    assert "ANTHROPIC_API_KEY" in env_vars


def test_sandbox_opencode_env_passthrough():
    """OpenCode mode should pass required env vars and auth mount."""
    config = SandboxConfig(enabled=True, runner_type="opencode")
    cmd = ["opencode", "run", "--format", "json"]
    project_path = Path("/home/user/projects/myapp")

    result = build_sandbox_command(cmd, project_path, config)

    env_vars = []
    for i, arg in enumerate(result):
        if arg == "-e" and i + 1 < len(result):
            env_vars.append(result[i + 1])

    assert "HOME" in env_vars
    assert "XDG_CONFIG_HOME" in env_vars
    assert "XDG_DATA_HOME" in env_vars
    assert "XDG_STATE_HOME" in env_vars
    assert "ANTHROPIC_API_KEY" not in env_vars
    assert any(
        arg == f"{Path.home() / '.local/share/opencode'}:/home/sandbox/.local/share/opencode:ro"
        for arg in result
    )


def test_sandbox_claude_env_unchanged():
    """Claude mode should continue passing ANTHROPIC_API_KEY."""
    config = SandboxConfig(enabled=True)
    cmd = ["claude", "--print"]
    project_path = Path("/home/user/projects/myapp")

    result = build_sandbox_command(cmd, project_path, config)

    env_vars = []
    for i, arg in enumerate(result):
        if arg == "-e" and i + 1 < len(result):
            env_vars.append(result[i + 1])

    assert "ANTHROPIC_API_KEY" in env_vars
    assert "HOME" not in env_vars


def test_sandbox_opencode_command_wrapping():
    """OpenCode inner command should be preserved in docker wrapping."""
    config = SandboxConfig(enabled=True, runner_type="opencode")
    cmd = ["opencode", "run", "--format", "json", "--dir", "/tmp/project"]
    project_path = Path("/home/user/projects/myapp")

    result = build_sandbox_command(cmd, project_path, config)
    image_idx = result.index(config.image)
    assert result[image_idx + 1:] == cmd


def test_tmpfs_size_configurable():
    """tmpfs_size should be reflected in the docker command."""
    config = SandboxConfig(enabled=True, tmpfs_size="512m")
    cmd = ["claude", "--print"]
    project_path = Path("/home/user/projects/myapp")

    result = build_sandbox_command(cmd, project_path, config)
    assert any("512m" in arg for arg in result)


def test_memory_limit_in_command():
    """Memory limit should appear in docker command."""
    config = SandboxConfig(enabled=True, memory_limit="4g")
    cmd = ["claude", "--print"]
    project_path = Path("/home/user/projects/myapp")

    result = build_sandbox_command(cmd, project_path, config)
    assert "--memory=4g" in result


def test_cpu_limit_in_command():
    """CPU limit should appear in docker command."""
    config = SandboxConfig(enabled=True, cpu_limit=4.0)
    cmd = ["claude", "--print"]
    project_path = Path("/home/user/projects/myapp")

    result = build_sandbox_command(cmd, project_path, config)
    assert "--cpus=4.0" in result


def test_validate_docker_available_success():
    """Should return True when Docker daemon is accessible."""
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        from nightwire.sandbox import validate_docker_available
        available, msg = validate_docker_available()
        assert available is True
        assert msg == ""


def test_validate_docker_available_not_installed():
    """Should return False with message when Docker is not installed."""
    with patch("subprocess.run", side_effect=FileNotFoundError):
        from nightwire.sandbox import validate_docker_available
        available, msg = validate_docker_available()
        assert available is False
        assert "not installed" in msg.lower()


def test_validate_docker_available_not_running():
    """Should return False with message when Docker daemon is not running."""
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=1)
        from nightwire.sandbox import validate_docker_available
        available, msg = validate_docker_available()
        assert available is False
        assert "not running" in msg.lower()


def test_validate_docker_available_timeout():
    """Should return False with message when Docker daemon times out."""
    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("docker", 10)):
        from nightwire.sandbox import validate_docker_available
        available, msg = validate_docker_available()
        assert available is False
        assert "did not respond" in msg.lower()


def test_sandbox_hardening_flags():
    """Sandbox should include container hardening flags with correct values."""
    config = SandboxConfig(enabled=True)
    cmd = ["claude", "--print"]
    project_path = Path("/home/user/projects/myapp")

    result = build_sandbox_command(cmd, project_path, config)

    # Verify flag/value pairs are adjacent
    cap_idx = result.index("--cap-drop")
    assert result[cap_idx + 1] == "ALL"

    pids_idx = result.index("--pids-limit")
    assert result[pids_idx + 1] == "256"

    user_idx = result.index("--user")
    assert result[user_idx + 1] == "1000:1000"

    sec_idx = result.index("--security-opt")
    assert result[sec_idx + 1] == "no-new-privileges"


def test_validate_docker_available_permission_denied():
    """Should return False with message when Docker socket permissions denied."""
    with patch("subprocess.run", side_effect=PermissionError):
        from nightwire.sandbox import validate_docker_available
        available, msg = validate_docker_available()
        assert available is False
        assert "permission denied" in msg.lower()
