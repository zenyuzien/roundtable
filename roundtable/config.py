"""Paths, project settings defaults, API-key storage and model pricing."""
from __future__ import annotations

import copy
import json
import os
import stat
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("ROUNDTABLE_DATA", ROOT / "data")).resolve()

TOOL_NAMES = [
    "workspace",       # read structured workspace + propose updates
    "files",           # list/read/write files in the project file tree (writes staged)
    "transcript",      # read raw past rounds / tool results
    "memory",          # private persistent memory
    "web_search",
    "fetch_url",
    "arxiv",
    "python",
    "bash",
]
DEFAULT_AGENT_TOOLS = ["workspace", "files", "transcript", "web_search", "fetch_url", "arxiv", "python"]

DEFAULT_SETTINGS: dict[str, Any] = {
    "max_rounds": 5,
    # agent id that writes one final synthesis turn when the run ends (converged / max rounds); None = off
    "synthesis_agent": None,
    "breakpoints": [],
    # halt: stop and ask the user. retry_then_halt: auto-retry failed agents
    # `auto_retries` times, then halt. skip_failed: complete the round without
    # failed agents (their failure is recorded and shown, never hidden).
    "failure_policy": "retry_then_halt",
    "auto_retries": 1,
    "max_concurrency": 4,
    "max_tool_iterations": 8,
    "budget": {"max_total_tokens": 2_000_000, "max_cost_usd": 20.0},
    "context": {
        "recent_rounds_full": 2,
        "max_context_tokens": 60_000,
        "max_chars_per_output": 24_000,
        "private_history_messages": 40,
        "summarize_older": False,
        "summarizer_provider": "",
        "summarizer_model": "",
    },
    "convergence": {
        "enabled": False,
        "min_rounds": 2,
        "support_fraction": 1.0,
        "max_open_objections": 0,
        "max_unresolved_steps": 0,
        "max_unsupported_claims": 0,
        "min_similarity": 0.8,
        "consecutive_rounds": 1,
        "require_no_workspace_conflicts": True,
        # agent id whose assessment alone decides termination (e.g. a neutral judge); None = all agents
        "decider_agent": None,
    },
    "tools": {
        "allow_network": True,
        "allow_python": True,
        "allow_bash": False,
        "exec_timeout_s": 120,
        "exec_memory_mb": 8192,
        "python_executable": "",
    },
}


def deep_merge(base: dict, patch: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (patch or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def normalize_settings(s: dict) -> dict:
    s = deep_merge(DEFAULT_SETTINGS, s or {})
    s["max_rounds"] = max(1, min(int(s["max_rounds"]), 10_000))
    s["breakpoints"] = sorted({int(b) for b in s.get("breakpoints") or [] if int(b) >= 1})
    if s["failure_policy"] not in ("halt", "retry_then_halt", "skip_failed"):
        s["failure_policy"] = "halt"
    s["auto_retries"] = max(0, min(int(s["auto_retries"]), 5))
    s["max_concurrency"] = max(1, int(s["max_concurrency"]))
    s["max_tool_iterations"] = max(0, min(int(s["max_tool_iterations"]), 50))
    return s


# ---------------------------------------------------------------- secrets

PROVIDER_ENV = {"openai": ("OPENAI_API_KEY",), "anthropic": ("ANTHROPIC_API_KEY",),
                "google": ("GEMINI_API_KEY", "GOOGLE_API_KEY")}


try:
    from .local_keys import BUILTIN_KEYS
except ImportError:
    BUILTIN_KEYS = {}


def _env_key(provider: str) -> str | None:
    """Environment variables first, then the built-in key (if any)."""
    v = next((os.environ[e] for e in PROVIDER_ENV.get(provider, ()) if os.environ.get(e)), None)
    return v or BUILTIN_KEYS.get(provider) or None


def _key_source(provider: str) -> str | None:
    if any(os.environ.get(e) for e in PROVIDER_ENV.get(provider, ())):
        return "env"
    return "builtin" if BUILTIN_KEYS.get(provider) else None


class Secrets:
    """API keys live in ``data/secrets.json`` (mode 0600) or the environment.

    Keys are never returned by the API, never logged, and never passed to tool
    subprocesses.
    """

    def __init__(self, data_dir: Path):
        self.path = data_dir / "secrets.json"

    def _read(self) -> dict:
        try:
            return json.loads(self.path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def get(self, provider: str) -> str | None:
        v = self._read().get(provider)
        if v:
            return v
        return _env_key(provider)

    def set(self, provider: str, key: str | None) -> None:
        d = self._read()
        if key:
            d[provider] = key.strip()
        else:
            d.pop(provider, None)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(fd, "w") as f:
            json.dump(d, f)
        os.replace(tmp, self.path)

    def status(self) -> dict:
        d = self._read()
        out = {}
        for p in PROVIDER_ENV:
            src = "file" if d.get(p) else _key_source(p)
            out[p] = {"configured": src is not None, "source": src}
        return out

    def all_values(self) -> list[str]:
        return [v for v in (self.get(p) for p in PROVIDER_ENV) if v]


def redact(text: str, secrets: Secrets) -> str:
    for v in secrets.all_values():
        if v and len(v) > 8:
            text = text.replace(v, "[REDACTED]")
    return text


# ---------------------------------------------------------------- pricing

class Pricing:
    """USD per million tokens. Defaults ship in pricing.json; user edits go to data/."""

    def __init__(self, data_dir: Path):
        self.default_path = ROOT / "pricing.json"
        self.user_path = data_dir / "pricing.json"

    def table(self) -> dict:
        base = {}
        for p in (self.default_path, self.user_path):
            try:
                base.update(json.loads(p.read_text()).get("models", {}))
            except (FileNotFoundError, json.JSONDecodeError):
                pass
        return base

    def save(self, models: dict) -> None:
        self.user_path.parent.mkdir(parents=True, exist_ok=True)
        self.user_path.write_text(json.dumps({"models": models}, indent=2))

    def cost(self, model: str, tokens_in: int, tokens_out: int) -> float | None:
        t = self.table()
        entry = t.get(model)
        if entry is None:
            # allow dated snapshots like "gpt-4o-2024-08-06" to match "gpt-4o"
            matches = [k for k in t if model.startswith(k)]
            entry = t[max(matches, key=len)] if matches else None
        if not entry:
            return None
        return tokens_in * entry["input"] / 1e6 + tokens_out * entry["output"] / 1e6
