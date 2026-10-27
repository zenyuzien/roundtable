from __future__ import annotations

import httpx

from .base import LLMResponse, Provider, ProviderError, RETRYABLE_STATUS, ToolCall, ToolSpec, parse_retry_after

API_URL = "https://api.anthropic.com/v1/messages"


class AnthropicProvider(Provider):
    name = "anthropic"

    def __init__(self, get_key, client: httpx.AsyncClient | None = None):
        self._get_key = get_key
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=20.0))

    def _convert(self, messages: list[dict]) -> list[dict]:
        out: list[dict] = []
        for m in messages:
            if m["role"] == "user":
                out.append({"role": "user", "content": m["content"]})
            elif m["role"] == "assistant":
                raw = m.get("_raw")
                if isinstance(raw, dict) and raw.get("provider") == self.name:
                    content = raw["content"]
                else:
                    content = []
                    if m.get("content"):
                        content.append({"type": "text", "text": m["content"]})
                    for tc in m.get("tool_calls", []):
                        content.append({"type": "tool_use", "id": tc["id"], "name": tc["name"], "input": tc["args"]})
                out.append({"role": "assistant", "content": content})
            elif m["role"] == "tool":
                out.append({
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": r["id"], "content": r["content"] or "(empty)"}
                        for r in m["results"]
                    ],
                })
        return out

    async def complete(self, *, model, system, messages, tools, max_tokens, temperature=None,
                       tool_choice_none=False) -> LLMResponse:
        key = self._get_key()
        if not key:
            raise ProviderError("Anthropic API key is not configured", retryable=False)
        body: dict = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": self._convert(messages),
        }
        if temperature is not None:
            body["temperature"] = temperature
        if tools:
            body["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": t.parameters} for t in tools
            ]
            if tool_choice_none:
                body["tool_choice"] = {"type": "none"}
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
        try:
            r = await self._client.post(API_URL, json=body, headers=headers)
        except httpx.HTTPError as e:
            raise ProviderError(f"Anthropic network error: {type(e).__name__}: {e}", retryable=True) from None
        if r.status_code != 200:
            try:
                detail = r.json().get("error", {}).get("message", r.text[:500])
            except ValueError:
                detail = r.text[:500]
            raise ProviderError(
                f"Anthropic HTTP {r.status_code}: {detail}",
                retryable=r.status_code in RETRYABLE_STATUS,
                status=r.status_code,
                retry_after=parse_retry_after(r.headers),
            )
        data = r.json()
        texts, calls = [], []
        for block in data.get("content", []):
            if block.get("type") == "text":
                texts.append(block["text"])
            elif block.get("type") == "tool_use":
                calls.append(ToolCall(id=block["id"], name=block["name"], args=block.get("input") or {}))
        usage = data.get("usage", {})
        tokens_in = (usage.get("input_tokens") or 0) + (usage.get("cache_creation_input_tokens") or 0) \
            + (usage.get("cache_read_input_tokens") or 0)
        return LLMResponse(
            text="\n".join(texts),
            tool_calls=calls,
            tokens_in=tokens_in,
            tokens_out=usage.get("output_tokens") or 0,
            stop_reason=data.get("stop_reason"),
            raw_assistant={"provider": self.name, "content": data.get("content", [])},
        )
