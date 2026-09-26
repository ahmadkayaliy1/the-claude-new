"""Phase 2 review findings (2026-09-27): each confirmed defect pinned by a test."""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import time
import types
from pathlib import Path

import pytest

from tradingsystem.ai import budget
from tradingsystem.ai.providers import claude_code as cc
from tradingsystem.core.filelock import FileLock
from tradingsystem.core.settings import INSTANCE_ENV, AIProviderCfg, load_settings
from tradingsystem.execution import drawdown
from tradingsystem.execution import executor as ex
from tradingsystem.ingest.mt5 import backfill as mt5bf
from tradingsystem.supervisor import control, procs
from tradingsystem.supervisor import supervisor as sv

NS = types.SimpleNamespace
ROOT = Path(__file__).resolve().parents[2]


def inst(pair: str, data_dir: Path | None = None):
    s = load_settings(extra_env={INSTANCE_ENV: pair})
    return sv.with_data_dir(s, str(data_dir)) if data_dir else s


def tool(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


# ------------------------------------------------------------------ proc-4: --instance before `run`
def test_supervisor_command_with_the_instance_before_run_is_recognised():
    base = ["python.exe", "-m", "tradingsystem"]
    assert procs.is_supervisor_cmd(base + ["--instance", "BTCUSDT", "run", "all"])
    assert procs.is_supervisor_cmd(base + ["--instance=BTCUSDT", "run", "all"])
    assert not procs.is_supervisor_cmd(base + ["--instance", "BTCUSDT", "run", "--status"])
    assert not procs.is_supervisor_cmd(base + ["--instance", "BTCUSDT", "engine"])


# ------------------------------------------------------------------ proc-5: the other kind's lock is probed too
def test_conflicts_are_found_by_the_other_systems_lock(tmp_path, monkeypatch):
    legacy = sv.with_data_dir(load_settings(extra_env={INSTANCE_ENV: ""}), str(tmp_path))
    btc = inst("BTCUSDT", tmp_path)
    monkeypatch.setattr(procs, "running_supervisors", lambda older_s=None: {})     # not visible by command line
    held = {control.instance_name("supervisor", tmp_path / "instances" / "ETHUSDT")}
    monkeypatch.setattr(control, "instance_running", lambda name: name in held)
    assert control.conflicting(legacy) == {-1: "ETHUSDT"}          # the all-pairs system sees the ETH lock
    assert control.conflicting(btc) == {}                           # a pair runs next to another pair
    held.add(control.instance_name("supervisor", tmp_path))
    assert control.conflicting(btc) == {-1: None}                   # ... but not next to the all-pairs system
    assert procs.describe({-1: None}) == "a supervisor (all pairs)"


def test_main_exit_codes_tell_already_running_from_refused(monkeypatch):
    monkeypatch.setattr(sv, "setup_from_settings", lambda *a, **k: None)
    monkeypatch.setattr(sv, "acquire_instance", lambda name, wait_s=0: True)
    monkeypatch.setattr(control, "instance_running", lambda name: False)
    monkeypatch.setattr(sv, "Supervisor", lambda *a, **k: pytest.fail("must not start"))
    monkeypatch.setattr(procs, "running_supervisors", lambda older_s=None: {1234: None})
    monkeypatch.setenv(INSTANCE_ENV, "")
    assert sv.main(["all"]) == control.EXIT_ALREADY_RUNNING       # an older build of the same system
    monkeypatch.setenv(INSTANCE_ENV, "BTCUSDT")
    assert sv.main(["all"]) == control.EXIT_REFUSED               # another kind of system


# ------------------------------------------------------------------ proc-2 / ops-3: no un-migrated pair start
def test_a_pair_without_its_own_history_is_refused_while_the_all_pairs_history_exists(tmp_path, monkeypatch):
    btc = inst("BTCUSDT", tmp_path)
    state = btc.paths.state()
    assert control.unmigrated(btc, state) is None                 # fresh install: nothing to migrate
    con = sqlite3.connect(tmp_path / "app.db")
    con.executescript("CREATE TABLE ai_decisions (id TEXT, pair TEXT); INSERT INTO ai_decisions VALUES ('a','BTCUSDT');")
    con.commit()
    con.close()
    eth = inst("ETHUSDT", tmp_path)
    assert control.unmigrated(eth, eth.paths.state()) is None      # no ETH history: a pair added later just starts
    why = control.unmigrated(btc, state)
    assert why and "switch_to_pairs.bat" in why and "migrate_instance.py BTCUSDT" in why and "1 records" in why
    monkeypatch.setattr(control, "instance_running", lambda name: False)
    monkeypatch.setattr(procs, "running_supervisors", lambda older_s=None: {})
    monkeypatch.setattr(control, "spawn_outside", lambda *a, **k: pytest.fail("must not start"))
    assert control.detach(["all", "--instance", "BTCUSDT"], state, btc, auto=True) == 1
    state.mkdir(parents=True)
    (state / "app.db").write_bytes(b"")
    assert control.unmigrated(btc, state) is None
    assert "switch_to_pairs.bat" in control.conflict_message("BTCUSDT", {7: None})


# ------------------------------------------------------------------ ops-4: stop.bat without a pair in per-pair mode
def test_stopping_the_all_pairs_system_says_that_pairs_still_run(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(control, "instance_running", lambda name: False)
    monkeypatch.setattr(procs, "running_supervisors", lambda older_s=None: {11: "BTCUSDT", 22: "ETHUSDT"})
    assert control.stop(tmp_path, instance=None, timeout=0) == 1
    out = capsys.readouterr().out
    assert "per-pair systems ARE running" in out and "BTCUSDT" in out and "stop_all.bat" in out


# ------------------------------------------------------------------ proc-6: the auth thread survives a stamp error
def test_auth_check_recovers_from_an_unusable_stamp_file(tmp_path, monkeypatch):
    exe = tmp_path / "claude.exe"
    exe.write_bytes(b"")
    monkeypatch.setattr(cc.tempfile, "gettempdir", lambda: str(tmp_path))
    prov = cc.ClaudeCodeProvider("claude_code", AIProviderCfg(kind="claude_code", model="sonnet", cli_path=str(exe)),
                                 "sonnet", None)

    def boom(*a, **k):
        raise PermissionError("locked by a scanner")

    monkeypatch.setattr(cc, "claim_start", boom)
    monkeypatch.setattr(prov, "check_auth", lambda: pytest.fail("no CLI start without the spacing"))
    prov._auth_refreshing = True
    prov._refresh_auth()
    assert not prov._auth_refreshing                                # the next look starts a new check
    assert "cannot space the CLI start" in prov._auth_problem       # never signed in yet: the reason is shown
    assert time.monotonic() - prov._auth_checked > cc.AUTH_CHECK_TTL_S - cc.AUTH_RECHECK_S - 5   # retried in ~1 min


# ------------------------------------------------------------------ proc-7: a stuck history-lock holder costs one wait
def test_history_calls_stop_waiting_for_a_stuck_lock_holder(tmp_path, monkeypatch):
    monkeypatch.setattr(mt5bf, "HEAVY_LOCK_WAIT_S", 0.3)
    b = object.__new__(mt5bf.MT5Backfill)
    b.heavy_lock, b._lock_warned, b._lock_busy_until = FileLock(tmp_path / "h.lock"), False, 0.0
    b.term = NS(mt5=NS(last_error=lambda: (1, "ok")), healthy=lambda: True)
    stuck = FileLock(tmp_path / "h.lock")
    assert stuck.acquire()
    t0 = time.monotonic()
    assert b._call("copy", lambda: [1]) == [1]
    assert time.monotonic() - t0 >= 0.25                            # one full wait
    t1 = time.monotonic()
    assert b._call("copy", lambda: [2]) == [2]
    assert time.monotonic() - t1 < 0.2                              # then only a try
    stuck.release()
    assert b._call("copy", lambda: [3]) == [3] and b._lock_busy_until == 0.0 and not b.heavy_lock.held


# ------------------------------------------------------------------ money-1: a bad peak file never clears a trip
def test_a_corrupt_peak_file_falls_back_to_the_backup_and_keeps_the_trip(tmp_path):
    a = drawdown.AccountPeak(tmp_path, "mt5:demo:1", 25.0)
    a.update(100.0)
    assert a.update(70.0).tripped                                   # the trip is the last write (MT5 mode)
    assert a.update(80.0).tripped                                   # recovered equity: no write, still tripped
    (tmp_path / drawdown.FILE).write_bytes(b"\x00" * 80)            # power loss / hand edit
    st = a.update(80.0)                                             # 20 % below the peak: only the record says tripped
    assert st.tripped and st.peak == 100.0
    (tmp_path / drawdown.FILE).write_bytes(b"{broken")
    (tmp_path / (drawdown.FILE + ".bak")).write_bytes(b"{also broken")
    with pytest.raises(drawdown.PeakFileError):
        a.update(72.0)                                              # the gate fails closed (retried, then rejected)


def test_an_unreadable_peak_file_fails_closed(tmp_path):
    a = drawdown.AccountPeak(tmp_path, "mt5:demo:1", 25.0)
    a.update(100.0)
    (tmp_path / drawdown.FILE).unlink()
    (tmp_path / drawdown.FILE).mkdir()                              # reading it raises an OSError (like a locked file)
    with pytest.raises(drawdown.PeakFileError, match="cannot be read"):
        a.update(80.0)


def test_a_busy_peak_lock_raises_instead_of_an_unrecorded_state(tmp_path):
    a = drawdown.AccountPeak(tmp_path, "mt5:demo:1", 25.0)
    other = FileLock(tmp_path / "locks" / "account_peak.lock")
    assert other.acquire()
    try:
        t0 = time.monotonic()
        with pytest.raises(drawdown.PeakFileError):
            a.update(100.0)
        assert time.monotonic() - t0 >= 4
    finally:
        other.release()


def test_reset_works_on_a_corrupt_file(tmp_path):
    (tmp_path / drawdown.FILE).write_bytes(b"{broken")
    assert drawdown.reset(tmp_path) == ["(unreadable file replaced)"]
    assert json.loads((tmp_path / drawdown.FILE).read_text(encoding="utf-8")) == {}


# ------------------------------------------------------------------ ops-2 / proc-3: per-pair kill switch everywhere
def test_a_pairs_kill_switch_also_stops_that_pair_in_the_all_pairs_system(tmp_path):
    legacy = sv.with_data_dir(load_settings(extra_env={INSTANCE_ENV: ""}), str(tmp_path))
    e = object.__new__(ex.Executor)
    e.s = legacy
    assert not e.kill_switch("BTCUSDT")
    (tmp_path / "instances" / "BTCUSDT").mkdir(parents=True)
    (tmp_path / "instances" / "BTCUSDT" / "KILL_SWITCH").write_text("x", encoding="utf-8")
    assert e.kill_switch("BTCUSDT") and not e.kill_switch("ETHUSDT")
    assert e.kill_switch() == ("BTCUSDT" in legacy.enabled_pairs())   # status: any switch of this system
    (tmp_path / "KILL_SWITCH").write_text("x", encoding="utf-8")
    assert e.kill_switch("ETHUSDT")


# ------------------------------------------------------------------ money-2: one MT5 placement at a time
def test_mt5_gate_and_placement_are_serialised_across_systems(tmp_path, monkeypatch):
    e = object.__new__(ex.Executor)
    e.s, e.mt5 = inst("ETHUSDT", tmp_path), object()
    calls = []
    e._handle = lambda cand: calls.append(cand) or True
    monkeypatch.setattr(ex, "PLACEMENT_LOCK_WAIT_S", 0.2)
    monkeypatch.setattr(ex, "MT5_SETTLE_MS", 0)
    other = FileLock(tmp_path / "shared" / "locks" / "mt5_placement.lock")
    assert other.acquire()
    with pytest.raises(ex.PlacementBusy, match="placement lock busy"):
        e.handle({"id": "x"})
    assert calls == []
    other.release()
    e.handle({"id": "y"})
    assert calls == [{"id": "y"}]


def test_a_busy_placement_lock_ends_the_pass_without_counting_a_failure(monkeypatch):
    e = object.__new__(ex.Executor)
    e.mt5_quiet_until, e.attempts = 0, {}
    tried = []
    e.candidates = lambda: [{"id": "a", "pair": "BTCUSDT"}, {"id": "b", "pair": "BTCUSDT"}]

    def busy(cand):
        tried.append(cand["id"])
        raise ex.PlacementBusy("busy")

    e.handle = busy
    e._handle_failed = lambda *a: pytest.fail("a busy lock is not a failed attempt")
    e.process_candidates()
    assert tried == ["a"] and e.attempts == {}                      # one wait per pass, not one per candidate


def test_settle_wait_also_after_a_placement_that_raised(tmp_path, monkeypatch):
    e = object.__new__(ex.Executor)
    e.s, e.mt5 = inst("ETHUSDT", tmp_path), object()
    slept = []
    monkeypatch.setattr(ex.time, "sleep", lambda s: slept.append(s))

    def partly(cand):
        e._placing = True                                           # order_send reached the broker ...
        raise RuntimeError("... then the terminal dropped")

    e._handle = partly
    with pytest.raises(RuntimeError, match="terminal dropped"):
        e.handle({"id": "z"})
    assert slept == [ex.MT5_SETTLE_MS / 1000]


# ------------------------------------------------------------------ money-5: the ratio uses this pair's AI cost
def test_governor_window_cost_is_the_pairs_own(tmp_path):
    usage = budget.UsageStore(tmp_path / "ai_usage.db")
    for pair, cost in (("BTCUSDT", 2.0), ("ETHUSDT", 5.0)):
        res = NS(input_tokens=1, output_tokens=1, cached_input_tokens=0, cost_usd=cost, latency_ms=1,
                 request_id=None, extra={})
        usage.record(res, provider="p", model="m", purpose="decision", pair=pair, ok=True)
    assert usage.cost_since(0) == 7.0 and usage.cost_since(0, pair="BTCUSDT") == 2.0
    cfg = load_settings().ai.budget
    gov = budget.CostGovernor(cfg, usage, profit_fn=lambda since: (100.0, 0), instance="BTCUSDT")
    st = gov.state()
    assert st.window_cost == 2.0
    usage.close()


# ------------------------------------------------------------------ money-8: disabled pairs without a profile symbol
def test_symbol_map_skips_disabled_pairs_without_a_symbol(monkeypatch):
    btc = inst("BTCUSDT")
    real = ex._resolve_symbol

    def picky(icfg, profile):
        if icfg.symbol_by_profile and "XAUUSD@" in json.dumps(icfg.symbol_by_profile):
            raise ValueError("no symbol configured for MT5 profile 'live'")
        return real(icfg, profile)

    monkeypatch.setattr(ex, "_resolve_symbol", picky)
    m = ex.all_pairs_by_symbol(btc, btc.mt5.data_profile)
    assert "XAUUSD" not in m.values() and "BTCUSDT" in m.values()
    xau = inst("XAUUSD")
    with pytest.raises(ValueError):
        ex.all_pairs_by_symbol(xau, xau.mt5.data_profile)          # an enabled pair still fails loudly


# ------------------------------------------------------------------ proc-8 / ops-9: the report follows what runs
def test_health_report_reports_the_running_systems_and_the_missing_ones(tmp_path, monkeypatch):
    hr = tool("health_report")
    a = NS(instance=None, all_pairs_system=False)
    monkeypatch.setenv(INSTANCE_ENV, "")
    monkeypatch.setattr(hr, "load_settings", lambda **kw: sv.with_data_dir(load_settings(**kw), str(tmp_path)))
    monkeypatch.setattr(hr.procs, "running_supervisors", lambda older_s=None: {5: None})
    assert [s.paths.instance for s in hr.systems(a)[0]] == [None]
    for p in ("BTCUSDT", "ETHUSDT", "XAUUSD"):
        (tmp_path / "instances" / p / "run").mkdir(parents=True)
        (tmp_path / "instances" / p / "app.db").write_bytes(b"")
    (tmp_path / "instances" / "XAUUSD" / "run" / "manual_stop").write_text("1", encoding="utf-8")   # stopped by the user
    monkeypatch.setattr(hr.procs, "running_supervisors", lambda older_s=None: {6: "BTCUSDT", 7: "EURUSD"})
    chosen, notes = hr.systems(a)
    assert [s.paths.instance for s in chosen] == ["BTCUSDT", "ETHUSDT"]     # ETH should run: reported, not dropped
    assert any("ETHUSDT" in n and "no supervisor runs" in n for n in notes)
    assert any("EURUSD" in n and "not in config" in n for n in notes)


# ------------------------------------------------------------------ ops-7: WAL-safe migration
def test_migration_removes_a_stale_wal_next_to_the_new_file(tmp_path):
    m = tool("migrate_instance")
    src = tmp_path / "app.db"
    con = sqlite3.connect(src)
    con.executescript("CREATE TABLE ai_decisions (id TEXT, pair TEXT); INSERT INTO ai_decisions VALUES ('a','BTCUSDT');")
    con.commit()
    con.close()
    dst = tmp_path / "instances" / "BTCUSDT" / "app.db"
    dst.parent.mkdir(parents=True)
    Path(str(dst) + "-wal").write_bytes(b"stale")
    m.copy_for_pair(src, dst, "BTCUSDT")
    assert not Path(str(dst) + "-wal").exists()
    assert sqlite3.connect(dst).execute("SELECT count(*) FROM ai_decisions").fetchone()[0] == 1


# ------------------------------------------------------------------ live finding 2026-09-27: XAUUSD has no Binance spot
def test_watchdog_only_watches_the_venues_this_system_feeds():
    assert sv.service_beats("ingest-binance", {"binance_usdm", "mt5"}) == (["binance_usdm"], 120)   # XAUUSD
    assert sv.service_beats("ingest-binance", {"binance_spot", "binance_usdm", "mt5"}) == (["binance_spot", "binance_usdm"], 120)
    assert sv.service_beats("ingest-binance", {"mt5"}) == ([], 0)                 # nothing to watch: never judged stale
    assert sv.service_beats("engine", {"mt5"}) == (["engine"], 120)               # service beats are not venue-bound
    assert sv.service_beats("api", set()) == ([], 0)


def test_a_xau_supervisor_does_not_watch_the_spot_heartbeat(monkeypatch):
    monkeypatch.setenv(INSTANCE_ENV, "XAUUSD")
    monkeypatch.setattr(sv.Supervisor, "_terminal_paths", lambda self: {})
    monkeypatch.setattr(sv, "JobObject", lambda: NS(handle=None, add=lambda pid: None, pids=lambda: [],
                                                    keep_members_on_close=lambda: False))
    s = sv.Supervisor(["ingest-binance", "ingest-mt5"], None)
    assert s.children["ingest-binance"].beats == ["binance_usdm"] and s.children["ingest-mt5"].beats == ["mt5"]
