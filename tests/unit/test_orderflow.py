"""Order flow on real data: delta identity, footprint conservation, profiles."""
import numpy as np
import pytest

from tradingsystem.analysis import orderflow as of


def test_bar_delta_identity(real_candles_1m):
    v = real_candles_1m["volume"].to_numpy()
    tb = real_candles_1m["taker_buy_base"].to_numpy()
    d = of.bar_delta(v, tb)
    np.testing.assert_allclose(d + (v - tb), tb, rtol=1e-12)        # buy − sell where sell = v − buy
    c = of.cvd(d)
    assert c[-1] == pytest.approx(d.sum())


def test_footprint_conserves_volume_and_side(real_aggtrades):
    t = real_aggtrades
    ts, px, q, m = (t[c].to_numpy() for c in ("ts", "price", "qty", "is_buyer_maker"))
    m = m.astype(int)
    bars = of.footprint(ts, px, q, m, 60_000, 5.0)
    assert sum(b.volume for b in bars) == pytest.approx(q.sum(), rel=1e-12)
    assert sum(b.buy.sum() for b in bars) == pytest.approx(q[m == 0].sum(), rel=1e-12)
    for b in bars:
        assert np.all(np.diff(b.levels) > 0)
        sel = (ts // 60_000 * 60_000) == b.open_time
        assert b.delta == pytest.approx(q[sel & (m == 0)].sum() - q[sel & (m == 1)].sum(), abs=1e-9)
    merged = of.merge_bars(bars, 900_000)
    assert sum(b.volume for b in merged) == pytest.approx(q.sum(), rel=1e-12)
    imb = of.imbalances(merged[0])
    assert set(imb) == {"buy_imbalances", "sell_imbalances", "stacked_buy", "stacked_sell"}


def test_value_area_and_profiles(real_aggtrades, real_candles_1m):
    t = real_aggtrades
    bars = of.footprint(t["ts"].to_numpy(), t["price"].to_numpy(), t["qty"].to_numpy(),
                        t["is_buyer_maker"].to_numpy().astype(int), 60_000, 5.0)
    va = of.profile_from_footprint(bars)
    assert va["val"] <= va["poc"] <= va["vah"] and va["coverage"] >= 0.7
    h, l = real_candles_1m["high"].to_numpy()[:500], real_candles_1m["low"].to_numpy()[:500]
    tpo = of.tpo_profile(h, l, 10.0)
    assert tpo["val"] <= tpo["poc"] <= tpo["vah"]
    tv = of.tick_volume_profile(h, l, real_candles_1m["trades"].to_numpy()[:500].astype(float), 10.0)
    assert tv["data_quality"] == "approx"


def test_delta_divergence_detection():
    # pure-logic check: price higher high while CVD lower high → bearish
    cvd = np.array([0, 5, 3, 10, 2, 7, 1], float)
    piv = [(1, 100.0, "high"), (3, 105.0, "high"), (5, 107.0, "high")]
    div = of.delta_divergence(piv, cvd)
    assert div["type"] == "bearish" and div["price_pivots"] == [3, 5]


@pytest.mark.integration
def test_footprint_matches_binance_klines_on_live_data():
    """P6.8 verification: per-minute footprint volume == Binance kline volume, buy == taker_buy (live DB)."""
    from pathlib import Path

    from tradingsystem.core.instruments import InstrumentRegistry
    from tradingsystem.core.settings import load_settings
    from tradingsystem.core.timeframes import Timeframe
    from tradingsystem.storage.reader import InstrumentReader
    from tradingsystem.storage.tablespec import spec_for

    s = load_settings()
    btc = InstrumentRegistry.from_settings(s).primary("BTCUSDT")
    rd = InstrumentReader(btc, s.paths.data())
    k = rd.read_range(spec_for(btc, "candles", Timeframe.M1))
    a = rd.read_range(spec_for(btc, "agg_trades"))
    if len(a["ts"]) < 1000:
        pytest.skip("not enough live aggTrades yet")
    lo = int(a["ts"].min()) // 60_000 * 60_000 + 60_000          # first complete minute
    hi = min(int(a["ts"].max()) // 60_000 * 60_000, int(k["open_time"].max()))
    sel = (a["ts"] >= lo) & (a["ts"] < hi)
    bars = {b.open_time: b for b in of.footprint(a["ts"][sel], a["price"][sel], a["qty"][sel],
                                                  a["is_buyer_maker"][sel], 60_000, 5.0)}
    km = (k["open_time"] >= lo) & (k["open_time"] < hi)
    checked = 0
    for ot, vol, tb in zip(k["open_time"][km], k["volume"][km], k["taker_buy_base"][km]):
        b = bars.get(int(ot))
        assert b is not None, f"no trades for minute {ot}"
        assert b.volume == pytest.approx(vol, rel=1e-9, abs=1e-8)
        assert b.buy.sum() == pytest.approx(tb, rel=1e-9, abs=1e-8)
        checked += 1
    assert checked >= 10
    rd.close()
