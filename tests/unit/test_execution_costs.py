"""Execution costs in the snapshot mirror the risk gate (Phase 1); Windsor specs measured in P1.5."""
import pytest


def test_execution_costs_mirror_the_gate():
    """Phase 1: the snapshot's minimum stop equals the gate's rule (stops level + spread, 0.5 ATR, spread / 20 %),
    measured with Windsor's real BTCUSD@ specs (P1.5: stops 2500 points, spread ~$26, swap -15 %/year)."""
    import numpy as np
    from tradingsystem.analysis.snapshot import execution_costs
    from tradingsystem.core.settings import RiskCfg
    btc = {"contract_size": 1.0, "volume_min": 0.01, "tick_size": 0.01, "stops_level_points": 2500,
           "swap_long": -15, "swap_short": -15, "swap_mode": "annual_pct", "triple_swap_weekday": "Wed"}
    risk = RiskCfg(risk_per_trade_pct=1, max_risk_per_trade_pct=3)
    c = execution_costs(btc, 84238.14, 84264.14, np.array([26.0, 26.0, 30.0]), np.array([20.0, 26.0, 80.0]),
                        150.0, risk, 2)
    assert c["spread_now"] == 26.0 and c["stops_level"] == 25.0
    assert c["min_stop_distance"] == 130.0 and c["min_stop_set_by"] == "spread_rule"      # 26 / 0.2 > 25 + 26, 75
    assert c["max_stop_distance"] == 750.0 and c["max_spread_pct_of_stop"] == 20
    assert c["swap_per_night_at_min_lot_usd"]["long"] == pytest.approx(-84251.14 * 0.01 * 0.15 / 360, abs=1e-3)
    wide_atr = execution_costs(btc, 84238.14, 84264.14, np.array([]), np.array([]), 400.0, risk, 2)
    assert wide_atr["min_stop_distance"] == 200.0 and wide_atr["min_stop_set_by"] == "atr_floor"
    xau = {"contract_size": 100, "volume_min": 0.01, "tick_size": 0.01, "stops_level_points": 25,
           "swap_long": -37.2, "swap_short": 21.15, "swap_mode": "points"}
    x = execution_costs(xau, 4300.0, 4300.48, np.array([0.48]), np.array([0.48]), 7.0, risk, 2)
    sw = x["swap_per_night_at_min_lot_usd"]                  # points × tick × contract × min lot
    assert x["min_stop_distance"] == 3.5 and sw["long"] == pytest.approx(-0.372) and sw["short"] == pytest.approx(0.2115, abs=1e-3)
