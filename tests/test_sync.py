"""Core synchronization protocol tests (deterministic, mock provider only)."""
import asyncio
import json
import time

import pytest

from conftest import round_of, run, who
from roundtable import service
from roundtable.db import jload
from roundtable.engine import EngineError
from roundtable.providers import LLMResponse, ProviderError, ToolCall


def ctx_of(h, pid, rnd, agent_id):
    r = [x for x in h.runs(pid, rnd) if x["agent_id"] == agent_id and not x["superseded"]][0]
    return jload(r["context"])


async def go(h, pid, action="start"):
    await h.runner(pid).control(action)
    await h.runner(pid).wait()


# 1 ---------------------------------------------------------------------------
def test_one_agent(make):
    h = make(lambda system, messages, **k: f"{who(system)} says hello in round {round_of(messages)}")
    pid, (a,) = h.project(1, {"max_rounds": 3})
    run(go(h, pid))
    rounds = h.rounds(pid)
    assert [r["number"] for r in rounds] == [1, 2, 3]
    assert all(r["status"] == "completed" for r in rounds)
    assert h.state(pid)["state"] == "completed"
    # round 3 sees its own round-1 and round-2 outputs, but not round 3
    u3 = ctx_of(h, pid, 3, a)["user"]
    assert "A1 says hello in round 1" in u3 and "A1 says hello in round 2" in u3
    assert "hello in round 3" not in u3
    assert len(h.mock.calls) == 3


# 2 ---------------------------------------------------------------------------
def test_three_agents(make):
    h = make(lambda system, messages, **k: f"{who(system)} r{round_of(messages)}")
    pid, ids = h.project(3, {"max_rounds": 2})
    run(go(h, pid))
    assert len(h.mock.calls) == 6
    outs = h.store.q("SELECT agent_id, round_number, content, status, visible_from_round FROM messages "
                     "WHERE project_id = ? AND kind = 'agent_output' ORDER BY round_number, id", (pid,))
    assert [(o["round_number"], o["content"]) for o in outs if o["round_number"] == 1] \
        and {o["content"] for o in outs} == {f"A{i} r{r}" for i in (1, 2, 3) for r in (1, 2)}
    assert all(o["status"] == "final" and o["visible_from_round"] == o["round_number"] + 1 for o in outs)
    # deterministic output ordering in the next round's context: by agent position
    u2 = ctx_of(h, pid, 2, ids[0])["user"]
    assert u2.index("A1 r1") < u2.index("A2 r1") < u2.index("A3 r1")


# 3 ---------------------------------------------------------------------------
def test_no_same_round_visibility(make):
    """A finishes instantly; B is slow and inspects shared state *after* A has finished."""
    seen = {}

    async def script(system, messages, tools, **k):
        name, rnd = who(system), round_of(messages)
        if name == "A1":
            if len(messages) == 1:
                return LLMResponse(text="", tool_calls=[
                    ToolCall("w1", "workspace_propose", {"op": "upsert", "key": "h1", "kind": "hypothesis",
                                                         "content": "SECRET-A-HYPOTHESIS"}),
                    ToolCall("f1", "write_file", {"path": "a.txt", "content": "SECRET-A-FILE"})])
            return f"A1 OUTPUT round {rnd} SECRET-A-TEXT"
        # B
        if len(messages) == 1:
            await asyncio.sleep(0.3)  # A is certainly done by now
            seen[rnd] = messages[0]["content"]
            return LLMResponse(text="", tool_calls=[ToolCall("r1", "workspace_read", {}),
                                                   ToolCall("r2", "read_file", {"path": "a.txt"}),
                                                   ToolCall("r3", "read_round", {"round": rnd})])
        seen[(rnd, "tools")] = messages[-1]["results"]
        return f"B output round {rnd}"

    h = make(script)
    pid, (a, b) = h.project(2, {"max_rounds": 2}, tools=["workspace", "files", "transcript"])
    run(go(h, pid))
    assert "SECRET-A" not in seen[1]
    tool_txt = json.dumps(seen[(1, "tools")])
    assert "SECRET-A-HYPOTHESIS" not in tool_txt and "SECRET-A-FILE" not in tool_txt
    assert "SECRET-A-TEXT" not in tool_txt
    # ... and everything is visible in round 2
    assert "SECRET-A-TEXT" in seen[2] and "SECRET-A-HYPOTHESIS" in seen[2]
    t2 = json.dumps(seen[(2, "tools")])
    assert "SECRET-A-FILE" in t2


# 4 ---------------------------------------------------------------------------
def test_round_barrier(make):
    spans = []
    delays = {"A1": 0.05, "A2": 0.25, "A3": 0.12}

    async def script(system, messages, **k):
        name, rnd = who(system), round_of(messages)
        start = time.monotonic()
        await asyncio.sleep(delays[name] if rnd % 2 else delays[name] / 2)
        spans.append((rnd, name, start, time.monotonic()))
        return f"{name} {rnd}"

    h = make(script)
    pid, _ = h.project(3, {"max_rounds": 4})
    run(go(h, pid))
    for t in (1, 2, 3):
        end_t = max(e for r, _, s, e in spans if r == t)
        start_next = min(s for r, _, s, e in spans if r == t + 1)
        assert start_next >= end_t, f"round {t + 1} began before round {t} finished"
    # an early finisher never got extra iterations
    for name in delays:
        assert sorted(r for r, n, *_ in spans if n == name) == [1, 2, 3, 4]
    # agents within a round really ran concurrently
    r1 = [(s, e) for r, _, s, e in spans if r == 1]
    assert max(s for s, _ in r1) < min(e for _, e in r1)


# 5 ---------------------------------------------------------------------------
def test_mid_round_message_waits_for_boundary(make):
    async def script(system, messages, **k):
        await asyncio.sleep(0.2)
        return f"{who(system)} {round_of(messages)}"

    h = make(script)
    pid, (a, b) = h.project(2, {"max_rounds": 2})

    async def scenario():
        await h.runner(pid).control("start")
        await asyncio.sleep(0.05)  # round 1 is running
        mid = service.post_message(h.store, pid, "common", "MID-ROUND COMMON NOTE")
        m = h.store.one("SELECT * FROM messages WHERE id = ?", (mid,))
        assert m["status"] == "pending" and m["visible_from_round"] is None and m["submitted_during_round"] == 1
        await h.runner(pid).wait()
        return mid

    mid = run(scenario())
    for ag in (a, b):
        assert "MID-ROUND COMMON NOTE" not in ctx_of(h, pid, 1, ag)["user"]
        assert "MID-ROUND COMMON NOTE" in ctx_of(h, pid, 2, ag)["user"]
    m = h.store.one("SELECT * FROM messages WHERE id = ?", (mid,))
    assert m["visible_from_round"] == 2 and m["status"] == "processed"


# 6 ---------------------------------------------------------------------------
def test_private_channel_privacy(make):
    seen = {}

    def script(system, messages, tools, **k):
        name, rnd = who(system), round_of(messages)
        if name == "A2" and rnd == 2 and len(messages) == 1:
            return LLMResponse(text="", tool_calls=[ToolCall("x", "read_round", {"round": 1}),
                                                   ToolCall("y", "list_files", {})])
        if len(messages) > 1:
            seen["tools"] = json.dumps(messages[-1]["results"])
        return f"{name} {rnd}"

    h = make(script)
    pid, (a, b) = h.project(2, {"max_rounds": 2}, tools=["transcript", "files"])
    fid = service.save_upload(h.store, h.tmp, pid, f"agent:{a}", "secret_notes.txt", b"PRIVATE FILE BODY")
    service.post_message(h.store, pid, f"agent:{a}", "PRIVATE-FOR-A1", [fid])
    run(go(h, pid))
    assert "PRIVATE-FOR-A1" in ctx_of(h, pid, 1, a)["user"]
    assert "secret_notes" in ctx_of(h, pid, 1, a)["user"]
    for r in (1, 2):
        assert "PRIVATE-FOR-A1" not in ctx_of(h, pid, r, b)["user"]
        assert "secret_notes" not in ctx_of(h, pid, r, b)["user"]
    assert "PRIVATE-FOR-A1" not in seen["tools"] and "secret_notes" not in seen["tools"]


# 7 + 8 -----------------------------------------------------------------------
def test_breakpoints_and_resume(make):
    h = make(lambda system, messages, **k: f"{who(system)} {round_of(messages)}")
    pid, _ = h.project(2, {"max_rounds": 5, "breakpoints": [2, 4]})
    run(go(h, pid))
    assert [r["number"] for r in h.rounds(pid)] == [1, 2]
    st = h.state(pid)
    assert st["state"] == "paused" and "breakpoint after round 2" in st["state_reason"]
    # intervention during the pause is delivered to round 3
    service.post_message(h.store, pid, "common", "BREAKPOINT NOTE")
    run(go(h, pid, "resume"))
    assert [r["number"] for r in h.rounds(pid)] == [1, 2, 3, 4]
    assert h.state(pid)["state"] == "paused"
    assert "BREAKPOINT NOTE" in ctx_of(h, pid, 3, h.runs(pid, 3)[0]["agent_id"])["user"]
    run(go(h, pid, "resume"))
    assert [r["number"] for r in h.rounds(pid)] == [1, 2, 3, 4, 5]
    assert h.state(pid)["state"] == "completed"


def test_pause_request_finishes_current_round(make):
    async def script(system, messages, **k):
        await asyncio.sleep(0.15)
        return "x"

    h = make(script)
    pid, _ = h.project(2, {"max_rounds": 10})

    async def scenario():
        await h.runner(pid).control("start")
        await asyncio.sleep(0.05)
        await h.runner(pid).control("pause")
        assert h.state(pid)["state"] == "pausing"
        await h.runner(pid).wait()

    run(scenario())
    assert [r["status"] for r in h.rounds(pid)] == ["completed"]
    assert h.state(pid)["state"] == "paused"
    run(go(h, pid, "step"))
    assert len(h.rounds(pid)) == 2 and h.state(pid)["state"] == "paused"


# 9 ---------------------------------------------------------------------------
def test_persistence_across_restart(make):
    h = make(lambda system, messages, **k: f"{who(system)} {round_of(messages)}")
    pid, ids = h.project(2, {"max_rounds": 4, "breakpoints": [2]})
    run(go(h, pid))
    h.reopen()
    assert [r["number"] for r in h.rounds(pid)] == [1, 2]
    assert h.state(pid)["state"] == "paused"
    assert h.store.val("SELECT COUNT(*) FROM messages WHERE project_id = ? AND kind = 'agent_output'", (pid,)) == 4
    run(go(h, pid, "resume"))
    assert [r["number"] for r in h.rounds(pid)] == [1, 2, 3, 4]
    assert "A1 2" in ctx_of(h, pid, 3, ids[0])["user"]


def test_interrupted_round_requires_explicit_retry(make):
    """Simulate a crash mid-round: only the unfinished agent is re-run, explicitly."""
    calls = []

    async def script(system, messages, **k):
        name = who(system)
        calls.append((name, round_of(messages)))
        if name == "A2" and len([c for c in calls if c[0] == "A2"]) == 1:
            await asyncio.sleep(30)  # still running when the "crash" happens
        return f"{name} done"

    h = make(script)
    pid, _ = h.project(2, {"max_rounds": 1})

    async def crash():
        await h.runner(pid).control("start")
        await asyncio.sleep(0.2)
        # hard crash: tasks die without the engine's cleanup running
        for t in list(h.runner(pid).agent_tasks.values()):
            t.cancel()
        h.runner(pid).loop_task.cancel()
        await asyncio.sleep(0)

    run(crash())
    # emulate the process dying with state left as "running" in the database
    h.store.ex("UPDATE rounds SET status = 'running' WHERE project_id = ?", (pid,))
    h.store.ex("UPDATE agent_runs SET status = 'running' WHERE project_id = ? AND status != 'succeeded'", (pid,))
    h.store.ex("UPDATE projects SET state = 'running' WHERE id = ?", (pid,))
    h.reopen()
    assert h.rounds(pid)[0]["status"] == "interrupted"
    assert h.state(pid)["state"] == "needs_attention"

    async def try_start():
        with pytest.raises(EngineError):
            await h.runner(pid).control("start")
    run(try_start())
    n_before = len(calls)
    run(go(h, pid, "retry_failed"))
    assert calls[n_before:] == [("A2", 1)]  # A1 was not re-run
    assert h.rounds(pid)[0]["status"] == "completed"
    assert len(h.rounds(pid)) == 1


# 10 --------------------------------------------------------------------------
def test_api_failure_does_not_corrupt_state(make):
    fail = {"on": True}

    def script(system, messages, tools, **k):
        name = who(system)
        if name == "A2" and fail["on"]:
            raise ProviderError("boom 500", retryable=False)
        if name == "A1" and len(messages) == 1:
            return LLMResponse(text="", tool_calls=[ToolCall("p", "workspace_propose", {
                "op": "append", "key": "notes", "kind": "note", "content": "A1 note"})])
        return f"{name} ok"

    h = make(script)
    pid, (a, b) = h.project(2, {"max_rounds": 2, "failure_policy": "halt"}, tools=["workspace"])
    run(go(h, pid))
    assert h.rounds(pid)[0]["status"] == "failed"
    assert h.state(pid)["state"] == "needs_attention"
    # nothing from the failed round leaked into shared state
    assert h.store.val("SELECT COUNT(*) FROM ws_versions WHERE project_id = ?", (pid,)) == 0
    assert h.store.val("SELECT COUNT(*) FROM messages WHERE project_id = ? AND visible_from_round IS NOT NULL AND "
                       "kind = 'agent_output'", (pid,)) == 0
    err = [r for r in h.runs(pid) if r["agent_id"] == b][0]
    assert err["status"] == "failed" and "boom" in err["error"]
    a1_calls = len([c for c in h.mock.calls if who(c["system"]) == "A1"])
    fail["on"] = False
    run(go(h, pid, "retry_failed"))
    assert len([c for c in h.mock.calls if who(c["system"]) == "A1"]) == a1_calls  # A1 not re-run
    assert h.rounds(pid)[0]["status"] == "completed"
    assert h.store.val("SELECT COUNT(*) FROM ws_versions WHERE project_id = ?", (pid,)) == 1
    # the failed attempt is preserved, not erased
    b_runs = [r for r in h.runs(pid, 1) if r["agent_id"] == b]
    assert [(r["status"], r["superseded"]) for r in b_runs] == [("failed", 1), ("succeeded", 0)]
    run(go(h, pid, "resume"))
    assert h.state(pid)["state"] == "completed"


def test_auto_retry_and_skip_policy(make):
    n = {"A2": 0}

    def script(system, messages, **k):
        name = who(system)
        if name == "A2":
            n["A2"] += 1
            if n["A2"] == 1:
                raise ProviderError("transient", retryable=False)
        if name == "A3":
            raise ProviderError("always fails", retryable=False)
        return f"{name} ok"

    h = make(script)
    pid, (a, b, c) = h.project(3, {"max_rounds": 1, "failure_policy": "retry_then_halt", "auto_retries": 1})
    run(go(h, pid))
    assert h.state(pid)["state"] == "needs_attention"  # A3 still failing; A2 recovered by auto retry
    statuses = {r["agent_id"]: r["status"] for r in h.runs(pid, 1) if not r["superseded"]}
    assert statuses == {a: "succeeded", b: "succeeded", c: "failed"}
    run(go(h, pid, "skip_failed"))
    assert h.rounds(pid)[0]["status"] == "completed"
    statuses = {r["agent_id"]: r["status"] for r in h.runs(pid, 1) if not r["superseded"]}
    assert statuses[c] == "skipped"


def test_provider_retries_rate_limits(make):
    n = {"k": 0}

    def script(system, messages, **k):
        n["k"] += 1
        if n["k"] <= 2:
            raise ProviderError("429 rate limited", retryable=True, status=429)
        return "fine"

    h = make(script)
    pid, _ = h.project(1, {"max_rounds": 1})
    run(go(h, pid))
    assert h.rounds(pid)[0]["status"] == "completed"
    assert h.store.val("SELECT COUNT(*) FROM events WHERE project_id = ? AND type = 'provider_retry'", (pid,)) == 2


# 11 --------------------------------------------------------------------------
def test_consistent_snapshots(make):
    async def script(system, messages, **k):
        await asyncio.sleep(0.05 * (1 + hash(who(system)) % 3))
        return f"{who(system)} {round_of(messages)}"

    h = make(script)
    pid, ids = h.project(3, {"max_rounds": 3})

    async def scenario():
        await h.runner(pid).control("start")
        for i in range(6):
            await asyncio.sleep(0.04)
            service.post_message(h.store, pid, "common", f"note {i}")
        await h.runner(pid).wait()

    run(scenario())
    for r in (1, 2, 3):
        snaps = [ctx_of(h, pid, r, a)["snapshot"] for a in ids]
        assert all(s == snaps[0] for s in snaps), f"inconsistent snapshot in round {r}"
    # every note was delivered exactly once
    notes = h.store.q("SELECT status, visible_from_round FROM messages WHERE project_id = ? AND kind = 'user'", (pid,))
    assert len(notes) == 6 and all(n["visible_from_round"] is not None for n in notes if n["status"] != "pending")


# 12 --------------------------------------------------------------------------
def test_instruction_change_applies_at_boundary(make):
    async def script(system, messages, **k):
        await asyncio.sleep(0.2)
        return "x"

    h = make(script)
    pid, (a,) = h.project(1, {"max_rounds": 2})

    async def scenario():
        await h.runner(pid).control("start")
        await asyncio.sleep(0.05)
        v = service.set_instructions(h.store, a, "instructions for A1 v2")
        service.update_agent(h.store, a, {"max_tokens": 1234})
        assert v == 2
        await h.runner(pid).wait()

    run(scenario())
    assert "A1 v1" in ctx_of(h, pid, 1, a)["system"] and "A1 v2" not in ctx_of(h, pid, 1, a)["system"]
    assert "A1 v2" in ctx_of(h, pid, 2, a)["system"]
    cfgs = [jload(r["agent_config"]) for r in h.runs(pid)]
    assert [c["instruction_version"] for c in cfgs] == [1, 2]
    assert [c["max_tokens"] for c in cfgs] == [4096, 1234]
    iv = h.store.one("SELECT created_during_round FROM instruction_versions WHERE agent_id = ? AND version = 2", (a,))
    assert iv["created_during_round"] == 1


def test_disabling_agent_mid_round_keeps_barrier(make):
    async def script(system, messages, **k):
        await asyncio.sleep(0.15)
        return who(system)

    h = make(script)
    pid, (a, b) = h.project(2, {"max_rounds": 2})

    async def scenario():
        await h.runner(pid).control("start")
        await asyncio.sleep(0.03)
        service.update_agent(h.store, b, {"enabled": False})
        await h.runner(pid).wait()

    run(scenario())
    assert sorted(r["agent_id"] for r in h.runs(pid, 1)) == [a, b]  # B finished its frozen round-1 membership
    assert [r["agent_id"] for r in h.runs(pid, 2)] == [a]


def test_stop_mid_round_preserves_completed_rounds(make):
    async def script(system, messages, **k):
        await asyncio.sleep(0.05 if round_of(messages) == 1 else 5)
        return "x"

    h = make(script)
    pid, _ = h.project(2, {"max_rounds": 5})

    async def scenario():
        await h.runner(pid).control("start")
        await asyncio.sleep(0.4)
        await h.runner(pid).control("stop")
        await h.runner(pid).wait()

    run(scenario())
    rs = h.rounds(pid)
    assert [(r["number"], r["status"]) for r in rs] == [(1, "completed"), (2, "aborted")]
    assert h.state(pid)["state"] == "stopped"
    assert all(r["status"] == "aborted" for r in h.runs(pid, 2))
    # discarding the partial round re-runs it from a fresh snapshot, explicitly
    run(go(h, pid, "discard_round"))
    assert [(r["number"], r["status"]) for r in h.rounds(pid)] == [(1, "completed"), (2, "discarded")]


def test_duplicate_round_rejected_by_database(make):
    h = make(lambda **k: "x")
    pid, _ = h.project(1, {"max_rounds": 1})
    run(go(h, pid))
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        h.store.ex("INSERT INTO rounds(project_id, number, status, members, started_at) VALUES (?, 1, 'running', "
                   "'[]', 'now')", (pid,))
