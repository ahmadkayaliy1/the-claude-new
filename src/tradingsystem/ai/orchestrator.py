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
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import time
from dataclasses import dataclass

from pydantic import ValidationError

from ..analysis.snapshot import SnapshotBuilder, payload_hash
from ..core.instruments import InstrumentRegistry
from ..core.settings import Settings
from ..core.timeutil import iso, now_ms, parse_date_spec
from .budget import CostGovernor, RateLimiter, UsageStore
from .contract import (AssessmentSet, Decision, Recommendation, RecommendationSet, RiskReview,
                       TimeframeAssessment)
from .prompts import render
from .providers import LLMProvider, ProviderError, make_provider
from .repair import Generation, generate_validated
from .store import DecisionRecord, DecisionStore

log = logging.getLogger(__name__)
ANALYST_TFS = ["1w", "1d", "4h", "1h", "15m", "5m"]


@dataclass
class CycleRequest:
    pair: str
    reason: str


class Orchestrator:
    def __init__(self, settings: Settings, registry: InstrumentRegistry, builder: SnapshotBuilder,
                 store: DecisionStore, usage: UsageStore, governor: CostGovernor) -> None:
        self.s, self.reg, self.builder, self.store = settings, registry, builder, store
        self.usage, self.governor = usage, governor
        self._providers: dict[str, LLMProvider] = {}
        self._limiters: dict[str, RateLimiter] = {}
        self._sem = asyncio.Semaphore(max(1, settings.ai.max_parallel_calls))
        self.route: tuple[str, str | None] = (settings.ai.active_provider, None)   # (provider in use, why rerouted)

    # ------------------------------------------------------------------ plumbing
    def provider(self, name: str | None = None) -> LLMProvider:
        """A named provider, or (no name) the active one — rerouted to ``ai.fallback_provider`` while the active
        provider is unavailable (not signed in, usage limit reached, missing key; D-030)."""
        if name is not None:
            return self._get(name)
        active, fallback = self.s.ai.active_provider, self.s.ai.fallback
        try:
            prov = self._get(active)
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
            fb = self._get(fallback)
            fb_why = fb.unavailable_reason()
        except ProviderError as exc:
            fb_why = str(exc)
        if fb_why:
            raise ProviderError(f"{why}; fallback {fallback}: {fb_why}", retryable=False)
        self._set_route(fallback, why)
        return fb

    def _get(self, name: str) -> LLMProvider:
        if name not in self._providers:
            prov = make_provider(self.s, name)
            self._providers[name] = prov
            self._limiters[prov.name] = RateLimiter(prov.name, prov.cfg, self.usage)
        return self._providers[name]

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
        return (lim.remaining_today(), lim.cfg.rpd) if lim else (None, None)

    def default_account(self) -> dict:
        return {"equity": self.s.execution.paper_equity, "currency": "USD", "mode": self.s.execution.mode,
                "equity_source": "configured paper equity (live balance and open positions are not wired in yet)"}

    def payload(self, pair: str, as_of: int, account: dict | None = None) -> dict:
        """The snapshot the model receives for ``pair`` at ``as_of`` (account block + recent decisions)."""
        return self.builder.build(pair, as_of, account=account or self.default_account(),
                                  history=self.store.recent(pair))

    def horizon_ms(self, pair: str | None) -> int:
        """Latest allowed ``valid_until`` after the cycle time: 4 decision bars."""
        return 4 * (self.s.pairs[pair].decision_timeframe.ms if pair else 900_000)

    def _system_vars(self, pair: str | None, account: dict) -> dict:
        r = self.s.risk
        pairs = list(self.s.enabled_pairs())
        return {
            "pair": pair or ", ".join(pairs), "pair_list": ", ".join(pairs),
            "decision_tf": (self.s.pairs[pair].decision_timeframe.value if pair else "15m"),
            "sl_min_atr": r.sl_atr_min_mult, "min_rr": r.min_rr, "max_risk_pct": r.max_risk_per_trade_pct,
            "account_equity": int(round(float(account.get("equity", self.s.execution.paper_equity)) / 10) * 10),
            "account_currency": account.get("currency", "USD"),
            "price_reference": self.reg.primary(pair).key if pair else "each pair's meta.price_reference",
            "output_language": "English" if self.s.ai.output_language == "en" else self.s.ai.output_language,
        }

    def _user_vars(self, pair: str | None, as_of: int, reason: str, payload_json: str, **extra) -> dict:
        return {"now_utc": iso(as_of), "trigger_reason": reason, "payload": payload_json,
                "max_valid_until": iso(as_of + self.horizon_ms(pair)), **extra}

    async def _gen(self, provider: LLMProvider, model_cls, system: str, user: str, purpose: str,
                   pair: str | None) -> Generation:
        try:
            async with self._sem:
                return await generate_validated(provider, model_cls, system=system, user=user,
                                                limiter=self._limiters[provider.name], governor=self.governor,
                                                usage=self.usage, purpose=purpose, pair=pair,
                                                est_input_tokens=max(2000, len(user) // 3))
        except Exception as exc:  # noqa: BLE001 — one failed sub-call must not take its siblings down (F4)
            log.exception("%s %s: generation failed", pair or "*", purpose)
            msg = f"{type(exc).__name__}: {exc}"[:300]
            return Generation(False, None, errors=[msg], provider_error=msg)

    # ------------------------------------------------------------------ entry point
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
                                                       errors=[f"snapshot failed: {exc!r}"[:300]]), label, as_of, None))
        if mode == "paused":
            return out + [self._finish(self._failed(p, mode, reasons[p], built[p], why, "budget_blocked"), label,
                                       as_of, built[p]) for p in built]
        units = self._units(mode, built, reasons, as_of, account)
        tasks = {asyncio.ensure_future(coro): pairs for pairs, coro in units}
        end = time.monotonic() + deadline_s if deadline_s else None
        pending = set(tasks)
        try:
            while pending:
                timeout = None if end is None else max(0.0, end - time.monotonic())
                done, pending = await asyncio.wait(pending, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
                if not done:
                    break
                for t in done:
                    try:
                        recs = t.result()
                    except Exception as exc:  # noqa: BLE001
                        log.error("AI unit %s failed: %r", tasks[t], exc, exc_info=exc)
                        recs = [self._failed(p, mode, reasons[p], built[p], f"{type(exc).__name__}: {exc}")
                                for p in tasks[t]]
                    out += [self._finish(rec, label, as_of, built.get(rec.pair)) for rec in recs]
        finally:
            for t in pending:                    # deadline or shutdown: cancelling kills any running CLI call
                t.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        for t in pending:
            log.warning("AI cycle deadline (%.0fs) reached — cancelled %s", deadline_s, tasks[t])
            out += [self._finish(self._failed(p, mode, reasons[p], built[p],
                                              f"cycle deadline of {deadline_s:.0f}s reached — cancelled"),
                                 label, as_of, built[p]) for p in tasks[t]]
        return out

    def _units(self, mode: str, payloads: dict, reasons: dict, as_of: int,
               account: dict) -> list[tuple[list[str], object]]:
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
            job = lambda p: self._consensus(p, payloads[p], reasons[p], as_of, account)  # noqa: E731
        else:
            review = mode == "agent_per_pair_with_risk_reviewer"
            job = lambda p: self._per_pair(p, payloads[p], reasons[p], as_of, account, review=review)  # noqa: E731
        return [([p], _as_list(job(p))) for p in payloads]

    @staticmethod
    def _failed(pair: str, mode: str, reason: str, payload: dict, error: str, status: str = "error") -> DecisionRecord:
        return DecisionRecord(pair, mode, reason, status, payload_hash=payload["meta"]["payload_hash"],
                              errors=[error[:300]])

    def _finish(self, rec: DecisionRecord, label: str, as_of: int, payload: dict | None) -> DecisionRecord:
        rec.mode = label
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
            if trade and ref == execu:
                return _reject(rec, f"price_reference {ref!r} is the execution instrument — levels must be in "
                                    f"{prim} prices")
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
                return _reject(rec, f"valid_until {iso(vu)} is not after the cycle time {iso(as_of)}")
            r["valid_until"] = iso(as_of + self.s.pairs[rec.pair].decision_timeframe.ms)
        r["next_review"] = self._review_plan(r.get("next_review") or {}, payload, notes)
        try:
            v = Recommendation.model_validate(r)
        except ValidationError as exc:
            return _reject(rec, f"normalised recommendation invalid: {exc.errors()[:3]}"[:300])
        rec.recommendation = json.loads(v.model_dump_json())
        rec.rr_computed = v.rr_computed()
        if notes:
            rec.errors.append("normalised: " + "; ".join(notes))

    def _review_plan(self, nr: dict, payload: dict | None, notes: list[str]) -> dict:
        """Time-based reviews never come sooner than the normal spacing; price/candle conditions (something
        happened) may, down to ``ai.review_floor_minutes`` (enforced by the trigger policy)."""
        floor = max(self.s.ai.review_floor_minutes, self.s.ai.min_minutes_between_calls)
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

    # ------------------------------------------------------------------ modes
    async def _per_pair(self, pair: str, payload: dict, reason: str, as_of: int, account: dict, *,
                        review: bool = False, provider_name: str | None = None) -> DecisionRecord:
        prov = self.provider(provider_name)
        pr = render("agent_per_pair", self._system_vars(pair, account),
                    self._user_vars(pair, as_of, reason, _dump(payload)))
        gen = await self._gen(prov, Recommendation, pr.system, pr.user, "agent_per_pair", pair)
        rec = self._record(pair, "agent_per_pair", reason, payload, gen, prov, pr.prompt_hash)
        if not review or rec.status != "valid" or rec.recommendation["decision"] == Decision.NO_TRADE.value:
            return rec
        rv = render("risk_reviewer", self._system_vars(pair, account),
                    self._user_vars(pair, as_of, reason, _dump(payload), proposal=json.dumps(rec.recommendation)))
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
            # a trade that could not be reviewed is not released
            rec.status = "invalid"
            rec.errors.append("risk review failed — trade withheld")
        return rec

    async def _global(self, payloads: dict, reasons: dict, as_of: int, account: dict) -> list[DecisionRecord]:
        prov = self.provider()
        pr = render("single_agent_global", self._system_vars(None, account),
                    self._user_vars(None, as_of, "; ".join(f"{p}: {r}" for p, r in reasons.items()),
                                    _dump(list(payloads.values()))))
        gen = await self._gen(prov, RecommendationSet, pr.system, pr.user, "single_agent_global", None)
        out = []
        by_pair = {r.pair: r for r in gen.value.recommendations} if gen.ok else {}
        for pair, payload in payloads.items():
            rec_v = by_pair.get(pair)
            rec = self._record(pair, "single_agent_global", reasons[pair], payload, gen, prov, pr.prompt_hash, rec_v,
                               use_gen_value=False)
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

        async def analyst(tf: str):
            sys_vars = {**self._system_vars(None, account), "timeframe": tf, "scope_text": f" across {', '.join(pairs)}"}
            pr = render("timeframe_analyst", sys_vars,
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
                             "cost_usd": g.cost_usd / len(pairs)})
            rec = await self._coordinate(pair, payloads[pair], reasons[pair], as_of, account, assessments, subs, prov,
                                         "agent_per_timeframe")
            out.append(rec)
        return out

    async def _pair_and_tf(self, pair: str, payload: dict, reason: str, as_of: int, account: dict) -> DecisionRecord:
        prov = self.provider()

        async def analyst(tf: str):
            sys_vars = {**self._system_vars(pair, account), "timeframe": tf, "scope_text": f" for {pair}"}
            pr = render("timeframe_analyst", sys_vars, {"now_utc": iso(as_of), "payload": _dump(self._slice(payload, tf))})
            g = await self._gen(prov, TimeframeAssessment, pr.system, pr.user, f"analyst_{tf}", pair)
            return tf, pr, g
        results = await asyncio.gather(*(analyst(tf) for tf in ANALYST_TFS))
        assessments = [json.loads(g.value.model_dump_json()) for _, _, g in results if g.ok]
        subs = [{"role": "timeframe_analyst", "label": tf, "provider": prov.name, "model": prov.model,
                 "prompt_hash": pr.prompt_hash, "ok": g.ok,
                 "output": json.loads(g.value.model_dump_json()) if g.ok else None, "errors": g.errors,
                 "cost_usd": g.cost_usd} for tf, pr, g in results]
        return await self._coordinate(pair, payload, reason, as_of, account, assessments, subs, prov,
                                      "agent_per_pair_and_timeframe")

    async def _coordinate(self, pair: str, payload: dict, reason: str, as_of: int, account: dict,
                          assessments: list[dict], subs: list[dict], prov: LLMProvider, mode: str) -> DecisionRecord:
        compact = {k: payload[k] for k in ("meta", "account", "market", "capabilities", "levels", "confluence", "history")}
        pr = render("coordinator", self._system_vars(pair, account),
                    self._user_vars(pair, as_of, reason, _dump(compact), assessments=json.dumps(assessments)))
        if not assessments:
            rec = DecisionRecord(pair, mode, reason, "invalid", provider=prov.name, model=prov.model,
                                 payload_hash=payload["meta"]["payload_hash"],
                                 errors=["no valid timeframe assessments — coordinator not called"])
        else:
            g = await self._gen(prov, Recommendation, pr.system, pr.user, "coordinator", pair)
            rec = self._record(pair, mode, reason, payload, g, prov, pr.prompt_hash)
        rec.sub_outputs = subs
        rec.cost_usd += sum(s.get("cost_usd", 0.0) for s in subs)
        return rec

    async def _consensus(self, pair: str, payload: dict, reason: str, as_of: int, account: dict) -> DecisionRecord:
        names = self.s.ai.consensus_providers or [self.s.ai.active_provider]
        got = await asyncio.gather(*(self._per_pair(pair, payload, reason, as_of, account, provider_name=n)
                                     for n in names), return_exceptions=True)
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
        return rec


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
    return json.dumps(obj, separators=(",", ":"), default=str, ensure_ascii=False)


async def _as_list(coro) -> list[DecisionRecord]:
    return [await coro]


def _reject(rec: DecisionRecord, why: str) -> None:
    rec.status, rec.recommendation, rec.rr_computed = "invalid", None, None
    rec.errors.append(why)


def _analysis_mid(payload: dict | None) -> float | None:
    """Analysis-instrument price at the payload's as-of (mid of bid/ask, else the last 1m close)."""
    ap = ((payload or {}).get("market") or {}).get("analysis_price") or {}
    if ap.get("bid") and ap.get("ask"):
        return (ap["bid"] + ap["ask"]) / 2
    return ap.get("last_close_1m")
