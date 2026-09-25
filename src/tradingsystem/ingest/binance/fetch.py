"""Async REST paginators (gap-fill and small backfills). All yield row batches in TableSpec order."""
from __future__ import annotations

import logging
from typing import AsyncIterator

from ...core.timeframes import Timeframe
from . import parsers
from .markets import Market
from .rest import BinanceRest

log = logging.getLogger(__name__)
FIVE_MIN = 300_000


async def klines(rest: BinanceRest, m: Market, symbol: str, tf: Timeframe, start_ms: int, end_ms: int
                 ) -> AsyncIterator[list[tuple]]:
    """Closed candles with open_time in [start_ms, end_ms)."""
    t = tf.floor(start_ms) if tf.floor(start_ms) >= start_ms else tf.floor(start_ms) + tf.ms
    weight = 2 if m.venue == "binance_spot" else 5
    while t < end_ms:
        data = await rest.get(m.rest_klines, {"symbol": symbol, "interval": tf.binance_interval, "startTime": t,
                                              "endTime": end_ms - 1, "limit": m.kline_limit}, weight=weight)
        if not data:
            return
        now = rest.binance_now()
        rows = [parsers.kline_rest(r) for r in data if int(r[6]) < now]   # closed only (close_time passed)
        if rows:
            yield rows
        last_open = int(data[-1][0])
        if len(data) < m.kline_limit or last_open + tf.ms <= t:
            return
        t = last_open + tf.ms


async def agg_trades_from_id(rest: BinanceRest, m: Market, symbol: str, from_id: int, until_id: int | None = None,
                             max_requests: int = 5000) -> AsyncIterator[list[tuple]]:
    """aggTrades with id ≥ from_id (and < until_id when given)."""
    weight = 4 if m.venue == "binance_spot" else 20
    nxt = from_id
    for _ in range(max_requests):
        data = await rest.get(m.rest_agg_trades, {"symbol": symbol, "fromId": nxt, "limit": m.agg_limit}, weight=weight)
        if not data:
            return
        rows = [parsers.agg_trade_rest(d) for d in data if until_id is None or int(d["a"]) < until_id]
        if rows:
            yield rows
        last = int(data[-1]["a"])
        if len(data) < m.agg_limit or (until_id is not None and last + 1 >= until_id):
            return
        nxt = last + 1
    log.warning("agg_trades_from_id(%s) stopped after %d requests", symbol, max_requests)


async def agg_trades_first_id_after(rest: BinanceRest, m: Market, symbol: str, start_ms: int) -> int | None:
    """Id of the first aggTrade at/after start_ms (one-hour window search)."""
    weight = 4 if m.venue == "binance_spot" else 20
    data = await rest.get(m.rest_agg_trades, {"symbol": symbol, "startTime": start_ms,
                                              "endTime": start_ms + 3_600_000 - 1, "limit": 1}, weight=weight)
    return int(data[0]["a"]) if data else None


async def funding(rest: BinanceRest, symbol: str, start_ms: int) -> AsyncIterator[list[tuple]]:
    t = start_ms
    while True:
        data = await rest.get("/fapi/v1/fundingRate", {"symbol": symbol, "startTime": t, "limit": 1000})
        if not data:
            return
        yield [parsers.funding_rest(d) for d in data]
        if len(data) < 1000:
            return
        t = int(data[-1]["fundingTime"]) + 1


async def metrics_5m(rest: BinanceRest, symbol: str, start_ms: int, end_ms: int) -> list[tuple]:
    """5-minute derivatives metrics for [start, end) from the REST ratio endpoints (≤ 30 days back)."""
    params = {"symbol": symbol, "period": "5m", "startTime": start_ms, "endTime": end_ms - 1, "limit": 500}
    series = {}
    for key, path in (("oi", "/futures/data/openInterestHist"), ("top_acc", "/futures/data/topLongShortAccountRatio"),
                      ("top_pos", "/futures/data/topLongShortPositionRatio"),
                      ("glob", "/futures/data/globalLongShortAccountRatio"), ("taker", "/futures/data/takerlongshortRatio")):
        data = await rest.get(path, params)
        series[key] = {int(d["timestamp"]): d for d in data}
    stamps = sorted(set().union(*[s.keys() for s in series.values()]))
    return [parsers.metrics_rest(ts, series["oi"].get(ts), series["top_acc"].get(ts), series["top_pos"].get(ts),
                                 series["glob"].get(ts), series["taker"].get(ts)) for ts in stamps]
