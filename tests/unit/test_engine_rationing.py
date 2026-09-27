"""Engine call rationing and liveness (ai_path F1, F2, F8, F14, F15; stall F5). The engine runs on a temporary
data directory; the AI side is replaced by awaitable stand-ins, market data comes from the real payload fixture."""
import asyncio
import json
import sqlite3
import time
from pathlib import Path

import pytest

from tradingsystem.ai.store import DecisionRecord
from tradingsystem.analysis import engine as eng_mod
from tradingsystem.analysis.engine import Engine
from tradingsystem.core.settings import load_settings

from .test_orchestrator import rec_for

T0 = 1790334600000
MIN = 60_000
PAYLOAD = json.loads((Path(__file__).resolve().parents[1] / "fixtures" / "real" / "payload_xauusd.json").read_text())


@pytest.fixture
def eng(tmp_path):
    s = load_settings(env_path=Path("nope.env"))
    s = s.model_copy(update={"paths": s.paths.model_copy(update={"data_dir": str(tmp_path)})})
    e = Engine(s)
    yield e
    e.close()


def one(e, sql, *args):
    con = sqlite3.connect(e.s.paths.data() / "app.db")
    try:
        return con.execute(sql, args).fetchone()
    finally:
        con.close()


def events(e, name):
    return one(e, "SELECT count(*) FROM ingestion_events WHERE event=?", name)[0]


def price_review(eng, monkeypatch, level=100.0):
    """A decision whose next_review waits for a price condition (met: the quote is 101/102)."""
    rec = rec_for()
    rec["next_review"] = {"in_minutes": 240, "conditions": [{"kind": "price_above", "value": level}]}
    monkeypatch.setattr(eng.builder, "quote_at", lambda inst, now: {"bid": 101, "ask": 102})
    return rec


def test_review_fires_once_per_decision(eng, monkeypatch):
    rec = price_review(eng, monkeypatch)
    eng.store.save(DecisionRecord("XAUUSD", "agent_per_pair", "t", "valid", recommendation=rec, ts=T0))
    fire, reasons, strength = eng.evaluate("XAUUSD", T0 + 31 * MIN, False, None, "hybrid")
    assert fire and strength == "review"
    eng.store.save(DecisionRecord("XAUUSD", "agent_per_pair", reasons[0], "invalid", ts=T0 + 33 * MIN))
    for t in (T0 + 40 * MIN, T0 + 90 * MIN):                       # the failed review consumed it: no re-firing
        assert not eng.evaluate("XAUUSD", t, False, None, "hybrid")[0]


def test_review_survives_a_call_that_never_answered(eng, monkeypatch):
    """Integration review: an 'error' (e.g. usage limit) or data-gate 'skipped' row does not consume the review;
    it re-fires after the floor instead of being lost."""
    rec = price_review(eng, monkeypatch)
    eng.store.save(DecisionRecord("XAUUSD", "agent_per_pair", "t", "valid", recommendation=rec, ts=T0))
    eng.store.save(DecisionRecord("XAUUSD", "agent_per_pair", "review", "error", ts=T0 + 31 * MIN))
    eng.store.save(DecisionRecord("XAUUSD", "agent_per_pair", "setup", "skipped", ts=T0 + 32 * MIN))
    assert not eng.evaluate("XAUUSD", T0 + 34 * MIN, False, None, "hybrid")[0]      # < review floor
    fire, _, strength = eng.evaluate("XAUUSD", T0 + 38 * MIN, False, None, "hybrid")
    assert fire and strength == "review"


def test_spacing_counts_from_the_dispatch_not_the_stored_answer(eng):
    """Integration review: a call answered 3 min after dispatch must not push the next 15m close out of reach."""
    eng.last_call["XAUUSD"] = T0
    eng.store.save(DecisionRecord("XAUUSD", "agent_per_pair", "t", "valid", recommendation=rec_for(), ts=T0 + 3 * MIN))
    assert eng.evaluate("XAUUSD", T0 + 15 * MIN + 5_000, True, PAYLOAD, "every_close")[0]


def test_price_review_uses_the_analysis_quote_and_the_floor(eng, monkeypatch):
    rec = rec_for("BTCUSDT", decision="NO_TRADE")
    rec.update(price_reference="binance_spot:BTCUSDT",
               next_review={"in_minutes": 240, "conditions": [{"kind": "price_above", "value": 100.0}]})
    eng.store.save(DecisionRecord("BTCUSDT", "agent_per_pair", "t", "valid", recommendation=rec, ts=T0))
    asked = []
    monkeypatch.setattr(eng.builder, "quote_at", lambda inst, now: asked.append(inst.key) or {"bid": 101, "ask": 102})
    assert not eng.evaluate("BTCUSDT", T0 + 2 * MIN, False, None, "hybrid")[0]     # < review floor (5 min)
    assert eng.evaluate("BTCUSDT", T0 + 6 * MIN, False, None, "hybrid")[0]
    assert set(asked) == {"binance_spot:BTCUSDT"}                                   # never the MT5 execution quote


def test_backoff_after_failed_cycles(eng):
    eng.last_call["XAUUSD"] = T0
    eng.fails["XAUUSD"] = 2                                          # 15 min × 2² = 60 min
    assert not eng.evaluate("XAUUSD", T0 + 20 * MIN, True, PAYLOAD, "every_close")[0]
    assert eng.evaluate("XAUUSD", T0 + 61 * MIN, True, PAYLOAD, "every_close")[0]


def test_not_ready_records_nothing_and_warns_once(eng, monkeypatch):
    monkeypatch.setattr(eng, "ai_ready", lambda: False)
    eng._ai_problem = "claude_code: not signed in"

    async def go():
        for _ in range(3):
            eng._dispatch([("XAUUSD", ["review"], "review", PAYLOAD)], T0)
    asyncio.run(go())
    assert not eng.inflight and "XAUUSD" not in eng.last_call and events(eng, "ai_not_ready") == 1


def test_data_gate_stores_skipped_without_a_call(eng, monkeypatch):
    monkeypatch.setattr(eng, "ai_ready", lambda: True)

    async def go():
        eng._dispatch([("XAUUSD", ["15m close"], "strong", PAYLOAD)], T0)   # real payload: 4h/1d too short
    asyncio.run(go())
    assert not eng.inflight
    row = one(eng, "SELECT status, errors FROM ai_decisions WHERE pair='XAUUSD'")
    assert row[0] == "skipped" and "4h: only 18 bars" in row[1]
    assert eng.last_call["XAUUSD"] == T0 and eng.store.recent("XAUUSD") == []


def test_cycle_runs_in_the_background_and_the_heartbeat_keeps_going(eng, monkeypatch):
    monkeypatch.setattr(eng, "ai_ready", lambda: True)
    monkeypatch.setattr(eng_mod, "data_problems", lambda *a: [])
    gate = asyncio.Event()

    async def run_cycle(queue, as_of, payloads, account=None):
        await gate.wait()
        return [DecisionRecord(q.pair, "m", q.reason, "invalid") for q in queue]
    monkeypatch.setattr(eng.orch, "run_cycle", run_cycle)

    async def go():
        eng._dispatch([("BTCUSDT", ["sweep"], "strong", PAYLOAD)], T0)
        task = eng.inflight["BTCUSDT"]
        await asyncio.sleep(0.05)
        assert not task.done()
        eng.write_status()                                            # the heartbeat does not wait for the AI
        detail = json.loads(one(eng, "SELECT detail FROM collector_status WHERE collector='engine'")[0])
        assert "BTCUSDT" in detail["cycles_inflight"]
        gate.set()
        await task
    asyncio.run(go())
    assert "BTCUSDT" not in eng.inflight and eng.fails["BTCUSDT"] == 1 and eng.last_call["BTCUSDT"] == T0


@pytest.mark.parametrize("left,kept", [(80, {"a", "b", "c"}), (40, {"b", "c"}), (15, {"c"}), (0, set())])
def test_quota_pressure(eng, monkeypatch, left, kept):
    monkeypatch.setattr(eng.orch, "quota", lambda name=None: (left, 100))
    fired = [("a", [], "weak", None), ("b", [], "strong", None), ("c", [], "review", None)]
    assert {f[0] for f in eng._ration(fired, T0)} == kept


# ------------------------------------------------------------------ Phase 3: time reviews only on change, events
def test_time_review_needs_a_change(eng, monkeypatch):
    """§3.7.2: a pure time-based next_review fires only when a decision bar closed since the call AND a new setup
    appeared or price moved more than screen_move_atr × ATR."""
    rec = rec_for()
    rec["next_review"] = {"in_minutes": 30}
    eng.store.save(DecisionRecord("XAUUSD", "agent_per_pair", "t", "valid", recommendation=rec, ts=T0))
    eng.last_call["XAUUSD"] = T0
    eng.store.kv_set("XAUUSD:signature", sorted(eng.evaluate("XAUUSD", T0, True, PAYLOAD, "hybrid") and
                                                 eng._sig.get("XAUUSD", [])))      # nothing new since the call
    mid = (PAYLOAD["market"]["analysis_price"]["bid"] + PAYLOAD["market"]["analysis_price"]["ask"]) / 2 \
        if PAYLOAD["market"].get("analysis_price", {}).get("bid") else PAYLOAD["market"]["analysis_price"]["last_close_1m"]
    eng.store.kv_set("XAUUSD:last_call_price", mid)                                # price has not moved
    assert not eng.evaluate("XAUUSD", T0 + 31 * MIN, True, PAYLOAD, "hybrid")[0]
    atr = PAYLOAD["timeframes"]["15m"]["indicators"]["atr14"]
    eng.store.kv_set("XAUUSD:last_call_price", mid - 0.8 * atr)                     # 0.8 ATR move since the call
    fire, reasons, strength = eng.evaluate("XAUUSD", T0 + 31 * MIN, True, PAYLOAD, "hybrid")
    assert fire and strength == "review" and any("ATR since the last call" in r for r in reasons)


def test_executor_events_wake_the_model_after_coalescing(eng):
    eng.last_call["XAUUSD"] = T0
    eng.appdb.add_event("executor", "order", "demo XAUUSD abcd1234: [placed]")
    rows = eng._pair_events("XAUUSD")
    assert len(rows) == 1
    ts = int(rows[0]["ts"])
    ripe = [e for e in rows if ts + 61_000 - int(e["ts"]) >= eng_mod.EVENT_COALESCE_MS]
    fire, reasons, strength = eng.evaluate("XAUUSD", ts + 61_000 if ts + 61_000 > T0 + 5 * MIN else T0 + 6 * MIN,
                                           False, None, "hybrid", events=ripe)
    assert fire and strength == "event" and reasons[0].startswith("event:") and "XAUUSD" in reasons[0]
    eng._remember_call("XAUUSD", PAYLOAD, "event")
    assert eng._events["XAUUSD"] == []                                              # consumed by the call


def test_event_calls_have_a_daily_budget(eng):
    now = T0 + 10 * MIN
    for i in range(eng.s.ai.event_calls_per_day):
        r = DecisionRecord("XAUUSD", "agent_per_pair", "event: x", "valid", ts=now - i)
        r.trigger_strength = "event"
        eng.store.save(r)
    assert not eng._event_budget("XAUUSD", now)
    assert eng._event_budget("BTCUSDT", now)


def test_screen_timeframe_is_5m_for_15m_pairs(eng):
    assert eng.screen_tf("XAUUSD").value == "5m"


def test_once_waits_for_the_first_sign_in_answer(monkeypatch):
    """``engine --once`` must not fall back just because the background ``claude auth status`` has not answered."""
    from tradingsystem.analysis.engine import wait_for_provider

    class Prov:
        def __init__(self, answers_after):
            self.asked, self.after = 0, answers_after

        def unavailable_reason(self):
            self.asked += 1
            return None if self.asked > self.after else "claude_code: checking the Claude Code sign-in"

        @property
        def availability_pending(self):
            return self.asked <= self.after

    class Orch:
        def __init__(self, prov):
            self.prov = prov
            self.s = type("S", (), {"ai": type("A", (), {"active_provider": "claude_code"})()})()

        def _get(self, name, role):
            return self.prov

    p = Prov(3)
    wait_for_provider(Orch(p), timeout_s=5, poll_s=0.01)
    assert p.asked == 4 and not p.availability_pending
    slow = Prov(10_000)
    t0 = time.monotonic()
    wait_for_provider(Orch(slow), timeout_s=0.1, poll_s=0.01)      # bounded: gives up and lets the cycle report it
    assert time.monotonic() - t0 < 2
