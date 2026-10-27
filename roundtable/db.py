"""SQLite persistence.

All database access happens on the asyncio event-loop thread (every API route is
``async`` and the engine runs on the loop), so writes are naturally serialized.
Multi-statement state transitions use ``Store.tx()`` (BEGIN IMMEDIATE) so a crash
can never leave a half-applied round boundary.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    settings TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'idle',
    state_reason TEXT,
    seq INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS agents (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    position INTEGER NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    temperature REAL,
    max_tokens INTEGER NOT NULL DEFAULT 4096,
    tools TEXT NOT NULL DEFAULT '[]',
    memory_enabled INTEGER NOT NULL DEFAULT 0,
    deleted INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS instruction_versions (
    id INTEGER PRIMARY KEY,
    agent_id INTEGER NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL,
    created_during_round INTEGER,
    UNIQUE(agent_id, version)
);

-- status: running | completed | failed | interrupted | aborted | discarded
CREATE TABLE IF NOT EXISTS rounds (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    number INTEGER NOT NULL,
    status TEXT NOT NULL,
    members TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    note TEXT
);
-- At most one live (non-discarded) round per number: duplicate execution is impossible.
CREATE UNIQUE INDEX IF NOT EXISTS rounds_unique_live
    ON rounds(project_id, number) WHERE status != 'discarded';

-- status: pending | running | succeeded | failed | interrupted | aborted | skipped
CREATE TABLE IF NOT EXISTS agent_runs (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL,
    round_id INTEGER NOT NULL REFERENCES rounds(id) ON DELETE CASCADE,
    round_number INTEGER NOT NULL,
    agent_id INTEGER NOT NULL,
    position INTEGER NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 1,
    superseded INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    instruction_version_id INTEGER,
    agent_config TEXT NOT NULL,
    context TEXT NOT NULL,
    transcript TEXT,
    output TEXT,
    error TEXT,
    tokens_in INTEGER NOT NULL DEFAULT 0,
    tokens_out INTEGER NOT NULL DEFAULT 0,
    cost REAL,
    started_at TEXT,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS agent_runs_round ON agent_runs(round_id);

-- channel: 'common' or 'agent:<id>'
-- kind: task | user | agent_output | system
-- status: pending | incorporated | processed | final
-- visible_from_round: first round whose frozen snapshot contains the message (NULL = not yet)
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    channel TEXT NOT NULL,
    kind TEXT NOT NULL,
    agent_id INTEGER,
    agent_run_id INTEGER,
    round_number INTEGER,
    content TEXT NOT NULL,
    attachments TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL,
    visible_from_round INTEGER,
    submitted_during_round INTEGER,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_project ON messages(project_id, channel);

CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    channel TEXT NOT NULL,
    filename TEXT NOT NULL,
    stored_path TEXT NOT NULL,
    workspace_path TEXT,
    size INTEGER NOT NULL,
    status TEXT NOT NULL,
    visible_from_round INTEGER,
    message_id INTEGER,
    created_at TEXT NOT NULL
);

-- Record of every committed (or conflicting) change to the shared file tree.
CREATE TABLE IF NOT EXISTS file_changes (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL,
    path TEXT NOT NULL,
    action TEXT NOT NULL,
    agent_id INTEGER,
    agent_run_id INTEGER,
    round_number INTEGER,
    conflict INTEGER NOT NULL DEFAULT 0,
    note TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ws_entries (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    key TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(project_id, key)
);

-- Versions are append-only. Several rows with the same (entry_id, version) = conflict.
CREATE TABLE IF NOT EXISTS ws_versions (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL,
    entry_id INTEGER NOT NULL REFERENCES ws_entries(id) ON DELETE CASCADE,
    version INTEGER,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    conflict INTEGER NOT NULL DEFAULT 0,
    author_type TEXT NOT NULL,
    agent_id INTEGER,
    agent_run_id INTEGER,
    round_number INTEGER,
    visible_from_round INTEGER,
    created_at TEXT NOT NULL
);

-- Staged agent proposals; committed only at the round boundary.
CREATE TABLE IF NOT EXISTS ws_proposals (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL,
    agent_run_id INTEGER NOT NULL,
    agent_id INTEGER NOT NULL,
    round_number INTEGER NOT NULL,
    op TEXT NOT NULL,
    key TEXT NOT NULL,
    kind TEXT,
    title TEXT,
    content TEXT,
    status TEXT NOT NULL DEFAULT 'staged',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memory_versions (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL,
    agent_id INTEGER NOT NULL,
    agent_run_id INTEGER,
    round_number INTEGER,
    content TEXT NOT NULL,
    status TEXT NOT NULL,
    visible_from_round INTEGER,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tool_calls (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL,
    agent_run_id INTEGER NOT NULL,
    agent_id INTEGER NOT NULL,
    round_number INTEGER NOT NULL,
    tool TEXT NOT NULL,
    args TEXT NOT NULL,
    result TEXT,
    status TEXT NOT NULL,
    private INTEGER NOT NULL DEFAULT 0,
    started_at TEXT NOT NULL,
    finished_at TEXT
);

-- Sources actually retrieved by tools (for honest citation tracking).
CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    ref TEXT NOT NULL,
    title TEXT,
    url TEXT,
    meta TEXT,
    agent_id INTEGER,
    round_number INTEGER,
    created_at TEXT NOT NULL,
    UNIQUE(project_id, kind, ref)
);

CREATE TABLE IF NOT EXISTS convergence_assessments (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL,
    round_number INTEGER NOT NULL,
    agent_id INTEGER NOT NULL,
    agent_run_id INTEGER NOT NULL,
    assessment TEXT,
    parsed_ok INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS convergence_decisions (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL,
    round_number INTEGER NOT NULL,
    decision TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS round_summaries (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL,
    round_number INTEGER NOT NULL,
    summary TEXT NOT NULL,
    model TEXT,
    tokens_in INTEGER NOT NULL DEFAULT 0,
    tokens_out INTEGER NOT NULL DEFAULT 0,
    cost REAL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL,
    type TEXT NOT NULL,
    data TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def jdump(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False)


def jload(s: str | None, default: Any = None) -> Any:
    if s is None or s == "":
        return default
    return json.loads(s)


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self.conn = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.executescript(SCHEMA)
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(rounds)")}
        if "kind" not in cols:  # migration: rounds are 'debate' (normal) or 'synthesis' (final, one agent)
            self.conn.execute("ALTER TABLE rounds ADD COLUMN kind TEXT NOT NULL DEFAULT 'debate'")
        self._depth = 0

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def tx(self) -> Iterator[None]:
        """Atomic transaction; nested calls join the outer transaction."""
        if self._depth:
            self._depth += 1
            try:
                yield
            finally:
                self._depth -= 1
            return
        self.conn.execute("BEGIN IMMEDIATE")
        self._depth = 1
        try:
            yield
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")
        finally:
            self._depth = 0

    def q(self, sql: str, args: tuple | list = ()) -> list[dict]:
        return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def one(self, sql: str, args: tuple | list = ()) -> dict | None:
        r = self.conn.execute(sql, args).fetchone()
        return dict(r) if r else None

    def val(self, sql: str, args: tuple | list = ()) -> Any:
        r = self.conn.execute(sql, args).fetchone()
        return r[0] if r else None

    def ex(self, sql: str, args: tuple | list = ()) -> int:
        return self.conn.execute(sql, args).lastrowid

    def bump(self, project_id: int) -> None:
        """Increment the project's change counter (the UI polls this)."""
        self.conn.execute("UPDATE projects SET seq = seq + 1 WHERE id = ?", (project_id,))

    def event(self, project_id: int, type_: str, **data: Any) -> None:
        self.ex(
            "INSERT INTO events(project_id, type, data, created_at) VALUES (?,?,?,?)",
            (project_id, type_, jdump(data), now()),
        )
        self.bump(project_id)
