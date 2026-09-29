"""As-of candle frames (P6.1): closed candles only, plus the forming bar flagged separately; quality flags for
stale, gapped, short or tail-missing windows so the engine never analyses silently broken data (spec §9)."""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from ..core.instruments import Instrument
from ..core.sessions import SessionCalendar
from ..core.timeframes import Timeframe
from ..core.timeutil import MS_PER_HOUR
from ..storage.gaps import candle_gaps
from ..storage.reader import InstrumentReader
from ..storage.tablespec import FORMING, spec_for

F = np.ndarray
FORMING_BEHIND_MS = 60_000          # a forming row updated longer before as_of than this: the writer is down
FORMING_AHEAD_MS = 15_000           # a row updated later than this after as_of holds ticks the screen must not know
MIN_BARS = 30                       # below this a timeframe has no trend/indicator analysis (snapshot._analyze_tf)


@dataclass
class Frame:
    inst_key: str
    tf: Timeframe
    open_time: F
    open: F
    high: F
    low: F
    close: F
    volume: F                       # traded volume (Binance) or tick volume (MT5)
    volume_kind: str                # "traded" | "tick"
    taker_buy: F | None = None      # Binance only (exact aggressive-buy volume)
    forming: dict | None = None
    as_of: int = 0
    quality: dict = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.close)

    @property
    def last_close(self) -> float:
        return float(self.close[-1]) if len(self.close) else float("nan")


def load_frame(reader: InstrumentReader, inst: Instrument, tf: Timeframe, bars: int, as_of: int,
               calendar: SessionCalendar | None = None) -> Frame:
    spec = spec_for(inst, "candles", tf)
    span = int(bars * tf.ms * (2.2 if inst.venue == "mt5" else 1.05)) + tf.ms   # MT5 has closed sessions
    cols = reader.read_range(spec, as_of - span, as_of)
    ot = cols["open_time"].astype(np.int64)
    closed = ot + tf.ms <= as_of                   # a bar is usable only once it has closed
    sel = np.nonzero(closed)[0][-bars:]
    mt5 = inst.venue == "mt5"
    fr = Frame(
        inst_key=inst.key, tf=tf, open_time=ot[sel],
        open=cols["open"][sel].astype(float), high=cols["high"][sel].astype(float),
        low=cols["low"][sel].astype(float), close=cols["close"][sel].astype(float),
        volume=(cols["tick_volume"] if mt5 else cols["volume"])[sel].astype(float),
        volume_kind="tick" if mt5 else "traded",
        taker_buy=None if mt5 else cols["taker_buy_base"][sel].astype(float), as_of=as_of,
    )
    fr.forming = _forming(reader, tf)
    fr.quality = _quality(fr, tf, as_of, calendar, mt5, bars)
    return fr


def forming_bar(fr: Frame) -> dict | None:
    """The still-open bar of ``fr`` as it was at ``fr.as_of``, or None. The FORMING table holds the LIVE state only,
    so it is a valid as-of reading just for the bar that contains ``as_of`` and only while it was written within
    [as_of - 60 s, as_of + 15 s] — for a replay of a past instant it is never used (no later tick leaks in). Never an
    input to indicators, structure, zones or triggers: display context only (Phase 5 B3)."""
    f = fr.forming
    if not f or not fr.as_of:
        return None
    try:
        if int(f["open_time"]) != fr.tf.floor(fr.as_of):
            return None
        upd = int(f["updated_ms"])
        if not (fr.as_of - FORMING_BEHIND_MS <= upd <= fr.as_of + FORMING_AHEAD_MS):
            return None
        vals = [float(f[k]) for k in ("open", "high", "low", "close")]
    except (KeyError, TypeError, ValueError):
        return None
    if not all(np.isfinite(vals)):
        return None
    vol = f.get("volume")
    return {"open_time": int(f["open_time"]), "open": vals[0], "high": vals[1], "low": vals[2], "close": vals[3],
            "volume": float(vol) if vol is not None else None, "age_s": max(0.0, (fr.as_of - int(f["open_time"])) / 1000)}


def _forming(reader: InstrumentReader, tf: Timeframe) -> dict | None:
    hot = reader.hot
    if hot is None:
        return None
    rows = hot.read_range(FORMING, columns=list(FORMING.column_names))
    for i, t in enumerate(rows["tf"]):
        if t == tf.value:
            return {k: (v[i].item() if hasattr(v[i], "item") else v[i]) for k, v in rows.items()}
    return None


def _quality(fr: Frame, tf: Timeframe, as_of: int, cal: SessionCalendar | None, mt5: bool, bars: int = 0) -> dict:
    """status: empty > stale > missing_last_bar > short_history > gaps > ok (the first that applies)."""
    q: dict = {"bars": len(fr)}
    if bars:
        q["coverage"] = round(len(fr) / bars, 2)
    if not len(fr):
        q["status"] = "empty"
        return q
    last_end = int(fr.open_time[-1]) + tf.ms
    lag = as_of - last_end
    market_open = cal.is_open(as_of) if cal else True
    q["last_bar_end_lag_s"] = round(lag / 1000, 1)
    stale = market_open and lag > max(2 * tf.ms, 180_000)
    q["stale"] = bool(stale)
    # the bar that closed last is not stored yet (intraday only: MT5 H4/D1/W1 follow the broker's day, not UTC)
    expected = tf.floor(as_of)
    missing = tf.ms <= MS_PER_HOUR and last_end < expected and (cal.is_open(expected - 1) if cal else True)
    q["missing_last_bar"] = bool(missing)
    q["short_history"] = len(fr) < min(MIN_BARS, bars or MIN_BARS)
    if not mt5 and tf.ms <= 3_600_000 and len(fr) > 1:
        g = candle_gaps(fr.open_time, tf, int(fr.open_time[0]), int(fr.open_time[-1]), cal)
        q["gaps"] = [(int(x.start), x.count) for x in g[-5:]]           # the most recent gaps matter most
    q["status"] = ("stale" if stale else "missing_last_bar" if missing else "short_history" if q["short_history"]
                   else "gaps" if q.get("gaps") else "ok")
    return q
