"""One executor candidate end to end on a paper account (B14): the same inputs are run for a pair with and without a
desk, and - for the golden - through the pre-B14 executor, whose stored result is frozen in
tests/fixtures/real/executor_gate_golden.json.

The gate, the sizing, the level translation, the store and the paper account are the real code. Three inputs are
hand-built pure-logic stand-ins (labelled): each pair's execution/analysis quote, the decision-timeframe ATR, and the
clock (NOW is fixed so the gate texts - ages, valid_until - are reproducible)."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from tradingsystem.ai.store import DecisionRecord
from tradingsystem.core.settings import PathsCfg, Settings, load_settings
from tradingsystem.core.timeutil import iso
from tradingsystem.execution import executor as ex_mod
from tradingsystem.execution.backends.paper import Tick
from tradingsystem.execution.executor import Executor

NOW = 1_790_000_000_000            # fixed clock (the gate texts carry ages and valid_until)

# hand-built stand-ins: execution quote (bid, ask), analysis quote (bid, ask), the decision-TF ATR
QUOTES = {"BTCUSDT": ((84_000.0, 84_026.0), (84_000.4, 84_000.6), 400.0),
          "ETHUSDT": ((4_000.0, 4_002.0), (4_000.5, 4_000.7), 20.0),
          "XAUUSD": ((4_300.00, 4_300.30), (4_300.00, 4_300.30), 8.0)}
EXEC_KEY = {"BTCUSDT": "mt5:BTCUSD@", "ETHUSDT": "mt5:ETHUSD@", "XAUUSD": "mt5:XAUUSD@"}
PRIM_KEY = {"BTCUSDT": "binance_spot:BTCUSDT", "ETHUSDT": "binance_spot:ETHUSDT", "XAUUSD": "mt5:XAUUSD@"}


class Open:
    def is_open(self, ms: int) -> bool:
        return True


def settings(tmp_path: Path, *, desk: bool, equity: float = 100_000.0, trigger: str = "manual") -> Settings:
    s = load_settings(env_path=Path("nope.env"), extra_env={"EXECUTION_MODE": "paper", "EXECUTION_TRIGGER": trigger,
                                                           "TS_INSTANCE": ""})
    upd = {"paths": PathsCfg(data_dir=str(tmp_path / "data"), logs_dir=str(tmp_path / "logs")),
           "execution": s.execution.model_copy(update={"paper_equity": equity})}
    if not desk:
        upd["pairs"] = {k: p.model_copy(update={"desk": None}) for k, p in s.pairs.items()}
    return s.model_copy(update=upd)


def make_executor(s: Settings, monkeypatch, *, no_quote: tuple[str, ...] = ()) -> Executor:
    monkeypatch.setattr(ex_mod, "calendar_for", lambda *a, **k: Open())
    monkeypatch.setattr(ex_mod, "now_ms", lambda: NOW)
    ex = Executor(s)

    def latest_quote(key, max_age_ms=600_000):
        for pair, ((b, a), (pb, pa), _) in QUOTES.items():
            if pair in no_quote:
                continue
            if key == EXEC_KEY[pair]:
                return Tick(NOW - 800, b, a, 1)
            if key == PRIM_KEY[pair] and key != EXEC_KEY[pair]:
                return Tick(NOW - 800, pb, pa, 1)
        return None

    ex.latest_quote = latest_quote
    ex.basis_history = lambda pair, minutes=60: []
    ex.atr = lambda pair, as_of=None: QUOTES[pair][2]
    return ex


def rec(pair: str, side: str = "BUY", order_type: str = "MARKET", *, sl_dist: float | None = None, rr: float = 2.0,
        confidence: int = 70, valid_ms: int = 3_600_000, age_s: int = 10) -> dict:
    (bid, ask), (pb, pa), atr = QUOTES[pair]
    buy = side == "BUY"
    dist = sl_dist if sl_dist is not None else atr * 1.2
    px = pa if buy else pb            # the analysis-instrument touch the model saw
    if order_type == "MARKET":
        entry = {"price": None}
        e = px
    elif order_type in ("BUY_LIMIT", "SELL_STOP"):
        e = round(px - 2 * dist, 2)
        entry = {"price": e}
    else:                             # BUY_STOP / SELL_LIMIT
        e = round(px + 2 * dist, 2)
        entry = {"price": e}
    sl = e - dist if buy else e + dist
    tp = e + rr * dist if buy else e - rr * dist
    return {"contract_version": "1.0", "pair": pair, "decision": side, "order_type": order_type, "entry": entry,
            "stop_loss": round(sl, 2), "take_profits": [{"price": round(tp, 2), "close_fraction": 1.0}],
            "timestamp": iso(NOW - age_s * 1000), "valid_until": iso(NOW + valid_ms), "confidence": confidence,
            "price_reference": PRIM_KEY[pair], "management": [], "risk_management": {"risk_percent_suggested": 1.0}}


def store_candidate(ex: Executor, pair: str, r: dict, did: str, state: str = "queued") -> dict:
    ex.store.save(DecisionRecord(pair, "agent_per_pair", "t", "valid", recommendation=r, id=did, ts=NOW - 10_000))
    ex.store.set_execution_state(did, state)
    return {"id": did, "ts": NOW - 10_000, "pair": pair, "rec": r, "state": state}


def stored(ex: Executor, did: str) -> tuple[str, dict]:
    con = sqlite3.connect(ex.app_db)
    try:
        st, det = con.execute("SELECT execution_state, execution_detail FROM ai_decisions WHERE id=?", (did,)).fetchone()
    finally:
        con.close()
    return st, (json.loads(det) if det else {})


def events(ex: Executor, kind: str) -> list[str]:
    con = sqlite3.connect(ex.app_db)
    try:
        return [r[0] for r in con.execute("SELECT detail FROM ingestion_events WHERE event=?", (kind,))]
    finally:
        con.close()


# the golden scenarios: (name, pair, kwargs of rec(), paper equity)
SCENARIOS = [
    ("btc_buy_market_places", "BTCUSDT", {}, 100_000.0),
    ("btc_sell_limit_places", "BTCUSDT", {"side": "SELL", "order_type": "SELL_LIMIT"}, 100_000.0),
    ("btc_size_fails_small_account", "BTCUSDT", {}, 100.0),
    ("btc_rr_too_low", "BTCUSDT", {"rr": 1.0}, 100_000.0),
    ("btc_stop_too_wide", "BTCUSDT", {"sl_dist": 400.0 * 6}, 100_000.0),
    ("btc_low_confidence", "BTCUSDT", {"confidence": 40}, 100_000.0),
    ("btc_expired", "BTCUSDT", {"valid_ms": -1000}, 100_000.0),
    ("eth_sell_market_places", "ETHUSDT", {"side": "SELL"}, 100_000.0),
    ("eth_buy_stop_pending", "ETHUSDT", {"order_type": "BUY_STOP"}, 100_000.0),
    ("eth_stale_recommendation", "ETHUSDT", {"age_s": 400}, 100_000.0),
]


def run_scenarios(tmp_path: Path, monkeypatch, *, desk: bool = False) -> dict[str, dict]:
    """Every scenario on a fresh executor: {name: {"state": ..., "detail": ...}} - what the store holds afterwards."""
    out = {}
    for i, (name, pair, kw, equity) in enumerate(SCENARIOS):
        sub = tmp_path / f"s{i}"
        sub.mkdir()
        ex = make_executor(settings(sub, desk=desk, equity=equity), monkeypatch)
        try:
            did = f"{i:032x}"
            cand = store_candidate(ex, pair, rec(pair, **kw), did)
            ex.handle(cand)
            st, det = stored(ex, did)
            det.pop("backend", None)                     # paper leg ids / fill prices are not the gate's output
            out[name] = {"state": st, "detail": det}
        finally:
            ex.paper.close()
    return out
