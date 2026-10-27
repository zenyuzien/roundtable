import json
import time

from fastapi.testclient import TestClient

from roundtable.api import create_app


def wait_idle(c, pid, timeout=15):
    t0 = time.time()
    while time.time() - t0 < timeout:
        s = c.get(f"/api/projects/{pid}/state").json()
        if not s["busy"]:
            return s
        time.sleep(0.05)
    raise AssertionError("project did not become idle")


def test_api_end_to_end(tmp_path):
    app = create_app(tmp_path, serve_frontend=False)
    with TestClient(app) as c:
        pid = c.get("/api/project").json()["id"]
        assert c.get("/api/project").json()["id"] == pid  # single project
        r = c.post(f"/api/projects/{pid}/setup", json={
            "task": "Demo task $x$", "settings": {"max_rounds": 4, "breakpoints": [2],
                                                 "convergence": {"enabled": True, "min_rounds": 3, "min_similarity": 0}},
            "agents": [{"name": "Explorer", "instructions": "i1", "provider": "mock", "model": "mock-slow"},
                       {"name": "Skeptic", "instructions": "i2", "provider": "mock", "model": "mock-tools"}]})
        assert r.status_code == 200, r.text
        s = c.get(f"/api/projects/{pid}/state").json()
        assert len(s["agents"]) == 2 and s["has_task"]
        aid = s["agents"][0]["id"]
        r = c.post(f"/api/projects/{pid}/messages", data={"channel": f"agent:{aid}", "content": "hi"},
                   files=[("files", ("notes.txt", b"hello", "text/plain"))])
        assert r.status_code == 200
        msgs = c.get(f"/api/projects/{pid}/messages").json()
        assert [m["status"] for m in msgs if m["kind"] == "user"] == ["pending"]
        # switch agents to the fast mock to keep the test quick
        for a in s["agents"]:
            c.patch(f"/api/agents/{a['id']}", json={"model": "mock"})
        assert c.post(f"/api/projects/{pid}/control/start").status_code == 200
        s = wait_idle(c, pid)
        assert s["project"]["state"] == "paused" and s["last_completed"] == 2  # breakpoint after round 2
        msgs = c.get(f"/api/projects/{pid}/messages").json()
        assert [m["status"] for m in msgs if m["kind"] == "user"] == ["processed"]
        run_id = s["rounds"][0]["runs"][0]["id"]
        detail = c.get(f"/api/runs/{run_id}").json()
        assert "Initial task" in detail["context"]["user"] and "hi" in detail["context"]["user"]
        assert c.post(f"/api/projects/{pid}/control/start").status_code == 200
        s = wait_idle(c, pid)
        assert s["project"]["state"] == "completed"
        conv = c.get(f"/api/projects/{pid}/convergence").json()
        assert conv["decisions"]
        # errors are reported, not crashes
        r = c.post(f"/api/projects/{pid}/control/bogus")
        assert r.status_code == 400
        exp = c.get(f"/api/projects/{pid}/export").json()
        assert exp["rounds"]
        # setup is locked after round 1; start over gives a fresh project
        assert c.post(f"/api/projects/{pid}/setup", json={"task": "x", "agents": [{"name": "a"}]}).status_code == 400
        assert c.delete(f"/api/projects/{pid}").status_code == 200
        fresh = c.get("/api/project").json()["id"]
        s = c.get(f"/api/projects/{fresh}/state").json()
        assert not s["agents"] and not s["has_task"] and not s["rounds"]


def test_secrets_never_returned(tmp_path):
    app = create_app(tmp_path, serve_frontend=False)
    with TestClient(app) as c:
        key = "sk-test-SUPERSECRET-0123456789"
        r = c.put("/api/secrets", json={"provider": "openai", "key": key})
        assert r.status_code == 200 and key not in r.text
        assert r.json()["openai"]["configured"]
        assert key not in c.get("/api/meta").text
        assert (tmp_path / "secrets.json").stat().st_mode & 0o077 == 0
        r = c.put("/api/secrets", json={"provider": "google", "key": key + "-g"})
        assert r.status_code == 200 and key not in r.text and r.json()["google"]["configured"]
        assert c.put("/api/secrets", json={"provider": "nope", "key": "x"}).status_code == 400
        pid = c.post("/api/projects", json={"name": "p"}).json()["id"]
        c.post(f"/api/projects/{pid}/agents", json={"name": "x", "provider": "openai", "model": "gpt-x"})
        c.put(f"/api/projects/{pid}/task", json={"content": "t"})
        # point the provider at a closed port so the call fails fast; error text must not contain the key
        app.state.engine.providers["openai"].base_url = "http://127.0.0.1:9"
        app.state.engine.retry_base_delay = 0.01
        c.patch(f"/api/projects/{pid}", json={"settings": {"failure_policy": "halt", "max_rounds": 1}})
        c.post(f"/api/projects/{pid}/control/start")
        wait_idle(c, pid, 60)
        dump = json.dumps(c.get(f"/api/projects/{pid}/export").json())
        assert key not in dump
        s = c.get(f"/api/projects/{pid}/state").json()
        assert s["project"]["state"] == "needs_attention"


def test_setup_with_judge(tmp_path):
    app = create_app(tmp_path, serve_frontend=False)
    with TestClient(app) as c:
        pid = c.get("/api/project").json()["id"]
        r = c.post(f"/api/projects/{pid}/setup", json={
            "task": "debate x", "settings": {"max_rounds": 2, "convergence": {"enabled": True, "min_rounds": 1}},
            "agents": [{"name": "Pro", "provider": "mock", "model": "mock"},
                       {"name": "Con", "provider": "mock", "model": "mock"},
                       {"name": "Judge", "provider": "mock", "model": "mock", "tools": [], "decider": True,
                        "synthesis": True}]})
        assert r.status_code == 200, r.text
        s = c.get(f"/api/projects/{pid}/state").json()
        judge = [a for a in s["agents"] if a["name"] == "Judge"][0]
        assert judge["tools"] == []
        assert s["project"]["settings"]["synthesis_agent"] == judge["id"]
        assert s["project"]["settings"]["convergence"]["decider_agent"] == judge["id"]
        c.post(f"/api/projects/{pid}/control/start")
        s = wait_idle(c, pid)
        kinds = [r["kind"] for r in s["rounds"]]
        assert kinds[-1] == "synthesis" and s["project"]["state"] == "completed", (kinds, s["project"])
