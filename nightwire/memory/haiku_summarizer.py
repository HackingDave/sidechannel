"""Haiku-based summarization for memory context.

Uses Claude Haiku (via Claude CLI subprocess) to summarize retrieved
memories into concise context that can be injected into prompts
without bloating the token count. The CLI subprocess is spawned
per-request with ``--model haiku``.

Key class:
    HaikuSummarizer -- lightweight subagent that distills relevant
        past conversations into bullet-point summaries.

Module-level functions:
    get_haiku_summarizer() -- returns the global singleton.
    close_summarizer() -- no-op retained for interface compatibility.
"""

import asyncio
import json
from typing import List, Optional

import structlog

from .models import SearchResult

logger = structlog.get_logger("nightwire.memory")


class HaikuSummarizer:
    """Uses Claude Haiku to summarize context for injection.

    Spawns a ``claude -p --model haiku`` subprocess per request.
    Lightweight subagent that summarizes retrieved memories into
    a concise format suitable for prompt injection.
    """

    def __init__(
        self,
        model: str = "haiku",
        timeout: int = 30,
    ):
        """Initialize the summarizer.

        Args:
            model: CLI model alias for summarization.
            timeout: Seconds before a CLI call times out.
        """
        self.model = model
        self.timeout = timeout
        self._last_usage: Optional[dict] = None

    @property
    def last_usage(self) -> Optional[dict]:
        """Usage data from the most recent summarization call.

        Returns a dict with keys: ``input_tokens``, ``output_tokens``,
        ``model``, ``cost_usd``. Returns None if no call has completed.
        """
        return self._last_usage

    async def close(self):
        """No-op retained for interface compatibility.

        The CLI subprocess runner has no persistent connections.
        """
        pass

    async def summarize_for_context(
        self,
        retrieved_memories: List[SearchResult],
        current_query: str,
        max_output_tokens: int = 500,
    ) -> Optional[str]:
        """Summarize retrieved memories relevant to current query.

        Args:
            retrieved_memories: List of relevant search results.
            current_query: The current user query/task.
            max_output_tokens: Maximum tokens in output.

        Returns:
            Summarized context string, or None if fails.
        """
        if not retrieved_memories:
            return None

        memory_sections = []
        for mem in retrieved_memories[:10]:
            date = mem.timestamp.strftime("%Y-%m-%d")
            role = "User" if mem.role == "user" else "Assistant"
            content = mem.content[:500]
            memory_sections.append(
                f"[{date}] {role}: {content}"
            )

        memory_text = "\n---\n".join(memory_sections)

        prompt = (
            "You are a context summarization assistant. "
            "Your job is to extract and summarize the most"
            " relevant information from past conversations."
            "\n\n"
            f"Given these past conversation snippets:\n\n"
            f"{memory_text}\n\n"
            "The user is now asking about or working on: "
            f'"{current_query}"\n\n'
            "Summarize ONLY the information from the past"
            " conversations that is directly relevant to the"
            " current query. Focus on:\n"
            "- Previous decisions or choices made\n"
            "- Technical preferences or patterns established\n"
            "- Key facts or context that would help\n"
            "- Any warnings or lessons learned\n\n"
            f"Output a concise summary (max {max_output_tokens}"
            " tokens) using bullet points.\n"
            'If nothing is relevant, say "No relevant past'
            ' context found."'
        )

        try:
            result = await self._run_claude(prompt)
            return result
        except Exception as e:
            logger.warning(
                "haiku_summarization_failed", error=str(e),
            )
            return None

    async def _run_claude(
        self, prompt: str,
    ) -> Optional[str]:
        """Run Claude Haiku via CLI subprocess.

        Args:
            prompt: The summarization prompt.

        Returns:
            Result text, or None on failure.
        """
        from ..config import get_config

        config = get_config()

        cmd = [
            config.claude_path, "-p",
            "--output-format", "json",
            "--model", self.model,
        ]

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_bytes, _ = await asyncio.wait_for(
                process.communicate(
                    input=prompt.encode("utf-8"),
                ),
                timeout=self.timeout,
            )
            stdout = stdout_bytes.decode(
                "utf-8", errors="replace"
            )
            response = json.loads(stdout)
            # Stash usage data for caller to record
            usage = response.get("usage", {})
            model_usage = response.get("modelUsage", {})
            model_name = next(iter(model_usage), self.model)
            self._last_usage = {
                "input_tokens": usage.get("input_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0),
                "model": model_name,
                "cost_usd": response.get("total_cost_usd", 0),
            }
            result = response.get("result", "").strip()
            return result or None

        except asyncio.TimeoutError:
            logger.warning(
                "haiku_timeout", timeout=self.timeout,
            )
            return None
        except Exception as e:
            logger.error("haiku_error", error=str(e))
            return None


# Global summarizer instance
_summarizer: Optional[HaikuSummarizer] = None


def get_haiku_summarizer() -> HaikuSummarizer:
    """Get or create the global Haiku summarizer singleton.

    Returns:
        The HaikuSummarizer singleton instance.
    """
    global _summarizer
    if _summarizer is None:
        _summarizer = HaikuSummarizer()
    return _summarizer


async def close_summarizer() -> None:
    """Close the global summarizer and reset it.

    No-op for the CLI-based summarizer, but retained for
    interface compatibility with callers that expect it.
    """
    global _summarizer
    if _summarizer is not None:
        await _summarizer.close()
        _summarizer = None
