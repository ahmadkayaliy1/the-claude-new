"""ai/usage_gauge.py + UsageStore.tokens_since (Phase 4, §3.8 component 6) on seeded tmp ledgers.

The ledger rows are test data written straight into a tmp ``ai_usage`` table at chosen times (the store's own
``record`` always stamps "now").
"""
from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

import pytest

from tradingsystem.ai.budget import UsageStore
from tradingsystem.ai.usage_gauge import GaugeState, UsageGauge, claude_providers, effective_tokens
from tradingsystem.core.settings import INSTANCE_ENV, PathsCfg, load_settings
from tradingsystem.core.timeutil import MS_PER_DAY, MS_PER_HOUR

ROOT = Path(__file__).resolve().parents[2]
NOW = 1_790_000_000_000
WEEK, FIVE_H = 1_000_000, 100_000


def settings(tmp_path: Path, **usage):
    s = load_settings(ROOT / "config" / "config.yaml", env_path=tmp_path / "absent.env", extra_env={INSTANCE_ENV: ""})
    s = s.model_copy(update={"paths": PathsCfg(data_dir=str(tmp_path / "data"), logs_dir=str(tmp_path / "logs"))})
    u = s.ai.usage.model_copy(update={"weekly_token_budget": WEEK, "five_hour_token_budget": FIVE_H, **usage})
    return s.model_copy(update={"ai": s.ai.model_copy(update={"usage": u})})


@pytest.fixture
def ledger(tmp_path):
    db = tmp_path / "ai_usage.db"
    store = UsageStore(db)

    def add(ts: int, inp: int, out: int = 0, cached: int = 0, pair: str | None = "BTCUSDT", ok: int = 1,
            provider: str = "claude_code", role: str | None = None) -> None:
        con = sqlite3.connect(db)
        con.execute("INSERT INTO ai_usage(ts, provider, model, purpose, pair, input_tokens, output_tokens, "
                    "cached_tokens, cost_usd, latency_ms, ok, role) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (ts, provider, "sonnet", "decision", pair, inp, out, cached, 0.0, 1000, ok, role))
        con.commit()
        con.close()

    store.add = add
    yield store
    store.close()


class Clock:
    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        return self.t


def test_tokens_since_sums_every_row(ledger):
    assert ledger.tokens_since(0) == {"input": 0, "cached": 0, "output": 0, "calls": 0}
    ledger.add(NOW - 10, 25_000, 4_000, cached=20_000)
    ledger.add(NOW - 5, 30_000, 5_000, cached=0, pair="ETHUSDT", ok=0)          # a failed call spent tokens too
    ledger.add(NOW - 1000, 99, 1)                                             # before the window
    ledger.add(NOW - 3, 1_000, 100, pair=None)                                # the all-pairs system
    assert ledger.tokens_since(NOW - 10) == {"input": 56_000, "cached": 20_000, "output": 9_100, "calls": 3}
    assert ledger.tokens_since(NOW - 10, pair="ETHUSDT") == {"input": 30_000, "cached": 0, "output": 5_000, "calls": 1}


def test_tokens_since_filters_by_provider(ledger):
    ledger.add(NOW - 10, 1_000, 100)
    ledger.add(NOW - 9, 50_000, 5_000, provider="gemini")
    ledger.add(NOW - 8, 2_000, 200, provider="cc2")
    assert ledger.tokens_since(NOW - 10, providers=["claude_code"]) == {"input": 1_000, "cached": 0, "output": 100,
                                                                       "calls": 1}
    assert ledger.tokens_since(NOW - 10, providers=("claude_code", "cc2"))["calls"] == 2
    assert ledger.tokens_since(NOW - 10, providers=[]) == {"input": 0, "cached": 0, "output": 0, "calls": 0}
    assert ledger.tokens_since(NOW - 10)["calls"] == 3                        # no filter: every row


def test_the_gauge_counts_only_the_claude_subscription_providers(tmp_path, ledger):
    """A fallback provider's calls (Gemini while Claude is at its limit) are not subscription usage; operator
    sessions are recorded under the claude_code provider's name and count."""
    ledger.add(NOW - MS_PER_HOUR, 30_000)                                     # a trader call
    ledger.add(NOW - MS_PER_HOUR, 20_000, pair=None, role="review")          # an operator session
    ledger.add(NOW - MS_PER_HOUR, 500_000, provider="gemini")                 # the fallback
    st = UsageGauge(settings(tmp_path), ledger).state(NOW)
    assert (st.five_h_tokens, st.five_h_calls, st.level) == (50_000, 2, 0)
    s = settings(tmp_path)
    two = s.model_copy(update={"ai": s.ai.model_copy(update={
        "providers": {**s.ai.providers, "cc2": s.ai.providers["claude_code"]}})})
    ledger.add(NOW - MS_PER_HOUR, 25_000, provider="cc2")                     # a second claude_code provider counts
    assert UsageGauge(two, ledger).state(NOW).five_h_tokens == 75_000
    assert sorted(claude_providers(two)) == ["cc2", "claude_code"] and claude_providers(s) == ["claude_code"]


def test_cache_reads_count_their_weight():
    sums = {"input": 100_000, "cached": 80_000, "output": 10_000}
    assert effective_tokens(sums, 0.1) == pytest.approx(38_000)
    assert effective_tokens(sums, 0.0) == pytest.approx(30_000)
    assert effective_tokens(sums, 1.0) == pytest.approx(110_000)
    assert effective_tokens({"input": 10, "cached": 50, "output": 0}, 0.1) == pytest.approx(1.0)   # never negative


def test_the_gauge_applies_the_cache_weight(tmp_path, ledger):
    ledger.add(NOW - MS_PER_HOUR, 100_000, 10_000, cached=80_000)
    st = UsageGauge(settings(tmp_path, cache_read_weight=0.25), ledger).state(NOW)
    assert st.week_tokens == pytest.approx(20_000 + 20_000 + 10_000) and st.five_h_tokens == st.week_tokens
    assert st.week_pct == pytest.approx(5.0) and st.five_h_pct == pytest.approx(50.0) and st.level == 0


@pytest.mark.parametrize("tokens, level", [(0, 0), (699_999, 0), (700_000, 1), (899_999, 1), (900_000, 2),
                                           (2_000_000, 2)])
def test_levels_at_70_and_90_percent(tmp_path, ledger, tokens, level):
    if tokens:
        ledger.add(NOW - 6 * MS_PER_HOUR, tokens)                             # outside the 5-hour window
    st = UsageGauge(settings(tmp_path), ledger).state(NOW)
    assert st.level == level and st.five_h_tokens == 0
    assert st.week_pct == pytest.approx(100 * tokens / WEEK)
    if level == 0:
        assert st.reason == "within budget"
    else:
        want = "≥ 90 % → events only" if level == 2 else "≥ 70 % → reviews and events only"
        assert st.reason.startswith("7 d ") and want in st.reason and "5 h" not in st.reason


def test_configurable_thresholds(tmp_path, ledger):
    ledger.add(NOW - 6 * MS_PER_HOUR, 550_000)
    assert UsageGauge(settings(tmp_path, level1_pct=50, level2_pct=60), ledger).state(NOW).level == 1
    assert UsageGauge(settings(tmp_path, level1_pct=40, level2_pct=55), ledger).state(NOW).level == 2


def test_five_hours_versus_seven_days(tmp_path, ledger):
    ledger.add(NOW - 8 * MS_PER_DAY, 5_000_000)                               # outside both windows
    ledger.add(NOW - 7 * MS_PER_DAY, 100_000)                                 # the 7-day window's first ms
    ledger.add(NOW - 6 * MS_PER_HOUR, 400_000)
    ledger.add(NOW - 5 * MS_PER_HOUR, 20_000)                                 # the 5-hour window's first ms
    ledger.add(NOW - MS_PER_HOUR, 75_000)
    st = UsageGauge(settings(tmp_path), ledger).state(NOW)
    assert st.week_tokens == pytest.approx(595_000) and st.week_pct == pytest.approx(59.5)
    assert st.five_h_tokens == pytest.approx(95_000) and st.five_h_pct == pytest.approx(95.0)
    assert st.level == 2 and st.reason.startswith("5 h ") and "7 d" not in st.reason
    assert (st.week_calls, st.five_h_calls, st.as_of_ms) == (4, 2, NOW)


def test_enforce_is_published_not_applied_here(tmp_path, ledger):
    ledger.add(NOW - MS_PER_HOUR, 95_000)
    watch = UsageGauge(settings(tmp_path), ledger).state(NOW)
    act = UsageGauge(settings(tmp_path, enforce=True), ledger).state(NOW)
    assert (watch.level, watch.enforce) == (2, False) and (act.level, act.enforce) == (2, True)
    d = act.as_detail()
    assert json.loads(json.dumps(d)) == d
    assert {k: d[k] for k in ("level", "enforce", "week_tokens", "five_h_tokens", "five_h_pct")} == {
        "level": 2, "enforce": True, "week_tokens": 95_000, "five_h_tokens": 95_000, "five_h_pct": 95.0}
    assert d["reason"] == act.reason and "error" not in d


def test_default_config_on_an_empty_ledger(tmp_path, ledger):
    s = load_settings(ROOT / "config" / "config.yaml", env_path=tmp_path / "absent.env", extra_env={INSTANCE_ENV: ""})
    st = UsageGauge(s, ledger).state()
    assert (st.level, st.enforce, st.reason, st.week_tokens) == (0, False, "within budget", 0)
    assert (st.week_budget, st.five_h_budget) == (s.ai.usage.weekly_token_budget, s.ai.usage.five_hour_token_budget)


def test_the_sums_are_cached(tmp_path, ledger):
    clock = Clock()
    g = UsageGauge(settings(tmp_path, cache_s=60), ledger, clock=clock)
    first = g.state()
    ledger.add(first.as_of_ms - MS_PER_HOUR, 80_000)
    assert g.state() is first                                                 # within cache_s: no SQL
    clock.t += 59
    assert g.state() is first
    clock.t += 2
    fresh = g.state()
    assert fresh is not first and fresh.level == 1 and fresh.five_h_tokens == 80_000
    at = fresh.as_of_ms
    assert g.state(at + 1) is not fresh                                       # another explicit time is re-read
    pinned = g.state(at + 1)
    assert g.state(at + 1) is pinned and g.state() is pinned


def test_never_raises(tmp_path, ledger, caplog):
    g = UsageGauge(settings(tmp_path, enforce=True), ledger, clock=Clock())
    ledger.close()                                                            # the ledger went away
    with caplog.at_level(logging.WARNING):
        st = g.state(NOW)
        g2 = g.state(NOW + 1)
    assert isinstance(st, GaugeState) and st.level == 0 and st.enforce is True
    assert st.reason.startswith("gauge unavailable (ProgrammingError") and st.error and "error" in st.as_detail()
    assert g2.level == 0
    assert len([r for r in caplog.records if "usage gauge unavailable" in r.getMessage()]) == 1   # warned once
    broken = UsageGauge(object(), ledger).state(NOW)                          # type: ignore[arg-type]
    assert broken.level == 0 and broken.reason.startswith("gauge unavailable (AttributeError")


class Sums:
    """A ledger stand-in: both windows hold ``pct`` % of a 1 M budget (the tests set both budgets to 1 M)."""

    def __init__(self) -> None:
        self.pct, self.fail, self.providers = 0.0, False, []

    def tokens_since(self, since_ms, pair=None, providers=None):
        self.providers.append(providers)
        if self.fail:
            raise sqlite3.OperationalError("database is locked")
        return {"input": int(self.pct * 10_000), "cached": 0, "output": 0, "calls": 1}


def flat(tmp_path):
    return settings(tmp_path, weekly_token_budget=1_000_000, five_hour_token_budget=1_000_000, cache_s=60)


def test_a_level_steps_down_only_five_points_below_its_threshold(tmp_path):
    sums = Sums()
    g = UsageGauge(flat(tmp_path), sums)
    times = iter(range(NOW, NOW + 100))

    def level(pct):
        sums.pct = pct
        return g.state(next(times))                                          # another explicit time: re-read

    assert level(92).level == 2
    held = level(87)                                                          # below 90, not 5 points below: still 2
    assert held.level == 2 and "held until ≤ 85 %" in held.reason and held.reason.startswith("7 d ")
    assert level(85.5).level == 2
    assert level(85).level == 1                                               # exactly 5 points below: down
    assert level(66).level == 1                                               # below 70, not 5 points below
    assert level(65).level == 0
    assert level(70).level == 1                                               # up at the threshold itself
    assert level(90).level == 2
    assert level(50).level == 0                                               # far below both: straight down to 0
    assert all(p == ["claude_code"] for p in sums.providers)


def test_a_failed_read_keeps_the_remembered_level(tmp_path):
    sums = Sums()
    g = UsageGauge(flat(tmp_path), sums)
    sums.pct = 95
    assert g.state(NOW).level == 2
    sums.fail = True
    err = g.state(NOW + 1)
    assert err.level == 0 and err.error                                       # published as unavailable
    sums.fail, sums.pct = False, 87
    assert g.state(NOW + 2).level == 2                                        # the error did not reset the level


def test_the_engine_notifies_once_per_real_level_change_and_never_on_a_failed_read(tmp_path):
    from types import SimpleNamespace as NS
    from tradingsystem.analysis.engine import Engine
    sums, clock = Sums(), Clock()
    g = UsageGauge(flat(tmp_path), sums, clock=clock)
    sent = []
    eng = NS(_gauge_state=g.state,
             _notify=lambda level, title, text, key=None, pair=None: sent.append((level, key)))

    def read(pct, fail=False):
        sums.pct, sums.fail = pct, fail
        clock.t += 61                                                         # past ai.usage.cache_s
        return Engine._gauge_detail(eng)

    for pct in (10, 71, 69, 72, 66):                                          # hovering at 70: one notification
        read(pct)
    d = read(0, fail=True)
    assert d["level"] == 0 and d["error"] and eng._gauge_level == 1           # published, not a level change
    for pct in (67, 64, 91, 86, 84):
        read(pct)
    assert sent == [("warn", "gauge:0>1"), ("info", "gauge:1>0"), ("warn", "gauge:0>2"), ("info", "gauge:2>1")]
