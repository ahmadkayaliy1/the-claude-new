"""Cross-asset context (Phase 5 B6 + B17): what the assets around a pair do, as ``market.cross``.

Two kinds of neighbour, one reader:
* a SIBLING (B6): the other members of a ``risk.correlated_groups`` group naming the pair (BTC <-> ETH); its primary
  instrument's hot DB (``data/hot/<venue>/<SYMBOL>.db``);
* a CONTEXT instrument (B17): an instrument of the pair with the role ``cross_context`` (gold: ``EURUSD@`` as the USD
  proxy and ``XAGUSD@`` silver, MT5, candles only).
Per neighbour: the correlation and beta of the pair's 15m log returns against it over the last 96 aligned bars, its 15m
and 1h trend (EMA 20/50 against the close), the percent change over 1 h / 4 h / the session in progress; for silver also
whether it confirms an extreme of gold; for gold an info-only ``votes`` list.

Nothing here decides anything: the block is display context for the model, never an input of the risk gate or the
executor (a test asserts that). Everything is causal and cheap:
* only bars closed by ``as_of`` (``load_frame``), read from the neighbour's hot DB opened ``mode=ro`` through the
  storage reader with a small cache — nothing is written, no file is created, no lock is taken;
* log returns exist only between consecutive bars of a series, the two series are aligned on bar OPEN times, and a bar one
  series lacks is dropped from the statistics — never filled forward;
* a missing pair, file, or too few aligned bars is ``unavailable`` with a reason, never an exception.
"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

from ..core.instruments import Instrument, InstrumentRegistry
from ..core.sessions import calendar_for
from ..core.settings import Settings
from ..core.timeframes import Timeframe
from ..storage.reader import InstrumentReader
from . import context as ctx
from . import indicators as ind
from .frames import Frame, load_frame
from .session_stats import session_window

log = logging.getLogger("engine")
UTC = dt.timezone.utc
M15, H1 = Timeframe.M15, Timeframe.H1
CORR_BARS = 96                  # 15m returns in the correlation / beta window (one day)
MIN_ALIGNED = 60                # fewer aligned returns than this: unavailable
TREND_BARS = {M15: 160, H1: 100}    # closed bars read for the EMA 20/50 trend (EMA(50) needs the warm-up)
MIN_TREND_BARS = 60
CACHE_MB = 4                    # SQLite page cache per neighbour DB (a few hundred KB are touched per call)
VOTE_MIN_CORR = 0.3             # below this |corr| a neighbour casts no vote
EXTREME_BARS = 3                # "at an extreme" = a new 24 h high / low within the last 3 closed bars
CONTEXT_ROLE = "cross_context"
SILVER = frozenset({"XAGUSD"})  # the context instrument that can confirm an extreme of a metal pair


@dataclass(frozen=True)
class Neighbour:
    label: str                  # short name in the payload: the instrument's file stem (ETHUSDT, EURUSD, XAGUSD)
    inst: Instrument
    kind: str                   # "sibling" | "context"


_SIBLINGS: dict[tuple, Instrument | None] = {}


def sibling_instrument(settings: Settings, name: str) -> Instrument | None:
    """The analysis-primary Instrument of pair ``name`` built from ``settings.pairs[name]``. A per-pair system (D-042)
    enables only its own pair, so the running registry does not hold the sibling; its data files are still there (the
    sibling's own ingest writes them). None when the pair is not configured."""
    key = (settings.config_hash, settings.mt5.data_profile, name)
    if key not in _SIBLINGS:
        pcfg = settings.pairs.get(name)
        inst = None
        if pcfg is not None:
            only = settings.model_copy(update={"pairs": {name: pcfg.model_copy(update={"enabled": True})}})
            try:
                inst = InstrumentRegistry.from_settings(only).primary(name)
            except Exception:  # noqa: BLE001 - a broken sibling entry means "no sibling", never an error here
                inst = None
        _SIBLINGS[key] = inst
    return _SIBLINGS[key]


def neighbours(settings: Settings, reg: InstrumentRegistry, pair: str) -> list[Neighbour]:
    """The configured neighbours of ``pair`` (independent of whether their data exists): the primary instrument of every
    other pair of a ``risk.correlated_groups`` group that names it, then the pair's own ``cross_context`` instruments."""
    out: list[Neighbour] = []
    for group in settings.risk.correlated_groups:
        if pair not in group:
            continue
        for other in group:
            inst = sibling_instrument(settings, other) if other != pair else None
            if inst is not None and all(n.inst.key != inst.key for n in out):
                out.append(Neighbour(inst.file_stem, inst, "sibling"))
    out += [Neighbour(i.file_stem, i, "context") for i in reg.with_role(pair, CONTEXT_ROLE)]
    return out


# ----------------------------------------------------------------------------------------------------- statistics
def log_returns(open_time: np.ndarray, close: np.ndarray, tf_ms: int) -> tuple[np.ndarray, np.ndarray]:
    """(open time of the later bar, log return) for every pair of CONSECUTIVE bars — a missing bar makes no return."""
    ot = np.asarray(open_time, dtype=np.int64)
    c = np.asarray(close, dtype=float)
    if len(ot) < 2:
        return np.empty(0, dtype=np.int64), np.empty(0)
    ok = (np.diff(ot) == tf_ms) & (c[:-1] > 0) & (c[1:] > 0)
    return ot[1:][ok], np.log(c[1:][ok] / c[:-1][ok])


def corr_beta(ot_a, ret_a, ot_b, ret_b, last_open: int, tf_ms: int, n: int = CORR_BARS, min_n: int = MIN_ALIGNED
              ) -> tuple[float, float, int] | None:
    """(correlation, beta of a on b, aligned returns used) over the returns whose bar opened in the last ``n`` grid slots
    up to ``last_open`` (the newest closed bar of the pair), aligned on the open time. None when fewer than ``min_n``
    aligned returns exist or ``b`` does not move."""
    lo = last_open - (n - 1) * tf_ms
    ka, kb = (ot_a >= lo) & (ot_a <= last_open), (ot_b >= lo) & (ot_b <= last_open)
    common, ia, ib = np.intersect1d(ot_a[ka], ot_b[kb], assume_unique=True, return_indices=True)
    if len(common) < min_n:
        return None
    x, y = ret_a[ka][ia], ret_b[kb][ib]
    sx, sy = float(np.std(x)), float(np.std(y))
    if sx <= 0 or sy <= 0:
        return None
    c = float(np.corrcoef(x, y)[0, 1])
    beta = float(np.cov(x, y, ddof=0)[0, 1] / np.var(y))
    return c, beta, int(len(common))


def trend_label(close: np.ndarray) -> str | None:
    """``up`` = the last close and EMA(20) are both above EMA(50); ``down`` = both below; else ``flat``. None = too few bars."""
    if len(close) < MIN_TREND_BARS:
        return None
    e20, e50 = ind.ema(close, 20)[-1], ind.ema(close, 50)[-1]
    if not (np.isfinite(e20) and np.isfinite(e50)):
        return None
    px = float(close[-1])
    return "up" if px > e50 and e20 > e50 else "down" if px < e50 and e20 < e50 else "flat"


def pct_change(close: np.ndarray, bars: int) -> float | None:
    if len(close) <= bars or close[-1 - bars] <= 0:
        return None
    return round(float(close[-1] / close[-1 - bars] - 1) * 100, 2)


def session_open_index(open_time: np.ndarray, as_of: int, tf_ms: int) -> int | None:
    """Index of the first closed bar of the session in progress at ``as_of`` (the most recently started of the sessions of
    ``context.SESSION_HOURS``), None when no session is in progress or its first bar is not there (within one bar)."""
    best = None
    for name, (tz, _a, _b) in ctx.SESSION_HOURS.items():
        day = dt.datetime.fromtimestamp(as_of / 1000, tz=UTC).astimezone(ZoneInfo(tz)).date()
        s, e = session_window(name, day)
        if s <= as_of < e and (best is None or s > best):
            best = s
    if best is None:
        return None
    i = int(np.searchsorted(open_time, best))
    if i >= len(open_time) or open_time[i] - best > tf_ms:
        return None
    return i


def extreme_side(open_time, high, low, last_open: int, tf_ms: int, n: int = CORR_BARS, recent: int = EXTREME_BARS
                 ) -> str | None:
    """``high`` / ``low`` when one of the last ``recent`` closed bars (opening after ``last_open - recent bars``) set a new
    high / low of the ``n`` bars (24 h) up to ``last_open`` — the bars before them being the reference; None when neither
    (or both) or the window is less than 80 % covered."""
    ot = np.asarray(open_time, dtype=np.int64)
    lo = last_open - (n - 1) * tf_ms
    w = (ot >= lo) & (ot <= last_open)
    if w.sum() < 0.8 * n:
        return None
    is_recent = ot[w] > last_open - recent * tf_ms
    if not is_recent.any() or is_recent.all():
        return None
    h, lw = np.asarray(high, dtype=float)[w], np.asarray(low, dtype=float)[w]
    up = bool(h[is_recent].max() > h[~is_recent].max())
    dn = bool(lw[is_recent].min() < lw[~is_recent].min())
    return "high" if up and not dn else "low" if dn and not up else None


# ----------------------------------------------------------------------------------------------------- the reader
class CrossContext:
    """Builds ``market.cross``. One read-only reader per neighbour, opened lazily with a small cache and closed with the
    builder."""

    def __init__(self, settings: Settings, reg: InstrumentRegistry, data_dir: Path) -> None:
        self.s, self.reg, self.data = settings, reg, data_dir
        self._readers: dict[str, InstrumentReader] = {}

    def _reader(self, inst: Instrument) -> InstrumentReader:
        if inst.key not in self._readers:
            self._readers[inst.key] = InstrumentReader(inst, self.data, cache_mb=CACHE_MB)
        return self._readers[inst.key]

    def close(self) -> None:
        for r in self._readers.values():
            r.close()
        self._readers.clear()

    def _frame(self, nb: Neighbour, tf: Timeframe, as_of: int) -> Frame:
        cal = calendar_for(nb.inst.venue, nb.inst.symbol, self.s.pairs[nb.inst.pair].asset_class)
        return load_frame(self._reader(nb.inst), nb.inst, tf, TREND_BARS[tf], as_of, cal)

    def block(self, pair: str, primary: Frame | None, as_of: int) -> dict:
        """``market.cross`` for ``pair`` at ``as_of`` from the pair's closed 15m frame ``primary``. Never raises."""
        try:
            return self._block(pair, primary, as_of)
        except Exception as e:  # noqa: BLE001 - advice only: never break the payload
            log.warning("cross %s: %r", pair, e)
            return {"data_quality": "unavailable", "reason": "cross-asset read failed"}

    def _block(self, pair: str, primary: Frame | None, as_of: int) -> dict:
        nbs = neighbours(self.s, self.reg, pair)
        if not nbs:
            return {"data_quality": "unavailable", "reason": "no correlated pair or context instrument configured"}
        if primary is None or len(primary) < MIN_TREND_BARS or M15 not in self.reg.primary(pair).timeframes:
            return {"data_quality": "unavailable", "reason": "no 15m frame of this pair"}
        tf_ms = M15.ms
        metal = self.s.pairs[pair].asset_class == "metal"
        last_open = int(primary.open_time[-1])
        p_ot, p_ret = log_returns(primary.open_time, primary.close, tf_ms)
        p_ext = extreme_side(primary.open_time, primary.high, primary.low, last_open, tf_ms) if metal else None
        assets: dict[str, dict] = {}
        missing: list[str] = []
        votes: list[str] = []
        muted: list[str] = []
        for nb in nbs:
            path = nb.inst.hot_db_path(self.data)
            if not path.exists():
                missing.append(nb.label)
                continue
            f15 = self._frame(nb, M15, as_of)
            if len(f15) < MIN_TREND_BARS or f15.quality.get("stale") or int(f15.open_time[-1]) < last_open - tf_ms:
                missing.append(nb.label)                       # no fresh newest bar: nothing is filled forward
                continue
            n_ot, n_ret = log_returns(f15.open_time, f15.close, tf_ms)
            cb = corr_beta(p_ot, p_ret, n_ot, n_ret, last_open, tf_ms)
            if cb is None:
                missing.append(nb.label)
                continue
            c, beta, _n = cb
            item: dict = {"corr": round(c, 2), "beta": round(beta, 2)}
            t15 = trend_label(f15.close)
            t1h = None
            if nb.kind == "context":                    # a sibling shows its 15m trend only
                f1h = self._frame(nb, H1, as_of)
                t1h = trend_label(f1h.close) if len(f1h) else None
            item["trend"] = [t15, t1h] if nb.kind == "context" else [t15]
            chg = {"1h": pct_change(f15.close, 4), "4h": pct_change(f15.close, 16)}
            si = session_open_index(f15.open_time, as_of, tf_ms)
            if si is not None and si < len(f15) - 1:
                chg["session"] = round(float(f15.close[-1] / f15.open[si] - 1) * 100, 2)
            item["chg_pct"] = {k: v for k, v in chg.items() if v is not None}
            if nb.kind == "context":
                if p_ext is not None and nb.label in SILVER and metal:
                    e = extreme_side(f15.open_time, f15.high, f15.low, last_open, tf_ms)
                    item["confirms_xau_extreme"] = p_ext if e == p_ext else 0
                if abs(c) < VOTE_MIN_CORR:
                    muted.append(nb.label)
                elif t1h in ("up", "down"):
                    votes.append(f"{nb.label}:" + (t1h if c > 0 else "down" if t1h == "up" else "up"))
            assets[nb.label] = item
        if not assets:
            return {"data_quality": "unavailable",
                    "reason": "neighbour data missing, stale or too short: " + ", ".join(missing)}
        out: dict = {"data_quality": "real", "bars": CORR_BARS, "assets": assets}
        if missing:
            out["missing"] = missing
        if any(n.kind == "context" for n in nbs):
            out["votes"] = votes
            if muted:
                out["votes_note"] = "|corr|<0.3 casts no vote: " + ", ".join(muted)
        return out
