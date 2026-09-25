"""GATE-01: a pending order must sit on the correct side of the touch by ≥ stops level (MT5 rule); a crossed order is
refused (MT5 answers 'invalid price'), so paper never fills what the broker would reject. Pure-logic inputs."""
import datetime as dt

import pytest

from tradingsystem.core.settings import RiskCfg
from tradingsystem.core.timeutil import to_ms
from tradingsystem.execution.risk_gate import ExecContext, evaluate, pending_price_check

NOW = to_ms(dt.datetime(2026, 9, 25, 10, 30, tzinfo=dt.timezone.utc))


@pytest.mark.parametrize("otype,price,ok", [
    ("BUY_LIMIT", 4296.0, True), ("BUY_LIMIT", 4300.1, False), ("BUY_LIMIT", 4301.0, False),
    ("BUY_STOP", 4301.0, True), ("BUY_STOP", 4300.4, False), ("BUY_STOP", 4299.0, False),
    ("SELL_LIMIT", 4301.0, True), ("SELL_LIMIT", 4300.1, False), ("SELL_LIMIT", 4299.0, False),
    ("SELL_STOP", 4299.0, True), ("SELL_STOP", 4299.9, False), ("SELL_STOP", 4301.0, False),
])
def test_pending_price_rule(otype, price, ok):
    assert pending_price_check(otype, price, bid=4300.0, ask=4300.26, stops_level=0.25)[0] is ok


def rec(**kw):
    r = {"pair": "XAUUSD", "timestamp": "2026-09-25T10:29:00Z", "valid_until": "2026-09-25T11:30:00Z",
         "decision": "BUY", "order_type": "BUY_LIMIT", "entry": {"range_min": 4290.0, "range_max": 4296.0},
         "stop_loss": 4284.0, "take_profits": [{"price": 4316.0, "close_fraction": 1.0}], "confidence": 64,
         "risk_management": {"risk_percent_suggested": 0.5}}
    r.update(kw)
    return r


def ctx(bid, ask):
    return ExecContext(now_ms=NOW, bid=bid, ask=ask, quote_age_s=0.5, market_open=True, atr=7.0, stops_level_price=0.25,
                       contract_size=100, volume_min=0.01, volume_step=0.01, volume_max=20, equity=10_000.0)


def test_limit_zone_already_crossed_is_rejected_market_is_not_checked():
    ok = evaluate(rec(), "XAUUSD", ctx(4300.0, 4300.26), RiskCfg(), [])
    assert ok.approved, ok.failures()
    crossed = evaluate(rec(), "XAUUSD", ctx(4293.0, 4293.26), RiskCfg(), [])       # ask inside the zone, below 4296
    assert not crossed.approved and [n for n, k, _ in crossed.checks if not k] == ["pending_price_valid"]
    mkt = evaluate(rec(order_type="MARKET", entry={"price": None}, stop_loss=4280.0,
                       take_profits=[{"price": 4330.0, "close_fraction": 1.0}]), "XAUUSD", ctx(4293.0, 4293.26), RiskCfg(), [])
    assert "pending_price_valid" not in [n for n, _, _ in mkt.checks]
