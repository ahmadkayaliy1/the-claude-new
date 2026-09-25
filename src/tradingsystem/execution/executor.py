"""Executor service: ``python -m tradingsystem executor`` (P9.6–P9.8).

* Candidates: valid BUY/SELL decisions — ``execution.trigger=auto``: every new decision; ``manual``: only those
  the user queued with the dashboard's Execute-Now button (``execution_state='queued'``).
* For each: live quotes → basis check + level translation (analysis → execution instrument) → deterministic
  risk gate → backend (paper by default; MT5 demo/live only when configured, account type asserted).
* Paper positions are advanced on the real tick stream of the execution instrument; outcomes are written back
  to the decision (pips / USD / %); virtual outcomes of every trade idea are computed on real prices.
* Kill switch: file ``data/KILL_SWITCH`` (or config) blocks all new orders.
"""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import time
from pathlib import Path

import numpy as np

from ..ai.store import DecisionStore
from ..analysis import indicators as ind
from ..analysis.frames import load_frame
from ..core.instruments import InstrumentRegistry
from ..core.logsetup import setup_from_settings
from ..core.sessions import calendar_for
from ..core.settings import Settings, load_settings
from ..core.timeutil import MS_PER_MINUTE, iso, now_ms, parse_date_spec
from ..ingest.common.appdb import AppDB
from ..storage.reader import InstrumentReader
from ..storage.tablespec import spec_for
from .backends.paper import PaperBackend, Tick
from .price_mapping import check_basis, translate
from .risk_gate import ExecContext, evaluate

log = logging.getLogger("executor")
STOPS_LEVEL_PRICE = {"XAUUSD@": 0.25, "BTCUSD@": 25.0, "ETHUSD@": 2.0}   # P1.5 (stops_level × point); MT5 backend reads live


class Executor:
    def __init__(self, s: Settings) -> None:
        self.s = s
        self.reg = InstrumentRegistry.from_settings(s)
        data = s.paths.data()
        self.app_db = data / "app.db"
        self.appdb = AppDB(self.app_db)
        self.store = DecisionStore(self.app_db, s.config_hash)
        self.readers: dict[str, InstrumentReader] = {}
        self.mode = s.execution.mode
        self.paper = PaperBackend(self.app_db, s.execution.paper_equity) if self.mode == "paper" else None
        self.mt5 = None
        if self.mode in ("demo", "live"):
            from ..ingest.mt5.terminal import MT5Terminal
            from .backends.mt5_backend import MT5Backend
            prof = s.mt5.profiles[s.mt5.execution_profile_by_mode[self.mode]]
            term = MT5Terminal(prof)
            term.connect()
            self.mt5 = MT5Backend(term, s.execution.magic, expected_account_type=prof.account_type)
            self.mt5.assert_account()
        self.tick_cursor: dict[str, int] = {}
        self.started = now_ms()
        self.stop = False

    # ------------------------------------------------------------------ data helpers
    def reader(self, key: str) -> InstrumentReader:
        if key not in self.readers:
            self.readers[key] = InstrumentReader(self.reg.get(key), self.s.paths.data())
        return self.readers[key]

    def latest_quote(self, key: str) -> Tick | None:
        inst = self.reg.get(key)
        dt_ = "ticks" if "ticks" in inst.datatypes else "book_ticker" if "book_ticker" in inst.datatypes else None
        if dt_ is None:
            return None
        spec = spec_for(inst, dt_)
        c = self.reader(key).read_range(spec, now_ms() - 10 * MS_PER_MINUTE, None,
                                        [spec.time_col, "bid", "ask"])
        if not len(c[spec.time_col]):
            return None
        return Tick(int(c[spec.time_col][-1]), float(c["bid"][-1]), float(c["ask"][-1]))

    def basis_history(self, pair: str, minutes: int = 60) -> list[float]:
        prim, exe = self.reg.primary(pair), self.reg.with_role(pair, "execution")[0]
        if prim.key == exe.key:
            return []
        out = []
        now = now_ms()
        pq = self.reader(prim.key).read_range(spec_for(prim, "book_ticker"), now - minutes * MS_PER_MINUTE, None,
                                              ["ts", "bid", "ask"])
        eq = self.reader(exe.key).read_range(spec_for(exe, "ticks"), now - minutes * MS_PER_MINUTE, None,
                                             ["time_msc", "bid", "ask"])
        if not len(pq["ts"]) or not len(eq["time_msc"]):
            return []
        for m in range(now - minutes * MS_PER_MINUTE, now, MS_PER_MINUTE):
            i, j = np.searchsorted(pq["ts"], m, "right") - 1, np.searchsorted(eq["time_msc"], m, "right") - 1
            if i >= 0 and j >= 0:
                out.append((eq["bid"][j] + eq["ask"][j]) / 2 - (pq["bid"][i] + pq["ask"][i]) / 2)
        return out

    def atr(self, pair: str) -> float:
        prim = self.reg.primary(pair)
        tf = self.s.pairs[pair].decision_timeframe
        fr = load_frame(self.reader(prim.key), prim, tf, 60, now_ms())
        a = ind.atr(fr.high, fr.low, fr.close)
        return float(a[-1]) if len(a) and not np.isnan(a[-1]) else float("nan")

    # ------------------------------------------------------------------ candidates
    def candidates(self) -> list[dict]:
        con = sqlite3.connect(f"file:{self.app_db.as_posix()}?mode=ro", uri=True)
        try:
            states = ("queued",) if self.s.execution.trigger == "manual" else ("queued", "not_executed")
            rows = con.execute(
                f"SELECT id, ts, pair, recommendation, execution_state FROM ai_decisions WHERE status='valid' "
                f"AND decision IN ('BUY','SELL') AND execution_state IN ({','.join('?' * len(states))}) "
                f"AND (execution_state='queued' OR ts >= ?) ORDER BY ts",
                (*states, self.started)).fetchall()
        finally:
            con.close()
        return [{"id": r[0], "ts": r[1], "pair": r[2], "rec": json.loads(r[3]), "state": r[4]} for r in rows]

    # ------------------------------------------------------------------ execution
    def handle(self, cand: dict) -> None:
        pair, rec, did = cand["pair"], cand["rec"], cand["id"]
        exe = self.reg.with_role(pair, "execution")[0]
        prim = self.reg.primary(pair)
        pcfg = self.s.pairs[pair]
        eq = self.latest_quote(exe.key)
        if eq is None:
            self.store.set_execution_state(did, "rejected", {"reason": "no execution quote"})
            return
        basis_ok, basis_reason, rec_x = True, "same instrument", rec
        if prim.key != exe.key:
            pq = self.latest_quote(prim.key)
            if pq is None:
                self.store.set_execution_state(did, "rejected", {"reason": "no analysis quote for basis"})
                return
            bc = check_basis((pq.bid + pq.ask) / 2, (eq.bid + eq.ask) / 2, self.basis_history(pair),
                             self.s.execution.max_basis_deviation_pct)
            basis_ok, basis_reason = bc.ok, bc.reason
            rec_x = translate(rec, bc.basis, exe.contract["tick_size"] if exe.contract else 0.01)
        acct = self.paper.account({exe.key: eq}) if self.paper else self.mt5.account()
        spec = exe.contract or {"contract_size": 1, "volume_min": 0.01, "volume_step": 0.01, "tick_size": 0.01}
        ctx = ExecContext(
            now_ms=now_ms(), bid=eq.bid, ask=eq.ask, quote_age_s=max(0.0, (now_ms() - eq.time_msc) / 1000),
            market_open=calendar_for(exe.venue, exe.symbol, pcfg.asset_class).is_open(now_ms()), atr=self.atr(pair),
            stops_level_price=STOPS_LEVEL_PRICE.get(exe.symbol, 0.0), contract_size=spec["contract_size"],
            volume_min=spec["volume_min"], volume_step=spec["volume_step"], volume_max=None, equity=acct["equity"],
            open_positions=acct["open_positions"], open_risk_pct_by_pair=acct.get("open_risk_pct_by_pair", {}),
            realized_pnl_today_usd=acct.get("realized_today_usd", 0.0), unrealized_pnl_usd=acct.get("unrealized_usd", 0.0),
            kill_switch=(self.s.paths.data() / "KILL_SWITCH").exists(), basis_ok=basis_ok, basis_reason=basis_reason)
        gate = evaluate(rec_x, pair, ctx, self.s.risk, self.s.risk.correlated_groups)
        detail = {"gate": [{"check": n, "ok": ok, "detail": d} for n, ok, d in gate.checks],
                  "executed_levels": {"entry": gate.entry, "stop_loss": rec_x["stop_loss"],
                                      "take_profits": [t["price"] for t in rec_x["take_profits"]]},
                  "translation": rec_x.get("price_reference_translated"), "mode": self.mode,
                  "lots": gate.size.lots if gate.size else None,
                  "risk_pct": round(gate.size.risk_pct, 3) if gate.size else None, "rr_exec": gate.rr_exec}
        if not gate.approved:
            detail["reason"] = "; ".join(gate.failures())
            self.store.set_execution_state(did, "rejected", detail)
            self.appdb.add_event("executor", "gate_rejected", f"{pair} {did[:8]}: {detail['reason']}"[:300])
            log.info("%s %s rejected by gate: %s", pair, did[:8], detail["reason"])
            return
        self.store.set_execution_state(did, "executing", detail)
        if self.paper:
            res = self.paper.place(decision_id=did, pair=pair, instrument=exe.key, rec=rec_x, lots=gate.size.lots,
                                   entry=gate.entry, contract_size=spec["contract_size"],
                                   volume_step=spec["volume_step"], volume_min=spec["volume_min"], quote=eq)
        else:
            res = self.mt5.place(decision_id=did, symbol=exe.symbol, rec=rec_x, lots=gate.size.lots, entry=gate.entry,
                                 dry_run=False)
        detail["backend"] = res
        self.store.set_execution_state(did, "executed" if res.get("ok") else "rejected", detail)
        self.appdb.add_event("executor", "order" if res.get("ok") else "order_failed",
                             f"{self.mode} {pair} {did[:8]}: {res.get('reason') or res.get('legs') or res.get('placed')}"[:300])
        log.info("%s %s → %s: %s", pair, did[:8], self.mode, "placed" if res.get("ok") else res.get("reason"))

    # ------------------------------------------------------------------ paper simulation + outcomes
    def advance_paper(self) -> None:
        for inst in {self.reg.with_role(p, "execution")[0] for p in self.s.enabled_pairs()}:
            spec = spec_for(inst, "ticks")
            cur = self.tick_cursor.get(inst.key)
            start = now_ms() - 5 * MS_PER_MINUTE if cur is None else None
            c = self.reader(inst.key).read_range(spec, start, None, ["key", "time_msc", "bid", "ask"])
            keys = c["key"]
            if cur is not None:
                m = keys > cur
                keys, c = keys[m], {k: v[m] for k, v in c.items()}
            if not len(keys):
                continue
            self.tick_cursor[inst.key] = int(keys[-1])
            ticks = [Tick(int(t), float(b), float(a)) for t, b, a in zip(c["time_msc"], c["bid"], c["ask"])]
            for ev in self.paper.process(inst.key, ticks):
                self.appdb.add_event("executor", f"paper_{ev['event']}", json.dumps(ev)[:300])
        self._settle_paper_outcomes()

    def _settle_paper_outcomes(self) -> None:
        for did in self.paper.decisions_with_legs(("closed", "expired", "cancelled")):
            legs = self.paper.decision_legs(did)
            if any(l["status"] in ("open", "pending") for l in legs):
                continue
            con = sqlite3.connect(f"file:{self.app_db.as_posix()}?mode=ro", uri=True)
            try:
                r = con.execute("SELECT outcome, pair FROM ai_decisions WHERE id=?", (did,)).fetchone()
            finally:
                con.close()
            if not r or r[0] is not None:
                continue
            pair = r[1]
            filled = [l for l in legs if l["fill_price"] is not None]
            if not filled:
                self.store.set_outcome(did, "not_filled", 0.0, 0.0, 0.0)
                continue
            pnl = sum(l["pnl_usd"] or 0.0 for l in filled)
            pip = self.s.pairs[pair].pip_size
            vol = sum(l["volume"] for l in filled)
            pips = sum(((l["close_price"] - l["fill_price"]) if l["side"] == "BUY" else (l["fill_price"] - l["close_price"]))
                       / pip * l["volume"] for l in filled if l["close_price"] is not None) / vol
            equity0 = self.s.execution.paper_equity
            outcome = "closed_profit" if pnl > 0 else "closed_loss" if pnl < 0 else "closed_breakeven"
            self.store.set_outcome(did, outcome, round(pnl, 2), round(pnl / equity0 * 100, 3), round(pips, 1))

    def virtual_outcomes(self) -> None:
        """P9.8: would the idea have reached TP1 before its SL? Evaluated on real 1m bars of the analysis instrument."""
        con = sqlite3.connect(f"file:{self.app_db.as_posix()}?mode=ro", uri=True)
        try:
            rows = con.execute("SELECT id, pair, recommendation FROM ai_decisions WHERE status='valid' AND "
                               "decision IN ('BUY','SELL') AND virtual_outcome IS NULL").fetchall()
        finally:
            con.close()
        for did, pair, rj in rows:
            rec = json.loads(rj)
            vo, vr = evaluate_virtual(rec, self.reader(self.reg.primary(pair).key), self.reg.primary(pair))
            if vo is not None:
                c = sqlite3.connect(self.app_db, timeout=10)
                c.execute("UPDATE ai_decisions SET virtual_outcome=?, virtual_r=? WHERE id=?", (vo, vr, did))
                c.commit()
                c.close()

    # ------------------------------------------------------------------ loop
    def run(self) -> None:
        self.appdb.set_status("executor", "starting")
        last_virtual = 0.0
        while not self.stop:
            t0 = time.time()
            try:
                if self.paper:
                    self.advance_paper()
                for cand in self.candidates():
                    self.handle(cand)
                if t0 - last_virtual > 60:
                    self.virtual_outcomes()
                    last_virtual = t0
                acct = self.paper.account() if self.paper else self.mt5.account()
                self.appdb.set_status("executor", "live", last_data_ms=now_ms(), detail={
                    "mode": self.mode, "trigger": self.s.execution.trigger, "equity": acct.get("equity"),
                    "open_positions": acct.get("open_positions"),
                    "kill_switch": (self.s.paths.data() / "KILL_SWITCH").exists()})
            except Exception as exc:  # noqa: BLE001
                log.exception("executor loop error")
                self.appdb.set_status("executor", "error", error=repr(exc)[:300])
            time.sleep(max(0.2, 1.0 - (time.time() - t0)))


def evaluate_virtual(rec: dict, reader: InstrumentReader, inst) -> tuple[str | None, float | None]:
    ts, vu = parse_date_spec(rec["timestamp"]), parse_date_spec(rec["valid_until"])
    horizon = vu + 24 * 3_600_000
    c = reader.read_range(spec_for(inst, "candles", inst.timeframes[0]), ts, None, ["open_time", "high", "low"])
    if not len(c["open_time"]):
        return None, None
    buy = rec["decision"] == "BUY"
    e = rec["entry"]
    entry = e.get("price") or ((e["range_max"] if buy else e["range_min"]) if e.get("range_min") is not None else None)
    sl, tp1 = rec["stop_loss"], rec["take_profits"][0]["price"]
    filled = rec["order_type"] == "MARKET"
    for t, h, l in zip(c["open_time"], c["high"], c["low"]):
        if not filled:
            if t >= vu:
                return "not_triggered", 0.0
            hit = {"BUY_LIMIT": l <= entry, "BUY_STOP": h >= entry,
                   "SELL_LIMIT": h >= entry, "SELL_STOP": l <= entry}[rec["order_type"]]
            if not hit:
                continue
            filled = True
        sl_hit = (l <= sl) if buy else (h >= sl)
        tp_hit = (h >= tp1) if buy else (l <= tp1)
        if sl_hit:                         # same-bar ambiguity resolved conservatively (stop first)
            return "sl_first", -1.0
        if tp_hit:
            r = abs(tp1 - entry) / abs(entry - sl) if entry and entry != sl else None
            return "tp1_first", round(r, 2) if r is not None else None
        if t > horizon:
            return "unresolved_24h", 0.0
    return None, None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="tradingsystem executor")
    ap.add_argument("--once", action="store_true", help="process pending candidates once and exit")
    args = ap.parse_args(argv)
    s = load_settings()
    setup_from_settings("executor", s)
    ex = Executor(s)
    log.info("executor started: mode=%s trigger=%s", s.execution.mode, s.execution.trigger)
    if args.once:
        for cand in ex.candidates():
            ex.handle(cand)
        return 0
    try:
        ex.run()
    except KeyboardInterrupt:
        pass
    return 0


__all__ = ["Executor", "evaluate_virtual", "main", "iso", "Path"]
