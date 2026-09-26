"""P9.5 — controlled order test on the MT5 **demo** account through the production execution code (MT5Backend).

1. BUY_LIMIT 0.01 far below the market (never fills) with SL/TP and an expiry → verify it at the broker → cancel
   → verify it is gone and settles as "not filled".
2. MARKET BUY 0.01 with SL/TP → verify the position (SL/TP attached, magic, tag) → close it at once → read the
   deals: fill prices, commission, swap, net result (the real round-trip cost).

Refuses anything but a demo account (trade_mode 0 on the configured demo server). Levels come from the live quote;
this is an execution test, not a trading decision. Usage: ``python tools/demo_order_test.py [SYMBOL]``
(default ETHUSD@, the cheapest round trip at 0.01 lot). Prints a JSON report.
"""
from __future__ import annotations

import datetime as dt
import json
import sys
import time
import uuid

from tradingsystem.core.settings import load_settings
from tradingsystem.core.timeutil import iso, now_ms
from tradingsystem.execution.backends.mt5_backend import MT5Backend
from tradingsystem.ingest.mt5.terminal import MT5Terminal


def main(symbol: str = "ETHUSD@") -> int:
    s = load_settings()
    prof = s.mt5.profiles[s.mt5.execution_profile_by_mode["demo"]]
    term = MT5Terminal(prof)
    term.connect()                                           # asserts server + demo trade mode
    be = MT5Backend(term, s.execution.magic, expected_account_type="demo")
    be.assert_account()
    m, report = be.mt5, {"symbol": symbol, "account": term.account().__dict__, "steps": []}
    spec = be.specs(symbol)
    stops = spec["stops_level_price"]

    def step(name: str, **kw) -> None:
        report["steps"].append({"step": name, **kw})
        print(f"- {name}: {json.dumps(kw, default=str)[:300]}")

    # ---- 1. pending order far from the market, then cancel
    q = m.symbol_info_tick(symbol)
    did1 = "demotest" + uuid.uuid4().hex
    entry = round(q.bid * 0.93, spec["digits"])
    sl = round(entry - max(20 * stops, entry * 0.01), spec["digits"])
    tp = round(entry + 2 * (entry - sl), spec["digits"])
    until = iso(now_ms() + 30 * 60_000)
    rec = {"decision": "BUY", "order_type": "BUY_LIMIT", "stop_loss": sl, "valid_until": until,
           "take_profits": [{"price": tp, "close_fraction": 1.0}]}
    res = be.place(decision_id=did1, symbol=symbol, rec=rec, lots=spec["volume_min"], entry=entry, dry_run=False)
    step("pending placed", ok=res.get("ok"), reason=res.get("reason"), attempts=res.get("attempts"))
    ex = be.existing(did1)
    o = ex["orders"][0] if ex["orders"] else None
    step("pending at broker", found=bool(o), price=getattr(o, "price_open", None), sl=getattr(o, "sl", None),
         tp=getattr(o, "tp", None), magic=getattr(o, "magic", None), comment=getattr(o, "comment", None),
         expiration_server=getattr(o, "time_expiration", None))
    if o is not None:
        step("pending cancelled", **be.cancel_order(o.ticket))
        time.sleep(1)
        step("pending gone", remaining=len(be.existing(did1)["orders"]),
             result=be.decision_result(did1, int(time.time()) - 3600))

    # ---- 2. market order at the minimum size, then close at once
    q = m.symbol_info_tick(symbol)
    did2 = "demotest" + uuid.uuid4().hex
    sl = round(q.bid - max(20 * stops, q.bid * 0.01), spec["digits"])
    tp = round(q.ask + 2 * (q.ask - sl), spec["digits"])
    rec = {"decision": "BUY", "order_type": "MARKET", "stop_loss": sl, "valid_until": iso(now_ms() + 900_000),
           "take_profits": [{"price": tp, "close_fraction": 1.0}]}
    res = be.place(decision_id=did2, symbol=symbol, rec=rec, lots=spec["volume_min"], entry=q.ask, dry_run=False)
    step("market placed", ok=res.get("ok"), reason=res.get("reason"), placed=res.get("placed"))
    ex = be.existing(did2)
    p = ex["positions"][0] if ex["positions"] else None
    step("position at broker", found=bool(p), price=getattr(p, "price_open", None), sl=getattr(p, "sl", None),
         tp=getattr(p, "tp", None), magic=getattr(p, "magic", None), comment=getattr(p, "comment", None),
         volume=getattr(p, "volume", None))
    if p is not None:
        step("position closed", **be.close_position(p.ticket))
        time.sleep(2)
        deals = m.history_deals_get(position=p.identifier) or ()
        step("deals", deals=[{"entry": d.entry, "type": d.type, "price": d.price, "volume": d.volume,
                              "profit": d.profit, "commission": d.commission, "swap": d.swap,
                              "fee": getattr(d, "fee", 0.0), "comment": d.comment} for d in deals])
        step("settled", result=be.decision_result(did2, int(time.time()) - 3600))
    report["finished"] = dt.datetime.now(dt.timezone.utc).isoformat()
    report["open_after"] = {"positions": m.positions_total(), "orders": m.orders_total()}
    print(json.dumps(report, default=str, indent=1))
    term.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
