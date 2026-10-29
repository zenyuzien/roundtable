"""Per-agent context assembly from a frozen round snapshot.

The snapshot for round t consists of everything with ``visible_from_round <= t``.
Contexts for all agents of a round are built in the same transaction that
freezes the snapshot, before any agent starts, and are stored verbatim in
``agent_runs.context`` so the user can inspect exactly what each agent received.

Budgeting: sections are filled in priority order under ``max_context_tokens``
(estimated at 4 chars/token); anything cut is marked and stays retrievable via
the transcript tools. The raw transcript in the database is never modified.
"""
from __future__ import annotations

from .db import Store, jload
from .tools.registry import format_entry
from .workspace import ProjectPaths, list_tree, snapshot_entries

CHARS_PER_TOKEN = 4

CONVERGENCE_INSTRUCTIONS = """
At the very end of every response, append a structured self-assessment in exactly this format (it is parsed
by the application and shown separately; be honest - agreement alone is not evidence of correctness):

```convergence
{"support_termination": false,
 "open_objections": ["substantive objections to claims made so far that are not yet resolved"],
 "unresolved_steps": ["derivation steps or arguments that are incomplete or unverified"],
 "unsupported_claims": ["important claims that lack evidence, proof, or a retrieved source"],
 "rationale": "one or two sentences"}
```
Set support_termination to true only if you believe further rounds would not materially improve the result.
"""


def protocol_preamble(agent_name: str, member_names: list[str], tool_groups: set[str], convergence: bool,
                      memory: bool, reviewer_names: list[str] | None = None, is_reviewer: bool = False) -> str:
    others = [n for n in member_names if n != agent_name]
    reviewer_names = reviewer_names or []
    if is_reviewer:
        timing = ("- You work in the second phase of each round: the other agents first respond independently, then you "
                  "receive their contributions from this round (shown below) and review them.\n"
                  "- Everyone sees your review at the start of the next round.")
    else:
        timing = ("- You cannot see other agents' outputs from the current round; you will see them next round."
                  + (f"\n- {', '.join(reviewer_names)} reviews each round after the others have responded; you see that "
                     "review (and any questions to you) at the start of the next round." if reviewer_names else ""))
    tools = []
    if "workspace" in tool_groups:
        tools.append("- workspace_read / workspace_propose: the shared structured research workspace "
                     "(questions, hypotheses, derivations, literature notes, results, open questions, ...).")
    if "files" in tool_groups:
        tools.append("- list_files / read_file / write_file: the shared project file tree.")
    if "transcript" in tool_groups:
        tools.append("- read_round / list_tool_calls / read_tool_result / list_sources: exact records of earlier "
                     "rounds (use these when the context below is summarized or truncated).")
    if "memory" in tool_groups and memory:
        tools.append("- memory_write: your private persistent memory.")
    if tool_groups & {"web_search", "fetch_url"}:
        tools.append("- web_search / fetch_url: the web.")
    if "arxiv" in tool_groups:
        tools.append("- arxiv_search / arxiv_read: arXiv papers (full text).")
    if "python" in tool_groups:
        tools.append("- run_python: execute Python (numerical checks, simulations, experiments).")
    if "bash" in tool_groups:
        tools.append("- run_bash: restricted shell in your working copy.")
    tool_txt = "\n".join(tools) if tools else "- (no tools are enabled for you)"
    return f"""You are "{agent_name}", one of {len(member_names)} independently instructed agents working on a shared \
research task in synchronized rounds{f" (the others: {', '.join(others)})" if others else ""}.

How rounds work:
- In each round every agent receives the same frozen snapshot of the shared state and responds independently.
{timing}
- Workspace proposals and files you write are staged and become visible to everyone in the next round.
- The user may intervene between rounds through the common channel or your private channel.

Standards:
- Clearly distinguish (a) established results, (b) claims from sources you or others actually retrieved in this
  project, (c) your own derivations, and (d) conjectures or unverified hypotheses.
- Never fabricate citations. Cite only sources retrieved with tools in this project (see list_sources) or
  provided by the user; label anything else as from memory and unverified.
- Agreement between agents is not evidence of correctness. Maintain a disagreement if the evidence does not
  resolve it, and state precisely what would resolve it.
- Your response is what the other agents and the user read: put the substance of your contribution in the
  response itself. Use files for long artifacts (code, data, long derivations) and summarize their content.
- If another agent or the user asked you a question, answer it directly.
- Report only numbers your tool runs actually printed. If a run failed, say so; never describe results of code
  that did not run successfully. Other agents and reviewers see your recorded tool output.
- Write in Markdown. Use LaTeX for mathematics: $...$ inline and $$...$$ for display equations.

Tools available to you:
{tool_txt}
{CONVERGENCE_INSTRUCTIONS if convergence else ""}
Your instructions from the user follow. They define your focus and behaviour."""


def _clip(text: str, limit: int, hint: str) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[: max(0, limit)] + f"\n[... truncated {len(text) - limit} characters; {hint}]", True


def build_context(store: Store, paths: ProjectPaths, project_id: int, round_number: int, agent: dict,
                  instructions: str, member_ids: list[int], names: dict[int, str], settings: dict,
                  tool_groups: set[str], synthesis: bool = False, synthesis_reason: str = "",
                  current_round: list[dict] | None = None, reviewer_ids: list[int] | None = None) -> dict:
    t = round_number
    cs = settings["context"]
    budget = int(cs["max_context_tokens"]) * CHARS_PER_TOKEN
    per_output = int(cs["max_chars_per_output"])
    aid = agent["id"]
    decider = settings["convergence"].get("decider_agent")
    conv = bool(settings["convergence"]["enabled"]) and not synthesis and (decider in (None, agent["id"])
                                                                         or decider not in member_ids)
    member_names = [names[m] for m in member_ids]

    reviewer_ids = [r for r in (reviewer_ids or []) if r in member_ids]
    system = protocol_preamble(agent["name"], member_names, tool_groups, conv, bool(agent["memory_enabled"]),
                               reviewer_names=[names[r] for r in reviewer_ids if r != agent["id"]],
                               is_reviewer=current_round is not None) \
        + "\n\n" + (instructions.strip() or "(no instructions provided)")

    msgs = store.q(
        "SELECT * FROM messages WHERE project_id = ? AND visible_from_round IS NOT NULL AND visible_from_round <= ? "
        "AND (channel = 'common' OR channel = ?) ORDER BY id", (project_id, t, f"agent:{aid}"))
    task = [m for m in msgs if m["kind"] == "task"]
    common_user = [m for m in msgs if m["channel"] == "common" and m["kind"] in ("user", "system")]
    private = [m for m in msgs if m["channel"] == f"agent:{aid}"]
    outputs = [m for m in msgs if m["kind"] == "agent_output"]

    sections: dict[str, str] = {}
    meta: list[dict] = []

    def attach_txt(m):
        att = jload(m["attachments"], [])
        return ("\nAttached files: " + ", ".join(a.get("workspace_path") or a["filename"] for a in att)) if att else ""

    sections["header"] = (f"# Round {t}\nThis is round {t} (maximum {settings['max_rounds']}). "
                          f"Agents in this round: {', '.join(member_names)}. You are {agent['name']}.")
    task_txt = "\n\n".join(m["content"] + attach_txt(m) for m in task) or "(no initial task recorded)"
    task_txt, cut = _clip(task_txt, budget // 3, "read_round round=0 for the full task")
    sections["task"] = "## Initial task (round 0)\n" + task_txt
    meta.append({"section": "task", "chars": len(task_txt), "truncated": cut})
    if synthesis:
        sections["header"] = (f"# Final synthesis (after round {t - 1})\nThe discussion has ended"
                              f"{f' ({synthesis_reason})' if synthesis_reason else ''}. You are {agent['name']}.")
        sections["closing"] = (
            "## Final synthesis\nWrite the final synthesis of the whole discussion for the user, following your "
            "instructions. Unless they say otherwise: represent every position fairly and in its strongest form; state "
            "what was agreed or conceded; list the cruxes that remain unresolved and what evidence or argument would "
            "resolve each; separate claims backed by retrieved sources or checked computations from claims that were "
            "only asserted; and do not declare a winner unless the evidence clearly supports one.")
    elif current_round is not None:
        sections["closing"] = (f"## Round {t} review\nReview the round-{t} contributions above according to your "
                               "instructions. Refer only to what the agents actually wrote.")
    else:
        sections["closing"] = (f"## Round {t}\nProduce your round-{t} contribution according to your instructions, "
                               f"taking into account everything above" + (" (items marked NEW arrived since the "
                                                                           "previous round)." if t > 1 else "."))
    remaining = budget - sum(len(s) for s in sections.values()) - len(system)

    def take(name: str, text: str, priority_note: str = "") -> None:
        nonlocal remaining
        if not text:
            return
        if len(text) <= remaining:
            sections[name] = text
            remaining -= len(text)
            meta.append({"section": name, "chars": len(text), "truncated": False})
        elif remaining > 400:
            clipped, _ = _clip(text, remaining - 120, priority_note or "use the transcript tools for the rest")
            sections[name] = clipped
            remaining -= len(clipped)
            meta.append({"section": name, "chars": len(clipped), "truncated": True})
        else:
            meta.append({"section": name, "chars": 0, "truncated": True, "omitted": True})

    def mark(m):
        return "NEW " if m["visible_from_round"] == t and t > 1 else ""

    # 0. review phase: this round's phase-1 contributions (highest priority)
    if current_round is not None:
        parts = [f"## This round's contributions (round {t}) - for your review"]
        for c in current_round:
            body, _ = _clip(c["content"], per_output, f"read the full text after the round with read_round round={t}")
            block = f"### {c['name']} - round {t}\n{body}"
            block += format_evidence(c.get("tool_calls") or [], per_result=3000)
            for path, text in c.get("files", []):
                block += f"\n\n**File written this round: `{path}` (file content - source, not program output)**\n```\n{text}\n```"
            parts.append(block)
        if not current_round:
            parts.append("(no successful contributions this round)")
        take("this_round", "\n\n".join(parts), "the rest is available after the round via read_round")

    # 1. private channel (newest first priority, displayed chronologically)
    if private:
        recent = private[-int(cs["private_history_messages"]):]
        lines = [f"### {mark(m)}user -> you (delivered at round {m['visible_from_round']})\n{m['content']}{attach_txt(m)}"
                 for m in recent]
        more = len(private) - len(recent)
        take("private", "## Your private channel with the user (other agents cannot see this)\n"
             + (f"[{more} older private messages omitted; use read_round]\n" if more else "") + "\n\n".join(lines))

    # 2. common-channel user interventions
    if common_user:
        lines = [f"### {mark(m)}user (delivered at round {m['visible_from_round']})\n{m['content']}{attach_txt(m)}"
                 for m in common_user]
        take("common_user", "## User messages in the common channel\n" + "\n\n".join(lines))

    # 3. recent rounds (exact text), newest first until the budget runs out
    k = 10 ** 6 if synthesis else int(cs["recent_rounds_full"])  # the synthesis sees every round
    prev_rounds = sorted({m["round_number"] for m in outputs})
    recent_rounds = [r for r in prev_rounds if r >= t - k]
    older_rounds = [r for r in prev_rounds if r < t - k]
    tool_rows = store.q(
        "SELECT id, agent_id, round_number, tool, args, status, private, result FROM tool_calls WHERE project_id = ? "
        "AND round_number < ? ORDER BY id", (project_id, t))
    round_blocks: dict[int, str] = {}
    for r in sorted(recent_rounds, reverse=True):
        parts = [f"## Round {r} outputs"]
        for m in sorted([m for m in outputs if m["round_number"] == r], key=lambda m: member_order(m, member_ids)):
            body, _ = _clip(m["content"], per_output, f"read_round round={r} for the full output")
            calls = [c for c in tool_rows if c["round_number"] == r and c["agent_id"] == m["agent_id"]
                     and (not c["private"] or c["agent_id"] == aid)]
            calls_txt = format_evidence(calls)
            who = names.get(m["agent_id"], f"agent {m['agent_id']}") + (" (you)" if m["agent_id"] == aid else "")
            parts.append(f"### Output of {who} - round {r}\n{body}{calls_txt}")
        block = "\n\n".join(parts)
        shrunk = False
        if len(block) > remaining and remaining > 2000:
            shrunk = True
            n = max(1, len(parts) - 1)
            shrink = max(500, (remaining - 400) // n)
            parts = [parts[0]] + [_clip(p, shrink, f"read_round round={r}")[0] for p in parts[1:]]
            block = "\n\n".join(parts)
        if len(block) <= remaining:
            round_blocks[r] = block
            remaining -= len(block)
            meta.append({"section": f"round_{r}", "chars": len(block),
                         "truncated": shrunk or "[... truncated" in block})
        else:
            older_rounds.append(r)
            meta.append({"section": f"round_{r}", "chars": 0, "omitted": True})

    # 4. memory
    if agent["memory_enabled"]:
        mem = store.one("SELECT content, round_number FROM memory_versions WHERE agent_id = ? AND visible_from_round "
                        "IS NOT NULL AND visible_from_round <= ? ORDER BY id DESC LIMIT 1", (aid, t))
        if mem:
            take("memory", f"## Your persistent memory (last updated round {mem['round_number']})\n{mem['content']}")

    # 5. workspace
    entries = snapshot_entries(store, project_id, t)
    if entries:
        take("workspace", "## Shared research workspace (state at the start of this round)\n"
             + "\n\n".join(format_entry(e, names) for e in entries), "use workspace_read for full entries")

    # 6. files and sources
    files = list_tree(paths.shared, 200)
    priv_files = list_tree(paths.private(aid), 100)
    if files or priv_files:
        lines = [f"- {f['path']} ({f['size']} bytes)" for f in files] + \
                [f"- private/{f['path']} ({f['size']} bytes, private to you)" for f in priv_files]
        take("files", "## Project files (read with read_file)\n" + "\n".join(lines))
    srcs = store.q("SELECT kind, ref, title, url FROM sources WHERE project_id = ? AND round_number < ? ORDER BY id "
                   "LIMIT 80", (project_id, t))
    if srcs:
        take("sources", "## Sources retrieved so far in this project (verified retrievals)\n" + "\n".join(
            f"- [{s['kind']}:{s['ref']}] {s['title'] or ''} {s['url'] or ''}" for s in srcs))

    # 7. older rounds: summaries (never replacing the stored originals)
    if older_rounds:
        lines = []
        for r in sorted(set(older_rounds)):
            s = store.one("SELECT summary FROM round_summaries WHERE project_id = ? AND round_number = ? "
                          "ORDER BY id DESC LIMIT 1", (project_id, r))
            lines.append(f"### Round {r} (summary)\n{s['summary']}" if s else
                         f"### Round {r}\n(not included; use read_round round={r})")
        take("older", "## Earlier rounds (summaries; exact text available via read_round)\n" + "\n\n".join(lines))

    order = ["header", "task", "older"] + [f"round_{r}" for r in sorted(round_blocks)] + \
        ["common_user", "private", "memory", "workspace", "files", "sources", "this_round", "closing"]
    for r, b in round_blocks.items():
        sections[f"round_{r}"] = b
    user = "\n\n".join(sections[n] for n in order if n in sections)
    return {
        "system": system,
        "user": user,
        "sections": meta,
        "token_estimate": (len(system) + len(user)) // CHARS_PER_TOKEN,
        "snapshot": {
            "round": t,
            "message_ids": [m["id"] for m in msgs],
            "workspace_versions": {e["key"]: e["version"] for e in entries},
        },
    }


EVIDENCE_TOOLS = ("run_python", "run_bash")


def _split_executed(body: str) -> tuple[str, str]:
    """Separate the recorded script ("executed X ... ```python ...```") from the program output."""
    if body.startswith("executed ") and "```\n" in body:
        head, rest = body.split("```python\n", 1) if "```python\n" in body else ("", body)
        code, _, out = rest.partition("\n```\n")
        return head.splitlines()[0] + "\n" + code, out
    return "", body


def _clip_mid(s: str, limit: int) -> str:
    return s if len(s) <= limit else s[:limit // 2] + "\n[... middle omitted ...]\n" + s[-limit // 2:]


def format_evidence(calls: list[dict], per_result: int = 1500, max_calls: int = 25) -> str:
    """Tool calls of one agent's turn; code-execution results are included verbatim (truncated) as evidence."""
    if not calls:
        return ""
    lines = []
    for c in calls[:max_calls]:
        res = c.get("result") or ""
        failed = c["status"] != "ok" or "Traceback" in res or res.startswith(("ERROR", "REFUSED")) or \
            ("exit_code: " in res and "exit_code: 0" not in res)
        head = f"#{c['id']} {c['tool']}({c['args'][:100]})" + (" -> FAILED" if failed else "")
        if c["tool"] in EVIDENCE_TOOLS:
            body = res.split("\n", 1)[1] if res.startswith("[tool call #") else res
            code, out = _split_executed(body)
            # program output has priority over the script text: it is the evidence
            out_budget = max(per_result, int(per_result * 2.5) - min(len(code), per_result // 2))
            block = f"- {head}\n"
            if code:
                first, _, src = code.partition("\n")
                src = src if len(src) <= per_result // 2 else src[:per_result // 2] + "\n# [... script truncated]"
                block += f"  {first}\n```python\n{src}\n```\n"
            block += f"  program output:\n```\n{_clip_mid(out.strip(), out_budget)}\n```"
            lines.append(block)
        else:
            lines.append(f"- {head}")
    more = f"\n- ... {len(calls) - max_calls} more calls" if len(calls) > max_calls else ""
    return "\n_Tool calls (code-execution output is recorded evidence):_\n" + "\n".join(lines) + more


def member_order(m: dict, member_ids: list[int]) -> int:
    try:
        return member_ids.index(m["agent_id"])
    except ValueError:
        return 10_000 + (m["agent_id"] or 0)
