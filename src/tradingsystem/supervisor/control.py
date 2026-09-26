"""``run --detach | --stop | --status``: start, stop and inspect the supervisor (scripts/*.bat, Task Scheduler).

Every function takes the system's *state* directory (``Settings.paths.state()``): ``data`` for the all-pairs
system, ``data/instances/<PAIR>`` for one system per pair (D-042).
``<state>/run/supervisor.json`` is rewritten by the supervisor every loop: pid, heartbeat on the system-wide *awake*
clock (sleep never makes it look hung), children, MT5 terminals, last suspend/clock jump.
``<state>/run/manual_stop`` = the user stopped it: the autostart keep-alive (``--detach --auto``) then leaves the
system down until ``--detach`` (scripts/start.bat) is run by hand.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Callable

import psutil

from ..core.settings import PROJECT_ROOT, Settings
from ..core.timeutil import now_ms
from . import procs
from .winops import instance_name, instance_running, sample_clock, spawn_outside

log = logging.getLogger("supervisor-ctl")
STATE, HOLD = "supervisor.json", "manual_stop"
HUNG_S = 300.0          # awake seconds without a supervisor heartbeat before --detach replaces it
START_WAIT_S = 60.0     # --detach waits this long for the first heartbeat
STOP_WAIT_S = 90.0      # --stop waits this long for a graceful exit before killing the supervisor tree


def run_dir(data: Path) -> Path:
    return Path(data) / "run"


def write_state(data: Path, state: dict) -> None:
    d = run_dir(data)
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / f"{STATE}.tmp"
    tmp.write_text(json.dumps(state, default=str), encoding="utf-8")
    for attempt in range(5):
        try:
            os.replace(tmp, d / STATE)
            return
        except PermissionError:         # a reader has it open (Windows) — retry briefly
            if attempt == 4:
                raise
            time.sleep(0.05)


def read_state(data: Path) -> dict | None:
    try:
        return json.loads((run_dir(data) / STATE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def heartbeat_age(state: dict | None, awake_now: float | None = None) -> float | None:
    """Awake seconds since the supervisor's last heartbeat (time asleep never counts); inf after a reboot."""
    if not state or "awake_s" not in state:
        return None
    now = sample_clock().awake if awake_now is None else awake_now
    age = now - float(state["awake_s"])
    return age if age >= -1.0 else float("inf")      # awake clock restarted → written in another boot


def state_process(state: dict | None) -> psutil.Process | None:
    """The live supervisor the state file describes (pid + create time + command line must all match)."""
    if not state:
        return None
    try:
        p = psutil.Process(int(state["pid"]))
        if abs(p.create_time() - float(state["create_time"])) > 2 or "tradingsystem" not in " ".join(p.cmdline()):
            return None
        return p
    except (psutil.Error, KeyError, TypeError, ValueError):
        return None


def console_python() -> str:
    """The console interpreter (children need a console for CTRL_BREAK even when we run under pythonw)."""
    exe = Path(sys.executable)
    alt = exe.with_name("python.exe")
    return str(alt) if exe.name.lower() == "pythonw.exe" and alt.exists() else str(exe)


def _say(code: int | None, msg: str, *, quiet: bool = False) -> int | None:
    """Print for the user (ASCII: any console code page) and log (``quiet``: not logged, e.g. every keep-alive)."""
    print(msg)
    if not quiet:
        log.info(msg)
    return code


def _running(name: str, instance: str | None = None) -> bool:
    """This system's supervisor runs: the lock is held, or an older build (no lock) is found by its command line."""
    return instance_running(name) or bool(procs.other_supervisors(instance=instance, scope="exact"))


def script(name: str, instance: str | None) -> str:
    """How the user runs a script for this system: ``scripts\\start.bat`` or ``scripts\\start.bat BTCUSDT``."""
    return f"scripts\\{name}.bat" + (f" {instance}" if instance else "")


def conflict_message(instance: str | None, found: dict[int, str | None]) -> str:
    """Why this system may not start next to the supervisors in ``found`` (see :func:`procs.conflicts`)."""
    if instance is None and any(v is not None for v in found.values()):
        return (f"per-pair systems are running ({procs.describe(found)}) - the all-pairs system cannot run next to "
                "them; stop them first (scripts\\stop_all.bat)")
    if instance is not None and any(v is None for v in found.values()):
        return (f"the all-pairs system is running ({procs.describe(found)}) - stop it first (scripts\\stop.bat), "
                "then start one system per pair")
    return (f"a supervisor without the single-instance lock (older build, {procs.describe(found)}) is running - "
            f"stop it first with {script('stop', instance)}")


def _wait(cond: Callable[[], bool], timeout: float, step: float = 0.5) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        if cond():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(step)


def _ago(age: float | None) -> str:
    if age is None:
        return "unknown"
    return "from another boot" if age == float("inf") else f"{age:.0f}s ago"


def _dur(s: float | None) -> str:
    if s is None or s < 0:
        return "?"
    h, m = divmod(int(s) // 60, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m{int(s) % 60:02d}s"


def detach(run_args: list[str], data: Path, s: Settings, *, auto: bool) -> int:
    """Start the supervisor in the background (own windowless console, outside our jobs); wait for its heartbeat.

    ``auto`` (Task Scheduler keep-alive): respect a manual stop; replace a supervisor whose heartbeat is dead.
    """
    inst = s.paths.instance
    name = instance_name("supervisor", data)
    hold = run_dir(data) / HOLD
    if auto and hold.exists():
        return _say(0, f"supervisor held: stopped by the user ({script('start', inst)} resumes it)")
    if instance_running(name):
        st = read_state(data)
        proc, age = state_process(st), heartbeat_age(st)
        if proc is None or age is None or age <= HUNG_S:
            return _say(0, f"supervisor already running (pid {(st or {}).get('pid', '?')}, heartbeat {_ago(age)})",
                        quiet=auto)
        _say(None, f"supervisor pid {proc.pid} wrote no heartbeat for {age:.0f}s of awake time - replacing it")
        procs.kill_tree(proc.pid)
        if not _wait(lambda: not instance_running(name), 30):
            return _say(1, "the old supervisor still holds the single-instance lock - see logs/supervisor.jsonl")
    else:
        found = {pid: i for pid, i in procs.running_supervisors().items() if procs.conflicts(inst, i)}
        if found:
            return _say(1, conflict_message(inst, found), quiet=auto)
    if not auto:
        hold.unlink(missing_ok=True)
    out = procs.open_rotating(s.paths.logs() / "supervisor.stderr.log", s.supervisor.child_log_max_bytes,
                              s.supervisor.child_log_backups)
    env = {**os.environ, "TS_LOG_CONSOLE": "0", "PYTHONFAULTHANDLER": "1", "PYTHONUNBUFFERED": "1"}
    t0 = now_ms()
    try:
        p = spawn_outside([console_python(), "-m", "tradingsystem", "run", *run_args], cwd=PROJECT_ROOT,
                          console=True, env=env, stdout=out)
    finally:
        out.close()
    deadline = time.monotonic() + START_WAIT_S
    while time.monotonic() < deadline:
        st = read_state(data)
        if st and int(st.get("heartbeat_ms", 0)) >= t0 and not st.get("stopping"):
            return _say(0, f"supervisor{' ' + inst if inst else ''} started (pid {st.get('pid')}) - dashboard "
                           f"http://{s.api.host}:{s.api.port}")
        code = p.poll()
        if code is not None:
            if code == 3:
                return _say(0, "supervisor already running (another start won the race)")
            logs = s.paths.logs()
            return _say(1, f"supervisor exited at start (code {code}) - see {logs / 'supervisor.stderr.log'} and "
                           f"{logs / 'supervisor.jsonl'}")
        time.sleep(0.5)
    return _say(1, f"no supervisor heartbeat after {START_WAIT_S:.0f}s - see {s.paths.logs() / 'supervisor.jsonl'}")


def stop(data: Path, *, instance: str | None = None, timeout: float = STOP_WAIT_S) -> int:
    """Graceful stop via ``<state>/STOP_ALL`` (also pauses autostart); kills the supervisor tree after ``timeout``.

    STOP_ALL is written even when no guarded supervisor runs (an older build without the lock honours it too;
    the next start removes it). Only this system is stopped: ``instance`` (a pair) or the all-pairs one (None).
    """
    d = run_dir(data)
    d.mkdir(parents=True, exist_ok=True)
    (d / HOLD).write_text(str(now_ms()), encoding="utf-8")
    stop_file = Path(data) / "STOP_ALL"
    stop_file.touch()
    name = instance_name("supervisor", data)
    who = f"supervisor {instance}" if instance else "supervisor"
    resume = f"autostart paused until {script('start', instance)}"
    if not _running(name, instance):
        return _say(0, f"{who} is not running ({resume})")
    _say(None, f"stop requested ({who}) - waiting for the services to shut down ...")
    if _wait(lambda: not _running(name, instance), timeout, 1.0):
        return _say(0, f"{who} stopped ({resume})")
    proc = state_process(read_state(data))
    pids = [proc.pid] if proc is not None else procs.other_supervisors(instance=instance, scope="exact")
    if not pids:
        return _say(1, f"still running after {timeout:.0f}s and its pid is unknown - see logs/supervisor.jsonl")
    _say(None, f"no clean exit after {timeout:.0f}s - killing supervisor pid {pids} and its services")
    for pid in pids:
        procs.kill_tree(pid)
    ok = _wait(lambda: not _running(name, instance), 20)
    stop_file.unlink(missing_ok=True)
    return _say(0 if ok else 1, "supervisor killed" if ok else "could not stop the supervisor")


def _collectors(app_db: Path) -> list[tuple]:
    if not app_db.exists():
        return []
    try:
        con = sqlite3.connect(f"file:{app_db.as_posix()}?mode=ro", uri=True, timeout=2)
        try:
            return con.execute("SELECT collector, state, updated_ms FROM collector_status ORDER BY collector").fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return []


def status(data: Path, s: Settings) -> int:
    """Human summary: supervisor, services, MT5 terminal(s), collectors, dashboard URL. Read-only."""
    inst = s.paths.instance
    st, running = read_state(data), instance_running(instance_name("supervisor", data))
    others = procs.running_supervisors()
    older = [] if running else [pid for pid, i in others.items() if i == inst]
    age = heartbeat_age(st)
    out: list[str] = [f"system     : {inst + ' (one system for this pair)' if inst else 'all pairs'}  "
                      f"state {data}  logs {s.paths.logs()}"]
    if older:
        out.append(f"supervisor : running WITHOUT the single-instance lock (older build), pid {older} - "
                   f"restart it: {script('stop', inst)} then {script('start', inst)}")
    elif running and st:
        hung = "  <-- NO HEARTBEAT (hung?)" if age is not None and age > 60 else ""
        out.append(f"supervisor : running, pid {st.get('pid')}, up {_dur((now_ms() - st['started_ms']) / 1000)}, "
                   f"heartbeat {_ago(age)}{hung}")
        for n, c in (st.get("services") or {}).items():
            out.append(f"  {n:<15} pid {c.get('pid') or '-':<7} restarts {c.get('restarts', 0)}")
        if st.get("last_gap"):
            g = st["last_gap"]
            out.append(f"  last {g.get('kind')}: {g.get('seconds')}s at {time.strftime('%Y-%m-%d %H:%M:%SZ', time.gmtime(g.get('ts', 0) / 1000))}")
    else:
        out.append("supervisor : running (no state file yet)" if running else "supervisor : not running")
    if (run_dir(data) / HOLD).exists():
        out.append(f"autostart  : paused (stopped by the user; {script('start', inst)} resumes it)")
    rest = {pid: i for pid, i in others.items() if i != inst}
    if rest:
        out.append(f"other      : {procs.describe(rest)}")
    paths = {s.mt5_data_profile().terminal_path}
    if s.execution.mode in ("demo", "live"):
        paths.add(s.mt5.profiles[s.mt5.execution_profile_by_mode[s.execution.mode]].terminal_path)
    in_job = {p: v.get("in_supervisor_job") for p, v in ((st or {}).get("terminals") or {}).items()} if running else {}
    for path, pids in procs.find_terminals(sorted(paths)).items():
        warn = "  <-- inside the supervisor job (started by a service)" if in_job.get(path) else ""
        out.append(f"MT5        : {'running pid ' + ','.join(map(str, pids)) if pids else 'NOT running'}  ({path}){warn}")
    for col, state, upd in _collectors(Path(data) / "app.db"):
        out.append(f"  {col:<20} {state:<14} heartbeat {(now_ms() - upd) / 1000:.0f}s ago")
    out.append(f"dashboard  : http://{s.api.host}:{s.api.port}")
    print("\n".join(out))
    return 0 if running or older else 1
