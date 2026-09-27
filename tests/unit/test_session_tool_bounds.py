"""What the tools an operator session may run keep apart from the system itself (session-safety review): tools/notify.py
namespaces its dedupe keys, so a key chosen by a script or a session never dedupes a system notification away; tune.py
records a session's change as operator-session:<review id>, whatever --actor it names."""
from __future__ import annotations

import argparse
import importlib.util
import io
import sqlite3
from pathlib import Path

import pytest

from tradingsystem.core.settings import INSTANCE_ENV, load_settings
from tradingsystem.core.timeutil import MS_PER_DAY, MS_PER_MINUTE

ROOT = Path(__file__).resolve().parents[2]


def notify_tool():
    spec = importlib.util.spec_from_file_location("test_ts_notify_tool", ROOT / "tools" / "notify.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture()
def sent(monkeypatch):
    t = notify_tool()
    calls: list[dict] = []
    monkeypatch.setattr(t, "setup_log", lambda s: None)
    monkeypatch.setattr(t.nt, "notify", lambda s, level, title, text, key=None, pair=None: calls.append(
        {"level": level, "title": title, "key": key, "pair": pair}))
    monkeypatch.setattr(t.nt, "flush", lambda timeout_s=15.0: None)
    monkeypatch.setattr(t.nt, "last_result", lambda: None)
    monkeypatch.setattr(t.nt, "pending", lambda: 0)
    return t, calls


@pytest.mark.parametrize("key", ["kill_switch_BTCUSDT", "kill_switch_all", "monitor:stale_heartbeat_ETHUSDT",
                                 "proposal_2026-09-27-x", "review_touched_checkout", "tune:BTCUSDT"])
def test_a_cli_key_never_collides_with_a_system_key(sent, key):
    """kill_switch.py, the monitor, propose.py, tune.py and the session runner dedupe under their own keys: the same
    text given to tools/notify.py --key is sent as cli:<key>, a different key."""
    t, calls = sent
    s = load_settings()
    assert t.main(["--level", "critical", "--title", "BTCUSDT order burst", "--text", "x", "--key", key],
                  settings=s) == 0
    assert calls == [{"level": "critical", "title": "BTCUSDT order burst", "key": f"cli:{key}", "pair": None}]


def test_no_key_stays_no_key_and_the_key_rules_apply_before_the_prefix(sent):
    t, calls = sent
    s = load_settings()
    assert t.main(["--level", "info", "--title", "t", "--text", "x"], settings=s) == 0
    assert t.main(["--level", "info", "--title", "t", "--text", "x", "--key", "  "], settings=s) == 0
    assert [c["key"] for c in calls] == [None, None]
    assert t.main(["--level", "info", "--title", "t", "--text", "x", "--key", "k" * t.KEY_MAX], settings=s) == 0
    assert calls[-1]["key"] == "cli:" + "k" * t.KEY_MAX
    assert t.main(["--level", "info", "--title", "t", "--text", "x", "--key", "k" * (t.KEY_MAX + 1)],
                  settings=s) == 3
    assert len(calls) == 3


def test_the_demo_order_test_refuses_to_run_in_an_operator_session():
    """The only tool that sends MT5 orders stops before it loads settings or touches the terminal when a session's
    marker is set (a second layer behind the allow-list, which never names it)."""
    import os
    import subprocess
    import sys
    env = {**os.environ, "TS_OPERATOR_SESSION": "1", "PYTHONPATH": str(ROOT / "src")}
    r = subprocess.run([sys.executable, str(ROOT / "tools" / "demo_order_test.py")], env=env, capture_output=True,
                       text=True, timeout=60)
    assert r.returncode == 2 and "refused" in r.stderr and not r.stdout


# ------------------------------------------------------------------ tools/tune.py: a session's change is the session's
TUNE_NOW = 1790334600000                        # 2026-09-25 11:10 UTC
EVIDENCE = '{"virtual": {"tp1_first": 9, "sl_first": 14}}'


@pytest.fixture()
def tune_env(tmp_path, monkeypatch):
    """tune.py on a tmp data root: BTCUSDT's app.db as its services create it, 25 resolved virtual outcomes; no .env,
    notifications captured."""
    from tradingsystem.ai.providers import base
    from tradingsystem.ai.store import DecisionStore
    from tradingsystem.ingest.common.appdb import AppDB
    monkeypatch.setattr(base, "ENV_FILE", tmp_path / "no_such_env_file")
    monkeypatch.setattr(base, "_dotenv", (float("-inf"), {}))
    for name in ("TS_OPERATOR_SESSION", "TS_OPERATOR_REVIEW_ID"):             # a human's call unless a test says so
        monkeypatch.delenv(name, raising=False)
    spec = importlib.util.spec_from_file_location("test_ts_tune_actor", ROOT / "tools" / "tune.py")
    t = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(t)
    notes: list[str] = []
    monkeypatch.setattr(t, "_notify", lambda s, title, text, *, key, pair: notes.append(text))
    cache: dict[str, object] = {}

    def loader(pair):
        if pair not in cache:
            s = load_settings(env_path=tmp_path / "none.env", extra_env={INSTANCE_ENV: pair or ""})
            paths = s.paths.model_copy(update={"data_dir": str(tmp_path / "data")})
            cache[pair] = s.model_copy(update={"paths": paths})
        return cache[pair]

    db = tmp_path / "data" / "instances" / "BTCUSDT" / "app.db"
    DecisionStore(db).close()
    AppDB(db).close()
    con = sqlite3.connect(db)
    con.executemany("INSERT INTO ai_decisions(id, ts, pair, mode, status, virtual_outcome) VALUES (?,?,?,?,?,?)",
                    [(f"v{i}", TUNE_NOW - 30 * MS_PER_MINUTE - i * (5 * MS_PER_DAY // 25), "BTCUSDT", "agent_per_pair",
                      "valid", "tp1_first" if i % 2 else "sl_first") for i in range(25)])
    con.commit()
    con.close()

    def run(*argv):
        out = io.StringIO()
        rc = t.run(list(argv), loader=loader, now_ms=TUNE_NOW, out=out)
        return rc, out.getvalue()

    def actors():
        con = sqlite3.connect(db)
        try:
            return [r[0] for r in con.execute("SELECT actor FROM tuning_changes ORDER BY id")]
        finally:
            con.close()

    return t, run, actors, loader, notes


def test_a_sessions_tuning_change_and_revert_are_recorded_as_the_session(tune_env, monkeypatch):
    """tuning_changes, changes.jsonl and the notification name operator-session:<review id> (exported by the session
    runner), never the --actor a session chose — the owner must see where a change came from."""
    from tradingsystem.core import adaptive as ad
    t, run, actors, loader, notes = tune_env
    monkeypatch.setenv("TS_OPERATOR_SESSION", "1")
    monkeypatch.setenv("TS_OPERATOR_REVIEW_ID", "20260927T043000Z_daily")
    me = "operator-session:20260927T043000Z_daily"
    rc, out = run("--pair", "BTCUSDT", "--actor", "owner", "set", "min_confidence_floor", "60", "--reason",
                  "review found it", "--evidence-json", EVIDENCE, "--review-id", "20260927T043000Z_daily")
    assert rc == 0, out
    rc, out = run("--pair", "BTCUSDT", "revert", "min_confidence_floor", "--reason", "undo", "--actor", "monitor")
    assert rc == 0, out
    assert actors() == [me, me]
    assert [c["actor"] for c in ad.read_changes(loader("BTCUSDT"), "BTCUSDT")] == [me, me]
    assert len(notes) == 2 and all(n.endswith(f"(by {me})") for n in notes)


def test_the_actor_rule_of_tune_py(tune_env, monkeypatch):
    t, _, _, loader, _ = tune_env
    s = loader("BTCUSDT")
    ask = argparse.Namespace(actor="owner")
    assert t._actor(ask, s) == "owner"                                        # a human names themselves
    with pytest.raises(t.Invalid):
        t._actor(argparse.Namespace(actor="bad actor!"), s)
    monkeypatch.setenv("TS_OPERATOR_SESSION", "1")
    monkeypatch.delenv("TS_OPERATOR_REVIEW_ID", raising=False)
    assert t._actor(ask, s) == "operator-session"                              # no review id exported
    monkeypatch.setenv("TS_OPERATOR_REVIEW_ID", "not a review id")
    assert t._actor(ask, s) == "operator-session"
    monkeypatch.setenv("TS_OPERATOR_REVIEW_ID", "20260927T120000Z_diagnose")
    assert t._actor(argparse.Namespace(actor="bad actor!"), s) == "operator-session:20260927T120000Z_diagnose"
