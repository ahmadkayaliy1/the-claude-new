"""B13: which rule sets ``min_stop_distance`` (the venue's stops level, or the SYSTEM's ATR floor / spread rule) and
whether the minimum lot fits now (per-trade risk cap AND leverage cap; the gate's leverage check only runs after
sizing passes, so gold's 42x never showed). Contract specs are the real Windsor ones (config.yaml, P1.5); prices are
XAU ~4130 / ETH ~4000 style quotes."""
from pathlib import Path

import numpy as np
import pytest

from tradingsystem.analysis.snapshot import SnapshotBuilder, execution_costs
from tradingsystem.core.instruments import InstrumentRegistry
from tradingsystem.core.settings import RiskCfg, load_settings
from tradingsystem.execution.sizing import min_lot_fit
from tradingsystem.ai.model_view import model_view

RISK = RiskCfg(risk_per_trade_pct=1, max_risk_per_trade_pct=3)
XAU = {"contract_size": 100, "volume_min": 0.01, "tick_size": 0.01, "stops_level_points": 25}
CALM = np.array([0.30] * 20)                       # a normal quiet-market spread series


def costs(contract, bid, ask, atr, s1=CALM, s24=CALM):
    return execution_costs(contract, bid, ask, s1, s24, atr, RISK, 2)


def test_label_atr_floor():
    c = costs(XAU, 4130.0, 4130.3, 7.0)                               # 0.5 x 7 = 3.5 > 0.25 + 0.3 and > 0.3 / 0.2
    assert c["min_stop_set_by"] == "system_atr_floor" and c["stops_level"] == 0.25


def test_label_venue_stops_plus_spread():
    wide = {**XAU, "stops_level_points": 900}                         # a venue that demands 9.00 from the price
    c = costs(wide, 4130.0, 4130.3, 7.0)
    assert c["min_stop_set_by"] == "venue_stops_plus_spread" and c["min_stop_distance"] >= (9.0 + 0.3) * 1.1 - 0.01


def test_label_system_spread_rule():
    s = np.array([2.0] * 20)                                          # 2.00 spread / 20 % = 10.00 > ATR floor 3.5
    c = costs(XAU, 4130.0, 4132.0, 7.0, s, s)
    assert c["min_stop_set_by"] == "system_spread_rule" and c["min_stop_distance"] == pytest.approx(11.0)


def test_label_without_atr_names_a_venue_or_spread_rule():
    c = costs(XAU, 4130.0, 4130.3, None)
    assert c["min_stop_set_by"] in {"venue_stops_plus_spread", "system_spread_rule"}


def builder():
    s = load_settings(env_path=Path("nope.env"))
    return s, SnapshotBuilder(s, InstrumentRegistry.from_settings(s))


def test_gold_at_99_does_not_fit_and_needs_hundreds():
    s, b = builder()
    execu = b.reg.with_role("XAUUSD", "execution")[0]
    m = b._account({"equity": 99.0, "currency": "USD"}, execu, 7.0, 3.85, 2, price=4130.0)["min_position_risk"]
    assert m["fits_now"] is False
    assert m["leverage_at_min_lot"] == pytest.approx(41.7, abs=0.1) and m["leverage_cap"] == s.risk.max_effective_leverage
    # the equity that carries 1 oz: notional 4130 / cap 10 = 413 (leverage binds); risk 3.85 / cap % is lower
    need = max(3.85 / (s.risk.max_risk_per_trade_pct / 100), 4130.0 / s.risk.max_effective_leverage)
    assert m["equity_for_min_lot"] == round(need) and 300 < m["equity_for_min_lot"] < 1000
    # at that equity the same call says it fits
    m2 = b._account({"equity": need + 1, "currency": "USD"}, execu, 7.0, 3.85, 2, price=4130.0)["min_position_risk"]
    assert m2["fits_now"] is True


def test_eth_at_100_fits():
    s, b = builder()
    execu = b.reg.with_role("ETHUSDT", "execution")[0]                 # 0.01 lot x 10 = 0.1 ETH
    m = b._account({"equity": 100.0, "currency": "USD"}, execu, 30.0, 8.0, 2, price=4000.0)["min_position_risk"]
    assert m["leverage_at_min_lot"] == pytest.approx(4.0) and m["fits_now"] is True
    assert m["equity_for_min_lot"] < 100


def test_missing_inputs_never_raise():
    s, b = builder()
    execu = b.reg.with_role("XAUUSD", "execution")[0]
    for price in (None, 0.0, float("nan")):
        m = b._account({"equity": 99.0}, execu, 7.0, 3.85, 2, price=price)["min_position_risk"]
        assert "risk_pct_at_min_lot_and_min_stop" in m and (price != price or not price)  # old fields kept
        assert "fits_now" not in m or price != price                                       # omitted without a price
    assert b._account({"equity": 99.0}, execu, None, None, 2, price=4130.0).get("min_position_risk") is None
    assert b._account({}, execu, 7.0, 3.85, 2, price=4130.0) == {}


def test_min_lot_fit_matches_the_gate_arithmetic():
    f = min_lot_fit(equity=99.0, entry=4130.0, stop_dist=3.85, contract_size=100, volume_min=0.01, volume_step=0.01,
                    max_risk_pct=3.0, max_leverage=10.0)
    assert f["leverage_at_min_lot"] == pytest.approx(0.01 * 100 * 4130.0 / 99.0) and not f["fits_now"]


def test_model_view_renders_the_fit_flag_as_int():
    v = model_view({"meta": {"as_of": "2026-09-29T10:00:00Z"},
                    "account": {"equity": 99, "min_position_risk": {"fits_now": False, "equity_for_min_lot": 413}}})
    assert v["account"]["min_position_risk"] == {"fits_now": 0, "equity_for_min_lot": 413}
