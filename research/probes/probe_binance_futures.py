"""P1.3 — Binance USDⓈ-M futures probe: contracts, history depth, derivatives context, XAUUSDT hours."""
from __future__ import annotations

import asyncio
import collections
import datetime as dt
import sys
import time
from pathlib import Path

import httpx
import orjson
import websockets

sys.path.insert(0, str(Path(__file__).parent))
from _report import Report, Timer  # noqa: E402

from tradingsystem.core.timeutil import MS_PER_DAY, iso, now_ms  # noqa: E402

BASE = "https://fapi.binance.com"
SYMBOLS = ["BTCUSDT", "ETHUSDT", "XAUUSDT", "PAXGUSDT"]


def get(client: httpx.Client, path: str, **params):
    with Timer() as t:
        r = client.get(BASE + path, params=params)
    if r.status_code != 200:
        return {"error": r.status_code, "body": r.text[:200]}, t.ms
    return r.json(), t.ms


async def liquidation_sample(seconds: int) -> dict[str, int]:
    counts: dict[str, int] = collections.Counter()
    url = "wss://fstream.binance.com/market/ws/!forceOrder@arr"
    end = time.time() + seconds
    try:
        async with websockets.connect(url, open_timeout=15) as ws:
            while time.time() < end:
                try:
                    msg = orjson.loads(await asyncio.wait_for(ws.recv(), timeout=max(0.1, end - time.time())))
                except asyncio.TimeoutError:
                    break
                counts[msg["o"]["s"]] += 1
    except Exception as exc:  # noqa: BLE001
        counts["error:" + type(exc).__name__] += 1
    return dict(counts)


async def _count(url: str, secs: float) -> int:
    n = 0
    try:
        async with websockets.connect(url, open_timeout=10) as w:
            end = time.time() + secs
            while time.time() < end:
                try:
                    await asyncio.wait_for(w.recv(), timeout=max(0.05, end - time.time()))
                    n += 1
                except asyncio.TimeoutError:
                    break
    except Exception:  # noqa: BLE001
        return -1
    return n


async def ws_routing() -> dict[str, dict[str, int]]:
    streams = ["btcusdt@bookTicker", "btcusdt@depth20@100ms", "btcusdt@aggTrade", "btcusdt@kline_1m",
               "btcusdt@markPrice@1s", "xauusdt@aggTrade", "xauusdt@bookTicker"]
    routes = ["ws", "public/ws", "market/ws"]
    jobs = {(s, r): _count(f"wss://fstream.binance.com/{r}/{s}", 6) for s in streams for r in routes}
    res = await asyncio.gather(*jobs.values())
    out: dict[str, dict[str, int]] = collections.defaultdict(dict)
    for (s, r), n in zip(jobs, res):
        out[s][r] = n
    return dict(out)


def main() -> None:
    rep = Report("probe_binance_futures", "Binance USDⓈ-M futures — actual data available (P1.3)")
    with httpx.Client(timeout=20) as c:
        info, _ = get(c, "/fapi/v1/exchangeInfo")
        by = {s["symbol"]: s for s in info["symbols"]}
        rep.h("Contracts")
        rows = []
        for s in SYMBOLS:
            x = by.get(s)
            if not x:
                rows.append([s, "NOT LISTED", "", "", "", ""])
                continue
            f = {y["filterType"]: y for y in x["filters"]}
            rows.append([s, x["contractType"], x["status"], iso(x["onboardDate"]), f["PRICE_FILTER"]["tickSize"],
                         f["LOT_SIZE"]["stepSize"]])
        rep.table(["symbol", "contractType", "status", "onboard", "tickSize", "stepSize"], rows)

        rep.h("24h activity")
        rows = []
        for s in SYMBOLS:
            t, _ = get(c, "/fapi/v1/ticker/24hr", symbol=s)
            if "error" not in t:
                rows.append([s, t["lastPrice"], round(float(t["quoteVolume"]) / 1e6, 1), t["count"]])
        rep.table(["symbol", "last", "quote vol (M USDT)", "trades 24h"], rows)

        rep.h("History depth per endpoint")
        rows = []
        for s in SYMBOLS:
            k, _ = get(c, "/fapi/v1/klines", symbol=s, interval="1m", startTime=0, limit=1)
            onboard = by[s]["onboardDate"]
            fr, _ = get(c, "/fapi/v1/fundingRate", symbol=s, startTime=onboard, limit=1)
            since = now_ms() - 29 * MS_PER_DAY - 3_600_000
            oi, _ = get(c, "/futures/data/openInterestHist", symbol=s, period="5m", limit=1, startTime=since)
            ls, _ = get(c, "/futures/data/globalLongShortAccountRatio", symbol=s, period="5m", limit=1, startTime=since)
            tk, _ = get(c, "/futures/data/takerlongshortRatio", symbol=s, period="5m", limit=1, startTime=since)
            ag, _ = get(c, "/fapi/v1/aggTrades", symbol=s, startTime=now_ms() - 2 * MS_PER_DAY + 60_000, limit=1)

            def first(x, key):
                if isinstance(x, list) and x:
                    return iso(int(x[0][key])) if not isinstance(x[0], list) else iso(x[0][0])
                return f"n/a ({x.get('error') if isinstance(x, dict) else 'empty'})"
            rows.append([s, first(k, 0) if isinstance(k, list) and k else "n/a", first(fr, "fundingTime"),
                         first(oi, "timestamp"), first(ls, "timestamp"), first(tk, "timestamp"), first(ag, "T")])
        rep.table(["symbol", "klines 1m", "funding", "OI hist 5m", "L/S ratio 5m", "taker ratio 5m", "aggTrades REST"],
                  rows)
        rep.p("`openInterestHist` / ratio endpoints serve only the last ~30 days via REST (older → HTTP 400) and "
              "`/fapi/v1/aggTrades` only searches the last **2 days** (error -4166) → longer history comes from "
              "Binance Vision (`aggTrades`, `metrics`, `fundingRate`) or our own live collection.")

        rep.h("Mark price / funding / open interest now")
        rows = []
        for s in SYMBOLS:
            pi, _ = get(c, "/fapi/v1/premiumIndex", symbol=s)
            oi, _ = get(c, "/fapi/v1/openInterest", symbol=s)
            if "error" in pi:
                continue
            rows.append([s, pi["markPrice"], pi["indexPrice"], pi["lastFundingRate"], iso(pi["nextFundingTime"]),
                         oi.get("openInterest")])
        rep.table(["symbol", "mark", "index", "last funding", "next funding", "open interest"], rows)

        rep.h("XAUUSDT trading hours (1h klines, last 21 days)")
        k, _ = get(c, "/fapi/v1/klines", symbol="XAUUSDT", interval="1h", startTime=now_ms() - 21 * MS_PER_DAY,
                   limit=1000)
        by_wd: dict[str, list[float]] = collections.defaultdict(list)
        for row in k:
            wd = dt.datetime.fromtimestamp(row[0] / 1000, tz=dt.timezone.utc).strftime("%a")
            by_wd[wd].append(int(row[8]))
        rep.table(["weekday (UTC)", "hours with bars", "avg trades/hour", "min trades/hour"],
                  [[d, len(v), round(sum(v) / len(v)), min(v)] for d, v in by_wd.items()])
        rep.raw["xau_hours"] = {d: v for d, v in by_wd.items()}

    rep.h("WebSocket routing (USDⓈ-M)")
    routes = asyncio.run(ws_routing())
    rep.table(["stream", "/ws (legacy)", "/public/ws", "/market/ws"],
              [[k, *[v.get(r, "-") for r in ("ws", "public/ws", "market/ws")]] for k, v in routes.items()])
    rep.p("Messages received in 6 s per route. Binance split USDⓈ-M market streams: high-frequency book streams on "
          "`/public`, trade/mark/kline/liquidation streams on `/market`. The ingester must route per stream type.")
    rep.raw["ws_routing"] = routes

    rep.h("Liquidation stream sample (!forceOrder@arr on /market, 120 s)")
    liq = asyncio.run(liquidation_sample(120))
    rep.table(["symbol", "events in 120 s"], sorted(liq.items(), key=lambda x: -x[1])[:15])
    rep.p("Binance pushes at most one liquidation snapshot per symbol per second → the stream is **partial** "
          "(flag as such; low weight in analysis).")
    rep.save()


if __name__ == "__main__":
    main()
