"""Synchronous round engine.

Protocol for round t (all under explicit application control):

1. FREEZE (one DB transaction, nothing awaited): pending user messages, files and
   workspace edits become visible from round t; round membership (enabled agents,
   by position) and each member's current instruction version and config are
   pinned; the full context of every member is built from the snapshot and stored.
   The partial unique index on (project, number) makes running a round twice
   impossible.
2. EXECUTE: all member runs execute concurrently (bounded by max_concurrency).
   Outputs and tool side effects are recorded but invisible to everyone else
   (outputs have visible_from_round NULL, files go to a per-run working copy,
   workspace edits are staged proposals).
3. BARRIER: wait for every run. Failed runs never disappear: the failure policy
   either retries them (same frozen context), or halts for the user, or - only
   if the user chose ``skip_failed`` - completes the round with them recorded as
   skipped.
4. COMMIT (one transaction): outputs become visible from t+1, staged workspace
   proposals / memory / files are committed (conflicts preserved), messages
   delivered in round t become 'processed', the round is marked completed.
5. Post-round checks: stop, convergence, max rounds, budget, breakpoints,
   pause request, step mode. Only then can round t+1 freeze.

There is exactly one loop task per project, so rounds can never overlap.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import traceback
from dataclasses import asdict
from pathlib import Path

from .config import Pricing, Secrets, normalize_settings, redact
from .context import build_context
from .convergence import evaluate, parse_assessment, strip_assessment
from .db import Store, jdump, jload, now
from .providers import Provider, ProviderError, with_retries
from .tools import registry
from .tools.sandbox import Sandbox
from .workspace import (ProjectPaths, archive_staging, cleanup_staging, commit_files, commit_proposals, incorporate_user_edits,
                        snapshot_entries)

log = logging.getLogger("roundtable.engine")

INCOMPLETE = ("running", "failed", "interrupted", "aborted")


class EngineError(Exception):
    pass


class BudgetExceeded(ProviderError):
    def __init__(self, msg: str):
        super().__init__(msg, retryable=False)


class Engine:
    def __init__(self, store: Store, data_dir: Path, providers: dict[str, Provider], secrets: Secrets,
                 pricing: Pricing, sandbox: Sandbox | None = None, retry_base_delay: float = 2.0):
        self.store = store
        self.data_dir = data_dir
        self.providers = providers
        self.secrets = secrets
        self.pricing = pricing
        self.sandbox = sandbox or Sandbox()
        self.retry_base_delay = retry_base_delay
        self.runners: dict[int, ProjectRunner] = {}

    def runner(self, project_id: int) -> "ProjectRunner":
        if project_id not in self.runners:
            if not self.store.one("SELECT id FROM projects WHERE id = ?", (project_id,)):
                raise EngineError("no such project")
            self.runners[project_id] = ProjectRunner(self, project_id)
        return self.runners[project_id]

    def recover(self) -> None:
        """Called at startup: anything that was mid-flight was interrupted by the restart."""
        s = self.store
        with s.tx():
            for r in s.q("SELECT * FROM rounds WHERE status = 'running'"):
                s.ex("UPDATE rounds SET status = 'interrupted', note = ? WHERE id = ?",
                     ("interrupted by application restart", r["id"]))
                s.ex("UPDATE agent_runs SET status = 'interrupted', error = 'interrupted by application restart', "
                     "finished_at = ? WHERE round_id = ? AND status IN ('pending', 'running')", (now(), r["id"]))
                s.event(r["project_id"], "round_interrupted", round=r["number"])
            for p in s.q("SELECT * FROM projects WHERE state IN ('running', 'pausing', 'stopping')"):
                inc = s.one("SELECT number FROM rounds WHERE project_id = ? AND status IN "
                            "('failed','interrupted','aborted')", (p["id"],))
                if inc:
                    s.ex("UPDATE projects SET state = 'needs_attention', state_reason = ? WHERE id = ?",
                         (f"Round {inc['number']} was interrupted by an application restart. Retry the unfinished "
                          "agents, continue without them, or discard the round.", p["id"]))
                else:
                    s.ex("UPDATE projects SET state = 'paused', state_reason = ? WHERE id = ?",
                         ("Application restarted; resume to continue.", p["id"]))
                s.bump(p["id"])

    async def shutdown(self) -> None:
        for r in self.runners.values():
            await r.stop(wait=True)


class ProjectRunner:
    def __init__(self, engine: Engine, project_id: int):
        self.e = engine
        self.s = engine.store
        self.pid = project_id
        self.paths = ProjectPaths(engine.data_dir, project_id)
        self.loop_task: asyncio.Task | None = None
        self.agent_tasks: dict[int, asyncio.Task] = {}
        self.pause_requested = False
        self.stop_requested = False
        self.synthesis_pending: str | None = None
        self.lock = asyncio.Lock()

    # ------------------------------------------------------------ helpers

    def settings(self) -> dict:
        p = self.s.one("SELECT settings FROM projects WHERE id = ?", (self.pid,))
        return normalize_settings(jload(p["settings"], {}))

    def set_state(self, state: str, reason: str | None = None) -> None:
        self.s.ex("UPDATE projects SET state = ?, state_reason = ? WHERE id = ?", (state, reason, self.pid))
        self.s.event(self.pid, "state", state=state, reason=reason)

    @property
    def busy(self) -> bool:
        return self.loop_task is not None and not self.loop_task.done()

    def last_completed(self) -> int:
        return self.s.val("SELECT COALESCE(MAX(number), 0) FROM rounds WHERE project_id = ? AND status = 'completed'",
                          (self.pid,))

    def debate_rounds_done(self) -> int:
        """Completed rounds that count towards max_rounds (the final synthesis turn does not)."""
        return self.s.val("SELECT COUNT(*) FROM rounds WHERE project_id = ? AND status = 'completed' AND "
                          "kind != 'synthesis'", (self.pid,))

    def _synthesis_agent(self, st: dict) -> dict | None:
        aid = st.get("synthesis_agent")
        if not aid:
            return None
        return self.s.one("SELECT * FROM agents WHERE id = ? AND project_id = ? AND enabled = 1 AND deleted = 0",
                          (aid, self.pid))

    def _finish(self, reason: str, st: dict) -> None:
        """The debate is over: complete, or first hand over to the final synthesis turn if configured."""
        last = self.s.one("SELECT kind FROM rounds WHERE project_id = ? AND status = 'completed' ORDER BY number "
                          "DESC LIMIT 1", (self.pid,))
        if self._synthesis_agent(st) and last and last["kind"] != "synthesis":
            self.synthesis_pending = reason
        else:
            self.set_state("completed", reason)

    async def _run_synthesis(self) -> None:
        reason, self.synthesis_pending = self.synthesis_pending, None
        st = self.settings()
        agent = self._synthesis_agent(st)
        bp = self.budget_problem(st)
        if not agent or bp:
            self.set_state("completed", reason + (f" (final synthesis skipped: {bp})" if bp else ""))
            return
        t = self.last_completed() + 1
        self.set_state("running", f"{reason}; writing the final synthesis ({agent['name']})")
        round_id = self._freeze_round(t, st, synthesis_agent=agent["id"], synthesis_reason=reason)
        run_ids = [r["id"] for r in self.s.q("SELECT id FROM agent_runs WHERE round_id = ?", (round_id,))]
        await self._execute(run_ids)
        if await self._settle(round_id, st):
            self.set_state("completed", f"{reason}; final synthesis by {agent['name']} (round {t})")

    def incomplete_round(self) -> dict | None:
        return self.s.one(f"SELECT * FROM rounds WHERE project_id = ? AND status IN {INCOMPLETE} "
                          "ORDER BY number DESC LIMIT 1", (self.pid,))

    def running_round_number(self) -> int | None:
        r = self.s.one("SELECT number FROM rounds WHERE project_id = ? AND status = 'running'", (self.pid,))
        return r["number"] if r else None

    def usage(self) -> dict:
        a = self.s.one("SELECT COALESCE(SUM(tokens_in),0) ti, COALESCE(SUM(tokens_out),0) tou, SUM(cost) c, "
                       "SUM(CASE WHEN cost IS NULL AND (tokens_in + tokens_out) > 0 THEN 1 ELSE 0 END) unpriced "
                       "FROM agent_runs WHERE project_id = ?", (self.pid,))
        b = self.s.one("SELECT COALESCE(SUM(tokens_in),0) ti, COALESCE(SUM(tokens_out),0) tou, SUM(cost) c "
                       "FROM round_summaries WHERE project_id = ?", (self.pid,))
        return {"tokens_in": a["ti"] + b["ti"], "tokens_out": a["tou"] + b["tou"],
                "cost_usd": (a["c"] or 0) + (b["c"] or 0), "unpriced_runs": a["unpriced"] or 0}

    def budget_problem(self, settings: dict) -> str | None:
        u = self.usage()
        b = settings["budget"]
        total = u["tokens_in"] + u["tokens_out"]
        if b.get("max_total_tokens") and total >= b["max_total_tokens"]:
            return f"token budget exhausted ({total:,} / {b['max_total_tokens']:,} tokens)"
        if b.get("max_cost_usd") and u["cost_usd"] >= b["max_cost_usd"]:
            return f"cost budget exhausted (${u['cost_usd']:.2f} / ${b['max_cost_usd']:.2f})"
        return None

    def agent_names(self) -> dict[int, str]:
        return {a["id"]: a["name"] for a in self.s.q("SELECT id, name FROM agents WHERE project_id = ?", (self.pid,))}

    # ------------------------------------------------------------ control API

    async def control(self, action: str) -> dict:
        async with self.lock:
            if action in ("start", "resume", "step"):
                self._check_can_run()
                self.pause_requested = False
                self.stop_requested = False
                self.set_state("running", "single round" if action == "step" else None)
                self.loop_task = asyncio.create_task(self._loop(single=(action == "step")))
            elif action == "pause":
                if self.busy:
                    self.pause_requested = True
                    self.set_state("pausing", "will pause after the current round completes")
                else:
                    raise EngineError("not running")
            elif action == "stop":
                await self.stop(wait=False)
            elif action in ("retry_failed", "skip_failed"):
                if self.busy:
                    raise EngineError("execution in progress")
                inc = self.incomplete_round()
                if not inc:
                    raise EngineError("there is no incomplete round")
                self.stop_requested = False
                self.pause_requested = False
                self.set_state("running", f"{'retrying' if action == 'retry_failed' else 'completing'} round "
                                          f"{inc['number']}")
                self.loop_task = asyncio.create_task(self._recover_round(inc["id"], skip=(action == "skip_failed")))
            elif action == "discard_round":
                if self.busy:
                    raise EngineError("execution in progress")
                inc = self.incomplete_round()
                if not inc:
                    raise EngineError("there is no incomplete round")
                self._discard_round(inc)
            else:
                raise EngineError(f"unknown action {action!r}")
        return {"ok": True}

    def _check_can_run(self) -> None:
        if self.busy:
            raise EngineError("already running")
        inc = self.incomplete_round()
        if inc:
            raise EngineError(f"round {inc['number']} is incomplete ({inc['status']}); retry the failed agents, "
                              "continue without them, or discard the round first")
        if not self.s.one("SELECT id FROM messages WHERE project_id = ? AND kind = 'task'", (self.pid,)):
            raise EngineError("set the initial task first")
        if not self.s.one("SELECT id FROM agents WHERE project_id = ? AND enabled = 1 AND deleted = 0", (self.pid,)):
            raise EngineError("enable at least one agent")

    async def stop(self, wait: bool = False) -> None:
        if not self.busy:
            if self.s.one("SELECT id FROM projects WHERE id = ? AND state IN ('running','pausing')", (self.pid,)):
                self.set_state("stopped", "stopped by user")
            return
        self.stop_requested = True
        self.set_state("stopping", "cancelling in-flight agent calls")
        for t in list(self.agent_tasks.values()):
            t.cancel()
        if wait and self.loop_task:
            try:
                await asyncio.wait_for(asyncio.shield(self.loop_task), timeout=30)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass

    async def wait(self) -> None:
        """Wait for the current loop to finish (used by tests)."""
        if self.loop_task:
            await self.loop_task

    # ------------------------------------------------------------ main loop

    async def _loop(self, single: bool) -> None:
        try:
            while True:
                st = self.settings()
                if self.stop_requested:
                    self.set_state("stopped", "stopped by user")
                    return
                if self.pause_requested:
                    self.set_state("paused", "paused by user")
                    return
                t = self.last_completed() + 1
                if self.debate_rounds_done() >= st["max_rounds"]:
                    self.set_state("completed", f"maximum rounds reached ({st['max_rounds']})")
                    return
                bp = self.budget_problem(st)
                if bp:
                    self.set_state("paused", f"Budget: {bp}. Raise the budget to continue.")
                    return
                round_id = self._freeze_round(t, st)
                run_ids = [r["id"] for r in self.s.q("SELECT id FROM agent_runs WHERE round_id = ? ORDER BY position",
                                                     (round_id,))]
                await self._execute(run_ids)
                if not await self._settle(round_id, st):
                    return
                await self._maybe_summarize(t, st)
                if self._post_round(t, single):
                    if self.synthesis_pending:
                        await self._run_synthesis()
                    return
        except Exception as e:  # never leave the project silently "running"
            log.exception("engine loop crashed")
            self._abort_running_round(f"engine error: {e}")
            self.set_state("needs_attention", f"Engine error: {type(e).__name__}: {e}")
            self.s.event(self.pid, "engine_error", error=traceback.format_exc()[-4000:])

    def _freeze_round(self, t: int, st: dict, synthesis_agent: int | None = None, synthesis_reason: str = "") -> int:
        s = self.s
        with s.tx():
            if s.one("SELECT id FROM rounds WHERE project_id = ? AND number = ? AND status != 'discarded'",
                     (self.pid, t)):
                raise EngineError(f"round {t} already exists")
            if self.incomplete_round():
                raise EngineError("an incomplete round exists")
            # pending interventions are incorporated into this snapshot (and only now)
            s.ex("UPDATE messages SET status = 'incorporated', visible_from_round = ? WHERE project_id = ? AND "
                 "status = 'pending'", (t, self.pid))
            self._incorporate_files(t)
            incorporate_user_edits(s, self.pid, t)
            agents = s.q("SELECT * FROM agents WHERE project_id = ? AND enabled = 1 AND deleted = 0 "
                         "ORDER BY position, id", (self.pid,))
            if synthesis_agent is not None:
                agents = [a for a in agents if a["id"] == synthesis_agent]
            if not agents:
                raise EngineError("no enabled agents")
            names = self.agent_names()
            member_ids = [a["id"] for a in agents]
            round_id = s.ex("INSERT INTO rounds(project_id, number, status, members, started_at, kind) VALUES "
                            "(?,?, 'running', ?, ?, ?)", (self.pid, t, jdump(member_ids), now(),
                                                          "synthesis" if synthesis_agent is not None else "debate"))
            for pos, a in enumerate(agents):
                iv = s.one("SELECT * FROM instruction_versions WHERE agent_id = ? ORDER BY version DESC LIMIT 1",
                           (a["id"],))
                groups = registry.allowed_groups(jload(a["tools"], []), st)
                if not a["memory_enabled"]:
                    groups.discard("memory")
                ctx = build_context(s, self.paths, self.pid, t, a, iv["text"] if iv else "", member_ids, names, st,
                                    groups, synthesis=synthesis_agent is not None, synthesis_reason=synthesis_reason)
                ctx["settings"] = st
                cfg = {"name": a["name"], "provider": a["provider"], "model": a["model"],
                       "temperature": a["temperature"], "max_tokens": a["max_tokens"],
                       "tool_groups": sorted(groups), "max_tool_iterations": st["max_tool_iterations"],
                       "instruction_version": iv["version"] if iv else None}
                s.ex("INSERT INTO agent_runs(project_id, round_id, round_number, agent_id, position, status, "
                     "instruction_version_id, agent_config, context) VALUES (?,?,?,?,?, 'pending', ?,?,?)",
                     (self.pid, round_id, t, a["id"], pos, iv["id"] if iv else None, jdump(cfg), jdump(ctx)))
            s.event(self.pid, "round_started", round=t, members=member_ids)
        return round_id

    def _incorporate_files(self, t: int) -> None:
        for f in self.s.q("SELECT * FROM files WHERE project_id = ? AND status = 'pending' ORDER BY id", (self.pid,)):
            if f["channel"] == "common":
                dest_dir = self.paths.shared / "uploads"
                rel = f"uploads/{f['filename']}"
            else:
                aid = int(f["channel"].split(":")[1])
                dest_dir = self.paths.private(aid) / "uploads"
                rel = f"private/uploads/{f['filename']}"
            dest_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f["stored_path"], dest_dir / f["filename"])
            self.s.ex("UPDATE files SET status = 'incorporated', visible_from_round = ?, workspace_path = ? "
                      "WHERE id = ?", (t, rel, f["id"]))

    async def _execute(self, run_ids: list[int]) -> None:
        st = self.settings()
        sem = asyncio.Semaphore(st["max_concurrency"])

        async def guarded(rid):
            async with sem:
                if self.stop_requested:
                    self._mark_run(rid, "aborted", "stopped before start")
                    return
                await self._run_agent(rid)

        self.agent_tasks = {rid: asyncio.create_task(guarded(rid)) for rid in run_ids}
        try:
            await asyncio.gather(*self.agent_tasks.values(), return_exceptions=True)
        finally:
            self.agent_tasks = {}
        # Defensive: a run that is not terminal here would break the barrier invariant.
        for rid in run_ids:
            r = self.s.one("SELECT status FROM agent_runs WHERE id = ?", (rid,))
            if r["status"] in ("pending", "running"):
                self._mark_run(rid, "aborted" if self.stop_requested else "failed", "did not reach a terminal state")

    def _mark_run(self, rid: int, status: str, error: str | None) -> None:
        self.s.ex("UPDATE agent_runs SET status = ?, error = ?, finished_at = ? WHERE id = ?",
                  (status, error, now(), rid))
        self.s.event(self.pid, "agent_run", run_id=rid, status=status)

    # ------------------------------------------------------------ one agent, one round

    async def _run_agent(self, rid: int) -> None:
        s = self.s
        run = s.one("SELECT * FROM agent_runs WHERE id = ?", (rid,))
        cfg = jload(run["agent_config"])
        ctx = jload(run["context"])
        st = ctx.get("settings") or self.settings()
        s.ex("UPDATE agent_runs SET status = 'running', started_at = ? WHERE id = ?", (now(), rid))
        s.event(self.pid, "agent_run", run_id=rid, status="running")
        messages: list[dict] = [{"role": "user", "content": ctx["user"]}]
        texts: list[str] = []
        try:
            provider = self.e.providers.get(cfg["provider"])
            if provider is None:
                raise ProviderError(f"unknown provider {cfg['provider']!r}")
            groups = set(cfg["tool_groups"])
            specs = registry.specs_for(groups)
            tctx = registry.ToolContext(
                store=s, paths=self.paths, sandbox=self.e.sandbox, project_id=self.pid, agent_id=run["agent_id"],
                agent_name=cfg["name"], run_id=rid, round_number=run["round_number"], settings=st,
                agent_names=self.agent_names(), redact=lambda x: redact(x, self.e.secrets))
            max_iter = int(cfg["max_tool_iterations"]) if specs else 0
            final, stop_reason = "", None
            for it in range(max_iter + 1):
                bp = self.budget_problem(self.settings())
                if bp:
                    raise BudgetExceeded(f"Budget exceeded mid-round: {bp}")
                last = it == max_iter

                def on_retry(n, err, delay, _rid=rid):
                    s.event(self.pid, "provider_retry", run_id=_rid, attempt=n, delay=round(delay, 1),
                            error=redact(str(err), self.e.secrets)[:300])

                resp = await with_retries(
                    lambda: provider.complete(model=cfg["model"], system=ctx["system"], messages=messages,
                                              tools=specs, max_tokens=int(cfg["max_tokens"]),
                                              temperature=cfg.get("temperature"),
                                              tool_choice_none=bool(last and specs)),
                    base_delay=self.e.retry_base_delay, on_retry=on_retry)
                self._add_usage(rid, cfg["model"], resp.tokens_in, resp.tokens_out)
                stop_reason = resp.stop_reason
                if resp.text:
                    texts.append(resp.text)
                if resp.tool_calls and not last:
                    messages.append({"role": "assistant", "content": resp.text,
                                     "tool_calls": [asdict(c) for c in resp.tool_calls], "_raw": resp.raw_assistant})
                    results = []
                    for call in resp.tool_calls:
                        out = await registry.execute(tctx, call, groups)
                        results.append({"id": call.id, "name": call.name, "content": out})
                    if it + 1 == max_iter and results:
                        results[-1]["content"] += ("\n\n[Tool-call limit reached for this round: write your final "
                                                   "response now without further tool calls.]")
                    messages.append({"role": "tool", "results": results})
                    s.ex("UPDATE agent_runs SET transcript = ? WHERE id = ?", (jdump(_clean(messages)), rid))
                    continue
                final = resp.text
                messages.append({"role": "assistant", "content": resp.text})
                break
            output = final.strip() or "\n\n".join(t for t in texts if t.strip()).strip()
            if not output and stop_reason not in ("max_tokens", "length"):
                # Some models end a tool loop without any text; ask once, explicitly, for the response.
                messages.append({"role": "user", "content": "Now write your full response for this round."})
                resp = await with_retries(
                    lambda: provider.complete(model=cfg["model"], system=ctx["system"], messages=messages,
                                              tools=specs, max_tokens=int(cfg["max_tokens"]),
                                              temperature=cfg.get("temperature"), tool_choice_none=bool(specs)),
                    base_delay=self.e.retry_base_delay)
                self._add_usage(rid, cfg["model"], resp.tokens_in, resp.tokens_out)
                stop_reason = resp.stop_reason
                output = resp.text.strip()
                messages.append({"role": "assistant", "content": resp.text})
            if not output:
                raise ProviderError(
                    "the model returned an empty response" + (
                        " (it hit max_tokens - its reasoning may have used the whole budget; raise "
                        "'Max output tokens' for this agent)" if stop_reason in ("max_tokens", "length") else ""))
            if stop_reason in ("max_tokens", "length"):
                output += "\n\n*[output truncated: the model hit its max_tokens limit]*"
            with s.tx():
                s.ex("UPDATE agent_runs SET status = 'succeeded', output = ?, transcript = ?, finished_at = ? "
                     "WHERE id = ?", (output, jdump(_clean(messages)), now(), rid))
                s.ex("INSERT INTO messages(project_id, channel, kind, agent_id, agent_run_id, round_number, content, "
                     "status, created_at) VALUES (?, 'common', 'agent_output', ?,?,?,?, 'unconfirmed', ?)",
                     (self.pid, run["agent_id"], rid, run["round_number"], output, now()))
                if st["convergence"]["enabled"]:
                    a = parse_assessment(output)
                    s.ex("INSERT INTO convergence_assessments(project_id, round_number, agent_id, agent_run_id, "
                         "assessment, parsed_ok, created_at) VALUES (?,?,?,?,?,?,?)",
                         (self.pid, run["round_number"], run["agent_id"], rid, jdump(a), int(a is not None), now()))
                s.event(self.pid, "agent_run", run_id=rid, status="succeeded")
        except asyncio.CancelledError:
            s.ex("UPDATE agent_runs SET transcript = ? WHERE id = ?", (jdump(_clean(messages)), rid))
            self._mark_run(rid, "aborted", "cancelled by stop request")
            raise
        except Exception as e:
            msg = redact(f"{type(e).__name__}: {e}", self.e.secrets)
            if not isinstance(e, ProviderError):
                log.exception("agent run %s crashed", rid)
            s.ex("UPDATE agent_runs SET transcript = ? WHERE id = ?", (jdump(_clean(messages)), rid))
            self._mark_run(rid, "failed", msg)

    def _add_usage(self, rid: int, model: str, ti: int, to: int) -> None:
        c = self.e.pricing.cost(model, ti, to)
        if c is None:
            self.s.ex("UPDATE agent_runs SET tokens_in = tokens_in + ?, tokens_out = tokens_out + ? WHERE id = ?",
                      (ti, to, rid))
        else:
            self.s.ex("UPDATE agent_runs SET tokens_in = tokens_in + ?, tokens_out = tokens_out + ?, "
                      "cost = COALESCE(cost, 0) + ? WHERE id = ?", (ti, to, c, rid))
        self.s.bump(self.pid)

    # ------------------------------------------------------------ barrier / settle

    def _current_runs(self, round_id: int) -> list[dict]:
        return self.s.q("SELECT * FROM agent_runs WHERE round_id = ? AND superseded = 0 ORDER BY position",
                        (round_id,))

    async def _settle(self, round_id: int, st: dict) -> bool:
        """Barrier reached. Returns True iff the round was completed."""
        rnd = self.s.one("SELECT * FROM rounds WHERE id = ?", (round_id,))
        if self.stop_requested:
            if all(r["status"] == "succeeded" for r in self._current_runs(round_id)):
                self._finalize(round_id, skip=[])
                self.set_state("stopped", f"stopped by user after round {rnd['number']} completed")
            else:
                self.s.ex("UPDATE rounds SET status = 'aborted', note = 'stopped by user' WHERE id = ?", (round_id,))
                self.set_state("stopped", f"Stopped during round {rnd['number']}. Completed rounds are preserved; "
                                          "the partial round can be retried or discarded.")
            return False
        failed = [r for r in self._current_runs(round_id) if r["status"] != "succeeded"]
        if failed and st["failure_policy"] == "retry_then_halt":
            for _ in range(st["auto_retries"]):
                if not failed or self.stop_requested:
                    break
                if any("Budget exceeded" in (r["error"] or "") for r in failed):
                    break
                await self._retry(round_id, failed)
                failed = [r for r in self._current_runs(round_id) if r["status"] != "succeeded"]
            if self.stop_requested:
                return await self._settle(round_id, st)
        if failed:
            if st["failure_policy"] == "skip_failed":
                self._finalize(round_id, skip=failed)
                return True
            self.s.ex("UPDATE rounds SET status = 'failed', note = ? WHERE id = ?",
                      (f"{len(failed)} agent(s) failed", round_id))
            names = ", ".join(jload(r["agent_config"])["name"] for r in failed)
            budget = any("Budget exceeded" in (r["error"] or "") for r in failed)
            self.set_state("needs_attention", f"Round {rnd['number']}: {names} failed"
                           + (" because the token/cost budget ran out mid-round. Raise the budget, then retry them"
                              if budget else ". Retry them") + ", continue without them, or discard the round.")
            return False
        self._finalize(round_id, skip=[])
        return True

    async def _retry(self, round_id: int, failed: list[dict]) -> None:
        new_ids = []
        with self.s.tx():
            for r in failed:
                self.s.ex("UPDATE agent_runs SET superseded = 1 WHERE id = ?", (r["id"],))
                self.s.ex("UPDATE ws_proposals SET status = 'discarded' WHERE agent_run_id = ? AND status = 'staged'",
                          (r["id"],))
                self.s.ex("UPDATE memory_versions SET status = 'discarded' WHERE agent_run_id = ? AND "
                          "status = 'staged'", (r["id"],))
                new_ids.append(self.s.ex(
                    "INSERT INTO agent_runs(project_id, round_id, round_number, agent_id, position, attempt, status, "
                    "instruction_version_id, agent_config, context) VALUES (?,?,?,?,?,?, 'pending', ?,?,?)",
                    (self.pid, round_id, r["round_number"], r["agent_id"], r["position"], r["attempt"] + 1,
                     r["instruction_version_id"], r["agent_config"], r["context"])))
            self.s.ex("UPDATE rounds SET status = 'running', note = NULL WHERE id = ?", (round_id,))
            self.s.event(self.pid, "retry", round_id=round_id, runs=[r["id"] for r in failed])
        archive_staging(self.paths, failed[0]["round_number"], failed)
        await self._execute(new_ids)

    def _finalize(self, round_id: int, skip: list[dict]) -> None:
        s = self.s
        rnd = s.one("SELECT * FROM rounds WHERE id = ?", (round_id,))
        t = rnd["number"]
        names = self.agent_names()
        with s.tx():
            for r in skip:
                s.ex("UPDATE agent_runs SET status = 'skipped', error = COALESCE(error, '') || ' [skipped by "
                     "failure policy]' WHERE id = ?", (r["id"],))
                s.ex("UPDATE ws_proposals SET status = 'discarded' WHERE agent_run_id = ? AND status = 'staged'",
                     (r["id"],))
                s.ex("UPDATE memory_versions SET status = 'discarded' WHERE agent_run_id = ? AND status = 'staged'",
                     (r["id"],))
            ok = [r for r in self._current_runs(round_id) if r["status"] == "succeeded"]
            ids = [r["id"] for r in ok]
            if ids:
                marks = ",".join("?" * len(ids))
                s.ex(f"UPDATE messages SET status = 'final', visible_from_round = ? WHERE agent_run_id IN ({marks}) "
                     "AND kind = 'agent_output'", [t + 1, *ids])
                s.ex(f"UPDATE memory_versions SET status = 'committed', visible_from_round = ? WHERE agent_run_id IN "
                     f"({marks}) AND status = 'staged'", [t + 1, *ids])
                commit_proposals(s, self.pid, t, ids, {r["agent_id"]: r["position"] for r in ok}, names)
                commit_files(s, self.paths, self.pid, t, ok, names)
            s.ex("UPDATE messages SET status = 'processed' WHERE project_id = ? AND status = 'incorporated' AND "
                 "visible_from_round <= ?", (self.pid, t))
            s.ex("UPDATE files SET status = 'processed' WHERE project_id = ? AND status = 'incorporated' AND "
                 "visible_from_round <= ?", (self.pid, t))
            note = f"{len(skip)} agent(s) skipped after failure" if skip else None
            s.ex("UPDATE rounds SET status = 'completed', finished_at = ?, note = ? WHERE id = ?", (now(), note, round_id))
            s.event(self.pid, "round_completed", round=t, skipped=[r["agent_id"] for r in skip])
        archive_staging(self.paths, t, skip)
        cleanup_staging(self.paths, t)

    def _post_round(self, t: int, single: bool) -> bool:
        """Returns True if the loop should stop after completed round t."""
        st = self.settings()
        kind = self.s.val("SELECT kind FROM rounds WHERE project_id = ? AND number = ? AND status = 'completed'",
                          (self.pid, t))
        if kind == "synthesis":
            self.set_state("completed", f"final synthesis written (round {t})")
            return True
        if self.stop_requested:
            self.set_state("stopped", f"stopped by user after round {t}")
            return True
        if st["convergence"]["enabled"]:
            d = self._convergence_decision(t, st)
            if d["converged"]:
                self._finish(f"converged after round {t} (see convergence report)", st)
                return True
        if self.debate_rounds_done() >= st["max_rounds"]:
            self._finish(f"maximum rounds reached ({st['max_rounds']})", st)
            return True
        bp = self.budget_problem(st)
        if bp:
            self.set_state("paused", f"Budget: {bp}. Raise the budget to continue.")
            return True
        if t in st["breakpoints"]:
            self.set_state("paused", f"breakpoint after round {t}")
            return True
        if self.pause_requested:
            self.set_state("paused", f"paused by user after round {t}")
            return True
        if single:
            self.set_state("paused", f"step complete (round {t})")
            return True
        return False

    def _convergence_decision(self, t: int, st: dict) -> dict:
        s = self.s
        rnd = s.one("SELECT * FROM rounds WHERE project_id = ? AND number = ? AND status = 'completed'", (self.pid, t))
        members = [r["agent_id"] for r in self._current_runs(rnd["id"]) if r["status"] == "succeeded"]
        assess = {}
        for a in s.q("SELECT * FROM convergence_assessments WHERE project_id = ? AND round_number = ? ORDER BY id",
                     (self.pid, t)):
            run = s.one("SELECT status, superseded FROM agent_runs WHERE id = ?", (a["agent_run_id"],))
            if run["status"] == "succeeded" and not run["superseded"]:
                assess[a["agent_id"]] = jload(a["assessment"])

        def outs(n):
            return {m["agent_id"]: strip_assessment(m["content"]) for m in s.q(
                "SELECT agent_id, content FROM messages WHERE project_id = ? AND kind = 'agent_output' AND "
                "round_number = ? AND status = 'final'", (self.pid, n))}

        conflicts = sum(1 for e in snapshot_entries(s, self.pid, None) if e["conflict"])
        prev = [jload(d["decision"]) for d in s.q(
            "SELECT decision FROM convergence_decisions WHERE project_id = ? AND round_number < ?", (self.pid, t))]
        decider = st["convergence"].get("decider_agent")
        if decider not in members:
            decider = None  # decider absent / failed / disabled: fall back to the all-agents policy
        d = evaluate(st["convergence"], t, members, assess, outs(t), outs(t - 1), conflicts, prev,
                     decider=decider, decider_name=self.agent_names().get(decider, "") if decider else "")
        s.ex("INSERT INTO convergence_decisions(project_id, round_number, decision, created_at) VALUES (?,?,?,?)",
             (self.pid, t, jdump(d), now()))
        s.event(self.pid, "convergence", round=t, converged=d["converged"])
        return d

    async def _maybe_summarize(self, t: int, st: dict) -> None:
        """Summarize the round that just left the exact-history window (stored separately, never replacing it)."""
        cs = st["context"]
        r = t - int(cs["recent_rounds_full"])
        if not cs.get("summarize_older") or r < 1:
            return
        if self.s.one("SELECT id FROM round_summaries WHERE project_id = ? AND round_number = ?", (self.pid, r)):
            return
        outs = self.s.q("SELECT agent_id, content FROM messages WHERE project_id = ? AND kind = 'agent_output' AND "
                        "round_number = ? AND status = 'final' ORDER BY id", (self.pid, r))
        if not outs:
            return
        first = self.s.one("SELECT agent_config FROM agent_runs WHERE project_id = ? AND round_number = ? "
                           "ORDER BY position LIMIT 1", (self.pid, r))
        fc = jload(first["agent_config"]) if first else {}
        prov_name = cs.get("summarizer_provider") or fc.get("provider")
        model = cs.get("summarizer_model") or fc.get("model")
        provider = self.e.providers.get(prov_name or "")
        if not provider or not model:
            return
        names = self.agent_names()
        body = "\n\n".join(f"### {names.get(o['agent_id'], o['agent_id'])}\n{strip_assessment(o['content'])}"
                            for o in outs)
        system = ("You compress research-discussion rounds for later context. Summarize faithfully and compactly. "
                  "Preserve: each agent's key claims and who made them, equations exactly (LaTeX), objections and "
                  "disagreements, open questions, sources cited (as given), and experimental results. Do not add "
                  "claims, do not resolve disagreements, do not upgrade conjectures to results.")
        try:
            resp = await with_retries(lambda: provider.complete(
                model=model, system=system, messages=[{"role": "user", "content": f"Round {r}:\n\n{body}"}],
                tools=[], max_tokens=2000), base_delay=self.e.retry_base_delay)
        except Exception as e:
            self.s.event(self.pid, "summary_failed", round=r, error=redact(str(e), self.e.secrets)[:300])
            return
        self.s.ex("INSERT INTO round_summaries(project_id, round_number, summary, model, tokens_in, tokens_out, cost, "
                  "created_at) VALUES (?,?,?,?,?,?,?,?)",
                  (self.pid, r, resp.text, model, resp.tokens_in, resp.tokens_out,
                   self.e.pricing.cost(model, resp.tokens_in, resp.tokens_out), now()))
        self.s.event(self.pid, "summary", round=r)

    # ------------------------------------------------------------ recovery of incomplete rounds

    async def _recover_round(self, round_id: int, skip: bool) -> None:
        try:
            rnd = self.s.one("SELECT * FROM rounds WHERE id = ?", (round_id,))
            failed = [r for r in self._current_runs(round_id) if r["status"] != "succeeded"]
            if skip:
                self._finalize(round_id, skip=failed)
            else:
                if failed:
                    await self._retry(round_id, failed)
                still = [r for r in self._current_runs(round_id) if r["status"] != "succeeded"]
                if self.stop_requested:
                    await self._settle(round_id, self.settings())
                    return
                if still:
                    self.s.ex("UPDATE rounds SET status = 'failed' WHERE id = ?", (round_id,))
                    names = ", ".join(jload(r["agent_config"])["name"] for r in still)
                    self.set_state("needs_attention", f"Round {rnd['number']}: retry failed again for {names}.")
                    return
                self._finalize(round_id, skip=[])
            if not self._post_round(rnd["number"], single=True):
                self.set_state("paused", f"round {rnd['number']} recovered")
            elif self.synthesis_pending:
                await self._run_synthesis()
        except Exception as e:
            log.exception("recovery crashed")
            self.set_state("needs_attention", f"Engine error during recovery: {type(e).__name__}: {e}")

    def _discard_round(self, rnd: dict) -> None:
        s = self.s
        with s.tx():
            runs = s.q("SELECT id FROM agent_runs WHERE round_id = ?", (rnd["id"],))
            ids = [r["id"] for r in runs]
            if ids:
                marks = ",".join("?" * len(ids))
                s.ex(f"UPDATE ws_proposals SET status = 'discarded' WHERE agent_run_id IN ({marks}) AND "
                     "status = 'staged'", ids)
                s.ex(f"UPDATE memory_versions SET status = 'discarded' WHERE agent_run_id IN ({marks}) AND "
                     "status = 'staged'", ids)
                s.ex(f"UPDATE messages SET status = 'discarded' WHERE agent_run_id IN ({marks})", ids)
            # interventions delivered to the discarded round go back to pending: nothing is lost
            s.ex("UPDATE messages SET status = 'pending', visible_from_round = NULL WHERE project_id = ? AND "
                 "status = 'incorporated' AND visible_from_round = ?", (self.pid, rnd["number"]))
            s.ex("UPDATE rounds SET status = 'discarded', finished_at = ? WHERE id = ?", (now(), rnd["id"]))
            self.set_state("paused", f"round {rnd['number']} discarded (its record is kept); it will be re-run "
                                     "from a fresh snapshot when you resume")
        archive_staging(self.paths, rnd["number"], s.q("SELECT id, agent_id FROM agent_runs WHERE round_id = ?",
                                                        (rnd["id"],)))
        cleanup_staging(self.paths, rnd["number"])

    def _abort_running_round(self, note: str) -> None:
        with self.s.tx():
            for r in self.s.q("SELECT id FROM rounds WHERE project_id = ? AND status = 'running'", (self.pid,)):
                self.s.ex("UPDATE agent_runs SET status = 'failed', error = ? WHERE round_id = ? AND status IN "
                          "('pending','running')", (note, r["id"]))
                self.s.ex("UPDATE rounds SET status = 'failed', note = ? WHERE id = ?", (note, r["id"]))


def _clean(messages: list[dict]) -> list[dict]:
    """Transcript copy for storage/inspection (drops bulky provider-raw payloads)."""
    return [{k: v for k, v in m.items() if k != "_raw"} for m in messages]
