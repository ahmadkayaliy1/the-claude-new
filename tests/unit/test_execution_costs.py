"""Execution costs in the snapshot mirror the risk gate (Phase 1); Windsor specs measured in P1.5."""
import numpy as np
import pytest

from tradingsystem.analysis.snapshot import STOP_BOUND_MARGIN, execution_costs
from tradingsystem.core.settings import RiskCfg

BTC = {"contract_size": 1.0, "volume_min": 0.01, "tick_size": 0.01, "stops_level_points": 2500,
       "swap_long": -15, "swap_short": -15, "swap_mode": "annual_pct", "triple_swap_weekday": "Wed"}
RISK = RiskCfg(risk_per_trade_pct=1, max_risk_per_trade_pct=3)


def gate_min(spread: float, atr: float, stops: float = 25.0) -> float:
    """risk_gate.evaluate: max(stops_level + spread, sl_atr_min_mult × ATR) and spread <= 20 % of the stop."""
    return max(stops + spread, RISK.sl_atr_min_mult * atr, spread / RISK.max_spread_to_sl_ratio)


def test_min_stop_holds_for_any_spread_up_to_the_conservative_one():
    """Review 2026-09-26: the spread 1-2.5 min later exceeds the current one in up to 24 % of cases; the bound
    shown to the model uses max(now, 1h p90, 24h p95) plus a 10 % margin, so it passes the gate for every spread up
    to that value (and a bit beyond)."""
    s1h = np.array([26.0] * 50 + [30.0] * 10)
    s24h = np.array([26.0] * 90 + [40.0] * 10)
    c = execution_costs(BTC, 84238.14, 84264.14, s1h, s24h, 150.0, RISK, 2)
    conservative = max(26.0, float(np.percentile(s1h, 90)), float(np.percentile(s24h, 95)))
    assert c["min_stop_distance"] >= gate_min(conservative, 150.0) * STOP_BOUND_MARGIN - 0.01
    for spread in (26.0, 30.0, conservative, conservative * 1.09):
        assert c["min_stop_distance"] >= gate_min(spread, 150.0), spread
    assert c["min_stop_set_by"] == "spread_rule" and c["stops_level"] == 25.0 and c["spread_now"] == 26.0
    assert c["max_stop_distance"] <= 5 * 150.0 / STOP_BOUND_MARGIN + 1e-9           # rounded down, inside the gate
    assert c["swap_per_night_at_min_lot_usd"]["long"] == pytest.approx(-84251.14 * 0.01 * 0.15 / 360, abs=1e-3)


def test_atr_floor_and_rounding():
    c = execution_costs(BTC, 84238.14, 84264.14, np.array([]), np.array([]), 400.0, RISK, 2)
    assert c["min_stop_set_by"] == "atr_floor" and c["min_stop_distance"] == pytest.approx(220.0)   # 200 × 1.1
    assert c["max_stop_distance"] == pytest.approx(1818.18)                                        # 2000 / 1.1 down
    xau = {"contract_size": 100, "volume_min": 0.01, "tick_size": 0.01, "stops_level_points": 25,
           "swap_long": -37.2, "swap_short": 21.15, "swap_mode": "points"}
    x = execution_costs(xau, 4300.0, 4300.48, np.array([0.48]), np.array([0.48]), 7.0, RISK, 2)
    sw = x["swap_per_night_at_min_lot_usd"]                  # points × tick × contract × min lot
    assert x["min_stop_distance"] == pytest.approx(3.85)      # 0.5 × 7 = 3.5 → × 1.1
    assert sw["long"] == pytest.approx(-0.372) and sw["short"] == pytest.approx(0.2115, abs=1e-3)
