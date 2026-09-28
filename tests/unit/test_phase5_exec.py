"""Phase 5 A7, execution side: the kill switch blocks new orders only (D-043: protection keeps running under it), the
virtual-outcome walk reads bounded windows with a per-pass budget, and the supervisor's MT5 terminal job check asks
about its own job only (handoff §0 ``terminal_in_job``, fixed in c844318).

Real data: XAUUSD@ ticks (shifted to end one second ago, as the Phase 3 position-action tests) and BTCUSDT 1m bars
(the decision-metrics fixture, bar i = minutes after 2025-11-09T10:40Z)."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from tradingsystem.ai.store import DecisionRecord
from tradingsystem.core.killswitch import kill_switch_path, set_kill_switch
from tradingsystem.core.settings import PathsCfg, load_settings
from tradingsystem.core.timeframes import Timeframe
from tradingsystem.core.timeutil import iso, now_ms, parse_date_spec
from tradingsystem.execution import executor as ex_mod
from tradingsystem.execution import metrics as mx
from tradingsystem.execution.backends.paper import Tick
from tradingsystem.execution.executor import Executor, evaluate_virtual, virtual_walk
from tradingsystem.execution.management import breakeven_level
from tradingsystem.execution.metrics import levels_of, virtual_trade
from tradingsystem.storage.reader import InstrumentReader
from tradingsystem.storage.tablespec import spec_for

from .test_executor_paper import place, write_ticks
from .test_metrics import MIN, T0, env, ot, save, trade  # noqa: F401 — env: the BTCUSDT 1m bars in a tmp data root

PAIR = "XAUUSD"
FAR = 1_900_000_000_000                    # a "now" long after every window


# ------------------------------------------------------------------ kill switch: new orders refused, protection runs
@pytest.fixture
def armed(tmp_path, real_xau_ticks, monkeypatch):
    """A paper executor (manual trigger, $100,000 so an XAU idea sizes within every gate limit) whose XAUUSD@ ticks end
    one second ago, the market reported open and a decision-TF ATR of 8.0 (no candles in this data root)."""
    t = real_xau_ticks
    shift = now_ms() - 1_000 - int(t["time_msc"].to_pylist()[-1])
    ticks = [Tick(int(ms) + shift, float(b), float(a), int(k) + shift * 1000) for k, ms, b, a in
             zip(t["key"].to_pylist(), t["time_msc"].to_pylist(), t["bid"].to_pylist(), t["ask"].to_pylist())]
    s = load_settings(env_path=Path("nope.env"), extra_env={"EXECUTION_MODE": "paper", "EXECUTION_TRIGGER": "manual",
                                                           "TS_INSTANCE": ""})
    s = s.model_copy(update={"paths": PathsCfg(data_dir=str(tmp_path / "data"), logs_dir=str(tmp_path / "logs")),
                             "execution": s.execution.model_copy(update={"paper_equity": 100_000.0})})
    write_ticks(s, ticks)

    class Open:
        def is_open(self, ms):
            return True
    monkeypatch.setattr(ex_mod, "calendar_for", lambda *a, **k: Open())
    ex = Executor(s)
    ex.atr = lambda pair, as_of=None: 8.0
    yield ex, ticks
    ex.paper.close()
    ex.store.close()


def _open_sell_in_profit(ex, q) -> tuple[str, float, float]:
    """An executed SELL (one 0.01-lot paper leg) filled 5.00 above the current ask 20 min ago, original stop ask + 20,
    with a declared 'breakeven at 0.3 R' rule (now 5/15 = 0.33 R → due). Returns (id, original stop, fill)."""
    did = "a" * 32
    sl0, fill = round(q.ask + 20, 2), round(q.ask + 5, 2)
    rule = {"action": "move_sl_to_breakeven", "trigger": "r_multiple", "value": 0.3, "params": {}}
    r = {"decision": "SELL", "order_type": "MARKET", "stop_loss": sl0, "valid_until": iso(now_ms() + 3_600_000),
         "take_profits": [{"price": round(q.bid - 40, 2), "close_fraction": 1.0}], "management": [rule],
         "entry": {"price": None}}
    assert place(ex.paper, did, r, q, entry=q.bid)["ok"]
    con = sqlite3.connect(ex.app_db)
    con.execute("UPDATE paper_legs SET fill_price=?, fill_ms=? WHERE decision_id=?", (fill, now_ms() - 20 * MIN, did))
    con.commit()
    con.close()
    ex.store.save(DecisionRecord(PAIR, "agent_per_pair", "t", "valid", id=did, ts=now_ms() - 30 * MIN,
                                 recommendation={**r, "pair": PAIR, "timestamp": iso(now_ms() - 30 * MIN)}))
    ex.store.set_execution_state(did, "executed", {"mode": "paper", "executed_levels": {"stop_loss": sl0},
                                                   "executed_management": [rule]})
    return did, sl0, fill


def _model_tightens(ex, target: str, value: float) -> str:
    """The model's NO_TRADE answer carrying one position action: tighten the SELL's stop to ``value``."""
    sid = "b" * 32
    rec = {"pair": PAIR, "decision": "NO_TRADE", "timestamp": iso(now_ms() - 30_000), "position_actions": [
        {"target": {"decision": target[:8], "kind": "position"}, "action": "modify_sl", "value": value,
         "reason": "tighten above the lower high"}]}
    r = DecisionRecord(PAIR, "agent_per_pair", "t", "valid", recommendation=rec, id=sid)
    r.actions_state = "pending"
    ex.store.save(r)
    return sid


def _queue_buy(ex, q, did: str) -> None:
    """A new BUY idea queued for execution that passes every other gate check (SL 10.27 = 1.3 ATR, RR 2.4, 1 %)."""
    rec = {"pair": PAIR, "decision": "BUY", "order_type": "MARKET", "timestamp": iso(now_ms() - 5_000),
           "valid_until": iso(now_ms() + 3_600_000), "entry": {"price": None}, "stop_loss": round(q.bid - 10, 2),
           "take_profits": [{"price": round(q.ask + 25, 2), "close_fraction": 1.0}], "confidence": 80,
           "risk_management": {"risk_percent_suggested": 1.0}, "management": []}
    ex.store.save(DecisionRecord(PAIR, "agent_per_pair", "t", "valid", recommendation=rec, id=did))
    ex.store.set_execution_state(did, "queued")


def _decision(ex, did: str) -> tuple[str, dict]:
    con = sqlite3.connect(ex.app_db)
    try:
        st, det = con.execute("SELECT execution_state, execution_detail FROM ai_decisions WHERE id=?", (did,)).fetchone()
    finally:
        con.close()
    return st, json.loads(det) if det else {}


def _actions(ex) -> list[tuple]:
    con = sqlite3.connect(ex.app_db)
    try:
        return con.execute("SELECT source, action, status FROM position_actions ORDER BY id").fetchall()
    finally:
        con.close()


@pytest.mark.parametrize("scope", [PAIR, None], ids=["pair_switch", "global_switch"])
def test_kill_switch_refuses_a_new_buy_while_breakeven_and_a_model_stop_still_apply(armed, scope):
    """D-043 under a kill switch (``data/instances/XAUUSD/KILL_SWITCH`` or the global ``data/KILL_SWITCH``), in ONE
    executor step: the queued BUY is refused by the gate with ``kill switch engaged`` as its only failed check (no
    paper leg); the model's tighter stop on the open SELL (ask + 20 → ask + 15) and the due breakeven rule (→ fill −
    max(spread × mult, stops level + spread)) are both applied. Switch off → the same idea is placed."""
    ex, ticks = armed
    q = ticks[-1]
    did, sl0, fill = _open_sell_in_profit(ex, q)
    _model_tightens(ex, did, round(sl0 - 5, 2))
    _queue_buy(ex, q, "c" * 32)
    _, created = set_kill_switch(ex.s, scope, reason="unit test", actor="test")
    assert created and ex.kill_switch(PAIR)

    ex.step(False)

    state, detail = _decision(ex, "c" * 32)
    failed = [(c["check"], c["detail"]) for c in detail["gate"] if not c["ok"]]
    assert state == "rejected" and failed == [("kill_switch", "kill switch engaged")]
    assert detail["reason"] == "kill_switch: kill switch engaged"
    assert ex.paper.decision_legs("c" * 32) == []
    # protection ran in the same step: the model's stop first, then the rule's breakeven (tighter still)
    assert _actions(ex) == [("model", "modify_sl", "applied"), ("rule", "set_sl", "applied")]
    [lg] = ex.model_legs.legs_of(did)
    mult = ex.s.execution.management.breakeven_buffer_spread_mult
    assert lg.kind == "position" and lg.sl == pytest.approx(breakeven_level(lg, ex.model_legs.venue(lg), mult))
    assert lg.sl < round(sl0 - 5, 2) and lg.sl < fill                   # a SELL's stop only ever moved down

    kill_switch_path(ex.s, scope).unlink()                              # scripts\kill_switch_off.bat
    _queue_buy(ex, q, "d" * 32)
    ex.step(False)
    state, detail = _decision(ex, "d" * 32)
    assert state == "executed" and [c["check"] for c in detail["gate"] if not c["ok"]] == []
    assert len(ex.paper.decision_legs("d" * 32)) == 1


# ------------------------------------------------------------------ virtual outcomes: bounded reads, per-pass budget
@pytest.fixture
def reads(monkeypatch):
    """Every candle read through InstrumentReader: (start, end, rows)."""
    seen: list[tuple] = []
    orig = InstrumentReader.read_range

    def spy(self, spec, start_ms=None, end_ms=None, columns=None):
        out = orig(self, spec, start_ms, end_ms, columns)
        seen.append((start_ms, end_ms, len(next(iter(out.values())))))
        return out

    monkeypatch.setattr(InstrumentReader, "read_range", spy)
    return seen


def _on(ex) -> NS:
    """``test_metrics.save`` into the executor's own app.db (the metrics fixture's store is another file)."""
    return NS(store=ex.store, app_db=ex.app_db)


def _stuck(price: float = 104956.77) -> dict:
    """A BUY at bar 2900 whose stop and target are never reached before the fixture's last bar (2999)."""
    return trade("BUY", "MARKET", {"price": price}, 99_000.0, [111_000.0], ot(2900))


def test_virtual_outcome_reads_bounded_chunks_for_a_decision_far_in_the_past(env, reads):
    """BUY MARKET at bar 550's open 104695.85, SL −150, TP1 +150 (TP1 at bar 560: R 1.0) and SELL at bar 777's open
    104690.38, SL +200, TP1 −150 (TP1 at bar 782: R 0.75), scored ~a year later: ONE read of [cycle, cycle +
    VIRTUAL_CHUNK_MS) each — never cycle → now (the old read took every bar since the cycle). Both agree with the
    metrics walk over the same bars."""
    rd = env.reader(env.inst.key)
    c = rd.read_range(spec_for(env.inst, "candles", Timeframe.M1), T0, ot(3000), ["open_time", "high", "low"])
    for rec, r in ((trade("BUY", "MARKET", {"price": 104695.85}, 104545.85, [104845.85], ot(550)), 1.0),
                   (trade("SELL", "MARKET", {"price": 104690.38}, 104890.38, [104540.38], ot(777)), 0.75)):
        reads.clear()
        cycle = parse_date_spec(rec["timestamp"])
        w = virtual_walk(rec, rd, env.inst, FAR)
        assert (w.outcome, w.r) == ("tp1_first", r)
        assert [(s, e) for s, e, _ in reads] == [(cycle, cycle + ex_mod.VIRTUAL_CHUNK_MS)]
        assert w.bars == reads[0][2] == 720
        assert virtual_trade(levels_of(rec), cycle, c["open_time"], c["high"], c["low"]).outcome == w.outcome


def test_a_gap_after_the_horizon_reads_only_the_window_and_is_asked_again_later(env, reads):
    """An idea at bar 2900 whose horizon (valid_until + 24 h) lies past the last stored bar (2999) — a gap: undecided;
    the reads stop at horizon + VIRTUAL_GAP_MS in VIRTUAL_CHUNK_MS pieces (100 bars read, not everything up to now).
    The executor asks it again only after VIRTUAL_RETRY_MS."""
    rec = _stuck()
    horizon = ot(2900) + 60 * MIN + mx.VIRTUAL_HORIZON_MS
    w = virtual_walk(rec, env.reader(env.inst.key), env.inst, FAR)
    assert (w.outcome, w.bars, w.horizon_ms) == (None, 100, horizon)
    assert reads[0][0] == ot(2900) and reads[-1][1] == horizon + ex_mod.VIRTUAL_GAP_MS
    assert all(e - s <= ex_mod.VIRTUAL_CHUNK_MS for s, e, _ in reads)
    ex = Executor(env.s)
    try:
        rid = save(_on(ex), rec, record_ts=ot(2901), state="rejected", detail={"reason": "gate"})
        reads.clear()
        ex.virtual_outcomes()
        assert reads and ex._virtual_later[rid] > now_ms()                  # undecided after its horizon: waits
        n = len(reads)
        ex.virtual_outcomes()
        assert len(reads) == n                                              # not read again on the next pass
    finally:
        ex.paper.close()
        ex.store.close()


def test_the_pass_budget_takes_the_oldest_idea_first_and_a_stuck_one_never_blocks(env, reads, monkeypatch):
    """Three ideas, oldest first by record time: one stuck in a gap (cycle at bar 2900, 100 bars), then the BUY at bar
    550 and the SELL at bar 777 (both TP1 first). With a budget of 150 bars, pass 1 walks the stuck one (100) and the
    BUY (720 read: over budget) → the SELL waits; pass 2 skips the stuck one (asked again later) and scores the SELL."""
    monkeypatch.setattr(ex_mod, "VIRTUAL_BARS_PER_PASS", 150)
    ex = Executor(env.s)
    try:
        stuck = save(_on(ex), _stuck(), record_ts=ot(40), state="rejected", detail={"reason": "gate"})
        buy = save(_on(ex), trade("BUY", "MARKET", {"price": 104695.85}, 104545.85, [104845.85], ot(550)),
                   record_ts=ot(551), state="rejected", detail={"reason": "gate"})
        sell = save(_on(ex), trade("SELL", "MARKET", {"price": 104690.38}, 104890.38, [104540.38], ot(777)),
                    record_ts=ot(778), state="expired", detail={"reason": "expired"})

        def outcomes():
            con = sqlite3.connect(ex.app_db)
            try:
                return dict(con.execute("SELECT id, virtual_outcome FROM ai_decisions").fetchall())
            finally:
                con.close()

        ex.virtual_outcomes()
        assert outcomes() == {stuck: None, buy: "tp1_first", sell: None}
        reads.clear()
        ex.virtual_outcomes()
        assert outcomes() == {stuck: None, buy: "tp1_first", sell: "tp1_first"}
        assert reads and all(s >= ot(777) for s, _, _ in reads)             # the stuck idea was not read again
    finally:
        ex.paper.close()
        ex.store.close()


def test_a_validity_beyond_the_cap_is_walked_as_the_cap_in_both_walks(env, reads, monkeypatch):
    """The contract sets no upper bound on valid_until: a BUY_LIMIT at 90,000 (never reached) valid for 30 days is
    walked as valid for VIRTUAL_MAX_VALID_MS (here 60 min) — not_triggered at bar 160 in both walks, one read."""
    monkeypatch.setattr(mx, "VIRTUAL_MAX_VALID_MS", 60 * MIN)
    p = 90_000.0
    rec = trade("BUY", "BUY_LIMIT", {"price": p}, p - 200, [p + 300], ot(100), valid_min=30 * 24 * 60)
    rd = env.reader(env.inst.key)
    assert evaluate_virtual(rec, rd, env.inst, FAR) == ("not_triggered", 0.0)
    assert [(s, e) for s, e, _ in reads] == [(ot(100), ot(100) + ex_mod.VIRTUAL_CHUNK_MS)]
    c = rd.read_range(spec_for(env.inst, "candles", Timeframe.M1), T0, ot(3000), ["open_time", "high", "low"])
    vt = virtual_trade(levels_of(rec), ot(100), c["open_time"], c["high"], c["low"])
    assert (vt.outcome, vt.resolved_ms) == ("not_triggered", ot(160))


# ------------------------------------------------------------------ handoff §0: terminal_in_job (fixed in main)
def test_the_terminal_job_check_asks_about_the_supervisor_job_only(monkeypatch):
    """``IsProcessInJob(h, NULL)`` means ANY job (Task Scheduler's included): the check passes the supervisor's own job
    handle, and without a job (handle None) it asks nothing — only a terminal inside OUR job is reported."""
    from tradingsystem.supervisor import supervisor as sup_mod
    asked: list[tuple] = []
    monkeypatch.setattr(sup_mod, "in_job", lambda pid, job=None: asked.append((pid, job)) or pid == 11)
    events: list[tuple] = []
    sup = sup_mod.Supervisor.__new__(sup_mod.Supervisor)
    sup.job, sup.term_info, sup._term_warned = NS(handle=4242), {}, set()
    sup._event = lambda name, event, detail, duration_ms=None: events.append((name, event))
    sup._check_terminal_job("terminal64.exe", [10, 11])
    assert asked == [(10, 4242), (11, 4242)] and events == [("mt5", "terminal_in_job")]
    assert sup.term_info["terminal64.exe"] == {"pids": [10, 11], "in_supervisor_job": True}
    asked.clear()
    sup.job = NS(handle=None)
    sup._check_terminal_job("terminal64.exe", [12])
    assert asked == [] and sup.term_info["terminal64.exe"]["in_supervisor_job"] is False
