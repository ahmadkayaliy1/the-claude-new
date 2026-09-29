"""Phase 5 B19: tools/desk_report.py - the gold desk scorecard (read-only).

The 1m bars are the REAL XAUUSD@ bars of 2026-09-24 (tests/fixtures/real/xauusd_1m_2026-09-24.csv); the fixture has no
spread column, so the stored bars carry a spread of 30 points ($0.30, the measured London median) - said where used.
The walk cases on hand-built bars are labelled. The stored decisions are built rows with the executor's own record
shape (gate list, shadow, desk_ok)."""
from __future__ import annotations

import csv
import importlib.util
import json
import sqlite3
from pathlib import Path

import numpy as np
import pytest

from tradingsystem.ai.store import DecisionStore
from tradingsystem.core.instruments import InstrumentRegistry
from tradingsystem.core.settings import INSTANCE_ENV, load_settings
from tradingsystem.core.timeframes import Timeframe
from tradingsystem.core.timeutil import MS_PER_MINUTE, parse_date_spec
from tradingsystem.storage.sqlite_store import SQLiteHotStore
from tradingsystem.storage.tablespec import spec_for, system_specs, table_specs

ROOT = Path(__file__).resolve().parents[2]
REAL = ROOT / "tests" / "fixtures" / "real"
spec = importlib.util.spec_from_file_location("desk_report_under_test", ROOT / "tools" / "desk_report.py")
dr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dr)

with open(REAL / "xauusd_1m_2026-09-24.csv", newline="") as f:
    BARS = [(int(r["open_time"]), float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]),
             int(float(r["tick_volume"]))) for r in csv.DictReader(f)]
SPREAD_POINTS = 30


def bars(rows):
    """Built: hand-made bid bars [(open_time, open, high, low, close)] with a 0.30 spread."""
    a = np.array(rows, dtype=float)
    return {"open_time": a[:, 0].astype(np.int64), "open": a[:, 1], "high": a[:, 2], "low": a[:, 3], "close": a[:, 4],
            "spread_px": np.full(len(a), 0.30)}


T0 = parse_date_spec("2026-09-24T13:00:00Z")              # 09:00 New York: the gold day ends at 21:00 UTC


def test_desk_ok_needs_every_unwaived_check_and_a_gate_that_reached_sizing():
    g = lambda *checks: [{"check": c, "ok": ok, "detail": ""} for c, ok in checks]  # noqa: E731
    assert dr.desk_ok_of(g(("kill_switch", True), ("position_size", False), ("rr_after_costs", True))) == (True, [])
    assert dr.desk_ok_of(g(("position_size", False), ("effective_leverage", False))) == (True, [])
    assert dr.desk_ok_of(g(("position_size", False), ("rr_after_costs", False))) == (False, ["rr_after_costs"])
    assert dr.desk_ok_of(g(("sl_side", False))) == (False, ["sl_side"])              # stopped before sizing
    assert dr.desk_ok_of(g(("kill_switch", True))) == (False, [])
    assert dr.desk_ok_of(None) == (None, [])


def test_the_walk_is_bid_ask_correct_and_takes_the_stop_first():
    m = MS_PER_MINUTE
    # BUY entry at the ask 100.30; stop 99.30, target 102.30 (bid)
    b = bars([(T0, 100, 100.1, 99.9, 100), (T0 + m, 100, 102.4, 99.2, 101), (T0 + 2 * m, 101, 101, 101, 101)])
    assert dr.walk(b, 0, 1.0, True) == (-1.0, 2)                                     # both on bar 2: the stop first
    b = bars([(T0, 100, 100.1, 99.9, 100), (T0 + m, 100, 102.4, 99.8, 102)])
    assert dr.walk(b, 0, 1.0, True) == (2.0, 2)
    # SELL at the bid 100; stop 101 triggers on the ask: a bid high of 100.75 + 0.30 reaches it
    b = bars([(T0, 100, 100.2, 99.9, 100), (T0 + m, 100, 100.75, 99.9, 100)])
    assert dr.walk(b, 0, 1.0, False) == (-1.0, 2)
    # flat at the gold day's end, marked at the last bar's close (a SELL buys back at the ask)
    b = bars([(T0, 100, 100.2, 99.9, 100), (T0 + m, 100, 100.2, 99.9, 99.5)])
    r, mins = dr.walk(b, 0, 1.0, False)
    assert mins == 2 and r == pytest.approx((100 - (99.5 + 0.30)) / 1.0)


@pytest.fixture
def root(tmp_path):
    """A data root with the real 1m XAU day and an XAU app.db."""
    s = load_settings(env_path=Path("nope.env"), extra_env={INSTANCE_ENV: "XAUUSD"})
    s = s.model_copy(update={"paths": s.paths.model_copy(update={"data_dir": str(tmp_path / "data")})})
    inst = InstrumentRegistry.from_settings(s).primary("XAUUSD")
    with SQLiteHotStore(inst.hot_db_path(s.paths.data())) as st:
        st.ensure_tables([*table_specs(inst), *system_specs()])
        st.upsert(spec_for(inst, "candles", Timeframe.M1),
                  [(t, t + 3 * 3_600_000, o, h, lo, c, v, SPREAD_POINTS, None) for t, o, h, lo, c, v in BARS])
    (s.paths.state()).mkdir(parents=True, exist_ok=True)
    DecisionStore(s.paths.state() / "app.db").close()
    return tmp_path, s


def add_idea(s, did, ts, decision, entry, sl, tp, detail, vo=None, vr=None):
    con = sqlite3.connect(s.paths.state() / "app.db")
    rec = {"decision": decision, "order_type": "LIMIT", "entry": {"price": entry}, "stop_loss": sl,
           "take_profits": [{"price": tp, "close_fraction": 1.0}]}
    cols = {r[1] for r in con.execute("PRAGMA table_info(ai_decisions)")}
    row = {"id": did, "ts": ts, "pair": "XAUUSD", "status": "valid", "decision": decision,
           "recommendation": json.dumps(rec), "execution_state": "not_executed", "execution_detail": json.dumps(detail),
           "virtual_outcome": vo, "virtual_r": vr}
    for c in cols - set(row):
        row[c] = None
    for c in ("provider", "model", "mode", "prompt_hash", "payload_hash", "trigger", "payload", "raw", "errors"):
        if c in cols and row.get(c) is None:
            row[c] = ""
    row = {k: v for k, v in row.items() if k in cols}
    con.execute(f"INSERT INTO ai_decisions ({','.join(row)}) VALUES ({','.join('?' * len(row))})", list(row.values()))
    con.commit()
    con.close()


GATE_OK = [{"check": "kill_switch", "ok": True, "detail": ""}, {"check": "spread_vs_sl", "ok": True,
           "detail": "spread 0.30 ≤ 20% of SL distance 9.00"}, {"check": "position_size", "ok": False, "detail": ""}]
GATE_RR = GATE_OK + [{"check": "rr_after_costs", "ok": False, "detail": "1.3 < 2"}]


def test_the_scorecard_reads_the_shadow_ideas_and_the_baseline(root, monkeypatch):
    tmp, s = root
    ts = parse_date_spec("2026-09-24T13:10:00Z")                     # New York window (09:10 EDT)
    add_idea(s, "a" * 32, ts, "SELL", 4290.0, 4299.0, 4272.0, {"shadow": True, "desk_ok": True, "gate": GATE_OK,
                                                               "spread_at_gate": 0.3}, "tp1_first", 2.0)
    add_idea(s, "b" * 32, ts + 60_000, "BUY", 4280.0, 4271.0, 4290.0, {"shadow": True, "desk_ok": False,
                                                                       "gate": GATE_RR}, "sl_first", -1.0)
    add_idea(s, "c" * 32, ts - 86_400_000, "SELL", 4300.0, 4309.06, 4288.0, {"gate": GATE_RR,
                                                                              "reason": "position_size"})
    monkeypatch.setattr(dr, "pair_settings", lambda pair, r: s)
    d = dr.collect("XAUUSD", None, days=2, now=parse_date_spec("2026-09-25T00:00:00Z"))
    assert d["shadow"]["n"] == 2 and d["desk_ok"]["n"] == 1 and d["not_desk_ok"]["n"] == 1
    a = next(r for r in d["ideas"] if r["id"] == "a" * 8)
    assert a["stop"] == 9.0 and a["window"] == "new_york" and a["r_net"] == pytest.approx(2.0 - 0.3 / 9.0, abs=1e-3)
    assert d["m1"] == {"regated": 3, "desk_ok": 1, "failed_checks": {"rr_after_costs": 2}}
    b = d["baseline"]
    assert b["bars"] == len(BARS) and b["spread_median"] == pytest.approx(0.30)
    assert set(b["by_stop_side"]) >= {"9 BUY", "9 SELL", "2 BUY", "2 SELL"}
    # every entry is a 5-min close inside a desk window: 2026-09-24 (EDT/BST): London 06:45-10:00, NY 12:15-15:30 UTC
    x = b["by_stop_side"]["9 SELL"]
    assert set(x["by_window"]) <= {"london", "new_york"} and x["n"] == b["entries"]
    assert d["verdict"]["passes"] is False and d["verdict"]["desk_ok_resolved"] == 1
    text = dr.render(d)
    assert "Random-entry baseline" in text and "not met" in text and "| aaaaaaaa |" in text


def test_the_baseline_matches_an_independent_walk_on_the_real_day(root, monkeypatch):
    tmp, s = root
    monkeypatch.setattr(dr, "pair_settings", lambda pair, r: s)
    b = dr.baseline(s, "XAUUSD", [5.0], 2, parse_date_spec("2026-09-25T00:00:00Z"))
    arr = np.array(BARS, dtype=float)
    t = arr[:, 0].astype(np.int64)
    rs = []
    for i in np.nonzero(t % 300_000 == 0)[0]:
        h = (t[i] // 3_600_000) % 24 + (t[i] // 60_000 % 60) / 60
        if not (6.75 <= h < 10.0 or 12.25 <= h < 15.5):
            continue
        entry, j = arr[i, 1] + 0.30, i                                  # BUY at the ask; flat at 21:00 UTC (17:00 EDT)
        end = np.searchsorted(t, parse_date_spec("2026-09-24T21:00:00Z"))
        lo, hi = arr[i:end, 3], arr[i:end, 2]
        k_sl = np.argmax(lo <= entry - 5) if (lo <= entry - 5).any() else None
        k_tp = np.argmax(hi >= entry + 10) if (hi >= entry + 10).any() else None
        if k_sl is not None and (k_tp is None or k_sl <= k_tp):
            rs.append(-1.0)
        elif k_tp is not None:
            rs.append(2.0)
        else:
            rs.append((arr[end - 1, 4] - entry) / 5)
    got = b["by_stop_side"]["5 BUY"]
    assert got["n"] == len(rs) and got["expectancy_r"] == pytest.approx(round(float(np.mean(rs)), 3), abs=1e-3)


def test_a_pair_without_a_desk_or_a_database_is_invalid(tmp_path, monkeypatch):
    s = load_settings(env_path=Path("nope.env"), extra_env={INSTANCE_ENV: "BTCUSDT"})
    monkeypatch.setattr(dr, "pair_settings", lambda pair, r: s)
    with pytest.raises(dr.Invalid, match="no desk"):
        dr.collect("BTCUSDT")
    assert dr.main(["--pair", "BTCUSDT"]) == 3                        # no destination either


def test_the_demo_report_carries_the_desk_section(root):
    tmp, s = root
    ts = parse_date_spec("2026-09-24T13:10:00Z")
    add_idea(s, "a" * 32, ts, "SELL", 4290.0, 4299.0, 4272.0, {"shadow": True, "desk_ok": True, "gate": GATE_OK,
                                                               "spread_at_gate": 0.3}, "tp1_first", 2.0)
    sp = importlib.util.spec_from_file_location("demo_report_desk_test", ROOT / "tools" / "demo_report.py")
    demo = importlib.util.module_from_spec(sp)
    sp.loader.exec_module(demo)
    con = sqlite3.connect(f"file:{(s.paths.state() / 'app.db').as_posix()}?mode=ro", uri=True)
    try:
        d = demo.desk_section(con, s, "XAUUSD", ts - 3_600_000, ts + 3_600_000)
    finally:
        con.close()
    assert d["shadow"]["n"] == 1 and d["desk_ok"]["resolved"] == 1 and d["calls_per_day"] == {"2026-09-24": 1}


def test_the_out_guard_protects_a_running_checkout_whatever_root_says(tmp_path):
    """Review fix: the file's own location decides (a production checkout with a system's app.db: only data/reviews)."""
    prod = tmp_path / "prod"
    (prod / "data" / "instances" / "XAUUSD").mkdir(parents=True)
    (prod / "data" / "instances" / "XAUUSD" / "app.db").write_bytes(b"")
    (prod / ".git").mkdir()
    (prod / "data" / "reviews").mkdir()
    assert dr._in_running_checkout(prod / "docs" / "measurements" / "gold_desk.md")
    assert not dr._in_running_checkout(prod / "data" / "reviews" / "gold_desk.md")
    wt = tmp_path / "wt"
    (wt / "data" / "instances" / "XAUUSD").mkdir(parents=True)         # a worktree: no app.db
    (wt / ".git").write_text("gitdir: elsewhere", encoding="utf-8")
    assert not dr._in_running_checkout(wt / "docs" / "measurements" / "gold_desk.md")
    assert dr.main(["--root", str(prod), "--out", str(prod / "docs" / "x.md")]) == 3


def test_the_scorecard_breaks_the_ideas_down_by_session(root, monkeypatch):
    tmp, s = root
    ts = parse_date_spec("2026-09-24T13:10:00Z")
    add_idea(s, "a" * 32, ts, "SELL", 4290.0, 4299.0, 4272.0, {"shadow": True, "desk_ok": True, "gate": GATE_OK,
                                                               "spread_at_gate": 0.3}, "tp1_first", 2.0)
    con = sqlite3.connect(s.paths.state() / "app.db")
    con.execute("UPDATE ai_decisions SET session='ny_open'")
    con.commit()
    con.close()
    monkeypatch.setattr(dr, "pair_settings", lambda pair, r: s)
    d = dr.collect("XAUUSD", None, days=2, with_baseline=False, now=parse_date_spec("2026-09-25T00:00:00Z"))
    assert d["by_session"]["ny_open"]["n"] == 1 and "| session ny_open | 1 |" in dr.render(d)
