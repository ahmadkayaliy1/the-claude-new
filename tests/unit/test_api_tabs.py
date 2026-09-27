"""Dashboard Phase 4 tabs (§3.8 component 7): Operator / Tuning / Proposals / Reviews routes on a seeded throw-away data
root (and on a database the Phase 3/4 migrations have not reached), review names never become paths, and the kill
switch POST — ON only, token + Origin like Execute Now, this system's switch only, idempotent."""
import json
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
from fastapi.testclient import TestClient

import tradingsystem.api.app as app_mod
from tradingsystem.ai.store import DecisionRecord, DecisionStore
from tradingsystem.core import adaptive as ad
from tradingsystem.core.settings import PathsCfg, load_settings
from tradingsystem.core.timeutil import now_ms
from tradingsystem.execution import management as mg

TOKEN = "unit-test-token"
HOUR, DAY = 3_600_000, 86_400_000


def _settings(tmp_path: Path, instance: str | None = None):
    extra = {"EXECUTION_MODE": "paper", **({"TS_INSTANCE": instance} if instance else {})}
    s = load_settings(env_path=Path("nope.env"), extra_env=extra)
    return s.model_copy(update={"paths": PathsCfg(data_dir=str(tmp_path / "data"), logs_dir=str(tmp_path / "logs"),
                                                  instance=instance)})


def _client(s, monkeypatch) -> TestClient:
    monkeypatch.setenv(s.api.token_env, TOKEN)                  # read once, at create_app
    monkeypatch.setattr(app_mod, "_process_list", lambda: [])
    app_mod._SNAP_CACHE["ts"] = 0
    return TestClient(app_mod.create_app(s), base_url=f"http://127.0.0.1:{s.api.port}")


def _env(tmp_path, monkeypatch, instance=None):
    s = _settings(tmp_path, instance)
    s.paths.state().mkdir(parents=True, exist_ok=True)
    return NS(s=s, db=s.paths.state() / "app.db", client=_client(s, monkeypatch),
              origin=f"http://127.0.0.1:{s.api.port}", data=s.paths.data())


@pytest.fixture
def env(tmp_path, monkeypatch):
    return _env(tmp_path, monkeypatch)


def _decision(store, did, ts, *, notes=None, actions=None, management=None, status="valid", pair="XAUUSD"):
    rec = {"decision": "BUY", "order_type": "MARKET", "stop_loss": 1.0, "take_profits": [{"price": 2.0}]}
    if notes is not None:
        rec["operator_notes"] = notes
    if actions is not None:
        rec["position_actions"] = actions
    if management is not None:
        rec["management"] = management
    store.save(DecisionRecord(pair=pair, mode="test", trigger="test", status=status, id=did, ts=ts, recommendation=rec))


def seed(env) -> dict:
    """A realistic app.db (the store's schema + the management tables) and the Phase 4 files of the data root."""
    now = now_ms()
    store = DecisionStore(env.db)
    plan = [{"action": "move_sl_to_be", "trigger": "tp1_hit", "params": {}}]
    act = [{"target": {"decision": "d1", "kind": "position"}, "action": "set_sl", "value": 1.5, "reason": "lock in"}]
    _decision(store, "d1", now - 5 * HOUR, notes="Watch the Asia high <b>", management=plan)
    _decision(store, "d2", now - 3 * HOUR, actions=act)
    _decision(store, "d3", now - 1 * HOUR, notes="")                  # later, no notes and no actions
    _decision(store, "e1", now - 30 * 60_000, status="error")         # never the memory nor the actions
    _decision(store, "b1", now - 2 * HOUR, notes="BTC note", pair="BTCUSDT")
    with sqlite3.connect(env.db) as con:
        for stmt in mg._DDL:
            con.execute(stmt)
        ins = ("INSERT INTO position_actions (ts, pair, source, source_decision, seq, target_decision, leg, action, "
               "requested, status, detail) VALUES (?,?,?,?,?,?,?,?,?,?,?)")
        con.execute(ins, (now - 2 * HOUR, "XAUUSD", "rule", "d1", 0, "d1", "111", "set_sl", '{"sl": 1.2}', "applied",
                          '{"note": "moved"}'))
        con.execute(ins, (now - HOUR, "XAUUSD", "model", "d2", 0, "d1", "111", "set_sl", '{"sl": 1.5}', "rejected",
                          "not json"))
        con.execute(ins, (now - HOUR, "BTCUSDT", "rule", "b1", 0, "b1", "222", "close", None, "applied", None))
        con.execute("INSERT INTO management_state VALUES (?,?,?,?,?,?,?)",
                    ("d1", 0, "111", "armed", now - HOUR, now - HOUR, '{"steps": 0}'))
        con.execute("INSERT INTO tuning_changes (ts, pair, key, old_value, new_value, reason, evidence, window_hours, "
                    "expires_ms, review_id, actor) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (now - HOUR, "XAUUSD", "min_confidence_floor", None, "70", "weak ideas lost", '{"n": 24}', 168,
                     now + DAY, "20260927T043000Z_daily", "operator"))
    store._con.close()
    a = env.data / "adaptive" / "XAUUSD"
    a.mkdir(parents=True)
    playbook = "- Prefer the London open.\n- Skip the first 5 minutes after high-impact news."
    (a / "playbook.md").write_text(playbook, encoding="utf-8")
    (a / "adaptive.yaml").write_text(
        "version: 1\n"
        f"min_confidence_floor: {{value: 70, set_ms: {now - HOUR}, expires_ms: {now + DAY}, reason: weak ideas lost, "
        "evidence: {n: 24}, window_hours: 168, review_id: 20260927T043000Z_daily}\n"
        f"max_idle_minutes: {{value: 120, set_ms: {now - 3 * DAY}, expires_ms: {now - HOUR}, reason: quiet market, "
        "window_hours: 168}\n"
        f"trigger:\n  weak_min: {{value: 3, set_ms: {now - HOUR}, expires_ms: {now + 2 * DAY}, reason: noise, "
        "window_hours: 72}\n"
        f"playbook: {{value: {ad.text_hash(playbook)}, set_ms: {now - HOUR}, expires_ms: {now + DAY}, "
        "reason: first playbook, window_hours: 168}\n", encoding="utf-8")
    (a / "changes.jsonl").write_text(
        json.dumps({"ts": now - 2 * HOUR, "action": "set", "key": "max_idle_minutes", "new": 120}) + "\n"
        + "this is not json\n"
        + json.dumps({"ts": now - HOUR, "action": "set", "key": "min_confidence_floor", "new": 70}) + "\n"
        + '{"ts": 1, "action": "se', encoding="utf-8")                   # a writer mid-append
    env.data.joinpath("shared").mkdir(parents=True, exist_ok=True)
    (env.data / "shared" / "proposals.jsonl").write_text(
        json.dumps({"ts": now - DAY, "slug": "older", "title": "Older idea", "status": "awaiting_user"}) + "\n"
        + json.dumps({"ts": now - HOUR, "slug": "wider-sl", "title": "Wider SL on news <script>", "pair": "XAUUSD",
                      "status": "awaiting_user"}) + "\n"
        + '{"ts": 2, "slug": "torn', encoding="utf-8")
    r = env.data / "reviews"
    r.mkdir()
    (r / "20260926T043000Z_daily.md").write_text("# Daily review 26\n<script>alert(1)</script>", encoding="utf-8")
    (r / "20260927T043000Z_daily.md").write_text("# Daily review 27", encoding="utf-8")
    (r / "20260927T043000Z_daily.json").write_text('{"pack_id": "20260927T043000Z_daily"}', encoding="utf-8")
    (r / "20260927T043000Z_daily.session.json").write_text(json.dumps(
        {"review_id": "20260927T043000Z_daily", "kind": "daily", "status": "ok", "summary": "All quiet.",
         "args": ["claude", "-p"], "env": {"X": "1"}}), encoding="utf-8")
    (r / "20260927T043000Z_daily.prompt.md").write_text("prompt", encoding="utf-8")      # not served
    (r / ".20260927T043000Z_daily.md.99.tmp").write_text("half", encoding="utf-8")       # a write in progress
    (r / "notes.txt").write_text("x", encoding="utf-8")
    (r / "20260927T050000Z_weekly.md").mkdir()                                           # a directory, not a file
    return {"now": now, "playbook": playbook}


# ---------------------------------------------------------------------- Operator
def test_operator_tab_shows_memory_model_actions_system_actions_rules_and_charts(env):
    seed(env)
    ch = env.s.paths.state() / "charts" / "XAUUSD"
    ch.mkdir(parents=True)
    (ch / "15m.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    d = env.client.get("/api/operator/XAUUSD").json()
    assert d["memory"]["id"] == "d1" and d["memory"]["notes"] == "Watch the Asia high <b>"   # raw: the page escapes
    assert d["last_model_actions"]["id"] == "d2" and d["last_model_actions"]["actions"][0]["action"] == "set_sl"
    pa = d["position_actions"]
    assert [a["source"] for a in pa] == ["model", "rule"] and {a["pair"] for a in pa} == {"XAUUSD"}   # newest first
    assert pa[1]["requested"] == {"sl": 1.2} and pa[1]["detail"] == {"note": "moved"} and pa[0]["detail"] == "not json"
    ms = d["management_state"]
    assert len(ms) == 1 and ms[0]["status"] == "armed" and ms[0]["detail"] == {"steps": 0}
    assert ms[0]["rule"]["action"] == "move_sl_to_be" and ms[0]["decision"] == "BUY"
    assert [c["tf"] for c in d["charts"]] == ["15m"]
    btc = env.client.get("/api/operator/BTCUSDT").json()
    assert btc["memory"]["notes"] == "BTC note" and btc["last_model_actions"] is None and btc["management_state"] == []
    assert env.client.get("/api/operator/NOPE").status_code == 404
    assert env.client.get("/api/operator/..%5C..%5Capp.db").status_code == 404


def test_position_actions_route_is_newest_first_filterable_and_limited(env):
    seed(env)
    rows = env.client.get("/api/position_actions").json()
    assert len(rows) == 3 and rows[0]["id"] > rows[1]["id"] > rows[2]["id"]
    assert [r["pair"] for r in env.client.get("/api/position_actions?pair=BTCUSDT").json()] == ["BTCUSDT"]
    assert len(env.client.get("/api/position_actions?limit=1").json()) == 1


# ---------------------------------------------------------------------- Tuning
def test_adaptive_tab_shows_entries_with_expiry_effective_values_playbook_and_flags(env):
    info = seed(env)
    a = env.client.get("/api/adaptive").json()
    assert a["enabled"] is True and a["tuning_freeze"] is False and set(a["pairs"]) == set(env.s.enabled_pairs())
    x = a["pairs"]["XAUUSD"]
    e = {r["key"]: r for r in x["entries"]}
    assert set(e) == {"min_confidence_floor", "max_idle_minutes", "trigger.weak_min", "playbook"}
    assert e["min_confidence_floor"]["value"] == 70 and not e["min_confidence_floor"]["expired"]
    assert e["max_idle_minutes"]["expired"] and e["max_idle_minutes"]["expires_ms"] < info["now"]
    assert e["trigger.weak_min"]["review_id"] is None and e["min_confidence_floor"]["evidence"] == {"n": 24}
    assert x["valid"] is True and x["problem"] is None and x["playbook"] == info["playbook"] and x["playbook_in_force"]
    eff = x["effective"]
    assert eff["min_confidence"] == max(env.s.risk.min_confidence, 70) and eff["weak_min"] == max(env.s.ai.weak_min, 3)
    assert eff["max_idle_minutes"] == env.s.ai.max_idle_minutes                            # the expired entry is absent
    assert a["pairs"]["BTCUSDT"]["file"] is False and a["pairs"]["BTCUSDT"]["valid"] is True
    # read-only: no "expired" line, no tuning_changes.reverted_ms (the services own that bookkeeping)
    assert "expired" not in (env.data / "adaptive" / "XAUUSD" / "changes.jsonl").read_text(encoding="utf-8")
    with sqlite3.connect(env.db) as con:
        assert con.execute("SELECT reverted_ms FROM tuning_changes").fetchone()[0] is None
    (env.data / "TUNING_FREEZE").write_text("holiday week", encoding="utf-8")
    off = env.s.model_copy(update={"adaptive": env.s.adaptive.model_copy(update={"enabled": False})})
    a2 = TestClient(app_mod.create_app(off), base_url=env.origin).get("/api/adaptive").json()
    assert a2["tuning_freeze"] is True and a2["freeze_note"] == "holiday week" and a2["enabled"] is False
    assert a2["pairs"]["XAUUSD"]["effective"]["min_confidence"] == env.s.risk.min_confidence   # disabled → defaults


def test_adaptive_tab_reports_an_invalid_or_broken_overlay_without_failing(env):
    now = now_ms()
    a = env.data / "adaptive" / "XAUUSD"
    a.mkdir(parents=True)
    (a / "adaptive.yaml").write_text(      # out of bounds + a risk key: the services refuse it (keep last good)
        f"min_confidence_floor: {{value: 99, set_ms: {now}, expires_ms: {now + DAY}, reason: r, window_hours: 24}}\n"
        "risk: {risk_per_trade_pct: 5}\nweird: .nan\n", encoding="utf-8")
    x = env.client.get("/api/adaptive").json()["pairs"]["XAUUSD"]
    assert x["valid"] is False and x["problem"] and x["effective"] is None
    assert {r["key"] for r in x["entries"]} >= {"min_confidence_floor", "risk.risk_per_trade_pct", "weird"}
    (a / "adaptive.yaml").write_text("min_confidence_floor: [unclosed\n", encoding="utf-8")
    x = env.client.get("/api/adaptive").json()["pairs"]["XAUUSD"]
    assert x["yaml_error"] and x["entries"] == [] and x["valid"] is False
    (a / "adaptive.yaml").write_text("a: &a [*a, *a]\nb: &b [*a, *a, *a]\nc: [*b, *b, *b, *b]\n", encoding="utf-8")
    assert env.client.get("/api/adaptive").status_code == 200                    # self-referencing aliases
    bomb = "a: &a [x,x,x,x,x,x,x,x,x,x]\n" + "".join(          # "billion laughs": 10^9 leaves once expanded
        f"{chr(98 + i)}: &{chr(98 + i)} [{','.join([f'*{chr(97 + i)}'] * 10)}]\n" for i in range(8))
    (a / "adaptive.yaml").write_text(bomb, encoding="utf-8")
    t0 = time.monotonic()
    x = env.client.get("/api/adaptive").json()["pairs"]["XAUUSD"]
    assert time.monotonic() - t0 < 10 and x["valid"] is False and "aliases" in x["problem"]


def test_adaptive_tab_refuses_a_merge_key_bomb_before_parsing_it(env):
    # merge keys are copied eagerly by safe_load (not shared like list aliases): 10^7 key copies at 7 levels, ~550 bytes
    lines = ["l0: &l0 {" + ", ".join(f"k{i}: {i}" for i in range(10)) + "}"]
    lines += [f"l{n}: &l{n} {{<<: [{', '.join([f'*l{n - 1}'] * 10)}]}}" for n in range(1, 8)]
    a = env.data / "adaptive" / "XAUUSD"
    a.mkdir(parents=True)
    (a / "adaptive.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    t0 = time.monotonic()
    r = env.client.get("/api/adaptive")
    assert time.monotonic() - t0 < 1 and r.status_code == 200
    x = r.json()["pairs"]["XAUUSD"]
    assert x["entries"] == [] and "aliases" in x["yaml_error"]
    assert x["valid"] is False and "aliases" in x["problem"] and x["effective"] is None


def test_adaptive_tab_answers_200_for_deep_nesting_a_bad_date_and_an_oversized_file(env):
    a = env.data / "adaptive" / "XAUUSD"
    a.mkdir(parents=True)
    f = a / "adaptive.yaml"
    # 3000 levels: RecursionError in the composer, not a YAMLError (one bracket a line keeps the scanner fast)
    f.write_text("a: " + "[\n" * 3000 + "]\n" * 3000, encoding="utf-8")
    t0 = time.monotonic()
    r = env.client.get("/api/adaptive")
    assert r.status_code == 200 and time.monotonic() - t0 < 5
    x = r.json()["pairs"]["XAUUSD"]
    assert x["valid"] is False and x["entries"] == [] and "nested too deeply" in x["yaml_error"]
    assert r.json()["pairs"]["BTCUSDT"]["valid"] is True                           # the other pairs still answer
    f.write_text("a: 2001-13-01\n", encoding="utf-8")                              # ValueError from the constructor
    r = env.client.get("/api/adaptive")
    assert r.status_code == 200 and r.json()["pairs"]["XAUUSD"]["yaml_error"]
    assert r.json()["pairs"]["XAUUSD"]["valid"] is False
    f.write_text("# " + "x" * ad.MAX_YAML_CHARS + "\n", encoding="utf-8")          # the services' own size cap
    x = env.client.get("/api/adaptive").json()["pairs"]["XAUUSD"]
    assert x["entries"] == [] and "larger than" in x["yaml_error"]
    assert x["valid"] is False and "larger than" in x["problem"]


def test_tuning_changes_route_reads_the_table_and_changes_jsonl_skipping_bad_lines(env):
    seed(env)
    t = env.client.get("/api/tuning_changes").json()
    row = t["table"]
    assert len(row) == 1 and row[0]["key"] == "min_confidence_floor" and row[0]["evidence"] == {"n": 24}
    assert [c["key"] for c in t["changes"]["XAUUSD"]] == ["min_confidence_floor", "max_idle_minutes"]   # newest first
    assert t["changes"]["BTCUSDT"] == []
    one = env.client.get("/api/tuning_changes?pair=XAUUSD&limit=1").json()
    assert list(one["changes"]) == ["XAUUSD"] and len(one["changes"]["XAUUSD"]) == 1
    assert env.client.get("/api/tuning_changes?pair=..%5CNOPE").status_code == 404


# ---------------------------------------------------------------------- Proposals
def test_proposals_newest_first_and_a_torn_last_line_is_skipped(env):
    seed(env)
    p = env.client.get("/api/proposals").json()
    assert [x["slug"] for x in p] == ["wider-sl", "older"] and p[0]["title"].endswith("<script>")


def test_jsonl_routes_split_records_on_newline_only_not_on_unicode_line_separators(env):
    # older writers kept U+2028 / U+2029 / U+0085 raw (ensure_ascii=False): str.splitlines() cut such a record in two
    a = env.data / "adaptive" / "XAUUSD"
    a.mkdir(parents=True)
    hint = "TP1 at the prior high trail the rest"
    (a / "changes.jsonl").write_text(
        json.dumps({"ts": 1, "action": "set", "key": "tp_hint", "new": hint}, ensure_ascii=False) + "\r\n"
        + json.dumps({"ts": 2, "action": "revert", "reason": "c d \u0085e"}, ensure_ascii=False) + "\n",
        encoding="utf-8")
    ch = env.client.get("/api/tuning_changes?pair=XAUUSD").json()["changes"]["XAUUSD"]
    assert [c["ts"] for c in ch] == [2, 1] and ch[1]["new"] == hint and ch[0]["reason"] == "c d \u0085e"
    env.data.joinpath("shared").mkdir(parents=True, exist_ok=True)
    (env.data / "shared" / "proposals.jsonl").write_text(
        json.dumps({"ts": 1, "slug": "one", "title": "a b"}, ensure_ascii=False) + "\n"
        + json.dumps({"ts": 2, "slug": "two", "title": "c d"}, ensure_ascii=False) + "\n", encoding="utf-8")
    p = env.client.get("/api/proposals").json()
    assert [x["slug"] for x in p] == ["two", "one"] and p[1]["title"] == "a b"


# ---------------------------------------------------------------------- Reviews
def test_reviews_list_only_review_files_and_serve_only_listed_names(env):
    seed(env)
    names = [r["name"] for r in env.client.get("/api/reviews").json()]
    assert names == ["20260927T043000Z_daily.session.json", "20260927T043000Z_daily.md",
                     "20260927T043000Z_daily.json", "20260926T043000Z_daily.md"]
    r = env.client.get("/api/reviews/20260926T043000Z_daily.md").json()
    assert r["text"].startswith("# Daily review 26") and r["kind"] == "daily" and not r["truncated"]
    sess = env.client.get("/api/reviews/20260927T043000Z_daily.session.json").json()
    assert sess["session"]["summary"] == "All quiet." and "args" not in sess["session"]
    for bad in ("..%5C..%5Capp.db", "..%2F..%2Fapp.db", "20260927T043000Z_daily.prompt.md", "notes.txt",
                ".20260927T043000Z_daily.md.99.tmp", "20260101T000000Z_daily.md", "20260927T050000Z_weekly.md",
                "20260927T043000Z_daily.md%00.txt"):
        assert env.client.get(f"/api/reviews/{bad}").status_code == 404, bad
    big = env.data / "reviews" / "20260927T060000Z_weekly.md"
    big.write_text("x" * (app_mod.MAX_REVIEW_BYTES + 10), encoding="utf-8")
    r = env.client.get(f"/api/reviews/{big.name}").json()
    assert r["truncated"] and len(r["text"]) == app_mod.MAX_REVIEW_BYTES


# ---------------------------------------------------------------------- old / missing databases
PHASE4_ROUTES = ("/api/operator/XAUUSD", "/api/position_actions", "/api/adaptive", "/api/tuning_changes",
                 "/api/proposals", "/api/reviews", "/api/kill_switch", "/api/status")
OLD_DECISION_COLUMNS = (   # ai_decisions before the Phase 3 / Phase 4 ALTERs
    "id TEXT PRIMARY KEY", "ts INTEGER", "pair TEXT", "mode TEXT", "trigger TEXT", "provider TEXT", "model TEXT",
    "prompt_hash TEXT", "payload_hash TEXT", "config_hash TEXT", "git_sha TEXT", "status TEXT", "decision TEXT",
    "order_type TEXT", "confidence INTEGER", "rr_computed REAL", "valid_until INTEGER", "recommendation TEXT",
    "raw_text TEXT", "errors TEXT", "cost_usd REAL", "latency_ms INTEGER", "input_tokens INTEGER",
    "output_tokens INTEGER", "execution_state TEXT", "execution_detail TEXT", "outcome TEXT", "outcome_pnl_usd REAL",
    "outcome_pnl_pct REAL", "outcome_pips REAL", "outcome_ts INTEGER", "virtual_outcome TEXT", "virtual_r REAL")


def test_every_tab_route_answers_on_a_database_without_the_phase3_and_phase4_tables(env):
    with sqlite3.connect(env.db) as con:     # Phase 1/2 shape: no position_actions, management_state, tuning_changes
        con.execute(f"CREATE TABLE ai_decisions ({', '.join(OLD_DECISION_COLUMNS)})")
        ins = ("INSERT INTO ai_decisions (id, ts, pair, status, decision, execution_state, recommendation) "
               "VALUES (?,?,?,?,?,?,?)")
        con.execute(ins, ("o1", now_ms(), "XAUUSD", "valid", "BUY", "executed",
                          json.dumps({"operator_notes": "old notes", "position_actions": []})))
        con.execute(ins, ("o2", now_ms() + 1, "XAUUSD", "valid", "SELL", "executed", "{broken"))
        con.execute("CREATE TABLE management_state (decision_id TEXT)")          # a table missing its columns
    for route in PHASE4_ROUTES:
        assert env.client.get(route).status_code == 200, route
    d = env.client.get("/api/operator/XAUUSD").json()
    assert d["position_actions"] == [] and d["management_state"] == [] and d["last_model_actions"] is None
    assert env.client.get("/api/tuning_changes").json()["table"] == []


def test_every_tab_route_answers_without_any_database_or_files(tmp_path, monkeypatch):
    e = _env(tmp_path, monkeypatch)
    for route in PHASE4_ROUTES:
        assert e.client.get(route).status_code == 200, route
    assert e.client.get("/api/operator/XAUUSD").json()["memory"] == {}
    assert e.client.get("/api/reviews").json() == [] and e.client.get("/api/proposals").json() == []


# ---------------------------------------------------------------------- kill switch
def test_kill_switch_post_needs_the_token_and_a_correct_origin(env):
    target = env.data / "KILL_SWITCH"
    assert env.client.post("/api/kill_switch").status_code == 401                                   # no token
    assert env.client.post("/api/kill_switch", headers={"X-Dashboard-Token": "nope"}).status_code == 401
    latin = {"X-Dashboard-Token": "tökén".encode("latin-1")}                # compare_digest refuses non-ASCII str
    assert env.client.post("/api/kill_switch", headers=latin).status_code == 401
    r = env.client.post("/api/kill_switch", headers={"X-Dashboard-Token": TOKEN, "Origin": "http://evil.example"})
    assert r.status_code == 403
    r = env.client.post("/api/kill_switch", headers={"Origin": env.origin})
    assert r.status_code == 401 and not target.exists()
    assert env.client.delete("/api/kill_switch", headers={"X-Dashboard-Token": TOKEN}).status_code == 405   # no OFF
    assert not target.exists()


def test_kill_switch_all_pairs_mode_writes_the_global_switch_once(env, monkeypatch):
    import tradingsystem.core.notify as nt
    sent = []
    monkeypatch.setattr(nt, "notify", lambda s, level, title, text, **k: sent.append((level, title, k.get("key"))))
    h = {"X-Dashboard-Token": TOKEN, "Origin": env.origin}
    assert env.client.get("/api/kill_switch").json() == {"on": False, "target_on": False,
                                                         "target": str(env.data / "KILL_SWITCH"),
                                                         "scope": "all", "global": True, "files": []}
    r = env.client.post("/api/kill_switch", headers=h, json={"reason": "news  spike\n"})
    body = r.json()
    assert r.status_code == 200 and body["created"] and body["global"] and body["scope"] == "all"
    assert "GLOBAL" in body["note"] and body["set_by"]["actor"] == "dashboard"
    assert body["set_by"]["reason"] == "news spike"
    f = env.data / "KILL_SWITCH"
    first = f.read_text(encoding="utf-8")
    again = env.client.post("/api/kill_switch", headers=h).json()                   # idempotent, first reason kept
    assert again["created"] is False and "already on" in again["note"] and f.read_text(encoding="utf-8") == first
    assert sent == [("critical", "kill switch ON: ALL", "kill_switch_all")]
    app_mod._SNAP_CACHE["ts"] = 0
    assert env.client.get("/api/status").json()["kill_switch"] == {"on": True, "target_on": True, "files": [str(f)]}
    ks = env.client.get("/api/kill_switch").json()
    assert ks["on"] is True and ks["target_on"] is True


def test_all_pairs_mode_a_pair_switch_alone_leaves_the_global_button_usable(env):
    """In the all-pairs system a pair's switch (monitor order burst, kill_switch.py --pair) stops that pair only: the
    badge shows it, but the ON button (which writes the GLOBAL switch) must stay enabled — target_on stays false."""
    pair = env.data / "instances" / "XAUUSD" / "KILL_SWITCH"
    pair.parent.mkdir(parents=True)
    pair.write_text("", encoding="utf-8")
    app_mod._SNAP_CACHE["ts"] = 0
    assert env.client.get("/api/status").json()["kill_switch"] == {"on": True, "target_on": False,
                                                                   "files": [str(pair)]}
    ks = env.client.get("/api/kill_switch").json()
    assert ks["on"] is True and ks["target_on"] is False and ks["global"] is True
    assert ks["target"] == str(env.data / "KILL_SWITCH") and [f["path"] for f in ks["files"]] == [str(pair)]
    (env.data / "KILL_SWITCH").write_text("", encoding="utf-8")          # the global one: nothing left to engage
    app_mod._SNAP_CACHE["ts"] = 0
    assert env.client.get("/api/status").json()["kill_switch"]["target_on"] is True
    assert env.client.get("/api/kill_switch").json()["target_on"] is True


def test_pair_instance_button_is_off_for_its_own_or_the_global_switch(tmp_path, monkeypatch):
    e = _env(tmp_path, monkeypatch, instance="XAUUSD")

    def st() -> dict:
        app_mod._SNAP_CACHE["ts"] = 0
        return e.client.get("/api/status").json()["kill_switch"]

    assert st() == {"on": False, "target_on": False, "files": []}
    (e.data / "KILL_SWITCH").write_text("", encoding="utf-8")            # global → this system is stopped too
    assert st()["target_on"] is True and e.client.get("/api/kill_switch").json()["target_on"] is True
    (e.data / "KILL_SWITCH").unlink()
    own = e.data / "instances" / "XAUUSD" / "KILL_SWITCH"
    own.write_text("", encoding="utf-8")
    assert st() == {"on": True, "target_on": True, "files": [str(own)]}


def test_kill_switch_of_a_pair_instance_writes_only_that_pairs_switch(tmp_path, monkeypatch):
    e = _env(tmp_path, monkeypatch, instance="XAUUSD")
    assert e.s.api.port == 8768 and e.origin.endswith(":8768")
    r = e.client.post("/api/kill_switch", headers={"X-Dashboard-Token": TOKEN, "Origin": e.origin})
    body = r.json()
    assert r.status_code == 200 and body["scope"] == "XAUUSD" and body["global"] is False
    assert (e.data / "instances" / "XAUUSD" / "KILL_SWITCH").exists()
    assert not (e.data / "KILL_SWITCH").exists() and not (e.data / "instances" / "BTCUSDT").exists()
    assert body["file"] == str(e.data / "instances" / "XAUUSD" / "KILL_SWITCH")
    # a POST without an Origin (a local script) needs only the token; the Origin of another instance's port is refused
    assert e.client.post("/api/kill_switch", headers={"X-Dashboard-Token": TOKEN}).status_code == 200
    r = e.client.post("/api/kill_switch", headers={"X-Dashboard-Token": TOKEN, "Origin": "http://127.0.0.1:8766"})
    assert r.status_code == 403


def test_execute_now_keeps_its_protection_after_the_guard_refactor(env):
    assert env.client.post("/api/decisions/x/execute").status_code == 401
    evil = {"X-Dashboard-Token": TOKEN, "Origin": "http://evil.example"}
    r = env.client.post("/api/decisions/x/execute", headers=evil)
    assert r.status_code == 403
    assert env.client.post("/api/decisions/x/execute", headers={"X-Dashboard-Token": TOKEN}).status_code == 404
    assert not env.db.exists()                          # no empty app.db created by the API


# ---------------------------------------------------------------------- page
def test_page_has_the_new_tabs_and_a_cache_buster(env):
    html = env.client.get("/").text
    assert "__ASSET_VERSION__" not in html
    v = html.split("/static/app.js?v=", 1)[1].split('"', 1)[0]
    assert v and f"/static/style.css?v={v}" in html
    for tab in ("operator", "tuning", "proposals", "reviews"):
        assert f'data-tab="{tab}"' in html and f'id="tab-{tab}"' in html
    assert 'id="kill-on"' in html
    assert env.client.get(f"/static/app.js?v={v}").status_code == 200


def test_kill_switch_button_reads_as_an_action_is_outlined_and_disabled_while_its_own_switch_is_on(env):
    html = env.client.get("/").text
    button = html.split('id="kill-on"', 1)[1].split("</button>", 1)[0]
    assert button.endswith(">Engage kill switch…") and ">Kill switch ON<" not in html     # an action, not a state
    assert 'class="badge kill hidden"' in html and ">KILL SWITCH ON</span>" in html        # the state stays the badge
    css = env.client.get("/static/style.css").text
    rule = lambda sel: css.split(sel + " {", 1)[1].split("}", 1)[0]                    # noqa: E731
    danger, badge = rule("button.danger"), rule(".badge.kill")
    assert "background: transparent" in danger and "color: var(--red)" in danger
    assert "border: 1px solid var(--red)" in danger and "color: white" not in danger
    assert "background: var(--red)" in badge and "color: white" in badge                # filled: clearly different
    assert "cursor: not-allowed" in rule("button.danger:disabled")
    js = env.client.get("/static/app.js").text
    # disabled only by the flag for the file the POST would write (or the global one): never by the badge's "any
    # switch" nor by the executor heartbeat; the tooltip follows the same flag
    assert "const targetOn = !!STATUS.kill_switch?.target_on;" in js and "kb.disabled = targetOn;" in js
    assert "kb.disabled = killOn" not in js and 'kb.title = targetOn ? "kill switch already engaged' in js
    assert '$("#kill-badge").classList.toggle("hidden", !killOn);' in js                    # the badge: any switch
    assert 'addEventListener("click", killSwitchNow)' in js and 'method: "POST"' in js    # flow unchanged


# ---------------------------------------------------------------------- scripts\check_ops.ps1 (static: never run here)
def test_check_ops_counts_every_fix_line_so_the_summary_cannot_say_all_is_right():
    """A FIX line printed outside Report() (e.g. an operator task's 'result 3 = config invalid') must count toward
    $script:todo in its own block, or the final line says 'all checked settings look right' next to it."""
    raw = (Path(__file__).resolve().parents[2] / "scripts" / "check_ops.ps1").read_bytes()
    assert raw.isascii() and b"\n" not in raw.replace(b"\r\n", b"")        # Windows PowerShell 5.1: ASCII + CRLF
    lines = raw.decode("ascii").split("\r\n")
    indent = lambda s: len(s) - len(s.lstrip())                              # noqa: E731
    direct = [i for i, ln in enumerate(lines) if ln.lstrip().startswith(('Write-Host ("FIX', 'Write-Host "FIX'))]
    heads = []
    for i in direct:                     # walk back through the FIX line's own block up to the line that opens it
        block, j = [], i - 1
        while j >= 0 and lines[j].strip() and indent(lines[j]) >= indent(lines[i]):
            block.append(lines[j])
            j -= 1
        assert any("$script:todo++" in ln for ln in block), f"line {i + 1} prints FIX without counting it"
        heads.append(lines[j])
    assert any('"Last Result") -eq "3"' in h for h in heads)        # the operator tasks' config-error branch is covered
