"""Daily volume profiles (Phase 5 B4): POC / value-area high / low of the last 5 COMPLETED UTC days.

* BTC / ETH (Binance spot, real): built from the stored aggTrades with ``orderflow.footprint`` and
  ``orderflow.value_area`` (70 % value area), one UTC day at a time and in bounded chunks — a cold Parquet day file is
  read row group by row group, a day still in the hot SQLite store in 15-minute slices — so the transient memory is a
  few MB of arrays plus one price-level table, never a day of trades. A day counts when trades exist in at least 20 of its
  24 hours (an outage is not a profile).
* XAU (MT5): the 1m tick-volume profile of the day (``orderflow.tick_volume_profile``), flagged ``approx``; a day
  counts with at least 600 one-minute bars (the weekend days drop out).

A day's profile never changes once the day is complete, so it is computed ONCE (a worker thread, ``DailyProfiles``)
and cached in ``engine_kv`` under ``dprofile1:<pair>:<YYYY-MM-DD>`` (in memory too) — the 5-minute screen path only
looks the cache up: while a day is being computed the block says ``pending`` (honestly, with what is already known)
and the screen is never held up. A day with no data is remembered as a miss for 6 hours, then tried again. The window
looks back at most 10 days to find 5 with data; every row carries its date.
"""
from __future__ import annotations

import datetime as dt
import logging
import threading
import time
from typing import Any

import numpy as np

from ..core.timeframes import Timeframe
from ..core.timeutil import MS_PER_MINUTE
from ..storage.reader import InstrumentReader
from ..storage.tablespec import spec_for
from . import orderflow as of

log = logging.getLogger("engine")
UTC = dt.timezone.utc
DAY_MS = 86_400_000
PROFILE_DAYS = 5
LOOKBACK_DAYS = 10
KEY_PREFIX = "dprofile1"                  # bump when the definition changes: old cache entries are then never read
MISS_RETRY_MS = 6 * 3_600_000
COMPLETE_GRACE_MS = 10 * MS_PER_MINUTE    # a day is complete this long after its end (the ingest writes with a lag)
MIN_HOURS = 20                            # aggTrades: hours of the day with trades
MIN_BARS_1M = 600                         # tick-volume profile: one-minute bars of the day
CHUNK_ROWS = 250_000                      # cold row-group batch
HOT_SLICE_MS = 15 * MS_PER_MINUTE
COLUMNS = ["date", "poc", "vah", "val"]
AGG_COLS = ["ts", "price", "qty", "is_buyer_maker"]


def day_ms(day: dt.date) -> int:
    return int(dt.datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp() * 1000)


def candidate_days(as_of: int) -> list[dt.date]:
    """The newest complete UTC day first, back ``LOOKBACK_DAYS`` days."""
    newest = dt.datetime.fromtimestamp((as_of - COMPLETE_GRACE_MS) / 1000, tz=UTC).date() - dt.timedelta(days=1)
    return [newest - dt.timedelta(days=i) for i in range(LOOKBACK_DAYS)]


# ------------------------------------------------------------------------------------------------- the day's profile
def _agg_chunks(reader: InstrumentReader, inst, day: dt.date):
    """The day's aggTrades as (ts, price, qty, is_buyer_maker) numpy chunks: the cold day file when it exists (row group
    by row group), else the hot store in 15-minute slices."""
    spec = spec_for(inst, "agg_trades")
    start = day_ms(day)
    path = reader.cold.day_path(inst, spec, day)
    if path.exists():
        import pyarrow.parquet as pq
        pf = pq.ParquetFile(path)
        for batch in pf.iter_batches(batch_size=CHUNK_ROWS, columns=AGG_COLS):
            yield tuple(batch.column(c).to_numpy(zero_copy_only=False) for c in AGG_COLS)
        return
    if reader.hot is None:
        return
    for t in range(start, start + DAY_MS, HOT_SLICE_MS):
        c = reader.hot.read_range(spec, t, t + HOT_SLICE_MS, AGG_COLS)
        if len(c["ts"]):
            yield tuple(c[k] for k in AGG_COLS)


def profile_agg_trades(reader: InstrumentReader, inst, day: dt.date, bucket: float) -> dict | None:
    """POC / VAH / VAL of the day's traded volume by price bucket (``orderflow.footprint`` per chunk, merged with
    ``orderflow.merge_bars``), or None when fewer than ``MIN_HOURS`` hours have trades."""
    lo, hi = day_ms(day), day_ms(day) + DAY_MS
    merged: list[of.FootprintBar] = []
    hours: set[int] = set()
    trades = 0
    for ts, price, qty, maker in _agg_chunks(reader, inst, day):
        ts = np.asarray(ts, dtype=np.int64)
        keep = (ts >= lo) & (ts < hi)
        if not keep.all():
            ts, price, qty, maker = ts[keep], np.asarray(price)[keep], np.asarray(qty)[keep], np.asarray(maker)[keep]
        if not len(ts):
            continue
        hours.update(np.unique((ts - lo) // 3_600_000).tolist())
        trades += len(ts)
        merged += of.footprint(ts, np.asarray(price, dtype=float), np.asarray(qty, dtype=float),
                               np.asarray(maker), 10**15, bucket)
        if len(merged) > 8:                          # fold the chunks so far: one level table, not one per chunk
            merged = of.merge_bars(merged, 10**15)
        time.sleep(0)                                # let the screen path run between chunks
    if len(hours) < MIN_HOURS:
        return None
    prof = of.profile_from_footprint(merged)
    if not prof:
        return None
    return {"poc": prof["poc"], "vah": prof["vah"], "val": prof["val"], "hours": len(hours), "trades": trades}


def profile_1m_tick_volume(reader: InstrumentReader, inst, day: dt.date, bucket: float) -> dict | None:
    """The approximate tick-volume profile of the day from its 1m bars (MT5 has no traded volume)."""
    lo = day_ms(day)
    c = reader.read_range(spec_for(inst, "candles", Timeframe.M1), lo, lo + DAY_MS,
                          ["open_time", "high", "low", "tick_volume"])
    if len(c["open_time"]) < MIN_BARS_1M:
        return None
    prof = of.tick_volume_profile(np.asarray(c["high"], dtype=float), np.asarray(c["low"], dtype=float),
                                  np.asarray(c["tick_volume"], dtype=float), bucket)
    if not prof:
        return None
    return {"poc": prof["poc"], "vah": prof["vah"], "val": prof["val"], "bars": int(len(c["open_time"]))}


# ---------------------------------------------------------------------------------------------- cache + the worker
class DailyProfiles:
    """The per-pair daily profile cache and its worker thread (one thread at a time per pair). ``kv`` = anything with
    ``kv_get(key, default)`` / ``kv_set(key, value)`` (the DecisionStore: ``engine_kv``); None keeps the cache in memory
    only (tools, tests). ``block`` never computes: it reads the cache and starts the worker for what is missing."""

    def __init__(self, data_dir, cache_mb: int, kv: Any = None, now=None) -> None:
        self.data_dir, self.cache_mb, self.kv = data_dir, cache_mb, kv
        self._now = now or (lambda: int(time.time() * 1000))
        self._mem: dict[str, dict] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._lock = threading.Lock()

    @staticmethod
    def key(pair: str, day: dt.date) -> str:
        return f"{KEY_PREFIX}:{pair}:{day.isoformat()}"

    def _get(self, key: str) -> dict | None:
        v = self._mem.get(key)
        if v is None and self.kv is not None:
            v = self.kv.kv_get(key)
            if v is not None:
                self._mem[key] = v
        return v

    def _put(self, key: str, value: dict) -> None:
        self._mem[key] = value
        if self.kv is not None:
            self.kv.kv_set(key, value)

    def _state(self, pair: str, day: dt.date, bucket: float) -> tuple[str, dict | None]:
        """('valid', profile) | ('miss', None) | ('unknown', None) — a miss older than 6 h is unknown again, and so is
        a profile computed with another price bucket (``footprint_bucket`` changed in the config: recomputed)."""
        v = self._get(self.key(pair, day))
        if v is None:
            return "unknown", None
        if v.get("miss"):
            return ("miss", None) if self._now() - int(v.get("at", 0)) < MISS_RETRY_MS else ("unknown", None)
        if v.get("bucket") != bucket:
            return "unknown", None
        return "valid", v

    def wait(self, timeout: float = 600.0) -> None:
        """Block until the running workers finish (tools and tests; never called on the engine's path)."""
        end = time.time() + timeout
        for t in list(self._threads.values()):
            t.join(max(0.0, end - time.time()))

    def running(self) -> bool:
        return any(t.is_alive() for t in self._threads.values())

    def block(self, pair: str, inst, as_of: int, bucket: float, d: int, *, approx: bool) -> dict:
        """The ``orderflow.daily_profiles`` block for ``as_of``: the newest 5 days with a profile out of the last 10
        complete UTC days, newest first. ``pending`` = complete days newer than the oldest shown that are still being
        computed; no profile at all yet → ``data_quality: pending``."""
        rows, pending, todo = [], 0, []
        for day in candidate_days(as_of):
            st, v = self._state(pair, day, bucket)
            if st == "valid":
                rows.append([day.isoformat(), round(v["poc"], d), round(v["vah"], d), round(v["val"], d)])
                if len(rows) == PROFILE_DAYS:
                    break
            elif st == "unknown":
                pending += 1
                todo.append(day)
        if len(rows) < PROFILE_DAYS:              # short of 5 days: no more can be computed than are missing
            pending = min(pending, PROFILE_DAYS - len(rows))
        if todo:
            self._start(pair, inst, candidate_days(as_of), bucket, approx)
        out: dict = {"data_quality": ("approx" if approx else "real") if rows else "pending", "bucket": bucket}
        if approx:
            out["approx"] = True
        if rows:
            out["columns"] = COLUMNS
            out["days"] = rows
        if pending:
            out["pending"] = pending
        return out

    # ---------------------------------------------------------------------------------------------- the worker
    def _start(self, pair: str, inst, cands: list[dt.date], bucket: float, approx: bool) -> None:
        with self._lock:
            t = self._threads.get(pair)
            if t is not None and t.is_alive():
                return
            t = threading.Thread(target=self._run, args=(pair, inst, cands, bucket, approx),
                                 name=f"daily-profiles-{pair}", daemon=True)
            self._threads[pair] = t
            t.start()

    def _run(self, pair: str, inst, cands: list[dt.date], bucket: float, approx: bool) -> None:
        """Compute the unknown days among ``cands`` (newest first) one UTC day at a time with its own reader, caching each
        as it finishes; stops once ``PROFILE_DAYS`` days with a profile are known. Any failure is a logged miss, never an
        exception."""
        reader = None
        try:
            reader = InstrumentReader(inst, self.data_dir, cache_mb=self.cache_mb)
            have = 0                                 # days with a profile so far, newest first
            for day in cands:
                if have >= PROFILE_DAYS:
                    break
                if self._state(pair, day, bucket)[0] == "unknown":
                    try:
                        res = (profile_1m_tick_volume if approx else profile_agg_trades)(reader, inst, day, bucket)
                    except Exception as e:  # noqa: BLE001 — a failed day is a miss; the next hour retries it
                        log.warning("daily profile %s %s failed: %r", pair, day, e)
                        res = None
                    self._put(self.key(pair, day), {**res, "bucket": bucket} if res else {"miss": True, "at": self._now()})
                have += self._state(pair, day, bucket)[0] == "valid"
                try:                                 # give the day's decode buffers back to the OS (RAM is tight)
                    import pyarrow as pa
                    pa.default_memory_pool().release_unused()
                except Exception:  # noqa: BLE001
                    pass
                time.sleep(0.05)
        except Exception:  # noqa: BLE001
            log.exception("daily profile worker %s", pair)
        finally:
            if reader is not None:
                reader.close()
