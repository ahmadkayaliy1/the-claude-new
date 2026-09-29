"""Phase 5 B16 (gold clock, round levels, the measured London-sweep base rate) and the DST side of B5 (sessions are read
in their own zone). Real rows: the XAUUSD@ / BTCUSDT 15m bars of the production stores (tests/fixtures/real, see
PROVENANCE.md) — every expected number is recomputed here with a different method (bars grouped per local date with
zoneinfo, plain loops). The synthetic bars are pure-logic edge cases and say so."""
import csv
import datetime as dt
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pytest

from tradingsystem.analysis import gold_clock as gc
from tradingsystem.analysis import indicators as ind
from tradingsystem.analysis import session_stats as ss
from tradingsystem.ai.model_view import model_view
from tradingsystem.core.timeutil import iso

REAL = Path(__file__).resolve().parents[1] / "fixtures" / "real"
UTC, LDN, NY = dt.timezone.utc, ZoneInfo("Europe/London"), ZoneInfo("America/New_York")
M15, H, DAY = 900_000, 3_600_000, 86_400_000
AS_OF = 1790666700000                      # 2026-09-29 07:25 UTC, the instant of the other real fixtures
DAY0 = AS_OF - AS_OF % DAY


def ms(y, mo, d, h=0, mi=0) -> int:
    return int(dt.datetime(y, mo, d, h, mi, tzinfo=UTC).timestamp() * 1000)


def load_bars(name: str):
    with open(REAL / name, newline="") as f:
        rows = list(csv.DictReader(f))
    return (np.array([int(r["open_time"]) for r in rows], dtype=np.int64),
            *(np.array([float(r[k]) for r in rows]) for k in ("open", "high", "low", "close")))


XAU = load_bars("xauusd_15m_9300.csv")
BTC = load_bars("btcusdt_15m_3500.csv")


def utc(ts: int) -> dt.datetime:
    return dt.datetime.fromtimestamp(ts / 1000, tz=UTC)


# ---------------------------------------------------------------------------------------------------- next events
def events_of(day: dt.date, n: int = 8) -> dict[str, str]:
    """{name: 'HH:MM' UTC} of the events of one weekday, asked from that day's 00:00 UTC."""
    at = int(dt.datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp() * 1000)
    return {name: t[11:16] for name, t, _ in gc.next_events(at, n) if t[:10] == day.isoformat()}


def test_event_times_in_the_two_dst_mismatch_windows():
    """UK clocks end BST 2026-10-25, US clocks end EDT 2026-11-01: for the week between London is on GMT (UTC+0) and New
    York on EDT (UTC-4), so the London-New York gap is 4 h instead of 5 h. Expected UTC times written out by hand."""
    both_dst = events_of(dt.date(2026, 10, 20))                     # Tuesday: BST + EDT
    assert both_dst == {"ldn_open": "07:00", "lbma_am": "09:30", "lbma_pm": "14:00", "comex_open": "12:20",
                        "us_data": "12:30", "nyse_open": "13:30", "comex_settle": "17:30", "rollover": "21:00"}
    mismatch = events_of(dt.date(2026, 10, 27))                     # Tuesday: GMT + EDT
    assert mismatch == {"ldn_open": "08:00", "lbma_am": "10:30", "lbma_pm": "15:00", "comex_open": "12:20",
                        "us_data": "12:30", "nyse_open": "13:30", "comex_settle": "17:30", "rollover": "21:00"}
    winter = events_of(dt.date(2026, 11, 3))                        # Tuesday: GMT + EST
    assert winter == {"ldn_open": "08:00", "lbma_am": "10:30", "lbma_pm": "15:00", "comex_open": "13:20",
                      "us_data": "13:30", "nyse_open": "14:30", "comex_settle": "18:30", "rollover": "22:00"}
    # the spring mismatch (US clocks change 2026-03-08, UK 2026-03-29): London GMT, New York EDT
    spring = events_of(dt.date(2026, 3, 10))
    assert spring["ldn_open"] == "08:00" and spring["comex_open"] == "12:20" and spring["us_data"] == "12:30"


def test_the_change_day_itself_uses_the_new_offset_for_events_after_the_change():
    """2026-10-25 is a Sunday (no events); Monday 10-26 already reads London as GMT."""
    assert events_of(dt.date(2026, 10, 26))["ldn_open"] == "08:00"
    assert events_of(dt.date(2026, 10, 23))["ldn_open"] == "07:00"          # the Friday before, still BST


def test_next_events_lists_the_soonest_three_with_minutes_away_and_skips_weekends():
    at = ms(2026, 9, 29, 7, 25)                                     # Tuesday 07:25 UTC (BST, EDT)
    nxt = gc.next_events(at)
    assert [n for n, _t, _m in nxt] == ["lbma_am", "comex_open", "us_data"]
    assert nxt[0][1] == "2026-09-29T09:30:00.000Z" and nxt[0][2] == 125
    assert nxt[1][2] == 295 and nxt[2][2] == 305
    sat = gc.next_events(ms(2026, 10, 24, 12, 0))                   # Saturday noon: everything is on Monday
    assert all(t.startswith("2026-10-26") for _n, t, _m in sat)
    assert sat[0][0] == "ldn_open" and sat[0][1] == "2026-10-26T08:00:00.000Z"      # GMT by then
    assert sat[0][2] == round((ms(2026, 10, 26, 8) - ms(2026, 10, 24, 12)) / 60_000)
    fri = gc.next_events(ms(2026, 10, 30, 21, 30))                  # Friday after the rollover: the next is Monday
    assert fri[0][1].startswith("2026-11-02")


def test_next_events_after_the_last_event_of_the_day_roll_into_the_next_weekday():
    nxt = gc.next_events(ms(2026, 9, 29, 21, 30))
    assert nxt[0][0] == "ldn_open" and nxt[0][1] == "2026-09-30T07:00:00.000Z"


# ------------------------------------------------------------------------------------------------- round levels
def test_round_levels_bracket_the_price_and_measure_the_distance_in_atr():
    r = gc.round_levels(4141.3, 5.0, 2)
    assert r["10"] == [4140.0, 0.26, 4150.0, 1.74]
    assert r["50"] == [4100.0, 8.26, 4150.0, 1.74]
    on = gc.round_levels(4150.0, 5.0, 2)                            # exactly on a level: the levels strictly around
    assert on["10"] == [4140.0, 2.0, 4160.0, 2.0] and on["50"] == [4100.0, 10.0, 4200.0, 10.0]
    assert gc.round_levels(4141.3, None, 2)["10"] == [4140.0, None, 4150.0, None]
    lo = gc.round_levels(4149.99, 1.0, 2)
    assert lo["10"][2] == 4150.0 and lo["50"][2] == 4150.0


# ------------------------------------------------------------------------------------------------- asia / sweep
def cut_bars(bars, as_of: int):
    ot = bars[0]
    keep = ot + M15 <= as_of                                          # only closed bars, like closed_only
    return tuple(a[keep] for a in bars)


def test_asia_range_partial_then_complete_on_real_bars_and_never_reads_later_bars():
    ot, o, h, lo, c = XAU
    day = dt.date(2026, 9, 29)
    inside = (ot >= DAY0) & (ot < DAY0 + 7 * H)
    early = DAY0 + 3 * H + 20 * 60_000                                # 03:20 UTC: bars up to 03:00 are closed
    part = gc.asia_range(ot, h, lo, M15, day, early)
    m = inside & (ot + M15 <= early)
    assert part["complete"] is False
    assert (part["high"], part["low"]) == (h[m].max(), lo[m].min()) and m.sum() == 13
    full = gc.asia_range(ot, h, lo, M15, day, AS_OF)
    assert full["complete"] is True and (full["high"], full["low"]) == (h[inside].max(), lo[inside].min())
    assert full["width"] == pytest.approx(h[inside].max() - lo[inside].min())
    # causality: a spike stamped after the instant changes nothing
    h2, lo2 = h.copy(), lo.copy()
    later = ot + M15 > early
    h2[later], lo2[later] = 1e9, -1e9
    assert gc.asia_range(ot, h2, lo2, M15, day, early) == part


def test_asia_range_needs_bars_and_a_complete_one_needs_coverage():
    ot, _o, h, lo, _c = XAU
    assert gc.asia_range(ot, h, lo, M15, dt.date(2026, 9, 26), ms(2026, 9, 26, 12)) is None      # Saturday: closed
    keep = ~((ot >= DAY0 + H) & (ot < DAY0 + 4 * H))                 # 3 h of the 7 h missing: 57 % coverage
    assert gc.asia_range(ot[keep], h[keep], lo[keep], M15, dt.date(2026, 9, 29), AS_OF) is None


def real_day_windows(bars, day: dt.date, cut: int):
    """Independent expectation for one date: (asia (hi, lo) | None, london bars mask) — bars grouped by their wall-clock
    time in Europe/London instead of the module's window arithmetic."""
    ot, _o, h, lo, c = bars
    asia = [i for i in range(len(ot)) if utc(int(ot[i])).date() == day and 0 <= utc(int(ot[i])).hour < 7
            and ot[i] + M15 <= cut]
    ldn = [i for i in range(len(ot)) if ot[i] + M15 <= cut
           and (lt := utc(int(ot[i])).astimezone(LDN)).date() == day and 8 <= lt.hour < 12]
    return asia, ldn


def test_london_sweep_on_real_bars_matches_a_wall_clock_grouping():
    ot, o, h, lo, c = XAU
    for day, cut in ((dt.date(2026, 9, 29), AS_OF), (dt.date(2026, 9, 28), ms(2026, 9, 28, 13, 0)),
                     (dt.date(2026, 9, 24), ms(2026, 9, 24, 9, 40))):
        asia, ldn = real_day_windows(XAU, day, cut)
        rng = gc.asia_range(ot, h, lo, M15, day, cut)
        hi_a, lo_a = h[asia].max(), lo[asia].min()
        assert rng["complete"] and (rng["high"], rng["low"]) == (hi_a, lo_a)
        got = gc.london_sweep(ot, h, lo, c, M15, rng, day, cut)
        up, dn = bool(h[ldn].max() > hi_a), bool(lo[ldn].min() < lo_a)
        if up or dn:
            side = "both" if up and dn else "high" if up else "low"
            assert got == (side, bool(lo_a <= c[ldn[-1]] <= hi_a)), (day, got)
        else:
            assert got == (None, None)


def synth(days: list[dt.date], asia=(100.0, 110.0), london=None, tf=M15):
    """Hand-built 15m bars for whole UTC days (00:00-24:00): a flat 105 except the Asia window (00:00-07:00 UTC, which
    spans exactly ``asia`` = (low, high)) and, for ``london(day)`` = None | (side, back_inside), the London window."""
    ot, o, h, lo, c = [], [], [], [], []
    for d in days:
        s = int(dt.datetime(d.year, d.month, d.day, tzinfo=UTC).timestamp() * 1000)
        ls, le = gc.london_window(d)
        plan = london(d) if london else None
        for t in range(s, s + DAY, tf):
            hi_, lo_, cl = 106.0, 104.0, 105.0
            if t < s + 7 * H:                                       # Asia: exactly the range, close inside
                hi_, lo_ = (asia[1], asia[0]) if t == s else (108.0, 102.0)
            elif plan and ls <= t < le:
                side, back = plan
                last = t == le - tf
                if t == ls:                                         # the sweep bar
                    hi_ = asia[1] + 5 if side in ("high", "both") else 108.0
                    lo_ = asia[0] - 5 if side in ("low", "both") else 102.0
                if last:                                            # the window's last bar decides "back inside"
                    cl = 105.0 if back else asia[1] + 3
                    hi_ = max(hi_, cl)
            ot.append(t), o.append(105.0), h.append(hi_), lo.append(lo_), c.append(cl)
    return tuple(np.array(x) for x in (ot, o, h, lo, c))


def test_london_sweep_sides_and_back_inside_on_synthetic_bars():
    """Pure logic (hand-built bars): high / low / both sweeps, closing back inside or staying outside, no sweep."""
    day = dt.date(2026, 9, 29)
    for plan, expected in (
            (("high", True), ("high", True)), (("high", False), ("high", False)), (("low", True), ("low", True)),
            (("both", True), ("both", True)), (None, (None, None))):
        ot, o, h, lo, c = synth([day], london=lambda d: plan)
        cut = ms(2026, 9, 30)
        rng = gc.asia_range(ot, h, lo, M15, day, cut)
        assert (rng["high"], rng["low"]) == (110.0, 100.0)
        assert gc.london_sweep(ot, h, lo, c, M15, rng, day, cut) == expected, plan


def test_london_swept_is_none_before_the_asia_range_is_complete_and_before_a_london_bar_exists():
    day = dt.date(2026, 9, 29)
    ot, o, h, lo, c = synth([day], london=lambda d: ("high", True))
    part = gc.asia_range(ot, h, lo, M15, day, ms(2026, 9, 29, 5))
    assert part["complete"] is False
    assert gc.london_sweep(ot, h, lo, c, M15, part, day, ms(2026, 9, 29, 5)) is None
    rng = gc.asia_range(ot, h, lo, M15, day, ms(2026, 9, 29, 7))
    assert rng["complete"] is True                                  # 07:00 UTC exactly: the window has passed
    assert gc.london_sweep(ot, h, lo, c, M15, rng, day, ms(2026, 9, 29, 7)) is None     # London (BST) opens at 07:00,
    assert gc.london_sweep(ot, h, lo, c, M15, rng, day, ms(2026, 9, 29, 7, 15)) == ("high", True)   # bar 07:00 closed


def test_the_london_window_moves_one_hour_with_the_uk_clock_and_the_asia_window_never_moves():
    summer, winter = dt.date(2026, 10, 23), dt.date(2026, 10, 26)
    assert gc.london_window(summer) == (ms(2026, 10, 23, 7), ms(2026, 10, 23, 11))
    assert gc.london_window(winter) == (ms(2026, 10, 26, 8), ms(2026, 10, 26, 12))
    assert gc.asia_window(summer) == (ms(2026, 10, 23, 0), ms(2026, 10, 23, 7)) == (
        gc.asia_window(summer)[0], gc.asia_window(winter)[1] - 3 * DAY)
    # a bar above the Asia high at 07:15 UTC is a London bar in summer and NOT one in winter (still before 08:00 GMT)
    for day, swept in ((summer, ("high", True)), (winter, (None, None))):
        ot, o, h, lo, c = synth([day])
        i = int(np.searchsorted(ot, int(dt.datetime(day.year, day.month, day.day, 7, 15, tzinfo=UTC).timestamp() * 1000)))
        h[i] = 200.0
        cut = int(dt.datetime(day.year, day.month, day.day, 9, tzinfo=UTC).timestamp() * 1000)
        rng = gc.asia_range(ot, h, lo, M15, day, cut)
        assert gc.london_sweep(ot, h, lo, c, M15, rng, day, cut) == swept


# --------------------------------------------------------------------------------------- the measured base rate
def test_gold_history_counts_the_measured_share_on_synthetic_days():
    """Pure logic: 100 consecutive days; day k sweeps and closes back inside when k % 4 == 0, sweeps and stays outside
    when k % 4 == 1, no sweep otherwise. The expected shares follow from the pattern; the cut is the day after."""
    start = dt.date(2026, 6, 1)
    days = [start + dt.timedelta(days=k) for k in range(100)]
    plan = {d: (("high", True) if k % 4 == 0 else ("high", False) if k % 4 == 1 else None) for k, d in enumerate(days)}
    ot, o, h, lo, c = synth(days, london=lambda d: plan[d])
    cut = ms(2026, 9, 9)                                            # 100 days from 06-01: the day after the last
    hist = gc.gold_history(ot, o, h, lo, c, M15, cut)
    assert hist["asia_days"] == 30 and hist["asia_width_median"] == 10.0
    for n in (30, 90):
        window = [(k, d) for k, d in enumerate(days) if d >= dt.date(2026, 9, 9) - dt.timedelta(days=n)]
        assert hist["sweep"][str(n)] == [len(window),
                                         round(100 * sum(1 for k, _ in window if k % 4 in (0, 1)) / len(window)),
                                         round(100 * sum(1 for k, _ in window if k % 4 == 0) / len(window))]


def test_gold_history_on_real_xau_bars_matches_a_wall_clock_recount_and_is_causal():
    ot, o, h, lo, c = XAU
    cut = DAY0
    hist = gc.gold_history(ot, o, h, lo, c, M15, cut)
    day0 = utc(cut).date()
    per_day = []
    for back in range(1, 91):
        day = day0 - dt.timedelta(days=back)
        asia, ldn = real_day_windows(XAU, day, cut)
        if len(asia) < 0.85 * 28:
            continue
        hi_a, lo_a = h[asia].max(), lo[asia].min()
        row = [back, hi_a - lo_a, None]
        if len(ldn) >= 0.85 * 16:
            up, dn = h[ldn].max() > hi_a, lo[ldn].min() < lo_a
            row[2] = (bool(up or dn), bool((up or dn) and lo_a <= c[ldn[-1]] <= hi_a))
        per_day.append(row)
    assert hist["asia_width_median"] == pytest.approx(float(np.median([w for b, w, _ in per_day if b <= 30])))
    assert hist["asia_days"] == sum(1 for b, _w, _s in per_day if b <= 30)
    for n in (30, 90):
        rows = [s for b, _w, s in per_day if b <= n and s is not None]
        assert hist["sweep"][str(n)] == [len(rows), round(100 * sum(s[0] for s in rows) / len(rows)),
                                         round(100 * sum(s[1] for s in rows) / len(rows))]
    # a measured share, not a constant: it moves when the bars do
    o2, h2, lo2, c2 = o.copy(), h.copy(), lo.copy(), c.copy()
    later = ot >= cut
    h2[later], lo2[later] = 1e9, -1e9                                # nothing at or after the cut may be read
    assert gc.gold_history(ot, o2, h2, lo2, c2, M15, cut) == hist
    flat = np.where(ot < cut, 105.0, c)
    assert gc.gold_history(ot, flat, flat + 1, flat - 1, flat, M15, cut)["sweep"] != hist["sweep"]


# ------------------------------------------------------------------------------------------------- the clock block
def test_clock_block_on_the_real_morning_and_its_token_budget():
    ot, o, h, lo, c = cut_bars(XAU, AS_OF)
    hist = gc.gold_history(*XAU[:1], XAU[1], XAU[2], XAU[3], XAU[4], M15, DAY0)
    blk = gc.clock_block(AS_OF, ot, h, lo, c, M15, hist, "open", 2)
    assert blk["session"] == "asia+london" and blk["desk_window"] == "open"
    a = blk["asia_range"]
    assert a["complete"] is True and a["width"] == pytest.approx(a["high"] - a["low"], abs=0.01)
    assert a["width_x_median"] == round(a["width"] / hist["asia_width_median"], 2)
    assert blk["london_swept"] == "none" or set(blk["london_swept"]) == {"side", "back_inside"}
    assert [n for n, _t, _m in blk["next"]] == ["lbma_am", "comex_open", "us_data"]
    assert blk["london_asia_sweep_days"] == hist["sweep"]
    json.dumps(blk)                                                 # plain JSON (no numpy scalars)
    view = model_view({"meta": {"as_of": iso(AS_OF)}, "market": {"gold_clock": blk},
                       "levels": {"round": gc.round_levels(4141.3, 5.0, 2)}})
    text = json.dumps({"gold_clock": view["market"]["gold_clock"], "round": view["levels"]["round"]},
                      separators=(",", ":"))
    assert len(text) / 4 <= 190                                     # the B16 budget, chars / 4
    assert '"complete":1' in text and "true" not in text            # flags as 1 / 0


def test_clock_block_before_the_asia_range_is_complete_and_on_a_weekend():
    ot, o, h, lo, c = cut_bars(XAU, DAY0 + 5 * H)
    hist = {"asia_width_median": 30.0}
    early = gc.clock_block(DAY0 + 5 * H, ot, h, lo, c, M15, hist, "closed:outside_window", 2)
    assert early["asia_range"]["complete"] is False and "width_x_median" not in early["asia_range"]
    assert early["london_swept"] == "none" and early["session"] == "asia"
    assert early["desk_window"] == "closed:outside_window"
    sat = ms(2026, 9, 26, 12)
    ot, o, h, lo, c = cut_bars(XAU, sat)
    weekend = gc.clock_block(sat, ot, h, lo, c, M15, hist, "closed:market_closed", 2)
    assert "asia_range" not in weekend and "london_swept" not in weekend and weekend["desk_window"] == "closed:market_closed"


# ---------------------------------------------------------------------------------------- B5: sessions and DST
def test_session_windows_follow_each_zone_across_both_dst_changes():
    def w(name, day):
        s, e = ss.session_window(name, day)
        return iso(s)[11:16], iso(e)[11:16]
    assert w("london", dt.date(2026, 10, 23)) == ("07:00", "16:00")      # BST
    assert w("london", dt.date(2026, 10, 26)) == ("08:00", "17:00")      # GMT: the UK change is 10-25
    assert w("new_york", dt.date(2026, 10, 28)) == ("12:00", "21:00")    # still EDT: the US change is 11-01
    assert w("new_york", dt.date(2026, 11, 2)) == ("13:00", "22:00")     # EST
    assert w("asia", dt.date(2026, 7, 1)) == w("asia", dt.date(2026, 12, 1)) == ("00:00", "09:00")   # Tokyo: no DST


def test_windows_between_only_returns_complete_windows_inside_the_range():
    lo, hi = ms(2026, 10, 23), ms(2026, 10, 30)
    ws = ss.windows_between("london", lo, hi)
    assert ws[0] == (ms(2026, 10, 23, 7), ms(2026, 10, 23, 16)) and ws[-1][1] <= hi
    assert [iso(s)[11:16] for s, _ in ws] == ["07:00"] * 2 + ["08:00"] * 5       # 10-23, 10-24 (Sat), then GMT days
    assert all(lo <= s and e <= hi for s, e in ws)
    assert ss.windows_between("new_york", ms(2026, 10, 30), ms(2026, 11, 4))[0][0] == ms(2026, 10, 30, 12)


def test_window_stats_covers_exactly_the_local_wall_clock_hours_in_summer_the_mismatch_week_and_winter():
    """Pure logic: bars inside the London 08:00-17:00 local window trade 50-100, every other bar 0-1; an ATR of 1. A
    window misplaced by an hour would include an outside bar and measure 100, not 50."""
    days = [dt.date(2026, 7, 15), dt.date(2026, 10, 28), dt.date(2026, 11, 5)]      # BST / GMT (US still EDT) / GMT
    for day in days:
        s0 = int(dt.datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp() * 1000) - DAY
        ot = np.arange(s0, s0 + 3 * DAY, M15)
        local = [utc(int(t)).astimezone(LDN) for t in ot]
        inside = np.array([8 <= x.hour < 17 for x in local])
        h = np.where(inside, 100.0, 1.0)
        lo = np.where(inside, 50.0, 0.0)
        c = np.where(inside, 60.0, 0.5)
        o = np.where(inside, 55.0, 0.5)
        s, e = ss.session_window("london", day)
        r = ss.window_stats(ot, o, h, lo, c, np.full(len(ot), 1.0), M15, s, e)
        assert r == (50.0, True), day
        assert utc(s).astimezone(LDN).hour == 8 and utc(e).astimezone(LDN).hour == 17


# ------------------------------------------------------------------------------- B5 on the real bars (independent)
def wall_clock_stats(bars, name: str, cut: int, days: int = 30):
    """[n, mean range in ATR, up %] recomputed by grouping bars per local date with zoneinfo."""
    ot, o, h, lo, c = bars
    keep = ot + M15 <= cut
    ot, o, h, lo, c = ot[keep], o[keep], h[keep], lo[keep], c[keep]
    atr = ind.atr(h, lo, c)
    tz, a, b = ss.ctx.SESSION_HOURS[name]
    z = ZoneInfo(tz)
    groups: dict[dt.date, list[int]] = {}
    for i, t in enumerate(ot):
        lt = utc(int(t)).astimezone(z)
        if a <= lt.hour < b:
            groups.setdefault(lt.date(), []).append(i)
    rs, ups = [], []
    for d, idx in sorted(groups.items()):
        end = int(dt.datetime(d.year, d.month, d.day, b, tzinfo=z).timestamp() * 1000)
        start = int(dt.datetime(d.year, d.month, d.day, a, tzinfo=z).timestamp() * 1000)
        if start < cut - days * DAY or end > cut or len(idx) < 0.85 * (b - a) * 4:
            continue
        j = int(np.searchsorted(ot, start - M15, side="right")) - 1
        if j < 0 or not np.isfinite(atr[j]) or start - ot[j] > 4 * DAY:
            continue
        rs.append((h[idx].max() - lo[idx].min()) / atr[j])
        ups.append(c[idx[-1]] > o[idx[0]])
    return [len(rs), round(float(np.mean(rs)), 2), round(100 * sum(ups) / len(ups))] if rs else None


@pytest.mark.parametrize("name,bars", [("BTC", BTC), ("XAU", XAU)])
def test_session_history_matches_a_wall_clock_recount_on_real_bars(name, bars):
    ot, o, h, lo, c = bars
    got = ss.session_history(ot, o, h, lo, c, M15, DAY0)
    for sess in ss.ctx.SESSION_HOURS:
        assert got[sess] == wall_clock_stats(bars, sess, DAY0), (name, sess)
    if name == "BTC":
        assert all(got[s][0] == 30 for s in got)                     # 24/7: every one of the 30 days
    else:
        assert all(15 <= got[s][0] <= 22 for s in got)               # weekdays only (holidays / weekends have no bars)


def test_session_history_is_causal_nothing_at_or_after_the_cut_is_read():
    ot, o, h, lo, c = BTC
    base = ss.session_history(ot, o, h, lo, c, M15, DAY0)
    keep = ot < DAY0
    assert ss.session_history(ot[keep], o[keep], h[keep], lo[keep], c[keep], M15, DAY0) == base   # a shorter frame
    h2, lo2, c2 = h.copy(), lo.copy(), c.copy()
    h2[~keep], lo2[~keep], c2[~keep] = 1e9, -1e9, 1e9                 # poisoned future
    assert ss.session_history(ot, o, h2, lo2, c2, M15, DAY0) == base
    shifted = ss.session_history(ot, o, h, lo, c, M15, DAY0 - DAY)   # an earlier cut sees one day less
    assert shifted != base


def test_current_session_uses_closed_bars_only_and_compares_with_the_mean():
    ot, o, h, lo, c = BTC
    hist = ss.session_history(ot, o, h, lo, c, M15, DAY0)
    keep = ot + M15 <= AS_OF
    now = ss.current_sessions(ot[keep], o[keep], h[keep], lo[keep], c[keep], M15, AS_OF, hist)
    assert set(now) == {"asia", "london"}                            # 07:25 UTC (BST): Tokyo and London are open
    atr = ind.atr(h[keep], lo[keep], c[keep])
    s, _e = ss.session_window("london", dt.date(2026, 9, 29))
    i0 = int(np.searchsorted(ot[keep], s))
    j = int(np.searchsorted(ot[keep], s - M15, side="right")) - 1
    exp = (h[keep][i0:].max() - lo[keep][i0:].min()) / atr[j]
    assert now["london"] == [round(exp, 2), round(exp / hist["london"][1], 2), 25]
    # the bar still forming and later bars never count
    o2, h2, lo2, c2, ot2 = (np.append(x, v) for x, v in ((o[keep], 1.0), (h[keep], 1e9), (lo[keep], 0.0),
                                                        (c[keep], 1.0), (ot[keep], AS_OF - 300_000)))
    assert ss.current_sessions(ot2, o2, h2, lo2, c2, M15, AS_OF, hist) == now      # opened before as_of, not closed
    o3, h3, lo3, c3, ot3 = (np.append(x, v) for x, v in ((o[keep], 1.0), (h[keep], 1e9), (lo[keep], 0.0),
                                                        (c[keep], 1.0), (ot[keep], AS_OF)))
    assert ss.current_sessions(ot3, o3, h3, lo3, c3, M15, AS_OF, hist) == now
    assert ss.current_sessions(ot[keep], o[keep], h[keep], lo[keep], c[keep], M15, AS_OF, {})["london"][1] is None


def test_session_active_flags_use_the_same_table_as_the_statistics():
    """``context.sessions`` and the statistics share ``SESSION_HOURS``: the active list at a window's first and last
    instant agrees with the window, in summer and winter."""
    from tradingsystem.analysis import context as ctx
    for day in (dt.date(2026, 7, 15), dt.date(2026, 10, 28), dt.date(2026, 12, 2)):
        for name in ctx.SESSION_HOURS:
            s, e = ss.session_window(name, day)
            assert name in ctx.sessions(s)["active"] and name not in ctx.sessions(s - 1)["active"]
            assert name in ctx.sessions(e - 1)["active"] and name not in ctx.sessions(e)["active"]
