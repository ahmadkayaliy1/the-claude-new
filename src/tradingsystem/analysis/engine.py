"""Decision engine service: ``python -m tradingsystem engine`` (P8.5–P8.7 wiring).

Loop (every 2 s): for each pair, once the decision-timeframe candle that just closed is stored, build the
snapshot, evaluate the trigger policy (setup events / next_review / idle), and dispatch an AI cycle for the
triggered pairs. Market-closed pairs are skipped.

* A decision bar that is not stored is never analysed: the pair waits (reported once) until it arrives or the
  next bar closes; a payload that fails the data gate (``snapshot.data_problems``) is stored as 'skipped'
  without an AI call (stall F5, F11).
* AI cycles run as background tasks bounded by ``ai.cycle_deadline_s``; a pair is never dispatched twice at
  once. The heartbeat in ``collector_status`` ("engine") has its own task (every 10 s), so a long Claude Code
  call can never make the supervisor kill the engine (F2).
* Calls are rationed: per-pair spacing that doubles after each failed cycle, ``next_review`` at most once per
  decision and never sooner than ``ai.review_floor_minutes`` (F1), and quota pressure — below 50 % of the
  provider's daily cap only strong setups and reviews, below 20 % only reviews, none when it is used up (F8).

CLI:
  engine                         run the service
  engine --once [--pairs A,B]    one AI cycle now (ignores triggers) and print the decisions
  engine --snapshot PAIR         print the payload the AI would receive (no AI call)
  engine --capabilities          regenerate docs/capability_matrix.md
  engine --triggers              show trigger evaluation for every pair now (no AI call)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import time
from collections import deque

from ..ai.budget import CostGovernor, UsageStore, usage_db
from ..ai.orchestrator import CycleRequest, Orchestrator
from ..ai.store import DecisionRecord, DecisionStore
from ..ai.triggers import decide, review_due_split
from ..core.instruments import InstrumentRegistry
from ..core.logsetup import setup_from_settings
from ..core.sessions import calendar_for
from ..core.settings import PROJECT_ROOT, Settings, load_settings
from ..core.timeframes import Timeframe
from ..core.timeutil import MS_PER_DAY, iso, now_ms
from ..ingest.common.appdb import AppDB
from ..storage.tablespec import spec_for
from .registry import matrix_markdown
from .snapshot import SnapshotBuilder, data_problems, to_json

log = logging.getLogger("engine")
SETTLE_MS = 5_000           # wait after a close so the closed bar is stored
DATA_WAIT_MS = 90_000       # MT5 closes a bar on the next tick — a bar still missing after this is reported
HEARTBEAT_S = 10.0          # collector_status heartbeat, independent of the AI work (supervisor stale limit 120 s)
QUIET_MS = 10 * 60_000      # repeated warnings (AI not ready, quota) are logged at most this often
EXECUTOR_FRESH_MS = 120_000  # the executor's account report is used while it is at most this old
MAX_FAILS = 8               # back-off exponent cap
EVENT_COALESCE_MS = 60_000  # executor events are gathered this long before they wake the model (fill + order …)
# executor events that wake the model (Phase 3): a placement, a fill, a closed position, a settled outcome, and the
# result of its own position_actions
WAKE_EVENTS = ("order", "mgmt_filled", "mgmt_position_closed", "outcome", "action_applied", "action_rejected")
EVENT_CURSOR_KEY = "executor_event_id"   # engine_kv: the last executor event handed to a pair (survives restarts)
EVENT_MAX_AGE_MS = 2 * 3_600_000          # … but an event older than this at start-up no longer wakes the model
ANSWERED = ("valid", "invalid", "refused")   # the model answered: the setup and events it was called for are seen


class Engine:
    def __init__(self, s: Settings) -> None:
        self.s = s
        self.reg = InstrumentRegistry.from_settings(s)
        self.appdb = AppDB(s.paths.state() / "app.db")
        self.builder = SnapshotBuilder(s, self.reg)
        self.store = DecisionStore(s.paths.state() / "app.db", s.config_hash)
        self.usage = UsageStore(usage_db(s))
        self.governor = CostGovernor(s.ai.budget, self.usage, profit_fn=self.store.realised_since,
                                     instance=s.paths.instance)
        self.orch = Orchestrator(s, self.reg, self.builder, self.store, self.usage, self.governor,
                                 charts=self._chart_renderer())
        self.processed: dict[str, int] = {}          # last decision-TF bar evaluated at its close
        self.processed_screen: dict[str, int] = {}   # last screen-TF (5m) bar evaluated at its close
        self._closes: dict[str, dict[str, float]] = {}   # last closed bar close per TF (candle review conditions)
        self._events: dict[str, list[dict]] = {}     # executor events waiting to wake the model, per pair
        self._event_id = 0                            # set in run() from engine_kv / the DB
        self._prev_call: dict[str, tuple] = {}        # pair → (signature, price, events) before the call in flight
        self._setup: dict[str, str | None] = {}       # pair → setup strength of the last evaluation
        self._build_ms: deque[int] = deque(maxlen=20)   # snapshot build times (5-min screening cost on this PC)
        self.last_call: dict[str, int] = {}          # last dispatch per pair (the DB covers restarts)
        self.fails: dict[str, int] = {}              # consecutive failed cycles per pair → back-off
        self.inflight: dict[str, asyncio.Task] = {}  # pair → its running cycle
        self.inflight_since: dict[str, int] = {}
        self._waiting: dict[str, int] = {}           # pair → decision bar reported as not stored
        self._quiet: dict[str, tuple[str, int]] = {}
        self._tick_error: str | None = None
        self.stop = False

    _ai_problem: str = ""

    def _chart_renderer(self):
        """The chart renderer (matplotlib is only imported when charts are on — RAM on 8 GB machines)."""
        if not self.s.ai.charts.enabled:
            return None
        try:
            from .charts import ChartRenderer
            return ChartRenderer(self.s.ai.charts)       # the orchestrator stores the PNGs per pair
        except Exception:  # noqa: BLE001 — no charts is a degraded mode, never a reason not to start
            log.exception("chart renderer unavailable — text-only AI calls")
            return None

    def live_account(self, pair: str | None) -> dict:
        """The ``account`` block of the payload (Phase 2): the live account this system trades, as its executor last
        reported it — equity, today's PnL of this system, the account's drawdown and, for ``pair``, the positions
        and pending orders it holds. The configured size when the executor is not reporting."""
        base = self.orch.default_account()
        try:
            row = next((r for r in self.appdb.statuses() if r["collector"] == "executor"), None)
            d = json.loads(row["detail"]) if row and row.get("detail") else {}
        except Exception:  # noqa: BLE001 — the payload must never fail on the status row
            row, d = None, {}
        age = now_ms() - int(row["updated_ms"]) if row else None
        if not d.get("equity") or age is None or age > EXECUTOR_FRESH_MS or row["state"] != "live":
            why = "the executor is not reporting" if row is None or age is None or age > EXECUTOR_FRESH_MS \
                else f"the executor is {row['state']}"
            return {**base, "equity_source": f"configured account size ({why}); positions unknown"}
        out = {"mode": d.get("mode", self.s.execution.mode), "currency": d.get("currency", "USD"),
               "equity": d["equity"], "balance": d.get("balance"),
               "equity_source": f"live {d.get('mode')} account, reported by the executor {age // 1000}s ago",
               "today_pnl_pct": d.get("today_pnl_pct"), "daily_loss_limit_pct": self.s.risk.max_daily_loss_pct}
        dd = d.get("account_drawdown") or {}
        if dd:
            out["account_drawdown_pct"] = dd.get("drawdown_pct")
            out["account_drawdown_stop_pct"] = self.s.risk.account_drawdown_stop_pct
            if dd.get("tripped"):
                out["account_drawdown_tripped"] = dd["tripped"]
        if pair:
            exe = next(iter(self.reg.with_role(pair, "execution")), None) if getattr(self, "reg", None) else None
            if exe is not None:
                out["holdings_price_space"] = exe.key     # the execution instrument's prices (legend)
            rows = [{k: v for k, v in x.items() if k not in ("pair", "kind")} for x in d.get("exposure") or []
                    if x.get("pair") == pair]
            kinds = [x.get("kind") for x in d.get("exposure") or [] if x.get("pair") == pair]
            out["open_positions"] = [r for r, k in zip(rows, kinds) if k == "position"]
            out["pending_orders"] = [{k: v for k, v in r.items() if k != "profit_usd"}
                                     for r, k in zip(rows, kinds) if k == "order"]
        return out

    def ai_ready(self) -> bool:
        """True when the active provider (or ``ai.fallback_provider`` while it is unavailable) can take calls."""
        try:
            self.orch.provider()
            self._ai_problem = ""
            return True
        except Exception as exc:  # noqa: BLE001
            self._ai_problem = str(exc)
            return False

    def _data_ready(self, pair: str, bar_open: int, tf: Timeframe | None = None) -> bool:
        inst = self.reg.primary(pair)
        tf = tf or self.s.pairs[pair].decision_timeframe
        last = self.builder.reader(inst).last_time(spec_for(inst, "candles", tf))
        return last is not None and last >= bar_open

    def screen_tf(self, pair: str) -> Timeframe:
        """The timeframe Python screens on (``ai.screen_timeframe``, 5m) — the decision timeframe itself when the pair
        does not collect it or it is not shorter."""
        pcfg = self.s.pairs[pair]
        dec = pcfg.decision_timeframe
        try:
            stf = Timeframe.parse(self.s.ai.screen_timeframe)
        except ValueError:
            return dec
        tfs = {Timeframe.parse(t).value for t in (pcfg.timeframes or self.s.timeframes)}
        return stf if stf.ms < dec.ms and stf.value in tfs else dec

    def _init_event_cursor(self) -> None:
        """Executor events: from where the last run stopped (events written during a restart still wake the model,
        when younger than EVENT_MAX_AGE_MS); the very first run starts after the newest event."""
        stored = self.store.kv_get(EVENT_CURSOR_KEY)
        self._event_id = int(stored) if isinstance(stored, int) else self.store.last_event_id()

    def _pair_events(self, pair: str) -> list[dict]:
        """New executor events of ``pair`` (the all-pairs system has every pair's events in one table)."""
        rows = self.store.events_after(self._event_id, WAKE_EVENTS)
        if rows:
            self._event_id = rows[-1]["id"]
            try:
                self.store.kv_set(EVENT_CURSOR_KEY, self._event_id)
            except Exception:  # noqa: BLE001 — the cursor is a restart aid; the events are handled anyway
                log.warning("could not persist the executor event cursor", exc_info=True)
        cutoff = now_ms() - EVENT_MAX_AGE_MS
        for r in rows:
            if int(r["ts"] or 0) < cutoff:
                continue                     # from before a long stop: history, not a reason to call now
            owner = None
            try:
                owner = (json.loads(r["detail"]) or {}).get("pair") if r["detail"] else None
            except (ValueError, AttributeError):
                owner = None
            if owner is None:           # older plain-text events: "<mode> <PAIR> <id>: …" or "<PAIR> <id>: …"
                owner = next((p for p in self.s.enabled_pairs() if f"{p} " in f" {r['detail'] or ''} "), None)
            if owner:
                self._events.setdefault(owner, []).append(r)
        return self._events.get(pair, [])

    @staticmethod
    def _event_text(r: dict) -> str:
        d = r.get("detail") or ""
        try:
            j = json.loads(d)
            d = j.get("text") or ", ".join(f"{k} {v}" for k, v in j.items() if k != "pair")
        except (ValueError, AttributeError):
            pass
        return f"{r['event']} {d}"[:160]

    def _policy(self) -> str:
        return "on_setup_event" if self.governor.state().level >= 2 else self.s.ai.trigger_policy

    def _backoff_ms(self, pair: str) -> int:
        n = min(self.fails.get(pair, 0), MAX_FAILS)
        if not n:
            return 0
        return min(self.s.ai.min_minutes_between_calls * 2 ** n, self.s.ai.max_backoff_minutes) * 60_000

    def _review_mid(self, pair: str, last: dict, now: int) -> float | None:
        """Quote mid in the recommendation's price space (its ``price_reference``, else the analysis instrument —
        never the execution venue, F14); read only when a price condition needs it."""
        rec = last.get("recommendation") or {}
        if not any(c.get("kind") in ("price_above", "price_below")
                   for c in (rec.get("next_review") or {}).get("conditions") or []):
            return None
        inst = next((i for i in self.reg.for_pair(pair) if i.key == rec.get("price_reference")), None) \
            or self.reg.primary(pair)
        q = self.builder.quote_at(inst, now) or {}
        return (q["bid"] + q["ask"]) / 2 if q.get("bid") and q.get("ask") else None

    def evaluate(self, pair: str, now: int, at_close: bool, payload: dict | None,
                 policy: str | None = None, *, at_decision_close: bool | None = None,
                 events: list[dict] | None = None) -> tuple[bool, list[str], str]:
        """The trigger decision for ``pair`` now (Phase 3: screened at every 5m close, called only on change).
        ``at_close`` = a screen-TF bar just closed (a payload was built); ``at_decision_close`` = the decision-TF
        bar too. The current setup signature is kept in ``self._sig[pair]`` for the dispatch."""
        if payload:
            self._closes[pair] = {tf: t["recent"][-1][4] for tf, t in (payload.get("timeframes") or {}).items()
                                  if t.get("recent")}
        last = self.store.last_decision(pair)
        attempt = self.store.last_attempt_ts(pair)
        answered = self.store.last_attempt_ts(pair, answered=True)
        time_r: list[str] = []
        cond_r: list[str] = []
        if last and not (answered is not None and answered > last["ts"]):  # a review is consumed by one answer
            time_r, cond_r = review_due_split(last, now, self._review_mid(pair, last, now), self._closes.get(pair, {}))
        # spacing counts from the dispatch (a row's ts is when the answer was stored — a slow call must not push
        # the next decision-bar close out of reach); the DB row only stands in after a restart
        last_call = self.last_call.get(pair, attempt)
        tf = self.s.pairs[pair].decision_timeframe
        last_dec_close = tf.floor(now)                      # close time of the last closed decision bar
        move_atr = None
        ref = self.store.kv_get(f"{pair}:last_call_price")
        atr = (((payload or {}).get("timeframes") or {}).get(tf.value) or {}).get("indicators", {}).get("atr14")
        mid = _payload_mid(payload) if payload else None
        if ref and atr and mid:
            move_atr = abs(mid - float(ref)) / float(atr)
        sig = self.store.kv_get(f"{pair}:signature")
        d = decide(policy or self._policy(), payload, last_call_ms=last_call, now=now,
                   min_spacing_min=self.s.ai.min_minutes_between_calls, max_idle_min=self.s.ai.max_idle_minutes,
                   review_reasons=cond_r, at_close=at_close, review_floor_min=self.s.ai.review_floor_minutes,
                   backoff_ms=self._backoff_ms(pair), weak_min=self.s.ai.weak_min,
                   liquidity_atr=self.s.ai.liquidity_atr, screen_tf=self.screen_tf(pair).value,
                   last_signature=frozenset(sig) if sig is not None else None, time_reasons=time_r,
                   event_reasons=[self._event_text(e) for e in events or []], at_decision_close=at_decision_close,
                   decision_bar_since_last_call=last_call is None or last_dec_close > last_call,
                   move_atr=move_atr, screen_move_atr=self.s.ai.screen_move_atr,
                   weak_needs_location=self.s.ai.weak_needs_location)
        self._sig = getattr(self, "_sig", {})
        if d.signature or payload:
            self._sig[pair] = sorted(d.signature)
        self._setup[pair] = d.setup
        return d.fire, d.reasons, d.strength

    async def tick(self) -> None:
        now = now_ms()
        policy = self._policy()
        fired: list[tuple[str, list[str], str, dict | None]] = []
        for pair, pcfg in self.s.enabled_pairs().items():
            if pair in self.inflight:
                continue                                        # its cycle is still running
            execu = self.reg.with_role(pair, "execution")[0]
            cal = calendar_for(execu.venue, execu.symbol, pcfg.asset_class)
            if not cal.is_open(now):
                continue
            tf = pcfg.decision_timeframe
            bar = tf.floor(now) - tf.ms
            stf = self.screen_tf(pair)
            sbar = stf.floor(now) - stf.ms
            if not self._data_ready(pair, bar):
                # never analyse (or review on) a decision bar that is not stored — wait for it (stall F5)
                if now >= bar + tf.ms + DATA_WAIT_MS and self._waiting.get(pair) != bar \
                        and cal.is_open(bar + tf.ms - 1):
                    self._waiting[pair] = bar
                    msg = f"{pair} {tf.value} bar {iso(bar)} still not stored {DATA_WAIT_MS // 1000}s after its close"
                    log.warning("%s — not analysed until it arrives", msg)
                    self.appdb.add_event("engine", "data_not_ready", msg)
                continue
            at_dec = bar > self.processed.get(pair, 0) and now >= bar + tf.ms + SETTLE_MS
            at_screen = (sbar > self.processed_screen.get(pair, 0) and now >= sbar + stf.ms + SETTLE_MS
                         and (stf == tf or self._data_ready(pair, sbar, stf)))
            at_close = at_dec or at_screen
            payload = None
            if at_close:
                t0 = time.perf_counter()
                payload = self.orch.payload(pair, now, self.live_account(pair))
                self._build_ms.append(int((time.perf_counter() - t0) * 1000))
            evs = self._pair_events(pair)
            wake = [e for e in evs if now - int(e["ts"]) >= EVENT_COALESCE_MS] and evs      # all, once the first is ripe
            if wake and not self._event_budget(pair, now):
                self._events[pair] = []                  # today's event calls are used up: the events stay in history
                wake = []
            fire, reasons, strength = self.evaluate(pair, now, at_close, payload, policy, at_decision_close=at_dec,
                                                    events=wake or None)
            if at_screen:
                self.processed_screen[pair] = sbar
            if at_dec:
                self.processed[pair] = bar
            if at_close:
                log.info("%s %s close %s: trigger=%s (%s) %s", pair, (tf if at_dec else stf).value,
                         iso(bar if at_dec else sbar), fire, strength, "; ".join(reasons)[:300])
            if fire:
                fired.append((pair, reasons, strength, payload))
        if fired:
            self._dispatch(fired, now)

    def _dispatch(self, fired: list[tuple[str, list[str], str, dict | None]], now: int) -> None:
        pairs = ", ".join(f[0] for f in fired)
        if not self.ai_ready():
            self._quietly("ai_not_ready", f"{self._ai_problem}|{pairs}", now, "ai_not_ready",
                          f"AI provider not ready ({self._ai_problem}) — not analysed: {pairs}")
            return
        fired = self._ration(fired, now)
        payloads: dict[str, dict] = {}
        queue: list[CycleRequest] = []
        for pair, reasons, strength, payload in fired:
            p = payload or self.orch.payload(pair, now, self.live_account(pair))
            problems = data_problems(p, now, self.s.risk.max_data_staleness_s)
            if problems:
                self.store.save(DecisionRecord(pair, self.orch.effective_mode()[0], "; ".join(reasons)[:600],
                                               "skipped", errors=[f"data gate: {x}" for x in problems]))
                self.last_call[pair] = now
                log.warning("%s: AI call skipped — %s", pair, "; ".join(problems)[:300])
                continue
            payloads[pair] = p
            queue.append(CycleRequest(pair, "; ".join(reasons)[:600], strength, self._setup.get(pair)))
        if not queue:
            return
        task = asyncio.create_task(self._cycle(queue, payloads, now))
        for q in queue:                          # mark every pair first: a failure below never leaves one unmarked
            self.last_call[q.pair] = now          # counted at dispatch, whatever the outcome (F15)
            self.inflight[q.pair] = task
            self.inflight_since[q.pair] = now
        for q in queue:
            try:
                self._remember_call(q.pair, payloads[q.pair], q.strength)
            except Exception:  # noqa: BLE001 — the call is on its way; the next screens compare with older values
                log.exception("%s: could not record the call's setup signature", q.pair)

    def _remember_call(self, pair: str, payload: dict, strength: str | None) -> None:
        """At dispatch: the setup signature and price the next "did anything change?" test compares against, and
        the executor events this call now covers. The values before are kept until the call ends: a call that got
        no answer gives them back (:meth:`_restore_call`), so its setup and events can call again after the
        back-off."""
        prev_sig, prev_px = self.store.kv_get(f"{pair}:signature"), self.store.kv_get(f"{pair}:last_call_price")
        consumed = list(self._events.get(pair) or [])
        self._prev_call[pair] = (prev_sig, prev_px, consumed)
        sig = getattr(self, "_sig", {}).get(pair)
        if sig is not None:
            self.store.kv_set(f"{pair}:signature", sig)
        mid = _payload_mid(payload)
        if mid:
            self.store.kv_set(f"{pair}:last_call_price", mid)
        if strength == "event" or consumed:
            self._events[pair] = []

    def _restore_call(self, pair: str) -> None:
        """The call for ``pair`` got no answer (error, budget block, deadline, crash): the setup it was called for
        is not seen and its executor events are not handled."""
        prev = self._prev_call.pop(pair, None)
        if prev is None:
            return
        sig, px, consumed = prev
        try:
            if sig is not None:
                self.store.kv_set(f"{pair}:signature", sig)
            if px is not None:
                self.store.kv_set(f"{pair}:last_call_price", px)
        except Exception:  # noqa: BLE001
            log.exception("%s: could not restore the setup signature", pair)
        if consumed:
            self._events[pair] = consumed + [e for e in self._events.get(pair, []) if e not in consumed]

    def _event_budget(self, pair: str, now: int) -> bool:
        """Event-woken calls per pair and UTC day (``ai.event_calls_per_day``)."""
        day0 = now // MS_PER_DAY * MS_PER_DAY
        return self.store.count_strength_since(pair, "event", day0) < self.s.ai.event_calls_per_day

    def _ration(self, fired: list, now: int) -> list:
        """Quota pressure (F8): the fewer requests left in the provider's quota day, the stronger a trigger must be."""
        left, cap = self.orch.quota()
        if left is None or not cap:
            return fired
        frac = left / cap
        ok = (set() if left <= 0 else {"review", "event"} if frac < 0.2 else {"review", "event", "strong"}
              if frac < 0.5 else None)
        keep = fired if ok is None else [f for f in fired if f[2] in ok]
        if len(keep) < len(fired):
            dropped = ", ".join(f[0] for f in fired if f not in keep)
            self._quietly("quota", dropped, now, "ai_quota",
                          f"{self.orch.route[0]}: {left}/{cap} requests left today — not analysed: {dropped}")
        return keep

    async def _cycle(self, queue: list[CycleRequest], payloads: dict[str, dict], as_of: int) -> None:
        pairs = [q.pair for q in queue]
        answered: set[str] = set()
        try:
            recs = await self.orch.run_cycle(queue, as_of=as_of, payloads=payloads, account=self.live_account(None))
            for r in recs:
                self.fails[r.pair] = 0 if r.status in ("valid", "skipped") else self.fails.get(r.pair, 0) + 1
                if r.status in ANSWERED:
                    answered.add(r.pair)
        except Exception as exc:  # noqa: BLE001
            log.exception("AI cycle failed")
            self.appdb.add_event("engine", "cycle_error", repr(exc)[:300])
            for p in pairs:
                self.fails[p] = self.fails.get(p, 0) + 1
        finally:
            for p in pairs:
                if p in answered:
                    self._prev_call.pop(p, None)
                else:
                    self._restore_call(p)
            me = asyncio.current_task()
            for p in pairs:
                if self.inflight.get(p) is me:
                    del self.inflight[p]
                    self.inflight_since.pop(p, None)

    def _quietly(self, key: str, sig: str, now: int, event: str, msg: str) -> None:
        """Warn (and add an event) when ``sig`` changes, else at most every ``QUIET_MS`` — never every tick."""
        prev = self._quiet.get(key)
        if prev and prev[0] == sig and now - prev[1] < QUIET_MS:
            return
        self._quiet[key] = (sig, now)
        log.warning(msg)
        self.appdb.add_event("engine", event, msg[:300])

    def write_status(self) -> None:
        st = self.governor.state()
        ready = self.ai_ready()
        in_use, rerouted = self.orch.route
        left, cap = self.orch.quota()
        self.appdb.set_status("engine", "error" if self._tick_error else "live", last_data_ms=now_ms(),
                              error=self._tick_error, detail={
            "mode": self.s.ai.agent_mode, "policy": self._policy(),
            "provider": in_use if ready else self.s.ai.active_provider,
            "provider_configured": self.s.ai.active_provider,
            "provider_fallback_reason": rerouted if ready else None,
            "governor_level": st.level, "governor_reason": st.reason, "ai_spend_today_usd": round(st.spend_today, 4),
            "ai_ready": ready, "ai_problem": self._ai_problem or None,
            "quota_left_today": left, "quota_per_day": cap,
            "cycles_inflight": {p: iso(t) for p, t in self.inflight_since.items()},
            "backoff_fails": {p: n for p, n in self.fails.items() if n},
            "processed": {p: iso(b) for p, b in self.processed.items()},
            "screened": {p: iso(b) for p, b in self.processed_screen.items()},
            "snapshot_build_ms": {"last": self._build_ms[-1] if self._build_ms else None,
                                  "max": max(self._build_ms) if self._build_ms else None},
            "events_waiting": {p: len(v) for p, v in self._events.items() if v}})

    async def _heartbeat(self) -> None:
        while not self.stop:
            try:
                self.write_status()
            except Exception:  # noqa: BLE001
                log.exception("engine heartbeat failed")
            await asyncio.sleep(HEARTBEAT_S)

    async def run(self) -> None:
        self.appdb.set_status("engine", "starting")
        now = now_ms()
        for pair, pcfg in self.s.enabled_pairs().items():   # don't fire for the bar that closed before start-up
            tf = pcfg.decision_timeframe
            self.processed[pair] = tf.floor(now) - tf.ms
            stf = self.screen_tf(pair)
            self.processed_screen[pair] = stf.floor(now) - stf.ms
        self._init_event_cursor()
        hb = asyncio.create_task(self._heartbeat())
        try:
            while not self.stop:
                t0 = time.time()
                try:
                    await self.tick()
                    self._tick_error = None
                except Exception as exc:  # noqa: BLE001
                    log.exception("engine tick failed")
                    self._tick_error = repr(exc)[:300]
                await asyncio.sleep(max(0.5, 2 - (time.time() - t0)))
        finally:
            tasks = [hb, *set(self.inflight.values())]
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    def close(self) -> None:
        self.builder.close()
        self.store.close()
        self.usage.close()
        self.appdb.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="tradingsystem engine")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--pairs", default="")
    ap.add_argument("--snapshot")
    ap.add_argument("--capabilities", action="store_true")
    ap.add_argument("--triggers", action="store_true")
    args = ap.parse_args(argv)
    s = load_settings()
    setup_from_settings("engine", s)
    if args.capabilities:
        reg = InstrumentRegistry.from_settings(s)
        (PROJECT_ROOT / "docs" / "capability_matrix.md").write_text(matrix_markdown(s, reg), encoding="utf-8")
        print("docs/capability_matrix.md written")
        return 0
    eng = Engine(s)
    try:
        if args.snapshot:
            print(json.dumps(eng.builder.build(args.snapshot, now_ms()), indent=1, default=str))
            return 0
        if args.triggers:
            now = now_ms()
            for pair in s.enabled_pairs():
                p = eng.builder.build(pair, now)
                fire, reasons, strength = eng.evaluate(pair, now, True, p)
                print(f"{pair}: fire={fire} ({strength})\n  - " + "\n  - ".join(reasons or ["(no reasons)"]))
            return 0
        if args.once:
            pairs = [p for p in (args.pairs.split(",") if args.pairs else s.enabled_pairs()) if p]
            wait_for_provider(eng.orch)
            now = now_ms()
            payloads = {p: eng.orch.payload(p, now, eng.live_account(p)) for p in pairs}
            for p, pl in payloads.items():          # manual run: the data gate only warns
                for problem in data_problems(pl, now, s.risk.max_data_staleness_s):
                    print(f"WARNING {p}: {problem}")
            recs = asyncio.run(eng.orch.run_cycle([CycleRequest(p, "manual --once") for p in pairs], as_of=now,
                                                  payloads=payloads, account=eng.live_account(None)))
            for r in recs:
                print(f"\n=== {r.pair}: {r.status} (mode {r.mode}, {r.provider}/{r.model}, ${r.cost_usd:.4f}, "
                      f"{r.latency_ms} ms) id={r.id}")
                if r.recommendation:
                    print(json.dumps(r.recommendation, indent=1)[:4000])
                if r.errors:
                    print("errors:", r.errors[:5])
            return 0
        try:
            asyncio.run(eng.run())
        except KeyboardInterrupt:
            pass
        return 0
    finally:
        eng.close()


__all__ = ["Engine", "main", "to_json", "wait_for_provider"]


def wait_for_provider(orch: Orchestrator, timeout_s: float = 120.0, poll_s: float = 0.5) -> None:
    """``--once``: the active provider's first availability check (the Claude Code sign-in, ``claude auth status``)
    runs in a background thread and answers "checking" until done — the long-running engine simply asks again on its
    next tick, a one-shot run must wait for that first answer or it would always fall back. Bounded; never raises."""
    try:
        prov = orch._get(orch.s.ai.active_provider, "decision")      # the instance the cycle will use
    except Exception:  # noqa: BLE001 — the cycle reports a provider that cannot even be built
        return
    deadline = time.monotonic() + timeout_s
    while True:
        prov.unavailable_reason()                       # starts (or re-starts after a stagger) the background check
        if not prov.availability_pending or time.monotonic() >= deadline:
            return
        time.sleep(poll_s)


def _payload_mid(payload: dict | None) -> float | None:
    """Analysis-instrument mid of a payload (bid/ask, else the last 1m close)."""
    ap = ((payload or {}).get("market") or {}).get("analysis_price") or {}
    if ap.get("bid") and ap.get("ask"):
        return (ap["bid"] + ap["ask"]) / 2
    return ap.get("last_close_1m")
