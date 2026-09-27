"""MetaTrader 5 execution backend (P9.4) — demo or live, same code; the account type is asserted first.

For every leg: build request (FOK filling, SL + TP attached, expiration in *server* time for pending orders)
→ ``order_check`` (margin/stops validation) → idempotency lookup by comment tag → ``order_send`` with bounded
retries for retryable codes (a timeout is only retried after verifying nothing was created) → post-fill
slippage recorded. Every attempt is returned with its retcode meaning — no silent failures (spec §7.3).
``dry_run=True`` stops after ``order_check`` (used until the user approves demo orders, H6).

Live-trade management (Phase 3, called only by ``PositionManager`` and the model's position actions): ``modify_sl``
only ever tightens a stop and defers one the venue would refuse right now (closer than stops level + spread, or
inside the freeze level); ``modify_tp`` keeps the take-profit on the profit side; ``close_position`` closes all or
part of a position; ``cancel_order`` removes a pending order; ``legs_of`` lists a decision's positions, pending
orders and closed legs (how each ended, from the deal history).
"""
from __future__ import annotations

import logging
import math
import time

from ...core.timeutil import now_ms, parse_date_spec
from ...ingest.mt5.servertime import ServerTimeModel
from ...ingest.mt5.terminal import MT5Terminal
from ..exposure import aggregate, leg
from ..management import Leg
from ..retcodes import SUCCESS, describe, retryable
from ..sizing import single_leg_index
from .paper import Tick, split_volume

log = logging.getLogger(__name__)
NO_CHANGES = 10025              # TRADE_RETCODE_NO_CHANGES: the request asks for what is already there → ok
INVALID_FILL = 10030            # unsupported filling mode → try the next one
TIMEOUT = 10012
RETRY_SLEEP_S = 0.5             # between retries of a retryable code (× attempt number)
HISTORY_DAYS = 45               # legs_of: how far back the history is searched for a decision's closed legs


def tag(decision_id: str, leg: int) -> str:
    return f"ts:{decision_id[:20]}:{leg}"          # ≤ 31 chars (MT5 comment limit)


def _tp_index(comment: str) -> int:
    """1-based TP index from the tag ``ts:<id20>:<k>`` (1 when unreadable)."""
    try:
        return max(1, int((comment or "").split(":")[2]))
    except (IndexError, ValueError):
        return 1


def _res(ok: bool, status: str, code: int | None, meaning: str, **kw) -> dict:
    return {"ok": ok, "status": status, "retcode": code, "meaning": meaning, **kw}


class MT5Backend:
    name = "mt5"

    def __init__(self, terminal: MT5Terminal, magic: int, *, expected_account_type: str,
                 pair_by_symbol: dict[str, str] | None = None, own_pairs: set[str] | None = None,
                 adopt_magic: int | None = None, family: set[int] | None = None) -> None:
        """``magic``: this system's orders. One system per pair (D-042): ``own_pairs`` are the pairs it trades,
        ``adopt_magic`` the all-pairs system's magic (its orders on ``own_pairs`` — placed before the switch to one
        system per pair — are managed and settled here), ``family`` every magic of this project on the account
        (the other systems' positions count towards the correlated-exposure cap)."""
        self.t = terminal
        self.mt5 = terminal.mt5
        self.magic = magic
        self.expected = expected_account_type
        self.pair_by_symbol = dict(pair_by_symbol or {})     # MT5 symbol → logical pair (risk is keyed by pair)
        self.own_pairs = set(own_pairs) if own_pairs is not None else set(self.pair_by_symbol.values())
        self.adopt_magic = adopt_magic if adopt_magic != magic else None
        self.family = set(family or ()) | {magic} | ({adopt_magic} if adopt_magic is not None else set())
        self._warned: set[str] = set()
        self.model = ServerTimeModel()

    def mine(self, x) -> bool:
        """An order / position / deal of this system (its magic, or an adopted all-pairs one on its pairs)."""
        return x.magic == self.magic or (self.adopt_magic is not None and x.magic == self.adopt_magic
                                         and self.pair_by_symbol.get(x.symbol) in self.own_pairs)

    def sibling(self, x) -> bool:
        """An order / position of another system of this project on the same account."""
        return x.magic in self.family and not self.mine(x)

    # ------------------------------------------------------------------ state
    def assert_account(self) -> None:
        acc = self.t.account()
        want = {"demo": 0, "real": 2}[self.expected]
        if acc.trade_mode != want or acc.server != self.t.profile.server:
            raise RuntimeError(f"account mismatch: {acc.server} trade_mode={acc.trade_mode}, expected "
                               f"{self.t.profile.server} ({self.expected}) — refusing to trade")

    def _pair(self, symbol: str) -> str:
        if symbol not in self.pair_by_symbol and symbol not in self._warned:
            self._warned.add(symbol)
            log.warning("MT5 symbol %s (magic %s) maps to no configured pair — its risk is kept under the raw symbol",
                        symbol, self.magic)
        return self.pair_by_symbol.get(symbol, symbol)

    def account(self, marks=None) -> dict:
        """Same contract as ``PaperBackend.account`` (the executor's risk gate reads these keys): our positions and
        pending orders (by magic) → open decisions, SL risk % of equity by *pair*, floating PnL, realised today."""
        # fail closed: a terminal that cannot list positions / orders / today's deals raises (the gate then never
        # sees "no positions, no risk, no loss today" — the daily limit and exposure caps depend on these, D-036)
        a = self.t.account()
        m = self.mt5
        all_pos = self._ask("positions", m.positions_get())
        all_ord = self._ask("orders", m.orders_get())
        positions = [p for p in all_pos if self.mine(p)]
        orders = [o for o in all_ord if self.mine(o)]
        buy_orders = {m.ORDER_TYPE_BUY, m.ORDER_TYPE_BUY_LIMIT, m.ORDER_TYPE_BUY_STOP}
        risk_usd: dict[str, float] = {}
        sibling_usd: dict[str, float] = {}

        def add_risk(into: dict[str, float], symbol: str, buy: bool, volume: float, price: float, sl: float) -> None:
            pair = self._pair(symbol)
            if not sl:           # never ours (every leg carries an SL) — unbounded risk: counts as the whole equity
                into[pair] = into.get(pair, 0.0) + max(a.equity, 0.0)
                return
            loss = m.order_calc_profit(m.ORDER_TYPE_BUY if buy else m.ORDER_TYPE_SELL, symbol, volume, price, sl)
            if loss is None:
                raise RuntimeError(f"MT5 order_calc_profit failed for {symbol} ({m.last_error()})")
            into[pair] = into.get(pair, 0.0) + max(0.0, -loss)

        for p in all_pos:
            if self.mine(p) or self.sibling(p):
                add_risk(risk_usd if self.mine(p) else sibling_usd, p.symbol, p.type == m.POSITION_TYPE_BUY,
                         p.volume, p.price_open, p.sl)
        for o in all_ord:
            if self.mine(o) or self.sibling(o):
                add_risk(risk_usd if self.mine(o) else sibling_usd, o.symbol, o.type in buy_orders,
                         o.volume_current, o.price_open, o.sl)
        today = now_ms() // 86_400_000 * 86_400_000
        deals = self._ask("today's deals",
                          m.history_deals_get(self.model.utc_to_server(today) // 1000, int(time.time()) + 86_400))
        realized = sum(d.profit + d.commission + d.swap + getattr(d, "fee", 0.0) for d in deals if self.mine(d))
        with_pos = {p.comment.rsplit(":", 1)[0] for p in positions}
        with_ord = {o.comment.rsplit(":", 1)[0] for o in orders}
        pct = (lambda d: {k: round(v / a.equity * 100, 3) for k, v in d.items()} if a.equity > 0 else {})
        return {"mode": self.expected, "currency": a.currency, "balance": a.balance, "equity": a.equity,
                "margin_free": a.margin_free, "login": getattr(a, "login", None), "server": getattr(a, "server", None),
                "unrealized_usd": round(sum(p.profit + p.swap for p in positions), 2),
                "open_positions": len(with_pos | with_ord), "open_orders": len(with_ord - with_pos),
                "open_risk_usd_by_pair": {k: round(v, 2) for k, v in risk_usd.items()},
                "open_risk_pct_by_pair": pct(risk_usd), "sibling_risk_pct_by_pair": pct(sibling_usd),
                "realized_today_usd": realized, "exposure": self._exposure(positions, orders, buy_orders)}

    def _exposure(self, positions: list, orders: list, buy_orders: set) -> list[dict]:
        m = self.mt5
        names = {getattr(m, f"ORDER_TYPE_{n}", None): n for n in
                 ("BUY", "SELL", "BUY_LIMIT", "SELL_LIMIT", "BUY_STOP", "SELL_STOP", "BUY_STOP_LIMIT", "SELL_STOP_LIMIT")}

        def utc(server_ms: int) -> int | None:
            return self.model.server_to_utc(int(server_ms), prefer="earlier") if server_ms else None

        def decision(comment: str) -> str:
            parts = (comment or "").split(":")
            return parts[1] if len(parts) >= 3 and parts[0] == "ts" else (comment or "?")

        legs = [leg(pair=self._pair(p.symbol), decision=decision(p.comment), kind="position",
                    side="BUY" if p.type == m.POSITION_TYPE_BUY else "SELL", order_type="MARKET", volume=p.volume,
                    price=p.price_open, sl=p.sl or None, tp=getattr(p, "tp", 0) or None, profit_usd=p.profit + p.swap,
                    since_ms=utc(getattr(p, "time_msc", 0))) for p in positions]
        legs += [leg(pair=self._pair(o.symbol), decision=decision(o.comment), kind="order",
                     side="BUY" if o.type in buy_orders else "SELL", order_type=names.get(o.type, str(o.type)),
                     volume=o.volume_current, price=o.price_open, sl=o.sl or None, tp=getattr(o, "tp", 0) or None,
                     since_ms=utc(getattr(o, "time_setup_msc", 0)),
                     expires_ms=utc(int(getattr(o, "time_expiration", 0) or 0) * 1000)) for o in orders]
        return aggregate(legs)

    def specs(self, symbol: str) -> dict:
        i = self.mt5.symbol_info(symbol)
        return {"stops_level_price": i.trade_stops_level * i.point, "volume_min": i.volume_min,
                "volume_step": i.volume_step, "volume_max": i.volume_max, "contract_size": i.trade_contract_size,
                "digits": i.digits, "point": i.point, "filling_mode": i.filling_mode}

    def quote(self, symbol: str) -> Tick:
        q = self.mt5.symbol_info_tick(symbol)
        return Tick(self.model.server_to_utc(int(q.time_msc), prefer="earlier"), q.bid, q.ask)

    def usd_per_unit_per_lot(self, symbol: str, buy: bool, price: float) -> float:
        otype = self.mt5.ORDER_TYPE_BUY if buy else self.mt5.ORDER_TYPE_SELL
        p = self.mt5.order_calc_profit(otype, symbol, 1.0, price, price + (1.0 if buy else -1.0))
        return abs(p) if p else 0.0

    def existing(self, decision_id: str) -> dict:
        """Orders / positions / deals carrying this decision's tag. The terminal answers None when it cannot be
        asked (link down) — that is *unknown*, never "nothing placed": raise so the caller retries later."""
        prefix = f"ts:{decision_id[:20]}:"
        got = {"orders": self.mt5.orders_get(), "positions": self.mt5.positions_get(),
               "deals": self.mt5.history_deals_get(int(time.time()) - 7 * 86_400, int(time.time()) + 86_400)}
        missing = [k for k, v in got.items() if v is None]
        if missing:
            err = self.mt5.last_error()
            if not (isinstance(err, tuple) and err and err[0] == 1):      # (1, 'Success'): an empty answer
                raise RuntimeError(f"MT5 could not list {', '.join(missing)} ({err}) — state unknown")
        return {k: [x for x in (v or ()) if x.comment.startswith(prefix)] for k, v in got.items()}

    # ------------------------------------------------------------------ placing
    def _filling(self, spec: dict) -> int:
        fm = spec["filling_mode"]
        if fm & 1:
            return self.mt5.ORDER_FILLING_FOK
        if fm & 2:
            return self.mt5.ORDER_FILLING_IOC
        return self.mt5.ORDER_FILLING_RETURN

    def build_requests(self, decision_id: str, symbol: str, rec: dict, lots: float, entry: float) -> tuple[list[dict], str | None]:
        spec = self.specs(symbol)
        buy = rec["decision"] == "BUY"
        tps = rec["take_profits"]
        vols = split_volume(lots, [t["close_fraction"] for t in tps], spec["volume_step"], spec["volume_min"]) \
            if len(tps) > 1 else [lots]
        note = None
        idx = list(range(1, len(tps) + 1))           # the tag carries the index of the leg's target in the decision
        if vols is None:
            k = single_leg_index([t["close_fraction"] for t in tps])
            tps, vols, idx = [tps[k]], [lots], [k + 1]
            note = f"volume too small to split — single position at TP{k + 1}"
        m = self.mt5
        otype = {("BUY", "MARKET"): m.ORDER_TYPE_BUY, ("SELL", "MARKET"): m.ORDER_TYPE_SELL,
                 ("BUY", "BUY_LIMIT"): m.ORDER_TYPE_BUY_LIMIT, ("SELL", "SELL_LIMIT"): m.ORDER_TYPE_SELL_LIMIT,
                 ("BUY", "BUY_STOP"): m.ORDER_TYPE_BUY_STOP, ("SELL", "SELL_STOP"): m.ORDER_TYPE_SELL_STOP}[
            (rec["decision"], rec["order_type"])]
        reqs = []
        q = self.mt5.symbol_info_tick(symbol)
        for i, tp, v in zip(idx, tps, vols):
            r = {"symbol": symbol, "volume": float(v), "type": otype, "sl": round(rec["stop_loss"], spec["digits"]),
                 "tp": round(tp["price"], spec["digits"]), "magic": self.magic, "comment": tag(decision_id, i),
                 "type_filling": self._filling(spec), "deviation": 0}
            if rec["order_type"] == "MARKET":
                r.update(action=m.TRADE_ACTION_DEAL, price=q.ask if buy else q.bid, type_time=m.ORDER_TIME_GTC)
            else:
                exp_srv = self.model.utc_to_server(parse_date_spec(rec["valid_until"])) // 1000
                r.update(action=m.TRADE_ACTION_PENDING, price=round(entry, spec["digits"]),
                         type_time=m.ORDER_TIME_SPECIFIED, expiration=int(exp_srv))
            reqs.append(r)
        return reqs, note

    def place(self, *, decision_id: str, symbol: str, rec: dict, lots: float, entry: float, dry_run: bool = True,
              max_retries: int = 2) -> dict:
        self.assert_account()
        prev = self.existing(decision_id)
        if prev["orders"] or prev["positions"] or prev["deals"]:
            return {"ok": False, "reason": "duplicate: orders/positions already exist for this decision (idempotency)"}
        reqs, note = self.build_requests(decision_id, symbol, rec, lots, entry)
        attempts = []
        for r in reqs:
            chk = self.mt5.order_check(r)
            attempts.append({"comment": r["comment"], "stage": "order_check",
                             "retcode": getattr(chk, "retcode", None), "meaning": describe(getattr(chk, "retcode", None)) if chk
                             else f"order_check returned None: {self.mt5.last_error()}",
                             "margin": getattr(chk, "margin", None)})
            if chk is None or chk.retcode != 0:
                return {"ok": False, "reason": f"order_check failed for {r['comment']}: {attempts[-1]['meaning']}",
                        "attempts": attempts, "note": note}
        if dry_run:
            return {"ok": True, "dry_run": True, "requests": reqs, "attempts": attempts, "note": note}
        results = []
        for r in reqs:
            for attempt in range(max_retries + 1):
                if r["action"] == self.mt5.TRADE_ACTION_DEAL:
                    q = self.mt5.symbol_info_tick(symbol)
                    r["price"] = q.ask if r["type"] == self.mt5.ORDER_TYPE_BUY else q.bid
                res = self.mt5.order_send(r)
                code = getattr(res, "retcode", None)
                attempts.append({"comment": r["comment"], "stage": "order_send", "retcode": code,
                                 "meaning": describe(code), "price": getattr(res, "price", None),
                                 "order": getattr(res, "order", None), "deal": getattr(res, "deal", None)})
                if code in SUCCESS:
                    slip = (res.price - r["price"]) if res.price and r["action"] == self.mt5.TRADE_ACTION_DEAL else 0.0
                    results.append({"comment": r["comment"], "order": res.order, "deal": res.deal, "price": res.price,
                                    "requested": r["price"], "slippage": slip, "volume": res.volume})
                    break
                if code == 10012 or code is None:        # timeout: never resend blindly
                    time.sleep(1)
                    ex = self.existing(decision_id)
                    if any(x.comment == r["comment"] for x in ex["orders"] + ex["positions"]):
                        results.append({"comment": r["comment"], "order": None, "note": "created despite timeout"})
                        break
                if not retryable(code) or attempt == max_retries:
                    return {"ok": False, "reason": f"order_send {r['comment']}: {describe(code)}", "attempts": attempts,
                            "placed": results, "note": note}
                time.sleep(0.5 * (attempt + 1))
        return {"ok": True, "placed": results, "attempts": attempts, "note": note}

    # ------------------------------------------------------------------ management (Phase 3)
    def _send(self, req: dict, *, max_retries: int = 2, refresh=None,
              settled=None) -> tuple[bool, int | None, object, list[dict]]:
        """``order_send`` with bounded retries of retryable codes. ``refresh(req)`` updates the request before each
        attempt (a fresh price); after a timeout / no answer, ``settled()`` checks whether the request took effect
        anyway — it is never resent blindly. Every attempt is returned with its retcode meaning."""
        attempts: list[dict] = []
        code, res = None, None
        for attempt in range(max_retries + 1):
            if refresh is not None:
                refresh(req)
            res = self.mt5.order_send(req)
            code = getattr(res, "retcode", None)
            attempts.append({"attempt": attempt + 1, "retcode": code, "meaning": describe(code),
                             "price": getattr(res, "price", None)})
            if code in SUCCESS or code == NO_CHANGES:
                return True, code, res, attempts
            if code in (TIMEOUT, None) and settled is not None:
                time.sleep(RETRY_SLEEP_S)
                try:
                    done = settled()
                except Exception as exc:  # noqa: BLE001 — cannot verify → never resend blindly
                    attempts[-1]["note"] = f"could not verify after the timeout: {exc!r}"[:200]
                    attempts[-1]["unknown"] = True
                    return False, code, res, attempts
                if done:
                    attempts[-1]["note"] = "took effect despite the missing confirmation"
                    return True, code, res, attempts
                # the server may still be processing it: the outcome is unknown, never resent here — the caller
                # re-reads the position on a later loop before anything is sent again
                attempts[-1]["note"] = "no confirmation and not visible yet — outcome unknown, not resent"
                attempts[-1]["unknown"] = True
                return False, code, res, attempts
            if not retryable(code) or attempt == max_retries:
                return False, code, res, attempts
            time.sleep(RETRY_SLEEP_S * (attempt + 1))
        return False, code, res, attempts

    def _position(self, ticket: int):
        return next(iter(self._ask(f"position {ticket}", self.mt5.positions_get(ticket=ticket))), None)

    def _venue_now(self, symbol: str):
        """(symbol_info, tick) or raises: nothing is changed at the broker without its live limits and price."""
        info, q = self.mt5.symbol_info(symbol), self.mt5.symbol_info_tick(symbol)
        if info is None or q is None:
            raise RuntimeError(f"MT5 has no symbol info / quote for {symbol} ({self.mt5.last_error()})")
        return info, q

    @staticmethod
    def _frozen(pos, q, freeze: float, buy: bool) -> str | None:
        """A position whose current stop or take-profit is within the freeze level cannot be modified now."""
        if freeze <= 0:
            return None
        px = q.bid if buy else q.ask
        for name, lvl in (("stop", pos.sl), ("take-profit", pos.tp)):
            if lvl and abs(px - lvl) <= freeze:
                return f"deferred: the current {name} {lvl} is within the freeze level {freeze:.5g} of the price {px}"
        return None

    def modify_sl(self, ticket: int, symbol: str, sl: float, tp: float | None = None, *,
                  max_retries: int = 2) -> dict:
        """Tighten a position's stop (``tp`` None keeps its take-profit). Refused — ``rejected`` — when it would
        widen or remove the stop (the position's current stop is read first); ``deferred`` when the venue would
        refuse it now (closer than stops level + spread to the bid/ask it triggers on, or a freeze level applies);
        retcode 10025 (no changes) counts as done; retryable codes are retried ``max_retries`` times."""
        m = self.mt5
        pos = self._position(ticket)
        if pos is None:
            return _res(False, "failed", None, f"position {ticket} not found (closed?)")
        info, q = self._venue_now(symbol)
        sl = round(float(sl), info.digits)
        buy = pos.type == m.POSITION_TYPE_BUY
        cur = pos.sl or None
        if sl <= 0:
            return _res(False, "rejected", None, "refused: a stop can never be removed", sl=sl)
        if cur is not None and abs(sl - cur) < info.point / 2:
            return _res(True, "applied", NO_CHANGES, f"{describe(NO_CHANGES)} — the stop is already {cur}", sl=sl)
        if cur is not None and not (sl > cur if buy else sl < cur):
            return _res(False, "rejected", None, f"refused: stop {sl} would widen {cur} of a "
                                                 f"{'BUY' if buy else 'SELL'} (tighten only)", sl=sl)
        stops, freeze = info.trade_stops_level * info.point, getattr(info, "trade_freeze_level", 0) * info.point
        px = q.bid if buy else q.ask
        dist, need = (px - sl) if buy else (sl - px), stops + (q.ask - q.bid)
        if dist < need - 1e-9 or dist <= freeze:
            return _res(False, "deferred", None, f"deferred: stop {sl} is {dist:.5g} from the {'bid' if buy else 'ask'} "
                                                 f"{px} (needs ≥ stops level + spread {need:.5g}"
                                                 f"{f', > freeze level {freeze:.5g}' if freeze else ''})", sl=sl)
        if (why := self._frozen(pos, q, freeze, buy)) is not None:
            return _res(False, "deferred", None, why, sl=sl)
        keep_tp = round(float(tp), info.digits) if tp else (pos.tp or 0.0)
        ok, code, _, attempts = self._send({"action": m.TRADE_ACTION_SLTP, "position": ticket, "symbol": symbol,
                                            "sl": sl, "tp": keep_tp, "magic": self.magic}, max_retries=max_retries)
        return _res(ok, "applied" if ok else "failed", code, describe(code), sl=sl, attempts=attempts)

    def modify_tp(self, ticket: int, symbol: str, tp: float, *, max_retries: int = 2) -> dict:
        """Move a position's take-profit (either way), keeping its stop. It must stay on the profit side of the
        price it triggers on (bid for a BUY, ask for a SELL) and at least the stops level away (else ``rejected``);
        a freeze level in force → ``deferred``."""
        m = self.mt5
        pos = self._position(ticket)
        if pos is None:
            return _res(False, "failed", None, f"position {ticket} not found (closed?)")
        info, q = self._venue_now(symbol)
        tp = round(float(tp), info.digits)
        buy = pos.type == m.POSITION_TYPE_BUY
        if tp <= 0:
            return _res(False, "rejected", None, "refused: a take-profit needs a price > 0", tp=tp)
        if pos.tp and abs(tp - pos.tp) < info.point / 2:
            return _res(True, "applied", NO_CHANGES, f"{describe(NO_CHANGES)} — the take-profit is already {pos.tp}",
                        tp=tp)
        stops, freeze = info.trade_stops_level * info.point, getattr(info, "trade_freeze_level", 0) * info.point
        px = q.bid if buy else q.ask
        dist = (tp - px) if buy else (px - tp)
        if dist < stops - 1e-9 or dist <= freeze:
            return _res(False, "rejected", None, f"refused: take-profit {tp} is {dist:.5g} beyond the "
                                                 f"{'bid' if buy else 'ask'} {px} (profit side, ≥ stops level "
                                                 f"{stops:.5g})", tp=tp)
        if (why := self._frozen(pos, q, freeze, buy)) is not None:
            return _res(False, "deferred", None, why, tp=tp)
        ok, code, _, attempts = self._send({"action": m.TRADE_ACTION_SLTP, "position": ticket, "symbol": symbol,
                                            "sl": pos.sl or 0.0, "tp": tp, "magic": self.magic},
                                           max_retries=max_retries)
        return _res(ok, "applied" if ok else "failed", code, describe(code), tp=tp, attempts=attempts)

    def _fillings(self, spec: dict) -> list[int]:
        """The symbol's preferred filling mode first (as ``place``), then the others in FOK → IOC → RETURN order."""
        m, first = self.mt5, self._filling(spec)
        return [first] + [f for f in (m.ORDER_FILLING_FOK, m.ORDER_FILLING_IOC, m.ORDER_FILLING_RETURN) if f != first]

    def close_position(self, ticket: int, volume: float | None = None, *, max_retries: int = 2) -> dict:
        """Market-close ``volume`` lots (None = all) of one of our positions at the current bid/ask, keeping its
        magic and comment tag. A partial volume is rounded down to the volume step and must leave at least the
        minimum lot open. An unsupported filling mode falls through to the next; a timeout is verified against the
        position's remaining volume before any retry (a close is never sent twice)."""
        m = self.mt5
        pos = self._position(ticket)
        if pos is None:
            return _res(False, "failed", None, f"position {ticket} not found (closed?)")
        spec = self.specs(pos.symbol)
        step, vmin, before = spec["volume_step"], spec["volume_min"], float(pos.volume)
        full = volume is None or volume >= before - 1e-9
        vol = before if full else round(math.floor(float(volume) / step + 1e-7) * step, 8)
        if not full and vol < vmin - 1e-9:
            return _res(False, "rejected", None, f"refused: {vol:g} lots is below the minimum lot {vmin:g}", volume=vol)
        if not full and before - vol < vmin - 1e-9:
            return _res(False, "rejected", None, f"refused: closing {vol:g} of {before:g} lots would leave less than "
                                                 f"the minimum lot {vmin:g}", volume=vol)
        buy = pos.type == m.POSITION_TYPE_BUY

        def refresh(r: dict) -> None:
            q = m.symbol_info_tick(pos.symbol)
            if q is None:
                raise RuntimeError(f"MT5 has no quote for {pos.symbol} ({m.last_error()})")
            r["price"] = q.bid if buy else q.ask

        def settled() -> bool:
            p = self._position(ticket)
            return p is None or float(p.volume) <= before - vol + 1e-9

        req = {"action": m.TRADE_ACTION_DEAL, "position": ticket, "symbol": pos.symbol, "volume": float(vol),
               "type": m.ORDER_TYPE_SELL if buy else m.ORDER_TYPE_BUY, "price": 0.0, "magic": self.magic,
               "comment": (pos.comment or "")[:31]}
        attempts: list[dict] = []
        ok, code, res = False, None, None
        for filling in self._fillings(spec):
            req["type_filling"] = filling
            ok, code, res, att = self._send(req, max_retries=max_retries, refresh=refresh, settled=settled)
            attempts += [{**a, "filling": filling} for a in att]
            if ok or code != INVALID_FILL:
                break
        unknown = not ok and bool(attempts) and bool(attempts[-1].get("unknown"))
        return _res(ok, "applied" if ok else "unknown" if unknown else "failed", code, describe(code),
                    price=getattr(res, "price", None), volume=vol, attempts=attempts)

    def cancel_order(self, ticket: int, *, max_retries: int = 2) -> dict:
        """Remove one of our pending orders; every attempt with its retcode meaning."""
        m = self.mt5
        if not self._ask(f"order {ticket}", m.orders_get(ticket=ticket)):
            return _res(False, "failed", None, f"order {ticket} not found (filled, cancelled or expired)")

        def settled() -> bool:
            return not self._ask(f"order {ticket}", m.orders_get(ticket=ticket))

        ok, code, _, attempts = self._send({"action": m.TRADE_ACTION_REMOVE, "order": ticket, "magic": self.magic},
                                           max_retries=max_retries, settled=settled)
        unknown = not ok and bool(attempts) and bool(attempts[-1].get("unknown"))
        return _res(ok, "applied" if ok else "unknown" if unknown else "failed", code, describe(code),
                    attempts=attempts)

    # ------------------------------------------------------------------ a decision's legs
    def _utc(self, server_ms) -> int | None:
        try:
            return self.model.server_to_utc(int(server_ms), prefer="earlier") if server_ms else None
        except Exception:  # noqa: BLE001 — a timestamp we cannot map is unknown, not wrong
            return None

    def legs_of(self, decision_id: str, *, since_s: int | None = None, history: bool = True,
                positions=None, orders=None) -> list[Leg]:
        """This system's legs of one decision (comment tag ``ts:<id20>:<k>``, :meth:`mine` ownership): open
        positions, pending orders and — with ``history`` — closed legs: filled positions no longer open (how they
        ended from the last closing deal: ``DEAL_REASON_TP`` → tp, ``DEAL_REASON_SL`` → sl, anything else →
        other) and orders that never filled (expired / cancelled). A closed position whose closing deals do not yet
        cover its volume (history still syncing) is left out until they do — unknown, never "closed".
        ``positions`` / ``orders`` reuse a snapshot already read this loop. Raises when the terminal cannot be asked."""
        m, prefix = self.mt5, f"ts:{decision_id[:20]}:"
        pos = self._ask("positions", m.positions_get() if positions is None else positions)
        ords = self._ask("orders", m.orders_get() if orders is None else orders)
        buy_orders = {getattr(m, f"ORDER_TYPE_{n}", None) for n in ("BUY", "BUY_LIMIT", "BUY_STOP", "BUY_STOP_LIMIT")}
        out: list[Leg] = []
        open_ids, pending_ids = set(), set()
        for p in pos:
            if not (self.mine(p) and (p.comment or "").startswith(prefix)):
                continue
            open_ids.add(getattr(p, "identifier", 0) or p.ticket)
            out.append(Leg(key=str(p.ticket), decision_id=decision_id, pair=self._pair(p.symbol), symbol=p.symbol,
                           side="BUY" if p.type == m.POSITION_TYPE_BUY else "SELL", kind="position",
                           volume=float(p.volume), fill=float(p.price_open), sl=p.sl or None,
                           tp=getattr(p, "tp", 0) or None, tp_index=_tp_index(p.comment),
                           opened_ms=self._utc(getattr(p, "time_msc", 0))))
        for o in ords:
            if not (self.mine(o) and (o.comment or "").startswith(prefix)):
                continue
            pending_ids.add(o.ticket)
            out.append(Leg(key=str(o.ticket), decision_id=decision_id, pair=self._pair(o.symbol), symbol=o.symbol,
                           side="BUY" if o.type in buy_orders else "SELL", kind="order",
                           volume=float(o.volume_current), order_price=float(o.price_open), sl=o.sl or None,
                           tp=getattr(o, "tp", 0) or None, tp_index=_tp_index(o.comment),
                           opened_ms=self._utc(getattr(o, "time_setup_msc", 0))))
        if not history:
            return out
        now_s = int(time.time())
        hist = self._ask("history orders", m.history_orders_get(
            since_s if since_s is not None else now_s - HISTORY_DAYS * 86_400, now_s + 86_400))
        by_pos: dict[int, list] = {}
        unfilled = []
        for o in hist:
            if not (self.mine(o) and (o.comment or "").startswith(prefix)):
                continue
            pid = getattr(o, "position_id", 0) or 0
            if pid:
                by_pos.setdefault(pid, []).append(o)       # the opening order and our own closing orders
            elif o.ticket not in pending_ids:
                unfilled.append(o)
        state_reason = {getattr(m, "ORDER_STATE_EXPIRED", 6): "expired",
                        getattr(m, "ORDER_STATE_CANCELED", 2): "cancelled"}
        for o in unfilled:
            out.append(Leg(key=str(o.ticket), decision_id=decision_id, pair=self._pair(o.symbol), symbol=o.symbol,
                           side="BUY" if o.type in buy_orders else "SELL", kind="closed",
                           volume=float(getattr(o, "volume_initial", 0) or getattr(o, "volume_current", 0)),
                           order_price=o.price_open or None, sl=o.sl or None, tp=getattr(o, "tp", 0) or None,
                           tp_index=_tp_index(o.comment), opened_ms=self._utc(getattr(o, "time_setup_msc", 0)),
                           closed_reason=state_reason.get(getattr(o, "state", None), "other")))
        for pid in sorted(by_pos):
            if pid not in open_ids and (lg := self._closed_leg(decision_id, pid, by_pos[pid])) is not None:
                out.append(lg)
        return out

    def _closed_leg(self, decision_id: str, pid: int, orders: list) -> Leg | None:
        m = self.mt5
        deals = self._ask(f"deals of position {pid}", m.history_deals_get(position=pid))
        ins = [d for d in deals if d.entry == m.DEAL_ENTRY_IN]
        outs = [d for d in deals if d.entry in (m.DEAL_ENTRY_OUT, m.DEAL_ENTRY_OUT_BY)]
        v_in, v_out = sum(d.volume for d in ins), sum(d.volume for d in outs)
        if not ins or not outs or v_out + 1e-9 < v_in:
            return None
        last = max(outs, key=lambda d: (getattr(d, "time_msc", 0), getattr(d, "ticket", 0)))
        reason = {getattr(m, "DEAL_REASON_SL", 4): "sl", getattr(m, "DEAL_REASON_TP", 5): "tp"}.get(
            getattr(last, "reason", None), "other")
        opener = next((o for o in orders if o.ticket == pid), orders[0])
        symbol = getattr(ins[0], "symbol", None) or opener.symbol
        return Leg(key=str(pid), decision_id=decision_id, pair=self._pair(symbol), symbol=symbol,
                   side="BUY" if ins[0].type == m.DEAL_TYPE_BUY else "SELL", kind="closed", volume=float(v_in),
                   fill=sum(d.price * d.volume for d in ins) / v_in, order_price=opener.price_open or None,
                   sl=opener.sl or None, tp=getattr(opener, "tp", 0) or None, tp_index=_tp_index(opener.comment),
                   opened_ms=self._utc(getattr(ins[0], "time_msc", 0)), closed_reason=reason)

    # ------------------------------------------------------------------ outcomes (P9.6)
    def _ask(self, what: str, value):
        """None from the terminal is an error unless last_error says Success (an empty answer)."""
        if value is None:
            err = self.mt5.last_error()
            if not (isinstance(err, tuple) and err and err[0] == 1):
                raise RuntimeError(f"MT5 could not list {what} ({err})")
            return ()
        return value

    def decision_result(self, decision_id: str, since_s: int, legs: set[str] | None = None) -> dict | None:
        """A decision's real result from the broker's own records, or None while it cannot be final: a leg is
        still a pending order or an open position, a leg tag recorded at placement (``legs``) is not in the history
        yet, a position's closing deals do not yet cover its opening volume (history still syncing after a
        relaunch), or the terminal is not connected. Else ``filled`` (any leg entered), ``pnl_usd`` (profit +
        commission + swap + fee of every deal of its positions), ``move`` (volume-weighted price move in the trade's
        favour) and ``volume``. Raises when the terminal cannot be asked."""
        m, prefix = self.mt5, f"ts:{decision_id[:20]}:"
        if not self.t.healthy():
            return None
        until_s = int(time.time()) + 86_400
        pending = self._ask("orders", m.orders_get())
        open_pos = self._ask("positions", m.positions_get())
        hist = self._ask("history orders", m.history_orders_get(since_s, until_s))
        placed = [o for o in hist if self.mine(o) and o.comment.startswith(prefix)]
        pids = {o.position_id for o in placed if getattr(o, "position_id", 0)}
        if any(o.comment.startswith(prefix) for o in pending) or \
                any(p.identifier in pids or p.comment.startswith(prefix) for p in open_pos):
            return None
        if not placed or (legs and not legs <= {o.comment for o in placed}):
            return None
        pnl, move_vol, vol_out = 0.0, 0.0, 0.0
        for pid in pids:
            deals = self._ask(f"deals of position {pid}", m.history_deals_get(position=pid))
            ins = [d for d in deals if d.entry == m.DEAL_ENTRY_IN]
            outs = [d for d in deals if d.entry in (m.DEAL_ENTRY_OUT, m.DEAL_ENTRY_OUT_BY)]
            v_in, v_out = sum(d.volume for d in ins), sum(d.volume for d in outs)
            if not ins or v_out + 1e-9 < v_in:
                return None                      # opening deal missing or not fully closed in the history yet
            pnl += sum(d.profit + d.commission + d.swap + getattr(d, "fee", 0.0) for d in deals)
            buy = ins[0].type == m.DEAL_TYPE_BUY
            p_in = sum(d.price * d.volume for d in ins) / sum(d.volume for d in ins)
            v = sum(d.volume for d in outs)
            p_out = sum(d.price * d.volume for d in outs) / v
            move_vol += ((p_out - p_in) if buy else (p_in - p_out)) * v
            vol_out += v
        return {"filled": bool(pids), "pnl_usd": round(pnl, 2), "legs": len(placed),
                "move": move_vol / vol_out if vol_out else None, "volume": vol_out}
