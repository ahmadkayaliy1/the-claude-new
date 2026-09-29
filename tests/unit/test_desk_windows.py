"""B15 (D-049): the gold desk's call windows - DST in each window's own zone (both directions, incl. the weeks the UK
and the US change clocks on different dates), the Friday cutoff, the Sunday reopen grace read from the session
calendar, the B8 news hook, and the engine's suppression (signature advances, one event per gap between windows,
review calls of a pair holding something stay). Pure-logic edge cases use hand-built instants (labelled); the market
payload is the real XAU fixture."""
import asyncio
import copy
import datetime as dt
import sqlite3
from types import SimpleNamespace

import pytest

from tradingsystem.analysis import desk_windows as dw
from tradingsystem.analysis.desk_windows import desk_state_text, desk_window_state, last_window_end
from tradingsystem.core.sessions import NYMetalsFX
from tradingsystem.core.settings import DeskCfg, DeskClockCfg, DeskWindowCfg

from .test_engine_rationing import MIN, PAYLOAD, eng  # noqa: F401 - the engine fixture (temp data dir)
from .test_phase5_engine import PAYLOAD_SIG, executor_reports

CAL = NYMetalsFX()
CFG = DeskCfg()                                     # the code defaults: London 07:45-11:00, New York 08:15-11:30


def utc(y, mo, d, h, mi=0):
    return int(dt.datetime(y, mo, d, h, mi, tzinfo=dt.timezone.utc).timestamp() * 1000)


def state(cfg, *when):
    return desk_window_state(cfg, utc(*when), CAL)


def test_london_window_follows_bst_and_gmt():
    assert state(CFG, 2026, 7, 15, 6, 44) == (False, "outside_window")     # BST: 07:45 local = 06:45 UTC
    assert state(CFG, 2026, 7, 15, 6, 45) == (True, "london")
    assert state(CFG, 2026, 7, 15, 9, 59) == (True, "london")
    assert state(CFG, 2026, 7, 15, 10, 0) == (False, "outside_window")     # 11:00 BST
    assert state(CFG, 2026, 12, 15, 6, 50) == (False, "outside_window")    # GMT: the same UTC minute is now early
    assert state(CFG, 2026, 12, 15, 7, 45) == (True, "london")
    assert state(CFG, 2026, 12, 15, 10, 59) == (True, "london")


def test_new_york_window_follows_edt_and_est():
    assert state(CFG, 2026, 7, 15, 12, 14) == (False, "outside_window")    # EDT: 08:15 local = 12:15 UTC
    assert state(CFG, 2026, 7, 15, 12, 15) == (True, "new_york")
    assert state(CFG, 2026, 7, 15, 15, 30) == (False, "outside_window")
    assert state(CFG, 2026, 12, 15, 13, 14) == (False, "outside_window")   # EST: 13:15 UTC
    assert state(CFG, 2026, 12, 15, 13, 15) == (True, "new_york")
    assert state(CFG, 2026, 12, 15, 16, 29) == (True, "new_york")


def test_autumn_mismatch_uk_on_gmt_while_the_us_is_still_on_daylight_time():
    # the UK went back on Sun 2026-10-25, the US goes back on Sun 2026-11-01: Mon 2026-10-26 is GMT + EDT
    assert state(CFG, 2026, 10, 26, 6, 50) == (False, "outside_window")    # would be open with BST (06:45)
    assert state(CFG, 2026, 10, 26, 7, 45) == (True, "london")
    assert state(CFG, 2026, 10, 26, 12, 14) == (False, "outside_window")   # New York still opens at 12:15 UTC
    assert state(CFG, 2026, 10, 26, 12, 15) == (True, "new_york")
    assert state(CFG, 2026, 10, 19, 6, 50) == (True, "london")             # the week before: both on daylight time
    assert state(CFG, 2026, 11, 2, 12, 20) == (False, "outside_window")    # both on standard time: NY at 13:15
    assert state(CFG, 2026, 11, 2, 13, 15) == (True, "new_york")


def test_spring_mismatch_us_on_edt_while_the_uk_is_still_on_gmt():
    # the US went forward on Sun 2026-03-08, the UK goes forward on Sun 2026-03-29: Mon 2026-03-09 is GMT + EDT
    assert state(CFG, 2026, 3, 9, 6, 50) == (False, "outside_window")
    assert state(CFG, 2026, 3, 9, 7, 45) == (True, "london")
    assert state(CFG, 2026, 3, 9, 12, 15) == (True, "new_york")
    assert state(CFG, 2026, 3, 9, 13, 0) == (True, "new_york")
    assert state(CFG, 2026, 3, 30, 6, 45) == (True, "london")              # after the UK change: both on daylight time


def test_friday_cutoff_in_new_york_time():
    cfg = CFG.model_copy(update={"friday_cutoff": DeskClockCfg(time="10:00", tz="America/New_York")})
    assert state(cfg, 2026, 9, 25, 13, 59) == (True, "new_york")           # Friday, EDT: 09:59 local
    assert state(cfg, 2026, 9, 25, 14, 0) == (False, "friday_cutoff")      # 10:00 local, inside the NY window
    assert state(cfg, 2026, 9, 24, 14, 0) == (True, "new_york")            # Thursday: no cutoff
    assert state(cfg, 2026, 12, 4, 15, 0) == (False, "friday_cutoff")      # Friday, EST: 10:00 = 15:00 UTC
    assert state(cfg, 2026, 12, 4, 14, 59) == (True, "new_york")


def test_default_friday_cutoff_is_after_both_default_windows():
    late = CFG.model_copy(update={"friday_cutoff": DeskClockCfg(time="23:59", tz="America/New_York")})
    for d in ((2026, 9, 25), (2026, 12, 4)):                               # Fridays: no window minute is cut
        for h in range(6, 17):
            for m in (0, 30):
                assert state(CFG, *d, h, m)[0] == state(late, *d, h, m)[0]


ALL_DAY = DeskCfg(windows=[DeskWindowCfg(name="all", tz="UTC", start="00:00", end="23:59")], reopen_grace_min=60)


def test_sunday_reopen_grace_is_read_from_the_calendar():
    assert state(ALL_DAY, 2026, 9, 27, 21, 59) == (False, "market_closed")
    assert state(ALL_DAY, 2026, 9, 27, 22, 0) == (False, "reopen_grace")   # Sun 18:00 EDT = 22:00 UTC
    assert state(ALL_DAY, 2026, 9, 27, 22, 59) == (False, "reopen_grace")
    assert state(ALL_DAY, 2026, 9, 27, 23, 0) == (True, "all")
    # after the US change the reopen moves to 23:00 UTC: nothing is hardcoded
    assert state(ALL_DAY, 2026, 11, 8, 23, 30) == (False, "reopen_grace")
    assert state(ALL_DAY, 2026, 11, 9, 0, 0) == (True, "all")
    assert state(ALL_DAY, 2026, 11, 8, 22, 30) == (False, "market_closed")


def test_the_daily_break_reopen_is_not_the_sunday_grace():
    assert state(ALL_DAY, 2026, 9, 29, 22, 5) == (True, "all")             # Tue 18:05 EDT, right after the break


def test_grace_zero_and_custom_length():
    assert state(ALL_DAY.model_copy(update={"reopen_grace_min": 0}), 2026, 9, 27, 22, 0) == (True, "all")
    assert state(ALL_DAY.model_copy(update={"reopen_grace_min": 90}), 2026, 9, 27, 23, 20) == (False, "reopen_grace")


def test_the_news_hook_blocks_and_names_its_reason(monkeypatch):
    assert dw.desk_news_block("XAUUSD", utc(2026, 9, 29, 8)) is None       # B8 fills it; nothing wired yet
    monkeypatch.setattr(dw, "desk_news_block", lambda pair, now: "news_stale")
    assert desk_window_state(CFG, utc(2026, 9, 29, 7, 50), CAL) == (False, "news_stale")
    assert desk_state_text(CFG, utc(2026, 9, 29, 7, 50), CAL) == "closed:news_stale"


def test_state_text():
    assert desk_state_text(CFG, utc(2026, 9, 29, 7, 0), CAL) == "open"
    assert desk_state_text(CFG, utc(2026, 9, 29, 3, 0), CAL) == "closed:outside_window"


def test_last_window_end_is_the_gap_identity():
    a = last_window_end(CFG, utc(2026, 9, 29, 3, 0))                       # overnight: yesterday's NY close 11:30 EDT
    assert a == utc(2026, 9, 28, 15, 30)
    assert last_window_end(CFG, utc(2026, 9, 29, 4, 0)) == a               # the whole gap has one identity
    assert last_window_end(CFG, utc(2026, 9, 29, 11, 0)) == utc(2026, 9, 29, 10, 0)   # London closed 11:00 BST
    assert last_window_end(CFG, utc(2026, 9, 29, 10, 0)) == utc(2026, 9, 29, 10, 0)


# ------------------------------------------------------------------ the engine
# Wed 2026-09-23 (a past date: the executor status row written by the test must not look stale to the engine clock)
WED_0510 = utc(2026, 9, 23, 5, 10) + 6_000        # XAU open, outside both windows; the 05:05 screen bar just closed
WED_0655 = utc(2026, 9, 23, 6, 55) + 6_000        # London window open (BST: 06:45-10:00 UTC)


@pytest.fixture
def desk(eng, monkeypatch):
    """The engine with the real XAU payload for every build; dispatches are recorded (BTC/ETH have no desk)."""
    rec = SimpleNamespace(builds=[], dispatched=[])

    def build(pair, as_of, account=None):
        rec.builds.append(pair)
        return copy.deepcopy(PAYLOAD)
    monkeypatch.setattr(eng.orch, "payload", build)
    monkeypatch.setattr(eng, "_dispatch", lambda fired, now: rec.dispatched.extend(f[0] for f in fired))
    monkeypatch.setattr(eng, "_data_ready", lambda pair, bar, tf=None: True)
    eng.rec = rec
    assert eng.s.pairs["XAUUSD"].desk is not None and eng.s.pairs["BTCUSDT"].desk is None
    return eng


def tick(e, now):
    from tradingsystem.analysis import engine as eng_mod
    real = eng_mod.now_ms
    eng_mod.now_ms = lambda: now
    try:
        asyncio.run(e.tick())
    finally:
        eng_mod.now_ms = real


def n_events(e, name):
    con = sqlite3.connect(e.s.paths.data() / "app.db")
    try:
        return con.execute("SELECT count(*) FROM ingestion_events WHERE event=?", (name,)).fetchone()[0]
    finally:
        con.close()


def fire_as(e, monkeypatch, why, strength):
    monkeypatch.setattr(e, "evaluate", lambda pair, now, at_close, payload, policy=None, **kw:
                        (pair == "XAUUSD", [why], strength))


def test_outside_a_window_the_desk_pair_makes_no_entry_call_and_the_signature_advances(desk):
    tick(desk, WED_0510)
    assert set(desk.rec.dispatched) == {"BTCUSDT", "ETHUSDT"}               # no desk: called as before
    assert desk.store.kv_get("XAUUSD:signature") == PAYLOAD_SIG             # the window's opening is not old structure
    assert desk.store.kv_get("XAUUSD:last_call_price") is not None
    assert n_events(desk, "skipped: outside_desk_window") == 1
    assert desk._held_back["XAUUSD"]["reason"] == "outside_window"


def test_inside_a_window_the_desk_pair_calls(desk):
    tick(desk, WED_0655)
    assert "XAUUSD" in desk.rec.dispatched
    assert n_events(desk, "skipped: outside_desk_window") == 0


def test_one_event_per_gap_not_per_screen(desk):
    for k in range(4):                                                      # four screens in one gap
        tick(desk, WED_0510 + k * 5 * MIN)
    assert n_events(desk, "skipped: outside_desk_window") == 1
    desk.store.kv_set("XAUUSD:signature", ["old"])                          # a fresh setup, same gap: no second event
    tick(desk, WED_0510 + 20 * MIN)
    assert n_events(desk, "skipped: outside_desk_window") == 1
    tick(desk, utc(2026, 9, 23, 11, 0) + 6_000)                             # after London closed (10:00): the next gap
    assert n_events(desk, "skipped: outside_desk_window") == 2


def test_a_pair_holding_a_position_keeps_review_and_event_calls(desk, monkeypatch):
    executor_reports(desk, [{"pair": "XAUUSD", "kind": "position", "decision": "abcd1234", "side": "BUY",
                             "volume": 0.01, "price": 4300.0, "sl": 4280.0}])
    fire_as(desk, monkeypatch, "event: stop hit", "event")
    tick(desk, WED_0510)
    assert "XAUUSD" in desk.rec.dispatched
    assert n_events(desk, "skipped: outside_desk_window") == 0
    desk.rec.dispatched.clear()
    fire_as(desk, monkeypatch, "new strong setup", "strong")
    tick(desk, WED_0510 + 5 * MIN)
    assert "XAUUSD" not in desk.rec.dispatched                              # an entry trigger still waits for a window
    assert n_events(desk, "skipped: outside_desk_window") == 1


def test_a_review_of_a_pair_holding_nothing_is_held_back_too(desk, monkeypatch):
    executor_reports(desk, [])
    fire_as(desk, monkeypatch, "review: price_above", "review")
    tick(desk, WED_0510)
    assert "XAUUSD" not in desk.rec.dispatched


def test_a_pair_without_a_desk_is_not_affected_by_windows(desk):
    tick(desk, WED_0510)                                                    # gold's window is closed
    assert {"BTCUSDT", "ETHUSDT"} <= set(desk.rec.dispatched)
    assert n_events(desk, "skipped: no_fit") == 0


def test_a_failure_in_the_suppression_lets_the_call_go_ahead(desk, monkeypatch):
    monkeypatch.setattr(desk, "_entry_suppression", lambda *a, **k: 1 / 0)
    tick(desk, WED_0510)
    assert "XAUUSD" in desk.rec.dispatched
