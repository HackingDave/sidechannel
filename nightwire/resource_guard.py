"""Resource guard -- checks system resources before spawning workers.

Uses psutil (optional dependency) to verify that memory and CPU
are sufficient before the autonomous loop dispatches a new parallel
task worker. Falls back gracefully if psutil is not installed.

Key classes:
    ResourceStatus: Dataclass snapshot of a resource check result.

Key functions:
    check_resources: Perform a single resource check and return
        a ResourceStatus indicating whether a worker can spawn.

Constants:
    MAX_MEMORY_PERCENT: Refuse to spawn above this threshold (90%).
    MIN_AVAILABLE_MB: Require at least this much free RAM (512 MB).
"""

from dataclasses import dataclass

import structlog

logger = structlog.get_logger("nightwire.autonomous")

# Thresholds
MAX_MEMORY_PERCENT = 90  # Don't spawn if memory > 90%
MIN_AVAILABLE_MB = 512   # Need at least 512MB free


@dataclass
class ResourceStatus:
    """Snapshot result of a system resource check.

    Attributes:
        ok: True if resources are sufficient to spawn a worker.
        memory_percent: Current memory usage percentage.
        memory_available_mb: Free memory in megabytes.
        cpu_count: Number of logical CPUs.
        reason: Human-readable explanation when ok is False.
    """
    ok: bool
    memory_percent: float
    memory_available_mb: float
    cpu_count: int
    reason: str = ""


def check_resources() -> ResourceStatus:
    """Check if system has enough resources to spawn a worker.

    Uses psutil if available, falls back gracefully.
    Returns ResourceStatus with ok=True if resources are sufficient.
    """
    try:
        import psutil
        mem = psutil.virtual_memory()
        memory_percent = mem.percent
        memory_available_mb = mem.available / (1024 * 1024)
        cpu_count = psutil.cpu_count() or 1

        ok = True
        reason = ""
        if memory_percent > MAX_MEMORY_PERCENT:
            ok = False
            reason = f"Memory usage too high: {memory_percent:.1f}%"
        elif memory_available_mb < MIN_AVAILABLE_MB:
            ok = False
            reason = f"Available memory too low: {memory_available_mb:.0f}MB"

        return ResourceStatus(
            ok=ok,
            memory_percent=memory_percent,
            memory_available_mb=memory_available_mb,
            cpu_count=cpu_count,
            reason=reason,
        )
    except ImportError:
        # psutil not installed — allow execution but warn
        logger.warning("psutil_not_installed", msg="Resource checks disabled")
        return ResourceStatus(
            ok=True,
            memory_percent=0.0,
            memory_available_mb=0.0,
            cpu_count=1,
            reason="psutil not installed, checks disabled",
        )
    except Exception as e:
        logger.warning("resource_check_error", error=str(e))
        return ResourceStatus(
            ok=True,
            memory_percent=0.0,
            memory_available_mb=0.0,
            cpu_count=1,
            reason=f"Check failed: {e}",
        )
