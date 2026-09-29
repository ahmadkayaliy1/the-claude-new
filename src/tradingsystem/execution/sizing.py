"""Position sizing from % risk (P9.2, spec §7.3 — never a fixed size).

lots = floor(risk_usd / (|entry − SL| × USD-per-price-unit-per-lot) / step) × step
USD-per-price-unit-per-lot = contract size for USD-quoted symbols (verified with order_calc_profit, P1.5); the
MT5 backend re-checks with ``order_calc_profit`` before sending. If even the minimum lot risks more than the
allowed maximum, the trade is rejected — the actual risk at the minimum lot is reported either way (D-021).
"""
from __future__ import annotations

import math
from dataclasses import dataclass


def split_volume(total: float, fractions: list[float], step: float, vmin: float) -> list[float] | None:
    """Split ``total`` lots by fractions on the volume step; None if any leg would fall below the minimum (then the
    backends place ONE position at the take-profit with the largest fraction, ties → the nearest)."""
    steps = round(total / step)
    raw = [steps * f / sum(fractions) for f in fractions]
    legs = [math.floor(x) for x in raw]
    for i in sorted(range(len(raw)), key=lambda i: raw[i] - legs[i], reverse=True)[: steps - sum(legs)]:
        legs[i] += 1
    vols = [round(n * step, 8) for n in legs]
    return vols if all(v >= vmin - 1e-12 for v in vols) else None


def single_leg_index(fractions: list[float]) -> int:
    """The take-profit a single position goes to when the volume cannot be split: the largest close fraction,
    ties → the nearest target (take-profits are ordered nearest first)."""
    return max(range(len(fractions)), key=lambda i: (fractions[i], -i))


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


def effective_leverage(lots: float, contract_size: float, entry: float, equity: float) -> float:
    """Notional / equity — the risk gate's ``effective_leverage`` check and the snapshot's fit fields use this."""
    return lots * contract_size * entry / equity


def min_lot_fit(*, equity: float, entry: float, stop_dist: float, contract_size: float, volume_min: float,
                volume_step: float, max_risk_pct: float, max_leverage: float) -> dict:
    """Does the minimum lot pass BOTH the per-trade risk cap (``size_position``, at the given stop distance) and the
    leverage cap (the gate checks it only after sizing passes, so a failing size hides it)? Also the equity at which
    it would: max(min-lot risk / cap, min-lot notional / leverage cap)."""
    s = size_position(equity=equity, target_risk_pct=max_risk_pct, max_risk_pct=max_risk_pct, entry=entry,
                      stop=entry - stop_dist, contract_size=contract_size, volume_min=volume_min,
                      volume_step=volume_step)
    lev = effective_leverage(volume_min, contract_size, entry, equity)
    min_lot_risk_usd = s.min_lot_risk_pct / 100 * equity
    need = max(min_lot_risk_usd / (max_risk_pct / 100), volume_min * contract_size * entry / max_leverage)
    return {"fits_now": s.min_lot_risk_pct <= max_risk_pct and lev <= max_leverage, "leverage_at_min_lot": lev,
            "leverage_cap": max_leverage, "equity_for_min_lot": need}
