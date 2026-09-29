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
