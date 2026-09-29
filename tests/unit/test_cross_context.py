"""Phase 5 B17: gold's intermarket context — EURUSD@ (the dollar) and XAGUSD@ (silver) recorded as ``cross_context``
instruments of XAUUSD (candles only) and read by ``analysis/cross.py`` like a sibling.

The XAU and silver bars are REAL (tests/fixtures/real: MT5 XAUUSD@ 15m and XAGUSD@ 15m/1h, the same UTC grid, read-only
from the Windsor demo terminal). EURUSD@ had no history in the terminal, so the EURUSD cases use a series BUILT here from
the real silver bars (inverted, or shifted by days) — each such case says so. The ingest cases replace the terminal by a
fake module (no MT5 needed). Every expected number is recomputed with plain numpy."""
import csv
import json
import time
import types
from pathlib import Path

import numpy as np
import pytest

from tradingsystem.ai.model_view import model_view
from tradingsystem.analysis import cross
from tradingsystem.analysis.cross import CrossContext, extreme_side, neighbours, trend_label
from tradingsystem.analysis.frames import load_frame
from tradingsystem.analysis.registry import capability_matrix
from tradingsystem.analysis.snapshot import SnapshotBuilder
from tradingsystem.core.instruments import InstrumentRegistry
from tradingsystem.core.sessions import calendar_for
from tradingsystem.core.settings import INSTANCE_ENV, InstrumentCfg, load_settings
from tradingsystem.core.timeframes import Timeframe
from tradingsystem.ingest.common.appdb import AppDB
from tradingsystem.ingest.mt5 import backfill as mt5bf
from tradingsystem.ingest.mt5 import service as mt5svc
from tradingsystem.ingest.mt5.servertime import ServerTimeModel
from tradingsystem.ingest.mt5.terminal import MT5Unavailable
from tradingsystem.storage.parquet_store import ParquetColdStore
from tradingsystem.storage.sqlite_store import SQLiteHotStore
from tradingsystem.storage.tablespec import spec_for, system_specs, table_specs

REAL = Path(__file__).resolve().parents[1] / "fixtures" / "real"
M15, H1 = Timeframe.M15, Timeframe.H1
SRV_OFFSET = 3 * 3_600_000                    # Windsor server time = UTC+3 in September (the fixtures' conversion)


def load(name: str) -> list[tuple]:
    with open(REAL / name, newline="") as f:
        return [(int(r["open_time"]), *(float(r[k]) for k in ("open", "high", "low", "close"))) for r in csv.DictReader(f)]


XAG15, XAG1H = load("xagusd_15m_1959.csv"), load("xagusd_1h_490.csv")
XAU15 = [b for b in load("xauusd_15m_9300.csv") if b[0] >= XAG15[0][0]]
END = 1790666700000                            # 2026-09-29 07:25 UTC: every fixture bar is closed


def mt5_rows(bars):
    return [(t, t + SRV_OFFSET, o, h, lo, c, 100, 20, None) for t, o, h, lo, c in bars]


def inverted(bars, k=100.0):
    """BUILT here (not a market series): k / silver — a series perfectly anti-correlated in log returns."""
    return [(t, k / o, k / lo, k / h, k / c) for t, o, h, lo, c in bars]


def shifted(bars, days):
    """BUILT here: the real silver bars moved ``days`` later in time (their moves no longer line up with gold's)."""
    return [(t + days * 86_400_000, o, h, lo, c) for t, o, h, lo, c in bars]


@pytest.fixture
def env(tmp_path):
    s = load_settings(env_path=Path("nope.env"), extra_env={INSTANCE_ENV: "XAUUSD"})
    s = s.model_copy(update={"paths": s.paths.model_copy(update={"data_dir": str(tmp_path / "data")})})
    reg = InstrumentRegistry.from_settings(s)
    by = {i.symbol: i for i in reg.for_pair("XAUUSD")}
    made = []

    def seed(inst, tf, bars):
        with SQLiteHotStore(inst.hot_db_path(s.paths.data())) as st:
            st.ensure_tables([*table_specs(inst), *system_specs()])
            st.upsert(spec_for(inst, "candles", tf), mt5_rows(bars))

    def context() -> CrossContext:
        made.append(CrossContext(s, reg, s.paths.data()))
        return made[-1]

    def frame(as_of):
        prim = reg.primary("XAUUSD")
        b = SnapshotBuilder(s, reg)
        made.append(b)
        return load_frame(b.reader(prim), prim, M15, 320, as_of, calendar_for(prim.venue, prim.symbol, "metal"))

    seed(by["XAUUSD@"], M15, XAU15)
    yield types.SimpleNamespace(s=s, reg=reg, xau=by["XAUUSD@"], eur=by["EURUSD@"], xag=by["XAGUSD@"], seed=seed,
                                context=context, frame=frame, root=s.paths.data())
    for c in made:
        c.close()


def independent(a_bars, b_bars, as_of):
    """corr / beta of a on b over the last 96 15m slots closed by as_of (a return only between consecutive slots)."""
    last = max(t for t, *_ in a_bars if t + M15.ms <= as_of)
    a, b = {t: c for t, *_x, c in a_bars}, {t: c for t, *_x, c in b_bars}
    xs, ys = [], []
    for t in range(last - 95 * M15.ms, last + 1, M15.ms):
        if all(k in a and k in b for k in (t, t - M15.ms)):
            xs.append(np.log(a[t] / a[t - M15.ms]))
            ys.append(np.log(b[t] / b[t - M15.ms]))
    x, y = np.array(xs), np.array(ys)
    return float(np.corrcoef(x, y)[0, 1]), float(np.cov(x, y, ddof=0)[0, 1] / y.var())


# --------------------------------------------------------------------------------------------- config and roles
def test_gold_records_the_dollar_and_silver_as_candles_only_context(env):
    ctx = env.reg.with_role("XAUUSD", "cross_context")
    assert sorted(i.symbol for i in ctx) == ["EURUSD@", "XAGUSD@"]
    for i in ctx:
        assert i.roles == ("cross_context",) and i.is_context and i.venue == "mt5" and tuple(i.datatypes) == ("candles",)
        assert {tf.value for tf in i.timeframes} == {"15m", "1h", "4h", "1d"}
    # no other role lookup ever returns them: the analysis, execution, quote and flow instruments are unchanged
    assert env.reg.primary("XAUUSD").symbol == "XAUUSD@"
    for role in ("analysis_primary", "execution", "quote_reference", "flow_context"):
        assert all(not i.is_context for i in env.reg.with_role("XAUUSD", role))
    assert [n.label for n in neighbours(env.s, env.reg, "XAUUSD")] == ["EURUSD", "XAGUSD"]
    assert all(n.kind == "context" for n in neighbours(env.s, env.reg, "XAUUSD"))


def test_btc_and_eth_have_no_context_instruments():
    for pair in ("BTCUSDT", "ETHUSDT"):
        s = load_settings(env_path=Path("nope.env"), extra_env={INSTANCE_ENV: pair})
        reg = InstrumentRegistry.from_settings(s)
        assert not [i for i in reg.for_pair(pair) if i.is_context]
        assert [n.kind for n in neighbours(s, reg, pair)] == ["sibling"]


@pytest.mark.parametrize("bad", [
    {"venue": "binance_spot", "symbol": "EURUSDT", "roles": ["cross_context"], "datatypes": ["candles"]},
    {"venue": "mt5", "symbol": "EURUSD@", "roles": ["cross_context"], "datatypes": ["candles", "ticks"]},
    {"venue": "mt5", "symbol": "EURUSD@", "roles": ["cross_context", "quote_reference"], "datatypes": ["candles"]},
])
def test_a_context_instrument_is_mt5_candles_only_with_no_other_role(bad):
    with pytest.raises(ValueError, match="cross_context"):
        InstrumentCfg.model_validate({**bad, "timeframes": ["15m", "1h"]})


def test_the_capability_is_configured_real_and_the_snapshot_downgrades_it_when_no_context_data_exists(env):
    cap = capability_matrix(env.s, env.reg, "XAUUSD")["cross_asset"]
    assert cap.quality == "real" and "EURUSD" in cap.reason and "XAGUSD" in cap.reason
    blk = env.context().block("XAUUSD", env.frame(END), END)
    assert blk["data_quality"] == "unavailable" and "EURUSD" in blk["reason"] and "XAGUSD" in blk["reason"]
    assert not env.eur.hot_db_path(env.root).exists() and not env.xag.hot_db_path(env.root).exists()   # nothing created


# ----------------------------------------------------------------------------------------------- the block
def test_silver_on_real_bars(env):
    env.seed(env.xag, M15, XAG15)
    env.seed(env.xag, H1, XAG1H)
    blk = env.context().block("XAUUSD", env.frame(END), END)
    assert blk["data_quality"] == "real" and blk["missing"] == ["EURUSD"] and set(blk["assets"]) == {"XAGUSD"}
    ag = blk["assets"]["XAGUSD"]
    c, beta = independent(XAU15, XAG15, END)
    assert ag["corr"] == pytest.approx(c, abs=0.006) and ag["beta"] == pytest.approx(beta, abs=0.006)
    assert c > 0.3                                           # gold and silver moved together on these real days
    closes15 = np.array([b[4] for b in XAG15 if b[0] + M15.ms <= END])
    closes1h = np.array([b[4] for b in XAG1H if b[0] + H1.ms <= END])
    assert ag["trend"] == [trend_label(closes15[-160:]), trend_label(closes1h[-100:])]
    assert ag["chg_pct"]["1h"] == round((closes15[-1] / closes15[-5] - 1) * 100, 2)
    assert ag["chg_pct"]["4h"] == round((closes15[-1] / closes15[-17] - 1) * 100, 2)
    t1h = ag["trend"][1]
    assert blk["votes"] == ([f"XAGUSD:{t1h}"] if t1h in ("up", "down") else [])
    assert "votes_note" not in blk


def test_the_dollar_votes_inverted_when_it_moves_against_gold(env):
    """EURUSD here is BUILT from the real silver bars (k / price): its log returns are silver's with the sign flipped."""
    env.seed(env.eur, M15, inverted(XAG15))
    env.seed(env.eur, H1, inverted(XAG1H))
    blk = env.context().block("XAUUSD", env.frame(END), END)
    eu = blk["assets"]["EURUSD"]
    c, _ = independent(XAU15, inverted(XAG15), END)
    assert eu["corr"] == pytest.approx(c, abs=0.006) and c < -0.3
    t1h = eu["trend"][1]
    if t1h in ("up", "down"):                                # a negatively correlated asset's trend implies the other way
        assert blk["votes"] == [f"EURUSD:{'down' if t1h == 'up' else 'up'}"]


def test_a_weakly_correlated_context_casts_no_vote_and_says_so(env):
    """BUILT here: the real silver bars shifted by whole weeks (the weekday / weekend layout stays) until their returns
    no longer line up with gold's."""
    for days in range(7, 29, 7):
        bars = shifted(XAG15, days)
        if abs(independent(XAU15, bars, END)[0]) < 0.25:
            break
    else:
        pytest.skip("no shift decorrelates the real series")
    env.seed(env.xag, M15, bars)
    env.seed(env.xag, H1, shifted(XAG1H, days))
    blk = env.context().block("XAUUSD", env.frame(END), END)
    assert abs(blk["assets"]["XAGUSD"]["corr"]) < 0.3
    assert blk["votes"] == [] and blk["votes_note"] == "|corr|<0.3 casts no vote: XAGUSD"


def test_silver_confirms_an_extreme_of_gold_only_when_it_made_the_same_one(env):
    env.seed(env.xag, M15, XAG15)
    env.seed(env.xag, H1, XAG1H)
    xau = np.array(XAU15)
    xag = np.array(XAG15)
    seen = set()
    for bar in xau[-900:, 0]:
        as_of = int(bar) + M15.ms
        w = xau[xau[:, 0] + M15.ms <= as_of]
        ext = extreme_side(w[:, 0], w[:, 2], w[:, 3], int(w[-1, 0]), M15.ms)
        if ext is None:
            continue
        g = xag[xag[:, 0] + M15.ms <= as_of]
        want = ext if extreme_side(g[:, 0], g[:, 2], g[:, 3], int(w[-1, 0]), M15.ms) == ext else 0
        if want in seen:
            continue
        blk = env.context().block("XAUUSD", env.frame(as_of), as_of)
        if blk["data_quality"] != "real" or "XAGUSD" not in blk["assets"]:
            continue
        assert blk["assets"]["XAGUSD"]["confirms_xau_extreme"] == want
        seen.add(want)
        if len(seen) == 2:
            break
    assert 0 in seen and len(seen) == 2                      # both a confirmation and a non-confirmation were checked


def test_the_block_is_cheap_and_small_and_the_view_drops_the_window_size(env):
    env.seed(env.xag, M15, XAG15)
    env.seed(env.xag, H1, XAG1H)
    env.seed(env.eur, M15, inverted(XAG15))
    env.seed(env.eur, H1, inverted(XAG1H))
    cc, fr = env.context(), env.frame(END)
    cc.block("XAUUSD", fr, END)                              # opens the readers once, as the engine's builder does
    t0 = time.perf_counter()
    blk = cc.block("XAUUSD", fr, END)
    assert (time.perf_counter() - t0) * 1000 < 100
    view = model_view({"meta": {"as_of": "2026-09-29T07:25:00.000Z"}, "market": {"cross": blk}})["market"]["cross"]
    assert "bars" not in view and "assets" in view
    assert len(json.dumps(view, separators=(",", ":"))) / 3.6 <= 170


def test_bars_after_as_of_never_reach_the_block(env):
    env.seed(env.xag, M15, XAG15)
    env.seed(env.xag, H1, XAG1H)
    as_of = END - 20 * M15.ms
    a = env.context().block("XAUUSD", env.frame(as_of), as_of)
    env.seed(env.xag, M15, [(t, o * 3, h * 3, lo * 3, c * 3) for t, o, h, lo, c in XAG15 if t + M15.ms > as_of])
    b = env.context().block("XAUUSD", env.frame(as_of), as_of)
    assert a == b


# ----------------------------------------------------------------------------------------------- ingest
RATE_DTYPE = [("time", "<i8"), ("open", "<f8"), ("high", "<f8"), ("low", "<f8"), ("close", "<f8"),
              ("tick_volume", "<u8"), ("spread", "<i4"), ("real_volume", "<u8")]
TICK_DTYPE = [("time", "<i8"), ("bid", "<f8"), ("ask", "<f8"), ("last", "<f8"), ("volume", "<u8"),
              ("time_msc", "<i8"), ("flags", "<u4"), ("volume_real", "<f8")]
NOW_SRV = (XAG15[-1][0] + M15.ms) + SRV_OFFSET + 30_000


class FakeMT5:
    """The MetaTrader5 calls the live service makes; answers are the real silver / gold bars (server time)."""
    COPY_TICKS_ALL = -1

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        for tf in Timeframe:
            setattr(self, tf.mt5_attr, tf.ms)

    def _bars(self, sym, n=3):
        src = XAG15 if sym.startswith("XAG") else XAU15
        arr = np.zeros(n, dtype=RATE_DTYPE)
        for k, (t, o, h, lo, c) in enumerate(src[-n:]):
            arr[k] = ((t + SRV_OFFSET) // 1000, o, h, lo, c, 10, 20, 0)
        return arr

    def symbol_info_tick(self, sym):
        self.calls.append(("tick", sym))
        return types.SimpleNamespace(time_msc=NOW_SRV)

    def copy_rates_from_pos(self, sym, tfc, start, n):
        self.calls.append(("rates", sym))
        return self._bars(sym, n)

    def copy_rates_range(self, sym, tfc, lo, hi):
        self.calls.append(("range", sym))
        return self._bars(sym, 3)

    def copy_ticks_from(self, sym, frm, n, flags):
        self.calls.append(("ticks", sym))
        return np.zeros(0, dtype=TICK_DTYPE)

    def last_error(self):
        return (1, "Success")


class FakeTerm:
    def __init__(self, unknown=()) -> None:
        self.mt5, self.unknown, self.selected = FakeMT5(), set(unknown), []

    def connect(self):
        return types.SimpleNamespace(server="WindsorBrokers1-Demo", trade_mode=0, leverage=100)

    def select_symbols(self, names):
        for n in names:
            if n in self.unknown:
                raise MT5Unavailable(f"symbol {n!r} not available")
        self.selected += list(names)
        return {}

    def timeframe(self, attr):
        return getattr(self.mt5, attr)

    def shutdown(self):
        pass


def service(env, term) -> mt5svc.MT5LiveService:
    svc = object.__new__(mt5svc.MT5LiveService)
    svc.s, svc.model = env.s, ServerTimeModel()
    svc.appdb = AppDB(env.root / "app.db")
    svc.cold = ParquetColdStore(env.root / "cold")
    svc.archive_guard = types.SimpleNamespace(allowed=lambda: True)
    svc.instruments = [i for i in env.reg.all() if i.venue == "mt5"]
    svc.term, svc.sinks, svc.missing = term, {}, set()
    svc.stop, svc.errors, svc.reconnects, svc._clear_error = False, 0, 0, False
    return svc


def events(svc, kind):
    import sqlite3
    con = sqlite3.connect(svc.appdb.path)
    try:
        return [r[0] for r in con.execute("SELECT detail FROM ingestion_events WHERE event=?", (kind,))]
    finally:
        con.close()


def test_the_live_ingest_never_asks_a_context_instrument_for_ticks_and_polls_its_bars_every_5_s(env, monkeypatch):
    svc = service(env, FakeTerm())
    svc.connect()
    assert {"mt5:XAUUSD@", "mt5:EURUSD@", "mt5:XAGUSD@"} <= set(svc.sinks)
    assert svc.sinks["mt5:XAGUSD@"].cursor is None and svc.sinks["mt5:XAUUSD@"].cursor is not None
    clock = types.SimpleNamespace(t=1_000.0)

    def sleep(s):
        clock.t += max(s, 0.1)
        if clock.t >= 1_012.0:
            svc.stop = True
    monkeypatch.setattr(mt5svc, "time", types.SimpleNamespace(time=lambda: clock.t, sleep=sleep))
    monkeypatch.setattr(svc, "_status", lambda: None)
    svc.term.mt5.calls.clear()
    svc._loop(0.1)
    calls = svc.term.mt5.calls
    assert not [c for c in calls if c == ("ticks", "XAGUSD@") or c == ("ticks", "EURUSD@")]
    assert [c for c in calls if c == ("ticks", "XAUUSD@")]
    per_poll = len(env.xag.timeframes)
    ag, au = calls.count(("rates", "XAGUSD@")), calls.count(("rates", "XAUUSD@"))
    assert ag == 3 * per_poll                                 # t = 1000, 1005, 1010 over 12 s
    assert au >= 11 * len(env.xau.timeframes)                 # the trading instrument keeps its 1-s cadence
    for sink in svc.sinks.values():
        sink.hot.close()


def test_a_context_symbol_the_broker_lacks_is_one_event_and_costs_the_gold_feed_nothing(env):
    svc = service(env, FakeTerm(unknown={"EURUSD@"}))
    svc.connect()
    svc.connect()                                            # a reconnect does not repeat the event
    assert "mt5:EURUSD@" not in svc.sinks and "mt5:XAUUSD@" in svc.sinks and "mt5:XAGUSD@" in svc.sinks
    assert len(events(svc, "context_symbol_missing")) == 1 and "EURUSD@" in events(svc, "context_symbol_missing")[0]
    assert not env.eur.hot_db_path(env.root).exists()        # no file is created for it
    assert "XAUUSD@" in svc.term.selected
    for sink in svc.sinks.values():
        sink.hot.close()


def test_a_broken_context_instrument_is_skipped_but_a_broken_trading_one_still_raises(env, monkeypatch):
    svc = service(env, FakeTerm())
    real = svc._gapfill_rates

    def boom(sink):
        if sink.inst.symbol == "XAGUSD@":
            raise RuntimeError("terminal hiccup")
        return real(sink)
    monkeypatch.setattr(svc, "_gapfill_rates", boom)
    svc.connect()
    assert "mt5:XAGUSD@" not in svc.sinks and "mt5:XAUUSD@" in svc.sinks
    assert "terminal hiccup" in events(svc, "context_skipped")[0]
    for sink in svc.sinks.values():
        sink.hot.close()
    svc2 = service(env, FakeTerm())

    def boom2(sink):
        raise RuntimeError("gold feed broken")
    monkeypatch.setattr(svc2, "_gapfill_rates", boom2)
    with pytest.raises(RuntimeError, match="gold feed broken"):
        svc2.connect()
    for sink in svc2.sinks.values():
        sink.hot.close()


def test_the_backfill_schedules_no_tick_work_for_context_instruments(env):
    bf = object.__new__(mt5bf.MT5Backfill)
    bf.instruments = [i for i in env.reg.all() if i.venue == "mt5"]
    assert {i.symbol for i in bf.instruments} >= {"EURUSD@", "XAGUSD@"}
    ticks = [i.symbol for i in bf.instruments if "ticks" in i.datatypes]
    assert "EURUSD@" not in ticks and "XAGUSD@" not in ticks and "XAUUSD@" in ticks


def test_no_decision_gate_or_executor_code_reads_the_context_instruments():
    """The context instruments and market.cross never feed a trigger, the gate or the executor. The orchestrator names
    the role only to attach the gold field notes to the prompt (Orchestrator._brief) and never reads the data."""
    import ast
    src = Path(__file__).resolve().parents[2] / "src" / "tradingsystem"
    for rel in ("execution", "ai/triggers.py", "ai/orchestrator.py"):
        for p in ([src / rel] if rel.endswith(".py") else sorted((src / rel).rglob("*.py"))):
            text = p.read_text(encoding="utf-8")
            assert "market.get(\"cross\")" not in text and "[\"cross\"]" not in text, p
            if p.name == "orchestrator.py":
                tree = ast.parse(text)
                brief = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_brief")
                outside = text.replace(ast.get_source_segment(text, brief), "")
                assert "cross_context" not in outside, p
            else:
                assert "cross_context" not in text, p
