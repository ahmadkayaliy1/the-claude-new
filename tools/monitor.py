"""Pure-Python monitor (no Claude, no network of its own): ``python tools/monitor.py [--quiet] [--json] [--dry-run]``.

Task Scheduler runs it every 15 min (``TradingSystemOps-Monitor`` → ``pythonw tools\\monitor.py --quiet``, H19);
``scripts\\monitor.bat`` runs it by hand. It watches production without the desktop app: which systems it checks follow
``tools/health_report.py:systems()`` (the all-pairs system when it runs, else every running or expected pair). Each
run reads the systems' ``app.db`` read-only, ``supervisor.json``, the shared account peak file and its own state
``data/shared/monitor_state.json``; every finding goes to ``core.notify`` with a stable dedupe key. Protective actions
are file writes only: a pair's ``data/instances/<PAIR>/KILL_SWITCH`` (order burst, daily loss) or the global
``data/KILL_SWITCH`` (equity drop between runs). It never restarts anything (an MT5 IPC hang stays a manual decision)
and never turns a switch OFF (``scripts\\kill_switch_off.bat``). With at least one new warning and no diagnosis for
``monitor.diagnose_every_hours`` it starts one Claude diagnosis session (``tools/operator/run_session.py --kind
diagnose --reason <the new findings>``, detached) unless ``monitor.diagnose_enabled`` is false,
``TS_MONITOR_NO_DIAGNOSE`` is set, the day's budget is used up (``monitor.diagnose_max_per_day`` sessions per UTC day),
the usage gauge is above ``monitor.diagnose_max_gauge_level`` (or cannot be read) or an operator session (a review)
holds the session lock.

Phase 5 (A3/A4) machine rules: the laptop on battery (longer than ``on_battery_warn_min``, below ``battery_warn_pct``,
critical below ``battery_critical_pct``), the Windows commit charge above ``commit_warn_pct`` and the P1.12 price
recorder without a flush for ``recorder_stall_min`` (docs/monitoring.md §3).

Suspend-aware: after a sleep, a reboot or a clock jump every wall-clock heartbeat looks stale by the time asleep.
The age of a beat older than the supervisor's last suspend is corrected by the time asleep, and while a system is
"settling" (booted, resumed or clock-jumped within the stale window, or the machine slept since the last run) a
staleness finding must be seen on two consecutive runs at least 5 min apart before it is reported; a "no supervisor
runs" note always must (after any logon the keep-alive tasks start the supervisors 90-150 s late), and while it waits
so do that pair's own staleness findings.

An event is handled once whatever the notification did (a burst counted, the equity baseline moved on). A notification
that reached no sink (the last flush timed out, every sink failed) is kept in ``pending_notify`` and re-sent by the
next runs under a ``:retry<n>`` key, at most ``NOTIFY_TRIES`` attempts in all; a re-send is never a new finding.

Exit code: 0 no warning, 1 a warning or worse (the task's "Last Result"), 3 the config could not be read (nothing was
checked; ``logs/monitor-config-error.log``). docs/monitoring.md has the full rule table.
"""
# No ``from __future__ import annotations``: tools are loaded by file path without a sys.modules entry (tests,
# review_pack), and dataclasses cannot resolve string annotations there.
import argparse
import datetime as dt
import hashlib
import importlib.util
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import psutil

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tradingsystem.core.filelock import FileLock, locks_dir  # noqa: E402
from tradingsystem.core.instruments import InstrumentRegistry  # noqa: E402
from tradingsystem.core.killswitch import kill_switch_path, set_kill_switch  # noqa: E402
from tradingsystem.core.logsetup import setup_logging  # noqa: E402
from tradingsystem.core.sessions import calendar_for  # noqa: E402
from tradingsystem.core.settings import Settings, load_settings  # noqa: E402
from tradingsystem.core.timeutil import MS_PER_DAY, MS_PER_HOUR, MS_PER_MINUTE, iso, now_ms  # noqa: E402
from tradingsystem.execution import drawdown  # noqa: E402
from tradingsystem.supervisor import control, winops  # noqa: E402

log = logging.getLogger("monitor")

STATE_FILE = "monitor_state.json"
RUN_SESSION = ROOT / "tools" / "operator" / "run_session.py"     # Claude sessions (docs/operator_sessions.md)
NO_DIAGNOSE_ENV = "TS_MONITOR_NO_DIAGNOSE"
LEVELS = {"info": 0, "warn": 1, "critical": 2}
ALL = "*"                                   # Finding.switch: the global data/KILL_SWITCH
SETTLE_MS = 5 * MS_PER_MINUTE               # while settling, a staleness must persist this long over two runs
SLEPT_S = 60.0                              # wall minus awake time between two runs above this = the machine slept
REMIND_MS = {"info": 0, "warn": 6 * MS_PER_HOUR, "critical": MS_PER_HOUR}    # a finding that persists is re-sent
# a cleared alert is remembered this long: when it comes back it persists (a flapping condition is not new each time)
COOL_MS = {"info": 2 * MS_PER_HOUR, "warn": REMIND_MS["warn"], "critical": 2 * MS_PER_HOUR}
EVERY_MS = 15 * MS_PER_MINUTE               # the task's repetition (TradingSystemOps-Monitor)
BASELINE_MAX_MS = max(3 * EVERY_MS, MS_PER_HOUR)    # an older equity baseline (plus the sleep since) only warns
SAME_BOOT_MS = 60_000                       # two boot times this close are the same boot
FLUSH_S = (15.0, 12.0, 240.0)               # notifier flush: base + per queued notification, capped
NOTIFY_TRIES = 3                            # attempts per undelivered notification: the first one + 2 re-sends
SESSION_LOCK = "operator_session.lock"      # tools/operator/run_session.py LOCK_NAME: one operator session at a time
WANTED_MS = 2 * MS_PER_HOUR                 # a diagnosis put off by a running review is retried this long
IPC_RE = re.compile(r"IPC|-1000[45]")       # MT5 "No IPC connection" / IPC timeout / IPC recv failed
GAP_EVENTS = ("system_suspend", "clock_jump", "stall")
PAIR_RE = re.compile(r"[A-Z0-9]{3,20}")
CONFIG_ERROR_EXIT = 3                       # load_settings() failed: nothing was checked
CONFIG_ERROR_LOG = ROOT / "logs" / "monitor-config-error.log"     # fixed: without Settings there is no logs dir
CONFIG_ERROR_LOG_MAX = 1_000_000            # bytes; then it is rotated once (.1)
CONFIG_TOAST_TIMEOUT_S = 15.0
DIAGNOSE_KEEP_MS = 2 * MS_PER_DAY           # diagnosis starts remembered this long (the per-UTC-day budget)


def _load_health_report():
    """tools/ is not a package: load the sibling health_report.py the way the tests do (its systems() is reused)."""
    here = Path(__file__).resolve().with_name("health_report.py")
    spec = importlib.util.spec_from_file_location("health_report", here)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


hr = _load_health_report()


# --------------------------------------------------------------------------- machine probes (patched by the tests)
def _clock() -> tuple[float, int]:
    """(awake seconds since boot — sleep excluded, system-wide; boot time as UTC ms)."""
    return winops.sample_clock().awake, int(psutil.boot_time() * 1000)


def _free_ram_mb() -> float:
    return psutil.virtual_memory().available / 2**20


def _free_disk_gb(path: Path) -> float:
    p = Path(path)
    while not p.exists() and p.parent != p:
        p = p.parent
    return psutil.disk_usage(str(p)).free / 1e9


def _adapters() -> dict[str, bool]:
    """Network adapter name → up (VPN watch)."""
    return {n: bool(st.isup) for n, st in psutil.net_if_stats().items()}


def _battery() -> tuple[float, bool | None] | None:
    """(percent, on mains — None when Windows cannot tell) of the laptop battery; None without one (a desktop).
    The health report's reading (GetSystemPowerStatus: an unknown AC line is None, never "on battery")."""
    return hr.battery()


def _commit() -> tuple[int, int] | None:
    """(committed bytes, commit limit) of Windows; None when it cannot be read."""
    return hr.commit_charge()


def _gauge_level(base: Settings, chosen: list[Settings], now: int) -> tuple[int | None, str]:
    """The usage gauge (ai/usage_gauge.py) over the AI ledger the systems write — the review pack's gauge on the
    review pack's ledger (the shared ``data/shared/ai_usage.db`` for per-pair systems, else the all-pairs app.db):
    ``(level, reason)``; ``(0, "no ledger yet")`` before any call; ``(None, why)`` when it cannot be read."""
    systems = chosen or [base]
    ledger = (base.paths.shared() / "ai_usage.db" if any(x.paths.instance for x in systems)
              else systems[0].paths.state() / "app.db")
    if not ledger.exists():
        return 0, "no ledger yet"
    try:
        from tradingsystem.ai.budget import UsageStore
        from tradingsystem.ai.usage_gauge import UsageGauge
    except Exception as exc:  # noqa: BLE001
        return None, f"unavailable ({type(exc).__name__})"
    store = None
    try:
        store = UsageStore(ledger)
        st = UsageGauge(base, store).state(now)
    except Exception as exc:  # noqa: BLE001
        return None, f"{type(exc).__name__}: {exc}"[:200]
    finally:
        if store is not None:
            try:
                store.close()
            except Exception:  # noqa: BLE001
                pass
    if getattr(st, "error", None):                      # the gauge says level 0 on a failed read: unknown here
        return None, str(st.error)[:200]
    return int(st.level), str(st.reason or "")


def _spawn(cmd: list[str], cwd: Path):
    """Detached, outside Task Scheduler's job (breakaway, with a fallback) and without a window: the child gets a
    hidden console of its own, which its console children (claude.exe, git) inherit — no window flashes."""
    return winops.spawn_outside(cmd, cwd=cwd, console=True)


def _notify(s: Settings, level: str, title: str, text: str, *, key: str, pair: str | None) -> dict | None:
    """core.notify (log line + toast + Telegram); a missing or failing notifier never stops the monitor. Returns the
    process notifier's result record (its worker fills in the sinks; :func:`_undelivered` reads it), None when there
    is none."""
    try:
        from tradingsystem.core import notify as nt
    except Exception:  # noqa: BLE001 — the notifier is optional for the monitor's own job
        log.warning("[notify unavailable] %s %s: %s", level, title, text)
        return None
    try:
        notifier = getattr(nt, "_DEFAULT", None)            # the one worker flush() waits for
        if notifier is None or not hasattr(notifier, "submit"):
            nt.notify(s, level, title, text, key=key, pair=pair)
            return None
        res = notifier.submit(s, level, title, text, key=key, pair=pair)
        return res if isinstance(res, dict) else None
    except Exception:  # noqa: BLE001
        log.warning("notify failed for %s", key, exc_info=True)
        return None


def _flush_notify(timeout_s: float = 15.0) -> None:
    """A short-lived process: wait for the notifier's worker (toast, Telegram) before exiting."""
    try:
        from tradingsystem.core.notify import flush
        flush(timeout_s)
    except Exception:  # noqa: BLE001
        pass


_SINK_OK = ("shown", "sent")
_SINK_LOST = ("pending", "failed", "queue full", "not queued")     # prefixes: in flight, or no delivery


def _undelivered(res: dict | None) -> bool:
    """True when a notifier result record shows that the notification reached no sink: every sink in use is still
    ``pending`` (a flush that timed out) or failed. A sink that is off or not configured does not count, so a
    notification that is a log line only by configuration counts as delivered; so does one skipped by the rate
    limit or the dedupe (a re-send has a key of its own, ``:retry<n>``). Unknown (no record) counts as delivered."""
    if not isinstance(res, dict):
        return False
    sinks = [v for v in (res.get("toast"), res.get("telegram")) if isinstance(v, str)]
    if any(v in _SINK_OK for v in sinks):
        return False
    return any(v.startswith(_SINK_LOST) for v in sinks)


# --------------------------------------------------------------------------- state file
def state_path(s: Settings) -> Path:
    return s.paths.shared() / STATE_FILE


def load_state(path: Path) -> dict:
    """The previous run's state ({} when missing or unreadable — the monitor then starts over)."""
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        return doc if isinstance(doc, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        log.warning("%s unreadable (%s) — starting from an empty state", path, exc)
        return {}


def save_state(path: Path, doc: dict) -> None:
    """Atomic: a flushed temp file replaces the state; retried while a reader (dashboard) holds it open."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, ensure_ascii=False, indent=1, sort_keys=True, default=str)
            fh.flush()
            os.fsync(fh.fileno())
        for attempt in range(10):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == 9:
                    raise
                time.sleep(0.1)
    finally:
        tmp.unlink(missing_ok=True)


# --------------------------------------------------------------------------- findings
@dataclass
class Finding:
    level: str                      # info | warn | critical
    key: str                        # stable across runs: the dedupe key (notify key = "monitor:" + key)
    title: str
    text: str
    system: str | None = None       # "BTCUSDT" | "all" (the all-pairs system) | None (machine / account)
    pair: str | None = None
    switch: str | None = None       # a pair, or ALL — the KILL_SWITCH this finding engages
    once: bool = False              # notified once, never re-sent while it persists (e.g. a drawdown trip)
    remind_ms: int | None = None    # None = REMIND_MS[level]
    action: str = ""                # what the monitor did (switch written, …)
    new: bool = False               # due for a notification this run (new, escalated or reminded)
    notified: bool = False          # … and sent (never in a dry run)

    def as_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if k not in ("once", "remind_ms")}


@dataclass
class Result:
    ts: int
    systems: list[str]
    findings: list[Finding] = field(default_factory=list)
    held: list[str] = field(default_factory=list)       # seen, but waiting for the second run (settling)
    skipped: list[str] = field(default_factory=list)
    diagnose: dict = field(default_factory=dict)
    state_error: str | None = None
    dry_run: bool = False

    @property
    def problems(self) -> list[Finding]:
        return [f for f in self.findings if LEVELS[f.level] >= 1]

    @property
    def exit_code(self) -> int:
        return 1 if self.problems or self.state_error else 0

    def as_dict(self) -> dict:
        return {"ts": self.ts, "time": iso(self.ts), "exit": self.exit_code, "systems": self.systems,
                "findings": [f.as_dict() for f in self.findings], "held": self.held, "skipped": self.skipped,
                "diagnose": self.diagnose, "state_error": self.state_error, "dry_run": self.dry_run}


@dataclass
class _Row:
    state: str
    updated_ms: int
    last_error: str
    last_error_ms: int | None
    detail: dict


def _ms(v) -> int | None:  # noqa: ANN001
    """An ISO string ("…Z" / "+00:00") or epoch ms → epoch ms; None when absent or unreadable."""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return int(v)
    if isinstance(v, str) and v:
        try:
            d = dt.datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            return None
        if d.tzinfo is None:                                  # never guess a zone
            return None
        return int(d.timestamp() * 1000)
    return None


def _num(v) -> float | None:  # noqa: ANN001
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _pair_of(detail: str | None, s: Settings) -> str | None:
    """The pair of an executor 'order' event: ``"<mode> <PAIR> <id8>: …"`` (all-pairs app.db)."""
    parts = (detail or "").split()
    if len(parts) >= 2:
        cand = parts[1].rstrip(":").upper()
        if cand in s.pairs:
            return cand
    return None


def _mins(ms: float) -> str:
    return f"{ms / MS_PER_MINUTE:.0f} min"


# --------------------------------------------------------------------------- the run
class Monitor:
    def __init__(self, base: Settings, chosen: list[Settings], notes: list[str] | None = None, *,
                 now: int | None = None, dry_run: bool = False, diagnose: bool = False) -> None:
        self.base = base
        self.chosen = chosen
        self.notes = list(notes or [])
        self.now = int(now if now is not None else now_ms())
        self.dry_run = dry_run
        self.allow_diagnose = diagnose
        self.path = state_path(base)
        self.prev = load_state(self.path)
        self.state: dict = {}
        self.res = Result(self.now, [s.paths.instance or "all" for s in chosen], dry_run=dry_run)
        self._by_key: dict[str, Finding] = {}
        self.gaps: dict[str, dict] = {}                   # system → its last suspend {ts, kind, seconds}
        self.samples: dict[str, tuple[float, int, str]] = {}    # account → (equity, updated_ms, system)
        self.accounts: set[str] = set()
        self.tripped: dict[str, int] = {}
        self.awake, self.boot_ms = 0.0, 0
        self.slept_s = 0.0                                # machine sleep since the previous run (0 = none/unknown)
        self.machine_settle: str | None = None
        self.logon_wait: dict[str, str] = {}              # pair → why its "no supervisor runs" note waits (_notes)
        # (finding key, its pending_notify entry, the notifier's result, the finding — None for a re-send)
        self._sent: list[tuple[str, dict, dict | None, Finding | None]] = []

    # ---- bookkeeping
    def add(self, f: Finding) -> bool:
        """Record a finding (one per key, the higher level wins) and engage its switch now. False when the switch
        could not be written."""
        old = self._by_key.get(f.key)
        if old is not None:
            if LEVELS[f.level] > LEVELS[old.level]:
                old.level, old.text = f.level, f.text
            return True
        self._by_key[f.key] = f
        self.res.findings.append(f)
        return self._engage(f) if f.switch else True

    def _engage(self, f: Finding) -> bool:
        pair = None if f.switch == ALL else f.switch
        if self.dry_run:
            f.action = f"dry run: would engage {kill_switch_path(self.base, pair)}"
            return True
        try:
            path, created = set_kill_switch(self.base, pair, reason=f"{f.title}: {f.text}", actor="monitor")
        except OSError as exc:
            f.level = "critical"
            f.action = f"KILL_SWITCH could not be written: {exc}"
            log.warning("%s", f.action)
            return False
        f.action = f"KILL_SWITCH {'engaged' if created else 'already on'}: {path}"
        log.warning("%s — %s", f.action, f.title)
        return True

    def _hold(self, name: str, kind: str, items: dict[str, str], settle: str | None) -> dict[str, str]:
        """While ``name`` settles, keep only the items also seen on the previous run at least SETTLE_MS ago
        (the two-run rule); the others wait for the next run. Every item's first sighting is remembered."""
        k = f"{name}:{kind}"
        prev = ((self.prev.get("seen") or {}).get(k)) or {}
        cur = {i: int(prev.get(i, self.now)) for i in items}
        self.state.setdefault("seen", {})[k] = cur
        if not settle:
            return items
        keep = {i: t for i, t in items.items() if self.now - cur[i] >= SETTLE_MS}
        for i in sorted(items.keys() - keep.keys()):
            self.res.held.append(f"{name} {kind} {i}: {settle} — checked again on the next run")
        return keep

    # ---- orchestration
    def run(self) -> Result:
        self.awake, self.boot_ms = _clock()
        self.state = {"version": 1, "first_run_ms": int(self.prev.get("first_run_ms") or self.now),
                      "ipc": {}, "seen": {}, "slow_build": {}}
        self.slept_s = self._slept_s()
        self.machine_settle = self._machine_settle()
        self._notes()
        for s in self.chosen:
            self._system(s)
        for name, fn in (("equity", self._equity), ("drawdown", self._drawdown), ("resources", self._resources),
                         ("power", self._power), ("commit", self._commit_charge), ("recorder", self._recorder),
                         ("vpn", self._vpn), ("reviews", self._reviews)):
            self._guard(None, name, fn)
        self._dispatch()
        self._deliver()
        self.state["run"] = {"ts": self.now, "awake_s": round(self.awake, 3), "boot_ms": self.boot_ms,
                             "exit": self.res.exit_code, "findings": [f.as_dict() for f in self.res.findings],
                             "held": self.res.held}
        self.res.diagnose = self._diagnose()        # saves the state itself before it starts a session
        if not self.dry_run:
            try:
                save_state(self.path, self.state)
            except OSError as exc:
                self.res.state_error = f"{self.path} could not be written: {exc}"
                log.warning("%s", self.res.state_error)
                _notify(self.base, "critical", "Monitor: state file not saved",
                        f"{self.res.state_error} — until it can be written every run re-sends its newer findings "
                        "and may start a diagnosis again; check the file's attributes and permissions "
                        "(docs/monitoring.md §8)", key="monitor:state_error", pair=None)
        return self.res

    def _notes(self) -> None:
        """health_report's notes become warnings. "No supervisor runs" always waits for the two-run rule: after any
        logon (a prompt one after a boot, one hours after an overnight update restart, a logoff/logon) the keep-alive
        tasks start the supervisors 90-150 s late, and the monitor cannot tell when the user logged on — a system that
        is really down is reported one run (15 min) later. A pair whose note waits settles for this run
        (``logon_wait``): its stale heartbeats, quotes, outages and MT5 IPC errors wait with it, and the next run
        reports a system that is really down with them. Other notes go at once."""
        notes = {hashlib.sha1(t.encode("utf-8")).hexdigest()[:12]: t
                 for t in (n[3:] if n.startswith("!! ") else n for n in self.notes)}
        down: dict[str, str] = {}                               # hold id (the pair when it names one) → note id
        for i, text in notes.items():
            if "no supervisor runs" in text:
                who = text.split(":", 1)[0].strip()
                down[who if PAIR_RE.fullmatch(who) else i] = i
        why = self.machine_settle or ("first seen on this run (the keep-alive starts a supervisor 90-150 s after "
                                      "a logon)")
        kept = self._hold("system", "not running", {h: notes[i] for h, i in down.items()}, why)
        waiting = {i for h, i in down.items() if h not in kept}
        self.logon_wait = {h: why for h, i in down.items() if h not in kept and h != i}    # h == i: no pair named
        for i, text in notes.items():
            if i not in waiting:
                self.add(Finding("warn", "note:" + i,
                                 "System not running" if "no supervisor runs" in text else "System check", text))

    def _guard(self, name: str | None, check: str, fn, *args) -> None:  # noqa: ANN001
        try:
            fn(*args)
        except Exception as exc:  # noqa: BLE001 — one broken check never hides the others
            log.warning("check %s failed for %s", check, name or "machine", exc_info=True)
            self.add(Finding("warn", f"check:{name or 'machine'}:{check}", f"Monitor check {check} failed",
                             f"{name or 'machine'}: {type(exc).__name__}: {str(exc)[:200]}", system=name))

    def _slept_s(self) -> float:
        """Seconds the machine slept since the previous run (wall minus awake time); 0 when unknown (no previous
        run, another boot)."""
        run = self.prev.get("run") or {}
        same_boot = abs(int(run.get("boot_ms") or 0) - self.boot_ms) < SAME_BOOT_MS
        if run.get("ts") and run.get("awake_s") is not None and same_boot:
            return (self.now - int(run["ts"])) / 1000 - (self.awake - float(run["awake_s"]))
        return 0.0

    def _machine_settle(self) -> str | None:
        win = self.base.monitor.stale_heartbeat_min * MS_PER_MINUTE
        if self.boot_ms and self.now - self.boot_ms < win:
            return f"booted {_mins(self.now - self.boot_ms)} ago"
        if self.slept_s > SLEPT_S:
            return f"the machine slept {self.slept_s / 60:.0f} min since the last run"
        return None

    def _settling(self, s: Settings, name: str, con: sqlite3.Connection) -> str | None:
        """Why this system's wall-clock ages cannot be trusted yet (None = they can)."""
        win = s.monitor.stale_heartbeat_min * MS_PER_MINUTE
        sup = control.read_state(s.paths.state())
        gap = (sup or {}).get("last_gap") or None
        row = con.execute("SELECT ts, event, duration_ms FROM ingestion_events WHERE collector='supervisor:all' AND "
                          f"event IN ({','.join('?' * len(GAP_EVENTS))}) ORDER BY ts DESC LIMIT 1",
                          GAP_EVENTS).fetchone()
        if row and (not gap or int(row[0]) > int(gap.get("ts") or 0)):
            gap = {"ts": int(row[0]), "kind": "suspend" if row[1] == "system_suspend" else row[1],
                   "seconds": (row[2] or 0) / 1000}
        if gap and _ms(gap.get("ts")):
            self.gaps[name] = gap
        if self.machine_settle:
            return self.machine_settle
        if gap and _ms(gap.get("ts")) and self.now - _ms(gap["ts"]) < win:
            return f"{gap.get('kind')} {_mins(self.now - _ms(gap['ts']))} ago"
        if sup and sup.get("heartbeat_ms") and "awake_s" in sup:
            awake_age = control.heartbeat_age(sup, awake_now=self.awake)
            wall_age = (self.now - int(sup["heartbeat_ms"])) / 1000
            if awake_age is not None and awake_age != float("inf") and awake_age * 1000 < win \
                    and wall_age - awake_age > SLEPT_S:
                return "the machine slept since the supervisor's last heartbeat"
        return None

    def _system(self, s: Settings) -> None:
        name = s.paths.instance or "all"
        st = s.paths.state()
        if (control.run_dir(st) / control.HOLD).exists() and control.state_process(control.read_state(st)) is None:
            self.res.skipped.append(f"{name}: stopped by the user (run/{control.HOLD})")
            return
        db = st / "app.db"
        if not db.exists():
            if s.paths.instance:        # chosen because its supervisor runs: this monitor looks at another data root
                self.add(Finding("warn", f"blind:{name}", f"{name}: monitor cannot see this system",
                                 f"its supervisor runs but {db} does not exist — the monitor runs from another "
                                 "checkout or data root (TRADINGSYSTEM_CONFIG)", system=name, pair=name))
            else:                       # the all-pairs fallback when nothing runs and nothing is expected
                self.res.skipped.append(f"{name}: {db} does not exist (never started here)")
            return
        try:
            con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=10)
        except sqlite3.Error as exc:
            self.add(Finding("warn", f"db:{name}", f"{name}: app.db unreadable", str(exc)[:200], system=name))
            return
        try:
            status: dict[str, _Row] = {}
            for c, state, upd, err, err_ms, det in con.execute(
                    "SELECT collector, state, updated_ms, last_error, last_error_ms, detail FROM collector_status"):
                try:
                    d = json.loads(det) if det else {}
                except ValueError:
                    d = {}
                status[c] = _Row(state, int(upd or 0), err or "", err_ms, d if isinstance(d, dict) else {})
            settle = None
            try:
                settle = self._settling(s, name, con)
            except Exception:  # noqa: BLE001 — unknown = not settling (report rather than stay silent)
                log.warning("settle check failed for %s", name, exc_info=True)
            settle = settle or self.logon_wait.get(name)     # its "no supervisor runs" note waits (a late logon)
            for fn in (self._heartbeats, self._ipc, self._executor, self._engine, self._bursts, self._restarts,
                       self._outages, self._quotes):
                self._guard(name, fn.__name__.lstrip("_"), fn, s, name, con, status, settle)
        except sqlite3.Error as exc:
            self.add(Finding("warn", f"db:{name}", f"{name}: app.db unreadable", str(exc)[:200], system=name))
        finally:
            con.close()

    # ---- per-system checks
    def _heartbeats(self, s: Settings, name: str, con, status: dict[str, _Row], settle: str | None) -> None:
        lim = s.monitor.stale_heartbeat_min * MS_PER_MINUTE
        gap = self.gaps.get(name) or {}
        gap_ts = _ms(gap.get("ts"))
        items: dict[str, str] = {}
        for c, r in sorted(status.items()):
            if r.state == "stopped":
                continue
            age = self.now - r.updated_ms
            if gap_ts and gap.get("kind") == "suspend" and r.updated_ms < gap_ts:
                age -= int(float(gap.get("seconds") or 0) * 1000)       # time asleep is not staleness
            if age > lim:
                items[c] = f"{c} ({r.state}) {_mins(age)}"
        items = self._hold(name, "stale", items, settle)
        if items:
            self.add(Finding("warn", f"stale:{name}:{','.join(sorted(items))}", f"{name}: stale heartbeat",
                             f"no heartbeat for more than {s.monitor.stale_heartbeat_min} min: "
                             + "; ".join(items.values())
                             + f" — scripts\\status.bat {s.paths.instance or ''}".rstrip(), system=name,
                             pair=s.paths.instance))

    def _ipc(self, s: Settings, name: str, con, status: dict[str, _Row], settle: str | None) -> None:
        if name in self.logon_wait:         # no supervisor runs (yet): the rows are from before the logon, and the
            return                          # next run reports a system that is really down (the two-run rule)
        prev = self.prev.get("ipc") or {}
        for c in ("executor", "mt5"):
            r = status.get(c)
            if r is None or r.state not in ("reconnecting", "error") or not IPC_RE.search(r.last_error):
                continue
            k = f"{name}:{c}"
            since = int(prev.get(k, self.now))
            failing = _ms(r.detail.get("failing_since"))
            if failing:
                since = min(since, failing)
            self.state["ipc"][k] = since
            if self.now - since >= s.monitor.ipc_hung_min * MS_PER_MINUTE:
                self.add(Finding("critical", f"ipc:{k}:{since}", f"{name}: MT5 IPC hung",
                                 f"{c} {r.state} for {_mins(self.now - since)}: {r.last_error[:200]} — the monitor "
                                 "never restarts the MT5 terminal; restart it by hand if it does not recover "
                                 "(docs/monitoring.md)", system=name, pair=s.paths.instance))

    def _executor(self, s: Settings, name: str, con, status: dict[str, _Row], settle: str | None) -> None:
        r = status.get("executor")
        if r is None:
            return
        d, mine = r.detail, s.paths.instance
        for x in d.get("exposure") or []:
            # 'sl' is the first leg's; 'sl_missing' (exposure.aggregate) = any leg of the decision without one
            if isinstance(x, dict) and x.get("kind") == "position" and (x.get("sl_missing") or not x.get("sl")):
                p = x.get("pair") or mine
                self.add(Finding("critical", f"nosl:{name}:{p}:{x.get('decision')}", f"{p}: position without SL",
                                 f"{x.get('side')} {x.get('volume')} @ {x.get('price')} (decision {x.get('decision')}) "
                                 + ("has a leg without a stop-loss" if x.get("sl") else "has no stop-loss")
                                 + " at the broker — set one in MetaTrader 5 now", system=name, pair=p,
                                 remind_ms=MS_PER_HOUR))
        pnl = _num(d.get("today_pnl_pct"))
        day = time.strftime("%Y-%m-%d", time.gmtime(self.now / 1000))
        if time.strftime("%Y-%m-%d", time.gmtime(r.updated_ms / 1000)) != day:
            pnl = None           # a stopped executor's row still holds an earlier UTC day's result: not today's loss
        if pnl is not None:
            mx = s.risk.max_daily_loss_pct
            band = mx - s.monitor.daily_loss_warn_margin_pct
            warn_at = -band if band > 0 else -0.8 * mx          # no band (margin ≥ limit): at 80 % of the limit
            who = mine or "every pair (all-pairs system)"
            if pnl <= -mx:
                key = f"dayloss:{name}:{day}"
                acted = key in (self.prev.get("acted") or {})
                f = Finding("critical", key, f"{who}: daily loss limit",
                            f"today {pnl:.2f} % ≤ −{mx} % (risk.max_daily_loss_pct)"
                            + (" — switch engaged earlier today, not re-engaged" if acted else ""),
                            system=name, pair=mine, switch=None if acted else (mine or ALL), once=acted)
                if self.add(f) and not acted and not self.dry_run:
                    self.state.setdefault("acted", {})[key] = self.now
            elif pnl < 0 and pnl <= warn_at:
                self.add(Finding("warn", f"dayloss_warn:{name}:{day}", f"{who}: daily loss near the limit",
                                 f"today {pnl:.2f} % (warning at {warn_at:.2f} %, limit −{mx} %)", system=name,
                                 pair=mine))
        eq = _num(d.get("equity"))
        dd = d.get("account_drawdown") if isinstance(d.get("account_drawdown"), dict) else {}
        acct = dd.get("account") or (f"paper:{mine or 'all'}" if (d.get("mode") or s.execution.mode) == "paper"
                                     else f"mt5:{d.get('mode') or s.execution.mode}:?")
        if eq is not None and eq > 0:
            old = self.samples.get(acct)
            if old is None or r.updated_ms > old[1]:           # every executor of one MT5 account reports it
                self.samples[acct] = (eq, r.updated_ms, name)
        if dd.get("account"):
            self.accounts.add(dd["account"])
            if _ms(dd.get("tripped")):
                self.tripped[dd["account"]] = _ms(dd["tripped"])

    def _engine(self, s: Settings, name: str, con, status: dict[str, _Row], settle: str | None) -> None:
        r = status.get("engine")
        sb = (r.detail.get("snapshot_build_ms") if r else None) or {}
        use = "median" if _num(sb.get("median")) is not None else "last"
        v = _num(sb.get(use))
        lim = s.monitor.snapshot_build_warn_ms
        if v is None or v <= lim:
            return
        prev = (self.prev.get("slow_build") or {}).get(name)
        self.state["slow_build"][name] = int(v)
        if prev is None:
            self.res.held.append(f"{name} snapshot build {v:.0f} ms ({use}) > {lim} ms — reported when the next run "
                                 "sees it too")
            return
        n = f" of the last {sb['n']}" if use == "median" and sb.get("n") else ""
        self.add(Finding("warn", f"snapshot:{name}", f"{name}: slow snapshot build",
                         f"the engine's payload build takes {v:.0f} ms ({use}{n} builds; previous run {prev} ms), "
                         f"above {lim} ms on two runs in a row — screening lags (RAM/CPU, docs/monitoring.md)",
                         system=name, pair=s.paths.instance))

    def _bursts(self, s: Settings, name: str, con, status: dict[str, _Row], settle: str | None) -> None:
        lim = s.monitor.order_burst_per_hour
        seen = self.prev.get("burst_seen") or {}
        keep = self.state.setdefault("burst_seen", {})
        for k, t in seen.items():                               # carried while it can still matter (the hour)
            if k.startswith(f"{name}:") and self.now - int(t) < 2 * MS_PER_HOUR:
                keep.setdefault(k, int(t))
        by: dict[str, list[int]] = {}
        for ts, det in con.execute("SELECT ts, detail FROM ingestion_events WHERE collector='executor' AND "
                                   "event='order' AND ts>=?", (self.now - MS_PER_HOUR,)):
            by.setdefault(s.paths.instance or _pair_of(det, s) or "?", []).append(int(ts))
        for p, tss in sorted(by.items()):
            k = f"{name}:{p}"
            new = [t for t in tss if t > int(keep.get(k, 0))]   # orders after the last burst this monitor acted on
            if len(new) <= lim:
                continue
            ok_pair = p != "?" and PAIR_RE.fullmatch(p) is not None and p in s.pairs
            target = p if ok_pair else ALL
            f = Finding("critical", f"burst:{name}:{p}:{max(new)}", f"{p}: order burst",
                        f"{len(new)} orders in the last hour (limit {lim}) → "
                        + (f"{p}'s KILL_SWITCH; once the orders are reviewed: scripts\\kill_switch_off.bat {p}"
                           if ok_pair else "pair unknown: the global KILL_SWITCH"),
                        system=name, pair=p if ok_pair else None, switch=target)
            if self.add(f):                 # counted whatever its notification does: never engaged twice
                keep[k] = max(new)

    def _restarts(self, s: Settings, name: str, con, status: dict[str, _Row], settle: str | None) -> None:
        lim = s.monitor.restart_loop_per_hour
        for col, n in con.execute("SELECT collector, count(*) FROM ingestion_events WHERE collector LIKE "
                                  "'supervisor:%' AND event IN ('exited','killed') AND ts>=? GROUP BY collector",
                                  (self.now - MS_PER_HOUR,)):
            if n > lim:
                svc = col.split(":", 1)[1]
                where = f"logs\\{s.paths.instance}\\" if s.paths.instance else "logs\\"
                self.add(Finding("warn", f"restart:{name}:{svc}", f"{name}: restart loop",
                                 f"{svc} exited or was killed {n}× in the last hour (limit {lim}) — see "
                                 f"{where}{svc}.stderr.log", system=name, pair=s.paths.instance))

    def _outages(self, s: Settings, name: str, con, status: dict[str, _Row], settle: str | None) -> None:
        lim = s.monitor.outage_warn_min * MS_PER_MINUTE
        down: dict[str, int] = {}
        for col, ev, ts in con.execute("SELECT collector, event, ts FROM ingestion_events WHERE event IN "
                                       "('disconnect','resumed','connect') AND ts>=? ORDER BY id",
                                       (self.now - MS_PER_DAY,)):
            if ev == "disconnect":
                down.setdefault(col, int(ts))                   # the first disconnect of an unbroken outage
            else:                                               # MT5 'resumed' / Binance 'connect' (reconnected)
                down.pop(col, None)
        items = {f"{c}@{t}": f"{c} since {iso(t)} ({_mins(self.now - t)})" for c, t in down.items()
                 if self.now - t > lim}
        for i, text in sorted(self._hold(name, "outage", items, settle).items()):
            self.add(Finding("warn", f"outage:{name}:{i}", f"{name}: connection outage",
                             f"disconnected without a reconnect for more than {s.monitor.outage_warn_min} min: {text}",
                             system=name, pair=s.paths.instance))

    def _quotes(self, s: Settings, name: str, con, status: dict[str, _Row], settle: str | None) -> None:
        lim = s.monitor.quote_stale_min * MS_PER_MINUTE
        reg = InstrumentRegistry.from_settings(s)
        items: dict[str, str] = {}
        for key, ts, recv in con.execute("SELECT instrument, ts, recv_ms FROM latest_quote"):
            try:
                inst = reg.get(key)
            except KeyError:
                continue                                        # not configured here (any more)
            if not {"ticks", "book_ticker"} & set(inst.datatypes):
                continue
            cal = calendar_for(inst.venue, inst.symbol, s.pairs[inst.pair].asset_class)
            if not (cal.is_open(self.now) and cal.is_open(self.now - lim)):     # open through the whole window
                continue
            age = self.now - max(int(ts or 0), int(recv or 0))
            if age > lim:
                items[key] = f"{key} {_mins(age)}"
        items = self._hold(name, "quote", items, settle)
        if items:
            self.add(Finding("warn", f"quote:{name}:{','.join(sorted(items))}", f"{name}: stale quotes",
                             f"no quote for more than {s.monitor.quote_stale_min} min while the market is open: "
                             + "; ".join(items.values()), system=name, pair=s.paths.instance))

    # ---- machine / account checks
    def _gap_ms(self, v: dict) -> int:
        """The time the machine slept or was off since equity baseline ``v`` was sampled, as of this run: the sleep
        the runs that carried it counted (``slept_ms``) plus the sleep since the previous run; after a reboot since
        the run that last took or carried it (``boot_ms``), plus the time from that run (``seen_ms``: it saw the
        machine up) to this boot — the hours those runs were awake without a sample are not downtime."""
        gap = int(_num(v.get("slept_ms")) or 0)
        boot = int(_num(v.get("boot_ms")) or 0)
        if boot and self.boot_ms and abs(boot - self.boot_ms) >= SAME_BOOT_MS:
            seen = int(_num(v.get("seen_ms")) or _num(v.get("ts")) or 0)
            return gap + max(self.boot_ms - seen, 0)
        return gap + round(max(self.slept_s, 0.0) * 1000)

    def _equity(self) -> None:
        """Equity drop between two samples, per account. Only a drop of an identified MT5 account (``mt5:…``, not the
        ``…:?`` fallback) against a recent baseline engages the global switch: at most ``BASELINE_MAX_MS`` old, not
        counting the time the machine slept or was off since that sample (:meth:`_gap_ms`). A paper account, an
        unidentified one or an older baseline (no sample for over an hour while the machine was up: the monitor did
        not run, or its runs found no equity in the executor row) only warns — the switch stops every system. The
        baseline moves on whatever the finding's notification does."""
        m = self.base.monitor
        prev = self.prev.get("equity") or {}
        keep = self.state.setdefault("equity", {})
        for acct, v in prev.items():                            # a baseline survives a run without a newer sample
            if isinstance(v, dict) and self.now - int(v.get("ts") or 0) < 7 * MS_PER_DAY:
                keep[acct] = {**v, "slept_ms": self._gap_ms(v), "boot_ms": self.boot_ms, "seen_ms": self.now}
        for acct, (eq, ts, name) in sorted(self.samples.items()):
            p = prev.get(acct) if isinstance(prev.get(acct), dict) else None
            if p is not None and ts <= int(p.get("ts") or 0):
                continue                                        # the same (or an older) row: carried, not a sample
            keep[acct] = {"equity": eq, "ts": ts, "boot_ms": self.boot_ms, "seen_ms": self.now}
            old = _num((p or {}).get("equity"))
            if not old:
                continue
            drop = (old - eq) / old * 100
            if drop <= m.equity_drop_warn_pct:
                continue
            kill = drop > m.equity_drop_kill_pct
            key = f"equity_drop:{acct}:{ts}"
            age = ts - int(p["ts"])
            gap = self._gap_ms(p)
            no_switch = None
            if kill and acct.startswith("paper:"):
                no_switch = "a paper account (simulated equity)"
            elif kill and (not acct.startswith("mt5:") or acct.endswith(":?")):
                no_switch = "the account is not identified (the executor row has no account_drawdown)"
            elif kill and age > BASELINE_MAX_MS + gap:
                no_switch = (f"the previous sample is {age / MS_PER_HOUR:.1f} h old"
                             + (f" ({gap / MS_PER_HOUR:.1f} h of it asleep or off)" if gap >= MS_PER_MINUTE else "")
                             + " — not a drop between two runs; check the account")
            switch = kill and no_switch is None
            self.add(Finding("critical" if switch else "warn", key, "Equity drop",
                             f"account {acct}: equity {old:.2f} → {eq:.2f} (−{drop:.1f} %) since {iso(int(p['ts']))}"
                             + (f" — above {m.equity_drop_kill_pct} % → the global KILL_SWITCH" if switch else
                                f" — above {m.equity_drop_kill_pct} % but no switch: {no_switch}" if kill else
                                f" (warning above {m.equity_drop_warn_pct} %, global switch above "
                                f"{m.equity_drop_kill_pct} %)"),
                             switch=ALL if switch else None))

    def _drawdown(self) -> None:
        try:
            doc = drawdown._read(self.base.paths.shared() / drawdown.FILE)
        except drawdown.PeakFileError as exc:
            self.add(Finding("warn", "peak_file", "Account peak file unreadable", f"{exc} — the gate refuses trades"))
            doc = {}
        for acct in sorted(self.accounts | set(self.tripped)):
            rec = doc.get(acct) if isinstance(doc.get(acct), dict) else {}
            t = _ms(rec.get("tripped_ms")) or self.tripped.get(acct)
            if t:
                self.add(Finding("critical", f"drawdown:{acct}:{t}", "Account drawdown stop tripped",
                                 f"account {acct}: the drawdown stop tripped at {iso(t)} "
                                 f"(equity {rec.get('tripped_equity')}, peak {rec.get('peak')}) — no new trades until "
                                 "scripts\\reset_drawdown_stop.bat",
                                 once=True))

    def _resources(self) -> None:
        m = self.base.monitor
        ram = _free_ram_mb()
        if ram < m.free_ram_warn_mb:
            self.add(Finding("warn", "ram", "Low free RAM",
                             f"{ram:.0f} MB available (warning below {m.free_ram_warn_mb} MB)"))
        disk = _free_disk_gb(self.base.paths.data())
        if disk < m.free_disk_warn_gb:
            self.add(Finding("warn", "disk", "Low free disk", f"{disk:.1f} GB free on the data drive (warning below "
                                                              f"{m.free_disk_warn_gb:g} GB)"))

    def _power(self) -> None:
        """The laptop on battery (Phase 5 A4: the outages of 2026-09-26/27 were critical-battery hibernates).
        GetSystemPowerStatus tells the percent and the power source, not how long it has run on battery: the first
        run that sees it on battery records ``battery.on_battery_since`` in the state and later runs carry it. Warn
        once it is on battery longer than ``on_battery_warn_min`` or below ``battery_warn_pct``; critical below
        ``battery_critical_pct``. Plugged in, an unknown power source (ACLineStatus 255) or no battery (a desktop):
        nothing. Each unplugged episode is a key of its own (a new unplug is news, not a flapping condition); a level
        rise within it is sent again."""
        m = self.base.monitor
        prev = self.prev.get("battery") if isinstance(self.prev.get("battery"), dict) else {}
        self.state["battery"] = dict(prev)                  # kept as it was if the probe fails below
        b = _battery()
        if b is None:
            self.state["battery"] = {"present": False, "ts": self.now}
            return
        pct, plugged = b
        since = None
        if plugged is False:
            since = int(_num(prev.get("on_battery_since")) or self.now) if prev.get("plugged") is False else self.now
            since = min(since, self.now)
        self.state["battery"] = {"present": True, "percent": round(pct, 1), "plugged": plugged,
                                 "on_battery_since": since, "ts": self.now}
        if since is None:
            return
        on_ms = self.now - since
        crit = pct < m.battery_critical_pct
        warn = pct < m.battery_warn_pct or on_ms > m.on_battery_warn_min * MS_PER_MINUTE
        if not (crit or warn):
            self.res.held.append(f"machine on battery ({pct:.0f} %) since {iso(since)} — a warning after "
                                 f"{m.on_battery_warn_min} min on battery or below {m.battery_warn_pct} %")
            return
        seen = f"on battery for {_mins(on_ms)} at least" if on_ms else "on battery (first seen on this run)"
        if crit:
            self.add(Finding("critical", f"battery:{since}", "Battery critical",
                             f"{pct:.0f} % left, {seen} — plug the charger in now: at 5 % Windows hibernates and every "
                             f"system stops (critical below {m.battery_critical_pct} %)"))
        else:
            self.add(Finding("warn", f"battery:{since}", "Running on battery",
                             f"the charger is unplugged: {seen}, {pct:.0f} % left — plug it in (warning after "
                             f"{m.on_battery_warn_min} min on battery or below {m.battery_warn_pct} %, critical below "
                             f"{m.battery_critical_pct} %)"))

    def _commit_charge(self) -> None:
        """Committed memory (RAM + page file reserved by programs) above ``commit_warn_pct`` of the commit limit: at
        the limit Windows refuses allocations and services die with MemoryError, whatever the free RAM says."""
        c = _commit()
        if not c:
            return
        used, limit = c
        pct = used / limit * 100
        lim = self.base.monitor.commit_warn_pct
        if pct > lim:
            self.add(Finding("warn", "commit", "High memory commit",
                             f"{used / 2**30:.1f} of {limit / 2**30:.1f} GB committed ({pct:.0f} %, warning above "
                             f"{lim:g} %) — at 100 % Windows refuses new memory and services crash; close programs "
                             "(a browser, the desktop app) or enlarge the page file"))

    def _recorder(self) -> None:
        """The P1.12 price recorder (outside the supervisors; the TradingSystemOps-Recorder task keeps it alive): its
        ``status.json`` not rewritten for ``recorder_stall_min`` (0 = not watched) while the owner has not stopped it
        (no ``STOP`` file). No status file (it never ran here) or one caught half-written: nothing. It dies at every
        suspend and its keep-alive needs up to 10 min to bring back a flush, so the two-run rule applies while the
        machine settles."""
        lim = self.base.monitor.recorder_stall_min
        if lim <= 0:
            return
        r = hr.recorder_state(self.base, self.now)
        if r is None or r["stop"] or not r["readable"]:
            return
        items: dict[str, str] = {}
        if r["age_min"] > lim:
            # hung: the recorder reads STOP only after a completed flush, so the STOP file cannot end it
            alive = {True: f"alive but not flushing (hung): end it with taskkill /PID {r['pid']} /T /F — the "
                           "TradingSystemOps-Recorder task then starts a fresh one within 5 min (by hand: "
                           "scripts\\start_recorder.bat)",
                     False: "not running: the TradingSystemOps-Recorder task restarts it every 5 min while MT5 runs "
                            "(install_operator_tasks.bat; by hand: scripts\\start_recorder.bat)",
                     None: "its state is unknown: scripts\\start_recorder.bat"}[r["alive"]]
            items["stall"] = (f"last flush {r['age_min']:.0f} min ago ({iso(r['updated_ms'])}, limit "
                              f"{lim} min); pid {r['pid']} {alive}")
        for text in self._hold("machine", "recorder", items, self.machine_settle).values():
            self.add(Finding("warn", "recorder", "Price recorder stalled", f"the P1.12 price recorder's {text}"))

    def _vpn(self) -> None:
        names = self.base.monitor.vpn_adapter_names
        prev = self.prev.get("vpn") or {}
        keep = self.state.setdefault("vpn", {})
        if not names:
            return
        now_up = {k.lower(): v for k, v in _adapters().items()}
        for n in names:
            up = bool(now_up.get(n.lower(), False))             # a missing adapter is down
            keep[n] = up                                        # whatever the notification does
            if n in prev and bool(prev[n]) != up:
                word = "up" if up else "down"
                self.add(Finding("info", f"vpn:{n}:{word}:{self.now}", f"VPN {word}",
                                 f"network adapter '{n}' is now {word}"))

    def _reviews(self) -> None:
        b = self.base
        if not b.operator.enabled:
            return
        d = b.paths.data() / "reviews"
        newest = None
        # a daily review HAPPENED when its session finished (ok / max_turns) — a pack alone (a --dry-run, a failed
        # start, review_pack.py by hand) is not a review
        for f in (d.glob("*_daily.session.json") if d.is_dir() else ()):
            try:
                if json.loads(f.read_text(encoding="utf-8")).get("status") in ("ok", "max_turns"):
                    newest = max(newest or 0.0, f.stat().st_mtime)
            except (OSError, ValueError):
                continue
        ref = int(newest * 1000) if newest else int(self.state["first_run_ms"])
        if self.now - ref > b.monitor.review_overdue_hours * MS_PER_HOUR:
            self.add(Finding("warn", "review_overdue" if newest else "review_overdue:never", "Daily review overdue",
                             (f"the last finished daily review is from {iso(ref)}" if newest else
                              f"no finished daily review in {d} since the monitor started ({iso(ref)})")
                             + f" — more than {b.monitor.review_overdue_hours} h; check the "
                               "TradingSystemOps-ReviewDaily task (docs/operator_sessions.md)"))

    # ---- notification, dedupe, diagnosis
    def _dispatch(self) -> None:
        """Decide per finding whether it is due (new, level risen or reminder due) and queue its notification, with
        the re-sends of earlier undelivered ones (:meth:`_resend`); criticals are queued first. A key that cleared
        within its cool-down (``COOL_MS``) and comes back persists: not sent again unless its level rose, and not new
        (no diagnosis). A due finding is recorded as notified whatever its sinks do: :meth:`_deliver` keeps an
        undelivered notification for a re-send, and a re-send is never a new finding."""
        prev = self.prev.get("alerts") or {}
        once = {k: v for k, v in (self.prev.get("once") or {}).items() if self.now - int(v) < 30 * MS_PER_DAY}
        alerts: dict[str, dict] = {}
        out: list[tuple[str, dict, Finding | None]] = []       # (finding key, pending_notify entry, finding)
        for f in sorted(self.res.findings, key=lambda x: -LEVELS[x.level]):     # criticals are queued first
            a = prev.get(f.key) if isinstance(prev.get(f.key), dict) else None
            if a is not None and a.get("cleared_ms") and not self._cooling(a):
                a = None                                        # cleared longer than its cool-down: a new episode
            remind = f.remind_ms if f.remind_ms is not None else REMIND_MS[f.level]
            if f.once and f.key in once:
                send = False
            elif a is None or LEVELS[f.level] > LEVELS.get(a.get("level"), 0):
                send = True
            else:
                send = bool(remind) and not f.once and self.now - int(a.get("notified_ms") or 0) >= remind
            alerts[f.key] = {"level": f.level, "first_ms": int((a or {}).get("first_ms") or self.now),
                             "last_ms": self.now,
                             "notified_ms": self.now if send else int((a or {}).get("notified_ms") or 0),
                             "title": f.title}
            if f.once:
                once.setdefault(f.key, self.now)
            (log.warning if LEVELS[f.level] else log.info)("[%s] %s: %s%s%s", f.level, f.title, f.text,
                                                          f" | {f.action}" if f.action else "",
                                                          "" if send else " (already notified)")
            f.new = send
            if send and not self.dry_run:
                out.append((f.key, {"level": f.level, "title": f"Monitor: {f.title}",
                                    "text": f.text + (f"\n{f.action}" if f.action else ""), "pair": f.pair,
                                    "first_ms": self.now, "tries": 1}, f))
        out += self._resend({k for k, _, _ in out})
        for key, e, f in sorted(out, key=lambda x: -LEVELS[x[1]["level"]]):     # stable: this run's, then re-sends
            n = e["tries"] - 1
            text = e["text"] + (f"\n(re-sent: detected {iso(e['first_ms'])}, the earlier notification reached no "
                                "sink)" if n else "")
            res = _notify(self.base, e["level"], e["title"], text,
                          key=f"monitor:{key}" + (f":retry{n}" if n else ""), pair=e.get("pair"))
            if f is not None:
                f.notified = True
            self._sent.append((key, e, res, f))
        for k, a in prev.items():                               # cleared: remembered for the cool-down
            if k not in alerts and isinstance(a, dict) and self._cooling(a):
                alerts[k] = {**a, "cleared_ms": int(a.get("cleared_ms") or self.now)}
        self.state["alerts"] = alerts
        self.state["once"] = once
        self.state["acted"] = {**{k: v for k, v in (self.prev.get("acted") or {}).items()
                                  if self.now - int(v) < 3 * MS_PER_DAY}, **(self.state.get("acted") or {})}

    def _cooling(self, a: dict) -> bool:
        """An alert last seen within its level's cool-down (``COOL_MS``)."""
        return self.now - int(a.get("last_ms") or 0) < COOL_MS.get(a.get("level"), 2 * MS_PER_HOUR)

    def _resend(self, fresh: set[str]) -> list[tuple[str, dict, None]]:
        """The notifications earlier runs could not deliver (``pending_notify``) that are due for a re-send: only the
        notification is sent again — the event is not detected again, no switch is engaged, nothing is new. One is
        dropped (a log line) when this run sends its key afresh (a level rise, a reminder), after ``NOTIFY_TRIES``
        attempts, or once older than its level's reminder interval (``REMIND_MS``; info has none): the normal
        reminder takes over. A dry run keeps them as they are."""
        pend = self.prev.get("pending_notify") if isinstance(self.prev.get("pending_notify"), dict) else {}
        self.state["pending_notify"] = {}
        out: list[tuple[str, dict, None]] = []
        for key, e in sorted(pend.items()):
            if not isinstance(e, dict) or e.get("level") not in LEVELS \
                    or not all(isinstance(e.get(x), str) for x in ("title", "text")):
                continue
            if self.dry_run:
                self.state["pending_notify"][key] = e
                continue
            tries, first = int(_num(e.get("tries")) or 1), int(_num(e.get("first_ms")) or 0)
            limit = REMIND_MS[e["level"]]
            why = ("sent afresh by this run" if key in fresh else
                   f"{tries} attempt(s)" if tries >= NOTIFY_TRIES else
                   f"older than {limit / MS_PER_HOUR:g} h" if limit and self.now - first >= limit else None)
            if why:
                (log.info if key in fresh else log.warning)("undelivered notification '%s' dropped: %s", key, why)
                continue
            out.append((key, {**e, "first_ms": first or self.now, "tries": tries + 1}, None))
        return out

    def _deliver(self) -> None:
        """Wait for this run's notifications (``FLUSH_S``: the budget grows with the queue), then keep every one that
        reached no sink (:func:`_undelivered`) in ``pending_notify`` for the next runs (:meth:`_resend`), unless it
        used up its ``NOTIFY_TRIES`` attempts. The event's own bookkeeping stays as it is — the alert's
        ``notified_ms``, a one-shot key, a burst counted, the equity baseline, the VPN state: the event was handled,
        only its notification is owed. main() looks again after its last flush (:meth:`reconcile`)."""
        queued = sum(1 for _, _, r, _ in self._sent if isinstance(r, dict) and not r.get("done"))
        if queued:
            base, per, cap = FLUSH_S
            _flush_notify(min(base + per * queued, cap))
        for key, e, r, f in self._sent:
            if not _undelivered(r):
                continue
            if f is not None:
                f.notified = False
            last = e["tries"] >= NOTIFY_TRIES
            if not last:
                self.state["pending_notify"][key] = e
            log.warning("notification '%s' not delivered (toast: %s, Telegram: %s) — %s", key, r.get("toast"),
                        r.get("telegram"), f"dropped after {e['tries']} attempts" if last
                        else "the next run sends it again")

    def reconcile(self) -> bool:
        """After main()'s last flush: a notification still in flight when :meth:`_deliver` looked that has reached a
        sink since leaves ``pending_notify`` (the next run would send it twice), and the state is saved again (best
        effort). True when something changed."""
        pend = self.state.get("pending_notify") or {}
        done = [(key, f) for key, e, r, f in self._sent if pend.get(key) is e and not _undelivered(r)]
        if self.dry_run or not done:
            return False
        for key, f in done:
            pend.pop(key, None)
            if f is not None:
                f.notified = True
        if isinstance(self.state.get("run"), dict):
            self.state["run"]["findings"] = [f.as_dict() for f in self.res.findings]
        log.info("delivered after the first wait: %s", ", ".join(k for k, _ in done))
        try:
            save_state(self.path, self.state)
        except OSError as exc:
            log.warning("%s could not be written after the last flush: %s — the next run may re-send %s", self.path,
                        exc, ", ".join(k for k, _ in done))
        return True

    def _session_running(self) -> bool:
        """An operator session (a review, another diagnosis) holds ``operator_session.lock`` — the one
        tools/operator/run_session.py takes. Tried once and released at once; unknown (an OSError) = not running."""
        lock = FileLock(locks_dir(self.base) / SESSION_LOCK)
        try:
            if not lock.acquire(timeout=0):
                return True
        except OSError:
            return False
        lock.release()
        return False

    def _launches(self) -> list[int]:
        """Diagnosis sessions started within ``DIAGNOSE_KEEP_MS`` (the per-UTC-day budget). A state from before the
        budget has no list: its ``last_diagnose_ms`` counts."""
        raw = self.prev.get("diagnose_launches")
        if not isinstance(raw, list):
            raw = [self.prev.get("last_diagnose_ms")]
        return sorted(int(t) for t in raw if _num(t) is not None and self.now - int(t) < DIAGNOSE_KEEP_MS)

    def _gauge_block(self) -> str | None:
        """Why the usage gauge holds a diagnosis back, None when it does not: its level above
        ``diagnose_max_gauge_level``, or a gauge that cannot be read — a billed session on the shared Max plan starts
        only while the plan's use is known to be low."""
        m = self.base.monitor
        try:
            level, reason = _gauge_level(self.base, self.chosen, self.now)
        except Exception as exc:  # noqa: BLE001 — unknown = held back
            level, reason = None, f"{type(exc).__name__}: {exc}"[:200]
        if level is None:
            return f"usage gauge unreadable ({reason}) — no billed diagnosis while the plan's use is unknown"
        if level > m.diagnose_max_gauge_level:
            return (f"usage gauge at level {level}, above monitor.diagnose_max_gauge_level "
                    f"{m.diagnose_max_gauge_level}" + (f" ({reason})" if reason else ""))
        return None

    def _diagnose(self) -> dict:
        """At most one Claude diagnosis per ``diagnose_every_hours``, for this run's new warnings, and within the
        budget (Phase 5 A4): at most ``diagnose_max_per_day`` starts per UTC day (``diagnose_launches``), none while
        the usage gauge is above ``diagnose_max_gauge_level``. A budget refusal is final for those findings (not
        wanted: a new day or a lower gauge serves the next new warning). One put off because an operator session
        holds the lock (or whose start failed) stays wanted (``diagnose_wanted``): the next runs retry it for the
        findings that persist, for ``WANTED_MS``, although those are no longer new."""
        m = self.base.monitor
        last = int(self.prev.get("last_diagnose_ms") or 0)
        self.state["last_diagnose_ms"] = last or None
        if self.prev.get("diagnose"):
            self.state["diagnose"] = self.prev["diagnose"]
        launches = self._launches()
        self.state["diagnose_launches"] = launches
        today = [t for t in launches if t // MS_PER_DAY == self.now // MS_PER_DAY]
        wanted = self.prev.get("diagnose_wanted") if isinstance(self.prev.get("diagnose_wanted"), dict) else {}
        since = int(wanted.get("ts") or 0)
        again = set(wanted.get("findings") or []) if self.now - since < WANTED_MS else set()
        new = [f for f in self.res.findings if LEVELS[f.level] >= 1 and (f.new or f.key in again)]

        def put_off(why: str) -> dict:
            self.state["diagnose_wanted"] = {"ts": since if again else self.now, "findings": [f.key for f in new]}
            return {"launched": False, "why": why}

        why = None
        if not new:
            why = "no new warning"
        elif not self.allow_diagnose:
            why = "not requested (--no-diagnose, --dry-run or a library call)"
        elif self.dry_run:
            why = "dry run"
        elif not m.diagnose_enabled:
            why = "monitor.diagnose_enabled is false"
        elif os.environ.get(NO_DIAGNOSE_ENV, "").strip() not in ("", "0"):
            why = f"{NO_DIAGNOSE_ENV} is set"
        elif not self.base.operator.enabled:
            why = "operator.enabled is false"
        elif last and self.now - last < m.diagnose_every_hours * MS_PER_HOUR:
            why = (f"last diagnosis {(self.now - last) / MS_PER_HOUR:.1f} h ago "
                   f"(every {m.diagnose_every_hours:g} h at most)")
        elif len(today) >= m.diagnose_max_per_day:
            why = (f"daily budget used: {len(today)} diagnosis session(s) started today (UTC), "
                   f"monitor.diagnose_max_per_day {m.diagnose_max_per_day}")
        elif not RUN_SESSION.exists():
            why = f"{RUN_SESSION} not found"
        else:
            why = self._gauge_block()
            if why is None and self._session_running():     # it would end 'busy' at once; the next run retries
                return put_off("a review session is running")
        if why:
            if why.startswith(("daily budget", "usage gauge")):
                log.info("diagnosis held back: %s", why)
            return {"launched": False, "why": why}
        reason = "; ".join(f"{f.level}: {f.title} — {f.text}" + (f" ({f.action})" if f.action else "") for f in new)
        cmd = [control.console_python(), str(RUN_SESSION), "--kind", "diagnose", "--reason",
               " ".join(reason.split())[:1500]]          # one line (a command-line argument), never empty
        # persisted BEFORE the start: a state that cannot be saved would start a session on every run
        kept = (self.state.get("last_diagnose_ms"), self.state.get("diagnose"), self.state.get("diagnose_launches"))
        self.state["last_diagnose_ms"] = self.now
        self.state["diagnose"] = {"ts": self.now, "pid": None, "findings": [f.key for f in new]}
        self.state["diagnose_launches"] = launches + [self.now]
        try:
            save_state(self.path, self.state)
        except OSError as exc:
            self.state["last_diagnose_ms"], self.state["diagnose"], self.state["diagnose_launches"] = kept
            log.warning("diagnosis session not started: %s could not be written: %s", self.path, exc)
            return {"launched": False, "why": "state not persisted"}
        try:
            proc = _spawn(cmd, ROOT)
        except OSError as exc:
            # the next run tries again: a start that failed does not use up the day's budget
            self.state["last_diagnose_ms"], self.state["diagnose"], self.state["diagnose_launches"] = kept
            log.warning("diagnosis session could not be started: %s", exc)
            return put_off(f"start failed: {exc}")
        self.state["diagnose"]["pid"] = getattr(proc, "pid", None)
        log.info("diagnosis session started (pid %s) for %s", getattr(proc, "pid", None), ", ".join(f.key for f in new))
        return {"launched": True, "pid": getattr(proc, "pid", None), "cmd": cmd}


def run(base: Settings, chosen: list[Settings], notes: list[str] | None = None, *, now: int | None = None,
        dry_run: bool = False, diagnose: bool = False) -> Result:
    """One monitor pass over ``chosen`` (``health_report.systems()``). ``diagnose`` must be asked for explicitly
    (main() does unless --no-diagnose): a library call or a test never starts a Claude session by accident."""
    return Monitor(base, chosen, notes, now=now, dry_run=dry_run, diagnose=diagnose).run()


# --------------------------------------------------------------------------- CLI
def render(res: Result) -> str:
    out = [f"# monitor {time.strftime('%Y-%m-%d %H:%M', time.gmtime(res.ts / 1000))} UTC — systems: "
           f"{', '.join(res.systems) or 'none'} — {len(res.problems)} problem(s)"
           + (" — DRY RUN" if res.dry_run else "")]
    for f in sorted(res.findings, key=lambda x: -LEVELS[x.level]):
        out.append(f"{'!! ' if LEVELS[f.level] else '   '}[{f.level}] {f.title}: {f.text}"
                   + (f" | {f.action}" if f.action else "")
                   + ("" if f.new else " (already notified)"))
    out += [f"   waiting: {h}" for h in res.held] + [f"   skipped: {x}" for x in res.skipped]
    if res.state_error:
        out.append(f"!! {res.state_error}")
    dg = res.diagnose or {}
    out.append("   diagnosis: " + (f"started (pid {dg.get('pid')})" if dg.get("launched")
                                   else f"not started — {dg.get('why')}"))
    if not res.findings:
        out.append("   no finding")
    return "\n".join(out)


def _utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass


def _config_error(exc: Exception, *, quiet: bool) -> int:
    """load_settings() failed (e.g. a bad ``config.local.yaml``): there are no Settings, so no normal log, no
    notifier and no data root. One timestamped line goes to ``CONFIG_ERROR_LOG``, a best-effort toast is shown
    (skipped under ``TS_NOTIFY_DISABLE``), the error is printed unless ``quiet``; exit ``CONFIG_ERROR_EXIT``. Never
    raises: this is the only trace the scheduled task leaves."""
    short = " ".join(f"{type(exc).__name__}: {exc}".split())
    try:
        from tradingsystem.core.logsetup import get_redactor
        short = get_redactor()(short)
    except Exception:  # noqa: BLE001
        pass
    line = f"{iso(now_ms())} monitor cannot read its config: {short[:4000]}"
    try:
        CONFIG_ERROR_LOG.parent.mkdir(parents=True, exist_ok=True)
        if CONFIG_ERROR_LOG.exists() and CONFIG_ERROR_LOG.stat().st_size > CONFIG_ERROR_LOG_MAX:
            CONFIG_ERROR_LOG.replace(CONFIG_ERROR_LOG.with_name(CONFIG_ERROR_LOG.name + ".1"))
        with open(CONFIG_ERROR_LOG, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass
    try:
        from tradingsystem.core import notify as nt
        if not nt.disabled():
            subprocess.run(nt.toast_command("critical", "Monitor cannot read its config",
                                            f"{short[:300]} — nothing is checked until it is fixed "
                                            f"({CONFIG_ERROR_LOG})"),
                           capture_output=True, timeout=CONFIG_TOAST_TIMEOUT_S,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception:  # noqa: BLE001 — best effort (no powershell, a timeout, …)
        pass
    if not quiet and sys.stderr is not None:
        try:
            print(f"!! {line}", file=sys.stderr)
        except (OSError, ValueError):
            pass
    return CONFIG_ERROR_EXIT


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Pure-Python production monitor (docs/monitoring.md). "
                                             "Exit 0 ok, 1 problems, 3 the config cannot be read.")
    ap.add_argument("--quiet", action="store_true", help="no console output (Task Scheduler runs it under pythonw)")
    ap.add_argument("--json", action="store_true", help="print the result as JSON")
    ap.add_argument("--dry-run", action="store_true",
                    help="check and print only: no KILL_SWITCH, no notification, no state file, no diagnosis")
    ap.add_argument("--no-diagnose", action="store_true", help="never start a Claude diagnosis session from this run")
    ap.add_argument("--instance", help="only this pair's system")
    ap.add_argument("--all-pairs-system", action="store_true", help="only the single all-pairs system")
    a = ap.parse_args(argv)
    _utf8()
    try:
        base = load_settings()
    except Exception as exc:  # noqa: BLE001 — a bad config.local.yaml must not silence the watchdog
        return _config_error(exc, quiet=a.quiet)
    setup_logging("monitor", logs_dir=base.paths.logs(), level=base.logging.level, max_bytes=base.logging.max_bytes,
                  backups=base.logging.backups, console=False, secret_env_names=base.secret_env_names())

    def say(text: str) -> None:
        if not a.quiet and sys.stdout is not None:
            print(text)

    if not base.monitor.enabled:
        say("monitor disabled (monitor.enabled: false)")
        return 0
    try:
        chosen, notes = hr.systems(argparse.Namespace(instance=a.instance, all_pairs_system=a.all_pairs_system))
        mon = Monitor(base, chosen, notes, dry_run=a.dry_run, diagnose=not (a.no_diagnose or a.dry_run))
        res = mon.run()
    except Exception as exc:  # noqa: BLE001 — a crashed monitor must still be heard of
        log.exception("monitor run failed")
        if not a.dry_run:
            _notify(base, "warn", "Monitor failed", f"{type(exc).__name__}: {str(exc)[:300]}", key="monitor:crash",
                    pair=None)
            _flush_notify()
        say(f"!! monitor failed: {type(exc).__name__}: {exc}")
        return 1
    log.info("monitor run: %d finding(s), %d problem(s), exit %d, diagnosis %s", len(res.findings), len(res.problems),
             res.exit_code, res.diagnose)
    say(json.dumps(res.as_dict(), ensure_ascii=False, indent=1) if a.json else render(res))
    if not a.dry_run:
        _flush_notify()
        try:
            mon.reconcile()             # delivery is judged after this last flush, not after the run's own wait
        except Exception:  # noqa: BLE001 — best effort: the state was saved by the run
            log.warning("reconcile after the last flush failed", exc_info=True)
    return res.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
