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

from ..ai.budget import CostGovernor, UsageStore
from ..ai.orchestrator import CycleRequest, Orchestrator
from ..ai.store import DecisionRecord, DecisionStore
from ..ai.triggers import decide, review_due
from ..core.instruments import InstrumentRegistry
from ..core.logsetup import setup_from_settings
from ..core.sessions import calendar_for
from ..core.settings import PROJECT_ROOT, Settings, load_settings
from ..core.timeutil import iso, now_ms
from ..ingest.common.appdb import AppDB
from ..storage.tablespec import spec_for
from .registry import matrix_markdown
from .snapshot import SnapshotBuilder, data_problems, to_json

log = logging.getLogger("engine")
SETTLE_MS = 5_000           # wait after a close so the closed bar is stored
DATA_WAIT_MS = 90_000       # MT5 closes a bar on the next tick — a bar still missing after this is reported
HEARTBEAT_S = 10.0          # collector_status heartbeat, independent of the AI work (supervisor stale limit 120 s)
QUIET_MS = 10 * 60_000      # repeated warnings (AI not ready, quota) are logged at most this often
MAX_FAILS = 8               # back-off exponent cap


class Engine:
    def __init__(self, s: Settings) -> None:
        self.s = s
        self.reg = InstrumentRegistry.from_settings(s)
        self.appdb = AppDB(s.paths.data() / "app.db")
        self.builder = SnapshotBuilder(s, self.reg)
        self.store = DecisionStore(s.paths.data() / "app.db", s.config_hash)
        self.usage = UsageStore(s.paths.data() / "app.db")
        self.governor = CostGovernor(s.ai.budget, self.usage, profit_fn=self.store.realised_since)
        self.orch = Orchestrator(s, self.reg, self.builder, self.store, self.usage, self.governor)
        self.processed: dict[str, int] = {}
        self.last_call: dict[str, int] = {}          # last dispatch per pair (the DB covers restarts)
        self.fails: dict[str, int] = {}              # consecutive failed cycles per pair → back-off
        self.inflight: dict[str, asyncio.Task] = {}  # pair → its running cycle
        self.inflight_since: dict[str, int] = {}
        self._waiting: dict[str, int] = {}           # pair → decision bar reported as not stored
        self._quiet: dict[str, tuple[str, int]] = {}
        self._tick_error: str | None = None
        self.stop = False

    _ai_problem: str = ""

    def ai_ready(self) -> bool:
        """True when the active provider (or ``ai.fallback_provider`` while it is unavailable) can take calls."""
        try:
            self.orch.provider()
            self._ai_problem = ""
            return True
        except Exception as exc:  # noqa: BLE001
            self._ai_problem = str(exc)
            return False

    def _data_ready(self, pair: str, bar_open: int) -> bool:
        inst = self.reg.primary(pair)
        tf = self.s.pairs[pair].decision_timeframe
        last = self.builder.reader(inst).last_time(spec_for(inst, "candles", tf))
        return last is not None and last >= bar_open

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
                 policy: str | None = None) -> tuple[bool, list[str], str]:
        last = self.store.last_decision(pair)
        attempt = self.store.last_attempt_ts(pair)
        answered = self.store.last_attempt_ts(pair, answered=True)
        rr: list[str] = []
        if last and not (answered is not None and answered > last["ts"]):  # a review is consumed by one answer
            closed = {tf: t["recent"][-1][4] for tf, t in ((payload or {}).get("timeframes") or {}).items()
                      if t.get("recent")}
            rr = review_due(last, now, self._review_mid(pair, last, now), closed)
        # spacing counts from the dispatch (a row's ts is when the answer was stored — a slow call must not push
        # the next decision-bar close out of reach); the DB row only stands in after a restart
        last_call = self.last_call.get(pair, attempt)
        d = decide(policy or self._policy(), payload, last_call_ms=last_call, now=now,
                   min_spacing_min=self.s.ai.min_minutes_between_calls, max_idle_min=self.s.ai.max_idle_minutes,
                   review_reasons=rr, at_close=at_close, review_floor_min=self.s.ai.review_floor_minutes,
                   backoff_ms=self._backoff_ms(pair))
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
            if not self._data_ready(pair, bar):
                # never analyse (or review on) a decision bar that is not stored — wait for it (stall F5)
                if now >= bar + tf.ms + DATA_WAIT_MS and self._waiting.get(pair) != bar \
                        and cal.is_open(bar + tf.ms - 1):
                    self._waiting[pair] = bar
                    msg = f"{pair} {tf.value} bar {iso(bar)} still not stored {DATA_WAIT_MS // 1000}s after its close"
                    log.warning("%s — not analysed until it arrives", msg)
                    self.appdb.add_event("engine", "data_not_ready", msg)
                continue
            at_close = bar > self.processed.get(pair, 0) and now >= bar + tf.ms + SETTLE_MS
            payload = self.orch.payload(pair, now) if at_close else None
            fire, reasons, strength = self.evaluate(pair, now, at_close, payload, policy)
            if at_close:
                self.processed[pair] = bar
                log.info("%s %s close %s: trigger=%s (%s) %s", pair, tf.value, iso(bar), fire, strength,
                         "; ".join(reasons)[:300])
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
        for pair, reasons, _, payload in fired:
            p = payload or self.orch.payload(pair, now)
            problems = data_problems(p, now, self.s.risk.max_data_staleness_s)
            if problems:
                self.store.save(DecisionRecord(pair, self.orch.effective_mode()[0], "; ".join(reasons)[:600],
                                               "skipped", errors=[f"data gate: {x}" for x in problems]))
                self.last_call[pair] = now
                log.warning("%s: AI call skipped — %s", pair, "; ".join(problems)[:300])
                continue
            payloads[pair] = p
            queue.append(CycleRequest(pair, "; ".join(reasons)[:600]))
        if not queue:
            return
        task = asyncio.create_task(self._cycle(queue, payloads, now))
        for q in queue:
            self.last_call[q.pair] = now          # counted at dispatch, whatever the outcome (F15)
            self.inflight[q.pair] = task
            self.inflight_since[q.pair] = now

    def _ration(self, fired: list, now: int) -> list:
        """Quota pressure (F8): the fewer requests left in the provider's quota day, the stronger a trigger must be."""
        left, cap = self.orch.quota()
        if left is None or not cap:
            return fired
        frac = left / cap
        ok = (set() if left <= 0 else {"review"} if frac < 0.2 else {"review", "strong"} if frac < 0.5 else None)
        keep = fired if ok is None else [f for f in fired if f[2] in ok]
        if len(keep) < len(fired):
            dropped = ", ".join(f[0] for f in fired if f not in keep)
            self._quietly("quota", dropped, now, "ai_quota",
                          f"{self.orch.route[0]}: {left}/{cap} requests left today — not analysed: {dropped}")
        return keep

    async def _cycle(self, queue: list[CycleRequest], payloads: dict[str, dict], as_of: int) -> None:
        pairs = [q.pair for q in queue]
        try:
            recs = await self.orch.run_cycle(queue, as_of=as_of, payloads=payloads)
            for r in recs:
                self.fails[r.pair] = 0 if r.status in ("valid", "skipped") else self.fails.get(r.pair, 0) + 1
        except Exception as exc:  # noqa: BLE001
            log.exception("AI cycle failed")
            self.appdb.add_event("engine", "cycle_error", repr(exc)[:300])
            for p in pairs:
                self.fails[p] = self.fails.get(p, 0) + 1
        finally:
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
            "processed": {p: iso(b) for p, b in self.processed.items()}})

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
            now = now_ms()
            payloads = {p: eng.orch.payload(p, now) for p in pairs}
            for p, pl in payloads.items():          # manual run: the data gate only warns
                for problem in data_problems(pl, now, s.risk.max_data_staleness_s):
                    print(f"WARNING {p}: {problem}")
            recs = asyncio.run(eng.orch.run_cycle([CycleRequest(p, "manual --once") for p in pairs], as_of=now,
                                                  payloads=payloads))
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


__all__ = ["Engine", "main", "to_json"]
