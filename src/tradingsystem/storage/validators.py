"""Row validation before storage (spec §9: validate every row; never silently drop — rejects are returned
with a reason so the caller logs them as data-quality events).

Works on column batches (numpy) so both live micro-batches and multi-million-row backfills are cheap.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import numpy as np

from ..core.timeframes import Timeframe
from ..core.timeutil import now_ms
from .tablespec import TableSpec

MIN_TS = 1_483_228_800_000       # 2017-01-01 — nothing we collect is older
FUTURE_TOLERANCE_MS = 5_000


@dataclass
class ValidationResult:
    good: list[tuple]
    rejected: list[tuple[tuple, str]]

    @property
    def n_rejected(self) -> int:
        return len(self.rejected)


def _cols(spec: TableSpec, rows: Sequence[tuple]) -> dict[str, np.ndarray]:
    names = spec.column_names
    out = {}
    for i, n in enumerate(names):
        col = [r[i] for r in rows]
        t = spec.columns[i].sql_type
        if t == "TEXT":
            out[n] = np.array(col, dtype=object)
        else:
            out[n] = np.array([np.nan if v is None else v for v in col], dtype=np.float64)
    return out


def _checks(spec: TableSpec, c: dict[str, np.ndarray], now: int, venue: str) -> list[tuple[np.ndarray, str]]:
    time_col = c[spec.time_col]
    checks = [(~np.isfinite(time_col) | (time_col < MIN_TS) | (time_col > now + FUTURE_TOLERANCE_MS), "time out of range")]
    d = spec.datatype
    if d == "candles":
        o, h, l, cl = c["open"], c["high"], c["low"], c["close"]
        checks += [
            (~(np.isfinite(o) & np.isfinite(h) & np.isfinite(l) & np.isfinite(cl)), "non-finite OHLC"),
            (l <= 0, "non-positive low"),
            (h < np.maximum(o, cl) - 1e-9, "high below open/close"),
            (l > np.minimum(o, cl) + 1e-9, "low above open/close"),
        ]
        if "volume" in c:
            checks += [(c["volume"] < 0, "negative volume"),
                       (c["taker_buy_base"] > c["volume"] * (1 + 1e-9) + 1e-12, "taker buy > volume"),
                       (c["trades"] < 0, "negative trade count")]
        if "tick_volume" in c:
            checks.append((c["tick_volume"] < 0, "negative tick volume"))
        tf = spec.timeframe
        if tf is not None and venue != "mt5" and tf is not Timeframe.W1:
            checks.append((time_col % tf.ms != 0, f"open_time not aligned to {tf.value}"))
        if tf is Timeframe.W1 and venue != "mt5":
            checks.append(((time_col - 4 * 86_400_000) % tf.ms != 0, "weekly open not Monday 00:00 UTC"))
    elif d == "agg_trades":
        checks += [(~(c["price"] > 0), "non-positive price"), (~(c["qty"] > 0), "non-positive qty"),
                   (c["first_id"] > c["last_id"], "first_id > last_id")]
    elif d == "ticks":
        checks += [(~(c["bid"] > 0) | ~(c["ask"] > 0), "non-positive bid/ask"),
                   (c["ask"] < c["bid"], "ask < bid")]
    elif d == "book_ticker":
        checks += [(~(c["bid"] > 0) | ~(c["ask"] > 0), "non-positive bid/ask"),
                   (c["ask"] < c["bid"], "crossed book"), (c["bid_qty"] < 0, "negative qty")]
    elif d == "funding":
        checks.append((np.abs(c["funding_rate"]) > 0.05, "implausible funding rate"))
    elif d in ("open_interest",):
        checks.append((c["open_interest"] < 0, "negative open interest"))
    elif d == "mark_price":
        checks.append((~(c["mark_price"] > 0), "non-positive mark price"))
    elif d == "liquidations":
        checks += [(~(c["price"] > 0), "non-positive price"), (~(c["qty"] > 0), "non-positive qty")]
    return checks


def validate_rows(spec: TableSpec, rows: Sequence[tuple], *, venue: str, now: int | None = None) -> ValidationResult:
    if not rows:
        return ValidationResult([], [])
    c = _cols(spec, rows)
    reasons: list[str | None] = [None] * len(rows)
    with np.errstate(invalid="ignore"):
        for mask, reason in _checks(spec, c, now if now is not None else now_ms(), venue):
            for i in np.nonzero(mask)[0]:
                if reasons[i] is None:
                    reasons[i] = reason
    good = [r for r, why in zip(rows, reasons) if why is None]
    bad = [(r, why) for r, why in zip(rows, reasons) if why is not None]
    return ValidationResult(good, bad)
