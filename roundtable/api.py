"""HTTP API (FastAPI). All routes are async so database access stays on the event loop."""
from __future__ import annotations

import json
import shutil
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import service
from .config import DATA_DIR, DEFAULT_SETTINGS, PROVIDER_ENV, ROOT, TOOL_NAMES, Pricing, Secrets, normalize_settings, redact
from .db import Store, jload
from .engine import Engine, EngineError
from .providers import PROVIDER_INFO, build_providers
from .tools.sandbox import Sandbox
from .workspace import WS_KINDS, ProjectPaths, list_tree, safe_join, snapshot_entries, staged_changes

DEFAULT_MODEL = "gemini-3.5-flash-lite"
MAX_UPLOAD = 100 * 1024 * 1024


def run_files(paths: ProjectPaths, rnd: dict, run: dict, committed: list[dict]) -> list[dict]:
    """Every file a run produced, wherever it currently lives."""
    out = [{"path": c["action"].split(":", 1)[1] if c["action"].startswith("conflict_variant:") else c["path"],
            "status": "conflict" if c["conflict"] else c["action"], "where": "committed"} for c in committed]
    if rnd["status"] != "completed" or run["status"] in ("running", "pending"):
        out += [{"path": rel, "status": st, "where": "staged", "size": f.stat().st_size}
                for rel, f, st in staged_changes(paths, rnd["number"], run["id"], run["agent_id"])]
    arch = paths.archived(rnd["number"], run["id"])
    if arch.is_dir():
        out += [{"path": f["path"], "status": "not committed", "where": "archived", "size": f["size"]}
                for f in list_tree(arch, 300)]
    return out


def create_app(data_dir: Path = DATA_DIR, providers=None, serve_frontend: bool = True) -> FastAPI:
    data_dir.mkdir(parents=True, exist_ok=True)
    secrets = Secrets(data_dir)
    pricing = Pricing(data_dir)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        store = Store(data_dir / "roundtable.sqlite3")
        engine = Engine(store, data_dir, providers or build_providers(secrets), secrets, pricing, Sandbox())
        engine.recover()
        app.state.store, app.state.engine = store, engine
        yield
        await engine.shutdown()
        store.close()

    app = FastAPI(title="Roundtable", lifespan=lifespan)

    @app.exception_handler(EngineError)
    @app.exception_handler(service.ServiceError)
    async def _user_error(request: Request, exc: Exception):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    def S(request: Request) -> Store:
        return request.app.state.store

    def E(request: Request) -> Engine:
        return request.app.state.engine

    def project_or_404(store: Store, pid: int) -> dict:
        p = store.one("SELECT * FROM projects WHERE id = ?", (pid,))
        if not p:
            raise HTTPException(404, "no such project")
        return p

    # ------------------------------------------------------------ meta / config

    @app.get("/api/meta")
    async def meta(request: Request):
        return {"providers": PROVIDER_INFO, "tools": TOOL_NAMES, "workspace_kinds": WS_KINDS,
                "default_settings": DEFAULT_SETTINGS, "sandbox": E(request).sandbox.describe(),
                "secrets": secrets.status()}

    @app.get("/api/models/google")
    async def google_models():
        """Text-generation models the configured Google AI Studio key can use (live list)."""
        key = secrets.get("google")
        if not key:
            return []
        import httpx
        try:
            async with httpx.AsyncClient(timeout=20) as c:
                r = await c.get("https://generativelanguage.googleapis.com/v1beta/models", params={"pageSize": 500},
                                headers={"x-goog-api-key": key})
            r.raise_for_status()
        except httpx.HTTPError:
            return []
        skip = ("tts", "image", "banana", "lyria", "robotics", "computer-use", "transcribe", "deep-research",
                "antigravity", "customtools", "omni")
        out = []
        for m in r.json().get("models", []):
            name = m["name"].removeprefix("models/")
            if "generateContent" in m.get("supportedGenerationMethods", []) and not any(k in name for k in skip):
                out.append({"id": name, "label": m.get("displayName", name), "input_limit": m.get("inputTokenLimit")})
        return out

    @app.post("/api/models/check")
    async def check_model(request: Request, body: dict):
        """One tiny request to see whether a model is usable with the configured key right now."""
        provider = E(request).providers.get(body.get("provider", "google"))
        if provider is None:
            raise HTTPException(400, "unknown provider")
        try:
            await provider.complete(model=body.get("model", ""), system="Reply with OK.",
                                    messages=[{"role": "user", "content": "ping"}], tools=[], max_tokens=64)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": redact(str(e), secrets)[:400]}

    @app.get("/api/secrets")
    async def get_secrets():
        return secrets.status()

    @app.put("/api/secrets")
    async def put_secrets(body: dict):
        provider = body.get("provider")
        if provider not in PROVIDER_ENV:
            raise HTTPException(400, f"provider must be one of {sorted(PROVIDER_ENV)}")
        secrets.set(provider, body.get("key") or None)
        return secrets.status()

    @app.get("/api/pricing")
    async def get_pricing():
        return pricing.table()

    @app.put("/api/pricing")
    async def put_pricing(body: dict):
        models = {}
        for k, v in body.items():
            try:
                models[str(k)] = {"input": float(v["input"]), "output": float(v["output"])}
            except (KeyError, TypeError, ValueError):
                raise HTTPException(400, f"invalid pricing entry for {k}")
        pricing.save(models)
        return pricing.table()

    # ------------------------------------------------------------ projects

    @app.get("/api/projects")
    async def list_projects(request: Request):
        return S(request).q("SELECT id, name, created_at, state, state_reason FROM projects ORDER BY id DESC")

    @app.post("/api/projects")
    async def new_project(request: Request, body: dict):
        pid = service.create_project(S(request), body.get("name", ""), body.get("settings"))
        return {"id": pid}

    @app.get("/api/project")
    async def the_project(request: Request):
        """The app works on a single project: return it, creating it on first use."""
        store = S(request)
        p = store.one("SELECT id FROM projects ORDER BY id LIMIT 1")
        return {"id": p["id"] if p else service.create_project(store, "Research project")}

    @app.post("/api/projects/{pid}/setup")
    async def setup(request: Request, pid: int, body: dict):
        """Initial setup: task + agents (replaces any agents defined so far). Only before round 1."""
        store = S(request)
        project_or_404(store, pid)
        if store.one("SELECT id FROM rounds WHERE project_id = ? AND status != 'discarded'", (pid,)):
            raise HTTPException(400, "setup is fixed once round 1 has started; use Start over in Settings")
        agents = body.get("agents") or []
        if not agents:
            raise HTTPException(400, "define at least one agent")
        if not (body.get("task") or "").strip():
            raise HTTPException(400, "the task is empty")
        with store.tx():
            store.ex("DELETE FROM agents WHERE project_id = ?", (pid,))
            settings = dict(body.get("settings") or {})
            settings.setdefault("synthesis_agent", None)
            settings.setdefault("convergence", {}).setdefault("decider_agent", None)
            for i, a in enumerate(agents):
                aid = service.add_agent(store, pid, a.get("name") or f"Agent {i + 1}", a.get("instructions", ""),
                                        provider=a.get("provider", "google"), model=a.get("model") or DEFAULT_MODEL,
                                        tools=a.get("tools"), max_tokens=int(a.get("max_tokens", 8192)))
                if a.get("decider"):
                    settings["convergence"]["decider_agent"] = aid
                if a.get("synthesis"):
                    settings["synthesis_agent"] = aid
            service.set_task(store, pid, body["task"])
            service.update_settings(store, pid, settings)
        return {"ok": True}

    @app.patch("/api/projects/{pid}")
    async def patch_project(request: Request, pid: int, body: dict):
        store = S(request)
        project_or_404(store, pid)
        if "name" in body:
            store.ex("UPDATE projects SET name = ? WHERE id = ?", (str(body["name"]).strip() or "Untitled", pid))
            store.bump(pid)
        if "settings" in body:
            service.update_settings(store, pid, body["settings"])
        return {"ok": True}

    @app.delete("/api/projects/{pid}")
    async def delete_project(request: Request, pid: int):
        store, engine = S(request), E(request)
        project_or_404(store, pid)
        if pid in engine.runners and engine.runners[pid].busy:
            raise HTTPException(400, "stop the project before deleting it")
        with store.tx():
            for t in ("tool_calls", "ws_proposals", "ws_versions", "memory_versions", "sources", "file_changes",
                      "convergence_assessments", "convergence_decisions", "round_summaries", "events", "agent_runs"):
                store.ex(f"DELETE FROM {t} WHERE project_id = ?", (pid,))
            store.ex("DELETE FROM projects WHERE id = ?", (pid,))
        engine.runners.pop(pid, None)
        shutil.rmtree(data_dir / "projects" / str(pid), ignore_errors=True)
        return {"ok": True}

    @app.get("/api/projects/{pid}/seq")
    async def seq(request: Request, pid: int):
        p = project_or_404(S(request), pid)
        return {"seq": p["seq"]}

    @app.get("/api/projects/{pid}/state")
    async def state(request: Request, pid: int):
        store, engine = S(request), E(request)
        p = project_or_404(store, pid)
        runner = engine.runner(pid)
        agents = store.q("SELECT * FROM agents WHERE project_id = ? AND deleted = 0 ORDER BY position, id", (pid,))
        for a in agents:
            a["tools"] = jload(a["tools"], [])
            iv = store.one("SELECT version, text, created_at FROM instruction_versions WHERE agent_id = ? "
                           "ORDER BY version DESC LIMIT 1", (a["id"],))
            a["instructions"] = iv["text"] if iv else ""
            a["instruction_version"] = iv["version"] if iv else 0
            a["pending_messages"] = store.val("SELECT COUNT(*) FROM messages WHERE channel = ? AND status = 'pending'",
                                              (f"agent:{a['id']}",))
        names = {a["id"]: a["name"] for a in store.q("SELECT id, name FROM agents WHERE project_id = ?", (pid,))}
        rounds = store.q("SELECT id, number, kind, status, members, started_at, finished_at, note FROM rounds "
                         "WHERE project_id = ? ORDER BY number, id", (pid,))
        runs = store.q("SELECT id, round_id, agent_id, position, attempt, superseded, status, error, tokens_in, "
                       "tokens_out, cost, started_at, finished_at, instruction_version_id FROM agent_runs "
                       "WHERE project_id = ? ORDER BY round_id, position, attempt", (pid,))
        paths = ProjectPaths(data_dir, pid)
        committed: dict[int, list] = {}
        for c in store.q("SELECT agent_run_id, path, action, conflict FROM file_changes WHERE project_id = ? ORDER BY id",
                         (pid,)):
            committed.setdefault(c["agent_run_id"], []).append(c)
        for r in rounds:
            r["members"] = jload(r["members"], [])
            r["runs"] = [dict(x, agent_name=names.get(x["agent_id"], "?"),
                              files=run_files(paths, r, x, committed.get(x["id"], [])))
                         for x in runs if x["round_id"] == r["id"]]
        return {
            "project": {"id": p["id"], "name": p["name"], "state": p["state"], "state_reason": p["state_reason"],
                        "seq": p["seq"], "settings": normalize_settings(jload(p["settings"], {}))},
            "busy": runner.busy,
            "agents": agents,
            "agent_names": names,
            "rounds": rounds,
            "last_completed": runner.last_completed(),
            "running_round": runner.running_round_number(),
            "incomplete_round": runner.incomplete_round(),
            "pending_common": store.val("SELECT COUNT(*) FROM messages WHERE project_id = ? AND channel = 'common' "
                                        "AND status = 'pending' AND kind = 'user'", (pid,)),
            "pending_files": store.val("SELECT COUNT(*) FROM files WHERE project_id = ? AND status = 'pending'", (pid,)),
            "pending_workspace": store.val("SELECT COUNT(*) FROM ws_versions WHERE project_id = ? AND "
                                           "visible_from_round IS NULL", (pid,)),
            "has_task": bool(store.one("SELECT id FROM messages WHERE project_id = ? AND kind = 'task'", (pid,))),
            "usage": runner.usage(),
        }

    @app.get("/api/runs/{rid}/file")
    async def run_file(request: Request, rid: int, path: str, where: str):
        """A file produced by a run: staged (in progress), archived (not committed) or committed (shared tree)."""
        run = S(request).one("SELECT project_id, round_number FROM agent_runs WHERE id = ?", (rid,))
        if not run:
            raise HTTPException(404, "no such run")
        paths = ProjectPaths(data_dir, run["project_id"])
        base = {"staged": paths.workdir(run["round_number"], rid),
                "archived": paths.archived(run["round_number"], rid),
                "committed": paths.shared}.get(where)
        if base is None:
            raise HTTPException(400, "where must be staged, archived or committed")
        try:
            p = safe_join(base, path)
        except PermissionError as e:
            raise HTTPException(400, str(e))
        if not p.is_file():
            raise HTTPException(404, "file no longer available here (it may have been committed, archived or "
                                     "changed since)")
        return FileResponse(p)

    @app.post("/api/projects/{pid}/control/{action}")
    async def control(request: Request, pid: int, action: str):
        project_or_404(S(request), pid)
        return await E(request).runner(pid).control(action)

    @app.get("/api/projects/{pid}/export")
    async def export(request: Request, pid: int):
        store = S(request)
        project_or_404(store, pid)
        out = {"project": store.one("SELECT * FROM projects WHERE id = ?", (pid,))}
        for t in ("agents", "rounds", "agent_runs", "messages", "files", "file_changes", "ws_versions",
                  "ws_proposals", "memory_versions", "tool_calls", "sources", "convergence_assessments",
                  "convergence_decisions", "round_summaries", "events"):
            out[t] = store.q(f"SELECT * FROM {t} WHERE project_id = ? ORDER BY id", (pid,))
        out["instruction_versions"] = store.q(
            "SELECT iv.* FROM instruction_versions iv JOIN agents a ON a.id = iv.agent_id WHERE a.project_id = ?",
            (pid,))
        return JSONResponse(out, headers={"Content-Disposition": f'attachment; filename="project-{pid}.json"'})

    # ------------------------------------------------------------ agents

    @app.post("/api/projects/{pid}/agents")
    async def add_agent(request: Request, pid: int, body: dict):
        project_or_404(S(request), pid)
        aid = service.add_agent(
            S(request), pid, body.get("name", "Agent"), body.get("instructions", ""),
            provider=body.get("provider", "google"), model=body.get("model") or DEFAULT_MODEL, tools=body.get("tools"),
            enabled=body.get("enabled", True), temperature=body.get("temperature"),
            max_tokens=int(body.get("max_tokens", 8192)), memory_enabled=body.get("memory_enabled", False))
        return {"id": aid}

    @app.patch("/api/agents/{aid}")
    async def patch_agent(request: Request, aid: int, body: dict):
        service.update_agent(S(request), aid, body)
        return {"ok": True}

    @app.delete("/api/agents/{aid}")
    async def delete_agent(request: Request, aid: int):
        service.remove_agent(S(request), aid)
        return {"ok": True}

    @app.get("/api/agents/{aid}/instructions")
    async def instruction_history(request: Request, aid: int):
        return S(request).q("SELECT * FROM instruction_versions WHERE agent_id = ? ORDER BY version DESC", (aid,))

    # ------------------------------------------------------------ messages / files

    @app.put("/api/projects/{pid}/task")
    async def put_task(request: Request, pid: int, body: dict):
        project_or_404(S(request), pid)
        service.set_task(S(request), pid, body.get("content", ""))
        return {"ok": True}

    @app.get("/api/projects/{pid}/messages")
    async def messages(request: Request, pid: int):
        rows = S(request).q("SELECT id, channel, kind, agent_id, agent_run_id, round_number, content, attachments, "
                            "status, visible_from_round, submitted_during_round, created_at FROM messages "
                            "WHERE project_id = ? ORDER BY id", (pid,))
        for r in rows:
            r["attachments"] = jload(r["attachments"], [])
        return rows

    @app.post("/api/projects/{pid}/messages")
    async def post_message(request: Request, pid: int, channel: str = Form(...), content: str = Form(""),
                           files: list[UploadFile] = File(default=[])):
        store = S(request)
        project_or_404(store, pid)
        fids = []
        for f in files:
            data = await f.read()
            if len(data) > MAX_UPLOAD:
                raise HTTPException(400, f"{f.filename} exceeds the 100MB upload limit")
            fids.append(service.save_upload(store, data_dir, pid, channel, f.filename or "file", data))
        mid = service.post_message(store, pid, channel, content, fids)
        return {"id": mid}

    @app.delete("/api/messages/{mid}")
    async def retract_message(request: Request, mid: int):
        store = S(request)
        m = store.one("SELECT * FROM messages WHERE id = ?", (mid,))
        if not m or m["status"] != "pending" or m["kind"] != "user":
            raise HTTPException(400, "only pending user messages can be retracted")
        store.ex("UPDATE messages SET status = 'retracted' WHERE id = ?", (mid,))
        store.ex("UPDATE files SET status = 'retracted' WHERE message_id = ? AND status = 'pending'", (mid,))
        store.event(m["project_id"], "message_retracted", id=mid)
        return {"ok": True}

    @app.get("/api/projects/{pid}/files")
    async def files(request: Request, pid: int):
        store = S(request)
        project_or_404(store, pid)
        paths = ProjectPaths(data_dir, pid)
        agents = store.q("SELECT id, name FROM agents WHERE project_id = ? AND deleted = 0", (pid,))
        return {
            "shared": list_tree(paths.shared, 2000),
            "private": {a["id"]: list_tree(paths.private(a["id"]), 500) for a in agents},
            "uploads": store.q("SELECT id, channel, filename, size, status, visible_from_round, workspace_path, "
                               "created_at FROM files WHERE project_id = ? ORDER BY id", (pid,)),
            "changes": store.q("SELECT * FROM file_changes WHERE project_id = ? ORDER BY id DESC LIMIT 500", (pid,)),
            "archived": [{"run_id": int(d.name[3:]), "round": int(rd.name[1:]), "files": list_tree(d, 300)}
                         for rd in sorted(paths.archive.glob("r*")) for d in sorted(rd.glob("run*"))],
        }

    @app.get("/api/projects/{pid}/files/raw")
    async def file_raw(request: Request, pid: int, path: str, scope: str = "shared"):
        paths = ProjectPaths(data_dir, pid)
        base = paths.shared if scope == "shared" else paths.private(int(scope.split(":")[1]))
        try:
            p = safe_join(base, path)
        except PermissionError as e:
            raise HTTPException(400, str(e))
        if not p.is_file():
            raise HTTPException(404, "no such file")
        return FileResponse(p)

    # ------------------------------------------------------------ inspection

    @app.get("/api/runs/{rid}")
    async def run_detail(request: Request, rid: int):
        store = S(request)
        r = store.one("SELECT * FROM agent_runs WHERE id = ?", (rid,))
        if not r:
            raise HTTPException(404, "no such run")
        r["context"] = jload(r["context"])
        r["context"].pop("settings", None)
        r["agent_config"] = jload(r["agent_config"])
        r["transcript"] = jload(r["transcript"], [])
        r["tool_calls"] = store.q("SELECT * FROM tool_calls WHERE agent_run_id = ? ORDER BY id", (rid,))
        a = store.one("SELECT assessment, parsed_ok FROM convergence_assessments WHERE agent_run_id = ?", (rid,))
        r["assessment"] = jload(a["assessment"]) if a else None
        r["proposals"] = store.q("SELECT * FROM ws_proposals WHERE agent_run_id = ? ORDER BY id", (rid,))
        return r

    @app.get("/api/projects/{pid}/workspace")
    async def workspace(request: Request, pid: int):
        store = S(request)
        names = {a["id"]: a["name"] for a in store.q("SELECT id, name FROM agents WHERE project_id = ?", (pid,))}
        pending = store.q("SELECT v.*, e.key FROM ws_versions v JOIN ws_entries e ON e.id = v.entry_id WHERE "
                          "v.project_id = ? AND v.visible_from_round IS NULL ORDER BY v.id", (pid,))
        staged = store.q("SELECT * FROM ws_proposals WHERE project_id = ? AND status = 'staged' ORDER BY id", (pid,))
        return {"entries": snapshot_entries(store, pid, None), "pending_user_edits": pending,
                "staged_proposals": staged, "agent_names": names}

    @app.get("/api/projects/{pid}/workspace/history")
    async def workspace_history(request: Request, pid: int, key: str):
        return S(request).q("SELECT v.* FROM ws_versions v JOIN ws_entries e ON e.id = v.entry_id WHERE "
                            "e.project_id = ? AND e.key = ? ORDER BY v.id", (pid, key))

    @app.post("/api/projects/{pid}/workspace")
    async def workspace_edit(request: Request, pid: int, body: dict):
        store = S(request)
        project_or_404(store, pid)
        key = (body.get("key") or "").strip().lower().replace(" ", "-")
        if not key:
            raise HTTPException(400, "key required")
        kind = body.get("kind") or "note"
        if kind not in WS_KINDS:
            raise HTTPException(400, "invalid kind")
        with store.tx():
            from .workspace import user_edit
            user_edit(store, pid, key, kind, body.get("title", ""), body.get("content", ""),
                      body.get("status", "active"))
            store.event(pid, "workspace_user_edit", key=key)
        return {"ok": True}

    @app.get("/api/projects/{pid}/sources")
    async def sources(request: Request, pid: int):
        rows = S(request).q("SELECT * FROM sources WHERE project_id = ? ORDER BY id", (pid,))
        for r in rows:
            r["meta"] = jload(r["meta"], {})
        return rows

    @app.get("/api/projects/{pid}/convergence")
    async def convergence(request: Request, pid: int):
        store = S(request)
        dec = store.q("SELECT * FROM convergence_decisions WHERE project_id = ? ORDER BY round_number", (pid,))
        ass = store.q("SELECT a.* FROM convergence_assessments a JOIN agent_runs r ON r.id = a.agent_run_id WHERE "
                      "a.project_id = ? AND r.superseded = 0 ORDER BY a.round_number, r.position", (pid,))
        return {"decisions": [dict(d, decision=jload(d["decision"])) for d in dec],
                "assessments": [dict(a, assessment=jload(a["assessment"])) for a in ass]}

    @app.get("/api/projects/{pid}/summaries")
    async def summaries(request: Request, pid: int):
        return S(request).q("SELECT * FROM round_summaries WHERE project_id = ? ORDER BY round_number", (pid,))

    @app.get("/api/projects/{pid}/events")
    async def events(request: Request, pid: int, limit: int = 200):
        rows = S(request).q("SELECT * FROM events WHERE project_id = ? ORDER BY id DESC LIMIT ?", (pid, limit))
        return [dict(r, data=jload(r["data"], {})) for r in rows]

    # ------------------------------------------------------------ frontend

    dist = ROOT / "frontend" / "dist"
    if serve_frontend and dist.is_dir():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

        @app.get("/{path:path}")
        async def spa(path: str):
            f = dist / path
            if path and f.is_file() and dist in f.resolve().parents:
                return FileResponse(f)
            return FileResponse(dist / "index.html")

    return app
