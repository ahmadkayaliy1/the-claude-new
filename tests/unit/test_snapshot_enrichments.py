"""Phase 5 checkpoint-B snapshot enrichments on real rows of the production databases (tests/fixtures/real, see
PROVENANCE.md): B1 the order book depth block (bands only where the book reaches, 1-h imbalance change, stale = no
bands and the capability downgraded), B2 the previous week / month levels and the month / year open from the daily
bars (broker day roll, completed periods only, causal), B3 the forming bar of the 15m / 5m frames (display context:
never in ``recent``, never a trigger input). Every expected number is computed here from the fixture rows,
independently of the module; the few hand-built inputs are pure-logic edge cases and say so."""
import copy
import csv
import datetime as dt
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pytest

from tradingsystem.ai import triggers
from tradingsystem.ai.model_view import model_view
from tradingsystem.analysis import context as ctx
from tradingsystem.analysis.frames import Frame, forming_bar
from tradingsystem.analysis.registry import capability_matrix
from tradingsystem.analysis.snapshot import DEPTH_MAX_AGE_MS, SnapshotBuilder, depth_block
from tradingsystem.core.instruments import InstrumentRegistry
from tradingsystem.core.settings import PathsCfg, load_settings
from tradingsystem.core.timeframes import Timeframe
from tradingsystem.core.timeutil import iso
from tradingsystem.storage.sqlite_store import SQLiteHotStore
from tradingsystem.storage.tablespec import spec_for, system_specs, table_specs

REAL = Path(__file__).resolve().parents[1] / "fixtures" / "real"
MIN, H, DAY = 60_000, 3_600_000, 86_400_000
M5, M15, H1 = (Timeframe.parse(t) for t in ("5m", "15m", "1h"))
UTC, NY = dt.timezone.utc, ZoneInfo("America/New_York")


def ms(y, mo, d, h=0, mi=0, s=0) -> int:
    return int(dt.datetime(y, mo, d, h, mi, s, tzinfo=UTC).timestamp() * 1000)


# ------------------------------------------------------------------------------------------------------------ depth
def load_depth(symbol: str) -> list[tuple]:
    with open(REAL / f"{symbol.lower()}_depth_snapshots.csv", newline="") as f:
        r = csv.reader(f)
        next(r)
        return [(int(a), float(b), float(c), float(d)) for a, b, c, d in r]


DEPTH = {"BTCUSDT": load_depth("BTCUSDT"), "ETHUSDT": load_depth("ETHUSDT")}


def snapshots(symbol: str) -> dict[int, dict[float, float]]:
    out: dict[int, dict[float, float]] = {}
    for ts, pct, _q, notional in DEPTH[symbol]:
        out.setdefault(ts, {})[pct] = notional
    return out


def block(symbol: str, as_of: int, rows=None, ref=True) -> dict:
    rows = DEPTH[symbol] if rows is None else rows
    a = list(zip(*rows))
    kw = {}
    if ref:                                     # what SnapshotBuilder._depth passes: the rows around one hour earlier
        kw = dict(ref_ts=a[0], ref_pct=a[1], ref_notional=a[3])
    return depth_block(a[0], a[1], a[3], as_of, **kw)


def expected_rows(snap: dict[float, float]) -> list[list]:
    """[band, bid $M, ask $M, imbalance] for the bands present on both sides."""
    out = []
    for b in sorted({abs(p) for p in snap}):
        if -b in snap and b in snap:
            bid, ask = snap[-b], snap[b]
            out.append([b, round(bid / 1e6, 2), round(ask / 1e6, 2), round((bid - ask) / (bid + ask), 2)])
    return out


def imb(snap: dict[float, float], b: float) -> float:
    return (snap[-b] - snap[b]) / (snap[-b] + snap[b])


def test_btc_depth_shows_only_the_bands_the_book_reaches_on_both_sides():
    snaps = snapshots("BTCUSDT")
    last = max(snaps)
    d = block("BTCUSDT", last + 30_000)
    assert d["data_quality"] == "real" and d["time"] == iso(last) and d["age_s"] == 30
    assert d["bands"] == expected_rows(snaps[last])
    # the real book: the bid side reaches -1 % but the ask side does not reach +1 % -> ±1 is not a band, ±2/±5 never
    assert -1.0 in snaps[last] and 1.0 not in snaps[last]
    assert [r[0] for r in d["bands"]] == [0.1, 0.25, 0.5]
    assert d["band_columns"] == ["band_pct", "bid_musd", "ask_musd", "imbalance"]


def test_eth_depth_reaches_two_percent_and_never_five():
    snaps = snapshots("ETHUSDT")
    last = max(snaps)
    d = block("ETHUSDT", last + 5_000)
    assert d["bands"] == expected_rows(snaps[last]) and [r[0] for r in d["bands"]] == [0.1, 0.25, 0.5, 1.0, 2.0]
    assert all(r[0] != 5.0 for r in d["bands"])


def test_the_one_hour_imbalance_change_uses_the_snapshot_nearest_to_an_hour_before():
    for sym, keys in (("BTCUSDT", {"0.5"}), ("ETHUSDT", {"0.5", "1"})):
        snaps = snapshots(sym)
        last = max(snaps)
        ref = min((t for t in snaps if t < last - 30 * MIN), key=lambda t: abs(t - (last - H)))
        assert abs(ref - (last - H)) < 5 * MIN
        d = block(sym, last + 1_000)
        assert set(d["imbalance_change_1h"]) == keys
        for k in keys:
            assert d["imbalance_change_1h"][k] == round(imb(snaps[last], float(k)) - imb(snaps[ref], float(k)), 2)


def test_no_change_without_a_snapshot_near_an_hour_before():
    snaps = snapshots("BTCUSDT")
    last = max(snaps)
    recent = [r for r in DEPTH["BTCUSDT"] if r[0] > last - 30 * MIN]          # the hour-old snapshots are gone
    d = block("BTCUSDT", last + 1_000, rows=recent)
    assert d["data_quality"] == "real" and "imbalance_change_1h" not in d
    assert "imbalance_change_1h" not in block("BTCUSDT", last + 1_000, ref=False)


def test_a_snapshot_older_than_120_s_is_stale_without_bands():
    last = max(snapshots("ETHUSDT"))
    ok = block("ETHUSDT", last + DEPTH_MAX_AGE_MS)                              # exactly 120 s: still the current book
    assert ok["data_quality"] == "real" and ok["age_s"] == 120
    st = block("ETHUSDT", last + DEPTH_MAX_AGE_MS + 1)
    assert st["data_quality"] == "stale" and "bands" not in st and st["time"] == iso(last)
    assert st["age_s"] == 120 and "120" in st["reason"]
    assert block("ETHUSDT", last + 10 * MIN)["data_quality"] == "stale"


def test_depth_never_reads_a_snapshot_stamped_after_as_of():
    snaps = snapshots("BTCUSDT")
    ts = sorted(snaps)
    as_of = ts[-3] + 20_000                                                    # two later snapshots exist
    d = block("BTCUSDT", as_of)
    assert d["time"] == iso(ts[-3]) and d["bands"] == expected_rows(snaps[ts[-3]])
    assert block("BTCUSDT", ts[0] - 1)["data_quality"] == "unavailable"


@pytest.fixture
def env(tmp_path):
    s = load_settings(env_path=Path("nope.env"))
    s = s.model_copy(update={"paths": PathsCfg(data_dir=str(tmp_path / "data"), logs_dir=str(tmp_path / "logs"))})
    reg = InstrumentRegistry.from_settings(s)
    builders: list[SnapshotBuilder] = []

    def seed(pair: str, rows) -> None:
        inst = reg.primary(pair)
        with SQLiteHotStore(inst.hot_db_path(s.paths.data())) as st:
            st.ensure_tables([*table_specs(inst), *system_specs()])
            st.upsert(spec_for(inst, "depth"), list(rows))

    def builder() -> SnapshotBuilder:
        b = SnapshotBuilder(s, reg)
        builders.append(b)
        return b

    yield type("Env", (), {"s": s, "reg": reg, "seed": staticmethod(seed), "builder": staticmethod(builder)})
    for b in builders:
        b.wait_background()               # a daily-profile worker of one test must not run into the next
        b.close()


def test_the_builder_reads_the_stored_depth_and_a_stale_book_downgrades_the_capability(env):
    env.seed("ETHUSDT", DEPTH["ETHUSDT"])
    last = max(snapshots("ETHUSDT"))
    b = env.builder()
    caps = capability_matrix(env.s, env.reg, "ETHUSDT")
    assert caps["order_book_depth"].quality == "real"
    d = b._depth(caps, last + 20_000)
    assert d["data_quality"] == "real" and d["bands"] == expected_rows(snapshots("ETHUSDT")[last])
    assert set(d["imbalance_change_1h"]) == {"0.5", "1"}                       # the hour-old rows come from the store
    p = b.build("ETHUSDT", last + 20_000)
    assert p["orderflow"]["depth"] == d and p["capabilities"]["order_book_depth"]["quality"] == "real"
    stale = b.build("ETHUSDT", last + 5 * MIN)                                 # today: capabilities claim real, no data
    assert stale["orderflow"]["depth"]["data_quality"] == "stale" and "bands" not in stale["orderflow"]["depth"]
    cap = stale["capabilities"]["order_book_depth"]
    assert cap["quality"] == "unavailable" and "300 s old" in cap["reason"]


def test_no_depth_block_where_depth_is_not_collected(env):
    b = env.builder()
    caps = capability_matrix(env.s, env.reg, "XAUUSD")
    assert caps["order_book_depth"].quality == "unavailable"
    assert b._depth(caps, 1790666700000) is None
    assert "depth" not in b.build("XAUUSD", 1790666700000)["orderflow"]


def test_the_model_view_renders_the_depth_compactly():
    last = max(snapshots("ETHUSDT"))
    d = block("ETHUSDT", last + 20_000)
    v = model_view({"meta": {"as_of": iso(last + 20_000)}, "orderflow": {"depth": d}})["orderflow"]["depth"]
    assert "band_columns" not in v and v["bands"] == d["bands"] and v["time"].count("-") == 1     # MM-DD HH:MM
    assert d["band_columns"]                                                    # the payload keeps the names
    text = json.dumps(v, separators=(",", ":"))
    assert len(text) / 4 <= 250                                                 # the B1 budget, chars / 4
    assert json.dumps(d)                                                        # plain JSON, no numpy scalars


# ------------------------------------------------------------------------------------------- previous week / month
def load_daily(name: str):
    with open(REAL / name, newline="") as f:
        r = csv.reader(f)
        next(r)
        rows = [tuple(float(x) for x in row) for row in r]
    a = np.array(rows)
    return a[:, 0].astype(np.int64), a[:, 1], a[:, 2], a[:, 3], a[:, 4]


BTC_D = load_daily("btcusdt_1d_400.csv")
XAU_D = load_daily("xauusd_1d_400.csv")
AS_OF_REAL = 1790666700000                                                     # 2026-09-29 07:25 UTC (Tuesday)


def levels(daily, as_of, roll):
    ot, o, h, lo, _c = daily
    return ctx.period_levels(ot, o, h, lo, as_of, day_roll=roll)


def in_range(daily, a: int, b: int, roll: str):
    """The bars whose OPEN lies in [a, b) — an interval formulation, independent of the module's day keys."""
    ot = daily[0]
    return (ot >= a) & (ot < b)


def test_btc_previous_week_month_and_opens_on_real_daily_bars():
    lv = levels(BTC_D, AS_OF_REAL, "utc")
    _, o, h, lo, _ = BTC_D
    wk = in_range(BTC_D, ms(2026, 9, 21), ms(2026, 9, 28), "utc")             # Mon 09-21 .. Sun 09-27
    mo = in_range(BTC_D, ms(2026, 8, 1), ms(2026, 9, 1), "utc")
    assert wk.sum() == 7 and mo.sum() == 31
    assert lv["pwh"] == h[wk].max() and lv["pwl"] == lo[wk].min()
    assert lv["pmh"] == h[mo].max() and lv["pml"] == lo[mo].min()
    assert lv["month_open"] == o[BTC_D[0] == ms(2026, 9, 1)][0]
    assert lv["year_open"] == o[BTC_D[0] == ms(2026, 1, 1)][0]
    assert set(lv) == {"pwh", "pwl", "pmh", "pml", "month_open", "year_open"}


def test_xau_broker_day_roll_previous_week_is_sunday_17_new_york_to_sunday_17():
    """XAU trades Sunday 17:00 -> Friday 17:00 New York: the trading week of Monday 09-21 opens Sunday 09-20 17:00 NY."""
    lv = levels(XAU_D, AS_OF_REAL, "ny17")
    _, o, h, lo, _ = XAU_D
    a = int(dt.datetime(2026, 9, 20, 17, tzinfo=NY).timestamp() * 1000)
    b = int(dt.datetime(2026, 9, 27, 17, tzinfo=NY).timestamp() * 1000)
    wk = in_range(XAU_D, a, b, "ny17")
    assert 5 <= wk.sum() <= 6
    assert lv["pwh"] == h[wk].max() and lv["pwl"] == lo[wk].min()
    ny = lambda y, m, d: int(dt.datetime(y, m, d, 17, tzinfo=NY).timestamp() * 1000)      # noqa: E731
    mo = in_range(XAU_D, ny(2026, 8, 2), ny(2026, 8, 31), "ny17")           # Aug 1 is a Saturday: Sun 08-02 17:00 NY
    assert mo.sum() >= 20
    assert lv["pmh"] == h[mo].max() and lv["pml"] == lo[mo].min()
    sep1 = int(dt.datetime(2026, 8, 31, 17, tzinfo=NY).timestamp() * 1000)     # Sep 1 (Tue) trading day opens Mon 17:00 NY
    assert lv["month_open"] == o[XAU_D[0] == sep1][0]
    jan = XAU_D[0][(XAU_D[0] >= ms(2025, 12, 31)) & (XAU_D[0] < ms(2026, 1, 8))]
    first_2026 = min(t for t in jan if dt.datetime.fromtimestamp(t / 1000, tz=UTC).astimezone(NY)
                     + dt.timedelta(hours=7) >= dt.datetime(2026, 1, 1, tzinfo=NY))
    assert lv["year_open"] == XAU_D[1][XAU_D[0] == first_2026][0]


def test_period_levels_are_causal_no_bar_at_or_after_as_of_leaks_in():
    ot, o, h, lo, c = BTC_D
    for as_of in (AS_OF_REAL, ms(2026, 9, 28), ms(2026, 9, 27, 23, 59, 59) + 999, ms(2026, 9, 1), ms(2026, 1, 1),
                  ms(2026, 10, 1)):
        past = ot < as_of
        clean = ctx.period_levels(ot[past], o[past], h[past], lo[past], as_of)
        # the same call with every bar of the fixture (bars after as_of, one opening exactly at as_of included), and
        # with the bars after as_of replaced by absurd values: nothing changes
        assert ctx.period_levels(ot, o, h, lo, as_of) == clean
        hh, ll, oo = h.copy(), lo.copy(), o.copy()
        hh[~past], ll[~past], oo[~past] = 1e9, -1e9, 1e9
        assert ctx.period_levels(ot, oo, hh, ll, as_of) == clean


def test_a_bar_at_the_period_boundary_belongs_to_the_new_period():
    ot, o, h, lo, _ = BTC_D
    mon = ms(2026, 9, 28)
    just_before = ctx.period_levels(ot[ot < mon], o[ot < mon], h[ot < mon], lo[ot < mon], mon - 1)
    at = ctx.period_levels(ot[ot <= mon], o[ot <= mon], h[ot <= mon], lo[ot <= mon], mon)
    wk_old = in_range(BTC_D, ms(2026, 9, 14), ms(2026, 9, 21), "utc")
    wk_new = in_range(BTC_D, ms(2026, 9, 21), ms(2026, 9, 28), "utc")
    assert just_before["pwh"] == h[wk_old].max()          # Sunday 09-27 23:59:59.999: the week of 09-21 is running
    assert at["pwh"] == h[wk_new].max() and at["pwl"] == lo[wk_new].min()     # Monday 00:00: it is the previous week
    first = ms(2026, 9, 1)
    at1 = ctx.period_levels(ot[ot <= first], o[ot <= first], h[ot <= first], lo[ot <= first], first)
    assert "month_open" not in at1                        # its bar opens at as_of: not closed, not a completed period
    assert at1["pmh"] == h[in_range(BTC_D, ms(2026, 8, 1), first, "utc")].max()
    with_day_open = ctx.period_levels(ot[ot < first], o[ot < first], h[ot < first], lo[ot < first], first,
                                      day_open=123.5)
    assert with_day_open["month_open"] == 123.5           # the first day of the month: the day's own open stands in
    later = ctx.period_levels(ot[ot < ms(2026, 9, 20)], o[ot < ms(2026, 9, 20)], h[ot < ms(2026, 9, 20)],
                              lo[ot < ms(2026, 9, 20)], ms(2026, 9, 20), day_open=123.5)
    assert later["month_open"] == o[ot == ms(2026, 9, 1)][0]                   # a day_open never replaces a closed bar


def test_a_short_history_gives_no_period_level_hand_built_bars():
    """Hand-built (pure logic): 12 daily bars from Wed 2026-09-16 — the previous week (09-21..09-27) is covered, but
    the month (starts 09-01) and the year are not, and neither is a previous month."""
    ot = np.array([ms(2026, 9, 16) + i * DAY for i in range(12)], dtype=np.int64)
    x = np.arange(12, dtype=float) + 100
    assert ctx.period_levels(ot, x, x + 5, x - 5, ms(2026, 9, 28)) == {"pwh": 116.0, "pwl": 100.0}
    assert ctx.period_levels(ot, x, x + 5, x - 5, ms(2026, 9, 28), day_open=1.0) == {"pwh": 116.0, "pwl": 100.0}
    assert ctx.period_levels(ot, x, x + 5, x - 5, ms(2026, 9, 22)) == {}          # week 09-14..20 starts before the data
    assert ctx.period_levels(ot[:0], x[:0], x[:0], x[:0], ms(2026, 9, 28)) == {}


def test_reference_levels_adds_the_period_levels_with_the_day_open_stand_in():
    m = np.arange(5, dtype=np.int64) * 60_000 + ms(2026, 9, 1)                 # today's first minutes
    px = np.array([10.0, 11, 12, 13, 14])
    ot, o, h, lo, _ = BTC_D
    past = ot < ms(2026, 9, 1)
    lv = ctx.reference_levels(m, px + 1, px - 1, px, px, ms(2026, 9, 1) + 5 * 60_000,
                              daily=(ot[past], o[past], h[past], lo[past]))
    assert lv["day_open"] == 10.0 and lv["month_open"] == 10.0                # 09-01: no closed September bar yet
    assert {"pwh", "pwl", "pmh", "pml", "year_open"} <= set(lv)
    assert "pwh" not in ctx.reference_levels(m, px + 1, px - 1, px, px, ms(2026, 9, 1) + 5 * 60_000)


def test_the_builder_puts_the_six_levels_in_the_payload(env, monkeypatch):
    """SnapshotBuilder._levels reads a year of daily bars (the 1d frame analysed has 200) and rounds to the price
    precision; the 1h frame only supplies the intraday levels."""
    b = env.builder()
    prim = env.reg.primary("BTCUSDT")
    ot, o, h, lo, c = BTC_D
    past = ot < AS_OF_REAL
    fr = Frame(prim.key, Timeframe.D1, ot[past], o[past], h[past], lo[past], c[past], np.ones(int(past.sum())),
               "traded", as_of=AS_OF_REAL)
    seen = {}

    def fake_load(reader, inst, tf, bars, as_of, cal=None):
        seen["tf"], seen["bars"] = tf, bars
        return fr

    monkeypatch.setattr("tradingsystem.analysis.snapshot.load_frame", fake_load)
    h1t = np.array([ms(2026, 9, 29, 6), ms(2026, 9, 29, 7)], dtype=np.int64)
    h1 = Frame(prim.key, H1, h1t, np.array([1.0, 2]), np.array([3.0, 4]), np.array([0.5, 1]), np.array([2.0, 3]),
               np.ones(2), "traded", as_of=AS_OF_REAL)
    lv = b._levels(h1, AS_OF_REAL, "crypto", 2, prim, None)
    assert seen == {"tf": Timeframe.D1, "bars": 380}
    assert lv["pwh"] == round(float(h[in_range(BTC_D, ms(2026, 9, 21), ms(2026, 9, 28), "utc")].max()), 2)
    assert {"day_open", "week_open", "pmh", "pml", "month_open", "year_open"} <= set(lv)


# ------------------------------------------------------------------------------------------------------ forming bar
def real_frame(tf: Timeframe, n: int = 300) -> Frame:
    """A frame of ``tf`` resampled from the real BTCUSDT 1m fixture (closed bars only)."""
    with open(REAL / "btcusdt_candles_1m_3000.csv", newline="") as f:
        rows = list(csv.DictReader(f))
    t = np.array([int(r["open_time"]) for r in rows], dtype=np.int64)
    c = {k: np.array([float(r[k]) for r in rows]) for k in ("open", "high", "low", "close", "volume")}
    key = np.array([tf.floor(int(x)) for x in t], dtype=np.int64)
    starts = np.r_[0, np.nonzero(np.diff(key))[0] + 1]
    ends = np.r_[starts[1:], len(key)]
    o, h, lo, cl = c["open"][starts], np.maximum.reduceat(c["high"], starts), np.minimum.reduceat(c["low"], starts), \
        c["close"][ends - 1]
    v = np.add.reduceat(c["volume"], starts)
    s = slice(1, -1)                                                             # drop the partial first / last bar
    ot = key[starts][s][-n:]
    return Frame("binance_spot:BTCUSDT", tf, ot, o[s][-n:], h[s][-n:], lo[s][-n:], cl[s][-n:], v[s][-n:], "traded",
                 as_of=int(ot[-1]) + tf.ms + 5_000)


def with_forming(fr: Frame, **kw) -> Frame:
    """The same frame with a forming row as the ingest stores it for the bar containing ``as_of``
    (hand-built values: the bar in progress is 5 s old, updated 1 s after as_of)."""
    open_time = fr.tf.floor(fr.as_of)
    row = {"tf": fr.tf.value, "open_time": open_time, "open": float(fr.close[-1]), "high": float(fr.close[-1]) + 4.0,
           "low": float(fr.close[-1]) - 2.5, "close": float(fr.close[-1]) + 1.5, "volume": 0.75,
           "updated_ms": fr.as_of + 1_000, **kw}
    return Frame(**{**fr.__dict__, "forming": row})


@pytest.fixture(scope="module")
def frames():
    return {tf: real_frame(tf) for tf in (M5, M15, H1)}


def analyze(fr: Frame) -> dict:
    return SnapshotBuilder.__new__(SnapshotBuilder)._analyze_tf(fr, 2, 12, False)


def test_forming_is_reported_on_15m_and_5m_only_and_never_inside_recent(frames):
    for tf in (M5, M15):
        fr = with_forming(frames[tf])
        out = analyze(fr)
        f = fr.forming
        assert out["forming"] == [iso(f["open_time"]), round(f["open"], 2), round(f["high"], 2), round(f["low"], 2),
                                  round(f["close"], 2), 0.75, 5]
        assert out["forming"][0] not in [r[0] for r in out["recent"]]           # the bar in progress is not a candle
        assert out["recent"][-1][0] == iso(int(fr.open_time[-1]))
    assert "forming" not in analyze(with_forming(frames[H1]))                   # 1h and above: no forming block


def test_forming_changes_nothing_else_in_the_timeframe_analysis(frames):
    """Indicators, structure, zones, patterns, regime and ``recent`` are bit-identical with and without it."""
    for tf in (M5, M15):
        plain = analyze(frames[tf])
        rich = analyze(with_forming(frames[tf]))
        assert "forming" in rich and "forming" not in plain
        assert {k: v for k, v in rich.items() if k != "forming"} == plain
        assert json.dumps({k: v for k, v in rich.items() if k != "forming"}, sort_keys=True) == \
            json.dumps(plain, sort_keys=True)


def test_the_trigger_inputs_and_the_setup_signature_are_identical_with_and_without_forming(frames):
    """Real payload (XAU) with a forming block added to its 15m and 5m analysis: the policy's inputs are unchanged."""
    real = json.loads((REAL / "payload_xauusd.json").read_text(encoding="utf-8"))
    plain = copy.deepcopy(real)
    rich = copy.deepcopy(real)
    for tf in ("15m", "5m"):                        # hand-built forming rows; the payload's own last close
        last = rich["timeframes"][tf]["recent"][-1]
        rich["timeframes"][tf]["forming"] = ["09-25 10:50", last[4], last[4] + 1, last[4] - 1, last[4] + 0.3, 12.0, 30]
    for tf in ("15m", "5m"):
        assert "forming" not in plain["timeframes"][tf]
    strip = copy.deepcopy(rich)
    for tf in ("15m", "5m"):
        del strip["timeframes"][tf]["forming"]
    assert strip == plain
    for screen_tf in (None, "5m"):
        a = triggers.scan_setups(plain, screen_tf=screen_tf)
        b = triggers.scan_setups(rich, screen_tf=screen_tf)
        assert a == b and triggers.setup_signature(a) == triggers.setup_signature(b)
        assert triggers.setup_reasons(plain, screen_tf=screen_tf) == triggers.setup_reasons(rich, screen_tf=screen_tf)


def test_forming_is_only_the_bar_containing_as_of_written_around_as_of(frames):
    fr = frames[M5]
    assert forming_bar(with_forming(fr))["age_s"] == 5.0
    # a live row read for a replay of a past instant is another bar: never used (no later tick leaks in)
    assert forming_bar(Frame(**{**with_forming(fr).__dict__, "as_of": fr.as_of - 10 * 300_000})) is None
    assert forming_bar(with_forming(fr, updated_ms=fr.as_of + 15_001)) is None         # holds ticks after as_of
    assert forming_bar(with_forming(fr, updated_ms=fr.as_of + 15_000)) is not None
    assert forming_bar(with_forming(fr, updated_ms=fr.as_of - 60_001)) is None         # the writer is down
    assert forming_bar(with_forming(fr, updated_ms=fr.as_of - 60_000)) is not None
    assert forming_bar(with_forming(fr, high=float("nan"))) is None
    assert forming_bar(Frame(**{**fr.__dict__, "forming": None})) is None
    assert forming_bar(Frame(**{**fr.__dict__, "forming": {"tf": "5m"}})) is None      # malformed row
    assert forming_bar(with_forming(fr, volume=None))["volume"] is None
    assert analyze(with_forming(fr, volume=None))["forming"][5] is None


def test_the_model_view_keeps_forming_out_of_recent_and_dates_it(frames):
    p = {"meta": {"as_of": iso(frames[M5].as_of)}, "timeframes": {"5m": analyze(with_forming(frames[M5]))}}
    v = model_view(p)["timeframes"]["5m"]
    assert v["forming"][0].count("-") == 1 and len(v["forming"]) == 7
    assert all(r[0] != v["forming"][0] for r in v["recent"])


# ================================================================================================ B4 / B5 / B16
# Daily profiles (B4), session statistics (B5) and the gold clock (B16) through the builder. Trades are hand-built
# (pure-logic edge cases: 24 h x 20 trades per day, the expected profile recomputed independently below); the 15m and
# 1m bars are the real ones of tests/fixtures/real. The builder's kv is a plain dict standing in for ``engine_kv``.
import threading
import time

from tradingsystem.analysis import daily_profiles as dpf
from tradingsystem.analysis import gold_clock as gclock
from tradingsystem.analysis import orderflow as of
from tradingsystem.analysis import session_stats as sstats
from tradingsystem.analysis import snapshot as snap_mod
from tradingsystem.analysis.desk_windows import desk_state_text
from tradingsystem.core.sessions import calendar_for


class KV:
    """engine_kv stand-in: JSON round trip like the real store, and a record of what was written."""

    def __init__(self) -> None:
        self.d: dict = {}
        self.sets: list[str] = []

    def kv_get(self, key, default=None):
        return json.loads(json.dumps(self.d[key])) if key in self.d else default

    def kv_set(self, key, value) -> None:
        self.d[key] = json.loads(json.dumps(value, default=str))
        self.sets.append(key)


def seed_table(env, pair: str, datatype: str, rows, tf: Timeframe | None = None) -> None:
    inst = env.reg.primary(pair)
    with SQLiteHotStore(inst.hot_db_path(env.s.paths.data())) as st:
        st.ensure_tables([*table_specs(inst), *system_specs()])
        st.upsert(spec_for(inst, datatype, tf) if tf else spec_for(inst, datatype), list(rows))


def day_rows(day: dt.date, first_id: int) -> list[tuple]:
    """Hand-built aggTrades of one UTC day: 20 trades in each of the 24 hours (agg_id, ts, price, qty, first_id, last_id,
    is_buyer_maker); prices on a 0.5 grid over nine 10-dollar buckets, one hour with heavy quantities."""
    base = ms(day.year, day.month, day.day)
    rows = []
    for hour in range(24):
        for i in range(20):
            price = 80000.0 + 10.0 * ((hour * 7 + i * 3 + day.day) % 9) + 0.5 * (i % 10)
            qty = 0.05 * (1 + i % 4) + (0.4 if hour == 13 and i % 3 == 0 else 0.0)
            rows.append((first_id + len(rows), base + hour * H + i * MIN, price, round(qty, 4), 0, 0, i % 2))
    return rows


def expected_profile(rows: list[tuple], bucket: float = 10.0) -> dict:
    vol: dict[float, float] = {}
    for _id, _ts, price, qty, *_ in rows:
        k = float(np.floor(price / bucket) * bucket)
        vol[k] = vol.get(k, 0.0) + qty
    lv = np.array(sorted(vol))
    va = of.value_area(lv, np.array([vol[x] for x in lv]))
    return {"poc": va["poc"], "vah": va["vah"], "val": va["val"]}


def btc_reader(env):
    from tradingsystem.storage.reader import InstrumentReader
    inst = env.reg.primary("BTCUSDT")
    return InstrumentReader(inst, env.s.paths.data()), inst


def test_profile_agg_trades_is_the_bucketed_value_area_of_the_day_hot_and_cold_in_any_chunking(env, monkeypatch):
    day = dt.date(2026, 9, 28)
    rows = day_rows(day, 1)
    # trades just outside the day must not enter: the last second of the day before, the first one of the day after
    edge = [(9_000_001, ms(2026, 9, 27, 23, 59, 59), 91000.0, 500.0, 0, 0, 0),
            (9_000_002, ms(2026, 9, 29), 71000.0, 500.0, 0, 0, 1)]
    seed_table(env, "BTCUSDT", "agg_trades", rows + edge)
    rd, inst = btc_reader(env)
    want = expected_profile(rows)
    got = dpf.profile_agg_trades(rd, inst, day, 10.0)
    assert {k: got[k] for k in ("poc", "vah", "val")} == want
    assert got["hours"] == 24 and got["trades"] == len(rows)
    monkeypatch.setattr(dpf, "HOT_SLICE_MS", 7 * MIN)                              # a different slicing, same day
    assert {k: dpf.profile_agg_trades(rd, inst, day, 10.0)[k] for k in ("poc", "vah", "val")} == want
    # the same kind of trades as a cold Parquet day file (a day older than storage.hot_days), read row group by row group
    import pyarrow as pa
    cold_day = dt.date(2026, 9, 20)
    crows = day_rows(cold_day, 100_000)
    spec = spec_for(inst, "agg_trades")
    rd.cold.write_day(inst, spec, cold_day, pa.table({c: [r[i] for r in crows] for i, c in enumerate(spec.column_names)}))
    assert rd.cold.day_path(inst, spec, cold_day).exists()
    assert len(rd.hot.read_range(spec, ms(2026, 9, 20), ms(2026, 9, 21))["ts"]) == 0        # not in the hot store
    monkeypatch.setattr(dpf, "CHUNK_ROWS", 37)
    cold = dpf.profile_agg_trades(rd, inst, cold_day, 10.0)
    assert {k: cold[k] for k in ("poc", "vah", "val")} == expected_profile(crows)
    rd.close()


def test_a_day_with_trades_in_fewer_than_20_hours_is_not_a_profile(env):
    day = dt.date(2026, 9, 28)
    rows = [r for r in day_rows(day, 1) if (r[1] - ms(2026, 9, 28)) // H < 19]        # 19 hours of 24
    seed_table(env, "BTCUSDT", "agg_trades", rows)
    rd, inst = btc_reader(env)
    assert dpf.profile_agg_trades(rd, inst, day, 10.0) is None
    assert dpf.profile_agg_trades(rd, inst, dt.date(2026, 9, 15), 10.0) is None        # no rows at all
    rd.close()


AS_OF_B4 = ms(2026, 9, 29, 12)


def profile_days(env, days: list[int]) -> dict[int, list[tuple]]:
    out = {d: day_rows(dt.date(2026, 9, d), 1 + n * 1000) for n, d in enumerate(days)}
    seed_table(env, "BTCUSDT", "agg_trades", [r for rs in out.values() for r in rs])
    return out


def test_the_profile_block_is_pending_while_the_worker_runs_then_filled_and_cached_per_day_in_kv(env, monkeypatch):
    days = profile_days(env, [20, 21, 22, 23, 24, 25, 27, 28])                      # 09-26 has no trades: a gap
    kv = KV()
    b = env.builder()
    b.kv = kv
    gate = threading.Event()
    real = dpf.profile_agg_trades
    calls = []

    def slow(reader, inst, day, bucket):
        gate.wait(20)
        calls.append(day)
        return real(reader, inst, day, bucket)

    monkeypatch.setattr(dpf, "profile_agg_trades", slow)
    t0 = time.perf_counter()
    first = b.build("BTCUSDT", AS_OF_B4)["orderflow"]["daily_profiles"]
    assert time.perf_counter() - t0 < 5 and b._profiles.running()                   # the screen path never waited
    assert first == {"data_quality": "pending", "bucket": 10.0, "pending": 5}
    gate.set()
    b.wait_background()
    done = b.build("BTCUSDT", AS_OF_B4)["orderflow"]["daily_profiles"]
    assert calls == [dt.date(2026, 9, d) for d in (28, 27, 26, 25, 24, 23)]         # newest first, stops at 5 profiles
    assert done["data_quality"] == "real" and "pending" not in done and done["columns"] == dpf.COLUMNS
    assert [r[0] for r in done["days"]] == [f"2026-09-{d}" for d in (28, 27, 25, 24, 23)]      # the empty 09-26 skipped
    for r in done["days"]:
        assert dict(zip(("poc", "vah", "val"), r[1:])) == expected_profile(days[int(r[0][-2:])])
    assert set(kv.sets) == {f"dprofile1:BTCUSDT:2026-09-{d}" for d in (28, 27, 26, 25, 24, 23)}
    assert kv.d["dprofile1:BTCUSDT:2026-09-26"]["miss"] is True                      # remembered, not retried per screen
    json.dumps(done)
    # a restart: a new builder over the same kv reads the days back and computes nothing
    monkeypatch.setattr(dpf, "profile_agg_trades", lambda *a: pytest.fail("recomputed a cached day"))
    b2 = env.builder()
    b2.kv = kv
    assert b2.build("BTCUSDT", AS_OF_B4)["orderflow"]["daily_profiles"] == done and not b2._profiles.running()
    # the next UTC day: 09-29 has no trades (a miss); meanwhile the block keeps the older five and says pending 1
    monkeypatch.setattr(dpf, "profile_agg_trades", real)
    nxt = ms(2026, 9, 30, 12)
    stale = b2.build("BTCUSDT", nxt)["orderflow"]["daily_profiles"]
    assert stale["pending"] == 1 and [r[0] for r in stale["days"]] == [r[0] for r in done["days"]]
    b2.wait_background()
    settled = b2.build("BTCUSDT", nxt)["orderflow"]["daily_profiles"]
    assert "pending" not in settled and settled["days"] == done["days"]


def test_a_miss_is_retried_after_six_hours_and_a_day_is_complete_only_ten_minutes_after_it_ends():
    assert dpf.candidate_days(ms(2026, 9, 29, 0, 9))[0] == dt.date(2026, 9, 27)       # 09-28 ends at 00:00: grace
    assert dpf.candidate_days(ms(2026, 9, 29, 0, 10))[0] == dt.date(2026, 9, 28)
    assert len(dpf.candidate_days(AS_OF_B4)) == 10
    now = [ms(2026, 9, 29, 12)]
    kv = KV()
    kv.kv_set("dprofile1:BTCUSDT:2026-09-20", {"miss": True, "at": now[0]})
    c = dpf.DailyProfiles(Path("."), 4, kv, now=lambda: now[0])
    assert c._state("BTCUSDT", dt.date(2026, 9, 20))[0] == "miss"
    now[0] += 6 * H + 1
    assert c._state("BTCUSDT", dt.date(2026, 9, 20))[0] == "unknown"


def test_the_profile_worker_never_raises_a_failing_day_is_a_logged_miss(env, monkeypatch):
    profile_days(env, [27, 28])
    kv = KV()
    b = env.builder()
    b.kv = kv

    def boom(*a):
        raise OSError("disk gone")

    monkeypatch.setattr(dpf, "profile_agg_trades", boom)
    b.build("BTCUSDT", AS_OF_B4)
    b.wait_background()
    p = b.build("BTCUSDT", AS_OF_B4)["orderflow"]["daily_profiles"]
    assert p["data_quality"] == "pending" and "days" not in p                       # honest: nothing known
    assert kv.d["dprofile1:BTCUSDT:2026-09-28"]["miss"] is True


def load_1m(name: str):
    with open(REAL / name, newline="") as f:
        rows = list(csv.DictReader(f))
    return [(int(r["open_time"]), *(float(r[k]) for k in ("open", "high", "low", "close", "tick_volume")))
            for r in rows]


def test_xau_profile_comes_from_the_1m_tick_volume_and_is_flagged_approx(env):
    bars = load_1m("xauusd_1m_2026-09-24.csv")                                     # a real Thursday of XAUUSD@
    assert len(bars) >= dpf.MIN_BARS_1M
    seed_table(env, "XAUUSD", "candles",
               [(t, t, o, h, lo, c, int(v), 0, None) for t, o, h, lo, c, v in bars], Timeframe.parse("1m"))
    kv = KV()
    b = env.builder()
    b.kv = kv
    as_of = ms(2026, 9, 25, 12)
    assert b.build("XAUUSD", as_of)["orderflow"]["daily_profiles"]["data_quality"] == "pending"
    b.wait_background()
    blk = b.build("XAUUSD", as_of)["orderflow"]["daily_profiles"]
    assert blk["data_quality"] == "approx" and blk["approx"] is True and "pending" not in blk
    assert [r[0] for r in blk["days"]] == ["2026-09-24"]                             # the other days have no bars
    arr = np.array([b_[1:] for b_ in bars])
    ref = of.tick_volume_profile(arr[:, 1], arr[:, 2], arr[:, 4], 0.5)
    assert blk["days"][0][1:] == [round(ref["poc"], 2), round(ref["vah"], 2), round(ref["val"], 2)]
    assert arr[:, 2].min() <= ref["val"] <= ref["poc"] <= ref["vah"] <= arr[:, 1].max()
    assert kv.d["dprofile1:XAUUSD:2026-09-24"]["bars"] == len(bars)
    assert kv.d["dprofile1:XAUUSD:2026-09-23"]["miss"] is True


def test_the_model_view_shortens_the_profile_dates_and_drops_the_column_names():
    blk = {"data_quality": "real", "bucket": 10.0, "columns": dpf.COLUMNS, "pending": 1,
           "days": [["2026-09-28", 83000.0, 83600.0, 82820.0], ["2026-09-27", 84770.0, 84850.0, 84380.0]]}
    v = model_view({"meta": {"as_of": "2026-09-29T07:25:00.000Z"}, "orderflow": {"daily_profiles": blk}})
    out = v["orderflow"]["daily_profiles"]
    assert "columns" not in out and out["days"][0] == ["09-28", 83000.0, 83600.0, 82820.0] and out["pending"] == 1
    assert len(json.dumps(out, separators=(",", ":"))) / 4 <= 200                    # the B4 budget, chars / 4
    other = model_view({"meta": {"as_of": "2027-01-02T00:00:00.000Z"}, "orderflow": {"daily_profiles": blk}})
    assert other["orderflow"]["daily_profiles"]["days"][0][0] == "2026-09-28"       # another year stays in full
    assert blk["columns"] == dpf.COLUMNS and blk["days"][0][0] == "2026-09-28"       # the payload itself is untouched


# ------------------------------------------------------------------------------- B5 / B16 through the builder
def bars15(name: str) -> list[tuple]:
    with open(REAL / name, newline="") as f:
        rows = list(csv.DictReader(f))
    return [(int(r["open_time"]), *(float(r[k]) for k in ("open", "high", "low", "close"))) for r in rows]


BARS15 = {"BTCUSDT": bars15("btcusdt_15m_3500.csv"), "XAUUSD": bars15("xauusd_15m_9300.csv")}
DAY0_REAL = AS_OF_REAL - AS_OF_REAL % DAY


def seed_15m(env, pair: str, extra: list[tuple] | None = None) -> None:
    rows = BARS15[pair] + (extra or [])
    if pair == "XAUUSD":
        rows = [(t, t, o, h, lo, c, 100, 0, None) for t, o, h, lo, c in rows]
    else:
        rows = [(t, o, h, lo, c, 1.0, 1.0, 1, 0.5, 0.5) for t, o, h, lo, c in rows]
    seed_table(env, pair, "candles", rows, M15)


HISTORY_BARS = {"BTCUSDT": 30 * 96 + 40, "XAUUSD": 90 * 96 + 40}     # the frame the builder reads (the ATR warm-up matters)


def arrays15(pair: str):
    """The bars the builder's history frame holds at DAY0_REAL: the last N closed before that UTC day's start."""
    a = np.array([r for r in BARS15[pair] if r[0] + M15.ms <= DAY0_REAL][-HISTORY_BARS[pair]:])
    return a[:, 0].astype(np.int64), a[:, 1], a[:, 2], a[:, 3], a[:, 4]


def tokens(obj) -> float:
    return len(json.dumps(obj, separators=(",", ":"), ensure_ascii=False)) / 4


def test_the_builder_adds_session_stats_for_every_pair_and_the_gold_blocks_only_for_a_desk_pair(env):
    seed_15m(env, "BTCUSDT")
    seed_15m(env, "XAUUSD")
    b = env.builder()
    btc = b.build("BTCUSDT", AS_OF_REAL)
    ot, o, h, lo, c = arrays15("BTCUSDT")
    st = btc["market"]["session_stats"]
    assert st["sessions"] == sstats.session_history(ot, o, h, lo, c, M15.ms, DAY0_REAL)
    assert set(st["now"]) == {"asia", "london"} and "gold_clock" not in btc["market"] and "round" not in btc["levels"]
    xau = b.build("XAUUSD", AS_OF_REAL)
    gc_ = xau["market"]["gold_clock"]
    ot, o, h, lo, c = arrays15("XAUUSD")
    hist = gclock.gold_history(ot, o, h, lo, c, M15.ms, DAY0_REAL)
    assert gc_["london_asia_sweep_days"] == hist["sweep"] and gc_["asia_range"]["complete"] is True
    assert gc_["asia_range"]["width_x_median"] == round(gc_["asia_range"]["width"] / hist["asia_width_median"], 2)
    pcfg = env.s.pairs["XAUUSD"]
    execu = env.reg.with_role("XAUUSD", "execution")[0]
    cal = calendar_for(execu.venue, execu.symbol, pcfg.asset_class)
    # the window state is B15's own function, not a copy: 07:25 UTC is 08:25 in London (BST), inside 07:45-11:00;
    # without a news calendar file the desk makes no entry call (B8, D-049) and the payload says so
    assert gc_["desk_window"] == desk_state_text(pcfg.desk, AS_OF_REAL, cal, "XAUUSD", "news_stale")
    assert gc_["desk_window"] == "closed:news_stale" and xau["market"]["news"]["data_quality"] == "unavailable"
    assert xau["capabilities"]["news_calendar"]["quality"] == "unavailable" and "news" not in btc["market"]
    from tradingsystem.analysis import news as newsmod                  # the real weekly file, fetched 1 h before
    raw = (REAL / "ff_calendar_thisweek_2026-09-29.json").read_bytes()
    opener = lambda req, timeout: __import__("io").BytesIO(raw)       # noqa: E731
    newsmod.fetch(pcfg.news_blackout, newsmod.calendar_path(env.s.paths.shared()), AS_OF_REAL - 3_600_000,
                  opener=opener)
    xau = b.build("XAUUSD", AS_OF_REAL)
    assert xau["market"]["gold_clock"]["desk_window"] == desk_state_text(pcfg.desk, AS_OF_REAL, cal, "XAUUSD") == "open"
    assert xau["market"]["news"]["blackout"] == "none" and xau["capabilities"]["news_calendar"]["quality"] == "real"
    ind15 = xau["timeframes"]["15m"]["indicators"]
    px, atr = ind15["close"], ind15["atr14"]                                        # no quote stored: the last 15m close
    rnd = xau["levels"]["round"]
    assert rnd == gclock.round_levels(px, atr, 2)
    assert rnd["10"][0] < px < rnd["10"][2] and rnd["50"][0] <= rnd["10"][0] and rnd["50"][2] >= rnd["10"][2]
    json.dumps(xau)


def test_the_new_blocks_stay_inside_their_token_budgets_on_real_payloads(env):
    seed_15m(env, "XAUUSD")
    seed_15m(env, "BTCUSDT")
    b = env.builder()
    x = model_view(b.build("XAUUSD", AS_OF_REAL))
    assert tokens(x["market"]["session_stats"]) <= 150
    assert tokens({"gold_clock": x["market"]["gold_clock"], "round": x["levels"]["round"]}) <= 190
    assert "true" not in json.dumps(x["market"]["gold_clock"]) and "columns" not in x["market"]["session_stats"]
    assert tokens(model_view(b.build("BTCUSDT", AS_OF_REAL))["market"]["session_stats"]) <= 150


def test_the_session_history_is_read_once_per_utc_day_and_cached_in_kv(env, monkeypatch):
    seed_15m(env, "XAUUSD")
    kv = KV()
    b = env.builder()
    b.kv = kv
    real = snap_mod.load_frame
    reads: list[int] = []

    def counting(reader, inst, tf, bars, as_of, cal=None):
        if tf == M15 and bars > 3000:
            reads.append(as_of)
        return real(reader, inst, tf, bars, as_of, cal)

    monkeypatch.setattr(snap_mod, "load_frame", counting)
    b.build("XAUUSD", AS_OF_REAL)
    b.build("XAUUSD", AS_OF_REAL + 5 * MIN)
    assert reads == [DAY0_REAL]                                                     # the history is cut at the day start
    assert {k for k in kv.d if k.startswith("sess1")} == {"sess1:XAUUSD:2026-09-29"}
    assert kv.d["sess1:XAUUSD:2026-09-29"]["gold"]["sweep"]
    b2 = env.builder()                                                              # a restart: read back from the kv
    b2.kv = kv
    b2.build("XAUUSD", AS_OF_REAL)
    assert reads == [DAY0_REAL]
    b2.build("XAUUSD", DAY0_REAL + DAY + 8 * H)                                     # the next UTC day: a new key
    assert reads == [DAY0_REAL, DAY0_REAL + DAY] and "sess1:XAUUSD:2026-09-30" in kv.d
    b3 = env.builder()                                                              # a BTC-style (non-desk) history: 30 d
    seed_15m(env, "BTCUSDT")
    b3.kv = kv
    b3.build("BTCUSDT", AS_OF_REAL)
    assert "gold" not in kv.d["sess1:BTCUSDT:2026-09-29"] and "stats" in kv.d["sess1:BTCUSDT:2026-09-29"]


def test_the_session_and_gold_blocks_never_see_bars_after_as_of(env):
    seed_15m(env, "XAUUSD")
    clean = env.builder().build("XAUUSD", AS_OF_REAL)
    # poisoned bars from the cycle instant on (the 07:15 bar is the one forming at 07:25, the rest lies in the future)
    poison = [(AS_OF_REAL - 600_000 + k * M15.ms, 9e3, 9e4, 1.0, 9e3) for k in range(8)]
    seed_15m(env, "XAUUSD", poison)
    dirty = env.builder().build("XAUUSD", AS_OF_REAL)
    assert dirty["market"]["gold_clock"] == clean["market"]["gold_clock"]
    assert dirty["market"]["session_stats"] == clean["market"]["session_stats"]
    assert dirty["levels"]["round"] == clean["levels"]["round"]


def test_a_failure_in_the_advice_blocks_leaves_them_out_and_never_breaks_the_payload(env, monkeypatch):
    seed_15m(env, "XAUUSD")
    b = env.builder()

    def boom(*a, **k):
        raise RuntimeError("bad bars")

    monkeypatch.setattr(snap_mod, "session_history", boom)
    p = b.build("XAUUSD", AS_OF_REAL)
    assert "session_stats" not in p["market"] and "gold_clock" not in p["market"] and p["meta"]["payload_hash"]


def test_too_little_history_says_so_instead_of_inventing_statistics(env):
    seed_table(env, "BTCUSDT", "candles",
               [(t, o, h, lo, c, 1.0, 1.0, 1, 0.5, 0.5) for t, o, h, lo, c in BARS15["BTCUSDT"][-400:]], M15)
    p = env.builder().build("BTCUSDT", AS_OF_REAL)
    st = p["market"]["session_stats"]
    assert "sessions" in st                                   # 400 bars = 4 days: a few complete sessions exist ...
    assert all(v[0] <= 5 for v in st["sessions"].values())      # ... with their true (small) day counts
    p2 = env.builder().build("BTCUSDT", ms(2026, 1, 1))
    assert p2["market"]["session_stats"]["data_quality"] == "unavailable"


def test_the_engine_hands_its_decision_store_to_the_builder(tmp_path):
    from tradingsystem.analysis.engine import Engine
    s = load_settings(env_path=Path("nope.env"))
    s = s.model_copy(update={"paths": s.paths.model_copy(update={"data_dir": str(tmp_path)})})
    e = Engine(s)
    try:
        assert e.builder.kv is e.store
        e.store.kv_set("sess1:X:2026-01-01", {"stats": {}})
        assert e.builder.kv.kv_get("sess1:X:2026-01-01") == {"stats": {}}
    finally:
        e.close()


def test_the_model_view_renders_the_session_and_gold_blocks_compactly():
    market = {"session_stats": {"days": 30, "columns": ["days", "range_atr", "up_pct"], "now_columns": ["a"],
                                "sessions": {"london": [30, 10.19, 60]}, "now": {"london": [1.03, 0.1, 41]}},
              "gold_clock": {"session": "london", "asia_range": {"complete": True}, "london_swept": {"side": "high",
                             "back_inside": False}, "next": [["lbma_am", "2026-09-29T09:30:00.000Z", 125]]},
              "note": "dropped"}
    v = model_view({"meta": {"as_of": "2026-09-29T07:25:00.000Z"}, "market": market})["market"]
    assert v["session_stats"] == {"sessions": {"london": [30, 10.19, 60]}, "now": {"london": [1.03, 0.1, 41]}}
    assert v["gold_clock"]["asia_range"] == {"complete": 1} and v["gold_clock"]["london_swept"]["back_inside"] == 0
    assert v["gold_clock"]["next"] == [["lbma_am", "09-29 09:30", 125]] and "note" not in v
    assert market["session_stats"]["columns"] and market["gold_clock"]["asia_range"]["complete"] is True


def test_the_legend_explains_every_new_field_in_place_without_a_version_bump():
    text = (Path(__file__).resolve().parents[1].parent / "src" / "tradingsystem" / "ai" / "prompts" / "shared"
            / "payload_legend.md").read_text(encoding="utf-8")
    assert text.startswith("<!-- prompt: shared/payload_legend · version 5 -->")
    for name in ("orderflow.daily_profiles", "market.session_stats", "market.gold_clock", "london_swept",
                 "london_asia_sweep_days", "levels.round", "desk_window", "width_x_median", "pending"):
        assert name in text, name
    assert "$" not in text                                    # a literal dollar sign breaks the template (test_prompts)
    assert snap_mod.PAYLOAD_VERSION == "3"                    # the lead bumps it at B7, not here
