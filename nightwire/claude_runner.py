"""Claude CLI runner for nightwire.

Wraps the Claude Code CLI (``claude -p``) as an async subprocess for
all Claude interactions. Supports both Pro/Max OAuth login and API
key authentication transparently — the CLI handles auth resolution.

Provides free-form text responses (run_claude) and structured JSON
output (run_claude_structured) with Pydantic model validation via
the ``--json-schema`` CLI flag.

Preserves the original ``run_claude() -> Tuple[bool, str]`` contract
so all existing call sites (bot.py, autonomous/) work unchanged.

Key classes:
    ClaudeRunner -- manages subprocess execution with retry,
        error classification, streaming, and cancel support.

Module-level functions:
    classify_error -- text-based error classification from CLI output.
    get_runner / get_claude_runner -- global singleton accessors.
"""

import asyncio
import json
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import (
    Awaitable,
    Callable,
    Optional,
    Tuple,
    Type,
    TypeVar,
    Union,
)

import structlog
from pydantic import BaseModel

from .config import get_config
from .exceptions import ErrorCategory
from .security import sanitize_input

logger = structlog.get_logger("nightwire.claude")

# Progress update interval in seconds (5 minutes to avoid spam)
PROGRESS_UPDATE_INTERVAL = 300

# Retry configuration
MAX_RETRIES = 1
RETRY_BASE_DELAY = 5  # seconds

# Streaming batch configuration
STREAM_SEND_INTERVAL = 2.0  # seconds between batched sends
STREAM_MIN_BATCH_CHARS = 50  # minimum chars before sending

T = TypeVar("T", bound=BaseModel)


@dataclass
class _InvocationState:
    """Per-invocation mutable state for concurrent run_claude() calls.

    Each call creates its own instance so concurrent invocations
    don't corrupt each other's process handle or cancelled flag.
    """

    process: Optional[asyncio.subprocess.Process] = None
    cancelled: bool = False
    _last_response: Optional[dict] = None
    _last_usage: Optional[dict] = None


def classify_error(
    return_code: int, output: str, error_text: str,
) -> ErrorCategory:
    """Classify error from CLI exit code and output text.

    Primary error classifier for the CLI subprocess runner. Parses
    stdout ``result`` text and stderr for known error patterns.

    Args:
        return_code: CLI process exit code.
        output: Stdout text (usually the ``result`` field).
        error_text: Stderr text from the CLI process.

    Returns:
        ErrorCategory indicating retry strategy.
    """
    combined = (output + error_text).lower()

    if "prompt is too long" in combined or "conversation too long" in combined:
        return ErrorCategory.PERMANENT
    if "invalid api key" in combined or "authentication" in combined:
        return ErrorCategory.PERMANENT
    if "permission denied" in combined:
        return ErrorCategory.PERMANENT

    if return_code == 127:
        return ErrorCategory.INFRASTRUCTURE

    if "rate limit" in combined or "429" in combined:
        subscription_patterns = (
            "usage limit",
            "daily limit",
            "capacity",
            "overloaded",
            "too many requests",
            "try again later",
            "quota exceeded",
            "hourly limit",
            "subscription",
        )
        for pattern in subscription_patterns:
            if pattern in combined:
                return ErrorCategory.RATE_LIMITED
        return ErrorCategory.TRANSIENT

    if "timeout" in combined or "timed out" in combined:
        return ErrorCategory.TRANSIENT
    if "connection" in combined and (
        "reset" in combined or "refused" in combined
    ):
        return ErrorCategory.TRANSIENT
    if (
        "server error" in combined
        or "500" in combined
        or "502" in combined
    ):
        return ErrorCategory.TRANSIENT
    # Kill signals (SIGKILL, SIGTERM) indicate the process was terminated —
    # retrying will not help
    if return_code in (-9, -15, 137, 143):
        return ErrorCategory.PERMANENT

    # Non-zero exit with no error details — nothing to retry
    if return_code != 0 and not error_text.strip():
        return ErrorCategory.PERMANENT

    return ErrorCategory.PERMANENT


class ClaudeRunner:
    """Manages Claude execution via the Claude Code CLI subprocess.

    Wraps ``claude -p`` with structured JSON output, streaming,
    retry with exponential backoff, per-invocation state isolation,
    and cooldown integration. Supports both Pro/Max OAuth and API
    key authentication transparently.
    """

    def __init__(self):
        """Initialize the runner with config-driven defaults."""
        self.config = get_config()
        self.current_project: Optional[Path] = None
        # Per-invocation state: keyed by incrementing ID
        self._active_invocations: dict[int, _InvocationState] = {}
        self._next_invocation_id: int = 0
        # Session ID from the most recent successful invocation.
        # Caller-managed: read via last_session_id, store externally.
        self._last_session_id: Optional[str] = None
        # Usage data from the most recent invocation (any outcome).
        # Race note: concurrent calls may overwrite. Callers should
        # read immediately after run_claude() returns.
        self._last_usage: Optional[dict] = None

    @property
    def last_session_id(self) -> Optional[str]:
        """Session ID from the most recent successful invocation.

        Only valid immediately after ``run_claude()`` returns successfully.
        Returns None if the last invocation failed or had no session_id.
        """
        return self._last_session_id

    @property
    def last_usage(self) -> Optional[dict]:
        """Usage data from the most recent invocation.

        Returns a dict with keys: ``input_tokens``, ``output_tokens``,
        ``model``, ``cost_usd``. Returns None if no invocation has
        completed or the last invocation produced no usage data.

        Race note: read immediately after ``run_claude()`` returns —
        concurrent invocations may overwrite this value.
        """
        return self._last_usage

    def _build_command(
        self,
        output_format: str = "json",
        json_schema: Optional[str] = None,
        verbose: bool = False,
        resume_session_id: Optional[str] = None,
    ) -> list:
        """Build the ``claude -p`` command with appropriate flags.

        Args:
            output_format: Output format (``json`` or ``stream-json``).
            json_schema: Optional JSON schema string for structured output.
            verbose: Whether to include verbose event output.
            resume_session_id: Optional session ID to resume a previous
                conversation via ``--resume``.

        Returns:
            List of command-line arguments.
        """
        cmd = [
            self.config.claude_path, "-p",
            "--output-format", output_format,
            "--model", self.config.claude_model,
            # Disable Claude Code sandbox to allow sudo/systemctl in tasks
            "--settings", '{"sandbox": {"enabled": false}}',
        ]
        if verbose:
            cmd.append("--verbose")
        if json_schema:
            cmd.extend(["--json-schema", json_schema])
        if resume_session_id:
            cmd.extend(["--resume", resume_session_id])
        max_turns = self.config.settings.get("claude_max_turns")
        if max_turns:
            cmd.extend(["--max-turns", str(max_turns)])
        budget = self.config.claude_max_budget_usd
        if budget is not None:
            cmd.extend(["--max-budget-usd", str(budget)])
        # System prompt from config CLAUDE.md file
        guidelines = self.config.config_dir / "CLAUDE.md"
        if guidelines.exists():
            cmd.extend([
                "--append-system-prompt-file",
                str(guidelines),
            ])
        return cmd

    def _build_prompt(
        self,
        prompt: str,
        memory_context: Optional[str] = None,
    ) -> str:
        """Build the prompt string for stdin delivery.

        Combines memory context and the user's prompt into a single
        string, separated by a horizontal rule.

        Args:
            prompt: The user's task or question.
            memory_context: Optional memory context to prepend.

        Returns:
            Assembled prompt string for piping to CLI stdin.
        """
        parts = []
        if memory_context:
            parts.append(memory_context)
        parts.append(f"## Current Task\n\n{prompt}")
        return "\n\n---\n\n".join(parts)

    def _new_invocation(self) -> Tuple[int, _InvocationState]:
        """Create and register a new per-invocation state.

        Returns:
            Tuple of (invocation_id, state). Caller MUST call
            ``_end_invocation(invocation_id)`` in a finally block.
        """
        inv_id = self._next_invocation_id
        self._next_invocation_id += 1
        state = _InvocationState()
        self._active_invocations[inv_id] = state
        return inv_id, state

    def _end_invocation(self, inv_id: int) -> None:
        """Remove a completed invocation from the active set."""
        self._active_invocations.pop(inv_id, None)

    async def _maybe_sandbox(
        self,
        cmd: list,
        effective_project: Optional[Path],
        cwd: Optional[str],
    ) -> Tuple[list, Optional[str]]:
        """Optionally wrap command in Docker sandbox.

        If ``sandbox.enabled`` is True in config, validates Docker
        availability and wraps the command using
        :func:`sandbox.build_sandbox_command`. When sandboxed, cwd
        is set to None (Docker manages the working directory).

        Args:
            cmd: Original CLI command list.
            effective_project: Project directory path.
            cwd: Original working directory string.

        Returns:
            Tuple of (possibly-wrapped cmd, possibly-cleared cwd).
        """
        if not self.config.sandbox_enabled or effective_project is None:
            return cmd, cwd
        try:
            from .sandbox import (
                SandboxConfig,
                build_sandbox_command,
                validate_docker_available,
            )
            available, error = await asyncio.to_thread(
                validate_docker_available,
            )
            if not available:
                logger.warning("sandbox_docker_unavailable", error=error)
                return cmd, cwd
            sandbox_cfg = SandboxConfig(**self.config.sandbox_config)
            sandbox_cfg.enabled = True
            wrapped = build_sandbox_command(
                cmd, effective_project, sandbox_cfg,
            )
            # Docker manages cwd via -w flag
            return wrapped, None
        except Exception as e:
            logger.warning("sandbox_wrap_error", error=str(e))
            return cmd, cwd

    def set_project(self, project_path: Optional[Path]):
        """Set or clear the current project directory.

        Args:
            project_path: Project path to validate and set, or
                None to clear the current project.

        Raises:
            ValueError: If path validation fails.
        """
        if project_path is None:
            self.current_project = None
            logger.info("project_cleared")
            return

        from .security import validate_project_path

        validated = validate_project_path(str(project_path))
        if validated is None:
            raise ValueError(
                "Project path validation failed: access denied"
            )
        self.current_project = validated
        logger.info("project_set", path=str(validated))

    async def run_claude(
        self,
        prompt: str,
        timeout: Optional[int] = None,
        progress_callback: Optional[
            Callable[[str], Awaitable[None]]
        ] = None,
        memory_context: Optional[str] = None,
        max_retries: int = MAX_RETRIES,
        project_path: Optional[Path] = None,
        stream: bool = False,
        resume_session_id: Optional[str] = None,
    ) -> Tuple[bool, str]:
        """Run Claude with the given prompt, retrying on transient errors.

        Primary method for free-form text responses. Preserves the
        exact same signature and return contract as always.

        Args:
            prompt: The prompt to send to Claude.
            timeout: Optional timeout in seconds (default from config).
            progress_callback: Optional async callback for progress.
                Non-streaming: receives heartbeat status messages.
                Streaming: receives batched text chunks.
            memory_context: Optional memory context to inject.
            max_retries: Max retries for transient failures.
            project_path: Explicit project path override.
            stream: If True, stream text chunks via progress_callback.
            resume_session_id: Optional CLI session ID to resume
                a prior conversation via ``--resume``.

        Returns:
            Tuple of (success: bool, output: str).
        """
        from .rate_limit_cooldown import get_cooldown_manager

        cooldown = get_cooldown_manager()
        if cooldown.is_active:
            state = cooldown.get_state()
            return False, state.user_message

        effective_project = project_path or self.current_project
        if effective_project is None:
            return False, (
                "No project selected. Use /select <project> first."
            )

        if not effective_project.exists():
            return False, (
                f"Project directory does not exist: "
                f"{effective_project}"
            )

        prompt = sanitize_input(prompt)
        prompt_str = self._build_prompt(prompt, memory_context)

        if timeout is None:
            timeout = self.config.claude_timeout

        logger.info(
            "claude_run_start",
            project=str(effective_project),
            prompt_length=len(prompt),
            timeout=timeout,
            model=self.config.claude_model,
        )

        inv_id, inv_state = self._new_invocation()
        try:
            result = await self._run_claude_inner(
                prompt_str=prompt_str,
                timeout=timeout,
                progress_callback=progress_callback,
                max_retries=max_retries,
                stream=stream,
                inv_state=inv_state,
                effective_project=effective_project,
                resume_session_id=resume_session_id,
            )
            # Extract session_id and usage before invocation cleanup
            success, output = result
            if success and inv_state._last_response:
                self._last_session_id = (
                    inv_state._last_response.get("session_id")
                )
            else:
                self._last_session_id = None
            # Always propagate usage (even on failure)
            self._last_usage = inv_state._last_usage
            return result
        finally:
            self._end_invocation(inv_id)

    async def _run_claude_inner(
        self,
        prompt_str: str,
        timeout: int,
        progress_callback: Optional[
            Callable[[str], Awaitable[None]]
        ],
        max_retries: int,
        stream: bool,
        inv_state: _InvocationState,
        effective_project: Path,
        resume_session_id: Optional[str] = None,
    ) -> Tuple[bool, str]:
        """Core retry loop, isolated with per-invocation state."""
        from .rate_limit_cooldown import get_cooldown_manager

        cooldown = get_cooldown_manager()
        last_error = ""

        for attempt in range(max_retries + 1):
            if attempt > 0:
                delay = RETRY_BASE_DELAY * (2 ** (attempt - 1))
                logger.info(
                    "claude_retry",
                    attempt=attempt,
                    delay=delay,
                    previous_error=last_error[:200],
                )
                if progress_callback:
                    try:
                        await progress_callback(
                            f"Retrying ({attempt}/{max_retries})"
                            f" after {delay}s delay..."
                        )
                    except Exception as e:
                        logger.warning(
                            "progress_callback_error",
                            error=str(e),
                        )
                await asyncio.sleep(delay)

            use_streaming = stream and progress_callback
            if use_streaming:
                logger.debug(
                    "claude_streaming_mode", stream=True,
                )
                success, output, error_category = (
                    await self._execute_once_streaming(
                        prompt_str=prompt_str,
                        timeout=timeout,
                        progress_callback=progress_callback,
                        inv_state=inv_state,
                        effective_project=effective_project,
                        resume_session_id=resume_session_id,
                    )
                )
            else:
                success, output, error_category = (
                    await self._execute_once(
                        prompt_str=prompt_str,
                        timeout=timeout,
                        progress_callback=progress_callback,
                        inv_state=inv_state,
                        effective_project=effective_project,
                        resume_session_id=resume_session_id,
                    )
                )

            if success:
                logger.info(
                    "claude_run_complete",
                    output_length=len(output),
                    attempt=attempt + 1,
                    success=True,
                )
                return True, output

            last_error = output

            if error_category == ErrorCategory.RATE_LIMITED:
                logger.warning(
                    "claude_rate_limited",
                    error=output[:200],
                )
                cooldown.activate()
                return False, cooldown.get_state().user_message

            if error_category != ErrorCategory.TRANSIENT:
                logger.info(
                    "claude_no_retry",
                    category=error_category.value,
                    error=output[:200],
                )
                break

            logger.debug(
                "claude_retry_decision",
                attempt=attempt + 1,
                max_retries=max_retries,
                error_category=error_category.value,
                will_retry=attempt < max_retries,
            )

        if (
            "rate limit" in last_error.lower()
            or "429" in last_error.lower()
        ):
            cooldown.record_rate_limit_failure()

        return False, last_error

    async def _execute_once(
        self,
        prompt_str: str,
        timeout: int,
        progress_callback: Optional[
            Callable[[str], Awaitable[None]]
        ] = None,
        inv_state: Optional[_InvocationState] = None,
        effective_project: Optional[Path] = None,
        json_schema: Optional[str] = None,
        resume_session_id: Optional[str] = None,
    ) -> Tuple[bool, str, Optional[ErrorCategory]]:
        """Execute a single CLI subprocess call (non-streaming).

        Args:
            prompt_str: Assembled prompt string for stdin.
            timeout: Timeout in seconds.
            progress_callback: Optional heartbeat callback.
            inv_state: Per-invocation state for cancel support.
            effective_project: Working directory for the subprocess.
            json_schema: Optional JSON schema for structured output.
            resume_session_id: Optional session ID for ``--resume``.

        Returns:
            Tuple of (success, output_or_error, error_category).
            error_category is None on success.
        """
        if inv_state is None:
            inv_state = _InvocationState()

        start_time = time.monotonic()
        progress_task = None

        cwd = str(effective_project) if effective_project else None

        cmd = self._build_command(
            output_format="json",
            json_schema=json_schema,
            resume_session_id=resume_session_id,
        )

        # Wrap in Docker sandbox if enabled
        cmd, cwd = await self._maybe_sandbox(cmd, effective_project, cwd)

        logger.debug(
            "claude_cli_exec",
            cmd_length=len(cmd),
            cwd=cwd,
            has_json_schema=bool(json_schema),
        )

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
            )
            inv_state.process = process

            # Heartbeat progress while subprocess runs
            if progress_callback:
                progress_task = asyncio.create_task(
                    self._send_heartbeats(
                        progress_callback, start_time,
                    )
                )

            try:
                stdout_bytes, stderr_bytes = (
                    await asyncio.wait_for(
                        process.communicate(
                            input=prompt_str.encode("utf-8"),
                        ),
                        timeout=timeout,
                    )
                )
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                elapsed = int(
                    (time.monotonic() - start_time) / 60
                )
                logger.warning(
                    "claude_timeout",
                    timeout=timeout, elapsed_min=elapsed,
                )
                return (
                    False,
                    f"Claude timed out after {elapsed} minutes."
                    " Consider breaking the task into smaller"
                    " pieces.",
                    ErrorCategory.TRANSIENT,
                )
            except asyncio.CancelledError:
                process.kill()
                await process.wait()
                logger.info("claude_cancelled_during_execution")
                return (
                    False,
                    "Claude request was cancelled.",
                    ErrorCategory.PERMANENT,
                )
            finally:
                inv_state.process = None
                if progress_task:
                    progress_task.cancel()
                    try:
                        await progress_task
                    except asyncio.CancelledError:
                        pass

            stdout = stdout_bytes.decode("utf-8", errors="replace")
            stderr = stderr_bytes.decode("utf-8", errors="replace")

            # Parse JSON response
            try:
                response = json.loads(stdout)
            except json.JSONDecodeError:
                logger.error(
                    "claude_non_json_output",
                    stdout_preview=stdout[:300],
                    stderr_preview=stderr[:300],
                    returncode=process.returncode,
                )
                category = classify_error(
                    process.returncode or 1, stdout, stderr,
                )
                return (
                    False,
                    f"Claude CLI error: "
                    f"{stderr or stdout[:500]}",
                    category,
                )

            # Check for error response
            if response.get("is_error"):
                result_text = response.get("result", "")
                category = classify_error(
                    process.returncode or 1,
                    result_text, stderr,
                )
                logger.debug(
                    "claude_error_classified",
                    category=category.value,
                    returncode=process.returncode,
                )

                if (
                    "too long" in result_text.lower()
                    or "token" in result_text.lower()
                ):
                    logger.warning(
                        "claude_token_limit",
                        error=result_text[:500],
                    )
                    return (
                        False,
                        "Task too complex - hit token limit."
                        " Try:\n"
                        "1. Break it into smaller tasks\n"
                        "2. Be more specific about what you"
                        " need\n"
                        "3. Work on smaller files/sections",
                        ErrorCategory.PERMANENT,
                    )

                return False, result_text or stderr, category

            # Log usage metadata
            usage = response.get("usage", {})
            model_usage = response.get("modelUsage", {})
            model_name = next(iter(model_usage), "unknown")
            logger.info(
                "claude_usage",
                input_tokens=usage.get("input_tokens", 0),
                output_tokens=usage.get("output_tokens", 0),
                model=model_name,
                response_time_ms=response.get(
                    "duration_ms", 0,
                ),
                cost_usd=response.get("total_cost_usd", 0),
                num_turns=response.get("num_turns", 1),
            )

            # Stash usage data for caller to record
            inv_state._last_usage = {
                "input_tokens": usage.get("input_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0),
                "model": model_name,
                "cost_usd": response.get("total_cost_usd", 0),
            }

            result = response.get("result", "")

            # Stash full response for structured output parsing
            inv_state._last_response = response

            return True, result, None

        except subprocess.SubprocessError as e:
            logger.error(
                "claude_subprocess_error",
                error=str(e),
                exc_type=type(e).__name__,
            )
            return (
                False,
                f"Error running Claude CLI: {e}",
                ErrorCategory.INFRASTRUCTURE,
            )
        except Exception as e:
            logger.error(
                "claude_exception",
                error=str(e),
                exc_type=type(e).__name__,
            )
            return (
                False,
                f"Error running Claude: {e}",
                ErrorCategory.INFRASTRUCTURE,
            )

    async def _send_heartbeats(
        self,
        progress_callback: Callable[[str], Awaitable[None]],
        start_time: float,
    ) -> None:
        """Send periodic heartbeat updates while Claude runs."""
        while True:
            await asyncio.sleep(PROGRESS_UPDATE_INTERVAL)
            elapsed_min = int(
                (time.monotonic() - start_time) / 60
            )
            try:
                await progress_callback(
                    f"Still working... ({elapsed_min} min elapsed)"
                )
            except Exception as e:
                logger.warning(
                    "progress_callback_error", error=str(e),
                )

    async def _execute_once_streaming(
        self,
        prompt_str: str,
        timeout: int,
        progress_callback: Callable[[str], Awaitable[None]],
        inv_state: _InvocationState,
        effective_project: Optional[Path] = None,
        resume_session_id: Optional[str] = None,
    ) -> Tuple[bool, str, Optional[ErrorCategory]]:
        """Execute CLI with streaming NDJSON output.

        Uses ``--output-format stream-json --verbose`` to receive
        NDJSON events. Text chunks are batched and delivered via
        progress_callback with time-based batching.

        Returns:
            Tuple of (success, full_output_or_error, error_category).
        """
        start_time = time.monotonic()
        cwd = str(effective_project) if effective_project else None

        cmd = self._build_command(
            output_format="stream-json", verbose=True,
            resume_session_id=resume_session_id,
        )

        # Wrap in Docker sandbox if enabled
        cmd, cwd = await self._maybe_sandbox(cmd, effective_project, cwd)

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
            )
            inv_state.process = process

            # Send prompt via stdin, then close stdin to signal EOF
            process.stdin.write(
                prompt_str.encode("utf-8")
            )
            await process.stdin.drain()
            process.stdin.close()

            # Drain stderr concurrently to prevent pipe deadlock
            # (subprocess blocks if stderr buffer fills while we
            # only read stdout)
            stderr_chunks: list = []

            async def drain_stderr():
                data = await process.stderr.read()
                if data:
                    stderr_chunks.append(data)

            stderr_task = asyncio.create_task(drain_stderr())

            result_text = ""
            batch_buffer: list = []
            batch_chars = 0
            last_send = time.monotonic()
            final_response: Optional[dict] = None

            async def read_ndjson_stream():
                """Read NDJSON lines from CLI stdout."""
                nonlocal result_text, final_response
                nonlocal batch_buffer, batch_chars, last_send

                while True:
                    line_bytes = await process.stdout.readline()
                    if not line_bytes:
                        break  # EOF
                    if inv_state.cancelled:
                        break

                    try:
                        event = json.loads(line_bytes)
                    except json.JSONDecodeError:
                        continue

                    etype = event.get("type")

                    if etype == "assistant":
                        msg = event.get("message", {})
                        for block in msg.get("content", []):
                            if block.get("type") == "text":
                                text = block["text"]
                                result_text += text
                                batch_buffer.append(text)
                                batch_chars += len(text)

                                now = time.monotonic()
                                elapsed = now - last_send
                                if (
                                    batch_chars
                                    >= STREAM_MIN_BATCH_CHARS
                                    and elapsed
                                    >= STREAM_SEND_INTERVAL
                                ):
                                    chunk = "".join(
                                        batch_buffer
                                    )
                                    try:
                                        await progress_callback(
                                            chunk,
                                        )
                                    except Exception as e:
                                        logger.warning(
                                            "stream_callback"
                                            "_error",
                                            error=str(e),
                                        )
                                    batch_buffer.clear()
                                    batch_chars = 0
                                    last_send = now

                    elif etype == "rate_limit_event":
                        info = event.get(
                            "rate_limit_info", {}
                        )
                        status = info.get("status", "")
                        if status not in (
                            "allowed",
                            "allowed_warning",
                        ):
                            from .rate_limit_cooldown import (
                                get_cooldown_manager,
                            )
                            get_cooldown_manager().activate()

                    elif etype == "result":
                        final_response = event
                        if not result_text:
                            result_text = event.get(
                                "result", ""
                            )

            try:
                await asyncio.wait_for(
                    read_ndjson_stream(), timeout=timeout,
                )
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                stderr_task.cancel()
                elapsed = int(
                    (time.monotonic() - start_time) / 60
                )
                logger.warning(
                    "claude_stream_timeout",
                    timeout=timeout, elapsed_min=elapsed,
                )
                return (
                    False,
                    f"Claude timed out after {elapsed}"
                    " minutes.",
                    ErrorCategory.TRANSIENT,
                )
            finally:
                inv_state.process = None

            if inv_state.cancelled:
                process.kill()
                await process.wait()
                stderr_task.cancel()
                logger.info(
                    "claude_cancelled_during_streaming",
                )
                return (
                    False,
                    "Claude request was cancelled.",
                    ErrorCategory.PERMANENT,
                )

            # Wait for process and stderr drain to finish
            await process.wait()
            await stderr_task
            stderr = b"".join(stderr_chunks).decode(
                "utf-8", errors="replace"
            )

            # Flush remaining buffer
            if batch_buffer:
                chunk = "".join(batch_buffer)
                try:
                    await progress_callback(chunk)
                except Exception as e:
                    logger.warning(
                        "stream_callback_error",
                        error=str(e),
                    )

            # Check for error in final response
            if final_response and final_response.get("is_error"):
                category = classify_error(
                    process.returncode or 1,
                    final_response.get("result", ""),
                    stderr,
                )
                return (
                    False,
                    final_response.get("result", ""),
                    category,
                )

            # Log usage from final result event
            if final_response:
                usage = final_response.get("usage", {})
                model_usage = final_response.get(
                    "modelUsage", {}
                )
                model_name = next(
                    iter(model_usage), "unknown"
                )
                logger.info(
                    "claude_usage",
                    input_tokens=usage.get(
                        "input_tokens", 0
                    ),
                    output_tokens=usage.get(
                        "output_tokens", 0
                    ),
                    model=model_name,
                    response_time_ms=final_response.get(
                        "duration_ms", 0
                    ),
                    cost_usd=final_response.get(
                        "total_cost_usd", 0
                    ),
                    streaming=True,
                )

                # Stash usage data for caller to record
                inv_state._last_usage = {
                    "input_tokens": usage.get(
                        "input_tokens", 0
                    ),
                    "output_tokens": usage.get(
                        "output_tokens", 0
                    ),
                    "model": model_name,
                    "cost_usd": final_response.get(
                        "total_cost_usd", 0
                    ),
                }

            # Stash full response for session_id extraction
            inv_state._last_response = final_response

            return True, result_text, None

        except Exception as e:
            logger.error(
                "claude_exception",
                error=str(e),
                exc_type=type(e).__name__,
                streaming=True,
            )
            return (
                False,
                f"Error running Claude: {e}",
                ErrorCategory.INFRASTRUCTURE,
            )

    async def run_claude_structured(
        self,
        prompt: str,
        response_model: Type[T],
        timeout: Optional[int] = None,
        memory_context: Optional[str] = None,
        max_retries: int = MAX_RETRIES,
        project_path: Optional[Path] = None,
    ) -> Tuple[bool, Union[T, str]]:
        """Run Claude with structured JSON output via --json-schema.

        Uses the CLI's ``--json-schema`` flag to enforce the response
        schema. Falls back to text extraction + Pydantic parsing if
        the structured_output field is missing.

        Args:
            prompt: The prompt to send to Claude.
            response_model: Pydantic BaseModel class for output.
            timeout: Optional timeout in seconds.
            memory_context: Optional memory context to inject.
            max_retries: Max retries for transient failures.
            project_path: Explicit project path override.

        Returns:
            Tuple of (success: bool, result: T | str).
            On success, result is a validated Pydantic model.
            On failure, result is an error string.
        """
        from .rate_limit_cooldown import get_cooldown_manager

        cooldown = get_cooldown_manager()
        if cooldown.is_active:
            state = cooldown.get_state()
            return False, state.user_message

        effective_project = project_path or self.current_project
        if effective_project is None:
            return False, (
                "No project selected. Use /select <project>"
                " first."
            )

        prompt = sanitize_input(prompt)
        prompt_str = self._build_prompt(prompt, memory_context)

        if timeout is None:
            timeout = self.config.claude_timeout

        # Build JSON schema from Pydantic model
        json_schema = json.dumps(
            response_model.model_json_schema()
        )

        logger.info(
            "claude_structured_start",
            model=self.config.claude_model,
            response_model=response_model.__name__,
            prompt_length=len(prompt),
        )

        inv_id, inv_state = self._new_invocation()
        try:
            result = await self._run_structured_inner(
                prompt_str=prompt_str,
                timeout=timeout,
                json_schema=json_schema,
                response_model=response_model,
                max_retries=max_retries,
                inv_state=inv_state,
                effective_project=effective_project,
            )
            # Propagate usage (even on failure)
            self._last_usage = inv_state._last_usage
            return result
        finally:
            self._end_invocation(inv_id)

    async def _run_structured_inner(
        self,
        prompt_str: str,
        timeout: int,
        json_schema: str,
        response_model: Type[T],
        max_retries: int,
        inv_state: _InvocationState,
        effective_project: Path,
    ) -> Tuple[bool, Union[T, str]]:
        """Core retry loop for run_claude_structured."""
        from .rate_limit_cooldown import get_cooldown_manager

        cooldown = get_cooldown_manager()
        last_error = ""

        for attempt in range(max_retries + 1):
            if attempt > 0:
                delay = RETRY_BASE_DELAY * (2 ** (attempt - 1))
                logger.info(
                    "claude_structured_retry",
                    attempt=attempt, delay=delay,
                )
                await asyncio.sleep(delay)

            success, output, error_category = (
                await self._execute_once(
                    prompt_str=prompt_str,
                    timeout=timeout,
                    inv_state=inv_state,
                    effective_project=effective_project,
                    json_schema=json_schema,
                )
            )

            if success:
                # Try structured_output field first
                resp = getattr(
                    inv_state, "_last_response", None
                )
                if resp and resp.get("structured_output"):
                    try:
                        parsed = response_model.model_validate(
                            resp["structured_output"]
                        )
                        logger.info(
                            "claude_structured_complete",
                            response_model=(
                                response_model.__name__
                            ),
                            attempt=attempt + 1,
                            source="structured_output",
                        )
                        return True, parsed
                    except Exception as e:
                        logger.warning(
                            "claude_structured_field_error",
                            error=str(e)[:200],
                        )

                # Fallback: parse result text as JSON
                try:
                    parsed = (
                        response_model.model_validate_json(
                            output
                        )
                    )
                    logger.info(
                        "claude_structured_complete",
                        response_model=(
                            response_model.__name__
                        ),
                        attempt=attempt + 1,
                        source="result_text",
                    )
                    return True, parsed
                except Exception as parse_err:
                    logger.warning(
                        "claude_structured_parse_error",
                        error=str(parse_err)[:200],
                        raw_output=output[:500],
                    )
                    # Try manual JSON extraction
                    try:
                        data = json.loads(output)
                        parsed = response_model.model_validate(
                            data
                        )
                        return True, parsed
                    except Exception:
                        last_error = (
                            "Failed to parse structured"
                            f" response: {parse_err}"
                        )
                        continue

            last_error = output

            if error_category == ErrorCategory.RATE_LIMITED:
                cooldown.activate()
                return False, (
                    cooldown.get_state().user_message
                )

            if error_category != ErrorCategory.TRANSIENT:
                break

        return False, last_error

    async def cancel(self):
        """Cancel ALL active Claude CLI subprocesses.

        Broadcast cancel: all concurrent run_claude() calls are
        affected. Sends SIGKILL to each active subprocess.
        """
        for state in list(self._active_invocations.values()):
            state.cancelled = True
            if state.process and state.process.returncode is None:
                try:
                    state.process.kill()
                except ProcessLookupError:
                    pass  # Already exited
        if self._active_invocations:
            logger.info(
                "claude_cancelled",
                count=len(self._active_invocations),
            )

    async def close(self):
        """No-op retained for interface compatibility.

        The CLI subprocess runner has no persistent connections
        to close. This method exists so callers that previously
        called ``runner.close()`` continue working without changes.
        """
        pass


# Global singleton
_runner: Optional[ClaudeRunner] = None


def get_runner() -> ClaudeRunner:
    """Get or create the global ClaudeRunner singleton."""
    global _runner
    if _runner is None:
        _runner = ClaudeRunner()
    return _runner


def get_claude_runner() -> ClaudeRunner:
    """Alias for get_runner() — backward compatibility."""
    return get_runner()
