"""Replay the Phase 3 trigger policy over stored market data (read-only; no AI call, nothing written):

    python tools/replay_triggers.py BTCUSDT [--hours 24] [--end-ms <ms>] [--data-dir <dir>] [--equity <usd>]

At every screen-TF (5m) close of the window it builds the same snapshot the engine builds, runs
``ai.triggers.decide`` with the engine's inputs (setup signature of the last call, price at the last call, spacing,
idle floor) and counts what would have called Claude, by strength. Time-based ``next_review`` and executor events
need the model's answers and live trades, so they are not replayed — the result is the setup/idle part of the budget
(``ai.daily_calls_per_pair`` must leave room for reviews and events on top). It also measures the snapshot build time
on this machine (5-minute screening cost).

Phase 5 B12/B15: the engine's entry-call suppression is replayed beside the plain policy (two independent
signature / spacing states over the same snapshots): a pair with a ``desk`` calls only inside ``desk.windows``
(``analysis.desk_windows``), any other pair is held back while ``account.min_position_risk.fits_now`` is false —
that needs an account, so pass ``--equity`` (without it the payload has no fit fields and no suppression applies,
exactly the pre-B12 count). ``calls`` is what the engine would now call; ``calls_without_rules`` the plain policy.
A replay holds no position, so no review/event exemption applies.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tradingsystem.ai.triggers import decide  # noqa: E402
from tradingsystem.analysis.desk_windows import desk_window_state  # noqa: E402
from tradingsystem.analysis.snapshot import SnapshotBuilder  # noqa: E402
from tradingsystem.core.instruments import InstrumentRegistry  # noqa: E402
from tradingsystem.core.sessions import calendar_for  # noqa: E402
from tradingsystem.core.settings import INSTANCE_ENV, load_settings  # noqa: E402
from tradingsystem.core.timeframes import Timeframe  # noqa: E402
from tradingsystem.core.timeutil import iso, now_ms  # noqa: E402

SETTLE_MS = 5_000


def mid_of(payload: dict) -> float | None:
    ap = (payload.get("market") or {}).get("analysis_price") or {}
    if ap.get("bid") and ap.get("ask"):
        return (ap["bid"] + ap["ask"]) / 2
    return ap.get("last_close_1m")


class Track:
    """One trigger-policy state: the signature / price / time of the last (would-be) call and its fires."""

    def __init__(self) -> None:
        self.last_call = self.sig = self.last_price = None
        self.fires: list[dict] = []
        self.suppressed: dict[str, int] = {}


def replay(pair: str, hours: float, end_ms: int | None, data_dir: str | None, equity: float | None = None) -> dict:
    s = load_settings(extra_env={INSTANCE_ENV: pair})
    if data_dir:
        s = s.model_copy(update={"paths": s.paths.model_copy(update={"data_dir": data_dir})})
    reg = InstrumentRegistry.from_settings(s)
    builder = SnapshotBuilder(s, reg)
    pcfg = s.pairs[pair]
    dec, stf = pcfg.decision_timeframe, Timeframe.parse(s.ai.screen_timeframe)
    execu = reg.with_role(pair, "execution")[0]
    cal = calendar_for(execu.venue, execu.symbol, pcfg.asset_class)
    end = stf.floor(end_ms or now_ms())
    start = end - int(hours * 3_600_000)
    plain, rules = Track(), Track()
    build_ms: list[int] = []
    screens = 0
    try:
        for bar_close in range(start, end + 1, stf.ms):
            now = bar_close + SETTLE_MS
            if not cal.is_open(now):
                continue
            t0 = time.perf_counter()
            account = {"equity": equity, "currency": "USD"} if equity else None
            payload = builder.build(pair, now, account=account, history=[], memory={}, performance={})
            build_ms.append(int((time.perf_counter() - t0) * 1000))
            screens += 1
            atr = ((payload.get("timeframes") or {}).get(dec.value) or {}).get("indicators", {}).get("atr14")
            mid = mid_of(payload)
            fit = (((payload.get("account") or {}).get("min_position_risk")) or {}).get("fits_now")
            if pcfg.desk is not None:
                ok, why = desk_window_state(pcfg.desk, now, cal, pair)
                held = None if ok else why
            elif s.ai.skip_entry_calls_when_no_fit and fit is False:
                held = "no_fit"
            else:
                held = None
            for tr, hold in ((plain, None), (rules, held)):
                move = abs(mid - tr.last_price) / atr if (tr.last_price and mid and atr) else None
                d = decide(s.ai.trigger_policy, payload, last_call_ms=tr.last_call, now=now,
                           min_spacing_min=s.ai.min_minutes_between_calls, max_idle_min=s.ai.max_idle_minutes,
                           review_reasons=[], at_close=True, review_floor_min=s.ai.review_floor_minutes,
                           weak_min=s.ai.weak_min, liquidity_atr=s.ai.liquidity_atr, screen_tf=stf.value,
                           last_signature=frozenset(tr.sig) if tr.sig is not None else None, time_reasons=[],
                           event_reasons=[], at_decision_close=bar_close % dec.ms == 0,
                           decision_bar_since_last_call=tr.last_call is None or dec.floor(now) > tr.last_call,
                           move_atr=move, screen_move_atr=s.ai.screen_move_atr,
                           weak_needs_location=s.ai.weak_needs_location)
                if d.fire and hold:                     # not called: the signature and price advance (the engine)
                    tr.suppressed[hold] = tr.suppressed.get(hold, 0) + 1
                    tr.sig, tr.last_price = d.signature, mid
                elif d.fire:
                    tr.fires.append({"time": iso(bar_close), "strength": d.strength, "reasons": d.reasons[:3]})
                    tr.last_call, tr.sig, tr.last_price = now, d.signature, mid
    finally:
        builder.close()
    fires = rules.fires
    by = {}
    for f in fires:
        by[f["strength"]] = by.get(f["strength"], 0) + 1
    per_day = len(fires) * 24 / hours if hours else 0
    return {"pair": pair, "window": [iso(start), iso(end)], "screens": screens, "calls": len(fires),
            "calls_per_day": round(per_day, 1), "by_strength": by,
            "calls_without_rules": len(plain.fires),
            "calls_per_day_without_rules": round(len(plain.fires) * 24 / hours, 1) if hours else 0,
            "suppressed": rules.suppressed, "desk": pcfg.desk is not None, "equity": equity, "cap": s.ai.daily_calls_per_pair,
            "within_cap": per_day <= s.ai.daily_calls_per_pair,
            "snapshot_build_ms": {"median": statistics.median(build_ms) if build_ms else None,
                                  "p95": sorted(build_ms)[int(0.95 * (len(build_ms) - 1))] if build_ms else None,
                                  "max": max(build_ms) if build_ms else None},
            "fires": fires}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("pair")
    ap.add_argument("--hours", type=float, default=24.0)
    ap.add_argument("--end-ms", type=int)
    ap.add_argument("--data-dir")
    ap.add_argument("--equity", type=float, help="USD account equity for the fit fields (B12); default: none")
    ap.add_argument("--json", action="store_true", help="print every fire")
    a = ap.parse_args(argv)
    out = replay(a.pair.upper(), a.hours, a.end_ms, a.data_dir, a.equity)
    if not a.json:
        out = {k: v for k, v in out.items() if k != "fires"}
    print(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
