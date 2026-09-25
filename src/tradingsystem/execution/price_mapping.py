"""Analysis → execution price translation and basis check (P7.3, spec §7.2, D-027).

Levels produced on the analysis instrument (e.g. Binance BTCUSDT) are shifted by the *live* basis
(execution mid − analysis mid) so structural distances (entry↔SL↔TP) are preserved, then rounded to the
execution tick. Execution is refused when the live basis deviates from its recent median by more than the
configured threshold (quotes out of line / stale feed) or when either quote is stale.
"""
from __future__ import annotations

import copy
import statistics
from dataclasses import dataclass


@dataclass
class BasisCheck:
    ok: bool
    basis: float
    median_basis: float | None
    deviation_pct: float | None
    reason: str


def check_basis(analysis_mid: float, exec_mid: float, recent_basis: list[float], max_dev_pct: float) -> BasisCheck:
    basis = exec_mid - analysis_mid
    if not recent_basis:
        return BasisCheck(True, basis, None, None, "no basis history — live basis used")
    med = statistics.median(recent_basis)
    dev = abs(basis - med) / exec_mid * 100
    if dev > max_dev_pct:
        return BasisCheck(False, basis, med, dev, f"basis {basis:.2f} deviates {dev:.3f}% from its 60-min median "
                                                   f"{med:.2f} (> {max_dev_pct}%)")
    return BasisCheck(True, basis, med, dev, "basis within tolerance")


def _round(x: float, tick: float) -> float:
    return round(round(x / tick) * tick, 10)


def translate(rec: dict, basis: float, tick: float) -> dict:
    """Shift every price level of a recommendation dict by ``basis`` and round to ``tick``."""
    r = copy.deepcopy(rec)
    if r.get("entry"):
        for k in ("price", "range_min", "range_max"):
            if r["entry"].get(k) is not None:
                r["entry"][k] = _round(r["entry"][k] + basis, tick)
    if r.get("stop_loss") is not None:
        r["stop_loss"] = _round(r["stop_loss"] + basis, tick)
    for tp in r.get("take_profits", []):
        tp["price"] = _round(tp["price"] + basis, tick)
    for c in (r.get("next_review") or {}).get("conditions", []):
        if c.get("kind") in ("price_above", "price_below", "candle_close_above", "candle_close_below"):
            c["value"] = _round(c["value"] + basis, tick)
    r["price_reference_translated"] = {"basis": basis, "from": rec.get("price_reference")}
    return r
