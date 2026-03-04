"""Tests for Docker sandbox module."""

from pathlib import Path

from nightwire.sandbox import SandboxConfig, build_sandbox_command


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
    # Use str(project_path) for cross-platform (backslash on Windows)
    assert any(str(project_path) in arg for arg in result)


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
