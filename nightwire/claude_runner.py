"""CLI runner for nightwire coding agents."""

import asyncio
import json
import os
import re
import signal
from enum import Enum
from pathlib import Path
from typing import Callable, List, Optional, Tuple, Awaitable

import structlog

from .config import get_config
from .security import sanitize_input

logger = structlog.get_logger()

# Progress update interval in seconds (5 minutes to avoid spam)
PROGRESS_UPDATE_INTERVAL = 300

# Retry configuration
MAX_RETRIES = 1
RETRY_BASE_DELAY = 5  # seconds


def _normalize_cursor_agent_text(text: str) -> str:
    """Collapse huge blank runs and stray whitespace from Cursor Agent JSON streams."""
    text = (text or "").replace("\r\n", "\n").strip()
    if not text:
        return text
    lines = [ln.rstrip() for ln in text.split("\n")]
    collapsed: List[str] = []
    prev_blank = False
    for line in lines:
        is_blank = line == ""
        if is_blank and prev_blank:
            continue
        prev_blank = is_blank
        collapsed.append(line)
    text = "\n".join(collapsed).strip()
    return re.sub(r"\n{3,}", "\n\n", text)


class ErrorCategory(str, Enum):
    """Classification of Claude CLI errors for retry decisions."""

    TRANSIENT = "transient"
    PERMANENT = "permanent"
    INFRASTRUCTURE = "infrastructure"
    RATE_LIMITED = "rate_limited"


def classify_error(return_code: int, output: str, error_text: str) -> ErrorCategory:
    """Classify a Claude CLI error to decide whether to retry.

    Returns ErrorCategory indicating if the error is transient (retry),
    permanent (don't retry), or infrastructure (don't retry).
    """
    combined = (output + error_text).lower()

    # Permanent errors - don't retry
    if "prompt is too long" in combined or "conversation too long" in combined:
        return ErrorCategory.PERMANENT
    if "invalid api key" in combined or "authentication" in combined:
        return ErrorCategory.PERMANENT
    if "permission denied" in combined:
        return ErrorCategory.PERMANENT

    # Infrastructure errors - don't retry
    if return_code == 127:  # Command not found
        return ErrorCategory.INFRASTRUCTURE

    # Rate limit errors - check for subscription-level patterns first
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
    if "connection" in combined and ("reset" in combined or "refused" in combined):
        return ErrorCategory.TRANSIENT
    if "server error" in combined or "500" in combined or "502" in combined:
        return ErrorCategory.TRANSIENT
    if return_code in (-9, -15, 137, 143):  # Killed signals (likely shutdown)
        return ErrorCategory.PERMANENT

    # Non-zero exit with no specific error pattern - don't retry
    return ErrorCategory.PERMANENT


class ClaudeRunner:
    """Manages Claude CLI execution."""

    def __init__(self):
        self.config = get_config()
        self.current_project: Optional[Path] = None
        self._active_processes: set[asyncio.subprocess.Process] = set()
        self._guidelines: str = self._load_guidelines()

    def _load_guidelines(self) -> str:
        """Load the CLAUDE.md guidelines file."""
        guidelines_path = self.config.config_dir / "CLAUDE.md"
        if guidelines_path.exists():
            try:
                with open(guidelines_path, "r") as f:
                    content = f.read()
                logger.info("guidelines_loaded", path=str(guidelines_path))
                return content
            except Exception as e:
                logger.error("guidelines_load_error", error=str(e))
        return ""

    def set_project(self, project_path: Path):
        """Set the current project directory."""
        from .security import validate_project_path

        validated = validate_project_path(str(project_path))
        if validated is None:
            raise ValueError(f"Project path validation failed: access denied")
        self.current_project = validated
        logger.info("project_set", path=str(validated))

    def _build_runner_command(self, project_path: Path, prompt: str) -> List[str]:
        """Build CLI command for configured runner."""
        if self.config.runner_type == "opencode":
            return [
                self.config.runner_path,
                "run",
                "--format",
                "json",
                "--dir",
                str(project_path),
            ]
        if self.config.runner_type == "codex":
            command = [
                self.config.runner_path,
                "exec",
                "--json",
                "--skip-git-repo-check",
                "--sandbox",
                "danger-full-access",
            ]
            runner_model = getattr(self.config, "runner_model", None)
            if runner_model:
                command.extend(["-m", runner_model])
            runner_reasoning_effort = getattr(self.config, "runner_reasoning_effort", None)
            if runner_reasoning_effort:
                command.extend(
                    ["-c", f'model_reasoning_effort="{runner_reasoning_effort}"']
                )
            command.extend(["-C", str(project_path), "--", prompt])
            return command
        if self.config.runner_type == "cursor":
            runner_path = Path(self.config.runner_path)
            command = [str(runner_path)]
            if runner_path.name == "cursor":
                command.append("agent")
            command.extend(
                [
                    "--print",
                    "--output-format",
                    "json",
                    "--force",
                    "--trust",
                    "--workspace",
                    str(project_path),
                ]
            )
            runner_model = getattr(self.config, "runner_model", None)
            if runner_model:
                command.extend(["--model", runner_model])
            command.append(prompt)
            return command

        return [
            self.config.claude_path,
            "--print",
            "--dangerously-skip-permissions",
            "--verbose",
            "--max-turns",
            str(self.config.claude_max_turns),
            "--settings",
            '{"sandbox": {"enabled": false}}',
        ]

    def _build_subprocess_env(self) -> dict:
        """Build minimal environment for configured runner."""
        env = {
            "HOME": os.environ.get("HOME", ""),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "USER": os.environ.get("USER", ""),
            "LANG": os.environ.get("LANG", "en_US.UTF-8"),
            # Cap Node.js heap to 8GB to prevent OOM kills from runaway CLI processes
            "NODE_OPTIONS": os.environ.get("NODE_OPTIONS", "--max-old-space-size=8192"),
        }

        if self.config.runner_type in {"opencode", "codex", "cursor"}:
            env.update(
                {
                    "XDG_CONFIG_HOME": os.environ.get("XDG_CONFIG_HOME", ""),
                    "XDG_DATA_HOME": os.environ.get("XDG_DATA_HOME", ""),
                    "XDG_STATE_HOME": os.environ.get("XDG_STATE_HOME", ""),
                }
            )
        if self.config.runner_type == "opencode":
            env["OPENCODE_CONFIG_DIR"] = os.environ.get("OPENCODE_CONFIG_DIR", "")
        elif self.config.runner_type == "codex":
            env["CODEX_HOME"] = os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))
        elif self.config.runner_type == "cursor":
            env["CURSOR_API_KEY"] = os.environ.get("CURSOR_API_KEY", "")
        else:
            env["ANTHROPIC_API_KEY"] = os.environ.get("ANTHROPIC_API_KEY", "")

        return env

    def _extract_opencode_text(self, output: str) -> str:
        """Extract readable text from OpenCode line-delimited JSON output."""
        text_parts: List[str] = []

        def append_content_parts(content: object) -> None:
            if not isinstance(content, list):
                return
            for part in content:
                if isinstance(part, str):
                    text_parts.append(part)
                    continue
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "text" and isinstance(part.get("text"), str):
                    text_parts.append(part["text"])

        for line in output.splitlines():
            line = line.strip()
            if not line:
                continue

            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            if not isinstance(event, dict):
                continue

            event_type = event.get("type")
            if event_type == "text":
                if isinstance(event.get("text"), str):
                    text_parts.append(event["text"])
                part = event.get("part")
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    text_parts.append(part["text"])
            elif event_type == "content":
                append_content_parts(event.get("content"))
            elif event_type == "assistant_message":
                message = event.get("message")
                if isinstance(message, dict):
                    append_content_parts(message.get("content"))

        return "\n".join(text_parts).strip()

    def _extract_codex_text(self, output: str) -> str:
        """Extract readable text from Codex JSONL output."""
        text_parts: List[str] = []

        def append_text(text: object) -> None:
            if isinstance(text, str) and text.strip():
                text_parts.append(text.strip())

        def append_content_parts(content: object) -> None:
            if not isinstance(content, list):
                return
            for part in content:
                if isinstance(part, str):
                    append_text(part)
                    continue
                if not isinstance(part, dict):
                    continue
                append_text(part.get("text"))

        for line in output.splitlines():
            line = line.strip()
            if not line or not line.startswith("{"):
                continue

            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            if not isinstance(event, dict):
                continue

            event_type = event.get("type")
            if event_type == "item.completed":
                item = event.get("item")
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "agent_message":
                    append_text(item.get("text"))
                    append_content_parts(item.get("content"))

        return "\n".join(text_parts).strip()

    def _extract_cursor_text(self, output: str) -> str:
        """Extract readable text from Cursor Agent JSON output."""
        text_parts: List[str] = []

        def append_text(value: object) -> None:
            if isinstance(value, str) and value.strip():
                text_parts.append(value.strip())

        def append_content(value: object) -> None:
            if isinstance(value, str):
                append_text(value)
                return
            if isinstance(value, list):
                for item in value:
                    append_content(item)
                return
            if not isinstance(value, dict):
                return

            append_text(value.get("text"))
            append_text(value.get("content"))
            append_content(value.get("message"))
            append_content(value.get("messages"))
            append_content(value.get("result"))

        for line in output.splitlines():
            line = line.strip()
            if not line or not line.startswith("{"):
                continue

            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            if not isinstance(event, dict):
                continue

            append_text(event.get("text"))
            append_content(event.get("message"))
            append_content(event.get("messages"))
            append_content(event.get("content"))
            append_content(event.get("result"))

        return _normalize_cursor_agent_text("\n".join(dict.fromkeys(text_parts)))

    async def run_claude(
        self,
        prompt: str,
        timeout: Optional[int] = None,
        progress_callback: Optional[Callable[[str], Awaitable[None]]] = None,
        memory_context: Optional[str] = None,
        max_retries: int = MAX_RETRIES,
        project_path: Optional[Path] = None,
    ) -> Tuple[bool, str]:
        """
        Run Claude CLI with the given prompt, retrying on transient errors.

        Args:
            prompt: The prompt to send to Claude
            timeout: Optional timeout in seconds
            progress_callback: Optional async callback for progress updates
            memory_context: Optional memory context to inject (from MemoryManager)
            max_retries: Max retries for transient failures (default 2)
            project_path: Explicit project path (avoids shared-state race conditions)

        Returns:
            Tuple of (success, output)
        """
        # Check cooldown before doing any work
        from .rate_limit_cooldown import get_cooldown_manager

        cooldown = get_cooldown_manager()
        if cooldown.is_active:
            state = cooldown.get_state()
            return False, state.user_message

        effective_project = project_path or self.current_project
        if effective_project is None:
            return False, "No project selected. Use /select <project> first."

        if not effective_project.exists():
            return False, f"Project directory does not exist: {effective_project}"

        # Sanitize the prompt
        prompt = sanitize_input(prompt)

        # Build the full prompt: guidelines + memory context + current task
        prompt_parts = []

        if self._guidelines:
            prompt_parts.append(self._guidelines)

        if memory_context:
            prompt_parts.append(memory_context)

        prompt_parts.append(f"## Current Task\n\n{prompt}")

        full_prompt = "\n\n---\n\n".join(prompt_parts)

        if timeout is None:
            timeout = self.config.claude_timeout

        # Build the runner command. Claude/OpenCode still read prompts from stdin;
        # Codex/Cursor expect the prompt as a CLI argument.
        cmd = self._build_runner_command(effective_project, full_prompt)

        logger.info(
            "claude_run_start",
            project=str(effective_project),
            prompt_length=len(prompt),
            timeout=timeout,
        )

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
                await asyncio.sleep(delay)

            result = await self._execute_claude_once(
                cmd=cmd,
                prompt=full_prompt,
                timeout=timeout,
                progress_callback=progress_callback,
                project_path=effective_project,
            )

            success, output, error_category = result
            if success:
                logger.info(
                    "claude_run_complete",
                    output_length=len(output),
                    attempt=attempt + 1,
                    success=True,
                )
                return True, output

            last_error = output

            # Rate-limited errors activate cooldown immediately
            if error_category == ErrorCategory.RATE_LIMITED:
                logger.warning(
                    "claude_rate_limited",
                    error=output[:200],
                )
                cooldown.activate()
                return False, cooldown.get_state().user_message

            # Decide whether to retry based on error classification
            if error_category != ErrorCategory.TRANSIENT:
                logger.info(
                    "claude_no_retry",
                    category=error_category.value,
                    error=output[:200],
                )
                break

        # If we exhausted retries on a transient rate-limit error,
        # record it so consecutive failures can trigger cooldown
        if "rate limit" in last_error.lower() or "429" in last_error.lower():
            cooldown.record_rate_limit_failure()

        return False, last_error

    async def _execute_claude_once(
        self,
        cmd: List[str],
        prompt: str,
        timeout: int,
        progress_callback: Optional[Callable[[str], Awaitable[None]]] = None,
        project_path: Optional[Path] = None,
    ) -> Tuple[bool, str, ErrorCategory]:
        """Execute a single Claude CLI invocation.

        Claude/OpenCode receive the prompt via stdin. Codex/Cursor receive it
        as a CLI argument and must not inherit an open stdin pipe, otherwise
        they will try to append piped stdin to the prompt.

        Returns:
            Tuple of (success, output_or_error, error_category)
        """
        progress_task = None
        start_time = asyncio.get_running_loop().time()

        async def send_progress_updates():
            """Send periodic progress updates while Claude is running."""
            while True:
                await asyncio.sleep(PROGRESS_UPDATE_INTERVAL)
                elapsed = int(asyncio.get_running_loop().time() - start_time)
                elapsed_min = elapsed // 60
                if progress_callback:
                    try:
                        await progress_callback(f"Still working... ({elapsed_min} min elapsed)")
                    except Exception as e:
                        logger.warning("progress_callback_error", error=str(e))

        try:
            _subprocess_env = self._build_subprocess_env()

            effective_cwd = project_path or self.current_project

            # Optionally wrap in Docker sandbox
            from .sandbox import build_sandbox_command, SandboxConfig, validate_docker_available

            sandbox_settings = self.config.sandbox_config
            if sandbox_settings.get("enabled", False):
                available, docker_error = await asyncio.to_thread(validate_docker_available)
                if not available:
                    logger.error("sandbox_docker_unavailable", error=docker_error)
                    return False, docker_error, ErrorCategory.INFRASTRUCTURE

                sandbox_cfg = SandboxConfig(
                    enabled=True,
                    image=sandbox_settings.get("image", "nightwire-sandbox:latest"),
                    network=sandbox_settings.get("network", False),
                    memory_limit=sandbox_settings.get("memory_limit", "2g"),
                    cpu_limit=sandbox_settings.get("cpu_limit", 2.0),
                    tmpfs_size=sandbox_settings.get("tmpfs_size", "256m"),
                    runner_type=self.config.runner_type,
                )
                cmd = build_sandbox_command(list(cmd), effective_cwd, sandbox_cfg)

            stdin_target = asyncio.subprocess.PIPE
            if self.config.runner_type in {"codex", "cursor"}:
                stdin_target = asyncio.subprocess.DEVNULL

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(effective_cwd),
                stdin=stdin_target,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=_subprocess_env,
                start_new_session=True,  # Own process group for clean kill
            )
            self._active_processes.add(proc)

            if progress_callback:
                progress_task = asyncio.create_task(send_progress_updates())

            try:
                stdin_data = None
                if self.config.runner_type not in {"codex", "cursor"}:
                    stdin_data = prompt.encode("utf-8")
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(input=stdin_data), timeout=timeout
                )
            except asyncio.TimeoutError:
                self._kill_process_group(proc)
                await proc.wait()
                self._active_processes.discard(proc)
                elapsed = int(asyncio.get_running_loop().time() - start_time)
                elapsed_min = elapsed // 60
                logger.warning("claude_timeout", timeout=timeout, elapsed=elapsed)
                return (
                    False,
                    f"{self.config.runner_name} timed out after {elapsed_min} minutes. Consider breaking the task into smaller pieces.",
                    ErrorCategory.TRANSIENT,
                )
            finally:
                if progress_task:
                    progress_task.cancel()
                    try:
                        await progress_task
                    except asyncio.CancelledError:
                        pass

            self._active_processes.discard(proc)

            if proc.returncode is None:
                # Process was cancelled externally (e.g. shutdown)
                return False, f"{self.config.runner_name} process was cancelled.", ErrorCategory.INFRASTRUCTURE

            return_code = proc.returncode

            output = stdout.decode("utf-8", errors="replace")
            errors = stderr.decode("utf-8", errors="replace")

            if return_code != 0:
                category = classify_error(return_code, output, errors)

                combined_output = output + errors
                if (
                    "prompt is too long" in combined_output
                    or "Conversation too long" in combined_output
                ):
                    logger.warning("claude_token_limit", output=combined_output[:500])
                    return (
                        False,
                        "Task too complex - hit token limit. Try:\n"
                        "1. Break it into smaller tasks\n"
                        "2. Be more specific about what you need\n"
                        "3. Work on smaller files/sections",
                        ErrorCategory.PERMANENT,
                    )

                # Always log both stdout and stderr on failure for diagnostics
                logger.error(
                    "claude_error",
                    return_code=return_code,
                    stdout=output[:500] if output else "",
                    stderr=errors[:500] if errors else "",
                    category=category.value,
                )

                # Return the most informative error message available
                if errors:
                    return False, f"{self.config.runner_name} error: {errors[:1000]}", category
                if output:
                    return False, f"{self.config.runner_name} error: {output[:1000]}", category

                return False, f"{self.config.runner_name} exited with code {return_code}", category

            result = output if output else errors
            if return_code == 0:
                if self.config.runner_type == "opencode":
                    extracted = self._extract_opencode_text(output)
                    if extracted:
                        result = extracted
                elif self.config.runner_type == "codex":
                    extracted = self._extract_codex_text(output)
                    if extracted:
                        result = extracted
                elif self.config.runner_type == "cursor":
                    extracted = self._extract_cursor_text(output)
                    if extracted:
                        result = extracted

            return True, result, ErrorCategory.PERMANENT

        except FileNotFoundError:
            runner_name = self.config.runner_name
            logger.error("claude_not_found", runner=runner_name.lower())
            return (
                False,
                f"{runner_name} CLI not found. Make sure it's installed and in PATH.",
                ErrorCategory.INFRASTRUCTURE,
            )

        except Exception as e:
            logger.error("claude_exception", error=str(e), exc_type=type(e).__name__)
            return False, f"Error running {self.config.runner_name}: {str(e)}", ErrorCategory.INFRASTRUCTURE

    @staticmethod
    def _kill_process_group(proc: asyncio.subprocess.Process):
        """Kill the entire process group so child processes (SSH, node, etc.) are cleaned up."""
        try:
            pgid = os.getpgid(proc.pid)
            os.killpg(pgid, signal.SIGKILL)
            logger.info("process_group_killed", pid=proc.pid, pgid=pgid)
        except (ProcessLookupError, PermissionError):
            # Process already exited or we can't kill it; fall back to direct kill
            try:
                proc.kill()
            except ProcessLookupError:
                pass

    async def cancel(self):
        """Cancel all running Claude processes and their children."""
        procs = list(self._active_processes)
        self._active_processes.clear()
        for proc in procs:
            self._kill_process_group(proc)
            try:
                await proc.wait()
            except ProcessLookupError:
                pass
        if procs:
            logger.info("claude_cancelled", count=len(procs))


# Global runner instance
_runner: Optional[ClaudeRunner] = None


def get_runner() -> ClaudeRunner:
    """Get or create the global Claude runner instance."""
    global _runner
    if _runner is None:
        _runner = ClaudeRunner()
    return _runner
