from .base import LLMResponse, Provider, ProviderError, ToolCall, ToolSpec, with_retries  # noqa: F401
from .anthropic import AnthropicProvider
from .google import GoogleProvider
from .mock import MockProvider
from .openai import OpenAIProvider

PROVIDER_INFO = {
    "anthropic": {"label": "Anthropic", "suggested_models": ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5",
                                                            "claude-sonnet-4-5"]},
    "openai": {"label": "OpenAI", "suggested_models": ["gpt-5", "gpt-5-mini", "gpt-4.1", "gpt-4o"]},
    "google": {"label": "Google AI Studio (Gemini)",
               "suggested_models": ["gemini-3.5-flash-lite", "gemini-3.6-flash", "gemini-3.1-flash-lite",
                                    "gemini-flash-latest", "gemini-pro-latest"]},
    "mock": {"label": "Mock (offline)", "hidden": True, "suggested_models": ["mock", "mock-slow", "mock-tools", "mock-fail"]},
}


def build_providers(secrets) -> dict[str, Provider]:
    return {
        "anthropic": AnthropicProvider(lambda: secrets.get("anthropic")),
        "openai": OpenAIProvider(lambda: secrets.get("openai")),
        "google": GoogleProvider(lambda: secrets.get("google")),
        "mock": MockProvider(),
    }
