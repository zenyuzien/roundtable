import asyncio
import os
import re
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("ROUNDTABLE_DATA", tempfile.mkdtemp(prefix="rt-test-"))
os.environ["ROUNDTABLE_NO_BWRAP"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from roundtable import service  # noqa: E402
from roundtable.config import Pricing, Secrets  # noqa: E402
from roundtable.db import Store  # noqa: E402
from roundtable.engine import Engine  # noqa: E402
from roundtable.providers import MockProvider  # noqa: E402


def who(system: str) -> str:
    return re.search(r'You are "([^"]+)"', system).group(1)


def round_of(messages) -> int:
    return int(re.search(r"This is round (\d+)", messages[0]["content"]).group(1))


class Harness:
    def __init__(self, tmp: Path, script=None):
        self.tmp = tmp
        self.db_path = tmp / "rt.sqlite3"
        self.script = script
        self.open()

    def open(self):
        self.store = Store(self.db_path)
        self.mock = MockProvider(script=self.script)
        self.engine = Engine(self.store, self.tmp, {"mock": self.mock}, Secrets(self.tmp), Pricing(self.tmp),
                             retry_base_delay=0.01)
        self.engine.recover()

    def reopen(self):
        self.store.close()
        self.open()

    def project(self, n_agents=3, settings=None, task="Investigate X.", names=None, tools=None):
        pid = service.create_project(self.store, "test", settings or {})
        names = names or [f"A{i + 1}" for i in range(n_agents)]
        ids = [service.add_agent(self.store, pid, n, instructions=f"instructions for {n} v1", tools=tools or [])
               for n in names]
        service.set_task(self.store, pid, task)
        return pid, ids

    def runner(self, pid):
        return self.engine.runner(pid)

    def state(self, pid):
        return self.store.one("SELECT state, state_reason FROM projects WHERE id = ?", (pid,))

    def runs(self, pid, round_number=None):
        sql = "SELECT * FROM agent_runs WHERE project_id = ?"
        args = [pid]
        if round_number is not None:
            sql += " AND round_number = ?"
            args.append(round_number)
        return self.store.q(sql + " ORDER BY id", args)

    def rounds(self, pid):
        return self.store.q("SELECT * FROM rounds WHERE project_id = ? ORDER BY number, id", (pid,))


@pytest.fixture
def make(tmp_path):
    hs = []

    def _make(script=None):
        h = Harness(tmp_path, script)
        hs.append(h)
        return h

    yield _make
    for h in hs:
        try:
            h.store.close()
        except Exception:
            pass


def run(coro):
    return asyncio.run(coro)
