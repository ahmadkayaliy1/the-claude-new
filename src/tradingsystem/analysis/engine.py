"""Decision engine service: ``python -m tradingsystem engine`` (P8.5–P8.7 wiring).

Loop (every 2 s): for each pair, when a new decision-timeframe candle has closed (and its data is stored),
build the snapshot, evaluate the trigger policy (setup events / next_review / idle), and run an AI cycle for
the triggered pairs. Heartbeat in ``collector_status`` ("engine"). Market-closed pairs are skipped.

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
from ..ai.store import DecisionStore
from ..ai.triggers import decide, review_due
from ..core.instruments import InstrumentRegistry
from ..core.logsetup import setup_from_settings
from ..core.sessions import calendar_for
from ..core.settings import PROJECT_ROOT, Settings, load_settings
from ..core.timeutil import iso, now_ms
from ..ingest.common.appdb import AppDB
from ..storage.tablespec import spec_for
from .registry import matrix_markdown
from .snapshot import SnapshotBuilder, to_json

log = logging.getLogger("engine")
SETTLE_MS = 5_000           # wait after a close so the closed bar is stored
DATA_WAIT_MS = 90_000       # MT5 closes a bar on the next tick — give it time before proceeding anyway


class Engine:
    def __init__(self, s: Settings) -> None:
        self.s = s
        self.reg = InstrumentRegistry.from_settings(s)
        self.appdb = AppDB(s.paths.data() / "app.db")
        self.builder = SnapshotBuilder(s, self.reg)
        self.store = DecisionStore(s.paths.data() / "app.db", s.config_hash)
        self.usage = UsageStore(s.paths.data() / "app.db")
        self.governor = CostGovernor(s.ai.budget, self.usage, profit_fn=None)   # wired to outcomes in M9
        self.orch = Orchestrator(s, self.reg, self.builder, self.store, self.usage, self.governor)
        self.processed: dict[str, int] = {}
        self.last_call: dict[str, int] = {}
        self.stop = False

    _ai_problem: str = ""

    def ai_ready(self) -> bool:
        """True when the active provider can be constructed (e.g. its API key is set — H4)."""
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

    def evaluate(self, pair: str, now: int, at_close: bool, payload: dict | None) -> tuple[bool, list[str], str]:
        last = self.store.last_decision(pair)
        q = self.builder.quote_at(self.reg.with_role(pair, "execution")[0], now) or {}
        mid = (q["bid"] + q["ask"]) / 2 if q.get("bid") and q.get("ask") else None
        closed = {}
        if payload:
            for tf, t in payload.get("timeframes", {}).items():
                if t.get("recent"):
                    closed[tf] = t["recent"][-1][4]
        rr = review_due(last, now, mid, closed)
        last_call = self.last_call.get(pair) or (last["ts"] if last else None)
        d = decide(self._policy(), payload, last_call_ms=last_call, now=now,
                   min_spacing_min=self.s.ai.min_minutes_between_calls, max_idle_min=self.s.ai.max_idle_minutes,
                   review_reasons=rr, at_close=at_close)
        return d.fire, d.reasons, d.strength

    async def tick(self) -> None:
        now = now_ms()
        queue: list[CycleRequest] = []
        for pair, pcfg in self.s.enabled_pairs().items():
            execu = self.reg.with_role(pair, "execution")[0]
            if not calendar_for(execu.venue, execu.symbol, pcfg.asset_class).is_open(now):
                continue
            tf = pcfg.decision_timeframe
            bar = tf.floor(now) - tf.ms
            at_close = bar > self.processed.get(pair, 0) and now >= bar + tf.ms + SETTLE_MS
            if at_close and not self._data_ready(pair, bar) and now < bar + tf.ms + DATA_WAIT_MS:
                continue
            payload = self.builder.build(pair, now) if at_close else None
            fire, reasons, strength = self.evaluate(pair, now, at_close, payload)
            if at_close:
                self.processed[pair] = bar
                log.info("%s %s close %s: trigger=%s (%s) %s", pair, tf.value, iso(bar), fire, strength,
                         "; ".join(reasons)[:300])
            if fire:
                queue.append(CycleRequest(pair, "; ".join(reasons)[:600]))
                self.last_call[pair] = now
        if queue and not self.ai_ready():
            log.warning("AI provider not ready (%s) — %d triggered pair(s) not analysed", self._ai_problem,
                        len(queue))
            self.appdb.add_event("engine", "ai_not_ready", f"{self._ai_problem}; skipped: "
                                 + ", ".join(q.pair for q in queue))
            return
        if queue:
            try:
                await self.orch.run_cycle(queue, as_of=now)
            except Exception as exc:  # noqa: BLE001
                log.exception("AI cycle failed")
                self.appdb.add_event("engine", "cycle_error", repr(exc)[:300])

    async def run(self) -> None:
        self.appdb.set_status("engine", "starting")
        now = now_ms()
        for pair, pcfg in self.s.enabled_pairs().items():   # don't fire for the bar that closed before start-up
            tf = pcfg.decision_timeframe
            self.processed[pair] = tf.floor(now) - tf.ms
        while not self.stop:
            t0 = time.time()
            try:
                await self.tick()
                st = self.governor.state()
                self.appdb.set_status("engine", "live", last_data_ms=now_ms(), detail={
                    "mode": self.s.ai.agent_mode, "policy": self._policy(), "provider": self.s.ai.active_provider,
                    "governor_level": st.level, "governor_reason": st.reason, "ai_spend_today_usd": round(st.spend_today, 4),
                    "ai_ready": self.ai_ready(), "ai_problem": self._ai_problem or None,
                    "processed": {p: iso(b) for p, b in self.processed.items()}})
            except Exception as exc:  # noqa: BLE001
                log.exception("engine tick failed")
                self.appdb.set_status("engine", "error", error=repr(exc)[:300])
            await asyncio.sleep(max(0.5, 2 - (time.time() - t0)))

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
            recs = asyncio.run(eng.orch.run_cycle([CycleRequest(p, "manual --once") for p in pairs]))
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
