"""Security module for nightwire."""

import asyncio
import functools
import inspect
import re
import time
import structlog
from collections import defaultdict
from pathlib import Path
from typing import Optional

from .config import get_config

logger = structlog.get_logger()

# Simple in-memory rate limiter
_rate_limit_data: dict = defaultdict(list)
_rate_limit_notified: dict = {}  # phone -> timestamp of first notification
_rate_limit_last_cleanup: float = 0.0
RATE_LIMIT_WINDOW = 60  # seconds
RATE_LIMIT_MAX_REQUESTS = 30  # max requests per window
_RATE_LIMIT_CLEANUP_INTERVAL = 300  # Prune stale entries every 5 minutes


def check_rate_limit(phone_number: str) -> str:
    """Check if a phone number is within rate limits.

    Returns:
        "allowed" — within limits, request recorded.
        "limited_notify" — first rejection in this window, caller should notify user.
        "limited_silent" — already notified this window, caller should silently drop.
    """
    global _rate_limit_last_cleanup
    now = time.time()
    window_start = now - RATE_LIMIT_WINDOW

    # Clean old entries and get recent requests
    _rate_limit_data[phone_number] = [
        ts for ts in _rate_limit_data[phone_number] if ts > window_start
    ]

    # Clear stale notification flags
    if phone_number in _rate_limit_notified and _rate_limit_notified[phone_number] < window_start:
        del _rate_limit_notified[phone_number]

    # Periodically prune phone numbers with no recent activity to prevent memory leak
    if now - _rate_limit_last_cleanup > _RATE_LIMIT_CLEANUP_INTERVAL:
        _rate_limit_last_cleanup = now
        stale_keys = [
            key for key, timestamps in _rate_limit_data.items()
            if not timestamps or timestamps[-1] < window_start
        ]
        for key in stale_keys:
            del _rate_limit_data[key]
        stale_notified = [
            key for key, ts in _rate_limit_notified.items() if ts < window_start
        ]
        for key in stale_notified:
            del _rate_limit_notified[key]

    if len(_rate_limit_data[phone_number]) >= RATE_LIMIT_MAX_REQUESTS:
        if phone_number in _rate_limit_notified:
            return "limited_silent"
        logger.warning(
            "rate_limit_exceeded",
            phone_number="..." + phone_number[-4:],
            requests_in_window=len(_rate_limit_data[phone_number])
        )
        _rate_limit_notified[phone_number] = now
        return "limited_notify"

    # Record this request
    _rate_limit_data[phone_number].append(now)
    return "allowed"


# Lock for rate limiter dict operations in async context
_rate_limit_lock = asyncio.Lock()


async def check_rate_limit_async(phone_number: str) -> str:
    """Async-safe version of check_rate_limit."""
    async with _rate_limit_lock:
        return check_rate_limit(phone_number)


def _reset_rate_limits():
    """Reset rate limit state (for testing)."""
    global _rate_limit_last_cleanup
    _rate_limit_data.clear()
    _rate_limit_notified.clear()
    _rate_limit_last_cleanup = 0.0


_UUID_PATTERN = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',
    re.IGNORECASE,
)


def is_uuid(value: str) -> bool:
    """Check if a string is a Signal UUID."""
    return bool(_UUID_PATTERN.match(value))


def normalize_phone_number(phone: str) -> str:
    """Normalize a phone number to E.164 format."""
    if phone.startswith("+"):
        return "+" + re.sub(r"[^\d]", "", phone[1:])
    return "+" + re.sub(r"[^\d]", "", phone)


def is_authorized(sender: str) -> bool:
    """Check if a sender (phone number or UUID) is authorized to use the bot."""
    config = get_config()
    allowed = config.allowed_numbers

    # Direct match first — handles UUIDs and already-normalized numbers
    if sender in allowed:
        return True

    # Try phone number normalization for non-UUID senders
    if not is_uuid(sender):
        normalized = normalize_phone_number(sender)
        normalized_allowed = [normalize_phone_number(n) for n in allowed if not is_uuid(n)]
        if normalized in normalized_allowed:
            return True

    logger.warning(
        "unauthorized_access_attempt",
        sender="..." + sender[-4:],
    )
    return False


def validate_project_path(path: str) -> Optional[Path]:
    """
    Validate that a project path is within the allowed directories.
    Returns the resolved path if valid, None otherwise.
    """
    config = get_config()
    base_path = config.projects_base_path.resolve()

    try:
        resolved = Path(path).resolve()

        if base_path in resolved.parents or resolved == base_path:
            return resolved

        # Append "/" to prevent prefix attacks (e.g. /home/projects matching /home/projects-evil)
        base_str = str(base_path) + "/"
        if str(resolved).startswith(base_str):
            return resolved

        for allowed_path in config.allowed_paths:
            allowed_resolved = allowed_path.resolve()
            if allowed_resolved in resolved.parents or resolved == allowed_resolved:
                return resolved
            allowed_str = str(allowed_resolved) + "/"
            if str(resolved).startswith(allowed_str):
                return resolved

        logger.warning(
            "path_validation_failed",
            requested_path=str(path),
            resolved_path=str(resolved),
            base_path=str(base_path),
            allowed_paths=[str(p) for p in config.allowed_paths]
        )
        return None

    except Exception as e:
        logger.error("path_validation_error", path=path, error=str(e))
        return None


def require_valid_project_path(func):
    """Decorator that validates the first 'path' argument via validate_project_path.

    Raises ValueError if path validation fails. Works with both positional
    and keyword 'path' arguments. Supports sync and async functions.
    """
    @functools.wraps(func)
    def sync_wrapper(*args, **kwargs):
        path = _extract_path(args, kwargs)
        if validate_project_path(str(path)) is None:
            raise ValueError(f"Path validation failed: access denied")
        return func(*args, **kwargs)

    @functools.wraps(func)
    async def async_wrapper(*args, **kwargs):
        path = _extract_path(args, kwargs)
        if validate_project_path(str(path)) is None:
            raise ValueError(f"Path validation failed: access denied")
        return await func(*args, **kwargs)

    if inspect.iscoroutinefunction(func):
        return async_wrapper
    return sync_wrapper


def _extract_path(args, kwargs):
    """Extract the path argument from args/kwargs. First positional arg or 'path' kwarg."""
    if "path" in kwargs:
        return kwargs["path"]
    if args:
        return args[0]
    raise ValueError("No path argument found")


def sanitize_input(text: str) -> str:
    """Sanitize user input — strip control characters and enforce length limit."""
    import unicodedata
    # Remove all control characters except newline, tab, carriage return
    text = ''.join(
        ch for ch in text
        if ch in ('\n', '\r', '\t') or not unicodedata.category(ch).startswith('C')
    )
    # Remove Unicode bidi override characters
    _BIDI_CHARS = set('\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069')
    text = ''.join(ch for ch in text if ch not in _BIDI_CHARS)
    max_length = 10000
    if len(text) > max_length:
        text = text[:max_length]
    return text
