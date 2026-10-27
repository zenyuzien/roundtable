"""Controlled execution of agent Python / Bash code.

Two isolation levels, chosen automatically at startup:

* ``bwrap``     - bubblewrap available and permitted: the process sees a read-only
                  host filesystem with $HOME and the app's data directory hidden,
                  only its own working copy writable, a private /tmp, and no
                  network unless allowed.
* ``guarded``   - fallback when bubblewrap cannot create namespaces (common on
                  hardened kernels / AppArmor). Code runs as the current user with:
                  cwd = the agent's staging copy, scrubbed environment (no API keys),
                  CPU/memory/file-size rlimits, a timeout that kills the process
                  group, a Python audit hook blocking writes/deletes outside the
                  working copy, subprocess spawning and access to app data, and
                  a Bash command filter. This is defence in depth, NOT a security
                  boundary - see README.
"""
from __future__ import annotations

import asyncio
import os
import re
import resource
import signal
import subprocess
import sys
from pathlib import Path

from ..config import DATA_DIR

OUTPUT_LIMIT = 40_000

_PY_GUARD = r'''
import os, sys
_WD = os.path.realpath(os.environ["RT_WORKDIR"])
_DATA = os.path.realpath(os.environ["RT_DATA"])
_NET = os.environ.get("RT_NET") == "1"
_TMP = ("/dev/null", "/dev/shm/", "/proc/self/")
def _inside(p, base):
    return p == base or p.startswith(base + os.sep)
def _check_path(path, write):
    try:
        if isinstance(path, int):
            return
        p = os.path.realpath(os.fsdecode(path))
    except Exception:
        return
    if _inside(p, _DATA) and not _inside(p, _WD):
        raise PermissionError(f"sandbox: access to application data denied: {p}")
    if write and not _inside(p, _WD) and not (p == "/dev/null" or p.startswith(_TMP)):
        raise PermissionError(f"sandbox: writes outside the workspace are not allowed: {p}")
def _hook(event, args):
    if event == "open":
        path, mode, flags = args
        write = (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
            isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))
        _check_path(path, bool(write))
    elif event in ("os.remove", "os.rmdir", "os.truncate", "os.chmod", "os.chown", "os.mkdir", "shutil.rmtree",
                   "os.utime"):
        _check_path(args[0], True)
    elif event in ("os.rename", "os.link", "os.symlink", "shutil.copyfile", "shutil.move"):
        _check_path(args[0], True); _check_path(args[1], True)
    elif event in ("subprocess.Popen", "os.system", "os.exec", "os.posix_spawn", "os.spawn", "pty.spawn"):
        raise PermissionError(f"sandbox: spawning processes is not allowed ({event})")
    elif event == "socket.connect" and not _NET:
        addr = args[1]
        if not isinstance(addr, (str, bytes)):
            raise PermissionError("sandbox: network access is disabled")
sys.addaudithook(_hook)
_code_path = sys.argv[1]
sys.argv = [_code_path]
sys.path.insert(0, _WD)
with open(_code_path) as _f:
    _src = _f.read()
_g = {"__name__": "__main__", "__file__": _code_path}
exec(compile(_src, _code_path, "exec"), _g)
'''

_BASH_DENY = [
    (r"\bsudo\b|\bsu\b\s|\bdoas\b", "privilege escalation"),
    (r"\bmkfs|\bfdisk\b|\bparted\b|\bdd\b[^|;&]*\bof=/dev", "disk operations"),
    (r"\b(shutdown|reboot|poweroff|halt|init\s+0)\b", "system power control"),
    (r":\(\)\s*\{", "fork bomb"),
    (r"\b(curl|wget)\b[^|]*\|\s*(ba|z|da)?sh\b", "piping downloads into a shell"),
    (r"\bchmod\s+(-R\s+)?[0-7]*\s*/(\s|$)|\bchown\b.*\s/(\s|$)", "permission changes on /"),
    (r"\brm\s+(-[a-zA-Z]*\s+)*(/|~|\$HOME|\*)(\s|$)", "recursive deletion of root/home"),
    (r"\b(crontab|systemctl|service|mount|umount|iptables|useradd|passwd)\b", "system administration"),
    (r"\bkill\s+-9\s+-1\b|\bkillall\b|\bpkill\b", "killing processes"),
    (r"(^|[^\w])\.\.(/|$)", "parent-directory traversal"),
    (r"(^|\s)(cd|pushd)\s+(/|~)", "leaving the workspace"),
    (r"\bsecrets\.json\b|\bANTHROPIC_API_KEY\b|\bOPENAI_API_KEY\b", "credential access"),
]
_ALLOWED_ABS = ("/usr/", "/bin/", "/dev/null", "/tmp/", "/proc/cpuinfo", "/proc/meminfo", "/etc/os-release")


def check_bash(cmd: str, workdir: Path) -> str | None:
    """Return a refusal reason, or None if the command passes the filter."""
    for pat, why in _BASH_DENY:
        if re.search(pat, cmd):
            return f"command refused by sandbox policy ({why})"
    wd = str(workdir.resolve())
    for tok in re.findall(r"(?<![\w$.-])(/[^\s'\";|&><()]*|~[^\s'\";|&><()]*)", cmd):
        if tok.startswith(wd) or tok.startswith(_ALLOWED_ABS) or tok in ("/", "/tmp"):
            if tok == "/":
                return "command refused by sandbox policy (operates on /)"
            continue
        return f"command refused by sandbox policy (absolute path outside workspace: {tok})"
    if re.search(r"(?:>|>>)\s*/(?!dev/null|tmp/)", cmd):
        return "command refused by sandbox policy (redirect outside workspace)"
    return None


def _bwrap_args(workdir: Path, allow_network: bool) -> list[str]:
    args = ["bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp",
            "--unshare-user", "--unshare-pid", "--unshare-ipc", "--unshare-uts", "--unshare-cgroup-try",
            "--die-with-parent", "--new-session"]
    if not allow_network:
        args.append("--unshare-net")
    home = os.path.expanduser("~")
    if home and home != "/":
        args += ["--tmpfs", home]
    for p in {sys.prefix, sys.base_prefix}:
        if p.startswith(home):
            args += ["--ro-bind", p, p]
    args += ["--tmpfs", str(DATA_DIR)] if not str(DATA_DIR).startswith(home) else []
    args += ["--bind", str(workdir), str(workdir), "--chdir", str(workdir)]
    return args


def probe_bwrap() -> bool:
    if os.environ.get("ROUNDTABLE_NO_BWRAP"):
        return False
    try:
        d = DATA_DIR / "probe"
        d.mkdir(parents=True, exist_ok=True)
        r = subprocess.run(_bwrap_args(d, False) + ["/bin/true"], capture_output=True, timeout=10)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


class Sandbox:
    def __init__(self):
        self.mode = "bwrap" if probe_bwrap() else "guarded"

    def describe(self) -> dict:
        return {
            "mode": self.mode,
            "isolated": self.mode == "bwrap",
            "note": ("bubblewrap isolation active" if self.mode == "bwrap" else
                     "bubblewrap unavailable: code runs as your user with resource limits, a scrubbed "
                     "environment and policy guards - not a hard security boundary"),
        }

    def _env(self, workdir: Path, allow_network: bool) -> dict:
        return {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": str(workdir),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "MPLBACKEND": "Agg",
            "MPLCONFIGDIR": str(workdir / ".cache" / "mpl"),
            "XDG_CACHE_HOME": str(workdir / ".cache"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "TMPDIR": str(workdir / ".cache" / "tmp"),
            "RT_WORKDIR": str(workdir),
            "RT_DATA": str(DATA_DIR),
            "RT_NET": "1" if allow_network else "0",
            **({"CUDA_VISIBLE_DEVICES": os.environ["CUDA_VISIBLE_DEVICES"]}
               if "CUDA_VISIBLE_DEVICES" in os.environ else {}),
        }

    async def run(self, argv: list[str], workdir: Path, *, timeout: int, memory_mb: int,
                  allow_network: bool) -> dict:
        workdir = workdir.resolve()
        (workdir / ".cache" / "tmp").mkdir(parents=True, exist_ok=True)

        def limits():
            os.setsid()
            resource.setrlimit(resource.RLIMIT_CPU, (timeout + 5, timeout + 10))
            if memory_mb:
                b = memory_mb * 1024 * 1024
                resource.setrlimit(resource.RLIMIT_AS, (b, b))
            resource.setrlimit(resource.RLIMIT_FSIZE, (512 * 1024 * 1024, 512 * 1024 * 1024))
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))

        if self.mode == "bwrap":
            argv = _bwrap_args(workdir, allow_network) + argv
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=str(workdir), env=self._env(workdir, allow_network),
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            preexec_fn=limits,
        )
        timed_out = False
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except (asyncio.TimeoutError, asyncio.CancelledError) as e:
            timed_out = isinstance(e, asyncio.TimeoutError)
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            out, err = await proc.communicate()
            if not timed_out:
                raise
        return {
            "exit_code": proc.returncode,
            "timed_out": timed_out,
            "stdout": _clip(out.decode(errors="replace")),
            "stderr": _clip(err.decode(errors="replace")),
        }

    async def run_python(self, code: str, workdir: Path, exec_id: str, *, python: str, timeout: int,
                         memory_mb: int, allow_network: bool) -> dict:
        d = workdir / ".exec"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{exec_id}.py").write_text(code)
        guard = d / "guard.py"
        guard.write_text(_PY_GUARD)
        argv = [python, "-I", str(guard), str(d / f"{exec_id}.py")]
        return await self.run(argv, workdir, timeout=timeout, memory_mb=memory_mb, allow_network=allow_network)

    async def run_bash(self, cmd: str, workdir: Path, *, timeout: int, memory_mb: int, allow_network: bool) -> dict:
        refusal = check_bash(cmd, workdir)
        if refusal:
            return {"exit_code": None, "timed_out": False, "stdout": "", "stderr": refusal, "refused": True}
        return await self.run(["/bin/bash", "--noprofile", "--norc", "-c", cmd], workdir, timeout=timeout,
                              memory_mb=memory_mb, allow_network=allow_network)


def _clip(s: str) -> str:
    if len(s) <= OUTPUT_LIMIT:
        return s
    half = OUTPUT_LIMIT // 2
    return s[:half] + f"\n... [{len(s) - OUTPUT_LIMIT} characters omitted] ...\n" + s[-half:]


def fmt_exec(r: dict) -> str:
    parts = []
    if r.get("refused"):
        return f"REFUSED: {r['stderr']}"
    parts.append(f"exit_code: {r['exit_code']}" + ("  (TIMED OUT - process killed)" if r["timed_out"] else ""))
    if r["stdout"]:
        parts.append("stdout:\n" + r["stdout"])
    if r["stderr"]:
        parts.append("stderr:\n" + r["stderr"])
    return "\n".join(parts)

