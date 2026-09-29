"""tools/monitor.py (Phase 4, §3.8 component 4): the pure-Python monitor on seeded tmp data roots.

Every test re-points the data root to tmp_path, stubs the machine probes (clock, RAM, disk, adapters) and captures
notifications; a diagnosis session is never started for real (the spawn is refused unless a test mocks Popen).
"""
from __future__ import annotations

import base64
import datetime as dt
import importlib.util
import json
import os
import re
import sqlite3
import subprocess
from pathlib import Path

import pytest

from tradingsystem.core.killswitch import read_reason
from tradingsystem.core.settings import INSTANCE_ENV, load_settings
from tradingsystem.core.timeutil import MS_PER_HOUR, MS_PER_MINUTE, iso
from tradingsystem.core.filelock import FileLock, locks_dir
from tradingsystem.execution import drawdown
from tradingsystem.execution.exposure import aggregate, leg
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
    probes = {"battery": None, "commit": None, "gauge": (0, "stub: within budget")}     # Phase 5 machine probes
    m.real_battery = m._battery                        # a test can put the real probe (health_report) back
    monkeypatch.setattr(m, "_battery", lambda: probes["battery"])
    monkeypatch.setattr(m, "_commit", lambda: probes["commit"])
    monkeypatch.setattr(m, "_gauge_level", lambda base, chosen, now: probes["gauge"])
    m.probes = probes
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


def test_a_paper_or_unidentified_account_drop_never_engages_the_global_switch(mon, world):
    seed(world["BTCUSDT"], status=fresh(equity=100.0))                          # paper → paper:BTCUSDT
    seed(world["ETHUSDT"], status=fresh(equity=100.0, mode="mt5"))              # no account_drawdown → mt5:mt5:?
    assert run(mon, world, ["BTCUSDT", "ETHUSDT"]).findings == []
    seed(world["BTCUSDT"], status=fresh(T0 + 15 * MIN, equity=80.0))
    seed(world["ETHUSDT"], status=fresh(T0 + 15 * MIN, equity=80.0, mode="mt5"))
    res = run(mon, world, ["BTCUSDT", "ETHUSDT"], at=T0 + 15 * MIN)
    got = {f.key.rsplit(":", 1)[0]: f for f in res.problems}
    assert set(got) == {"equity_drop:paper:BTCUSDT", "equity_drop:mt5:mt5:?"}
    assert all(f.level == "warn" and f.switch is None and "but no switch" in f.text for f in got.values())
    assert "paper account" in got["equity_drop:paper:BTCUSDT"].text
    assert "not identified" in got["equity_drop:mt5:mt5:?"].text
    assert switches(world["data"]) == set()


def test_an_equity_baseline_older_than_the_runs_only_warns_unless_the_machine_slept(mon, world):
    acct = {"account": "mt5:Demo:7", "peak": 100.0, "drawdown_pct": 0.0, "tripped": None}
    s = world["BTCUSDT"]
    seed(s, status=fresh(equity=100.0, account_drawdown=acct, mode="mt5"))
    run(mon, world, ["BTCUSDT"])
    later = T0 + 2 * MS_PER_HOUR                                     # no run in between (the task was not running)
    seed(s, status=fresh(later, equity=85.0, account_drawdown=acct, mode="mt5"))
    (f,) = run(mon, world, ["BTCUSDT"], at=later).problems
    assert (f.level, f.switch) == ("warn", None) and "2.0 h old" in f.text and switches(world["data"]) == set()
    mon.clock["awake_lag_s"] = 90 * 60.0                             # this time the machine slept 90 of 120 min
    seed(s, status=fresh(later + 2 * MS_PER_HOUR, equity=70.0, account_drawdown=acct, mode="mt5"))
    (f,) = run(mon, world, ["BTCUSDT"], at=later + 2 * MS_PER_HOUR).problems
    assert (f.level, f.switch) == ("critical", "*") and switches(world["data"]) == {"KILL_SWITCH"}


def test_the_sleep_since_the_baseline_counts_when_the_first_run_after_the_resume_has_no_equity(mon, world):
    acct = {"account": "mt5:Demo:8", "peak": 100.0, "drawdown_pct": 0.0, "tripped": None}
    s = world["BTCUSDT"]
    seed(s, status=fresh(equity=100.0, account_drawdown=acct, mode="mt5"))
    run(mon, world, ["BTCUSDT"])
    mon.clock["awake_lag_s"] = 3 * 3600.0                            # the machine sleeps 3 h
    resumed = T0 + 3 * MS_PER_HOUR + 5 * MIN
    seed(s, status=fresh(resumed, equity=None, account_drawdown=acct, mode="mt5"))    # MT5 reconnecting: no equity
    assert run(mon, world, ["BTCUSDT"], at=resumed).problems == []
    base = state(world)["equity"]["mt5:Demo:8"]
    assert (base["equity"], base["slept_ms"]) == (100.0, 3 * MS_PER_HOUR)          # carried, with the sleep since
    seed(s, status=fresh(resumed + 15 * MIN, equity=85.0, account_drawdown=acct, mode="mt5"))
    (f,) = run(mon, world, ["BTCUSDT"], at=resumed + 15 * MIN).problems            # 3.3 h old, 3 h of it asleep
    assert (f.level, f.switch) == ("critical", "*") and switches(world["data"]) == {"KILL_SWITCH"}
    assert state(world)["equity"]["mt5:Demo:8"] == {"equity": 85.0, "ts": resumed + 15 * MIN - 1_000, "boot_ms": 0,
                                                    "seen_ms": resumed + 15 * MIN}


def test_an_awake_hour_without_an_equity_sample_only_warns(mon, world):
    acct = {"account": "mt5:Demo:9", "peak": 100.0, "drawdown_pct": 0.0, "tripped": None}
    s = world["BTCUSDT"]
    seed(s, status=fresh(equity=100.0, account_drawdown=acct, mode="mt5"))
    run(mon, world, ["BTCUSDT"])
    for i in range(1, 6):                                            # the executor loop fails: rows without equity
        seed(s, status=fresh(T0 + i * 15 * MIN, equity=None, account_drawdown=acct, mode="mt5"))
        run(mon, world, ["BTCUSDT"], at=T0 + i * 15 * MIN)
    seed(s, status=fresh(T0 + 90 * MIN, equity=85.0, account_drawdown=acct, mode="mt5"))
    (f,) = run(mon, world, ["BTCUSDT"], at=T0 + 90 * MIN).problems
    assert (f.level, f.switch) == ("warn", None) and switches(world["data"]) == set()
    assert "no switch: the previous sample is 1.5 h old — not a drop between two runs" in f.text


def test_a_reboot_since_the_baseline_counts_as_downtime_until_the_boot(mon, world, monkeypatch):
    boot = {"ms": T0 - 5 * MS_PER_HOUR}
    monkeypatch.setattr(mon, "_clock", lambda: ((mon.clock["now"] - boot["ms"]) / 1000, boot["ms"]))
    acct = {"account": "mt5:Demo:3", "peak": 100.0, "drawdown_pct": 0.0, "tripped": None}
    s, data = world["BTCUSDT"], world["data"]
    seed(s, status=fresh(equity=100.0, account_drawdown=acct, mode="mt5"))
    run(mon, world, ["BTCUSDT"])
    boot["ms"] = T0 + 2 * MS_PER_HOUR                                # rebooted; the user logs on at once
    at = T0 + 2 * MS_PER_HOUR + 10 * MIN
    seed(s, status=fresh(at, equity=85.0, account_drawdown=acct, mode="mt5"))
    (f,) = run(mon, world, ["BTCUSDT"], at=at).problems
    assert (f.level, f.switch) == ("critical", "*") and switches(data) == {"KILL_SWITCH"}
    (data / "KILL_SWITCH").unlink()
    boot["ms"] = T0 + 3 * MS_PER_HOUR                                # an update restart; the user logs on 4 h later
    later = T0 + 7 * MS_PER_HOUR
    seed(s, status=fresh(later, equity=70.0, account_drawdown=acct, mode="mt5"))
    (f,) = run(mon, world, ["BTCUSDT"], at=later).problems           # up 4 h without a sample: only a warning
    assert (f.level, f.switch) == ("warn", None) and switches(data) == set()
    assert "the previous sample is 4.8 h old (0.8 h of it asleep or off)" in f.text


def test_awake_hours_without_an_equity_sample_before_a_reboot_are_not_downtime(mon, world, monkeypatch):
    boot = {"ms": T0 - 5 * MS_PER_HOUR}
    monkeypatch.setattr(mon, "_clock", lambda: ((mon.clock["now"] - boot["ms"]) / 1000, boot["ms"]))
    acct = {"account": "mt5:Demo:4", "peak": 100.0, "drawdown_pct": 0.0, "tripped": None}
    s, data = world["BTCUSDT"], world["data"]
    seed(s, status=fresh(equity=100.0, account_drawdown=acct, mode="mt5"))
    run(mon, world, ["BTCUSDT"])
    for i in range(1, 6):                                            # the executor loop fails: rows without equity
        seed(s, status=fresh(T0 + i * 15 * MIN, equity=None, account_drawdown=acct, mode="mt5"))
        run(mon, world, ["BTCUSDT"], at=T0 + i * 15 * MIN)
    base = state(world)["equity"]["mt5:Demo:4"]
    assert (base["slept_ms"], base["seen_ms"]) == (0, T0 + 75 * MIN)   # carried by runs that saw the machine up
    boot["ms"] = T0 + 80 * MIN                                       # the user reboots to fix MT5
    at = T0 + 90 * MIN
    seed(s, status=fresh(at, equity=85.0, account_drawdown=acct, mode="mt5"))
    (f,) = run(mon, world, ["BTCUSDT"], at=at).problems              # off only from the last run to the boot
    assert (f.level, f.switch) == ("warn", None) and switches(data) == set()
    assert "the previous sample is 1.5 h old (0.1 h of it asleep or off) — not a drop between two runs" in f.text


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
    assert run(mon, world, ["BTCUSDT"], at=T0 + 8 * MS_PER_HOUR).findings == []          # recovered: cooling down
    stale_at(T0 + 9 * MS_PER_HOUR)
    res = run(mon, world, ["BTCUSDT"], at=T0 + 9 * MS_PER_HOUR)                           # back within 6 h: persists
    assert len(mon.sent) == 2 and not res.problems[0].new
    seed(s, status=fresh(T0 + 10 * MS_PER_HOUR))
    run(mon, world, ["BTCUSDT"], at=T0 + 10 * MS_PER_HOUR)
    stale_at(T0 + 16 * MS_PER_HOUR)
    res = run(mon, world, ["BTCUSDT"], at=T0 + 16 * MS_PER_HOUR)                          # 7 h after: a new episode
    assert len(mon.sent) == 3 and res.problems[0].new


def test_a_flapping_finding_is_not_new_each_time_it_returns_within_the_cool_down(mon, world, monkeypatch, tmp_path):
    script = tmp_path / "run_session.py"
    script.write_text("# stand-in\n", encoding="utf-8")
    monkeypatch.setattr(mon, "RUN_SESSION", script)                  # _spawn refuses: no diagnosis may start
    ram = {"mb": 250.0}
    monkeypatch.setattr(mon, "_free_ram_mb", lambda: ram["mb"])
    for i in range(8):                                               # below / above the 300 MB line, every 15 min
        at = T0 + i * 15 * MIN
        ram["mb"] = 250.0 if i % 2 == 0 else 400.0
        seed(world["BTCUSDT"], status=fresh(at))
        res = run(mon, world, ["BTCUSDT"], at=at, diagnose=i > 0)
    assert [k for _, k, _, _ in mon.sent] == ["monitor:ram"]          # sent once, not every 30 min
    ram["mb"] = 250.0
    seed(world["BTCUSDT"], status=fresh(T0 + 8 * 15 * MIN))
    res = run(mon, world, ["BTCUSDT"], at=T0 + 8 * 15 * MIN, diagnose=True)
    assert not res.problems[0].new and res.diagnose["why"] == "no new warning"
    st = json.loads((world["data"] / "shared" / "monitor_state.json").read_text(encoding="utf-8"))
    assert "cleared_ms" not in st["alerts"]["ram"]


def test_a_cleared_alert_that_returns_at_a_higher_level_is_sent(mon, world, monkeypatch):
    shared = world["data"] / "shared"
    shared.mkdir(parents=True)
    (shared / "monitor_state.json").write_text(json.dumps({"alerts": {"ram": {
        "level": "info", "first_ms": T0 - 60 * MIN, "last_ms": T0 - 30 * MIN, "notified_ms": T0 - 60 * MIN,
        "cleared_ms": T0 - 15 * MIN, "title": "Low free RAM"}}}), encoding="utf-8")
    seed(world["BTCUSDT"], status=fresh())
    monkeypatch.setattr(mon, "_free_ram_mb", lambda: 120.0)
    res = run(mon, world, ["BTCUSDT"])
    assert [f.key for f in res.problems] == ["ram"] and res.problems[0].new and len(mon.sent) == 1


# ------------------------------------------------------------------ undelivered notifications are re-armed
def result(toast="pending", telegram="pending", **kw) -> dict:
    """A core.notify result record as the worker leaves it (done once no sink is pending)."""
    return {"toast": toast, "telegram": telegram, "done": "pending" not in (toast, telegram), "deduped": False, **kw}


def test_undelivered_means_no_sink_got_it_through_and_config_log_only_counts_as_delivered(mon):
    lost = mon._undelivered
    assert lost(result()) and lost(result("failed: timed out after 10 s", "failed: ConnectTimeout"))
    assert lost(result("pending", "failed: HTTP 502")) and lost(result("queue full: log only", "queue full: log only"))
    assert lost(result("failed: exit 1", "skipped (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set)"))
    assert not lost(result("shown", "failed: ConnectTimeout")) and not lost(result("pending", "sent"))
    assert not lost(result("off (notify.toast: false)", "skipped (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set)"))
    assert not lost(result(None, None, why="log only (TS_NOTIFY_DISABLE)"))                 # log only by config
    assert not lost(None) and not lost(result("rate limit 20/hour: log only", "rate limit 20/hour: log only"))
    dup = result("deduped (key sent within 30 min)", "deduped (key sent within 30 min)", deduped=True)
    assert not lost(dup)                                     # a re-send has a key of its own (:retry<n>)


def outcomes(mon, monkeypatch, make=result) -> dict:
    """_notify returns ``box["r"]()`` (a notifier result record); ``box["flush"]`` lists the flush budgets."""
    box = {"r": make, "flush": []}
    monkeypatch.setattr(mon, "_notify", lambda s, level, title, text, *, key, pair: (
        mon.sent.append((level, key, title, text)), box["r"]())[1])
    monkeypatch.setattr(mon, "_flush_notify", lambda timeout_s=15.0: box["flush"].append(timeout_s))
    return box


def state(world) -> dict:
    return json.loads((world["data"] / "shared" / "monitor_state.json").read_text(encoding="utf-8"))


def test_an_undelivered_notification_is_re_sent_by_the_next_run_without_detecting_the_event_again(mon, world,
                                                                                                   monkeypatch):
    data = world["data"]
    box = outcomes(mon, monkeypatch)
    shared = data / "shared"
    shared.mkdir(parents=True)
    trip = T0 - 30 * MIN
    (shared / drawdown.FILE).write_text(json.dumps({"mt5:Demo:1": {"peak": 110.0, "tripped_ms": trip}}),
                                        encoding="utf-8")
    acct = {"account": "mt5:Demo:1", "peak": 110.0, "drawdown_pct": 12.7, "tripped": iso(trip)}
    orders = [(T0 - i * MIN, "executor", "order", f"mt5 BTCUSDT {i:08d}: legs 1") for i in range(1, 5)]
    rows = fresh(account_drawdown=acct, mode="mt5")
    rows[1] = ("engine", "live", T0 - 20 * MIN, None, {})
    seed(world["BTCUSDT"], status=rows, events=orders)
    res = run(mon, world, ["BTCUSDT"])                    # the network is black-holed: nothing arrives
    keys = {f.key.split(":")[0]: f for f in res.problems}
    assert set(keys) == {"stale", "burst", "drawdown"}
    assert [lv for lv, *_ in mon.sent] == ["critical", "critical", "warn"]        # criticals are queued first
    assert box["flush"] == [min(15 + 12 * 3, 240)]                               # the budget grows with the queue
    assert not any(f.notified for f in res.problems) and all(f.new for f in res.problems)
    st = state(world)
    assert set(st["pending_notify"]) == {f.key for f in res.problems}
    assert all(e["tries"] == 1 and e["first_ms"] == T0 for e in st["pending_notify"].values())
    # the events themselves are handled: alert notified, one-shot recorded, the burst counted
    assert all(st["alerts"][f.key]["notified_ms"] == T0 and "unsent" not in st["alerts"][f.key] for f in res.problems)
    assert list(st["once"]) == [keys["drawdown"].key] and st["burst_seen"] == {"BTCUSDT:BTCUSDT": T0 - MIN}
    assert switches(data) == {"instances/BTCUSDT/KILL_SWITCH"}
    # the owner reviews the orders and turns the switch off; the next run re-sends the three, now delivered, under
    # retry keys (the failed attempts claimed the plain ones in the notifier's dedupe) — nothing new, no switch
    (data / "instances" / "BTCUSDT" / "KILL_SWITCH").unlink()
    box["r"] = lambda: result("shown", "sent")
    rows = fresh(T0 + 15 * MIN, account_drawdown=acct, mode="mt5")
    rows[1] = ("engine", "live", T0 - 20 * MIN, None, {})
    seed(world["BTCUSDT"], status=rows)
    res = run(mon, world, ["BTCUSDT"], at=T0 + 15 * MIN)
    assert sorted(k for _, k, _, _ in mon.sent[3:]) == sorted(
        f"monitor:{f.key}:retry1" for f in keys.values())
    assert all("re-sent: detected " + iso(T0) in text for *_, text in mon.sent[3:])
    assert [lv for lv, *_ in mon.sent[3:]] == ["critical", "critical", "warn"]
    assert "burst" not in {f.key.split(":")[0] for f in res.findings} and switches(data) == set()
    assert not any(f.new or f.notified for f in res.findings)                    # a re-send is never new
    assert box["flush"] == [51]                                                   # delivered at once: no wait
    st = state(world)
    assert st["pending_notify"] == {} and list(st["once"]) == [keys["drawdown"].key]
    rows = fresh(T0 + 30 * MIN, account_drawdown=acct, mode="mt5")
    rows[1] = ("engine", "live", T0 - 20 * MIN, None, {})
    seed(world["BTCUSDT"], status=rows)
    run(mon, world, ["BTCUSDT"], at=T0 + 30 * MIN)                # delivered: nothing is sent again
    assert len(mon.sent) == 6 and switches(data) == set()


def test_an_undelivered_equity_warning_moves_the_baseline_on(mon, world, monkeypatch):
    box = outcomes(mon, monkeypatch)
    acct = {"account": "mt5:Demo:5", "peak": 100.0, "drawdown_pct": 0.0, "tripped": None}
    s = world["BTCUSDT"]
    seed(s, status=fresh(equity=100.0, account_drawdown=acct, mode="mt5"))
    run(mon, world, ["BTCUSDT"])
    seed(s, status=fresh(T0 + 15 * MIN, equity=94.0, account_drawdown=acct, mode="mt5"))
    (f,) = run(mon, world, ["BTCUSDT"], at=T0 + 15 * MIN).problems              # −6 %: a warning, undelivered
    assert f.level == "warn" and not f.notified and state(world)["equity"]["mt5:Demo:5"]["equity"] == 94.0
    seed(s, status=fresh(T0 + 30 * MIN, equity=89.5, account_drawdown=acct, mode="mt5"))
    res = run(mon, world, ["BTCUSDT"], at=T0 + 30 * MIN)                        # −4.8 % since 94: nothing new
    assert res.problems == [] and switches(world["data"]) == set()
    assert [k for _, k, _, _ in mon.sent] == [f"monitor:{f.key}", f"monitor:{f.key}:retry1"]
    assert box["flush"] == [27, 27]


def test_a_persistently_failing_sink_is_tried_three_times_and_starts_one_diagnosis(mon, world, monkeypatch,
                                                                                   tmp_path, caplog):
    outcomes(mon, monkeypatch, lambda: result("failed: exit 1", "failed: HTTP 401"))
    script = tmp_path / "run_session.py"
    script.write_text("# stand-in\n", encoding="utf-8")
    monkeypatch.setattr(mon, "RUN_SESSION", script)
    calls = []
    monkeypatch.setattr(mon, "_spawn", lambda cmd, cwd: calls.append(cmd) or type("P", (), {"pid": 9})())
    monkeypatch.setattr(mon, "_free_ram_mb", lambda: 120.0)
    world["base"] = world["base"].model_copy(update={"monitor": world["base"].monitor.model_copy(
        update={"vpn_adapter_names": ["Local Area Connection 4"]})})
    monkeypatch.setattr(mon, "_adapters", lambda: {"Local Area Connection 4": True})
    news = []
    for i in range(16):                                              # 4 h, every 15 min
        at = T0 + i * 15 * MIN
        if i == 1:
            monkeypatch.setattr(mon, "_adapters", lambda: {})          # the VPN goes down once
        seed(world["BTCUSDT"], status=fresh(at))
        res = run(mon, world, ["BTCUSDT"], at=at, diagnose=True)
        news.append(sorted(f.key.split(":")[0] for f in res.findings if f.new))
    assert [k for _, k, _, _ in mon.sent if k.startswith("monitor:ram")] == [
        "monitor:ram", "monitor:ram:retry1", "monitor:ram:retry2"]
    vpn = [k for _, k, _, _ in mon.sent if k.startswith("monitor:vpn")]
    assert len(vpn) == 3 and len({k.split(":retry")[0] for k in vpn}) == 1    # one VPN event, not one per run
    assert news[0] == ["ram"] and news[1] == ["vpn"] and not any(news[2:])
    assert len(calls) == 1                                          # one diagnosis, not one every 3 h
    assert state(world)["pending_notify"] == {} and state(world)["vpn"] == {"Local Area Connection 4": False}
    assert "dropped after 3 attempts" in caplog.text
    seed(world["BTCUSDT"], status=fresh(T0 + 6 * MS_PER_HOUR))
    run(mon, world, ["BTCUSDT"], at=T0 + 6 * MS_PER_HOUR)           # then the normal 6-h reminder
    assert [k for _, k, _, _ in mon.sent][-1] == "monitor:ram"


def test_a_pending_notification_is_dropped_after_its_reminder_interval_or_when_sent_afresh(mon, world, monkeypatch):
    box = outcomes(mon, monkeypatch)
    rows = fresh()
    rows[2] = ("executor", "error", T0 - 1_000, "(-10004, 'No IPC connection')",
               {"mode": "mt5", "failing_since": iso(T0 - 6 * MIN)})
    seed(world["BTCUSDT"], status=rows)
    (f,) = run(mon, world, ["BTCUSDT"]).problems                     # critical, undelivered
    assert f.level == "critical" and list(state(world)["pending_notify"]) == [f.key]
    seed(world["BTCUSDT"], status=fresh(T0 + 70 * MIN))              # recovered; the next run comes 70 min later
    run(mon, world, ["BTCUSDT"], at=T0 + 70 * MIN)
    assert len(mon.sent) == 1 and state(world)["pending_notify"] == {}    # older than the critical reminder: dropped
    monkeypatch.setattr(mon, "_free_ram_mb", lambda: 120.0)
    seed(world["BTCUSDT"], status=fresh(T0 + 80 * MIN))
    run(mon, world, ["BTCUSDT"], at=T0 + 80 * MIN)                   # a RAM warning, undelivered
    shared = world["data"] / "shared"
    st = state(world)
    st["alerts"]["ram"]["level"] = "info"                            # (as if it had been info: now its level rose)
    (shared / "monitor_state.json").write_text(json.dumps(st), encoding="utf-8")
    box["r"] = lambda: result("shown", "sent")
    seed(world["BTCUSDT"], status=fresh(T0 + 95 * MIN))
    run(mon, world, ["BTCUSDT"], at=T0 + 95 * MIN)                   # sent afresh: the pending one is not re-sent
    assert [k for _, k, _, _ in mon.sent[1:]] == ["monitor:ram", "monitor:ram"] and state(world)["pending_notify"] == {}


def test_a_notification_that_arrives_during_the_last_flush_is_not_sent_twice(mon, world, monkeypatch):
    records = []

    def notify(s, level, title, text, *, key, pair):               # noqa: ANN001
        mon.sent.append((level, key, title, text))
        records.append(result())                                     # in flight when the run's own wait ends
        return records[-1]

    monkeypatch.setattr(mon, "_notify", notify)
    order = []
    monkeypatch.setattr(mon, "_flush_notify", lambda timeout_s=15.0: order.append(("flush", timeout_s)))
    stale_engine(world["BTCUSDT"])
    m = mon.Monitor(world["base"], [world["BTCUSDT"]], now=T0)
    res = m.run()
    assert list(state(world)["pending_notify"]) == ["stale:BTCUSDT:engine"] and not res.problems[0].notified
    records[0].update(toast="shown", telegram="skipped (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set)", done=True)
    assert m.reconcile() and res.problems[0].notified
    st = state(world)
    assert st["pending_notify"] == {} and st["run"]["findings"][0]["notified"]
    assert not m.reconcile()                                         # nothing more to do
    seed(world["BTCUSDT"], status=fresh(T0 + 15 * MIN))
    run(mon, world, ["BTCUSDT"], at=T0 + 15 * MIN)
    assert len(mon.sent) == 1                                        # not sent a second time
    # main() judges delivery after its last flush
    monkeypatch.setattr(mon, "load_settings", lambda **kw: world["base"])
    monkeypatch.setattr(mon, "setup_logging", lambda *a, **kw: None)
    monkeypatch.setattr(mon.hr, "systems", lambda ns: ([world["BTCUSDT"]], []))
    monkeypatch.setattr(mon, "now_ms", lambda: T0 + 30 * MIN)
    monkeypatch.setattr(mon.Monitor, "reconcile", lambda self: order.append(("reconcile", None)) or False)
    order.clear()
    assert mon.main(["--quiet", "--no-diagnose"]) in (0, 1)
    assert order[-2:] == [("flush", 15.0), ("reconcile", None)]
    order.clear()
    mon.main(["--quiet", "--dry-run"])
    assert order == []                                              # a dry run neither flushes nor reconciles


def test_a_notification_delivered_by_one_sink_is_not_re_sent(mon, world, monkeypatch):
    outcomes(mon, monkeypatch, lambda: result("shown", "failed: ConnectTimeout"))
    stale_engine(world["BTCUSDT"])
    run(mon, world, ["BTCUSDT"])
    st = state(world)
    assert st["alerts"]["stale:BTCUSDT:engine"]["notified_ms"] == T0 and st["pending_notify"] == {}


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


def test_the_state_is_saved_before_the_diagnosis_starts(mon, world, monkeypatch, tmp_path):
    script = tmp_path / "run_session.py"
    script.write_text("# stand-in\n", encoding="utf-8")
    monkeypatch.setattr(mon, "RUN_SESSION", script)
    path = world["data"] / "shared" / "monitor_state.json"
    seen = []

    def spawn(cmd, cwd):                                             # noqa: ANN001
        seen.append(json.loads(path.read_text(encoding="utf-8")))
        return type("P", (), {"pid": 77})()

    monkeypatch.setattr(mon, "_spawn", spawn)
    stale_engine(world["BTCUSDT"])
    res = run(mon, world, ["BTCUSDT"], diagnose=True)
    assert res.diagnose["launched"] and seen[0]["last_diagnose_ms"] == T0
    assert seen[0]["alerts"]["stale:BTCUSDT:engine"]["notified_ms"] == T0
    assert json.loads(path.read_text(encoding="utf-8"))["diagnose"]["pid"] == 77


def test_a_state_that_cannot_be_saved_starts_no_diagnosis_and_sends_a_critical(mon, world, monkeypatch, tmp_path):
    script = tmp_path / "run_session.py"
    script.write_text("# stand-in\n", encoding="utf-8")
    monkeypatch.setattr(mon, "RUN_SESSION", script)                  # _spawn refuses: it must not be called

    def full(path, doc):                                             # noqa: ANN001
        raise PermissionError(13, "read-only file")

    monkeypatch.setattr(mon, "save_state", full)
    stale_engine(world["BTCUSDT"])
    res = run(mon, world, ["BTCUSDT"], diagnose=True)
    assert res.diagnose == {"launched": False, "why": "state not persisted"}
    assert res.state_error and res.exit_code == 1
    crit = [x for x in mon.sent if x[1] == "monitor:state_error"]
    assert [x[0] for x in crit] == ["critical"] and "could not be written" in crit[0][3]


def test_no_diagnosis_while_a_review_session_holds_the_lock_and_the_next_run_retries(mon, world, monkeypatch,
                                                                                     tmp_path):
    script = tmp_path / "run_session.py"
    script.write_text("# stand-in\n", encoding="utf-8")
    monkeypatch.setattr(mon, "RUN_SESSION", script)
    review = FileLock(locks_dir(world["base"]) / mon.SESSION_LOCK)   # the daily review runs
    assert review.acquire(timeout=0)
    try:
        stale_engine(world["BTCUSDT"])
        res = run(mon, world, ["BTCUSDT"], diagnose=True)            # _spawn refuses: it must not be called
    finally:
        review.release()
    assert res.diagnose == {"launched": False, "why": "a review session is running"}
    st = json.loads((world["data"] / "shared" / "monitor_state.json").read_text(encoding="utf-8"))
    assert st["last_diagnose_ms"] is None and st["diagnose_wanted"]["findings"] == ["stale:BTCUSDT:engine"]
    calls = []
    monkeypatch.setattr(mon, "_spawn", lambda cmd, cwd: calls.append(cmd) or type("P", (), {"pid": 5})())
    stale_engine(world["BTCUSDT"], T0 + 15 * MIN)                    # the same warning, no longer new
    res = run(mon, world, ["BTCUSDT"], at=T0 + 15 * MIN, diagnose=True)
    assert res.diagnose["launched"] and "stale heartbeat" in calls[0][5] and not res.problems[0].new
    st = json.loads((world["data"] / "shared" / "monitor_state.json").read_text(encoding="utf-8"))
    assert st["last_diagnose_ms"] == T0 + 15 * MIN and "diagnose_wanted" not in st


def test_the_session_lock_is_the_one_run_session_takes(mon):
    text = (ROOT / "tools" / "operator" / "run_session.py").read_text(encoding="utf-8")
    assert f'LOCK_NAME = "{mon.SESSION_LOCK}"' in text


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


def test_a_second_leg_without_sl_is_a_position_without_sl(mon, world):
    def legs(decision: str, sl2):                                    # noqa: ANN001 — TP1 / TP2 legs of one decision
        return [leg(pair="BTCUSDT", decision=decision, kind="position", side="BUY", order_type="MARKET",
                    volume=0.01, price=60000.0, sl=59000.0, tp=61000.0),
                leg(pair="BTCUSDT", decision=decision, kind="position", side="BUY", order_type="MARKET",
                    volume=0.01, price=60000.0, sl=sl2, tp=62000.0)]
    rows = aggregate(legs("a" * 20, None) + legs("b" * 20, 59000.0))
    by = {r["decision"]: r for r in rows}
    assert by["aaaaaaaa"]["sl"] == 59000.0 and by["aaaaaaaa"]["sl_missing"] is True   # 'sl' stays the first leg's
    assert by["bbbbbbbb"]["sl_missing"] is False
    seed(world["BTCUSDT"], status=fresh(exposure=rows))
    (f,) = run(mon, world, ["BTCUSDT"]).problems
    assert f.key == "nosl:BTCUSDT:BTCUSDT:aaaaaaaa" and f.level == "critical" and "a leg without a stop-loss" in f.text


def test_a_stopped_executors_row_from_an_earlier_day_never_engages_the_daily_loss_switch(mon, world):
    """A pair whose executor stopped yesterday keeps yesterday's today_pnl_pct in its status row: at a later UTC day
    that is not today's loss (the stale heartbeat is reported instead)."""
    yesterday = T0 - 13 * 60 * MIN                                   # 2026-09-22 23:00 UTC
    seed(world["BTCUSDT"], status=fresh(yesterday, today_pnl_pct=-10.4))
    res = run(mon, world, ["BTCUSDT"])
    assert not [f for f in res.findings if f.key.startswith("dayloss")]
    assert switches(world["data"]) == set()
    seed(world["BTCUSDT"], status=fresh(T0, today_pnl_pct=-10.4))    # the same result written today: engaged
    assert switches(world["data"]) == set() and "dayloss:BTCUSDT:2026-09-23" in {
        f.key for f in run(mon, world, ["BTCUSDT"], at=T0 + MIN).findings}


def test_the_daily_loss_warning_needs_a_band_below_the_limit(mon, world):
    s = world["ETHUSDT"]
    s = s.model_copy(update={"risk": s.risk.model_copy(update={"max_daily_loss_pct": 2.0}),
                             "monitor": s.monitor.model_copy(update={"daily_loss_warn_margin_pct": 2.0})})
    world["ETHUSDT"] = s                                              # the code defaults: no band at all
    seed(s, status=fresh(today_pnl_pct=-0.01))
    assert run(mon, world, ["ETHUSDT"]).problems == []               # a floating −0.01 % is not "near the limit"
    seed(s, status=fresh(T0 + 15 * MIN, today_pnl_pct=-1.7))
    (f,) = run(mon, world, ["ETHUSDT"], at=T0 + 15 * MIN).problems
    assert f.level == "warn" and "near the limit" in f.title and "warning at -1.60 %" in f.text


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
    assert res.problems == [] and "stopped by the user" in res.skipped[0] and "ETHUSDT" in res.held[0]
    res = run(mon, world, ["XAUUSD"], notes=[note], at=T0 + 15 * MIN)          # still down on the next run
    assert [f.title for f in res.problems] == ["System not running"]


def test_a_no_supervisor_note_waits_for_the_second_run_after_a_logon_long_after_the_boot(mon, world, monkeypatch,
                                                                                         tmp_path):
    script = tmp_path / "run_session.py"
    script.write_text("# stand-in\n", encoding="utf-8")
    monkeypatch.setattr(mon, "RUN_SESSION", script)                  # _spawn refuses: no diagnosis may start
    boot = {"ms": T0 - 20 * MS_PER_HOUR}
    monkeypatch.setattr(mon, "_clock", lambda: ((mon.clock["now"] - boot["ms"]) / 1000, boot["ms"]))
    both = ["BTCUSDT", "ETHUSDT"]

    def up(p, at):                                                   # healthy rows and quotes at ``at``
        seed(world[p], status=fresh(at), quotes=[(f"binance_spot:{p}", at), (f"mt5:{p[:3]}USD@", at)])

    before = T0 - 5 * MS_PER_HOUR - 10 * MIN                         # the last run before the restart: both healthy
    for p in both:
        up(p, before)
    assert run(mon, world, both, at=before).findings == []
    boot["ms"] = T0 - 5 * MS_PER_HOUR                                # an update restart at night, logon at T0
    up("BTCUSDT", T0)
    # production shape: ETHUSDT (own app.db) is chosen with its rows, quotes and an outage from before the restart —
    # its executor last saw the MT5 terminal close first (an IPC error since then)
    gone = T0 - 5 * MS_PER_HOUR - MIN
    rows = [r if r[0] != "executor" else ("executor", "error", gone, "(-10004, 'No IPC connection')",
                                          {"mode": "paper", "failing_since": iso(gone - 2 * MIN)})
            for r in fresh(gone)]
    seed(world["ETHUSDT"], status=rows, events=[(gone - MIN, "mt5", "disconnect", "terminal closed")],
         quotes=[("binance_spot:ETHUSDT", gone), ("mt5:ETHUSD@", gone)])
    down = "!! ETHUSDT: its system should run (own app.db, not stopped by the user) but no supervisor runs"
    res = run(mon, world, both, at=T0, notes=[down], diagnose=True)
    assert res.problems == [] and mon.sent == [] and res.diagnose["why"] == "no new warning"
    assert res.held[0].startswith("system not running ETHUSDT: first seen on this run")
    assert {h.split(" ")[1] for h in res.held[1:]} == {"stale", "outage", "quote"}      # the same logon: they wait too
    assert all(h.startswith("ETHUSDT ") and "first seen on this run" in h for h in res.held[1:])
    up("BTCUSDT", T0 + 2 * MIN)
    up("ETHUSDT", T0 + 2 * MIN)                                      # started 90-150 s after the logon
    seed(world["ETHUSDT"], events=[(T0 + 2 * MIN, "mt5", "resumed", "connected")])
    assert run(mon, world, both, at=T0 + 2 * MIN).findings == []
    up("ETHUSDT", T0 + 3 * MIN)                                      # … and dies a minute later
    up("BTCUSDT", T0 + 15 * MIN)
    res = run(mon, world, both, at=T0 + 15 * MIN, notes=[down])      # down again: first seen again
    assert res.problems == [] and {h.split(" ")[1] for h in res.held} == {"not", "quote"}
    up("BTCUSDT", T0 + 30 * MIN)
    res = run(mon, world, both, at=T0 + 30 * MIN, notes=[down])      # really down: reported 15 min later, with its
    assert sorted(f.title for f in res.problems) == ["ETHUSDT: stale heartbeat", "ETHUSDT: stale quotes",
                                                     "System not running"]              # stale heartbeats and quotes
    assert all(f.new for f in res.problems) and not res.held


def test_a_no_supervisor_note_waits_for_the_second_run_while_the_machine_settles(mon, world, monkeypatch, tmp_path):
    script = tmp_path / "run_session.py"
    script.write_text("# stand-in\n", encoding="utf-8")
    monkeypatch.setattr(mon, "RUN_SESSION", script)                  # _spawn refuses: no diagnosis may start
    monkeypatch.setattr(mon, "_clock", lambda: (mon.clock["now"] / 1000, T0 - 2 * MIN))     # booted 2 min ago
    seed(world["BTCUSDT"], status=fresh())
    down = "!! ETHUSDT: its system should run (own app.db, not stopped by the user) but no supervisor runs"
    res = run(mon, world, ["BTCUSDT"], notes=[down], diagnose=True)   # the keep-alive starts it 90-150 s after logon
    assert res.problems == [] and mon.sent == [] and res.diagnose["why"] == "no new warning"
    assert res.held == ["system not running ETHUSDT: booted 2 min ago — checked again on the next run"]
    other = "!! supervisor running for DOGEUSDT, which is not in config 'instances:' — not reported"
    seed(world["BTCUSDT"], status=fresh(T0 + 6 * MIN))
    res = run(mon, world, ["BTCUSDT"], at=T0 + 6 * MIN, notes=[down, other])      # still down 6 min later
    assert sorted(f.title for f in res.problems) == ["System check", "System not running"] and not res.held
    seed(world["BTCUSDT"], status=fresh(T0 + 7 * MIN))
    res = run(mon, world, ["BTCUSDT"], at=T0 + 7 * MIN, notes=[other.replace("DOGE", "SOL")])
    assert [f.title for f in res.problems] == ["System check"]       # any other note: at once, even while settling


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


def test_a_config_that_cannot_be_read_exits_3_with_a_log_line_and_a_toast(mon, monkeypatch, tmp_path, capsys):
    def bad(**kw):                                                   # noqa: ANN003
        raise ValueError("1 validation error for Settings\nmonitor\n  equity_drop_warn_pct must be below ...")

    monkeypatch.setattr(mon, "load_settings", bad)
    monkeypatch.setattr(mon, "setup_logging", lambda *a, **kw: pytest.fail("no Settings, no normal log"))
    log_file = tmp_path / "logs" / "monitor-config-error.log"
    monkeypatch.setattr(mon, "CONFIG_ERROR_LOG", log_file)
    toasts = []
    monkeypatch.setattr(mon.subprocess, "run", lambda cmd, **kw: toasts.append((cmd, kw)))
    assert mon.main(["--quiet"]) == 3                                # TS_NOTIFY_DISABLE (the suite): no toast
    assert toasts == [] and capsys.readouterr().err == ""
    (line,) = log_file.read_text(encoding="utf-8").splitlines()
    assert "monitor cannot read its config: ValueError: 1 validation error for Settings monitor" in line
    assert line[:4].isdigit() and line[10] == "T"                    # timestamped (ISO UTC)
    monkeypatch.delenv("TS_NOTIFY_DISABLE", raising=False)
    assert mon.main([]) == 3
    (cmd, kw), = toasts
    assert cmd[cmd.index("-Level") + 1] == "critical" and kw["timeout"] == 15.0
    title = cmd[cmd.index("-TitleB64") + 1]
    assert base64.b64decode(title).decode("utf-8") == "Monitor cannot read its config"
    assert "!! " in capsys.readouterr().err and len(log_file.read_text(encoding="utf-8").splitlines()) == 2

    def hang(cmd, **kw):                                             # noqa: ANN001, ANN003
        raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))

    monkeypatch.setattr(mon.subprocess, "run", hang)
    monkeypatch.setattr(mon.sys, "stderr", None)                     # pythonw
    assert mon.main([]) == 3                                         # a toast that hangs never raises


# ================================================================== Phase 5 A4 (and the monitor part of A3)
REAL = ROOT / "tests" / "fixtures" / "real"
GB = 2**30


def monitor_cfg(world, **kw) -> None:
    world["base"] = world["base"].model_copy(update={"monitor": world["base"].monitor.model_copy(update=kw)})


def beat(world, at: int, pair: str = "BTCUSDT") -> None:
    """Healthy rows of ``pair`` as of ``at`` (nothing but the rule under test fires)."""
    seed(world[pair], status=fresh(at))


# ------------------------------------------------------------------ battery
def test_on_battery_longer_than_on_battery_warn_min_warns_that_the_charger_is_unplugged(mon, world):
    beat(world, T0)
    mon.probes["battery"] = (96.0, False)                             # unplugged, nearly full: time decides
    first = run(mon, world, ["BTCUSDT"])
    assert first.problems == [] and first.held[0].startswith("machine on battery (96 %)")
    assert state(world)["battery"]["on_battery_since"] == T0          # psutil cannot say since when: tracked here
    beat(world, T0 + 15 * MIN)
    mon.probes["battery"] = (90.0, False)
    (f,) = run(mon, world, ["BTCUSDT"], at=T0 + 15 * MIN).problems
    assert (f.level, f.key, f.title) == ("warn", f"battery:{T0}", "Running on battery")
    assert "the charger is unplugged: on battery for 15 min at least, 90 % left" in f.text
    assert [x[1] for x in mon.sent] == [f"monitor:battery:{T0}"]
    beat(world, T0 + 30 * MIN)
    mon.probes["battery"] = (91.0, True)                              # plugged in again: nothing
    assert run(mon, world, ["BTCUSDT"], at=T0 + 30 * MIN).findings == []
    assert state(world)["battery"]["on_battery_since"] is None
    later = T0 + 90 * MIN                                              # unplugged again: a new episode, told again
    mon.probes["battery"] = (88.0, False)
    beat(world, later)
    assert run(mon, world, ["BTCUSDT"], at=later).problems == []
    beat(world, later + 15 * MIN)
    (f,) = run(mon, world, ["BTCUSDT"], at=later + 15 * MIN).problems
    assert f.key == f"battery:{later}" and f.new and mon.sent[-1][1] == f"monitor:battery:{later}"


def test_a_low_battery_warns_at_once_and_below_the_critical_level_says_plug_it_in_now(mon, world):
    beat(world, T0)
    mon.probes["battery"] = (27.0, False)                             # below battery_warn_pct 30: the first run
    (f,) = run(mon, world, ["BTCUSDT"]).problems
    assert f.level == "warn" and "on battery (first seen on this run), 27 % left" in f.text
    beat(world, T0 + 15 * MIN)
    mon.probes["battery"] = (14.0, False)                             # below battery_critical_pct 15: escalates
    res = run(mon, world, ["BTCUSDT"], at=T0 + 15 * MIN)
    (f,) = res.problems
    assert (f.level, f.key, f.title) == ("critical", f"battery:{T0}", "Battery critical") and f.new
    assert "plug the charger in now: at 5 % Windows hibernates and every system stops" in f.text
    assert [x[:2] for x in mon.sent] == [("warn", f"monitor:battery:{T0}"), ("critical", f"monitor:battery:{T0}")]
    assert res.exit_code == 1


def test_plugged_in_an_unknown_power_source_or_no_battery_is_nothing(mon, world, monkeypatch):
    """Windows' own readings (GetSystemPowerStatus: ACLineStatus, BatteryFlag, BatteryLifePercent) through the real
    probe the monitor uses (health_report.battery): charging from 9 %, the same with an unknown AC line (255 — psutil
    turns it into "unplugged", which at 9 % would be a critical at once), a desktop (no system battery)."""
    monkeypatch.setattr(mon, "_battery", mon.real_battery)
    for i, st in enumerate([(1, 8 | 4, 9), (255, 8 | 4, 9), (1, 128, 255)]):
        at = T0 + i * 15 * MIN
        monkeypatch.setattr(mon.hr, "_power_status", lambda st=st: st)
        beat(world, at)
        res = run(mon, world, ["BTCUSDT"], at=at)
        assert res.findings == [] and res.held == []
        if i == 1:
            b = state(world)["battery"]
            assert (b["percent"], b["plugged"], b["on_battery_since"]) == (9.0, None, None)
    assert state(world)["battery"] == {"present": False, "ts": T0 + 30 * MIN}
    for i, b in enumerate([(9.0, True), (9.0, None), None]):          # the probe's three shapes, stubbed
        at = T0 + (3 + i) * 15 * MIN
        mon.probes["battery"] = b
        monkeypatch.setattr(mon, "_battery", lambda: mon.probes["battery"])
        beat(world, at)
        res = run(mon, world, ["BTCUSDT"], at=at)
        assert res.findings == [] and res.held == []


def test_a_real_unplugged_reading_still_warns_through_the_power_status_probe(mon, world, monkeypatch):
    """ACLineStatus 0 (offline) at 27 % (BatteryFlag 2 = low): on battery below battery_warn_pct, a warning at once."""
    monkeypatch.setattr(mon, "_battery", mon.real_battery)
    monkeypatch.setattr(mon.hr, "_power_status", lambda: (0, 2, 27))
    beat(world, T0)
    (f,) = run(mon, world, ["BTCUSDT"]).problems
    assert (f.level, f.key, f.title) == ("warn", f"battery:{T0}", "Running on battery")
    assert "on battery (first seen on this run), 27 % left" in f.text


# ------------------------------------------------------------------ docs/ops_windows.md: the Phase 5 switches, the way back
OPS = ROOT / "docs" / "ops_windows.md"
PHASE5_KEYS = {"ai": ["skip_closed_market", "transient_retry_s", "quota_reserve_share", "quota_reserve_hours_utc"],
               "monitor": ["battery_warn_pct", "battery_critical_pct", "on_battery_warn_min", "commit_warn_pct",
                           "recorder_stall_min", "diagnose_max_per_day", "diagnose_max_gauge_level"],
               "storage": ["cold_archive_min_free_gb"]}
PHASE5_SECTIONS = ["backup", "evaluation"]


def test_the_runbooks_battery_switch_turns_the_whole_battery_rule_off(mon, world):
    """§8 'a monitor rule is noisy': the battery switch as written (both levels at 0 and a large
    on_battery_warn_min — on_battery_warn_min has ge=1, so it cannot be 0) — no warning after 2 h on battery at 12 %."""
    (row,) = [ln for ln in OPS.read_text(encoding="utf-8").splitlines()
              if ln.startswith("| Phase 5: a monitor rule is noisy |")]
    switch = {k: int(v) for k, v in re.findall(r"`(battery_warn_pct|battery_critical_pct|on_battery_warn_min): (\d+)`",
                                               row)}
    assert switch == {"battery_warn_pct": 0, "battery_critical_pct": 0, "on_battery_warn_min": 1440}
    monitor_cfg(world, **switch)
    mon.probes["battery"] = (12.0, False)
    for at in (T0, T0 + 60 * MIN, T0 + 120 * MIN):
        beat(world, at)
        assert run(mon, world, ["BTCUSDT"], at=at).problems == []
    assert state(world)["battery"]["on_battery_since"] == T0              # still tracked, only not warned about


def test_going_back_before_phase5_names_every_phase5_key_and_puts_diagnose_off_back():
    """Phase 4's settings (extra='forbid') refuse every Phase 5 key, also inside ai:/monitor:/storage:, so §9.7 names
    each one (also under instances.*.overrides), puts diagnose_enabled: false back (Phase 4 has no diagnosis budget)
    and has the older code's `config` check run before restart_all.bat."""
    s = load_settings()
    for section, keys in PHASE5_KEYS.items():
        assert set(keys) <= set(type(getattr(s, section)).model_fields), section   # real keys of this code
    assert all(sec in type(s).model_fields for sec in PHASE5_SECTIONS)
    text = OPS.read_text(encoding="utf-8")
    back = " ".join(text.split("### 9.7 Going back to the code before Phase 5", 1)[1].split("\n## ", 1)[0].split())
    for key in [k for keys in PHASE5_KEYS.values() for k in keys]:
        assert f"`{key}`" in back, key
    assert all(f"`{sec}:`" in back for sec in PHASE5_SECTIONS)
    assert "`instances.*.overrides`" in back
    assert "put `diagnose_enabled: false` back into the `monitor:` block" in back
    # the config is cleaned while the Phase 5 code still runs, then stop → check out → check → start (a leftover key
    # would stop every service the supervisor restarts after the checkout); the lock file leaves the checkout
    assert "`.venv\\Scripts\\python.exe -m tradingsystem config` must succeed with the older code" in back
    assert back.index("install_operator_tasks.bat -Uninstall") < back.index("Clean `config\\config.local.yaml` FIRST") \
        < back.index("scripts\\stop_all.bat") < back.index("-m tradingsystem config") < back.index("start_all.bat")
    assert "Delete `backups\\backup.lock`" in back


# ------------------------------------------------------------------ commit charge
def test_a_commit_charge_above_commit_warn_pct_warns(mon, world):
    beat(world, T0)
    mon.probes["commit"] = (int(17 * GB), int(20 * GB))               # 85 %: at the limit, not above it
    assert run(mon, world, ["BTCUSDT"]).findings == []
    beat(world, T0 + 15 * MIN)
    mon.probes["commit"] = (int(18.4 * GB), int(20 * GB))             # 92 %
    (f,) = run(mon, world, ["BTCUSDT"], at=T0 + 15 * MIN).problems
    assert (f.level, f.key, f.title) == ("warn", "commit", "High memory commit")
    assert "18.4 of 20.0 GB committed (92 %, warning above 85 %)" in f.text


# ------------------------------------------------------------------ the P1.12 price recorder (A3)
RECORDER_UPD = int(dt.datetime(2026, 9, 28, 6, 5, 34, tzinfo=dt.timezone.utc).timestamp() * 1000)   # the fixture


def recorder(world) -> Path:
    """The real production status.json (fixture: pid 10892, updated 2026-09-28T06:05:34Z) under the tmp data root."""
    d = world["data"] / "research" / "price_matching"
    d.mkdir(parents=True, exist_ok=True)
    (d / "status.json").write_bytes((REAL / "recorder_status.json").read_bytes())
    return d


def test_a_stalled_recorder_warns_unless_the_owner_stopped_it_or_the_rule_is_off(mon, world, monkeypatch):
    d = recorder(world)
    monkeypatch.setattr(mon.hr, "_recorder_alive", lambda pid: False)
    at = RECORDER_UPD + 20 * MIN
    beat(world, at)
    assert run(mon, world, ["BTCUSDT"], at=at).findings == []          # 20 min: within recorder_stall_min 30
    at = RECORDER_UPD + 45 * MIN
    beat(world, at)
    (f,) = run(mon, world, ["BTCUSDT"], at=at).problems
    assert (f.level, f.key, f.title) == ("warn", "recorder", "Price recorder stalled")
    assert "last flush 45 min ago (2026-09-28T06:05:34.000Z, limit 30 min); pid 10892 not running" in f.text
    assert "TradingSystemOps-Recorder" in f.text
    monkeypatch.setattr(mon.hr, "_recorder_alive", lambda pid: True)
    at += 15 * MIN
    beat(world, at)
    (f,) = run(mon, world, ["BTCUSDT"], at=at).problems
    assert "alive but not flushing (hung)" in f.text
    # a hung recorder reads STOP only after a completed flush: the advice ends the process, never "create STOP"
    assert "end it with taskkill /PID 10892 /T /F" in f.text and "starts a fresh one within 5 min" in f.text
    assert "STOP" not in f.text
    (d / "STOP").write_text("", encoding="utf-8")                      # the owner stopped it: nothing
    at += 15 * MIN
    beat(world, at)
    assert run(mon, world, ["BTCUSDT"], at=at).findings == []
    (d / "STOP").unlink()
    monitor_cfg(world, recorder_stall_min=0)                           # 0 = not watched
    at += 15 * MIN
    beat(world, at)
    assert run(mon, world, ["BTCUSDT"], at=at).findings == []
    monitor_cfg(world, recorder_stall_min=30)
    (d / "status.json").unlink()                                       # never ran here: nothing
    at += 15 * MIN
    beat(world, at)
    assert run(mon, world, ["BTCUSDT"], at=at).findings == []


def test_after_a_sleep_a_stalled_recorder_needs_two_runs(mon, world, monkeypatch):
    """The recorder dies at every suspend and its keep-alive needs up to 10 min to bring back a flush: the run right
    after a resume holds it (the two-run rule), the next one reports it if it still has not flushed."""
    recorder(world)
    monkeypatch.setattr(mon.hr, "_recorder_alive", lambda pid: False)
    at = RECORDER_UPD + 5 * MIN
    beat(world, at)
    assert run(mon, world, ["BTCUSDT"], at=at).findings == []
    at += 60 * MIN
    mon.clock["awake_lag_s"] = 50 * 60.0                               # asleep 50 of those 60 min
    beat(world, at)
    res = run(mon, world, ["BTCUSDT"], at=at)
    assert res.problems == [] and any(h.startswith("machine recorder stall: the machine slept") for h in res.held)
    at += 15 * MIN
    beat(world, at)
    assert [f.key for f in run(mon, world, ["BTCUSDT"], at=at).problems] == ["recorder"]


# ------------------------------------------------------------------ the diagnosis budget
def spawned(mon, monkeypatch, tmp_path) -> list:
    script = tmp_path / "run_session.py"
    script.write_text("# stand-in\n", encoding="utf-8")
    monkeypatch.setattr(mon, "RUN_SESSION", script)
    calls: list = []
    monkeypatch.setattr(mon, "_spawn", lambda cmd, cwd: calls.append(cmd) or type("P", (), {"pid": 9})())
    return calls


def stale_at(world, pair: str, at: int) -> None:
    """``pair``'s engine silent for 20 min as of ``at``: a new warning (stale:<pair>:engine)."""
    rows = fresh(at)
    rows[1] = ("engine", "live", at - 20 * MIN, None, {})
    seed(world[pair], status=rows)


def test_at_most_diagnose_max_per_day_diagnoses_start_per_utc_day(mon, world, monkeypatch, tmp_path):
    calls = spawned(mon, monkeypatch, tmp_path)
    monitor_cfg(world, diagnose_every_hours=0.5)                       # the interval is not what limits here
    day = int(dt.datetime(2026, 9, 23, 1, 0, tzinfo=dt.timezone.utc).timestamp() * 1000)
    res = None
    for i, pair in enumerate(("BTCUSDT", "ETHUSDT", "XAUUSD")):        # a new warning every hour
        stale_at(world, pair, day + i * MS_PER_HOUR)
        res = run(mon, world, [pair], at=day + i * MS_PER_HOUR, diagnose=True)
    assert len(calls) == 2 and not res.diagnose["launched"]
    assert res.diagnose["why"] == ("daily budget used: 2 diagnosis session(s) started today (UTC), "
                                   "monitor.diagnose_max_per_day 2")
    assert "diagnosis: not started — daily budget used: 2" in mon.render(res)
    st = state(world)
    assert st["diagnose_launches"] == [day, day + MS_PER_HOUR] and "diagnose_wanted" not in st
    nxt = int(dt.datetime(2026, 9, 24, 0, 5, tzinfo=dt.timezone.utc).timestamp() * 1000)    # a new UTC day
    stale_at(world, "ETHUSDT", nxt)
    assert run(mon, world, ["ETHUSDT"], at=nxt, diagnose=True).diagnose["launched"] and len(calls) == 3
    assert state(world)["diagnose_launches"] == [day, day + MS_PER_HOUR, nxt]


def test_a_state_from_before_the_budget_counts_its_last_diagnosis_and_0_means_none(mon, world, monkeypatch, tmp_path):
    calls = spawned(mon, monkeypatch, tmp_path)
    monitor_cfg(world, diagnose_every_hours=0.5, diagnose_max_per_day=1)
    path = world["data"] / "shared" / "monitor_state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "last_diagnose_ms": T0 - MS_PER_HOUR}), encoding="utf-8")
    stale_engine(world["BTCUSDT"])
    res = run(mon, world, ["BTCUSDT"], diagnose=True)
    assert calls == [] and res.diagnose["why"].startswith("daily budget used: 1 diagnosis session(s)")
    monitor_cfg(world, diagnose_max_per_day=0)
    stale_at(world, "ETHUSDT", T0 + 15 * MIN)
    res = run(mon, world, ["ETHUSDT"], at=T0 + 15 * MIN, diagnose=True)
    assert calls == [] and "monitor.diagnose_max_per_day 0" in res.diagnose["why"]


def test_no_diagnosis_while_the_usage_gauge_is_above_its_level_or_unreadable(mon, world, monkeypatch, tmp_path):
    calls = spawned(mon, monkeypatch, tmp_path)
    mon.probes["gauge"] = (1, "7 d 8.60 M tokens = 72 % of 12.00 M (≥ 70 % → reviews and events only)")
    stale_engine(world["BTCUSDT"])
    res = run(mon, world, ["BTCUSDT"], diagnose=True)
    assert calls == [] and not res.diagnose["launched"]
    assert res.diagnose["why"].startswith("usage gauge at level 1, above monitor.diagnose_max_gauge_level 0 (7 d")
    st = state(world)
    assert st["last_diagnose_ms"] is None and st["diagnose_launches"] == [] and "diagnose_wanted" not in st
    mon.probes["gauge"] = (None, "OperationalError: database is locked")      # unknown = held back
    stale_at(world, "ETHUSDT", T0 + 15 * MIN)
    res = run(mon, world, ["ETHUSDT"], at=T0 + 15 * MIN, diagnose=True)
    assert calls == [] and res.diagnose["why"] == ("usage gauge unreadable (OperationalError: database is locked) — "
                                                   "no billed diagnosis while the plan's use is unknown")
    monitor_cfg(world, diagnose_max_gauge_level=1)                     # the owner allows level 1
    mon.probes["gauge"] = (1, "")
    stale_at(world, "XAUUSD", T0 + 30 * MIN)
    assert run(mon, world, ["XAUUSD"], at=T0 + 30 * MIN, diagnose=True).diagnose["launched"] and len(calls) == 1


def test_the_gauge_level_is_read_from_the_ledger_the_review_pack_reads(world, monkeypatch):
    m = tool("monitor")                                                # the real probe (the fixture stubs it)
    base, btc = world["base"], world["BTCUSDT"]
    assert m._gauge_level(base, [btc], T0) == (0, "no ledger yet")     # a check never creates the ledger
    assert not (base.paths.shared() / "ai_usage.db").exists()
    from tradingsystem.ai.budget import UsageStore
    base.paths.shared().mkdir(parents=True, exist_ok=True)
    UsageStore(base.paths.shared() / "ai_usage.db").close()            # the per-pair systems' shared ledger
    level, reason = m._gauge_level(base, [btc], T0)
    assert level == 0 and "within budget" in reason
    assert m._gauge_level(base, [base], T0) == (0, "no ledger yet")    # the all-pairs system: its own app.db
    broken = type("G", (), {"level": 0, "reason": "gauge unavailable", "error": "OperationalError: locked"})()
    import tradingsystem.ai.usage_gauge as ug
    monkeypatch.setattr(ug, "UsageGauge", lambda s, store: type("U", (), {"state": lambda self, now=None: broken})())
    assert m._gauge_level(base, [btc], T0) == (None, "OperationalError: locked")


# ------------------------------------------------------------------ restart loop on the real 2026-09-26 XAU pattern
def test_the_restart_loop_rule_fires_on_the_2026_09_26_xau_pattern(mon, world):
    """Production, 2026-09-26 22:17-23:19 UTC: XAUUSD's supervisor killed ingest-binance 21 times in 62 min
    (heartbeat unchanged ~176 s each time). The monitor run 15 min into the loop reports it."""
    kills = [tuple(k) for k in json.loads((REAL / "xau_supervisor_kills_2026-09-26.json").read_text(encoding="utf-8"))]
    assert len(kills) == 21 and {k[1:3] for k in kills} == {("supervisor:ingest-binance", "killed")}
    at = kills[0][0] + 15 * MIN
    seed(world["XAUUSD"], status=fresh(at), events=[k for k in kills if k[0] <= at])
    (f,) = [x for x in run(mon, world, ["XAUUSD"], at=at).problems if x.key.startswith("restart:")]
    assert (f.level, f.key, f.title) == ("warn", "restart:XAUUSD:ingest-binance", "XAUUSD: restart loop")
    assert "ingest-binance exited or was killed 5× in the last hour (limit 3)" in f.text
    assert "logs\\XAUUSD\\ingest-binance.stderr.log" in f.text and f.new
    end = kills[-1][0] + MIN                                           # the end of the loop: the last hour's kills
    seed(world["XAUUSD"], status=fresh(end), events=[k for k in kills if k[0] > at])
    n = sum(1 for k in kills if k[0] >= end - MS_PER_HOUR)
    (f,) = [x for x in run(mon, world, ["XAUUSD"], at=end).problems if x.key.startswith("restart:")]
    assert n == 20 and f"{n}× in the last hour" in f.text and not f.new       # the same alert: already notified


# ------------------------------------------------------------------ the supervisor tells the owner about a sleep
def test_the_supervisor_notifies_once_per_sleep_on_resume_and_never_fails_for_it(tmp_path, monkeypatch):
    from tradingsystem.core import notify as nt
    monkeypatch.setenv(INSTANCE_ENV, "BTCUSDT")
    monkeypatch.setattr(sv, "keep_awake", lambda on: False)
    sup = sv.Supervisor(["engine"], str(tmp_path / "data"))
    sup.term_for = {}
    sent: list = []
    monkeypatch.setattr(nt, "notify", lambda s, level, title, text, *, key=None, pair=None:
                        sent.append((level, title, text, key, pair)))
    sup.on_gap("stall", 30.0)                                          # not a sleep: no notification
    sup.on_gap("clock_jump", 3600.0)
    assert sent == []
    sup.on_gap("suspend", 3137.4)                                      # the 52-min hibernate of 2026-09-26
    ((level, title, text, key, pair),) = sent
    assert (level, title, key, pair) == ("warn", "PC was asleep", sv.SUSPEND_NOTIFY_KEY, None)
    assert key == "supervisor:system_suspend"                          # one key for every system: one toast a sleep
    assert "the PC was asleep for ~52 min (3137 s) until 20" in text and "(seen by BTCUSDT)" in text

    def boom(*a, **kw):                                                # noqa: ANN002, ANN003
        raise RuntimeError("notifier broken")

    monkeypatch.setattr(nt, "notify", boom)
    sup.on_gap("suspend", 60.0)                                        # the watchdog goes on regardless
    con = sqlite3.connect(tmp_path / "data" / "instances" / "BTCUSDT" / "app.db")
    rows = con.execute("SELECT event, duration_ms FROM ingestion_events WHERE collector='supervisor:all' "
                       "ORDER BY id").fetchall()
    con.close()
    assert rows == [("stall", 30_000), ("clock_jump", 3_600_000), ("system_suspend", 3_137_400),
                    ("system_suspend", 60_000)]
    assert sup.last_gap["kind"] == "suspend" and sup.grace_until > 0
