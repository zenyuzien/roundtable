"""Deterministic offline provider for tests and key-free demos.

Model names select behaviour:
  mock        - immediate deterministic reply summarizing what it received
  mock-slow   - same, after a 1.5 s delay (useful to watch the round barrier)
  mock-tools  - first proposes a workspace entry via a tool call, then replies
  mock-fail   - always fails (non-retryable), to exercise failure handling
Tests may pass ``script`` to fully control responses.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import re

from .base import LLMResponse, Provider, ProviderError, ToolCall


def _est(text: str) -> int:
    return max(1, len(text) // 4)


class MockProvider(Provider):
    name = "mock"

    def __init__(self, script=None):
        self.script = script
        self.calls: list[dict] = []

    async def complete(self, *, model, system, messages, tools, max_tokens, temperature=None,
                       tool_choice_none=False) -> LLMResponse:
        call = {"model": model, "system": system, "messages": messages, "tools": [t.name for t in tools],
                "tool_choice_none": tool_choice_none}
        self.calls.append(call)
        if self.script is not None:
            r = self.script(**call)
            if inspect.isawaitable(r):
                r = await r
            if isinstance(r, str):
                r = LLMResponse(text=r)
            r.tokens_in = r.tokens_in or _est(system + json.dumps(messages, default=str))
            r.tokens_out = r.tokens_out or _est(r.text)
            return r
        if model == "mock-fail":
            raise ProviderError("mock-fail: simulated provider failure", retryable=False, status=500)
        if model == "mock-slow":
            await asyncio.sleep(1.5)
        first_user = messages[0]["content"] if messages else ""
        m = re.search(r"This is round (\d+)", first_user)
        rnd = int(m.group(1)) if m else 0
        if model == "mock-tools" and not tool_choice_none and len(messages) == 1 \
                and any(t.name == "workspace_propose" for t in tools):
            return LLMResponse(
                text="Recording a note in the workspace.",
                tool_calls=[ToolCall(id=f"call_{rnd}", name="workspace_propose", args={
                    "op": "append", "key": "mock-notes", "kind": "note", "title": "Mock notes",
                    "content": f"Mock agent note from round {rnd}.",
                })],
                tokens_in=_est(first_user), tokens_out=20,
            )
        n_outputs = first_user.count("### Output of ")
        text = (
            f"Mock response (model `{model}`) for round {rnd}.\n\n"
            f"I received {len(first_user)} characters of context containing {n_outputs} prior agent outputs.\n\n"
            "Example equation: $$E = \\tfrac12 \\sum_l \\lVert \\epsilon_l \\rVert^2$$"
        )
        if "```convergence" in system:
            text += "\n\n```convergence\n" + json.dumps({
                "support_termination": rnd >= 2, "open_objections": [], "unresolved_steps": [],
                "unsupported_claims": [], "rationale": "mock assessment",
            }) + "\n```"
        return LLMResponse(text=text, tokens_in=_est(system + first_user), tokens_out=_est(text))
