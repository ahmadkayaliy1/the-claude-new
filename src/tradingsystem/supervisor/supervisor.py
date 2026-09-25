"""Supervisor: ``python -m tradingsystem run all`` — one command runs every layer (spec §11.7, P5.1).

* Each service is its own OS process (D-005): ingest-binance, ingest-mt5, engine, executor, api.
* All children are placed in a Windows Job Object with KILL_ON_JOB_CLOSE, so nothing is orphaned when the
  supervisor exits (children's own worker processes are included).
* Watchdog: a dead child is restarted with exponential backoff; a child whose heartbeat in
  ``collector_status`` is stale (e.g. a hung MT5 call, D-019) is killed and restarted.
* Ctrl+C / STOP file (``data/STOP_ALL``): graceful stop (CTRL_BREAK), then terminate after a timeout.
"""
from __future__ import annotations

import argparse
import ctypes
import logging
import os
import signal
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import psutil

from ..core.logsetup import setup_from_settings
from ..core.settings import PROJECT_ROOT, load_settings
from ..core.timeutil import now_ms

log = logging.getLogger("supervisor")

SERVICES = {
    # name: (args, heartbeat collectors, stale threshold s)
    "ingest-binance": (["ingest", "--source", "binance"], ["binance_spot", "binance_usdm"], 120),
    "ingest-mt5": (["ingest", "--source", "mt5"], ["mt5"], 90),
    "engine": (["engine"], ["engine"], 120),
    "executor": (["executor"], ["executor"], 90),
    "api": (["api"], [], 0),
}
STARTUP_GRACE_S = 180


@dataclass
class Child:
    name: str
    args: list[str]
    beats: list[str]
    stale_s: int
    proc: subprocess.Popen | None = None
    started: float = 0.0
    restarts: int = 0
    backoff: float = 2.0
    next_start: float = 0.0
    history: list[str] = field(default_factory=list)


class JobObject:
    """Windows job object: children die with the supervisor (no orphans)."""

    def __init__(self) -> None:
        self.handle = None
        if os.name != "nt":
            return
        k32 = ctypes.windll.kernel32
        self.handle = k32.CreateJobObjectW(None, None)

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [(n, ctypes.c_ulonglong) for n in ("r", "w", "o", "rb", "wb", "ob")]

        class BASIC(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", ctypes.c_uint32), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", ctypes.c_uint32),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", ctypes.c_uint32),
                        ("SchedulingClass", ctypes.c_uint32)]

        class EXTENDED(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BASIC), ("IoInfo", IO_COUNTERS),
                        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

        info = EXTENDED()
        info.BasicLimitInformation.LimitFlags = 0x2000          # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        k32.SetInformationJobObject(self.handle, 9, ctypes.byref(info), ctypes.sizeof(info))

    def add(self, pid: int) -> None:
        if self.handle is None:
            return
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x1F0FFF, False, pid)                  # PROCESS_ALL_ACCESS
        if h:
            k32.AssignProcessToJobObject(self.handle, h)
            k32.CloseHandle(h)


class Supervisor:
    def __init__(self, services: list[str], data_dir: str | None) -> None:
        self.s = load_settings()
        self.data = Path(data_dir) if data_dir else self.s.paths.data()
        self.app_db = self.data / "app.db"
        self.children = {n: Child(n, SERVICES[n][0] + (["--data-dir", str(self.data)] if data_dir and n.startswith("ingest") else []),
                                  SERVICES[n][1], SERVICES[n][2]) for n in services}
        self.job = JobObject()
        self.stop = False

    def _python(self) -> str:
        return sys.executable

    def start(self, c: Child) -> None:
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        c.proc = subprocess.Popen([self._python(), "-m", "tradingsystem", *c.args], cwd=PROJECT_ROOT,
                                  creationflags=flags, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.job.add(c.proc.pid)
        c.started = time.time()
        log.info("started %s (pid %d)", c.name, c.proc.pid)
        self._event(c.name, "started", f"pid {c.proc.pid}")

    def _event(self, name: str, event: str, detail: str) -> None:
        try:
            con = sqlite3.connect(self.app_db, timeout=5)
            con.execute("CREATE TABLE IF NOT EXISTS ingestion_events (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, "
                        "collector TEXT NOT NULL, event TEXT NOT NULL, detail TEXT, duration_ms INTEGER)")
            con.execute("INSERT INTO ingestion_events(ts, collector, event, detail) VALUES (?,?,?,?)",
                        (now_ms(), f"supervisor:{name}", event, detail))
            con.commit()
            con.close()
        except sqlite3.Error:
            pass

    def _heartbeat_age(self, collectors: list[str]) -> float | None:
        if not collectors or not self.app_db.exists():
            return None
        try:
            con = sqlite3.connect(f"file:{self.app_db.as_posix()}?mode=ro", uri=True, timeout=5)
            rows = con.execute(f"SELECT updated_ms FROM collector_status WHERE collector IN ({','.join('?' * len(collectors))})",
                               collectors).fetchall()
            con.close()
        except sqlite3.Error:
            return None
        if len(rows) < len(collectors):
            return None
        return (now_ms() - min(r[0] for r in rows)) / 1000

    @staticmethod
    def kill_tree(pid: int) -> None:
        """Kill a process and all its descendants (the venv launcher starts the real interpreter as a child)."""
        try:
            root = psutil.Process(pid)
        except psutil.NoSuchProcess:
            return
        procs = root.children(recursive=True) + [root]
        for p in procs:
            try:
                p.kill()
            except psutil.Error:
                pass
        psutil.wait_procs(procs, timeout=10)

    def kill(self, c: Child, reason: str) -> None:
        if c.proc and c.proc.poll() is None:
            log.warning("killing %s: %s", c.name, reason)
            self.kill_tree(c.proc.pid)
            try:
                c.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
        self._event(c.name, "killed", reason)

    def watch(self) -> None:
        now = time.time()
        for c in self.children.values():
            if c.proc is None:
                if now >= c.next_start:
                    self.start(c)
                continue
            code = c.proc.poll()
            if code is not None:
                uptime = now - c.started
                c.backoff = 2.0 if uptime > 300 else min(c.backoff * 2, 120)
                c.restarts += 1
                log.warning("%s exited (code %s after %.0fs) — restart in %.0fs", c.name, code, uptime, c.backoff)
                self._event(c.name, "exited", f"code {code} after {uptime:.0f}s; restart in {c.backoff:.0f}s")
                c.proc, c.next_start = None, now + c.backoff
                continue
            if c.stale_s and now - c.started > STARTUP_GRACE_S:
                age = self._heartbeat_age(c.beats)
                if age is not None and age > c.stale_s:
                    self.kill(c, f"heartbeat stale for {age:.0f}s (> {c.stale_s}s)")
                    c.proc, c.next_start = None, now + 2

    def shutdown(self) -> None:
        log.info("stopping all services")
        for c in self.children.values():
            if c.proc and c.proc.poll() is None:
                try:
                    c.proc.send_signal(signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGTERM)
                except OSError:
                    pass
        deadline = time.time() + 15
        for c in self.children.values():
            if c.proc:
                try:
                    c.proc.wait(timeout=max(0.1, deadline - time.time()))
                except subprocess.TimeoutExpired:
                    pass
                self.kill_tree(c.proc.pid)
        self._event("all", "stopped", "supervisor shutdown")

    def run(self) -> None:
        stop_file = self.data / "STOP_ALL"
        stop_file.unlink(missing_ok=True)
        try:
            while not self.stop:
                self.watch()
                if stop_file.exists():
                    log.info("STOP_ALL file found")
                    stop_file.unlink(missing_ok=True)
                    break
                time.sleep(5)
        except KeyboardInterrupt:
            pass
        finally:
            self.shutdown()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="tradingsystem run")
    ap.add_argument("target", nargs="?", default="all", help="all | comma-separated services "
                    f"({', '.join(SERVICES)})")
    ap.add_argument("--without", default="", help="comma-separated services to skip")
    ap.add_argument("--data-dir")
    args = ap.parse_args(argv)
    names = list(SERVICES) if args.target == "all" else [x for x in args.target.split(",") if x]
    names = [n for n in names if n not in set(args.without.split(","))]
    unknown = [n for n in names if n not in SERVICES]
    if unknown:
        print(f"unknown services: {unknown}")
        return 2
    s = load_settings()
    setup_from_settings("supervisor", s)
    log.info("supervisor starting: %s", ", ".join(names))
    Supervisor(names, args.data_dir).run()
    return 0
