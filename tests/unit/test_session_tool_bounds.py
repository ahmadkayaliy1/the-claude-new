"""What the tools an operator session may run keep apart from the system itself (session-safety review): tools/notify.py
namespaces its dedupe keys, so a key chosen by a script or a session never dedupes a system notification away."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from tradingsystem.core.settings import load_settings

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
