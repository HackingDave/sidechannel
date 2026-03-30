"""Tests for security module."""

import asyncio
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from nightwire.security import require_valid_project_path, validate_project_path


def test_require_valid_project_path_passes_valid_path():
    """Decorator should call the wrapped function when path is valid."""
    @require_valid_project_path
    def my_func(path: str, extra: str = "hello"):
        return f"ok:{path}:{extra}"

    with patch("nightwire.security.validate_project_path") as mock_validate:
        mock_validate.return_value = Path("/home/user/projects/valid")
        result = my_func("/home/user/projects/valid", extra="world")
        assert result == "ok:/home/user/projects/valid:world"
        mock_validate.assert_called_once_with("/home/user/projects/valid")


def test_require_valid_project_path_rejects_invalid_path():
    """Decorator should raise ValueError when path validation fails."""
    @require_valid_project_path
    def my_func(path: str):
        return "should not reach"

    with patch("nightwire.security.validate_project_path") as mock_validate:
        mock_validate.return_value = None
        with pytest.raises(ValueError, match="Path validation failed"):
            my_func("/etc/passwd")


def test_require_valid_project_path_works_with_path_kwarg():
    """Decorator should find 'path' in kwargs too."""
    @require_valid_project_path
    def my_func(path: str):
        return "ok"

    with patch("nightwire.security.validate_project_path") as mock_validate:
        mock_validate.return_value = Path("/valid")
        result = my_func(path="/valid")
        assert result == "ok"


def test_claude_runner_set_project_validates_path():
    """ClaudeRunner.set_project should reject invalid paths."""
    with patch("nightwire.security.validate_project_path") as mock_validate:
        mock_validate.return_value = None
        with patch("nightwire.claude_runner.get_config"):
            from nightwire.claude_runner import ClaudeRunner
            runner = ClaudeRunner.__new__(ClaudeRunner)
            runner.current_project = None
            with pytest.raises(ValueError, match="validation failed"):
                runner.set_project(Path("/etc/shadow"))


@pytest.mark.asyncio
async def test_rate_limiter_thread_safety():
    """Rate limiter should be safe under concurrent access."""
    from nightwire.security import check_rate_limit_async, _reset_rate_limits

    _reset_rate_limits()

    # Run many concurrent checks — should not raise
    async def check_many():
        tasks = [
            asyncio.create_task(check_rate_limit_async(f"+1555000{i:04d}"))
            for i in range(50)
        ]
        results = await asyncio.gather(*tasks)
        assert all(r == "allowed" for r in results)

    await check_many()


# --- validate_project_path tests ---

def test_validate_project_path_allows_base_path():
    """Path within base should be allowed."""
    with patch("nightwire.security.get_config") as mock_config:
        mock_config.return_value.projects_base_path = Path("/home/user/projects")
        mock_config.return_value.allowed_paths = []
        result = validate_project_path("/home/user/projects/myapp")
        assert result is not None
        assert result == Path("/home/user/projects/myapp").resolve()


def test_validate_project_path_blocks_traversal():
    """Directory traversal should be blocked."""
    with patch("nightwire.security.get_config") as mock_config:
        mock_config.return_value.projects_base_path = Path("/home/user/projects")
        mock_config.return_value.allowed_paths = []
        result = validate_project_path("/home/user/projects/../../etc/passwd")
        assert result is None


def test_validate_project_path_blocks_prefix_attack():
    """Path prefix attacks should be blocked (e.g. /home/user/projects-evil)."""
    with patch("nightwire.security.get_config") as mock_config:
        mock_config.return_value.projects_base_path = Path("/home/user/projects")
        mock_config.return_value.allowed_paths = []
        result = validate_project_path("/home/user/projects-evil/hack")
        assert result is None


# --- sanitize_input tests ---

def test_sanitize_input_strips_control_chars():
    """Control characters should be removed."""
    from nightwire.security import sanitize_input
    result = sanitize_input("hello\x00world\x01test")
    assert "\x00" not in result
    assert "\x01" not in result
    assert "hello" in result


def test_sanitize_input_preserves_newlines():
    """Newlines and tabs should be preserved."""
    from nightwire.security import sanitize_input
    result = sanitize_input("hello\nworld\ttab")
    assert "\n" in result
    assert "\t" in result


def test_sanitize_input_enforces_length_limit():
    """Input over 10000 chars should be truncated."""
    from nightwire.security import sanitize_input
    long_input = "a" * 20000
    result = sanitize_input(long_input)
    assert len(result) == 10000


def test_sanitize_input_removes_bidi_chars():
    """Unicode bidi override characters should be removed."""
    from nightwire.security import sanitize_input
    result = sanitize_input("hello\u202eworld")
    assert "\u202e" not in result


# --- normalize_phone_number tests ---

def test_normalize_phone_preserves_plus():
    from nightwire.security import normalize_phone_number
    assert normalize_phone_number("+12125551234") == "+12125551234"


def test_normalize_phone_strips_formatting():
    from nightwire.security import normalize_phone_number
    assert normalize_phone_number("+1 (212) 555-1234") == "+12125551234"


# --- is_uuid tests ---

def test_is_uuid_recognizes_valid_uuid():
    from nightwire.security import is_uuid
    assert is_uuid("abc12345-def6-7890-abcd-ef1234567890") is True


def test_is_uuid_rejects_phone_number():
    from nightwire.security import is_uuid
    assert is_uuid("+12125551234") is False


def test_is_uuid_rejects_partial_uuid():
    from nightwire.security import is_uuid
    assert is_uuid("abc12345-def6-7890") is False


# --- is_authorized UUID tests ---

def test_is_authorized_allows_uuid_sender():
    """UUID sender should be authorized when UUID is in allowed_numbers."""
    from nightwire.security import is_authorized
    uuid = "abc12345-def6-7890-abcd-ef1234567890"
    with patch("nightwire.security.get_config") as mock_config:
        mock_config.return_value.allowed_numbers = [uuid]
        assert is_authorized(uuid) is True


def test_is_authorized_rejects_unknown_uuid():
    """UUID sender not in allowed_numbers should be rejected."""
    from nightwire.security import is_authorized
    with patch("nightwire.security.get_config") as mock_config:
        mock_config.return_value.allowed_numbers = ["+12125551234"]
        assert is_authorized("abc12345-def6-7890-abcd-ef1234567890") is False


def test_is_authorized_mixed_uuid_and_phone():
    """Both UUIDs and phone numbers should work in allowed_numbers."""
    from nightwire.security import is_authorized
    uuid = "abc12345-def6-7890-abcd-ef1234567890"
    phone = "+12125551234"
    with patch("nightwire.security.get_config") as mock_config:
        mock_config.return_value.allowed_numbers = [uuid, phone]
        assert is_authorized(uuid) is True
        assert is_authorized(phone) is True
        assert is_authorized("+15559999999") is False


def test_is_authorized_phone_normalization_still_works():
    """Phone number normalization should still work for non-UUID senders."""
    from nightwire.security import is_authorized
    with patch("nightwire.security.get_config") as mock_config:
        mock_config.return_value.allowed_numbers = ["+12125551234"]
        assert is_authorized("+1 (212) 555-1234") is True
