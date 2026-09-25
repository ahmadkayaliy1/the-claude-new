"""Position sizing from % risk (P9.2, spec §7.3 — never a fixed size).

lots = floor(risk_usd / (|entry − SL| × USD-per-price-unit-per-lot) / step) × step
USD-per-price-unit-per-lot = contract size for USD-quoted symbols (verified with order_calc_profit, P1.5); the
MT5 backend re-checks with ``order_calc_profit`` before sending. If even the minimum lot risks more than the
allowed maximum, the trade is rejected — the actual risk at the minimum lot is reported either way (D-021).
"""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass
class SizeResult:
    ok: bool
    lots: float
    risk_usd: float
    risk_pct: float
    reason: str
    target_risk_pct: float
    min_lot_risk_pct: float


def size_position(*, equity: float, target_risk_pct: float, max_risk_pct: float, entry: float, stop: float,
                  contract_size: float, volume_min: float, volume_step: float, volume_max: float | None = None,
                  usd_per_unit_per_lot: float | None = None) -> SizeResult:
    per_lot = abs(entry - stop) * (usd_per_unit_per_lot if usd_per_unit_per_lot is not None else contract_size)
    if equity <= 0 or per_lot <= 0:
        return SizeResult(False, 0.0, 0.0, 0.0, "invalid equity or zero stop distance", target_risk_pct, float("inf"))
    min_lot_risk_pct = volume_min * per_lot / equity * 100
    risk_usd = equity * target_risk_pct / 100
    steps = math.floor(risk_usd / per_lot / volume_step + 1e-9)
    lots = round(steps * volume_step, 8)
    if volume_max is not None:
        lots = min(lots, volume_max)
    if lots < volume_min:
        if min_lot_risk_pct <= max_risk_pct:
            lots = volume_min
            reason = (f"target {target_risk_pct:.2f}% is below the minimum lot; minimum lot risks "
                      f"{min_lot_risk_pct:.2f}% (≤ max {max_risk_pct:.2f}%)")
        else:
            return SizeResult(False, 0.0, volume_min * per_lot, min_lot_risk_pct,
                              f"minimum lot {volume_min} risks {min_lot_risk_pct:.2f}% of equity > max {max_risk_pct:.2f}%",
                              target_risk_pct, min_lot_risk_pct)
    else:
        reason = "sized to target risk"
    actual = lots * per_lot
    return SizeResult(True, lots, actual, actual / equity * 100, reason, target_risk_pct, min_lot_risk_pct)
