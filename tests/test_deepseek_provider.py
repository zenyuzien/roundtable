"""DeepSeek provider against a fake OpenAI-compatible endpoint - no network, no key needed."""
import json

import httpx
import pytest

from conftest import run
from roundtable import service
from roundtable.config import Pricing, Secrets
from roundtable.db import Store, jload
from roundtable.engine import Engine
from roundtable.providers import DeepSeekProvider, OpenAIProvider, ProviderError

KEY = "sk-deepseek-test-0123456789abcdef"


def fake(requests, responses):
    def handler(request: httpx.Request):
        requests.append(request)
        status, body = responses.pop(0)
        return httpx.Response(status, json=body)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def reply(message, prompt=50, completion=10, finish="stop"):
    return 200, {"choices": [{"message": message, "finish_reason": finish}],
                 "usage": {"prompt_tokens": prompt, "completion_tokens": completion}}


def test_deepseek_tool_loop_through_engine(tmp_path):
    reqs = []
    responses = [
        reply({"role": "assistant", "content": None, "reasoning_content": "THINKING-ABOUT-IT",
               "tool_calls": [{"id": "c1", "type": "function",
                               "function": {"name": "workspace_propose",
                                            "arguments": json.dumps({"op": "upsert", "key": "h1", "content": "DS-HYP"})}}]},
              finish="tool_calls"),
        reply({"role": "assistant", "content": "Final answer.", "reasoning_content": "MORE-THINKING"}),
    ]
    store = Store(tmp_path / "db.sqlite3")
    ds = DeepSeekProvider(lambda: KEY, client=fake(reqs, responses))
    engine = Engine(store, tmp_path, {"deepseek": ds}, Secrets(tmp_path), Pricing(tmp_path), retry_base_delay=0.01)
    pid = service.create_project(store, "d", {"max_rounds": 1})
    service.add_agent(store, pid, "D", "be brief", provider="deepseek", model="deepseek-chat", tools=["workspace"],
                      max_tokens=16384)
    service.set_task(store, pid, "task")

    async def go():
        await engine.runner(pid).control("start")
        await engine.runner(pid).wait()

    run(go())
    r = store.one("SELECT * FROM agent_runs WHERE project_id = ?", (pid,))
    assert r["status"] == "succeeded", r["error"]
    assert r["output"] == "Final answer."                      # reasoning is not part of the output
    assert "THINKING" not in r["output"] and "MORE-THINKING" not in r["output"]
    assert r["tokens_in"] == 100 and r["tokens_out"] == 20
    assert r["cost"] == pytest.approx(100 * 0.28 / 1e6 + 20 * 0.42 / 1e6)
    assert len(reqs) == 2
    for q in reqs:
        assert str(q.url) == "https://api.deepseek.com/chat/completions"
        assert q.headers["authorization"] == f"Bearer {KEY}" and KEY not in str(q.url)
    first, second = json.loads(reqs[0].content), json.loads(reqs[1].content)
    assert first["max_tokens"] == 8192 and "max_completion_tokens" not in first  # deepseek-chat output cap
    assert first["tools"][0]["type"] == "function"
    # the tool-call turn is sent back with its reasoning; the tool result follows
    assistant = second["messages"][2]
    assert assistant["reasoning_content"] == "THINKING-ABOUT-IT" and assistant["tool_calls"][0]["id"] == "c1"
    assert second["messages"][3]["role"] == "tool" and "Staged upsert" in second["messages"][3]["content"]
    assert store.val("SELECT COUNT(*) FROM ws_versions WHERE project_id = ?", (pid,)) == 1
    assert "DS-HYP" in json.dumps(jload(r["transcript"]))


def test_deepseek_reasoner_keeps_agent_max_tokens_and_errors_are_labelled(tmp_path):
    reqs = []
    ds = DeepSeekProvider(lambda: KEY, client=fake(reqs, [reply({"role": "assistant", "content": "ok"}),
                                                          (401, {"error": {"message": "Authentication Fails"}})]))
    msg = [{"role": "user", "content": "hi"}]

    async def go():
        await ds.complete(model="deepseek-reasoner", system="s", messages=msg, tools=[], max_tokens=16384)
        assert json.loads(reqs[0].content)["max_tokens"] == 16384
        with pytest.raises(ProviderError) as e:
            await ds.complete(model="deepseek-chat", system="s", messages=msg, tools=[], max_tokens=100)
        assert str(e.value).startswith("DeepSeek HTTP 401") and not e.value.retryable
        with pytest.raises(ProviderError, match="DeepSeek API key is not configured"):
            await DeepSeekProvider(lambda: None).complete(model="m", system="s", messages=msg, tools=[], max_tokens=5)

    run(go())


def test_openai_provider_unchanged_by_refactor(tmp_path):
    reqs = []
    oa = OpenAIProvider(lambda: KEY, client=fake(reqs, [reply({"role": "assistant", "content": "ok",
                                                              "reasoning_content": "x"})]))

    async def go():
        r = await oa.complete(model="gpt-x", system="s", messages=[{"role": "user", "content": "hi"}], tools=[],
                              max_tokens=123)
        body = json.loads(reqs[0].content)
        assert body["max_completion_tokens"] == 123 and "max_tokens" not in body
        assert str(reqs[0].url) == "https://api.openai.com/v1/chat/completions"
        assert "reasoning_content" not in r.raw_assistant["message"]

    run(go())


def test_deepseek_key_sources(tmp_path, monkeypatch):
    import roundtable.config as cfg
    monkeypatch.setattr(cfg, "BUILTIN_KEYS", {})
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    s = Secrets(tmp_path)
    assert s.status()["deepseek"] == {"configured": False, "source": None}
    monkeypatch.setenv("DEEPSEEK_API_KEY", "from-env")
    assert s.get("deepseek") == "from-env" and s.status()["deepseek"]["source"] == "env"
    s.set("deepseek", "from-file")
    assert s.get("deepseek") == "from-file" and s.status()["deepseek"]["source"] == "file"
