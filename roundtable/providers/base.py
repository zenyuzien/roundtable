"""Provider abstraction.

Internal (provider-neutral) message format used by the engine:

    {"role": "user", "content": str}
    {"role": "assistant", "content": str, "tool_calls": [ToolCall-as-dict], "_raw": {...}}
    {"role": "tool", "results": [{"id": str, "name": str, "content": str}]}

``_raw`` holds the provider's exact assistant payload so a tool-use loop can be
continued faithfully; each provider only trusts ``_raw`` it produced itself.
"""
from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict


@dataclass
class LLMResponse:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0
    stop_reason: str | None = None
    raw_assistant: Any = None


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict  # JSON schema


class ProviderError(Exception):
    def __init__(self, message: str, *, retryable: bool = False, status: int | None = None,
                 retry_after: float | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.status = status
        self.retry_after = retry_after


class Provider:
    name = "base"

    async def complete(
        self,
        *,
        model: str,
        system: str,
        messages: list[dict],
        tools: list[ToolSpec],
        max_tokens: int,
        temperature: float | None = None,
        tool_choice_none: bool = False,
    ) -> LLMResponse:
        raise NotImplementedError


RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504, 529}


async def with_retries(
    fn: Callable[[], Awaitable[LLMResponse]],
    *,
    max_retries: int = 6,
    base_delay: float = 2.0,
    on_retry: Callable[[int, Exception, float], None] | None = None,
) -> LLMResponse:
    """Retry rate limits / transient failures with exponential backoff + jitter."""
    attempt = 0
    while True:
        try:
            return await fn()
        except ProviderError as e:
            if not e.retryable or attempt >= max_retries:
                raise
            delay = e.retry_after if e.retry_after else base_delay * (2 ** attempt)
            delay = min(delay, 90.0) * (0.8 + 0.4 * random.random())
            attempt += 1
            if on_retry:
                on_retry(attempt, e, delay)
            await asyncio.sleep(delay)


def parse_retry_after(headers: Any) -> float | None:
    v = headers.get("retry-after") if headers else None
    try:
        return float(v) if v is not None else None
    except ValueError:
        return None
