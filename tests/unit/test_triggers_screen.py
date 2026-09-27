"""Phase 3 §3.7.2 — 5-minute screening and "call only on change" (ai/triggers.py, the pure part).

Every case starts from the real stored snapshot payload fixture (XAUUSD, decision TF 15m, screen TF 5m) and changes
only the block under test: stable setup keys, the 5m structure events counting as weak, ``liquidity_atr``, the
dedupe against the last dispatch's signature, ``weak_min``, the "changed" rule for time-based reviews, the review
floor for events/conditions, the idle floor at a decision close, and the unchanged pre-Phase-3 path."""
import copy
import json
from pathlib import Path

import pytest

from tradingsystem.ai.triggers import (Reason, TriggerDecision, decide, review_due, review_due_split, scan_setups,
                                       setup_reasons, setup_signature)

FIXTURE = json.loads((Path(__file__).resolve().parents[1] / "fixtures" / "real" / "payload_xauusd.json").read_text())
MIN = 60_000
NOW = 1790334600000                      # 2026-09-25T11:10:00Z
SWEEP_1H = "1h:sweep:bearish:2026-09-25T09:00:00.000Z"      # the fixture's one setup: a 1h sweep on its last bar


def payload() -> dict:
    return copy.deepcopy(FIXTURE)


def plain() -> dict:
    """The fixture without its 15m reversal candle (bearish engulfing), so a zone alone is ONE weak reason."""
    p = payload()
    p["timeframes"]["15m"]["patterns"]["last_bar"] = []
    return p


def last_bar(p: dict, tf: str) -> str:
    return p["timeframes"][tf]["recent"][-1][0]


def close(p: dict, tf: str) -> float:
    return p["timeframes"][tf]["indicators"]["close"]


def add_event(p: dict, tf: str, kind: str, direction: str = "bullish", level: float = 4300.0) -> dict:
    p["timeframes"][tf]["structure"]["events"].append(
        {"time": last_bar(p, tf), "kind": kind, "dir": direction, "level": level})
    return p


def add_zone(p: dict, tf: str, kind: str = "fvg", direction: str = "bullish", half: float = 1.0) -> dict:
    """A zone straddling the timeframe's close (``half`` = distance of each edge from it)."""
    c = close(p, tf)
    p["timeframes"][tf]["zones"][kind].append({"dir": direction, "top": round(c + half, 2),
                                               "bottom": round(c - half, 2), "formed": last_bar(p, tf),
                                               "fill_pct": 0, "touched": False, "strength_atr": 1.0, "age_bars": 1})
    return p


def rich(p: dict) -> dict:
    """The fixture plus one of every weak reason kind on the decision TF and order flow."""
    add_zone(p, "15m", "order_blocks", "bullish", 2.0)
    add_zone(p, "15m", "fvg", "bullish", 1.0)
    p["timeframes"]["15m"]["liquidity"]["buy_side_above"][0]["level"] = round(close(p, "15m") + 1.0, 2)
    p["timeframes"]["15m"]["patterns"]["last_bar"] = ["bullish_pin_bar"]
    p["orderflow"]["footprint"] = {"data_quality": "real", "timeframe": "15m", "bars": [
        {"time": "2026-09-25T10:15:00.000Z", "stacked_buy": [], "stacked_sell": [], "absorption": None},
        {"time": last_bar(p, "15m"), "stacked_buy": [[4304.0, 4305.0]], "stacked_sell": [], "absorption": "bid"}]}
    p["orderflow"]["bar_delta"] = {"data_quality": "real", "timeframe": "15m", "divergence": {
        "type": "bearish", "price_pivots": [180, 190], "price": [4297.88, 4309.52], "cvd": [12.0, 8.5]}}
    return p


def old_setup_reasons(payload: dict) -> tuple[list[str], list[str]]:
    """Reference oracle: ``setup_reasons`` verbatim as it was before Phase 3 (commit 5887341)."""
    strong, weak = [], []
    tfs = payload.get("timeframes", {})
    dec = payload["meta"]["decision_timeframe"]
    bias = payload.get("confluence", {}).get("bias")
    for tf in (dec, "1h"):
        t = tfs.get(tf) or {}
        recent = t.get("recent") or []
        if not recent:
            continue
        last_bar = recent[-1][0]
        for e in (t.get("structure") or {}).get("events", []):
            if e["time"] != last_bar:
                continue
            if e["kind"] in {"BOS", "CHoCH"}:
                strong.append(f"{tf} {e['kind']} {e['dir']} through {e['level']}")
            elif e["kind"] == "sweep":
                strong.append(f"{tf} liquidity sweep ({e['dir']}) at {e['level']}")
        close = (t.get("indicators") or {}).get("close")
        atr = (t.get("indicators") or {}).get("atr14")
        if close is None or not atr:
            continue
        for kind in ("order_blocks", "fvg"):
            for z in (t.get("zones") or {}).get(kind, []):
                inside = z["bottom"] <= close <= z["top"]
                aligned = bias in (None, "mixed") or (z["dir"] == bias)
                if inside and aligned:
                    weak.append(f"price inside {tf} {z['dir']} {kind[:-1] if kind.endswith('s') else kind} "
                                f"{z['bottom']}-{z['top']}")
        liq = t.get("liquidity") or {}
        for side, key in (("buy-side", "buy_side_above"), ("sell-side", "sell_side_below")):
            for p in liq.get(key, [])[:1]:
                if abs(p["level"] - close) <= 0.3 * atr:
                    weak.append(f"price within 0.3 ATR of {tf} {side} liquidity {p['level']}")
        pats = (t.get("patterns") or {}).get("last_bar", [])
        if any(x in pats for x in ("bullish_engulfing", "bearish_engulfing", "bullish_pin_bar", "bearish_pin_bar")) \
                and any(r.startswith(f"price inside {tf}") for r in weak):
            weak.append(f"{tf} reversal candle ({', '.join(pats)}) inside a zone")
    fp = (payload.get("orderflow") or {}).get("footprint") or {}
    bars = fp.get("bars") or []
    if bars:
        lb = bars[-1]
        if lb.get("stacked_buy") or lb.get("stacked_sell"):
            weak.append("stacked footprint imbalances on the last decision bar")
        if lb.get("absorption"):
            weak.append(f"absorption on the last decision bar ({lb['absorption']})")
    div = ((payload.get("orderflow") or {}).get("bar_delta") or {}).get("divergence")
    if div:
        weak.append(f"{div['type']} price/CVD divergence")
    return strong, weak


def screen(p: dict | None, **kw) -> TriggerDecision:
    """``decide`` on the Phase 3 path as the engine calls it at a 5m screen close (overridable)."""
    args = dict(policy="hybrid", payload=p, last_call_ms=NOW - 20 * MIN, now=NOW, min_spacing_min=15,
                max_idle_min=120, review_reasons=[], at_close=p is not None, review_floor_min=5, backoff_ms=0,
                weak_min=2, liquidity_atr=0.3, screen_tf="5m", last_signature=None, time_reasons=[],
                event_reasons=[], at_decision_close=False, decision_bar_since_last_call=True, move_atr=None,
                screen_move_atr=0.5)
    args.update(kw)
    return decide(**args)


# ------------------------------------------------------------------ keys and the scan
def test_keys_are_stable_across_two_scans_and_independent_of_the_price():
    a, b = scan_setups(rich(payload()), screen_tf="5m"), scan_setups(rich(payload()), screen_tf="5m")
    assert a == b and len(a) == len(setup_signature(a)) == 8
    keys = setup_signature(a)
    dec = last_bar(FIXTURE, "15m")
    c = close(FIXTURE, "15m")
    assert keys == {
        SWEEP_1H,
        f"15m:order_block:bullish:{round(c + 2, 2)}-{round(c - 2, 2)}",
        f"15m:fvg:bullish:{round(c + 1, 2)}-{round(c - 1, 2)}",
        f"15m:liq:buy:{round(c + 1, 2)}",
        f"15m:pattern:{dec}",
        f"fp:{dec}:stacked", f"fp:{dec}:absorption",
        "div:bearish:4309.52",
    }
    # the next screen: price moved inside the same zones, the divergence pivots slid by one bar → same keys
    p = rich(payload())
    p["timeframes"]["15m"]["indicators"]["close"] = round(c + 0.4, 2)
    p["orderflow"]["bar_delta"]["divergence"]["price_pivots"] = [179, 189]
    assert setup_signature(scan_setups(p, screen_tf="5m")) == keys
    assert all(isinstance(r, Reason) and r.strength in ("strong", "weak") for r in a)


def test_setup_reasons_defaults_reproduce_the_old_output():
    assert setup_reasons(payload()) == (["1h liquidity sweep (bearish) at 4295.76"], [])
    assert setup_reasons(payload()) == old_setup_reasons(payload())
    p = rich(payload())
    strong, weak = setup_reasons(p)
    assert (strong, weak) == old_setup_reasons(p)
    c = close(FIXTURE, "15m")
    assert weak == [f"price inside 15m bullish order_block {round(c - 2, 2)}-{round(c + 2, 2)}",
                    f"price inside 15m bullish fvg {round(c - 1, 2)}-{round(c + 1, 2)}",
                    f"price within 0.3 ATR of 15m buy-side liquidity {round(c + 1, 2)}",
                    "15m reversal candle (bullish_pin_bar) inside a zone",
                    "stacked footprint imbalances on the last decision bar",
                    "absorption on the last decision bar (bid)",
                    "bearish price/CVD divergence"]
    # more variants against the oracle: 15m BOS + 1h zone + bias filter + a reversal candle without a zone
    for mutate in (lambda q: add_event(q, "15m", "BOS"), lambda q: add_zone(q, "1h", "order_blocks", "bearish"),
                   lambda q: q["confluence"].update(bias="bullish") or add_zone(q, "15m", "fvg", "bearish"),
                   lambda q: q["timeframes"]["15m"]["patterns"].update(last_bar=["bearish_engulfing"])):
        q = payload()
        mutate(q)
        assert setup_reasons(q) == old_setup_reasons(q)


def test_5m_structure_is_weak_15m_and_1h_strong():
    p = add_event(add_event(payload(), "5m", "BOS"), "15m", "BOS")
    found = {r.key: r for r in scan_setups(p, screen_tf="5m")}
    assert found[f"5m:BOS:bullish:{last_bar(p, '5m')}"].strength == "weak"
    assert found[f"15m:BOS:bullish:{last_bar(p, '15m')}"].strength == "strong"
    assert found[SWEEP_1H].strength == "strong"
    # without screen_tf the 5m frame is not scanned at all (the old behaviour)
    assert not any(k.startswith("5m:") for k in setup_signature(scan_setups(p)))
    # a 5m CHoCH/sweep: weak too; an event on an older 5m bar is not a setup
    q = add_event(payload(), "5m", "sweep", "bearish")
    q["timeframes"]["5m"]["structure"]["events"].append({"time": "2026-09-25T10:00:00.000Z", "kind": "CHoCH",
                                                         "dir": "bearish", "level": 4290.0})
    weak = [r for r in scan_setups(q, screen_tf="5m") if r.key.startswith("5m:")]
    assert [(r.key.split(":")[1], r.strength) for r in weak] == [("sweep", "weak")]
    # a screen TF missing from the payload, or equal to the decision TF, adds nothing
    assert scan_setups(p, screen_tf="30m") == scan_setups(p)
    assert scan_setups(p, screen_tf="15m") == scan_setups(p)


def test_liquidity_atr_changes_what_is_near():
    # 1h: close 4295.08, nearest buy-side pool 4303.15 (8.07 away), ATR 15.88 → 0.51 ATR
    near = lambda k: {r.key for r in scan_setups(payload(), liquidity_atr=k)}  # noqa: E731
    assert "1h:liq:buy:4303.15" not in near(0.3)
    assert "1h:liq:buy:4303.15" in near(0.6)
    texts = setup_reasons(payload(), liquidity_atr=0.6)[1]
    assert "price within 0.6 ATR of 1h buy-side liquidity 4303.15" in texts
    # 5m pool 4309.52 is 4.16 away with ATR 4.4 (0.95 ATR): near only on the screen TF with a wide setting
    assert "5m:liq:buy:4309.52" in {r.key for r in scan_setups(payload(), liquidity_atr=1.0, screen_tf="5m")}


def test_divergence_and_footprint_keys():
    p = rich(payload())
    keys = setup_signature(scan_setups(p))
    assert "div:bearish:4309.52" in keys                     # the last pivot's price, not its sliding index
    p["orderflow"]["bar_delta"]["divergence"]["last_pivot_time"] = "2026-09-25T09:45:00.000Z"
    assert "div:bearish:2026-09-25T09:45:00.000Z" in setup_signature(scan_setups(p))
    p["orderflow"]["footprint"]["bars"][-1]["stacked_buy"] = []
    p["orderflow"]["footprint"]["bars"][-1]["absorption"] = None
    assert not any(k.startswith("fp:") for k in setup_signature(scan_setups(p)))


# ------------------------------------------------------------------ call only on change
def test_same_payload_twice_fires_once():
    p = payload()
    first = screen(p)
    assert first.fire and first.strength == "strong" and first.reasons == ["1h liquidity sweep (bearish) at 4295.76"]
    assert first.signature == {SWEEP_1H}
    again = screen(payload(), last_signature=first.signature)
    assert not again.fire and again.strength == "none" and again.signature == first.signature
    assert again.reasons == ["1h liquidity sweep (bearish) at 4295.76"]         # still reported, not new


def test_a_new_zone_fires_weak_only_at_weak_min():
    seen = screen(payload()).signature
    one = add_zone(plain(), "15m")
    assert not screen(one, last_signature=seen).fire                             # 1 new weak < weak_min 2
    d = screen(one, last_signature=seen, weak_min=1)
    assert d.fire and d.strength == "weak" and len(d.reasons) == 1 and "15m bullish fvg" in d.reasons[0]
    # the same zone with the fixture's 15m bearish engulfing inside it: zone + reversal candle = 2 → a call
    d = screen(add_zone(payload(), "15m"), last_signature=seen)
    assert d.fire and d.strength == "weak" and d.reasons[1] == "15m reversal candle (bearish_engulfing) inside a zone"
    two = add_zone(add_zone(plain(), "15m"), "5m", "order_blocks")
    d = screen(two, last_signature=seen)
    assert d.fire and d.strength == "weak" and len(d.reasons) == 2
    # already dispatched with those zones inside → quiet on the next screen
    assert not screen(two, last_signature=d.signature).fire


def test_a_new_5m_bos_alone_does_not_call_but_a_new_15m_bos_does():
    seen = screen(payload()).signature
    assert not screen(add_event(payload(), "5m", "BOS"), last_signature=seen).fire
    d = screen(add_event(payload(), "15m", "BOS"), last_signature=seen)
    assert d.fire and d.strength == "strong" and d.reasons == ["15m BOS bullish through 4300.0"]


def test_time_review_fires_only_on_change():
    seen = screen(payload()).signature
    tr = ["next_review time reached (60 min after 2026-09-25T10:10:00.000Z)"]
    base = dict(last_signature=seen, time_reasons=tr)
    # no new decision bar since the last call: never, whatever moved
    assert not screen(payload(), decision_bar_since_last_call=False, move_atr=0.9, **base).fire
    assert not screen(add_zone(plain(), "15m"), decision_bar_since_last_call=False, **base).fire
    # new bar + one new weak reason (below weak_min on its own) → a review
    d = screen(add_zone(plain(), "15m"), **base)
    assert d.fire and d.strength == "review" and d.reasons[0] == tr[0] and "fvg" in d.reasons[1]
    # new bar + a 0.6 ATR move → a review; 0.3 ATR and nothing new → quiet
    d = screen(payload(), move_atr=0.6, **base)
    assert d.fire and d.strength == "review" and d.reasons == tr + ["price moved 0.60 ATR since the last call"]
    assert not screen(payload(), move_atr=0.3, **base).fire
    assert not screen(payload(), move_atr=0.6, screen_move_atr=0.7, **base).fire
    # no payload between closes: nothing to judge the change on
    assert not screen(None, move_atr=None, **base).fire
    # a time review keeps the full spacing (not the review floor)
    assert not screen(payload(), move_atr=0.6, last_call_ms=NOW - 10 * MIN, **base).fire


def test_events_and_conditions_use_the_review_floor_setups_the_full_spacing():
    seen = screen(payload()).signature
    new_setup = add_event(payload(), "15m", "BOS")
    ev = ["paper_filled BTCUSDT filled 0.01 @ 84350", "event: TP1 hit"]
    d = screen(payload(), last_call_ms=NOW - 6 * MIN, last_signature=seen, event_reasons=ev)
    assert d.fire and d.strength == "event"
    assert d.reasons == ["event: paper_filled BTCUSDT filled 0.01 @ 84350; TP1 hit"]      # coalesced into ONE reason
    assert not screen(payload(), last_call_ms=NOW - 3 * MIN, last_signature=seen, event_reasons=ev).fire
    blocked = screen(new_setup, last_call_ms=NOW - 6 * MIN, last_signature=seen)
    assert not blocked.fire and blocked.reasons[0].startswith("spacing: last AI call 6 min ago (next after 15 min)")
    assert screen(new_setup, last_call_ms=NOW - 15 * MIN, last_signature=seen).fire
    # an event and a new strong setup together: one event call that mentions both
    d = screen(new_setup, last_call_ms=NOW - 6 * MIN, last_signature=seen, event_reasons=ev)
    assert d.fire and d.strength == "event" and "15m BOS bullish through 4300.0" in d.reasons
    # the failure back-off overrides the floor
    assert not screen(payload(), last_call_ms=NOW - 6 * MIN, backoff_ms=30 * MIN, event_reasons=ev).fire


def test_condition_reviews_fire_between_closes():
    cond = ["review condition: price above 4300.0"]
    d = screen(None, review_reasons=cond, last_call_ms=NOW - 6 * MIN)
    assert d.fire and d.strength == "review" and d.reasons == cond and d.signature == frozenset()
    assert not screen(None, review_reasons=cond, last_call_ms=NOW - 4 * MIN).fire
    # a condition outranks a time review due at the same time
    d = screen(payload(), review_reasons=cond, time_reasons=["next_review time reached"], move_atr=0.9,
               last_call_ms=NOW - 6 * MIN)
    assert d.fire and d.reasons[0] == cond[0]


def test_idle_only_at_a_decision_close():
    seen = screen(payload()).signature
    kw = dict(last_signature=seen, last_call_ms=NOW - 200 * MIN)
    assert not screen(payload(), at_decision_close=False, **kw).fire
    d = screen(payload(), at_decision_close=True, **kw)
    assert d.fire and d.strength == "idle" and d.reasons == ["idle: no AI review for ≥120 min"]
    assert not screen(payload(), at_decision_close=True, last_signature=seen, last_call_ms=NOW - 100 * MIN).fire
    assert not screen(payload(), at_decision_close=True, policy="on_setup_event", **kw).fire


def test_every_close_fires_at_decision_closes_only():
    assert not screen(payload(), policy="every_close", at_decision_close=False).fire
    d = screen(payload(), policy="every_close", at_decision_close=True)
    assert d.fire and d.strength == "close" and d.reasons == ["15m close"]
    # at_decision_close defaults to at_close
    assert screen(payload(), policy="every_close", at_decision_close=None).fire


# ------------------------------------------------------------------ the pre-Phase-3 call path
def test_old_call_path_is_unchanged():
    kw = dict(now=NOW, min_spacing_min=15, max_idle_min=120)
    d = decide("hybrid", None, last_call_ms=NOW - 6 * MIN, review_reasons=["r"], at_close=False, **kw)
    assert (d.fire, d.reasons, d.strength) == (True, ["r"], "review")
    d = decide("hybrid", None, last_call_ms=NOW - 3 * MIN, review_reasons=["r"], at_close=False, **kw)
    assert (d.fire, d.reasons, d.strength) == (False, ["spacing: last AI call 3 min ago (next after 5 min)"], "none")
    # a strong setup fires at every close — no dedupe on the old path
    for _ in range(2):
        d = decide("on_setup_event", payload(), last_call_ms=NOW - 20 * MIN, review_reasons=[], at_close=True, **kw)
        assert (d.fire, d.reasons, d.strength) == (True, ["1h liquidity sweep (bearish) at 4295.76"], "strong")
    # ≥2 weak at a close (strong event removed)
    p = add_zone(add_zone(plain(), "15m"), "15m", "order_blocks")
    p["timeframes"]["1h"]["structure"]["events"].pop()
    d = decide("on_setup_event", p, last_call_ms=None, review_reasons=[], at_close=True, **kw)
    assert d.fire and d.strength == "weak" and len(d.reasons) == 2
    one = add_zone(plain(), "15m")
    one["timeframes"]["1h"]["structure"]["events"].pop()
    d = decide("on_setup_event", one, last_call_ms=None, review_reasons=[], at_close=True, **kw)
    assert (d.fire, d.strength) == (False, "none") and len(d.reasons) == 1
    d = decide("hybrid", one, last_call_ms=NOW - 121 * MIN, review_reasons=[], at_close=True, **kw)
    assert d.fire and d.strength == "idle" and d.reasons[0] == "idle: no AI review for ≥120 min"
    d = decide("every_close", payload(), last_call_ms=None, review_reasons=[], at_close=True, **kw)
    assert (d.fire, d.reasons, d.strength) == (True, ["15m close"], "close")
    assert decide("hybrid", payload(), last_call_ms=None, review_reasons=[], at_close=False, **kw) \
        == TriggerDecision(False, [], "none")
    # the old path ignores the 5m frame unless asked
    q = add_event(payload(), "5m", "BOS")
    q["timeframes"]["1h"]["structure"]["events"].pop()
    assert not decide("on_setup_event", q, last_call_ms=None, review_reasons=[], at_close=True, **kw).fire


# ------------------------------------------------------------------ next_review split
def test_review_due_split_separates_time_from_conditions():
    base = NOW - 70 * MIN
    last = {"id": "a", "ts": base, "recommendation": {"next_review": {"in_minutes": 60, "conditions": [
        {"kind": "price_above", "value": 4300.0},
        {"kind": "minutes_elapsed", "value": 30},
        {"kind": "candle_close_below", "value": 4310.0, "timeframe": "5m"},
        {"kind": "price_below", "value": 4000.0},
        {"kind": "candle_close_above", "value": 4400.0, "timeframe": "15m"}]}}}
    closes = {"5m": 4305.36, "15m": 4305.36}
    time_r, cond_r = review_due_split(last, NOW, 4305.86, closes)
    assert time_r == ["next_review time reached (60 min after 2026-09-25T10:00:00.000Z)",
                      "review condition: 30 minutes elapsed"]
    assert cond_r == ["review condition: price above 4300.0", "review condition: 5m close below 4310.0"]
    assert review_due(last, NOW, 4305.86, closes) == [time_r[0], cond_r[0], time_r[1], cond_r[1]]
    assert review_due_split(None, NOW, None, {}) == ([], [])
    assert review_due_split(last, base + 10 * MIN, None, {}) == ([], [])


@pytest.mark.parametrize("weak_min", [1, 2, 3])
def test_weak_min_counts_distinct_new_keys(weak_min):
    seen = screen(payload()).signature
    p = add_zone(add_zone(plain(), "15m"), "15m", "order_blocks")                # 2 new weak keys
    assert screen(p, last_signature=seen, weak_min=weak_min).fire is (weak_min <= 2)
