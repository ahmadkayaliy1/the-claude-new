"""Compact health + performance report (read-only): ``python tools/health_report.py [--hours 6] [--instance PAIR]``.

Services and heartbeats, the MT5 terminal, events and log errors of the window, AI decisions (status, latency,
tokens), executions (placed / gate rejections with reasons), broker outcomes and virtual outcomes, account state.
Built for a periodic check by a person or an agent: short, and every problem line starts with "!!".

Which system (D-042): ``--instance PAIR`` one pair's system; ``--all-pairs-system`` the single all-pairs system;
default: the all-pairs system when it runs; otherwise every running pair plus every pair that should run (its own
``data/instances/<PAIR>/app.db``, not stopped by the user); when nothing applies, the all-pairs system — so the
all-pairs block never shows while the per-pair systems are in use.

Phase 5 (A3/A4/A7): the price-recorder line (last flush, pid alive, STOP), the machine line (power source and time on
battery, commit charge, the last suspend of any system), the disconnect reasons of the window (``PermissionError(13)``
on its own), and ``n/a (no instrument)`` for a venue collector a pair has no instrument on (XAUUSD: Binance spot).
"""
from __future__ import annotations

import argparse
import calendar
import collections
import ctypes
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

import psutil

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tradingsystem.core.instruments import InstrumentRegistry  # noqa: E402
from tradingsystem.core.settings import INSTANCE_ENV, Settings, load_settings  # noqa: E402
from tradingsystem.supervisor import control, procs  # noqa: E402
from tradingsystem.supervisor.supervisor import VENUE_BEATS  # noqa: E402

STALE_S = {"binance_backfill": 900, "mt5_backfill": 900}
RECORDER_DIR = Path("research") / "price_matching"         # under the data root: status.json, STOP (P1.12 recorder)
MONITOR_STATE = "monitor_state.json"                        # tools/monitor.py STATE_FILE (data/shared)
_EXC_RE = re.compile(r"\s*([A-Za-z_][\w.]*)\((-?\d+)?")    # "PermissionError(13, 'Access is denied', …)" → name, 13


class _MemoryStatusEx(ctypes.Structure):
    """MEMORYSTATUSEX (GlobalMemoryStatusEx): ullTotalPageFile is the commit limit (RAM + page files)."""
    _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]


class _SystemPowerStatus(ctypes.Structure):
    """SYSTEM_POWER_STATUS (GetSystemPowerStatus)."""
    _fields_ = [("ACLineStatus", ctypes.c_ubyte), ("BatteryFlag", ctypes.c_ubyte),
                ("BatteryLifePercent", ctypes.c_ubyte), ("SystemStatusFlag", ctypes.c_ubyte),
                ("BatteryLifeTime", ctypes.c_ulong), ("BatteryFullLifeTime", ctypes.c_ulong)]


# --------------------------------------------------------------------------- machine probes (the monitor uses them too)
def _power_status() -> tuple[int, int, int] | None:
    """GetSystemPowerStatus as Windows gives it: ``(ACLineStatus, BatteryFlag, BatteryLifePercent)``. None off
    Windows or on failure."""
    if os.name != "nt":
        return None
    try:
        st = _SystemPowerStatus()
        if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(st)):
            return None
        return int(st.ACLineStatus), int(st.BatteryFlag), int(st.BatteryLifePercent)
    except Exception:  # noqa: BLE001
        return None


def battery_from_status(ac_line: int, flag: int, percent: int) -> tuple[float, bool | None] | None:
    """One GetSystemPowerStatus reading as ``(percent, on mains)``: ACLineStatus 1 → True, 0 → False, anything else
    (255 = unknown) → None. BatteryFlag 128 (no system battery) or 255 (cannot be read) — both have bit 7 — and
    BatteryLifePercent 255 (unknown) → None."""
    if flag & 128 or not 0 <= percent <= 100:
        return None
    return float(percent), {1: True, 0: False}.get(ac_line)


def battery() -> tuple[float, bool | None] | None:
    """The laptop battery: ``(percent, on mains)`` — on mains None when Windows cannot tell (ACLineStatus 255); None
    without a battery (a desktop) or when it cannot be read. On Windows straight from GetSystemPowerStatus: psutil
    turns an unknown AC line into "unplugged" (``power_plugged = ACLineStatus == 1``), which would read as on
    battery. psutil only where that call is not available."""
    st = _power_status()
    if st is not None:
        return battery_from_status(*st)
    try:
        b = psutil.sensors_battery()
    except Exception:  # noqa: BLE001 — psutil raises on odd firmware; the battery is then unknown
        return None
    if b is None or b.percent is None:
        return None
    return float(b.percent), (None if b.power_plugged is None else bool(b.power_plugged))


def commit_charge() -> tuple[int, int] | None:
    """Windows commit charge: ``(committed bytes, commit limit)``. The limit is RAM + page files
    (GlobalMemoryStatusEx ``ullTotalPageFile``), what is left of it ``ullAvailPageFile``. At the limit Windows refuses
    new allocations and services die with MemoryError, whatever the free RAM says. None off Windows or on failure."""
    if os.name != "nt":
        return None
    try:
        st = _MemoryStatusEx()
        st.dwLength = ctypes.sizeof(_MemoryStatusEx)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
            return None
        total, avail = int(st.ullTotalPageFile), int(st.ullAvailPageFile)
    except Exception:  # noqa: BLE001
        return None
    return (max(total - avail, 0), total) if total > 0 else None


def _recorder_alive(pid) -> bool | None:  # noqa: ANN001
    """The pid of status.json still runs the recorder (a python process with ``…/price_matching/recorder.py`` on its
    command line, as tools/recorder_keepalive.py decides); None when that cannot be told (access denied)."""
    try:
        p = psutil.Process(int(pid))
        if not (p.name() or "").lower().startswith("python"):
            return False
        return any(a.replace("\\", "/").lower().endswith("price_matching/recorder.py") for a in p.cmdline()[1:])
    except psutil.AccessDenied:
        return None
    except (psutil.Error, ValueError, TypeError, OSError):
        return False


def recorder_state(s: Settings, now: float) -> dict | None:
    """The P1.12 price recorder (``research/price_matching/recorder.py``, outside the supervisors): its
    ``status.json`` (rewritten at every flush, 5 min), the pid in it and the owner's ``STOP`` file. None when it never
    ran here (no status.json). Keys: ``stop``, ``readable``, ``updated_ms``, ``age_min``, ``pid``, ``alive`` (None =
    unknown)."""
    d = s.paths.data() / RECORDER_DIR
    st_file = d / "status.json"
    if not st_file.exists():
        return None
    out = {"stop": (d / "STOP").exists(), "readable": False, "updated_ms": None, "age_min": None, "pid": None,
           "alive": None}
    try:
        doc = json.loads(st_file.read_text(encoding="utf-8"))
        upd = calendar.timegm(time.strptime(doc["updated"][:19], "%Y-%m-%dT%H:%M:%S")) * 1000   # UTC, DST-proof
    except (OSError, ValueError, KeyError, TypeError):
        return out                          # the recorder writes it in place: a read can catch it half-written
    pid = doc.get("pid")
    out.update(readable=True, updated_ms=upd, age_min=(now - upd) / 60_000, pid=pid, alive=_recorder_alive(pid))
    return out


def recorder_line(s: Settings, now: float) -> str | None:
    """``price recorder: last flush N min ago, pid P alive`` — a problem line when it has not flushed for more than
    ``monitor.recorder_stall_min`` (the monitor's rule; 0 = not watched) and the owner did not stop it (STOP)."""
    r = recorder_state(s, now)
    if r is None:
        return None
    if not r["readable"]:
        return "!! price recorder: status.json unreadable" + (" (STOP present)" if r["stop"] else "")
    lim = s.monitor.recorder_stall_min
    stalled = not r["stop"] and lim > 0 and r["age_min"] > lim
    alive = {True: "alive", False: "not running", None: "(state unknown)"}[r["alive"]]
    text = f"price recorder: last flush {r['age_min']:.0f} min ago, pid {r['pid']} {alive}"
    if r["stop"]:
        text += " — STOP present (stopped by the owner; scripts\\start_recorder.bat starts it again)"
    elif stalled:
        # hung: it reads STOP only after a flush, so the STOP file cannot end it — the process has to be ended
        text += (f" — stalled (no flush for more than {lim} min): "
                 + (f"alive but not flushing (hung) — end it with taskkill /PID {r['pid']} /T /F; the "
                    "TradingSystemOps-Recorder task then starts a fresh one within 5 min (by hand: "
                    "scripts\\start_recorder.bat)" if r["alive"] else
                    "the TradingSystemOps-Recorder task restarts it every 5 min while MT5 runs; by hand: "
                    "scripts\\start_recorder.bat"))
    return ("!! " if stalled else "   ") + text


def _state_dirs(s: Settings) -> list[tuple[str, Path]]:
    """Every system's state dir on this data root: the all-pairs one (the data root) and each pair's."""
    data = s.paths.data()
    out = [("all-pairs", data)]
    inst = data / "instances"
    if inst.is_dir():
        out += [(d.name, d) for d in sorted(inst.iterdir()) if d.is_dir()]
    return out


def last_suspend(s: Settings) -> tuple[int, float, str] | None:
    """The newest sleep any system's supervisor saw: ``(resumed at ms, seconds asleep, system)`` from the
    ``system_suspend`` events (every app.db, read-only) and ``run/supervisor.json`` ``last_gap`` — every supervisor
    records the same machine sleep. None when none is recorded."""
    best: tuple[int, float, str] | None = None
    for name, d in _state_dirs(s):
        cands: list[tuple[int, float]] = []
        gap = (control.read_state(d) or {}).get("last_gap") or {}
        if isinstance(gap, dict) and gap.get("kind") == "suspend" and isinstance(gap.get("ts"), (int, float)):
            cands.append((int(gap["ts"]), float(gap.get("seconds") or 0)))
        db = d / "app.db"
        if db.exists():
            try:
                con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=5)
                try:
                    row = con.execute("SELECT ts, duration_ms FROM ingestion_events WHERE collector='supervisor:all' "
                                      "AND event='system_suspend' ORDER BY ts DESC LIMIT 1").fetchone()
                finally:
                    con.close()
                if row:
                    cands.append((int(row[0]), (row[1] or 0) / 1000))
            except sqlite3.Error:
                pass
        for ts, sec in cands:
            if best is None or ts > best[0]:
                best = (ts, sec, name)
    return best


def _when(ms: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(ms / 1000))


def _on_battery_since(s: Settings) -> int | None:
    """When the monitor first saw the machine on battery in this episode (its state file), None when it has not."""
    try:
        doc = json.loads((s.paths.shared() / MONITOR_STATE).read_text(encoding="utf-8"))
        b = doc.get("battery") if isinstance(doc, dict) else None
        since = b.get("on_battery_since") if isinstance(b, dict) and b.get("plugged") is False else None
        return int(since) if isinstance(since, (int, float)) and not isinstance(since, bool) else None
    except (OSError, ValueError):
        return None


def machine_line(s: Settings, now: float) -> str:
    """``machine: <power> — commit N % of G GB — last suspend …``; a problem line on battery or above
    ``monitor.commit_warn_pct``. Time on battery comes from the monitor's state (psutil cannot tell how long)."""
    parts, bad = [], False
    b = battery()
    if b is None:
        parts.append("no battery")
    elif b[1] is False:
        bad = True
        since = _on_battery_since(s)
        parts.append(f"ON BATTERY {b[0]:.0f} %"
                     + (f" for {(now - since) / 60_000:.0f} min at least (since {_when(since)})" if since else
                        " (time on battery not known yet: the monitor has not seen it)")
                     + " — plug the charger in (at 5 % Windows hibernates and every system stops)")
    else:
        parts.append(f"on AC power (battery {b[0]:.0f} %)" if b[1] else f"power source unknown (battery {b[0]:.0f} %)")
    c = commit_charge()
    if c:
        pct = c[0] / c[1] * 100
        bad = bad or pct > s.monitor.commit_warn_pct
        parts.append(f"commit {pct:.0f} % of {c[1] / 2**30:.1f} GB")
    last = last_suspend(s)
    parts.append(f"last suspend: resumed {_when(last[0])} after ~{last[1] / 60:.0f} min asleep ({last[2]})" if last
                 else "last suspend: none recorded")
    return f"{'!! ' if bad else '   '}machine: " + " — ".join(parts)


def _disconnect_reason(detail: str | None) -> str:
    """The exception class of a ``disconnect`` event, with its errno when the first argument is one:
    ``PermissionError(13)`` (Windows refused the socket) and ``gaierror(11001)`` (DNS) are reasons of their own."""
    m = _EXC_RE.match(detail or "")
    if not m:
        return (detail or "?").strip()[:40] or "?"
    return f"{m.group(1)}({m.group(2)})" if m.group(2) else m.group(1)


def _snapshot_build(d: dict) -> tuple[str, float | None, str]:
    """The engine's payload build time (follow-up b): (engine-line text, reference ms, which statistic). The median
    of the recent builds when the engine publishes it — one cold build after a restart must not look like a
    lasting problem — else the last build."""
    sb = d.get("snapshot_build_ms") if isinstance(d.get("snapshot_build_ms"), dict) else {}
    use = "median" if isinstance(sb.get("median"), (int, float)) else "last"
    ref = sb.get(use) if isinstance(sb.get(use), (int, float)) else None
    if ref is None:
        return "", None, use
    n = f" of {sb['n']}" if use == "median" and sb.get("n") else ""
    return f" snapshot_build {use}{n} {ref:.0f} ms (last {sb.get('last')}, max {sb.get('max')})", float(ref), use


def _kill_switch_lines(s: Settings) -> list[str]:
    """Every kill switch this system obeys that is ON, with who set it and why (core/killswitch.py)."""
    try:
        from tradingsystem.core.killswitch import kill_switch_path, read_reason
    except ImportError:                                  # before Phase 4
        return []
    paths = {"every system": kill_switch_path(s, None)}
    for pair in s.enabled_pairs():
        paths[pair] = kill_switch_path(s, pair)
    paths["this system"] = s.paths.state() / "KILL_SWITCH"     # = one of the above for today's layouts
    out, done = [], set()
    for scope, path in paths.items():
        if path in done or not path.exists():
            continue
        done.add(path)
        why = read_reason(path)
        told = " ".join(str(why[k]) for k in ("ts", "actor", "reason") if why.get(k)) or "no reason recorded"
        out.append(f"!! kill switch ON ({scope}): {path} — {told}")
    return out


def usage_gauge_line(s: Settings) -> str | None:
    """The usage gauge over the shared AI ledger (Phase 4, ai/usage_gauge.py); None before it exists or when there
    is no ledger yet (a report never creates one). A problem line ("!!") at level 2, at level 1 when enforced, and
    while an operator session of the last 7 days has unknown usage (timed out or crashed: its row holds 0 tokens, so
    the sums undercount). A fresh gauge: the thresholds as they are, without an engine's step-down margin."""
    ledger = s.paths.shared() / "ai_usage.db" if s.paths.instance else s.paths.state() / "app.db"
    if not ledger.exists():
        return None
    try:
        from tradingsystem.ai.budget import UsageStore
        from tradingsystem.ai.usage_gauge import UsageGauge
    except ImportError:
        return None
    store = None
    try:
        store = UsageStore(ledger)
        g = UsageGauge(s, store).state()
    except Exception as exc:  # noqa: BLE001 — the report goes on without the gauge
        return f"!! usage gauge unreadable: {type(exc).__name__}: {exc}"
    finally:
        if store is not None:
            store.close()
    unknown = int(getattr(g, "unknown_7d", 0) or 0)
    reason = g.reason or ""
    if unknown and "unknown usage" not in reason:          # the gauge's reason names them; say it when it does not
        reason = f"{reason} + {unknown} session(s) with unknown usage in 7 d".lstrip()
    bad = g.level >= 2 or (g.level >= 1 and g.enforce) or unknown > 0
    return (f"{'!! ' if bad else '   '}usage gauge: level {g.level} — 7 d {g.week_tokens / 1e6:.2f} M tokens "
            f"({g.week_pct:.0f} % of the weekly budget), 5 h {g.five_h_tokens / 1e6:.2f} M ({g.five_h_pct:.0f} %)"
            + (" — enforced" if g.enforce else " — observe only (ai.usage.enforce off)")
            + (f" — {reason}" if reason else ""))


def systems(a: argparse.Namespace) -> tuple[list[Settings], list[str]]:
    """(systems to report, problem lines). Default: the all-pairs system when it runs; otherwise every running pair
    plus every pair that should run (own app.db, not stopped by the user) — a pair whose supervisor died is
    reported (stale heartbeats), not silently left out."""
    if a.instance:
        return [load_settings(extra_env={INSTANCE_ENV: a.instance.upper()})], []
    if a.all_pairs_system:
        return [load_settings(extra_env={INSTANCE_ENV: ""})], []
    base = load_settings()
    if base.paths.instance:
        return [base], []
    running = set(procs.running_supervisors().values())
    if None in running:                             # the all-pairs system runs (pairs cannot run next to it)
        return [base], []
    notes = [f"!! supervisor running for {p}, which is not in config 'instances:' — not reported"
             for p in sorted(x for x in running if x not in base.instances)]
    data = base.paths.data()
    expected = {p for p in base.instances if (data / "instances" / p / "app.db").exists()
                and not (data / "instances" / p / "run" / "manual_stop").exists()}
    pairs = sorted((running & set(base.instances)) | expected)
    for p in sorted(expected - running):
        notes.append(f"!! {p}: its system should run (own app.db, not stopped by the user) but no supervisor runs "
                     f"— scripts\\start.bat {p}")
    if pairs:
        return [load_settings(extra_env={INSTANCE_ENV: p}) for p in pairs], notes
    return [base], notes


def machine_report(s: Settings, now: float) -> list[str]:
    """What every system shares: the MT5 terminal, the supervisors, the machine (power, commit charge, last suspend),
    the price recorder, the AI ledger."""
    out: list[str] = []
    p = out.append
    terms = [x for x in psutil.process_iter(["name"]) if (x.info["name"] or "").lower() == "terminal64.exe"]
    p(("   " if terms else "!! ") + f"MT5 terminal running: {bool(terms)}")
    sups = procs.running_supervisors()
    p(("   " if sups else "!! ") + "supervisors: " + (procs.describe(sups) if sups else "none running"))
    p(machine_line(s, now))
    # research recorder (P1.12, runs outside the supervisor): its status.json is rewritten every flush (5 min)
    rec = recorder_line(s, now)
    if rec:
        p(rec)
    ledger = s.paths.shared() / "ai_usage.db"
    if ledger.exists():
        try:
            con = sqlite3.connect(f"file:{ledger.as_posix()}?mode=ro", uri=True, timeout=10)
            day0 = now // 86_400_000 * 86_400_000
            rows = con.execute("SELECT provider, pair, count(*), COALESCE(sum(ok),0) FROM ai_usage WHERE ts>=? "
                               "GROUP BY provider, pair ORDER BY provider, pair", (day0,)).fetchall()
            con.close()
            p("   AI calls today (every system, shared ledger): "
              + (", ".join(f"{pv}/{pr or '-'} {n} ({ok} ok)" for pv, pr, n, ok in rows) or "none"))
        except sqlite3.Error as exc:
            p(f"!! AI usage ledger unreadable: {exc}")
    gauge = usage_gauge_line(s)
    if gauge:
        p(gauge)
    return out


def system_report(s: Settings, hours: float, now: float) -> list[str]:
    state, logs = s.paths.state(), s.paths.logs()
    since = now - hours * 3_600_000
    out: list[str] = []
    p = out.append
    p(f"## {'system ' + s.paths.instance if s.paths.instance else 'all-pairs system'} — mode "
      f"{s.execution.mode}/{s.execution.trigger} — {state}")
    db = state / "app.db"
    if not db.exists():
        p(f"!! {db} does not exist (never started)")
        return out
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=10)
    try:
        venues = {i.venue for i in InstrumentRegistry.from_settings(s).all()}
    except Exception:  # noqa: BLE001 — unknown: every row is shown as it is
        venues = set(VENUE_BEATS)

    # ---- heartbeats
    for c, st, upd, err, det in con.execute("SELECT collector, state, updated_ms, last_error, detail FROM collector_status "
                                            "ORDER BY collector"):
        if c in VENUE_BEATS and c not in venues and st == "stopped":
            p(f"   {c:17s} n/a (no instrument)")         # stopped by design: this system has no instrument there
            continue
        age = (now - upd) / 1000 if upd else None
        bad = st in ("error",) or (age is not None and age > STALE_S.get(c, 120) and st not in ("stopped",))
        extra = ""
        d = json.loads(det) if det else {}
        if c == "executor":
            dd = d.get("account_drawdown") or {}
            extra = (f" equity {d.get('equity')} today {d.get('today_pnl_pct')}% open {d.get('open_positions')} "
                     f"orders {d.get('open_orders')} kill_switch {d.get('kill_switch')}"
                     + (f" drawdown {dd.get('drawdown_pct')}%" if dd else "")
                     + (" DRAWDOWN STOP TRIPPED" if dd.get("tripped") else ""))
            bad = bad or bool(dd.get("tripped"))
        sb_text, sb_ms, sb_use = _snapshot_build(d) if c == "engine" else ("", None, "")
        if c == "engine":
            extra = f" ai_ready {d.get('ai_ready')} provider {d.get('provider')} quota_left {d.get('quota_left_today')}" \
                    + (f" problem: {d.get('ai_problem')}" if d.get("ai_problem") else "") + sb_text
        p(f"{'!! ' if bad else '   '}{c:17s} {st:12s} beat {age:6.0f}s{extra}" + (f" | last_error: {err[:120]}" if err and bad else ""))
        for x in (d.get("exposure") or []) if c == "executor" else []:
            p(f"     {x.get('pair')} {x.get('kind')} {x.get('side')} {x.get('volume')} @ {x.get('price')} sl {x.get('sl')} "
              f"tps {x.get('tps')} profit {x.get('profit_usd')} ({x.get('decision')})")
        if c == "engine":                                  # Phase 4: follow-up (b) + the adaptive overlay in use
            if sb_ms is not None and sb_ms > s.monitor.snapshot_build_warn_ms:
                p(f"!! snapshot build {sb_ms:.0f} ms ({sb_use}) above monitor.snapshot_build_warn_ms "
                  f"{s.monitor.snapshot_build_warn_ms} — the 5-min screening lags (RAM/CPU)")
            for pair, o in sorted((d["adaptive"] if isinstance(d.get("adaptive"), dict) else {}).items()):
                if isinstance(o, dict):
                    p(f"   adaptive overlay {pair}: adaptive {o.get('adaptive_hash') or '-'} playbook "
                      f"{o.get('playbook_hash') or '-'}"
                      + (f" — AI paused until {o['paused_until']}" if o.get("paused_until") else ""))
    out += _kill_switch_lines(s)

    # ---- events
    ev = collections.Counter(r[0] for r in con.execute("SELECT event FROM ingestion_events WHERE ts>=?", (since,)))
    p("events: " + (", ".join(f"{k} {v}" for k, v in ev.most_common(14)) or "none"))
    why = collections.Counter(_disconnect_reason(d) for (d,) in con.execute(
        "SELECT detail FROM ingestion_events WHERE ts>=? AND event='disconnect'", (since,)))
    if why:
        p("   disconnect reasons: " + ", ".join(f"{k} {v}" for k, v in why.most_common(10)))
    for ts, col, e, det in con.execute("SELECT ts, collector, event, detail FROM ingestion_events WHERE ts>=? AND event IN "
                                       "('exited','killed','cycle_error','order_failed','worker_exit','ai_not_ready',"
                                       "'data_not_ready','system_suspend','terminal_started') ORDER BY ts DESC LIMIT 8",
                                       (since,)):
        p(f"!! {time.strftime('%H:%M', time.gmtime(ts / 1000))} {col} {e}: {(det or '')[:140]}")

    # ---- AI decisions
    rows = con.execute("SELECT pair, status, decision, latency_ms, input_tokens, output_tokens, errors FROM ai_decisions "
                       "WHERE ts>=?", (since,)).fetchall()
    st = collections.Counter(f"{r[1]}/{r[2] or '-'}" for r in rows)
    lat = [r[3] for r in rows if r[3]]
    p(f"AI decisions: {len(rows)} ({', '.join(f'{k} {v}' for k, v in st.most_common())})"
      + (f" | latency avg {sum(lat) / len(lat) / 1000:.0f}s max {max(lat) / 1000:.0f}s" if lat else "")
      + (f" | tokens in {sum(r[4] or 0 for r in rows)} out {sum(r[5] or 0 for r in rows)}" if rows else ""))
    for r in rows:
        if r[1] in ("error", "invalid", "refused"):
            p(f"!! {r[0]} {r[1]}: {(r[6] or '')[:160]}")

    # ---- execution
    ex = con.execute("SELECT pair, decision, execution_state, execution_detail FROM ai_decisions WHERE ts>=? AND "
                     "decision IN ('BUY','SELL')", (since,)).fetchall()
    p(f"trade ideas: {len(ex)} ({', '.join(f'{k} {v}' for k, v in collections.Counter(r[2] for r in ex).most_common())})")
    shadow = [r for r in ex if r[2] == "not_executed" and r[3] and '"shadow": true' in r[3]]
    if shadow:       # D-049: a desk in shadow is gated and scored, never sent - not a rejection, not an error
        ok = sum('"desk_ok": true' in r[3] for r in shadow)
        p(f"   shadow desk ideas (recorded, never sent): {len(shadow)} (desk_ok {ok}) - "
          + ", ".join(f"{k} {v}" for k, v in collections.Counter(r[0] for r in shadow).most_common()))
    reasons = collections.Counter()
    for r in ex:
        if r[2] == "rejected" and r[3]:
            reasons.update(x.split(" ")[0] for x in (json.loads(r[3]).get("reason") or "").split("; ") if x)
    if reasons:
        p("   gate rejections: " + ", ".join(f"{k} {v}" for k, v in reasons.most_common(8)))

    # ---- outcomes (all time) and virtual outcomes
    oc = con.execute("SELECT outcome, count(*), round(sum(outcome_pnl_usd),2) FROM ai_decisions WHERE outcome IS NOT NULL "
                     "GROUP BY outcome").fetchall()
    p("broker/paper outcomes (all time): " + (", ".join(f"{o} {n} ({usd:+} USD)" for o, n, usd in oc) or "none yet"))
    vo = con.execute("SELECT count(*), sum(virtual_outcome='tp1_first'), round(avg(virtual_r),2), "
                     "(SELECT count(*) FROM ai_decisions WHERE virtual_outcome IN ('not_triggered','unresolved_24h')) "
                     "FROM ai_decisions WHERE virtual_outcome IN ('tp1_first','sl_first')").fetchone()
    if vo and (vo[0] or vo[3]):
        p(f"virtual outcomes (every trade idea, on real prices): {vo[0]} resolved — TP1 first {vo[1] or 0}, "
          f"SL first {vo[0] - (vo[1] or 0)}, avg R {vo[2]}; not triggered/unresolved {vo[3]}")
    con.close()

    # ---- log errors
    errs = collections.Counter()
    for f in sorted(logs.glob("*.jsonl")):
        try:
            with f.open("rb") as fh:
                fh.seek(max(0, f.stat().st_size - 2_000_000))
                for line in fh.read().decode("utf-8", "replace").splitlines():
                    if '"level": "ERROR"' in line or '"level": "CRITICAL"' in line:
                        try:
                            j = json.loads(line)
                        except ValueError:
                            continue
                        if j.get("ts", "") >= time.strftime("%Y-%m-%dT%H:%M", time.gmtime(since / 1000)):
                            errs[f"{f.stem}: {j.get('msg', '')[:110]}"] += 1
        except OSError:
            continue
    for k, v in errs.most_common(8):
        p(f"!! log error ×{v} {k}")
    if not errs:
        p("   no ERROR lines in the logs of the window")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=6)
    ap.add_argument("--instance", help="only this pair's system")
    ap.add_argument("--all-pairs-system", action="store_true", help="the single all-pairs system")
    a = ap.parse_args()
    now = time.time() * 1000
    chosen, notes = systems(a)
    s0 = chosen[0]
    out = [f"# report {time.strftime('%Y-%m-%d %H:%M', time.gmtime())} UTC — last {a.hours:g} h — AI {s0.ai.active_provider}"]
    out += machine_report(s0, now) + notes
    for s in chosen:
        out += system_report(s, a.hours, now)
    print("\n".join(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
