"""Derivatives block (Phase 5 A6, §3.9 item 2) on real rows of the production hot DBs (tests/fixtures/real, see
PROVENANCE.md): OI changes from the 5-min metrics' ``sum_open_interest`` (the 60-s table only for ``last``), causal
30-day percentile ranks of OI and funding, positioning ratios as the newest NON-null value per column with its own
time. The gaps are real (live rows without the taker ratio since 2026-09-25, a row without OI) or seeded by deleting
real rows in the test. Every expected number is computed here from the fixture rows, independently of the module."""
import csv
import gzip
import json
from pathlib import Path

import pytest

from tradingsystem.analysis.registry import Capability, capability_matrix
from tradingsystem.analysis.snapshot import SnapshotBuilder
from tradingsystem.core.instruments import InstrumentRegistry
from tradingsystem.core.settings import PathsCfg, load_settings
from tradingsystem.core.timeutil import iso
from tradingsystem.storage.sqlite_store import SQLiteHotStore
from tradingsystem.storage.tablespec import spec_for, system_specs, table_specs

REAL = Path(__file__).resolve().parents[1] / "fixtures" / "real"
MIN, H, DAY = 60_000, 3_600_000, 86_400_000
T0720 = 1790580000000                      # 2026-09-28 07:20 UTC: the newest metrics row of both fixtures
AS_OF = T0720 + 3 * MIN                    # 07:23 — a screen three minutes after the newest row


def ms(hh: int, mm: int, ss: int = 0) -> int:
    """2026-09-28 hh:mm:ss UTC."""
    return T0720 + ((hh - 7) * 60 + (mm - 20)) * MIN + ss * 1000


def load(name: str) -> list[tuple]:
    opener = gzip.open if name.endswith(".gz") else open
    with opener(REAL / name, "rt", newline="") as f:
        r = csv.reader(f)
        next(r)
        return [tuple(int(v) if i == 0 else (float(v) if v != "" else None) for i, v in enumerate(row)) for row in r]


ETH_METRICS = load("ethusdt_metrics_30d.csv.gz")
ETH_FUNDING = load("ethusdt_funding_31d.csv")
ETH_OI_60S = load("ethusdt_open_interest_1h.csv")
XAU_METRICS = load("xauusdt_metrics_26h.csv")
XAU_FUNDING = load("xauusdt_funding_3d.csv")
OI, TOP_ACC, TOP_POS, GLOB, TAKER = 1, 3, 4, 5, 6        # column positions in a metrics row


@pytest.fixture
def env(tmp_path):
    s = load_settings(env_path=Path("nope.env"))
    s = s.model_copy(update={"paths": PathsCfg(data_dir=str(tmp_path / "data"), logs_dir=str(tmp_path / "logs"))})
    reg = InstrumentRegistry.from_settings(s)
    builders: list[SnapshotBuilder] = []

    def seed(symbol: str, metrics=(), funding=(), oi_60s=()) -> None:
        inst = reg.get(f"binance_usdm:{symbol}")
        with SQLiteHotStore(inst.hot_db_path(s.paths.data())) as st:      # laid out as the ingester does
            st.ensure_tables([*table_specs(inst), *system_specs()])
            for dt_, rows in (("metrics", metrics), ("funding", funding), ("open_interest", oi_60s)):
                if rows:
                    st.upsert(spec_for(inst, dt_), list(rows))

    def derivatives(pair: str, as_of: int, caps=None) -> dict:
        b = SnapshotBuilder(s, reg)
        builders.append(b)
        return b._derivatives(pair, caps or capability_matrix(s, reg, pair), as_of, s.pairs[pair].price_decimals)

    yield type("Env", (), {"s": s, "reg": reg, "seed": staticmethod(seed), "derivatives": staticmethod(derivatives)})
    for b in builders:
        b.close()


def by_ts(rows) -> dict[int, tuple]:
    return {r[0]: r for r in rows}


def pct(new: float, old: float) -> float:
    return round((new / old - 1) * 100, 3)


def eth(env, metrics=ETH_METRICS, oi_60s=ETH_OI_60S, as_of=AS_OF) -> dict:
    env.seed("ETHUSDT", metrics, ETH_FUNDING, oi_60s)
    return env.derivatives("ETHUSDT", as_of)


# ------------------------------------------------------------------------------------------------ open interest
def test_oi_changes_come_from_the_metrics_one_four_and_24_hours_before_the_newest_row(env):
    m = by_ts(ETH_METRICS)
    oi = eth(env)["open_interest"]
    assert oi["change_pct_1h"] == pct(m[T0720][OI], m[T0720 - H][OI])
    assert oi["change_pct_4h"] == pct(m[T0720][OI], m[T0720 - 4 * H][OI])
    assert oi["change_pct_24h"] == pct(m[T0720][OI], m[T0720 - DAY][OI])
    assert None not in (oi["change_pct_1h"], oi["change_pct_4h"], oi["change_pct_24h"])   # production: 4h/24h null


def test_oi_last_is_the_fresh_60s_reading_else_the_newest_metrics_row(env):
    live = [r for r in ETH_OI_60S if r[0] < AS_OF][-1]
    oi = eth(env)["open_interest"]
    assert oi["last"] == round(live[1], 3) and oi["time"] == iso(live[0])
    m = by_ts(ETH_METRICS)
    oi = env.derivatives("ETHUSDT", AS_OF + 13 * MIN)["open_interest"]      # the 60-s rows end 07:24:51: stale at 07:36
    assert oi["last"] == round(m[T0720][OI], 3) and oi["time"] == iso(T0720)


def test_oi_changes_use_the_nearest_row_within_ten_minutes_of_a_seeded_gap_else_none(env):
    m = by_ts(ETH_METRICS)
    gap = {ms(6, 15), ms(6, 20)}                                             # the 1-h reference row is missing
    oi = eth(env, [r for r in ETH_METRICS if r[0] not in gap], oi_60s=())["open_interest"]
    assert oi["change_pct_1h"] == pct(m[T0720][OI], m[ms(6, 25)][OI])       # 5 min away beats 06:10 (10 min)
    assert oi["change_pct_4h"] == pct(m[T0720][OI], m[T0720 - 4 * H][OI])


def test_oi_change_is_none_when_no_row_lies_within_ten_minutes(env):
    m = by_ts(ETH_METRICS)
    gap = {ms(6, 5) + i * 5 * MIN for i in range(7)}                         # 06:05..06:35: nearest rows 20 min away
    oi = eth(env, [r for r in ETH_METRICS if r[0] not in gap], oi_60s=())["open_interest"]
    assert oi["change_pct_1h"] is None
    assert oi["change_pct_24h"] == pct(m[T0720][OI], m[T0720 - DAY][OI])


def test_stale_metrics_give_no_oi_changes_and_no_oi_rank(env):
    eth(env, oi_60s=())
    fresh = env.derivatives("ETHUSDT", T0720 + 10 * MIN)                   # exactly 10 min old: still fresh
    assert fresh["open_interest"]["change_pct_1h"] is not None and fresh["oi_pct_rank_30d"] is not None
    stale = env.derivatives("ETHUSDT", T0720 + 11 * MIN)
    assert [stale["open_interest"][f"change_pct_{h}h"] for h in (1, 4, 24)] == [None, None, None]
    assert stale["oi_pct_rank_30d"] is None
    assert stale["open_interest"]["time"] == iso(T0720)                     # the reading is still shown, dated


def test_the_newest_oi_row_without_a_value_is_skipped_not_shown(env):
    """The real 05:10 row has the taker ratio but no OI (the live poll stored it from one endpoint)."""
    m = by_ts(ETH_METRICS)
    assert m[ms(5, 10)][OI] is None
    oi = eth(env, oi_60s=(), as_of=ms(5, 12))["open_interest"]
    assert oi["time"] == iso(ms(5, 5)) and oi["last"] == round(m[ms(5, 5)][OI], 3)
    assert oi["change_pct_1h"] == pct(m[ms(5, 5)][OI], m[ms(4, 5)][OI])


# ------------------------------------------------------------------------------------------------ causality
@pytest.mark.parametrize("as_of", [ms(6, 0), ms(5, 27, 30), AS_OF])
def test_derivatives_never_use_a_row_stamped_at_or_after_as_of(env, tmp_path, as_of):
    """The block built on the full tables equals the block built on tables cut at ``as_of`` (rows ≥ as_of deleted)."""
    full = eth(env, as_of=as_of)
    s2 = env.s.model_copy(update={"paths": PathsCfg(data_dir=str(tmp_path / "cut"), logs_dir=str(tmp_path / "logs"))})
    inst = env.reg.get("binance_usdm:ETHUSDT")
    with SQLiteHotStore(inst.hot_db_path(s2.paths.data())) as st:
        st.ensure_tables([*table_specs(inst), *system_specs()])
        for dt_, rows in (("metrics", ETH_METRICS), ("funding", ETH_FUNDING), ("open_interest", ETH_OI_60S)):
            st.upsert(spec_for(inst, dt_), [r for r in rows if r[0] < as_of])
    b = SnapshotBuilder(s2, env.reg)
    try:
        cut = b._derivatives("ETHUSDT", capability_matrix(s2, env.reg, "ETHUSDT"), as_of, 2)
    finally:
        b.close()
    assert full == cut
    stamps = [full["open_interest"]["time"], full["positioning"]["time"],
              *full["positioning"].get("value_times", {}).values(), *(f["time"] for f in full["funding"])]
    assert all(t < iso(as_of) for t in stamps)


# ------------------------------------------------------------------------------------------------ positioning
def test_positioning_is_the_newest_non_null_value_per_column_with_its_own_time(env):
    """At 07:23 every live row since 05:30 lacks the taker ratio: the old block showed null, the new one the last
    real value (05:25) and says it is older than the other ratios (07:20)."""
    m = by_ts(ETH_METRICS)
    assert all(m[ms(5, 30) + i * 5 * MIN][TAKER] is None for i in range(23))
    p = eth(env)["positioning"]
    assert p["time"] == iso(T0720)
    assert (p["top_trader_account_ls"], p["top_trader_position_ls"], p["global_account_ls"]) == \
        (round(m[T0720][TOP_ACC], 3), round(m[T0720][TOP_POS], 3), round(m[T0720][GLOB], 3))
    assert p["taker_buy_sell_vol_ratio"] == round(m[ms(5, 25)][TAKER], 3) == 0.905
    assert p["value_times"] == {"taker_buy_sell_vol_ratio": iso(ms(5, 25))}


def test_taker_ratio_counts_as_known_only_when_its_five_minute_bucket_closes(env):
    m = by_ts(ETH_METRICS)
    before = eth(env, as_of=ms(5, 30))                    # the 05:25 bucket closes at 05:30: not yet known
    assert before["positioning"]["taker_buy_sell_vol_ratio"] == round(m[ms(5, 20)][TAKER], 3)
    assert before["positioning"]["value_times"] == {"taker_buy_sell_vol_ratio": iso(ms(5, 20))}
    after = env.derivatives("ETHUSDT", ms(5, 30) + 1)
    assert after["positioning"]["taker_buy_sell_vol_ratio"] == round(m[ms(5, 25)][TAKER], 3)


def test_a_ratio_older_than_six_hours_is_left_out(env):
    """Seeded: the taker ratio removed from every row since 01:00 — the newest value (00:55) is 6 h 28 min old."""
    rows = [r[:TAKER] + (None,) if r[0] >= ms(1, 0) else r for r in ETH_METRICS]
    p = eth(env, rows)["positioning"]
    assert p["taker_buy_sell_vol_ratio"] is None and "value_times" not in p
    assert p["global_account_ls"] is not None


# ------------------------------------------------------------------------------------------------ 30-day ranks
def rank(values: list[float], current: float) -> int:
    return int(round(100 * sum(v <= current for v in values) / len(values)))


def test_funding_and_oi_ranks_are_causal_30_day_percentiles(env):
    d = eth(env)
    fund = [r for r in ETH_FUNDING if AS_OF - 30 * DAY <= r[0] < AS_OF]
    assert len(fund) == 90
    assert d["funding_pct_rank_30d"] == rank([r[1] for r in fund], fund[-1][1])
    oi = [r[OI] for r in ETH_METRICS if AS_OF - 30 * DAY <= r[0] < AS_OF and r[OI] is not None]
    assert len(oi) > 8_600
    assert d["oi_pct_rank_30d"] == rank(oi, by_ts(ETH_METRICS)[T0720][OI])
    assert [f["time"] for f in d["funding"]] == [iso(r[0]) for r in fund[-3:]]          # the last 3 as before


def test_no_funding_rank_without_a_funding_row_in_the_last_three_days(env):
    d = eth(env, as_of=ms(0, 0) + 3 * DAY + MIN)                           # newest funding 09-28 00:00, 3 days + 1 min
    assert d["funding"] == [] and d["funding_pct_rank_30d"] is None


def test_a_rank_needs_half_the_30_day_window(env):
    env.seed("XAUUSDT", XAU_METRICS, XAU_FUNDING)                # 26 h of metrics and 3 days of funding only
    d = env.derivatives("XAUUSD", AS_OF)
    assert d["oi_pct_rank_30d"] is None and d["funding_pct_rank_30d"] is None
    short = [r for r in ETH_METRICS if r[0] >= AS_OF - 14 * DAY]    # seeded: history starts in the 2nd half
    assert eth(env, short)["oi_pct_rank_30d"] is None


# ------------------------------------------------------------------------------------------------ XAU proxy
def test_xau_uses_the_xauusdt_proxy_through_the_same_code(env):
    env.seed("XAUUSDT", XAU_METRICS, XAU_FUNDING)
    d = env.derivatives("XAUUSD", AS_OF)
    m = by_ts(XAU_METRICS)
    assert d["data_quality"] == "proxy" and d["source"] == "binance_usdm:XAUUSDT"
    assert d["open_interest"]["change_pct_1h"] == pct(m[T0720][OI], m[T0720 - H][OI])
    assert d["open_interest"]["change_pct_24h"] == pct(m[T0720][OI], m[T0720 - DAY][OI])
    taker = [r for r in XAU_METRICS if r[TAKER] is not None and r[0] + 5 * MIN < AS_OF][-1]
    assert d["positioning"]["taker_buy_sell_vol_ratio"] == round(taker[TAKER], 3)
    assert d["positioning"]["value_times"] == {"taker_buy_sell_vol_ratio": iso(taker[0])}
    assert [f["rate"] for f in d["funding"]] == [round(r[1], 6) for r in XAU_FUNDING[-3:]]


def test_unavailable_derivatives_stay_unavailable(env):
    cap = {"derivatives": Capability("unavailable", None, "no derivatives instrument configured")}
    assert env.derivatives("XAUUSD", AS_OF, cap) == {"data_quality": "unavailable",
                                                     "reason": "no derivatives instrument configured"}


def test_the_model_view_shows_the_taker_ratio_and_its_time(env):
    from tradingsystem.ai.model_view import model_view
    d = eth(env)
    v = model_view({"meta": {"as_of": iso(AS_OF)}, "derivatives": d})["derivatives"]
    assert v["positioning"]["taker_buy_sell_vol_ratio"] == 0.905
    assert v["positioning"]["value_times"] == {"taker_buy_sell_vol_ratio": "09-28 05:25"}
    assert json.dumps(v)                                                    # plain JSON, no numpy scalars
