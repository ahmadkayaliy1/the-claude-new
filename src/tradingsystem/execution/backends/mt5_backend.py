"""MetaTrader 5 execution backend (P9.4) — demo or live, same code; the account type is asserted first.

For every leg: build request (FOK filling, SL + TP attached, expiration in *server* time for pending orders)
→ ``order_check`` (margin/stops validation) → idempotency lookup by comment tag → ``order_send`` with bounded
retries for retryable codes (a timeout is only retried after verifying nothing was created) → post-fill
slippage recorded. Every attempt is returned with its retcode meaning — no silent failures (spec §7.3).
``dry_run=True`` stops after ``order_check`` (used until the user approves demo orders, H6).
"""
from __future__ import annotations

import logging
import time

from ...core.timeutil import now_ms, parse_date_spec
from ...ingest.mt5.servertime import ServerTimeModel
from ...ingest.mt5.terminal import MT5Terminal
from ..retcodes import SUCCESS, describe, retryable
from .paper import Tick, split_volume

log = logging.getLogger(__name__)


def tag(decision_id: str, leg: int) -> str:
    return f"ts:{decision_id[:20]}:{leg}"          # ≤ 31 chars (MT5 comment limit)


class MT5Backend:
    name = "mt5"

    def __init__(self, terminal: MT5Terminal, magic: int, *, expected_account_type: str,
                 pair_by_symbol: dict[str, str] | None = None) -> None:
        self.t = terminal
        self.mt5 = terminal.mt5
        self.magic = magic
        self.expected = expected_account_type
        self.pair_by_symbol = dict(pair_by_symbol or {})     # MT5 symbol → logical pair (risk is keyed by pair)
        self._warned: set[str] = set()
        self.model = ServerTimeModel()

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
        a = self.t.account()
        m = self.mt5
        positions = [p for p in (m.positions_get() or []) if p.magic == self.magic]
        orders = [o for o in (m.orders_get() or []) if o.magic == self.magic]
        buy_orders = {m.ORDER_TYPE_BUY, m.ORDER_TYPE_BUY_LIMIT, m.ORDER_TYPE_BUY_STOP}
        risk_usd: dict[str, float] = {}

        def add_risk(symbol: str, buy: bool, volume: float, price: float, sl: float) -> None:
            loss = m.order_calc_profit(m.ORDER_TYPE_BUY if buy else m.ORDER_TYPE_SELL, symbol, volume, price, sl) or 0.0
            pair = self._pair(symbol)
            risk_usd[pair] = risk_usd.get(pair, 0.0) + max(0.0, -loss)

        for p in positions:
            if p.sl:
                add_risk(p.symbol, p.type == m.POSITION_TYPE_BUY, p.volume, p.price_open, p.sl)
        for o in orders:
            if o.sl:
                add_risk(o.symbol, o.type in buy_orders, o.volume_current, o.price_open, o.sl)
        today = now_ms() // 86_400_000 * 86_400_000
        deals = m.history_deals_get(self.model.utc_to_server(today) // 1000, int(time.time()) + 86_400) or []
        realized = sum(d.profit + d.commission + d.swap for d in deals if d.magic == self.magic)
        with_pos = {p.comment.rsplit(":", 1)[0] for p in positions}
        with_ord = {o.comment.rsplit(":", 1)[0] for o in orders}
        return {"mode": self.expected, "currency": a.currency, "balance": a.balance, "equity": a.equity,
                "margin_free": a.margin_free, "unrealized_usd": round(sum(p.profit + p.swap for p in positions), 2),
                "open_positions": len(with_pos | with_ord), "open_orders": len(with_ord - with_pos),
                "open_risk_usd_by_pair": {k: round(v, 2) for k, v in risk_usd.items()},
                "open_risk_pct_by_pair":
                    {k: round(v / a.equity * 100, 3) for k, v in risk_usd.items()} if a.equity > 0 else {},
                "realized_today_usd": realized}

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
        prefix = f"ts:{decision_id[:20]}:"
        orders = [o for o in (self.mt5.orders_get() or []) if o.comment.startswith(prefix)]
        positions = [p for p in (self.mt5.positions_get() or []) if p.comment.startswith(prefix)]
        deals = [d for d in (self.mt5.history_deals_get(int(time.time()) - 7 * 86_400, int(time.time()) + 86_400) or [])
                 if d.comment.startswith(prefix)]
        return {"orders": orders, "positions": positions, "deals": deals}

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
        if vols is None:
            k = max(range(len(tps)), key=lambda i: (tps[i]["close_fraction"], -i))
            tps, vols = [tps[k]], [lots]
            note = f"volume too small to split — single position at TP{k + 1}"
        m = self.mt5
        otype = {("BUY", "MARKET"): m.ORDER_TYPE_BUY, ("SELL", "MARKET"): m.ORDER_TYPE_SELL,
                 ("BUY", "BUY_LIMIT"): m.ORDER_TYPE_BUY_LIMIT, ("SELL", "SELL_LIMIT"): m.ORDER_TYPE_SELL_LIMIT,
                 ("BUY", "BUY_STOP"): m.ORDER_TYPE_BUY_STOP, ("SELL", "SELL_STOP"): m.ORDER_TYPE_SELL_STOP}[
            (rec["decision"], rec["order_type"])]
        reqs = []
        q = self.mt5.symbol_info_tick(symbol)
        for i, (tp, v) in enumerate(zip(tps, vols), start=1):
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

    # ------------------------------------------------------------------ management
    def modify_sl(self, ticket: int, symbol: str, sl: float, tp: float) -> dict:
        res = self.mt5.order_send({"action": self.mt5.TRADE_ACTION_SLTP, "position": ticket, "symbol": symbol,
                                   "sl": sl, "tp": tp, "magic": self.magic})
        return {"ok": getattr(res, "retcode", None) in SUCCESS, "meaning": describe(getattr(res, "retcode", None))}

    def cancel_order(self, ticket: int) -> dict:
        res = self.mt5.order_send({"action": self.mt5.TRADE_ACTION_REMOVE, "order": ticket, "magic": self.magic})
        return {"ok": getattr(res, "retcode", None) in SUCCESS, "meaning": describe(getattr(res, "retcode", None))}
