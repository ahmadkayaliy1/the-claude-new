"""tools/health_report.py Phase 4 additions: the engine's snapshot build time (follow-up b), the usage gauge, the
adaptive overlay and the kill-switch reasons — every existing line kept."""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
import time
import types
from dataclasses import dataclass
from pathlib import Path

import pytest

from tradingsystem.core.killswitch import set_kill_switch
from tradingsystem.core.settings import INSTANCE_ENV, load_settings
from tradingsystem.ingest.common.appdb import AppDB
from tradingsystem.supervisor import supervisor as sv

ROOT = Path(__file__).resolve().parents[2]


def tool(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture
def hr():
    return tool("health_report")


@pytest.fixture
def btc(tmp_path):
    return sv.with_data_dir(load_settings(extra_env={INSTANCE_ENV: "BTCUSDT"}), str(tmp_path / "data"))


def seed(s, engine_detail: dict, now: float) -> None:
    db = s.paths.state() / "app.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    AppDB(db).close()
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE ai_decisions (id TEXT, ts INTEGER, pair TEXT, status TEXT, decision TEXT, latency_ms "
                "INTEGER, input_tokens INTEGER, output_tokens INTEGER, errors TEXT, execution_state TEXT, "
                "execution_detail TEXT, outcome TEXT, outcome_pnl_usd REAL, virtual_outcome TEXT, virtual_r REAL)")
    rows = [("engine", "live", json.dumps(engine_detail)),
            ("executor", "live", json.dumps({"equity": 100.0, "today_pnl_pct": 0.5, "open_positions": 0,
                                             "open_orders": 0, "kill_switch": False, "exposure": []}))]
    for c, st, det in rows:
        con.execute("INSERT INTO collector_status(collector, state, detail, updated_ms) VALUES (?,?,?,?)",
                    (c, st, det, int(now) - 5_000))
    con.commit()
    con.close()


ENGINE = {"ai_ready": True, "provider": "claude_code", "quota_left_today": 30}


def test_the_engine_line_keeps_its_fields_and_shows_the_median_build_time(hr, btc):
    now = time.time() * 1000
    seed(btc, {**ENGINE, "snapshot_build_ms": {"last": 5200, "max": 15000, "median": 1800, "n": 20}}, now)
    out = hr.system_report(btc, 6, now)
    (line,) = [x for x in out if x.lstrip("! ").startswith("engine ")]
    assert "ai_ready True provider claude_code quota_left 30" in line                    # the Phase 3 fields stay
    assert "snapshot_build median of 20 1800 ms (last 5200, max 15000)" in line
    assert not [x for x in out if x.startswith("!! snapshot build")]                     # the median is below 3000


def test_a_slow_build_gets_its_own_problem_line(hr, btc):
    now = time.time() * 1000
    seed(btc, {**ENGINE, "snapshot_build_ms": {"last": 4000, "max": 15000, "median": 3400, "n": 12}}, now)
    (warn,) = [x for x in hr.system_report(btc, 6, now) if x.startswith("!! snapshot build")]
    assert "3400 ms (median)" in warn and "3000" in warn


def test_without_the_median_the_last_build_is_used(hr, btc):
    now = time.time() * 1000
    seed(btc, {**ENGINE, "snapshot_build_ms": {"last": 3100, "max": 3100}}, now)
    out = hr.system_report(btc, 6, now)
    assert any("snapshot_build last 3100 ms" in x for x in out)
    assert any(x.startswith("!! snapshot build 3100 ms (last)") for x in out)
    assert hr._snapshot_build({**ENGINE, "snapshot_build_ms": {"last": None, "max": None}}) == ("", None, "last")


def test_the_adaptive_overlay_and_the_kill_switch_reasons_are_listed(hr, btc):
    now = time.time() * 1000
    seed(btc, {**ENGINE, "adaptive": {"BTCUSDT": {"adaptive_hash": "a1b2c3d4e5f60718", "playbook_hash": None,
                                                  "paused_until": "2026-09-28T10:00:00.000Z"}}}, now)
    set_kill_switch(btc, "BTCUSDT", reason="BTCUSDT: order burst: 4 orders", actor="monitor")
    out = hr.system_report(btc, 6, now)
    assert any(x.startswith("   adaptive overlay BTCUSDT: adaptive a1b2c3d4e5f60718 playbook -")
               and "AI paused until 2026-09-28T10:00" in x for x in out)
    (ks,) = [x for x in out if x.startswith("!! kill switch ON")]
    assert "(BTCUSDT)" in ks and "monitor" in ks and "order burst" in ks
    set_kill_switch(btc, None, reason="by hand", actor="user")
    assert len([x for x in hr.system_report(btc, 6, now) if x.startswith("!! kill switch ON")]) == 2


@dataclass
class _Gauge:
    level: int
    week_tokens: float
    week_pct: float
    five_h_tokens: float
    five_h_pct: float
    enforce: bool
    reason: str


def test_the_usage_gauge_is_printed_from_the_shared_ledger(hr, btc, monkeypatch):
    seen = {}

    class FakeGauge:
        def __init__(self, s, usage):
            seen["usage"] = usage

        def state(self, now_ms=None):
            return seen["state"]

    monkeypatch.setitem(sys.modules, "tradingsystem.ai.usage_gauge", types.SimpleNamespace(UsageGauge=FakeGauge))
    monkeypatch.setattr(hr.procs, "running_supervisors", lambda older_s=None: {})
    now = time.time() * 1000
    assert hr.usage_gauge_line(btc) is None                                   # no ledger: nothing, none created
    assert not (btc.paths.shared() / "ai_usage.db").exists()
    btc.paths.shared().mkdir(parents=True)
    from tradingsystem.ai.budget import UsageStore
    UsageStore(btc.paths.shared() / "ai_usage.db").close()
    seen["state"] = _Gauge(1, 8_600_000, 71.7, 400_000, 26.7, False, "7 d at 72 % of 12 M")
    line = [x for x in hr.machine_report(btc, now) if "usage gauge" in x][0]
    assert line.startswith("   usage gauge: level 1") and "8.60 M tokens (72 % of the weekly budget)" in line
    assert "observe only" in line and "72 % of 12 M" in line
    seen["state"] = _Gauge(2, 11_000_000, 91.7, 1_400_000, 93.3, False, "")
    assert hr.usage_gauge_line(btc).startswith("!! usage gauge: level 2")
    seen["state"] = _Gauge(1, 8_600_000, 71.7, 400_000, 26.7, True, "")
    assert hr.usage_gauge_line(btc).startswith("!! usage gauge: level 1") and "enforced" in hr.usage_gauge_line(btc)


def test_sessions_with_unknown_usage_make_the_gauge_line_a_problem_line(hr, btc, monkeypatch):
    """A timed-out or crashed operator session (0 tokens recorded, error 'usage_unknown: …') is flagged with '!!' on
    the gauge line even at level 0: the sums undercount, they are not the real spend."""
    from tradingsystem.ai.budget import USAGE_UNKNOWN_PREFIX, UsageStore
    btc.paths.shared().mkdir(parents=True)
    ledger = btc.paths.shared() / "ai_usage.db"
    UsageStore(ledger).close()
    assert hr.usage_gauge_line(btc).startswith("   usage gauge: level 0")
    con = sqlite3.connect(ledger)
    con.execute("INSERT INTO ai_usage(ts, provider, model, purpose, pair, input_tokens, output_tokens, cached_tokens, "
                "cost_usd, latency_ms, ok, error, role) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (int(time.time() * 1000) - 3_600_000, "claude_code", "opus", "operator_weekly", None, 0, 0, 0, 0.0,
                 2_280_000, 0, f"{USAGE_UNKNOWN_PREFIX}no result document after 2280 s (timeout): -", "review"))
    con.commit()
    con.close()
    line = hr.usage_gauge_line(btc)
    assert line.startswith("!! usage gauge: level 0") and line.endswith(
        "— within budget + 1 session(s) with unknown usage in 7 d")
    assert line.count("unknown usage") == 1                                   # said once, by the gauge's reason

    @dataclass
    class _Older(_Gauge):                                                     # a reason that does not name them
        unknown_7d: int = 0

    monkeypatch.setitem(sys.modules, "tradingsystem.ai.usage_gauge", types.SimpleNamespace(
        UsageGauge=lambda s, usage: types.SimpleNamespace(state=lambda: _Older(0, 0, 0, 0, 0, False, "ok", 3))))
    assert hr.usage_gauge_line(btc).startswith("!! usage gauge: level 0") and hr.usage_gauge_line(btc).endswith(
        "— ok + 3 session(s) with unknown usage in 7 d")


def test_a_broken_gauge_does_not_break_the_report(hr, btc, monkeypatch):
    class Boom:
        def __init__(self, s, usage):
            raise RuntimeError("no tokens_since yet")

    monkeypatch.setitem(sys.modules, "tradingsystem.ai.usage_gauge", types.SimpleNamespace(UsageGauge=Boom))
    btc.paths.shared().mkdir(parents=True)
    from tradingsystem.ai.budget import UsageStore
    UsageStore(btc.paths.shared() / "ai_usage.db").close()
    assert hr.usage_gauge_line(btc) == "!! usage gauge unreadable: RuntimeError: no tokens_since yet"
