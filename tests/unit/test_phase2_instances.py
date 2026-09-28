"""Phase 2 (P12.2, D-042): one independent system per pair — overlay, CLI, sibling rules, cross-process locks,
shared AI ledger with a per-pair share, account drawdown stop, same-direction guard, exposure, live account block,
migration."""
from __future__ import annotations

import json
import sqlite3
import time
import types
from pathlib import Path

import pytest

from tradingsystem import cli
from tradingsystem.ai import budget
from tradingsystem.ai.providers import claude_code as cc
from tradingsystem.analysis.engine import Engine
from tradingsystem.core.filelock import FileLock
from tradingsystem.core.settings import INSTANCE_ENV, AIProviderCfg, load_settings, system_state_dirs
from tradingsystem.core.timeutil import now_ms
from tradingsystem.execution import drawdown
from tradingsystem.execution.backends.mt5_backend import MT5Backend
from tradingsystem.execution.backends.paper import _COLS, PaperBackend
from tradingsystem.execution.executor import all_pairs_by_symbol, magics
from tradingsystem.execution.exposure import aggregate, leg, live_sides
from tradingsystem.execution.risk_gate import evaluate
from tradingsystem.supervisor import control, procs
from tradingsystem.supervisor import supervisor as sv

from .test_risk_gate import RISK, ctx, rec

NS = types.SimpleNamespace


def inst(pair: str):
    return load_settings(extra_env={INSTANCE_ENV: pair})


# ------------------------------------------------------------------ overlay
def test_instance_overlay_isolates_state_logs_port_magic_and_budget():
    legacy, btc, eth = load_settings(extra_env={INSTANCE_ENV: ""}), inst("BTCUSDT"), inst("ethusdt")
    assert legacy.paths.instance is None and legacy.paths.state() == legacy.paths.data()
    assert list(btc.enabled_pairs()) == ["BTCUSDT"] and eth.paths.instance == "ETHUSDT"
    assert btc.paths.state() == btc.paths.data() / "instances" / "BTCUSDT"
    assert btc.paths.logs() == legacy.paths.logs() / "BTCUSDT" and btc.paths.data() == legacy.paths.data()
    assert btc.paths.shared() == legacy.paths.data() / "shared"
    assert btc.api.port == legacy.instances["BTCUSDT"].api_port != eth.api.port != legacy.api.port
    assert btc.execution.magic == legacy.execution.magic + legacy.instances["BTCUSDT"].magic_offset
    n = len(legacy.instances)
    assert btc.binance.rest_weight_budget_per_min == max(300, legacy.binance.rest_weight_budget_per_min // n)
    assert btc.config_hash != legacy.config_hash != eth.config_hash
    assert system_state_dirs(btc) == [legacy.paths.data(), *(legacy.paths.data() / "instances" / p
                                                              for p in legacy.instances)]
    with pytest.raises(ValueError, match="not a configured instance"):
        load_settings(extra_env={INSTANCE_ENV: "DOGEUSDT"})


def test_worker_data_dir_override_keeps_the_instance(tmp_path):
    btc = inst("BTCUSDT")
    moved = sv.with_data_dir(btc, str(tmp_path))
    assert moved.paths.instance == "BTCUSDT" and moved.paths.state() == tmp_path / "instances" / "BTCUSDT"
    assert sv.with_data_dir(btc, None) is btc


def test_magics_and_the_symbol_map_of_every_pair():
    legacy, btc = load_settings(extra_env={INSTANCE_ENV: ""}), inst("BTCUSDT")
    own, base, family = magics(btc)
    assert base == legacy.execution.magic and own == base + legacy.instances["BTCUSDT"].magic_offset
    assert family == {base, *(base + i.magic_offset for i in legacy.instances.values())}
    assert magics(legacy)[:2] == (legacy.execution.magic, legacy.execution.magic)
    m = all_pairs_by_symbol(btc, btc.mt5.data_profile)
    assert set(m.values()) == set(legacy.pairs)          # disabled pairs of other systems too


# ------------------------------------------------------------------ CLI
@pytest.mark.parametrize("argv,rest,pair", [
    (["run", "all", "--detach", "--instance", "btcusdt"], ["run", "all", "--detach"], "BTCUSDT"),
    (["--instance=XAUUSD", "engine", "--once"], ["engine", "--once"], "XAUUSD"),
    (["run", "--status"], ["run", "--status"], None),
])
def test_cli_takes_the_instance_from_anywhere(argv, rest, pair):
    assert cli.take_instance(argv) == (rest, pair)


def test_cli_instance_without_a_pair_exits():
    with pytest.raises(SystemExit):
        cli.take_instance(["run", "--instance"])


def test_cli_sets_the_environment_for_the_command_and_its_children(monkeypatch):
    seen = {}
    monkeypatch.setenv(INSTANCE_ENV, "")          # restored (removed) after the test: cli.main sets it for real
    monkeypatch.setattr(cli, "_dispatch", lambda cmd, argv: seen.update(cmd=cmd, argv=argv, env=cli.os.environ.get(INSTANCE_ENV)) or 0)
    assert cli.main(["run", "--status", "--instance", "ethusdt"]) == 0
    assert seen == {"cmd": "run", "argv": ["--status"], "env": "ETHUSDT"}


# ------------------------------------------------------------------ which supervisors may run together
@pytest.mark.parametrize("mine,other,clash", [
    (None, None, True), (None, "BTCUSDT", True), ("BTCUSDT", None, True),
    ("BTCUSDT", "BTCUSDT", True), ("BTCUSDT", "ETHUSDT", False)])
def test_conflict_rules(mine, other, clash):
    assert procs.conflicts(mine, other) is clash


def test_cmd_instance_and_scopes(monkeypatch):
    assert procs.cmd_instance(["python", "-m", "tradingsystem", "run", "all", "--instance", "btcusdt"]) == "BTCUSDT"
    assert procs.cmd_instance(["python", "-m", "tradingsystem", "run", "all", "--instance=XAUUSD"]) == "XAUUSD"
    assert procs.cmd_instance(["python", "-m", "tradingsystem", "run", "all"]) is None
    monkeypatch.setattr(procs, "running_supervisors", lambda older_s=None: {1: None, 2: "BTCUSDT", 3: "ETHUSDT"})
    assert procs.other_supervisors() == [1, 2, 3]
    assert procs.other_supervisors(instance="BTCUSDT", scope="exact") == [2]
    assert procs.other_supervisors(instance=None, scope="exact") == [1]
    assert procs.other_supervisors(instance="BTCUSDT", scope="conflict") == [1, 2]
    assert procs.other_supervisors(instance=None, scope="conflict") == [1, 2, 3]
    assert procs.describe({2: "BTCUSDT", 1: None}) == "pid 2 (BTCUSDT), pid 1 (all pairs)"


class FakeProc:
    def __init__(self, pid, ppid, cmd):
        self.pid, self.info, self._cmd = pid, {"name": "python.exe", "ppid": ppid}, cmd

    def cmdline(self):
        return self._cmd

    def ppid(self):                     # read per supervisor match since Phase 5 (not prefetched for every process)
        return self.info["ppid"]

    def create_time(self):
        return 0.0

    def environ(self):
        return {}


def test_venv_launcher_and_its_interpreter_count_once(monkeypatch):
    cmd = ["python.exe", "-m", "tradingsystem", "run", "all", "--instance", "BTCUSDT"]
    fakes = [FakeProc(100, 1, cmd), FakeProc(101, 100, cmd),                      # launcher + real interpreter
             FakeProc(200, 1, cmd[:5]), FakeProc(300, 1, cmd[:4] + ["--status"])]  # all-pairs; a status call
    monkeypatch.setattr(procs.psutil, "process_iter", lambda attrs=None: iter(fakes))
    assert procs.running_supervisors() == {100: "BTCUSDT", 200: None}


def test_detach_refuses_across_scopes(tmp_path, monkeypatch):
    monkeypatch.setattr(control, "spawn_outside", lambda *a, **k: pytest.fail("must not start"))
    monkeypatch.setattr(control, "instance_running", lambda name: False)
    monkeypatch.setattr(procs, "running_supervisors", lambda older_s=None: {7: "BTCUSDT"})
    legacy, eth = load_settings(extra_env={INSTANCE_ENV: ""}), inst("ETHUSDT")
    assert control.detach(["all"], tmp_path, legacy, auto=False) == 1        # a pair runs: no all-pairs system
    assert "stop_all" in control.conflict_message(None, {7: "BTCUSDT"})
    monkeypatch.setattr(procs, "running_supervisors", lambda older_s=None: {8: None})
    assert control.detach(["all"], tmp_path / "e", eth, auto=False) == 1     # the all-pairs system runs
    assert "all-pairs system is running" in control.conflict_message("ETHUSDT", {8: None})


def test_detach_of_a_pair_next_to_another_pair_starts(tmp_path, monkeypatch):
    eth = inst("ETHUSDT")
    started = {}

    class P:
        def poll(self):
            return 0

    monkeypatch.setattr(control, "instance_running", lambda name: False)
    monkeypatch.setattr(procs, "running_supervisors", lambda older_s=None: {7: "BTCUSDT"})
    monkeypatch.setattr(control, "spawn_outside", lambda cmd, **k: started.update(cmd=cmd) or P())
    monkeypatch.setattr(control.procs, "open_rotating", lambda *a, **k: open(tmp_path / "x.log", "ab"))
    control.detach(["all", "--instance", "ETHUSDT"], tmp_path, eth, auto=False)
    assert started["cmd"][-2:] == ["--instance", "ETHUSDT"]


def test_stop_of_a_pair_only_waits_for_that_pair(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(control, "instance_running", lambda name: False)
    monkeypatch.setattr(procs, "running_supervisors", lambda older_s=None: {7: "ETHUSDT", 8: None})
    assert control.stop(tmp_path, instance="BTCUSDT", timeout=0) == 0
    out = capsys.readouterr().out
    assert "supervisor BTCUSDT is not running" in out and "start.bat BTCUSDT" in out


def test_main_refuses_a_pair_next_to_the_all_pairs_system(monkeypatch):
    monkeypatch.setenv(INSTANCE_ENV, "BTCUSDT")
    monkeypatch.setattr(sv, "setup_from_settings", lambda *a, **k: None)
    monkeypatch.setattr(sv, "acquire_instance", lambda name, wait_s=0: True)
    monkeypatch.setattr(procs, "running_supervisors", lambda older_s=None: {1234: None})
    monkeypatch.setattr(sv, "Supervisor", lambda *a, **k: pytest.fail("must not start"))
    monkeypatch.setattr(control, "instance_running", lambda name: False)
    assert sv.main(["all"]) == control.EXIT_REFUSED
    monkeypatch.setattr(procs, "running_supervisors", lambda older_s=None: {1234: "ETHUSDT"})
    ran = []
    monkeypatch.setattr(sv, "Supervisor", lambda *a, **k: NS(run=lambda: ran.append(1)))
    assert sv.main(["all"]) == 0 and ran == [1]


# ------------------------------------------------------------------ cross-process locks
def test_file_lock_excludes_a_second_holder_and_is_released(tmp_path):
    a, b = FileLock(tmp_path / "x.lock"), FileLock(tmp_path / "x.lock")
    assert a.acquire() and a.held
    t0 = time.monotonic()
    assert not b.acquire(timeout=0.3) and time.monotonic() - t0 >= 0.25
    a.release()
    with b.hold(timeout=1) as got:
        assert got and b.held
    assert not b.held and a.acquire(timeout=0)
    a.release()


def test_claude_cli_starts_are_spaced_across_processes(tmp_path, monkeypatch):
    assert cc.claim_start(tmp_path, gap_s=10) == 0.0
    wait = cc.claim_start(tmp_path, gap_s=10)
    assert 0 < wait <= 10
    assert cc.claim_start(tmp_path, gap_s=0) == 0.0                    # spacing switched off
    (tmp_path / cc.START_STAMP).write_text(f"{time.time() + 3600:.3f}", encoding="ascii")    # clock stepped back
    assert cc.claim_start(tmp_path, gap_s=10) == 0.0


def test_auth_check_waits_for_another_systems_cli_start(tmp_path, monkeypatch):
    exe = tmp_path / "claude.exe"
    exe.write_bytes(b"")
    monkeypatch.setattr(cc.tempfile, "gettempdir", lambda: str(tmp_path))
    prov = cc.ClaudeCodeProvider("claude_code", AIProviderCfg(kind="claude_code", model="sonnet", cli_path=str(exe)),
                                 "sonnet", None)
    assert cc.claim_start(prov.workdir) == 0.0                         # another system started a CLI just now
    monkeypatch.setattr(prov, "check_auth", lambda: pytest.fail("must wait for the spacing"))
    prov._auth_refreshing = True
    prov._refresh_auth()
    assert not prov._auth_refreshing and prov._auth_problem and "checking" in prov._auth_problem


# ------------------------------------------------------------------ shared AI ledger, per-pair share
def test_usage_ledger_is_shared_by_instances(tmp_path):
    legacy, btc = load_settings(extra_env={INSTANCE_ENV: ""}), inst("BTCUSDT")
    assert budget.usage_db(legacy) == legacy.paths.state() / "app.db"
    btc = sv.with_data_dir(btc, str(tmp_path))
    assert budget.usage_db(btc) == tmp_path / "shared" / "ai_usage.db" and (tmp_path / "shared").is_dir()


def test_rpd_is_shared_and_capped_per_pair(tmp_path):
    usage = budget.UsageStore(tmp_path / "ai_usage.db")
    cfg = AIProviderCfg(kind="claude_code", model="sonnet", rpd=10)
    for pair in ["BTCUSDT"] * 5 + ["ETHUSDT"] * 3:
        usage.record(None, provider="claude_code", model="sonnet", purpose="decision", pair=pair, ok=True)
    btc = budget.RateLimiter("claude_code", cfg, usage, instance="BTCUSDT", daily_cap=6)
    eth = budget.RateLimiter("claude_code", cfg, usage, instance="ETHUSDT", daily_cap=6)
    alone = budget.RateLimiter("claude_code", cfg, usage)
    assert btc.cap() == 6 and alone.cap() == 10
    assert btc.remaining_today() == 1          # its own share: 6 − 5
    assert eth.remaining_today() == 2          # the account's cap: 10 − 8 (its share would allow 3)
    assert alone.remaining_today() == 2
    usage.close()


# ------------------------------------------------------------------ account drawdown stop
def test_account_peak_trips_sticks_and_resets(tmp_path):
    a = drawdown.AccountPeak(tmp_path, "mt5:demo:1", 25.0)
    b = drawdown.AccountPeak(tmp_path, "mt5:demo:1", 25.0)          # another system on the same account
    assert a.update(100.0).drawdown_pct == 0 and a.update(120.0).peak == 120.0
    st = b.update(95.0)
    assert st.peak == 120.0 and st.drawdown_pct == pytest.approx(20.83, abs=0.01) and not st.tripped
    assert b.update(89.0).tripped                                       # 25.8 % below the peak
    back = a.update(119.0)
    assert back.tripped and back.drawdown_pct < 1                       # a recovery does not re-arm it
    assert drawdown.reset(tmp_path) == ["mt5:demo:1"]
    assert not a.update(119.0).tripped and a.last.peak == 119.0
    other = drawdown.AccountPeak(tmp_path, "paper:BTCUSDT", 25.0)
    assert other.update(10.0).peak == 10.0                              # accounts are kept apart


def test_gate_refuses_after_the_drawdown_stop_and_a_same_direction_trade():
    g = evaluate(rec(), "XAUUSD", ctx(account_drawdown_pct=26.0), RISK, [])
    assert "account_drawdown" in g.failures()[0]
    g = evaluate(rec(), "XAUUSD", ctx(account_drawdown_pct=3.0, account_drawdown_tripped=True), RISK, [])
    assert any(f.startswith("account_drawdown") and "TRIPPED" in f for f in g.failures())
    g = evaluate(rec(), "XAUUSD", ctx(live_sides={"BUY": ["position abcd1234"]}), RISK, [])
    assert not g.approved and any(f.startswith("no_same_direction") and "abcd1234" in f for f in g.failures())
    g = evaluate(rec(), "XAUUSD", ctx(live_sides={"SELL": ["order abcd1234"]}, account_drawdown_pct=3.0), RISK, [])
    assert g.approved, g.failures()                                    # the opposite side is a new idea


def test_correlated_cap_counts_the_other_pairs_systems():
    r = rec(pair="ETHUSDT")
    ok = evaluate(r, "ETHUSDT", ctx(contract_size=10), RISK, [["BTCUSDT", "ETHUSDT"]])
    assert "correlated_exposure" not in [n for n, k, _ in ok.checks if not k]
    g = evaluate(r, "ETHUSDT", ctx(contract_size=10, sibling_risk_pct_by_pair={"BTCUSDT": 0.8}), RISK,
                 [["BTCUSDT", "ETHUSDT"]])
    bad = [d for n, k, d in g.checks if n == "correlated_exposure" and not k]
    assert bad and "other pairs' systems" in bad[0]


# ------------------------------------------------------------------ exposure
def test_exposure_rows_fold_legs_per_decision_and_kind():
    legs = [leg(pair="BTCUSDT", decision="d" * 20, kind="position", side="BUY", order_type="MARKET", volume=0.01,
                price=100.0, sl=90.0, tp=120.0, profit_usd=1.0, since_ms=1_000),
            leg(pair="BTCUSDT", decision="d" * 20, kind="position", side="BUY", order_type="MARKET", volume=0.03,
                price=104.0, sl=90.0, tp=130.0, profit_usd=2.0, since_ms=2_000),
            leg(pair="BTCUSDT", decision="e" * 20, kind="order", side="SELL", order_type="SELL_LIMIT", volume=0.02,
                price=110.0, sl=115.0, tp=100.0, since_ms=3_000, expires_ms=9_000)]
    rows = aggregate(legs)
    pos = next(r for r in rows if r["kind"] == "position")
    assert pos["volume"] == pytest.approx(0.04) and pos["price"] == pytest.approx(103.0)
    assert pos["tps"] == [120.0, 130.0] and pos["profit_usd"] == 3.0 and pos["decision"] == "d" * 8
    assert pos["since"].startswith("1970-01-01T00:00:01")
    assert live_sides(rows, "BTCUSDT") == {"BUY": ["position dddddddd"], "SELL": ["order eeeeeeee"]}
    assert live_sides(rows, "ETHUSDT") == {}


def test_paper_exposure_lists_open_and_live_pending_legs(tmp_path):
    pb = PaperBackend(tmp_path / "app.db", 1_000)
    now = now_ms()
    base = dict(zip(_COLS, [None] * len(_COLS)))

    def add(i, **kw):
        row = {**base, "id": f"l{i}", "decision_id": "x" * 20, "pair": "BTCUSDT", "instrument": "mt5:BTCUSD@",
               "side": "BUY", "order_type": "BUY_LIMIT", "order_price": 100.0, "volume": 0.01, "contract_size": 1.0,
               "sl": 90.0, "tp": 120.0, "tp_index": 0, "status": "pending", "created_ms": now - 1000, **kw}
        pb._con.execute(f"INSERT INTO paper_legs ({','.join(_COLS)}) VALUES ({','.join('?' * len(_COLS))})",
                        [row[c] for c in _COLS])

    add(1, status="open", fill_price=100.0, fill_ms=now - 500, decision_id="y" * 20, order_type="MARKET")
    add(2)                                                   # live pending order
    add(3, expires_ms=now - 1)                               # expired: not live
    add(4, status="closed", fill_price=100.0, close_price=110.0, pnl_usd=0.1)
    marks = {"mt5:BTCUSD@": types.SimpleNamespace(bid=105.0, ask=106.0)}
    rows = pb.exposure(marks, now)
    assert [(r["kind"], r["decision"]) for r in rows] in ([("position", "y" * 8), ("order", "x" * 8)],
                                                          [("order", "x" * 8), ("position", "y" * 8)])
    pos = next(r for r in rows if r["kind"] == "position")
    assert pos["profit_usd"] == pytest.approx(0.05) and pb.account(marks)["exposure"] == rows


def test_mt5_ownership_adopts_the_all_pairs_orders_of_its_own_pair_only():
    b = object.__new__(MT5Backend)
    b.magic, b.adopt_magic, b.own_pairs = 102, 101, {"BTCUSDT"}
    b.pair_by_symbol = {"BTCUSD@": "BTCUSDT", "ETHUSD@": "ETHUSDT"}
    b.family = {101, 102, 103}
    x = lambda magic, sym: NS(magic=magic, symbol=sym)   # noqa: E731
    assert b.mine(x(102, "BTCUSD@")) and b.mine(x(101, "BTCUSD@"))       # own, and adopted from the all-pairs system
    assert not b.mine(x(101, "ETHUSD@")) and b.sibling(x(101, "ETHUSD@"))
    assert b.sibling(x(103, "ETHUSD@")) and not b.sibling(x(999, "BTCUSD@")) and not b.mine(x(999, "BTCUSD@"))


# ------------------------------------------------------------------ the account block the model reads
def engine_with(row: dict | None, s=None):
    e = object.__new__(Engine)
    e.s = s or inst("BTCUSDT")
    e.appdb = NS(statuses=lambda: [row] if row else [])
    e.orch = NS(default_account=lambda: {"equity": 100.0, "currency": "USD", "mode": "demo",
                                         "equity_source": "configured account size"})
    return e


def test_live_account_carries_equity_positions_and_orders_of_the_pair():
    detail = {"mode": "demo", "equity": 99.5, "balance": 100.0, "currency": "USD", "today_pnl_pct": -0.5,
              "account_drawdown": {"account": "mt5:x:1", "peak": 101.0, "drawdown_pct": 1.49, "tripped": None},
              "exposure": [{"pair": "BTCUSDT", "decision": "abcd1234", "kind": "position", "side": "BUY",
                            "order_type": "MARKET", "volume": 0.01, "price": 1.0, "sl": 0.9, "tps": [1.2],
                            "profit_usd": -0.5, "since": "2026-09-26T10:00:00Z", "expires": None},
                           {"pair": "BTCUSDT", "decision": "ef012345", "kind": "order", "side": "SELL",
                            "order_type": "SELL_LIMIT", "volume": 0.01, "price": 1.1, "sl": 1.2, "tps": [0.9],
                            "profit_usd": None, "since": "2026-09-26T11:00:00Z", "expires": "2026-09-26T12:00:00Z"},
                           {"pair": "ETHUSDT", "decision": "99999999", "kind": "position", "side": "BUY",
                            "order_type": "MARKET", "volume": 0.1, "price": 1.0, "sl": 0.9, "tps": [],
                            "profit_usd": 0.0, "since": None, "expires": None}]}
    row = {"collector": "executor", "state": "live", "updated_ms": now_ms() - 3000, "detail": json.dumps(detail)}
    a = engine_with(row).live_account("BTCUSDT")
    assert a["equity"] == 99.5 and a["today_pnl_pct"] == -0.5 and a["account_drawdown_pct"] == 1.49
    assert [p["decision"] for p in a["open_positions"]] == ["abcd1234"]
    assert [o["decision"] for o in a["pending_orders"]] == ["ef012345"] and "profit_usd" not in a["pending_orders"][0]
    assert "live demo account" in a["equity_source"]
    assert "open_positions" not in engine_with(row).live_account(None)


def test_live_account_falls_back_when_the_executor_is_silent():
    old = {"collector": "executor", "state": "live", "updated_ms": now_ms() - 600_000,
           "detail": json.dumps({"equity": 50.0})}
    for r in (None, old, {**old, "updated_ms": now_ms(), "state": "error"}):
        a = engine_with(r).live_account("BTCUSDT")
        assert a["equity"] == 100.0 and "open_positions" not in a and "configured account size" in a["equity_source"]


# ------------------------------------------------------------------ migration
def test_migration_keeps_one_pair_and_seeds_the_ledger_once(tmp_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location("migrate_instance",
                                                  Path(__file__).resolve().parents[2] / "tools" / "migrate_instance.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    src = tmp_path / "app.db"
    con = sqlite3.connect(src)
    con.executescript("""
        CREATE TABLE ai_decisions (id TEXT PRIMARY KEY, ts INTEGER, pair TEXT);
        CREATE TABLE ai_payloads (payload_hash TEXT, ts INTEGER, pair TEXT);
        CREATE TABLE ai_sub_outputs (id TEXT, decision_id TEXT);
        CREATE TABLE paper_legs (id TEXT, pair TEXT, status TEXT, pnl_usd REAL);
        CREATE TABLE paper_account (id INTEGER, start_equity REAL, realized_usd REAL, created_ms INTEGER);
        CREATE TABLE collector_status (collector TEXT);
        CREATE TABLE ai_usage (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, provider TEXT NOT NULL,
            model TEXT NOT NULL, purpose TEXT, pair TEXT, ok INTEGER NOT NULL);
        INSERT INTO ai_decisions VALUES ('a', 1, 'BTCUSDT'), ('b', 2, 'ETHUSDT');
        INSERT INTO ai_payloads VALUES ('h1', 1, 'BTCUSDT'), ('h2', 2, 'ETHUSDT');
        INSERT INTO ai_sub_outputs VALUES ('s1', 'a'), ('s2', 'b');
        INSERT INTO paper_legs VALUES ('l1', 'BTCUSDT', 'closed', 2.5), ('l2', 'ETHUSDT', 'closed', -1.0);
        INSERT INTO paper_account VALUES (1, 100, 1.5, 0);
        INSERT INTO collector_status VALUES ('engine');
        INSERT INTO ai_usage (ts, provider, model, purpose, pair, ok) VALUES (1, 'claude_code', 'sonnet', 'x', 'BTCUSDT', 1),
            (2, 'claude_code', 'sonnet', 'x', 'ETHUSDT', 1);
    """)
    con.commit()
    con.close()
    kept = m.copy_for_pair(src, tmp_path / "instances" / "BTCUSDT" / "app.db", "BTCUSDT")
    assert kept == {"ai_decisions": 1, "ai_payloads": 1, "paper_legs": 1, "ai_sub_outputs": 1}
    d = sqlite3.connect(tmp_path / "instances" / "BTCUSDT" / "app.db")
    assert d.execute("SELECT realized_usd FROM paper_account").fetchone()[0] == 2.5
    assert d.execute("SELECT count(*) FROM collector_status").fetchone()[0] == 0
    d.close()
    ledger = tmp_path / "shared" / "ai_usage.db"
    assert m.seed_ledger(src, ledger) == 2 and m.seed_ledger(src, ledger) == 0
    assert sqlite3.connect(src).execute("SELECT count(*) FROM ai_decisions").fetchone()[0] == 2   # source untouched
