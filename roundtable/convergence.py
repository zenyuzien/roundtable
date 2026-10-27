"""Transparent, configurable convergence policy.

Each agent appends a structured ```convergence block (stored separately from
its output). After a completed round the policy evaluates explicit criteria;
every criterion and its evidence is recorded so the user can see *why* the run
did or did not stop. Agreement alone never suffices: objections, unresolved
derivation steps, unsupported claims, output stability and workspace conflicts
are all checked.
"""
from __future__ import annotations

import json
import re

_BLOCK = re.compile(r"```convergence\s*\n(.*?)```", re.S)


def parse_assessment(text: str) -> dict | None:
    blocks = _BLOCK.findall(text or "")
    if not blocks:
        return None
    raw = blocks[-1].strip()
    try:
        d = json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", raw, re.S)
        if not m:
            return None
        try:
            d = json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
    if not isinstance(d, dict):
        return None

    def lst(v):
        if v is None:
            return []
        if isinstance(v, list):
            return [str(x) for x in v if str(x).strip()]
        return [str(v)] if str(v).strip() else []

    st = d.get("support_termination")
    return {
        "support_termination": st is True or (isinstance(st, str) and st.lower() == "true"),
        "open_objections": lst(d.get("open_objections")),
        "unresolved_steps": lst(d.get("unresolved_steps")),
        "unsupported_claims": lst(d.get("unsupported_claims")),
        "rationale": str(d.get("rationale", ""))[:2000],
    }


def strip_assessment(text: str) -> str:
    return _BLOCK.sub("", text or "").strip()


def _shingles(text: str, n: int = 3) -> set:
    w = re.findall(r"\w+", strip_assessment(text).lower())
    return {tuple(w[i:i + n]) for i in range(max(0, len(w) - n + 1))}


def similarity(a: str, b: str) -> float:
    sa, sb = _shingles(a), _shingles(b)
    if not sa and not sb:
        return 1.0
    return len(sa & sb) / max(1, len(sa | sb))


def evaluate(cfg: dict, round_number: int, members: list[int], assessments: dict[int, dict | None],
             outputs: dict[int, str], prev_outputs: dict[int, str], workspace_conflicts: int,
             previous_decisions: list[dict], decider: int | None = None, decider_name: str = "") -> dict:
    crit = []

    def add(name, passed, detail):
        crit.append({"name": name, "passed": bool(passed), "detail": detail})

    add("min_rounds", round_number >= cfg["min_rounds"], f"round {round_number}, minimum {cfg['min_rounds']}")
    if decider is not None:
        # Designated decider (e.g. a neutral judge): its structured assessment alone decides.
        a = assessments.get(decider)
        add("decider_assessment", a is not None,
            f"{decider_name or decider} {'gave' if a else 'did not give'} a parsable assessment")
        add("decider_supports_termination", bool(a and a.get("support_termination")),
            f"{decider_name or decider} {'supports' if a and a.get('support_termination') else 'does not support'} "
            "stopping" + (f": {a['rationale']}" if a and a.get("rationale") else ""))
        return _finish(crit, cfg, round_number, previous_decisions, decider=decider)
    present = [a for a in members if assessments.get(a)]
    add("assessments_present", len(present) == len(members),
        f"{len(present)}/{len(members)} agents gave a parsable assessment")
    support = sum(1 for a in members if (assessments.get(a) or {}).get("support_termination"))
    frac = support / max(1, len(members))
    add("support_termination", frac >= cfg["support_fraction"],
        f"{support}/{len(members)} support termination (required fraction {cfg['support_fraction']})")
    for key, limit_key, label in (("open_objections", "max_open_objections", "open objections"),
                                  ("unresolved_steps", "max_unresolved_steps", "unresolved derivation steps"),
                                  ("unsupported_claims", "max_unsupported_claims", "unsupported claims")):
        total = sum(len((assessments.get(a) or {}).get(key, [])) for a in members)
        add(key, total <= cfg[limit_key], f"{total} {label} reported (allowed {cfg[limit_key]})")
    ms = cfg.get("min_similarity")
    if ms:
        sims = [similarity(outputs[a], prev_outputs[a]) for a in members if a in outputs and a in prev_outputs]
        if sims:
            mean = sum(sims) / len(sims)
            add("stability", mean >= ms, f"mean similarity to previous round {mean:.2f} (required >= {ms}); "
                                         "high similarity means rounds are no longer changing much")
        else:
            add("stability", False, "no previous-round outputs to compare")
    if cfg.get("require_no_workspace_conflicts", True):
        add("workspace_conflicts", workspace_conflicts == 0, f"{workspace_conflicts} unresolved workspace conflicts")
    return _finish(crit, cfg, round_number, previous_decisions)


def _finish(crit: list[dict], cfg: dict, round_number: int, previous_decisions: list[dict],
            decider: int | None = None) -> dict:
    all_met = all(c["passed"] for c in crit)
    need = max(1, int(cfg.get("consecutive_rounds", 1)))
    streak = 1 if all_met else 0
    if all_met:
        for d in sorted(previous_decisions, key=lambda d: -d["round_number"]):
            if d.get("all_criteria_met") and d["round_number"] == round_number - streak:
                streak += 1
            else:
                break
    converged = all_met and streak >= need
    return {"round_number": round_number, "criteria": crit, "all_criteria_met": all_met,
            "consecutive": streak, "required_consecutive": need, "converged": converged, "decider": decider}
