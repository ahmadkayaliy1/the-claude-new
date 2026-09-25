"""Structure / SMC on real candles: causality (no look-ahead) and sanity."""
import numpy as np
import pytest

from tradingsystem.analysis import indicators as ind
from tradingsystem.analysis.price_action import patterns
from tradingsystem.analysis.structure import analyze_structure, pivots, premium_discount
from tradingsystem.analysis.zones import fair_value_gaps, order_blocks, update_mitigation


@pytest.fixture(scope="module")
def d(real_candles_1m):
    t = real_candles_1m
    x = {k: t[k].to_numpy().astype(float) for k in ("open", "high", "low", "close")}
    x["atr"] = ind.atr(x["high"], x["low"], x["close"])
    return x


def cut(d, t):
    return {k: v[:t + 1] for k, v in d.items()}


def run(x):
    st = analyze_structure(x["high"], x["low"], x["close"], x["atr"])
    fvg = fair_value_gaps(x["high"], x["low"], x["close"], x["atr"])
    ob = order_blocks(x["open"], x["high"], x["low"], x["close"], x["atr"], st)
    return st, fvg, ob


def test_pivot_known_only_after_confirmation(d):
    for p in pivots(d["high"], d["low"], 3, 3):
        assert p.confirm_idx == p.idx + 3


@pytest.mark.parametrize("t", [700, 1500, 2300, 2999])
def test_causality_truncate_and_compare(d, t):
    st_full, fvg_full, ob_full = run(d)
    st_cut, fvg_cut, ob_cut = run(cut(d, t))
    known = lambda evs: [(e.idx, e.kind, e.direction, e.level) for e in evs if e.idx <= t]  # noqa: E731
    assert known(st_cut.events) == known(st_full.events)
    assert [p for p in st_cut.pivots] == [p for p in st_full.pivots if p.confirm_idx <= t]
    zk = lambda zs: [(z.kind, z.direction, z.top, z.bottom, z.known_idx) for z in zs if z.known_idx <= t]  # noqa: E731
    assert zk(fvg_cut) == zk(fvg_full)
    assert zk(ob_cut) == zk(ob_full)
    # mitigation state as of t computed on truncated data == computed on full data but stopped at t
    a = update_mitigation(fvg_cut, d["high"][:t + 1], d["low"][:t + 1], d["close"][:t + 1])
    b = update_mitigation([z for z in fvg_full if z.known_idx <= t], d["high"], d["low"], d["close"], upto=t)
    assert [(z.fill, z.first_touch_idx, z.invalidated_idx) for z in a] == [(z.fill, z.first_touch_idx, z.invalidated_idx) for z in b]


def test_events_are_sane(d):
    st, fvg, ob = run(d)
    kinds = {e.kind for e in st.events}
    assert {"BOS", "CHoCH"} <= kinds
    for e in st.events:
        if e.kind != "sweep" and e.direction == "bullish":
            assert d["close"][e.idx] > e.level
        if e.kind != "sweep" and e.direction == "bearish":
            assert d["close"][e.idx] < e.level
    for z in fvg:
        assert z.top > z.bottom
    for z in ob:
        assert z.top >= z.bottom and z.origin_idx <= z.known_idx
    for t in (1000, 2000, 2999):
        x = cut(d, t)
        s2 = analyze_structure(x["high"], x["low"], x["close"], x["atr"])
        pd = premium_discount(s2, float(x["close"][-1]), x["high"], x["low"])
        assert pd is None or 0 <= pd["position"] <= 1


def test_price_action_patterns_run(d):
    found = set()
    for i in range(2, len(d["close"])):
        found.update(patterns(d["open"], d["high"], d["low"], d["close"], i))
    assert {"doji", "inside_bar"} <= found
