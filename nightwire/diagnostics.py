"""Configuration diagnostics for nightwire.

Provides health-check functions for each subsystem dependency:
Claude CLI, Signal API, sqlite-vec, sentence-transformers, and Docker.
Used by the ``/diagnose`` command and startup logging.

Key functions:
    check_claude_cli: Verify Claude CLI binary is accessible.
    check_signal_api: Verify Signal REST API is reachable.
    check_sqlite_vec: Verify sqlite-vec extension is importable.
    check_embeddings: Verify sentence-transformers is installed.
    check_docker: Verify Docker daemon is accessible.
    run_all_checks: Run all checks and return aggregated results.
"""

import asyncio
from typing import Dict, Tuple

import structlog

logger = structlog.get_logger("nightwire.diagnostics")

# Result tuple: (ok, detail, hint)
DiagResult = Tuple[bool, str, str]


async def check_claude_cli(claude_path: str = "claude") -> DiagResult:
    """Check if the Claude CLI binary is accessible.

    Args:
        claude_path: Path to the claude binary.

    Returns:
        Tuple of (ok, detail, hint).
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            claude_path, "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(
            proc.communicate(), timeout=10,
        )
        version = stdout.decode("utf-8", errors="replace").strip()
        if proc.returncode == 0 and version:
            return True, version, ""
        return (
            False,
            f"claude exited with code {proc.returncode}",
            "Install Claude Code: npm install -g @anthropic-ai/claude-code",
        )
    except FileNotFoundError:
        return (
            False,
            "claude binary not found",
            "Install Claude Code: npm install -g @anthropic-ai/claude-code",
        )
    except asyncio.TimeoutError:
        return (
            False,
            "claude --version timed out",
            "Check Claude Code installation: claude --version",
        )
    except Exception as exc:
        return (
            False,
            str(exc),
            "Install Claude Code: npm install -g @anthropic-ai/claude-code",
        )


async def check_signal_api(signal_api_url: str) -> DiagResult:
    """Check if the Signal REST API is reachable.

    Args:
        signal_api_url: Base URL of the Signal CLI REST API.

    Returns:
        Tuple of (ok, detail, hint).
    """
    try:
        import aiohttp

        async with aiohttp.ClientSession() as session:
            url = f"{signal_api_url}/v1/about"
            async with session.get(
                url, timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    versions = data.get("versions", [])
                    ver = versions[0] if versions else "unknown"
                    return True, f"Signal API v{ver}", ""
                return (
                    False,
                    f"Signal API returned {resp.status}",
                    "Start Signal bridge: docker compose up -d",
                )
    except Exception as exc:
        return (
            False,
            str(exc),
            "Start Signal bridge: docker compose up -d",
        )


def check_sqlite_vec() -> DiagResult:
    """Check if the sqlite-vec extension is importable.

    Returns:
        Tuple of (ok, detail, hint).
    """
    try:
        import sqlite_vec  # noqa: F401

        version = getattr(sqlite_vec, "__version__", "unknown")
        return True, f"sqlite-vec {version}", ""
    except ImportError:
        return (
            False,
            "sqlite-vec not installed",
            "Install: pip install sqlite-vec",
        )


def check_embeddings() -> DiagResult:
    """Check if sentence-transformers is installed.

    Returns:
        Tuple of (ok, detail, hint).
    """
    try:
        import sentence_transformers

        version = getattr(
            sentence_transformers, "__version__", "unknown",
        )
        return True, f"sentence-transformers {version}", ""
    except ImportError:
        return (
            False,
            "sentence-transformers not installed",
            "Install: pip install sentence-transformers",
        )


async def check_docker() -> DiagResult:
    """Check if the Docker daemon is accessible.

    Returns:
        Tuple of (ok, detail, hint).
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "info",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(
            proc.communicate(), timeout=10,
        )
        if proc.returncode == 0:
            # Extract version from output
            for line in stdout.decode(
                "utf-8", errors="replace"
            ).splitlines():
                if "Server Version:" in line:
                    ver = line.split(":", 1)[1].strip()
                    return True, f"Docker {ver}", ""
            return True, "Docker available", ""
        return (
            False,
            "Docker daemon not running",
            "Install Docker: https://docs.docker.com/get-docker/",
        )
    except FileNotFoundError:
        return (
            False,
            "docker binary not found",
            "Install Docker: https://docs.docker.com/get-docker/",
        )
    except asyncio.TimeoutError:
        return (
            False,
            "docker info timed out",
            "Check Docker daemon: docker info",
        )
    except Exception as exc:
        return (
            False,
            str(exc),
            "Install Docker: https://docs.docker.com/get-docker/",
        )


async def run_all_checks(config) -> Dict[str, DiagResult]:
    """Run all health checks and return aggregated results.

    Args:
        config: Nightwire Config instance.

    Returns:
        Dict mapping check name to (ok, detail, hint) tuple.
    """
    results: Dict[str, DiagResult] = {}

    results["Claude CLI"] = await check_claude_cli(
        config.claude_path,
    )
    results["Signal API"] = await check_signal_api(
        config.signal_api_url,
    )
    results["sqlite-vec"] = check_sqlite_vec()
    results["Embeddings"] = check_embeddings()
    results["Docker"] = await check_docker()

    return results
