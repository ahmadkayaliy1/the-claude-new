"""Phase 5 B8 (D-046 a) with the gold tiers (D-049): the news blackout from the public weekly calendar feed.

The calendar is the REAL export downloaded 2026-09-29 (tests/fixtures/real/ff_calendar_thisweek_2026-09-29.json:
Core PCE + Final GDP on Wed 09-30 08:30 ET, NFP on Fri 10-02 08:30 ET). Downloads go through a fake opener that returns
those bytes (the unit suite never touches the network: TS_NEWS_NO_FETCH). Events whose title or date is changed to
reach an edge case (FOMC, a DST week, a medium release) are built in the test and say so."""
import asyncio
import copy
import io
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from tradingsystem.analysis import news
from tradingsystem.analysis.desk_windows import desk_news_reason
from tradingsystem.core.settings import NewsBlackoutCfg, load_settings
from tradingsystem.core.timeutil import iso, parse_date_spec
from tradingsystem.execution.risk_gate import ExecContext, evaluate

from .test_engine_rationing import MIN, PAYLOAD, eng  # noqa: F401 - the engine fixture (temp data dir)

REAL = Path(__file__).resolve().parents[1] / "fixtures" / "real"
RAW = (REAL / "ff_calendar_thisweek_2026-09-29.json").read_bytes()
GOLD = NewsBlackoutCfg(enabled=True, windows={"fomc": (60, 75), "high": (30, 30), "medium": (10, 10)})
BASE = NewsBlackoutCfg(enabled=True)                    # B8's base: USD high impact and FOMC +-15 min
FETCHED = parse_date_spec("2026-09-29T08:30:00Z")
PCE = parse_date_spec("2026-09-30T12:30:00Z")           # Core PCE Price Index m/m, 08:30 EDT
NFP = parse_date_spec("2026-10-02T12:30:00Z")


def opener(raw=RAW):
    return lambda req, timeout: io.BytesIO(raw)


def stored(tmp_path, raw=RAW, fetched=FETCHED, cfg=GOLD) -> Path:
    shared = tmp_path / "shared"
    news.fetch(cfg, news.calendar_path(shared), fetched, opener=opener(raw))
    return shared


def ev(title, when, impact="High", country="USD"):
    return news.Event(parse_date_spec(when), title, country, impact)


# ------------------------------------------------------------------------------------------------ the feed
def test_the_real_feed_parses_with_its_new_york_offsets():
    evs = news.parse_feed(RAW)
    assert len(evs) == len(json.loads(RAW)) == 141
    assert [e.time_ms for e in evs] == sorted(e.time_ms for e in evs)
    pce = [e for e in evs if e.title == "Core PCE Price Index m/m"]
    assert len(pce) == 1 and pce[0].time_ms == PCE and pce[0].country == "USD" and pce[0].impact == "High"
    assert any(e.title == "Non-Farm Employment Change" and e.time_ms == NFP for e in evs)


def test_the_offset_decides_the_utc_time_across_the_dst_change():
    """Built: the same 08:30 New York release in EDT (before 2026-11-01) and in EST (after)."""
    assert news.parse_time("2026-10-30T08:30:00-04:00") == parse_date_spec("2026-10-30T12:30:00Z")
    assert news.parse_time("2026-11-06T08:30:00-05:00") == parse_date_spec("2026-11-06T13:30:00Z")
    with pytest.raises(ValueError):
        news.parse_time("2026-11-06T08:30:00")                 # a naive time is refused, never guessed


@pytest.mark.parametrize("raw", [b"<!DOCTYPE html><title>Request Denied</title>", b'{"error": "limit"}',
                                 b'[{"title": "x", "date": "tomorrow"}]'])
def test_what_is_not_a_calendar_is_refused_and_the_previous_file_stays(tmp_path, raw):
    shared = stored(tmp_path)
    before = news.calendar_path(shared).read_bytes()
    with pytest.raises(ValueError):
        news.fetch(GOLD, news.calendar_path(shared), FETCHED + 3_600_000, opener=opener(raw))
    assert news.calendar_path(shared).read_bytes() == before
    assert [p.name for p in shared.iterdir()] == [news.FILE_NAME]                 # no temp file left behind


def test_the_stored_file_round_trips(tmp_path):
    cal = news.load(news.calendar_path(stored(tmp_path)))
    assert cal.fetched_ms == FETCHED and len(cal.events) == 141 and cal.source_url == GOLD.source_url
    assert news.load(tmp_path / "missing.json") is None
    (tmp_path / "bad.json").write_text("{", encoding="utf-8")
    assert news.load(tmp_path / "bad.json") is None


def test_the_unit_suite_never_downloads(tmp_path):
    with pytest.raises(RuntimeError, match="TS_NEWS_NO_FETCH"):
        news.fetch(GOLD, tmp_path / "x.json", FETCHED)


# ------------------------------------------------------------------------------------------------ tiers
def test_tiers():
    """FOMC / medium titles are built (this week's real file has none of them at those ratings)."""
    assert news.tier_of(ev("FOMC Statement", "2026-10-28T18:00:00Z"), GOLD) == "fomc"
    assert news.tier_of(ev("Federal Funds Rate", "2026-10-28T18:00:00Z"), GOLD) == "fomc"
    assert news.tier_of(ev("FOMC Press Conference", "2026-10-28T18:30:00Z"), GOLD) == "fomc"
    assert news.tier_of(ev("CPI m/m", "2026-10-14T12:30:00Z"), GOLD) == "high"
    assert news.tier_of(ev("PPI m/m", "2026-10-15T12:30:00Z", "Medium"), GOLD) == "medium"
    assert news.tier_of(ev("ISM Manufacturing PMI", "2026-10-01T14:00:00Z", "Medium"), GOLD) == "medium"
    assert news.tier_of(ev("FOMC Member Bowman Speaks", "2026-09-28T12:15:00Z", "Low"), GOLD) is None
    assert news.tier_of(ev("FOMC Member Waller Speaks", "2026-10-01T14:00:00Z", "Medium"), GOLD) is None   # real
    assert news.tier_of(ev("FOMC Meeting Minutes", "2026-10-08T18:00:00Z"), GOLD) == "high"
    assert news.tier_of(ev("Consumer Confidence", "2026-09-29T14:00:00Z", "Medium"), GOLD) is None
    assert news.tier_of(ev("Cash Rate", "2026-09-29T04:30:00Z", "High", "AUD"), GOLD) is None
    assert news.tier_of(ev("PPI m/m", "2026-10-15T12:30:00Z", "Medium"), BASE) is None      # no medium window


# ------------------------------------------------------------------------------------------------ the state
def test_the_gold_windows_around_the_real_core_pce_release(tmp_path):
    cal = news.load(news.calendar_path(stored(tmp_path, fetched=PCE - 120 * MIN)))   # the engine refreshes hourly
    for at, blocked in ((PCE - 31 * MIN, False), (PCE - 30 * MIN, True), (PCE, True), (PCE + 29 * MIN, True),
                        (PCE + 30 * MIN, False)):
        st = news.state_at(GOLD, cal, at)
        assert st.fresh and st.blocked is blocked, iso(at)
    st = news.state_at(GOLD, cal, PCE)
    tier, e, lo, hi = st.blackout
    assert tier == "high" and lo == PCE - 30 * MIN and hi == PCE + 30 * MIN and e.time_ms == PCE
    ok, detail = news.gate_check(st)
    assert not ok and "08:30" not in detail and iso(PCE) in detail and iso(hi) in detail
    assert news.gate_check(news.state_at(GOLD, cal, PCE - 2 * 60 * MIN)) == (
        True, "no scheduled release within its blackout window")
    assert news.state_at(BASE, cal, PCE - 20 * MIN).blocked is False                  # B8's base window is 15 min
    assert news.state_at(BASE, cal, PCE - 15 * MIN).blocked is True


def test_the_payload_block_lists_the_next_releases_and_the_blackout(tmp_path):
    cal = news.load(news.calendar_path(stored(tmp_path, fetched=PCE - 120 * MIN)))
    quiet = news.block(news.state_at(GOLD, cal, PCE - 60 * MIN), PCE - 60 * MIN)
    assert quiet["blackout"] == "none" and quiet["data_quality"] == "real"
    assert quiet["next"] == [[iso(PCE), "high", "Core PCE Price Index m/m", 60], [iso(PCE), "high", "Final GDP q/q", 60]]
    on = news.block(news.state_at(GOLD, cal, PCE), PCE)
    assert on["blackout"][0] == "high" and on["blackout"][3] == iso(PCE + 30 * MIN)
    from tradingsystem.ai.model_view import model_view
    view = model_view({"meta": {"as_of": iso(PCE)}, "market": {"news": on}})["market"]["news"]
    assert "data_quality" not in view and view["blackout"][2:] == ["09-30 12:30", "09-30 13:00"]
    assert len(json.dumps(view, separators=(",", ":"))) / 3.6 <= 60


def test_freshness_is_the_fetch_week_and_24_hours(tmp_path):
    cal = news.load(news.calendar_path(stored(tmp_path)))
    assert news.state_at(GOLD, cal, FETCHED + 23 * 60 * MIN).fresh
    late = news.state_at(GOLD, cal, FETCHED + 25 * 60 * MIN)
    assert not late.fresh and "stale" in late.reason and late.blackout is None
    assert news.gate_check(late)[0] is True and "no blackout applied" in news.gate_check(late)[1]
    assert news.block(late, FETCHED + 25 * 60 * MIN)["data_quality"] == "unavailable"
    # the next week (Sunday 2026-10-04 00:00 New York) is not covered even by a file fetched an hour earlier
    sat_late = parse_date_spec("2026-10-04T03:00:00Z")          # Sat 23:00 EDT
    cal2 = news.load(news.calendar_path(stored(tmp_path / "b", fetched=sat_late)))
    assert news.state_at(GOLD, cal2, sat_late + 30 * MIN).fresh
    assert not news.state_at(GOLD, cal2, sat_late + 90 * MIN).fresh
    # a download just after the week rolled that still serves last week's list is not fresh
    cal3 = news.load(news.calendar_path(stored(tmp_path / "c", fetched=sat_late + 90 * MIN)))
    assert not news.state_at(GOLD, cal3, sat_late + 95 * MIN).fresh
    assert not news.state_at(GOLD, None, FETCHED).fresh


def test_the_week_boundaries_follow_new_york_dst():
    """Built: a file fetched on Mon 2026-11-02 (the US clocks went back on Sun 11-01)."""
    cal = news.Calendar(parse_date_spec("2026-11-02T15:00:00Z"), "u", ())
    lo, hi = cal.week()
    assert lo == parse_date_spec("2026-11-01T04:00:00Z") and hi == parse_date_spec("2026-11-08T05:00:00Z")


def test_a_broken_file_is_not_fresh_and_never_raises(tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir()
    news.calendar_path(shared).write_text('{"version": 1, "fetched_ms": "x", "events": 3}', encoding="utf-8")
    st = news.state_now(GOLD, shared, FETCHED)
    assert not st.fresh and desk_news_reason(st) == "news_stale"


# ------------------------------------------------------------------------------------------------ the gate
def ctx(**kw):
    base = dict(now_ms=parse_date_spec("2026-09-30T12:31:00Z"), bid=4130.0, ask=4130.3, quote_age_s=1.0,
                market_open=True, atr=6.0, stops_level_price=0.25, contract_size=100.0, volume_min=0.01,
                volume_step=0.01, volume_max=100.0, equity=5000.0)
    return ExecContext(**{**base, **kw})


REC = {"decision": "BUY", "order_type": "MARKET", "entry": {"price": 4130.3}, "stop_loss": 4121.0,
       "take_profits": [{"price": 4150.0, "close_fraction": 1.0}], "confidence": 70,
       "timestamp": "2026-09-30T12:30:30Z", "valid_until": "2026-09-30T13:30:00Z"}


def test_the_gate_refuses_inside_the_window_and_says_why_and_adds_nothing_without_a_blackout():
    s = load_settings(env_path=Path("nope.env"))
    refused = evaluate(REC, "XAUUSD", ctx(news=(False, "USD high release 'Core PCE Price Index m/m'")), s.risk,
                       s.risk.correlated_groups)
    chk = [c for c in refused.checks if c[0] == "news_blackout"]
    assert chk == [("news_blackout", False, "USD high release 'Core PCE Price Index m/m'")] and not refused.approved
    assert "news_blackout: USD high release" in "; ".join(refused.failures())
    passed = evaluate(REC, "XAUUSD", ctx(news=(True, "no scheduled release")), s.risk, s.risk.correlated_groups)
    assert ("news_blackout", True, "no scheduled release") in passed.checks
    without = evaluate(REC, "XAUUSD", ctx(), s.risk, s.risk.correlated_groups)
    assert not [c for c in without.checks if c[0] == "news_blackout"]            # a pair without it: unchanged
    assert [c for c in passed.checks if c[0] != "news_blackout"] == without.checks


def test_the_executor_check_reads_the_stored_calendar_and_refuses_on_an_unexpected_error(tmp_path, monkeypatch):
    from tradingsystem.execution import executor as ex
    s = load_settings(env_path=Path("nope.env"))
    s = s.model_copy(update={"paths": s.paths.model_copy(update={"data_dir": str(tmp_path)})})
    stored(tmp_path, fetched=PCE - 120 * MIN)
    e = object.__new__(ex.Executor)
    e.s = s
    monkeypatch.setattr(ex, "now_ms", lambda: PCE + 5 * MIN)
    ok, detail = e._news_check("XAUUSD")
    assert not ok and "Core PCE" in detail
    assert e._news_check("BTCUSDT") is None                                     # no blackout configured
    monkeypatch.setattr(ex, "now_ms", lambda: PCE + 3 * 60 * MIN)
    assert e._news_check("XAUUSD")[0] is True
    monkeypatch.setattr(news, "state_now", lambda *a, **k: 1 / 0)
    ok, detail = e._news_check("XAUUSD")
    assert ok is False and "refused" in detail


# ------------------------------------------------------------------------------------------------ the fetcher
def test_the_fetcher_downloads_when_due_and_retries_a_failure_after_15_minutes(tmp_path, monkeypatch):
    got = []
    f = news.Fetcher(GOLD, tmp_path / "shared", on_event=lambda kind, text: got.append(kind))
    calls, real_fetch = [], news.fetch

    def fake_fetch(cfg, dest, now, opener=None):
        calls.append(now)
        if len(calls) == 1:
            raise OSError("network down")
        return real_fetch(cfg, dest, now, opener=globals()["opener"]())
    monkeypatch.setattr(news, "fetch", fake_fetch)
    assert f.due(FETCHED) and f.maybe_fetch(FETCHED, background=False) is False and got == ["news_fetch_failed"]
    assert not f.due(FETCHED + 14 * MIN) and f.maybe_fetch(FETCHED + 14 * MIN, background=False) is False
    assert f.maybe_fetch(FETCHED + 15 * MIN, background=False) is True and len(calls) == 2
    assert not f.due(FETCHED + 16 * MIN) and not f.due(FETCHED + 74 * MIN)
    assert f.due(FETCHED + 75 * MIN)                                            # refresh_minutes after the success


# ------------------------------------------------------------------------------------------------ the engine
def tick(e, now):
    from tradingsystem.analysis import engine as eng_mod
    real = eng_mod.now_ms
    eng_mod.now_ms = lambda: now
    try:
        asyncio.run(e.tick())
    finally:
        eng_mod.now_ms = real


def n_events(e, name, pair="%"):
    con = sqlite3.connect(e.s.paths.data() / "app.db")
    try:
        return con.execute("SELECT count(*) FROM ingestion_events WHERE event=? AND detail LIKE ?",
                           (name, f"{pair}%")).fetchone()[0]
    finally:
        con.close()


@pytest.fixture
def desk(eng, monkeypatch):
    rec = SimpleNamespace(dispatched=[])
    monkeypatch.setattr(eng.orch, "payload", lambda pair, as_of, account=None: copy.deepcopy(PAYLOAD))
    monkeypatch.setattr(eng, "_dispatch", lambda fired, now: rec.dispatched.extend(f[0] for f in fired))
    monkeypatch.setattr(eng, "_data_ready", lambda pair, bar, tf=None: True)
    monkeypatch.setattr(eng, "evaluate", lambda pair, now, at_close, payload, policy=None, **kw:
                        (True, ["new strong setup"], "strong"))
    eng.rec = rec
    return eng


LONDON_PCE_WEEK = parse_date_spec("2026-09-30T07:10:00Z") + 6_000   # Wed, the London window (BST 06:45-10:00 UTC)
NY_PCE = PCE + 10 * MIN + 6_000                                       # 12:40 UTC: inside the NY window AND the blackout


def test_the_desk_makes_no_entry_call_without_a_fresh_calendar_and_says_so_once(desk):
    tick(desk, LONDON_PCE_WEEK)                                         # no file (downloads are off in the suite)
    assert "XAUUSD" not in desk.rec.dispatched and {"BTCUSDT", "ETHUSDT"} <= set(desk.rec.dispatched)
    assert desk._held_back["XAUUSD"]["reason"] == "news_stale"
    tick(desk, LONDON_PCE_WEEK + 5 * MIN)
    assert n_events(desk, "news_stale") == 1 and n_events(desk, "news_fetch_failed") >= 1


def test_the_desk_calls_inside_its_window_with_a_fresh_calendar_and_waits_out_a_release(desk, tmp_path):
    news.fetch(GOLD, news.calendar_path(desk.s.paths.shared()), LONDON_PCE_WEEK - 60 * MIN, opener=opener())
    tick(desk, LONDON_PCE_WEEK)
    assert "XAUUSD" in desk.rec.dispatched
    desk.rec.dispatched.clear()
    news.fetch(GOLD, news.calendar_path(desk.s.paths.shared()), NY_PCE - 60 * MIN, opener=opener())
    tick(desk, NY_PCE)
    assert "XAUUSD" not in desk.rec.dispatched and desk._held_back["XAUUSD"]["reason"] == "news_blackout"
    assert n_events(desk, "skipped: news_blackout") == 1 and n_events(desk, "skipped: outside_desk_window") == 0


def test_a_pair_without_a_desk_waits_out_a_blackout_but_ignores_a_stale_file(desk, monkeypatch):
    """Built: BTCUSDT with the gold blackout turned on (no pair is configured so; the rule is generic)."""
    s = desk.s
    pairs = {**s.pairs, "BTCUSDT": s.pairs["BTCUSDT"].model_copy(update={"news_blackout": GOLD})}
    desk.s = s.model_copy(update={"pairs": pairs})
    monkeypatch.setattr(desk, "_holds", lambda pair: False)             # known holdings: nothing held
    tick(desk, NY_PCE)                                                  # no file: stale -> BTC is not held back
    assert "BTCUSDT" in desk.rec.dispatched
    desk.rec.dispatched.clear()
    news.fetch(GOLD, news.calendar_path(desk.s.paths.shared()), NY_PCE - 60 * MIN, opener=opener())
    desk.store.kv_set("BTCUSDT:signature", ["old"])
    tick(desk, NY_PCE + 5 * MIN)
    assert "BTCUSDT" not in desk.rec.dispatched and n_events(desk, "skipped: news_blackout", "BTCUSDT") == 1
