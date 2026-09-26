"""Data honesty and the AI data gate (ai_path F10, F11; stall F5): frame quality on real candles, confluence
without phantom timeframes, the minimum-position risk block and ``data_problems`` on the real XAUUSD payload."""
import copy
import csv
import json
from pathlib import Path

import numpy as np
import pytest

from tradingsystem.analysis import context as ctx
from tradingsystem.analysis.frames import Frame, _quality
from tradingsystem.analysis.snapshot import SnapshotBuilder, data_problems
from tradingsystem.core.instruments import InstrumentRegistry
from tradingsystem.core.settings import load_settings
from tradingsystem.core.timeframes import Timeframe
from tradingsystem.core.timeutil import parse_date_spec

REAL = Path(__file__).resolve().parents[1] / "fixtures" / "real"
M1 = Timeframe.parse("1m")


@pytest.fixture(scope="module")
def payload():
    return json.loads((REAL / "payload_xauusd.json").read_text())


@pytest.fixture(scope="module")
def candles():
    with open(REAL / "btcusdt_candles_1m_3000.csv", newline="") as f:
        rows = list(csv.DictReader(f))
    return np.array([int(r["open_time"]) for r in rows[-300:]], dtype=np.int64), \
        np.array([float(r["close"]) for r in rows[-300:]])


def frame(ot, c):
    return Frame("binance_spot:BTCUSDT", M1, ot, c, c, c, c, np.ones(len(c)), "traded")


def test_quality_flags_missing_last_bar_and_short_history(candles):
    ot, c = candles
    end = int(ot[-1]) + M1.ms
    q = _quality(frame(ot, c), M1, end + 5_000, None, False, 240)
    assert q["status"] == "ok" and not q["missing_last_bar"] and q["coverage"] == 1.25
    q = _quality(frame(ot, c), M1, end + 2 * M1.ms + 5_000, None, False, 240)      # 2 closed bars not stored
    assert q["status"] == "missing_last_bar" and not q["stale"]
    q = _quality(frame(ot[-12:], c[-12:]), M1, end + 5_000, None, False, 240)
    assert q["status"] == "short_history" and q["short_history"]


def test_confluence_leaves_out_timeframes_without_readings():
    c = ctx.confluence({"1d": {"trend": None, "ema_alignment": None},
                        "1h": {"trend": "bullish", "ema_alignment": "bullish"}})
    assert c["bias"] == "bullish" and c["missing"] == ["1d"]


def test_data_gate_on_the_real_xau_payload(payload):
    as_of = parse_date_spec(payload["meta"]["as_of"])
    probs = data_problems(payload, as_of, 120)
    assert any(p.startswith("4h: only 18 bars") for p in probs) and any(p.startswith("1d: only 3 bars") for p in probs)
    assert not any("analysis price" in p or "decision candles" in p for p in probs)
    p = copy.deepcopy(payload)
    p["timeframes"]["4h"]["bars"] = p["timeframes"]["1d"]["bars"] = 200
    assert data_problems(p, as_of, 120) == []
    p["market"]["analysis_price"]["age_s"] = 600.0
    p["timeframes"]["15m"]["quality"]["status"] = "missing_last_bar"
    p["timeframes"]["15m"]["quality"]["gaps"] = [[as_of - 3 * 900_000, 2]]
    probs = data_problems(p, as_of, 120)
    assert len(probs) == 3 and any("600s old" in x for x in probs) and any("2 missing bar" in x for x in probs)
    p["market"]["execution_market_open"] = False                       # closed market: quote age is irrelevant
    assert not any("analysis price" in x for x in data_problems(p, as_of, 120))


def test_min_position_risk_block(payload):
    s = load_settings(env_path=Path("nope.env"))
    reg = InstrumentRegistry.from_settings(s)
    b = SnapshotBuilder(s, reg)
    execu = reg.with_role("XAUUSD", "execution")[0]
    atr = payload["timeframes"]["15m"]["indicators"]["atr14"]
    m = b._account({"equity": 100, "currency": "USD"}, execu, atr, None, 2)["min_position_risk"]
    assert m["usd_per_price_unit_at_min_lot"] == 1.0                      # 0.01 lot × 100 oz
    assert m["min_stop_distance"] == pytest.approx(s.risk.sl_atr_min_mult * 6.97, abs=0.01)
    assert m["risk_pct_at_min_lot_and_min_stop"] == pytest.approx(m["min_stop_distance"], abs=0.01)
    assert m["max_stop_distance_at_min_lot_within_max_risk"] == pytest.approx(s.risk.max_risk_per_trade_pct)
    # the gate's real minimum (market.execution.costs.min_stop_distance) wins over the ATR floor when given
    m = b._account({"equity": 100, "currency": "USD"}, execu, atr, 4.8, 2)["min_position_risk"]
    assert m["min_stop_distance"] == 4.8 and m["risk_pct_at_min_lot_and_min_stop"] == pytest.approx(4.8)
    assert b._account(None, execu, atr, None, 2) == {}
