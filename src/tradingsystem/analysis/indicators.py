"""Technical indicators (P6.2) — pure numpy, causal (value at i uses data ≤ i only), NaN during warm-up.

Formulas follow the standard definitions (Wilder smoothing for RSI/ATR/ADX) and are checked against the
`ta` library on real candles in tests/unit/test_indicators.py.
"""
from __future__ import annotations

import numpy as np

F = np.ndarray


def sma(x: F, n: int) -> F:
    out = np.full(len(x), np.nan)
    if len(x) >= n:
        c = np.cumsum(np.insert(x.astype(float), 0, 0.0))
        out[n - 1:] = (c[n:] - c[:-n]) / n
    return out


def ema(x: F, n: int) -> F:
    """EMA seeded with the SMA of the first n values (matches `ta`/pandas adjust=False after warm-up)."""
    out = np.full(len(x), np.nan)
    if len(x) < n:
        return out
    a = 2.0 / (n + 1)
    out[n - 1] = x[:n].mean()
    for i in range(n, len(x)):
        out[i] = a * x[i] + (1 - a) * out[i - 1]
    return out


def ema_adjust_false(x: F, n: int) -> F:
    """EMA seeded with the first value (pandas ewm(span=n, adjust=False)); used where `ta` does the same."""
    out = np.empty(len(x))
    a = 2.0 / (n + 1)
    out[0] = x[0]
    for i in range(1, len(x)):
        out[i] = a * x[i] + (1 - a) * out[i - 1]
    return out


def wilder(x: F, n: int) -> F:
    """Wilder's smoothing (RMA): seed = mean of first n, then prev + (x - prev)/n."""
    out = np.full(len(x), np.nan)
    if len(x) < n:
        return out
    out[n - 1] = np.nanmean(x[:n])
    for i in range(n, len(x)):
        out[i] = out[i - 1] + (x[i] - out[i - 1]) / n
    return out


def rsi(close: F, n: int = 14) -> F:
    d = np.diff(close, prepend=np.nan)
    up, dn = np.where(d > 0, d, 0.0), np.where(d < 0, -d, 0.0)
    up[0] = dn[0] = np.nan
    au, ad = np.full(len(close), np.nan), np.full(len(close), np.nan)
    if len(close) > n:
        au[n] = up[1:n + 1].mean()
        ad[n] = dn[1:n + 1].mean()
        for i in range(n + 1, len(close)):
            au[i] = (au[i - 1] * (n - 1) + up[i]) / n
            ad[i] = (ad[i - 1] * (n - 1) + dn[i]) / n
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = au / ad
        out = 100 - 100 / (1 + rs)
    out[(ad == 0) & (au > 0)] = 100.0
    return out


def true_range(high: F, low: F, close: F) -> F:
    prev = np.concatenate([[np.nan], close[:-1]])
    tr = np.nanmax(np.vstack([high - low, np.abs(high - prev), np.abs(low - prev)]), axis=0)
    tr[0] = high[0] - low[0]
    return tr


def atr(high: F, low: F, close: F, n: int = 14) -> F:
    return wilder(true_range(high, low, close), n)


def macd(close: F, fast: int = 12, slow: int = 26, signal: int = 9) -> tuple[F, F, F]:
    line = ema(close, fast) - ema(close, slow)
    sig = np.full(len(close), np.nan)
    valid = np.nonzero(~np.isnan(line))[0]
    if len(valid) >= signal:
        sig[valid] = ema(line[valid], signal)
    return line, sig, line - sig


def bollinger(close: F, n: int = 20, k: float = 2.0) -> tuple[F, F, F]:
    mid = sma(close, n)
    sd = np.full(len(close), np.nan)
    if len(close) >= n:
        w = np.lib.stride_tricks.sliding_window_view(close.astype(float), n)
        sd[n - 1:] = w.std(axis=1)                      # population std, like `ta`
    return mid - k * sd, mid, mid + k * sd


def adx(high: F, low: F, close: F, n: int = 14) -> tuple[F, F, F]:
    """(ADX, +DI, −DI) with Wilder smoothing."""
    up = np.diff(high, prepend=np.nan)
    dn = -np.diff(low, prepend=np.nan)
    pdm = np.where((up > dn) & (up > 0), up, 0.0)
    mdm = np.where((dn > up) & (dn > 0), dn, 0.0)
    tr = true_range(high, low, close)
    pdm[0] = mdm[0] = tr[0] = np.nan
    s_tr, s_p, s_m = (np.full(len(close), np.nan) for _ in range(3))
    if len(close) > n:
        s_tr[n] = np.nansum(tr[1:n + 1])
        s_p[n] = np.nansum(pdm[1:n + 1])
        s_m[n] = np.nansum(mdm[1:n + 1])
        for i in range(n + 1, len(close)):
            s_tr[i] = s_tr[i - 1] - s_tr[i - 1] / n + tr[i]
            s_p[i] = s_p[i - 1] - s_p[i - 1] / n + pdm[i]
            s_m[i] = s_m[i - 1] - s_m[i - 1] / n + mdm[i]
    with np.errstate(divide="ignore", invalid="ignore"):
        pdi, mdi = 100 * s_p / s_tr, 100 * s_m / s_tr
        dx = 100 * np.abs(pdi - mdi) / (pdi + mdi)
    out = np.full(len(close), np.nan)
    first = n * 2 - 1
    if len(close) > first:
        out[first] = np.nanmean(dx[n:first + 1])
        for i in range(first + 1, len(close)):
            out[i] = (out[i - 1] * (n - 1) + dx[i]) / n
    return out, pdi, mdi


def vwap(high: F, low: F, close: F, volume: F, anchor_idx: int = 0) -> F:
    """Anchored VWAP from ``anchor_idx`` using the typical price."""
    out = np.full(len(close), np.nan)
    tp = (high + low + close) / 3
    pv = np.cumsum((tp * volume)[anchor_idx:])
    vv = np.cumsum(volume[anchor_idx:])
    with np.errstate(divide="ignore", invalid="ignore"):
        out[anchor_idx:] = pv / vv
    return out


def session_vwap(open_time: F, high: F, low: F, close: F, volume: F, session_ms: int = 86_400_000) -> F:
    """VWAP reset at each UTC session boundary (daily by default)."""
    out = np.full(len(close), np.nan)
    day = open_time // session_ms
    tp = (high + low + close) / 3
    for d in np.unique(day):
        idx = np.nonzero(day == d)[0]
        pv, vv = np.cumsum(tp[idx] * volume[idx]), np.cumsum(volume[idx])
        with np.errstate(divide="ignore", invalid="ignore"):
            out[idx] = pv / vv
    return out


def realized_vol(close: F, n: int = 20) -> F:
    r = np.diff(np.log(close), prepend=np.nan)
    out = np.full(len(close), np.nan)
    if len(close) > n:
        w = np.lib.stride_tricks.sliding_window_view(r[1:], n)
        out[n:] = w.std(axis=1, ddof=1)
    return out


def percentile_rank(x: F, n: int = 100) -> F:
    """Rank of x[i] within the previous n values (0..1) — used for volatility regime."""
    out = np.full(len(x), np.nan)
    for i in range(n - 1, len(x)):
        w = x[i - n + 1:i + 1]
        w = w[~np.isnan(w)]
        if len(w) > 1 and not np.isnan(x[i]):
            out[i] = (w < x[i]).sum() / (len(w) - 1)
    return out
