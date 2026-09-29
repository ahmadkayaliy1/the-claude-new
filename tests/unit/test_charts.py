"""Candle charts for the model (Phase 3, handoff §3.7.2): real BTCUSDT 1m candles (+ a 5m resample) and the real
XAUUSD payload; overlays for the BTC charts come from the real structure/zone analysis of those candles."""
from __future__ import annotations

import copy
import csv
import datetime as dt
import gc
import io
import json
from pathlib import Path

import numpy as np
import psutil
import pytest
from PIL import Image

from tradingsystem.analysis import charts
from tradingsystem.analysis import indicators as ind
from tradingsystem.analysis.charts import ChartImage, ChartRenderer, overlay_spec, render_png, token_estimate
from tradingsystem.analysis.frames import Frame
from tradingsystem.analysis.structure import analyze_structure
from tradingsystem.analysis.zones import fair_value_gaps, nearest_active, order_blocks, update_mitigation
from tradingsystem.core.instruments import Instrument
from tradingsystem.core.settings import ChartsCfg
from tradingsystem.core.timeframes import Timeframe
from tradingsystem.core.timeutil import MS_PER_MINUTE, iso, to_ms
from tradingsystem.storage.tablespec import spec_for

REAL = Path(__file__).resolve().parents[1] / "fixtures" / "real"
M1, M5, H1, D1 = (Timeframe.parse(t) for t in ("1m", "5m", "1h", "1d"))
W, H = 720, 400
KEY = "binance_spot:BTCUSDT"


# ------------------------------------------------------------------------------------------------------------ data
@pytest.fixture(scope="module")
def cols() -> dict[str, np.ndarray]:
    with open(REAL / "btcusdt_candles_1m_3000.csv", newline="") as f:
        rows = list(csv.DictReader(f))
    out = {k: np.array([float(r[k]) for r in rows]) for k in ("open", "high", "low", "close", "volume",
                                                              "taker_buy_base")}
    out["open_time"] = np.array([int(r["open_time"]) for r in rows], dtype=np.int64)
    return out


def resample(c: dict[str, np.ndarray], tf: Timeframe) -> dict[str, np.ndarray]:
    """OHLCV of ``tf`` from the 1m rows (first open, max high, min low, last close, summed volumes)."""
    if tf is M1:
        return c
    key = np.array([tf.floor(int(t)) for t in c["open_time"]], dtype=np.int64)
    starts = np.r_[0, np.nonzero(np.diff(key))[0] + 1]
    ends = np.r_[starts[1:], len(key)]
    return {"open_time": key[starts], "open": c["open"][starts], "close": c["close"][ends - 1],
            "high": np.maximum.reduceat(c["high"], starts), "low": np.minimum.reduceat(c["low"], starts),
            "volume": np.add.reduceat(c["volume"], starts), "taker_buy_base": np.add.reduceat(c["taker_buy_base"],
                                                                                              starts)}


def make_frame(c: dict[str, np.ndarray], tf: Timeframe, last: int, volume_kind: str = "traded") -> Frame:
    r = resample(c, tf)
    s = slice(-last, None)
    return Frame(KEY, tf, r["open_time"][s], r["open"][s], r["high"][s], r["low"][s], r["close"][s],
                 r["volume"][s], volume_kind, taker_buy=r["taker_buy_base"][s])


@pytest.fixture(scope="module")
def f1m(cols) -> Frame:
    return make_frame(cols, M1, 120)


@pytest.fixture(scope="module")
def f5m(cols) -> Frame:
    return make_frame(cols, M5, 96)


@pytest.fixture(scope="module")
def xau() -> dict:
    return json.loads((REAL / "payload_xauusd.json").read_text(encoding="utf-8"))


def real_payload(fr: Frame, tf: str) -> dict:
    """A payload in the snapshot's shape (``SnapshotBuilder._analyze_tf`` zones/liquidity/structure blocks) built from
    the real analysis of ``fr``, plus reference levels from its own range and one held position."""
    o, h, l, c = fr.open, fr.high, fr.low, fr.close
    atr = ind.atr(h, l, c)
    st = analyze_structure(h, l, c, atr)
    price = float(c[-1])
    t = lambda i: iso(int(fr.open_time[i]))  # noqa: E731

    def zone(z):
        return {"dir": z.direction, "top": round(z.top, 2), "bottom": round(z.bottom, 2), "formed": t(z.origin_idx)}

    fvg = update_mitigation(fair_value_gaps(h, l, c, atr), h, l, c)
    obs = update_mitigation(order_blocks(o, h, l, c, atr, st), h, l, c)
    pools = [p for p in st.pools if p.swept_idx is None]
    block = {
        "structure": {"events": [{"time": t(e.idx), "kind": e.kind, "dir": e.direction, "level": round(e.level, 2)}
                                 for e in st.events[-6:]]},
        "zones": {"fvg": [zone(z) for z in nearest_active(fvg, price)],
                  "order_blocks": [zone(z) for z in nearest_active(obs, price)]},
        "liquidity": {
            "buy_side_above": [{"level": round(p.level, 2), "touches": len(p.touches)}
                               for p in pools if p.side == "buy_side" and p.level > price][:3],
            "sell_side_below": [{"level": round(p.level, 2), "touches": len(p.touches)}
                                for p in pools if p.side == "sell_side" and p.level < price][:3]},
    }
    hi, lo = float(h.max()), float(l.min())
    basis = 12.5
    return {"levels": {"range_high": round(hi, 2), "range_low": round(lo, 2), "far_away": round(hi * 2, 2)},
            "timeframes": {tf: block},
            "market": {"basis_exec_minus_analysis": basis},
            "account": {"open_positions": [{"decision": "abc12345ffff", "side": "BUY", "order_type": "MARKET",
                                            "volume": 0.01, "price": round(price + basis, 2),
                                            "sl": round(lo + basis, 2), "tps": [round(hi + basis, 2)]}],
                        "pending_orders": []}}


ALL = ["levels", "zones", "liquidity", "structure", "holdings", "ema"]


def png(fr: Frame, spec: dict | None = None, title: str = "t") -> bytes:
    return render_png(fr, fr.tf.value, spec or {}, width=W, height=H, title=title)


# ------------------------------------------------------------------------------------------------------ rendering
def test_png_is_720x400_opaque_and_decodes(f1m, f5m):
    for fr in (f1m, f5m):
        data = png(fr)
        img = Image.open(io.BytesIO(data))
        img.load()
        assert img.format == "PNG" and img.size == (W, H)
        if "A" in img.getbands():
            assert img.getchannel("A").getextrema() == (255, 255)          # no transparency
        assert charts.png_size(data) == (W, H)
        assert len(np.unique(np.asarray(img.convert("RGB")).reshape(-1, 3), axis=0)) > 10   # drawn


def test_identical_inputs_give_identical_bytes(f5m):
    spec = overlay_spec(real_payload(f5m, "5m"), "5m", ALL)
    assert png(f5m, spec, "same") == png(f5m, spec, "same")


def test_overlays_change_the_image(f5m):
    payload = real_payload(f5m, "5m")
    spec = overlay_spec(payload, "5m", ALL)
    assert spec["structure"] and spec["holdings"] and spec["levels"]      # the real analysis produced overlays
    base = png(f5m)
    assert png(f5m, spec) != base
    for name in ALL:                                                      # each overlay alone draws something
        one = overlay_spec(payload, "5m", [name])
        if any(one.values()):
            assert png(f5m, one) != base, name


def test_tick_volume_panel_title_differs(f5m):
    tick = Frame(**{**f5m.__dict__, "volume_kind": "tick"})
    assert png(tick) != png(f5m)


def test_off_chart_holdings_and_degenerate_bars_do_not_fail(f1m):
    spec = {"holdings": [{"id": "x", "kind": "order", "side": "SELL", "order_type": "LIMIT", "entry": 1e9,
                          "sl": -5.0, "tps": [0.5]}], "zones": [{"kind": "ob", "dir": "bullish", "top": 1e9,
                                                                  "bottom": 1e8, "formed_ms": None}],
            "structure": [{"time_ms": 0, "kind": "BOS", "dir": "bullish", "level": 1.0}], "ema": [20, 50]}
    assert charts.png_size(png(f1m, spec)) == (W, H)
    flat = Frame(**{**f1m.__dict__, **{k: np.full(len(f1m), 100.0) for k in ("open", "high", "low", "close")}})
    assert charts.png_size(png(flat, spec)) == (W, H)                     # zero price range


def test_token_estimate():
    assert token_estimate(720, 400) == 384
    assert token_estimate(1, 1) == 1


def test_thirty_renders_keep_rss_flat_and_leave_no_figures(f5m):
    from matplotlib import _pylab_helpers
    spec = overlay_spec(real_payload(f5m, "5m"), "5m", ALL)
    png(f5m, spec)                                                        # warm-up: imports, fonts, caches
    gc.collect()
    proc = psutil.Process()
    before = proc.memory_info().rss
    for i in range(30):
        png(f5m, spec, f"render {i}")
    gc.collect()
    grown = proc.memory_info().rss - before
    assert grown < 20 * 1024 * 1024, f"RSS grew {grown / 1e6:.1f} MB over 30 renders"
    assert _pylab_helpers.Gcf.get_num_fig_managers() == 0


def test_as_input_label_and_tokens(f5m):
    data = png(f5m)
    img = ChartImage(tf="5m", png=data, bars=96, width=W, height=H, token_est=token_estimate(W, H))
    inp = img.as_input("XAUUSD", "mt5:XAUUSD@")
    assert inp.label == "Chart 5m — XAUUSD mt5:XAUUSD@, last 96 closed candles, analysis prices"
    assert inp.data == data and inp.media_type == "image/png" and inp.token_est == 384


# --------------------------------------------------------------------------------------------------- overlay spec
def test_overlay_spec_reads_the_real_payload(xau):
    s = overlay_spec(xau, "15m", ALL)
    assert {x["name"] for x in s["levels"]} == {"pdh", "pdl", "pdc", "day_open", "week_open", "asia_high",
                                                "asia_low"}
    assert [z["kind"] for z in s["zones"]] == ["ob"] * 3 + ["fvg"] * 3
    ob = s["zones"][0]
    assert ob == {"kind": "ob", "dir": "bearish", "top": 4318.75, "bottom": 4308.88,
                  "formed_ms": to_ms(dt.datetime(2026, 9, 23, 12, tzinfo=dt.timezone.utc))}
    assert [(p["side"], p["level"], p["touches"]) for p in s["liquidity"]][:2] == [("buy", 4313.44, 1),
                                                                                  ("buy", 4318.89, 2)]
    assert len(s["liquidity"]) == 6
    assert len(s["structure"]) == 6 and s["structure"][-1]["kind"] == "BOS"
    assert s["structure"][-1]["time_ms"] == to_ms(dt.datetime(2026, 9, 25, 10, tzinfo=dt.timezone.utc))
    assert s["ema"] == [20, 50] and s["holdings"] == []
    json.dumps(s)                                                         # hashable for the cache


def test_overlay_spec_only_requested_and_missing_blocks(xau):
    assert set(overlay_spec(xau, "15m", ["levels"])) == {"levels"}
    s = overlay_spec(xau, "1w", ALL)                                      # 1w: "insufficient history" block
    assert s["zones"] == [] and s["liquidity"] == [] and s["structure"] == []
    # the weekly / daily charts draw only their own horizon's levels; 4h and lower draw all seven
    assert [lv["name"] for lv in s["levels"]] == ["week_open"]
    assert [lv["name"] for lv in overlay_spec(xau, "1d", ["levels"])["levels"]] == ["pdh", "pdl", "pdc", "week_open"]
    assert len(overlay_spec(xau, "4h", ["levels"])["levels"]) == 7
    assert overlay_spec({}, "5m", ALL) == {"levels": [], "zones": [], "liquidity": [], "structure": [],
                                           "ema": [20, 50], "holdings": []}
    junk = {"levels": {"a": None, "b": True, "c": "1", "d": float("nan"), "e": 5}, "timeframes": [1, 2],
            "account": {"open_positions": [None, {"price": None}, {"decision": "d1", "price": 1.0, "sl": "x"}]},
            "market": {"basis_exec_minus_analysis": None}}
    s = overlay_spec(junk, "5m", ALL)
    assert s["levels"] == [{"name": "e", "price": 5.0}]
    assert s["holdings"] == [{"id": "d1", "kind": "position", "side": "", "order_type": "", "entry": 1.0,
                              "sl": None, "tps": []}]


def test_overlay_spec_translates_holdings_back_by_the_basis(xau):
    p = copy.deepcopy(xau)
    p["market"]["basis_exec_minus_analysis"] = 12.5
    p["account"]["open_positions"] = [{"decision": "0123456789ab", "side": "BUY", "order_type": "MARKET",
                                       "volume": 0.01, "price": 4318.5, "sl": 4300.0, "tps": [4330.0, 4345.5]}]
    p["account"]["pending_orders"] = [{"decision": "feedbeef", "side": "SELL", "order_type": "LIMIT",
                                       "volume": 0.01, "price": 4340.0, "sl": 4350.0, "tps": [4320.0]}]
    hs = overlay_spec(p, "15m", ["holdings"])["holdings"]
    assert hs == [{"id": "01234567", "kind": "position", "side": "BUY", "order_type": "MARKET", "entry": 4306.0,
                   "sl": 4287.5, "tps": [4317.5, 4333.0]},
                  {"id": "feedbeef", "kind": "order", "side": "SELL", "order_type": "LIMIT", "entry": 4327.5,
                   "sl": 4337.5, "tps": [4307.5]}]
    del p["market"]["basis_exec_minus_analysis"]                          # same instrument → no basis
    assert overlay_spec(p, "15m", ["holdings"])["holdings"][0]["entry"] == 4318.5


# ------------------------------------------------------------------------------------------------------- renderer
class FakeReader:
    """Serves the real 1m fixture (resampled per timeframe) as the store would: rows with open_time in range."""

    def __init__(self, c: dict[str, np.ndarray], inst: Instrument, fail: tuple[str, ...] = ()) -> None:
        self.c, self.inst, self.fail = c, inst, fail
        self.hot = None
        self.reads = 0

    def read_range(self, spec, start_ms=None, end_ms=None, columns=None):
        self.reads += 1
        tf = spec.timeframe
        if tf.value in self.fail:
            raise OSError(f"disk error on {spec.name}")
        r = resample(self.c, tf)
        m = (r["open_time"] >= (start_ms or 0)) & (r["open_time"] <= (end_ms if end_ms is not None else 2**62))
        return {k: v[m] for k, v in r.items()}


@pytest.fixture()
def inst() -> Instrument:
    return Instrument(pair="BTCUSDT", venue="binance_spot", symbol="BTCUSDT", roles=("primary", "execution"),
                      datatypes=("candles",), timeframes=(M1, M5, H1, D1))


def cfg(**kw) -> ChartsCfg:
    base = {"timeframes": ["1d", "1h", "5m"], "bars": {"1d": 20, "1h": 120, "5m": 96}}
    return ChartsCfg(**{**base, **kw})


def counting(monkeypatch) -> list[dict]:
    calls: list[dict] = []
    real = charts.render_png

    def wrapper(frame, tf, spec, **kw):
        calls.append({"tf": tf, "bars": len(frame), "volume_kind": frame.volume_kind, **kw})
        return real(frame, tf, spec, **kw)

    monkeypatch.setattr(charts, "render_png", wrapper)
    return calls


def test_render_set_caches_skips_short_timeframes_and_writes_files(monkeypatch, cols, inst, tmp_path, caplog):
    assert spec_for(inst, "candles", M5)                                  # the instrument has the tables
    calls = counting(monkeypatch)
    end = int(cols["open_time"][-1]) + MS_PER_MINUTE
    as_of = H1.floor(end) - 40 * MS_PER_MINUTE + 1_000                    # :20:01 — and +5 min stays in the hour
    reader = FakeReader(cols, inst)
    payload = real_payload(make_frame(cols, M5, 300), "5m")
    r = ChartRenderer(cfg(), out_dir=tmp_path / "charts")
    with caplog.at_level("INFO", logger="tradingsystem.analysis.charts"):
        first = r.render_set(reader, inst, as_of, payload)
        r.render_set(reader, inst, as_of, payload)
    assert [i.tf for i in first] == ["1h", "5m"]                          # 1d: 3 bars → skipped
    assert sum("1d" in m and "skipped" in m for m in caplog.messages) == 1   # logged once, not per call
    assert len(calls) == 2 and r.renders == 2
    for img in first:
        assert (img.width, img.height, img.token_est) == (W, H, 384)
        assert (tmp_path / "charts" / f"{img.tf}.png").read_bytes() == img.png
    h1 = first[0]
    assert 40 <= h1.bars < 120 and calls[0]["bars"] == h1.bars and calls[0]["warmup"] == 0   # all the fixture has
    assert calls[1]["title"] == (f"{KEY} 5m — as of {iso(as_of - 1_000)} "        # the last close drawn
                                 "(closed candles, analysis prices)")
    assert calls[1]["warmup"] == charts.EMA_WARMUP and calls[1]["bars"] == 96 + charts.EMA_WARMUP

    again = r.render_set(reader, inst, as_of, payload)
    assert len(calls) == 2 and [a.png for a in again] == [b.png for b in first]   # served from the cache

    later = r.render_set(reader, inst, as_of + 5 * MS_PER_MINUTE, payload)  # a new 5m bar, same 1h bar
    assert [c["tf"] for c in calls[2:]] == ["5m"] and later[1].png != first[1].png

    moved = copy.deepcopy(payload)
    moved["account"]["open_positions"][0]["sl"] -= 50                      # overlay change → both redrawn
    r.render_set(reader, inst, as_of + 5 * MS_PER_MINUTE, moved)
    assert [c["tf"] for c in calls[3:]] == ["1h", "5m"]


def test_render_set_isolates_a_failing_timeframe(cols, inst, caplog):
    as_of = int(cols["open_time"][-1]) + MS_PER_MINUTE
    r = ChartRenderer(cfg(timeframes=["1h", "5m"]))
    with caplog.at_level("WARNING", logger="tradingsystem.analysis.charts"):
        out = r.render_set(FakeReader(cols, inst, fail=("1h",)), inst, as_of, {})
        r.render_set(FakeReader(cols, inst, fail=("1h",)), inst, as_of, {})
    assert [i.tf for i in out] == ["5m"]
    assert sum("1h" in m and "OSError" in m for m in caplog.messages) == 1
    no_table = Instrument(**{**inst.__dict__, "timeframes": (M1, M5)})
    assert [i.tf for i in ChartRenderer(cfg(timeframes=["1h", "5m"])).render_set(
        FakeReader(cols, no_table), no_table, as_of, {})] == ["5m"]


def test_render_set_without_ema_loads_only_the_drawn_bars(monkeypatch, cols, inst):
    calls = counting(monkeypatch)
    as_of = int(cols["open_time"][-1]) + MS_PER_MINUTE
    out = ChartRenderer(cfg(timeframes=["5m"], overlays=["levels"])).render_set(FakeReader(cols, inst), inst,
                                                                                as_of, {})
    assert out[0].bars == 96 and calls[0]["bars"] == 96 and calls[0]["warmup"] == 0


def test_edge_labels_are_spread_apart_inside_the_band():
    lab = charts._Labels(0.0, "right", gap=2.0)
    for y in (50.0, 50.5, 51.0, 99.9, 150.0, -3.0):                      # a cluster, one at the top, two outside
        lab.add(y, f"l{y}", "k")
    ys = [y for y, _, _ in lab.placed((0.0, 100.0))]
    assert all(b - a >= 2.0 - 1e-9 for a, b in zip(ys, ys[1:]))
    assert 1.0 - 1e-9 <= ys[0] and ys[-1] <= 99.0 + 1e-9
    assert [t for _, t, _ in lab.placed((0.0, 100.0))] == ["l-3.0", "l50.0", "l50.5", "l51.0", "l99.9", "l150.0"]


def test_importing_the_module_does_not_load_matplotlib():
    """``ai.charts.enabled: false`` must not cost the engine matplotlib's RAM: it is imported on the first render."""
    import subprocess
    import sys
    src = Path(__file__).resolve().parents[2] / "src"
    code = ("import sys; sys.path.insert(0, sys.argv[1]); import tradingsystem.analysis.charts; "
            "print(sorted(m for m in sys.modules if m.split('.')[0] in ('matplotlib', 'PIL')))")
    out = subprocess.run([sys.executable, "-c", code, str(src)], capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "[]"


def test_one_renderer_keeps_instruments_apart(monkeypatch, cols, inst):
    """A legacy multi-pair engine shares one renderer: identical bar times and empty overlays must not make the
    second pair reuse the first pair's image."""
    calls = counting(monkeypatch)
    other = Instrument(**{**inst.__dict__, "pair": "ETHUSDT", "symbol": "ETHUSDT"})
    as_of = int(cols["open_time"][-1]) + MS_PER_MINUTE
    r = ChartRenderer(cfg(timeframes=["5m"], overlays=[]))
    a = r.render_set(FakeReader(cols, inst), inst, as_of, {})
    b = r.render_set(FakeReader(cols, other), other, as_of, {})
    assert len(calls) == 2 and a[0].png != b[0].png
    assert calls[1]["title"].startswith("binance_spot:ETHUSDT 5m")
    r.render_set(FakeReader(cols, inst), inst, as_of, {})
    assert len(calls) == 2                                                # each pair still hits its own cache


def test_mt5_instrument_renders_with_tick_volume(monkeypatch, cols):
    """The MT5 path (XAU): tick-volume column, no taker volume; the frame says ``tick`` and the chart is drawn."""
    calls = counting(monkeypatch)
    mt5 = Instrument(pair="XAUUSD", venue="mt5", symbol="XAUUSD@", roles=("primary", "execution"),
                     datatypes=("candles",), timeframes=(M1, M5, H1))

    class MT5Reader(FakeReader):
        def read_range(self, spec, start_ms=None, end_ms=None, columns=None):
            r = super().read_range(spec, start_ms, end_ms, columns)
            return {"open_time": r["open_time"], "open": r["open"], "high": r["high"], "low": r["low"],
                    "close": r["close"], "tick_volume": np.round(r["volume"] * 100)}

    as_of = int(cols["open_time"][-1]) + MS_PER_MINUTE
    out = ChartRenderer(cfg(timeframes=["5m"])).render_set(MT5Reader(cols, mt5), mt5, as_of, {})
    assert [i.tf for i in out] == ["5m"] and [c["volume_kind"] for c in calls] == ["tick"]


# ------------------------------------------------------------------- Phase 5: period levels and the forming bar
def test_period_levels_are_drawn_on_the_daily_and_4h_charts_only(xau):
    """B2: pwh/pwl/pmh/pml/month_open/year_open — 1d and 4h only (not 1w, 1h, 15m, 5m)."""
    p = copy.deepcopy(xau)
    six = {"pwh": 4400.0, "pwl": 4200.0, "pmh": 4500.0, "pml": 4100.0, "month_open": 4300.0, "year_open": 3900.0}
    p["levels"].update(six)
    names = lambda tf: {x["name"] for x in overlay_spec(p, tf, ["levels"])["levels"]}  # noqa: E731
    assert set(six) <= names("1d") and set(six) <= names("4h")
    for tf in ("1w", "1h", "15m", "5m"):
        assert not set(six) & names(tf), tf
    assert names("1w") == {"week_open"}                                      # the weekly chart: its own horizon
    assert {"pdh", "pdl", "pdc", "day_open"} <= names("4h") and "day_open" not in names("1d")
    assert names("15m") == set(p["levels"]) - set(six) and names("5m") == names("15m") == names("1h")


class FakeForming:
    """The hot store's FORMING table as ``frames._forming`` reads it (rows of the bar in progress per timeframe)."""

    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows

    def read_range(self, spec, columns=None):
        return {k: np.array([r[k] for r in self.rows], dtype=object if k == "tf" else float)
                for k in self.rows[0]} if self.rows else {}


def forming_reader(cols, inst, as_of: int, close_delta: float = 1.5, tfs=("5m", "1h")) -> FakeReader:
    """A reader whose store holds a forming row for each ``tfs`` bar containing ``as_of`` (hand-built values around the
    last real close: a bar 5 s old, written 1 s after as_of)."""
    r = FakeReader(cols, inst)
    last = float(cols["close"][-1])
    r.hot = FakeForming([{"tf": t, "open_time": float(Timeframe.parse(t).floor(as_of)), "open": last,
                          "high": last + 40.0, "low": last - 25.0, "close": last + close_delta, "volume": 0.5,
                          "updated_ms": float(as_of + 1_000)} for t in tfs])
    return r


def forming_payload(cols, as_of: int, close_delta: float = 1.5, tfs=("5m", "1h")) -> dict:
    """The payload's ``timeframes.<tf>.forming`` rows for the bars containing ``as_of`` (hand-built values around the
    last real close) - what the model reads, and what the chart must draw."""
    last = float(cols["close"][-1])
    return {"timeframes": {t: {"forming": [iso(Timeframe.parse(t).floor(as_of)), last, last + 40.0, last - 25.0,
                                           last + close_delta, 0.5, 5]} for t in tfs}}


def test_the_5m_chart_draws_the_forming_bar_and_only_the_5m_chart(monkeypatch, cols, inst):
    """B3: a hollow candle after the last closed bar on the 5m chart, taken from the payload the model reads (never a
    second read of the live table - review fix); the 1h chart is byte-identical with or without one and keeps its
    cache while the forming bar moves."""
    calls = counting(monkeypatch)
    as_of = M5.floor(int(cols["open_time"][-1]) + MS_PER_MINUTE) + 5_000
    c = cfg(timeframes=["1h", "5m"], overlays=[])
    plain = ChartRenderer(c).render_set(FakeReader(cols, inst), inst, as_of, {})
    r = ChartRenderer(c)
    rich = r.render_set(FakeReader(cols, inst), inst, as_of, forming_payload(cols, as_of))
    assert [i.tf for i in rich] == ["1h", "5m"]
    assert rich[0].png == plain[0].png                                        # 1h: closed bars only
    assert rich[1].png != plain[1].png and calls[-1]["forming"]["close"] > 0   # 5m: the hollow bar is drawn
    assert [k.get("forming") is not None for k in calls if k["tf"] == "1h"] == [False, False]
    assert (rich[1].width, rich[1].height) == (W, H)

    n = len(calls)
    r.render_set(FakeReader(cols, inst), inst, as_of, forming_payload(cols, as_of))   # same values: both cached
    assert len(calls) == n
    moved = r.render_set(FakeReader(cols, inst), inst, as_of, forming_payload(cols, as_of, close_delta=-9.0))
    assert [k["tf"] for k in calls[n:]] == ["5m"]                               # only the 5m chart follows its bar
    assert moved[0].png == plain[0].png and moved[1].png != rich[1].png
    # a payload row of another bar (a replayed past instant) is ignored, and so is the live table's own row
    old = ChartRenderer(c).render_set(FakeReader(cols, inst), inst, as_of,
                                      forming_payload(cols, as_of - 10 * 300_000))
    assert old[1].png == plain[1].png
    live_only = ChartRenderer(c).render_set(forming_reader(cols, inst, as_of), inst, as_of, {})
    assert live_only[1].png == plain[1].png


def test_the_forming_bar_is_drawn_inside_the_price_band_and_leaves_the_last_close_line(cols):
    fr = make_frame(cols, M5, 96)
    fb = {"open_time": int(fr.open_time[-1]) + 300_000, "open": float(fr.close[-1]), "high": float(fr.close[-1]) + 500,
          "low": float(fr.close[-1]) - 5, "close": float(fr.close[-1]) + 400, "volume": None, "age_s": 5.0}
    a = render_png(fr, "5m", {}, width=W, height=H, title="t")
    b = render_png(fr, "5m", {}, width=W, height=H, title="t", forming=fb)
    assert a != b and Image.open(io.BytesIO(b)).size == (W, H)
