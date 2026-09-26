"""Output contract v1 — semantic rules (pure-logic tests; prices are illustrative, not market data)."""
import copy

import pytest
from pydantic import ValidationError

from tradingsystem.ai.contract import Recommendation

BASE = {
    "pair": "XAUUSD", "timestamp": "2026-09-25T10:00:00Z", "valid_until": "2026-09-25T11:00:00Z",
    "price_reference": "mt5:XAUUSD@",
    "market_summary": "Uptrend on 4h, pullback into a 15m bullish order block.",
    "decision": "BUY", "order_type": "BUY_LIMIT",
    "entry": {"range_min": 4270.0, "range_max": 4274.0},
    "take_profits": [{"price": 4290.0, "close_fraction": 0.5}, {"price": 4305.0, "close_fraction": 0.5}],
    "stop_loss": 4262.0,
    "risk_management": {"risk_percent_suggested": 0.5, "risk_reward_ratio": 2.0,
                        "invalidation_reason": "15m close below 4262", "sl_basis": "below OB low minus 0.5 ATR"},
    "confidence": 64, "instructions": "Move SL to breakeven at TP1.",
    "management": [{"action": "move_sl_to_breakeven", "trigger": "tp_hit", "value": 1}],
    "next_review": {"in_minutes": 60, "conditions": [{"kind": "price_below", "value": 4262}]},
    "reasoning_trace": "HTF bullish; liquidity swept below Asia low; displacement created OB.",
}


def make(**changes):
    d = copy.deepcopy(BASE)
    for k, v in changes.items():
        if v is ...:
            d.pop(k, None)
        else:
            d[k] = v
    return d


def test_valid_buy_and_rr_recomputed():
    r = Recommendation.model_validate(make())
    # worst BUY fill = 4274; risk 12; reward (16*0.5 + 31*0.5) = 23.5 → RR 1.958
    assert r.rr_computed() == pytest.approx(23.5 / 12)
    assert not r.rr_mismatch()


@pytest.mark.parametrize("change,msg", [
    ({"stop_loss": None}, "without stop_loss"),
    ({"stop_loss": 4280.0}, "below the entry"),
    ({"order_type": "SELL_LIMIT"}, "not valid for BUY"),
    ({"take_profits": [{"price": 4268.0, "close_fraction": 1.0}]}, "above the entry"),
    ({"take_profits": [{"price": 4305.0, "close_fraction": 0.5}, {"price": 4290.0, "close_fraction": 0.5}]}, "ordered"),
    ({"take_profits": [{"price": 4290.0, "close_fraction": 0.7}, {"price": 4305.0, "close_fraction": 0.7}]}, "exceed"),
    ({"risk_management": None}, "requires risk_management"),
    ({"entry": {"range_min": 4280.0, "range_max": 4270.0}}, "range_min > range_max"),
    ({"valid_until": "2026-09-25T09:00:00Z"}, "valid_until"),
    ({"timestamp": "2026-09-25T10:00:00"}, "timezone"),
])
def test_invalid_trades_rejected(change, msg):
    with pytest.raises(ValidationError, match=msg):
        Recommendation.model_validate(make(**change))


def test_no_trade_is_first_class_and_carries_no_levels():
    ok = make(decision="NO_TRADE", order_type=None, entry=None, take_profits=[], stop_loss=None,
              risk_management=None, management=[])
    r = Recommendation.model_validate(ok)
    assert not r.is_trade and r.rr_computed() is None
    with pytest.raises(ValidationError, match="NO_TRADE must not"):
        Recommendation.model_validate(make(decision="NO_TRADE", order_type=None, entry=None, take_profits=[],
                                           risk_management=None))


def test_sell_market_with_reference_price():
    d = make(decision="SELL", order_type="MARKET", entry={"price": 4270.0}, stop_loss=4278.0,
             take_profits=[{"price": 4254.0, "close_fraction": 1.0}])
    r = Recommendation.model_validate(d)
    assert r.rr_computed() == pytest.approx(16 / 8)


def test_rr_mismatch_flagged():
    d = make()
    d["risk_management"]["risk_reward_ratio"] = 4.0
    assert Recommendation.model_validate(d).rr_mismatch()


def test_extra_fields_forbidden():
    with pytest.raises(ValidationError):
        Recommendation.model_validate(make(secret_sauce="x"))


def test_overlong_prose_is_trimmed_not_rejected():
    """D-035: a label/basis a few characters too long must not cost a whole repair round-trip."""
    from tradingsystem.ai.contract import Recommendation
    from tests.unit.test_orchestrator import rec_for
    r = rec_for()
    r["take_profits"][0]["label"] = "1h/5m liquidity, range low, previous day low cluster"
    r["risk_management"]["sl_basis"] = "x" * 450
    v = Recommendation.model_validate(r)
    assert len(v.take_profits[0].label) == 40 and len(v.risk_management.sl_basis) == 300


def test_no_trade_with_a_zero_risk_block_is_accepted_without_it():
    from tradingsystem.ai.contract import Recommendation
    from tests.unit.test_orchestrator import rec_for
    r = rec_for(decision="NO_TRADE")
    r["risk_management"] = {"risk_percent_suggested": 0, "risk_reward_ratio": 0, "invalidation_reason": "n/a",
                            "sl_basis": "n/a"}
    assert Recommendation.model_validate(r).risk_management is None
