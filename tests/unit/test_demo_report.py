"""Demo report (Phase 5 A8): the demo window's evidence per pair and in total, read-only, every number with its n."""
from __future__ import annotations

import collections
import hashlib
import importlib.util
import json
import sqlite3
import subprocess
import sys
import types
from collections import namedtuple
from pathlib import Path

import pytest

from tradingsystem.ai.budget import USAGE_UNKNOWN_PREFIX, UsageStore
from tradingsystem.ai.providers.base import LLMResult
from tradingsystem.ai.store import DecisionRecord, DecisionStore
from tradingsystem.core.filelock import FileLock
from tradingsystem.core.settings import INSTANCE_ENV, load_settings
from tradingsystem.core.timeutil import MS_PER_DAY, MS_PER_HOUR, MS_PER_MINUTE, iso, parse_date_spec
from tradingsystem.ingest.common.appdb import AppDB

ROOT = Path(__file__).resolve().parents[2]
REAL = ROOT / "tests" / "fixtures" / "real"
SLOT = 15 * MS_PER_MINUTE
T = parse_date_spec          # "2026-09-27T21:50:00Z" → ms


def tool(name: str, modname: str | None = None):
    spec = importlib.util.spec_from_file_location(modname or f"{name}_under_test", ROOT / "tools" / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture()
def dr(monkeypatch):
    m = tool("demo_report")
    rp = m.pack_module()
    monkeypatch.setattr(rp, "git_info", lambda root: {"sha": "abc1234", "branch": "main", "dirty": False})
    # this machine's own System log is not the report's subject here (test_power_events_… reads a recorded one)
    monkeypatch.setattr(m, "power_events", lambda since, until: {"episodes": [], "counts": {}, "error": "not read"})
    return m


def base_at(tmp_path: Path):
    s = load_settings(extra_env={INSTANCE_ENV: ""})
    return s.model_copy(update={"paths": s.paths.model_copy(update={"data_dir": str(tmp_path / "data"),
                                                                     "logs_dir": str(tmp_path / "logs")})})


def system_db(dr, base, pair: str) -> tuple[object, Path]:
    """A pair's system on the tmp root: its app.db with the app tables and the decision tables."""
    sp = dr.settings_for(base, pair)
    db = sp.paths.state() / "app.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    AppDB(db).close()
    DecisionStore(db, config_hash="c" * 16).close()
    return sp, db


def add_events(db: Path, rows) -> None:
    con = sqlite3.connect(db)
    con.executemany("INSERT INTO ingestion_events (ts, collector, event, detail, duration_ms) VALUES (?,?,?,?,?)",
                    [(r["ts"], r["collector"], r["event"], r.get("detail"), r.get("duration_ms")) for r in rows])
    con.commit()
    con.close()


def add_decisions(db: Path, pair: str, rows) -> None:
    con = sqlite3.connect(db)
    con.executemany("INSERT INTO ai_decisions (id, ts, pair, mode, status, errors) VALUES (?,?,?,?,?,?)",
                    [(r["id"], r["ts"], pair, "agent_per_pair", r["status"], r.get("errors")) for r in rows])
    con.commit()
    con.close()


def idea(store: DecisionStore, pair: str, ts: int, side: str = "BUY") -> str:
    return store.save(DecisionRecord(pair=pair, mode="agent_per_pair", trigger="setup", status="valid", ts=ts,
                                     recommendation={"decision": side, "order_type": "LIMIT", "confidence": 65,
                                                     "stop_loss": 2600.0, "entry": {"price": 2650.0},
                                                     "take_profits": [{"price": 2750.0, "close_fraction": 1.0}]}))


def _digest(root: Path) -> dict[str, str]:
    """Every database and JSON file under ``root`` (a read-only SQLite open may leave a -shm next to a WAL file)."""
    return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.rglob("*"))
            if p.is_file() and p.suffix in (".db", ".json")}


# --------------------------------------------------------------------------- the window
def test_demo_window_comes_from_the_evaluation_config_and_is_cut_at_now(dr, tmp_path):
    s = base_at(tmp_path)
    start = T(s.evaluation.demo_start_utc)
    assert start == T("2026-09-27T21:50:00Z") and s.evaluation.demo_days == 5
    w = dr.demo_window(s, now=start + 10 * MS_PER_HOUR)
    assert (w["since_ms"], w["until_ms"], w["planned_until_ms"]) == (start, start + 10 * MS_PER_HOUR,
                                                                     start + 5 * MS_PER_DAY)
    assert not w["complete"] and w["days_covered"] == round(10 / 24, 2) and w["source"] == "config"
    w = dr.demo_window(s, now=start + 6 * MS_PER_DAY)
    assert w["complete"] and w["until_ms"] == start + 5 * MS_PER_DAY and w["days_covered"] == 5
    w = dr.demo_window(s, since=start + MS_PER_DAY, now=start + 20 * MS_PER_DAY)       # --since alone: demo_days
    assert w["planned_until_ms"] == start + 6 * MS_PER_DAY and w["source"] == "arguments"
    w = dr.demo_window(s, since=start, until=start + MS_PER_DAY, now=start + 20 * MS_PER_DAY)
    assert w["days"] == 1 and w["complete"]
    with pytest.raises(dr.Invalid):
        dr.demo_window(s, since=start, until=start - 1, now=start + MS_PER_DAY)
    with pytest.raises(dr.Invalid, match="after now"):
        dr.demo_window(s, now=start - 1)
    unset = s.model_copy(update={"evaluation": s.evaluation.model_copy(update={"demo_start_utc": None})})
    with pytest.raises(dr.Invalid, match="no window"):
        dr.demo_window(unset, now=start)


# --------------------------------------------------------------------------- availability
def test_availability_on_the_real_btc_window_counts_every_loss_class(dr, tmp_path):
    """The real BTC evidence of the first demo night (fixture exported read-only from production): the restart at
    22:16 (down since 21:26), the Binance outage 00:22–05:30 (data_not_ready + no screens), the quota used up
    before the window on 09-27 (ai_quota), one AI timeout. Every class is recomputed here from the fixture."""
    fx = json.loads((REAL / "demo_window_btc_2026-09-28.json").read_text(encoding="utf-8"))
    base = base_at(tmp_path)
    sp, db = system_db(dr, base, "BTCUSDT")
    add_events(db, fx["ingestion_events"])
    add_decisions(db, "BTCUSDT", fx["ai_decisions"])
    logs = sp.paths.logs()
    logs.mkdir(parents=True)
    (logs / "engine.jsonl").write_text("\n".join(fx["engine_log"]) + "\n", encoding="utf-8")
    since, until = T("2026-09-27T21:50:00Z"), T("2026-09-28T08:15:00Z")
    slots = list(range(T("2026-09-27T22:00:00Z"), until - SLOT + 1, SLOT))
    assert len(slots) == 41                                           # crypto CFD: open all Sunday night / Monday

    def slot_of(ts):
        return ts // SLOT * SLOT

    ev = [e for e in fx["ingestion_events"] if since <= e["ts"] < until]
    want = {k: {slot_of(e["ts"]) for e in ev if e["event"] == k} & set(slots) for k in ("data_not_ready", "ai_quota")}
    want["stopped"] = {T("2026-09-27T22:00:00Z"), T("2026-09-27T22:15:00Z")}    # down since 21:26, started 22:16
    want["ai_error"] = {slot_of(d["ts"]) for d in fx["ai_decisions"] if since <= d["ts"] < until
                        and d["status"] == "error"}
    screened = {slot_of(int(dr._ts_ms(json.loads(ln)["ts"]))) for ln in fx["engine_log"]}
    want["no_screen"] = set(slots) - screened
    assert len(want["ai_error"]) == 1                                            # 05:31 "no answer within 180s"
    data = dr.collect(base, since=since, until=until, now=until + MS_PER_HOUR, pair="BTCUSDT")
    av = data["per_pair"]["BTCUSDT"]["availability"]
    assert av["calendar"] == "windsor_crypto_cfd" and (av["cycles"], av["open_cycles"]) == (41, 41)
    assert av["lost"] == {k: len(v) for k, v in want.items() if v}
    lost = set().union(*want.values())
    assert av["lost_total"] == len(lost) and av["share"] == round(1 - len(lost) / 41, 4)
    assert av["lost"]["data_not_ready"] == 22 and av["lost"]["no_screen"] == 20 and av["lost_total"] == 29
    assert av["screen_log_from"].startswith("2026-09-27T09:")
    # the suspend of 18:32–19:24 lies before this window; a window over it loses its slots
    data = dr.collect(base, since=T("2026-09-27T18:00:00Z"), until=T("2026-09-27T20:00:00Z"),
                      now=until, pair="BTCUSDT")
    assert data["per_pair"]["BTCUSDT"]["availability"]["lost"]["system_suspend"] == 4     # 18:30 … 19:15


def test_closed_market_cycles_are_not_counted_and_their_events_are_ignored(dr, tmp_path):
    """XAU: Friday 17:00 → Sunday 18:00 New York (and the daily break) are closed; the crypto CFDs close Saturday
    05:00–08:00 UTC for maintenance. Closed cycles are neither open nor lost, whatever happened in them."""
    base = base_at(tmp_path)
    _, xdb = system_db(dr, base, "XAUUSD")
    add_events(xdb, [{"ts": T("2026-10-03T12:01:00Z"), "collector": "engine", "event": "data_not_ready"},
                     {"ts": T("2026-10-04T22:31:30Z"), "collector": "engine", "event": "data_not_ready"}])
    data = dr.collect(base, since=T("2026-10-02T20:00:00Z"), until=T("2026-10-04T23:00:00Z"),
                      now=T("2026-10-05T00:00:00Z"), pair="XAUUSD")
    av = data["per_pair"]["XAUUSD"]["availability"]
    assert av["calendar"] == "ny_metals_fx"
    assert av["open_cycles"] == 8 and av["closed_cycles"] == av["cycles"] - 8     # Fri 20–21 and Sun 22–23 UTC
    assert av["lost"] == {"data_not_ready": 1} and av["share"] == 0.875
    assert av["reopen_waits"] == 1                                               # Sun 22:00: its bar 21:45 is closed
    assert any("no_screen" in n for n in av["notes"])                            # no engine log: not measured
    _, bdb = system_db(dr, base, "BTCUSDT")
    data = dr.collect(base, since=T("2026-10-03T04:00:00Z"), until=T("2026-10-03T09:00:00Z"),
                      now=T("2026-10-05T00:00:00Z"), pair="BTCUSDT")
    av = data["per_pair"]["BTCUSDT"]["availability"]
    assert (av["cycles"], av["closed_cycles"], av["open_cycles"]) == (20, 12, 8)


# the XAU engine's screen lines around the daily reopen of 2026-09-28 (production logs/XAUUSD/engine.jsonl, the msg
# cut after the trigger): the market closes 21:00 and reopens 22:00 UTC; the first screen after it is 22:15:07
XAU_REOPEN_LOG = [
    ("2026-09-28T20:51:33.579+00:00", "XAUUSD 15m close 2026-09-28T20:30:00.000Z: trigger=True (review) review "
                                      "condition: 15m close below 4115.29"),
    ("2026-09-28T22:15:07.071+00:00", "XAUUSD 15m close 2026-09-28T22:00:00.000Z: trigger=True (review) next_review "
                                      "time reached (30 min after 2026-09-28T17:25:56.541Z)"),
    ("2026-09-28T22:20:06.868+00:00", "XAUUSD 5m close 2026-09-28T22:15:00.000Z: trigger=False (none) price inside "
                                      "15m bearish order_block 4116.46-4147.94"),
    ("2026-09-28T22:25:07.503+00:00", "XAUUSD 5m close 2026-09-28T22:20:00.000Z: trigger=False (none) price inside "
                                      "15m bearish order_block 4116.46-4147.94")]


def test_the_first_cycle_after_a_reopen_is_not_lost_the_engine_waits_for_its_first_bar(dr, tmp_path):
    """The real reopen of 2026-09-28: at 22:00 the calendar is open but the decision bar (21:45) lies in the daily
    break, so the engine logs nothing before 22:15 — by design, not a loss. Before the fix the 22:00 cycle was
    'no_screen' (reproduced on production: open 1, lost 1). A later cycle without a screen still is lost."""
    base = base_at(tmp_path)
    sp, _ = system_db(dr, base, "XAUUSD")
    logs = sp.paths.logs()
    logs.mkdir(parents=True)
    (logs / "engine.jsonl").write_text("\n".join(json.dumps({"ts": ts, "level": "INFO", "proc": "engine",
                                                             "logger": "engine", "msg": msg})
                                                 for ts, msg in XAU_REOPEN_LOG) + "\n", encoding="utf-8")
    now = T("2026-09-29T00:00:00Z")
    av = dr.collect(base, since=T("2026-09-28T21:00:00Z"), until=T("2026-09-28T22:15:00Z"), now=now,
                    pair="XAUUSD")["per_pair"]["XAUUSD"]["availability"]
    assert av["calendar"] == "ny_metals_fx" and (av["open_cycles"], av["reopen_waits"]) == (1, 1)
    assert av["lost"] == {} and av["lost_total"] == 0 and av["share"] == 1.0
    assert any("first cycle(s) after a reopen" in n for n in av["notes"])
    av = dr.collect(base, since=T("2026-09-28T21:00:00Z"), until=T("2026-09-28T22:45:00Z"), now=now,
                    pair="XAUUSD")["per_pair"]["XAUUSD"]["availability"]
    assert av["open_cycles"] == 3 and av["lost"] == {"no_screen": 1}          # 22:30 has no line; 22:15 has three
    _, db = system_db(dr, base, "XAUUSD")                                       # a stop over the reopen: only 22:15
    add_events(db, [{"ts": T("2026-09-28T21:30:00Z"), "collector": "supervisor:all", "event": "stopped"},
                    {"ts": T("2026-09-28T22:20:00Z"), "collector": "supervisor:engine", "event": "started"}])
    av = dr.collect(base, since=T("2026-09-28T21:00:00Z"), until=T("2026-09-28T22:45:00Z"), now=now,
                    pair="XAUUSD")["per_pair"]["XAUUSD"]["availability"]
    assert av["lost"] == {"stopped": 1, "no_screen": 1} and av["lost_total"] == 2


def test_a_cycle_held_back_with_calls_left_is_rationed_not_lost(dr, tmp_path):
    """The engine logs F8 rationing and the pre-12:00 reserve as 'ai_quota' events while calls are LEFT: deliberate
    budget allocation, reported as 'rationed' and never counted against the availability threshold. Only a quota event
    with no call left (the cap used up) is a lost cycle (checkpoint-A re-review)."""
    base = base_at(tmp_path)
    _, db = system_db(dr, base, "BTCUSDT")
    add_events(db, [
        {"ts": T("2026-09-30T06:05:10Z"), "collector": "engine", "event": "ai_quota",
         "detail": "BTCUSDT: 18/30 calls used today — the 12 left are kept for 12:00–21:00 UTC (not analysed: strong)"},
        {"ts": T("2026-09-30T06:20:10Z"), "collector": "engine", "event": "ai_quota",
         "detail": "claude_code: 5/30 requests left today — not analysed: BTCUSDT"},
        {"ts": T("2026-09-30T06:35:10Z"), "collector": "engine", "event": "ai_quota",
         "detail": "claude_code: 0/30 requests left today — not analysed: BTCUSDT"}])
    av = dr.collect(base, since=T("2026-09-30T06:00:00Z"), until=T("2026-09-30T07:00:00Z"),
                    now=T("2026-09-30T08:00:00Z"), pair="BTCUSDT")["per_pair"]["BTCUSDT"]["availability"]
    assert av["open_cycles"] == 4 and av["rationed"] == 2
    assert av["lost"] == {"ai_quota": 1} and av["lost_total"] == 1 and av["share"] == 0.75
    assert any("held back with calls left" in n for n in av["notes"])
    assert dr.calls_left("the 12 left are kept for") == 12 and dr.calls_left("quota exhausted") is None


# --------------------------------------------------------------------------- gate, SL, outcomes
def test_gate_rejections_are_classified_with_the_min_lot_split_and_not_gated(dr, tmp_path):
    base = base_at(tmp_path)
    sp, db = system_db(dr, base, "ETHUSDT")
    since, until = T("2026-09-28T00:00:00Z"), T("2026-09-29T00:00:00Z")
    store = DecisionStore(db, config_hash="c" * 16)
    ids = [idea(store, "ETHUSDT", since + (i + 1) * MS_PER_HOUR, "BUY" if i % 2 else "SELL") for i in range(7)]
    outside = idea(store, "ETHUSDT", until + MS_PER_HOUR)
    store.close()

    def gate(*failed):
        return {"gate": [{"check": "stop_loss_present", "ok": True, "detail": "SL present"},
                         *({"check": c, "ok": False, "detail": d} for c, d in failed)]}
    # real details from production (2026-09-27): the minimum-lot refusal and the correlated cap
    minlot = ("position_size", "0.0 lots, risk 3.14% ($3.26) — minimum lot 0.01 risks 3.14% of equity > max 3.00%")
    corr = ("correlated_exposure", "group ['BTCUSDT', 'ETHUSDT']: 4.63% open+new SL risk (≤ 4.0%), of which 2.54% "
                                   "held by the other pairs' systems")
    rows = [(ids[0], "rejected", gate(minlot)),
            (ids[1], "rejected", gate(("position_size", "0.0 lots, risk 0.00% ($0.00) — invalid equity or zero stop "
                                                        "distance"))),
            (ids[2], "rejected", gate(("rr_after_costs", "1.20 < 1.5"), corr)),
            (ids[3], "rejected", {"reason": "no execution quote"}),
            (ids[4], "expired", {"reason": "expired (valid until …)"}),
            (ids[5], "executed", gate()),
            (ids[6], "executed", {"mode": "demo"}),                             # no gate record: SL not proven
            (outside, "rejected", gate(minlot))]
    con = sqlite3.connect(db)
    for did, state, det in rows:
        con.execute("UPDATE ai_decisions SET execution_state=?, execution_detail=? WHERE id=?",
                    (state, json.dumps(det), did))
    con.commit()
    con.close()
    data = dr.collect(base, since=since, until=until, now=until, pair="ETHUSDT")
    g = data["per_pair"]["ETHUSDT"]["gate"]
    assert g["rejected"] == 4 and g["expired"] == 1                              # the idea after 'until' is out
    assert g["by_class"] == {"position_size_min_lot": 1, "position_size_other": 1, "rr_after_costs": 1,
                             "correlated_exposure": 1, "not_gated: no execution quote": 1}
    assert g["by_idea"]["correlated_exposure + rr_after_costs"] == 1
    assert "minimum lot 0.01 risks 3.14%" in g["examples"]["position_size_min_lot"]
    assert data["per_pair"]["ETHUSDT"]["sl_proof"] == {"placed": 2, "sl_not_proven": 1, "ids": [ids[6][:8]]}
    md = dr.render(data)
    assert "gate rejections 4 by class:" in md and "gate class position_size_min_lot: e.g." in md


def test_an_order_refused_after_a_passed_gate_is_not_placed_with_the_backends_reason(dr, tmp_path):
    """The executor stores a broker refusal after an approved gate as the gate list (all ok) + ``backend`` with the
    reason, and no top-level ``reason``: the class names that reason instead of 'not_gated: no reason recorded'."""
    from tradingsystem.execution.backends.mt5_backend import tag
    from tradingsystem.execution.retcodes import describe
    base = base_at(tmp_path)
    sp, db = system_db(dr, base, "ETHUSDT")
    since, until = T("2026-09-28T00:00:00Z"), T("2026-09-29T00:00:00Z")
    store = DecisionStore(db, config_hash="c" * 16)
    refused, quoteless = idea(store, "ETHUSDT", since + MS_PER_HOUR), idea(store, "ETHUSDT", since + 2 * MS_PER_HOUR)
    store.close()
    why = f"order_send {tag(refused, 0)}: {describe(10019)}"                   # as MT5Backend.place words it
    passed = {"gate": [{"check": "stop_loss_present", "ok": True, "detail": "SL present"},
                       {"check": "position_size", "ok": True, "detail": "0.01 lots, risk 1.20% ($1.22)"}],
              "backend": {"ok": False, "reason": why, "attempts": [], "placed": [], "note": None}, "mode": "demo"}
    con = sqlite3.connect(db)
    for did, det in ((refused, passed), (quoteless, {"reason": "no execution quote"})):
        con.execute("UPDATE ai_decisions SET execution_state='rejected', execution_detail=? WHERE id=?",
                    (json.dumps(det), did))
    con.commit()
    con.close()
    g = dr.collect(base, since=since, until=until, now=until, pair="ETHUSDT")["per_pair"]["ETHUSDT"]["gate"]
    cls = f"not_placed: {dr._short(why, 80)}"
    assert "10019 NO_MONEY: not enough money" in why
    assert g["by_class"] == {cls: 1, "not_gated: no execution quote": 1}
    assert g["examples"][cls].endswith(f"gate passed, the order refused: {dr._short(why, 140)}")
    assert not any(k.startswith("not_gated: no reason recorded") for k in g["by_class"])


def test_realised_counts_outcomes_settled_in_the_window_and_unknown_costs_stay_unknown(dr, tmp_path):
    base = base_at(tmp_path)
    sp, db = system_db(dr, base, "BTCUSDT")
    since, until = T("2026-09-28T00:00:00Z"), T("2026-09-29T00:00:00Z")
    store = DecisionStore(db, config_hash="c" * 16)
    before = idea(store, "BTCUSDT", since - 5 * MS_PER_HOUR)                   # made before, settled inside
    inside = idea(store, "BTCUSDT", since + MS_PER_HOUR, "SELL")
    late = idea(store, "BTCUSDT", since + 2 * MS_PER_HOUR)                      # settled after 'until'
    store.close()
    con = sqlite3.connect(db)
    for did, oc, pnl, ots, det in (
            (before, "closed_profit", 2.55, since + 2 * MS_PER_HOUR, None),     # before Phase 4: no split stored
            (inside, "closed_loss", -2.52, since + 3 * MS_PER_HOUR,
             {"venue": "mt5", "commission": -0.12, "swap": -0.05, "fee": 0.0, "open_ms": since, "close_ms": since}),
            (late, "closed_profit", 9.0, until + MS_PER_HOUR, None)):
        con.execute("UPDATE ai_decisions SET execution_state='executed', outcome=?, outcome_pnl_usd=?, outcome_ts=?, "
                    "outcome_detail=? WHERE id=?", (oc, pnl, ots, json.dumps(det) if det else None, did))
    con.commit()
    con.close()
    data = dr.collect(base, since=since, until=until, now=until + MS_PER_DAY, pair="BTCUSDT")
    rl = data["per_pair"]["BTCUSDT"]["realised"]
    assert (rl["n"], rl["wins"], rl["losses"], rl["pnl_usd"]) == (2, 1, 1, 0.03)
    assert (rl["commission"], rl["commission_n"], rl["swap"], rl["swap_n"], rl["fee"], rl["fee_n"]) == \
        (-0.12, 1, -0.05, 1, 0.0, 1)
    assert [(t["id"], t["cum_usd"]) for t in data["equity_path"]] == [(before[:8], 2.55), (inside[:8], 0.03)]
    md = dr.render(data)
    assert "| BTCUSDT | 2 | 1 / 1 | +0.03 | -0.1200 (1) | -0.0500 (1) | 0.0000 (1) |" in md


# --------------------------------------------------------------------------- incidents
def test_monitor_findings_become_episodes_with_first_detection_onset_and_state_alerts(dr, tmp_path):
    """The real monitor log of the first demo night: the 00:22 Binance outage (first detection 00:35, 13.5 min
    after the onset the finding names), three separate low-RAM episodes, the runs every 15 min."""
    fx = json.loads((REAL / "demo_window_btc_2026-09-28.json").read_text(encoding="utf-8"))
    logs = tmp_path / "logs"
    logs.mkdir()
    extra = [json.dumps({"ts": "2026-09-28T07:00:00.000+00:00", "level": "WARNING", "logger": "monitor",
                         "msg": "[critical] ETHUSDT: position without SL: decision abc has no stop-loss | ETHUSDT "
                                "kill switch written (already notified)"})]
    (logs / "monitor.jsonl").write_text("\n".join(fx["monitor_log"] + extra) + "\n", encoding="utf-8")
    since, until = T("2026-09-27T21:50:00Z"), T("2026-09-28T08:30:00Z")
    mon = dr.monitor_log(logs, since, until)
    runs = sum(1 for ln in fx["monitor_log"] if json.loads(ln)["msg"].startswith("monitor run:")
               and since <= dr._ts_ms(json.loads(ln)["ts"]) < until)
    assert mon["runs"] == runs and mon["max_gap_min"] <= 16 and mon["ram_min_mb"] == 147
    by = collections.defaultdict(list)
    for e in mon["episodes"]:
        by[e["title"]].append(e)
    assert len(by["Low free RAM"]) == 3                                          # 22:20, 01:20–01:50, 06:06
    out = by["BTCUSDT: connection outage"][0]
    assert iso(out["onset_ms"]) == "2026-09-28T00:22:19.854Z" and out["n"] > 10
    sl = by["ETHUSDT: position without SL"][0]
    assert sl["level"] == "critical" and sl["text"] == "decision abc has no stop-loss"
    # the state: an earlier first detection moves the episode; an alert the log no longer holds is added
    state = {"alerts": {**fx["monitor_state_alerts"],
                        "stale:XAUUSD:engine": {"level": "warn", "first_ms": T("2026-09-28T03:00:00Z"),
                                                "last_ms": T("2026-09-28T03:30:00Z"), "title": "XAUUSD: stale heartbeat"},
                        "info:vpn": {"level": "info", "first_ms": T("2026-09-28T03:00:00Z"), "title": "VPN up"}}}
    moved = out["first_ms"] - 10 * MS_PER_MINUTE
    state["alerts"]["outage:BTCUSDT:binance_spot:spot@1790554939854"]["first_ms"] = moved
    dr.merge_state_alerts(mon["episodes"], state, since, until)
    assert out["first_ms"] == moved and out["source"] == "monitor.jsonl + state"
    titles = [e["title"] for e in mon["episodes"]]
    assert "XAUUSD: stale heartbeat" in titles and "VPN up" not in titles        # info alerts are not incidents


def test_one_sleep_recorded_by_every_system_is_one_incident_row(dr, tmp_path):
    base = base_at(tmp_path)
    resumed = T("2026-09-27T19:24:52Z")
    for pair, jitter in (("BTCUSDT", 0), ("ETHUSDT", 900)):
        _, db = system_db(dr, base, pair)
        add_events(db, [{"ts": resumed + jitter, "collector": "supervisor:all", "event": "system_suspend",
                         "detail": "PC was asleep for ~3122s", "duration_ms": 3_122_207},
                        *({"ts": T("2026-09-27T22:16:33Z") + jitter, "collector": f"supervisor:{svc}",
                           "event": "started", "detail": "pid 1"} for svc in ("engine", "executor", "api"))])
    systems = dr.report_systems(base)
    rows = dr.system_events(systems, T("2026-09-27T12:00:00Z"), T("2026-09-28T00:00:00Z"))
    assert [r["title"] for r in rows] == ["PC asleep ~52 min", "services started (restart)"]
    sleep, start = rows
    assert sleep["systems"] == ["BTCUSDT", "ETHUSDT"] and sleep["first_ms"] == resumed - 3_122_207
    assert start["systems"] == ["BTCUSDT", "ETHUSDT"] and start["n"] == 6


SERVICES = ("ingest-binance", "ingest-mt5", "engine", "executor", "api")
EXIT_SESSION = "code 1073807364 (0x40010004) after 206s; restart in 4s"


def _start(at: str, svcs=SERVICES) -> list[tuple]:
    return [(at, svc, "started", "pid 1") for svc in svcs]


# the supervisors' rows of production 2026-09-26 → 09-28 (data/instances/*/app.db, read-only): the Start-menu shutdown
# of 09-27 06:10 (Fast Startup: boot 09:11, psutil's boot time unchanged) left no row — BTC and ETH simply started
# again at 09:12/09:13 (XAU had been stopped cleanly at 09-26 23:22); stop_all/restart_all write 'supervisor:all
# stopped'; the kills after the 20:44 resume are supervised restarts; the 20:54 session end (0x40010004 exits) and
# the cold boot at 21:38 (Kernel-Power 41) end in the 21:40-21:41 starts
SUPERVISOR_ROWS = {
    "BTCUSDT": [*_start("2026-09-26T22:13:47Z"), *_start("2026-09-27T09:12:48Z"),
                ("2026-09-27T09:14:11Z", "all", "stopped", "supervisor shutdown"), *_start("2026-09-27T09:18:30Z"),
                ("2026-09-27T19:24:52Z", "all", "system_suspend", "PC was asleep for ~3122s"),
                ("2026-09-27T21:26:25Z", "all", "stopped", "supervisor shutdown"), *_start("2026-09-27T22:16:33Z"),
                ("2026-09-28T16:32:32Z", "all", "system_suspend", "PC was asleep for ~9711s"),
                ("2026-09-28T20:44:41Z", "all", "system_suspend", "PC was asleep for ~2756s"),
                ("2026-09-28T20:47:41Z", "ingest-mt5", "killed", "heartbeat unchanged for 116s"),
                ("2026-09-28T20:47:41Z", "executor", "killed", "heartbeat unchanged for 116s"),
                *_start("2026-09-28T20:47:46Z", ("ingest-mt5", "executor")),
                ("2026-09-28T20:54:19Z", "ingest-mt5", "exited", EXIT_SESSION),
                ("2026-09-28T20:54:19Z", "executor", "exited", EXIT_SESSION), *_start("2026-09-28T21:40:09Z")],
    "ETHUSDT": [*_start("2026-09-26T22:14:00Z"), *_start("2026-09-27T09:13:26Z"),
                ("2026-09-27T09:14:29Z", "all", "stopped", "supervisor shutdown"), *_start("2026-09-27T09:18:44Z"),
                ("2026-09-27T21:26:43Z", "all", "stopped", "supervisor shutdown"), *_start("2026-09-27T22:16:45Z"),
                ("2026-09-28T20:44:40Z", "all", "system_suspend", "PC was asleep for ~2756s"),
                ("2026-09-28T20:50:48Z", "executor", "killed", "heartbeat unchanged for 181s"),
                *_start("2026-09-28T20:50:53Z", ("executor",)), *_start("2026-09-28T21:41:05Z")],
    "XAUUSD": [*_start("2026-09-26T22:14:19Z"), ("2026-09-26T22:17:22Z", "ingest-binance", "killed", "heartbeat"),
               *_start("2026-09-26T22:17:27Z", ("ingest-binance",)),
               ("2026-09-26T23:22:08Z", "all", "stopped", "supervisor shutdown"), *_start("2026-09-27T09:19:12Z"),
               ("2026-09-27T21:27:02Z", "all", "stopped", "supervisor shutdown"), *_start("2026-09-27T22:17:04Z"),
               *((("2026-09-28T20:54:18Z", svc, "exited", EXIT_SESSION) for svc in SERVICES)),
               *_start("2026-09-28T21:41:36Z")]}


def test_supervisor_starts_after_a_run_without_a_stop_are_the_shutdowns_the_boot_time_misses(dr, tmp_path):
    base = base_at(tmp_path)
    for pair, rows in SUPERVISOR_ROWS.items():
        _, db = system_db(dr, base, pair)
        add_events(db, [{"ts": T(at), "collector": f"supervisor:{svc}", "event": ev, "detail": det}
                        for at, svc, ev, det in rows])
    systems = dr.report_systems(base)
    out = dr.unclean_starts(systems, T("2026-09-26T20:00:00Z"), T("2026-09-29T06:00:00Z"))
    assert [(u["first"], u["systems"], u["services"]) for u in out] == [
        ("2026-09-27T09:12:48.000Z", ["BTCUSDT", "ETHUSDT"], 10),                 # the Fast Startup shutdown
        ("2026-09-28T21:40:09.000Z", ["BTCUSDT", "ETHUSDT", "XAUUSD"], 15)]       # the cold boot after 20:54
    assert out[0]["previous"] == "ingest-binance: started 2026-09-26T22:13:47.000Z"
    demo = dr.unclean_starts(systems, T("2026-09-27T21:50:00Z"), T("2026-09-29T06:00:00Z"))    # the D-047 window
    assert [u["first"] for u in demo] == ["2026-09-28T21:40:09.000Z"]
    # a quick reboot after the session ended is no supervised restart (the 0x40010004 exits): XAU alone, 3 min later
    xau = [(p, sp) for p, sp in systems if p == "XAUUSD"]
    _, xdb = system_db(dr, base, "XAUUSD")
    add_events(xdb, [*({"ts": T("2026-09-29T08:00:00Z"), "collector": f"supervisor:{s}", "event": "exited",
                        "detail": EXIT_SESSION} for s in SERVICES),
                     *({"ts": T("2026-09-29T08:03:00Z"), "collector": f"supervisor:{s}", "event": "started",
                        "detail": "pid 2"} for s in SERVICES),
                     # a kill, a sleep, the restart after the resume: the supervisor lived through it
                     {"ts": T("2026-09-29T09:00:00Z"), "collector": "supervisor:engine", "event": "killed"},
                     {"ts": T("2026-09-29T10:00:00Z"), "collector": "supervisor:all", "event": "system_suspend",
                      "duration_ms": 3_500_000},
                     {"ts": T("2026-09-29T10:00:05Z"), "collector": "supervisor:engine", "event": "started"}])
    late = dr.unclean_starts(xau, T("2026-09-29T06:00:00Z"), T("2026-09-29T12:00:00Z"))
    assert [(u["first"], u["services"]) for u in late] == [("2026-09-29T08:03:00.000Z", 5)]
    data = dr.collect(base, since=T("2026-09-27T21:50:00Z"), until=T("2026-09-29T06:00:00Z"),
                      now=T("2026-09-29T06:00:00Z"))
    assert [u["first"] for u in data["unclean_starts"]] == ["2026-09-28T21:40:09.000Z"]
    assert "Supervisor starts after a run that ended without a stop" in dr.render(data) and \
        "09-28 21:40 BTCUSDT, ETHUSDT, XAUUSD (15 services" in dr.render(data)


def _win_event(provider: str, eid: int, at: str, **data: str) -> str:
    """One event as ``wevtutil qe System /f:xml`` prints it (the elements the report reads)."""
    fields = "".join(f"<Data Name='{k}'>{v}</Data>" for k, v in data.items())
    return (f"<Event xmlns='http://schemas.microsoft.com/win/2004/08/events/event'><System><Provider Name='{provider}'"
            f"/><EventID>{eid}</EventID><TimeCreated SystemTime='{at}'/><Channel>System</Channel></System><EventData>"
            f"{fields}</EventData></Event>")


# the production laptop's System log 2026-09-26 → 09-28 (read-only query, values as logged; newest first, /rd:true)
SYSTEM_LOG = [
    _win_event("Microsoft-Windows-Kernel-Power", 41, "2026-09-28T21:38:34.1234567Z"),
    _win_event("Microsoft-Windows-Kernel-Boot", 27, "2026-09-28T21:38:31.5000000Z", BootType="0"),
    _win_event("Microsoft-Windows-Kernel-General", 12, "2026-09-28T21:38:31.5000000Z",
               StartTime="2026-09-28T21:38:31.5000000Z"),
    _win_event("Microsoft-Windows-Kernel-Boot", 27, "2026-09-28T20:44:37.0000000Z", BootType="2"),
    _win_event("Microsoft-Windows-Kernel-Power", 107, "2026-09-28T19:58:40.0000000Z", TargetState="5"),
    _win_event("Microsoft-Windows-Kernel-Power", 42, "2026-09-28T19:58:39.0000000Z", TargetState="5", Reason="2"),
    _win_event("Microsoft-Windows-Kernel-Boot", 27, "2026-09-27T09:11:09.0000000Z", BootType="1"),
    _win_event("Microsoft-Windows-Kernel-Power", 107, "2026-09-27T06:10:57.0000000Z", TargetState="6"),
    _win_event("Microsoft-Windows-Kernel-Power", 42, "2026-09-27T06:10:56.0000000Z", TargetState="6", Reason="4"),
    _win_event("User32", 1074, "2026-09-27T06:10:49.0000000Z",
               param1="C:\\WINDOWS\\SystemApps\\Microsoft.Windows.StartMenuExperienceHost_cw5n1h2txyewy\\"
                      "StartMenuExperienceHost.exe (DESKTOP-M8JTHQ2)", param3="Other (Unplanned)", param5="power off"),
    _win_event("Microsoft-Windows-Kernel-Boot", 27, "2026-09-26T20:18:45.0000000Z", BootType="2")]


def test_power_events_read_the_system_log_best_effort_and_group_its_episodes(monkeypatch):
    m = tool("demo_report", "demo_report_power_under_test")
    calls: list = []

    def run(args, **kw):
        calls.append((args, kw))
        return subprocess.CompletedProcess(args, 0, "".join(SYSTEM_LOG).encode("utf-8"), b"")
    monkeypatch.setattr(m, "os", types.SimpleNamespace(name="nt"))
    monkeypatch.setattr(m.subprocess, "run", run)
    out = m.power_events(T("2026-09-26T20:00:00Z"), T("2026-09-29T06:00:00Z"))
    args, kw = calls[0]
    assert args[:3] == ["wevtutil", "qe", "System"] and kw["timeout"] == m.POWER_LOG_TIMEOUT_S
    assert "@SystemTime>='2026-09-26T20:00:00.000Z'" in args[3] and "EventID=1074" in args[3]
    assert not {"cl", "clear-log", "sl", "set-log"} & set(args)                         # a query, nothing else
    assert out["error"] is None and out["counts"] == {"shutdown": 1, "boot": 2, "sleep": 2}
    eps = [(e["first"][5:16], e["kind"], e["what"]) for e in out["episodes"]]
    assert eps == [
        ("09-26T20:18", "sleep", "resumed from hibernate (asleep since before the window)"),
        ("09-27T06:10", "shutdown", "power off requested by StartMenuExperienceHost.exe (Other (Unplanned)); hybrid "
                                    "shutdown (Fast Startup)"),
        ("09-27T09:11", "boot", "Fast Startup boot (after a Start-menu shut down)"),
        ("09-28T19:58", "sleep", "hibernate"),
        ("09-28T21:38", "boot", "cold boot; the OS started; rebooted without a clean shutdown (Kernel-Power 41)")]
    assert out["episodes"][3]["until"] == "2026-09-28T20:44:37.000Z"
    # never fails its caller: a timeout, a refused query, not Windows
    def slow(args, **kw):
        raise subprocess.TimeoutExpired(args, kw["timeout"])
    monkeypatch.setattr(m.subprocess, "run", slow)
    assert m.power_events(0, 1)["error"].startswith("not read: TimeoutExpired")
    monkeypatch.setattr(m.subprocess, "run", lambda a, **kw: subprocess.CompletedProcess(a, 5, b"", b"Access denied"))
    assert m.power_events(0, 1) == {"episodes": [], "counts": {"shutdown": 0, "boot": 0, "sleep": 0},
                                    "error": "not read: wevtutil exit 5: Access denied"}
    monkeypatch.setattr(m, "os", types.SimpleNamespace(name="posix"))
    assert m.power_events(0, 1)["error"] == "not read (not Windows)"


# --------------------------------------------------------------------------- the ledger
REAL_ROW = {"ts": T("2026-09-28T07:45:48Z"), "provider": "claude_code", "model": "claude-sonnet-5", "role": "decision",
            "pair": "ETHUSDT", "input_tokens": 25111, "cached_tokens": 2241, "cache_creation_tokens": 15620,
            "output_tokens": 9489, "ok": 1, "error": None, "api_equivalent_usd": 0.0867918}     # production row 158


def test_cost_per_day_prices_only_known_models_and_weights_cache_reads(dr, tmp_path):
    s = base_at(tmp_path)
    rows = [REAL_ROW,
            {**REAL_ROW, "model": "opus", "role": "review", "pair": None, "api_equivalent_usd": 1.2},  # an alias
            {**REAL_ROW, "input_tokens": 0, "cached_tokens": 0, "cache_creation_tokens": None, "output_tokens": 0,
             "ok": 0, "error": "claude_code: no answer within 180s", "api_equivalent_usd": 0.0},
            {**REAL_ROW, "role": "review", "pair": None, "input_tokens": 0, "cached_tokens": 0, "output_tokens": 0,
             "error": USAGE_UNKNOWN_PREFIX + " session timed out", "api_equivalent_usd": 0.0},
            {**REAL_ROW, "provider": "gemini", "model": "gemini-3.8-flash", "ts": REAL_ROW["ts"] + MS_PER_DAY,
             "cache_creation_tokens": 0}]
    c = dr.cost_per_day(rows, s)
    day = c["days"]["2026-09-28"]
    price = s.ai.providers["anthropic"].model_prices["claude-sonnet-5"]
    assert tuple(price) == (2.0, 10.0)
    one = (7250 * 2.0 + 2241 * 2.0 * 0.1 + 15620 * 2.0 * 1.25 + 9489 * 10.0) / 1e6          # fresh = 25111-2241-15620
    assert dr.api_usd(REAL_ROW, price) == pytest.approx(one)
    assert (day["calls"], day["ok"], day["priced_n"], day["with_tokens_n"]) == (4, 3, 1, 2)
    assert day["api_usd"] == round(one, 2) and day["unpriced_models"] == {"opus": 1} and day["usage_unknown"] == 1
    weighted = (25111 - 2241) + 2241 * s.ai.usage.cache_read_weight + 9489
    assert day["weighted"] == int(round(2 * weighted))                          # both claude rows with tokens
    assert day["cli_usd"] == round(0.0867918 + 1.2, 2) and day["by_role"] == {"decision": 2, "review": 2}
    nxt = c["days"]["2026-09-29"]
    assert nxt["weighted"] == 0 and nxt["priced_n"] == 1                        # gemini: priced, not the gauge's
    assert c["total"]["calls"] == 5 and c["total"]["cache_read_share"] == round(2241 * 3 / (25111 * 3), 3)


def test_session_limit_rows_within_ten_minutes_are_one_event_and_calls_count_against_the_cap(dr, tmp_path):
    s = base_at(tmp_path)
    err = "claude_code: subscription usage limit (You've hit your session limit · resets 12am (Asia/Beirut))"
    t0 = T("2026-09-26T20:45:12Z")                                              # production: BTC and ETH, same second
    rows = [{**REAL_ROW, "ts": t0, "pair": "BTCUSDT", "ok": 0, "error": err},
            {**REAL_ROW, "ts": t0, "pair": "ETHUSDT", "ok": 0, "error": err},
            {**REAL_ROW, "ts": t0 + 2 * MS_PER_HOUR, "pair": "BTCUSDT", "ok": 0, "error": err}]
    sl = dr.session_limits(rows)
    assert sl["rows"] == 3 and [(e["rows"], e["pairs"]) for e in sl["events"]] == [(2, ["BTCUSDT", "ETHUSDT"]),
                                                                                  (1, ["BTCUSDT"])]
    day = [{**REAL_ROW, "pair": "BTCUSDT"}] * 3 + [
        {**REAL_ROW, "pair": "BTCUSDT", "role": "escalation"},
        {**REAL_ROW, "pair": None, "role": "review"}, {**REAL_ROW, "pair": "BTCUSDT", "role": "diagnose"},
        {**REAL_ROW, "pair": "BTCUSDT", "provider": "gemini"},
        {**REAL_ROW, "pair": "BTCUSDT", "ts": REAL_ROW["ts"] + MS_PER_DAY}]
    assert dr.calls_per_day(day, "BTCUSDT", s) == {"2026-09-28": 4, "2026-09-29": 1}


# --------------------------------------------------------------------------- the whole report
def seed_pair(dr, base, pair: str, since: int, until: int) -> Path:
    sp, db = system_db(dr, base, pair)
    app = AppDB(db)
    app.set_status("engine", "live", detail={"quota_per_day": 30, "snapshot_build_ms": {"median": 560, "n": 20}})
    app.set_status("executor", "live", detail={"mode": "demo", "equity": 101.33, "balance": 101.33, "exposure": [],
                                               "account_drawdown": {"drawdown_pct": 2.45, "tripped": None}})
    app.close()
    store = DecisionStore(db, config_hash="c" * 16)
    for i in range(6):
        store.save(DecisionRecord(pair=pair, mode="agent_per_pair", trigger="review", status="valid",
                                  ts=since + (i + 1) * MS_PER_HOUR, prompt_hash="p" * 16, library_hash="l" * 16,
                                  recommendation={"decision": "NO_TRADE", "confidence": 40}))
    store.save(DecisionRecord(pair=pair, mode="agent_per_pair", trigger="review", status="valid", ts=until + 1,
                              recommendation={"decision": "NO_TRADE", "confidence": 40}))       # after the window
    store.close()
    base.paths.shared().mkdir(parents=True, exist_ok=True)
    u = UsageStore(base.paths.shared() / "ai_usage.db")
    for i in range(6):
        u.record(LLMResult("claude_code", "claude-sonnet-5", "", None, input_tokens=25_111, output_tokens=9_489,
                           cached_input_tokens=2_241), provider="claude_code", model="claude-sonnet-5",
                 purpose="agent_per_pair", pair=pair, ok=True, role="decision")
    u.close()
    con = sqlite3.connect(base.paths.shared() / "ai_usage.db")                  # the ledger stamps now: move them in
    con.execute("UPDATE ai_usage SET ts=? WHERE pair=?", (since + 2 * MS_PER_HOUR, pair))
    con.commit()
    con.close()
    return db


def test_the_report_is_read_only_bounded_to_the_window_and_written_by_the_cli(dr, tmp_path, monkeypatch):
    base = base_at(tmp_path)
    since, until = T("2026-09-20T00:00:00Z"), T("2026-09-20T12:00:00Z")      # a closed window: complete
    for pair in ("BTCUSDT", "ETHUSDT"):
        seed_pair(dr, base, pair, since, until)
    peak = base.paths.shared() / "account_peak.json"
    peak.write_text(json.dumps({"mt5:WindsorBrokers1-Demo:1": {"peak": 103.87, "peak_ms": since}}), encoding="utf-8")
    before = _digest(tmp_path / "data")
    real = dr.load_settings
    monkeypatch.setattr(dr, "load_settings", lambda **kw: real(**kw) if kw else base)
    out, js = tmp_path / "runs" / "demo.md", tmp_path / "runs" / "demo.json"
    assert dr.main(["--since", iso(since), "--until", iso(until), "--out", str(out), "--json", str(js)]) == dr.EXIT_OK
    assert _digest(tmp_path / "data") == before                                     # nothing under data/ changed
    md = out.read_text(encoding="utf-8")
    data = json.loads(js.read_text(encoding="utf-8"))
    assert data["systems"] == ["BTCUSDT", "ETHUSDT"] and data["window"]["complete"]
    f = data["per_pair"]["BTCUSDT"]["funnel"]
    assert f["calls_stored"] == 6 and f["decisions"] == {"NO_TRADE": 6}             # the one after 'until' is out
    assert data["per_pair"]["BTCUSDT"]["calls_by_role"]["decision"]["calls"] == 6
    assert data["per_pair"]["BTCUSDT"]["cap"] == {"value": 30, "source": "the engine status (quota_per_day)"}
    assert data["per_pair"]["BTCUSDT"]["hashes_seen"]["prompt_hash"][0] == {
        "value": "p" * 16, "n": 6, "first": iso(since + MS_PER_HOUR), "last": iso(since + 6 * MS_PER_HOUR)}
    assert "slots" not in data["per_pair"]["BTCUSDT"]["screens"]
    assert data["execution_mode"].startswith("demo")
    for text in ("# Demo report — 2026-09-20T00:00:00.000Z → 2026-09-20T12:00:00.000Z (complete)",
                 "## Summary per pair", "## Funnel and execution quality per pair", "## Availability",
                 "## Incidents", "## AI cost per day", "## Versions seen in the window", "## Tuning changes",
                 "High-water mark mt5:WindsorBrokers1-Demo:1: peak 103.87", "MT5 deals history: not read"):
        assert text in md, text
    assert md.count("**Sample size.**") == 2 and "not a statistical edge" in md
    assert dr.main(["--pair", "DOGEUSDT", "--print"]) == dr.EXIT_INVALID
    assert dr.main(["--since", "yesterday-ish", "--print"]) == dr.EXIT_INVALID
    assert dr.main(["--root", str(tmp_path / "nowhere"), "--print"]) == dr.EXIT_INVALID


def test_the_docs_default_is_refused_in_a_checkout_that_runs_a_system(dr, tmp_path, monkeypatch, capsys):
    """docs/runs/demo.md written in the production checkout makes it dirty and can block the next ff-merge: with no
    --out/--print/--root and a system's app.db under this checkout's data root, the default is refused (exit 3)."""
    base = base_at(tmp_path)
    since, until = T("2026-09-20T00:00:00Z"), T("2026-09-20T12:00:00Z")
    real = dr.load_settings
    monkeypatch.setattr(dr, "load_settings", lambda **kw: real(**kw) if kw else base)
    default = tmp_path / "checkout" / "docs" / "runs" / "demo.md"
    monkeypatch.setattr(dr, "ROOT", tmp_path / "checkout")
    monkeypatch.setattr(dr, "DEFAULT_OUT", default)
    window = ["--since", iso(since), "--until", iso(until)]
    (tmp_path / "data").mkdir()
    assert dr.main(window) == dr.EXIT_OK and default.exists()                   # a worktree without a system
    default.unlink()
    seed_pair(dr, base, "ETHUSDT", since, until)                               # data/instances/ETHUSDT/app.db
    capsys.readouterr()
    assert dr.main(window) == dr.EXIT_INVALID and not default.exists()
    out = capsys.readouterr().out
    assert "would make it dirty" in out and "--out data\\reviews\\demo.md" in out
    assert dr.main([*window, "--out", str(tmp_path / "reviews" / "demo.md")]) == dr.EXIT_OK
    assert dr.main([*window, "--print"]) == dr.EXIT_OK
    assert dr.main([*window, "--root", str(tmp_path)]) == dr.EXIT_OK and default.exists()   # a worktree → production


# --------------------------------------------------------------------------- --mt5
Deal = namedtuple("Deal", "ticket magic symbol time time_msc profit commission swap fee")
Account = namedtuple("Account", "login server trade_mode currency leverage balance equity margin_free")


def fake_mt5(monkeypatch, *, server: str, deals, on_history=None) -> dict:
    calls: dict = {"initialize": 0, "history": 0, "shutdown": 0}
    m = types.ModuleType("MetaTrader5")

    def initialize(**kw):
        calls["initialize"] += 1
        return True

    def history_deals_get(a, b):
        calls["history"] += 1
        if on_history:
            on_history()
        return tuple(deals)

    m.initialize = initialize
    m.account_info = lambda: Account(1, server, 0, "USD", 100, 101.33, 101.33, 90.0)
    m.history_deals_get = history_deals_get
    m.shutdown = lambda: calls.__setitem__("shutdown", calls["shutdown"] + 1)
    m.last_error = lambda: (1, "Success")
    monkeypatch.setitem(sys.modules, "MetaTrader5", m)          # no order_send: a trade call would raise
    return calls


def test_mt5_deals_are_read_under_the_history_lock_and_never_launch_the_terminal(dr, tmp_path, monkeypatch):
    from tradingsystem.ingest.mt5 import terminal
    from tradingsystem.ingest.mt5.servertime import ServerTimeModel
    base = base_at(tmp_path)
    since, until = T("2026-09-28T00:00:00Z"), T("2026-09-29T00:00:00Z")
    model = ServerTimeModel()
    magic = base.execution.magic

    def deal(i, mg, at, profit):
        srv = model.utc_to_server(at)
        return Deal(i, mg, "ETHUSD@", srv // 1000, srv, profit, -0.06, -0.01, 0.0)
    deals = [deal(1, magic + base.instances["ETHUSDT"].magic_offset, since + MS_PER_HOUR, 1.5),
             deal(2, magic + base.instances["BTCUSDT"].magic_offset, since + 2 * MS_PER_HOUR, -2.0),
             deal(3, magic + base.instances["ETHUSDT"].magic_offset, until + MS_PER_HOUR, 5.0),   # after the window
             deal(4, 12345, since + MS_PER_HOUR, 50.0)]                                         # not ours
    calls = fake_mt5(monkeypatch, server="WindsorBrokers1-Demo", deals=deals)
    monkeypatch.setattr(terminal, "terminal_running", lambda path, min_age_s=0.0: False)
    out = dr.mt5_deals(base, since, until)
    assert "not running" in out["error"] and calls["initialize"] == 0              # never launched
    lock_path = base.paths.shared() / "locks" / "mt5_history.lock"
    held: list[bool] = []

    def probe():
        other = FileLock(lock_path)
        got = other.acquire(timeout=0)
        held.append(not got)
        if got:
            other.release()
    calls = fake_mt5(monkeypatch, server="WindsorBrokers1-Demo", deals=deals, on_history=probe)
    monkeypatch.setattr(terminal, "terminal_running", lambda path, min_age_s=0.0: True)
    out = dr.mt5_deals(base, since, until)
    assert held == [True] and calls["history"] == 1 and calls["shutdown"] >= 1
    after = FileLock(lock_path)
    assert after.acquire(timeout=0)                                                # released afterwards
    after.release()
    assert out["deals"] == 2 and out["net_usd"] == round(1.5 - 0.07 - 2.0 - 0.07, 2)
    assert out["per_pair"]["ETHUSDT"] == {"deals": 1, "profit": 1.5, "commission": -0.06, "swap": -0.01, "fee": 0.0}
    assert [x["pair"] for x in out["path"]] == ["ETHUSDT", "BTCUSDT"]
    calls = fake_mt5(monkeypatch, server="SomeOther-Real", deals=deals)
    out = dr.mt5_deals(base, since, until)
    assert out["error"].startswith("AccountMismatch") and calls["history"] == 0
