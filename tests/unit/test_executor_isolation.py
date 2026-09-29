"""Executor robustness (EXE-03): atomic claim, per-candidate isolation with bounded retries, reconciliation of
'executing' rows, heartbeat behaviour of a failing loop. Pure logic on a throw-away app.db (no market data)."""
import json
import sqlite3
from pathlib import Path

import pytest

import tradingsystem.execution.executor as ex_mod
from tests.conftest import no_desk
from tradingsystem.ai.store import DecisionRecord
from tradingsystem.core.settings import PathsCfg, load_settings
from tradingsystem.core.timeutil import iso, now_ms
from tradingsystem.execution.backends.paper import Tick
from tradingsystem.execution.executor import MAX_HANDLE_ATTEMPTS, Executor


@pytest.fixture
def ex(tmp_path):
    s = load_settings(env_path=Path("nope.env"), extra_env={"EXECUTION_MODE": "paper", "EXECUTION_TRIGGER": "manual"})
    s = no_desk(s.model_copy(update={"paths": PathsCfg(data_dir=str(tmp_path / "data"), logs_dir=str(tmp_path / "logs"))}))
    return Executor(s)


def decision(ex, did, state="queued", pair="XAUUSD", valid_ms=3_600_000):
    r = {"decision": "BUY", "order_type": "MARKET", "stop_loss": 1.0, "valid_until": iso(now_ms() + valid_ms),
         "take_profits": [{"price": 2.0, "close_fraction": 1.0}], "entry": {"price": None}}
    ex.store.save(DecisionRecord(pair=pair, mode="test", trigger="test", status="valid", id=did, recommendation=r))
    ex.store.set_execution_state(did, state)


def state(ex, did):
    con = sqlite3.connect(ex.app_db)
    try:
        s, d = con.execute("SELECT execution_state, execution_detail FROM ai_decisions WHERE id=?", (did,)).fetchone()
    finally:
        con.close()
    return s, json.loads(d) if d else {}


def test_claim_is_atomic(ex):
    decision(ex, "a")
    assert ex.claim("a", {"x": 1}) and state(ex, "a")[0] == "executing"
    assert not ex.claim("a", {"x": 2})                           # a second claimant never places it again


def test_failing_candidate_never_blocks_the_others(ex, monkeypatch):
    decision(ex, "bad")
    decision(ex, "good")
    seen = []

    def handle(cand):
        seen.append(cand["id"])
        if cand["id"] == "bad":
            raise sqlite3.OperationalError("database is locked")
        ex.store.set_execution_state(cand["id"], "executed", {"ok": True})

    monkeypatch.setattr(ex, "handle", handle)
    clock = [now_ms()]
    monkeypatch.setattr(ex_mod, "now_ms", lambda: clock[0])
    ex.process_candidates()
    assert seen == ["bad", "good"] and state(ex, "good")[0] == "executed"
    assert state(ex, "bad")[0] == "queued" and ex.attempts["bad"][0] == 1
    ex.process_candidates()                                        # inside the backoff window → not retried
    assert seen == ["bad", "good"]
    for _ in range(MAX_HANDLE_ATTEMPTS):
        clock[0] += 61_000
        ex.process_candidates()
    st, det = state(ex, "bad")
    assert st == "rejected" and "internal error" in det["reason"] and "bad" not in ex.attempts
    assert seen.count("bad") == MAX_HANDLE_ATTEMPTS


def test_error_after_claim_is_reconciled(ex, monkeypatch):
    decision(ex, "c")

    def handle(cand):
        ex.claim(cand["id"], {"gate": []})
        raise RuntimeError("backend exploded")

    monkeypatch.setattr(ex, "handle", handle)
    ex.process_candidates()
    st, det = state(ex, "c")
    assert st == "rejected" and "interrupted" in det["reason"] and "backend exploded" in det["reconciled"]


def test_startup_reconciles_executing_rows_from_the_backend(ex):
    decision(ex, "placed", state="executing")
    decision(ex, "lost", state="executing")
    t = Tick(now_ms(), 100.0, 100.2, now_ms() * 1000)
    rec = {"decision": "BUY", "order_type": "MARKET", "stop_loss": 90.0, "valid_until": iso(now_ms() + 60_000),
           "take_profits": [{"price": 120.0, "close_fraction": 1.0}]}
    assert ex.paper.place(decision_id="placed", pair="XAUUSD", instrument="mt5:XAUUSD@", rec=rec, lots=0.01, entry=t.ask,
                          contract_size=100, volume_step=0.01, volume_min=0.01, quote=t)["ok"]
    ex.reconcile_all()
    assert state(ex, "placed")[0] == "executed" and state(ex, "lost")[0] == "rejected"


def test_guards_expired_and_disabled_pairs(ex):
    decision(ex, "old", valid_ms=-1000)
    decision(ex, "gone", pair="NOPE")
    ex.process_candidates()
    assert state(ex, "old")[0] == "expired" and state(ex, "gone")[0] == "rejected"


def test_no_execution_quote_rejects_cleanly(ex):
    decision(ex, "q")
    ex.process_candidates()                                        # no tick store at all → clear rejection, no crash
    assert state(ex, "q") == ("rejected", {"reason": "no execution quote"})


def test_failing_loop_stops_refreshing_its_heartbeat(ex, monkeypatch):
    calls = []
    monkeypatch.setattr(ex.appdb, "set_status", lambda *a, **k: calls.append((a, k)))
    clock = [now_ms()]
    monkeypatch.setattr(ex_mod, "now_ms", lambda: clock[0])
    ex._loop_failed(RuntimeError("x"))
    assert calls[-1][0][1] == "error" and calls[-1][1]["detail"]["errors_last_5min"] == 1
    clock[0] += ex_mod.ERROR_HEARTBEAT_MS + 1_000
    n = len(calls)
    ex._loop_failed(RuntimeError("x"))                             # failing > 5 min → heartbeat left to go stale
    assert len(calls) == n
