"""Virtual outcome of a trade idea on real 1m candles (BTCUSDT fixture)."""
from types import SimpleNamespace

import numpy as np

from tradingsystem.core.timeutil import iso
from tradingsystem.execution.executor import evaluate_virtual


class Reader:
    def __init__(self, t):
        self.c = {k: t[k].to_numpy() for k in ("open_time", "high", "low")}

    def read_range(self, spec, start, end, cols):
        m = self.c["open_time"] >= start
        return {k: self.c[k][m] for k in cols}


def test_virtual_outcome_matches_first_touch(real_candles_1m, monkeypatch):
    import tradingsystem.execution.executor as ex
    monkeypatch.setattr(ex, "spec_for", lambda inst, dt_, tf=None: None)
    t = real_candles_1m
    rd = Reader(t)
    inst = SimpleNamespace(timeframes=[None])
    i0 = 100
    ts = int(t["open_time"][i0].as_py())
    close = float(t["close"][i0].as_py())
    rec = {"decision": "BUY", "order_type": "MARKET", "entry": {"price": close}, "stop_loss": close - 150,
           "take_profits": [{"price": close + 150, "close_fraction": 1.0}], "timestamp": iso(ts),
           "valid_until": iso(ts + 3_600_000)}
    vo, vr = evaluate_virtual(rec, rd, inst)
    hi, lo = t["high"].to_numpy()[i0:], t["low"].to_numpy()[i0:]
    first_sl = np.argmax(lo <= close - 150) if (lo <= close - 150).any() else None
    first_tp = np.argmax(hi >= close + 150) if (hi >= close + 150).any() else None
    if first_sl is None and first_tp is None:
        assert vo in (None, "unresolved_24h")
    elif first_tp is None or (first_sl is not None and first_sl <= first_tp):
        assert vo == "sl_first" and vr == -1.0
    else:
        assert vo == "tp1_first" and vr == 1.0
