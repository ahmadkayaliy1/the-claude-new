"""As-of candle frames (P6.1): closed candles only, plus the forming bar flagged separately; quality flags for
stale or gapped windows so the engine never analyses silently broken data (spec §9)."""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from ..core.instruments import Instrument
from ..core.sessions import SessionCalendar
from ..core.timeframes import Timeframe
from ..storage.gaps import candle_gaps
from ..storage.reader import InstrumentReader
from ..storage.tablespec import FORMING, spec_for

F = np.ndarray


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
    fr.quality = _quality(fr, tf, as_of, calendar, mt5)
    return fr


def _forming(reader: InstrumentReader, tf: Timeframe) -> dict | None:
    hot = reader.hot
    if hot is None:
        return None
    rows = hot.read_range(FORMING, columns=list(FORMING.column_names))
    for i, t in enumerate(rows["tf"]):
        if t == tf.value:
            return {k: (v[i].item() if hasattr(v[i], "item") else v[i]) for k, v in rows.items()}
    return None


def _quality(fr: Frame, tf: Timeframe, as_of: int, cal: SessionCalendar | None, mt5: bool) -> dict:
    q: dict = {"bars": len(fr)}
    if not len(fr):
        q["status"] = "empty"
        return q
    last_end = int(fr.open_time[-1]) + tf.ms
    lag = as_of - last_end
    market_open = cal.is_open(as_of) if cal else True
    q["last_bar_end_lag_s"] = round(lag / 1000, 1)
    stale = market_open and lag > max(2 * tf.ms, 180_000)
    q["stale"] = bool(stale)
    if not mt5 and tf.ms <= 3_600_000 and len(fr) > 1:
        g = candle_gaps(fr.open_time, tf, int(fr.open_time[0]), int(fr.open_time[-1]), cal)
        q["gaps"] = [(int(x.start), x.count) for x in g[:5]]
    q["status"] = "stale" if stale else ("gaps" if q.get("gaps") else "ok")
    return q
