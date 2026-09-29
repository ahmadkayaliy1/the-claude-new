"""Candle-chart images for the model (Phase 3, handoff §3.7.2 "Charts", D-038).

The model reads six charts (1w … 5m) next to the text payload: candles, volume and the payload's own overlays (levels,
zones, liquidity pools, structure events, EMA20/50 and this pair's holdings). Everything drawn comes from the stored
closed candles and from the payload the model also receives — the chart adds a picture, never new numbers.

Why it is built this way:

* matplotlib's object-oriented API only (``Figure`` + ``FigureCanvasAgg``): no pyplot means no global figure registry,
  nothing to close, and renders are safe from ``asyncio.to_thread``; a module lock still serialises them because
  matplotlib does not promise thread safety for its text/font caches.
* matplotlib is imported on the first render, not at import time: an engine with ``ai.charts.enabled: false`` never
  pays its ~30 MB (RAM is the laptop's scarcest resource, D-005). No mplfinance, no pandas.
* Deterministic PNG bytes (``metadata={"Software": None}``, fixed size and styles): identical inputs → identical
  images, so the per-timeframe cache and the tests can compare bytes.
* The title's "as of" is the close time of the last candle drawn, not the cycle time: a cached image stays true
  when it is re-sent a cycle later.
* Holdings are published in the execution instrument's prices; the chart is in the analysis instrument's prices, so
  they are translated back by subtracting ``market.basis_exec_minus_analysis`` (the reverse of what the executor does
  before sending orders).
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import struct
import threading
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any

import numpy as np

from ..ai.providers.base import ImageInput
from ..core.instruments import Instrument
from ..core.sessions import SessionCalendar
from ..core.settings import ChartsCfg
from ..core.timeframes import Timeframe
from ..core.timeutil import from_ms, iso, parse_date_spec
from ..storage.reader import InstrumentReader
from . import indicators as ind
from .frames import Frame, forming_bar, load_frame

log = logging.getLogger(__name__)

MIN_CHART_BARS = 20                 # below this a chart says nothing (and ChartsCfg.bars is ≥ 20)
EMA_PERIODS = (20, 50)
EMA_WARMUP = max(EMA_PERIODS)       # extra bars loaded so the EMAs span the whole visible window
MAX_ZONES = 3                       # per kind (order blocks, FVGs)
MAX_EVENTS = 6                      # structure events drawn
DPI = 100
BAND_PAD = 0.05                     # the visible price band = candle range ± 5 % of its span
RIGHT_PAD = 0.2                     # empty space right of the last candle for the labels (fraction of the bars)
LABEL_GAP_PX = 8.5                  # minimum vertical distance between two labels on one edge

UP, DOWN = "#26a69a", "#ef5350"
BULL, BEAR = "#2e7d32", "#c62828"
ZONE_COLORS = {("ob", "bullish"): "#43a047", ("ob", "bearish"): "#e53935",
               ("fvg", "bullish"): "#1e88e5", ("fvg", "bearish"): "#fb8c00"}
LEVEL_COLOR, LIQ_BUY, LIQ_SELL = "#78909c", "#8e24aa", "#6d4c41"
EMA_COLORS = {20: "#ff9800", 50: "#1565c0"}
ENTRY_COLOR, SL_COLOR, TP_COLOR = "#212121", "#d50000", "#00a152"
EVENT_MARKERS = {"BOS": "^", "CHoCH": "D", "sweep": "x"}

_RENDER_LOCK = threading.Lock()


# ---------------------------------------------------------------------------------------------------------- API types
@dataclass(frozen=True)
class ChartImage:
    """One rendered chart. ``width``/``height`` are read back from the PNG header (the pixels actually sent)."""
    tf: str
    png: bytes
    bars: int
    width: int
    height: int
    token_est: int

    def as_input(self, pair: str, instrument_key: str) -> ImageInput:
        """The provider-side image with its caption (sent as a text block right before the image)."""
        label = f"Chart {self.tf} — {pair} {instrument_key}, last {self.bars} closed candles, analysis prices"
        return ImageInput(label=label, data=self.png, media_type="image/png", token_est=self.token_est)


def token_estimate(width: int, height: int) -> int:
    """Anthropic's image token estimate ``ceil(w*h/750)`` (720×400 → 384); feeds ``ai_usage.image_tokens_est``."""
    return math.ceil(width * height / 750)


# ------------------------------------------------------------------------------------------------------ overlay spec
# Session and daily levels mean nothing at weekly / daily scale (and crowd the edge labels): those charts draw only
# the levels of their own horizon; the 4h chart draws every level. The previous week / month levels and the month /
# year open (Phase 5 B2) are drawn on the 1d and 4h charts only: at 1h and below they would sit far outside the band.
PERIOD_LEVELS = frozenset({"pwh", "pwl", "pmh", "pml", "month_open", "year_open"})
HTF_LEVELS = {"1w": frozenset({"week_open"}),
              "1d": frozenset({"pdh", "pdl", "pdc", "week_open"}) | PERIOD_LEVELS}
NO_PERIOD_LEVELS = frozenset({"1h", "15m", "5m"})
FORMING_CHART_TFS = frozenset({"5m"})       # the hollow bar in progress is drawn on the 5m chart only (B3)


def _draws_level(tf: str, name: str) -> bool:
    keep = HTF_LEVELS.get(tf)
    if keep is not None:
        return name in keep
    return not (tf in NO_PERIOD_LEVELS and name in PERIOD_LEVELS)


def overlay_spec(payload: dict, tf: str, overlays: list[str]) -> dict:
    """The overlays of one timeframe's chart, extracted from the payload (pure; JSON-serialisable so it can be hashed
    for the cache). Only the requested overlays appear; a missing or malformed payload block gives an empty list."""
    want = set(overlays)
    tfb = _as_dict(_as_dict(payload.get("timeframes")).get(tf))
    spec: dict[str, Any] = {}
    if "levels" in want:
        spec["levels"] = [{"name": str(k), "price": float(v)} for k, v in _as_dict(payload.get("levels")).items()
                          if _finite(v) and _draws_level(tf, str(k))]
    if "zones" in want:
        z = _as_dict(tfb.get("zones"))
        spec["zones"] = ([r for r in (_zone("ob", x) for x in _rows(z.get("order_blocks"))[:MAX_ZONES]) if r]
                         + [r for r in (_zone("fvg", x) for x in _rows(z.get("fvg"))[:MAX_ZONES]) if r])
    if "liquidity" in want:
        lq = _as_dict(tfb.get("liquidity"))
        spec["liquidity"] = [{"side": side, "level": float(x["level"]), "touches": _int(x.get("touches"))}
                             for side, key in (("buy", "buy_side_above"), ("sell", "sell_side_below"))
                             for x in _rows(lq.get(key)) if _finite(x.get("level"))]
    if "structure" in want:
        events = _rows(_as_dict(tfb.get("structure")).get("events"))[-MAX_EVENTS:]
        spec["structure"] = [{"time_ms": t, "kind": str(e.get("kind")), "dir": str(e.get("dir")),
                              "level": float(e["level"])}
                             for e in events if (t := _ms(e.get("time"))) is not None and _finite(e.get("level"))]
    if "ema" in want:
        spec["ema"] = list(EMA_PERIODS)
    if "holdings" in want:
        acct = _as_dict(payload.get("account"))
        basis = _as_dict(payload.get("market")).get("basis_exec_minus_analysis")
        space = acct.get("holdings_price_space")
        ref = _as_dict(payload.get("meta")).get("price_reference")
        if _finite(basis):
            basis = float(basis)
        elif space and ref and space != ref:
            basis = None                  # holdings in another instrument's prices and no basis: not drawn at all
        else:
            basis = 0.0                   # the same instrument (or nothing to translate)
        spec["holdings"] = [] if basis is None else [
            h for kind, key in (("position", "open_positions"), ("order", "pending_orders"))
            for h in (_holding(kind, r, basis) for r in _rows(acct.get(key))) if h]
    return spec


def _holding(kind: str, row: dict, basis: float) -> dict | None:
    if not _finite(row.get("price")):
        return None
    back = lambda p: round(float(p) - basis, 8)  # noqa: E731 — execution prices → analysis prices
    tps = row.get("tps") if isinstance(row.get("tps"), list) else [row.get("tp")]
    return {"id": str(row.get("decision") or "?")[:8], "kind": kind, "side": str(row.get("side") or ""),
            "order_type": str(row.get("order_type") or ""), "entry": back(row["price"]),
            "sl": back(row["sl"]) if _finite(row.get("sl")) else None,
            "tps": [back(t) for t in tps if _finite(t)]}


def _zone(kind: str, row: dict) -> dict | None:
    if not (_finite(row.get("top")) and _finite(row.get("bottom"))):
        return None
    top, bottom = float(row["top"]), float(row["bottom"])
    return {"kind": kind, "dir": str(row.get("dir")), "top": max(top, bottom), "bottom": min(top, bottom),
            "formed_ms": _ms(row.get("formed"))}


def _as_dict(x: Any) -> dict:
    return x if isinstance(x, dict) else {}


def _rows(x: Any) -> list[dict]:
    return [r for r in x if isinstance(r, dict)] if isinstance(x, list) else []


def _finite(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _int(x: Any) -> int:
    return int(x) if _finite(x) else 0


def _ms(x: Any) -> int | None:
    """Payload times are ISO strings (``iso()``); raw ms are accepted too."""
    if _finite(x):
        return int(x)
    if isinstance(x, str) and x:
        try:
            return parse_date_spec(x)
        except ValueError:
            return None
    return None


def spec_hash(spec: dict) -> str:
    return hashlib.sha256(json.dumps(spec, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:16]


# ------------------------------------------------------------------------------------------------------------ render
def _mpl():
    """matplotlib's OO pieces, imported on first use (see the module docstring)."""
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure
    return Figure, FigureCanvasAgg


def render_png(frame: Frame, tf: str, spec: dict, *, width: int, height: int, title: str, warmup: int = 0,
               forming: dict | None = None) -> bytes:
    """One chart as PNG bytes. The first ``warmup`` bars of ``frame`` only warm the EMAs up; the rest is drawn.
    ``tf`` is informational (the overlay ``spec`` was already extracted for it). ``forming`` (``frames.forming_bar``)
    adds the bar in progress as a hollow candle after the last closed one - drawn only, never analysed."""
    n_all = len(frame)
    warmup = max(0, min(int(warmup), n_all - 1))
    sl = slice(warmup, n_all)
    t, o, h, lo_, c, v = (frame.open_time[sl], frame.open[sl], frame.high[sl], frame.low[sl], frame.close[sl],
                          frame.volume[sl])
    n = len(c)
    if n == 0:
        raise ValueError(f"{frame.inst_key} {tf}: no bars to draw")
    Figure, FigureCanvasAgg = _mpl()
    with _RENDER_LOCK:          # nothing to close afterwards: no pyplot registry holds the figure, the gc frees it
        fig = Figure(figsize=(width / DPI, height / DPI), dpi=DPI, facecolor="white")
        FigureCanvasAgg(fig)
        gs = fig.add_gridspec(2, 1, height_ratios=(4, 1), hspace=0.04, left=0.012, right=0.905, top=0.925,
                              bottom=0.085)
        ax = fig.add_subplot(gs[0])
        axv = fig.add_subplot(gs[1], sharex=ax)
        fig.suptitle(title, fontsize=8.5, x=0.012, ha="left", y=0.985)
        x = np.arange(n, dtype=float)
        band = _band(np.append(lo_, forming["low"]) if forming else lo_,
                     np.append(h, forming["high"]) if forming else h)
        right = (n - 1) + max(4.0, RIGHT_PAD * n)
        _candles(ax, x, o, h, lo_, c)
        if forming:
            _hollow(ax, float(n), forming)
        _volume(axv, x, o, c, v, "tick volume" if frame.volume_kind == "tick" else "volume")
        ax.set_xlim(-1.0, right)
        ax.set_ylim(*band)
        _style(ax, axv, t, n)
        gap = (band[1] - band[0]) * LABEL_GAP_PX / max(ax.get_position().height * height, 1.0)
        left_labels, right_labels = _Labels(0.0, "left", gap), _Labels(right - 0.3, "right", gap)
        last = float(c[-1])
        ax.hlines(last, n - 1, right, colors="#90a4ae", linewidth=0.6, linestyles=":", zorder=2)
        right_labels.add(last, f"last {_fmt(last)}", "#37474f")
        if "levels" in spec:
            _levels(ax, spec["levels"], band, left_labels)
        if "zones" in spec:
            _zones(ax, spec["zones"], t, band, right)
        if "liquidity" in spec:
            _liquidity(ax, spec["liquidity"], band, right_labels)
        if "structure" in spec:
            _structure(ax, spec["structure"], t, band)
        if "ema" in spec:
            _emas(ax, frame.close, warmup, x, spec["ema"], right_labels)
        if "holdings" in spec:
            _holdings(ax, spec["holdings"], band, right_labels)
        left_labels.draw(ax, band)
        right_labels.draw(ax, band)
        buf = BytesIO()
        fig.savefig(buf, format="png", dpi=DPI, metadata={"Software": None}, facecolor="white",
                    transparent=False)
        return buf.getvalue()


def _band(low: np.ndarray, high: np.ndarray) -> tuple[float, float]:
    lo, hi = float(np.nanmin(low)), float(np.nanmax(high))
    span = hi - lo
    pad = span * BAND_PAD if span > 0 else max(abs(hi) * 1e-3, 1e-6)
    return lo - pad, hi + pad


def _within(p: float, band: tuple[float, float]) -> bool:
    return band[0] <= p <= band[1]


def _fmt(p: float) -> str:
    a = abs(p)
    return f"{p:,.2f}" if a >= 100 else f"{p:.4f}" if a >= 1 else f"{p:.6g}"


class _Labels:
    """Line labels on one edge of the price panel, spread apart so that close levels stay readable: each label sits
    at its level when there is room, otherwise it is nudged by the minimum gap (the line itself stays exact)."""

    def __init__(self, x: float, ha: str, gap: float) -> None:
        self.x, self.ha, self.gap = x, ha, gap
        self.items: list[tuple[float, str, str]] = []

    def add(self, y: float, text: str, color: str) -> None:
        self.items.append((y, text, color))

    def placed(self, band: tuple[float, float]) -> list[tuple[float, str, str]]:
        items = sorted(self.items, key=lambda it: (it[0], it[1]))
        lo, hi = band[0] + self.gap / 2, band[1] - self.gap / 2
        ys = [min(max(y, lo), hi) for y, _, _ in items]
        for i in range(1, len(ys)):                         # push up …
            ys[i] = max(ys[i], ys[i - 1] + self.gap)
        if ys and ys[-1] > hi:                              # … and back down from the top when it overflowed
            ys[-1] = hi
            for i in range(len(ys) - 2, -1, -1):
                ys[i] = min(ys[i], ys[i + 1] - self.gap)
        return [(y, text, color) for y, (_, text, color) in zip(ys, items)]

    def draw(self, ax, band: tuple[float, float]) -> None:
        for y, text, color in self.placed(band):
            ax.text(self.x, y, text, fontsize=5.8, color=color, va="center", ha=self.ha, zorder=7,
                    bbox={"boxstyle": "square,pad=0.1", "facecolor": "white", "alpha": 0.75, "linewidth": 0})


def _boxes(x: np.ndarray, bottom: np.ndarray, top: np.ndarray, half: float = 0.33) -> np.ndarray:
    """(n, 4, 2) rectangle vertices: one PolyCollection draws all bodies — ``Axes.bar`` makes one patch per bar
    and costs ~4× the whole render."""
    xl, xr = x - half, x + half
    return np.stack([np.column_stack(p) for p in ((xl, bottom), (xl, top), (xr, top), (xr, bottom))], axis=1)


def _candles(ax, x, o, h, lo_, c) -> None:
    from matplotlib.collections import PolyCollection
    colors = np.where(c >= o, UP, DOWN)
    body_lo = np.minimum(o, c)
    min_body = max(float(np.nanmax(h) - np.nanmin(lo_)), 1e-9) * 0.0015      # a doji stays visible
    ax.vlines(x, lo_, h, colors=colors, linewidth=0.8, zorder=2)
    ax.add_collection(PolyCollection(_boxes(x, body_lo, body_lo + np.maximum(np.abs(c - o), min_body)),
                                     facecolors=colors, edgecolors=colors, linewidths=0.3, zorder=3),
                      autolim=False)


def _hollow(ax, x: float, f: dict) -> None:
    """The bar in progress: an unfilled body and a wick in the direction colour."""
    from matplotlib.patches import Rectangle
    color = UP if f["close"] >= f["open"] else DOWN
    ax.vlines(x, f["low"], f["high"], colors=color, linewidth=0.8, zorder=2)
    body = abs(f["close"] - f["open"])
    ax.add_patch(Rectangle((x - 0.33, min(f["open"], f["close"])), 0.66, body, fill=False, edgecolor=color,
                           linewidth=0.8, zorder=3))


def _volume(axv, x, o, c, v, label: str) -> None:
    from matplotlib.collections import PolyCollection
    vol = np.nan_to_num(v.astype(float))
    colors = np.where(c >= o, UP, DOWN)
    axv.add_collection(PolyCollection(_boxes(x, np.zeros(len(vol)), vol), facecolors=colors, alpha=0.7,
                                      linewidths=0), autolim=False)
    axv.text(0.004, 0.93, label, transform=axv.transAxes, fontsize=6.5, va="top", ha="left", color="#455a64")
    axv.set_ylim(0, max(float(vol.max()) if len(vol) else 0.0, 1e-9) * 1.1)


def _style(ax, axv, t: np.ndarray, n: int) -> None:
    for a in (ax, axv):
        a.set_facecolor("white")
        a.grid(True, color="#eceff1", linewidth=0.6, zorder=0)
        a.yaxis.tick_right()
        a.tick_params(labelsize=6.5, length=2, pad=1.5)
        for s in a.spines.values():
            s.set_color("#b0bec5")
            s.set_linewidth(0.6)
    ax.ticklabel_format(axis="y", style="plain", useOffset=False)
    axv.ticklabel_format(axis="y", style="plain", useOffset=False)
    axv.set_yticks([])
    ax.tick_params(labelbottom=False)
    idx = sorted({int(round(i)) for i in np.linspace(0, n - 1, min(6, n))})
    axv.set_xticks(idx)
    labels = axv.set_xticklabels([from_ms(int(t[i])).strftime("%m-%d %H:%M") for i in idx])
    labels[0].set_horizontalalignment("left")               # the edge labels stay inside the image
    if len(labels) > 1:
        labels[-1].set_horizontalalignment("right")


def _levels(ax, levels: list[dict], band, labels: _Labels) -> None:
    for lv in levels:
        p = lv["price"]
        if _within(p, band):
            ax.axhline(p, color=LEVEL_COLOR, linewidth=0.7, alpha=0.9, zorder=1)
            labels.add(p, f"{lv['name']} {_fmt(p)}", LEVEL_COLOR)


def _bar_index(t: np.ndarray, ms: int | None) -> int | None:
    """Index of the bar containing ``ms`` (None when it is before the first bar drawn or unknown)."""
    if ms is None or ms < int(t[0]):
        return None
    return int(np.searchsorted(t, ms, side="right")) - 1


def _zones(ax, zones: list[dict], t: np.ndarray, band, right: float) -> None:
    for z in zones:
        top, bottom = min(z["top"], band[1]), max(z["bottom"], band[0])
        if top <= bottom:
            continue                                        # entirely outside the visible band
        i = _bar_index(t, z.get("formed_ms"))
        x0 = -1.0 if i is None else i - 0.5                 # older than the window → from the left edge
        color = ZONE_COLORS.get((z["kind"], z["dir"]), "#9e9e9e")
        ax.fill_between([x0, right], bottom, top, color=color, alpha=0.14, linewidth=0, zorder=1)
        tag = f"{'OB' if z['kind'] == 'ob' else 'FVG'} {'bull' if z['dir'] == 'bullish' else 'bear'}"
        ax.text(max(x0, 0.0) + 0.3, top, tag, fontsize=5.5, color=color, va="top", ha="left", zorder=6, clip_on=True)


def _liquidity(ax, pools: list[dict], band, labels: _Labels) -> None:
    for p in pools:
        lvl = p["level"]
        if not _within(lvl, band):
            continue
        color = LIQ_BUY if p["side"] == "buy" else LIQ_SELL
        ax.axhline(lvl, color=color, linewidth=0.8, linestyle=(0, (4, 3)), alpha=0.85, zorder=2)
        labels.add(lvl, f"{'BSL' if p['side'] == 'buy' else 'SSL'} {_fmt(lvl)} x{p['touches']}", color)


def _structure(ax, events: list[dict], t: np.ndarray, band) -> None:
    for e in events:
        i = _bar_index(t, e["time_ms"])
        if i is None or not _within(e["level"], band):
            continue
        color = BULL if e["dir"] == "bullish" else BEAR
        ax.plot([i], [e["level"]], marker=EVENT_MARKERS.get(e["kind"], "o"), markersize=4.5, color=color,
                linestyle="none", zorder=5)
        ax.hlines(e["level"], i - 0.5, i + 3.5, colors=color, linewidth=0.8, zorder=4)
        ax.text(i + 0.6, e["level"], e["kind"], fontsize=5.8, color=color, va="bottom" if e["dir"] == "bullish"
                else "top", ha="left", zorder=6, clip_on=True)


def _emas(ax, close_all: np.ndarray, warmup: int, x: np.ndarray, periods: list[int], labels: _Labels) -> None:
    """EMAs over all loaded closes (the warm-up bars are not drawn), named at their last value on the right edge."""
    for p in periods:
        e = ind.ema(close_all.astype(float), int(p))[warmup:]
        if np.isfinite(e[-1:]).all():
            color = EMA_COLORS.get(int(p), "#607d8b")
            ax.plot(x, e, color=color, linewidth=1.0, zorder=4)
            labels.add(float(e[-1]), f"EMA{p} {_fmt(float(e[-1]))}", color)


def _holdings(ax, holdings: list[dict], band, labels: _Labels) -> None:
    for hd in holdings:
        pos = hd["kind"] == "position"
        name = hd["id"] + ("" if pos else f" {hd['order_type'].lower() or 'order'}")
        lines = [("entry", hd["entry"], ENTRY_COLOR, "-")]
        if hd.get("sl") is not None:
            lines.append(("SL", hd["sl"], SL_COLOR, "--"))
        lines += [(f"TP{k}", tp, TP_COLOR, "--") for k, tp in enumerate(hd.get("tps") or [], 1)]
        for what, p, color, ls in lines:
            text = f"{name} {what} {_fmt(p)}"
            if _within(p, band):
                ax.axhline(p, color=color, linewidth=1.1 if what == "entry" else 0.9, linestyle=ls,
                           alpha=1.0 if pos else 0.6, zorder=5)
                labels.add(p, text, color)
            else:                                           # off-chart: listed at the nearer edge with an arrow
                above = p > band[1]
                labels.add(band[1] if above else band[0], ("↑ " if above else "↓ ") + text, color)


def png_size(png: bytes) -> tuple[int, int]:
    """(width, height) from the PNG IHDR chunk."""
    if len(png) < 24 or png[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    w, h = struct.unpack(">II", png[16:24])
    return int(w), int(h)


# ---------------------------------------------------------------------------------------------------------- renderer
class ChartRenderer:
    """Renders the configured timeframes and caches each image until its last closed candle or its overlays change
    (a 1w chart is re-drawn once a week, not every 5 minutes). The cache is keyed by instrument and timeframe, so one
    renderer can serve an engine that runs several pairs."""

    def __init__(self, cfg: ChartsCfg, out_dir: Path | None = None) -> None:
        self.cfg = cfg
        self.out_dir = Path(out_dir) if out_dir is not None else None
        self._cache: dict[tuple[str, str], tuple[int, str, ChartImage]] = {}   # (inst, tf) → (last open, key, img)
        self._logged: set[tuple[str, str, str]] = set()  # (inst, tf, reason) logged — one line, not one per cycle
        self._lock = threading.Lock()
        self.renders = 0                                # images actually drawn (not served from the cache)
        self._first_set_logged = False

    def render_set(self, reader: InstrumentReader, inst: Instrument, as_of: int, payload: dict,
                   calendar: SessionCalendar | None = None) -> list[ChartImage]:
        """The charts of ``cfg.timeframes`` in order; a timeframe that cannot be drawn is logged and left out (this
        never raises: a call without one chart is better than no call)."""
        payload = payload if isinstance(payload, dict) else {}
        rss0 = _rss() if not self._first_set_logged else 0
        out: list[ChartImage] = []
        with self._lock:
            for tf in self.cfg.timeframes:
                try:
                    img = self._one(reader, inst, tf, as_of, payload, calendar)
                except Exception as exc:  # noqa: BLE001 — one bad timeframe must not cost the others
                    self._once((inst.key, tf), f"error:{type(exc).__name__}", logging.WARNING,
                               "charts %s %s: not rendered (%s: %s)", inst.key, tf, type(exc).__name__, exc)
                    continue
                if img is not None:
                    out.append(img)
            if not self._first_set_logged and out:
                self._first_set_logged = True
                log.info("charts %s: first set of %d images rendered, process RSS %+.1f MB", inst.key, len(out),
                         (_rss() - rss0) / 1e6)
        return out

    def _one(self, reader: InstrumentReader, inst: Instrument, tf: str, as_of: int, payload: dict,
             calendar: SessionCalendar | None) -> ChartImage | None:
        tfo, where = Timeframe.parse(tf), (inst.key, tf)
        if tfo not in inst.timeframes:
            self._once(where, "no_table", logging.INFO, "charts %s %s: the instrument stores no %s candles — skipped",
                       inst.key, tf, tf)
            return None
        bars = int(self.cfg.bars[tf])
        spec = overlay_spec(payload, tf, list(self.cfg.overlays))
        extra = EMA_WARMUP if "ema" in spec else 0
        fr = load_frame(reader, inst, tfo, bars + extra, as_of, calendar)
        drawn = min(len(fr), bars)
        if drawn < MIN_CHART_BARS:
            self._once(where, "short", logging.INFO, "charts %s %s: %d closed bars (< %d) — skipped", inst.key, tf,
                       drawn, MIN_CHART_BARS)
            return None
        self._logged -= {k for k in self._logged if k[:2] == where and k[2] != "write"}   # recovered: log a relapse
        last_open = int(fr.open_time[-1])
        # the 15m / 1h / ... charts are keyed by closed bars alone; only the 5m chart also follows its bar in progress
        fb = forming_bar(fr) if tf in FORMING_CHART_TFS else None
        key = spec_hash({"spec": spec, "bars": drawn, "first": int(fr.open_time[-drawn]), "w": self.cfg.width,
                         "h": self.cfg.height, **({"forming": fb} if fb else {})})
        hit = self._cache.get(where)
        if hit is not None and hit[0] == last_open and hit[1] == key:
            return hit[2]
        title = (f"{inst.key} {tf} — as of {iso(last_open + tfo.ms)} (closed candles, analysis prices)")
        png = render_png(fr, tf, spec, width=self.cfg.width, height=self.cfg.height, title=title,
                         warmup=len(fr) - drawn, forming=fb)
        self.renders += 1
        w, h = png_size(png)
        img = ChartImage(tf=tf, png=png, bars=drawn, width=w, height=h, token_est=token_estimate(w, h))
        self._cache[where] = (last_open, key, img)
        self._write(where, png)
        return img

    def _write(self, where: tuple[str, str], png: bytes) -> None:
        """Latest image for the dashboard (``<out_dir>/<tf>.png``), replaced atomically; a failure is only logged."""
        if self.out_dir is None:
            return
        tf = where[1]
        try:
            self.out_dir.mkdir(parents=True, exist_ok=True)
            tmp = self.out_dir / f".{tf}.png.tmp"
            tmp.write_bytes(png)
            os.replace(tmp, self.out_dir / f"{tf}.png")
        except OSError as exc:      # e.g. the dashboard holds the file open on Windows
            self._once(where, "write", logging.WARNING, "charts: cannot write %s/%s.png (%s)", self.out_dir, tf, exc)

    def _once(self, where: tuple[str, str], reason: str, level: int, msg: str, *args: Any) -> None:
        if (*where, reason) not in self._logged:
            self._logged.add((*where, reason))
            log.log(level, msg, *args)


def _rss() -> int:
    try:
        import psutil
        return int(psutil.Process().memory_info().rss)
    except Exception:  # noqa: BLE001 — a missing measurement never blocks a render
        return 0


__all__ = ["ChartImage", "ChartRenderer", "overlay_spec", "png_size", "render_png", "spec_hash", "token_estimate"]
