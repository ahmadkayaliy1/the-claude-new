"""P9.6: a decision's outcome from the broker's records (history orders → positions → deals). Control flow on the
MetaTrader5 call contract; values are shaped like the terminal's named tuples."""
from types import SimpleNamespace as NS

import pytest

from tradingsystem.execution.backends.mt5_backend import MT5Backend

DID = "d" * 32
TAG = "ts:" + "d" * 20 + ":"
IN, OUT, BUY, SELL = 0, 1, 0, 1


def backend(orders=(), positions=(), hist=(), deals_by_pos=None, err=(1, "Success")):
    b = object.__new__(MT5Backend)
    b.magic, b.adopt_magic, b.own_pairs, b.family, b.pair_by_symbol = 7, None, set(), {7}, {}
    b.t = NS(healthy=lambda: True)
    b.mt5 = NS(orders_get=lambda: orders, positions_get=lambda: positions,
               history_orders_get=lambda a, z: hist, history_deals_get=lambda position: (deals_by_pos or {}).get(position),
               last_error=lambda: err, DEAL_ENTRY_IN=IN, DEAL_ENTRY_OUT=OUT, DEAL_ENTRY_OUT_BY=3, DEAL_TYPE_BUY=BUY)
    return b


def order(leg, pid=0, magic=7):
    return NS(comment=f"{TAG}{leg}", magic=magic, position_id=pid)


def deal(entry, typ, price, vol, profit=0.0, commission=0.0, swap=0.0):
    return NS(entry=entry, type=typ, price=price, volume=vol, profit=profit, commission=commission, swap=swap, fee=0.0)


def test_open_leg_means_not_settled():
    assert backend(orders=[NS(comment=f"{TAG}1")], hist=[order(1)]).decision_result(DID, 0) is None
    assert backend(positions=[NS(comment="x", identifier=11)], hist=[order(0, pid=11)]).decision_result(DID, 0) is None


def test_expired_pending_order_is_not_filled():
    r = backend(hist=[order(0)]).decision_result(DID, 0)
    assert r["filled"] is False and r["pnl_usd"] == 0.0


def test_two_legs_closed_sum_profit_commission_swap():
    deals = {11: [deal(IN, SELL, 84000, 0.01, commission=-0.05), deal(OUT, BUY, 83628, 0.01, profit=3.72)],
             12: [deal(IN, SELL, 84000, 0.01, commission=-0.05), deal(OUT, BUY, 84215, 0.01, profit=-2.15, swap=-0.01)]}
    r = backend(hist=[order(0, 11), order(1, 12), order(1, 99, magic=8)], deals_by_pos=deals).decision_result(DID, 0)
    assert r["filled"] and r["pnl_usd"] == pytest.approx(3.72 - 2.15 - 0.10 - 0.01)
    assert r["move"] == pytest.approx(((84000 - 83628) + (84000 - 84215)) / 2)


def test_terminal_down_raises():
    with pytest.raises(RuntimeError):
        backend(err=(-10004, "No IPC"), orders=None).decision_result(DID, 0)


def test_opening_deal_only_is_not_final():
    """Review #1: a position whose closing deal is not in the history yet (terminal still syncing) never settles."""
    deals = {11: [deal(IN, SELL, 84000, 0.01, commission=-0.02)]}
    assert backend(hist=[order(1, 11)], deals_by_pos=deals).decision_result(DID, 0) is None
    assert backend(hist=[order(1, 11)], deals_by_pos={11: ()}).decision_result(DID, 0) is None


def test_missing_leg_in_history_is_not_final():
    deals = {11: [deal(IN, SELL, 84000, 0.01), deal(OUT, BUY, 83628, 0.01, profit=3.72)]}
    b = backend(hist=[order(1, 11)], deals_by_pos=deals)
    assert b.decision_result(DID, 0, legs={f"{TAG}1", f"{TAG}2"}) is None
    assert b.decision_result(DID, 0, legs={f"{TAG}1"})["pnl_usd"] == pytest.approx(3.72)


def test_disconnected_terminal_settles_nothing():
    b = backend(hist=[order(1)])
    b.t = NS(healthy=lambda: False)
    assert b.decision_result(DID, 0) is None


def test_account_fails_closed_when_the_terminal_cannot_list():
    """Review #2: the 10 %/day guarantee must never read 'no positions / no loss today' from a failed call."""
    b = backend(orders=None, err=(-10004, "No IPC"))
    b.t = NS(healthy=lambda: True, account=lambda: NS(currency="USD", balance=100.0, equity=100.0, margin_free=100.0))
    b.mt5.ORDER_TYPE_BUY, b.mt5.ORDER_TYPE_BUY_LIMIT, b.mt5.ORDER_TYPE_BUY_STOP = 0, 2, 4
    with pytest.raises(RuntimeError, match="could not list"):
        b.account()
