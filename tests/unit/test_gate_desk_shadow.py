"""B14 (D-049): the shadow desk. A pair with ``desk.mode == "shadow"`` is gated in full for the record and stored
``not_executed`` / ``shadow`` (scored in R by the existing virtual-outcome machinery) - and NEVER reaches the backend.

Golden: for the pairs WITHOUT a desk (BTCUSDT / ETHUSDT) the gate output must be identical before and after B14:
* ``test_gate_replay_*`` - the real stored gate records of the production BTCUSDT / ETHUSDT systems
  (tests/fixtures/real/gate_replay_btc_eth.json, trimmed to the gate's inputs) are re-gated from the inputs their check
  texts show; the verdicts equal the stored ones, and every check equals the frozen output of the pre-B14 gate
  (tests/fixtures/gate_replay_golden.json). ``risk_gate.py`` itself is untouched by B14.
* ``test_executor_golden_*`` - ten candidates through the whole executor path (gate + placement on the paper account),
  frozen from the pre-B14 executor (tests/fixtures/executor_gate_golden.json: hand-built quotes / ATR / clock, labelled
  in tests/unit/_desk_harness.py) - state and stored detail are identical, with and without a desk configured for gold.
"""
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.unit import _desk_harness as h
from tests.unit._gate_replay import Unreplayable, as_records, replay
from tradingsystem.core.settings import load_settings
from tradingsystem.execution import executor as ex_mod
from tradingsystem.execution.desk import ADDING, MUTATING, ShadowGuard, ShadowViolation
from tradingsystem.execution.executor import VirtualWalk

FIX = Path(__file__).resolve().parents[1] / "fixtures"


# ------------------------------------------------------------------ golden: the gate on real stored inputs
@pytest.fixture(scope="module")
def replayed():
    s = load_settings(env_path=Path("nope.env"))
    rows = json.loads((FIX / "real" / "gate_replay_btc_eth.json").read_text(encoding="utf-8"))
    out = []
    for r in rows:
        try:
            out.append((r, replay(r, s)))
        except Unreplayable:
            continue                       # a record of an older check set: its inputs are not all shown
    return out


def test_gate_replay_verdicts_and_checks_equal_the_stored_records(replayed):
    assert len(replayed) >= 12 and {r["pair"] for r, _ in replayed} == {"BTCUSDT", "ETHUSDT"}
    exact = 0
    for r, g in replayed:
        new, old = as_records(g), r["detail"]["gate"]
        assert [c["check"] for c in new] == [c["check"] for c in old], r["id"]
        assert [c["ok"] for c in new] == [c["ok"] for c in old], r["id"]
        # the details are the texts of the code that ran then: identical except the rr text (its format and the
        # single-leg measure changed in Phase 3) and a 0.01 % rounding of the sibling exposure the record shows
        drift = {"rr_after_costs", "correlated_exposure"}
        assert [c for c in new if c["check"] not in drift] == [c for c in old if c["check"] not in drift], r["id"]
        exact += new == old
    assert exact >= 5


def test_gate_replay_equals_the_frozen_pre_b14_gate(replayed):
    golden = json.loads((FIX / "gate_replay_golden.json").read_text(encoding="utf-8"))
    assert {r["id"] for r, _ in replayed} == set(golden)
    for r, g in replayed:
        want = golden[r["id"]]
        assert g.approved == want["approved"] and as_records(g) == want["checks"], r["id"]
        assert (g.entry, g.rr_exec, g.size.lots if g.size else None) == (want["entry"], want["rr_exec"], want["lots"])


@pytest.mark.parametrize("desk", [False, True])
def test_executor_golden_btc_eth_unchanged(tmp_path, monkeypatch, desk):
    golden = json.loads((FIX / "executor_gate_golden.json").read_text(encoding="utf-8"))
    got = h.run_scenarios(tmp_path, monkeypatch, desk=desk)          # desk=True: gold is a shadow desk, BTC/ETH are not
    assert got == golden
    assert {v["state"] for v in got.values()} == {"executed", "rejected", "expired"}


# ------------------------------------------------------------------ the shadow path
def run_xau(tmp_path, monkeypatch, *, equity=100_000.0, **kw):
    ex = h.make_executor(h.settings(tmp_path, desk=True, equity=equity), monkeypatch)
    did = "a" * 32
    ex.handle(h.store_candidate(ex, "XAUUSD", h.rec("XAUUSD", **kw), did))
    return ex, did


def legs(ex):
    con = sqlite3.connect(ex.app_db)
    try:
        return con.execute("SELECT count(*) FROM paper_legs").fetchone()[0]
    finally:
        con.close()


def test_a_shadow_idea_is_gated_in_full_stored_not_executed_and_never_placed(tmp_path, monkeypatch):
    ex, did = run_xau(tmp_path, monkeypatch)                        # equity 100000: even the size passes
    st, det = h.stored(ex, did)
    assert st == "not_executed" and det["shadow"] is True and det["desk_ok"] is True and det["gate_approved"] is True
    assert legs(ex) == 0 and "backend" not in det
    assert det["lots"] and det["executed_levels"]["stop_loss"] and det["spread_at_gate"] == pytest.approx(0.3)
    assert [c["check"] for c in det["gate"]][-1] == "daily_loss_worst_case"           # every check ran
    assert h.events(ex, "order") == [] and h.events(ex, "order_failed") == [] and h.events(ex, "gate_rejected") == []
    assert len(h.events(ex, "shadow_idea")) == 1
    # the very same idea on a pair without a desk gates identically (and is then placed)
    sub = tmp_path / "plain"
    sub.mkdir()
    ex2 = h.make_executor(h.settings(sub, desk=False), monkeypatch)
    ex2.handle(h.store_candidate(ex2, "XAUUSD", h.rec("XAUUSD"), did))
    st2, det2 = h.stored(ex2, did)
    assert st2 == "executed" and det2["gate"] == det["gate"] and det2["executed_levels"] == det["executed_levels"]


def test_desk_ok_is_every_check_but_position_size_and_effective_leverage(tmp_path, monkeypatch):
    # 100 USD: the minimum lot (1 oz) risks 9.6 % at a 9.6 stop -> position_size fails, the rest passes
    ex, did = run_xau(tmp_path, monkeypatch, equity=100.0)
    st, det = h.stored(ex, did)
    failed = [c["check"] for c in det["gate"] if not c["ok"]]
    assert st == "not_executed" and failed == ["position_size"] and det["gate_approved"] is False
    assert det["desk_ok"] is True and det["desk_failed"] == [] and det["desk_waived"] == ["position_size", "effective_leverage"]
    assert "effective_leverage" not in [c["check"] for c in det["gate"]]      # the check never ran (size failed)
    # 400 USD: the size passes (2.4 %), the leverage 10.75x does not -> still desk_ok
    sub = tmp_path / "lev"
    sub.mkdir()
    ex, did = run_xau(sub, monkeypatch, equity=400.0)
    st, det = h.stored(ex, did)
    failed = [c["check"] for c in det["gate"] if not c["ok"]]
    assert failed == ["effective_leverage"] and det["desk_ok"] is True and det["gate_approved"] is False
    # any other failed check -> desk_ok False (and it is named)
    for i, kw in enumerate(({"rr": 1.0}, {"confidence": 40}, {"age_s": 400}, {"sl_dist": 60.0})):
        sub = tmp_path / f"bad{i}"
        sub.mkdir()
        ex, did = run_xau(sub, monkeypatch, **kw)
        st, det = h.stored(ex, did)
        assert st == "not_executed" and det["shadow"] is True and det["desk_ok"] is False and det["desk_failed"], kw
        assert set(det["desk_failed"]) <= {c["check"] for c in det["gate"] if not c["ok"]}


def test_a_shadow_idea_that_never_reached_the_gate_is_still_a_shadow_record(tmp_path, monkeypatch):
    ex, did = run_xau(tmp_path, monkeypatch, valid_ms=-1000)
    st, det = h.stored(ex, did)
    assert (st, det["shadow"], det["desk_ok"]) == ("not_executed", True, False) and "expired" in det["reason"]
    sub = tmp_path / "nq"
    sub.mkdir()
    ex = h.make_executor(h.settings(sub, desk=True), monkeypatch, no_quote=("XAUUSD",))
    ex.handle(h.store_candidate(ex, "XAUUSD", h.rec("XAUUSD"), did))
    st, det = h.stored(ex, did)
    assert (st, det["shadow"], det["desk_ok"], det["reason"]) == ("not_executed", True, False, "no execution quote")


# ------------------------------------------------------------------ never reaches the backend
class FakeBackend:
    """Records every call. Reads the shadow path needs (the account for the full gate) are allowed; every other
    method - place, modify, cancel, close, the position actions, management, the outcome settlement - is a mutation
    or a leg read of a live trade, and the tests assert none happened."""
    READS = {"account"}

    def __init__(self, equity=100.0):
        self.calls, self.equity, self.places = [], equity, []

    def account(self, marks=None):
        self.calls.append("account")
        return {"equity": self.equity, "balance": self.equity, "open_positions": 0, "open_orders": 0,
                "exposure": [], "open_risk_pct_by_pair": {}, "sibling_risk_pct_by_pair": {},
                "realized_today_usd": 0.0, "unrealized_usd": 0.0, "currency": "USD"}

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)

        def call(*a, **k):
            self.calls.append(name)
            if name == "place":
                self.places.append(k.get("pair"))
            return {"ok": True, "status": "applied", "placed": [], "legs": []}
        return call

    def mutations(self):
        return [c for c in self.calls if c not in self.READS]


def with_fake(ex, equity=100.0):
    fake = FakeBackend(equity)
    ex.paper = fake                                   # bypasses the guard on purpose: the executor's own checks first
    return fake


def test_a_shadow_pair_never_reaches_the_backend_from_any_executor_step(tmp_path, monkeypatch):
    ex = h.make_executor(h.settings(tmp_path, desk=True), monkeypatch)
    fake = with_fake(ex, 100_000.0)                   # a size that passes: nothing but the shadow branch stops it
    for i, kw in enumerate(({}, {"order_type": "BUY_LIMIT"}, {"side": "SELL"}, {"rr": 1.0})):
        ex.handle(h.store_candidate(ex, "XAUUSD", h.rec("XAUUSD", **kw), f"{i:032x}"))
    ex.process_candidates()
    assert fake.mutations() == [] and set(fake.calls) == {"account"}
    # a legacy EXECUTED gold decision (placed before the desk existed) with model actions and management rules:
    # neither the manager nor the action step touches the backend for a shadow pair - not even a read of its legs
    did = "b" * 32
    r = h.rec("XAUUSD")
    h.store_candidate(ex, "XAUUSD", r, did)
    ex.store.set_execution_state(did, "executed", {"mode": "paper", "executed_levels": {"stop_loss": r["stop_loss"]}})
    src = h.rec("XAUUSD", side="BUY")
    src.update(decision="NO_TRADE", position_actions=[{"target": {"decision": did[:8], "kind": "position"},
                                                       "action": "close", "fraction": 1.0, "reason": "x"}])
    from tradingsystem.ai.store import DecisionRecord
    rec2 = DecisionRecord("XAUUSD", "agent_per_pair", "t", "valid", recommendation=src, id="c" * 32, ts=h.NOW - 5_000)
    rec2.actions_state = "pending"
    ex.store.save(rec2)
    fake.calls.clear()
    ex.manage_positions()
    ex.process_actions()
    assert fake.calls == []
    assert ex.store.pending_actions("XAUUSD", 0)                                 # left untouched, never "done"


def test_the_pair_check_is_the_first_thing_handle_does(tmp_path, monkeypatch):
    ex = h.make_executor(h.settings(tmp_path, desk=True), monkeypatch)
    seen = []
    ex._handle = lambda cand: seen.append(cand["pair"]) or False       # the full path - a shadow pair must not use the lock
    ex.mt5 = SimpleNamespace()                                          # an MT5 executor would take the placement lock first
    ex.handle({"id": "x", "pair": "XAUUSD", "rec": {}, "ts": 0, "state": "queued"})
    assert seen == ["XAUUSD"]


def test_the_backend_wrapper_refuses_every_mutation_of_a_shadow_pair_and_passes_the_others():
    fake = FakeBackend()
    resolve = lambda method, a, k: k.get("pair") or (a[0] if a else None)          # noqa: E731
    g = ShadowGuard(fake, frozenset({"XAUUSD"}), resolve)
    for name in sorted(MUTATING):
        with pytest.raises(ShadowViolation):
            getattr(g, name)("XAUUSD")
        assert getattr(g, name)("BTCUSDT")                                          # a traded pair goes through
    assert fake.calls == sorted(MUTATING)                                           # only the BTC calls arrived
    assert g.account() and fake.calls[-1] == "account"                              # reads pass
    # an unresolvable placement is refused (it could be gold); an unresolvable reduction passes (nothing to protect)
    with pytest.raises(ShadowViolation):
        g.place()
    fake.calls.clear()
    assert g.close_position() and fake.calls == ["close_position"]
    assert ADDING == {"place"}
    # a resolver that fails is judged by the kind of call
    broken = ShadowGuard(fake, frozenset({"XAUUSD"}), lambda *a: 1 / 0)
    with pytest.raises(ShadowViolation):
        broken.place(pair="XAUUSD")
    assert broken.cancel_decision("d")


def test_the_executors_paper_backend_is_wrapped_and_resolves_pairs_from_its_own_tables(tmp_path, monkeypatch):
    ex = h.make_executor(h.settings(tmp_path, desk=True), monkeypatch)
    assert isinstance(ex.paper, ShadowGuard) and ex.shadow_pairs == {"XAUUSD"}
    q = h.Tick(h.NOW, 4300.0, 4300.3, 1)
    with pytest.raises(ShadowViolation):
        ex.paper.place(decision_id="z" * 32, pair="XAUUSD", instrument="mt5:XAUUSD@", rec=h.rec("XAUUSD"), lots=0.01,
                       entry=4300.3, contract_size=100, volume_step=0.01, volume_min=0.01, quote=q)
    assert legs(ex) == 0
    did = "d" * 32                                                     # a gold decision id resolves through ai_decisions
    h.store_candidate(ex, "XAUUSD", h.rec("XAUUSD"), did)
    with pytest.raises(ShadowViolation):
        ex.paper.cancel_decision(did)
    with pytest.raises(ShadowViolation):
        ex.paper.close_legs(did, None, q)
    btc = "e" * 32
    h.store_candidate(ex, "BTCUSDT", h.rec("BTCUSDT"), btc)
    assert ex.paper.cancel_decision(btc) == 0                          # a traded pair reaches the real backend
    res = ex.paper.place(decision_id=btc, pair="BTCUSDT", instrument="mt5:BTCUSD@", rec=h.rec("BTCUSDT"), lots=0.01,
                         entry=84026.0, contract_size=1, volume_step=0.01, volume_min=0.01, quote=q)
    assert res["ok"] and legs(ex) == 1


# ------------------------------------------------------------------ fail closed, isolation, no re-processing
def test_an_exception_on_the_shadow_path_records_an_event_and_never_blocks_the_loop(tmp_path, monkeypatch):
    ex = h.make_executor(h.settings(tmp_path, desk=True, equity=100_000.0), monkeypatch)
    fake = with_fake(ex, 100_000.0)
    calls = []
    real_atr = ex.atr

    def atr(pair, as_of=None):
        calls.append(pair)
        if pair == "XAUUSD" and len(calls) == 1:
            raise RuntimeError("boom: the frame could not be read")
        return real_atr(pair, as_of)
    ex.atr = atr
    h.store_candidate(ex, "XAUUSD", h.rec("XAUUSD"), "1" * 32)
    h.store_candidate(ex, "XAUUSD", h.rec("XAUUSD", side="SELL"), "2" * 32)
    h.store_candidate(ex, "BTCUSDT", h.rec("BTCUSDT"), "3" * 32)
    ex.process_candidates()
    st, det = h.stored(ex, "1" * 32)
    assert st == "not_executed" and det["shadow"] is True and det["desk_ok"] is False and "boom" in det["error"]
    assert any("boom" in e for e in h.events(ex, "shadow_error")) and len(h.events(ex, "shadow_error")) == 1
    assert h.stored(ex, "2" * 32)[1]["desk_ok"] is True                # the next shadow idea went through
    # the BTC candidate (a real pair) took the real path: its placement reached the backend and is the ONLY mutation
    # - the shadow branch is per pair, not global
    assert fake.places == ["BTCUSDT"] and fake.mutations() == ["place"]


def test_a_stored_shadow_idea_is_never_picked_up_again_in_auto_mode(tmp_path, monkeypatch):
    s = h.settings(tmp_path, desk=True, equity=100_000.0, trigger="auto")
    ex = h.make_executor(s, monkeypatch)
    ts = h.NOW + 1_000                                                   # after the executor's start: auto mode's rule
    for pair, did in (("XAUUSD", "1" * 32), ("BTCUSDT", "2" * 32), ("BTCUSDT", "3" * 32)):
        h.store_candidate(ex, pair, h.rec(pair), did, state="not_executed")     # saved at NOW-10s: moved below
    con = sqlite3.connect(ex.app_db)
    con.execute("UPDATE ai_decisions SET ts=?", (ts,))
    con.execute("UPDATE ai_decisions SET execution_detail='{\"reason\": \"kept\"}' WHERE id=?", ("3" * 32,))
    con.commit()
    con.close()
    assert sorted(c["id"][:1] for c in ex.candidates()) == ["1", "2", "3"]      # a real pair's rows: same as before B14
    ex.handle(next(c for c in ex.candidates() if c["pair"] == "XAUUSD"))
    assert h.stored(ex, "1" * 32)[0] == "not_executed"
    assert sorted(c["id"][:1] for c in ex.candidates()) == ["2", "3"]           # the shadow idea is final


# ------------------------------------------------------------------ scoring in R and the reports' view of it
def test_a_shadow_idea_is_scored_in_r_exactly_like_a_refused_one(tmp_path, monkeypatch, real_candles_1m):
    ex = h.make_executor(h.settings(tmp_path, desk=True, equity=100.0), monkeypatch)
    t = real_candles_1m                                                  # real BTCUSDT 1m bars stand in as the price path
    i0 = 100
    ts0 = int(t["open_time"][i0].as_py())
    close = float(t["close"][i0].as_py())

    class Reader:
        c = {k: t[k].to_numpy() for k in ("open_time", "high", "low")}

        def read_range(self, spec, start, end, cols):
            m = self.c["open_time"] >= start
            return {k: self.c[k][m] for k in cols}
    monkeypatch.setattr(ex_mod, "spec_for", lambda inst, dt_, tf=None: None)
    ex.reader = lambda key: Reader()
    monkeypatch.setattr(ex_mod, "now_ms", lambda: ts0 + 7 * 86_400_000)
    base = h.rec("XAUUSD")
    base.update(timestamp=ex_mod.iso(ts0), valid_until=ex_mod.iso(ts0 + 3_600_000), entry={"price": close},
                stop_loss=close - 150, take_profits=[{"price": close + 300, "close_fraction": 1.0}])
    ids = {"shadow": "1" * 32, "refused": "2" * 32}
    for did in ids.values():
        h.store_candidate(ex, "XAUUSD", base, did)
    ex.store.set_execution_state(ids["shadow"], "not_executed", {"shadow": True, "desk_ok": True})
    ex.store.set_execution_state(ids["refused"], "rejected", {"reason": "position_size: x"})
    ex.virtual_outcomes()
    con = sqlite3.connect(ex.app_db)
    res = dict(con.execute("SELECT id, virtual_outcome || '|' || coalesce(virtual_r, '') FROM ai_decisions").fetchall())
    con.close()
    assert res[ids["shadow"]] == res[ids["refused"]] and res[ids["shadow"]].split("|")[0] in ("tp1_first", "sl_first")
    # the decision-metrics job picks a scored shadow idea up as an idea that was not executed (basis: virtual)
    pend = ex.store.pending_metrics(10, no_trade_before_ms=0, pairs=["XAUUSD"])
    by_id = {p["id"]: p for p in pend}
    assert by_id[ids["shadow"]]["execution_state"] == "not_executed"
    assert by_id[ids["shadow"]]["execution_detail"]["shadow"] is True


def test_shadow_rows_are_not_errors_or_rejections_in_the_reports(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("demo_report_b14", FIX.parents[1] / "tools" / "demo_report.py")
    demo_report = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = demo_report
    spec.loader.exec_module(demo_report)
    ex, did = run_xau(tmp_path, monkeypatch, equity=100.0)               # not_executed / shadow, position_size failed
    con = sqlite3.connect(f"file:{ex.app_db.as_posix()}?mode=ro", uri=True)
    try:
        out = demo_report.gate_rejections(con, "XAUUSD", 0, h.NOW * 2)
    finally:
        con.close()
    assert out["rejected"] == 0 and out["by_class"] == {}
    assert "position_size" not in json.dumps(out)                        # the waived check is not a gate rejection


@pytest.mark.parametrize("trigger", ["manual", "auto"])
def test_a_shadow_idea_is_recorded_in_any_trigger_mode_and_after_executor_downtime(tmp_path, monkeypatch, trigger):
    """Review fix: an XAU idea is gated for the record whatever the trigger mode (nothing is ever sent, so there is no
    manual approval for it) and also when it was stored while the executor was down (bounded to 24 h); a real pair's
    candidates are exactly as before."""
    s = h.settings(tmp_path, desk=True, equity=100_000.0, trigger=trigger)
    ex = h.make_executor(s, monkeypatch)
    for pair, did in (("XAUUSD", "1" * 32), ("BTCUSDT", "2" * 32), ("XAUUSD", "3" * 32)):
        h.store_candidate(ex, pair, h.rec(pair), did, state="not_executed")      # NOW-10 s: before the start
    con = sqlite3.connect(ex.app_db)
    con.execute("UPDATE ai_decisions SET ts=? WHERE id=?", (h.NOW - 25 * 3_600_000, "3" * 32))   # beyond 24 h
    con.commit()
    con.close()
    assert [c["id"][:1] for c in ex.candidates()] == ["1"]                     # BTC before the start: not (as before)
    ex.handle(next(c for c in ex.candidates()))
    st, det = h.stored(ex, "1" * 32)
    assert st == "not_executed" and det["shadow"] is True and "gate" in det
    assert ex.candidates() == []                                                # final once recorded


def test_the_model_sees_a_shadow_ideas_verdict_in_its_history(tmp_path, monkeypatch):
    """Review fix: history[] of a shadow idea says shadow / desk_ok and the failed desk checks as gate_reason (rule 10's
    feedback), and a plain not_executed row (not picked up yet) says nothing."""
    s = h.settings(tmp_path, desk=True, equity=100_000.0)
    ex = h.make_executor(s, monkeypatch)
    ex.handle(h.store_candidate(ex, "XAUUSD", h.rec("XAUUSD", rr=1.0), "4" * 32))          # RR 1.0 < min_rr
    ex.handle(h.store_candidate(ex, "XAUUSD", h.rec("XAUUSD", side="SELL"), "5" * 32))
    h.store_candidate(ex, "XAUUSD", h.rec("XAUUSD"), "6" * 32, state="not_executed")      # not gated yet
    rows = ex.store.recent("XAUUSD", 10)
    bad = [x for x in rows if x.get("gate_reason", "").startswith("rr_after_costs")]
    assert len(bad) == 1 and bad[0]["shadow"] == 1 and bad[0]["desk_ok"] == 0
    assert any(x.get("shadow") == 1 and x.get("desk_ok") == 1 and "gate_reason" not in x for x in rows)
    assert sum("shadow" in x for x in rows) == 2                               # the ungated row carries nothing
