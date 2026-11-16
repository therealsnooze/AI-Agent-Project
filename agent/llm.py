"""LLM integration: wraps the Anthropic Messages API with rate limiting,
retries, friendly error mapping, refusal fallbacks and usage accounting."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

import anthropic

from . import config


class LLMError(Exception):
    """An API failure translated into something a user can act on."""

    def __init__(self, user_message: str, *, retryable: bool = False, detail: str = ""):
        super().__init__(user_message)
        self.user_message = user_message
        self.retryable = retryable
        self.detail = detail


@dataclass
class Usage:
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0

    def add(self, model: str, usage: Any) -> None:
        price = config.MODELS.get(model, config.MODELS[config.DEFAULT_MODEL])
        inp = getattr(usage, "input_tokens", 0) or 0
        out = getattr(usage, "output_tokens", 0) or 0
        c_read = getattr(usage, "cache_read_input_tokens", 0) or 0
        c_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
        self.requests += 1
        self.input_tokens += inp
        self.output_tokens += out
        self.cache_read_tokens += c_read
        self.cache_write_tokens += c_write
        self.cost_usd += (
            inp * price["input"] + c_read * price["input"] * 0.1 + c_write * price["input"] * 1.25 + out * price["output"]
        ) / 1_000_000


@dataclass
class LLMClient:
    api_key: str | None = None
    model: str = config.DEFAULT_MODEL
    effort: str = config.DEFAULT_EFFORT
    on_notice: Callable[[str], None] | None = None
    usage: Usage = field(default_factory=Usage)
    fallbacks_enabled: bool = True
    last_latency_ms: int = 0
    _last_request_at: float = 0.0

    def __post_init__(self) -> None:
        self._client = anthropic.Anthropic(
            api_key=self.api_key or None,
            max_retries=config.SDK_MAX_RETRIES,
            timeout=config.REQUEST_TIMEOUT_SECONDS,
        )

    def _notice(self, msg: str) -> None:
        if self.on_notice:
            self.on_notice(msg)

    def _throttle(self) -> None:
        if self.usage.requests >= config.MAX_REQUESTS_PER_SESSION:
            raise LLMError(
                f"This session reached its limit of {config.MAX_REQUESTS_PER_SESSION} model calls. "
                "Reset the session to continue."
            )
        wait = config.MIN_SECONDS_BETWEEN_REQUESTS - (time.monotonic() - self._last_request_at)
        if wait > 0:
            time.sleep(wait)

    def _build_request(self, system: str, tools: list[dict], messages: list) -> dict:
        request: dict[str, Any] = {
            "model": self.model,
            "max_tokens": config.MAX_TOKENS,
            "system": system,
            "tools": tools,
            "messages": messages,
            "thinking": {"type": "adaptive", "display": "summarized"},
            "output_config": {"effort": self.effort},
            "cache_control": {"type": "ephemeral"},  # caches system + tools + history prefix
        }
        if self.fallbacks_enabled and config.MODELS.get(self.model, {}).get("server_fallbacks"):
            # If a safety classifier declines, the API re-runs the request on a
            # recommended fallback model instead of returning a refusal.
            request["betas"] = [config.FALLBACK_BETA]
            request["fallbacks"] = "default"
        return request

    def create(self, system: str, tools: list[dict], messages: list) -> Any:
        """Send one Messages API request. Raises LLMError on failure."""
        self._throttle()
        rate_limit_waits = 0
        while True:
            request = self._build_request(system, tools, messages)
            started = time.monotonic()
            try:
                self._last_request_at = started
                response = self._client.beta.messages.create(**request)
                self.usage.add(self.model, response.usage)
                self.last_latency_ms = int((time.monotonic() - started) * 1000)
                return response
            except anthropic.BadRequestError as e:
                # Org without access to the fallback beta: drop it and retry once.
                if "fallback" in str(e).lower() and self.fallbacks_enabled:
                    self.fallbacks_enabled = False
                    self._notice("Refusal fallbacks are unavailable for this API key; continuing without them.")
                    continue
                raise LLMError("The request was rejected by the API (400).", detail=_err_detail(e)) from e
            except anthropic.AuthenticationError as e:
                raise LLMError("Invalid or missing Anthropic API key.", detail=_err_detail(e)) from e
            except anthropic.PermissionDeniedError as e:
                raise LLMError("This API key is not allowed to use this model or feature.", detail=_err_detail(e)) from e
            except anthropic.NotFoundError as e:
                raise LLMError(f"Model '{self.model}' is not available to this API key.", detail=_err_detail(e)) from e
            except anthropic.RateLimitError as e:
                # The SDK already retried with backoff. Wait once more if the
                # server tells us a short retry-after, then give up.
                retry_after = _retry_after_seconds(e)
                if rate_limit_waits == 0 and retry_after <= 30:
                    rate_limit_waits += 1
                    self._notice(f"Rate limited by the API - waiting {retry_after:.0f}s before retrying...")
                    time.sleep(retry_after)
                    continue
                raise LLMError(
                    f"Rate limit reached. Please wait about {retry_after:.0f}s and try again.",
                    retryable=True,
                    detail=_err_detail(e),
                ) from e
            except anthropic.APITimeoutError as e:
                raise LLMError("The model took too long to respond. Try again or lower the effort level.", retryable=True) from e
            except anthropic.APIConnectionError as e:
                raise LLMError("Could not reach the Anthropic API. Check the network connection.", retryable=True) from e
            except anthropic.APIStatusError as e:
                if e.status_code >= 500:
                    raise LLMError(
                        f"The API is temporarily unavailable ({e.status_code}). Please try again shortly.",
                        retryable=True,
                        detail=_err_detail(e),
                    ) from e
                raise LLMError(f"API error ({e.status_code}).", detail=_err_detail(e)) from e


def _retry_after_seconds(e: anthropic.APIStatusError) -> float:
    try:
        return float(e.response.headers.get("retry-after", "20"))
    except (TypeError, ValueError):
        return 20.0


def _err_detail(e: anthropic.APIStatusError | Exception) -> str:
    request_id = getattr(e, "request_id", None)
    message = getattr(e, "message", str(e))
    return f"{message} (request_id={request_id})" if request_id else str(message)
