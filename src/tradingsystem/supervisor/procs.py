"""Process helpers: tree kills that spare the MT5 terminal, terminal discovery/launch, rotating child logs.

The MT5 terminal must never live in the supervisor's job or die with a child's tree (OPS-04): a child that calls
``mt5.initialize(path)`` on a closed terminal starts it as *its* child (CreateProcessW, D-023).
"""
from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from typing import IO, Iterable

import psutil

from .winops import CREATE_NO_WINDOW, WIN, spawn_outside

log = logging.getLogger("supervisor")
TERMINAL_NAMES = frozenset({"terminal64.exe", "terminal.exe"})


def is_terminal(p: psutil.Process) -> bool:
    try:
        return p.name().lower() in TERMINAL_NAMES
    except psutil.Error:
        return False


def kill_tree(pid: int, timeout: float = 10.0) -> None:
    """Kill a process and all its descendants — except MT5 terminals (and their own children)."""
    try:
        root = psutil.Process(pid)
        desc = root.children(recursive=True)
    except psutil.Error:
        return
    spare: set[int] = set()
    for t in (p for p in desc if is_terminal(p)):
        spare.add(t.pid)
        try:
            spare.update(c.pid for c in t.children(recursive=True))
        except psutil.Error:
            pass
    if spare:
        log.info("sparing MT5 terminal pid(s) %s in the tree of %d", sorted(spare), pid)
    procs = [p for p in desc if p.pid not in spare] + [root]
    for p in procs:
        try:
            p.kill()
        except psutil.Error:
            pass
    psutil.wait_procs(procs, timeout=timeout)


def _norm(path: str | Path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def find_terminals(paths: Iterable[str]) -> dict[str, list[int]]:
    """``{configured terminal path: [pids running that exe]}`` (name filter first: exe() only for candidates)."""
    want = {_norm(p): p for p in paths}
    out: dict[str, list[int]] = {p: [] for p in want.values()}
    for p in psutil.process_iter(["name"]):
        if (p.info.get("name") or "").lower() not in TERMINAL_NAMES:
            continue
        try:
            key = _norm(p.exe())
        except (psutil.Error, OSError):
            continue
        if key in want:
            out[want[key]].append(p.pid)
    return out


def task_exists(task: str) -> bool:
    if not WIN or not task:
        return False
    try:
        return subprocess.run(["schtasks", "/Query", "/TN", task], capture_output=True, timeout=20,
                              creationflags=CREATE_NO_WINDOW).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def launch_terminal(path: str, task: str | None = None) -> str:
    """Start the terminal outside our job *and* process tree. Returns how.

    Its scheduled task if registered (Task Scheduler is the parent), else a short-lived ``cmd /c start``: cmd exits
    at once, so the terminal is an orphan no tree kill of ours can reach.
    """
    if task and task_exists(task):
        r = subprocess.run(["schtasks", "/Run", "/TN", task], capture_output=True, timeout=30,
                           creationflags=CREATE_NO_WINDOW)
        if r.returncode == 0:
            return f"task {task}"
    if WIN:
        p = spawn_outside(["cmd.exe", "/d", "/c", "start", "", path], cwd=Path(path).parent, console=True)
        return f"cmd start (pid {p.pid})"
    p = spawn_outside([path], cwd=Path(path).parent, console=False)
    return f"detached pid {p.pid}"


_CTL_FLAGS = frozenset({"--detach", "--stop", "--status", "-h", "--help"})


def is_supervisor_cmd(cmd: list[str]) -> bool:
    """``... -m tradingsystem [--instance X] run ...`` without a control flag (``--detach/--stop/--status`` only
    talk to one). ``--instance`` may also stand before ``run`` (the CLI takes it anywhere)."""
    try:
        i = cmd.index("tradingsystem")
    except ValueError:
        return False
    rest, j = cmd[i + 1:], 0
    while j < len(rest) and (rest[j] == "--instance" or rest[j].startswith("--instance=")):
        j += 2 if rest[j] == "--instance" else 1
    return i > 0 and cmd[i - 1] == "-m" and rest[j:j + 1] == ["run"] and not _CTL_FLAGS & set(rest[j + 1:])


def cmd_instance(cmd: list[str]) -> str | None:
    """The pair a ``tradingsystem`` command line runs as (``--instance X`` / ``--instance=X``); None = all pairs."""
    for i, a in enumerate(cmd):
        if a == "--instance" and i + 1 < len(cmd):
            return cmd[i + 1].strip().upper() or None
        if a.startswith("--instance="):
            return a.split("=", 1)[1].strip().upper() or None
    return None


def _proc_instance(p: psutil.Process, cmd: list[str]) -> str | None:
    inst = cmd_instance(cmd)
    if inst is None:                    # started with TS_INSTANCE in its environment instead of --instance
        try:
            inst = (p.environ().get("TS_INSTANCE") or "").strip().upper() or None
        except (psutil.Error, OSError):
            inst = None
    return inst


def running_supervisors(older_s: float | None = None) -> dict[int, str | None]:
    """``{pid: instance}`` of other running supervisors of any build (older ones hold no single-instance lock),
    never ours; instance None = the all-pairs system. ``older_s``: only those started at least that long before this
    process (two new starts racing for the lock must not both give up)."""
    try:
        me = psutil.Process()
        mine = {me.pid, *(p.pid for p in me.parents()), *(c.pid for c in me.children(recursive=True))}
        born = me.create_time()
    except psutil.Error:
        mine, born = {os.getpid()}, None
    out: dict[int, str | None] = {}
    parent: dict[int, int] = {}
    for p in psutil.process_iter(["name", "ppid"]):
        if p.pid in mine or not (p.info.get("name") or "").lower().startswith("python"):
            continue
        try:
            cmd = p.cmdline()
            if not is_supervisor_cmd(cmd):
                continue
            if older_s is not None and born is not None and p.create_time() > born - older_s:
                continue
            out[p.pid] = _proc_instance(p, cmd)
            parent[p.pid] = p.info.get("ppid") or 0
        except (psutil.Error, OSError):
            continue
    # a venv's python.exe is a launcher that runs the real interpreter as its child with the same command line:
    # one supervisor, two processes — keep the outermost (killing its tree ends both)
    return {pid: inst for pid, inst in sorted(out.items()) if parent.get(pid) not in out}


def conflicts(mine: str | None, other: str | None) -> bool:
    """D-042: one system per pair may run next to the others; the all-pairs system runs alone (it trades every pair
    and would double every order and every heartbeat)."""
    return mine is None or other is None or mine == other


def other_supervisors(older_s: float | None = None, *, instance: str | None = None, scope: str = "all") -> list[int]:
    """PIDs of other running supervisors. ``scope``: ``all`` (any system), ``exact`` (the same system as
    ``instance``: that pair, or the all-pairs one for None), ``conflict`` (those that may not run next to it)."""
    found = running_supervisors(older_s)
    if scope == "exact":
        return [pid for pid, inst in found.items() if inst == instance]
    if scope == "conflict":
        return [pid for pid, inst in found.items() if conflicts(instance, inst)]
    return list(found)


def describe(pids: dict[int, str | None] | list[int]) -> str:
    """``pid 123 (BTCUSDT), pid 456 (all pairs)`` for messages."""
    found = pids if isinstance(pids, dict) else {p: running_supervisors().get(p) for p in pids}
    return ", ".join(f"{'pid ' + str(pid) if pid > 0 else 'a supervisor'} ({inst or 'all pairs'})"
                     for pid, inst in found.items())


def open_rotating(path: Path, max_bytes: int, backups: int) -> IO[bytes]:
    """Append handle for a child's stdout/stderr; rolls ``path`` → ``path.1`` … when over ``max_bytes``.

    Positioned at the end so an inherited handle (no O_APPEND in the child's CRT) never overwrites old lines.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if max_bytes > 0 and path.exists() and path.stat().st_size > max_bytes:
            for i in range(backups, 0, -1):
                src = path if i == 1 else path.with_name(f"{path.name}.{i - 1}")
                if src.exists():
                    os.replace(src, path.with_name(f"{path.name}.{i}"))
            if backups <= 0:
                path.unlink()
    except OSError as exc:              # still held open by an orphan — keep appending
        log.warning("could not rotate %s: %s", path, exc)
    fh = open(path, "ab")               # noqa: SIM115 (handed to Popen, closed by the caller)
    fh.seek(0, os.SEEK_END)
    return fh


def tail(path: Path | None, since: int = 0, lines: int = 5, max_bytes: int = 4096) -> str:
    """Last ``lines`` non-empty lines written after offset ``since`` (for exit events)."""
    if path is None:
        return ""
    try:
        with open(path, "rb") as f:
            size = f.seek(0, os.SEEK_END)
            start = max(since, size - max_bytes)
            if size <= start:
                return ""
            f.seek(start)
            text = f.read().decode("utf-8", "replace")
    except OSError:
        return ""
    return " | ".join([ln.strip() for ln in text.splitlines() if ln.strip()][-lines:])
