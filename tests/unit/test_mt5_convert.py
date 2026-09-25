"""MT5 conversion: tick keys identical across polling batches and a single re-fetch (real XAU ticks)."""
import numpy as np
import pytest

from tradingsystem.ingest.mt5.convert import TickCursor, ticks_to_rows
from tradingsystem.ingest.mt5.servertime import MonotonicServerClock, ServerTimeModel

TICK_DTYPE = [("time", "<i8"), ("bid", "<f8"), ("ask", "<f8"), ("last", "<f8"), ("volume", "<u8"),
              ("time_msc", "<i8"), ("flags", "<u4"), ("volume_real", "<f8")]


@pytest.fixture(scope="module")
def raw_ticks(real_xau_ticks):
    """Rebuild the terminal's raw (server-time) tick array from the real fixture."""
    model = ServerTimeModel()
    utc = real_xau_ticks["time_msc"].to_numpy()
    srv = np.array([model.utc_to_server(int(t)) for t in utc], dtype=np.int64)
    arr = np.zeros(len(srv), dtype=TICK_DTYPE)
    arr["time_msc"], arr["time"] = srv, srv // 1000
    arr["bid"], arr["ask"] = real_xau_ticks["bid"].to_numpy(), real_xau_ticks["ask"].to_numpy()
    arr["flags"] = real_xau_ticks["flags"].to_numpy()
    return arr


def test_keys_match_fixture_and_are_unique(raw_ticks, real_xau_ticks):
    rows = ticks_to_rows(raw_ticks, MonotonicServerClock(ServerTimeModel()))
    keys = [r[0] for r in rows]
    assert keys == real_xau_ticks["key"].to_pylist()          # same keys as bench_prepare produced
    assert len(set(keys)) == len(keys)
    assert all(r[5] is None and r[6] is None for r in rows)    # last/volume_real = 0 on Windsor → None


def test_incremental_polling_gives_identical_keys(raw_ticks):
    full = ticks_to_rows(raw_ticks, MonotonicServerClock(ServerTimeModel()))
    tm = raw_ticks["time_msc"]
    # find a millisecond with several ticks and cut a batch in the middle of it
    dup_ms = next(int(t) for t in np.unique(tm) if (tm == t).sum() >= 2)
    cut = int(np.nonzero(tm == dup_ms)[0][0]) + 1
    clock = MonotonicServerClock(ServerTimeModel())
    cur = TickCursor("XAUUSD@", last_srv_msc=int(tm[0]) - 1)
    out = []
    # poll 1 returns ticks up to the cut; poll 2 re-returns everything from the start of that second
    for batch in (raw_ticks[:cut], raw_ticks[np.nonzero(tm >= (dup_ms // 1000) * 1000)[0][0]:]):
        new = cur.take_new(batch)
        prev_ms, seq_start = cur.advance(new)
        cont = new[0]["time_msc"] == prev_ms if len(new) else False
        out += ticks_to_rows(new, clock, seq_start=seq_start if cont else 0, prev_srv_msc=prev_ms if cont else None)
    assert [r[0] for r in out] == [r[0] for r in full]
