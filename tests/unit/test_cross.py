"""Phase 5 B6: ``analysis/cross.py`` - the correlated sibling (BTC <-> ETH) as ``market.cross``.

The 15m bars are the REAL ones of tests/fixtures/real (Binance spot BTCUSDT and ETHUSDT, the same 3500 bars, exported
read-only from production); every expected number is recomputed here with plain numpy, independently of the module.
Bars that are removed or shifted are edge cases made in the test and say so. Nothing here needs MT5 or the network."""
import csv
import hashlib
import json
import time
from pathlib import Path

import numpy as np
import pytest

from tradingsystem.ai.model_view import model_view
from tradingsystem.analysis import cross
from tradingsystem.analysis.cross import CrossContext, corr_beta, extreme_side, log_returns, neighbours, trend_label
from tradingsystem.analysis.frames import load_frame
from tradingsystem.analysis.registry import capability_matrix
from tradingsystem.analysis.snapshot import SnapshotBuilder
from tradingsystem.core.instruments import InstrumentRegistry
from tradingsystem.core.sessions import calendar_for
from tradingsystem.core.settings import INSTANCE_ENV, load_settings
from tradingsystem.core.timeframes import Timeframe
from tradingsystem.storage.sqlite_store import SQLiteHotStore
from tradingsystem.storage.tablespec import spec_for, system_specs, table_specs

REAL = Path(__file__).resolve().parents[1] / "fixtures" / "real"
SRC = Path(__file__).resolve().parents[2] / "src" / "tradingsystem"
M15 = Timeframe.M15
MIN, H = 60_000, 3_600_000


def load(name: str) -> list[tuple]:
    with open(REAL / name, newline="") as f:
        return [(int(r["open_time"]), *(float(r[k]) for k in ("open", "high", "low", "close")))
                for r in csv.DictReader(f)]


BTC, ETH = load("btcusdt_15m_3500.csv"), load("ethusdt_15m_3500.csv")
assert [r[0] for r in BTC] == [r[0] for r in ETH]                    # the same 3500 grid slots
END = BTC[-1][0] + M15.ms                                            # every fixture bar is closed here (2026-09-29 07:15 UTC)
MID = 1790604000000                                                  # 2026-09-28 14:00 UTC: London and New York sessions in progress


def binance_rows(bars):
    return [(t, o, h, lo, c, 1.0, 1.0, 1, 0.5, 0.5) for t, o, h, lo, c in bars]


@pytest.fixture
def env(tmp_path):
    """Settings of the BTC system (D-042: only BTCUSDT enabled, ETHUSDT is configured but not in the registry)."""
    s = load_settings(env_path=Path("nope.env"), extra_env={INSTANCE_ENV: "BTCUSDT"})
    s = s.model_copy(update={"paths": s.paths.model_copy(update={"data_dir": str(tmp_path / "data")})})
    reg = InstrumentRegistry.from_settings(s)
    made = []

    def seed(inst, bars):
        with SQLiteHotStore(inst.hot_db_path(s.paths.data())) as st:
            st.ensure_tables([*table_specs(inst), *system_specs()])
            st.upsert(spec_for(inst, "candles", M15), binance_rows(bars))

    eth = cross.sibling_instrument(s, "ETHUSDT")

    def reseed(bars):
        """Replace the sibling's DB (Windows cannot delete an open file: the readers are closed first)."""
        for c in made:
            c.close()
        for p in eth.hot_db_path(s.paths.data()).parent.glob("ETHUSDT*"):
            p.unlink()
        if bars is not None:
            seed(eth, bars)

    def context() -> CrossContext:
        made.append(CrossContext(s, reg, s.paths.data()))
        return made[-1]

    def frame(as_of):
        prim = reg.primary("BTCUSDT")
        rd = SnapshotBuilder(s, reg).reader(prim)
        return load_frame(rd, prim, M15, 320, as_of, calendar_for(prim.venue, prim.symbol, "crypto"))

    yield type("Env", (), {"s": s, "reg": reg, "btc": reg.primary("BTCUSDT"), "eth": eth, "seed": staticmethod(seed),
                           "context": staticmethod(context), "reseed": staticmethod(reseed), "frame": staticmethod(frame), "root": s.paths.data()})
    for c in made:
        c.close()


def files(root: Path, only: str = "ETHUSDT") -> dict[str, str]:
    """name -> sha256 of every file under ``root`` whose name has ``only`` (the sibling's; the pair's own reader, opened by
    the test to build its frame, keeps its own -wal/-shm files)."""
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.rglob("*"))
            if p.is_file() and only in p.name} if root.exists() else {}


def independent(as_of: int, drop: set[int] = frozenset()):
    """corr / beta / n of BTC vs ETH over the last 96 bars closed by as_of, plain numpy: a return exists only between
    consecutive slots that both series have."""
    bars = [b for b in BTC if b[0] + M15.ms <= as_of][-96 - 1:]
    last = bars[-1][0]
    eth = {b[0]: b[4] for b in ETH if b[0] not in drop}
    btc = {b[0]: b[4] for b in BTC}
    xs, ys = [], []
    for t in range(last - 95 * M15.ms, last + 1, M15.ms):
        if all(k in eth and k in btc for k in (t, t - M15.ms)):
            xs.append(np.log(btc[t] / btc[t - M15.ms]))
            ys.append(np.log(eth[t] / eth[t - M15.ms]))
    x, y = np.array(xs), np.array(ys)
    return float(np.corrcoef(x, y)[0, 1]), float(np.cov(x, y, ddof=0)[0, 1] / y.var()), len(x)


# ------------------------------------------------------------------------------------------------ neighbours
def test_the_sibling_comes_from_settings_even_when_the_pair_is_not_enabled_in_this_system(env):
    assert env.reg.pairs() == ["BTCUSDT"]                                  # a per-pair system: ETH is not in the registry
    nb = neighbours(env.s, env.reg, "BTCUSDT")
    assert [(n.label, n.kind, n.inst.key) for n in nb] == [("ETHUSDT", "sibling", "binance_spot:ETHUSDT")]
    assert env.eth.hot_db_path(env.root) == env.root / "hot" / "binance_spot" / "ETHUSDT.db"
    assert neighbours(env.s, env.reg, "ETHUSDT")[0].label == "BTCUSDT"     # symmetric


def test_a_pair_outside_every_correlated_group_has_no_sibling(env):
    s = env.s.model_copy(update={"risk": env.s.risk.model_copy(update={"correlated_groups": [["BTCUSDT", "ETHUSDT"]]})})
    assert neighbours(s, env.reg, "XAUUSD") == []                              # not in a group, no context in this registry
    s2 = env.s.model_copy(update={"risk": env.s.risk.model_copy(update={"correlated_groups": []})})
    assert neighbours(s2, env.reg, "BTCUSDT") == []
    assert capability_matrix(s2, env.reg, "BTCUSDT")["cross_asset"].quality == "unavailable"


def test_the_capability_is_real_when_a_sibling_is_configured(env):
    cap = capability_matrix(env.s, env.reg, "BTCUSDT")["cross_asset"]
    assert cap.quality == "real" and cap.source == "binance_spot:ETHUSDT"


# ------------------------------------------------------------------------------------------------ statistics
def test_a_missing_bar_makes_no_return_and_nothing_is_filled_forward():
    ot = np.array([0, 1, 2, 4, 5], dtype=np.int64) * M15.ms                 # bar 3 is missing (hand-built edge case)
    close = np.array([100.0, 101.0, 102.0, 104.0, 105.0])
    t, r = log_returns(ot, close, M15.ms)
    assert list(t // M15.ms) == [1, 2, 5]                                     # 2 -> 4 and 4 -> ... skip the hole
    assert r == pytest.approx(np.log([101 / 100, 102 / 101, 105 / 104]))


def test_corr_beta_are_the_numpy_ones_on_the_real_btc_eth_bars(env):
    env.seed(env.btc, BTC)
    env.seed(env.eth, ETH)
    blk = env.context().block("BTCUSDT", env.frame(END), END)
    c, b, n = independent(END)
    a = blk["assets"]["ETHUSDT"]
    assert blk["data_quality"] == "real" and blk["bars"] == 96 and n == 96
    assert a["corr"] == round(c, 2) and a["beta"] == round(b, 2)
    assert 0.5 < a["corr"] < 1.0 and 0 < a["beta"]                            # BTC and ETH 15m returns do move together


def test_the_change_and_trend_fields_are_computed_from_the_siblings_closed_bars(env):
    env.seed(env.btc, BTC)
    env.seed(env.eth, ETH)
    blk = env.context().block("BTCUSDT", env.frame(MID), MID)
    a = blk["assets"]["ETHUSDT"]
    bars = [b for b in ETH if b[0] + M15.ms <= MID]
    close = np.array([b[4] for b in bars])
    assert a["chg_pct"]["1h"] == round((close[-1] / close[-5] - 1) * 100, 2)
    assert a["chg_pct"]["4h"] == round((close[-1] / close[-17] - 1) * 100, 2)
    ny_open = next(b for b in bars if b[0] == MID - 2 * H)                     # 12:00 UTC = 08:00 New York (EDT)
    assert a["chg_pct"]["session"] == round((close[-1] / ny_open[1] - 1) * 100, 2)
    e = lambda n: pd_ema(close, n)                                            # noqa: E731
    up = close[-1] > e(50) and e(20) > e(50)
    down = close[-1] < e(50) and e(20) < e(50)
    assert a["trend"] == ["up" if up else "down" if down else "flat"] and len(a["trend"]) == 1


def pd_ema(x, n):
    """EMA seeded with the SMA of the first n values (the module's convention), recomputed here."""
    k = 2 / (n + 1)
    v = x[:n].mean()
    for p in x[n:]:
        v = p * k + v * (1 - k)
    return v


def test_trend_labels_on_hand_built_series():
    up = np.linspace(100, 130, 120)
    assert trend_label(up) == "up" and trend_label(up[::-1].copy()) == "down"
    assert trend_label(np.full(120, 100.0)) == "flat" and trend_label(up[:20]) is None


# ------------------------------------------------------------------------------------------------ degraded data
def test_a_missing_sibling_file_is_unavailable_without_an_exception_and_creates_nothing(env):
    env.seed(env.btc, BTC)
    before = files(env.root)
    dirs = sorted(str(p) for p in env.root.rglob("*") if p.is_dir())
    assert before == {}
    blk = env.context().block("BTCUSDT", env.frame(END), END)
    assert blk["data_quality"] == "unavailable" and "ETHUSDT" in blk["reason"]
    assert files(env.root) == before and sorted(str(p) for p in env.root.rglob("*") if p.is_dir()) == dirs
    assert not env.eth.hot_db_path(env.root).exists()


def test_a_stale_or_short_sibling_is_unavailable(env):
    env.seed(env.btc, BTC)
    env.seed(env.eth, [b for b in ETH if b[0] < END - 3 * H])                 # the newest sibling bar is 3 h old
    stale = env.context().block("BTCUSDT", env.frame(END), END)
    assert stale["data_quality"] == "unavailable" and "stale" in stale["reason"]
    env.reseed(ETH[-50:])                                              # 50 bars: no trend, too few returns
    short = env.context().block("BTCUSDT", env.frame(END), END)
    assert short["data_quality"] == "unavailable"


def test_a_few_missing_sibling_bars_shrink_the_sample_and_many_make_it_unavailable(env):
    env.seed(env.btc, BTC)
    drop = {ETH[-10][0], ETH[-30][0], ETH[-31][0], ETH[-60][0]}               # hand-built holes inside the window
    env.seed(env.eth, [b for b in ETH if b[0] not in drop])
    blk = env.context().block("BTCUSDT", env.frame(END), END)
    c, b, n = independent(END, drop)
    assert n == 96 - 2 - 3 - 2                                                  # a hole removes the return into it and out of it
    assert blk["assets"]["ETHUSDT"]["corr"] == round(c, 2) and blk["assets"]["ETHUSDT"]["beta"] == round(b, 2)
    env.reseed([b for i, b in enumerate(ETH) if i % 2 == 0 or i < len(ETH) - 100])   # every other bar of the window
    assert env.context().block("BTCUSDT", env.frame(END), END)["data_quality"] == "unavailable"


def test_a_flat_sibling_has_no_beta_and_is_unavailable(env):
    env.seed(env.btc, BTC)
    env.seed(env.eth, [(t, 2000.0, 2000.0, 2000.0, 2000.0) for t, *_ in ETH[-200:]])   # hand-built: a constant price
    assert env.context().block("BTCUSDT", env.frame(END), END)["data_quality"] == "unavailable"


def test_a_reader_failure_is_unavailable_never_an_exception(env, monkeypatch):
    env.seed(env.btc, BTC)
    env.seed(env.eth, ETH)
    cc = env.context()
    monkeypatch.setattr(cc, "_frame", lambda *a: (_ for _ in ()).throw(RuntimeError("disk error")))
    blk = cc.block("BTCUSDT", env.frame(END), END)
    assert blk == {"data_quality": "unavailable", "reason": "cross-asset read failed"}


# ------------------------------------------------------------------------------------------------ causality
def test_bars_after_as_of_never_reach_the_block(env):
    env.seed(env.btc, BTC)
    env.seed(env.eth, ETH)
    full = env.context().block("BTCUSDT", env.frame(MID), MID)
    env.reseed([b for b in ETH if b[0] + M15.ms <= MID])                # only what had closed by MID
    assert env.context().block("BTCUSDT", env.frame(MID), MID) == full
    shifted = [(t, *(v * (5.0 if t + M15.ms > MID else 1.0) for v in rest)) for t, *rest in ETH]   # the future is wrong
    env.reseed(shifted)
    assert env.context().block("BTCUSDT", env.frame(MID), MID) == full


# ------------------------------------------------------------------------------------------------ read-only, cost
def test_the_sibling_database_is_opened_read_only_and_left_untouched(env):
    env.seed(env.btc, BTC)
    env.seed(env.eth, ETH)
    before = files(env.root)
    db = env.eth.hot_db_path(env.root)
    assert list(before) == [str(db.relative_to(env.root))]                        # the writer closed cleanly: no sidecars
    cc = env.context()
    cc.block("BTCUSDT", env.frame(END), END)
    hot = cc._reader(env.eth).hot
    assert hot.readonly is True
    with pytest.raises(Exception):
        hot.upsert(spec_for(env.eth, "candles", M15), binance_rows(ETH[:1]))     # a writer call on the reader is refused
    cc.close()
    after = files(env.root)
    assert after[str(db.relative_to(env.root))] == before[str(db.relative_to(env.root))]    # not one byte of the DB changed
    # SQLite gives a read-only WAL reader its (empty) -wal/-shm sidecars when no writer holds the DB; in production the
    # ingest keeps them open. Nothing else may appear.
    assert {Path(k).name for k in after} <= {db.name, db.name + "-wal", db.name + "-shm"}
    assert all((env.root / k).stat().st_size == 0 for k in after if k.endswith("-wal"))


def test_the_block_costs_under_100_ms_and_100_tokens(env):
    env.seed(env.btc, BTC)
    env.seed(env.eth, ETH)
    cc, fr = env.context(), env.frame(END)
    cc.block("BTCUSDT", fr, END)                                                  # cold read
    t = []
    for _ in range(5):
        t0 = time.perf_counter()
        blk = cc.block("BTCUSDT", fr, END)
        t.append(time.perf_counter() - t0)
    assert min(t) < 0.1
    assert len(json.dumps(blk, separators=(",", ":"))) / 4 <= 100


# ------------------------------------------------------------------------------------------------ the snapshot
def test_the_snapshot_carries_the_block_and_downgrades_the_capability_when_the_data_is_missing(env):
    env.seed(env.btc, BTC)
    env.seed(env.eth, ETH)
    b = SnapshotBuilder(env.s, env.reg)
    try:
        p = b.build("BTCUSDT", END)
        assert p["market"]["cross"]["assets"]["ETHUSDT"]["corr"] is not None
        assert p["capabilities"]["cross_asset"]["quality"] == "real"
        v = model_view(p)
        assert v["market"]["cross"] == {k: x for k, x in p["market"]["cross"].items() if k != "bars"}   # in the legend
        assert "cross_asset" in v["capabilities"]["real"]
        b.close()
        env.reseed(None)
        q = b.build("BTCUSDT", END)
        assert q["market"]["cross"]["data_quality"] == "unavailable"
        cap = q["capabilities"]["cross_asset"]
        assert cap["quality"] == "unavailable" and "ETHUSDT" in cap["reason"]
    finally:
        b.close()


def test_a_pair_without_a_neighbour_has_no_cross_block(env):
    s = env.s.model_copy(update={"risk": env.s.risk.model_copy(update={"correlated_groups": []})})
    env.seed(env.btc, BTC)
    b = SnapshotBuilder(s, env.reg)
    try:
        assert "cross" not in b.build("BTCUSDT", END)["market"]
    finally:
        b.close()


# ------------------------------------------------------------------------------------------------ extremes
def test_extreme_side_on_hand_built_bars():
    n = 96
    ot = np.arange(n, dtype=np.int64) * M15.ms
    hi, lo = np.full(n, 101.0), np.full(n, 99.0)
    assert extreme_side(ot, hi, lo, int(ot[-1]), M15.ms) is None
    hi2 = hi.copy()
    hi2[-2] = 103.0                                                            # a new 24 h high 2 bars ago
    assert extreme_side(ot, hi2, lo, int(ot[-1]), M15.ms) == "high"
    lo2 = lo.copy()
    lo2[-1] = 97.0
    assert extreme_side(ot, hi2, lo2, int(ot[-1]), M15.ms) is None             # both sides: no direction
    assert extreme_side(ot, hi, lo2, int(ot[-1]), M15.ms) == "low"
    hi3 = hi.copy()
    hi3[-10] = 103.0                                                           # older than 3 bars: it is the reference
    assert extreme_side(ot, hi3, lo, int(ot[-1]), M15.ms) is None
    assert extreme_side(ot[:60], hi[:60], lo[:60], int(ot[59]), M15.ms) is None   # window < 80 % covered


def test_corr_beta_needs_enough_aligned_returns_and_a_moving_sibling():
    rng = np.random.default_rng(7)                                             # synthetic, pure-logic
    ot = np.arange(1, 100, dtype=np.int64) * M15.ms
    y = rng.normal(0, 0.001, len(ot))
    x = 2.0 * y + rng.normal(0, 0.0002, len(ot))
    last = int(ot[-1])
    c, b, n = corr_beta(ot, x, ot, y, last, M15.ms)
    assert n == 96 and c > 0.9 and b == pytest.approx(2.0, abs=0.15)
    assert corr_beta(ot, x, ot, np.zeros(len(ot)), last, M15.ms) is None
    assert corr_beta(ot[:40], x[:40], ot[:40], y[:40], int(ot[39]), M15.ms) is None


# ------------------------------------------------------------------------------------------------ never a decision input
def test_no_risk_or_execution_code_reads_the_cross_block():
    """The block is context for the model. The gate, the executor, the sizing and the recommendation contract never
    import ``analysis.cross`` nor read ``market.cross`` (B6/B17: never a gate input)."""
    checked = [*(SRC / "execution").rglob("*.py"), SRC / "ai" / "contract.py", SRC / "ai" / "triggers.py"]
    assert len(checked) > 8
    for p in checked:
        text = p.read_text(encoding="utf-8")
        for needle in ("analysis.cross", "from .cross", "..analysis.cross", '["cross"]', "['cross']", '"cross_asset"',
                       "get(\"cross\")", "cross_context"):
            assert needle not in text, f"{p.name} mentions {needle}"
