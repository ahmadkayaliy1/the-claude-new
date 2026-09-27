"""What a system holds at its venue, one row per decision and kind (Phase 2, D-042).

Both backends list their legs (a decision with several take profits has several positions / pending orders);
:func:`aggregate` folds them into rows the executor publishes (``collector_status`` "executor" detail), the risk gate
checks (no second trade in the direction of a live one) and the model reads (``account.open_positions`` /
``account.pending_orders``).
"""
from __future__ import annotations

from ..core.timeutil import iso

MAX_ROWS = 20           # published per system (the dashboard and the payload need a handful)


def leg(*, pair: str, decision: str, kind: str, side: str, order_type: str, volume: float, price: float,
        sl: float | None, tp: float | None, profit_usd: float | None = None, since_ms: int | None = None,
        expires_ms: int | None = None) -> dict:
    return {"pair": pair, "decision": decision, "kind": kind, "side": side, "order_type": order_type,
            "volume": volume, "price": price, "sl": sl, "tp": tp, "profit_usd": profit_usd, "since_ms": since_ms,
            "expires_ms": expires_ms}


def aggregate(legs: list[dict]) -> list[dict]:
    """Legs → one row per (decision, kind): volume summed, price volume-weighted, every TP listed, profit summed.
    ``sl`` is the first leg's (for display); ``sl_missing`` is True when ANY leg has none (the monitor's critical)."""
    groups: dict[tuple, list[dict]] = {}
    for x in legs:
        groups.setdefault((x["pair"], x["decision"], x["kind"], x["side"]), []).append(x)
    out = []
    for (pair, decision, kind, side), xs in groups.items():
        vol = sum(x["volume"] for x in xs)
        price = sum(x["price"] * x["volume"] for x in xs) / vol if vol else xs[0]["price"]
        profits = [x["profit_usd"] for x in xs if x["profit_usd"] is not None]
        since = min((x["since_ms"] for x in xs if x["since_ms"]), default=None)
        exp = max((x["expires_ms"] for x in xs if x["expires_ms"]), default=None)
        out.append({"pair": pair, "decision": decision[:8], "kind": kind, "side": side,
                    "order_type": xs[0]["order_type"], "volume": round(vol, 8), "price": round(price, 8),
                    "sl": xs[0]["sl"], "sl_missing": any(not x["sl"] for x in xs),
                    "tps": sorted({x["tp"] for x in xs if x["tp"]}),
                    "profit_usd": round(sum(profits), 2) if profits else None,
                    "since": iso(since) if since else None, "expires": iso(exp) if exp else None})
    out.sort(key=lambda r: (r["pair"], r["since"] or ""))
    return out[:MAX_ROWS]


def live_sides(rows: list[dict], pair: str) -> dict[str, list[str]]:
    """``{"BUY": ["position abc12345", ...], "SELL": [...]}`` of ``pair``'s live positions and pending orders."""
    out: dict[str, list[str]] = {}
    for r in rows:
        if r["pair"] == pair:
            out.setdefault(r["side"], []).append(f"{r['kind']} {r['decision']}")
    return out


__all__ = ["MAX_ROWS", "aggregate", "leg", "live_sides"]
