"""B12: no entry call while no position fits (pairs without a desk). ``account.min_position_risk.fits_now`` False
(the minimum lot at the minimum stop breaks the risk or leverage cap) suppresses strong/weak/idle triggers, the setup
signature advances (as A5's closed market), one ``skipped: no_fit`` event a pair and hour; a pair holding something
keeps its review / event calls; an absent ``fits_now`` (or unknown holdings) never suppresses. The market payload is
the real XAU fixture; ``account`` is hand-built (pure logic: the fit fields themselves are B13's tests)."""
import copy
import sqlite3

import pytest

from tradingsystem.core.timeutil import MS_PER_HOUR

from .test_desk_windows import WED_0510, WED_0655, desk, fire_as, n_events, tick  # noqa: F401 - the engine fixture
from .test_engine_rationing import MIN, PAYLOAD, eng  # noqa: F401
from .test_phase5_engine import PAYLOAD_SIG, executor_reports, with_ai

NOFIT = {"min_position_risk": {"fits_now": False, "leverage_at_min_lot": 42.0, "equity_for_min_lot": 400}}
FITS = {"min_position_risk": {"fits_now": True}}


def builds(desk, monkeypatch, account):
    """Every payload build carries ``account`` (None = the block absent, like a payload before B13)."""
    def build(pair, as_of, acct=None):
        desk.rec.builds.append(pair)
        p = copy.deepcopy(PAYLOAD)
        if account is not None:
            p["account"] = copy.deepcopy(account)
        return p
    monkeypatch.setattr(desk.orch, "payload", build)


def detail_events(e, pair):
    con = sqlite3.connect(e.s.paths.data() / "app.db")
    try:
        return con.execute("SELECT detail FROM ingestion_events WHERE event='skipped: no_fit' AND detail LIKE ?",
                           (f"{pair}:%",)).fetchall()
    finally:
        con.close()


def test_no_fit_suppresses_entry_calls_and_advances_the_signature(desk, monkeypatch):
    builds(desk, monkeypatch, NOFIT)
    executor_reports(desk, [])
    tick(desk, WED_0510)
    assert "BTCUSDT" not in desk.rec.dispatched and "ETHUSDT" not in desk.rec.dispatched
    assert desk.store.kv_get("BTCUSDT:signature") == PAYLOAD_SIG
    assert desk.store.kv_get("BTCUSDT:last_call_price") is not None
    assert len(detail_events(desk, "BTCUSDT")) == 1 and len(detail_events(desk, "ETHUSDT")) == 1
    assert desk._held_back["BTCUSDT"]["reason"] == "no_fit"


def test_a_desk_pair_uses_its_windows_not_the_fit(desk, monkeypatch):
    builds(desk, monkeypatch, NOFIT)
    executor_reports(desk, [])
    tick(desk, WED_0655)                                        # inside London: XAU calls although nothing fits
    assert desk.rec.dispatched == ["XAUUSD"]                    # BTC / ETH (no desk) are suppressed
    assert n_events(desk, "skipped: no_fit") == 2               # only the two pairs without a desk
    assert not detail_events(desk, "XAUUSD")


def test_a_fitting_or_absent_fit_never_suppresses(desk, monkeypatch):
    for k, account in enumerate((FITS, None, {"min_position_risk": {"risk_pct_at_min_lot_and_min_stop": 9.0}})):
        desk.rec.dispatched.clear()
        desk.store.kv_set("BTCUSDT:signature", None)
        builds(desk, monkeypatch, account)
        executor_reports(desk, [])
        desk._fits.clear()
        tick(desk, WED_0510 + k * 5 * MIN)
        assert "BTCUSDT" in desk.rec.dispatched, account
    assert n_events(desk, "skipped: no_fit") == 0


def test_the_key_off_is_todays_behaviour(desk, monkeypatch):
    with_ai(desk, skip_entry_calls_when_no_fit=False)
    builds(desk, monkeypatch, NOFIT)
    executor_reports(desk, [])
    tick(desk, WED_0510)
    assert "BTCUSDT" in desk.rec.dispatched and n_events(desk, "skipped: no_fit") == 0


def test_unknown_holdings_never_suppress(desk, monkeypatch):
    builds(desk, monkeypatch, NOFIT)                            # no executor status row: positions unknown
    tick(desk, WED_0510)
    assert "BTCUSDT" in desk.rec.dispatched and n_events(desk, "skipped: no_fit") == 0


def test_review_and_event_calls_stay_while_the_pair_holds_something(desk, monkeypatch):
    builds(desk, monkeypatch, NOFIT)
    executor_reports(desk, [{"pair": "BTCUSDT", "kind": "order", "decision": "abcd1234", "side": "BUY",
                             "order_type": "BUY_LIMIT", "volume": 0.01, "price": 60000.0, "sl": 59000.0}])
    for strength in ("event", "review"):
        desk.rec.dispatched.clear()
        monkeypatch.setattr(desk, "evaluate", lambda pair, now, at_close, payload, policy=None, s=strength, **kw:
                            (pair == "BTCUSDT", ["x"], s))
        tick(desk, WED_0510)
        assert desk.rec.dispatched == ["BTCUSDT"], strength
    for strength in ("strong", "weak", "idle"):                 # entry triggers wait even then
        desk.rec.dispatched.clear()
        monkeypatch.setattr(desk, "evaluate", lambda pair, now, at_close, payload, policy=None, s=strength, **kw:
                            (pair == "BTCUSDT", ["x"], s))
        tick(desk, WED_0510)
        assert desk.rec.dispatched == [], strength


def test_a_review_of_a_pair_holding_nothing_is_suppressed(desk, monkeypatch):
    builds(desk, monkeypatch, NOFIT)
    executor_reports(desk, [])
    fire_as_btc(desk, monkeypatch, "review")
    tick(desk, WED_0510)
    assert desk.rec.dispatched == []


def fire_as_btc(e, monkeypatch, strength):
    monkeypatch.setattr(e, "evaluate", lambda pair, now, at_close, payload, policy=None, **kw:
                        (pair == "BTCUSDT", ["x"], strength))


def test_one_event_per_pair_and_hour(desk, monkeypatch):
    builds(desk, monkeypatch, NOFIT)
    executor_reports(desk, [])
    fire_as_btc(desk, monkeypatch, "strong")
    for k in range(5):
        tick(desk, WED_0510 + k * 5 * MIN)
    assert len(detail_events(desk, "BTCUSDT")) == 1
    tick(desk, WED_0510 + MS_PER_HOUR + MIN)
    assert len(detail_events(desk, "BTCUSDT")) == 2


def test_fits_again_calls_and_clears_the_status_line(desk, monkeypatch):
    builds(desk, monkeypatch, NOFIT)
    executor_reports(desk, [])
    fire_as_btc(desk, monkeypatch, "strong")
    tick(desk, WED_0510)
    assert "BTCUSDT" in desk._held_back
    builds(desk, monkeypatch, FITS)
    tick(desk, WED_0510 + 5 * MIN)
    assert desk.rec.dispatched == ["BTCUSDT"] and "BTCUSDT" not in desk._held_back


def test_the_status_detail_names_what_holds_a_pair_back(desk, monkeypatch):
    import json
    builds(desk, monkeypatch, NOFIT)
    executor_reports(desk, [])
    tick(desk, WED_0510)
    desk.write_status()
    row = next(r for r in desk.appdb.statuses() if r["collector"] == "engine")
    held = json.loads(row["detail"])["entry_calls_held_back"]
    assert held["BTCUSDT"]["reason"] == "no_fit" and held["XAUUSD"]["reason"] == "outside_window"
