"""DeepSeek API (OpenAI-compatible Chat Completions). ``DEEPSEEK_BASE_URL`` overrides the endpoint."""
from __future__ import annotations

from .openai import OpenAIProvider

# deepseek-chat accepts at most 8192 output tokens; other models use the agent's setting
OUTPUT_LIMITS = {"deepseek-chat": 8192}


class DeepSeekProvider(OpenAIProvider):
    name = "deepseek"
    label = "DeepSeek"
    base_url_env = "DEEPSEEK_BASE_URL"
    default_base_url = "https://api.deepseek.com"
    max_tokens_param = "max_tokens"

    def _max_tokens(self, model: str, max_tokens: int) -> int:
        limit = OUTPUT_LIMITS.get(model)
        return min(max_tokens, limit) if limit else max_tokens

    def _raw_message(self, msg: dict) -> dict:
        raw = super()._raw_message(msg)
        # Reasoning models must get their reasoning back while a tool-use turn is in progress;
        # it is never part of the agent's visible output.
        if msg.get("tool_calls") and msg.get("reasoning_content"):
            raw["reasoning_content"] = msg["reasoning_content"]
        return raw
