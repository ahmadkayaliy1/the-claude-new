"""P1.12 — Price-matching recorder (research grade, runs for days).

Records, with local UTC receive timestamps:
  * Binance spot  bookTicker: BTCUSDT, ETHUSDT                (price changes only)
  * Binance USDⓈ-M bookTicker: BTCUSDT, ETHUSDT, XAUUSDT       (price changes only, + E/T times)
  * MT5 ticks (bid/ask, server-time ``time_msc``): BTCUSD@, ETHUSD@, XAUUSD@
  * clock offset local↔Binance every 60 s (NTP-style, with RTT)
Output: ``data/research/price_matching/<stream>/<YYYY-MM-DD>/<HHMMSS>.parquet`` (UTC), flushed
every ``--flush-s`` seconds, plus ``status.json`` (heartbeat, counts, pid).

Stop: create the file ``data/research/price_matching/STOP`` (or Ctrl+C) — buffers are flushed.
MT5 ``time_msc`` is stored raw (server-time scale); the analysis (P7.1) derives the offset.
Only real market data is recorded; nothing is synthesised.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import multiprocessing as mp
import os
import queue
import threading
import time
from collections import defaultdict
from pathlib import Path

import httpx
import orjson
import pyarrow as pa
import pyarrow.parquet as pq
import websockets

from tradingsystem.core.logsetup import setup_logging
from tradingsystem.core.settings import PROJECT_ROOT
from tradingsystem.core.timeutil import iso, now_ms

OUT = PROJECT_ROOT / "data" / "research" / "price_matching"
SPOT_WS = "wss://stream.binance.com:9443/stream?streams=btcusdt@bookTicker/ethusdt@bookTicker"
USDM_WS = "wss://fstream.binance.com/stream?streams=btcusdt@bookTicker/ethusdt@bookTicker/xauusdt@bookTicker"
MT5_PATH = r"C:/Program Files/MetaTrader 5/terminal64.exe"
MT5_SYMBOLS = ["BTCUSD@", "ETHUSD@", "XAUUSD@"]

log = logging.getLogger("recorder")

_BOOK = pa.schema([
    ("recv_ms", pa.int64()), ("symbol", pa.string()), ("u", pa.int64()),
    ("bid", pa.float64()), ("bid_qty", pa.float64()), ("ask", pa.float64()), ("ask_qty", pa.float64()),
    ("event_ms", pa.int64()), ("trans_ms", pa.int64()),
])
SCHEMAS = {
    "spot_book": _BOOK,
    "usdm_book": _BOOK,
    "mt5_ticks": pa.schema([
        ("recv_ms", pa.int64()), ("symbol", pa.string()), ("time_msc", pa.int64()),
        ("bid", pa.float64()), ("ask", pa.float64()), ("flags", pa.int32()),
    ]),
    "clock": pa.schema([
        ("recv_ms", pa.int64()), ("symbol", pa.string()), ("offset_ms", pa.float64()), ("rtt_ms", pa.int64()),
    ]),
    "events": pa.schema([
        ("recv_ms", pa.int64()), ("symbol", pa.string()), ("event", pa.string()), ("detail", pa.string()),
    ]),
}


class Buffers:
    """Thread-safe row buffers per stream, flushed to Parquet."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._rows: dict[str, list[dict]] = defaultdict(list)
        self.counts: dict[str, int] = defaultdict(int)
        self.last_recv: dict[str, int] = {}

    def add(self, stream: str, row: dict) -> None:
        with self._lock:
            self._rows[stream].append(row)
            self.counts[stream] += 1
            self.last_recv[stream] = row["recv_ms"]

    def flush(self) -> int:
        with self._lock:
            pending, self._rows = self._rows, defaultdict(list)
        written = 0
        stamp = time.strftime("%Y-%m-%d/%H%M%S", time.gmtime())
        for stream, rows in pending.items():
            if not rows:
                continue
            day, hms = stamp.split("/")
            path = OUT / stream / day / f"{hms}.parquet"
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            pq.write_table(pa.Table.from_pylist(rows, schema=SCHEMAS[stream]), tmp, compression="zstd")
            os.replace(tmp, path)
            written += len(rows)
        return written


async def binance_stream(name: str, url: str, buf: Buffers, stop: asyncio.Event) -> None:
    last_px: dict[str, tuple[str, str]] = {}
    backoff = 1.0
    while not stop.is_set():
        try:
            async with websockets.connect(url, open_timeout=15, ping_interval=20, max_size=2**20) as ws:
                log.info("%s connected", name)
                backoff = 1.0
                connected_at = time.monotonic()
                while not stop.is_set():
                    raw = await asyncio.wait_for(ws.recv(), timeout=30)
                    recv = now_ms()
                    d = orjson.loads(raw)["data"]
                    sym = d["s"]
                    px = (d["b"], d["a"])
                    if last_px.get(sym) == px:
                        continue  # quantity-only update
                    last_px[sym] = px
                    buf.add(name, {
                        "recv_ms": recv, "symbol": sym, "u": int(d["u"]),
                        "bid": float(d["b"]), "bid_qty": float(d["B"]),
                        "ask": float(d["a"]), "ask_qty": float(d["A"]),
                        "event_ms": int(d["E"]) if "E" in d else None,
                        "trans_ms": int(d["T"]) if "T" in d else None,
                    })
                    if time.monotonic() - connected_at > 23 * 3600:  # Binance drops at 24 h
                        log.info("%s planned reconnect (23 h)", name)
                        break
        except Exception as exc:  # noqa: BLE001 — log and reconnect, never die
            buf.add("events", {"recv_ms": now_ms(), "symbol": name, "event": "disconnect", "detail": repr(exc)[:300]})
            log.warning("%s disconnected: %r (retry in %.0fs)", name, exc, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)


def mt5_poller(q: mp.Queue, stop, interval_s: float) -> None:
    """Runs in its own process: MT5 calls hold the GIL, a slow terminal must not stall the WS loop."""
    import MetaTrader5 as mt5

    setup_logging("recorder-mt5", logs_dir=PROJECT_ROOT / "logs", console=False)

    state: dict[str, tuple[int, int]] = {}  # symbol -> (last_time_msc, n_ticks_seen_at_that_ms)
    connected = False
    while not stop.is_set():
        try:
            if not connected:
                if not mt5.initialize(path=MT5_PATH):
                    raise RuntimeError(f"initialize failed: {mt5.last_error()}")
                for s in MT5_SYMBOLS:
                    mt5.symbol_select(s, True)
                connected = True
                acc = mt5.account_info()
                log.info("mt5 connected server=%s", acc.server if acc else None)
                q.put(("events", {"recv_ms": now_ms(), "symbol": "mt5", "event": "connect",
                                   "detail": acc.server if acc else ""}))
            for s in MT5_SYMBOLS:
                if s not in state:
                    tick = mt5.symbol_info_tick(s)
                    if tick is None:
                        continue
                    state[s] = (int(tick.time_msc), 0)  # start at the latest tick (inclusive)
                last_msc, seen = state[s]
                ticks = mt5.copy_ticks_from(s, int(last_msc // 1000), 100_000, mt5.COPY_TICKS_ALL)
                recv = now_ms()
                if ticks is None:
                    raise RuntimeError(f"copy_ticks_from({s}) failed: {mt5.last_error()}")
                skip_at_last = seen
                new_last, new_seen = last_msc, seen
                for t in ticks:
                    tm = int(t["time_msc"])
                    if tm < last_msc:
                        continue
                    if tm == last_msc and skip_at_last > 0:
                        skip_at_last -= 1
                        continue
                    q.put(("mt5_ticks", {
                        "recv_ms": recv, "symbol": s, "time_msc": tm,
                        "bid": float(t["bid"]), "ask": float(t["ask"]), "flags": int(t["flags"]),
                    }))
                    if tm == new_last:
                        new_seen += 1
                    else:
                        new_last, new_seen = tm, 1
                state[s] = (new_last, new_seen)
        except Exception as exc:  # noqa: BLE001
            log.warning("mt5 error: %r", exc)
            q.put(("events", {"recv_ms": now_ms(), "symbol": "mt5", "event": "error", "detail": repr(exc)[:300]}))
            try:
                mt5.shutdown()
            except Exception:  # noqa: BLE001
                pass
            connected = False
            stop.wait(5)
            continue
        stop.wait(interval_s)
    try:
        mt5.shutdown()
    except Exception:  # noqa: BLE001
        pass


async def clock_sampler(buf: Buffers, stop: asyncio.Event) -> None:
    async with httpx.AsyncClient(timeout=10) as client:
        while not stop.is_set():
            try:
                t0 = now_ms()
                r = await client.get("https://api.binance.com/api/v3/time")
                t1 = now_ms()
                server = r.json()["serverTime"]
                buf.add("clock", {"recv_ms": t1, "symbol": "binance", "offset_ms": server - (t0 + t1) / 2,
                                  "rtt_ms": t1 - t0})
            except Exception as exc:  # noqa: BLE001
                log.warning("clock sample failed: %r", exc)
            try:
                await asyncio.wait_for(stop.wait(), timeout=60)
            except asyncio.TimeoutError:
                pass


async def flusher(buf: Buffers, stop: asyncio.Event, flush_s: float, started: int) -> None:
    while True:
        try:
            await asyncio.wait_for(stop.wait(), timeout=flush_s)
        except asyncio.TimeoutError:
            pass
        n = await asyncio.to_thread(buf.flush)
        status = {
            "pid": os.getpid(), "started": iso(started), "updated": iso(now_ms()),
            "counts": dict(buf.counts), "last_recv": {k: iso(v) for k, v in buf.last_recv.items()},
        }
        (OUT / "status.json").write_text(json.dumps(status, indent=2), encoding="utf-8")
        log.info("flushed %d rows; totals=%s", n, dict(buf.counts))
        if (OUT / "STOP").exists():
            log.info("STOP file found — shutting down")
            stop.set()
        if stop.is_set():
            return


async def main_async(args: argparse.Namespace) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "STOP").unlink(missing_ok=True)
    buf = Buffers()
    stop = asyncio.Event()
    tstop = threading.Event()
    started = now_ms()
    mq: mp.Queue = mp.Queue()
    mstop = mp.Event()
    proc = mp.Process(target=mt5_poller, args=(mq, mstop, args.mt5_interval_ms / 1000), daemon=True)
    proc.start()

    def drain() -> None:
        while not (tstop.is_set() and mq.empty()):
            try:
                buf.add(*mq.get(timeout=0.5))
            except queue.Empty:
                continue

    th = threading.Thread(target=drain, daemon=True)
    th.start()
    tasks = [
        asyncio.create_task(binance_stream("spot_book", SPOT_WS, buf, stop)),
        asyncio.create_task(binance_stream("usdm_book", USDM_WS, buf, stop)),
        asyncio.create_task(clock_sampler(buf, stop)),
    ]
    try:
        await flusher(buf, stop, args.flush_s, started)
    finally:
        stop.set()
        mstop.set()
        proc.join(timeout=15)
        tstop.set()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        th.join(timeout=10)
        n = buf.flush()
        log.info("final flush %d rows", n)


def main() -> None:
    global OUT
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--flush-s", type=float, default=300)
    ap.add_argument("--mt5-interval-ms", type=float, default=50)
    ap.add_argument("--out", default=str(OUT), help="output folder (tests use a scratch folder)")
    args = ap.parse_args()
    OUT = Path(args.out)
    setup_logging("recorder", logs_dir=PROJECT_ROOT / "logs", console=True)
    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
