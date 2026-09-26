"""Executor service: ``python -m tradingsystem executor`` (P9.6–P9.8).

* Candidates: valid BUY/SELL decisions — ``execution.trigger=auto``: every new decision; ``manual``: only those
  the user queued with the dashboard's Execute-Now button (``execution_state='queued'``).
* For each: live quotes → basis check + level translation (analysis → execution instrument) → deterministic
  risk gate → backend (paper by default; MT5 demo/live only when configured, account type asserted).
* Paper positions are advanced on the real tick stream of the execution instrument from each leg's persisted
  watermark (a restart replays exactly the ticks it missed; steady state reads only new ticks through the time
  index, never the whole history); outcomes are written back to the decision (pips / USD / %); virtual outcomes
  of every trade idea are computed on real prices.
* Each candidate is isolated: claimed atomically ('executing'), a failure never blocks the others (transient errors
  retried with backoff, then rejected), and 'executing' rows left by a crash are reconciled with the backend.
* Kill switch: file ``data/KILL_SWITCH`` blocks all new orders of every system, ``data/instances/<PAIR>/KILL_SWITCH``
  those of one system.
* One system per pair (D-042): its own MT5 magic (``execution.magic`` + the instance's offset) → its own positions,
  daily loss and open-trade limits; the other systems' positions count towards the correlated cap; the account-wide
  drawdown stop (:mod:`.drawdown`) is shared. What it holds is published in its status row (``exposure``) for the
  dashboard and the model's payload, and the gate refuses a second trade in the direction of a live one.
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
from ..core.filelock import FileLock, locks_dir
from ..core.instruments import InstrumentRegistry, _resolve_symbol
from ..core.logsetup import setup_from_settings
from ..core.sessions import calendar_for
from ..core.settings import Settings, load_settings
from ..core.timeutil import MS_PER_DAY, MS_PER_HOUR, MS_PER_MINUTE, iso, now_ms, parse_date_spec
from ..ingest.common.appdb import AppDB
from ..storage.reader import InstrumentReader
from ..storage.tablespec import spec_for
from .backends.paper import PaperBackend, Tick
from .drawdown import AccountPeak
from .exposure import live_sides
from .price_mapping import check_basis, translate
from .risk_gate import ExecContext, evaluate

log = logging.getLogger("executor")
STOPS_LEVEL_PRICE = {"XAUUSD@": 0.25, "BTCUSD@": 25.0, "ETHUSD@": 2.0}   # P1.5 (stops_level × point); MT5 backend reads live
MT5_SETTLE_MS = 3_000          # after an MT5 placement, before the next candidate is gated (broker listing lag)
SETTLE_LOOKBACK_DAYS = 45      # executed decisions older than this are no longer polled for their outcome
TICK_CHUNK_MS = MS_PER_HOUR          # paper replay window: bounded memory, indexed [start, end) time-range reads
MAX_REPLAY_TICKS = 300_000           # per instrument per loop — a long catch-up resumes on the next loop
MAX_HANDLE_ATTEMPTS = 5              # transient failures of one decision before it is rejected
ERROR_HEARTBEAT_MS = 5 * MS_PER_MINUTE   # a loop failing longer stops refreshing its heartbeat → watchdog restart
PEAK_EVERY_S = 5.0                   # the account peak file is read/updated at most this often by the loop
PLACEMENT_LOCK_WAIT_S = 20.0         # MT5: gate + placement of one system at a time on the account (D-042)


class PlacementBusy(RuntimeError):
    """Another system holds the MT5 placement lock: the candidate stays queued and the pass ends (the heartbeat
    keeps going; this is not a failed attempt)."""


def magics(s: Settings) -> tuple[int, int, set[int]]:
    """(this system's magic, the all-pairs system's magic, every magic of this project) — D-042."""
    inst = s.paths.instance
    base = s.execution.magic - (s.instances[inst].magic_offset if inst and inst in s.instances else 0)
    return s.execution.magic, base, {base, *(base + i.magic_offset for i in s.instances.values())}


def all_pairs_by_symbol(s: Settings, profile: str) -> dict[str, str]:
    """Execution-account MT5 symbol → pair for every configured pair (enabled here or in another system)."""
    out, enabled = {}, set(s.enabled_pairs())
    for name, pair in s.pairs.items():
        for icfg in pair.instruments:
            if icfg.venue == "mt5" and "execution" in icfg.roles:
                try:
                    out[_resolve_symbol(icfg, profile)] = name
                except ValueError:
                    if name in enabled:
                        raise
                    log.warning("pair %s has no symbol for MT5 profile %r - its positions are not attributed to it",
                                name, profile)
    return out


class Executor:
    def __init__(self, s: Settings) -> None:
        self.s = s
        self.reg = InstrumentRegistry.from_settings(s)
        self.exec_reg = self.reg             # symbols as named on the execution account (MT5 profile of the mode)
        data = s.paths.state()
        data.mkdir(parents=True, exist_ok=True)
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
            pname = s.mt5.execution_profile_by_mode[self.mode]
            prof = s.mt5.profiles[pname]
            self.exec_reg = InstrumentRegistry.from_settings(s, mt5_profile=pname)
            term = MT5Terminal(prof)
            self._connect_mt5(term)
            own, base, family = magics(s)
            self.mt5 = MT5Backend(term, own, expected_account_type=prof.account_type,
                                  pair_by_symbol={**all_pairs_by_symbol(s, pname), **self.pair_by_symbol()},
                                  own_pairs=set(s.enabled_pairs()),
                                  adopt_magic=base if s.paths.instance else None, family=family)
            self.mt5.assert_account()
        self.attempts: dict[str, tuple[int, int]] = {}     # decision id → (failed attempts, next try ms)
        self.unreconciled: set[str] = set()                # 'executing' rows whose reconciliation failed
        self.reconcile_due = True                          # 'executing' rows left by a previous run: settle first
        self.loop_errors: list[int] = []
        self.error_since: int | None = None
        self.clear_error = True                            # first healthy loop clears a stale last_error
        self._warned: set[str] = set()
        self.started = now_ms()
        self.mt5_quiet_until = 0
        self.stop = False
        self.peak: AccountPeak | None = None                # the account's high-water mark (shared by every system)
        self._peak_at = 0.0

    def _connect_mt5(self, term, first_delay_s: float = 5.0) -> None:
        """MT5 may be offline at start-up (terminal starting, reconnecting after a network drop): retry with backoff,
        reporting 'reconnecting' (a fresh heartbeat), instead of exiting into a supervisor restart loop. An account
        mismatch is permanent and still raises."""
        from ..ingest.mt5.terminal import MT5Unavailable
        delay = first_delay_s
        while True:
            try:
                term.connect()
                return
            except MT5Unavailable as exc:
                self.appdb.set_status("executor", "reconnecting", error=f"MT5 at start-up: {exc}"[:200])
                log.warning("MT5 not available at start-up (%s) — retry in %.0fs", exc, delay)
                time.sleep(delay)
                delay = min(delay * 2, 60.0)

    def pair_by_symbol(self) -> dict[str, str]:
        """Execution-account MT5 symbol → pair (the MT5 account's risk is reported per pair)."""
        out = {}
        for p in self.s.enabled_pairs():
            exe = self.exec_reg.with_role(p, "execution")
            if exe and exe[0].venue == "mt5":
                out[exe[0].symbol] = p
        return out

    # ------------------------------------------------------------------ data helpers
    def reader(self, key: str) -> InstrumentReader:
        if key not in self.readers:
            self.readers[key] = InstrumentReader(self.reg.get(key), self.s.paths.data())
        return self.readers[key]

    def latest_quote(self, key: str, max_age_ms: int | None = 10 * MS_PER_MINUTE) -> Tick | None:
        """Newest stored tick / bookTicker (``ORDER BY key DESC LIMIT 1`` on the hot store — no range scan);
        None when older than ``max_age_ms`` (None = any age: last known price, e.g. to mark positions)."""
        inst = self.reg.get(key)
        dt_ = "ticks" if "ticks" in inst.datatypes else "book_ticker" if "book_ticker" in inst.datatypes else None
        if dt_ is None:
            return None
        spec = spec_for(inst, dt_)
        cols = ["key", spec.time_col, "bid", "ask"]
        rd = self.reader(key)
        c = rd.hot.read_last(spec, 1, cols) if rd.hot is not None else {"key": ()}
        if not len(c["key"]):                        # no hot rows yet → the recent cold archive
            now = now_ms()
            c = rd.read_range(spec, now - (max_age_ms or MS_PER_DAY), now + MS_PER_MINUTE, cols)
        if not len(c["key"]):
            return None
        t = int(c[spec.time_col][-1])
        if max_age_ms is not None and now_ms() - t > max_age_ms:
            return None
        return Tick(t, float(c["bid"][-1]), float(c["ask"][-1]), int(c["key"][-1]))

    def marks(self) -> dict[str, Tick]:
        """Last known quote of every execution instrument → unrealised PnL of all open paper legs (every pair)."""
        out: dict[str, Tick] = {}
        for p in self.s.enabled_pairs():
            exe = self.reg.with_role(p, "execution")[0]
            if exe.key not in out and (q := self.latest_quote(exe.key, None)) is not None:
                out[exe.key] = q
        return out

    def basis_history(self, pair: str, minutes: int = 60) -> list[float]:
        prim, exe = self.reg.primary(pair), self.reg.with_role(pair, "execution")[0]
        if prim.key == exe.key:
            return []
        out = []
        now = now_ms()
        lo, hi = now - minutes * MS_PER_MINUTE, now + MS_PER_MINUTE     # both bounds → the time index is used
        pq = self.reader(prim.key).read_range(spec_for(prim, "book_ticker"), lo, hi, ["ts", "bid", "ask"])
        eq = self.reader(exe.key).read_range(spec_for(exe, "ticks"), lo, hi, ["time_msc", "bid", "ask"])
        if not len(pq["ts"]) or not len(eq["time_msc"]):
            return []
        for m in range(now - minutes * MS_PER_MINUTE, now, MS_PER_MINUTE):
            i, j = np.searchsorted(pq["ts"], m, "right") - 1, np.searchsorted(eq["time_msc"], m, "right") - 1
            if i >= 0 and j >= 0:
                out.append((eq["bid"][j] + eq["ask"][j]) / 2 - (pq["bid"][i] + pq["ask"][i]) / 2)
        return out

    def account_peak(self, acct: dict) -> AccountPeak:
        """The shared high-water-mark record of this account: the MT5 login, or this system's paper account."""
        if self.peak is None:
            key = (f"mt5:{acct.get('server') or self.s.execution.mode}:{acct.get('login') or '?'}" if self.mt5 else
                   f"paper:{self.s.paths.instance or 'all'}")
            self.peak = AccountPeak(self.s.paths.shared(), key, self.s.risk.account_drawdown_stop_pct)
        return self.peak

    def update_peak(self, acct: dict, force: bool = False):
        """Record the account's equity in the shared peak file (every ``PEAK_EVERY_S`` from the loop, always at
        gate time) → :class:`.drawdown.PeakState`, or None without a usable equity."""
        eq = acct.get("equity")
        if not eq or eq <= 0:
            return None
        peak = self.account_peak(acct)
        if not force and peak.last is not None and time.time() - self._peak_at < PEAK_EVERY_S:
            return peak.last
        self._peak_at = time.time()
        return peak.update(float(eq))

    def kill_switch(self, pair: str | None = None) -> bool:
        """``data/KILL_SWITCH`` stops every system (scripts/kill_switch_on.bat); ``data/instances/<PAIR>/KILL_SWITCH``
        (kill_switch_on.bat <PAIR>) stops that pair — in its own system and in the all-pairs system alike.
        Without ``pair``: whether any switch that concerns this system is on (status)."""
        data = self.s.paths.data()
        if (data / "KILL_SWITCH").exists() or (self.s.paths.state() / "KILL_SWITCH").exists():
            return True
        pairs = [pair] if pair else list(self.s.enabled_pairs())
        return any((data / "instances" / p / "KILL_SWITCH").exists() for p in pairs)

    def atr(self, pair: str, as_of: int | None = None) -> float:
        """Decision-TF ATR14 exactly as the snapshot computed it for the model (same bar count, as of the
        recommendation's cycle time), so the stop bounds the model was shown are the ones the gate checks."""
        from ..analysis.snapshot import TF_PLAN
        prim = self.reg.primary(pair)
        tf = self.s.pairs[pair].decision_timeframe
        fr = load_frame(self.reader(prim.key), prim, tf, TF_PLAN.get(tf.value, (60, 0))[0], as_of or now_ms())
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
        """Gate and place one candidate. MT5: one system at a time from reading the account to the broker listing
        the new order (``mt5_placement.lock``), so two pairs' systems never both pass the correlated-risk cap on
        the same account snapshot; a busy lock raises (the candidate is retried with backoff)."""
        if self.mt5 is None:
            self._handle(cand)
            return
        with FileLock(locks_dir(self.s) / "mt5_placement.lock").hold(timeout=PLACEMENT_LOCK_WAIT_S) as got:
            if not got:
                raise PlacementBusy(f"MT5 placement lock busy for {PLACEMENT_LOCK_WAIT_S:.0f}s (another system is "
                                    "placing) - retried on the next pass")
            self._placing = False
            try:
                self._handle(cand)
            finally:
                if self._placing:                  # also when placing raised after some legs reached the broker
                    time.sleep(MT5_SETTLE_MS / 1000)   # the broker lists the new order before another system gates

    def _handle(self, cand: dict) -> bool:
        """True when an order reached the backend (placed or partly placed)."""
        pair, rec, did = cand["pair"], cand["rec"], cand["id"]
        if pair not in self.s.enabled_pairs():
            self.store.set_execution_state(did, "rejected", {"reason": f"pair {pair} is not enabled"})
            return False
        if now_ms() >= parse_date_spec(rec["valid_until"]):
            self.store.set_execution_state(did, "expired", {"reason": f"expired (valid until {rec['valid_until']})"})
            return False
        exe = self.reg.with_role(pair, "execution")[0]
        exe_symbol = self.exec_reg.with_role(pair, "execution")[0].symbol
        prim = self.reg.primary(pair)
        pcfg = self.s.pairs[pair]
        eq = self.latest_quote(exe.key)
        if eq is None:
            self.store.set_execution_state(did, "rejected", {"reason": "no execution quote"})
            return False
        basis_ok, basis_reason, rec_x = True, "same instrument", rec
        if prim.key != exe.key:
            pq = self.latest_quote(prim.key)
            if pq is None:
                self.store.set_execution_state(did, "rejected", {"reason": "no analysis quote for basis"})
                return False
            bc = check_basis((pq.bid + pq.ask) / 2, (eq.bid + eq.ask) / 2, self.basis_history(pair),
                             self.s.execution.max_basis_deviation_pct)
            basis_ok, basis_reason = bc.ok, bc.reason
            rec_x = translate(rec, bc.basis, exe.contract["tick_size"] if exe.contract else 0.01)
        acct = self.paper.account({**self.marks(), exe.key: eq}) if self.paper else self.mt5.account()
        dd = self.update_peak(acct, force=True)
        spec = exe.contract or {"contract_size": 1, "volume_min": 0.01, "volume_step": 0.01, "tick_size": 0.01}
        stops, vmax = STOPS_LEVEL_PRICE.get(exe.symbol, 0.0), None
        if self.mt5:                           # the broker's live specs: stops level, volume limits, contract size
            live = self.mt5.specs(exe_symbol)
            stops, vmax = live["stops_level_price"], live["volume_max"]
            spec = {**spec, **{k: live[k] for k in ("contract_size", "volume_min", "volume_step")}}
        ctx = ExecContext(
            now_ms=now_ms(), bid=eq.bid, ask=eq.ask, quote_age_s=max(0.0, (now_ms() - eq.time_msc) / 1000),
            market_open=calendar_for(exe.venue, exe.symbol, pcfg.asset_class).is_open(now_ms()), atr=self.atr(pair, _rec_as_of(rec)),
            stops_level_price=stops, contract_size=spec["contract_size"],
            volume_min=spec["volume_min"], volume_step=spec["volume_step"], volume_max=vmax, equity=acct["equity"],
            open_positions=acct["open_positions"], open_risk_pct_by_pair=acct.get("open_risk_pct_by_pair", {}),
            realized_pnl_today_usd=acct.get("realized_today_usd", 0.0), unrealized_pnl_usd=acct.get("unrealized_usd", 0.0),
            kill_switch=self.kill_switch(pair), basis_ok=basis_ok, basis_reason=basis_reason,
            sibling_risk_pct_by_pair=acct.get("sibling_risk_pct_by_pair", {}),
            live_sides=live_sides(acct.get("exposure") or [], pair),
            account_drawdown_pct=dd.drawdown_pct if dd else None, account_drawdown_tripped=bool(dd and dd.tripped))
        gate = evaluate(rec_x, pair, ctx, self.s.risk, self.s.risk.correlated_groups,
                        min_confidence=self.s.risk.min_confidence)
        detail = {"gate": [{"check": n, "ok": ok, "detail": d} for n, ok, d in gate.checks],
                  "executed_levels": {"entry": gate.entry, "stop_loss": rec_x["stop_loss"],
                                      "take_profits": [t["price"] for t in rec_x["take_profits"]]},
                  "translation": rec_x.get("price_reference_translated"), "mode": self.mode,
                  "equity_at_entry": acct["equity"], "lots": gate.size.lots if gate.size else None,
                  "risk_pct": round(gate.size.risk_pct, 3) if gate.size else None, "rr_exec": gate.rr_exec}
        if not gate.approved:
            detail["reason"] = "; ".join(gate.failures())
            self.store.set_execution_state(did, "rejected", detail)
            self.appdb.add_event("executor", "gate_rejected", f"{pair} {did[:8]}: {detail['reason']}"[:300])
            log.info("%s %s rejected by gate: %s", pair, did[:8], detail["reason"])
            return False
        if not self.claim(did, detail):
            log.warning("%s %s was no longer queued (claimed elsewhere or changed) — skipped", pair, did[:8])
            return False
        if self.paper:
            res = self.paper.place(decision_id=did, pair=pair, instrument=exe.key, rec=rec_x, lots=gate.size.lots,
                                   entry=gate.entry, contract_size=spec["contract_size"],
                                   volume_step=spec["volume_step"], volume_min=spec["volume_min"], quote=eq)
        else:
            self._placing = True
            res = self.mt5.place(decision_id=did, symbol=exe_symbol, rec=rec_x, lots=gate.size.lots, entry=gate.entry,
                                 dry_run=False)
            # the broker lists a new position/order a moment later: no second trade may be gated against an account
            # snapshot that does not show this one yet (exposure caps, worst-case daily loss)
            self.mt5_quiet_until = now_ms() + MT5_SETTLE_MS
        detail["backend"] = res
        partial = not res.get("ok") and bool(res.get("placed"))
        if partial:              # some legs are live at the broker: executed (settled from the history), not rejected
            detail["partial"] = f"{len(res['placed'])} leg(s) placed before: {res.get('reason')}"
        self.store.set_execution_state(did, "executed" if res.get("ok") or partial else "rejected", detail)
        self.appdb.add_event("executor", "order" if res.get("ok") else "order_failed",
                             f"{self.mode} {pair} {did[:8]}: {res.get('reason') or res.get('legs') or res.get('placed')}"[:300])
        log.info("%s %s → %s: %s", pair, did[:8], self.mode, "placed" if res.get("ok") else res.get("reason"))
        return bool(res.get("ok") or partial)

    def claim(self, did: str, detail: dict) -> bool:
        """Atomic hand-over to 'executing' (only from queued / not_executed): a decision is never placed twice."""
        con = sqlite3.connect(self.app_db, timeout=10)
        try:
            cur = con.execute("UPDATE ai_decisions SET execution_state='executing', execution_detail=? WHERE id=? "
                              "AND execution_state IN ('queued','not_executed')", (json.dumps(detail, default=str), did))
            con.commit()
            return cur.rowcount == 1
        finally:
            con.close()

    def state_of(self, did: str) -> tuple[str | None, dict]:
        con = sqlite3.connect(f"file:{self.app_db.as_posix()}?mode=ro", uri=True, timeout=10)
        try:
            r = con.execute("SELECT execution_state, execution_detail FROM ai_decisions WHERE id=?", (did,)).fetchone()
        finally:
            con.close()
        return (r[0], json.loads(r[1]) if r[1] else {}) if r else (None, {})

    def reconcile(self, did: str, why: str) -> str:
        """Settle a decision stuck in 'executing' (crash / error after the claim) from what the backend holds."""
        _, detail = self.state_of(did)
        if self.paper:
            legs = self.paper.decision_legs(did)
            placed, found = bool(legs), f"{len(legs)} paper leg(s)"
        else:
            n = {k: len(v) for k, v in self.mt5.existing(did).items()}
            placed, found = any(n.values()), f"MT5 orders {n['orders']}, positions {n['positions']}, deals {n['deals']}"
        state = "executed" if placed else "rejected"
        detail["reconciled"] = f"{why} — backend holds {found}"
        if not placed:
            detail["reason"] = f"interrupted before any order was placed ({why})"
        self.store.set_execution_state(did, state, detail)
        self.appdb.add_event("executor", "reconciled", f"{did[:8]} → {state}: {detail['reconciled']}"[:300])
        self.unreconciled.discard(did)
        return state

    def reconcile_all(self, why: str = "found 'executing' at start-up") -> None:
        con = sqlite3.connect(f"file:{self.app_db.as_posix()}?mode=ro", uri=True, timeout=10)
        try:
            ids = {r[0] for r in con.execute("SELECT id FROM ai_decisions WHERE execution_state='executing'")}
        finally:
            con.close()
        for did in ids | self.unreconciled:
            try:
                self.reconcile(did, why)
            except Exception:  # noqa: BLE001
                log.exception("reconciliation of %s failed — retried later", did[:8])
                self.unreconciled.add(did)

    def process_candidates(self) -> None:
        """Handle every candidate in isolation: one failing decision never blocks or starves the others."""
        now = now_ms()
        for cand in self.candidates():
            if now_ms() < self.mt5_quiet_until:
                return                          # an MT5 order was just placed: the next candidate waits (see handle)
            n, next_ms = self.attempts.get(cand["id"], (0, 0))
            if now < next_ms:
                continue
            try:
                self.handle(cand)
                self.attempts.pop(cand["id"], None)
            except PlacementBusy as exc:
                log.warning("%s %s: %s", cand["pair"], cand["id"][:8], exc)
                return                          # every other candidate would wait for the same lock
            except Exception as exc:  # noqa: BLE001
                log.exception("%s %s: execution attempt %d failed", cand["pair"], cand["id"][:8], n + 1)
                self._handle_failed(cand, exc, n + 1)

    def _handle_failed(self, cand: dict, exc: Exception, n: int) -> None:
        did = cand["id"]
        self.attempts[did] = (n, now_ms() + min(60_000, 2_000 * 2 ** (n - 1)))      # backoff 2 s … 60 s
        try:
            self.appdb.add_event("executor", "handle_error", f"{cand['pair']} {did[:8]} attempt {n}: {exc!r}"[:300])
            if self.state_of(did)[0] == "executing":          # failed after the claim: the backend may have acted
                self.attempts.pop(did, None)
                self.unreconciled.add(did)
                self.reconcile(did, f"error after claim: {exc!r}"[:200])
                return
            try:
                expired = now_ms() >= parse_date_spec(cand["rec"]["valid_until"])
            except Exception:  # noqa: BLE001
                expired = True
            if n >= MAX_HANDLE_ATTEMPTS or expired:
                self.store.set_execution_state(did, "rejected",
                                               {"reason": f"internal error after {n} attempt(s): {exc!r}"[:500]})
                self.attempts.pop(did, None)
        except Exception:  # noqa: BLE001
            log.exception("could not record the failure of %s", did[:8])

    # ------------------------------------------------------------------ paper simulation + outcomes
    def advance_paper(self) -> None:
        """Advance paper legs on the stored real ticks from each instrument's lowest leg watermark (``eval_key``).
        Reads are bounded [start, end) windows through the time index — a few seconds of ticks in steady state,
        exactly the missed ticks after a restart, nothing at all for instruments without pending/open legs."""
        for key, lo in self.paper.watermarks().items():
            try:
                inst = self.reg.get(key)
            except KeyError:
                if key not in self._warned:
                    self._warned.add(key)
                    log.warning("paper legs on %s, which is not a configured instrument — not advanced", key)
                continue
            spec, rd = spec_for(inst, "ticks"), self.reader(key)
            start, now, done = lo // 1000, now_ms(), 0          # key = utc_ms*1000+seq → ticks > lo have time ≥ lo//1000
            while done < MAX_REPLAY_TICKS:
                end = start + TICK_CHUNK_MS
                c = rd.read_range(spec, start, end, ["key", "time_msc", "bid", "ask"])
                m = c["key"] > lo
                if m.any():
                    ticks = [Tick(t, b, a, k) for k, t, b, a in zip(c["key"][m].tolist(), c["time_msc"][m].tolist(),
                                                                   c["bid"][m].tolist(), c["ask"][m].tolist())]
                    done += len(ticks)
                    for ev in self.paper.process(key, ticks):
                        self.appdb.add_event("executor", f"paper_{ev['event']}", json.dumps(ev)[:300])
                if end > now:
                    break
                start = end
        self._settle_paper_outcomes()

    def _settle_paper_outcomes(self) -> None:
        for did in self.paper.unsettled_decisions():
            legs = self.paper.decision_legs(did)
            filled = [l for l in legs if l["fill_price"] is not None]
            if not filled:
                self.store.set_outcome(did, "not_filled", 0.0, 0.0, 0.0)
                continue
            pnl = sum(l["pnl_usd"] or 0.0 for l in filled)
            pcfg = self.s.pairs.get(legs[0]["pair"])
            closed = [l for l in filled if l["close_price"] is not None]
            vol = sum(l["volume"] for l in closed)
            pips = sum(((l["close_price"] - l["fill_price"]) if l["side"] == "BUY" else (l["fill_price"] - l["close_price"]))
                       / pcfg.pip_size * l["volume"] for l in closed) / vol if pcfg and vol else None
            outcome = "closed_profit" if pnl > 0 else "closed_loss" if pnl < 0 else "closed_breakeven"
            self.store.set_outcome(did, outcome, round(pnl, 2), round(pnl / self.paper.start_equity * 100, 3),
                                   round(pips, 1) if pips is not None else None)

    def _settle_mt5_outcomes(self) -> None:
        """P9.6: executed decisions whose legs are all closed / expired at the broker get their real outcome (profit
        + commission + swap from the deal history). A terminal that cannot be asked settles nothing (retried)."""
        con = sqlite3.connect(f"file:{self.app_db.as_posix()}?mode=ro", uri=True, timeout=10)
        try:
            rows = con.execute("SELECT id, ts, pair, execution_detail FROM ai_decisions WHERE execution_state='executed' "
                               "AND outcome IS NULL AND json_extract(execution_detail,'$.mode')=? AND ts>=?",
                               (self.mode, now_ms() - SETTLE_LOOKBACK_DAYS * 86_400_000)).fetchall()
        finally:
            con.close()
        for did, ts, pair, dj in rows:
            detail = json.loads(dj) if dj else {}
            legs = {x["comment"] for x in ((detail.get("backend") or {}).get("placed") or []) if x.get("comment")}
            if now_ms() - ts > (SETTLE_LOOKBACK_DAYS - 1) * 86_400_000 and did not in self._warned:
                self._warned.add(did)
                log.warning("%s %s: still unsettled after %d days — polling stops soon", pair, did[:8],
                            SETTLE_LOOKBACK_DAYS - 1)
            try:
                r = self.mt5.decision_result(did, since_s=ts // 1000 - 86_400, legs=legs or None)
            except Exception as exc:  # noqa: BLE001
                log.warning("outcome of %s not readable yet: %s", did[:8], exc)
                continue
            if r is None:
                continue
            if not r["filled"]:
                self.store.set_outcome(did, "not_filled", 0.0, 0.0, 0.0)
                self.appdb.add_event("executor", "outcome", f"{pair} {did[:8]}: not filled (expired/cancelled)")
                continue
            pnl = r["pnl_usd"]
            base = detail.get("equity_at_entry") or (self.mt5.t.account().balance - pnl)
            pcfg = self.s.pairs.get(pair)
            pips = r["move"] / pcfg.pip_size if r["move"] is not None and pcfg else None
            outcome = "closed_profit" if pnl > 0 else "closed_loss" if pnl < 0 else "closed_breakeven"
            self.store.set_outcome(did, outcome, pnl, round(pnl / base * 100, 3) if base and base > 0 else None,
                                   round(pips, 1) if pips is not None else None)
            self.appdb.add_event("executor", "outcome", f"{pair} {did[:8]}: {outcome} {pnl:+.2f} USD")
            log.info("%s %s settled at the broker: %s %+.2f USD", pair, did[:8], outcome, pnl)

    def virtual_outcomes(self) -> None:
        """P9.8: would the idea have reached TP1 before its SL? Evaluated on real 1m bars of the analysis instrument."""
        con = sqlite3.connect(f"file:{self.app_db.as_posix()}?mode=ro", uri=True)
        try:
            rows = con.execute("SELECT id, pair, recommendation FROM ai_decisions WHERE status='valid' AND "
                               "decision IN ('BUY','SELL') AND virtual_outcome IS NULL").fetchall()
        finally:
            con.close()
        for did, pair, rj in rows:
            if pair not in self.s.enabled_pairs():
                continue
            try:
                rec = json.loads(rj)
                vo, vr = evaluate_virtual(rec, self.reader(self.reg.primary(pair).key), self.reg.primary(pair))
            except Exception:  # noqa: BLE001
                log.exception("virtual outcome of %s failed", did[:8])
                continue
            if vo is not None:
                c = sqlite3.connect(self.app_db, timeout=10)
                c.execute("UPDATE ai_decisions SET virtual_outcome=?, virtual_r=? WHERE id=?", (vo, vr, did))
                c.commit()
                c.close()

    # ------------------------------------------------------------------ loop
    def step(self, housekeeping: bool) -> dict:
        """One loop iteration: paper ticks → candidates → (every minute) virtual outcomes. Returns the account."""
        if self.reconcile_due or self.unreconciled:
            self.reconcile_all("found 'executing' at start-up" if self.reconcile_due else "retry")
            self.reconcile_due = False
        if self.paper:
            self.advance_paper()
        self.process_candidates()
        if housekeeping:
            if self.mt5:
                try:
                    self._settle_mt5_outcomes()
                except Exception:  # noqa: BLE001
                    log.exception("MT5 outcome settlement failed")
            try:
                self.virtual_outcomes()
            except Exception:  # noqa: BLE001
                log.exception("virtual outcomes failed")
        return self.paper.account(self.marks()) if self.paper else self.mt5.account()

    def run(self) -> None:
        self.appdb.set_status("executor", "starting")
        last_virtual = 0.0
        while not self.stop:
            t0 = time.time()
            try:
                acct = self.step(t0 - last_virtual > 60)
                if t0 - last_virtual > 60:
                    last_virtual = t0
                now = now_ms()
                self.loop_errors = [t for t in self.loop_errors if now - t < 5 * MS_PER_MINUTE]
                self.appdb.set_status("executor", "live", last_data_ms=now, error="" if self.clear_error else None,
                                      detail=self.status_detail(acct))
                self.error_since, self.clear_error = None, False
            except Exception as exc:  # noqa: BLE001
                log.exception("executor loop error")
                self._loop_failed(exc)
            time.sleep(max(0.2, 1.0 - (time.time() - t0)))

    def status_detail(self, acct: dict) -> dict:
        """The executor's status row: the live account of this system and what it holds (dashboard, and the
        ``account`` block of the model's payload — :meth:`..analysis.engine.Engine.live_account`)."""
        eq = acct.get("equity")
        today = (acct.get("realized_today_usd") or 0.0) + (acct.get("unrealized_usd") or 0.0)
        try:
            dd = self.update_peak(acct)
        except Exception:  # noqa: BLE001 — the status row must not fail on the shared peak file
            log.exception("account peak update failed")
            dd = None
        return {"mode": self.mode, "trigger": self.s.execution.trigger, "equity": eq, "balance": acct.get("balance"),
                "currency": acct.get("currency", "USD"), "open_positions": acct.get("open_positions"),
                "open_orders": acct.get("open_orders"),
                "today_pnl_pct": round(today / eq * 100, 2) if eq else None,
                "exposure": acct.get("exposure") or [], "account_drawdown": dd.as_detail() if dd else None,
                "errors_last_5min": len(self.loop_errors), "kill_switch": self.kill_switch()}

    def _loop_failed(self, exc: Exception) -> None:
        """Report a failing loop — but only for ERROR_HEARTBEAT_MS: a loop that keeps failing stops refreshing its
        heartbeat, so the supervisor's watchdog restarts the executor instead of trusting a live-looking 'error'."""
        now = now_ms()
        self.loop_errors = [t for t in self.loop_errors if now - t < 5 * MS_PER_MINUTE] + [now]
        self.error_since = self.error_since or now
        self.clear_error = True
        if now - self.error_since > ERROR_HEARTBEAT_MS:
            return
        try:
            self.appdb.set_status("executor", "error", error=repr(exc)[:300], detail={
                "mode": self.mode, "trigger": self.s.execution.trigger, "errors_last_5min": len(self.loop_errors),
                "failing_since": iso(self.error_since)})
        except Exception:  # noqa: BLE001
            log.exception("could not report the executor error status")


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


def _rec_as_of(rec: dict) -> int | None:
    try:
        return parse_date_spec(rec["timestamp"])
    except (KeyError, TypeError, ValueError):
        return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="tradingsystem executor")
    ap.add_argument("--once", action="store_true", help="process pending candidates once and exit")
    args = ap.parse_args(argv)
    s = load_settings()
    setup_from_settings("executor", s)
    ex = Executor(s)
    log.info("executor started: mode=%s trigger=%s", s.execution.mode, s.execution.trigger)
    if args.once:
        ex.reconcile_all()
        ex.process_candidates()
        return 0
    try:
        ex.run()
    except KeyboardInterrupt:
        pass
    return 0


__all__ = ["Executor", "evaluate_virtual", "main", "iso", "Path"]
