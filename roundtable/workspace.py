"""Shared research workspace: structured entries + a shared file tree.

Synchronization rule: nothing an agent does during round t is visible to any
other agent before round t+1.

* Structured entries: agents stage *proposals*; ``commit_round`` turns them into
  versions at the round boundary. Concurrent non-append edits to the same key
  are all kept as conflicting versions (never silently overwritten).
* Files: the ``shared/`` tree is read-only during a round. Each agent run gets a
  private working copy (``staging/r<t>/run<id>/``), created lazily on its first
  write/exec. At the boundary the copies are diffed against ``shared/`` and
  changes are applied; if two agents change the same path, both versions are
  kept as ``<path>.conflict-r<t>-<agent>`` and the conflict is recorded.
"""
from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

from .db import Store, now

WS_KINDS = [
    "research_question", "hypothesis", "derivation", "literature_note", "experiment_proposal",
    "citation", "code", "result", "open_question", "rejected_hypothesis", "note",
]
MAX_FILE_COMMIT_BYTES = 50 * 1024 * 1024
MAX_WORKDIR_COPY_BYTES = 500 * 1024 * 1024


class ProjectPaths:
    def __init__(self, data_dir: Path, project_id: int):
        self.root = data_dir / "projects" / str(project_id)
        self.shared = self.root / "shared"
        self.private_root = self.root / "private"
        self.uploads = self.root / "uploads"
        self.staging = self.root / "staging"
        self.cache = self.root / "cache"
        self.archive = self.root / "archive"
        for p in (self.shared, self.private_root, self.uploads, self.staging, self.cache, self.archive):
            p.mkdir(parents=True, exist_ok=True)

    def private(self, agent_id: int) -> Path:
        p = self.private_root / str(agent_id)
        p.mkdir(parents=True, exist_ok=True)
        return p

    def workdir(self, round_number: int, run_id: int) -> Path:
        return self.staging / f"r{round_number}" / f"run{run_id}"

    def history(self, round_number: int, run_id: int) -> Path:
        """Snapshot of the files a run committed, as they were at that round."""
        return self.root / "history" / f"r{round_number}" / f"run{run_id}"

    def archived(self, round_number: int, run_id: int) -> Path:
        return self.archive / f"r{round_number}" / f"run{run_id}"


def safe_join(base: Path, rel: str) -> Path:
    """Resolve ``rel`` under ``base``; refuse escapes (.., absolute paths, symlinks out)."""
    rel = (rel or "").strip().lstrip("/")
    p = (base / rel).resolve()
    b = base.resolve()
    if p != b and b not in p.parents:
        raise PermissionError(f"path escapes the workspace: {rel!r}")
    return p


def _hash(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _tree_size(p: Path) -> int:
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file() and not f.is_symlink())


def ensure_workdir(paths: ProjectPaths, round_number: int, run_id: int, agent_id: int) -> Path:
    """Create (once) the agent run's copy of the shared tree + its private files."""
    wd = paths.workdir(round_number, run_id)
    if wd.exists():
        return wd
    if _tree_size(paths.shared) > MAX_WORKDIR_COPY_BYTES:
        raise RuntimeError("shared workspace too large to stage (>500MB); move large data out of it")
    tmp = wd.with_name(wd.name + ".tmp")
    if tmp.exists():
        shutil.rmtree(tmp)
    shutil.copytree(paths.shared, tmp, symlinks=False)
    priv = paths.private(agent_id)
    if any(priv.iterdir()):
        shutil.copytree(priv, tmp / "private", symlinks=False)
    tmp.rename(wd)
    return wd


def _changed_files(workdir: Path, base: Path, skip_prefix: str | None = None) -> list[tuple[str, Path]]:
    out = []
    for f in sorted(workdir.rglob("*")):
        if not f.is_file() or f.is_symlink():
            continue
        rel = f.relative_to(workdir).as_posix()
        if skip_prefix and (rel == skip_prefix or rel.startswith(skip_prefix + "/")):
            continue
        if rel.startswith(".exec/") or "/__pycache__/" in f"/{rel}" or rel.startswith(".cache/"):
            continue
        orig = base / rel
        if orig.is_file() and orig.stat().st_size == f.stat().st_size and _hash(orig) == _hash(f):
            continue
        out.append((rel, f))
    return out


def _snapshot(paths: ProjectPaths, round_number: int, run_id: int, rel: str, src: Path) -> None:
    dst = paths.history(round_number, run_id) / rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def commit_files(store: Store, paths: ProjectPaths, project_id: int, round_number: int,
                 runs: list[dict], agent_names: dict[int, str]) -> list[dict]:
    """Apply every succeeded run's file changes to shared/ (deterministic agent order)."""
    changes: dict[str, list[tuple[dict, Path]]] = {}
    for run in sorted(runs, key=lambda r: r["position"]):
        wd = paths.workdir(round_number, run["id"])
        if not wd.exists():
            continue
        # private files go back to the agent's own private area (no conflicts possible)
        if (wd / "private").is_dir():
            priv = paths.private(run["agent_id"])
            for rel, f in _changed_files(wd / "private", priv):
                dst = safe_join(priv, rel)
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, dst)
        for rel, f in _changed_files(wd, paths.shared, skip_prefix="private"):
            changes.setdefault(rel, []).append((run, f))
    log = []
    for rel, items in sorted(changes.items()):
        if len(items) == 1:
            run, f = items[0]
            if f.stat().st_size > MAX_FILE_COMMIT_BYTES:
                log.append(dict(path=rel, action="skipped_too_large", run=run, conflict=0))
                continue
            dst = safe_join(paths.shared, rel)
            action = "modified" if dst.exists() else "created"
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, dst)
            _snapshot(paths, round_number, run["id"], rel, f)
            log.append(dict(path=rel, action=action, run=run, conflict=0))
        else:
            for run, f in items:
                name = "".join(c if c.isalnum() else "_" for c in agent_names.get(run["agent_id"], str(run["agent_id"])))
                alt = f"{rel}.conflict-r{round_number}-{name}"
                dst = safe_join(paths.shared, alt)
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, dst)
                _snapshot(paths, round_number, run["id"], alt, f)
                log.append(dict(path=rel, action=f"conflict_variant:{alt}", run=run, conflict=1))
    for e in log:
        store.ex(
            "INSERT INTO file_changes(project_id, path, action, agent_id, agent_run_id, round_number, conflict, created_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (project_id, e["path"], e["action"], e["run"]["agent_id"], e["run"]["id"], round_number, e["conflict"], now()),
        )
    return log


def staged_changes(paths: ProjectPaths, round_number: int, run_id: int, agent_id: int) -> list[tuple[str, Path, str]]:
    """Files a run has created/modified in its working copy (not yet shared): (path, file, new|modified)."""
    wd = paths.workdir(round_number, run_id)
    if not wd.exists():
        return []
    out = []
    for rel, f in _changed_files(wd, paths.shared, skip_prefix="private"):
        out.append((rel, f, "modified" if (paths.shared / rel).is_file() else "new"))
    if (wd / "private").is_dir():
        priv = paths.private(agent_id)
        for rel, f in _changed_files(wd / "private", priv):
            out.append(("private/" + rel, f, "modified" if (priv / rel).is_file() else "new"))
    return out


def archive_staging(paths: ProjectPaths, round_number: int, runs: list[dict]) -> None:
    """Keep the changed files of runs whose work is NOT committed (failed, skipped, discarded), then drop the copy."""
    for run in runs:
        changes = staged_changes(paths, round_number, run["id"], run["agent_id"])
        if changes:
            dst = paths.archived(round_number, run["id"])
            for rel, f, _ in changes:
                t = dst / rel
                t.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, t)
    cleanup_staging(paths, round_number, [r["id"] for r in runs])


def cleanup_staging(paths: ProjectPaths, round_number: int, run_ids: list[int] | None = None) -> None:
    d = paths.staging / f"r{round_number}"
    if run_ids is None:
        shutil.rmtree(d, ignore_errors=True)
    else:
        for rid in run_ids:
            shutil.rmtree(d / f"run{rid}", ignore_errors=True)
            shutil.rmtree(d / f"run{rid}.tmp", ignore_errors=True)


def list_tree(base: Path, limit: int = 500) -> list[dict]:
    out = []
    if not base.exists():
        return out
    for f in sorted(base.rglob("*")):
        if f.is_file() and not f.is_symlink():
            rel = f.relative_to(base).as_posix()
            if rel.startswith(".exec/") or "__pycache__" in rel or rel.startswith(".cache/"):
                continue
            out.append({"path": rel, "size": f.stat().st_size})
            if len(out) >= limit:
                break
    return out


# ------------------------------------------------------------ structured entries

def snapshot_entries(store: Store, project_id: int, round_number: int | None) -> list[dict]:
    """Entries as seen by round ``round_number`` (None = latest committed state).

    Each entry: key, kind, title, status, version, content, provenance; if the head
    version has several rows it is a conflict and ``variants`` lists all of them.
    """
    if round_number is None:
        rows = store.q(
            "SELECT v.*, e.key FROM ws_versions v JOIN ws_entries e ON e.id = v.entry_id "
            "WHERE v.project_id = ? AND v.visible_from_round IS NOT NULL ORDER BY v.id", (project_id,))
    else:
        rows = store.q(
            "SELECT v.*, e.key FROM ws_versions v JOIN ws_entries e ON e.id = v.entry_id "
            "WHERE v.project_id = ? AND v.visible_from_round IS NOT NULL AND v.visible_from_round <= ? ORDER BY v.id",
            (project_id, round_number))
    by_key: dict[str, list[dict]] = {}
    for r in rows:
        by_key.setdefault(r["key"], []).append(r)
    out = []
    for key, vs in by_key.items():
        head = max(v["version"] for v in vs)
        heads = [v for v in vs if v["version"] == head]
        h = heads[-1]
        out.append({
            "key": key, "kind": h["kind"], "title": h["title"], "status": h["status"], "version": head,
            "content": h["content"], "author_type": h["author_type"], "agent_id": h["agent_id"],
            "round_number": h["round_number"], "created_at": h["created_at"],
            "conflict": len(heads) > 1,
            "variants": heads if len(heads) > 1 else [],
        })
    out.sort(key=lambda e: (WS_KINDS.index(e["kind"]) if e["kind"] in WS_KINDS else 99, e["key"]))
    return out


def _head(store: Store, entry_id: int) -> tuple[int, list[dict]]:
    rows = store.q("SELECT * FROM ws_versions WHERE entry_id = ? AND visible_from_round IS NOT NULL", (entry_id,))
    if not rows:
        return 0, []
    v = max(r["version"] for r in rows)
    return v, [r for r in rows if r["version"] == v]


def _entry_id(store: Store, project_id: int, key: str) -> int:
    e = store.one("SELECT id FROM ws_entries WHERE project_id = ? AND key = ?", (project_id, key))
    if e:
        return e["id"]
    return store.ex("INSERT INTO ws_entries(project_id, key, created_at) VALUES (?,?,?)", (project_id, key, now()))


def _apply_ops(base: dict | None, ops: list[dict]) -> dict:
    """Apply one agent's ops on a key sequentially, returning the resulting fields."""
    cur = dict(base) if base else {"kind": "note", "title": "", "content": "", "status": "active"}
    for p in ops:
        if p["kind"]:
            cur["kind"] = p["kind"]
        if p["title"]:
            cur["title"] = p["title"]
        if p["op"] == "upsert":
            cur["content"] = p["content"] or ""
        elif p["op"] == "append":
            add = p["content"] or ""
            cur["content"] = (cur["content"] + "\n\n" if cur["content"] else "") + add
        elif p["op"] == "set_status":
            cur["status"] = (p["content"] or "active").strip()
    if not cur["title"]:
        cur["title"] = ""
    return cur


def commit_proposals(store: Store, project_id: int, round_number: int, run_ids: list[int],
                     positions: dict[int, int], agent_names: dict[int, str]) -> list[dict]:
    """Turn staged proposals from succeeded runs into versions visible from round+1."""
    if not run_ids:
        return []
    marks = ",".join("?" * len(run_ids))
    props = store.q(
        f"SELECT * FROM ws_proposals WHERE project_id = ? AND status = 'staged' AND agent_run_id IN ({marks}) ORDER BY id",
        [project_id, *run_ids])
    by_key: dict[str, dict[int, list[dict]]] = {}
    for p in props:
        by_key.setdefault(p["key"], {}).setdefault(p["agent_id"], []).append(p)
    report = []
    vis = round_number + 1
    for key, per_agent in sorted(by_key.items()):
        eid = _entry_id(store, project_id, key)
        head_v, heads = _head(store, eid)
        base = None
        if heads:
            h = heads[-1]
            base = {"kind": h["kind"], "title": h["title"], "content": h["content"], "status": h["status"]}
        agents = sorted(per_agent, key=lambda a: positions.get(a, 0))
        all_append = all(p["op"] == "append" for a in agents for p in per_agent[a])
        conflict = len(agents) > 1 and not all_append
        if conflict:
            for a in agents:
                res = _apply_ops(base, per_agent[a])
                _insert_version(store, project_id, eid, head_v + 1, res, a, per_agent[a][-1]["agent_run_id"],
                                round_number, vis, conflict=1)
            report.append({"key": key, "conflict": True, "agents": agents})
        else:
            # sequential application (single agent, or several agents only appending)
            cur = base
            for a in agents:
                ops = per_agent[a]
                if len(agents) > 1:
                    ops = [dict(p, content=f"*[{agent_names.get(a, a)}, round {round_number}]* {p['content'] or ''}")
                           for p in ops]
                cur = _apply_ops(cur, ops)
            last = per_agent[agents[-1]][-1]
            _insert_version(store, project_id, eid, head_v + 1, cur, agents[-1] if len(agents) == 1 else None,
                            last["agent_run_id"] if len(agents) == 1 else None, round_number, vis, conflict=0,
                            author_type="agent" if len(agents) == 1 else "agents")
            report.append({"key": key, "conflict": False, "agents": agents})
    store.ex(
        f"UPDATE ws_proposals SET status = 'committed' WHERE project_id = ? AND status = 'staged' AND agent_run_id IN ({marks})",
        [project_id, *run_ids])
    return report


def _insert_version(store, project_id, entry_id, version, fields, agent_id, run_id, round_number, vis, conflict,
                    author_type="agent"):
    store.ex(
        "INSERT INTO ws_versions(project_id, entry_id, version, kind, title, content, status, conflict, author_type,"
        " agent_id, agent_run_id, round_number, visible_from_round, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (project_id, entry_id, version, fields["kind"], fields["title"] or "", fields["content"], fields["status"],
         conflict, author_type, agent_id, run_id, round_number, vis, now()))


def user_edit(store: Store, project_id: int, key: str, kind: str, title: str, content: str, status: str) -> int:
    """User edits are pending (version assigned) until the next snapshot freeze."""
    eid = _entry_id(store, project_id, key)
    return store.ex(
        "INSERT INTO ws_versions(project_id, entry_id, version, kind, title, content, status, conflict, author_type,"
        " created_at) VALUES (?,?,NULL,?,?,?,?,0,'user',?)",
        (project_id, eid, kind, title, content, status, now()))


def incorporate_user_edits(store: Store, project_id: int, round_number: int) -> int:
    rows = store.q("SELECT * FROM ws_versions WHERE project_id = ? AND visible_from_round IS NULL "
                   "AND author_type = 'user' ORDER BY id", (project_id,))
    for r in rows:
        v, _ = _head(store, r["entry_id"])
        store.ex("UPDATE ws_versions SET version = ?, visible_from_round = ?, round_number = ? WHERE id = ?",
                 (v + 1, round_number, round_number - 1, r["id"]))
    return len(rows)
