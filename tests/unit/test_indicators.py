"""Indicators vs the `ta` library on real BTCUSDT 1m candles (fixture), plus causality."""
import numpy as np
import pandas as pd
import pytest
import ta

from tradingsystem.analysis import indicators as ind


@pytest.fixture(scope="module")
def ohlc(real_candles_1m):
    t = real_candles_1m
    return {k: t[k].to_numpy().astype(float) for k in ("open", "high", "low", "close", "volume")}


def close_enough(a, b, tol=1e-6, skip=200):
    a, b = np.asarray(a, float)[skip:], np.asarray(b, float)[skip:]
    m = ~np.isnan(a) & ~np.isnan(b)
    assert m.sum() > 100
    np.testing.assert_allclose(a[m], b[m], rtol=tol, atol=tol * np.nanmax(np.abs(b[m])))


def test_rsi_matches_ta(ohlc):
    ref = ta.momentum.RSIIndicator(pd.Series(ohlc["close"]), window=14, fillna=False).rsi()
    close_enough(ind.rsi(ohlc["close"]), ref, tol=1e-6)


def test_atr_matches_ta(ohlc):
    ref = ta.volatility.AverageTrueRange(pd.Series(ohlc["high"]), pd.Series(ohlc["low"]), pd.Series(ohlc["close"]),
                                         window=14, fillna=False).average_true_range()
    close_enough(ind.atr(ohlc["high"], ohlc["low"], ohlc["close"]), ref, tol=1e-6)


def test_ema_sma_bollinger_match_ta(ohlc):
    c = pd.Series(ohlc["close"])
    close_enough(ind.sma(ohlc["close"], 50), ta.trend.SMAIndicator(c, 50).sma_indicator())
    close_enough(ind.ema(ohlc["close"], 21), ta.trend.EMAIndicator(c, 21).ema_indicator(), tol=1e-6, skip=400)
    bb = ta.volatility.BollingerBands(c, 20, 2)
    lo, mid, hi = ind.bollinger(ohlc["close"])
    close_enough(hi, bb.bollinger_hband())
    close_enough(lo, bb.bollinger_lband())


def test_macd_matches_ta(ohlc):
    m = ta.trend.MACD(pd.Series(ohlc["close"]))
    line, sig, hist = ind.macd(ohlc["close"])
    close_enough(line, m.macd(), tol=1e-5, skip=500)
    close_enough(sig, m.macd_signal(), tol=1e-4, skip=500)


def test_adx_matches_ta(ohlc):
    a = ta.trend.ADXIndicator(pd.Series(ohlc["high"]), pd.Series(ohlc["low"]), pd.Series(ohlc["close"]), 14)
    val, pdi, mdi = ind.adx(ohlc["high"], ohlc["low"], ohlc["close"])
    close_enough(pdi, a.adx_pos(), tol=1e-4, skip=500)
    close_enough(mdi, a.adx_neg(), tol=1e-4, skip=500)
    close_enough(val, a.adx(), tol=1e-3, skip=500)


@pytest.mark.parametrize("fn", [
    lambda d: ind.rsi(d["close"]), lambda d: ind.atr(d["high"], d["low"], d["close"]),
    lambda d: ind.macd(d["close"])[0], lambda d: ind.adx(d["high"], d["low"], d["close"])[0],
    lambda d: ind.bollinger(d["close"])[2], lambda d: ind.ema(d["close"], 50),
])
def test_causal(ohlc, fn):
    """Value at t must not change when data after t is removed (no look-ahead)."""
    full = fn(ohlc)
    for t in (800, 1500, 2400):
        cut = fn({k: v[:t + 1] for k, v in ohlc.items()})
        np.testing.assert_array_equal(np.nan_to_num(cut, nan=-1), np.nan_to_num(full[:t + 1], nan=-1))
