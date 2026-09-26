"""Supervisor: ``python -m tradingsystem run all`` — one command runs every layer (spec §11.7, P5.1).

* Each service is its own OS process (D-005): ingest-binance, ingest-mt5, engine, executor, api.
* All children are placed in a Windows Job Object with KILL_ON_JOB_CLOSE, so nothing is orphaned when the
  supervisor exits (children's own worker processes are included).
* Watchdog: a dead child is restarted with exponential backoff; a child whose heartbeat in ``collector_status``
  has not changed for ``stale_s`` of *awake* time (e.g. a hung MT5 call, D-019) is killed and restarted.
  PC sleep and wall-clock steps never count: staleness runs on the unbiased interrupt time, and a detected
  suspend / clock jump / loop stall resets every baseline and grants ``RESUME_GRACE_S`` (F2/OPS-05) before
  anyone can be judged stale again. Unreadable heartbeats fail open, loudly.
* The MT5 terminal is started outside the job/tree and is never killed with a child (OPS-04).
* One supervisor per system (named mutex on its state dir, OPS-03): the all-pairs system (state ``data``) or one
  system per pair (``--instance BTCUSDT``, state ``data/instances/BTCUSDT``, D-042) — pairs run next to each other,
  never next to the all-pairs system. Own heartbeat in ``<state>/run/supervisor.json`` and the
  ``collector_status`` row "supervisor" (dashboard); idle sleep blocked while running (H2).
* Systems sharing one MT5 terminal launch it under a machine-wide file lock (never two copies).
* Child stdout/stderr → ``logs/<service>.stderr.log`` (rolled at each start); exit events carry its tail.
* Ctrl+C / STOP file (``<state>/STOP_ALL``): graceful stop (CTRL_BREAK), then terminate after a timeout.
* ``run --detach | --stop | --status``: see :mod:`.control` (scripts/*.bat, docs/ops_windows.md).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sqlite3
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

import psutil

from ..core.filelock import FileLock, locks_dir
from ..core.logsetup import get_redactor, setup_from_settings, setup_logging
from ..core.settings import PROJECT_ROOT, Settings, load_settings
from ..core.timeutil import now_ms
from . import control, procs
from .winops import JobObject, acquire_instance, in_job, instance_name, keep_awake, sample_clock, time_gap

__all__ = ["SERVICES", "Child", "JobObject", "Supervisor", "main"]
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
RESUME_GRACE_S = 180            # after a suspend / clock jump / stall: network, MT5 and disks need time to recover
LOOP_S = 5.0
UNREADABLE_ALERT = 6            # consecutive unreadable heartbeat passes (≈30 s) before an event
TERMINAL_CHECK_S = 15.0         # how often a missing MT5 terminal is looked for / relaunched
TERMINAL_RELAUNCH_S = 60.0      # minimum awake time between two launches of the same terminal
TERMINAL_APPEAR_S = 10.0        # the launch lock is held until the new terminal shows (another system then sees it)
_STATUS_DDL = ("CREATE TABLE IF NOT EXISTS collector_status (collector TEXT PRIMARY KEY, state TEXT NOT NULL, "
               "last_data_ms INTEGER, last_error TEXT, last_error_ms INTEGER, detail TEXT, updated_ms INTEGER NOT NULL)")
_EVENTS_DDL = ("CREATE TABLE IF NOT EXISTS ingestion_events (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, "
               "collector TEXT NOT NULL, event TEXT NOT NULL, detail TEXT, duration_ms INTEGER)")


@dataclass
class Child:
    name: str
    args: list[str]
    beats: list[str]
    stale_s: int
    proc: subprocess.Popen | None = None
    started: float = 0.0                                        # awake clock (s) at start
    restarts: int = 0
    backoff: float = 2.0
    next_start: float = 0.0                                     # awake clock (s)
    seen: dict[str, int] = field(default_factory=dict)          # collector → last updated_ms seen
    changed: dict[str, float] = field(default_factory=dict)     # collector → awake time it last changed
    unreadable: int = 0                                         # consecutive passes without a readable heartbeat
    log_path: Path | None = None
    log_offset: int = 0
    history: list[str] = field(default_factory=list)


def _code(code: int) -> str:
    """Exit code, plus hex for Windows NTSTATUS values (0xC0000005 = access violation)."""
    return f"{code} (0x{code & 0xFFFFFFFF:08X})" if code < 0 or code > 0xFFFF else str(code)


class Supervisor:
    kill_tree = staticmethod(procs.kill_tree)       # spares MT5 terminals (OPS-04)

    def __init__(self, services: list[str], data_dir: str | None) -> None:
        self.s = with_data_dir(load_settings(), data_dir)
        self.cfg = self.s.supervisor
        self.data = self.s.paths.data()                     # market data (shared by every system)
        self.state = self.s.paths.state()                   # this system's app.db, run/, STOP_ALL
        self.state.mkdir(parents=True, exist_ok=True)
        self.app_db = self.state / "app.db"
        self.children = {n: Child(n, SERVICES[n][0] + (["--data-dir", str(self.data)] if data_dir and n.startswith("ingest") else []),
                                  SERVICES[n][1], SERVICES[n][2]) for n in services}
        self.job = JobObject()
        self.stop = False
        self.clock, self.sleep = sample_clock, time.sleep     # injectable (tests)
        self.started_ms = now_ms()
        try:
            self.create_time = psutil.Process().create_time()
        except psutil.Error:
            self.create_time = 0.0
        self.last_gap: dict | None = None
        self.grace_until = 0.0                      # awake clock: no stale kills before this (after a gap)
        self.term_for = self._terminal_paths() if self.cfg.manage_mt5_terminal else {}
        self.term_info: dict[str, dict] = {}
        self._term_launch: dict[str, tuple[float, str]] = {}
        self._term_warned: set = set()
        self._next_term_check = 0.0
        self._state_err = 0

    # ------------------------------------------------------------------ children
    def _terminal_paths(self) -> dict[str, str]:
        """service → MT5 terminal path it attaches to (only services that will call ``mt5.initialize``)."""
        out = {}
        if "ingest-mt5" in self.children:
            out["ingest-mt5"] = self.s.mt5_data_profile().terminal_path
        mode = self.s.execution.mode
        if "executor" in self.children and mode in ("demo", "live"):
            out["executor"] = self.s.mt5.profiles[self.s.mt5.execution_profile_by_mode[mode]].terminal_path
        return out

    def _python(self) -> str:
        return control.console_python()

    def start(self, c: Child) -> None:
        if c.name in self.term_for:                 # we start the terminal (outside the job), not the child
            try:
                self.ensure_terminals(wait_s=30)
            except Exception:  # noqa: BLE001
                log.exception("MT5 terminal check failed")
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        # children log to logs/<name>.jsonl only; raw stdout/stderr (tracebacks before logging is set up, native
        # faults via PYTHONFAULTHANDLER) go to logs/<name>.stderr.log instead of the void (F8/OPS-06)
        env = {**os.environ, "TS_LOG_CONSOLE": "0", "PYTHONFAULTHANDLER": "1", "PYTHONUNBUFFERED": "1"}
        if self.cfg.manage_mt5_terminal:
            env["TS_MT5_NO_LAUNCH"] = "1"          # services never launch the terminal themselves (OPS-04)
        c.log_path = self.s.paths.logs() / f"{c.name}.stderr.log"
        fh = procs.open_rotating(c.log_path, self.cfg.child_log_max_bytes, self.cfg.child_log_backups)
        try:
            c.log_offset = fh.tell()
            c.proc = subprocess.Popen([self._python(), "-m", "tradingsystem", *c.args], cwd=PROJECT_ROOT, env=env,
                                      creationflags=flags, stdin=subprocess.DEVNULL, stdout=fh,
                                      stderr=subprocess.STDOUT)
        finally:
            fh.close()
        self.job.add(c.proc.pid)
        now = self.clock().awake
        c.started, c.unreadable = now, 0
        beats, _ = self._read_beats(c.beats)
        c.seen = dict(beats or {})                  # values left by a previous instance do not count as alive
        c.changed = {b: now for b in c.beats}
        log.info("started %s (pid %d)", c.name, c.proc.pid)
        self._event(c.name, "started", f"pid {c.proc.pid}")

    def _db(self, *stmts: tuple[str, tuple]) -> None:
        try:
            con = sqlite3.connect(self.app_db, timeout=5)
            try:
                for sql, params in stmts:
                    con.execute(sql, params)
                con.commit()
            finally:
                con.close()
        except sqlite3.Error:
            pass

    def _event(self, name: str, event: str, detail: str, duration_ms: int | None = None) -> None:
        self._db((_EVENTS_DDL, ()), ("INSERT INTO ingestion_events(ts, collector, event, detail, duration_ms) VALUES (?,?,?,?,?)",
                                     (now_ms(), f"supervisor:{name}", event, detail, duration_ms)))

    def _read_beats(self, collectors: list[str]) -> tuple[dict[str, int] | None, str | None]:
        """``({collector: updated_ms}, None)`` or ``(None, reason)`` when app.db cannot be read."""
        if not collectors:
            return {}, None
        if not self.app_db.exists():
            return None, "app.db missing"
        try:
            con = sqlite3.connect(f"file:{self.app_db.as_posix()}?mode=ro", uri=True, timeout=5)
            try:
                rows = con.execute(f"SELECT collector, updated_ms FROM collector_status WHERE collector IN "
                                   f"({','.join('?' * len(collectors))})", collectors).fetchall()
            finally:
                con.close()
        except sqlite3.Error as exc:
            return None, f"sqlite: {exc}"
        return {r[0]: int(r[1]) for r in rows}, None

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
        now = self.clock().awake
        wanted = sorted({b for c in self.children.values() if c.proc is not None and c.stale_s for b in c.beats})
        beats, err = self._read_beats(wanted)
        for c in self.children.values():
            try:
                self._watch_child(c, now, beats, err)
            except Exception:  # noqa: BLE001 — one child's failure must not take the others down
                log.exception("watchdog error for %s", c.name)
        if self.term_for and now >= self._next_term_check:
            self._next_term_check = now + TERMINAL_CHECK_S
            try:
                self.ensure_terminals()
            except Exception:  # noqa: BLE001
                log.exception("MT5 terminal check failed")

    def _watch_child(self, c: Child, now: float, beats: dict[str, int] | None, err: str | None) -> None:
        if c.proc is None:
            if now >= c.next_start:
                self.start(c)
            return
        code = c.proc.poll()
        if code is not None:
            uptime = now - c.started
            c.backoff = 2.0 if uptime > 300 else min(c.backoff * 2, 120)
            c.restarts += 1
            tail = get_redactor()(procs.tail(c.log_path, c.log_offset))[:600]
            log.warning("%s exited (code %s after %.0fs) — restart in %.0fs%s", c.name, _code(code), uptime,
                        c.backoff, f"; output: {tail}" if tail else "")
            self._event(c.name, "exited", f"code {_code(code)} after {uptime:.0f}s; restart in {c.backoff:.0f}s"
                        + (f"; output: {tail}" if tail else ""))
            c.proc, c.next_start = None, now + c.backoff
            return
        if not c.stale_s:
            return
        missing = [b for b in c.beats if beats is not None and b not in beats]
        if beats is None or missing:
            if now - c.started > STARTUP_GRACE_S:
                self._unreadable(c, err or f"no collector_status row for {', '.join(missing)}")
            return
        if c.unreadable:
            log.info("heartbeat of %s readable again after %d passes", c.name, c.unreadable)
            c.unreadable = 0
        for b in c.beats:
            if c.seen.get(b) != beats[b]:
                c.seen[b], c.changed[b] = beats[b], now
        if now - c.started <= STARTUP_GRACE_S or now < self.grace_until:
            return
        age = max(now - c.changed.get(b, c.started) for b in c.beats)
        if age > c.stale_s:
            wall_age = (now_ms() - min(beats[b] for b in c.beats)) / 1000
            self.kill(c, f"heartbeat unchanged for {age:.0f}s of awake time (> {c.stale_s}s; wall age {wall_age:.0f}s)")
            c.proc, c.next_start = None, now + 2

    def _unreadable(self, c: Child, reason: str) -> None:
        """Fail open (never kill on an unreadable heartbeat — app.db trouble would restart-loop everything), loudly."""
        c.unreadable += 1
        if c.unreadable == 1 or c.unreadable % 60 == 0:
            log.warning("heartbeat of %s unreadable (%s) — not judged stale", c.name, reason)
        if c.unreadable == UNREADABLE_ALERT:
            log.error("heartbeat of %s unreadable for %d passes: %s", c.name, c.unreadable, reason)
            self._event(c.name, "heartbeat_unreadable", reason[:300])

    def on_gap(self, kind: str, seconds: float) -> None:
        """PC slept / wall clock stepped / our own loop stalled: nobody's heartbeat could have moved — restart the
        staleness clocks and hold stale kills for ``RESUME_GRACE_S`` so a healthy child is never killed for it
        (network and MT5 reconnect after a resume); a child still hung when the grace ends is killed then."""
        now = self.clock().awake
        text = {"suspend": f"PC was asleep for ~{seconds:.0f}s", "clock_jump": f"wall clock stepped {seconds:+.0f}s",
                "stall": f"supervisor loop took {seconds:.0f}s (machine starved?)"}.get(kind, f"{kind} {seconds:.0f}s")
        log.warning("%s — heartbeat baselines reset, no stale kills for %ds", text, RESUME_GRACE_S)
        self.last_gap = {"ts": now_ms(), "kind": kind, "seconds": round(seconds, 1)}
        self.grace_until = now + RESUME_GRACE_S
        self._event("all", {"suspend": "system_suspend"}.get(kind, kind), text, int(abs(seconds) * 1000))
        for c in self.children.values():
            c.changed = {b: now for b in c.beats}

    # ------------------------------------------------------------------ MT5 terminal (OPS-04)
    def ensure_terminals(self, wait_s: float = 0.0) -> None:
        """Start every needed MT5 terminal that is not running — outside our job and child trees."""
        paths = sorted(set(self.term_for.values()))
        found = procs.find_terminals(paths)
        now = self.clock().awake
        data_path = self.term_for.get("ingest-mt5")
        for path in paths:
            pids = found.get(path, [])
            if pids:
                self._check_terminal_job(path, pids)
                continue
            self.term_info[path] = {"pids": [], "in_supervisor_job": False}
            if not Path(path).exists():
                if path not in self._term_warned:
                    self._term_warned.add(path)
                    log.error("MT5 terminal %s not found on disk — cannot start it", path)
                continue
            last = self._term_launch.get(path)
            if last and now - last[0] < TERMINAL_RELAUNCH_S:
                continue
            # every system on this machine checks the same terminal: one launches it, the others see it (D-042)
            lock = FileLock(locks_dir(self.s) / "mt5_terminal.lock")
            if not lock.acquire(timeout=wait_s or 0.0):
                continue                            # another system is starting it right now
            try:
                if procs.find_terminals([path])[path]:
                    continue                        # started by another system meanwhile
                # the scheduled task (Task Scheduler creates it: no parent of ours, no job of ours); if a task start
                # did not bring it up last time, start it detached instead
                task = self.cfg.mt5_task if path == data_path and not (last and last[1].startswith("task")) else None
                via = procs.launch_terminal(path, task)
                self._term_launch[path] = (now, via)
                log.warning("MT5 terminal was not running — started it (%s): %s", via, path)
                self._event("mt5", "terminal_started", f"{via}: {path}")
                deadline = time.monotonic() + max(wait_s, TERMINAL_APPEAR_S)
                while time.monotonic() < deadline and not procs.find_terminals([path])[path]:
                    time.sleep(0.5)
            finally:
                lock.release()

    def _check_terminal_job(self, path: str, pids: list[int]) -> None:
        inside = [p for p in pids if self.job.handle is not None and in_job(p, self.job.handle)]
        self.term_info[path] = {"pids": pids, "in_supervisor_job": bool(inside)}
        for pid in inside:
            if pid not in self._term_warned:
                self._term_warned.add(pid)
                msg = (f"MT5 terminal pid {pid} was started by a service and sits inside the supervisor job: a "
                       "supervisor crash would close it. Close it once; the supervisor restarts it outside.")
                log.error(msg)
                self._event("mt5", "terminal_in_job", msg)

    def _release_terminals(self) -> None:
        """At shutdown: if a terminal ended up in our job, kill every *other* member and drop kill-on-close."""
        members = self.job.pids()
        terms = []
        for pid in members:
            try:
                if procs.is_terminal(psutil.Process(pid)):
                    terms.append(pid)
            except psutil.Error:
                pass
        if not terms:
            return
        spared = set(terms)
        for pid in terms:
            try:
                spared.update(c.pid for c in psutil.Process(pid).children(recursive=True))
            except psutil.Error:
                pass
        others = []
        for pid in members:
            if pid not in spared:
                try:
                    p = psutil.Process(pid)
                    p.kill()
                    others.append(p)
                except psutil.Error:
                    pass
        psutil.wait_procs(others, timeout=5)
        if self.job.keep_members_on_close():
            log.warning("MT5 terminal pid(s) %s were inside the supervisor job — left running", sorted(terms))

    # ------------------------------------------------------------------ own liveness
    def _beat(self, state: str = "live") -> None:
        """Heartbeat: data/run/supervisor.json (lock-free, for scripts/Task Scheduler) + collector_status row."""
        children = {c.name: {"pid": c.proc.pid if c.proc else None, "restarts": c.restarts}
                    for c in self.children.values()}
        st = {"pid": os.getpid(), "create_time": self.create_time, "started_ms": self.started_ms,
              "heartbeat_ms": now_ms(), "awake_s": round(self.clock().awake, 3), "data_dir": str(self.data),
              "instance": self.s.paths.instance, "state_dir": str(self.state),
              "stopping": state != "live", "services": children, "terminals": self.term_info,
              "last_gap": self.last_gap}
        try:
            control.write_state(self.state, st)
            self._state_err = 0
        except OSError as exc:
            self._state_err += 1
            if self._state_err in (1, 60):
                log.warning("cannot write %s: %s", control.run_dir(self.state) / control.STATE, exc)
        detail = json.dumps({"pid": os.getpid(), "children": {n: v["pid"] for n, v in children.items()},
                             "restarts": {n: v["restarts"] for n, v in children.items()}, "last_gap": self.last_gap})
        self._db((_STATUS_DDL, ()),
                 ("INSERT INTO collector_status(collector, state, last_data_ms, detail, updated_ms) VALUES ('supervisor',?,?,?,?) "
                  "ON CONFLICT(collector) DO UPDATE SET state=excluded.state, last_data_ms=excluded.last_data_ms, "
                  "detail=excluded.detail, updated_ms=excluded.updated_ms", (state, now_ms(), detail, now_ms())))

    # ------------------------------------------------------------------ lifecycle
    def shutdown(self) -> None:
        log.info("stopping all services")
        self._beat("stopped")
        for c in self.children.values():
            if c.proc and c.proc.poll() is None:
                try:
                    c.proc.send_signal(signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGTERM)
                except OSError:
                    pass
        deadline = time.monotonic() + 15
        for c in self.children.values():
            if c.proc:
                try:
                    c.proc.wait(timeout=max(0.1, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    pass
                self.kill_tree(c.proc.pid)
        try:
            self._release_terminals()
        except Exception:  # noqa: BLE001
            log.exception("releasing MT5 terminals from the job failed")
        self._event("all", "stopped", "supervisor shutdown")
        self._beat("stopped")

    def run(self) -> None:
        stop_file = self.state / "STOP_ALL"
        stop_file.unlink(missing_ok=True)
        awake = self.cfg.keep_awake and keep_awake(True)
        if awake:
            log.info("idle sleep blocked while running (lid close / power button: docs/ops_windows.md)")
        self._beat()
        try:
            while not self.stop:
                self.watch()
                self._beat()
                if stop_file.exists():
                    log.info("STOP_ALL file found")
                    stop_file.unlink(missing_ok=True)
                    break
                before = self.clock()           # measure the sleep only: kill()/DB waits in watch() never count
                self.sleep(LOOP_S)
                gap = time_gap(before, self.clock(), LOOP_S)
                if gap:
                    self.on_gap(*gap)
        except KeyboardInterrupt:
            pass
        finally:
            self.shutdown()
            if awake:
                keep_awake(False)


def with_data_dir(s: Settings, data_dir: str | None) -> Settings:
    """``--data-dir`` moves the data root; the system (``paths.instance``) stays the same."""
    if not data_dir:
        return s
    return s.model_copy(update={"paths": s.paths.model_copy(update={"data_dir": str(data_dir)})})


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="tradingsystem run", epilog="--instance PAIR (any command): one system for that "
                                 "pair (D-042), e.g. `run all --detach --instance BTCUSDT`")
    ap.add_argument("target", nargs="?", default="all", help="all | comma-separated services "
                    f"({', '.join(SERVICES)})")
    ap.add_argument("--without", default="", help="comma-separated services to skip")
    ap.add_argument("--data-dir")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--detach", action="store_true", help="start in the background (hidden console) and return")
    g.add_argument("--stop", action="store_true", help="graceful stop via <state>/STOP_ALL (also pauses autostart)")
    g.add_argument("--status", action="store_true", help="show supervisor, services and MT5 terminal status")
    ap.add_argument("--auto", action="store_true", help="with --detach: autostart keep-alive (respects --stop)")
    args = ap.parse_args(argv)
    names = list(SERVICES) if args.target == "all" else [x for x in args.target.split(",") if x]
    names = [n for n in names if n not in set(args.without.split(","))]
    unknown = [n for n in names if n not in SERVICES]
    if unknown:
        print(f"unknown services: {unknown}")
        return 2
    s = with_data_dir(load_settings(), args.data_dir)
    inst = s.paths.instance
    state = s.paths.state()
    if args.status:
        return control.status(state, s)
    if args.stop or args.detach:
        setup_logging("supervisor-ctl", logs_dir=s.paths.logs(), level=s.logging.level, max_bytes=s.logging.max_bytes,
                      backups=s.logging.backups, console=False, secret_env_names=s.secret_env_names())
        if args.stop:
            return control.stop(state, instance=inst)
        run_args = [args.target] + (["--without", args.without] if args.without else []) + \
                   (["--data-dir", str(s.paths.data().resolve())] if args.data_dir else []) + \
                   (["--instance", inst] if inst else [])      # it runs in PROJECT_ROOT; the pair is on its command line
        return control.detach(run_args, state, s, auto=args.auto)
    setup_from_settings("supervisor", s)
    if not acquire_instance(instance_name("supervisor", state), wait_s=10):
        log.error("another supervisor is already running for %s — not starting (%s)", state,
                  control.script("status", inst))
        return 3
    # an older build holds no lock, and the all-pairs system never runs next to a per-pair one (OPS-03, D-042)
    found = {pid: i for pid, i in procs.running_supervisors(older_s=1.0).items() if procs.conflicts(inst, i)}
    if found:
        log.error("not starting: %s", control.conflict_message(inst, found))
        return 3
    log.info("supervisor starting%s: %s", f" for {inst}" if inst else "", ", ".join(names))
    Supervisor(names, args.data_dir).run()
    return 0
