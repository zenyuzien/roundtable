"""Agent tools. Every invocation is recorded in ``tool_calls`` with its full result.

Tool groups (``agents.tools``) gate which tools an agent sees; project-level
settings (``tools.allow_network/allow_python/allow_bash``) can veto groups.
"""
from __future__ import annotations

import asyncio
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from ..db import Store, jdump, now
from ..providers.base import ToolCall, ToolSpec
from ..workspace import WS_KINDS, ProjectPaths, ensure_workdir, list_tree, safe_join, snapshot_entries
from . import web
from .sandbox import Sandbox, fmt_exec

RESULT_TO_MODEL_LIMIT = 20_000
RESULT_STORE_LIMIT = 500_000


@dataclass
class ToolContext:
    store: Store
    paths: ProjectPaths
    sandbox: Sandbox
    project_id: int
    agent_id: int
    agent_name: str
    run_id: int
    round_number: int
    settings: dict
    agent_names: dict[int, str] = field(default_factory=dict)
    redact: Callable[[str], str] = lambda s: s
    touched_private: bool = False

    @property
    def workdir(self) -> Path:
        return self.paths.workdir(self.round_number, self.run_id)

    def ensure_workdir(self) -> Path:
        return ensure_workdir(self.paths, self.round_number, self.run_id, self.agent_id)

    def resolve_read(self, rel: str) -> Path:
        rel = (rel or "").strip().lstrip("/")
        if self.workdir.exists():
            p = safe_join(self.workdir, rel)
            if p.exists():
                return p
        if rel == "private" or rel.startswith("private/"):
            self.touched_private = True
            return safe_join(self.paths.private(self.agent_id), rel[len("private"):].lstrip("/"))
        return safe_join(self.paths.shared, rel)

    def source(self, kind: str, ref: str, title: str, url: str, meta: dict | None = None) -> None:
        self.store.ex(
            "INSERT OR IGNORE INTO sources(project_id, kind, ref, title, url, meta, agent_id, round_number, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (self.project_id, kind, ref, title, url, jdump(meta or {}), self.agent_id, self.round_number, now()))


@dataclass
class Tool:
    name: str
    group: str
    description: str
    parameters: dict
    fn: Callable[[ToolContext, dict], Awaitable[str]]


TOOLS: dict[str, Tool] = {}


def tool(name: str, group: str, description: str, properties: dict, required: list[str] | None = None):
    def deco(fn):
        TOOLS[name] = Tool(name, group, description,
                           {"type": "object", "properties": properties, "required": required or []}, fn)
        return fn
    return deco


def _page(text: str, offset: int, max_chars: int) -> str:
    offset = max(0, int(offset or 0))
    max_chars = max(500, min(int(max_chars or 15_000), RESULT_TO_MODEL_LIMIT))
    chunk = text[offset:offset + max_chars]
    more = len(text) - (offset + len(chunk))
    tail = f"\n\n[showing characters {offset}-{offset + len(chunk)} of {len(text)}" + (
        f"; call again with offset={offset + len(chunk)} for more]" if more > 0 else "]")
    return chunk + tail


S = {"type": "string"}
I = {"type": "integer"}


# ------------------------------------------------------------------ workspace

@tool("workspace_read", "workspace",
      "Read the shared research workspace as of the start of this round (full content). "
      "Optionally pass a key to read one entry, including any conflicting variants.",
      {"key": S})
async def _ws_read(ctx: ToolContext, a: dict) -> str:
    entries = snapshot_entries(ctx.store, ctx.project_id, ctx.round_number)
    if a.get("key"):
        entries = [e for e in entries if e["key"] == a["key"]]
        if not entries:
            return f"No entry with key {a['key']!r}."
    if not entries:
        return "The workspace is empty."
    return "\n\n".join(format_entry(e, ctx.agent_names, full=True) for e in entries)


@tool("workspace_propose", "workspace",
      "Propose a change to the shared research workspace. Proposals are staged and committed only at the end of "
      "this round, so other agents see them next round. op: 'upsert' (create or replace content), 'append' "
      "(add to content), 'set_status' (content = new status, e.g. 'rejected', 'supported', 'open'). "
      f"kind is one of: {', '.join(WS_KINDS)}. If several agents upsert the same key in one round, all versions "
      "are kept and flagged as a conflict.",
      {"op": {"type": "string", "enum": ["upsert", "append", "set_status"]}, "key": S,
       "kind": {"type": "string", "enum": WS_KINDS}, "title": S, "content": S},
      ["op", "key", "content"])
async def _ws_propose(ctx: ToolContext, a: dict) -> str:
    key = re.sub(r"[^a-z0-9_\-.]+", "-", str(a["key"]).strip().lower()).strip("-")[:80]
    if not key:
        return "ERROR: key must contain letters or digits"
    op = a.get("op", "upsert")
    if op not in ("upsert", "append", "set_status"):
        return "ERROR: invalid op"
    kind = a.get("kind") or None
    if kind and kind not in WS_KINDS:
        return f"ERROR: kind must be one of {WS_KINDS}"
    ctx.store.ex(
        "INSERT INTO ws_proposals(project_id, agent_run_id, agent_id, round_number, op, key, kind, title, content,"
        " status, created_at) VALUES (?,?,?,?,?,?,?,?,?, 'staged', ?)",
        (ctx.project_id, ctx.run_id, ctx.agent_id, ctx.round_number, op, key, kind, a.get("title"),
         str(a.get("content", "")), now()))
    ctx.store.bump(ctx.project_id)
    return f"Staged {op} on '{key}'. It will be committed at the end of round {ctx.round_number}."


def format_entry(e: dict, names: dict[int, str], full: bool = False, limit: int = 4000) -> str:
    def who(v):
        if v.get("author_type") == "user":
            return "user"
        if v.get("author_type") == "agents":
            return "several agents"
        return names.get(v.get("agent_id"), f"agent {v.get('agent_id')}")

    def body(c):
        return c if full or len(c) <= limit else c[:limit] + f"\n[... truncated; use workspace_read key={e['key']}]"

    head = f"#### [{e['kind']}] {e['title'] or e['key']}  (key: `{e['key']}`, v{e['version']}, status: {e['status']})"
    if e["conflict"]:
        parts = [head + "\n**CONFLICT** - concurrent edits; all variants preserved. To resolve it, upsert a version "
                        "that fairly represents every variant (or explicitly says why one is rejected):"]
        for v in e["variants"]:
            parts.append(f"- variant by {who(v)} (round {v['round_number']}):\n{body(v['content'])}")
        return "\n".join(parts)
    return f"{head}\n_last change: {who(e)}, round {e['round_number']}_\n{body(e['content'])}"


# ------------------------------------------------------------------ files

@tool("list_files", "files",
      "List files in the project file tree (shared files, plus your private files under private/).", {"dir": S})
async def _list_files(ctx: ToolContext, a: dict) -> str:
    base = ctx.workdir if ctx.workdir.exists() else ctx.paths.shared
    items = list_tree(base)
    if not ctx.workdir.exists():
        items += [{"path": "private/" + f["path"], "size": f["size"]} for f in list_tree(ctx.paths.private(ctx.agent_id))]
    d = (a.get("dir") or "").strip("/")
    if d:
        items = [f for f in items if f["path"].startswith(d + "/")]
    if not items:
        return "No files."
    return "\n".join(f"{f['path']}  ({f['size']} bytes)" for f in items)


@tool("read_file", "files", "Read a text file (or extract text from a PDF) from the project file tree.",
      {"path": S, "offset": I, "max_chars": I}, ["path"])
async def _read_file(ctx: ToolContext, a: dict) -> str:
    p = ctx.resolve_read(a["path"])
    if not p.is_file():
        return f"ERROR: no such file: {a['path']}"
    data = p.read_bytes()
    if p.suffix.lower() == ".pdf":
        text = await asyncio.to_thread(web.pdf_to_text, data)
    else:
        if b"\x00" in data[:4096]:
            return f"Binary file ({len(data)} bytes); cannot display."
        text = data.decode(errors="replace")
    return _page(text, a.get("offset", 0), a.get("max_chars", 15_000))


@tool("write_file", "files",
      "Write a text file into your working copy of the project file tree. Writes are staged and merged into the "
      "shared tree at the end of the round (visible to others next round). mode: 'overwrite' or 'append'.",
      {"path": S, "content": S, "mode": {"type": "string", "enum": ["overwrite", "append"]}}, ["path", "content"])
async def _write_file(ctx: ToolContext, a: dict) -> str:
    wd = ctx.ensure_workdir()
    rel = a["path"].strip().lstrip("/")
    if rel.startswith(".exec") or not rel:
        return "ERROR: invalid path"
    p = safe_join(wd, rel)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a" if a.get("mode") == "append" else "w") as f:
        f.write(str(a.get("content", "")))
    if rel.startswith("private/"):
        ctx.touched_private = True
    return f"Wrote {p.stat().st_size} bytes to {rel} (staged until end of round {ctx.round_number})."


# ------------------------------------------------------------------ transcript

@tool("read_round", "transcript",
      "Read the exact, untruncated record of an earlier completed round: every agent's output plus user messages "
      "you were shown. Optionally restrict to one agent by name.",
      {"round": I, "agent": S}, ["round"])
async def _read_round(ctx: ToolContext, a: dict) -> str:
    r = int(a["round"])
    if r >= ctx.round_number or r < 0:
        return f"ERROR: only rounds 0..{ctx.round_number - 1} are readable in round {ctx.round_number}"
    chan = f"agent:{ctx.agent_id}"
    msgs = ctx.store.q(
        "SELECT * FROM messages WHERE project_id = ? AND (channel = 'common' OR channel = ?) AND visible_from_round "
        "IS NOT NULL AND visible_from_round <= ? AND ((kind = 'agent_output' AND round_number = ?) OR "
        "(kind = 'task' AND ? = 0) OR (kind IN ('user', 'system') AND visible_from_round = ?)) ORDER BY id",
        (ctx.project_id, chan, ctx.round_number, r, r, r))
    if a.get("agent"):
        msgs = [m for m in msgs if m["kind"] != "agent_output" or ctx.agent_names.get(m["agent_id"]) == a["agent"]]
    if any(m["channel"] != "common" for m in msgs):
        ctx.touched_private = True
    if not msgs:
        return "Nothing recorded for that round."
    return "\n\n".join(_fmt_msg(m, ctx.agent_names) for m in msgs)


def _fmt_msg(m: dict, names: dict[int, str]) -> str:
    if m["kind"] == "agent_output":
        return f"### Output of {names.get(m['agent_id'], m['agent_id'])} (round {m['round_number']})\n{m['content']}"
    where = "common channel" if m["channel"] == "common" else "your private channel"
    return f"### User message ({where})\n{m['content']}"


@tool("list_tool_calls", "transcript",
      "List tool calls recorded in earlier rounds (yours and other agents'), with ids for read_tool_result.",
      {"round": I})
async def _list_tool_calls(ctx: ToolContext, a: dict) -> str:
    args: list[Any] = [ctx.project_id, ctx.round_number, ctx.agent_id]
    sql = ("SELECT id, agent_id, round_number, tool, args, status FROM tool_calls WHERE project_id = ? AND "
           "round_number < ? AND (private = 0 OR agent_id = ?)")
    if a.get("round") is not None:
        sql += " AND round_number = ?"
        args.append(int(a["round"]))
    rows = ctx.store.q(sql + " ORDER BY id DESC LIMIT 100", args)
    if not rows:
        return "No earlier tool calls."
    return "\n".join(
        f"#{r['id']} round {r['round_number']} {ctx.agent_names.get(r['agent_id'], r['agent_id'])}: "
        f"{r['tool']}({r['args'][:150]}) -> {r['status']}" for r in rows)


@tool("read_tool_result", "transcript", "Read the full recorded result of an earlier tool call by id.",
      {"id": I, "offset": I}, ["id"])
async def _read_tool_result(ctx: ToolContext, a: dict) -> str:
    r = ctx.store.one("SELECT * FROM tool_calls WHERE id = ? AND project_id = ?", (int(a["id"]), ctx.project_id))
    if not r:
        return "ERROR: no such tool call"
    mine_now = r["agent_run_id"] == ctx.run_id
    if not mine_now and r["round_number"] >= ctx.round_number:
        return "ERROR: results from the current round are not visible until the round completes"
    if r["private"] and r["agent_id"] != ctx.agent_id:
        return "ERROR: that tool call touched another agent's private material"
    return f"{r['tool']}({r['args']}):\n" + _page(r["result"] or "", a.get("offset", 0), 15_000)


@tool("list_sources", "transcript",
      "List the sources (papers, web pages) that have actually been retrieved by tools in this project. "
      "Only these, and user-provided files, count as verified sources.", {})
async def _list_sources(ctx: ToolContext, a: dict) -> str:
    rows = ctx.store.q("SELECT * FROM sources WHERE project_id = ? AND round_number <= ? ORDER BY id",
                       (ctx.project_id, ctx.round_number))
    if not rows:
        return "No sources retrieved yet."
    return "\n".join(f"[{r['kind']}:{r['ref']}] {r['title'] or ''} <{r['url'] or ''}>" for r in rows)


# ------------------------------------------------------------------ memory

@tool("memory_write", "memory",
      "Update your private persistent memory (only you see it, in every later round). mode 'replace' or 'append'. "
      "Committed at the end of the round.",
      {"content": S, "mode": {"type": "string", "enum": ["replace", "append"]}}, ["content"])
async def _memory_write(ctx: ToolContext, a: dict) -> str:
    content = str(a.get("content", ""))
    if a.get("mode", "append") == "append":
        prev = ctx.store.one(
            "SELECT content FROM memory_versions WHERE agent_id = ? AND ((visible_from_round IS NOT NULL AND "
            "visible_from_round <= ?) OR (agent_run_id = ? AND status = 'staged')) ORDER BY id DESC LIMIT 1",
            (ctx.agent_id, ctx.round_number, ctx.run_id))
        if prev and prev["content"]:
            content = prev["content"] + "\n" + content
    ctx.store.ex(
        "INSERT INTO memory_versions(project_id, agent_id, agent_run_id, round_number, content, status, created_at)"
        " VALUES (?,?,?,?,?, 'staged', ?)",
        (ctx.project_id, ctx.agent_id, ctx.run_id, ctx.round_number, content, now()))
    return "Memory update staged (committed at end of round)."


# ------------------------------------------------------------------ network

@tool("web_search", "web_search", "Search the web (DuckDuckGo). Returns titles, URLs and snippets.",
      {"query": S, "max_results": I}, ["query"])
async def _web_search(ctx: ToolContext, a: dict) -> str:
    res = await web.web_search(a["query"], int(a.get("max_results") or 8))
    if not res:
        return "No results (the search backend may be rate-limiting; try fetch_url or arxiv_search)."
    return "\n\n".join(f"{i + 1}. {r['title']}\n   {r['url']}\n   {r['snippet']}" for i, r in enumerate(res))


@tool("fetch_url", "fetch_url", "Fetch a web page or PDF by URL and return its text.",
      {"url": S, "offset": I, "max_chars": I}, ["url"])
async def _fetch_url(ctx: ToolContext, a: dict) -> str:
    ctype, text = await web.fetch_url(a["url"])
    m = re.search(r"(?im)^\s*(.{5,150})$", text)
    ctx.source("url", a["url"], m.group(1).strip() if m else a["url"], a["url"], {"content_type": ctype})
    return _page(text, a.get("offset", 0), a.get("max_chars", 15_000))


@tool("arxiv_search", "arxiv",
      "Search arXiv. Query syntax: plain words, or fields like ti:, au:, abs:, cat: (e.g. 'ti:predictive coding "
      "AND cat:cs.LG'). Returns ids, titles, authors, dates, abstracts.",
      {"query": S, "max_results": I}, ["query"])
async def _arxiv_search(ctx: ToolContext, a: dict) -> str:
    try:
        res = await web.arxiv_search(a["query"], int(a.get("max_results") or 8))
    except Exception as e:
        return (f"ERROR: the arXiv API is unavailable ({type(e).__name__}: {e}). It rate-limits aggressively; try "
                "web_search with 'site:arxiv.org ...' and fetch_url / arxiv_read on the result instead.")
    if not res:
        return "No results."
    for r in res:
        ctx.source("arxiv", r["arxiv_id"], r["title"], r["url"],
                   {"authors": r["authors"], "published": r["published"]})
    return "\n\n".join(
        f"[{r['arxiv_id']}] {r['title']} ({r['published']})\n{', '.join(r['authors'][:6])}"
        f"{' et al.' if len(r['authors']) > 6 else ''}\n{r['abstract'][:1200]}" for r in res)


@tool("arxiv_read", "arxiv", "Download an arXiv paper's PDF and return its extracted text (paged).",
      {"arxiv_id": S, "offset": I, "max_chars": I}, ["arxiv_id"])
async def _arxiv_read(ctx: ToolContext, a: dict) -> str:
    res = await web.arxiv_paper(a["arxiv_id"], ctx.paths.cache)
    meta = res["meta"]
    ctx.source("arxiv", meta.get("arxiv_id", a["arxiv_id"]), meta.get("title", ""), meta.get("url", ""),
               {"authors": meta.get("authors"), "published": meta.get("published"), "full_text": True})
    head = f"{meta.get('title', '')} - {', '.join((meta.get('authors') or [])[:6])} ({meta.get('published', '')})\n\n"
    return head + _page(res["text"], a.get("offset", 0), a.get("max_chars", 15_000))


# ------------------------------------------------------------------ execution

@tool("run_python", "python",
      "Execute Python in your working copy of the project files (cwd): pass `code`, or `path` to run a saved "
      ".py file. numpy/sympy (and torch, if installed) are available. Spawning subprocesses is blocked, so use "
      "`path` instead of subprocess. Files you create are staged and merged into the shared tree at round end. "
      "Print what you want to see. Your stdout is recorded and shown to the other agents as evidence: report "
      "only numbers that your runs actually printed.",
      {"code": S, "path": S, "timeout_s": I}, [])
async def _run_python(ctx: ToolContext, a: dict) -> str:
    t = ctx.settings["tools"]
    wd = ctx.ensure_workdir()
    ran_file = None
    if not a.get("code") and a.get("path"):
        p = safe_join(wd, a["path"])
        if not p.is_file():
            return f"ERROR: no such file: {a['path']}"
        a = dict(a, code=p.read_text())
        ran_file = a["path"]
    if not a.get("code"):
        return "ERROR: pass `code` or `path`"
    timeout = min(int(a.get("timeout_s") or t["exec_timeout_s"]), int(t["exec_timeout_s"]))
    exec_id = f"py_{int(asyncio.get_running_loop().time() * 1000)}"
    r = await ctx.sandbox.run_python(a["code"], wd, exec_id, python=t.get("python_executable") or sys.executable,
                                     timeout=timeout, memory_mb=int(t["exec_memory_mb"]),
                                     allow_network=bool(t["allow_network"]))
    if ran_file:  # record exactly what ran: the file may be edited afterwards
        code = a["code"] if len(a["code"]) <= 6000 else a["code"][:6000] + "\n# [... truncated]"
        return f"executed {ran_file} (exact content at run time):\n```python\n{code}\n```\n" + fmt_exec(r)
    return fmt_exec(r)


@tool("run_bash", "bash",
      "Run a shell command in your working copy of the project files. Restricted: no access outside the "
      "workspace, no privileged or destructive commands.",
      {"command": S, "timeout_s": I}, ["command"])
async def _run_bash(ctx: ToolContext, a: dict) -> str:
    t = ctx.settings["tools"]
    wd = ctx.ensure_workdir()
    timeout = min(int(a.get("timeout_s") or t["exec_timeout_s"]), int(t["exec_timeout_s"]))
    r = await ctx.sandbox.run_bash(a["command"], wd, timeout=timeout, memory_mb=int(t["exec_memory_mb"]),
                                   allow_network=bool(t["allow_network"]))
    return fmt_exec(r)


# ------------------------------------------------------------------ registry API

NETWORK_GROUPS = {"web_search", "fetch_url", "arxiv"}


def allowed_groups(agent_tools: list[str], settings: dict) -> set[str]:
    t = settings["tools"]
    g = set(agent_tools)
    if not t.get("allow_network"):
        g -= NETWORK_GROUPS
    if not t.get("allow_python"):
        g.discard("python")
    if not t.get("allow_bash"):
        g.discard("bash")
    return g


def specs_for(groups: set[str]) -> list[ToolSpec]:
    return [ToolSpec(t.name, t.description, t.parameters) for t in TOOLS.values() if t.group in groups]


async def execute(ctx: ToolContext, call: ToolCall, groups: set[str]) -> str:
    t = TOOLS.get(call.name)
    rec = ctx.store.ex(
        "INSERT INTO tool_calls(project_id, agent_run_id, agent_id, round_number, tool, args, status, started_at)"
        " VALUES (?,?,?,?,?,?, 'running', ?)",
        (ctx.project_id, ctx.run_id, ctx.agent_id, ctx.round_number, call.name, jdump(call.args), now()))
    ctx.store.bump(ctx.project_id)
    ctx.touched_private = False
    status = "ok"
    if t is None or t.group not in groups:
        result, status = f"ERROR: tool {call.name!r} is not available to you", "denied"
    else:
        try:
            missing = [k for k in t.parameters.get("required", []) if k not in call.args]
            if missing:
                raise ValueError(f"missing required argument(s): {missing}")
            result = await asyncio.wait_for(t.fn(ctx, call.args), timeout=int(ctx.settings["tools"]["exec_timeout_s"]) + 60)
        except asyncio.TimeoutError:
            result, status = "ERROR: tool timed out", "error"
        except Exception as e:  # tool errors are reported to the model, not fatal to the round
            result, status = f"ERROR: {type(e).__name__}: {e}", "error"
    result = ctx.redact(result)
    private = int(ctx.touched_private or (t is not None and t.group == "memory") or any(isinstance(v, str) and v.lstrip("/").startswith("private")
                                             for v in call.args.values()))
    ctx.store.ex("UPDATE tool_calls SET result = ?, status = ?, private = ?, finished_at = ? WHERE id = ?",
                 (result[:RESULT_STORE_LIMIT], status, private, now(), rec))
    ctx.store.bump(ctx.project_id)
    if len(result) > RESULT_TO_MODEL_LIMIT:
        result = result[:RESULT_TO_MODEL_LIMIT] + f"\n[truncated; full result stored as tool call #{rec}]"
    return f"[tool call #{rec}]\n{result}"

