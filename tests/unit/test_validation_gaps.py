"""Row validators, session calendars and gap detection on real data."""
import datetime as dt
from pathlib import Path

import numpy as np
import pytest

from tradingsystem.core.instruments import InstrumentRegistry
from tradingsystem.core.sessions import CALENDARS
from tradingsystem.core.settings import load_settings
from tradingsystem.core.timeframes import Timeframe
from tradingsystem.core.timeutil import to_ms
from tradingsystem.storage.gaps import candle_gaps, id_gaps
from tradingsystem.storage.tablespec import spec_for
from tradingsystem.storage.validators import validate_rows

UTC = dt.timezone.utc


@pytest.fixture(scope="module")
def reg():
    return InstrumentRegistry.from_settings(load_settings(env_path=Path("nope.env")))


def u(*a):
    return to_ms(dt.datetime(*a, tzinfo=UTC))


def candle_rows(tbl, spec):
    return list(zip(*(tbl[c].to_pylist() for c in spec.column_names)))


def test_real_candles_all_valid(reg, real_candles_1m):
    spec = spec_for(reg.primary("BTCUSDT"), "candles", Timeframe.M1)
    res = validate_rows(spec, candle_rows(real_candles_1m, spec), venue="binance_spot")
    assert res.n_rejected == 0 and len(res.good) == 3000


def test_corrupted_candles_rejected_with_reason(reg, real_candles_1m):
    spec = spec_for(reg.primary("BTCUSDT"), "candles", Timeframe.M1)
    rows = candle_rows(real_candles_1m, spec)[:5]
    bad_high = list(rows[1]); bad_high[2] = min(rows[1][1], rows[1][4]) - 1     # high below open/close
    misaligned = list(rows[2]); misaligned[0] += 30_000
    res = validate_rows(spec, [rows[0], tuple(bad_high), tuple(misaligned)], venue="binance_spot")
    assert [why for _, why in res.rejected] == ["high below open/close", "open_time not aligned to 1m"]


def test_real_agg_trades_and_ticks_valid(reg, real_aggtrades, real_xau_ticks):
    s = spec_for(reg.primary("BTCUSDT"), "agg_trades")
    rows = list(zip(real_aggtrades["agg_id"].to_pylist(), real_aggtrades["ts"].to_pylist(),
                    real_aggtrades["price"].to_pylist(), real_aggtrades["qty"].to_pylist(),
                    real_aggtrades["first_id"].to_pylist(), real_aggtrades["last_id"].to_pylist(),
                    [int(x) for x in real_aggtrades["is_buyer_maker"].to_pylist()]))
    assert validate_rows(s, rows, venue="binance_spot").n_rejected == 0
    t = spec_for(reg.primary("XAUUSD"), "ticks")
    x = real_xau_ticks
    trows = list(zip(x["key"].to_pylist(), x["time_msc"].to_pylist(), x["time_msc"].to_pylist(),
                     x["bid"].to_pylist(), x["ask"].to_pylist(), [None] * x.num_rows, [None] * x.num_rows,
                     x["flags"].to_pylist()))
    assert validate_rows(t, trows, venue="mt5").n_rejected == 0


def test_candle_gap_detection_on_real_series(real_candles_1m):
    ot = real_candles_1m["open_time"].to_numpy()
    assert candle_gaps(ot, Timeframe.M1, int(ot[0]), int(ot[-1])) == []
    holed = np.delete(ot, [10, 11, 12, 500])
    gaps = candle_gaps(holed, Timeframe.M1, int(ot[0]), int(ot[-1]))
    assert [(g.start, g.count) for g in gaps] == [(int(ot[10]), 3), (int(ot[500]), 1)]


def test_agg_id_gaps(real_aggtrades):
    ids = real_aggtrades["agg_id"].to_numpy()
    assert id_gaps(ids) == []
    g = id_gaps(np.delete(ids, [5, 6]))
    assert len(g) == 1 and g[0].start == ids[5] and g[0].count == 2


def test_gold_calendar_follows_new_york():
    cal = CALENDARS["ny_metals_fx"]
    # summer (EDT): break 21:00–22:00 UTC; winter (EST): 22:00–23:00 UTC
    assert not cal.is_open(u(2026, 9, 23, 21, 30)) and cal.is_open(u(2026, 9, 23, 22, 5))
    assert cal.is_open(u(2026, 1, 14, 21, 30)) and not cal.is_open(u(2026, 1, 14, 22, 30))
    assert not cal.is_open(u(2026, 9, 26, 12))          # Saturday
    assert not cal.is_open(u(2026, 9, 25, 21, 0)) and cal.is_open(u(2026, 9, 25, 20, 59))   # Friday close
    assert cal.is_open(u(2026, 9, 27, 22, 0)) and not cal.is_open(u(2026, 9, 27, 21, 59))   # Sunday open


def test_session_aware_gaps_ignore_closed_hours():
    cal = CALENDARS["ny_metals_fx"]
    start, end = u(2026, 9, 25, 20, 0), u(2026, 9, 27, 23, 0)       # Fri → Sun night
    grid = np.arange(start, end + 1, 60_000)
    present = np.array([t for t in grid if cal.is_open(int(t))])
    assert candle_gaps(present, Timeframe.M1, start, end, cal) == []
    assert candle_gaps(present, Timeframe.M1, start, end, None) != []     # naive check would flag the weekend


def test_windsor_crypto_maintenance():
    cal = CALENDARS["windsor_crypto_cfd"]
    assert not cal.is_open(u(2026, 9, 26, 6)) and cal.is_open(u(2026, 9, 26, 8, 0)) and cal.is_open(u(2026, 9, 27, 6))
