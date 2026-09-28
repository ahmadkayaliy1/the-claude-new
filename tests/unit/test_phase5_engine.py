"""Phase 5 A5 (engine / provider, §3.9.1): no trader call on a closed execution market unless something of the pair
is open or pending (the setup signature follows the closed market; one INFO line an hour), cycles interrupted by a
suspend, one delayed retry of the transient Claude Code sign-in errors, a ledger row for a call cancelled after its
request went out, the session-aware quota reserve, the next_review prompt line, and the CLI capability file in
data/shared. Market data comes from the real payload fixture; the AI side is replaced by awaitable stand-ins."""
import asyncio
import copy
import datetime as dt
import json
import logging
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from tradingsystem.ai import orchestrator as orch_mod
from tradingsystem.ai import repair as repair_mod
from tradingsystem.ai.budget import CANCELLED_PREFIX, CostGovernor, RateLimiter, UsageStore
from tradingsystem.ai.contract import Recommendation
from tradingsystem.ai.orchestrator import CycleRequest
from tradingsystem.ai.prompts import render, versions
from tradingsystem.ai.providers import claude_code as cc
from tradingsystem.ai.providers.base import ProviderError
from tradingsystem.ai.repair import generate_validated
from tradingsystem.ai.store import DecisionRecord
from tradingsystem.ai.triggers import scan_setups, setup_signature
from tradingsystem.analysis.engine import Engine
from tradingsystem.core.settings import AIBudgetCfg, AIProviderCfg
from tradingsystem.core.timeutil import now_ms
from tradingsystem.supervisor.winops import ClockSample

from .test_ai_budget_repair import Scripted
from .test_ai_path_fixes import AS_OF, orch  # noqa: F401 — the orchestrator fixture (scripted provider double)
from .test_contract import BASE
from .test_engine_rationing import MIN, PAYLOAD, T0, eng, one  # noqa: F401 — the engine fixture (temp data dir)
from .test_orchestrator import rec_for

SAT_0610 = int(dt.datetime(2026, 9, 26, 6, 10, 6, tzinfo=dt.timezone.utc).timestamp() * 1000)  # XAU weekend + the
#                                                                                   Windsor crypto maintenance 05–08
PAYLOAD_SIG = sorted(setup_signature(scan_setups(PAYLOAD, liquidity_atr=0.3, screen_tf="5m")))
PAYLOAD_MID = (PAYLOAD["market"]["analysis_price"]["bid"] + PAYLOAD["market"]["analysis_price"]["ask"]) / 2


def with_ai(e, **upd):
    """The engine's settings with ``ai.<key>`` changed (the pydantic models are frozen)."""
    e.s = e.s.model_copy(update={"ai": e.s.ai.model_copy(update=upd)})
    return e


def executor_reports(e, exposure):
    """A fresh executor status row (the ``account`` block's source) holding ``exposure``."""
    e.appdb.set_status("executor", "live", last_data_ms=now_ms(),
                       detail={"mode": "demo", "equity": 100.0, "balance": 100.0, "currency": "USD", "exposure": exposure})


@pytest.fixture
def closed(eng, monkeypatch):
    """The engine on a Saturday during the Windsor crypto maintenance (every pair's execution market is closed):
    payload builds return the real XAU payload and are counted, dispatches are recorded."""
    rec = SimpleNamespace(builds=[], dispatched=[])

    def build(pair, as_of, account=None):
        rec.builds.append(pair)
        return copy.deepcopy(PAYLOAD)
    monkeypatch.setattr(eng.orch, "payload", build)
    monkeypatch.setattr(eng, "_dispatch", lambda fired, now: rec.dispatched.extend(f[0] for f in fired))
    eng.rec = rec
    return eng


# ------------------------------------------------------------------ (1) closed execution market
def test_closed_market_makes_no_trader_call_and_the_signature_follows_it(closed):
    _tick(closed, SAT_0610)
    assert closed.rec.dispatched == []
    for pair in ("BTCUSDT", "ETHUSDT", "XAUUSD"):
        assert closed.store.kv_get(f"{pair}:signature") == PAYLOAD_SIG
        assert closed.store.kv_get(f"{pair}:last_call_price") == pytest.approx(PAYLOAD_MID)
        assert closed.processed_screen[pair] == SAT_0610 - 6_000 - 5 * MIN     # the 06:05 screen bar is processed
    assert sorted(closed.rec.builds) == ["BTCUSDT", "ETHUSDT", "XAUUSD"]


def test_closed_market_rebuilds_only_when_a_new_screen_bar_was_stored(closed, monkeypatch):
    last = {"t": None}
    monkeypatch.setattr(closed.builder, "reader",
                        lambda inst: SimpleNamespace(last_time=lambda spec: last["t"]))
    _tick(closed, SAT_0610)
    _tick(closed, SAT_0610 + MIN)                              # same screen bar: nothing to do
    _tick(closed, SAT_0610 + 5 * MIN)                          # next screen, no new bar (gold at the weekend)
    assert len(closed.rec.builds) == 3                          # one per pair, once
    last["t"] = SAT_0610 - 6_000                                # Binance stored a new 5m bar meanwhile
    _tick(closed, SAT_0610 + 10 * MIN)
    assert len(closed.rec.builds) == 6 and closed.rec.dispatched == []


def test_a_failed_closed_market_build_waits_for_new_data(closed, monkeypatch, caplog):
    def broken(pair, as_of, account=None):
        closed.rec.builds.append(pair)
        raise ValueError("no bars")
    monkeypatch.setattr(closed.orch, "payload", broken)
    _tick(closed, SAT_0610)
    _tick(closed, SAT_0610 + 5 * MIN)                           # no new bar stored: not tried (nor logged) again
    assert len(closed.rec.builds) == 3 and closed.rec.dispatched == []
    assert closed.store.kv_get("XAUUSD:signature") is None
    assert sum("could not follow the closed market" in r.getMessage() for r in caplog.records) == 3


def test_reopen_does_not_fire_on_the_closed_session_structure(closed):
    """The first screen after the reopen compares with the structure seen while closed: an unchanged 1h sweep is
    not a new setup (without the follow-up it would call 'strong' on the old structure)."""
    fresh = Engine(closed.s)
    try:
        fire, _, strength = fresh.evaluate("XAUUSD", T0, True, PAYLOAD, "on_setup_event")
        assert fire and strength == "strong"                    # no signature yet: the old structure looks new
    finally:
        fresh.close()
    _tick(closed, SAT_0610)
    closed.last_call["XAUUSD"] = T0 - 60 * MIN
    fire, reasons, _ = closed.evaluate("XAUUSD", T0, True, PAYLOAD, "on_setup_event")
    assert not fire, reasons


def test_closed_market_with_an_open_position_is_still_screened(closed, monkeypatch):
    executor_reports(closed, [{"pair": "XAUUSD", "kind": "position", "decision": "abcd1234", "side": "BUY",
                               "volume": 0.01, "price": 4300.0, "sl": 4280.0}])
    closed.store.kv_set("XAUUSD:signature", ["old"])
    monkeypatch.setattr(closed, "_data_ready", lambda pair, bar, tf=None: pair == "XAUUSD")
    monkeypatch.setattr(closed, "evaluate", lambda pair, now, at_close, payload, policy=None, **kw:
                        (pair == "XAUUSD", ["event: stop hit"], "event"))
    _tick(closed, SAT_0610)
    assert closed.rec.dispatched == ["XAUUSD"]                  # management calls on a held position stay allowed
    assert closed.store.kv_get("XAUUSD:signature") == ["old"]   # … and its bookkeeping is the normal one
    assert closed.store.kv_get("BTCUSDT:signature") == PAYLOAD_SIG   # nothing held there: followed


def test_a_pending_order_counts_as_held(closed):
    executor_reports(closed, [{"pair": "XAUUSD", "kind": "order", "decision": "abcd1234", "side": "BUY",
                               "order_type": "BUY_LIMIT", "volume": 0.01, "price": 4290.0, "sl": 4280.0}])
    assert closed._holds("XAUUSD") is True and closed._holds("BTCUSDT") is False


def test_positions_unknown_when_the_executor_is_not_reporting(closed):
    assert closed._holds("XAUUSD") is None                      # no status row: not screened (Phase 4 did the same)
    _tick(closed, SAT_0610)
    assert closed.rec.dispatched == [] and closed.store.kv_get("XAUUSD:signature") == PAYLOAD_SIG


def test_skip_closed_market_off_keeps_the_phase4_behaviour(closed):
    with_ai(closed, skip_closed_market=False)
    executor_reports(closed, [{"pair": "XAUUSD", "kind": "position", "decision": "abcd1234", "side": "BUY"}])
    _tick(closed, SAT_0610)
    assert closed.rec.dispatched == [] and closed.rec.builds == []
    assert closed.store.kv_get("XAUUSD:signature") is None and closed.processed_screen == {}


def test_closed_market_logs_one_info_line_per_hour_per_pair(closed, caplog):
    caplog.set_level(logging.INFO, logger="engine")
    for dt_ms in (0, 2_000, 5 * MIN, 59 * MIN, 61 * MIN):
        _tick(closed, SAT_0610 + dt_ms)
    lines = [r.getMessage() for r in caplog.records if "execution market closed" in r.getMessage()]
    assert len([x for x in lines if x.startswith("XAUUSD")]) == 2          # at the first look and an hour later
    assert all(r.levelno == logging.INFO for r in caplog.records if "execution market closed" in r.getMessage())


def test_the_market_closed_note_is_reset_when_the_market_opens(closed, caplog, monkeypatch):
    caplog.set_level(logging.INFO, logger="engine")
    monkeypatch.setattr(closed, "_data_ready", lambda *a, **k: False)
    _tick(closed, SAT_0610)
    _tick(closed, SAT_0610 + 3 * 3_600_000)                     # 09:10 UTC: the crypto pairs are open again
    assert "BTCUSDT" not in closed._closed_note_at and "XAUUSD" in closed._closed_note_at


def _tick(e, now):
    """One engine tick at ``now`` (the calendars read ``now``; ``now_ms`` in the engine module is pinned to it)."""
    from tradingsystem.analysis import engine as eng_mod
    real = eng_mod.now_ms
    eng_mod.now_ms = lambda: now
    try:
        asyncio.run(e.tick())
    finally:
        eng_mod.now_ms = real


# ------------------------------------------------------------------ (2) a cycle interrupted by a suspend
class FakeClock:
    """``sample_clock`` stand-in: the tick clock runs ``slept`` seconds ahead of the awake clock after a 'suspend'."""

    def __init__(self):
        self.slept = 0.0

    def __call__(self):
        t = time.monotonic()
        return ClockSample(time.time(), t + self.slept, t)


def test_a_cycle_cut_off_after_a_suspend_is_stored_interrupted(orch, monkeypatch):  # noqa: F811
    clock = FakeClock()
    monkeypatch.setattr(orch_mod, "sample_clock", clock)

    async def asleep_then_slow():
        clock.slept = 120.0                                      # the PC slept 2 min while the CLI ran
        await asyncio.sleep(30)
        return rec_for()

    o, _, store = orch(lambda schema, user: asleep_then_slow())
    [r] = asyncio.run(o.run_cycle([CycleRequest("XAUUSD", "t")], as_of=AS_OF, deadline_s=0.3))
    assert r.status == "interrupted" and "deadline" in r.errors[0] and "asleep ~120s" in r.errors[-1]
    assert one_status(store) == "interrupted"
    assert store.last_attempt_ts("XAUUSD", answered=True) is None          # nothing was seen


def test_a_failed_unit_without_a_suspend_stays_an_error(orch, monkeypatch):  # noqa: F811
    monkeypatch.setattr(orch_mod, "sample_clock", FakeClock())

    def act(schema, user):
        raise ProviderError("claude_code: API Error: 500", retryable=False)
    o, _, _ = orch(act)
    [r] = asyncio.run(o.run_cycle([CycleRequest("XAUUSD", "t")], as_of=AS_OF))
    assert r.status == "error"


def test_an_interrupted_cycle_does_not_raise_the_backoff(eng, monkeypatch):  # noqa: F811
    eng.fails["XAUUSD"] = 1
    eng.store.kv_set("XAUUSD:signature", ["old"])
    eng._sig = {"XAUUSD": ["new"]}

    async def interrupted(queue, **kw):
        return [DecisionRecord("XAUUSD", "agent_per_pair", "t", "interrupted", errors=["cycle deadline"])]
    monkeypatch.setattr(eng.orch, "run_cycle", interrupted)

    async def go():
        eng._remember_call("XAUUSD", PAYLOAD, "strong")
        await eng._cycle([CycleRequest("XAUUSD", "t", "strong")], {"XAUUSD": PAYLOAD}, T0)
    asyncio.run(go())
    assert eng.fails["XAUUSD"] == 1                              # unchanged: neither raised nor reset
    assert eng.store.kv_get("XAUUSD:signature") == ["old"]       # the setup stays unseen: the next screen calls


def test_a_cycle_that_raised_across_a_suspend_does_not_raise_the_backoff(eng, monkeypatch):  # noqa: F811
    clock = FakeClock()
    monkeypatch.setattr(orch_mod, "sample_clock", clock)
    nap = {"s": 300.0}

    async def boom(queue, **kw):
        clock.slept += nap["s"]
        raise RuntimeError("event loop woke up to a dead pipe")
    monkeypatch.setattr(eng.orch, "run_cycle", boom)
    asyncio.run(eng._cycle([CycleRequest("XAUUSD", "t", "strong")], {"XAUUSD": PAYLOAD}, T0))
    assert eng.fails.get("XAUUSD", 0) == 0
    nap["s"] = 0.0
    asyncio.run(eng._cycle([CycleRequest("XAUUSD", "t", "strong")], {"XAUUSD": PAYLOAD}, T0))
    assert eng.fails["XAUUSD"] == 1                              # without a suspend it is a failed cycle


def one_status(store):
    return store._con.execute("SELECT status FROM ai_decisions ORDER BY ts DESC LIMIT 1").fetchone()[0]


# ------------------------------------------------------------------ (3) transient CLI errors: one delayed retry
@pytest.fixture
def usage(tmp_path):
    u = UsageStore(tmp_path / "ai_usage.db")
    yield u
    u.close()


def gen(provider, usage, **kw):
    return asyncio.run(generate_validated(provider, Recommendation, system="s", user="u",
                                          limiter=RateLimiter(provider.name, provider.cfg, usage),
                                          governor=CostGovernor(AIBudgetCfg(), usage), usage=usage,
                                          purpose="agent_per_pair", pair="BTCUSDT", role="decision", **kw))


@pytest.fixture
def pauses(monkeypatch):
    got = []

    async def no_wait(s):
        got.append(s)
    monkeypatch.setattr(repair_mod.asyncio, "sleep", no_wait)
    return got


def rows(usage):
    return usage._con.execute("SELECT ok, error FROM ai_usage ORDER BY id").fetchall()


RACE = "claude_code: sign-in token refresh race (Failed to refresh OAuth token: another Claude Code process …)"


def test_transient_cli_error_gets_one_retry_after_the_pause(usage, pauses):
    p = Scripted([ProviderError(RACE, retryable=True, transient=True), copy.deepcopy(BASE)])
    g = gen(p, usage, transient_retry_s=60)
    assert g.ok and g.provider_error is None and pauses == [60]
    assert [ok for ok, _ in rows(usage)] == [0, 1]               # the failed attempt is a ledger row (D-043)


def test_transient_cli_error_is_retried_only_once(usage, pauses):
    p = Scripted([ProviderError(RACE, retryable=True, transient=True)] * 3 + [copy.deepcopy(BASE)])
    g = gen(p, usage, transient_retry_s=60)
    assert not g.ok and g.provider_error and pauses == [60] and len(p.calls) == 2


def test_transient_retry_needs_room_before_the_cycle_deadline(usage, pauses):
    p = Scripted([ProviderError(RACE, retryable=True, transient=True), copy.deepcopy(BASE)])
    g = gen(p, usage, transient_retry_s=60, retry_until=time.monotonic() + 100)   # 60 s pause + 120 s call > 100 s
    assert not g.ok and pauses == [] and len(p.calls) == 1


def test_transient_retry_off_keeps_the_quick_retries(usage, pauses):
    p = Scripted([ProviderError(RACE, retryable=True, transient=True)] * 2 + [copy.deepcopy(BASE)])
    g = gen(p, usage, transient_retry_s=0)
    assert g.ok and pauses == [2, 4]                               # Phase 4: 2 s, 4 s, 8 s back-off


def test_a_non_transient_retryable_error_keeps_the_quick_retries(usage, pauses):
    p = Scripted([ProviderError("claude_code: API Error: 529 Overloaded", retryable=True), copy.deepcopy(BASE)])
    assert gen(p, usage, transient_retry_s=60).ok and pauses == [2]


@pytest.mark.parametrize("text", [
    "Failed to refresh OAuth token: another Claude Code process is refreshing it or exited mid-refresh.",
    "Failed to authenticate. API Error: 403 Request not allowed",
    "Failed to refresh OAuth token",
])
def test_the_cli_marks_the_sign_in_hiccups_transient(prov, text):
    e = prov._error(text, None)
    assert e.transient and e.retryable and not e.rate_limited and prov.cooldown_until_ms == 0


def test_other_cli_errors_are_not_transient(prov):
    assert not prov._error("API Error: 529 Overloaded", 529).transient
    assert not prov._error("Not logged in · Please run /login", None).transient


def test_the_orchestrator_passes_the_configured_pause_and_the_cycle_deadline(orch, monkeypatch):  # noqa: F811
    seen = {}
    real = orch_mod.generate_validated

    async def spy(*a, **kw):
        seen.update(pause=kw["transient_retry_s"], until=kw["retry_until"])
        return await real(*a, **kw)
    monkeypatch.setattr(orch_mod, "generate_validated", spy)
    o, _, _ = orch(lambda schema, user: rec_for())
    t0 = time.monotonic()
    [r] = asyncio.run(o.run_cycle([CycleRequest("XAUUSD", "t")], as_of=AS_OF, deadline_s=300))
    assert r.status == "valid" and seen["pause"] == o.s.ai.transient_retry_s == 60
    assert t0 + 300 <= seen["until"] <= time.monotonic() + 300


# ------------------------------------------------------------------ (4) a cancelled call is a ledger row
class HangingProc:
    """A CLI process that never answers (the call is cancelled while it runs)."""

    def __init__(self):
        self.returncode, self.killed = None, False

    async def communicate(self, data=None):
        await asyncio.sleep(3600)

    def kill(self):
        self.killed, self.returncode = True, -9

    async def wait(self):
        return self.returncode


@pytest.fixture
def prov(tmp_path, monkeypatch):
    exe = tmp_path / "claude.exe"
    exe.write_bytes(b"")
    monkeypatch.setattr(cc.tempfile, "gettempdir", lambda: str(tmp_path / "tmp"))
    (tmp_path / "tmp").mkdir()
    monkeypatch.setattr(cc, "START_STAGGER_S", 0.0)
    cfg = AIProviderCfg(kind="claude_code", model="sonnet", effort="medium", free_tier=True, cli_path=str(exe))
    p = cc.ClaudeCodeProvider("claude_code", cfg, "sonnet", "token")      # a token: no `claude auth status`
    p._auth_problem, p._auth_checked = None, time.monotonic()
    return p


def cancel_while_running(prov, usage, monkeypatch, *, block_start: bool = False):
    """Start one decision call and cancel it once the CLI runs (or, ``block_start``, while it still waits for the
    machine-wide start stagger); returns the CLI processes started."""
    procs = []

    async def main():
        spawned = asyncio.Event()

        async def spawn(*args, **kw):
            procs.append(HangingProc())
            spawned.set()
            return procs[-1]
        monkeypatch.setattr(cc.asyncio, "create_subprocess_exec", spawn)
        if block_start:
            gate = asyncio.Event()

            async def never():
                spawned.set()                                   # "waiting for the start stagger"
                await gate.wait()
            monkeypatch.setattr(prov, "_staggered_start", never)
        task = asyncio.ensure_future(generate_validated(
            prov, Recommendation, system="s", user="u", limiter=RateLimiter(prov.name, prov.cfg, usage),
            governor=CostGovernor(AIBudgetCfg(), usage), usage=usage, purpose="agent_per_pair", pair="BTCUSDT",
            role="decision"))
        await spawned.wait()
        await asyncio.sleep(0.01)
        task.cancel("cycle deadline of 600s reached")
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(main())
    return procs


def test_a_call_cancelled_after_the_cli_started_writes_a_ledger_row(prov, usage, monkeypatch):
    procs = cancel_while_running(prov, usage, monkeypatch)
    assert procs and procs[0].killed                              # no orphan keeps spending the plan
    [(ok, error)] = rows(usage)
    assert ok == 0 and error.startswith(CANCELLED_PREFIX + "cycle deadline of 600s reached") and "tokens unknown" in error
    lim = RateLimiter("claude_code", prov.cfg, usage, instance="BTCUSDT", daily_cap=30)
    assert lim.used_today() == (1, 30)                            # it counts towards the pair's day (D-043)
    assert usage.unknown_since(0) == 1                            # … and the usage gauge knows its tokens are unknown


def test_a_call_cancelled_before_the_cli_started_writes_no_row(prov, usage, monkeypatch):
    procs = cancel_while_running(prov, usage, monkeypatch, block_start=True)
    assert procs == [] and rows(usage) == []


def test_the_cycle_deadline_cancellation_is_in_the_ledger(orch, monkeypatch):  # noqa: F811
    async def slow():
        await asyncio.sleep(30)
        return rec_for()
    o, _, _ = orch(lambda schema, user: slow())
    [r] = asyncio.run(o.run_cycle([CycleRequest("XAUUSD", "t")], as_of=AS_OF, deadline_s=0.2))
    assert r.status == "error"
    [(ok, error)] = o.usage._con.execute("SELECT ok, error FROM ai_usage").fetchall()
    assert ok == 0 and error.startswith("cancelled: cycle deadline of 0s reached")


# ------------------------------------------------------------------ (5) the session-aware quota reserve
FIRED = [("XAUUSD", ["15m BOS"], "strong", None), ("XAUUSD", ["event: stop hit"], "event", None)]
H11, H13, H22 = T0, T0 + 2 * 3_600_000, T0 + 11 * 3_600_000     # 11:10, 13:10, 22:10 UTC


@pytest.mark.parametrize("now,used,kept", [
    (H11, 17, ["strong", "event"]),                   # 17 < floor(0.6 × 30) = 18
    (H11, 18, ["event"]),                             # the reserve binds before 12:00 …
    (H22, 25, ["event"]),                             # … and after 21:00
    (H13, 29, ["strong", "event"]),                   # inside the busy hours the whole cap is there
])
def test_quota_reserve_keeps_calls_for_the_busy_hours(eng, monkeypatch, now, used, kept):  # noqa: F811
    monkeypatch.setattr(eng.orch, "quota", lambda name=None: (None, None))
    monkeypatch.setattr(eng.orch, "quota_used", lambda name=None: (used, 30))
    assert [f[2] for f in eng._ration(list(FIRED), now)] == kept


def test_quota_reserve_is_logged_like_the_quota_messages(eng, monkeypatch, caplog):  # noqa: F811
    monkeypatch.setattr(eng.orch, "quota", lambda name=None: (None, None))
    monkeypatch.setattr(eng.orch, "quota_used", lambda name=None: (18, 30))
    for _ in range(3):
        eng._ration(list(FIRED[:1]), H11)
    [(n,)] = [one(eng, "SELECT count(*) FROM ingestion_events WHERE event='ai_quota'")]
    assert n == 1                                                  # quietly: once, not on every screen
    assert any("kept for 12:00–21:00 UTC" in r.getMessage() and "18/30" in r.getMessage() for r in caplog.records)


def test_quota_reserve_zero_is_off(eng, monkeypatch):  # noqa: F811
    with_ai(eng, quota_reserve_share=0.0)
    monkeypatch.setattr(eng.orch, "quota", lambda name=None: (None, None))
    monkeypatch.setattr(eng.orch, "quota_used", lambda name=None: (29, 30))
    assert [f[2] for f in eng._ration(list(FIRED), H11)] == ["strong", "event"]


def test_quota_reserve_needs_a_per_pair_cap(eng, monkeypatch):  # noqa: F811
    monkeypatch.setattr(eng.orch, "quota", lambda name=None: (None, None))
    assert eng.orch.quota_used() is None                           # the all-pairs system: no per-pair cap
    assert len(eng._ration(list(FIRED), H11)) == 2


def test_used_today_counts_every_ledger_row_of_the_pair(usage):
    cfg = AIProviderCfg(kind="claude_code", model="sonnet", rpd=120)
    for pair in ["BTCUSDT"] * 5 + ["ETHUSDT"] * 2:
        usage.record(None, provider="claude_code", model="sonnet", purpose="p", pair=pair, ok=True, role="decision")
    assert RateLimiter("claude_code", cfg, usage, instance="BTCUSDT", daily_cap=30).used_today() == (5, 30)
    assert RateLimiter("claude_code", cfg, usage).used_today() is None


# ------------------------------------------------------------------ (6) the next_review prompt line
def test_core_rules_v7_bound_next_review_on_a_flat_no_trade(orch):  # noqa: F811
    assert versions()["shared/core_rules"] == 7
    o, _, _ = orch(lambda schema, user: rec_for())
    system = render("agent_per_pair", o._system_vars("XAUUSD", {}),
                    o._user_vars("XAUUSD", AS_OF, "t", "{}")).system            # no new $placeholder: renders
    assert "NO_TRADE and nothing of this pair is open or pending" in system
    assert "`next_review.in_minutes` to at least 30" in system


# ------------------------------------------------------------------ (7) the CLI capability file in data/shared
def test_cli_capabilities_are_written_to_data_shared(prov, tmp_path):
    shared = tmp_path / "data" / "shared"
    prov.caps_dir = shared
    prov._remember_shape("content", '{"type":"system","claude_code_version":"2.1.300"}\n')
    assert json.loads((shared / cc.CAPS_FILE).read_text(encoding="utf-8"))["stream_json_user_shape"] == "content"
    assert not (prov.workdir / cc.CAPS_FILE).exists()
    assert prov.known_capabilities()["cli_version"] == "2.1.300"


def test_the_old_capability_file_is_read_once_and_moved(prov, tmp_path):
    cc.write_capabilities(prov.workdir, {"stream_json_user_shape": "content", "cli_version": "2.1.28x"})
    prov.caps_dir = tmp_path / "data" / "shared"
    assert prov.known_capabilities()["stream_json_user_shape"] == "content"
    assert cc.read_capabilities(prov.caps_dir)["cli_version"] == "2.1.28x"          # copied to data/shared
    (prov.workdir / cc.CAPS_FILE).unlink()
    assert prov.known_capabilities()["stream_json_user_shape"] == "content"         # now read from data/shared


def test_the_old_location_is_looked_at_only_once(prov, tmp_path, monkeypatch):
    prov.caps_dir = tmp_path / "data" / "shared"
    looked = []
    real = cc.read_capabilities
    monkeypatch.setattr(cc, "read_capabilities", lambda d: looked.append(Path(d)) or real(d))
    assert prov.known_capabilities() == {} and prov.known_capabilities() == {}
    assert looked.count(prov.workdir) == 1


def test_the_orchestrator_points_the_cli_at_data_shared(orch, monkeypatch):  # noqa: F811
    o, prov, _ = orch(lambda schema, user: rec_for())
    fake = SimpleNamespace(name="claude_code", cfg=prov.cfg, caps_dir=None, unavailable_reason=lambda: None)
    monkeypatch.setattr(orch_mod, "make_provider", lambda settings, name=None, **kw: fake)
    o._providers.clear()
    assert o._get("claude_code") is fake and fake.caps_dir == o.s.paths.shared()
