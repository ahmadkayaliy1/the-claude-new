"""tools/monitor.py (Phase 4, §3.8 component 4): the pure-Python monitor on seeded tmp data roots.

Every test re-points the data root to tmp_path, stubs the machine probes (clock, RAM, disk, adapters) and captures
notifications; a diagnosis session is never started for real (the spawn is refused unless a test mocks Popen).
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import json
import os
import sqlite3
import subprocess
from pathlib import Path

import pytest

from tradingsystem.core.killswitch import read_reason
from tradingsystem.core.settings import INSTANCE_ENV, load_settings
from tradingsystem.core.timeutil import MS_PER_HOUR, MS_PER_MINUTE, iso
from tradingsystem.execution import drawdown
from tradingsystem.ingest.common.appdb import AppDB
from tradingsystem.supervisor import control, winops
from tradingsystem.supervisor import supervisor as sv

ROOT = Path(__file__).resolve().parents[2]
T0 = int(dt.datetime(2026, 9, 23, 12, 0, tzinfo=dt.timezone.utc).timestamp() * 1000)     # a Wednesday, markets open
MIN = MS_PER_MINUTE


def tool(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture
def mon(monkeypatch):
    m = tool("monitor")
    clock = {"now": T0, "awake_lag_s": 0.0}           # awake clock = wall clock (minus time asleep)
    monkeypatch.setattr(m, "_clock", lambda: (clock["now"] / 1000 - clock["awake_lag_s"], 0))
    monkeypatch.setattr(m, "_free_ram_mb", lambda: 8000.0)
    monkeypatch.setattr(m, "_free_disk_gb", lambda p: 500.0)
    monkeypatch.setattr(m, "_adapters", lambda: {})
    sent: list[tuple] = []
    monkeypatch.setattr(m, "_notify",
                        lambda s, level, title, text, *, key, pair: sent.append((level, key, title, text)))

    def refuse(cmd, cwd):                              # noqa: ANN001
        raise AssertionError(f"a diagnosis session must not start here: {cmd}")

    monkeypatch.setattr(m, "_spawn", refuse)
    monkeypatch.delenv(m.NO_DIAGNOSE_ENV, raising=False)
    m.clock, m.sent = clock, sent
    return m


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv(INSTANCE_ENV, "")
    data = tmp_path / "data"

    def system(pair: str | None):
        s = load_settings(extra_env={INSTANCE_ENV: pair or ""})
        return sv.with_data_dir(s, str(data))

    return {"data": data, "base": system(None), "BTCUSDT": system("BTCUSDT"), "ETHUSDT": system("ETHUSDT"),
            "XAUUSD": system("XAUUSD"), "system": system}


def seed(s, *, status=(), events=(), quotes=()) -> Path:
    """A system's app.db with collector rows (collector, state, updated_ms, last_error, detail), events
    (ts, collector, event, detail[, duration_ms]) and quotes (instrument, ts)."""
    db = s.paths.state() / "app.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    AppDB(db).close()
    con = sqlite3.connect(db)
    for c, state, upd, err, det in status:
        con.execute("INSERT OR REPLACE INTO collector_status(collector, state, last_data_ms, last_error, "
                    "last_error_ms, detail, updated_ms) VALUES (?,?,?,?,?,?,?)",
                    (c, state, upd, err, upd if err else None, json.dumps(det) if det is not None else None, upd))
    for ev in events:
        ts, col, e, det, dur = (*ev, None) if len(ev) == 4 else ev
        con.execute("INSERT INTO ingestion_events(ts, collector, event, detail, duration_ms) VALUES (?,?,?,?,?)",
                    (ts, col, e, det, dur))
    for inst, ts in quotes:
        con.execute("INSERT OR REPLACE INTO latest_quote VALUES (?,?,?,?,?,?,?)", (inst, ts, 1.0, 1.1, None, "t", ts))
    con.commit()
    con.close()
    return db


def fresh(now: int = T0, **extra_detail) -> list[tuple]:
    """Healthy collector rows of one system."""
    return [("supervisor", "live", now - 5_000, None, {}), ("engine", "live", now - 5_000, None, {}),
            ("executor", "live", now - 1_000, None, {"mode": "paper", "equity": 100.0, "today_pnl_pct": 0.0,
                                                      "exposure": [], **extra_detail}),
            ("mt5", "live", now - 2_000, None, {})]


def stale_engine(s, now: int = T0) -> None:
    """Healthy rows at ``now`` except the engine, silent since T0 − 20 min."""
    rows = fresh(now)
    rows[1] = ("engine", "live", T0 - 20 * MIN, None, {})
    seed(s, status=rows)


def files(root: Path) -> set[str]:
    """Every file under ``root`` except SQLite's own -wal/-shm companions (a read-only reader of a WAL database
    creates them; the services always have them anyway)."""
    return {p.relative_to(root).as_posix() for p in root.rglob("*")
            if p.is_file() and not p.name.endswith(("-wal", "-shm"))}


def switches(root: Path) -> set[str]:
    return {x for x in files(root) if x.endswith("KILL_SWITCH")}


def run(mon, world, systems, *, at=None, notes=None, **kw):
    if at is not None:
        mon.clock["now"] = at
    return mon.run(world["base"], [world[p] for p in systems], notes, now=mon.clock["now"], **kw)


# ------------------------------------------------------------------ stale heartbeat (acceptance: one run)
def test_a_stale_heartbeat_is_detected_in_one_run(mon, world):
    rows = [r if r[0] != "engine" else ("engine", "live", T0 - 20 * MIN, None, {}) for r in fresh()]
    rows.append(("mt5_backfill", "stopped", T0 - 3 * MS_PER_HOUR, None, {}))       # stopped rows never count
    seed(world["BTCUSDT"], status=rows)
    res = run(mon, world, ["BTCUSDT"])
    assert [f.key for f in res.problems] == ["stale:BTCUSDT:engine"]
    assert res.exit_code == 1 and not res.held
    assert [s[1] for s in mon.sent] == ["monitor:stale:BTCUSDT:engine"]
    assert switches(world["data"]) == set()
    st = json.loads((world["data"] / "shared" / "monitor_state.json").read_text(encoding="utf-8"))
    assert st["alerts"]["stale:BTCUSDT:engine"]["level"] == "warn"


def test_a_healthy_system_exits_0(mon, world):
    seed(world["BTCUSDT"], status=fresh())
    res = run(mon, world, ["BTCUSDT"])
    assert res.findings == [] and res.exit_code == 0 and mon.sent == []


def test_after_a_suspend_staleness_needs_two_runs_and_time_asleep_is_not_staleness(mon, world):
    s = world["BTCUSDT"]
    rows = fresh()
    rows[1] = ("engine", "live", T0 - 15 * MIN, None, {})          # 15 min wall, 10 of them asleep → 5 min: fine
    rows[2] = ("executor", "live", T0 - 40 * MIN, None, {"mode": "paper"})     # 30 min awake → really stale
    seed(s, status=rows)
    control.write_state(s.paths.state(), {"pid": 1, "heartbeat_ms": T0 - 60_000, "awake_s": T0 / 1000 - 60,
                                          "last_gap": {"ts": T0 - 2 * MIN, "kind": "suspend", "seconds": 600.0}})
    first = run(mon, world, ["BTCUSDT"])
    assert first.problems == [] and first.exit_code == 0
    assert len(first.held) == 1 and "executor" in first.held[0] and "suspend" in first.held[0]
    second = run(mon, world, ["BTCUSDT"], at=T0 + 6 * MIN)          # still settling (gap 8 min ago), seen 6 min ago
    assert [f.key for f in second.problems] == ["stale:BTCUSDT:executor"]


def test_the_machine_sleeping_between_two_runs_holds_staleness_once(mon, world):
    s = world["BTCUSDT"]
    seed(s, status=fresh())
    assert run(mon, world, ["BTCUSDT"]).findings == []
    later = T0 + 40 * MIN
    rows = fresh(later)
    rows[1] = ("engine", "live", T0, None, {})                      # beat just before the machine slept 30 min
    seed(s, status=rows)
    mon.clock["awake_lag_s"] = 30 * 60.0                            # 40 min wall, 10 min awake
    res = run(mon, world, ["BTCUSDT"], at=later)
    assert res.problems == [] and "slept" in res.held[0]
    rows = fresh(later + 15 * MIN)
    rows[1] = ("engine", "live", T0, None, {})
    seed(s, status=rows)
    res = run(mon, world, ["BTCUSDT"], at=later + 15 * MIN)         # no sleep since the previous run
    assert [f.key for f in res.problems] == ["stale:BTCUSDT:engine"]


# ------------------------------------------------------------------ order burst (acceptance: only that pair's file)
def test_an_order_burst_writes_exactly_that_pairs_switch(mon, world):
    data = world["data"]
    orders = [(T0 - (50 - i * 10) * MIN, "executor", "order", f"paper BTCUSDT {i:08d}: legs 2") for i in range(4)]
    seed(world["BTCUSDT"], status=fresh(), events=orders)
    seed(world["ETHUSDT"], status=fresh(), events=[(T0 - 5 * MIN, "executor", "order", "paper ETHUSDT 1: legs 1")] * 3)
    before = files(data)
    res = run(mon, world, ["BTCUSDT", "ETHUSDT"])
    assert files(data) - before == {"instances/BTCUSDT/KILL_SWITCH", "shared/monitor_state.json"}
    (burst,) = res.problems
    assert burst.level == "critical" and burst.switch == "BTCUSDT" and "engaged" in burst.action
    why = read_reason(data / "instances" / "BTCUSDT" / "KILL_SWITCH")
    assert why["actor"] == "monitor" and why["scope"] == "BTCUSDT" and "4 orders" in why["reason"]
    # the user reviews and turns it off: the same four orders never engage it again
    (data / "instances" / "BTCUSDT" / "KILL_SWITCH").unlink()
    for p in ("BTCUSDT", "ETHUSDT"):
        seed(world[p], status=fresh(T0 + 15 * MIN))
    again = run(mon, world, ["BTCUSDT", "ETHUSDT"], at=T0 + 15 * MIN)
    assert again.problems == [] and switches(data) == set()


def test_the_all_pairs_system_parses_the_pair_of_each_order(mon, world):
    ev = [(T0 - i * MIN, "executor", "order", f"paper ETHUSDT {i:08d}: placed") for i in range(1, 5)]
    ev.append((T0 - 3 * MIN, "executor", "order", "paper BTCUSDT 0000000a: placed"))
    seed(world["base"], status=fresh(), events=ev)
    res = run(mon, world, ["base"])
    assert switches(world["data"]) == {"instances/ETHUSDT/KILL_SWITCH"}
    assert [f.pair for f in res.problems] == ["ETHUSDT"]


# ------------------------------------------------------------------ equity drop (acceptance: the global file)
def test_an_equity_drop_between_runs_writes_the_global_switch(mon, world):
    data = world["data"]
    acct = {"account": "mt5:Demo:123", "peak": 100.0, "drawdown_pct": 0.0, "tripped": None}
    for p in ("BTCUSDT", "ETHUSDT"):                                  # one MT5 account, reported by both executors
        seed(world[p], status=fresh(equity=100.0, account_drawdown=acct, mode="mt5"))
    assert run(mon, world, ["BTCUSDT", "ETHUSDT"]).findings == []
    later = T0 + 15 * MIN
    for p in ("BTCUSDT", "ETHUSDT"):
        seed(world[p], status=fresh(later, equity=88.0, account_drawdown=acct, mode="mt5"))
    res = run(mon, world, ["BTCUSDT", "ETHUSDT"], at=later)
    (drop,) = res.problems                                           # counted once per account, not per executor
    assert drop.level == "critical" and drop.switch == "*" and "−12.0 %" in drop.text
    assert switches(data) == {"KILL_SWITCH"}
    assert read_reason(data / "KILL_SWITCH")["scope"] == "all"


def test_a_smaller_equity_drop_only_warns(mon, world):
    seed(world["BTCUSDT"], status=fresh(equity=100.0))
    run(mon, world, ["BTCUSDT"])
    seed(world["BTCUSDT"], status=fresh(T0 + 15 * MIN, equity=94.0))
    res = run(mon, world, ["BTCUSDT"], at=T0 + 15 * MIN)
    assert [(f.level, f.switch) for f in res.problems] == [("warn", None)]
    assert switches(world["data"]) == set()


# ------------------------------------------------------------------ dedupe across runs
def test_findings_are_deduplicated_across_runs_and_reminded_later(mon, world):
    s = world["BTCUSDT"]

    def stale_at(now):
        stale_engine(s, now)

    stale_at(T0)
    assert run(mon, world, ["BTCUSDT"]).exit_code == 1 and len(mon.sent) == 1
    stale_at(T0 + 15 * MIN)
    res = run(mon, world, ["BTCUSDT"], at=T0 + 15 * MIN)
    assert res.exit_code == 1 and len(mon.sent) == 1 and not res.problems[0].notified   # a problem, not re-sent
    stale_at(T0 + 7 * MS_PER_HOUR)
    run(mon, world, ["BTCUSDT"], at=T0 + 7 * MS_PER_HOUR)                                 # warn reminder after 6 h
    assert len(mon.sent) == 2
    seed(s, status=fresh(T0 + 8 * MS_PER_HOUR))
    assert run(mon, world, ["BTCUSDT"], at=T0 + 8 * MS_PER_HOUR).findings == []          # recovered: forgotten
    stale_at(T0 + 9 * MS_PER_HOUR)
    run(mon, world, ["BTCUSDT"], at=T0 + 9 * MS_PER_HOUR)                                 # a new episode: sent again
    assert len(mon.sent) == 3


# ------------------------------------------------------------------ follow-up (b): snapshot build on two runs
def test_a_slow_snapshot_build_needs_two_consecutive_runs(mon, world):
    s = world["BTCUSDT"]
    slow = {"last": 5200, "max": 15000, "median": 4100, "n": 20}

    def engine(now, sb):
        rows = fresh(now)
        rows[1] = ("engine", "live", now - 5_000, None, {"snapshot_build_ms": sb})
        seed(s, status=rows)

    engine(T0, slow)
    first = run(mon, world, ["BTCUSDT"])
    assert first.problems == [] and "snapshot build" in first.held[0]
    engine(T0 + 15 * MIN, slow)
    (f,) = run(mon, world, ["BTCUSDT"], at=T0 + 15 * MIN).problems
    assert f.key == "snapshot:BTCUSDT" and "4100 ms (median of the last 20" in f.text
    engine(T0 + 30 * MIN, {"last": 5200, "max": 15000, "median": 900, "n": 20})    # the median decides, not last/max
    assert run(mon, world, ["BTCUSDT"], at=T0 + 30 * MIN).findings == []
    engine(T0 + 45 * MIN, {"last": 3500, "max": 3500})                           # an engine without the median
    assert run(mon, world, ["BTCUSDT"], at=T0 + 45 * MIN).problems == []
    engine(T0 + 60 * MIN, {"last": 3600, "max": 3600})
    assert [f.key for f in run(mon, world, ["BTCUSDT"], at=T0 + 60 * MIN).problems] == ["snapshot:BTCUSDT"]


# ------------------------------------------------------------------ diagnosis launch
def test_a_new_warning_starts_one_detached_diagnosis(mon, world, monkeypatch, tmp_path):
    script = tmp_path / "run_session.py"
    script.write_text("# stand-in\n", encoding="utf-8")
    monkeypatch.setattr(mon, "RUN_SESSION", script)
    monkeypatch.setattr(mon, "_spawn", tool("monitor")._spawn)        # the real launcher, with Popen mocked
    calls = []

    class FakePopen:
        def __init__(self, cmd, **kw):
            calls.append((cmd, kw))
            self.pid = 4242

    monkeypatch.setattr(winops.subprocess, "Popen", FakePopen)
    stale_engine(world["BTCUSDT"])
    res = run(mon, world, ["BTCUSDT"], diagnose=True)
    assert res.diagnose["launched"] and res.diagnose["pid"] == 4242
    (cmd, kw), = calls
    assert cmd[1:5] == [str(script), "--kind", "diagnose", "--reason"]
    assert Path(cmd[0]).name.lower().startswith("python") and "pythonw" not in Path(cmd[0]).name.lower()
    assert cmd[5].startswith("warn: BTCUSDT: stale heartbeat — ") and "\n" not in cmd[5]
    if winops.WIN:
        flags = kw["creationflags"]
        assert flags & subprocess.CREATE_NO_WINDOW and flags & subprocess.CREATE_NEW_PROCESS_GROUP
        assert kw["stdin"] == subprocess.DEVNULL
    st = json.loads((world["data"] / "shared" / "monitor_state.json").read_text(encoding="utf-8"))
    assert st["last_diagnose_ms"] == T0
    # a second, different warning an hour later: the interval (3 h) holds it back
    stale_engine(world["BTCUSDT"], T0 + 60 * MIN)
    seed(world["ETHUSDT"], status=[r if r[0] != "mt5" else ("mt5", "live", T0, None, {}) for r in fresh(T0 + 60 * MIN)])
    res = run(mon, world, ["BTCUSDT", "ETHUSDT"], at=T0 + 60 * MIN, diagnose=True)
    assert not res.diagnose["launched"] and "last diagnosis" in res.diagnose["why"] and len(calls) == 1


def test_the_diagnosis_is_suppressed_by_the_env_the_config_and_without_new_warnings(mon, world, monkeypatch, tmp_path):
    script = tmp_path / "run_session.py"
    script.write_text("# stand-in\n", encoding="utf-8")
    monkeypatch.setattr(mon, "RUN_SESSION", script)
    stale_engine(world["BTCUSDT"])
    monkeypatch.setenv(mon.NO_DIAGNOSE_ENV, "1")
    res = run(mon, world, ["BTCUSDT"], diagnose=True)                # _spawn refuses: it must not be called
    assert not res.diagnose["launched"] and mon.NO_DIAGNOSE_ENV in res.diagnose["why"]
    monkeypatch.delenv(mon.NO_DIAGNOSE_ENV)
    stale_engine(world["BTCUSDT"], T0 + 15 * MIN)
    res = run(mon, world, ["BTCUSDT"], at=T0 + 15 * MIN, diagnose=True)
    assert res.exit_code == 1 and res.diagnose["why"] == "no new warning"          # the same warning, already sent
    world["base"] = world["base"].model_copy(update={"monitor": world["base"].monitor.model_copy(
        update={"diagnose_enabled": False})})
    stale_engine(world["BTCUSDT"], T0 + 30 * MIN)
    seed(world["ETHUSDT"], status=[r if r[0] != "mt5" else ("mt5", "live", T0, None, {}) for r in fresh(T0 + 30 * MIN)])
    res = run(mon, world, ["BTCUSDT", "ETHUSDT"], at=T0 + 30 * MIN, diagnose=True)
    assert "diagnose_enabled" in res.diagnose["why"]
    stale_engine(world["ETHUSDT"], T0 + 45 * MIN)                     # yet another new warning
    res = run(mon, world, ["ETHUSDT"], at=T0 + 45 * MIN)             # a library call never diagnoses
    assert not res.diagnose["launched"]


# ------------------------------------------------------------------ executor rules
def test_a_position_without_sl_and_the_daily_loss_limit(mon, world):
    data = world["data"]
    expo = [{"pair": "BTCUSDT", "decision": "abcd1234", "kind": "position", "side": "BUY", "volume": 0.01,
             "price": 60000, "sl": None, "tps": []},
            {"pair": "BTCUSDT", "decision": "abcd9999", "kind": "position", "side": "BUY", "volume": 0.01,
             "price": 60000, "sl": 59000, "tps": []}]
    seed(world["BTCUSDT"], status=fresh(exposure=expo, today_pnl_pct=-10.4))
    res = run(mon, world, ["BTCUSDT"])
    keys = {f.key: f for f in res.problems}
    assert set(keys) == {"nosl:BTCUSDT:BTCUSDT:abcd1234", "dayloss:BTCUSDT:2026-09-23"}
    assert keys["dayloss:BTCUSDT:2026-09-23"].switch == "BTCUSDT"
    assert switches(data) == {"instances/BTCUSDT/KILL_SWITCH"}
    (data / "instances" / "BTCUSDT" / "KILL_SWITCH").unlink()          # the user decides to go on today
    seed(world["BTCUSDT"], status=fresh(T0 + 15 * MIN, exposure=expo, today_pnl_pct=-10.6))
    res = run(mon, world, ["BTCUSDT"], at=T0 + 15 * MIN)
    again = {f.key: f for f in res.findings}["dayloss:BTCUSDT:2026-09-23"]
    assert switches(data) == set() and again.switch is None and "not re-engaged" in again.text
    assert not again.notified
    seed(world["ETHUSDT"], status=fresh(T0 + 30 * MIN, today_pnl_pct=-8.5))
    (warn,) = [f for f in run(mon, world, ["ETHUSDT"], at=T0 + 30 * MIN).problems]
    assert warn.level == "warn" and warn.switch is None and "near the limit" in warn.title


def test_an_mt5_ipc_hang_is_critical_after_the_limit_and_never_restarts_anything(mon, world):
    rows = fresh()
    rows[2] = ("executor", "error", T0 - 1_000, "(-10004, 'No IPC connection')",
               {"mode": "mt5", "failing_since": iso(T0 - 6 * MIN)})
    rows[3] = ("mt5", "reconnecting", T0 - 1_000, "RuntimeError('IPC timeout (-10005)')", {})
    seed(world["BTCUSDT"], status=rows)
    res = run(mon, world, ["BTCUSDT"])
    assert [f.key.rsplit(":", 1)[0] for f in res.problems] == ["ipc:BTCUSDT:executor"]      # mt5: first seen now
    assert res.problems[0].level == "critical" and switches(world["data"]) == set()
    res = run(mon, world, ["BTCUSDT"], at=T0 + 6 * MIN)
    assert sorted(f.key.rsplit(":", 1)[0] for f in res.problems) == ["ipc:BTCUSDT:executor", "ipc:BTCUSDT:mt5"]


def test_a_drawdown_trip_is_reported_once(mon, world):
    shared = world["data"] / "shared"
    shared.mkdir(parents=True)
    trip = T0 - 30 * MIN
    (shared / drawdown.FILE).write_text(json.dumps({"mt5:Demo:1": {"peak": 110.0, "peak_ms": trip - MS_PER_HOUR,
                                                                   "tripped_ms": trip, "tripped_equity": 96.0}}),
                                        encoding="utf-8")
    acct = {"account": "mt5:Demo:1", "peak": 110.0, "drawdown_pct": 12.7, "tripped": iso(trip)}
    seed(world["BTCUSDT"], status=fresh(account_drawdown=acct, mode="mt5"))
    res = run(mon, world, ["BTCUSDT"])
    assert [f.key for f in res.problems] == [f"drawdown:mt5:Demo:1:{trip}"] and len(mon.sent) == 1
    seed(world["BTCUSDT"], status=fresh(T0 + 2 * MS_PER_HOUR, account_drawdown=acct, mode="mt5"))
    res = run(mon, world, ["BTCUSDT"], at=T0 + 2 * MS_PER_HOUR)       # past the critical reminder: still once
    assert res.problems and len(mon.sent) == 1


# ------------------------------------------------------------------ restarts, outages, quotes
def test_restart_loop_outage_and_stale_quotes(mon, world):
    ev = [(T0 - i * 10 * MIN, "supervisor:engine", "exited", "code 1") for i in range(1, 5)]
    ev += [(T0 - 20 * MIN, "mt5", "disconnect", "lost"),
           (T0 - 30 * MIN, "binance_spot:spot", "disconnect", "x"),
           (T0 - 29 * MIN, "binance_spot:spot", "connect", "y")]
    seed(world["BTCUSDT"], status=fresh(), events=ev,
         quotes=[("binance_spot:BTCUSDT", T0 - 30 * MIN), ("mt5:BTCUSD@", T0 - 5_000)])
    keys = sorted(f.key.split(":")[0] for f in run(mon, world, ["BTCUSDT"]).problems)
    assert keys == ["outage", "quote", "restart"]


def test_a_quote_of_a_closed_market_is_not_stale(mon, world):
    saturday = int(dt.datetime(2026, 9, 26, 12, 0, tzinfo=dt.timezone.utc).timestamp() * 1000)
    mon.clock["now"] = saturday
    seed(world["XAUUSD"], status=fresh(saturday), quotes=[("mt5:XAUUSD@", saturday - 16 * MS_PER_HOUR)])
    assert run(mon, world, ["XAUUSD"]).findings == []


# ------------------------------------------------------------------ machine checks
def test_a_vpn_change_is_info_only(mon, world, monkeypatch):
    world["base"] = world["base"].model_copy(update={"monitor": world["base"].monitor.model_copy(
        update={"vpn_adapter_names": ["Local Area Connection 4"]})})
    seed(world["BTCUSDT"], status=fresh())
    monkeypatch.setattr(mon, "_adapters", lambda: {"Local Area Connection 4": True})
    assert run(mon, world, ["BTCUSDT"]).findings == []
    monkeypatch.setattr(mon, "_adapters", lambda: {})
    seed(world["BTCUSDT"], status=fresh(T0 + 15 * MIN))
    res = run(mon, world, ["BTCUSDT"], at=T0 + 15 * MIN)
    assert [(f.level, f.title) for f in res.findings] == [("info", "VPN down")] and res.exit_code == 0


def test_low_resources_and_an_overdue_daily_review(mon, world, monkeypatch):
    seed(world["BTCUSDT"], status=fresh())
    monkeypatch.setattr(mon, "_free_ram_mb", lambda: 120.0)
    monkeypatch.setattr(mon, "_free_disk_gb", lambda p: 2.0)
    assert sorted(f.key for f in run(mon, world, ["BTCUSDT"]).problems) == ["disk", "ram"]
    later = T0 + 31 * MS_PER_HOUR
    seed(world["BTCUSDT"], status=fresh(later))
    assert "review_overdue:never" in [f.key for f in run(mon, world, ["BTCUSDT"], at=later).problems]
    reviews = world["data"] / "reviews"
    reviews.mkdir(parents=True)
    pack = reviews / "20260924T1800Z_daily.md"
    pack.write_text("# pack\n", encoding="utf-8")
    os.utime(pack, ((later - MS_PER_HOUR) / 1000, (later - MS_PER_HOUR) / 1000))
    # a pack alone (a --dry-run, a start that failed) is not a review: still overdue
    res = run(mon, world, ["BTCUSDT"], at=later + MIN)
    assert [f for f in res.problems if f.key.startswith("review")]
    sess = reviews / "20260924T1800Z_daily.session.json"
    sess.write_text('{"status": "running"}', encoding="utf-8")
    os.utime(sess, ((later - MS_PER_HOUR) / 1000, (later - MS_PER_HOUR) / 1000))
    assert [f for f in run(mon, world, ["BTCUSDT"], at=later + 2 * MIN).problems if f.key.startswith("review")]
    sess.write_text('{"status": "max_turns"}', encoding="utf-8")        # a finished session (ok or max_turns)
    os.utime(sess, ((later - MS_PER_HOUR) / 1000, (later - MS_PER_HOUR) / 1000))
    res = run(mon, world, ["BTCUSDT"], at=later + 3 * MIN)
    assert not [f for f in res.problems if f.key.startswith("review")]


def test_system_notes_become_warnings_and_a_stopped_system_is_skipped(mon, world):
    s = world["XAUUSD"]
    seed(s, status=[("engine", "live", T0 - 5 * MS_PER_HOUR, None, {})])
    (control.run_dir(s.paths.state()) / control.HOLD).parent.mkdir(parents=True, exist_ok=True)
    (control.run_dir(s.paths.state()) / control.HOLD).write_text("1", encoding="utf-8")
    note = "!! ETHUSDT: its system should run (own app.db, not stopped by the user) but no supervisor runs"
    res = run(mon, world, ["XAUUSD"], notes=[note])
    assert [f.title for f in res.problems] == ["System not running"] and "stopped by the user" in res.skipped[0]


def test_a_running_pair_without_an_app_db_here_means_the_monitor_is_blind(mon, world):
    res = run(mon, world, ["BTCUSDT", "base"])                   # BTC chosen (its supervisor runs), nothing seeded
    assert [f.key for f in res.problems] == ["blind:BTCUSDT"]
    assert "all: " in res.skipped[0]                             # the all-pairs fallback without a db: just skipped


def test_the_state_file_is_replaced_atomically_with_retries(mon, tmp_path, monkeypatch):
    path = tmp_path / "shared" / "monitor_state.json"
    real, calls = os.replace, []

    def flaky(src, dst):
        calls.append(dst)
        if len(calls) < 3:
            raise PermissionError(13, "held open by a reader")
        real(src, dst)

    monkeypatch.setattr(mon.os, "replace", flaky)
    monkeypatch.setattr(mon.time, "sleep", lambda s: None)
    mon.save_state(path, {"a": 1})
    assert json.loads(path.read_text(encoding="utf-8")) == {"a": 1} and len(calls) == 3
    assert [p.name for p in path.parent.iterdir()] == ["monitor_state.json"]           # no temp file left
    path.write_text("{broken", encoding="utf-8")
    assert mon.load_state(path) == {}


def test_dry_run_writes_nothing_and_sends_nothing(mon, world):
    data = world["data"]
    orders = [(T0 - i * MIN, "executor", "order", f"paper BTCUSDT {i:08d}: legs 1") for i in range(1, 6)]
    seed(world["BTCUSDT"], status=fresh(), events=orders)
    before = files(data)
    res = run(mon, world, ["BTCUSDT"], dry_run=True)
    assert files(data) == before and mon.sent == []
    assert res.problems[0].action.startswith("dry run: would engage") and res.exit_code == 1


def test_a_broken_check_is_reported_and_the_others_still_run(mon, world, monkeypatch):
    stale_engine(world["BTCUSDT"])

    def _quotes(self, *a):                                          # noqa: ANN001, ANN002
        raise ZeroDivisionError("boom")

    monkeypatch.setattr(mon.Monitor, "_quotes", _quotes)
    keys = sorted(f.key for f in run(mon, world, ["BTCUSDT"]).problems)
    assert keys == ["check:BTCUSDT:quotes", "stale:BTCUSDT:engine"]


# ------------------------------------------------------------------ CLI
def test_main_json_quiet_and_exit_codes(mon, world, monkeypatch, capsys):
    rows = fresh(T0)
    seed(world["BTCUSDT"], status=rows)
    monkeypatch.setattr(mon, "load_settings", lambda **kw: world["base"])
    monkeypatch.setattr(mon, "setup_logging", lambda *a, **kw: None)
    monkeypatch.setattr(mon, "_flush_notify", lambda *a: None)
    monkeypatch.setattr(mon.hr, "load_settings", lambda **kw: world["system"](
        (kw.get("extra_env") or {}).get(INSTANCE_ENV) or None))
    monkeypatch.setattr(mon.hr.procs, "running_supervisors", lambda older_s=None: {7: "BTCUSDT"})
    monkeypatch.setattr(mon, "now_ms", lambda: T0 + 20 * MIN)           # the rows are 20 min old by then
    assert mon.main(["--json", "--no-diagnose"]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["systems"] == ["BTCUSDT"] and out["exit"] == 1
    assert {f["key"].split(":")[0] for f in out["findings"]} == {"stale"}
    assert mon.main(["--quiet", "--no-diagnose"]) == 1 and capsys.readouterr().out == ""
    monkeypatch.setattr(mon, "now_ms", lambda: T0)
    seed(world["BTCUSDT"], status=fresh(T0))
    assert mon.main(["--no-diagnose"]) == 0 and "no finding" in capsys.readouterr().out
    monkeypatch.setattr(mon.sys, "stdout", None)                     # pythonw: no console at all
    monkeypatch.setattr(mon.sys, "stderr", None)
    assert mon.main(["--no-diagnose"]) == 0
