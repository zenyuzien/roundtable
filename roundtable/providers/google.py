"""Google AI Studio (Gemini API) via the REST ``generateContent`` endpoint.

The API key is sent in the ``x-goog-api-key`` header, never in the URL, so it cannot
end up in logged request lines.
"""
from __future__ import annotations

import os
import uuid

import httpx

from .base import LLMResponse, Provider, ProviderError, RETRYABLE_STATUS, ToolCall, ToolSpec, parse_retry_after


def _schema(params: dict) -> dict | None:
    """Gemini rejects OBJECT schemas with no properties; omit parameters in that case."""
    if params.get("type") == "object" and not params.get("properties"):
        return None
    return params


def _duration(s: str | None) -> float | None:
    try:
        return float(s.rstrip("s")) if s else None
    except ValueError:
        return None


def _error(r: httpx.Response) -> ProviderError:
    """Map a Gemini error response. Daily quota exhaustion is not worth retrying; per-minute limits are."""
    try:
        err = r.json().get("error", {})
    except ValueError:
        err = {}
    detail = err.get("message") or r.text[:500]
    retry_after = parse_retry_after(r.headers)
    daily = None
    for d in err.get("details") or []:
        t = d.get("@type", "")
        if t.endswith("RetryInfo"):
            retry_after = _duration(d.get("retryDelay")) or retry_after
        elif t.endswith("QuotaFailure"):
            for v in d.get("violations") or []:
                if "PerDay" in (v.get("quotaId") or ""):
                    daily = v
    if r.status_code == 429 and daily:
        hours = f" (resets in about {retry_after / 3600:.1f} h)" if retry_after else ""
        model = (daily.get("quotaDimensions") or {}).get("model", "this model")
        return ProviderError(
            f"Gemini daily free-tier quota exhausted for {model}: {daily.get('quotaValue', '?')} requests/day{hours}. "
            "Switch this agent to another model (quotas are per model), or enable billing in Google AI Studio.",
            retryable=False, status=429)
    return ProviderError(f"Gemini HTTP {r.status_code}: {detail}", retryable=r.status_code in RETRYABLE_STATUS,
                         status=r.status_code, retry_after=retry_after)


class GoogleProvider(Provider):
    name = "google"

    def __init__(self, get_key, client: httpx.AsyncClient | None = None):
        self._get_key = get_key
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=20.0))
        self.base_url = os.environ.get("GEMINI_BASE_URL",
                                       "https://generativelanguage.googleapis.com/v1beta").rstrip("/")

    def _convert(self, messages: list[dict]) -> list[dict]:
        out: list[dict] = []
        for m in messages:
            if m["role"] == "user":
                out.append({"role": "user", "parts": [{"text": m["content"]}]})
            elif m["role"] == "assistant":
                raw = m.get("_raw")
                if isinstance(raw, dict) and raw.get("provider") == self.name:
                    # echo the model's parts verbatim: they may carry thought signatures
                    out.append(raw["content"])
                    continue
                parts: list[dict] = []
                if m.get("content"):
                    parts.append({"text": m["content"]})
                for tc in m.get("tool_calls", []):
                    parts.append({"functionCall": {"name": tc["name"], "args": tc["args"]}})
                out.append({"role": "model", "parts": parts or [{"text": ""}]})
            elif m["role"] == "tool":
                out.append({"role": "user", "parts": [
                    {"functionResponse": {"name": r["name"], "response": {"result": r["content"] or "(empty)"}}}
                    for r in m["results"]]})
        return out

    async def complete(self, *, model, system, messages, tools, max_tokens, temperature=None,
                       tool_choice_none=False) -> LLMResponse:
        key = self._get_key()
        if not key:
            raise ProviderError("Google AI Studio (Gemini) API key is not configured", retryable=False)
        gen: dict = {"maxOutputTokens": max_tokens}
        if temperature is not None:
            gen["temperature"] = temperature
        body: dict = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": self._convert(messages),
            "generationConfig": gen,
        }
        if tools:
            decls = []
            for t in tools:
                d = {"name": t.name, "description": t.description}
                s = _schema(t.parameters)
                if s is not None:
                    d["parameters"] = s
                decls.append(d)
            body["tools"] = [{"functionDeclarations": decls}]
            if tool_choice_none:
                body["toolConfig"] = {"functionCallingConfig": {"mode": "NONE"}}
        model_path = model if model.startswith(("models/", "tunedModels/")) else f"models/{model}"
        headers = {"x-goog-api-key": key, "Content-Type": "application/json"}
        try:
            r = await self._client.post(f"{self.base_url}/{model_path}:generateContent", json=body, headers=headers)
        except httpx.HTTPError as e:
            raise ProviderError(f"Gemini network error: {type(e).__name__}: {e}", retryable=True) from None
        if r.status_code != 200:
            raise _error(r)
        data = r.json()
        usage = data.get("usageMetadata") or {}
        tokens_in = usage.get("promptTokenCount") or 0
        # thinking tokens are billed as output
        tokens_out = (usage.get("candidatesTokenCount") or 0) + (usage.get("thoughtsTokenCount") or 0)
        cands = data.get("candidates") or []
        if not cands:
            reason = (data.get("promptFeedback") or {}).get("blockReason", "no candidates returned")
            raise ProviderError(f"Gemini returned no answer (prompt blocked: {reason})", retryable=False)
        cand = cands[0]
        content = cand.get("content") or {"role": "model", "parts": []}
        texts, calls = [], []
        for p in content.get("parts") or []:
            if p.get("thought"):
                continue  # thought summaries are not part of the answer
            if "text" in p:
                texts.append(p["text"])
            elif "functionCall" in p:
                fc = p["functionCall"]
                calls.append(ToolCall(id=fc.get("id") or f"call_{uuid.uuid4().hex[:12]}",
                                      name=fc.get("name", ""), args=fc.get("args") or {}))
        finish = cand.get("finishReason")
        if not texts and not calls and finish not in (None, "STOP", "MAX_TOKENS"):
            raise ProviderError(f"Gemini produced no content (finishReason: {finish})", retryable=False)
        content.setdefault("role", "model")
        return LLMResponse(
            text="".join(texts),
            tool_calls=calls,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            stop_reason="max_tokens" if finish == "MAX_TOKENS" else finish,
            raw_assistant={"provider": self.name, "content": content},
        )
