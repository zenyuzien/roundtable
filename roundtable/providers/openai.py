from __future__ import annotations

import json
import os

import httpx

from .base import LLMResponse, Provider, ProviderError, RETRYABLE_STATUS, ToolCall, parse_retry_after


class OpenAIProvider(Provider):
    """Chat Completions API. ``OPENAI_BASE_URL`` may point at any compatible server."""

    name = "openai"

    def __init__(self, get_key, client: httpx.AsyncClient | None = None):
        self._get_key = get_key
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=20.0))
        self.base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")

    def _convert(self, system: str, messages: list[dict]) -> list[dict]:
        out: list[dict] = [{"role": "system", "content": system}]
        for m in messages:
            if m["role"] == "user":
                out.append({"role": "user", "content": m["content"]})
            elif m["role"] == "assistant":
                raw = m.get("_raw")
                if isinstance(raw, dict) and raw.get("provider") == self.name:
                    out.append(raw["message"])
                    continue
                msg: dict = {"role": "assistant", "content": m.get("content") or None}
                if m.get("tool_calls"):
                    msg["tool_calls"] = [
                        {"id": tc["id"], "type": "function",
                         "function": {"name": tc["name"], "arguments": json.dumps(tc["args"])}}
                        for tc in m["tool_calls"]
                    ]
                out.append(msg)
            elif m["role"] == "tool":
                for r in m["results"]:
                    out.append({"role": "tool", "tool_call_id": r["id"], "content": r["content"] or "(empty)"})
        return out

    async def complete(self, *, model, system, messages, tools, max_tokens, temperature=None,
                       tool_choice_none=False) -> LLMResponse:
        key = self._get_key()
        if not key:
            raise ProviderError("OpenAI API key is not configured", retryable=False)
        body: dict = {
            "model": model,
            "messages": self._convert(system, messages),
            "max_completion_tokens": max_tokens,
        }
        if temperature is not None:
            body["temperature"] = temperature
        if tools:
            body["tools"] = [
                {"type": "function", "function": {"name": t.name, "description": t.description,
                                                  "parameters": t.parameters}}
                for t in tools
            ]
            if tool_choice_none:
                body["tool_choice"] = "none"
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        try:
            r = await self._client.post(f"{self.base_url}/chat/completions", json=body, headers=headers)
        except httpx.HTTPError as e:
            raise ProviderError(f"OpenAI network error: {type(e).__name__}: {e}", retryable=True) from None
        if r.status_code != 200:
            try:
                detail = r.json().get("error", {}).get("message", r.text[:500])
            except ValueError:
                detail = r.text[:500]
            raise ProviderError(
                f"OpenAI HTTP {r.status_code}: {detail}",
                retryable=r.status_code in RETRYABLE_STATUS,
                status=r.status_code,
                retry_after=parse_retry_after(r.headers),
            )
        data = r.json()
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        calls = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function", {})
            try:
                args = json.loads(fn.get("arguments") or "{}")
                if not isinstance(args, dict):
                    args = {"_value": args}
            except json.JSONDecodeError:
                args = {"_unparsed_arguments": fn.get("arguments")}
            calls.append(ToolCall(id=tc["id"], name=fn.get("name", ""), args=args))
        usage = data.get("usage") or {}
        raw_msg = {"role": "assistant", "content": msg.get("content")}
        if msg.get("tool_calls"):
            raw_msg["tool_calls"] = msg["tool_calls"]
        return LLMResponse(
            text=msg.get("content") or "",
            tool_calls=calls,
            tokens_in=usage.get("prompt_tokens") or 0,
            tokens_out=usage.get("completion_tokens") or 0,
            stop_reason=choice.get("finish_reason"),
            raw_assistant={"provider": self.name, "message": raw_msg},
        )
