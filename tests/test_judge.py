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


def review_script(log):
    async def script(system, messages, **k):
        name, user = who(system), messages[0]["content"]
        if "# Final synthesis" in user:
            return "SYNTH"
        rnd = round_of(messages)
        log.append((name, rnd, user))
        if name == "Judge":
            await asyncio.sleep(0)
            return f"JUDGE-REVIEW-{rnd}: question for Pro." + block(rnd >= 2)
        await asyncio.sleep(0.05)
        return f"{name}-ARG-{rnd}"
    return script


def test_review_phase_judge_sees_same_round_and_debaters_see_review_next_round(make):
    log = []
    h = make(review_script(log))
    pid, (a, b, j) = h.project(3, {"max_rounds": 5, "convergence": {"enabled": True, "min_rounds": 1}},
                               names=["Pro", "Con", "Judge"])
    service.update_settings(h.store, pid, {"review_agents": [j], "synthesis_agent": j,
                                           "convergence": {"decider_agent": j}})
    run(go(h, pid))
    seen = {(n, r): u for n, r, u in log}
    # judge in round 1 reviews round-1 contributions (no lag)
    assert "Pro-ARG-1" in seen[("Judge", 1)] and "Con-ARG-1" in seen[("Judge", 1)]
    assert "This round's contributions (round 1)" in seen[("Judge", 1)]
    # debaters never see same-round output; they see the judge's round-1 review in round 2
    assert "Con-ARG-1" not in seen[("Pro", 1)]
    assert "JUDGE-REVIEW-1" in seen[("Pro", 2)] and "JUDGE-REVIEW-2" not in seen[("Pro", 2)]
    # phase ordering: every judge call of round t comes after both debaters of round t
    order = [(n, r) for n, r, _ in log]
    for r in (1, 2):
        assert order.index(("Judge", r)) > max(order.index(("Pro", r)), order.index(("Con", r)))
    # judge (decider) supported stopping in round 2 -> synthesis, run ends
    assert [(x["number"], x["kind"]) for x in h.rounds(pid)] == [(1, "debate"), (2, "debate"), (3, "synthesis")]
    runs1 = h.runs(pid, 1)
    assert jload([r for r in runs1 if r["agent_id"] == j][0]["agent_config"])["phase"] == 2
    assert "second phase" in jload([r for r in runs1 if r["agent_id"] == j][0]["context"])["system"]
    assert "reviews each round after the others" in jload([r for r in runs1 if r["agent_id"] == a][0]["context"])["system"]


def test_review_phase_failure_and_retry_keeps_frozen_review_context(make):
    fail = {"on": True}
    log = []
    inner = review_script(log)

    async def script(system, messages, **k):
        if who(system) == "Judge" and fail["on"]:
            from roundtable.providers import ProviderError
            raise ProviderError("judge down", retryable=False)
        return await inner(system=system, messages=messages)

    h = make(script)
    pid, (a, b, j) = h.project(3, {"max_rounds": 1, "failure_policy": "halt"}, names=["Pro", "Con", "Judge"])
    service.update_settings(h.store, pid, {"review_agents": [j]})
    run(go(h, pid))
    assert h.rounds(pid)[0]["status"] == "failed" and h.state(pid)["state"] == "needs_attention"
    # debaters' work is not lost or re-run: only the judge is retried
    n_debater_calls = len([1 for n, _, _ in log if n != "Judge"])
    fail["on"] = False
    run(go(h, pid, "retry_failed"))
    assert len([1 for n, _, _ in log if n != "Judge"]) == n_debater_calls
    assert h.rounds(pid)[0]["status"] == "completed"
    judge_user = [u for n, _, u in log if n == "Judge"][0]
    assert "Pro-ARG-1" in judge_user and "Con-ARG-1" in judge_user


def test_review_phase_skip_policy_completes_round(make):
    def script(system, messages, **k):
        if who(system) == "Judge":
            from roundtable.providers import ProviderError
            raise ProviderError("down", retryable=False)
        return "arg"

    h = make(script)
    pid, (a, b, j) = h.project(3, {"max_rounds": 1, "failure_policy": "skip_failed"}, names=["Pro", "Con", "Judge"])
    service.update_settings(h.store, pid, {"review_agents": [j]})
    run(go(h, pid))
    assert h.rounds(pid)[0]["status"] == "completed"
    st = {r["agent_id"]: r["status"] for r in h.runs(pid, 1)}
    assert st == {a: "succeeded", b: "succeeded", j: "skipped"}


def test_reviewer_and_peers_see_recorded_tool_evidence(make):
    """The judge must be able to check reported numbers against what code actually printed (or failed)."""
    from roundtable.providers import LLMResponse, ToolCall
    log = []

    def script(system, messages, tools, **k):
        name, rnd = who(system), round_of(messages)
        if len(messages) == 1:
            log.append((name, rnd, messages[0]["content"]))
        if name == "Exp" and rnd == 1 and len(messages) == 1:
            return LLMResponse(text="", tool_calls=[
                ToolCall("a", "write_file", {"path": "exp.py", "content": "print('MEASURED cos=0.123')"}),
                ToolCall("b", "run_python", {"path": "exp.py"}),
                ToolCall("c", "run_python", {"code": "import subprocess; subprocess.run(['ls'])"})])
        return f"{name} says cos=1.0000"  # a claim that does not match the evidence

    h = make(script)
    pid, (a, b, j) = h.project(3, {"max_rounds": 2}, names=["Theo", "Exp", "Judge"], tools=["files", "python"])
    service.update_settings(h.store, pid, {"review_agents": [j]})
    run(go(h, pid))
    judge1 = [u for n, r, u in log if n == "Judge" and r == 1][0]
    assert "MEASURED cos=0.123" in judge1               # the actual printed number
    assert "executed exp.py (exact content at run time)" in judge1 and "print('MEASURED" in judge1
    assert "FAILED" in judge1 and "PermissionError" in judge1  # the failed run is visible as such
    theo2 = [u for n, r, u in log if n == "Theo" and r == 2][0]
    assert "MEASURED cos=0.123" in theo2                # peers see the evidence next round too


def test_long_script_does_not_push_program_output_out_of_evidence(make):
    from roundtable.providers import LLMResponse, ToolCall
    long_script = "# padding\n" * 900 + "for i in range(12):\n    print(f'RESULT-LINE-{i}')\n"
    log = []

    def script(system, messages, tools, **k):
        name, rnd = who(system), round_of(messages)
        if len(messages) == 1:
            log.append((name, rnd, messages[0]["content"]))
        if name == "Exp" and rnd == 1 and len(messages) == 1:
            return LLMResponse(text="", tool_calls=[ToolCall("a", "write_file", {"path": "e.py", "content": long_script}),
                                                   ToolCall("b", "run_python", {"path": "e.py"})])
        return "ok"

    h = make(script)
    pid, (a, b, j) = h.project(3, {"max_rounds": 2}, names=["Theo", "Exp", "Judge"], tools=["files", "python"])
    service.update_settings(h.store, pid, {"review_agents": [j]})
    run(go(h, pid))
    judge1 = [u for n, r, u in log if n == "Judge" and r == 1][0]
    theo2 = [u for n, r, u in log if n == "Theo" and r == 2][0]
    for ctx in (judge1, theo2):
        assert all(f"RESULT-LINE-{i}" in ctx for i in range(12)), "program output lines were cut"
    assert "source, not program output" in judge1


def test_retry_can_switch_model_but_keeps_frozen_context(make):
    from roundtable.providers import ProviderError
    seen = []

    def script(model, system, messages, **k):
        seen.append((who(system), model, messages[0]["content"]))
        if model == "dead-model":
            raise ProviderError("daily quota exhausted", retryable=False, status=429)
        return "ok"

    h = make(script)
    pid, (a,) = h.project(1, {"max_rounds": 1, "failure_policy": "halt"})
    service.update_agent(h.store, a, {"model": "dead-model"})
    run(go(h, pid))
    assert h.state(pid)["state"] == "needs_attention"
    service.update_agent(h.store, a, {"model": "alive-model"})
    run(go(h, pid, "retry_failed"))
    assert h.rounds(pid)[0]["status"] == "completed"
    assert [m for _, m, _ in seen] == ["dead-model", "alive-model"]
    assert seen[0][2] == seen[1][2]  # same frozen context
    cfg = jload([r for r in h.runs(pid, 1) if not r["superseded"]][0]["agent_config"])
    assert cfg["model"] == "alive-model" and cfg["retried_with_model_change"]
