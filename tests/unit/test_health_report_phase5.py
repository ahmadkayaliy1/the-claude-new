"""tools/health_report.py Phase 5 (A3/A4/A7): the price-recorder line, the machine line (power source and time on
battery, commit charge, the last suspend), the disconnect reasons with ``PermissionError(13)`` on its own, ``n/a (no
instrument)`` for a venue row a pair has no instrument on, the all-pairs block hidden while the per-pair systems are in
use — and the same points in scripts/check_ops.ps1 (static: never run here)."""
from __future__ import annotations

import argparse
import collections
import ctypes
import datetime as dt
import importlib.util
import json
import os
import sqlite3
from pathlib import Path

import psutil
import pytest

from tradingsystem.core.settings import INSTANCE_ENV, load_settings
from tradingsystem.ingest.common.appdb import AppDB
from tradingsystem.supervisor import control
from tradingsystem.supervisor import supervisor as sv

ROOT = Path(__file__).resolve().parents[2]
REAL = ROOT / "tests" / "fixtures" / "real"
MIN = 60_000
GB = 2**30
RECORDER_UPD = int(dt.datetime(2026, 9, 28, 6, 5, 34, tzinfo=dt.timezone.utc).timestamp() * 1000)   # the fixture


def tool(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture
def hr():
    return tool("health_report")


@pytest.fixture
def system(tmp_path):
    def make(pair: str | None):
        return sv.with_data_dir(load_settings(extra_env={INSTANCE_ENV: pair or ""}), str(tmp_path / "data"))
    return make


def seed(s, *, status=(), events=()) -> Path:
    """A system's app.db: collector rows (collector, state, updated_ms, detail) and events (ts, collector, event,
    detail[, duration_ms]), plus the ai_decisions table system_report reads."""
    db = s.paths.state() / "app.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    AppDB(db).close()
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE IF NOT EXISTS ai_decisions (id TEXT, ts INTEGER, pair TEXT, status TEXT, decision TEXT, "
                "latency_ms INTEGER, input_tokens INTEGER, output_tokens INTEGER, errors TEXT, execution_state TEXT, "
                "execution_detail TEXT, outcome TEXT, outcome_pnl_usd REAL, virtual_outcome TEXT, virtual_r REAL)")
    for c, st, upd, det in status:
        con.execute("INSERT OR REPLACE INTO collector_status(collector, state, detail, updated_ms) VALUES (?,?,?,?)",
                    (c, st, json.dumps(det) if det is not None else None, upd))
    for ev in events:
        ts, col, e, det, dur = (*ev, None) if len(ev) == 4 else ev
        con.execute("INSERT INTO ingestion_events(ts, collector, event, detail, duration_ms) VALUES (?,?,?,?,?)",
                    (ts, col, e, det, dur))
    con.commit()
    con.close()
    return db


# ------------------------------------------------------------------ the price recorder (A3)
def test_the_recorder_line_shows_the_flush_age_the_pid_and_the_stop_file(hr, system, monkeypatch):
    btc = system("BTCUSDT")
    d = btc.paths.data() / "research" / "price_matching"
    d.mkdir(parents=True)
    (d / "status.json").write_bytes((REAL / "recorder_status.json").read_bytes())   # production, pid 10892
    monkeypatch.setattr(hr, "_recorder_alive", lambda pid: True)
    assert hr.recorder_line(btc, RECORDER_UPD + 3 * MIN) == "   price recorder: last flush 3 min ago, pid 10892 alive"
    monkeypatch.setattr(hr, "_recorder_alive", lambda pid: False)
    line = hr.recorder_line(btc, RECORDER_UPD + 45 * MIN)
    assert line.startswith("!! price recorder: last flush 45 min ago, pid 10892 not running — stalled (no flush for "
                           "more than 30 min): the TradingSystemOps-Recorder task restarts it")
    monkeypatch.setattr(hr, "_recorder_alive", lambda pid: True)
    line = hr.recorder_line(btc, RECORDER_UPD + 45 * MIN)
    assert line.startswith("!! ") and "alive but not flushing (hung)" in line
    # it reads STOP only after a completed flush: the advice ends the process (the pid shown), never "create STOP"
    assert "end it with taskkill /PID 10892 /T /F; the TradingSystemOps-Recorder task then starts a fresh one" in line
    assert "STOP" not in line
    (d / "STOP").write_text("", encoding="utf-8")                      # stopped by the owner: not a problem
    line = hr.recorder_line(btc, RECORDER_UPD + 45 * MIN)
    assert line.startswith("   price recorder: last flush 45 min ago") and "STOP present (stopped by the owner" in line
    (d / "STOP").unlink()
    (d / "status.json").write_text('{"pid": 10892, "updat', encoding="utf-8")          # caught half-written
    assert hr.recorder_line(btc, RECORDER_UPD) == "!! price recorder: status.json unreadable"
    (d / "status.json").unlink()
    assert hr.recorder_line(btc, RECORDER_UPD) is None                 # never ran here: no line


def test_recorder_alive_needs_the_recorder_on_the_pids_command_line(hr):
    assert hr._recorder_alive(os.getpid()) is False                   # a python process, but not the recorder
    assert hr._recorder_alive(None) is False and hr._recorder_alive(2**31 - 7) is False


# ------------------------------------------------------------------ the machine line (A4/A7)
def test_the_machine_line_shows_time_on_battery_the_commit_charge_and_the_last_suspend(hr, system, monkeypatch):
    btc, eth = system("BTCUSDT"), system("ETHUSDT")
    now = RECORDER_UPD
    monkeypatch.setattr(hr, "commit_charge", lambda: (10 * GB, 20 * GB))
    monkeypatch.setattr(hr, "battery", lambda: (100.0, True))
    assert hr.machine_line(btc, now) == ("   machine: on AC power (battery 100 %) — commit 50 % of 20.0 GB — last "
                                         "suspend: none recorded")
    # BTC's supervisor.json holds an older sleep, ETH's app.db the newest one (every supervisor records the same)
    control.write_state(btc.paths.state(), {"pid": 1, "last_gap": {"ts": now - 5 * 3600_000, "kind": "suspend",
                                                                   "seconds": 600.0}})
    seed(eth, events=[(now - 2 * 3600_000, "supervisor:all", "system_suspend", "PC was asleep for ~3137s", 3_137_000),
                      (now - 3600_000, "supervisor:all", "stall", "supervisor loop took 30s", 30_000)])
    shared = btc.paths.shared()
    shared.mkdir(parents=True, exist_ok=True)
    (shared / "monitor_state.json").write_text(json.dumps({"battery": {"present": True, "percent": 41.0,
                                                                       "plugged": False,
                                                                       "on_battery_since": now - 23 * MIN}}),
                                               encoding="utf-8")
    monkeypatch.setattr(hr, "battery", lambda: (41.0, False))
    line = hr.machine_line(btc, now)
    assert line.startswith("!! machine: ON BATTERY 41 % for 23 min at least (since 2026-09-28 05:42 UTC) — plug the "
                           "charger in (at 5 % Windows hibernates and every system stops)")
    assert line.endswith("— commit 50 % of 20.0 GB — last suspend: resumed 2026-09-28 04:05 UTC after ~52 min asleep "
                         "(ETHUSDT)")
    (shared / "monitor_state.json").unlink()                           # the monitor has not seen it yet
    assert "ON BATTERY 41 % (time on battery not known yet" in hr.machine_line(btc, now)
    monkeypatch.setattr(hr, "battery", lambda: None)                   # a desktop
    monkeypatch.setattr(hr, "commit_charge", lambda: (int(18.4 * GB), 20 * GB))    # above monitor.commit_warn_pct
    assert hr.machine_line(btc, now).startswith("!! machine: no battery — commit 92 % of 20.0 GB")


def test_the_battery_is_read_as_windows_gives_it_and_an_unknown_ac_line_is_not_on_battery(hr):
    """GetSystemPowerStatus (ACLineStatus, BatteryFlag, BatteryLifePercent). (1, 10, 32) is this laptop's reading
    on 2026-09-28 (on AC, charging + low, 32 %). psutil maps ACLineStatus 255 (unknown) to power_plugged False —
    "on battery"; here it is None (the monitor and this report then say nothing)."""
    assert hr.battery_from_status(1, 10, 32) == (32.0, True)
    assert hr.battery_from_status(255, 10, 32) == (32.0, None)          # unknown AC line: never "on battery"
    assert hr.battery_from_status(255, 1, 80) == (80.0, None)
    assert hr.battery_from_status(0, 2, 27) == (27.0, False)            # offline: on battery
    assert hr.battery_from_status(1, 128, 255) is None                  # no system battery (a desktop)
    assert hr.battery_from_status(255, 255, 255) is None                # the flags cannot be read
    assert hr.battery_from_status(0, 1, 255) is None                    # the charge is unknown


def test_battery_prefers_the_power_status_and_uses_psutil_only_without_it(hr, system, monkeypatch):
    def no_psutil():
        raise AssertionError("psutil must not be read when GetSystemPowerStatus answers")

    monkeypatch.setattr(psutil, "sensors_battery", no_psutil)
    monkeypatch.setattr(hr, "_power_status", lambda: (255, 10, 32))
    assert hr.battery() == (32.0, None)
    monkeypatch.setattr(hr, "commit_charge", lambda: None)
    line = hr.machine_line(system("BTCUSDT"), RECORDER_UPD)
    assert line.startswith("   machine: power source unknown (battery 32 %)") and "ON BATTERY" not in line
    # off Windows (or the call failed): psutil's shape (its sbattery named tuple)
    sbattery = collections.namedtuple("sbattery", "percent secsleft power_plugged")
    monkeypatch.setattr(hr, "_power_status", lambda: None)
    monkeypatch.setattr(psutil, "sensors_battery", lambda: sbattery(percent=41, secsleft=3000, power_plugged=False))
    assert hr.battery() == (41.0, False)
    monkeypatch.setattr(psutil, "sensors_battery", lambda: None)
    assert hr.battery() is None


def test_the_machine_probes_read_this_machine(hr):
    b = hr.battery()
    assert b is None or (0 <= b[0] <= 100 and b[1] in (True, False, None))
    c = hr.commit_charge()
    if os.name == "nt":
        assert ctypes.sizeof(hr._SystemPowerStatus) == 12                # SYSTEM_POWER_STATUS: 4 bytes + 2 DWORDs
        st = hr._power_status()
        assert st is not None and st[0] in (0, 1, 255) and 0 <= st[2] <= 255
        assert c is not None and 0 < c[0] <= c[1]
    else:
        assert hr._power_status() is None and c is None


# ------------------------------------------------------------------ per system: disconnect reasons, n/a rows (A7)
def test_permission_error_13_is_its_own_disconnect_reason(hr, system):
    """The real disconnects of BTC's system on 2026-09-27 (production): DNS failures, closed sockets, stalls,
    handshake timeouts — and 4 PermissionError(13) (Windows refused the socket), counted apart."""
    btc = system("BTCUSDT")
    rows = [tuple(r) for r in json.loads((REAL / "btc_disconnects_2026-09-27.json").read_text(encoding="utf-8"))]
    now = rows[-1][0] + MIN
    seed(btc, status=[("engine", "live", now - 5_000, {"ai_ready": True})], events=rows)
    out = hr.system_report(btc, 24, now)
    (line,) = [x for x in out if x.startswith("   disconnect reasons: ")]
    assert line == ("   disconnect reasons: gaierror(11001) 60, ConnectionClosedError 21, ConnectionError 10, "
                    "TimeoutError 7, PermissionError(13) 4")
    assert hr._disconnect_reason("PermissionError(13, 'Access is denied', None, 5, None)") == "PermissionError(13)"
    assert hr._disconnect_reason("lost") == "lost" and hr._disconnect_reason(None) == "?"


def test_a_venue_row_stopped_by_design_reads_na_no_instrument(hr, system):
    """XAUUSD has no Binance spot instrument: its binance_spot row is 'stopped' by design (production 2026-09-28);
    a pair that has an instrument there keeps the plain 'stopped'."""
    now = RECORDER_UPD
    rows = [("binance_spot", "stopped", now - 8 * 3600_000, None), ("binance_usdm", "stopped", now - 60_000, None),
            ("mt5", "live", now - 1_000, {"rows_written": 1})]
    xau, btc = system("XAUUSD"), system("BTCUSDT")
    seed(xau, status=rows)
    seed(btc, status=rows)
    out = hr.system_report(xau, 6, now)
    assert "   binance_spot      n/a (no instrument)" in out
    assert not [x for x in out if "binance_spot" in x and "stopped" in x]
    assert [x for x in out if x.lstrip("! ").startswith("binance_usdm") and " stopped " in x]   # XAUUSDT perp: real
    out = hr.system_report(btc, 6, now)
    assert not [x for x in out if "n/a (no instrument)" in x]
    assert [x for x in out if x.lstrip("! ").startswith("binance_spot") and " stopped " in x]


def test_the_all_pairs_block_is_hidden_while_the_pair_systems_are_in_use(hr, system, monkeypatch):
    def load(**kw):                                                    # noqa: ANN003
        return system((kw.get("extra_env") or {}).get(INSTANCE_ENV) or None)

    monkeypatch.setattr(hr, "load_settings", load)
    AppDB(system(None).paths.state() / "app.db").close()               # an old all-pairs install's app.db
    seed(system("BTCUSDT"))
    monkeypatch.setattr(hr.procs, "running_supervisors", lambda older_s=None: {7: "BTCUSDT"})
    chosen, notes = hr.systems(argparse.Namespace(instance=None, all_pairs_system=False))
    assert [s.paths.instance for s in chosen] == ["BTCUSDT"] and notes == []
    monkeypatch.setattr(hr.procs, "running_supervisors", lambda older_s=None: {8: None})     # the all-pairs runs
    chosen, _ = hr.systems(argparse.Namespace(instance=None, all_pairs_system=False))
    assert [s.paths.instance for s in chosen] == [None]


# ------------------------------------------------------------------ scripts\check_ops.ps1 (static: never run here)
def test_check_ops_skips_the_all_pairs_status_while_pairs_run_and_shows_battery_time_and_last_suspend():
    raw = (ROOT / "scripts" / "check_ops.ps1").read_bytes()
    assert raw.isascii() and b"\n" not in raw.replace(b"\r\n", b"")        # Windows PowerShell 5.1: ASCII + CRLF
    text = raw.decode("ascii")
    # the all-pairs status is captured, printed only when it runs (exit 0) or when no pair has a system of its own
    assert "$allPairs = @(& $py -m tradingsystem run --status)" in text
    block = text.split("$allPairs = @(& $py -m tradingsystem run --status)", 1)[1]
    run_branch, rest = block.split("if ($LASTEXITCODE -eq 0) {", 1)[1].split("} else {", 1)
    assert "$allPairs | ForEach-Object { Write-Host $_ }" in run_branch
    pairs_branch, none_branch = rest.split("} else { $allPairs", 1)
    assert 'Write-Host "all-pairs system: not in use (one system per pair below)"' in pairs_branch
    assert "run --status --instance $p" in pairs_branch and none_branch.startswith(" | ForEach-Object")
    assert text.count("tradingsystem run --status") == 2                  # never a second bare all-pairs call
    # the power section: time on battery from the monitor's state, the last suspend from the supervisors
    assert "monitor_state.json" in text and ".battery.on_battery_since" in text
    assert "run\\supervisor.json" in text and '$g.kind -eq "suspend"' in text and "Last suspend" in text
