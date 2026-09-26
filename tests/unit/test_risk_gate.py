"""Risk gate, sizing and price translation — table-driven (contract specs measured on Windsor, P1.5)."""
import copy
import datetime as dt

import pytest

from tradingsystem.core.settings import RiskCfg
from tradingsystem.core.timeutil import to_ms
from tradingsystem.execution.price_mapping import check_basis, translate
from tradingsystem.execution.risk_gate import ExecContext, evaluate
from tradingsystem.execution.sizing import size_position

NOW = to_ms(dt.datetime(2026, 9, 25, 10, 30, tzinfo=dt.timezone.utc))


def rec(**kw):
    r = {"pair": "XAUUSD", "timestamp": "2026-09-25T10:29:00Z", "valid_until": "2026-09-25T11:30:00Z",
         "price_reference": "mt5:XAUUSD@", "decision": "BUY", "order_type": "BUY_LIMIT",
         "entry": {"range_min": 4290.0, "range_max": 4296.0}, "stop_loss": 4284.0,
         "take_profits": [{"price": 4316.0, "close_fraction": 1.0}], "confidence": 64,
         "risk_management": {"risk_percent_suggested": 0.5, "risk_reward_ratio": 1.7,
                             "invalidation_reason": "x", "sl_basis": "y"}}
    r.update(kw)
    return r


def ctx(**kw):
    base = dict(now_ms=NOW, bid=4300.0, ask=4300.26, quote_age_s=0.5, market_open=True, atr=7.0,
                stops_level_price=0.25, contract_size=100, volume_min=0.01, volume_step=0.01, volume_max=20,
                equity=10_000.0)
    base.update(kw)
    return ExecContext(**base)


RISK = RiskCfg()          # 0.5 % / max 1 % / RR ≥ 1.5 / SL 0.5–5 ATR / spread ≤ 20 % of SL


def test_good_trade_passes_and_is_sized():
    g = evaluate(rec(), "XAUUSD", ctx(), RISK, [])
    assert g.approved, g.failures()
    # worst BUY_LIMIT fill = 4296; risk 12 × 100 = $1200/lot; 0.5 % of 10k = $50 → 0.04 lots
    assert g.entry == 4296.0 and g.size.lots == pytest.approx(0.04) and g.rr_exec == pytest.approx(20 / 12)


@pytest.mark.parametrize("change,ctxchange,failing", [
    ({"stop_loss": None}, {}, "stop_loss_present"),
    ({}, {"market_open": False}, "market_open"),
    ({}, {"quote_age_s": 120}, "quote_fresh"),
    ({"valid_until": "2026-09-25T10:00:00Z", "timestamp": "2026-09-25T09:59:00Z"}, {}, "not_expired"),
    ({"confidence": 40}, {}, "confidence"),
    ({"stop_loss": 4294.0, "entry": {"price": 4296.0}}, {}, "sl_min_distance"),        # 2 < 0.5 ATR
    ({"stop_loss": 4250.0}, {}, "sl_max_distance"),                                     # 46 > 5 ATR
    ({"take_profits": [{"price": 4310.0, "close_fraction": 1.0}]}, {}, "rr_after_costs"),  # 14/12 < 1.5
    ({}, {"bid": 4299.0, "ask": 4302.0}, "spread_vs_sl"),                               # 3 > 20 % of 12
    ({}, {"open_positions": 3}, "max_open_positions"),
    ({}, {"realized_pnl_today_usd": -250}, "daily_loss_limit"),
    ({}, {"kill_switch": True}, "kill_switch"),
    ({}, {"basis_ok": False, "basis_reason": "deviation"}, "basis"),
])
def test_each_rule_can_reject(change, ctxchange, failing):
    g = evaluate(rec(**change), "XAUUSD", ctx(**ctxchange), RISK, [])
    assert not g.approved
    assert failing in [n for n, ok, _ in g.checks if not ok]


def test_small_account_min_lot_too_risky():
    """$100 account (D-021): 0.01 lot of gold with a 12-point stop risks 12 % → rejected."""
    g = evaluate(rec(), "XAUUSD", ctx(equity=100.0), RISK, [])
    assert not g.approved and "position_size" in [n for n, ok, _ in g.checks if not ok]
    assert g.size.risk_pct == pytest.approx(12.0)


def test_correlated_exposure():
    r = rec(pair="ETHUSDT")
    g = evaluate(r, "ETHUSDT", ctx(contract_size=10, open_risk_pct_by_pair={"BTCUSDT": 0.8}), RISK,
                 [["BTCUSDT", "ETHUSDT"]])
    assert "correlated_exposure" in [n for n, ok, _ in g.checks if not ok]


def test_no_trade_never_passes():
    g = evaluate({"decision": "NO_TRADE"}, "XAUUSD", ctx(), RISK, [])
    assert not g.approved


def test_sizing_rounds_down_and_min_lot_rules():
    s = size_position(equity=1000, target_risk_pct=0.5, max_risk_pct=1.0, entry=84000, stop=83500,
                      contract_size=1, volume_min=0.01, volume_step=0.01)
    assert s.lots == 0.01 and s.risk_pct == pytest.approx(0.5)            # $5 exactly
    s2 = size_position(equity=1000, target_risk_pct=0.5, max_risk_pct=1.0, entry=84000, stop=83300,
                       contract_size=1, volume_min=0.01, volume_step=0.01)
    assert s2.ok and s2.lots == 0.01 and s2.risk_pct == pytest.approx(0.7)  # min lot within max
    s3 = size_position(equity=1000, target_risk_pct=0.5, max_risk_pct=0.6, entry=84000, stop=83300,
                       contract_size=1, volume_min=0.01, volume_step=0.01)
    assert not s3.ok and s3.min_lot_risk_pct == pytest.approx(0.7)


def test_translation_preserves_distances_and_basis_check():
    r = rec(price_reference="binance_spot:BTCUSDT", entry={"price": 84000.0}, stop_loss=83500.0,
            take_profits=[{"price": 85000.0, "close_fraction": 1.0}], order_type="BUY_LIMIT")
    t = translate(r, -16.0, 0.01)
    assert t["entry"]["price"] == 83984.0 and t["stop_loss"] == 83484.0 and t["take_profits"][0]["price"] == 84984.0
    assert check_basis(84000, 83984, [-15, -17, -16], 0.15).ok
    bad = check_basis(84000, 83700, [-15, -17, -16], 0.15)
    assert not bad.ok and "deviates" in bad.reason


def test_daily_worst_case_counts_open_risk_and_the_new_trade():
    """D-036: at a 10 % daily limit, realised −6 % + open SL risk 3 % + this trade must stay ≥ −10 % (the new trade risks 0.48 %)."""
    r = RiskCfg(risk_per_trade_pct=1.0, max_risk_per_trade_pct=3.0, max_daily_loss_pct=10.0, max_correlated_risk_pct=10)
    ok = evaluate(rec(), "XAUUSD", ctx(realized_pnl_today_usd=-600, open_risk_pct_by_pair={"BTCUSDT": 3.0}), r, [])
    assert ok.approved, ok.failures()                                           # −6 − 3 − 0.48 = −9.5 %
    bad = evaluate(rec(), "XAUUSD", ctx(realized_pnl_today_usd=-600, open_risk_pct_by_pair={"BTCUSDT": 4.0}), r, [])
    assert [c for c, okc, _ in bad.checks if not okc] == ["daily_loss_worst_case"]  # −6 − 4 − 0.48 < −10 %
