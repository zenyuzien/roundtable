"""Judge-moderated debate: a designated decider stops the run; a final synthesis turn runs under the protocol."""
import asyncio
import json

from conftest import round_of, run, who
from roundtable import service
from roundtable.db import jload


def block(support):
    return "\n\n```convergence\n" + json.dumps({"support_termination": support, "open_objections": ["x"],
                                                "unresolved_steps": [], "unsupported_claims": []}) + "\n```"


async def go(h, pid, action="start"):
    await h.runner(pid).control(action)
    await h.runner(pid).wait()


def judge_script(stop_at):
    def script(system, messages, **k):
        name, user = who(system), messages[0]["content"]
        if "# Final synthesis" in user:
            return f"SYNTHESIS by {name}: cruxes listed."
        rnd = round_of(messages)
        if name == "Judge":
            return f"Judge notes round {rnd}." + block(rnd >= stop_at)
        return f"{name} argues in round {rnd}."  # debaters never support stopping
    return script


def setup_debate(h, settings):
    pid, (a, b, j) = h.project(3, settings, names=["Pro", "Con", "Judge"])
    service.update_settings(h.store, pid, {"synthesis_agent": j, "convergence": {"decider_agent": j}})
    return pid, a, b, j


def test_judge_decides_termination_and_writes_synthesis(make):
    h = make(judge_script(stop_at=2))
    pid, a, b, j = setup_debate(h, {"max_rounds": 6, "convergence": {"enabled": True, "min_rounds": 1}})
    run(go(h, pid))
    rounds = h.rounds(pid)
    assert [(r["number"], r["kind"], r["status"]) for r in rounds] == [
        (1, "debate", "completed"), (2, "debate", "completed"), (3, "synthesis", "completed")]
    assert jload(rounds[2]["members"]) == [j]  # only the judge takes part in the synthesis turn
    st = h.state(pid)
    assert st["state"] == "completed" and "converged after round 2" in st["state_reason"]
    assert "final synthesis by Judge" in st["state_reason"]
    # only the decider is asked for a convergence block; its view alone decided
    ctxs = {r["agent_id"]: jload(r["context"]) for r in h.runs(pid, 1)}
    assert "```convergence" in ctxs[j]["system"] and "```convergence" not in ctxs[a]["system"]
    d = jload(h.store.q("SELECT decision FROM convergence_decisions WHERE project_id = ? ORDER BY round_number",
                        (pid,))[-1]["decision"])
    assert d["converged"] and d["decider"] == j
    # the synthesis sees every round verbatim, and is told it is the final synthesis
    syn = jload(h.runs(pid, 3)[0]["context"])["user"]
    assert "Pro argues in round 1." in syn and "Con argues in round 2." in syn and "# Final synthesis" in syn
    out = h.store.one("SELECT content, status FROM messages WHERE project_id = ? AND round_number = 3", (pid,))
    assert out["content"].startswith("SYNTHESIS by Judge") and out["status"] == "final"


def test_synthesis_after_max_rounds_does_not_count_as_a_round(make):
    h = make(judge_script(stop_at=99))
    pid, a, b, j = setup_debate(h, {"max_rounds": 2, "convergence": {"enabled": True, "min_rounds": 1}})
    run(go(h, pid))
    assert [r["kind"] for r in h.rounds(pid)] == ["debate", "debate", "synthesis"]
    assert "maximum rounds reached (2)" in h.state(pid)["state_reason"]
    # resuming does not re-run anything or write a second synthesis
    run(go(h, pid, "resume"))
    assert len(h.rounds(pid)) == 3
    # raising the limit continues the debate, then synthesizes again
    service.update_settings(h.store, pid, {"max_rounds": 3})
    run(go(h, pid, "resume"))
    assert [r["kind"] for r in h.rounds(pid)] == ["debate", "debate", "synthesis", "debate", "synthesis"]


def test_absent_decider_falls_back_to_all_agents(make):
    h = make(judge_script(stop_at=1))
    pid, a, b, j = setup_debate(h, {"max_rounds": 2, "convergence": {"enabled": True, "min_rounds": 1}})
    service.update_agent(h.store, j, {"enabled": False})
    run(go(h, pid))
    # judge disabled -> all-agents policy; debaters never support stopping -> runs to max rounds, no synthesis
    assert [r["kind"] for r in h.rounds(pid)] == ["debate", "debate"]
    assert h.state(pid)["state"] == "completed"


def test_failed_synthesis_can_be_retried(make):
    fail = {"on": True}

    def script(system, messages, **k):
        if "# Final synthesis" in messages[0]["content"] and fail["on"]:
            from roundtable.providers import ProviderError
            raise ProviderError("boom", retryable=False)
        return judge_script(stop_at=1)(system=system, messages=messages)

    h = make(script)
    pid, a, b, j = setup_debate(h, {"max_rounds": 3, "failure_policy": "halt",
                                    "convergence": {"enabled": True, "min_rounds": 1}})
    run(go(h, pid))
    assert h.state(pid)["state"] == "needs_attention"
    fail["on"] = False
    run(go(h, pid, "retry_failed"))
    assert [(r["kind"], r["status"]) for r in h.rounds(pid)] == [("debate", "completed"), ("synthesis", "completed")]
    assert "final synthesis written" in h.state(pid)["state_reason"]


def test_preamble_asks_for_substance_in_response(make):
    h = make(lambda **k: "x")
    pid, _ = h.project(1, {"max_rounds": 1})
    run(go(h, pid))
    system = jload(h.runs(pid, 1)[0]["context"])["system"]
    assert "put the substance of your contribution in the\n  response itself" in system
    assert "asked you a question, answer it" in system
