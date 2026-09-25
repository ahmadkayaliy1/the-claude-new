"""P1.2 — Binance spot WebSocket probe: message rates, latency, kline-close delay, depth20 span.

Usage: python -m tradingsystem probe binance_ws [minutes]   (default 30)
"""
from __future__ import annotations

import asyncio
import collections
import sys
import time
from pathlib import Path

import httpx
import orjson
import websockets

sys.path.insert(0, str(Path(__file__).parent))
from _report import Report, summary  # noqa: E402

from tradingsystem.core.timeutil import now_ms  # noqa: E402

SYMS = ["btcusdt", "ethusdt"]
STREAMS = [f"{s}@{t}" for s in SYMS for t in ("kline_1m", "kline_15m", "aggTrade", "bookTicker", "depth20@100ms")]
URL = "wss://stream.binance.com:9443/stream?streams=" + "/".join(STREAMS)


def clock_offset() -> tuple[float, int]:
    best = None
    with httpx.Client(timeout=10) as c:
        c.get("https://api.binance.com/api/v3/time")
        for _ in range(5):
            t0 = now_ms()
            srv = c.get("https://api.binance.com/api/v3/time").json()["serverTime"]
            t1 = now_ms()
            if best is None or t1 - t0 < best[1]:
                best = (srv - (t0 + t1) / 2, t1 - t0)
    return best  # (binance - local, rtt)


async def run(minutes: float) -> dict:
    off, rtt = clock_offset()
    counts: dict[str, int] = collections.Counter()
    nbytes: dict[str, int] = collections.Counter()
    lat: dict[str, list[float]] = collections.defaultdict(list)
    close_delay: dict[str, list[float]] = collections.defaultdict(list)
    depth_span: dict[str, list[float]] = collections.defaultdict(list)
    reconnects = 0
    end = time.time() + minutes * 60
    while time.time() < end:
        try:
            async with websockets.connect(URL, open_timeout=15, max_size=2**22) as ws:
                while time.time() < end:
                    raw = await asyncio.wait_for(ws.recv(), timeout=30)
                    recv = now_ms() + off  # receive time on Binance's clock
                    msg = orjson.loads(raw)
                    stream, d = msg["stream"], msg["data"]
                    kind = stream.split("@", 1)[1]
                    counts[stream] += 1
                    nbytes[stream] += len(raw)
                    if "E" in d:
                        lat[kind].append(recv - d["E"])
                    if kind.startswith("kline") and d["k"]["x"]:
                        close_delay[kind].append(recv - (d["k"]["T"] + 1))
                    if kind.startswith("depth20"):
                        bids, asks = d["bids"], d["asks"]
                        mid = (float(bids[0][0]) + float(asks[0][0])) / 2
                        depth_span[stream].append((float(asks[-1][0]) - float(bids[-1][0])) / mid * 100)
        except Exception as exc:  # noqa: BLE001
            reconnects += 1
            print("reconnect:", repr(exc))
            await asyncio.sleep(2)
    secs = minutes * 60
    return {"offset_ms": off, "rtt_ms": rtt, "secs": secs, "counts": dict(counts), "bytes": dict(nbytes),
            "lat": {k: summary(v) for k, v in lat.items()}, "close_delay": {k: summary(v) for k, v in close_delay.items()},
            "depth_span": {k: summary(v) for k, v in depth_span.items()}, "reconnects": reconnects}


def main() -> None:
    minutes = float(sys.argv[1]) if len(sys.argv) > 1 else 30
    res = asyncio.run(run(minutes))
    rep = Report("probe_binance_ws", f"Binance spot WebSocket — {minutes:g} min measurement (P1.2)")
    rep.p(f"Clock offset (Binance − local) = {res['offset_ms']:.0f} ms (RTT {res['rtt_ms']} ms); latencies below are "
          f"on Binance's clock. Reconnects: {res['reconnects']}.")
    rep.h("Message rates")
    rep.table(["stream", "msgs", "msg/s", "KB/s"],
              [[s, n, round(n / res["secs"], 2), round(res["bytes"][s] / res["secs"] / 1024, 2)]
               for s, n in sorted(res["counts"].items())])
    rep.h("Event → receive latency (ms)")
    rep.table(["kind", "n", "p50", "p90", "p99", "max"],
              [[k, v["n"], round(v["p50"]), round(v["p90"]), round(v["p99"]), round(v["max"])] for k, v in res["lat"].items()])
    rep.h("Kline close delay (receive − candle end, ms)")
    rep.table(["kind", "n", "p50", "p90", "max"],
              [[k, v["n"], round(v["p50"]), round(v["p90"]), round(v["max"])] for k, v in res["close_delay"].items()])
    rep.h("depth20 price span (% of mid, best-20 bid to best-20 ask)")
    rep.table(["stream", "p50 %", "p90 %", "max %"],
              [[k, round(v["p50"], 4), round(v["p90"], 4), round(v["max"], 4)] for k, v in res["depth_span"].items()])
    rep.raw = res
    rep.save()


if __name__ == "__main__":
    main()
