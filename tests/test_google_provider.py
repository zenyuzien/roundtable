"""Gemini (Google AI Studio) provider against a fake HTTP endpoint - no network, no key needed."""
import json

import httpx
import pytest

from conftest import run
from roundtable import service
from roundtable.config import Pricing, Secrets
from roundtable.db import Store, jload
from roundtable.engine import Engine
from roundtable.providers import GoogleProvider, ProviderError, ToolSpec

KEY = "AIza-test-KEY-0123456789abcdef"


def fake_gemini(requests, responses):
    def handler(request: httpx.Request):
        requests.append(request)
        status, body = responses.pop(0)
        return httpx.Response(status, json=body, headers={"retry-after": "0"} if status == 429 else {})
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def ok(parts, prompt=100, out=20, thoughts=0, finish="STOP"):
    return 200, {"candidates": [{"content": {"role": "model", "parts": parts}, "finishReason": finish}],
                 "usageMetadata": {"promptTokenCount": prompt, "candidatesTokenCount": out,
                                   "thoughtsTokenCount": thoughts}}


def test_gemini_tool_loop_through_engine(tmp_path):
    reqs = []
    responses = [
        (429, {"error": {"message": "quota"}}),  # retried transparently
        ok([{"functionCall": {"name": "workspace_propose", "args": {"op": "upsert", "key": "h1", "kind": "hypothesis",
                                                                    "content": "GEMINI-HYP"}},
             "thoughtSignature": "SIG-123"}], thoughts=7),
        ok([{"text": "thinking...", "thought": True}, {"text": "Final answer with $x^2$."}]),
    ]
    store = Store(tmp_path / "db.sqlite3")
    google = GoogleProvider(lambda: KEY, client=fake_gemini(reqs, responses))
    engine = Engine(store, tmp_path, {"google": google}, Secrets(tmp_path), Pricing(tmp_path), retry_base_delay=0.01)
    pid = service.create_project(store, "g", {"max_rounds": 1})
    service.add_agent(store, pid, "G", "be brief", provider="google", model="gemini-2.5-flash",
                      tools=["workspace", "transcript"])
    service.set_task(store, pid, "task")

    async def go():
        await engine.runner(pid).control("start")
        await engine.runner(pid).wait()

    run(go())
    run_row = store.one("SELECT * FROM agent_runs WHERE project_id = ?", (pid,))
    assert run_row["status"] == "succeeded", run_row["error"]
    assert run_row["output"] == "Final answer with $x^2$."  # thought parts excluded
    assert run_row["tokens_in"] == 200 and run_row["tokens_out"] == 47  # thinking tokens count as output
    assert run_row["cost"] == pytest.approx(200 * 0.3 / 1e6 + 47 * 2.5 / 1e6)

    assert len(reqs) == 3
    for r in reqs:
        assert r.headers["x-goog-api-key"] == KEY
        assert KEY not in str(r.url)
        assert r.url.path.endswith("/models/gemini-2.5-flash:generateContent")
    first = json.loads(reqs[1].content)
    assert "be brief" in first["systemInstruction"]["parts"][0]["text"]
    decls = {d["name"]: d for d in first["tools"][0]["functionDeclarations"]}
    assert "parameters" not in decls["list_sources"]  # empty OBJECT schemas are omitted
    assert decls["workspace_propose"]["parameters"]["required"] == ["op", "key", "content"]
    second = json.loads(reqs[2].content)
    model_turn, tool_turn = second["contents"][1], second["contents"][2]
    assert model_turn["role"] == "model" and model_turn["parts"][0]["thoughtSignature"] == "SIG-123"
    fr = tool_turn["parts"][0]["functionResponse"]
    assert fr["name"] == "workspace_propose" and "Staged upsert" in fr["response"]["result"]
    assert "GEMINI-HYP" in json.dumps(jload(run_row["transcript"]))
    assert store.val("SELECT COUNT(*) FROM ws_versions WHERE project_id = ?", (pid,)) == 1


def test_gemini_tool_choice_none_and_errors(tmp_path):
    reqs = []
    responses = [
        ok([{"text": "done"}]),
        (200, {"promptFeedback": {"blockReason": "SAFETY"}}),
        (400, {"error": {"message": "API key not valid"}}),
    ]
    g = GoogleProvider(lambda: KEY, client=fake_gemini(reqs, responses))
    spec = [ToolSpec("t", "d", {"type": "object", "properties": {"a": {"type": "string"}}})]

    async def go():
        r = await g.complete(model="gemini-2.5-pro", system="s", messages=[{"role": "user", "content": "hi"}],
                             tools=spec, max_tokens=50, temperature=0.2, tool_choice_none=True)
        assert r.text == "done"
        body = json.loads(reqs[0].content)
        assert body["toolConfig"] == {"functionCallingConfig": {"mode": "NONE"}}
        assert body["generationConfig"] == {"maxOutputTokens": 50, "temperature": 0.2}
        with pytest.raises(ProviderError, match="blocked: SAFETY"):
            await g.complete(model="x", system="s", messages=[{"role": "user", "content": "hi"}], tools=[],
                             max_tokens=5)
        with pytest.raises(ProviderError) as e:
            await g.complete(model="x", system="s", messages=[{"role": "user", "content": "hi"}], tools=[],
                             max_tokens=5)
        assert not e.value.retryable and e.value.status == 400

    run(go())


def test_gemini_key_from_env_and_missing_key(tmp_path, monkeypatch):
    import roundtable.config as cfg
    monkeypatch.setattr(cfg, "BUILTIN_KEYS", {})
    s = Secrets(tmp_path)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    assert s.status()["google"] == {"configured": False, "source": None}
    monkeypatch.setattr(cfg, "BUILTIN_KEYS", {"google": "builtin-key"})
    assert s.get("google") == "builtin-key" and s.status()["google"]["source"] == "builtin"
    monkeypatch.setenv("GOOGLE_API_KEY", "from-google-env")
    assert s.get("google") == "from-google-env" and s.status()["google"]["source"] == "env"
    monkeypatch.setenv("GEMINI_API_KEY", "from-gemini-env")
    assert s.get("google") == "from-gemini-env"  # GEMINI_API_KEY takes precedence
    s.set("google", "from-file")
    assert s.get("google") == "from-file" and s.status()["google"]["source"] == "file"

    async def go():
        with pytest.raises(ProviderError, match="not configured"):
            await GoogleProvider(lambda: None).complete(model="m", system="s", messages=[], tools=[], max_tokens=5)
    run(go())


def test_gemini_daily_quota_is_not_retried_but_minute_limit_is(tmp_path):
    daily = (429, {"error": {"message": "quota", "details": [
        {"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [{
            "quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier", "quotaValue": "20",
            "quotaDimensions": {"model": "gemini-3.6-flash"}}]},
        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "38034s"}]}})
    minute = (429, {"error": {"message": "slow down", "details": [
        {"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [{
            "quotaId": "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"}]},
        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "7s"}]}})
    reqs = []
    g = GoogleProvider(lambda: KEY, client=fake_gemini(reqs, [daily, minute]))
    msg = [{"role": "user", "content": "hi"}]

    async def go():
        with pytest.raises(ProviderError) as e:
            await g.complete(model="gemini-3.6-flash", system="s", messages=msg, tools=[], max_tokens=5)
        assert not e.value.retryable and "daily free-tier quota" in str(e.value) and "10.6 h" in str(e.value)
        with pytest.raises(ProviderError) as e:
            await g.complete(model="gemini-3.6-flash", system="s", messages=msg, tools=[], max_tokens=5)
        assert e.value.retryable and e.value.retry_after == 7.0

    run(go())
