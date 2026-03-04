"""Rate limit cooldown manager for nightwire.

Detects Claude subscription rate limits and pauses all Claude operations
until the cooldown period expires. Prevents wasted retries and spammy
failure notifications when the account hits its usage cap.
"""

import asyncio
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, List, Optional

import structlog

from .config import get_config

logger = structlog.get_logger("nightwire.security")

# Default configuration values
DEFAULT_COOLDOWN_MINUTES = 60
DEFAULT_CONSECUTIVE_THRESHOLD = 3
DEFAULT_FAILURE_WINDOW_SECONDS = 300


@dataclass
class CooldownState:
    """Snapshot of current cooldown status."""
    active: bool
    expires_at: Optional[float] = None  # Unix timestamp
    remaining_minutes: int = 0
    user_message: str = ""


@dataclass
class _FailureRecord:
    """Tracks a single rate-limit failure timestamp."""
    timestamp: float


class CooldownManager:
    """Manages rate-limit cooldown state for Claude operations.

    Tracks consecutive rate-limit failures within a time window and
    activates a cooldown period when the threshold is reached. Supports
    callbacks for pause/resume and user notification.
    """

    def __init__(self):
        """Initialize from rate_limit_cooldown settings.yaml section.

        Reads enabled, cooldown_minutes, consecutive_threshold,
        and failure_window_seconds from config with sensible
        defaults.
        """
        config = get_config()
        rl_config = config.settings.get("rate_limit_cooldown", {})

        self.enabled: bool = rl_config.get("enabled", True)
        self.cooldown_minutes: int = rl_config.get(
            "cooldown_minutes", DEFAULT_COOLDOWN_MINUTES
        )
        self.consecutive_threshold: int = rl_config.get(
            "consecutive_threshold", DEFAULT_CONSECUTIVE_THRESHOLD
        )
        self.failure_window_seconds: int = rl_config.get(
            "failure_window_seconds", DEFAULT_FAILURE_WINDOW_SECONDS
        )

        self._active: bool = False
        self._expires_at: Optional[float] = None
        self._resume_task: Optional[asyncio.Task] = None
        self._failures: List[_FailureRecord] = []

        # Callbacks
        self._on_activate: List[Callable[[], Awaitable[None]]] = []
        self._on_deactivate: List[Callable[[], Awaitable[None]]] = []

    def on_activate(self, callback: Callable[[], Awaitable[None]]) -> None:
        """Register a callback to fire when cooldown activates."""
        self._on_activate.append(callback)

    def on_deactivate(self, callback: Callable[[], Awaitable[None]]) -> None:
        """Register a callback to fire when cooldown deactivates."""
        self._on_deactivate.append(callback)

    def get_state(self) -> CooldownState:
        """Return a snapshot of current cooldown state."""
        if not self._active:
            return CooldownState(active=False)

        remaining = 0
        if self._expires_at:
            remaining = max(0, int((self._expires_at - time.time()) / 60))

        return CooldownState(
            active=True,
            expires_at=self._expires_at,
            remaining_minutes=remaining,
            user_message=(
                f"Claude is in cooldown mode (~{remaining} min remaining). "
                "The account has hit its rate limit. Commands will auto-resume "
                "when the cooldown expires, or use /cooldown clear to override."
            ),
        )

    @property
    def is_active(self) -> bool:
        """Check if cooldown is currently active."""
        return self._active

    def record_rate_limit_failure(self) -> None:
        """Record a rate-limit failure and activate cooldown if threshold met.

        Called when a Claude invocation fails with a rate-limit error.
        Prunes old failures outside the window, then checks if threshold
        is reached.
        """
        if not self.enabled:
            return

        now = time.time()
        self._failures.append(_FailureRecord(timestamp=now))

        # Prune failures outside the window
        cutoff = now - self.failure_window_seconds
        self._failures = [f for f in self._failures if f.timestamp >= cutoff]

        if len(self._failures) >= self.consecutive_threshold and not self._active:
            logger.warning(
                "cooldown_threshold_reached",
                failures=len(self._failures),
                threshold=self.consecutive_threshold,
                window_seconds=self.failure_window_seconds,
            )
            self._do_activate()

    def activate(self, cooldown_minutes: Optional[int] = None) -> None:
        """Explicitly activate cooldown.

        Called when a RATE_LIMITED error is detected or via
        ``/cooldown test``. Schedules auto-resume after expiry.

        Args:
            cooldown_minutes: Override duration (default from config).
        """
        if not self.enabled:
            return
        self._do_activate(cooldown_minutes)

    def _do_activate(self, cooldown_minutes: Optional[int] = None) -> None:
        """Internal activation logic."""
        minutes = cooldown_minutes or self.cooldown_minutes
        self._active = True
        self._expires_at = time.time() + (minutes * 60)
        self._failures.clear()

        logger.warning(
            "cooldown_activated",
            cooldown_minutes=minutes,
            expires_at=self._expires_at,
        )

        # Schedule auto-resume
        self._schedule_resume(minutes * 60)

        # Fire callbacks (best-effort, don't block)
        try:
            loop = asyncio.get_running_loop()
            for cb in self._on_activate:
                loop.create_task(self._safe_callback(cb, "activate"))
        except RuntimeError:
            pass  # No running loop (e.g., in sync test context)

    def deactivate(self) -> None:
        """Deactivate cooldown and resume Claude operations.

        Called by auto-resume timer or ``/cooldown clear``. Fires
        on_deactivate callbacks to notify users and restart the
        autonomous loop.
        """
        was_active = self._active
        self._active = False
        self._expires_at = None
        self._failures.clear()

        if self._resume_task and not self._resume_task.done():
            self._resume_task.cancel()
            self._resume_task = None

        if was_active:
            logger.info("cooldown_deactivated")
            try:
                loop = asyncio.get_running_loop()
                for cb in self._on_deactivate:
                    loop.create_task(self._safe_callback(cb, "deactivate"))
            except RuntimeError:
                pass

    def cancel_timer(self) -> None:
        """Cancel the auto-resume timer (for shutdown)."""
        if self._resume_task and not self._resume_task.done():
            self._resume_task.cancel()
            self._resume_task = None

    def _schedule_resume(self, delay_seconds: float) -> None:
        """Schedule automatic deactivation after delay."""
        if self._resume_task and not self._resume_task.done():
            self._resume_task.cancel()

        try:
            loop = asyncio.get_running_loop()
            self._resume_task = loop.create_task(self._auto_resume(delay_seconds))
        except RuntimeError:
            pass  # No running loop

    async def _auto_resume(self, delay_seconds: float) -> None:
        """Wait for cooldown to expire, then deactivate."""
        try:
            await asyncio.sleep(delay_seconds)
            logger.info("cooldown_auto_resume", delay_seconds=delay_seconds)
            self.deactivate()
        except asyncio.CancelledError:
            pass

    async def _safe_callback(self, cb: Callable[[], Awaitable[None]], name: str) -> None:
        """Run a callback with error handling."""
        try:
            await cb()
        except Exception as e:
            logger.error("cooldown_callback_error", callback=name, error=str(e))


# Global singleton
_manager: Optional[CooldownManager] = None


def get_cooldown_manager() -> CooldownManager:
    """Get or create the global CooldownManager instance."""
    global _manager
    if _manager is None:
        _manager = CooldownManager()
    return _manager
