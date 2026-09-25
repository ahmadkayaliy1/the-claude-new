"""Per-market endpoint facts (measured in P1.1/P1.3/P1.4; see docs/exploration/)."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Market:
    venue: str
    rest_klines: str
    rest_agg_trades: str
    rest_depth: str
    rest_time: str
    kline_limit: int
    agg_limit: int
    depth_limit: int
    depth_weight: int
    vision_prefix: str             # data/<prefix>/{monthly,daily}/...
    vision_header: bool            # futures files carry a header row
    agg_rest_window_ms: int | None  # how far back REST aggTrades can search (None = unlimited by id)


SPOT = Market(
    venue="binance_spot", rest_klines="/api/v3/klines", rest_agg_trades="/api/v3/aggTrades",
    rest_depth="/api/v3/depth", rest_time="/api/v3/time", kline_limit=1000, agg_limit=1000,
    depth_limit=5000, depth_weight=250, vision_prefix="spot", vision_header=False, agg_rest_window_ms=None,
)
USDM = Market(
    venue="binance_usdm", rest_klines="/fapi/v1/klines", rest_agg_trades="/fapi/v1/aggTrades",
    rest_depth="/fapi/v1/depth", rest_time="/fapi/v1/time", kline_limit=1500, agg_limit=1000,
    depth_limit=1000, depth_weight=20, vision_prefix="futures/um", vision_header=True,
    agg_rest_window_ms=2 * 86_400_000 - 3_600_000,
)
MARKETS = {m.venue: m for m in (SPOT, USDM)}

DEPTH_BANDS_PCT = (0.1, 0.25, 0.5, 1.0, 2.0, 5.0)
