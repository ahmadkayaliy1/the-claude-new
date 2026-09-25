"""Binance Vision historical backfill (P3.2) — runs in the backfill worker process (CPU isolated from WS).

* Plans monthly files for complete months and daily files for the rest (accepting only expected names —
  the listing contains stray objects, D-017); a month already started from daily files is finished with daily
  files instead of re-downloading the monthly zip (BF-10).
* Every request is retried on network/5xx/429 errors with jittered backoff inside a time budget (BF-02/BF-09);
  a listing must be a valid S3 ``ListBucketResult`` for the prefix.
* Downloads stream to ``data/vision_cache/<key>.part`` (never held in RAM), resume with ``Range`` after a
  failure, are SHA-256 checked against the CHECKSUM (fetched first) and only then renamed (BF-04/BF-05).
  The zip is deleted once its period is recorded in ``vision_done`` (unless ``keep_zips``).
* Parses from the zip file with pyarrow in bounded blocks, normalises µs→ms per value, validates every row.
* aggTrades → cold Parquet day files streamed day by day; klines / metrics → hot SQLite.
* Progress is recorded in the ``vision_done`` table, so reruns skip finished periods.
* Stops (without touching live capture) when free disk < ``storage.min_free_disk_gb``.
"""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import logging
import os
import random
import re
import shutil
import time
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, TypeVar

import httpx
import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.csv as pacsv

from ...core.instruments import Instrument
from ...core.timeframes import Timeframe
from ...core.timeutil import MS_PER_DAY, now_ms
from ...storage.parquet_store import DayWriter, ParquetColdStore
from ...storage.sqlite_store import SQLiteHotStore
from ...storage.tablespec import VISION_DONE, TableSpec
from ...storage.validators import validate_rows
from .markets import Market
from .parsers import metrics_vision

log = logging.getLogger(__name__)

LIST_URL = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
_NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"

KLINE_COLS = ["open_time", "open", "high", "low", "close", "volume", "close_time", "quote_volume", "trades",
              "taker_buy_base", "taker_buy_quote", "ignore"]
AGG_COLS_SPOT = ["agg_id", "price", "qty", "first_id", "last_id", "ts", "is_buyer_maker", "is_best_match"]
AGG_COLS_FUT = ["agg_id", "price", "qty", "first_id", "last_id", "ts", "is_buyer_maker"]

VISION_RETRY_DEADLINE_S = 900.0   # consecutive-failure budget per request (post-wake recovery took ≈ 5.5 min)
RETRY_STATUS = frozenset({408, 429, 500, 502, 503, 504})
DOWNLOAD_CHUNK = 1 << 20
BEAT_S = 30.0                     # collector heartbeat interval while one request/download is in progress
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
T = TypeVar("T")


class DiskFullError(RuntimeError):
    pass


class VisionTransientError(RuntimeError):
    """Network/server errors outlasted the retry budget (or the CHECKSUM failed twice) — retry the pass later."""


class VisionMissing(RuntimeError):
    """A listed object answered 403/404 — skipped this pass, never recorded as done or as a source gap."""


class VisionBadResponse(RuntimeError):
    """A 200 response whose body is not a valid listing / CHECKSUM (captive portal, truncation) — retried."""


class _RetryableStatus(RuntimeError):
    def __init__(self, r: httpx.Response) -> None:
        super().__init__(f"HTTP {r.status_code}")
        ra = r.headers.get("retry-after")
        try:
            self.retry_after: float | None = min(300.0, float(ra)) if ra else None
        except ValueError:
            self.retry_after = None


@dataclass(frozen=True)
class VisionFile:
    key: str            # object key under data.binance.vision
    period: str         # "2026-08" (monthly) or "2026-09-24" (daily)
    monthly: bool

    @property
    def first_day(self) -> dt.date:
        return dt.date.fromisoformat(self.period + "-01") if self.monthly else dt.date.fromisoformat(self.period)

    @property
    def end_day(self) -> dt.date:  # exclusive
        d = self.first_day
        if not self.monthly:
            return d + dt.timedelta(days=1)
        return (d.replace(day=28) + dt.timedelta(days=4)).replace(day=1)

    def days(self) -> list[str]:
        """ISO dates covered by the file."""
        return [(self.first_day + dt.timedelta(days=i)).isoformat() for i in range((self.end_day - self.first_day).days)]


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while block := fh.read(DOWNLOAD_CHUNK):
            h.update(block)
    return h.hexdigest()


class VisionClient:
    def __init__(self, base_url: str, cache_dir: Path, *, min_free_gb: float, keep_zips: bool = False,
                 transport: httpx.BaseTransport | None = None, retry_deadline_s: float = VISION_RETRY_DEADLINE_S,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.time) -> None:
        self.base = base_url.rstrip("/")
        self.cache = cache_dir
        self.min_free_gb = min_free_gb
        self.keep_zips = keep_zips
        self.retry_deadline_s = retry_deadline_s
        self._sleep, self._clock = sleep, clock
        self.http = httpx.Client(timeout=httpx.Timeout(connect=15, read=60, write=30, pool=30),
                                 follow_redirects=True, transport=transport)
        self.bytes_downloaded = 0
        self._listings: dict[str, list[str]] = {}     # per pass (one client per pass)
        self.beat: Callable[[str], None] | None = None  # heartbeat hook during long transfers / retry waits
        self._last_beat = 0.0

    def close(self) -> None:
        self.http.close()

    def _beat(self, msg: str, force: bool = False) -> None:
        now = self._clock()
        if self.beat is None or (not force and now - self._last_beat < BEAT_S):
            return
        self._last_beat = now
        try:
            self.beat(msg)
        except Exception:  # noqa: BLE001 — bookkeeping never fails a download
            log.exception("vision heartbeat failed")

    # ------------------------------------------------------------------ retry
    @staticmethod
    def _check(r: httpx.Response) -> httpx.Response:
        if r.status_code in RETRY_STATUS:
            raise _RetryableStatus(r)
        if r.status_code in (403, 404):
            raise VisionMissing(f"HTTP {r.status_code} {r.url}")
        r.raise_for_status()
        return r

    @staticmethod
    def _backoff(i: int, exc: BaseException) -> float:
        ra = getattr(exc, "retry_after", None)
        if ra is not None:
            return ra
        cap = min(60.0, 2.0 * 2 ** i)
        return cap / 2 + random.uniform(0, cap / 2)

    def _retry(self, fn: Callable[[], T], what: str) -> T:
        """``fn()`` retried on network errors / 408,429,5xx / bad bodies until the time budget is spent."""
        deadline, i = self._clock() + self.retry_deadline_s, 0
        while True:
            try:
                return fn()
            except (httpx.TransportError, _RetryableStatus, VisionBadResponse) as exc:
                wait = self._backoff(i, exc)
                if self._clock() + wait > deadline:
                    raise VisionTransientError(f"{what}: {exc!r}") from exc
                log.warning("vision %s failed (%r) — retry %d in %.0fs", what, exc, i + 1, wait)
                self._beat(f"retrying {what}: {exc!r}", force=True)
                self._sleep(wait)
                i += 1

    # ------------------------------------------------------------------ listing
    def list_zips(self, prefix: str) -> list[str]:
        if prefix in self._listings:
            return self._listings[prefix]
        keys: list[str] = []
        marker = ""
        while True:
            page, truncated, nxt = self._retry(lambda: self._list_page(prefix, marker), f"list {prefix}")
            keys += [k for k in page if k.endswith(".zip")]
            if not truncated or not nxt or nxt == marker:
                break
            marker = nxt
        self._listings[prefix] = keys
        return keys

    def _list_page(self, prefix: str, marker: str) -> tuple[list[str], bool, str]:
        r = self._check(self.http.get(LIST_URL, params={"delimiter": "/", "prefix": prefix, "marker": marker}))
        try:
            root = ET.fromstring(r.content)
        except ET.ParseError as exc:
            raise VisionBadResponse(f"listing {prefix}: not XML ({exc})") from exc
        if root.tag != _NS + "ListBucketResult" or (root.findtext(_NS + "Prefix") or "") != prefix:
            raise VisionBadResponse(f"listing {prefix}: unexpected document <{root.tag}>")
        keys = [k.text or "" for k in root.findall(f"{_NS}Contents/{_NS}Key")]
        truncated = (root.findtext(_NS + "IsTruncated") or "").strip().lower() == "true"
        return keys, truncated, root.findtext(_NS + "NextMarker") or (keys[-1] if keys else "")

    def plan(self, market: Market, dtype: str, symbol: str, interval: str | None, start: dt.date,
             end: dt.date, done: set[str] | frozenset[str] = frozenset()) -> list[VisionFile]:
        """Files covering [start, end) — monthly where the whole month is inside, daily elsewhere.

        ``done`` (periods already ingested): a month fully covered by daily periods is skipped, and a month
        already started day by day is finished with daily files when they are all listed (BF-10).
        """
        sub = f"{dtype}/{symbol}/" + (f"{interval}/" if interval else "")
        stem = f"{symbol}-{interval}" if interval else f"{symbol}-{dtype}"
        mpat = re.compile(rf"/{re.escape(stem)}-(\d{{4}}-\d{{2}})\.zip$")
        dpat = re.compile(rf"/{re.escape(stem)}-(\d{{4}}-\d{{2}}-\d{{2}})\.zip$")
        monthly = {m.group(1): k for k in self.list_zips(f"data/{market.vision_prefix}/monthly/{sub}")
                   if (m := mpat.search(k))}
        daily = {m.group(1): k for k in self.list_zips(f"data/{market.vision_prefix}/daily/{sub}")
                 if (m := dpat.search(k))}
        out: list[VisionFile] = []
        d = start
        while d < end:
            month = f"{d:%Y-%m}"
            mf = VisionFile(monthly.get(month, ""), month, True)
            if d.day == 1 and mf.end_day <= end:
                missing = [x for x in mf.days() if x not in done]
                if month not in done and not missing:            # fully ingested from daily files
                    d = mf.end_day
                    continue
                started = len(missing) < len(mf.days())
                if month in monthly and (month in done or not (started and all(x in daily for x in missing))):
                    out.append(mf)
                    d = mf.end_day
                    continue
            ds = d.isoformat()
            if ds in daily:
                out.append(VisionFile(daily[ds], ds, False))
            d += dt.timedelta(days=1)
        return out

    # ------------------------------------------------------------------ download
    def _check_disk(self, need_bytes: int = 0) -> None:
        free = shutil.disk_usage(self.cache.anchor if self.cache.anchor else self.cache).free
        if (free - need_bytes) / 2**30 < self.min_free_gb:
            raise DiskFullError(f"free disk {free / 2**30:.1f} GB (next download {need_bytes / 2**20:.0f} MB) "
                                f"< {self.min_free_gb} GB — backfill paused")

    def _checksum(self, f: VisionFile) -> str:
        def get() -> str:
            words = self._check(self.http.get(f"{self.base}/{f.key}.CHECKSUM")).text.split()
            if not words or not _SHA256.match(words[0].lower()):
                raise VisionBadResponse(f"{f.key}.CHECKSUM: unexpected body")
            return words[0].lower()
        return self._retry(get, f"checksum {f.key}")

    def download(self, f: VisionFile) -> Path:
        """Verified local zip for ``f``: CHECKSUM first, then a streamed, resumable download to ``.part``,
        SHA-256 checked before the rename (a mismatch refetches once, then counts as transient)."""
        self._check_disk()
        path = self.cache / f.key
        expected = self._checksum(f)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            if _sha256(path) == expected:
                return path
            path.unlink()
        part = path.with_name(path.name + ".part")
        for attempt in (1, 2):
            self._stream_to(f"{self.base}/{f.key}", part, f.key)
            if _sha256(part) == expected:
                os.replace(part, path)
                return path
            log.warning("CHECKSUM mismatch for %s (attempt %d) — refetching", f.key, attempt)
            part.unlink(missing_ok=True)
        raise VisionTransientError(f"CHECKSUM mismatch for {f.key} twice")

    def _stream_to(self, url: str, part: Path, what: str) -> None:
        """GET ``url`` into ``part`` as bytes arrive; resumes with Range after errors (budget resets on progress)."""
        deadline, i, etag = self._clock() + self.retry_deadline_s, 0, None
        while True:
            have = part.stat().st_size if part.exists() else 0
            headers = {"Accept-Encoding": "identity"}             # byte offsets = file offsets
            if have:
                headers["Range"] = f"bytes={have}-"
            if have and etag:
                headers["If-Range"] = etag
            try:
                with self.http.stream("GET", url, headers=headers) as r:
                    if r.status_code == 416 and have:
                        return                                   # .part already complete — the hash decides
                    self._check(r)
                    etag = r.headers.get("etag") or etag
                    resumed = r.status_code == 206 and r.headers.get("content-range", "").startswith(f"bytes {have}-")
                    if r.status_code == 206 and not resumed:     # unexpected range → start over
                        part.unlink(missing_ok=True)
                        raise httpx.RemoteProtocolError(f"unexpected Content-Range {r.headers.get('content-range')}")
                    self._check_disk(int(r.headers.get("content-length") or 0))
                    with open(part, "ab" if resumed else "wb") as fh:
                        for chunk in r.iter_bytes():         # as they arrive: a drop loses nothing written
                            fh.write(chunk)
                            self.bytes_downloaded += len(chunk)
                            self._beat(f"downloading {what}: {fh.tell() / 2**20:.0f} MB")
                return
            except (httpx.TransportError, _RetryableStatus) as exc:
                if part.exists() and part.stat().st_size > have:
                    deadline, i = self._clock() + self.retry_deadline_s, 0     # progress → fresh budget
                wait = self._backoff(i, exc)
                if self._clock() + wait > deadline:
                    raise VisionTransientError(f"download {what}: {exc!r}") from exc
                log.warning("vision download %s interrupted at %d bytes (%r) — resume in %.0fs",
                            what, part.stat().st_size if part.exists() else 0, exc, wait)
                self._beat(f"resuming {what}: {exc!r}", force=True)
                self._sleep(wait)
                i += 1

    def release(self, f: VisionFile) -> None:
        """Drop the local zip once its period is recorded as done (a locked file is left for later)."""
        if not self.keep_zips:
            try:
                (self.cache / f.key).unlink(missing_ok=True)
            except OSError as exc:
                log.warning("cannot delete %s: %r", f.key, exc)


# ---------------------------------------------------------------------- parsing
def _csv_batches(path: Path, names: list[str], header: bool, block_bytes: int,
                 use_threads: bool = True) -> Iterator[pa.RecordBatch]:
    """Record batches straight from the zip member on disk (bounded memory: one block at a time)."""
    with zipfile.ZipFile(path) as zf:
        name = zf.namelist()[0]
        with zf.open(name) as member:
            first = member.read(64)
        # detect a header row regardless of what the market is documented to do
        has_header = header or first[:1].isalpha()
        with zf.open(name) as stream:
            reader = pacsv.open_csv(
                stream,
                read_options=pacsv.ReadOptions(column_names=names, skip_rows=1 if has_header else 0,
                                               block_size=block_bytes, use_threads=use_threads),
                convert_options=pacsv.ConvertOptions(
                    column_types={n: pa.string() for n in ("is_buyer_maker", "is_best_match")}),
            )
            yield from reader


def _norm_ms(arr: pa.Array) -> np.ndarray:
    a = arr.to_numpy(zero_copy_only=False).astype(np.int64)
    return np.where(a > 10**14, a // 1000, a)


def _bool01(arr: pa.Array) -> np.ndarray:
    s = pc.utf8_lower(arr.cast(pa.string()))
    return pc.equal(s, "true").to_numpy(zero_copy_only=False).astype(np.int64)


def klines_rows(batch: pa.RecordBatch) -> list[tuple]:
    ot = _norm_ms(batch.column("open_time"))
    cols = [batch.column(n).to_numpy(zero_copy_only=False).astype(np.float64)
            for n in ("open", "high", "low", "close", "volume", "quote_volume")]
    trades = batch.column("trades").to_numpy(zero_copy_only=False).astype(np.int64)
    tb = batch.column("taker_buy_base").to_numpy(zero_copy_only=False).astype(np.float64)
    tq = batch.column("taker_buy_quote").to_numpy(zero_copy_only=False).astype(np.float64)
    return list(zip(ot.tolist(), *(c.tolist() for c in cols), trades.tolist(), tb.tolist(), tq.tolist()))


def agg_table(batch: pa.RecordBatch) -> pa.Table:
    return pa.table({
        "agg_id": batch.column("agg_id").cast(pa.int64()),
        "ts": pa.array(_norm_ms(batch.column("ts"))),
        "price": batch.column("price").cast(pa.float64()),
        "qty": batch.column("qty").cast(pa.float64()),
        "first_id": batch.column("first_id").cast(pa.int64()),
        "last_id": batch.column("last_id").cast(pa.int64()),
        "is_buyer_maker": pa.array(_bool01(batch.column("is_buyer_maker"))),
    })


def _day_slices(tbl: pa.Table, days: np.ndarray) -> Iterator[tuple[dt.date, pa.Table]]:
    """(UTC day, rows of that day) in ascending day order; zero-copy slices when the batch is time-ordered."""
    if len(days) and bool((np.diff(days) >= 0).all()):
        bounds = np.flatnonzero(np.diff(days)) + 1
        for a, b in zip(np.concatenate([[0], bounds]), np.concatenate([bounds, [len(days)]])):
            yield dt.date(1970, 1, 1) + dt.timedelta(days=int(days[a])), tbl.slice(int(a), int(b - a))
    else:
        for dnum in np.unique(days):
            yield dt.date(1970, 1, 1) + dt.timedelta(days=int(dnum)), tbl.filter(pa.array(days == dnum))


# ---------------------------------------------------------------------- ingest one file
class VisionIngestor:
    def __init__(self, client: VisionClient, hot: SQLiteHotStore, cold: ParquetColdStore, *,
                 block_bytes: int = 32 * 2**20, use_threads: bool = True) -> None:
        self.client, self.hot, self.cold = client, hot, cold
        self.block_bytes, self.use_threads = block_bytes, use_threads

    def done_periods(self, spec: TableSpec) -> set[str]:
        cols = self.hot.read_range(VISION_DONE, columns=["table_name", "period"])
        return {p for t, p in zip(cols["table_name"], cols["period"]) if t == spec.name}

    def _mark_done(self, spec: TableSpec, f: VisionFile, rows: int) -> None:
        self.hot.replace(VISION_DONE, [(spec.name, f.period, rows, now_ms())])
        self.client.release(f)

    def _batches(self, path: Path, names: list[str], header: bool) -> Iterator[pa.RecordBatch]:
        return _csv_batches(path, names, header, self.block_bytes, self.use_threads)

    def ingest_klines(self, inst: Instrument, market: Market, spec: TableSpec, f: VisionFile) -> int:
        path = self.client.download(f)
        n = rejected = 0
        for batch in self._batches(path, KLINE_COLS, market.vision_header):
            rows = klines_rows(batch)
            res = validate_rows(spec, rows, venue=inst.venue)
            if res.rejected:
                rejected += res.n_rejected
                log.warning("%s %s: %d rows rejected, e.g. %s", spec.name, f.period, res.n_rejected, res.rejected[:2])
            n += self.hot.upsert(spec, res.good)
        self._mark_done(spec, f, n)
        return n

    def ingest_agg_trades(self, inst: Instrument, market: Market, spec: TableSpec, f: VisionFile) -> int:
        """Stream the file into cold day files, one open day writer at a time (memory ≈ one CSV block)."""
        path = self.client.download(f)
        names = AGG_COLS_FUT if market.vision_header else AGG_COLS_SPOT
        cur: DayWriter | None = None
        cur_day: dt.date | None = None
        total = 0
        try:
            for batch in self._batches(path, names, market.vision_header):
                tbl = agg_table(batch)
                # validation (vectorised): price/qty > 0, first ≤ last, time range
                ok = pc.and_(pc.and_(pc.greater(tbl["price"], 0), pc.greater(tbl["qty"], 0)),
                             pc.less_equal(tbl["first_id"], tbl["last_id"]))
                bad = tbl.num_rows - pc.sum(ok.cast(pa.int64())).as_py()
                if bad:
                    log.warning("%s %s: %d invalid aggTrades dropped (logged)", spec.name, f.period, bad)
                    tbl = tbl.filter(ok)
                for day, part in _day_slices(tbl, tbl["ts"].to_numpy() // MS_PER_DAY):
                    if cur_day is not None and day < cur_day:        # out of order (not expected): merge it
                        total += self.cold.write_day(inst, spec, day, part, merge=True)
                        continue
                    if day != cur_day:
                        if cur is not None:
                            total += cur.commit()
                        cur, cur_day = self.cold.day_writer(inst, spec, day, dense_key=True), day
                    cur.write(part)
            if cur is not None:
                total += cur.commit()
                cur = None
        finally:
            if cur is not None:
                cur.abort()
        self._mark_done(spec, f, total)
        return total

    def ingest_metrics(self, inst: Instrument, spec: TableSpec, f: VisionFile) -> int:
        path = self.client.download(f)
        with zipfile.ZipFile(path) as z, z.open(z.namelist()[0]) as fh:
            rows = [metrics_vision(r) for r in csv.DictReader(io.TextIOWrapper(fh, encoding="utf-8"))]
        res = validate_rows(spec, rows, venue=inst.venue)
        n = self.hot.upsert(spec, res.good)
        self._mark_done(spec, f, n)
        return n


def choose_kline_source(tf: Timeframe, start_ms: int, end_ms: int, limit: int) -> str:
    """REST when it needs few requests (higher TFs), Vision otherwise (1m/5m/15m history)."""
    return "rest" if (end_ms - start_ms) / tf.ms / limit <= 200 else "vision"
