"""Project / agent / message operations used by the HTTP API and the tests."""
from __future__ import annotations

import re
from pathlib import Path

from .config import DEFAULT_AGENT_TOOLS, TOOL_NAMES, normalize_settings
from .db import Store, jdump, jload, now
from .workspace import ProjectPaths


class ServiceError(Exception):
    pass


def create_project(store: Store, name: str, settings: dict | None = None) -> int:
    with store.tx():
        return store.ex("INSERT INTO projects(name, created_at, settings) VALUES (?,?,?)",
                        (name.strip() or "Untitled project", now(), jdump(normalize_settings(settings or {}))))


def update_settings(store: Store, project_id: int, patch: dict) -> dict:
    from .config import deep_merge
    p = store.one("SELECT settings FROM projects WHERE id = ?", (project_id,))
    if not p:
        raise ServiceError("no such project")
    s = normalize_settings(deep_merge(jload(p["settings"], {}), patch))
    store.ex("UPDATE projects SET settings = ? WHERE id = ?", (jdump(s), project_id))
    store.event(project_id, "settings_changed")
    return s


def running_round(store: Store, project_id: int) -> int | None:
    return store.val("SELECT number FROM rounds WHERE project_id = ? AND status = 'running'", (project_id,))


def add_agent(store: Store, project_id: int, name: str, instructions: str = "", provider: str = "mock",
              model: str = "mock", tools: list[str] | None = None, enabled: bool = True,
              temperature: float | None = None, max_tokens: int = 4096, memory_enabled: bool = False) -> int:
    tools = [t for t in (tools if tools is not None else DEFAULT_AGENT_TOOLS) if t in TOOL_NAMES]
    with store.tx():
        pos = store.val("SELECT COALESCE(MAX(position), -1) + 1 FROM agents WHERE project_id = ?", (project_id,))
        aid = store.ex(
            "INSERT INTO agents(project_id, name, position, enabled, provider, model, temperature, max_tokens, tools,"
            " memory_enabled, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (project_id, _name(store, project_id, name), pos, int(enabled), provider, model, temperature, max_tokens,
             jdump(tools), int(memory_enabled), now()))
        store.ex("INSERT INTO instruction_versions(agent_id, version, text, created_at, created_during_round) "
                 "VALUES (?,1,?,?,?)", (aid, instructions, now(), running_round(store, project_id)))
        store.event(project_id, "agent_added", agent_id=aid)
    return aid


def _name(store: Store, project_id: int, name: str, exclude: int | None = None) -> str:
    name = re.sub(r"\s+", " ", name or "").strip()[:60] or "Agent"
    taken = {a["name"] for a in store.q("SELECT name, id FROM agents WHERE project_id = ? AND deleted = 0",
                                        (project_id,)) if a["id"] != exclude}
    base, i = name, 2
    while name in taken:
        name = f"{base} {i}"
        i += 1
    return name


AGENT_FIELDS = {"name", "enabled", "provider", "model", "temperature", "max_tokens", "tools", "memory_enabled",
                "position"}


def update_agent(store: Store, agent_id: int, patch: dict) -> None:
    """Config changes take effect at the next round boundary (runs pin a config copy at freeze)."""
    a = store.one("SELECT * FROM agents WHERE id = ? AND deleted = 0", (agent_id,))
    if not a:
        raise ServiceError("no such agent")
    with store.tx():
        for k, v in patch.items():
            if k not in AGENT_FIELDS:
                continue
            if k == "name":
                v = _name(store, a["project_id"], v, exclude=agent_id)
            elif k == "tools":
                v = jdump([t for t in v if t in TOOL_NAMES])
            elif k in ("enabled", "memory_enabled"):
                v = int(bool(v))
            elif k == "max_tokens":
                v = max(16, int(v))
            store.ex(f"UPDATE agents SET {k} = ? WHERE id = ?", (v, agent_id))
        if "instructions" in patch:
            set_instructions(store, agent_id, patch["instructions"])
        store.event(a["project_id"], "agent_updated", agent_id=agent_id)


def set_instructions(store: Store, agent_id: int, text: str) -> int:
    """New instruction versions are pinned by the next round freeze, never by a running round."""
    a = store.one("SELECT project_id FROM agents WHERE id = ?", (agent_id,))
    cur = store.one("SELECT version, text FROM instruction_versions WHERE agent_id = ? ORDER BY version DESC LIMIT 1",
                    (agent_id,))
    if cur and cur["text"] == text:
        return cur["version"]
    v = (cur["version"] if cur else 0) + 1
    store.ex("INSERT INTO instruction_versions(agent_id, version, text, created_at, created_during_round) "
             "VALUES (?,?,?,?,?)", (agent_id, v, text, now(), running_round(store, a["project_id"])))
    store.event(a["project_id"], "instructions_changed", agent_id=agent_id, version=v)
    return v


def remove_agent(store: Store, agent_id: int) -> None:
    """Soft delete: history (runs, outputs, instruction versions) is preserved."""
    a = store.one("SELECT project_id FROM agents WHERE id = ?", (agent_id,))
    if not a:
        raise ServiceError("no such agent")
    store.ex("UPDATE agents SET deleted = 1, enabled = 0 WHERE id = ?", (agent_id,))
    store.event(a["project_id"], "agent_removed", agent_id=agent_id)


def _check_channel(store: Store, project_id: int, channel: str) -> None:
    if channel == "common":
        return
    m = re.fullmatch(r"agent:(\d+)", channel)
    if not m or not store.one("SELECT id FROM agents WHERE id = ? AND project_id = ? AND deleted = 0",
                              (int(m.group(1)), project_id)):
        raise ServiceError("invalid channel")


def set_task(store: Store, project_id: int, content: str) -> int:
    if store.one("SELECT id FROM rounds WHERE project_id = ? AND status != 'discarded'", (project_id,)):
        raise ServiceError("the initial task is fixed once round 1 has started; post a message to the common "
                           "channel instead")
    with store.tx():
        store.ex("DELETE FROM messages WHERE project_id = ? AND kind = 'task'", (project_id,))
        mid = store.ex("INSERT INTO messages(project_id, channel, kind, round_number, content, status, created_at) "
                       "VALUES (?, 'common', 'task', 0, ?, 'pending', ?)", (project_id, content, now()))
        store.event(project_id, "task_set")
    return mid


def post_message(store: Store, project_id: int, channel: str, content: str, file_ids: list[int] | None = None) -> int:
    """User intervention. Always enters the pending queue; delivered at the next round freeze."""
    _check_channel(store, project_id, channel)
    if not content.strip() and not file_ids:
        raise ServiceError("empty message")
    with store.tx():
        atts = []
        for fid in file_ids or []:
            f = store.one("SELECT * FROM files WHERE id = ? AND project_id = ?", (fid, project_id))
            if f:
                atts.append({"file_id": f["id"], "filename": f["filename"],
                             "workspace_path": ("uploads/" if channel == "common" else "private/uploads/") + f["filename"]})
        mid = store.ex(
            "INSERT INTO messages(project_id, channel, kind, content, attachments, status, submitted_during_round, "
            "created_at) VALUES (?,?, 'user', ?,?, 'pending', ?, ?)",
            (project_id, channel, content, jdump(atts), running_round(store, project_id), now()))
        for a in atts:
            store.ex("UPDATE files SET message_id = ? WHERE id = ?", (mid, a["file_id"]))
        store.event(project_id, "message", channel=channel)
    return mid


def save_upload(store: Store, data_dir: Path, project_id: int, channel: str, filename: str, data: bytes) -> int:
    _check_channel(store, project_id, channel)
    paths = ProjectPaths(data_dir, project_id)
    safe = re.sub(r"[^\w.\-]+", "_", Path(filename).name).strip("._") or "file"
    # keep names unique inside uploads/
    existing = {f["filename"] for f in store.q("SELECT filename FROM files WHERE project_id = ? AND channel = ?",
                                                (project_id, channel))}
    stem, suf, i = Path(safe).stem, Path(safe).suffix, 2
    while safe in existing:
        safe = f"{stem}_{i}{suf}"
        i += 1
    with store.tx():
        fid = store.ex("INSERT INTO files(project_id, channel, filename, stored_path, size, status, created_at) "
                       "VALUES (?,?,?,?,?, 'pending', ?)", (project_id, channel, safe, "", len(data), now()))
        dest = paths.uploads / f"{fid}_{safe}"
        dest.write_bytes(data)
        store.ex("UPDATE files SET stored_path = ? WHERE id = ?", (str(dest), fid))
    return fid
