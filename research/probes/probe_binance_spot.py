"""P1.1 — Binance spot REST probe: fields, filters, history depth, request weights, latency."""
from __future__ import annotations

import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent))
from _report import Report, Timer, summary  # noqa: E402

from tradingsystem.core.timeutil import iso, now_ms  # noqa: E402

BASE = "https://api.binance.com"
SYMBOLS = ["BTCUSDT", "ETHUSDT"]
TFS = ["1m", "5m", "15m", "1h", "4h", "1d", "1w"]
KLINE_FIELDS = ["open_time", "open", "high", "low", "close", "volume", "close_time", "quote_volume",
                "trades", "taker_buy_base", "taker_buy_quote", "ignore"]


def get(client: httpx.Client, path: str, **params):
    with Timer() as t:
        r = client.get(BASE + path, params=params)
    r.raise_for_status()
    return r.json(), t.ms, int(r.headers.get("x-mbx-used-weight-1m", -1))


def main() -> None:
    rep = Report("probe_binance_spot", "Binance spot REST — actual data available (P1.1)")
    with httpx.Client(timeout=20, http2=False) as client:
        # --- latency: cold vs keep-alive
        cold = []
        for _ in range(3):
            with httpx.Client(timeout=20) as c2, Timer() as t:
                c2.get(BASE + "/api/v3/time").raise_for_status()
            cold.append(t.ms)
        warm = []
        for _ in range(20):
            _, ms, _ = get(client, "/api/v3/time")
            warm.append(ms)
        rep.h("Latency")
        rep.table(["mode", "n", "p50 ms", "p90 ms", "max ms"],
                  [["cold (new TLS)", *[round(summary(cold)[k], 1) for k in ("n", "p50", "p90", "max")]],
                   ["keep-alive", *[round(summary(warm)[k], 1) for k in ("n", "p50", "p90", "max")]]])
        rep.raw["latency"] = {"cold": cold, "warm": warm}

        # --- exchange info filters
        info, _, w = get(client, "/api/v3/exchangeInfo", symbols='["BTCUSDT","ETHUSDT"]')
        rep.h("Symbol filters")
        rows = []
        for s in info["symbols"]:
            f = {x["filterType"]: x for x in s["filters"]}
            rows.append([s["symbol"], s["status"], f["PRICE_FILTER"]["tickSize"], f["LOT_SIZE"]["stepSize"],
                         f.get("NOTIONAL", {}).get("minNotional", "-"), ",".join(s.get("orderTypes", []))])
        rep.table(["symbol", "status", "tickSize", "stepSize", "minNotional", "orderTypes"], rows)
        rep.raw["rate_limits"] = info.get("rateLimits")
        rep.bullet("Rate limits: " + "; ".join(
            f"{x['rateLimitType']} {x['limit']}/{x['intervalNum']}{x['interval'][0]}" for x in info["rateLimits"]))

        # --- klines: fields + earliest bar per TF
        rep.h("Klines (all timeframes)")
        rep.p("Fields: `" + ", ".join(KLINE_FIELDS) + "` — `taker_buy_base` gives the **exact** aggressive-buy "
              "volume per candle (delta = 2·taker_buy − volume).")
        rows = []
        for sym in SYMBOLS:
            for tf in TFS:
                first, _, _ = get(client, "/api/v3/klines", symbol=sym, interval=tf, startTime=0, limit=1)
                last, ms, w = get(client, "/api/v3/klines", symbol=sym, interval=tf, limit=2)
                rows.append([sym, tf, iso(first[0][0]), iso(last[-1][0]), round(ms, 1), w])
        rep.table(["symbol", "tf", "earliest open", "latest open (forming)", "req ms", "used weight/1m"], rows)
        k, _, _ = get(client, "/api/v3/klines", symbol="BTCUSDT", interval="1m", limit=1)
        rep.code(str(dict(zip(KLINE_FIELDS, k[0]))))

        # --- aggTrades
        rep.h("aggTrades")
        agg, ms, w = get(client, "/api/v3/aggTrades", symbol="BTCUSDT", limit=1000)
        span_s = (agg[-1]["T"] - agg[0]["T"]) / 1000
        first_agg, _, _ = get(client, "/api/v3/aggTrades", symbol="BTCUSDT", fromId=0, limit=1)
        rep.p("Fields: `a` agg id, `p` price, `q` qty, `f`/`l` first/last trade id, `T` time ms, `m` buyer-is-maker "
              "(m=true → aggressive SELL), `M` best-match.")
        rep.table(["symbol", "first agg id", "first time", "last 1000 span (s)", "≈ agg/s now", "req ms", "weight"],
                  [["BTCUSDT", first_agg[0]["a"], iso(first_agg[0]["T"]), round(span_s, 1),
                    round(1000 / max(span_s, 1e-9), 1), round(ms, 1), w]])
        rep.code(str(agg[-1]))
        rep.raw["agg_sample"] = agg[-3:]

        # --- raw trades
        tr, ms, w = get(client, "/api/v3/trades", symbol="BTCUSDT", limit=5)
        rep.h("trades (raw)")
        rep.code(str(tr[-1]))

        # --- depth: span covered by N levels
        rep.h("Order book depth (REST snapshot)")
        rows = []
        for sym in SYMBOLS:
            for lim in (20, 100, 1000, 5000):
                d, ms, w = get(client, "/api/v3/depth", symbol=sym, limit=lim)
                bb, ba = float(d["bids"][0][0]), float(d["asks"][0][0])
                lo, hi = float(d["bids"][-1][0]), float(d["asks"][-1][0])
                mid = (bb + ba) / 2
                rows.append([sym, lim, round(ms, 1), w, round((mid - lo) / mid * 100, 4), round((hi - mid) / mid * 100, 4),
                             round(sum(float(q) for _, q in d["bids"]), 3), round(sum(float(q) for _, q in d["asks"]), 3)])
        rep.table(["symbol", "levels", "req ms", "weight", "bid span %", "ask span %", "bid qty", "ask qty"], rows)
        rep.p("Interpretation: the top 20 levels of BTCUSDT span only a few cents to dollars (tick 0.01), i.e. "
              "micro-structure noise for a 15m decision; depth for analysis should be measured as liquidity within "
              "±x % bands (like Binance Vision futures `bookDepth`).")

        # --- bookTicker
        bt, _, _ = get(client, "/api/v3/ticker/bookTicker", symbol="BTCUSDT")
        rep.h("bookTicker")
        rep.code(str(bt))
    rep.raw["generated"] = now_ms()
    rep.save()


if __name__ == "__main__":
    main()
