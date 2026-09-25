"""Paper (dry-run) backend (P9.3, spec §9): simulated orders filled tick-by-tick on the *real* bid/ask stream of
the execution instrument. Nothing is sent to any broker.

Fill rules: BUY fills at the ask, SELL at the bid; BUY_LIMIT when ask ≤ price, BUY_STOP when ask ≥ price
(fills at the ask → real slippage), SELL_LIMIT when bid ≥ price, SELL_STOP when bid ≤ price. A BUY position's
SL/TP trigger on the bid (exit at the bid, so gaps slip through the stop), a SELL's on the ask. Multiple TPs
are separate legs sized by close fraction; ``move_sl_to_breakeven`` after TP k is applied to the remaining legs.
State persists in ``app.db`` (``paper_orders`` / ``paper_positions``).
"""
from __future__ import annotations

import json
import math
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path

from ...core.timeutil import now_ms, parse_date_spec
from ...storage.sqlite_store import connect

_DDL = [
    """CREATE TABLE IF NOT EXISTS paper_account (id INTEGER PRIMARY KEY CHECK (id = 1), start_equity REAL NOT NULL,
        realized_usd REAL NOT NULL DEFAULT 0, created_ms INTEGER NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS paper_legs (
        id TEXT PRIMARY KEY, decision_id TEXT NOT NULL, pair TEXT NOT NULL, instrument TEXT NOT NULL, side TEXT NOT NULL,
        order_type TEXT NOT NULL, order_price REAL, volume REAL NOT NULL, contract_size REAL NOT NULL, sl REAL NOT NULL,
        tp REAL, tp_index INTEGER, status TEXT NOT NULL, created_ms INTEGER NOT NULL, expires_ms INTEGER,
        fill_price REAL, fill_ms INTEGER, close_price REAL, close_ms INTEGER, close_reason TEXT, pnl_usd REAL,
        management TEXT)""",
    "CREATE INDEX IF NOT EXISTS paper_legs_status ON paper_legs(status, instrument)",
]
PENDING, OPEN, CLOSED, CANCELLED, EXPIRED = "pending", "open", "closed", "cancelled", "expired"


@dataclass
class Tick:
    time_msc: int
    bid: float
    ask: float


def split_volume(total: float, fractions: list[float], step: float, vmin: float) -> list[float] | None:
    """Split ``total`` lots by fractions on the volume step; None if any leg would fall below the minimum."""
    steps = round(total / step)
    raw = [steps * f / sum(fractions) for f in fractions]
    legs = [math.floor(x) for x in raw]
    for i in sorted(range(len(raw)), key=lambda i: raw[i] - legs[i], reverse=True)[: steps - sum(legs)]:
        legs[i] += 1
    vols = [round(n * step, 8) for n in legs]
    return vols if all(v >= vmin - 1e-12 for v in vols) else None


class PaperBackend:
    name = "paper"

    def __init__(self, app_db: Path, start_equity: float) -> None:
        self._con = connect(app_db, cache_mb=2)
        self._lock = threading.Lock()
        with self._lock:
            for s in _DDL:
                self._con.execute(s)
            if self._con.execute("SELECT count(*) FROM paper_account").fetchone()[0] == 0:
                self._con.execute("INSERT INTO paper_account VALUES (1, ?, 0, ?)", (start_equity, now_ms()))

    # ------------------------------------------------------------------ account
    def account(self, marks: dict[str, Tick] | None = None) -> dict:
        with self._lock:
            start, realized = self._con.execute("SELECT start_equity, realized_usd FROM paper_account").fetchone()
            open_legs = self._con.execute("SELECT instrument, side, volume, contract_size, fill_price, sl, pair "
                                          "FROM paper_legs WHERE status='open'").fetchall()
        unreal, risk_by_pair = 0.0, {}
        for inst, side, vol, cs, fill, sl, pair in open_legs:
            m = (marks or {}).get(inst)
            if m is not None:
                px = m.bid if side == "BUY" else m.ask
                unreal += (px - fill if side == "BUY" else fill - px) * vol * cs
            risk_by_pair[pair] = risk_by_pair.get(pair, 0.0) + max(0.0, (fill - sl if side == "BUY" else sl - fill)) * vol * cs
        balance = start + realized
        equity = balance + unreal
        return {"mode": "paper", "currency": "USD", "balance": round(balance, 2), "equity": round(equity, 2),
                "unrealized_usd": round(unreal, 2), "open_legs": len(open_legs),
                "open_positions": len({(p) for *_, p in open_legs}),
                "open_risk_pct_by_pair": {p: round(v / equity * 100, 3) for p, v in risk_by_pair.items()} if equity > 0 else {},
                "realized_today_usd": self.realized_since(now_ms() // 86_400_000 * 86_400_000)}

    def realized_since(self, since_ms: int) -> float:
        with self._lock:
            r = self._con.execute("SELECT COALESCE(sum(pnl_usd),0) FROM paper_legs WHERE status='closed' AND close_ms>=?",
                                  (since_ms,)).fetchone()
        return float(r[0])

    def closed_trades_since(self, since_ms: int) -> tuple[float, int]:
        """(realised PnL, number of closed decisions) — feeds the Cost Governor (D-004)."""
        with self._lock:
            r = self._con.execute("SELECT COALESCE(sum(pnl_usd),0), count(DISTINCT decision_id) FROM paper_legs "
                                  "WHERE status='closed' AND close_ms>=?", (since_ms,)).fetchone()
        return float(r[0]), int(r[1])

    # ------------------------------------------------------------------ orders
    def place(self, *, decision_id: str, pair: str, instrument: str, rec: dict, lots: float, entry: float,
              contract_size: float, volume_step: float, volume_min: float, quote: Tick) -> dict:
        """Create legs for an approved (translated) recommendation. Returns a detail dict."""
        with self._lock:
            if self._con.execute("SELECT 1 FROM paper_legs WHERE decision_id=? LIMIT 1", (decision_id,)).fetchone():
                return {"ok": False, "reason": "duplicate: decision already has paper legs (idempotency)"}
        side = rec["decision"]
        tps = rec["take_profits"]
        vols = split_volume(lots, [tp["close_fraction"] for tp in tps], volume_step, volume_min) if len(tps) > 1 else [lots]
        note = None
        if vols is None:
            k = max(range(len(tps)), key=lambda i: (tps[i]["close_fraction"], -i))
            tps, vols = [tps[k]], [lots]
            note = f"volume {lots} too small to split across {len(rec['take_profits'])} targets — single leg at TP{k + 1}"
        expires = parse_date_spec(rec["valid_until"]) if rec["order_type"] != "MARKET" else None
        mgmt = json.dumps(rec.get("management", []))
        created = quote.time_msc
        rows = []
        for i, (tp, v) in enumerate(zip(tps, vols)):
            leg_id = f"{decision_id}:{i + 1}"
            if rec["order_type"] == "MARKET":
                fill = quote.ask if side == "BUY" else quote.bid
                rows.append((leg_id, decision_id, pair, instrument, side, "MARKET", None, v, contract_size,
                             rec["stop_loss"], tp["price"], i + 1, OPEN, created, None, fill, created, None, None,
                             None, None, mgmt))
            else:
                rows.append((leg_id, decision_id, pair, instrument, side, rec["order_type"], entry, v, contract_size,
                             rec["stop_loss"], tp["price"], i + 1, PENDING, created, expires, None, None, None, None,
                             None, None, mgmt))
        with self._lock:
            self._con.execute("BEGIN")
            self._con.executemany("INSERT INTO paper_legs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
            self._con.execute("COMMIT")
        return {"ok": True, "legs": [r[0] for r in rows], "volumes": vols, "note": note,
                "fill_price": rows[0][15] if rec["order_type"] == "MARKET" else None}

    def cancel_decision(self, decision_id: str, reason: str = "cancelled") -> int:
        with self._lock:
            cur = self._con.execute("UPDATE paper_legs SET status=?, close_reason=? WHERE decision_id=? AND status=?",
                                    (CANCELLED, reason, decision_id, PENDING))
            return cur.rowcount

    # ------------------------------------------------------------------ simulation
    def process(self, instrument: str, ticks: list[Tick]) -> list[dict]:
        """Advance every pending/open leg of ``instrument`` through ``ticks`` (chronological). Returns events."""
        with self._lock:
            legs = [dict(zip([c[0] for c in self._con.execute("SELECT * FROM paper_legs LIMIT 0").description], r))
                    for r in self._con.execute("SELECT * FROM paper_legs WHERE instrument=? AND status IN (?,?)",
                                               (instrument, PENDING, OPEN)).fetchall()]
        if not legs or not ticks:
            return []
        events = []
        for t in ticks:
            for leg in legs:
                if leg["status"] == PENDING:
                    if leg["expires_ms"] is not None and t.time_msc >= leg["expires_ms"]:
                        leg.update(status=EXPIRED, close_ms=t.time_msc, close_reason="expired before fill")
                        events.append({"leg": leg["id"], "event": "expired"})
                        continue
                    if leg["created_ms"] is not None and t.time_msc <= leg["created_ms"]:
                        continue
                    if _pending_triggered(leg, t):
                        fill = t.ask if leg["side"] == "BUY" else t.bid
                        if leg["order_type"] in ("BUY_LIMIT", "SELL_LIMIT"):
                            fill = min(fill, leg["order_price"]) if leg["side"] == "BUY" else max(fill, leg["order_price"])
                        leg.update(status=OPEN, fill_price=fill, fill_ms=t.time_msc)
                        events.append({"leg": leg["id"], "event": "filled", "price": fill})
                elif leg["status"] == OPEN and (leg["fill_ms"] or 0) < t.time_msc:
                    hit = _exit_hit(leg, t)
                    if hit:
                        reason, px = hit
                        pnl = ((px - leg["fill_price"]) if leg["side"] == "BUY" else (leg["fill_price"] - px)) \
                            * leg["volume"] * leg["contract_size"]
                        leg.update(status=CLOSED, close_price=px, close_ms=t.time_msc, close_reason=reason,
                                   pnl_usd=round(pnl, 4))
                        events.append({"leg": leg["id"], "event": reason, "price": px, "pnl_usd": leg["pnl_usd"]})
                        if reason == "tp":
                            _apply_management(legs, leg)
        with self._lock:
            self._con.execute("BEGIN")
            for leg in legs:
                self._con.execute(
                    "UPDATE paper_legs SET status=?, sl=?, fill_price=?, fill_ms=?, close_price=?, close_ms=?, "
                    "close_reason=?, pnl_usd=? WHERE id=?",
                    (leg["status"], leg["sl"], leg["fill_price"], leg["fill_ms"], leg["close_price"], leg["close_ms"],
                     leg["close_reason"], leg["pnl_usd"], leg["id"]))
            realized = sum(e.get("pnl_usd", 0.0) for e in events if e["event"] in ("tp", "sl"))
            if realized:
                self._con.execute("UPDATE paper_account SET realized_usd = realized_usd + ?", (realized,))
            self._con.execute("COMMIT")
        return events

    def decision_legs(self, decision_id: str) -> list[dict]:
        with self._lock:
            cur = self._con.execute("SELECT * FROM paper_legs WHERE decision_id=? ORDER BY id", (decision_id,))
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]

    def decisions_with_legs(self, statuses: tuple[str, ...]) -> list[str]:
        with self._lock:
            q = f"SELECT DISTINCT decision_id FROM paper_legs WHERE status IN ({','.join('?' * len(statuses))})"
            return [r[0] for r in self._con.execute(q, statuses).fetchall()]

    def close(self) -> None:
        with self._lock:
            self._con.close()


def _pending_triggered(leg: dict, t: Tick) -> bool:
    p = leg["order_price"]
    return {"BUY_LIMIT": t.ask <= p, "BUY_STOP": t.ask >= p, "SELL_LIMIT": t.bid >= p, "SELL_STOP": t.bid <= p}[leg["order_type"]]


def _exit_hit(leg: dict, t: Tick) -> tuple[str, float] | None:
    if leg["side"] == "BUY":
        if t.bid <= leg["sl"]:
            return "sl", t.bid
        if leg["tp"] is not None and t.bid >= leg["tp"]:
            return "tp", leg["tp"]            # a TP is a limit order: it fills at its price
    else:
        if t.ask >= leg["sl"]:
            return "sl", t.ask
        if leg["tp"] is not None and t.ask <= leg["tp"]:
            return "tp", leg["tp"]
    return None


def _apply_management(legs: list[dict], closed_leg: dict) -> None:
    rules = json.loads(closed_leg.get("management") or "[]")
    for r in rules:
        if r.get("action") == "move_sl_to_breakeven" and r.get("trigger") == "tp_hit" \
                and int(r.get("value") or 1) == closed_leg["tp_index"]:
            for other in legs:
                if other["decision_id"] == closed_leg["decision_id"] and other["status"] == OPEN:
                    other["sl"] = other["fill_price"]


__all__ = ["PaperBackend", "Tick", "split_volume", "sqlite3"]
