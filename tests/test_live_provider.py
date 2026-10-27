"""Optional smoke test against a real provider (costs a few cents).

Skipped unless RT_LIVE_TEST=1 and the provider key is set, e.g.:
    RT_LIVE_TEST=1 ANTHROPIC_API_KEY=... RT_LIVE_PROVIDER=anthropic RT_LIVE_MODEL=claude-haiku-5-5 pytest tests/test_live_provider.py
"""
import os

import pytest

from conftest import run
from roundtable import service
from roundtable.config import Pricing, Secrets
from roundtable.db import Store
from roundtable.engine import Engine
from roundtable.providers import build_providers

PROVIDER = os.environ.get("RT_LIVE_PROVIDER", "anthropic")
MODEL = os.environ.get("RT_LIVE_MODEL", {"anthropic": "claude-haiku-5-5", "openai": "gpt-4o-mini",
                                         "google": "gemini-2.5-flash"}.get(PROVIDER, ""))


@pytest.mark.skipif(os.environ.get("RT_LIVE_TEST") != "1", reason="set RT_LIVE_TEST=1 to run against a real API")
def test_two_agents_two_rounds_real_provider(tmp_path):
    secrets = Secrets(tmp_path)
    if not secrets.get(PROVIDER):
        pytest.skip(f"no {PROVIDER} key")
    store = Store(tmp_path / "db.sqlite3")
    engine = Engine(store, tmp_path, build_providers(secrets), secrets, Pricing(tmp_path))
    pid = service.create_project(store, "live", {"max_rounds": 2, "budget": {"max_total_tokens": 60000}})
    for n in ("A", "B"):
        service.add_agent(store, pid, n, "Answer in at most three sentences. Use the workspace tool once.",
                          provider=PROVIDER, model=MODEL, tools=["workspace"], max_tokens=4000)
    service.set_task(store, pid, "Is $\\sum_{n\\ge1} 1/n^2$ finite? Give its value.")

    async def go():
        r = engine.runner(pid)
        await r.control("start")
        await r.wait()

    run(go())
    rounds = store.q("SELECT status FROM rounds WHERE project_id = ?", (pid,))
    errors = store.q("SELECT error FROM agent_runs WHERE project_id = ? AND error IS NOT NULL", (pid,))
    assert [r["status"] for r in rounds] == ["completed", "completed"], errors
    assert store.val("SELECT SUM(tokens_in) FROM agent_runs WHERE project_id = ?", (pid,)) > 0
