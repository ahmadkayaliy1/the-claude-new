"""Paper backend on real XAUUSD@ ticks: fills on the correct side, exits, PnL, expiry, breakeven, idempotency."""
import pytest

from tradingsystem.execution.backends.paper import PaperBackend, Tick, split_volume
from tradingsystem.core.timeutil import iso


@pytest.fixture
def ticks(real_xau_ticks):
    t = real_xau_ticks
    return [Tick(int(a), float(b), float(c)) for a, b, c in zip(t["time_msc"].to_pylist(), t["bid"].to_pylist(),
                                                                  t["ask"].to_pylist())]


def rec(side, order_type, sl, tps, valid_until_ms, entry=None, mgmt=None):
    r = {"decision": side, "order_type": order_type, "stop_loss": sl, "valid_until": iso(valid_until_ms),
         "take_profits": [{"price": p, "close_fraction": f} for p, f in tps], "management": mgmt or []}
    r["entry"] = {"price": entry} if entry else {"price": None}
    return r


def first_cross(ticks, start, pred):
    return next((t for t in ticks[start:] if pred(t)), None)


def test_market_buy_exits_on_bid_with_correct_pnl(tmp_path, ticks):
    pb = PaperBackend(tmp_path / "app.db", 10_000)
    t0 = ticks[0]
    d = 0.5
    sl, tp = round(t0.bid - d, 2), round(t0.ask + d, 2)
    out = pb.place(decision_id="d1", pair="XAUUSD", instrument="mt5:XAUUSD@", rec=rec("BUY", "MARKET", sl, [(tp, 1.0)], t0.time_msc + 3_600_000),
                   lots=0.02, entry=t0.ask, contract_size=100, volume_step=0.01, volume_min=0.01, quote=t0)
    assert out["ok"] and out["fill_price"] == t0.ask
    ev = pb.process("mt5:XAUUSD@", ticks[1:])
    exp = first_cross(ticks, 1, lambda t: t.bid <= sl or t.bid >= tp)
    if exp is None:
        assert ev == []
        return
    [e] = ev
    if exp.bid <= sl:
        assert e["event"] == "sl" and e["price"] == exp.bid
    else:
        assert e["event"] == "tp" and e["price"] == tp
    assert e["pnl_usd"] == pytest.approx((e["price"] - t0.ask) * 0.02 * 100, abs=1e-6)
    acct = pb.account()
    assert acct["balance"] == pytest.approx(10_000 + e["pnl_usd"], abs=1e-6)


def test_buy_limit_fills_only_when_ask_reaches_price_and_expires(tmp_path, ticks):
    pb = PaperBackend(tmp_path / "app.db", 10_000)
    t0 = ticks[0]
    lo_ask = min(t.ask for t in ticks[1:])
    far = round(lo_ask - 5, 2)                        # never reached → expires
    pb.place(decision_id="d2", pair="XAUUSD", instrument="mt5:XAUUSD@",
             rec=rec("BUY", "BUY_LIMIT", far - 3, [(far + 10, 1.0)], ticks[200].time_msc, entry=far),
             lots=0.01, entry=far, contract_size=100, volume_step=0.01, volume_min=0.01, quote=t0)
    ev = pb.process("mt5:XAUUSD@", ticks[1:])
    assert [e["event"] for e in ev] == ["expired"]
    near = round(lo_ask + 0.02, 2)                     # reached at some point → fills at ask ≤ price
    pb.place(decision_id="d3", pair="XAUUSD", instrument="mt5:XAUUSD@",
             rec=rec("BUY", "BUY_LIMIT", near - 50, [(near + 50, 1.0)], ticks[-1].time_msc + 1, entry=near),
             lots=0.01, entry=near, contract_size=100, volume_step=0.01, volume_min=0.01, quote=t0)
    ev = pb.process("mt5:XAUUSD@", ticks[1:])
    fills = [e for e in ev if e["event"] == "filled"]
    assert len(fills) == 1 and fills[0]["price"] <= near


def test_split_legs_breakeven_and_idempotency(tmp_path, ticks):
    assert split_volume(0.05, [0.5, 0.5], 0.01, 0.01) == [0.03, 0.02] or split_volume(0.05, [0.5, 0.5], 0.01, 0.01) == [0.02, 0.03]
    assert split_volume(0.01, [0.5, 0.5], 0.01, 0.01) is None
    pb = PaperBackend(tmp_path / "app.db", 10_000)
    t0 = ticks[0]
    r = rec("SELL", "MARKET", round(t0.ask + 30, 2), [(round(t0.bid - 0.3, 2), 0.5), (round(t0.bid - 40, 2), 0.5)],
            t0.time_msc + 3_600_000, mgmt=[{"action": "move_sl_to_breakeven", "trigger": "tp_hit", "value": 1}])
    out = pb.place(decision_id="d4", pair="XAUUSD", instrument="mt5:XAUUSD@", rec=r, lots=0.02, entry=t0.bid,
                   contract_size=100, volume_step=0.01, volume_min=0.01, quote=t0)
    assert out["ok"] and len(out["legs"]) == 2
    again = pb.place(decision_id="d4", pair="XAUUSD", instrument="mt5:XAUUSD@", rec=r, lots=0.02, entry=t0.bid,
                     contract_size=100, volume_step=0.01, volume_min=0.01, quote=t0)
    assert not again["ok"] and "duplicate" in again["reason"]
    pb.process("mt5:XAUUSD@", ticks[1:])
    legs = pb.decision_legs("d4")
    if legs[0]["status"] == "closed" and legs[0]["close_reason"] == "tp":
        assert legs[1]["sl"] == pytest.approx(legs[1]["fill_price"])     # moved to breakeven
