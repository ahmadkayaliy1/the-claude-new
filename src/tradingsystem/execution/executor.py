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
from typing import NamedTuple

import numpy as np

from ..ai.store import DecisionStore
from ..analysis import indicators as ind
from ..analysis.frames import load_frame
from ..analysis.structure import analyze_structure
from ..core.filelock import FileLock, locks_dir
from ..core.instruments import InstrumentRegistry, _resolve_symbol
from ..core.logsetup import setup_from_settings
from ..core.sessions import calendar_for
from ..core.settings import Settings, load_settings
from ..core.timeutil import MS_PER_DAY, MS_PER_HOUR, MS_PER_MINUTE, iso, now_ms, parse_date_spec
from ..ingest.common.appdb import AppDB
from ..storage.reader import InstrumentReader
from ..storage.tablespec import spec_for
from .action_gate import ActionContext
from .action_gate import evaluate as gate_action
from .backends.paper import PaperBackend, Tick
from .desk import SHADOW_KEY, ShadowGuard, ShadowViolation, assert_not_shadow, shadow_pairs
from .drawdown import AccountPeak
from .exposure import live_sides
from .management import ActionLog, MT5Legs, PaperLegs, PositionManager
from .metrics import VIRTUAL_HORIZON_MS, MetricsJob, mt5_outcome_detail, paper_outcome_detail, virtual_valid_until
from ..core.tunables import Tunables
from .price_mapping import check_basis, translate
from .risk_gate import ExecContext, evaluate

log = logging.getLogger("executor")
# risk-gate checks a shadow idea is not held to (D-049): the account cannot carry the 1-oz minimum while the model is
# asked for the trade a professional would take; ``desk_ok`` = every OTHER check passed
DESK_WAIVED_CHECKS = ("position_size", "effective_leverage")
SHADOW_BACKLOG_MS = 24 * 3_600_000   # a shadow idea stored while the executor was down is still recorded this long
STOPS_LEVEL_PRICE = {"XAUUSD@": 0.25, "BTCUSD@": 25.0, "ETHUSD@": 2.0}   # P1.5 (stops_level × point); MT5 backend reads live
MT5_SETTLE_MS = 3_000          # after an MT5 placement, before the next candidate is gated (broker listing lag)
SETTLE_LOOKBACK_DAYS = 45      # executed decisions older than this are no longer polled for their outcome
TICK_CHUNK_MS = MS_PER_HOUR          # paper replay window: bounded memory, indexed [start, end) time-range reads
MAX_REPLAY_TICKS = 300_000           # per instrument per loop — a long catch-up resumes on the next loop
MAX_HANDLE_ATTEMPTS = 5              # transient failures of one decision before it is rejected
ERROR_HEARTBEAT_MS = 5 * MS_PER_MINUTE   # a loop failing longer stops refreshing its heartbeat → watchdog restart
PEAK_EVERY_S = 5.0                   # the account peak file is read/updated at most this often by the loop
PLACEMENT_LOCK_WAIT_S = 20.0         # MT5: gate + placement of one system at a time on the account (D-042)
ACTION_WINDOW_MS = 3_600_000         # the model's position actions: decisions of the last hour are handled
SWING_BARS = 150                     # decision-TF bars for the trailing-structure swing
BASIS_QUOTE_MAX_MS = 60_000          # management / model actions: both quotes at most this old for the live basis
BASIS_CACHE_S = 15.0                 # … checked against its 60-min history at most this often per pair
ACTION_SETTLE_MS = 30_000            # a model action sent with an unknown outcome is re-read (not re-sent) this long
ACTION_ERROR_REPEAT_MS = 10 * MS_PER_MINUTE   # a failing decision's action_error event at most this often
PRICED = ("modify_sl", "modify_tp")  # actions with a price (translated by the live basis)
# virtual outcomes (Phase 5 A7): bounded reads — a decision far in the past or a long data gap never loads everything
# up to now (an unbounded read of 90 days of 1m bars took 0.8 s / 22 MB per idea and pass on the production stores)
VIRTUAL_CHUNK_MS = 12 * MS_PER_HOUR          # one read: at most 720 1m bars, [start, end) through the time index
VIRTUAL_GAP_MS = 4 * MS_PER_DAY              # read past the horizon only to find its first bar (weekend / holiday gap)
VIRTUAL_BARS_PER_PASS = 10_000               # 1m bars one housekeeping pass reads (oldest idea first; the idea that
                                             # crosses it finishes its own window, the rest wait for the next pass)
VIRTUAL_RETRY_MS = 15 * MS_PER_MINUTE        # an idea still undecided after its horizon (a gap in the stored bars:
                                             # only a later bar or a backfill decides it) is asked again this often
# executor events the owner is notified of (Phase 4, D-043): kind → (level, title)
NOTIFY_EVENTS = {"mgmt_filled": ("info", "filled"), "mgmt_position_closed": ("info", "position closed"),
                 "mgmt_applied": ("info", "management rule applied"), "action_applied": ("info", "Claude's action applied"),
                 "action_rejected": ("warn", "Claude's action refused"), "mgmt_error": ("warn", "management error"),
                 "action_error": ("warn", "Claude's action could not run")}


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
    shadow_pairs: frozenset[str] = frozenset()      # set in __init__ (a test double built without it has no desk)

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
        # D-049 / B14: a pair whose desk is in shadow never reaches the backend. The executor checks the pair itself
        # (handle, _handle, the management and model-action steps); this wrapper is the second layer - a mutating
        # call that resolves to a shadow pair raises even if a later change forgets the first check
        self.shadow_pairs = shadow_pairs(s)
        if self.shadow_pairs:
            if self.paper is not None:
                self.paper = ShadowGuard(self.paper, self.shadow_pairs, self._mutation_pair)
            if self.mt5 is not None:
                self.mt5 = ShadowGuard(self.mt5, self.shadow_pairs, self._mutation_pair)
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
        # Phase 3 (P9.6 / D-043): the system manages its trades; the model may act on them within the action gate
        self.actions = ActionLog(self.app_db)
        if self.mt5:
            self.legs = self.model_legs = MT5Legs(self.mt5)
        else:
            self.legs = PaperLegs(self.paper, self._leg_quote, self._leg_specs, reason="rule")
            self.model_legs = PaperLegs(self.paper, self._leg_quote, self._leg_specs, reason="model")
        self.manager = PositionManager(s, self.actions, self.legs, self, self._emit)
        # Phase 4 (§3.8): what happened after each decision, measured on real bars (housekeeping, bounded per pass)
        # Phase 4: the pair's adaptive confidence floor (tools/tune.py; never below risk.min_confidence) and the
        # notifier's view of this system (kill switch / drawdown transitions are announced once)
        self.tunables = Tunables(s)
        self._last_switch: bool | None = None
        self._last_tripped: bool | None = None
        self.metrics = MetricsJob(s, self.store, self.reg, self.reader, actions=self.actions,
                                  paper_legs=self.paper.decision_legs if self.paper else None)
        self._mkt: dict[tuple, object] = {}
        self._basis: dict[str, tuple[float, float | None]] = {}      # pair → (checked at, basis or None)
        self._action_errs: dict[str, tuple[str, int]] = {}           # decision → (error, last reported)
        self._virtual_later: dict[str, int] = {}                     # idea → not asked again before (ms)
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

    def _mutation_pair(self, method: str, args: tuple, kwargs: dict) -> str | None:
        """The pair a mutating backend call acts on (:class:`.desk.ShadowGuard`); None when it cannot be told."""
        def arg(i: int, name: str):
            return kwargs[name] if name in kwargs else (args[i] if len(args) > i else None)

        def one(sql: str, val) -> str | None:
            con = sqlite3.connect(f"file:{self.app_db.as_posix()}?mode=ro", uri=True, timeout=10)
            try:
                r = con.execute(sql, (val,)).fetchone()
            finally:
                con.close()
            return r[0] if r else None

        by_symbol = {**self.pair_by_symbol(), **(getattr(self.mt5, "pair_by_symbol", None) or {})}
        if method == "place":
            return kwargs.get("pair") or by_symbol.get(kwargs.get("symbol"))
        if method in ("cancel_decision", "close_legs"):
            return one("SELECT pair FROM ai_decisions WHERE id=?", arg(0, "decision_id"))
        if method == "modify_leg":
            return one("SELECT pair FROM paper_legs WHERE id=?", arg(0, "leg_id"))
        if method in ("modify_sl", "modify_tp"):
            return by_symbol.get(arg(1, "symbol"))
        if method in ("close_position", "cancel_order") and self.mt5 is not None:
            raw, ticket = self.mt5.wrapped, arg(0, "ticket")
            found = raw._position(ticket) if method == "close_position" else next(
                iter(raw._ask(f"order {ticket}", raw.mt5.orders_get(ticket=ticket))), None)
            return by_symbol.get(found.symbol) if found is not None else None
        return None

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
            # D-049: a shadow desk's idea is recorded whatever the trigger mode (nothing is ever sent, so there is no
            # manual approval for it) and also when it was stored while the executor was down (it is gated for the
            # record: an old idea fails its age check and is stored desk_ok false - never silently missing)
            shadow = sorted(self.shadow_pairs & set(self.s.enabled_pairs()))
            shadow_sql = (f" OR (execution_state='not_executed' AND pair IN ({','.join('?' * len(shadow))}) "
                          f"AND ts >= ?)") if shadow else ""
            rows = con.execute(
                f"SELECT id, ts, pair, recommendation, execution_state FROM ai_decisions WHERE status='valid' "
                f"AND decision IN ('BUY','SELL') AND ((execution_state IN ({','.join('?' * len(states))}) "
                f"AND (execution_state='queued' OR ts >= ?)){shadow_sql}) "
                # a shadow idea is final once stored (D-049): never picked up again, whatever the trigger mode
                f"AND (execution_detail IS NULL OR NOT json_valid(execution_detail) "
                f"OR json_extract(execution_detail, '$.{SHADOW_KEY}') IS NULL) ORDER BY ts",
                (*states, self.started, *((*shadow, now_ms() - SHADOW_BACKLOG_MS) if shadow else ()))).fetchall()
        finally:
            con.close()
        return [{"id": r[0], "ts": r[1], "pair": r[2], "rec": json.loads(r[3]), "state": r[4]} for r in rows]

    # ------------------------------------------------------------------ execution
    def handle(self, cand: dict) -> None:
        """Gate and place one candidate. MT5: one system at a time from reading the account to the broker listing
        the new order (``mt5_placement.lock``), so two pairs' systems never both pass the correlated-risk cap on
        the same account snapshot; a busy lock raises (the candidate is retried with backoff).

        A shadow desk's idea (D-049) is decided FIRST, before any lock or backend call: it is gated for the record and
        stored, never placed; an exception there is recorded and fails closed (nothing is sent)."""
        if cand.get("pair") in self.shadow_pairs:
            self._handle_shadow(cand)
            return
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

    def _handle_shadow(self, cand: dict) -> None:
        """One shadow idea: gated in full and stored ``not_executed`` / ``shadow`` (:meth:`_handle`), never sent. Any
        exception ends in an ``ingestion_events`` row and a stored ``shadow`` error record - nothing was sent, and the
        loop goes on with the other candidates. Only when even that record cannot be written the exception goes up to
        :meth:`process_candidates`, whose back-off and reject path also touch nothing but the database."""
        pair, did = cand["pair"], cand["id"]
        try:
            self._handle(cand)
        except Exception as exc:  # noqa: BLE001 - fail closed, never block process_candidates
            log.exception("%s %s: shadow path failed - nothing sent", pair, did[:8])
            self.appdb.add_event("executor", "shadow_error", f"{pair} {did[:8]}: {exc!r}"[:300])
            self.store.set_execution_state(did, "not_executed", {SHADOW_KEY: True, "desk_ok": False,
                                                                  "error": repr(exc)[:300], "mode": self.mode})

    def _news_check(self, pair: str) -> tuple[bool, str] | None:
        """B8: the gate's ``news_blackout`` check from the stored calendar (``data/shared/news_calendar.json``, written by
        the engine) - None for a pair without the blackout (no check, its gate output unchanged). Refused inside a
        release's window while the file is fresh; a stale or unreadable file applies no blackout and says so
        (§3.9.1 a); an unexpected error here refuses (fail closed)."""
        cfg = self.s.pairs[pair].news_blackout
        if not cfg.enabled:
            return None
        try:
            from ..analysis import news
            return news.gate_check(news.state_now(cfg, self.s.paths.shared(), now_ms()))
        except Exception as exc:  # noqa: BLE001 - a money path: unknown = refused
            log.exception("%s: news blackout check failed", pair)
            return False, f"news blackout check failed ({type(exc).__name__}) - refused"

    def _refuse(self, did: str, pair: str, state: str, detail: dict) -> None:
        """A candidate that never reached the gate (not enabled, expired, no quote): a real pair is ``state``; a shadow
        idea stays ``not_executed`` with the shadow record (it is still scored in R from its levels)."""
        if pair in self.shadow_pairs:
            self.store.set_execution_state(did, "not_executed", {SHADOW_KEY: True, "desk_ok": False, **detail})
        else:
            self.store.set_execution_state(did, state, detail)

    def _handle(self, cand: dict) -> bool:
        """True when an order reached the backend (placed or partly placed)."""
        pair, rec, did = cand["pair"], cand["rec"], cand["id"]
        shadow = pair in self.shadow_pairs
        if pair not in self.s.enabled_pairs():
            self.store.set_execution_state(did, "rejected", {"reason": f"pair {pair} is not enabled"})
            return False
        if now_ms() >= parse_date_spec(rec["valid_until"]):
            self._refuse(did, pair, "expired", {"reason": f"expired (valid until {rec['valid_until']})"})
            return False
        exe = self.reg.with_role(pair, "execution")[0]
        exe_symbol = self.exec_reg.with_role(pair, "execution")[0].symbol
        prim = self.reg.primary(pair)
        pcfg = self.s.pairs[pair]
        eq = self.latest_quote(exe.key)
        if eq is None:
            self._refuse(did, pair, "rejected", {"reason": "no execution quote"})
            return False
        basis_ok, basis_reason, rec_x = True, "same instrument", rec
        if prim.key != exe.key:
            pq = self.latest_quote(prim.key)
            if pq is None:
                self._refuse(did, pair, "rejected", {"reason": "no analysis quote for basis"})
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
            account_drawdown_pct=dd.drawdown_pct if dd else None, account_drawdown_tripped=bool(dd and dd.tripped),
            news=self._news_check(pair))
        gate = evaluate(rec_x, pair, ctx, self.s.risk, self.s.risk.correlated_groups,
                        min_confidence=self.tunables.get(pair).min_confidence)
        detail = {"gate": [{"check": n, "ok": ok, "detail": d} for n, ok, d in gate.checks],
                  "executed_levels": {"entry": gate.entry, "stop_loss": rec_x["stop_loss"],
                                      "take_profits": [t["price"] for t in rec_x["take_profits"]]},
                  "translation": rec_x.get("price_reference_translated"), "mode": self.mode,
                  "equity_at_entry": acct["equity"], "lots": gate.size.lots if gate.size else None,
                  "risk_pct": round(gate.size.risk_pct, 3) if gate.size else None, "rr_exec": gate.rr_exec,
                  # the management plan in execution prices (P9.6 executes it on the live legs)
                  "executed_management": rec_x.get("management") or [],
                  # the execution quote's spread the gate judged (decision metrics; execution-instrument units)
                  "spread_at_gate": round(eq.ask - eq.bid, 10)}
        if gate.single_leg_tp is not None:        # one position at the minimum lot: that is the target placed
            detail["executed_levels"]["take_profits"] = [rec_x["take_profits"][gate.single_leg_tp]["price"]]
            detail["single_leg"] = f"TP{gate.single_leg_tp + 1}"
            detail["executed_management"], notes = single_leg_management(detail["executed_management"],
                                                                         rec_x["take_profits"], gate.single_leg_tp)
            if notes:
                detail["management_notes"] = notes
        if shadow:
            # D-049: the full gate ran for the record; the idea is stored and scored, never placed. desk_ok = every
            # check but the two the account's size fails (position_size, effective_leverage) passed AND the gate got
            # as far as sizing (an early return leaves later checks unrun - not a pass)
            failed = [n for n, ok, _ in gate.checks if not ok and n not in DESK_WAIVED_CHECKS]
            detail.update({SHADOW_KEY: True, "desk_ok": gate.size is not None and not failed,
                           "desk_waived": list(DESK_WAIVED_CHECKS), "desk_failed": failed,
                           "gate_approved": gate.approved})
            self.store.set_execution_state(did, "not_executed", detail)
            self.appdb.add_event("executor", "shadow_idea", f"{pair} {did[:8]}: desk_ok={detail['desk_ok']}"
                                 + (f" failed {failed}" if failed else ""))
            log.info("%s %s shadow idea stored (desk_ok=%s, failed %s)", pair, did[:8], detail["desk_ok"], failed)
            return False
        assert_not_shadow(self.shadow_pairs, pair, "placement")      # unreachable for a shadow pair (returned above)
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
        if res.get("ok") or partial:
            self._notify("info", f"{pair} {rec['decision']} placed",
                         f"{self.mode} {rec['order_type']} {gate.size.lots if gate.size else '?'} lots, entry "
                         f"{gate.entry}, SL {rec_x['stop_loss']}, TP {detail['executed_levels']['take_profits']}"
                         + (f" — {detail['partial']}" if partial else ""), key=f"order:{did}", pair=pair)
        else:
            self._notify("warn", f"{pair} {rec['decision']} not placed", str(res.get("reason"))[:300],
                         key=f"order_failed:{did}", pair=pair)
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
            split = paper_outcome_detail(legs)          # how each leg ended, when (decision metrics)
            if not filled:
                self.store.set_outcome(did, "not_filled", 0.0, 0.0, 0.0, detail=split)
                self.appdb.add_event("executor", "outcome", f"{legs[0]['pair']} {did[:8]}: not filled (expired/cancelled)")
                continue
            pnl = sum(l["pnl_usd"] or 0.0 for l in filled)
            pcfg = self.s.pairs.get(legs[0]["pair"])
            closed = [l for l in filled if l["close_price"] is not None]
            vol = sum(l["volume"] for l in closed)
            pips = sum(((l["close_price"] - l["fill_price"]) if l["side"] == "BUY" else (l["fill_price"] - l["close_price"]))
                       / pcfg.pip_size * l["volume"] for l in closed) / vol if pcfg and vol else None
            outcome = "closed_profit" if pnl > 0 else "closed_loss" if pnl < 0 else "closed_breakeven"
            self.store.set_outcome(did, outcome, round(pnl, 2), round(pnl / self.paper.start_equity * 100, 3),
                                   round(pips, 1) if pips is not None else None, detail=split)
            # like the MT5 settlement: the outcome wakes the model and reaches the owner
            self.appdb.add_event("executor", "outcome", f"{legs[0]['pair']} {did[:8]}: {outcome} {pnl:+.2f} USD")
            self._notify("info", f"{legs[0]['pair']} trade closed", f"paper {did[:8]}: {outcome} {pnl:+.2f} USD",
                         key=f"outcome:{did}", pair=legs[0]["pair"])

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
            # the broker's split (commission / swap / fee, close time, how each leg ended) exists only here
            split = mt5_outcome_detail(r)
            if not r["filled"]:
                self.store.set_outcome(did, "not_filled", 0.0, 0.0, 0.0, detail=split)
                self.appdb.add_event("executor", "outcome", f"{pair} {did[:8]}: not filled (expired/cancelled)")
                continue
            pnl = r["pnl_usd"]
            base = detail.get("equity_at_entry") or (self.mt5.t.account().balance - pnl)
            pcfg = self.s.pairs.get(pair)
            pips = r["move"] / pcfg.pip_size if r["move"] is not None and pcfg else None
            outcome = "closed_profit" if pnl > 0 else "closed_loss" if pnl < 0 else "closed_breakeven"
            self.store.set_outcome(did, outcome, pnl, round(pnl / base * 100, 3) if base and base > 0 else None,
                                   round(pips, 1) if pips is not None else None, detail=split)
            self.appdb.add_event("executor", "outcome", f"{pair} {did[:8]}: {outcome} {pnl:+.2f} USD")
            log.info("%s %s settled at the broker: %s %+.2f USD", pair, did[:8], outcome, pnl)
            self._notify("info", f"{pair} trade closed", f"{self.mode} {did[:8]}: {outcome} {pnl:+.2f} USD",
                         key=f"outcome:{did}", pair=pair)

    def virtual_outcomes(self) -> None:
        """P9.8: would the idea have reached TP1 before its SL? Evaluated on real 1m bars of the analysis instrument
        (:func:`virtual_walk`: bounded reads). Oldest idea first, at most ``VIRTUAL_BARS_PER_PASS`` bars per pass (the
        rest wait for the next pass); an idea still undecided after its horizon, or failing, is asked again only every
        ``VIRTUAL_RETRY_MS`` — it cannot hold up the newer ones."""
        con = sqlite3.connect(f"file:{self.app_db.as_posix()}?mode=ro", uri=True)
        try:
            rows = con.execute("SELECT id, pair, recommendation FROM ai_decisions WHERE status='valid' AND "
                               "decision IN ('BUY','SELL') AND virtual_outcome IS NULL ORDER BY ts, id").fetchall()
        finally:
            con.close()
        now, enabled, budget = now_ms(), set(self.s.enabled_pairs()), VIRTUAL_BARS_PER_PASS
        later = {k: v for k, v in (getattr(self, "_virtual_later", None) or {}).items() if v > now}
        self._virtual_later = later
        for did, pair, rj in rows:
            if pair not in enabled or later.get(did, 0) > now:
                continue
            if budget <= 0:
                log.debug("virtual outcomes: bar budget of this pass spent - %s and newer wait", did[:8])
                break
            try:
                rec = json.loads(rj)
                w = virtual_walk(rec, self.reader(self.reg.primary(pair).key), self.reg.primary(pair), now)
            except Exception:  # noqa: BLE001
                log.exception("virtual outcome of %s failed", did[:8])
                later[did] = now + VIRTUAL_RETRY_MS
                continue
            budget -= w.bars
            vo, vr = w.outcome, w.r
            if vo is None and now > w.horizon_ms:
                later[did] = now + VIRTUAL_RETRY_MS
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
        # protective work runs even when a kill switch is on (it only ever reduces risk)
        for step in (self.process_actions, self.manage_positions):
            try:
                step()
            except Exception as exc:  # noqa: BLE001 — never blocks the candidates or the heartbeat
                log.exception("%s failed", step.__name__)
                self._emit("mgmt_error", {"text": f"{step.__name__} failed: {exc!r}"[:200]})
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
            try:                    # after the outcomes it scores; bounded per pass, never blocks the candidates
                if (job := getattr(self, "metrics", None)) is not None:
                    job.run()
            except Exception:  # noqa: BLE001
                log.exception("decision metrics failed")
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
        switch = self.kill_switch()
        self._announce_transitions(switch, dd)
        return {"mode": self.mode, "trigger": self.s.execution.trigger, "equity": eq, "balance": acct.get("balance"),
                "currency": acct.get("currency", "USD"), "open_positions": acct.get("open_positions"),
                "open_orders": acct.get("open_orders"),
                "today_pnl_pct": round(today / eq * 100, 2) if eq else None,
                "exposure": acct.get("exposure") or [], "account_drawdown": dd.as_detail() if dd else None,
                "errors_last_5min": len(self.loop_errors), "kill_switch": switch,
                "shadow_pairs": sorted(self.shadow_pairs & set(self.s.enabled_pairs()))}

    def _announce_transitions(self, switch: bool, dd) -> None:
        """Kill switch on/off and the account drawdown stop, announced once per change (not at start-up)."""
        scope = self.s.paths.instance or "all"
        if self._last_switch is not None and switch != self._last_switch:
            self._notify("critical" if switch else "warn", f"Kill switch {'ON' if switch else 'OFF'} ({scope})",
                         "new orders are blocked; open trades keep their stops and management" if switch
                         else "new orders are allowed again", key=f"kill_switch_{scope}_{'on' if switch else 'off'}")
        self._last_switch = switch
        tripped = bool(dd and dd.tripped)
        if self._last_tripped is not None and tripped and not self._last_tripped:
            det = dd.as_detail() if hasattr(dd, "as_detail") else {}
            self._notify("critical", "Account drawdown stop", f"every system stops opening trades: {det}"[:400],
                         key=f"drawdown:{det.get('account') or scope}")
        self._last_tripped = tripped

    def _notify(self, level: str, title: str, text: str, *, key: str | None = None, pair: str | None = None) -> None:
        """Log + toast + Telegram (core.notify: non-blocking, never raises)."""
        try:
            from ..core.notify import notify
            notify(self.s, level, title, text, key=key, pair=pair)
        except Exception:  # noqa: BLE001 — a notification never touches the trading loop
            log.debug("notify failed", exc_info=True)

    # ------------------------------------------------------------------ Phase 3: management and model actions
    def _emit(self, kind: str, payload: dict) -> None:
        """Executor event (fills, closes, actions) — the engine wakes the model on some of them; the owner is told
        about fills, closes, rule executions and the model's actions (NOTIFY_EVENTS)."""
        self.appdb.add_event("executor", kind, json.dumps(payload, default=str)[:1500])
        if kind in NOTIFY_EVENTS:
            level, title = NOTIFY_EVENTS[kind]
            pair = payload.get("pair")
            ident = payload.get("leg") or payload.get("decision") or ""
            if kind == "mgmt_applied":
                # one key per rule of the plan (its index) and leg, without the new stop value: a trailing rule moves
                # the stop on every decision bar, and those moves collapse into one notification per
                # notify.dedupe_minutes (the log and the events keep each one); two rules of the same action (two
                # partial closes) stay two notifications. A payload without the index falls back to the action.
                rule = payload.get("rule_idx", payload.get("rule"))
                key = f"{kind}:{payload.get('decision')}:{payload.get('leg')}:{rule}"
            else:
                key = f"{kind}:{payload.get('decision')}:{ident}:{payload.get('text', '')[:60]}"
            self._notify(level, f"{pair or ''} {title}".strip(), str(payload.get("text") or payload)[:400],
                         key=key, pair=pair)

    def _leg_quote(self, key: str) -> Tick | None:
        return self.latest_quote(key)

    def _leg_specs(self, key: str) -> dict:
        inst = self.reg.get(key)
        c = inst.contract or {}
        return {"stops_level": STOPS_LEVEL_PRICE.get(inst.symbol, 0.0), "tick_size": c.get("tick_size", 0.01),
                "volume_min": c.get("volume_min", 0.01), "volume_step": c.get("volume_step", 0.01)}

    def _live_basis(self, pair: str) -> float | None:
        """Execution mid − analysis mid now, for the model's actions and the management levels (0 when both are the
        same instrument). None — the actions and the rules that need it wait — when a quote is older than
        BASIS_QUOTE_MAX_MS or the basis deviates from its 60-min history (``check_basis``, as for new orders)."""
        prim, exe = self.reg.primary(pair), self.reg.with_role(pair, "execution")[0]
        if prim.key == exe.key:
            return 0.0
        t, basis = self._basis.get(pair, (-1e18, None))
        if time.monotonic() - t < BASIS_CACHE_S:
            return basis
        pq, eq = self.latest_quote(prim.key, BASIS_QUOTE_MAX_MS), self.latest_quote(exe.key, BASIS_QUOTE_MAX_MS)
        basis = None
        if pq is not None and eq is not None:
            bc = check_basis((pq.bid + pq.ask) / 2, (eq.bid + eq.ask) / 2, self.basis_history(pair),
                             self.s.execution.max_basis_deviation_pct)
            basis = bc.basis if bc.ok else None
            if not bc.ok and f"basis:{pair}" not in self._warned:
                self._warned.add(f"basis:{pair}")
                log.warning("%s: %s — model actions and price-based management wait", pair, bc.reason)
        self._basis[pair] = (time.monotonic(), basis)
        return basis

    def market_open(self, pair: str) -> bool:
        """MarketView: whether the execution venue trades this pair now (nothing is sent while it is closed)."""
        exe = self.reg.with_role(pair, "execution")[0]
        return calendar_for(exe.venue, exe.symbol, self.s.pairs[pair].asset_class).is_open(now_ms())

    def decision_bar_open_ms(self, pair: str) -> int | None:
        """MarketView: open time of the latest closed decision-TF bar."""
        tf = self.s.pairs[pair].decision_timeframe
        return tf.floor(now_ms()) - tf.ms

    def _decision_frame(self, pair: str):
        """The decision-TF frame of the analysis instrument once it holds the latest closed bar (cached for that
        bar); None while that bar is not stored yet (the first seconds after a close) — the rules needing it wait
        instead of reading the bar before (a candle_close must never judge a candle older than the trade)."""
        bar = self.decision_bar_open_ms(pair)
        key = ("frame", pair, bar)
        if key not in self._mkt:
            prim = self.reg.primary(pair)
            tf = self.s.pairs[pair].decision_timeframe
            fr = load_frame(self.reader(prim.key), prim, tf, SWING_BARS, now_ms())
            if not len(fr) or int(fr.open_time[-1]) != bar:
                return None                                  # not stored yet: asked again on the next loop
            self._mkt = {k: v for k, v in self._mkt.items() if k[2] == bar}      # drop older bars' entries
            self._mkt[key] = fr
        return self._mkt[key]

    def swing(self, pair: str, side: str) -> float | None:
        """MarketView: the last confirmed swing low/high of the decision timeframe, in execution prices."""
        basis = self._live_basis(pair)
        if basis is None:
            return None
        bar = self.decision_bar_open_ms(pair)
        key = ("swing", pair, bar)
        if key not in self._mkt:
            fr = self._decision_frame(pair)
            if fr is None or len(fr) < 20:
                return None
            st = analyze_structure(fr.high, fr.low, fr.close, ind.atr(fr.high, fr.low, fr.close))
            self._mkt[key] = (st.last_low.price if st.last_low else None, st.last_high.price if st.last_high else None)
        lo, hi = self._mkt[key]
        v = lo if side == "low" else hi
        return None if v is None else float(v) + basis

    def decision_bar_close(self, pair: str) -> float | None:
        """MarketView (optional): the latest closed decision bar's close, in execution prices."""
        basis = self._live_basis(pair)
        fr = self._decision_frame(pair)
        if basis is None or fr is None or not len(fr):
            return None
        return float(fr.close[-1]) + basis

    def _open_decisions(self) -> list[dict]:
        """Executed decisions of this mode that are not settled yet (their legs may still be live)."""
        con = sqlite3.connect(f"file:{self.app_db.as_posix()}?mode=ro", uri=True, timeout=10)
        try:
            rows = con.execute("SELECT id, pair, recommendation, execution_detail FROM ai_decisions WHERE "
                               "execution_state='executed' AND outcome IS NULL AND ts>=? AND "
                               "json_extract(execution_detail,'$.mode')=?",
                               (now_ms() - SETTLE_LOOKBACK_DAYS * MS_PER_DAY, self.mode)).fetchall()
        finally:
            con.close()
        enabled = set(self.s.enabled_pairs()) - self.shadow_pairs      # a shadow desk has nothing to manage (D-049)
        return [{"id": r[0], "pair": r[1], "recommendation": json.loads(r[2]) if r[2] else {},
                 "execution_detail": json.loads(r[3]) if r[3] else {}} for r in rows if r[1] in enabled]

    def manage_positions(self) -> None:
        """P9.6: apply every open trade's declared management rules (breakeven, trailing, partials, time stop)."""
        decisions = self._open_decisions()
        for d in decisions:
            assert_not_shadow(self.shadow_pairs, d["pair"], "position management")
        if decisions:
            self.manager.manage(decisions, now_ms())

    def _action_status(self, source_decision: str, seq: int, target: str, leg: str) -> str | None:
        return self._action_row(source_decision, seq, target, leg)[0]

    def _action_row(self, source_decision: str, seq: int, target: str, leg: str) -> tuple[str | None, dict, int]:
        """(status, detail, ts) of one model action row; (None, {}, 0) when there is none."""
        con = sqlite3.connect(f"file:{self.app_db.as_posix()}?mode=ro", uri=True, timeout=10)
        try:
            r = con.execute("SELECT status, detail, ts FROM position_actions WHERE source='model' AND "
                            "source_decision=? AND seq=? AND target_decision=? AND leg=?",
                            (source_decision, seq, target, leg)).fetchone()
        except sqlite3.OperationalError:
            return None, {}, 0
        finally:
            con.close()
        if not r:
            return None, {}, 0
        try:
            det = json.loads(r[1]) if r[1] else {}
        except ValueError:
            det = {}
        return r[0], det, int(r[2] or 0)

    def _deferred_sources(self, pair: str) -> set[str]:
        """Decisions of ``pair`` with a model stop move still deferred (kept past ACTION_WINDOW_MS)."""
        con = sqlite3.connect(f"file:{self.app_db.as_posix()}?mode=ro", uri=True, timeout=10)
        try:
            rows = con.execute("SELECT DISTINCT source_decision FROM position_actions WHERE source='model' AND "
                               "pair=? AND status='deferred'", (pair,)).fetchall()
        except sqlite3.OperationalError:
            return set()
        finally:
            con.close()
        return {r[0] for r in rows}

    def _resolve_target(self, pair: str, short_id: str) -> str | None:
        """The executed decision of ``pair`` whose id starts with ``short_id`` — exactly one, else None."""
        con = sqlite3.connect(f"file:{self.app_db.as_posix()}?mode=ro", uri=True, timeout=10)
        try:
            rows = con.execute("SELECT id FROM ai_decisions WHERE substr(id, 1, ?)=? AND pair=? AND "
                               "execution_state='executed'", (len(short_id), short_id, pair)).fetchall()
        finally:
            con.close()
        return rows[0][0] if len(rows) == 1 else None

    def process_actions(self) -> None:
        """D-043: the model's ``position_actions`` on its live trades — each checked by the action gate, applied through
        the leg backend, recorded in ``position_actions`` and announced as an event (which wakes the model)."""
        if not self.s.execution.position_actions.enabled:
            return
        now = now_ms()
        for pair in self.s.enabled_pairs():
            if pair in self.shadow_pairs:
                continue                       # D-049: a shadow desk's actions are never applied (there is nothing live)
            since, waiting = now - ACTION_WINDOW_MS, self._deferred_sources(pair)
            for d in self.store.pending_actions(pair, 0 if waiting else since):
                if d["ts"] < since and d["id"] not in waiting:
                    continue
                try:
                    if self._actions_of(d, pair, now):
                        self.store.set_actions_state(d["id"], "done")
                    self._action_errs.pop(d["id"], None)
                except Exception as exc:  # noqa: BLE001 — one decision's actions never block another's
                    # an infrastructure error (terminal down, no quote) is not a refusal: it must not wake the model
                    # every loop — reported once per distinct error, then every ACTION_ERROR_REPEAT_MS
                    sig, prev = repr(exc)[:120], self._action_errs.get(d["id"])
                    if prev is not None and prev[0] == sig and now - prev[1] < ACTION_ERROR_REPEAT_MS:
                        continue
                    self._action_errs[d["id"]] = (sig, now)
                    log.warning("%s %s: position actions failed (retried every loop): %r", pair, d["id"][:8], exc,
                                exc_info=prev is None)
                    self._emit("action_error", {"pair": pair, "decision": d["id"][:8],
                                                "text": f"{pair} actions of {d['id'][:8]} could not run yet: {exc!r}"[:200]})

    def _actions_of(self, d: dict, pair: str, now: int) -> bool:
        """Handle one decision's actions; True when every action has a final result.

        An action is planned ONCE: its per-leg plan (legs, volumes, prices) is written ahead of the first send and
        carried out exactly — a restart or an unknown outcome re-reads the legs and re-sends only what did not take
        effect, never re-plans a fraction on a reduced position. Only a waiting (deferred) stop is re-checked by the
        gate every loop. Priced actions (a stop, a target) need the live basis; close and cancel_order do not."""
        assert_not_shadow(self.shadow_pairs, pair, "position actions")
        rec, d = d["rec"], {**d, "pair": pair}
        acts = rec.get("position_actions") or []
        if not acts:
            return True
        exe = self.reg.with_role(pair, "execution")[0]
        rec_x, no_basis = rec, False
        if self.reg.primary(pair).key != exe.key and any(a.get("action") in PRICED for a in acts):
            basis = self._live_basis(pair)
            if basis is None:
                no_basis = True                               # priced actions wait; close / cancel need no price
            else:
                rec_x = translate(rec, basis, (exe.contract or {}).get("tick_size", 0.01))
        cfg = self.s.execution.position_actions
        rec_ts = parse_date_spec(rec["timestamp"])
        market_open = calendar_for(exe.venue, exe.symbol, self.s.pairs[pair].asset_class).is_open(now)
        done = True
        for seq, a in enumerate(rec_x.get("position_actions") or []):
            if seq >= cfg.max_per_decision:
                break
            if no_basis and a.get("action") in PRICED:
                done = False
                continue
            short = a["target"]["decision"]
            target = self._resolve_target(pair, short)
            if target is None:
                if self._action_status(d["id"], seq, short, "-") is None:
                    self.actions.record("model", d["id"], seq, short, "-", a["action"], a, "rejected",
                                        {"reason": f"no single executed decision {short} of {pair}"}, pair=pair)
                    self._emit("action_rejected", {"pair": pair, "decision": d["id"][:8],
                                                   "text": f"{a['action']} on {short}: unknown or ambiguous target"})
                continue
            legs = self.model_legs.legs_of(target)
            rows = self.actions.action_rows("model", d["id"], seq, target)
            planned = any((r[1] or {}).get("pending") for r in rows.values())   # sent (in part) before
            if any(r[0] == "pending" for r in rows.values()):
                ok = self._resume_plan(d, seq, target, a, legs, rows, now)
                done = ok and done
                rows = self.actions.action_rows("model", d["id"], seq, target)
                if any(r[0] == "pending" for r in rows.values()):
                    continue                                  # still settling / re-read on the next loops
            by_key = {lg.key: lg for lg in legs}
            for k, (st, det, ts) in list(rows.items()):
                if st == "deferred" and (k not in by_key or by_key[k].kind == "closed"):
                    self.actions.record("model", d["id"], seq, target, k, a["action"], a, "skipped",
                                        {**det, "reason": "the leg closed before the stop could be moved"}, pair=pair)
                    rows[k] = ("skipped", det, ts)
            if rows and all(r[0] in ("applied", "rejected", "skipped") for r in rows.values()):
                continue            # carried out, refused or superseded: never a second pass through the gate
            ctx = ActionContext(now_ms=now, pair=pair, rec_ts_ms=rec_ts, max_age_s=self.s.risk.max_recommendation_age_s,
                                market_open=market_open, cfg=cfg,
                                applied_today=self.actions.applied_today(pair, "model"),
                                last_sl_change_ms=self.actions.last_sl_change_ms,
                                deferred_legs=frozenset(k for k, r in rows.items() if r[0] == "deferred"))
            to_send = []
            for plan in gate_action(a, legs, self.model_legs.venue, ctx):
                leg_key = plan.leg.key if plan.leg else "-"
                if (rows.get(leg_key) or (None,))[0] in ("applied", "rejected", "skipped"):
                    continue
                if planned and (rows.get(leg_key) or (None,))[0] != "deferred":
                    continue                # once planned, only the legs still waiting are checked again
                checks = [{"check": n, "ok": ok, "detail": x} for n, ok, x in plan.checks]
                if plan.status == "deferred":
                    self.actions.record("model", d["id"], seq, target, leg_key, a["action"], a, "deferred",
                                        {"checks": checks, "reason": plan.reason}, pair=pair)
                    if plan.op == "set_sl":
                        self.actions.supersede_deferred(leg_key, d["id"], d["ts"])
                    done = False
                    continue
                if plan.status != "apply":
                    self.actions.record("model", d["id"], seq, target, leg_key, a["action"], a, "rejected",
                                        {"checks": checks, "reason": plan.reason}, pair=pair)
                    self._emit("action_rejected", {"pair": pair, "decision": d["id"][:8], "target": target[:8],
                                                   "text": f"{a['action']} on {target[:8]} refused: {plan.reason}"[:200]})
                    continue
                lg = plan.leg
                before = {"op": plan.op, "value": plan.value, "volume": plan.volume, "leg_volume_before": lg.volume,
                          "sl_before": lg.sl, "tp_before": lg.tp, "kind_before": lg.kind}
                to_send.append((plan, checks, before))
            # write-ahead the whole plan first: a crash between two legs resumes exactly this plan
            for plan, checks, before in to_send:
                self.actions.record("model", d["id"], seq, target, plan.leg.key, a["action"], a, "pending",
                                    {"checks": checks, "pending": before}, pair=pair)
            for plan, checks, before in to_send:
                done = self._send_model_plan(d, seq, target, a, plan, checks, before) and done
        return done

    def _send_model_plan(self, d: dict, seq: int, target: str, a: dict, plan, checks: list, before: dict) -> bool:
        """Send one planned leg action (its 'pending' row is written); True when it ended final."""
        pair, leg_key = d["pair"], plan.leg.key
        assert_not_shadow(self.shadow_pairs, pair, "position action")
        res = self._apply_plan(plan)
        if res.status == "unknown":                   # sent, no confirmation: stays pending, re-read first
            return False
        if res.status == "deferred":                  # the venue said "not now" (freeze level …): the gate re-checks
            self.actions.record("model", d["id"], seq, target, leg_key, a["action"], a, "deferred",
                                {"checks": checks, "result": res.detail, "retcode": res.retcode}, pair=pair)
            return False
        status = "applied" if res.status == "applied" else "rejected"
        self.actions.record("model", d["id"], seq, target, leg_key, a["action"], a, status,
                            {"checks": checks, "pending": before, "result": res.detail, "retcode": res.retcode},
                            pair=pair)
        if status == "applied" and plan.op == "set_sl":
            self.actions.supersede_deferred(leg_key, d["id"], d["ts"])
        kind = "action_applied" if status == "applied" else "action_rejected"
        self._emit(kind, {"pair": pair, "decision": d["id"][:8], "target": target[:8], "leg": leg_key,
                          "text": f"{a['action']} on {target[:8]} leg {leg_key}: {status} — {res.detail}"[:200]})
        return True

    def _resume_plan(self, d: dict, seq: int, target: str, a: dict, legs: list, rows: dict, now: int) -> bool:
        """An action planned (and partly sent) before: reconcile each pending leg against the venue and re-send
        only what did not take effect, with the recorded volume / price. True when every leg is final."""
        from .action_gate import LegPlan
        pair, by_key, done = d["pair"], {lg.key: lg for lg in legs}, True
        for k, (st, det, ts) in rows.items():
            if st != "pending":
                continue
            pend, lg = det.get("pending") or {}, by_key.get(k)
            if lg is not None and _action_took_effect(det, lg):
                self.actions.record("model", d["id"], seq, target, k, a["action"], a, "applied",
                                    {**det, "reconciled": "found applied at the venue"}, pair=pair)
                self._emit("action_applied", {"pair": pair, "decision": d["id"][:8], "target": target[:8], "leg": k,
                                              "text": f"{a['action']} on {target[:8]} leg {k}: applied (reconciled)"})
                if pend.get("op") == "set_sl":
                    self.actions.supersede_deferred(k, d["id"], d["ts"])
                continue
            if now - ts < ACTION_SETTLE_MS:
                done = False                               # the venue may still be processing it: re-read later
                continue
            if lg is None or lg.kind not in ("position", "order"):
                gone = pend.get("op") in ("close", "cancel")
                self.actions.record("model", d["id"], seq, target, k, a["action"], a,
                                    "applied" if gone else "skipped",
                                    {**det, "reason": "the leg is no longer live" + (" — the close took effect"
                                                                                     if gone else "")}, pair=pair)
                continue
            plan = LegPlan(lg, pend.get("op"), value=pend.get("value"), volume=pend.get("volume"))
            self.actions.record("model", d["id"], seq, target, k, a["action"], a, "pending", det, pair=pair)
            done = self._send_model_plan(d, seq, target, a, plan, det.get("checks") or [], pend) and done
        return done

    def _apply_plan(self, plan):
        lg = plan.leg
        if plan.op == "set_sl":
            return self.model_legs.set_sl(lg, plan.value)
        if plan.op == "set_tp":
            return self.model_legs.set_tp(lg, plan.value)
        if plan.op == "close":
            return self.model_legs.close(lg, plan.volume, reason="model")
        return self.model_legs.cancel(lg, reason="model")

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


def _action_took_effect(det: dict, lg) -> bool:
    """A model action recorded as pending (sent, outcome unknown) that the leg already shows."""
    pend = det.get("pending") or {}
    op, val = pend.get("op"), pend.get("value")
    if op in ("set_sl", "set_tp") and val is not None:
        cur = lg.sl if op == "set_sl" else lg.tp
        return cur is not None and abs(cur - float(val)) <= 1e-6 * max(1.0, abs(float(val)))
    if op == "close":
        return lg.kind == "closed" or lg.volume <= float(pend.get("leg_volume_before") or 0) \
            - float(pend.get("volume") or 0) + 1e-9
    if op == "cancel":
        return lg.kind == "closed"                 # an order that filled meanwhile is not cancelled
    return False


def single_leg_management(rules: list[dict], tps: list[dict], placed: int) -> tuple[list[dict], list[str]]:
    """The management plan of a trade placed as ONE position at take-profit ``placed`` (0-based) because the minimum
    lot cannot be split: no other take-profit leg exists, so ``tp_hit k`` could never fire. A nearer target k
    becomes ``price_reached`` at that target's (execution) price — the moment its leg would have closed; ``tp_hit``
    of the placed or a farther target is dropped (the position is gone by then). Returns (rules, notes)."""
    out, notes = [], []
    for r in rules or []:
        if r.get("trigger") != "tp_hit" or r.get("value") is None:
            out.append(r)
            continue
        k = int(r["value"]) - 1
        if 0 <= k < placed and k < len(tps):
            out.append({**r, "trigger": "price_reached", "value": tps[k]["price"]})
            notes.append(f"{r.get('action')} on TP{k + 1} hit → price_reached {tps[k]['price']} (single leg at "
                         f"TP{placed + 1})")
        else:
            notes.append(f"{r.get('action')} on TP{k + 1} hit dropped: the single leg at TP{placed + 1} closes the "
                         "whole trade")
    return out, notes


class VirtualWalk(NamedTuple):
    outcome: str | None          # tp1_first | sl_first | not_triggered | unresolved_24h; None = undecided
    r: float | None
    bars: int                    # 1m bars read (the pass budget)
    horizon_ms: int              # valid_until (capped) + 24 h: undecided after it = a gap in the stored bars


def virtual_walk(rec: dict, reader: InstrumentReader, inst, now: int | None = None) -> VirtualWalk:
    """P9.8 on bounded reads (Phase 5 A7): walk the analysis instrument's 1m bars from the cycle time in
    ``VIRTUAL_CHUNK_MS`` windows [start, end) and stop at the bar that decides the idea. The window ends at the horizon
    (``valid_until`` — at most ``metrics.VIRTUAL_MAX_VALID_MS`` after the cycle — + 24 h) plus ``VIRTUAL_GAP_MS`` (only
    to find the first bar after the horizon across a weekend or holiday), and never after ``now``: a decision far in
    the past or a long gap in the bars reads at most its own window, never everything up to now. The decision rules
    are unchanged: fill, then stop before target on one bar, ``not_triggered`` at valid_until, ``unresolved_24h`` on
    the first bar after the horizon."""
    ts = parse_date_spec(rec["timestamp"])
    vu = virtual_valid_until(parse_date_spec(rec["valid_until"]), ts)
    horizon = vu + VIRTUAL_HORIZON_MS
    now = now_ms() if now is None else int(now)
    buy = rec["decision"] == "BUY"
    e = rec["entry"]
    entry = e.get("price") or ((e["range_max"] if buy else e["range_min"]) if e.get("range_min") is not None else None)
    sl, tp1 = rec["stop_loss"], rec["take_profits"][0]["price"]
    filled = rec["order_type"] == "MARKET"
    spec = spec_for(inst, "candles", inst.timeframes[0])
    end = min(horizon + VIRTUAL_GAP_MS, now + 1)          # a stored bar never opens after now
    start, bars = ts, 0
    while start < end:
        hi = min(start + VIRTUAL_CHUNK_MS, end)
        c = reader.read_range(spec, start, hi, ["open_time", "high", "low"])
        t = np.asarray(c["open_time"])
        keep = (t >= start) & (t < hi)                    # exactly this chunk, whatever the reader returned
        bars += int(keep.sum())
        for t_, h, l in zip(t[keep].tolist(), np.asarray(c["high"])[keep].tolist(),
                            np.asarray(c["low"])[keep].tolist()):
            if not filled:
                if t_ >= vu:
                    return VirtualWalk("not_triggered", 0.0, bars, horizon)
                hit = {"BUY_LIMIT": l <= entry, "BUY_STOP": h >= entry,
                       "SELL_LIMIT": h >= entry, "SELL_STOP": l <= entry}[rec["order_type"]]
                if not hit:
                    continue
                filled = True
            sl_hit = (l <= sl) if buy else (h >= sl)
            tp_hit = (h >= tp1) if buy else (l <= tp1)
            if sl_hit:                     # same-bar ambiguity resolved conservatively (stop first)
                return VirtualWalk("sl_first", -1.0, bars, horizon)
            if tp_hit:
                r = abs(tp1 - entry) / abs(entry - sl) if entry and entry != sl else None
                return VirtualWalk("tp1_first", round(r, 2) if r is not None else None, bars, horizon)
            if t_ > horizon:
                return VirtualWalk("unresolved_24h", 0.0, bars, horizon)
        start = hi
    return VirtualWalk(None, None, bars, horizon)


def evaluate_virtual(rec: dict, reader: InstrumentReader, inst,
                     now: int | None = None) -> tuple[str | None, float | None]:
    """(virtual outcome, R) of a trade idea — :func:`virtual_walk` without its read statistics."""
    w = virtual_walk(rec, reader, inst, now)
    return w.outcome, w.r


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


__all__ = ["Executor", "VirtualWalk", "evaluate_virtual", "main", "iso", "Path", "virtual_walk"]
