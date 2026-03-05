"""Nightwire AI assistant runner for any OpenAI-compatible provider.

Provides a lightweight alternative to Claude for quick questions.
Supports any provider with an OpenAI-compatible chat completions
endpoint (OpenAI, Grok/xAI, local models, etc.). Includes both
plain-text and structured JSON output modes.

Key classes:
    AssistantResponse: Pydantic model wrapping a provider response.
    NightwireRunner: Manages API calls, session lifecycle, and
        response parsing for the configured provider.

Key functions:
    get_nightwire_runner: Singleton accessor for global instance.
"""

import asyncio
import json
from typing import Optional, Tuple, Type, TypeVar, Union
from urllib.parse import urlparse

import aiohttp
import structlog
from pydantic import BaseModel

logger = structlog.get_logger("nightwire.claude")

T = TypeVar("T", bound=BaseModel)


class AssistantResponse(BaseModel):
    """Structured response from the nightwire assistant.

    Wraps the raw OpenAI-compatible API response with typed fields.
    """

    content: str
    tokens_used: Optional[int] = None
    model: str


class NightwireRunner:
    """Manages API execution for nightwire AI assistant.

    Supports any OpenAI-compatible provider via /v1/chat/completions.
    OpenAI and Grok are available as built-in convenience presets.
    """

    SYSTEM_PROMPT = (
        "You are nightwire, an intelligent AI development assistant "
        "integrated into a Signal messaging bot.\n\n"
        "Personality:\n"
        "- Professional yet friendly\n"
        "- Concise and helpful\n"
        "- Technical but approachable\n"
        "- Direct and efficient\n\n"
        "Capabilities:\n"
        "- Answer questions on any topic\n"
        "- Provide technical guidance and recommendations\n"
        "- Help with research and analysis\n"
        "- Give coding advice and best practices\n\n"
        "When responding:\n"
        "- Be concise (responses go through Signal messages)\n"
        "- Lead with the answer, then provide context if needed\n"
        "- For coding/development tasks, remind the user that /select, "
        "/ask, and /do commands are available for hands-on Claude "
        "integration\n"
        "- Be helpful and informative without being verbose\n\n"
        "Response Style:\n"
        "- Clear and organized\n"
        "- Use bullet points for lists\n"
        "- Be thorough but concise\n"
        "- No emojis unless specifically requested"
    )

    def __init__(
        self,
        api_url: str,
        api_key: str,
        model: str,
        max_tokens: int = 1024,
    ):
        """Initialize the runner with provider credentials.

        Args:
            api_url: Full URL to the chat completions endpoint.
                Must use HTTPS.
            api_key: Bearer token for the provider API.
            model: Model identifier (e.g. "gpt-4o", "grok-3").
            max_tokens: Max tokens per response (default 1024).

        Raises:
            ValueError: If api_url is not HTTPS or has no host.
        """
        self.api_url = api_url
        self.api_key = api_key
        self.model = model
        self.max_tokens = max_tokens
        self._session: Optional[aiohttp.ClientSession] = None
        self._session_lock = asyncio.Lock()
        self._last_usage: Optional[dict] = None

        # Validate API URL scheme and hostname
        parsed = urlparse(self.api_url)
        if parsed.scheme != "https":
            logger.warning("insecure_api_url", url=self.api_url)
            raise ValueError("API URL must use HTTPS")
        if not parsed.hostname:
            logger.warning("invalid_api_url", url=self.api_url)
            raise ValueError("API URL must have a valid hostname")
        logger.info("nightwire_api_configured", host=parsed.hostname)

        if not self.api_key:
            logger.warning("nightwire_api_key_not_found")

    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create a shared aiohttp session (thread-safe)."""
        async with self._session_lock:
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession()
            return self._session

    @property
    def last_usage(self) -> Optional[dict]:
        """Usage data from the most recent invocation.

        Returns a dict with keys: ``input_tokens``, ``output_tokens``,
        ``model``, ``cost_usd``. Returns None if no invocation has
        completed or parsing failed.
        """
        return self._last_usage

    async def close(self):
        """Close the shared HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    def _clean_message(self, message: str) -> str:
        """Strip nightwire/sidechannel prefix variations from user message."""
        clean_message = message.strip()
        msg_lower = clean_message.lower()
        for variant in [
            "nightwire:", "nightwire,", "nightwire ",
            "hey nightwire ", "hi nightwire ", "ok nightwire ",
            "sidechannel:", "sidechannel,", "sidechannel ",
            "hey sidechannel ", "hi sidechannel ", "ok sidechannel ",
        ]:
            if msg_lower.startswith(variant):
                clean_message = clean_message[len(variant):].strip()
                break

        if clean_message.lower() in ("nightwire", "sidechannel") or not clean_message:
            clean_message = "Hello, how can you help me?"

        return clean_message

    def _build_headers(self) -> dict:
        """Build HTTP headers for the API request."""
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _build_payload(
        self,
        clean_message: str,
        response_format: Optional[dict] = None,
    ) -> dict:
        """Build the OpenAI-compatible API payload."""
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": self.SYSTEM_PROMPT},
                {"role": "user", "content": clean_message},
            ],
            "temperature": 0.7,
            "max_tokens": self.max_tokens,
        }
        if response_format:
            payload["response_format"] = response_format
        return payload

    def _parse_response(self, data: dict) -> Optional[AssistantResponse]:
        """Parse an OpenAI-compatible API response into AssistantResponse.

        Returns None if the response is malformed or has no content.
        """
        choices = data.get("choices")
        if not choices or not isinstance(choices, list):
            logger.error("nightwire_malformed_response", data_keys=list(data.keys()))
            return None

        content = choices[0].get("message", {}).get("content", "")
        if not content:
            logger.warning("nightwire_empty_response")
            return None

        usage = data.get("usage", {})
        tokens_used = usage.get("total_tokens") if usage else None
        model = data.get("model", self.model)

        # Stash usage data for caller to record
        self._last_usage = {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
            "model": model,
            "cost_usd": 0.0,  # OpenAI-compatible APIs don't report cost
        }

        return AssistantResponse(
            content=content,
            tokens_used=tokens_used,
            model=model,
        )

    async def _make_request(
        self,
        payload: dict,
        timeout: int,
    ) -> Tuple[bool, Union[AssistantResponse, str]]:
        """Execute an API request and parse the response.

        Returns:
            Tuple of (success, AssistantResponse | error_string).
        """
        if not self.api_key:
            return False, (
                "nightwire assistant is not configured. "
                "Set the API key for your provider in .env "
                "(see api_key_env in settings.yaml)."
            )

        try:
            session = await self._get_session()
            async with session.post(
                self.api_url,
                json=payload,
                headers=self._build_headers(),
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    parsed = self._parse_response(data)
                    if parsed is None:
                        return False, (
                            "Received an unexpected response from the AI provider. "
                            "Please try again."
                        )

                    logger.info(
                        "nightwire_response_success",
                        length=len(parsed.content),
                        tokens_used=parsed.tokens_used,
                        model=parsed.model,
                    )
                    return True, parsed
                else:
                    error_text = await resp.text()
                    logger.error(
                        "nightwire_api_error",
                        status=resp.status,
                        error=error_text[:500],
                    )
                    return False, (
                        f"nightwire encountered an error (status {resp.status}). "
                        "Please try again."
                    )

        except asyncio.TimeoutError:
            logger.warning("nightwire_timeout", timeout=timeout)
            return False, (
                "That query required more processing time than anticipated. "
                "Perhaps try a more specific question?"
            )

        except Exception as e:
            logger.error("nightwire_exception", error=str(e))
            return False, "I'm experiencing a temporary issue. Please try again."

    async def ask(
        self,
        message: str,
        timeout: int = 60,
    ) -> Tuple[bool, str]:
        """Send a query to the configured provider as nightwire AI assistant.

        Args:
            message: The user's message.
            timeout: Request timeout in seconds.

        Returns:
            Tuple of (success, response_text).
        """
        clean_message = self._clean_message(message)
        payload = self._build_payload(clean_message)

        success, result = await self._make_request(payload, timeout)
        if not success:
            return False, result  # result is an error string

        return True, result.content

    # Deprecated alias — use ask() instead
    ask_jarvis = ask

    async def ask_with_metadata(
        self,
        message: str,
        timeout: int = 60,
    ) -> Tuple[bool, Union[AssistantResponse, str]]:
        """Send a query and return the full AssistantResponse with metadata.

        Same as ask() but returns the AssistantResponse model on success,
        which includes tokens_used and model fields.

        Args:
            message: The user's message.
            timeout: Request timeout in seconds.

        Returns:
            Tuple of (success, AssistantResponse | error_string).
        """
        clean_message = self._clean_message(message)
        payload = self._build_payload(clean_message)

        success, result = await self._make_request(payload, timeout)
        if not success:
            return False, result

        return True, result

    async def ask_structured(
        self,
        message: str,
        response_model: Type[T],
        timeout: int = 60,
    ) -> Tuple[bool, Union[T, str]]:
        """Send a query with structured JSON output, returning a Pydantic model.

        Uses the OpenAI-compatible response_format: {"type": "json_object"}
        to request JSON output, then validates against the provided model.

        Args:
            message: The user's message (should instruct JSON output format).
            response_model: Pydantic BaseModel class defining expected schema.
            timeout: Request timeout in seconds.

        Returns:
            Tuple of (success, validated_model | error_string).
        """
        clean_message = self._clean_message(message)
        payload = self._build_payload(
            clean_message,
            response_format={"type": "json_object"},
        )

        success, result = await self._make_request(payload, timeout)
        if not success:
            return False, result

        # result is an AssistantResponse — parse content as JSON into model
        raw_content = result.content
        try:
            parsed = response_model.model_validate_json(raw_content)
            logger.info(
                "nightwire_structured_success",
                response_model=response_model.__name__,
                tokens_used=result.tokens_used,
            )
            return True, parsed
        except Exception as parse_err:
            logger.warning(
                "nightwire_structured_parse_error",
                error=str(parse_err)[:200],
                raw_output=raw_content[:500],
            )
            # Fallback: try manual JSON extraction
            try:
                data = json.loads(raw_content)
                parsed = response_model.model_validate(data)
                return True, parsed
            except Exception:
                return False, (
                    f"Failed to parse structured response: {parse_err}"
                )


# Global instance
_nightwire_runner: Optional[NightwireRunner] = None


def get_nightwire_runner(
    api_url: str = "",
    api_key: str = "",
    model: str = "",
    max_tokens: int = 1024,
) -> NightwireRunner:
    """Get or create the global nightwire runner instance."""
    global _nightwire_runner
    if _nightwire_runner is None:
        _nightwire_runner = NightwireRunner(
            api_url=api_url,
            api_key=api_key,
            model=model,
            max_tokens=max_tokens,
        )
    return _nightwire_runner
