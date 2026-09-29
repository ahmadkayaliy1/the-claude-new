"""Multi-agent orchestration (P8.5, spec §4.2) — every mode is ready; ``ai.agent_mode`` selects one.

Modes
  single_agent_global            one call for all pairs → RecommendationSet
  agent_per_pair                 one call per pair → Recommendation
  agent_per_timeframe            one analyst per timeframe (all pairs) → per-pair coordinator
  agent_per_pair_and_timeframe   one analyst per (pair, timeframe) → per-pair coordinator
  agent_per_pair_with_risk_reviewer  per-pair trader, then an independent risk reviewer (can veto/modify)
  multi_provider_consensus       the same per-pair prompt to several providers → deterministic aggregation
The Cost Governor may degrade the configured mode (level ≥1 → agent_per_pair; level 3 → no calls).
Nothing leaves this module unless it validated against the contract; everything is stored (P8.7).

A cycle runs each pair (or the whole batch in the batch modes) as its own unit: one unit's failure is stored as
status 'error' for that unit only, every record is stored as soon as its unit finishes, and units still running
at ``ai.cycle_deadline_s`` are cancelled (F2, F4). System-owned fields of a recommendation (pair, timestamp,
validity horizon, price reference, review timing) are set here, never taken from the model (F6).

Phase 5 A5: a unit that got no answer while the machine slept more than ``SUSPEND_GAP_S`` during the cycle (the
awake clock of ``supervisor/winops.py`` fell behind the tick clock — the CLI or the network died with the suspend,
or the deadline passed while asleep: 2026-09-27 19:24 UTC) is stored as 'interrupted', not 'error' — the engine does
not back off for it. A transient provider error gets one retry after ``ai.transient_retry_s`` when the deadline
leaves room for it (``ai/repair.py``).
"""
from __future__ import annotations

import asyncio
import contextvars
import datetime as dt
import json
import logging
import time
from dataclasses import dataclass

from pydantic import ValidationError

from ..analysis.snapshot import SnapshotBuilder, payload_hash
from ..core.instruments import InstrumentRegistry
from ..core.sessions import calendar_for
from ..core.settings import Settings
from ..core.timeutil import MS_PER_DAY, iso, now_ms, parse_date_spec
from ..core.timeframes import Timeframe
from ..core.tunables import tunables_of
from ..supervisor.winops import ClockSample, sample_clock
from .budget import CostGovernor, RateLimiter, UsageStore
from .contract import (AssessmentSet, Decision, EscalationReview, Recommendation, RecommendationSet, RiskReview,
                       TimeframeAssessment)
from .model_view import view
from .prompts import DIR as PROMPTS_DIR
from .prompts import library_hash, render
from .providers import LLMProvider, ProviderError, make_provider
from .providers.base import ImageInput
from .repair import Generation, generate_validated
from .store import DecisionRecord, DecisionStore

log = logging.getLogger(__name__)
ANALYST_TFS = ["1w", "1d", "4h", "1h", "15m", "5m"]
NO_CHARTS = "No charts this cycle."
NO_PLAYBOOK = "(no playbook yet)"
NO_TP_HINT = "(none)"
# a confirmed escalation must keep every one of these exactly as the trader proposed them (the trader's
# position_actions always stand: the escalation judges the new trade, not the protective actions on live ones)
ESCALATION_FIXED = ("decision", "order_type", "entry", "stop_loss", "take_profits", "management")
ESCALATION_SIGN_IN_WAIT_S = 60.0         # a new escalation provider instance waits this long for its sign-in check
SUSPEND_GAP_S = 60.0                     # time asleep inside a cycle beyond which its unanswered units are 'interrupted'
# the running cycle's deadline (time.monotonic()), seen by every unit task it starts (the transient retry's room)
_CYCLE_END: contextvars.ContextVar[float | None] = contextvars.ContextVar("ai_cycle_end", default=None)


def clock_sample() -> ClockSample | None:
    """The machine's clocks now (wall / tick incl. sleep / awake excl. sleep); None when they cannot be read."""
    try:
        return sample_clock()
    except Exception:  # noqa: BLE001 — suspend detection is a label, never a reason to fail a cycle
        return None


def asleep_since(since: ClockSample | None) -> float:
    """Seconds the machine slept since ``since`` (the tick clock ran on, the awake clock did not); 0 when unknown."""
    now = clock_sample()
    if since is None or now is None:
        return 0.0
    return max(0.0, (now.tick - since.tick) - (now.awake - since.awake))


@dataclass
class CycleRequest:
    pair: str
    reason: str
    strength: str | None = None          # the trigger strength (strong | weak | review | event | idle | close | manual)
    setup: str | None = None             # a setup that fired with it (strong | weak), also when the call is labelled
    #                                      review/event (escalation looks at both)
    setup_alone: bool = False            # that setup was due by itself (then an event label costs no event call)


class Orchestrator:
    def __init__(self, settings: Settings, registry: InstrumentRegistry, builder: SnapshotBuilder,
                 store: DecisionStore, usage: UsageStore, governor: CostGovernor, charts=None) -> None:
        self.s, self.reg, self.builder, self.store = settings, registry, builder, store
        self.usage, self.governor = usage, governor
        self.charts = charts                      # analysis.charts.ChartRenderer | None (Phase 3)
        self.library_hash = library_hash()        # the prompt library in force (stored with every decision)
        self._providers: dict[tuple, LLMProvider] = {}
        self._limiters: dict[str, RateLimiter] = {}
        self._sem = asyncio.Semaphore(max(1, settings.ai.max_parallel_calls))
        self.route: tuple[str, str | None] = (settings.ai.active_provider, None)   # (provider in use, why rerouted)

    # ------------------------------------------------------------------ plumbing
    def provider(self, name: str | None = None, role: str = "decision") -> LLMProvider:
        """A named provider, or (no name) the active one — rerouted to ``ai.fallback_provider`` while the active
        provider is unavailable (not signed in, usage limit reached, missing key; D-030). ``role`` picks the model and
        effort of ``ai.models.<role>`` (claude_code only; D-043)."""
        if name is not None:
            return self._get(name, role)
        active, fallback = self.s.ai.active_provider, self.s.ai.fallback
        try:
            prov = self._get(active, role)
            why = prov.unavailable_reason()
        except ProviderError as exc:
            if not fallback:
                raise
            prov, why = None, str(exc)
        if why is None:
            self._set_route(active, None)
            return prov
        if not fallback:
            raise ProviderError(why, retryable=False)
        try:
            fb = self._get(fallback, role)
            fb_why = fb.unavailable_reason()
        except ProviderError as exc:
            fb_why = str(exc)
        if fb_why:
            raise ProviderError(f"{why}; fallback {fallback}: {fb_why}", retryable=False)
        self._set_route(fallback, why)
        return fb

    def role_model(self, name: str, role: str) -> tuple[str | None, str | None]:
        """(model, effort) overrides of ``ai.models.<role>`` for provider ``name`` — only claude_code takes the
        aliases (sonnet | opus | fable); other providers keep their configured model."""
        rm = getattr(self.s.ai.models, role, None)
        if rm is None or self.s.ai.providers[name].kind != "claude_code":
            return None, None
        return rm.model, rm.effort

    def _get(self, name: str, role: str = "decision") -> LLMProvider:
        model, effort = self.role_model(name, role)
        key = (name, model, effort)
        if key not in self._providers:
            prov = make_provider(self.s, name, model=model, effort=effort) if (model or effort) \
                else make_provider(self.s, name)
            if (adopt := getattr(prov, "adopt_sign_in", None)) is not None:
                for (n, *_), other in self._providers.items():   # the sign-in is the machine's: reuse a verified one
                    if n == name:
                        adopt(other)
            if hasattr(prov, "caps_dir"):              # Phase 5 A7: the CLI capability file lives in data/shared
                prov.caps_dir = self.s.paths.shared()
            self._providers[key] = prov
            if prov.name not in self._limiters:        # one limiter per provider: every role shares the daily cap
                self._limiters[prov.name] = RateLimiter(prov.name, prov.cfg, self.usage,
                                                        instance=self.s.paths.instance,
                                                        daily_cap=self.s.ai.daily_calls_per_pair)
        return self._providers[key]

    def _set_route(self, name: str, why: str | None) -> None:
        if (name, why) != self.route:
            if why:
                log.warning("AI provider %s unavailable (%s) — using fallback %s", self.s.ai.active_provider, why, name)
            elif self.route[1]:
                log.info("AI provider %s available again", name)
            self.route = (name, why)

    def effective_mode(self) -> tuple[str, str]:
        st = self.governor.state()
        mode = self.s.ai.agent_mode
        if st.level >= 3:
            return "paused", st.reason
        if st.level >= 1 and mode not in ("agent_per_pair",):
            return "agent_per_pair", f"degraded by Cost Governor: {st.reason}"
        return mode, "configured"

    def quota(self, name: str | None = None) -> tuple[int | None, int | None]:
        """(requests left in the provider's quota day, its daily cap) for ``name`` or the provider in use."""
        lim = self._limiters.get(name or self.route[0])
        return (lim.remaining_today(), lim.cap()) if lim else (None, None)

    def quota_used(self, name: str | None = None) -> tuple[int, int] | None:
        """(calls this pair made in the quota day, ``ai.daily_calls_per_pair``) for ``name`` or the provider in use —
        None for the all-pairs system or before the provider's first use (the session-aware quota reserve)."""
        lim = self._limiters.get(name or self.route[0])
        return lim.used_today() if lim else None

    def default_account(self) -> dict:
        return {"equity": self.s.execution.paper_equity, "currency": "USD", "mode": self.s.execution.mode,
                "equity_source": "configured account size"}

    def payload(self, pair: str, as_of: int, account: dict | None = None) -> dict:
        """The snapshot the model receives for ``pair`` at ``as_of`` (account block + recent decisions)."""
        return self.builder.build(pair, as_of, account=account or self.default_account(),
                                  history=self.store.recent(pair), memory=self.store.memory(pair),
                                  performance=self.store.performance(pair))

    def horizon_ms(self, pair: str | None) -> int:
        """Latest allowed ``valid_until`` after the cycle time: 4 decision bars."""
        return 4 * (self.s.pairs[pair].decision_timeframe.ms if pair else 900_000)

    def _system_vars(self, pair: str | None, account: dict, tn=None) -> dict:
        """Values of the SYSTEM prompt: they must not change from cycle to cycle, or the CLI's prompt cache and the
        prompt hash break (the live equity lives in the user prompt — Phase 3 fix). ``tn``: the unit's tunables
        snapshot (default: read now)."""
        r = self.s.risk
        tn = tn if tn is not None else tunables_of(self, pair)
        pairs = list(self.s.enabled_pairs())
        return {
            "pair": pair or ", ".join(pairs), "pair_list": ", ".join(pairs),
            "decision_tf": (self.s.pairs[pair].decision_timeframe.value if pair else "15m"),
            "sl_min_atr": r.sl_atr_min_mult, "sl_max_atr": r.sl_atr_max_mult, "min_rr": r.min_rr,
            "max_risk_pct": r.max_risk_per_trade_pct, "max_spread_pct": round(r.max_spread_to_sl_ratio * 100),
            # the confidence floor in force (a raised adaptive floor is the one the gate applies — rare changes, so
            # the system prompt stays cacheable)
            "min_confidence": tn.min_confidence,
            "max_rec_age_min": round(r.max_recommendation_age_s / 60),
            "price_reference": self.reg.primary(pair).key if pair else "each pair's meta.price_reference",
            "output_language": "English" if self.s.ai.output_language == "en" else self.s.ai.output_language,
            "sl_change_minutes": self.s.execution.position_actions.min_minutes_between_sl_changes,
            "actions_per_day": self.s.execution.position_actions.max_per_pair_per_day,
        }

    def _user_vars(self, pair: str | None, as_of: int, reason: str, payload_json: str, *,
                   account: dict | None = None, tn=None, **extra) -> dict:
        """Values of the user prompt; ``tn``: the unit's tunables snapshot (default: read now)."""
        account = account or self.default_account()
        eq = account.get("equity", self.s.execution.paper_equity)
        # the pair's playbook and take-profit hint (adaptive overlay)
        tn = tn if tn is not None else tunables_of(self, pair)
        return {"now_utc": iso(as_of), "trigger_reason": reason, "payload": payload_json,
                "max_valid_until": iso(as_of + self.horizon_ms(pair)),
                "account_equity": f"{float(eq):.2f}" if eq is not None else "unknown",
                "account_currency": account.get("currency", "USD"),
                "charts_note": NO_CHARTS, "playbook": tn.playbook or NO_PLAYBOOK, "tp_hint": tn.tp_hint or NO_TP_HINT,
                **extra}

    def _brief(self, pair: str | None) -> tuple[str, ...]:
        """What is appended to a pair's trader, reviewer and escalation prompts (B18, D-049): the desk brief of a desk
        pair, and the pair's own field notes (``desks/<pair>_fields``: the legend of its pair-only payload blocks) while
        its payload can carry them - a desk, a news blackout or context instruments - so turning the desk off never
        leaves `market.news` or the cross votes without their legend. Empty for BTC/ETH (byte-identical prompts)."""
        pcfg = self.s.pairs.get(pair) if pair else None
        if pcfg is None:
            return ()
        out = [pcfg.desk.brief] if pcfg.desk is not None and pcfg.desk.brief else []
        fields = f"desks/{pair.lower()}_fields"
        if (PROMPTS_DIR / f"{fields}.md").is_file() and (
                pcfg.desk is not None or pcfg.news_blackout.enabled
                or any("cross_context" in i.roles for i in pcfg.instruments)):
            out.append(fields)
        return tuple(out)

    def _render(self, role: str, system_vars: dict, user_vars: dict, appendix: tuple[str, ...] | str | None = None):
        """Render a role and register the prompt versions it used (``prompt_versions``; never fails the cycle)."""
        pr = render(role, system_vars, user_vars, appendix=appendix)
        try:
            from .prompts import register_versions
            register_versions(self.store, pr, role, lib_hash=self.library_hash)
        except Exception:  # noqa: BLE001
            log.debug("prompt versions of %s not registered", role, exc_info=True)
        return pr

    async def _gen(self, provider: LLMProvider, model_cls, system: str, user: str, purpose: str,
                   pair: str | None, *, images: list[ImageInput] | None = None,
                   role: str | None = "decision", until: float | None = None) -> Generation:
        """``until``: the ``time.monotonic()`` this call must be done by (default: the running cycle's deadline) —
        a transient provider error is retried only when the retry still fits before it."""
        try:
            async with self._sem:
                kw = {"images": images, "role": role} if (images or role) else {}
                return await generate_validated(provider, model_cls, system=system, user=user,
                                                limiter=self._limiters[provider.name], governor=self.governor,
                                                usage=self.usage, purpose=purpose,
                                                # one system per pair: every call counts towards its share (D-042)
                                                pair=pair or self.s.paths.instance,
                                                est_input_tokens=max(2000, len(user) // 3),
                                                transient_retry_s=self.s.ai.transient_retry_s,
                                                retry_until=until if until is not None else _CYCLE_END.get(), **kw)
        except Exception as exc:  # noqa: BLE001 — one failed sub-call must not take its siblings down (F4)
            log.exception("%s %s: generation failed", pair or "*", purpose)
            msg = f"{type(exc).__name__}: {exc}"[:300]
            return Generation(False, None, errors=[msg], provider_error=msg)

    # ------------------------------------------------------------------ entry point
    async def charts_for(self, pair: str, as_of: int, payload: dict) -> tuple[list[ImageInput], str]:
        """The pair's chart images (rendered in a worker thread, cached per closed bar) and the cycle-message note.
        Never fails the cycle: without charts the call goes out as text only."""
        if self.charts is None or not self.s.ai.charts.enabled:
            return [], NO_CHARTS
        inst = self.reg.primary(pair)
        out = self.s.paths.state() / "charts" / pair

        def render() -> list:
            cal = calendar_for(inst.venue, inst.symbol, self.s.pairs[pair].asset_class)
            imgs = self.charts.render_set(self.builder.reader(inst), inst, as_of, payload, cal)
            try:                                  # the latest set, for the dashboard (never a reason to fail)
                out.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                log.warning("%s: could not store the chart PNGs: %s", pair, exc)
                return imgs
            done = set()
            for c in imgs:                        # each file on its own: one locked file never stops the others
                try:
                    tmp = out / f"{c.tf}.png.tmp"
                    tmp.write_bytes(c.png)
                    tmp.replace(out / f"{c.tf}.png")
                    done.add(c.tf)
                except OSError as exc:
                    log.warning("%s: could not store the %s chart: %s", pair, c.tf, exc)
            for tf in self.s.ai.charts.timeframes:    # a timeframe not rendered this time: no stale picture left
                if tf not in done and tf not in {c.tf for c in imgs}:
                    try:
                        (out / f"{tf}.png").unlink(missing_ok=True)
                    except OSError:
                        pass
            return imgs

        try:
            imgs = await asyncio.to_thread(render)
        except Exception:  # noqa: BLE001
            log.exception("%s: chart rendering failed — text-only call", pair)
            return [], NO_CHARTS
        if not imgs:
            return [], NO_CHARTS
        cfg = self.s.ai.charts
        note = (f"Attached: {len(imgs)} charts ({', '.join(c.tf for c in imgs)}), {cfg.width}×{cfg.height}, the last "
                f"closed candles of each timeframe, analysis prices ({inst.key}).")
        return [c.as_input(pair, inst.key) for c in imgs], note

    async def run_cycle(self, requests: list[CycleRequest], *, as_of: int | None = None,
                        account: dict | None = None, payloads: dict[str, dict] | None = None,
                        deadline_s: float | None = None) -> list[DecisionRecord]:
        """Run one cycle; ``payloads`` may carry snapshots the caller already built for this ``as_of``. Every
        requested pair ends with exactly one stored record, even on failure or when the deadline cuts it off."""
        as_of = as_of or now_ms()
        account = account or self.default_account()
        deadline_s = self.s.ai.cycle_deadline_s if deadline_s is None else deadline_s
        mode, why = self.effective_mode()
        label = mode if why == "configured" else f"{mode} ({why})"
        reasons = {rq.pair: rq.reason for rq in requests}
        # per call, never shared state: cycles of different pairs overlap in the all-pairs layout
        trig = {rq.pair: (rq.strength, rq.setup, rq.setup_alone) for rq in requests}
        built: dict[str, dict] = {}
        out: list[DecisionRecord] = []
        for rq in requests:
            try:
                p = (payloads or {}).get(rq.pair) or self.payload(rq.pair, as_of, account)
                self.store.save_payload(p["meta"]["payload_hash"], rq.pair, p)
                built[rq.pair] = p
            except Exception as exc:  # noqa: BLE001
                log.exception("%s: snapshot failed", rq.pair)
                out.append(self._finish(DecisionRecord(rq.pair, label, rq.reason, "error",
                                                       errors=[f"snapshot failed: {exc!r}"[:300]]), label, as_of, None,
                                        trig))
        if mode == "paused":
            return out + [self._finish(self._failed(p, mode, reasons[p], built[p], why, "budget_blocked"), label,
                                       as_of, built[p], trig) for p in built]
        clock0 = clock_sample()                  # a suspend inside the cycle turns its failed units 'interrupted'
        end = time.monotonic() + deadline_s if deadline_s else None
        units = self._units(mode, built, reasons, as_of, account, trig)
        cycle_end = _CYCLE_END.set(end)          # copied into every unit task created below (transient retry room)
        try:
            tasks = {asyncio.ensure_future(coro): pairs for pairs, coro in units}
        finally:
            _CYCLE_END.reset(cycle_end)
        pending = set(tasks)
        cut = False
        try:
            while pending:
                timeout = None if end is None else max(0.0, end - time.monotonic())
                done, pending = await asyncio.wait(pending, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
                if not done:
                    cut = True
                    break
                for t in done:
                    try:
                        recs = t.result()
                    except Exception as exc:  # noqa: BLE001
                        log.error("AI unit %s failed: %r", tasks[t], exc, exc_info=exc)
                        recs = [self._failed(p, mode, reasons[p], built[p], f"{type(exc).__name__}: {exc}")
                                for p in tasks[t]]
                    out += [self._finish(self._interrupted(rec, clock0), label, as_of, built.get(rec.pair), trig)
                            for rec in recs]
        finally:
            # deadline or shutdown: cancelling kills any running CLI call (and writes its ledger row, D-043)
            why = f"cycle deadline of {deadline_s:.0f}s reached" if cut else "the AI cycle was stopped"
            for t in pending:
                t.cancel(why)
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        for t in pending:
            log.warning("AI cycle deadline (%.0fs) reached — cancelled %s", deadline_s, tasks[t])
            out += [self._finish(self._interrupted(self._failed(p, mode, reasons[p], built[p],
                                                                f"cycle deadline of {deadline_s:.0f}s reached — "
                                                                "cancelled"), clock0),
                                 label, as_of, built[p], trig) for p in tasks[t]]
        return out

    @staticmethod
    def _interrupted(rec: DecisionRecord, since: ClockSample | None) -> DecisionRecord:
        """An unanswered unit ('error') of a cycle during which the machine slept more than ``SUSPEND_GAP_S`` is
        'interrupted': the suspend, not the provider, cut it off — the engine does not back off for it and its setup
        and events stay unseen (the next screens call again)."""
        if rec.status != "error":
            return rec
        slept = asleep_since(since)
        if slept > SUSPEND_GAP_S:
            rec.status = "interrupted"
            rec.errors.append(f"interrupted: the PC was asleep ~{slept:.0f}s during the cycle")
            log.warning("%s: AI call interrupted by a suspend (~%.0fs asleep) — stored as 'interrupted'", rec.pair,
                        slept)
        return rec

    def _units(self, mode: str, payloads: dict, reasons: dict, as_of: int,
               account: dict, trig: dict | None = None) -> list[tuple[list[str], object]]:
        """(pairs, coroutine → list[DecisionRecord]) per independent unit of work."""
        if not payloads:
            return []
        if mode == "single_agent_global":
            return [(list(payloads), self._global(payloads, reasons, as_of, account))]
        if mode == "agent_per_timeframe":
            return [(list(payloads), self._per_timeframe(payloads, reasons, as_of, account))]
        if mode == "agent_per_pair_and_timeframe":
            job = lambda p: self._pair_and_tf(p, payloads[p], reasons[p], as_of, account)  # noqa: E731
        elif mode == "multi_provider_consensus":
            job = lambda p: self._consensus(p, payloads[p], reasons[p], as_of, account,  # noqa: E731
                                            trigger=(trig or {}).get(p))
        else:
            review = mode == "agent_per_pair_with_risk_reviewer"
            job = lambda p: self._per_pair(p, payloads[p], reasons[p], as_of, account, review=review,  # noqa: E731
                                           charts=True, trigger=(trig or {}).get(p))
        return [([p], _as_list(job(p))) for p in payloads]

    @staticmethod
    def _failed(pair: str, mode: str, reason: str, payload: dict, error: str, status: str = "error") -> DecisionRecord:
        return DecisionRecord(pair, mode, reason, status, payload_hash=payload["meta"]["payload_hash"],
                              errors=[error[:300]])

    def _finish(self, rec: DecisionRecord, label: str, as_of: int, payload: dict | None,
                trig: dict | None = None) -> DecisionRecord:
        rec.mode = label
        rec.library_hash = rec.library_hash or self.library_hash
        t = (trig or {}).get(rec.pair) or (None, None, False)
        rec.trigger_strength = rec.trigger_strength or t[0]
        rec.setup_strength = rec.setup_strength or (t[1] if t[2] else None)
        try:
            self._attribute(rec, payload)
        except Exception:  # noqa: BLE001 — attribution is for the learning loop, never a reason to lose a record
            log.exception("%s: attribution failed", rec.pair)
        if rec.status == "valid" and rec.recommendation:
            try:
                self._finalize(rec, as_of, payload)
            except Exception as exc:  # noqa: BLE001 — never release what could not be normalised
                log.exception("%s: normalisation failed", rec.pair)
                _reject(rec, f"normalisation failed: {exc!r}"[:300])
        self.store.save(rec)
        log.info("%s %s: %s %s conf=%s cost=$%.4f", rec.pair, rec.status,
                 (rec.recommendation or {}).get("decision"), (rec.recommendation or {}).get("order_type"),
                 (rec.recommendation or {}).get("confidence"), rec.cost_usd)
        return rec

    def _attribute(self, rec: DecisionRecord, payload: dict | None) -> None:
        """Phase 4 attribution at record time (what the market looked like, what was in force): setup kinds on
        screen, session (killzone, else the active sessions), the decision-TF regime, the higher-timeframe bias,
        the data warnings, and the pair's playbook / adaptive hashes. A record whose prompts were rendered carries
        that unit's tunables snapshot (:meth:`_stamp`) — its hashes stand, even when the overlay changed during the
        model call; only a record without one (no prompt rendered: snapshot failed, paused, cancelled) reads now."""
        snap = rec.tunables
        tn = snap if snap is not None else tunables_of(self, rec.pair)
        if snap is None:
            rec.playbook_hash = rec.playbook_hash or tn.playbook_hash
            rec.adaptive_hash = rec.adaptive_hash or tn.adaptive_hash
        if not payload:
            return
        from .triggers import scan_setups
        meta = payload.get("meta") or {}
        dec = meta.get("decision_timeframe") or self.s.pairs[rec.pair].decision_timeframe.value
        kinds = sorted({r.kind for r in scan_setups(payload, liquidity_atr=tn.liquidity_atr,
                                                    screen_tf=self._screen_tf(rec.pair))})
        rec.setup_kinds = rec.setup_kinds if rec.setup_kinds is not None else kinds
        sess = ((payload.get("market") or {}).get("session")) or {}
        rec.session = rec.session or sess.get("killzone") or ("+".join(sess.get("active") or []) or None)
        reg = (((payload.get("timeframes") or {}).get(dec) or {}).get("regime")) or {}
        if reg:
            rec.regime = rec.regime or f"{reg.get('trend_strength') or '?'}/{reg.get('volatility') or '?'}"
        rec.htf_bias = rec.htf_bias or (payload.get("confluence") or {}).get("bias")
        if rec.data_warnings is None:
            rec.data_warnings = list(meta.get("data_warnings") or [])

    def _screen_tf(self, pair: str) -> str | None:
        """The engine's screening timeframe for ``pair`` (5m when collected and shorter than the decision TF)."""
        pcfg = self.s.pairs[pair]
        try:
            stf = Timeframe.parse(self.s.ai.screen_timeframe)
        except ValueError:
            return None
        tfs = {Timeframe.parse(t).value for t in (pcfg.timeframes or self.s.timeframes)}
        return stf.value if stf.ms < pcfg.decision_timeframe.ms and stf.value in tfs else None

    # ------------------------------------------------------------------ system-owned fields (F6, AI-01)
    def _finalize(self, rec: DecisionRecord, as_of: int, payload: dict | None) -> None:
        """pair and timestamp (= the cycle's as-of) are set here; valid_until is kept in (as_of, as_of + horizon];
        price_reference must be the analysis instrument; next_review never sooner than the review floor and without
        conditions already met at as_of. The result is validated against the contract again."""
        r = dict(rec.recommendation)
        notes: list[str] = []
        prim = self.reg.primary(rec.pair).key
        execu = self.reg.with_role(rec.pair, "execution")[0].key
        trade = r.get("decision") != Decision.NO_TRADE.value
        ref = r.get("price_reference")
        if ref != prim:
            if ref == execu:
                # the prices were read in the execution instrument: priced actions would be shifted by the basis
                # a second time — dropped (close / cancel_order carry no price and stand)
                acts = r.get("position_actions") or []
                keep = [a for a in acts if a.get("action") not in ("modify_sl", "modify_tp")]
                if len(keep) != len(acts):
                    notes.append(f"{len(acts) - len(keep)} priced position action(s) dropped: price_reference "
                                 f"{ref!r} is the execution instrument")
                r["position_actions"] = keep
                if trade:
                    why = f"price_reference {ref!r} is the execution instrument — levels must be in {prim} prices"
                    if not keep:
                        return _reject(rec, why)
                    r, trade = _as_no_trade(r, why), False
                    rec.errors.append(f"{why} — the trade is withheld; its position_actions stand")
            notes.append(f"price_reference {ref!r} -> {prim}")
            r["price_reference"] = prim
        r["pair"], r["timestamp"] = rec.pair, iso(as_of)
        cap = as_of + self.horizon_ms(rec.pair)
        vu = parse_date_spec(str(r.get("valid_until")))
        if vu > cap:
            r["valid_until"] = iso(cap)
            notes.append(f"valid_until {iso(vu)} clamped to {iso(cap)}")
        elif vu <= as_of:
            if trade:
                why = f"valid_until {iso(vu)} is not after the cycle time {iso(as_of)}"
                if not r.get("position_actions"):
                    return _reject(rec, why)
                r, trade = _as_no_trade(r, why), False
                rec.errors.append(f"{why} — the trade is withheld; its position_actions stand")
            r["valid_until"] = iso(as_of + self.s.pairs[rec.pair].decision_timeframe.ms)
        r["next_review"] = self._review_plan(r.get("next_review") or {}, payload, notes, rec.pair)
        r["position_actions"] = self._action_targets(r.get("position_actions") or [], payload, notes)
        try:
            v = Recommendation.model_validate(r)
        except ValidationError as exc:
            return _reject(rec, f"normalised recommendation invalid: {exc.errors()[:3]}"[:300])
        rec.recommendation = json.loads(v.model_dump_json())
        rec.rr_computed = v.rr_computed()
        rec.actions_state = "pending" if v.position_actions else None
        if notes:
            rec.errors.append("normalised: " + "; ".join(notes))

    @staticmethod
    def _action_targets(actions: list[dict], payload: dict | None, notes: list[str]) -> list[dict]:
        """Keep only actions whose target is a live position / pending order of this pair in the payload the model
        saw (the executor re-checks everything against the broker before acting)."""
        acct = (payload or {}).get("account") or {}
        live = {("position", str(x.get("decision"))) for x in acct.get("open_positions") or []}
        live |= {("order", str(x.get("decision"))) for x in acct.get("pending_orders") or []}
        keep = []
        for a in actions:
            t = a.get("target") or {}
            if (t.get("kind"), str(t.get("decision"))) in live:
                keep.append(a)
            else:
                notes.append(f"position action {a.get('action')} on {t.get('kind')} {t.get('decision')} dropped "
                             "(not a live trade of this pair)")
        return keep

    def _review_plan(self, nr: dict, payload: dict | None, notes: list[str], pair: str | None = None) -> dict:
        """Time-based reviews never come sooner than the normal spacing; price/candle conditions (something
        happened) may, down to ``ai.review_floor_minutes`` (enforced by the trigger policy). Both are the pair's
        effective values (adaptive overlay)."""
        tn = tunables_of(self, pair)
        floor = max(tn.review_floor_minutes, tn.min_minutes_between_calls)
        nr = dict(nr)
        if int(nr.get("in_minutes") or floor) < floor:
            nr["in_minutes"] = floor
            notes.append(f"next_review.in_minutes raised to {floor}")
        price = _analysis_mid(payload)
        closes = {tf: t["recent"][-1][4] for tf, t in ((payload or {}).get("timeframes") or {}).items()
                  if t.get("recent")}
        keep = []
        for c in nr.get("conditions") or []:
            k, v = c.get("kind"), c.get("value")
            close = closes.get(c.get("timeframe") or "15m")
            met = ((k == "price_above" and price is not None and price > v)
                   or (k == "price_below" and price is not None and price < v)
                   or (k == "candle_close_above" and close is not None and close > v)
                   or (k == "candle_close_below" and close is not None and close < v))
            if met:
                notes.append(f"review condition {k} {v} dropped (already met at the cycle time)")
                continue
            if k == "minutes_elapsed" and v < floor:
                c = {**c, "value": float(floor)}
                notes.append(f"review condition minutes_elapsed raised to {floor}")
            keep.append(c)
        nr["conditions"] = keep
        return nr

    # ------------------------------------------------------------------ helpers
    def _record(self, pair: str, mode: str, reason: str, payload: dict, gen: Generation, provider: LLMProvider,
                prompt_hash: str, rec: Recommendation | None = None, use_gen_value: bool = True) -> DecisionRecord:
        r = DecisionRecord(pair=pair, mode=mode, trigger=reason, status="valid", provider=provider.name,
                           model=(gen.attempts[-1].model if gen.attempts else provider.model), prompt_hash=prompt_hash,
                           payload_hash=payload["meta"]["payload_hash"], raw_text=gen.raw_text,
                           errors=list(gen.errors), cost_usd=gen.cost_usd,
                           latency_ms=sum(a.latency_ms for a in gen.attempts),
                           input_tokens=sum(a.input_tokens for a in gen.attempts),
                           output_tokens=sum(a.output_tokens for a in gen.attempts))
        value = rec if rec is not None else (gen.value if gen.ok and use_gen_value else None)
        if value is None:
            r.status = ("budget_blocked" if gen.budget_blocked else "refused" if gen.refused
                        else "error" if gen.provider_error else "invalid")
            return r
        if value.pair != pair:
            value = value.model_copy(update={"pair": pair})
        r.recommendation = json.loads(value.model_dump_json())
        r.rr_computed = value.rr_computed()
        return r

    @staticmethod
    def _stamp(rec: DecisionRecord, tn) -> DecisionRecord:
        """Attribute ``rec`` to the tunables snapshot its prompts were rendered with (the playbook / adaptive hashes,
        None included — :meth:`_attribute` then keeps them and uses the snapshot's other values)."""
        rec.tunables, rec.playbook_hash, rec.adaptive_hash = tn, tn.playbook_hash, tn.adaptive_hash
        return rec

    # ------------------------------------------------------------------ modes
    async def _per_pair(self, pair: str, payload: dict, reason: str, as_of: int, account: dict, *,
                        review: bool = False, provider_name: str | None = None, charts: bool = False,
                        trigger: tuple[str | None, str | None] | None = None, tn=None) -> DecisionRecord:
        # one tunables snapshot for the unit: every prompt and the record's attribution see the same overlay, even
        # when tools/tune.py changes it (or an entry expires) during the model call
        tn = tn if tn is not None else tunables_of(self, pair)
        prov = self.provider(provider_name)
        # a provider that cannot read images (e.g. the text-only fallback) gets no charts and is not told of any
        charts = charts and bool(getattr(prov, "supports_images", False))
        images, note = await self.charts_for(pair, as_of, payload) if charts else ([], NO_CHARTS)
        uv = self._user_vars(pair, as_of, reason, _dump(payload), account=payload.get("account") or account,
                             tn=tn, charts_note=note)
        pr = self._render("agent_per_pair", self._system_vars(pair, account, tn), uv, appendix=self._brief(pair))
        gen = await self._gen(prov, Recommendation, pr.system, pr.user, "agent_per_pair", pair, images=images)
        rec = self._stamp(self._record(pair, "agent_per_pair", reason, payload, gen, prov, pr.prompt_hash), tn)
        rec.trigger_strength, setup = (trigger or (None, None, False))[:2]
        if rec.status == "valid" and rec.recommendation["decision"] != Decision.NO_TRADE.value:
            await self._escalate(rec, pair, payload, reason, as_of, account, uv, images, setup=setup, tn=tn)
        if not review or rec.status != "valid" or rec.recommendation["decision"] == Decision.NO_TRADE.value:
            return rec
        rv = self._render("risk_reviewer", self._system_vars(pair, account, tn),
                    self._user_vars(pair, as_of, reason, _dump(payload), account=payload.get("account") or account,
                                    tn=tn, proposal=json.dumps(rec.recommendation)), appendix=self._brief(pair))
        g2 = await self._gen(prov, RiskReview, rv.system, rv.user, "risk_reviewer", pair)
        rec.sub_outputs.append({"role": "trader", "label": pair, "provider": prov.name, "model": rec.model,
                                "prompt_hash": pr.prompt_hash, "ok": True, "output": rec.recommendation,
                                "cost_usd": gen.cost_usd})
        rec.sub_outputs.append({"role": "risk_reviewer", "label": pair, "provider": prov.name, "model": prov.model,
                                "prompt_hash": rv.prompt_hash, "ok": g2.ok,
                                "output": json.loads(g2.value.model_dump_json()) if g2.ok else None,
                                "errors": g2.errors, "cost_usd": g2.cost_usd})
        rec.cost_usd += g2.cost_usd
        if g2.ok:
            final = g2.value.final_recommendation
            rec.recommendation = json.loads(final.model_dump_json())
            rec.rr_computed = final.rr_computed()
            rec.errors.append(f"risk review: {g2.value.verdict}: " + "; ".join(g2.value.issues[:5]))
        else:
            _withhold(rec, "risk review failed")         # a trade that could not be reviewed is not released
        return rec

    def escalation_due(self, rec: DecisionRecord, pair: str, setup: str | None = None) -> str | None:
        """Why a strong trade idea goes to the stronger model first (None = no escalation). ``setup``: the setup
        strength when a setup fired together with a review condition or an executor event (the call is labelled by
        the review/event, the setup is still strong)."""
        esc = self.s.ai.escalation
        if not esc.enabled:
            return None
        why = rec.trigger_strength if rec.trigger_strength in esc.on_strength else setup if setup in esc.on_strength \
            else None
        if why is None:
            return None
        if int(rec.recommendation.get("confidence") or 0) < esc.min_confidence:
            return None
        day0 = now_ms() // MS_PER_DAY * MS_PER_DAY
        if self.usage.count_role_since("escalation", day0, pair) >= esc.max_per_day_per_pair:
            return None
        left, _ = self.quota()
        if left is not None and left < 2:
            return None
        return f"{why} setup, confidence {rec.recommendation.get('confidence')}"

    async def _escalation_provider(self, wait_s: float) -> LLMProvider:
        """The escalation role's provider: the ACTIVE provider with ``ai.models.escalation`` — never rerouted to the
        fallback (a text-only fallback confirming for "Opus" would defeat the check). A new instance waits (bounded)
        for its sign-in check; unavailable → raises, and ``escalation.on_failure`` decides."""
        prov = self._get(self.s.ai.active_provider, "escalation")
        end = time.monotonic() + wait_s
        why = prov.unavailable_reason()
        while getattr(prov, "availability_pending", False) and time.monotonic() < end:
            await asyncio.sleep(0.5)
            why = prov.unavailable_reason()
        if why:
            raise ProviderError(f"escalation provider unavailable: {why}", retryable=False)
        return prov

    async def _escalate(self, rec: DecisionRecord, pair: str, payload: dict, reason: str, as_of: int, account: dict,
                        uv: dict, images: list[ImageInput], *, setup: str | None = None, tn=None) -> None:
        """D-043: a stronger model (``ai.models.escalation``) may confirm the trade unchanged or downgrade it to
        NO_TRADE; it can never change levels, raise confidence or add risk. ``uv`` and ``tn``: the trader's user
        values and the unit's tunables snapshot (the same playbook and floor as the trader's prompt)."""
        why = self.escalation_due(rec, pair, setup)
        if why is None:
            return
        esc = self.s.ai.escalation
        first = rec.recommendation
        try:
            prov = await self._escalation_provider(min(ESCALATION_SIGN_IN_WAIT_S, esc.timeout_s / 2))
            if not getattr(prov, "supports_images", False):
                images, uv = [], {**uv, "charts_note": NO_CHARTS}
            pr = self._render("escalation", self._system_vars(pair, account, tn), {**uv, "proposal": json.dumps(first)},
                              appendix=self._brief(pair))
            until = time.monotonic() + esc.timeout_s        # its own time limit (a retry must fit it too)
            cycle_end = _CYCLE_END.get()
            g = await asyncio.wait_for(self._gen(prov, EscalationReview, pr.system, pr.user, "escalation", pair,
                                                 images=images, role="escalation",
                                                 until=min(until, cycle_end) if cycle_end is not None else until),
                                       esc.timeout_s)
        except Exception as exc:  # noqa: BLE001 — timeout, provider or prompt error: the failure policy decides
            g, prov = None, None
            err = f"escalation failed: {type(exc).__name__}: {exc}"[:300]
        rec.sub_outputs.append({"role": "trader", "label": pair, "provider": rec.provider, "model": rec.model,
                                "prompt_hash": rec.prompt_hash, "ok": True, "output": first, "cost_usd": rec.cost_usd})
        if g is not None:
            rec.sub_outputs.append({"role": "escalation", "label": why, "provider": prov.name, "model": prov.model,
                                    "prompt_hash": pr.prompt_hash, "ok": g.ok,
                                    "output": json.loads(g.value.model_dump_json()) if g.ok else None,
                                    "errors": g.errors, "cost_usd": g.cost_usd})
            rec.cost_usd += g.cost_usd
        if g is None or not g.ok:
            msg = err if g is None else "escalation invalid: " + "; ".join(g.errors[:3])
            if esc.on_failure == "withhold":
                _withhold(rec, msg)
            else:
                rec.errors.append(f"{msg} — trader's decision kept")
            self._notify("warn", f"{pair} escalation failed", f"{first.get('decision')} {msg[:200]} — "
                         + ("withheld" if esc.on_failure == "withhold" else "trader's decision kept"), pair=pair)
            return
        final = json.loads(g.value.final_recommendation.model_dump_json())
        if g.value.verdict == "confirm":
            changed = [k for k in ESCALATION_FIXED if final.get(k) != first.get(k)]
            if changed:
                _withhold(rec, f"escalation altered levels ({', '.join(changed)})")
                self._notify("warn", f"{pair} escalation", f"{first.get('decision')} withheld: the reviewer changed "
                             f"{', '.join(changed)}", pair=pair)
                return
            conf = min(int(first.get("confidence") or 0), int(g.value.confidence), int(final.get("confidence") or 0))
            rec.recommendation = {**first, "confidence": conf}
            rec.errors.append(f"escalation: confirmed ({why}); confidence {first.get('confidence')} → {conf}")
            self._notify("info", f"{pair} escalation", f"{first.get('decision')} confirmed by "
                         f"{getattr(prov, 'model', '?')}, confidence {conf}", pair=pair)
            return
        final["confidence"] = min(int(final.get("confidence") or 0), int(first.get("confidence") or 0))
        final["position_actions"] = first.get("position_actions") or []    # protective actions stand
        rec.recommendation = final
        rec.rr_computed = None
        rec.errors.append("escalation: downgraded to NO_TRADE — " + "; ".join(g.value.issues[:4]))
        self._notify("info", f"{pair} escalation", f"{first.get('decision')} downgraded to NO_TRADE: "
                     + "; ".join(g.value.issues[:2])[:300], pair=pair)

    def _notify(self, level: str, title: str, text: str, *, pair: str | None = None) -> None:
        try:
            from ..core.notify import notify
            notify(self.s, level, title, text, pair=pair)
        except Exception:  # noqa: BLE001 — a notification never fails a cycle
            log.debug("notify failed", exc_info=True)

    async def _global(self, payloads: dict, reasons: dict, as_of: int, account: dict) -> list[DecisionRecord]:
        prov = self.provider()
        snaps = {p: tunables_of(self, p) for p in payloads}     # what was in force when the prompt was built
        pr = self._render("single_agent_global", self._system_vars(None, account),
                    self._user_vars(None, as_of, "; ".join(f"{p}: {r}" for p, r in reasons.items()),
                                    _dump(list(payloads.values())), account=account))
        gen = await self._gen(prov, RecommendationSet, pr.system, pr.user, "single_agent_global", None)
        out = []
        by_pair = {r.pair: r for r in gen.value.recommendations} if gen.ok else {}
        for pair, payload in payloads.items():
            rec_v = by_pair.get(pair)
            rec = self._stamp(self._record(pair, "single_agent_global", reasons[pair], payload, gen, prov,
                                           pr.prompt_hash, rec_v, use_gen_value=False), snaps[pair])
            if gen.ok and rec_v is None:
                rec.status, rec.recommendation = "invalid", None
                rec.errors.append(f"model returned no recommendation for {pair}")
            rec.cost_usd = gen.cost_usd / max(len(payloads), 1)
            out.append(rec)
        return out

    def _slice(self, payload: dict, tf: str) -> dict:
        keep = {k: payload[k] for k in ("meta", "market", "capabilities", "levels")}
        keep["timeframe"] = {tf: payload["timeframes"].get(tf)}
        if tf == payload["meta"]["decision_timeframe"] or tf in ("5m", "1m"):
            keep["orderflow"] = payload["orderflow"]
        if tf in ("4h", "1d", "1w", "1h"):
            keep["derivatives"] = payload["derivatives"]
        return keep

    async def _per_timeframe(self, payloads: dict, reasons: dict, as_of: int, account: dict) -> list[DecisionRecord]:
        prov = self.provider()
        pairs = list(payloads)
        snaps = {p: tunables_of(self, p) for p in pairs}         # one snapshot per pair for its coordinator

        async def analyst(tf: str):
            sys_vars = {**self._system_vars(None, account), "timeframe": tf, "scope_text": f" across {', '.join(pairs)}"}
            pr = self._render("timeframe_analyst", sys_vars,
                              {"now_utc": iso(as_of), "payload": _dump([self._slice(payloads[p], tf) for p in pairs])})
            g = await self._gen(prov, AssessmentSet, pr.system, pr.user, f"analyst_{tf}", None)
            return tf, pr, g
        results = await asyncio.gather(*(analyst(tf) for tf in ANALYST_TFS))
        out = []
        for pair in pairs:
            assessments = []
            subs = []
            for tf, pr, g in results:
                a = [x for x in (g.value.assessments if g.ok else []) if x.pair == pair]
                assessments += [json.loads(x.model_dump_json()) for x in a]
                subs.append({"role": "timeframe_analyst", "label": tf, "provider": prov.name, "model": prov.model,
                             "prompt_hash": pr.prompt_hash, "ok": g.ok and bool(a),
                             "output": [json.loads(x.model_dump_json()) for x in a], "errors": g.errors,
                             "cost_usd": g.cost_usd / len(pairs),
                             "answered": not (g.provider_error or g.budget_blocked)})
            rec = await self._coordinate(pair, payloads[pair], reasons[pair], as_of, account, assessments, subs, prov,
                                         "agent_per_timeframe", tn=snaps[pair])
            out.append(rec)
        return out

    async def _pair_and_tf(self, pair: str, payload: dict, reason: str, as_of: int, account: dict) -> DecisionRecord:
        prov = self.provider()
        tn = tunables_of(self, pair)                              # one snapshot for the analysts and the coordinator

        async def analyst(tf: str):
            sys_vars = {**self._system_vars(pair, account, tn), "timeframe": tf, "scope_text": f" for {pair}"}
            pr = self._render("timeframe_analyst", sys_vars,
                              {"now_utc": iso(as_of), "payload": _dump(self._slice(payload, tf))})
            g = await self._gen(prov, TimeframeAssessment, pr.system, pr.user, f"analyst_{tf}", pair)
            return tf, pr, g
        results = await asyncio.gather(*(analyst(tf) for tf in ANALYST_TFS))
        assessments = [json.loads(g.value.model_dump_json()) for _, _, g in results if g.ok]
        subs = [{"role": "timeframe_analyst", "label": tf, "provider": prov.name, "model": prov.model,
                 "prompt_hash": pr.prompt_hash, "ok": g.ok,
                 "output": json.loads(g.value.model_dump_json()) if g.ok else None, "errors": g.errors,
                 "cost_usd": g.cost_usd, "answered": not (g.provider_error or g.budget_blocked)}
                for tf, pr, g in results]
        return await self._coordinate(pair, payload, reason, as_of, account, assessments, subs, prov,
                                      "agent_per_pair_and_timeframe", tn=tn)

    async def _coordinate(self, pair: str, payload: dict, reason: str, as_of: int, account: dict,
                          assessments: list[dict], subs: list[dict], prov: LLMProvider, mode: str,
                          tn=None) -> DecisionRecord:
        tn = tn if tn is not None else tunables_of(self, pair)
        compact = {k: payload[k] for k in ("meta", "account", "market", "capabilities", "levels", "confluence", "history",
                                           "memory", "performance") if k in payload}
        pr = self._render("coordinator", self._system_vars(pair, account, tn),
                    self._user_vars(pair, as_of, reason, _dump(compact), account=payload.get("account") or account,
                                    tn=tn, assessments=json.dumps(assessments)))
        if not assessments:
            # no model answered at all (usage limit, provider down, budget) is an error — the setup is not seen;
            # 'invalid' only when an analyst answered but nothing usable came back
            answered = any(s.get("answered", True) for s in subs)
            rec = DecisionRecord(pair, mode, reason, "invalid" if answered else "error", provider=prov.name,
                                 model=prov.model, payload_hash=payload["meta"]["payload_hash"],
                                 errors=["no valid timeframe assessments — coordinator not called"])
        else:
            g = await self._gen(prov, Recommendation, pr.system, pr.user, "coordinator", pair)
            rec = self._record(pair, mode, reason, payload, g, prov, pr.prompt_hash)
        rec.sub_outputs = subs
        rec.cost_usd += sum(s.get("cost_usd", 0.0) for s in subs)
        return self._stamp(rec, tn)

    async def _consensus(self, pair: str, payload: dict, reason: str, as_of: int, account: dict,
                         trigger: tuple | None = None) -> DecisionRecord:
        names = self.s.ai.consensus_providers or [self.s.ai.active_provider]
        tn = tunables_of(self, pair)                              # every member sees the same overlay
        got = await asyncio.gather(*(self._per_pair(pair, payload, reason, as_of, account, provider_name=n,
                                                    trigger=trigger, tn=tn) for n in names), return_exceptions=True)
        members = [m if isinstance(m, DecisionRecord) else
                   DecisionRecord(pair, "agent_per_pair", reason, "error", provider=n, errors=[repr(m)[:300]])
                   for n, m in zip(names, got)]
        valid = [m for m in members if m.status == "valid"]
        base = members[0]
        rec = DecisionRecord(pair, "multi_provider_consensus", reason, "valid", provider="+".join(names),
                             model="+".join(m.model or "?" for m in members), prompt_hash=base.prompt_hash,
                             payload_hash=payload["meta"]["payload_hash"],
                             cost_usd=sum(m.cost_usd for m in members),
                             sub_outputs=[{"role": "consensus_member", "label": m.provider, "provider": m.provider,
                                           "model": m.model, "prompt_hash": m.prompt_hash, "ok": m.status == "valid",
                                           "output": m.recommendation, "errors": m.errors, "cost_usd": m.cost_usd}
                                          for m in members])
        agg = aggregate_consensus([m.recommendation for m in valid], len(members), pair, as_of)
        rec.recommendation = agg
        rec.rr_computed = Recommendation.model_validate(agg).rr_computed()
        return self._stamp(rec, tn)


def aggregate_consensus(recs: list[dict], n_members: int, pair: str, as_of: int) -> dict:
    """Deterministic: trade only if a strict majority of *all* members agree on the direction; then take the
    agreeing proposal with the best recomputed RR and the *lowest* confidence among the agreeing members."""
    dirs = [r["decision"] for r in recs if r["decision"] != "NO_TRADE"]
    for side in ("BUY", "SELL"):
        agree = [r for r in recs if r["decision"] == side]
        if len(agree) * 2 > n_members:
            best = max(agree, key=lambda r: Recommendation.model_validate(r).rr_computed() or 0)
            out = dict(best)
            out["confidence"] = min(r["confidence"] for r in agree)
            out["reasoning_trace"] = (f"[consensus {len(agree)}/{n_members} {side}] " + best["reasoning_trace"])[:3000]
            return out
    ts = dt.datetime.fromtimestamp(as_of / 1000, dt.timezone.utc)
    return json.loads(Recommendation(
        pair=pair, timestamp=ts, valid_until=ts + dt.timedelta(minutes=15),
        price_reference=recs[0]["price_reference"] if recs else "n/a",
        market_summary=f"No majority among {n_members} providers (directions: {dirs or 'none'}).",
        decision=Decision.NO_TRADE, confidence=0, next_review={"in_minutes": 15},
        reasoning_trace="Consensus rule: a trade requires a strict majority of providers agreeing on the direction.",
    ).model_dump_json())


def _dump(obj) -> str:
    """The text the model reads: payloads go through the compact model view (the stored payload is unchanged)."""
    return json.dumps(view(obj), separators=(",", ":"), default=str, ensure_ascii=False)


async def _as_list(coro) -> list[DecisionRecord]:
    return [await coro]


def _reject(rec: DecisionRecord, why: str) -> None:
    rec.status, rec.recommendation, rec.rr_computed = "invalid", None, None
    rec.errors.append(why)


def _as_no_trade(r: dict, why: str = "") -> dict:
    """The recommendation without its new trade (NO_TRADE carries no order, entry, stop or targets). Its notes and
    summary say that the trade was NOT placed — they feed the model's memory and history on the next cycle."""
    tag = (f"[system: your {r.get('decision')} {r.get('order_type') or ''} was NOT placed"
           f"{' — ' + why[:160] if why else ''}; only the position_actions were carried out] ")
    return {**r, "decision": Decision.NO_TRADE.value, "order_type": None, "entry": None, "stop_loss": None,
            "take_profits": [], "risk_management": None, "management": [],
            "operator_notes": (tag + str(r.get("operator_notes") or ""))[:600],
            "market_summary": (tag + str(r.get("market_summary") or ""))[:1500]}


def _withhold(rec: DecisionRecord, why: str) -> None:
    """A new trade that is not released (escalation failed or altered it, risk review failed). Its
    ``position_actions`` on live trades still stand — the record becomes a valid NO_TRADE carrying them, exactly as
    a downgrade does; without actions the record is invalid, as before."""
    r = rec.recommendation or {}
    if not r.get("position_actions"):
        rec.status, rec.recommendation, rec.rr_computed = "invalid", None, None
        rec.errors.append(f"{why} — trade withheld")
        return
    rec.status, rec.recommendation, rec.rr_computed = "valid", _as_no_trade(r, why), None
    rec.errors.append(f"{why} — trade withheld; its position_actions stand")


def _analysis_mid(payload: dict | None) -> float | None:
    """Analysis-instrument price at the payload's as-of (mid of bid/ask, else the last 1m close)."""
    ap = ((payload or {}).get("market") or {}).get("analysis_price") or {}
    if ap.get("bid") and ap.get("ask"):
        return (ap["bid"] + ap["ask"]) / 2
    return ap.get("last_close_1m")
