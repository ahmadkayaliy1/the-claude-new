"""Dashboard API: Host allow-list vs DNS rebinding (SEC-01), Execute-Now age rule + state push (UX-01), shared status
snapshot and cheap process scan (PERF-01), quote staleness flags (OBS-01). In-process TestClient, throw-away app.db."""
import json
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketDenialResponse
from starlette.websockets import WebSocketDisconnect

import tradingsystem.api.app as app_mod
from tradingsystem.ai.store import DecisionRecord, DecisionStore
from tradingsystem.core.settings import PathsCfg, load_settings
from tradingsystem.core.timeutil import iso, now_ms
from tradingsystem.ingest.common.appdb import AppDB

TOKEN = "unit-test-token"


@pytest.fixture
def env(tmp_path, monkeypatch):
    s = load_settings(env_path=Path("nope.env"), extra_env={"EXECUTION_MODE": "paper"})
    s = s.model_copy(update={"paths": PathsCfg(data_dir=str(tmp_path / "data"), logs_dir=str(tmp_path / "logs"))})
    (tmp_path / "data").mkdir()
    monkeypatch.setenv(s.api.token_env, TOKEN)
    monkeypatch.setattr(app_mod, "_process_list", lambda: [])
    store, appdb = DecisionStore(s.paths.data() / "app.db"), AppDB(s.paths.data() / "app.db")
    client = TestClient(app_mod.create_app(s), base_url=f"http://127.0.0.1:{s.api.port}")
    return NS(s=s, store=store, appdb=appdb, client=client, origin=f"http://127.0.0.1:{s.api.port}")


def add_decision(store, did, age_s, pair="XAUUSD"):
    now = now_ms()
    r = {"decision": "BUY", "order_type": "MARKET", "stop_loss": 1.0, "valid_until": iso(now + 3_600_000),
         "take_profits": [{"price": 2.0, "close_fraction": 1.0}], "entry": {"price": None}}
    store.save(DecisionRecord(pair=pair, mode="test", trigger="test", status="valid", id=did, ts=now - age_s * 1000,
                              recommendation=r))


def fresh(env):
    app_mod._SNAP_CACHE["ts"] = 0                        # the snapshot is shared for ~1 s; tests want a new one
    return env.client.get("/api/status").json()


def test_host_header_allow_list_blocks_dns_rebinding(env):
    assert env.client.get("/api/decisions").status_code == 200
    assert env.client.get("/api/decisions", headers={"host": f"localhost:{env.s.api.port}"}).status_code == 200
    r = env.client.get("/", headers={"host": f"attacker.example:{env.s.api.port}"})
    assert r.status_code == 400 and TOKEN not in r.text
    ok = env.client.get("/")
    assert ok.status_code == 200 and TOKEN in ok.text and ok.headers["cache-control"] == "no-store"
    with pytest.raises((WebSocketDenialResponse, WebSocketDisconnect)):
        with env.client.websocket_connect(f"ws://attacker.example:{env.s.api.port}/ws") as ws:
            ws.receive_json()


def test_execute_refuses_what_the_gate_would_call_too_old(env):
    age = env.s.risk.max_recommendation_age_s
    add_decision(env.store, "old", age + 60)
    add_decision(env.store, "new", 10)
    h = {"X-Dashboard-Token": TOKEN, "Origin": env.origin}
    r = env.client.post("/api/decisions/old/execute", headers=h)
    assert r.status_code == 409 and "too old" in r.text
    r = env.client.post("/api/decisions/new/execute", headers=h)
    assert r.status_code == 200 and r.json()["execution_state"] == "queued"
    assert env.client.get("/api/pairs").json()[0]["max_recommendation_age_s"] == age


def test_status_pushes_latest_state_and_reason_per_pair(env):
    add_decision(env.store, "x1", 20)
    assert fresh(env)["latest_decisions"]["XAUUSD"]["execution_state"] == "not_executed"
    env.store.set_execution_state("x1", "rejected", {"reason": "market_open: execution market closed", "gate": []})
    d = fresh(env)["latest_decisions"]["XAUUSD"]
    assert d["id"] == "x1" and d["execution_state"] == "rejected" and "market closed" in d["execution_reason"]
    with env.client.websocket_connect(f"ws://127.0.0.1:{env.s.api.port}/ws", headers={"origin": env.origin}) as ws:
        msg = ws.receive_json()
    assert msg["latest_decisions"]["XAUUSD"]["execution_state"] == "rejected" and msg["new_decisions"]


def test_quotes_flag_stale_and_unconfigured_rows(env):
    env.appdb.upsert_quote("mt5:XAUUSD@", now_ms() - 3_600_000, 1.0, 1.1, None, "test")
    env.appdb.upsert_quote("mt5:XAUUSD@", now_ms() - 3_600_000, 1.0, 1.1, None, "test")
    env.appdb.upsert_quote("binance_spot:GONEUSDT", now_ms(), 1.0, 1.1, None, "test")
    q = {x["instrument"]: x for x in fresh(env)["quotes"]}
    assert q["mt5:XAUUSD@"]["stale"] and q["mt5:XAUUSD@"]["configured"]
    assert not q["binance_spot:GONEUSDT"]["stale"] and not q["binance_spot:GONEUSDT"]["configured"]


def test_snapshot_is_shared_between_clients(env, monkeypatch):
    calls = []
    real = app_mod._status_snapshot
    monkeypatch.setattr(app_mod, "_status_snapshot", lambda *a, **k: calls.append(1) or real(*a, **k))
    monkeypatch.setattr(app_mod, "_SNAP_TTL_MS", 60_000)       # the sharing, not the machine's speed, is under test
    app_mod._SNAP_CACHE["ts"] = 0
    for _ in range(5):
        env.client.get("/api/status")
    assert len(calls) == 1


def test_process_scan_opens_only_python_processes(monkeypatch):
    opened = []

    class P:
        def __init__(self, pid, name, cmd):
            self.info, self._cmd = {"pid": pid, "name": name}, cmd

        def cmdline(self):
            opened.append(self.info["name"])
            return self._cmd

        def memory_info(self):
            return NS(rss=50 * 2**20)

    procs = [P(1, "System", None), P(2, "chrome.exe", ["chrome"]), P(3, "python.exe", ["python", "-m", "tradingsystem", "api"]),
             P(4, "python.exe", ["python", "other.py"])]
    monkeypatch.setattr(app_mod.psutil, "process_iter", lambda attrs: iter(procs))
    monkeypatch.setitem(app_mod._PROC_CACHE, "ts", 0)
    rows = app_mod._process_list()
    assert opened == ["python.exe", "python.exe"] and [r["pid"] for r in rows] == [3] and rows[0]["rss_mb"] == 50
    json.dumps(rows)


def test_chart_routes_serve_only_configured_files_and_nothing_while_charts_are_off(env):
    d = env.s.paths.state() / "charts" / "XAUUSD"
    d.mkdir(parents=True)
    (d / "15m.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    assert [c["tf"] for c in env.client.get("/api/charts/XAUUSD").json()] == ["15m"]
    assert env.client.get("/api/charts/XAUUSD/15m").status_code == 200
    assert env.client.get("/api/charts/XAUUSD/..%5C..%5Capp.db").status_code == 404      # never a path
    assert env.client.get("/api/charts/NOPE").status_code == 404
    off = env.s.model_copy(update={"ai": env.s.ai.model_copy(update={"charts": env.s.ai.charts.model_copy(
        update={"enabled": False})})})
    client = TestClient(app_mod.create_app(off), base_url=env.origin)
    assert client.get("/api/charts/XAUUSD").json() == []                                # a rollback shows no leftovers
