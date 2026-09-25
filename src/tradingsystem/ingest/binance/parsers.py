"""Row builders: Binance REST / WebSocket / Vision payloads → tuples in TableSpec column order.

Pure functions (no I/O) so they are unit-tested against recorded real payloads.
Column orders (see storage/tablespec.py):
  candles      (open_time, open, high, low, close, volume, quote_volume, trades, taker_buy_base, taker_buy_quote)
  agg_trades   (agg_id, ts, price, qty, first_id, last_id, is_buyer_maker)
  book_ticker  (key=update_id, ts, update_id, bid, bid_qty, ask, ask_qty)
  depth        (ts, percentage, depth, notional)
  funding      (funding_time, funding_rate, mark_price)
  open_interest(ts, open_interest, open_interest_value)
  metrics      (ts, sum_oi, sum_oi_value, count_toptrader_ls, sum_toptrader_ls, count_ls, sum_taker_ls_vol)
  mark_price   (ts, mark_price, index_price, est_settle_price, funding_rate, next_funding_time)
  liquidations (key, ts, side, price, avg_price, qty, filled_qty, status)
"""
from __future__ import annotations

import datetime as dt
from typing import Any, Iterable

UTC = dt.timezone.utc


def norm_ts(v: int | str) -> int:
    """Binance Vision spot files switched to µs timestamps in 2025 → normalise to ms."""
    x = int(v)
    return x // 1000 if x > 10**14 else x


def kline_rest(row: list[Any]) -> tuple:
    return (int(row[0]), float(row[1]), float(row[2]), float(row[3]), float(row[4]), float(row[5]),
            float(row[7]), int(row[8]), float(row[9]), float(row[10]))


def kline_ws(k: dict[str, Any]) -> tuple:
    return (int(k["t"]), float(k["o"]), float(k["h"]), float(k["l"]), float(k["c"]), float(k["v"]),
            float(k["q"]), int(k["n"]), float(k["V"]), float(k["Q"]))


def agg_trade_rest(d: dict[str, Any]) -> tuple:
    return (int(d["a"]), int(d["T"]), float(d["p"]), float(d["q"]), int(d["f"]), int(d["l"]), int(bool(d["m"])))


agg_trade_ws = agg_trade_rest     # same field names in the WS payload


def book_ticker(d: dict[str, Any], ts: int) -> tuple:
    u = int(d["u"])
    return (u, int(d.get("E", ts)), u, float(d["b"]), float(d["B"]), float(d["a"]), float(d["A"]))


def funding_rest(d: dict[str, Any]) -> tuple:
    mp = d.get("markPrice")
    return (int(d["fundingTime"]), float(d["fundingRate"]), float(mp) if mp not in (None, "") else None)


def mark_price_ws(d: dict[str, Any], minute_ts: int) -> tuple:
    return (minute_ts, float(d["p"]), float(d["i"]) if d.get("i") else None,
            float(d["P"]) if d.get("P") else None, float(d["r"]) if d.get("r") not in (None, "") else None,
            int(d["T"]) if d.get("T") else None)


def open_interest_rest(d: dict[str, Any], ts: int) -> tuple:
    return (ts, float(d["openInterest"]), None)


def open_interest_hist(d: dict[str, Any]) -> tuple:
    return (int(d["timestamp"]), float(d["sumOpenInterest"]), float(d["sumOpenInterestValue"]))


def liquidation_ws(o: dict[str, Any], seq: int) -> tuple:
    ts = int(o["T"])
    return (ts * 1000 + seq, ts, str(o["S"]), float(o["p"]), float(o["ap"]), float(o["q"]), float(o["z"]),
            str(o["X"]))


def metrics_vision(row: dict[str, str]) -> tuple:
    """Vision `metrics` CSV row → metrics tuple (create_time is 'YYYY-MM-DD HH:MM:SS' UTC)."""
    t = dt.datetime.strptime(row["create_time"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
    f = lambda k: float(row[k]) if row.get(k) not in (None, "") else None  # noqa: E731
    return (int(t.timestamp() * 1000), f("sum_open_interest"), f("sum_open_interest_value"),
            f("count_toptrader_long_short_ratio"), f("sum_toptrader_long_short_ratio"),
            f("count_long_short_ratio"), f("sum_taker_long_short_vol_ratio"))


def metrics_rest(ts: int, oi: dict | None, top_acc: dict | None, top_pos: dict | None, glob: dict | None,
                 taker: dict | None) -> tuple:
    """Assemble one metrics row from the five 5m REST endpoints (same semantics as the Vision file)."""
    g = lambda d, k: float(d[k]) if d and d.get(k) not in (None, "") else None  # noqa: E731
    return (ts, g(oi, "sumOpenInterest"), g(oi, "sumOpenInterestValue"), g(top_acc, "longShortRatio"),
            g(top_pos, "longShortRatio"), g(glob, "longShortRatio"), g(taker, "buySellRatio"))


def depth_bands(bids: Iterable[tuple[float, float]], asks: Iterable[tuple[float, float]], ts: int,
                bands: tuple[float, ...]) -> list[tuple]:
    """Cumulative liquidity within ±band % of the mid price (Vision `bookDepth` layout; bids negative %).

    A band is emitted only when the snapshot actually reaches that far on that side (no extrapolation).
    """
    bids = [(float(p), float(q)) for p, q in bids]
    asks = [(float(p), float(q)) for p, q in asks]
    if not bids or not asks:
        return []
    mid = (bids[0][0] + asks[0][0]) / 2
    out: list[tuple] = []
    for side, levels, sign in (("bid", bids, -1), ("ask", asks, 1)):
        reach = abs(levels[-1][0] - mid) / mid * 100
        for b in bands:
            if b > reach:
                break
            lim = mid * (1 + sign * b / 100)
            qty = notional = 0.0
            for p, q in levels:
                if (sign < 0 and p < lim) or (sign > 0 and p > lim):
                    break
                qty += q
                notional += p * q
            out.append((ts, sign * b, qty, notional))
    return out
