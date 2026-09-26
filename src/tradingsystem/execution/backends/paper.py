"""Paper (dry-run) backend (P9.3, spec §9): simulated orders filled tick-by-tick on the *real* bid/ask stream of
the execution instrument. Nothing is sent to any broker.

Fill rules: BUY fills at the ask, SELL at the bid; BUY_LIMIT when ask ≤ price, BUY_STOP when ask ≥ price
(fills at the ask → real slippage), SELL_LIMIT when bid ≥ price, SELL_STOP when bid ≤ price. A BUY position's
SL/TP trigger on the bid (exit at the bid, so gaps slip through the stop), a SELL's on the ask. Multiple TPs
are separate legs sized by close fraction; ``move_sl_to_breakeven`` after TP k is applied to the remaining legs.
State persists in ``app.db`` (``paper_account`` / ``paper_legs``). Each leg keeps a persisted watermark ``eval_key``
(key of the last tick it was evaluated on, written in the same transaction as its state), so a restart replays
exactly the ticks it missed and no tick is ever applied twice (idempotent replay, e.g. after a breakeven move).
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
from ..exposure import aggregate, leg

_DDL = [
    """CREATE TABLE IF NOT EXISTS paper_account (id INTEGER PRIMARY KEY CHECK (id = 1), start_equity REAL NOT NULL,
        realized_usd REAL NOT NULL DEFAULT 0, created_ms INTEGER NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS paper_legs (
        id TEXT PRIMARY KEY, decision_id TEXT NOT NULL, pair TEXT NOT NULL, instrument TEXT NOT NULL, side TEXT NOT NULL,
        order_type TEXT NOT NULL, order_price REAL, volume REAL NOT NULL, contract_size REAL NOT NULL, sl REAL NOT NULL,
        tp REAL, tp_index INTEGER, status TEXT NOT NULL, created_ms INTEGER NOT NULL, expires_ms INTEGER,
        fill_price REAL, fill_ms INTEGER, close_price REAL, close_ms INTEGER, close_reason TEXT, pnl_usd REAL,
        management TEXT, eval_key INTEGER)""",
    "CREATE INDEX IF NOT EXISTS paper_legs_status ON paper_legs(status, instrument)",
]
_COLS = ("id", "decision_id", "pair", "instrument", "side", "order_type", "order_price", "volume", "contract_size", "sl",
         "tp", "tp_index", "status", "created_ms", "expires_ms", "fill_price", "fill_ms", "close_price", "close_ms",
         "close_reason", "pnl_usd", "management", "eval_key")
# legacy legs (before eval_key): nothing at/before creation, fill or the last sibling close (breakeven moment) is replayed
_LEGACY_WATERMARK = """UPDATE paper_legs SET eval_key = max(created_ms, COALESCE(fill_ms, 0), COALESCE((SELECT max(o.close_ms)
    FROM paper_legs o WHERE o.decision_id = paper_legs.decision_id AND o.status = 'closed'), 0)) * 1000 + 999
    WHERE eval_key IS NULL"""
PENDING, OPEN, CLOSED, CANCELLED, EXPIRED = "pending", "open", "closed", "cancelled", "expired"


@dataclass
class Tick:
    time_msc: int
    bid: float
    ask: float
    key: int | None = None           # stored tick key (utc_ms*1000+seq); None → time_msc*1000+999

    @property
    def order_key(self) -> int:
        return self.key if self.key is not None else self.time_msc * 1000 + 999


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
            if "eval_key" not in {r[1] for r in self._con.execute("PRAGMA table_info(paper_legs)")}:
                try:
                    self._con.execute("ALTER TABLE paper_legs ADD COLUMN eval_key INTEGER")
                except sqlite3.OperationalError as exc:       # another process migrated first
                    if "duplicate column" not in str(exc):
                        raise
            self._con.execute(_LEGACY_WATERMARK)
            if self._con.execute("SELECT count(*) FROM paper_account").fetchone()[0] == 0:
                self._con.execute("INSERT INTO paper_account VALUES (1, ?, 0, ?)", (start_equity, now_ms()))
            self.start_equity = float(self._con.execute("SELECT start_equity FROM paper_account").fetchone()[0])

    # ------------------------------------------------------------------ account
    def account(self, marks: dict[str, Tick] | None = None) -> dict:
        """Account snapshot (same keys as ``MT5Backend.account``). ``open_positions`` counts *decisions* holding an open
        leg or a live pending order (a pending order takes a slot and its SL risk); unrealised PnL needs ``marks``."""
        now = now_ms()
        with self._lock:
            start, realized = self._con.execute("SELECT start_equity, realized_usd FROM paper_account").fetchone()
            open_legs = self._con.execute("SELECT instrument, side, volume, contract_size, fill_price, sl, pair, decision_id "
                                          "FROM paper_legs WHERE status='open'").fetchall()
            pending = self._con.execute(
                "SELECT side, volume, contract_size, order_price, sl, pair, decision_id FROM paper_legs "
                "WHERE status='pending' AND (expires_ms IS NULL OR expires_ms > ?)", (now,)).fetchall()
        unreal, risk_by_pair = 0.0, {}
        for inst, side, vol, cs, fill, sl, pair, _ in open_legs:
            m = (marks or {}).get(inst)
            if m is not None:
                px = m.bid if side == "BUY" else m.ask
                unreal += (px - fill if side == "BUY" else fill - px) * vol * cs
            risk_by_pair[pair] = risk_by_pair.get(pair, 0.0) + max(0.0, (fill - sl if side == "BUY" else sl - fill)) * vol * cs
        for side, vol, cs, price, sl, pair, _ in pending:          # order price, never fill_price (NULL until filled)
            risk = max(0.0, (price - sl if side == "BUY" else sl - price)) * vol * cs
            risk_by_pair[pair] = risk_by_pair.get(pair, 0.0) + risk
        with_open, with_pending = {r[-1] for r in open_legs}, {r[-1] for r in pending}
        balance = start + realized
        equity = balance + unreal
        return {"mode": "paper", "currency": "USD", "balance": round(balance, 2), "equity": round(equity, 2),
                "unrealized_usd": round(unreal, 2), "open_legs": len(open_legs), "start_equity": start,
                "open_positions": len(with_open | with_pending), "open_orders": len(with_pending - with_open),
                "open_risk_pct_by_pair": {p: round(v / equity * 100, 3) for p, v in risk_by_pair.items()} if equity > 0 else {},
                "realized_today_usd": self.realized_since(now // 86_400_000 * 86_400_000),
                "exposure": self.exposure(marks, now)}

    def exposure(self, marks: dict[str, Tick] | None = None, now: int | None = None) -> list[dict]:
        """Open positions and live pending orders, one row per decision and kind (see :mod:`..exposure`)."""
        now = now_ms() if now is None else now
        with self._lock:
            rows = self._con.execute(
                "SELECT decision_id, pair, instrument, side, order_type, status, volume, contract_size, order_price, "
                "fill_price, sl, tp, created_ms, fill_ms, expires_ms FROM paper_legs WHERE status='open' OR "
                "(status='pending' AND (expires_ms IS NULL OR expires_ms > ?))", (now,)).fetchall()
        legs = []
        for did, pair, inst, side, otype, status, vol, cs, oprice, fill, sl, tp, created, fill_ms, exp in rows:
            is_open = status == OPEN
            profit = None
            if is_open and (m := (marks or {}).get(inst)) is not None:
                px = m.bid if side == "BUY" else m.ask
                profit = (px - fill if side == "BUY" else fill - px) * vol * cs
            legs.append(leg(pair=pair, decision=did, kind="position" if is_open else "order", side=side,
                            order_type=otype, volume=vol, price=fill if is_open else oprice, sl=sl, tp=tp,
                            profit_usd=profit, since_ms=fill_ms if is_open else created,
                            expires_ms=None if is_open else exp))
        return aggregate(legs)

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
        mark = quote.order_key                     # ticks at/before the placement quote are never evaluated
        rows = []
        for i, (tp, v) in enumerate(zip(tps, vols)):
            leg_id = f"{decision_id}:{i + 1}"
            if rec["order_type"] == "MARKET":
                fill = quote.ask if side == "BUY" else quote.bid
                rows.append((leg_id, decision_id, pair, instrument, side, "MARKET", None, v, contract_size,
                             rec["stop_loss"], tp["price"], i + 1, OPEN, created, None, fill, created, None, None,
                             None, None, mgmt, mark))
            else:
                rows.append((leg_id, decision_id, pair, instrument, side, rec["order_type"], entry, v, contract_size,
                             rec["stop_loss"], tp["price"], i + 1, PENDING, created, expires, None, None, None, None,
                             None, None, mgmt, mark))
        with self._lock:
            self._con.execute("BEGIN")
            self._con.executemany(f"INSERT INTO paper_legs ({', '.join(_COLS)}) VALUES ({','.join('?' * len(_COLS))})", rows)
            self._con.execute("COMMIT")
        return {"ok": True, "legs": [r[0] for r in rows], "volumes": vols, "note": note,
                "fill_price": rows[0][15] if rec["order_type"] == "MARKET" else None}

    def cancel_decision(self, decision_id: str, reason: str = "cancelled") -> int:
        with self._lock:
            cur = self._con.execute("UPDATE paper_legs SET status=?, close_reason=? WHERE decision_id=? AND status=?",
                                    (CANCELLED, reason, decision_id, PENDING))
            return cur.rowcount

    # ------------------------------------------------------------------ simulation
    def watermarks(self) -> dict[str, int]:
        """Instrument → lowest ``eval_key`` over its pending/open legs: where its tick replay must resume."""
        with self._lock:
            return {i: int(k) for i, k in self._con.execute(
                "SELECT instrument, min(eval_key) FROM paper_legs WHERE status IN (?,?) GROUP BY instrument",
                (PENDING, OPEN)).fetchall() if k is not None}

    def process(self, instrument: str, ticks: list[Tick]) -> list[dict]:
        """Advance every pending/open leg of ``instrument`` through ``ticks`` (chronological). Returns events.

        A leg skips ticks at or below its ``eval_key``, so overlapping or replayed batches are idempotent."""
        with self._lock:
            legs = [dict(zip([c[0] for c in self._con.execute("SELECT * FROM paper_legs LIMIT 0").description], r))
                    for r in self._con.execute("SELECT * FROM paper_legs WHERE instrument=? AND status IN (?,?)",
                                               (instrument, PENDING, OPEN)).fetchall()]
        if not legs or not ticks:
            return []
        for leg in legs:
            if leg["eval_key"] is None:
                leg["eval_key"] = max(leg["created_ms"], leg["fill_ms"] or 0) * 1000 + 999
        events = []
        for t in ticks:
            tk = t.order_key
            for leg in legs:
                if tk <= leg["eval_key"] or leg["status"] not in (PENDING, OPEN):
                    continue
                leg["eval_key"] = tk               # a finished leg keeps the key of the tick that ended it
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
                    "close_reason=?, pnl_usd=?, eval_key=? WHERE id=?",
                    (leg["status"], leg["sl"], leg["fill_price"], leg["fill_ms"], leg["close_price"], leg["close_ms"],
                     leg["close_reason"], leg["pnl_usd"], leg["eval_key"], leg["id"]))
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

    def unsettled_decisions(self) -> list[str]:
        """Decisions whose legs are all finished but whose outcome is not written yet (one query, same app.db)."""
        with self._lock:
            try:
                return [r[0] for r in self._con.execute(
                    "SELECT l.decision_id FROM paper_legs l JOIN ai_decisions d ON d.id = l.decision_id "
                    "WHERE d.outcome IS NULL GROUP BY l.decision_id HAVING sum(l.status IN (?,?)) = 0",
                    (PENDING, OPEN)).fetchall()]
            except sqlite3.OperationalError as exc:
                if "no such table" in str(exc):
                    return []
                raise

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
