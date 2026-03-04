"""Docker sandbox for Claude CLI task execution.

Optional security isolation layer that runs Claude CLI inside a Docker
container with hardened settings. Disabled by default — enable via
``sandbox.enabled: true`` in settings.yaml.

Key functions:
    validate_docker_available: Check if Docker daemon is accessible.
    build_sandbox_command: Wrap a CLI command in Docker with hardening.

Key classes:
    SandboxConfig: Dataclass for sandbox container settings.
"""

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Tuple

import structlog

logger = structlog.get_logger("nightwire.claude")

logger.debug(
    "sandbox_module_loaded",
    msg="sandbox is optional — enable via sandbox.enabled in settings.yaml",
)


@dataclass
class SandboxConfig:
    """Configuration for Docker sandbox.

    Attributes:
        enabled: Whether sandboxing is active.
        image: Docker image to use (default nightwire-sandbox:latest).
        network: Allow network access (default False).
        memory_limit: Container memory cap (e.g. "2g").
        cpu_limit: CPU core limit (e.g. 2.0).
        tmpfs_size: Size of /tmp tmpfs mount (e.g. "256m").
    """

    enabled: bool = False
    image: str = "nightwire-sandbox:latest"
    network: bool = False
    memory_limit: str = "2g"
    cpu_limit: float = 2.0
    tmpfs_size: str = "256m"


def validate_docker_available() -> Tuple[bool, str]:
    """Check if Docker daemon is accessible.

    Runs ``docker info`` with a 10-second timeout. Returns a tuple
    indicating availability and an error message if unavailable.
    This is a blocking call — wrap in ``asyncio.to_thread()`` when
    called from async code.

    Returns:
        Tuple of (available, error_message). error_message is empty
        if available.
    """
    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            timeout=10,
        )
        if result.returncode != 0:
            return False, (
                "Docker daemon is not running. "
                "Start Docker or disable sandbox in config/settings.yaml."
            )
        return True, ""
    except FileNotFoundError:
        return False, (
            "Docker is not installed. "
            "Install Docker or disable sandbox in config/settings.yaml."
        )
    except PermissionError:
        return False, (
            "Permission denied accessing Docker. "
            "Add your user to the docker group or disable sandbox "
            "in config/settings.yaml."
        )
    except subprocess.TimeoutExpired:
        return False, (
            "Docker daemon did not respond. "
            "Check Docker status or disable sandbox in config/settings.yaml."
        )


def build_sandbox_command(
    cmd: List[str],
    project_path: Path,
    config: SandboxConfig,
) -> List[str]:
    """Wrap a command in a Docker sandbox if enabled.

    Mounts only project_path read-write, /tmp as tmpfs, no network
    by default. Applies container hardening: non-root user, no-new-
    privileges, all capabilities dropped, PID limit.

    Args:
        cmd: Original command as a list of strings.
        project_path: Project directory to mount into container.
        config: Sandbox configuration.

    Returns:
        Docker-wrapped command list, or original cmd if disabled.
    """
    if not config.enabled:
        return cmd

    docker_cmd = [
        "docker",
        "run",
        "--rm",
        "--interactive",
        "--user", "1000:1000",
        "--security-opt", "no-new-privileges",
        "--cap-drop", "ALL",
        "--pids-limit", "256",
        f"--memory={config.memory_limit}",
        f"--cpus={config.cpu_limit}",
        "--tmpfs",
        f"/tmp:size={config.tmpfs_size}",
        "-v",
        f"{project_path}:{project_path}:rw",
        "-w",
        str(project_path),
    ]

    if not config.network:
        docker_cmd.append("--network=none")

    # Pass through essential env vars (not PATH/HOME — container has its own)
    docker_cmd.extend([
        "-e",
        "ANTHROPIC_API_KEY",
    ])

    docker_cmd.append(config.image)
    docker_cmd.extend(cmd)

    logger.info(
        "sandbox_command_built",
        project=str(project_path), network=config.network,
    )

    return docker_cmd
