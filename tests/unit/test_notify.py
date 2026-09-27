"""core/notify.py + tools/notify.py (Phase 4, §3.8 component 5): log line, toast, Telegram, limits, redaction.

Nothing here reaches the desktop or the network: Telegram goes through ``httpx.MockTransport``, the toast runner
(``subprocess.run``) is replaced by a recorder, the ``.env`` the notifier reads (``base.ENV_FILE``) is a tmp file
(never the checkout's), and the data root is tmp_path. ``tests/conftest.py`` sets ``TS_NOTIFY_DISABLE=1`` for the
suite; the tests that exercise the sinks remove it.
"""
from __future__ import annotations

import base64
import importlib.util
import json
import logging
import os
import subprocess
import threading
import time
from pathlib import Path

import httpx
import pytest

from tradingsystem.ai.providers import base
from tradingsystem.core import notify as nt
from tradingsystem.core.settings import INSTANCE_ENV, TELEGRAM_SECRET_ENV, PathsCfg, load_settings
from tradingsystem.core.timeutil import MS_PER_MINUTE

ROOT = Path(__file__).resolve().parents[2]
TOKEN = "1234567890:AAHtestTokenNotReal_abcdefghijklmnopq"      # the shape of a bot token (redactor patterns)
CHAT = "987654321"
T0 = 1_790_000_000_000


def settings(tmp_path: Path, **notify):
    s = load_settings(ROOT / "config" / "config.yaml", env_path=tmp_path / "absent.env",
                      extra_env={INSTANCE_ENV: ""})
    paths = PathsCfg(data_dir=str(tmp_path / "data"), logs_dir=str(tmp_path / "logs"))
    return s.model_copy(update={"paths": paths, "notify": s.notify.model_copy(update=notify)})


def ok_response(req: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """No real .env, no Telegram secrets from the environment, no process start, no real network."""
    monkeypatch.setattr(base, "ENV_FILE", tmp_path / "absent.env")
    monkeypatch.setattr(base, "_dotenv", (float("-inf"), {}))
    for name in TELEGRAM_SECRET_ENV:
        monkeypatch.delenv(name, raising=False)
    runs: list[tuple[list[str], dict]] = []

    def fake_run(cmd, **kw):                          # noqa: ANN001
        runs.append((list(cmd), kw))
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr(nt.subprocess, "run", fake_run)
    net: list[httpx.Request] = []
    monkeypatch.setattr(nt, "_DEFAULT", nt.Notifier(transport=httpx.MockTransport(
        lambda r: net.append(r) or httpx.Response(599))))
    return {"runs": runs, "net": net}


@pytest.fixture
def live(monkeypatch):
    """Sinks on (the suite-wide TS_NOTIFY_DISABLE removed) and Telegram configured through the environment."""
    monkeypatch.delenv(nt.DISABLE_ENV, raising=False)
    monkeypatch.setenv(TELEGRAM_SECRET_ENV[0], TOKEN)
    monkeypatch.setenv(TELEGRAM_SECRET_ENV[1], CHAT)


def recorder(handler=ok_response):
    reqs: list[httpx.Request] = []

    def h(req: httpx.Request) -> httpx.Response:
        reqs.append(req)
        return handler(req)

    return httpx.MockTransport(h), reqs


def notify_records(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == "notify"]


# --------------------------------------------------------------------------- the log line and the switches
def test_levels_and_the_log_line(tmp_path, caplog, isolated):
    s = settings(tmp_path)
    with caplog.at_level(logging.DEBUG, logger="notify"):
        nt.notify(s, "info", "Order filled", "BTCUSDT buy 0.01 @ 65000", pair="BTCUSDT")
        nt.notify(s, "warn", "Stale heartbeat", "engine 20 min")
        nt.notify(s, "critical", "Kill switch ON", "order burst", key="kill_switch_btcusdt")
        nt.notify(s, "bogus", "Unknown level", "shown as a warning")
        nt.flush(5)
    recs = notify_records(caplog)
    assert [r.levelno for r in recs] == [logging.INFO, logging.WARNING, logging.WARNING, logging.WARNING]
    assert [r.getMessage() for r in recs] == ["Order filled: BTCUSDT buy 0.01 @ 65000",
                                              "Stale heartbeat: engine 20 min",
                                              "[CRITICAL] Kill switch ON: order burst",
                                              "Unknown level: shown as a warning"]
    assert recs[0].ctx == {"level": "info", "pair": "BTCUSDT"}
    assert recs[2].ctx == {"level": "critical", "key": "kill_switch_btcusdt"}
    assert not any(r.levelno >= logging.ERROR for r in caplog.records)     # the health report counts those
    assert nt.LEVELS == ("info", "warn", "critical")


def test_disable_env_gives_the_log_line_only(tmp_path, monkeypatch, caplog, isolated):
    monkeypatch.setenv(nt.DISABLE_ENV, "1")
    monkeypatch.setenv(TELEGRAM_SECRET_ENV[0], TOKEN)
    monkeypatch.setenv(TELEGRAM_SECRET_ENV[1], CHAT)
    s = settings(tmp_path)
    with caplog.at_level(logging.INFO, logger="notify"):
        nt.notify(s, "critical", "Drawdown stop", "account 12 % below its peak", key="drawdown:acct")
        nt.flush(5)
    assert [r.getMessage() for r in notify_records(caplog)] == ["[CRITICAL] Drawdown stop: account 12 % below its peak"]
    assert nt.last_result()["why"] == "log only (TS_NOTIFY_DISABLE)"
    assert not isolated["net"] and not isolated["runs"]
    assert not (tmp_path / "data" / "shared" / nt.STATE_FILE).exists()


def test_disabled_config_and_min_level_only_log(tmp_path, live, isolated):
    n = nt.Notifier(transport=recorder()[0])
    off = n.submit(settings(tmp_path, enabled=False), "critical", "t", "x")
    low = n.submit(settings(tmp_path, min_level="warn"), "info", "t", "x")
    assert n.flush(5)
    assert off["why"] == "log only (notify.enabled: false)" and off["log"]
    assert low["why"] == "log only (below notify.min_level warn)"
    assert not isolated["runs"] and not isolated["net"]


# --------------------------------------------------------------------------- Telegram
def test_telegram_payload(tmp_path, live, isolated):
    transport, reqs = recorder()
    n = nt.Notifier(transport=transport)
    res = n.submit(settings(tmp_path, toast=False), "critical", "Kill switch ON", "x" * 5000, pair="ETHUSDT")
    assert n.flush(5)
    assert res["telegram"] == "sent" and res["toast"] == "off (notify.toast: false)" and res["done"]
    (req,) = reqs
    assert req.method == "POST" and str(req.url) == f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    body = json.loads(req.content)
    assert body["chat_id"] == CHAT and body["disable_web_page_preview"] is True and "parse_mode" not in body
    assert body["text"].startswith("[CRITICAL] ETHUSDT - Kill switch ON\nxxx") and len(body["text"]) <= 4000
    # the pair is not repeated when the title names it; the instance stands in for a missing pair
    s_eth = settings(tmp_path, toast=False)
    s_eth = s_eth.model_copy(update={"paths": s_eth.paths.model_copy(update={"instance": "ETHUSDT"})})
    n.submit(s_eth, "info", "ETHUSDT order placed", "buy")
    n.submit(s_eth, "warn", "Escalation failed", "timeout")
    assert n.flush(5)
    assert [json.loads(r.content)["text"] for r in reqs[1:]] == [
        "[INFO] ETHUSDT order placed\nbuy", "[WARN] ETHUSDT - Escalation failed\ntimeout"]


def test_token_added_to_env_file_is_used_without_a_restart(tmp_path, monkeypatch, isolated):
    monkeypatch.delenv(nt.DISABLE_ENV, raising=False)
    env = tmp_path / "user.env"
    env.write_text(f"TELEGRAM_BOT_TOKEN={TOKEN}\nTELEGRAM_CHAT_ID={CHAT}\n", encoding="utf-8")
    monkeypatch.setattr(base, "ENV_FILE", env)
    transport, reqs = recorder()
    n = nt.Notifier(transport=transport)
    res = n.submit(settings(tmp_path, toast=False), "info", "Review summary", "all quiet")
    assert n.flush(5)
    assert res["telegram"] == "sent" and json.loads(reqs[0].content)["chat_id"] == CHAT


NEW_TOKEN = "2222222222:AAHrotatedTokenNotReal_zyxwvutsrqpon"


def test_telegram_values_follow_the_env_file_without_a_restart(tmp_path, monkeypatch, isolated):
    """A running service: load_settings() copied the start-up .env into os.environ (the supervisor passes it on).
    Adding, rotating, emptying and removing the lines in .env must all reach it within a minute."""
    monkeypatch.delenv(nt.DISABLE_ENV, raising=False)
    env = tmp_path / "user.env"
    monkeypatch.setattr(base, "ENV_FILE", env)
    clock = {"t": 1000.0}
    transport, reqs = recorder()
    n = nt.Notifier(transport=transport, monotonic=lambda: clock["t"])
    s = settings(tmp_path, toast=False, rate_per_hour=100)

    def send() -> str:
        res = n.submit(s, "info", "Order filled", "x")
        assert n.flush(5)
        return res["telegram"]

    def token_used() -> str:
        return str(reqs[-1].url).split("/bot")[1].split("/")[0]

    skipped = "skipped (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set)"
    assert send() == skipped                                         # no .env yet
    env.write_text(f"TELEGRAM_BOT_TOKEN={TOKEN}\nTELEGRAM_CHAT_ID={CHAT}\n", encoding="utf-8")        # add
    clock["t"] += 61
    assert send() == "sent" and token_used() == TOKEN
    monkeypatch.setenv(TELEGRAM_SECRET_ENV[0], TOKEN)                # the start-up copy load_env() makes
    monkeypatch.setenv(TELEGRAM_SECRET_ENV[1], CHAT)
    env.write_text(f"TELEGRAM_BOT_TOKEN={NEW_TOKEN}\nTELEGRAM_CHAT_ID={CHAT}\n", encoding="utf-8")    # rotate
    assert send() == "sent" and token_used() == TOKEN                # the file is re-read at most once a minute
    clock["t"] += 61
    assert send() == "sent" and token_used() == NEW_TOKEN            # the revoked token is no longer used
    env.write_text("TELEGRAM_BOT_TOKEN=\nTELEGRAM_CHAT_ID=\n", encoding="utf-8")                     # empty = off
    clock["t"] += 61
    assert send() == skipped
    env.write_text(f"TELEGRAM_BOT_TOKEN={NEW_TOKEN}\nTELEGRAM_CHAT_ID={CHAT}\n", encoding="utf-8")
    clock["t"] += 61
    assert send() == "sent"
    env.write_text("OTHER=1\n", encoding="utf-8")                   # the lines removed: off, not the start-up copy
    clock["t"] += 61
    assert send() == skipped
    assert len(reqs) == 4


def test_environment_values_are_used_while_the_env_file_has_no_line(tmp_path, monkeypatch, isolated):
    monkeypatch.delenv(nt.DISABLE_ENV, raising=False)
    env = tmp_path / "user.env"
    env.write_text("OTHER=1\n", encoding="utf-8")
    monkeypatch.setattr(base, "ENV_FILE", env)
    monkeypatch.setenv(TELEGRAM_SECRET_ENV[0], TOKEN)
    monkeypatch.setenv(TELEGRAM_SECRET_ENV[1], CHAT)
    transport, reqs = recorder()
    n = nt.Notifier(transport=transport)
    res = n.submit(settings(tmp_path, toast=False), "info", "Order filled", "x")
    assert n.flush(5) and res["telegram"] == "sent" and TOKEN in str(reqs[0].url)


def test_an_unreadable_env_file_keeps_the_last_telegram_values(tmp_path, monkeypatch, isolated):
    """A .env being saved (locked, half written) must not switch Telegram off or back to the start-up copy."""
    monkeypatch.delenv(nt.DISABLE_ENV, raising=False)
    env = tmp_path / "user.env"
    env.write_text(f"TELEGRAM_BOT_TOKEN={NEW_TOKEN}\nTELEGRAM_CHAT_ID={CHAT}\n", encoding="utf-8")
    monkeypatch.setattr(base, "ENV_FILE", env)
    monkeypatch.setenv(TELEGRAM_SECRET_ENV[0], TOKEN)
    monkeypatch.setenv(TELEGRAM_SECRET_ENV[1], CHAT)
    clock = {"t": 1000.0}
    n = nt.Notifier(transport=recorder()[0], monotonic=lambda: clock["t"])
    assert n._telegram_creds() == (NEW_TOKEN, CHAT)

    def locked(path):                                                # noqa: ANN001
        raise PermissionError(13, "in use", str(path))

    monkeypatch.setattr(nt, "dotenv_values", locked)
    clock["t"] += 61
    assert n._telegram_creds() == (NEW_TOKEN, CHAT)


@pytest.mark.parametrize("env", [{}, {"TELEGRAM_BOT_TOKEN": TOKEN}, {"TELEGRAM_CHAT_ID": CHAT}])
def test_unset_token_is_skipped_silently(tmp_path, monkeypatch, caplog, isolated, env):
    monkeypatch.delenv(nt.DISABLE_ENV, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    transport, reqs = recorder()
    n = nt.Notifier(transport=transport)
    with caplog.at_level(logging.DEBUG):
        res = n.submit(settings(tmp_path, toast=False), "info", "Order filled", "fine")
        assert n.flush(5)
    assert res["telegram"] == "skipped (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set)" and not reqs
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert not (tmp_path / "data" / "shared" / nt.STATE_FILE).exists()      # nothing to send, nothing recorded


def test_the_token_and_chat_id_never_reach_a_log_line(tmp_path, live, caplog, isolated):
    def refused(req: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"ok": False, "description": f"Unauthorized: chat {CHAT} bot {TOKEN}"})

    def unreachable(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot reach {req.url}", request=req)

    def status_error(req: httpx.Request) -> httpx.Response:
        resp = httpx.Response(502, request=req)
        resp.raise_for_status()                          # HTTPStatusError: its text holds the full URL
        return resp

    def encoded(req: httpx.Request) -> httpx.Response:
        raise RuntimeError(f"bad url https://api.telegram.org/bot{TOKEN.replace(':', '%3A')}/sendMessage")

    s = settings(tmp_path, toast=False)
    results = []
    with caplog.at_level(logging.DEBUG), caplog.at_level(logging.DEBUG, logger="httpx"):
        for handler in (refused, unreachable, status_error, encoded):
            n = nt.Notifier(transport=httpx.MockTransport(handler))
            results.append(n.submit(s, "warn", "Order failed", "retcode 10019"))
            assert n.flush(5)
    everything = caplog.text + "\n".join(r.getMessage() for r in caplog.records) + json.dumps(results)
    assert TOKEN not in everything and TOKEN.split(":")[1] not in everything and CHAT not in everything
    assert [r["telegram"].split(":")[0] for r in results] == ["failed"] * 4
    assert results[0]["telegram"].startswith("failed: HTTP 401 Unauthorized: chat ****")
    assert "ConnectError" in results[1]["telegram"] and "/bot****" in results[1]["telegram"]
    assert "HTTPStatusError" in results[2]["telegram"] and "/bot****" in results[2]["telegram"]
    httpx_lines = [r.getMessage() for r in caplog.records if r.name == "httpx"]
    assert httpx_lines and all("/bot****/sendMessage" in m for m in httpx_lines)    # httpx's own INFO line
    warned = [r for r in caplog.records if r.name == "notify" and "Telegram sendMessage failed" in r.getMessage()]
    assert warned and all(r.levelno == logging.WARNING for r in warned)


# --------------------------------------------------------------------------- limits
def test_rate_limit_per_process(tmp_path, live, caplog, isolated):
    clock = {"t": 1000.0}
    transport, reqs = recorder()
    n = nt.Notifier(transport=transport, monotonic=lambda: clock["t"])
    s = settings(tmp_path, toast=False, rate_per_hour=3)
    with caplog.at_level(logging.DEBUG, logger="notify"):
        res = [n.submit(s, "info", f"event {i}", "x") for i in range(5)]
        assert n.flush(5)
    assert [r["telegram"] for r in res] == ["sent"] * 3 + ["rate limit 3 info/hour: log only"] * 2
    assert [r["rate_limited"] for r in res] == [False] * 3 + [True] * 2 and len(reqs) == 3
    limit_lines = [r for r in notify_records(caplog) if "rate limit" in r.getMessage()]
    assert [r.levelno for r in limit_lines] == [logging.WARNING, logging.DEBUG]      # one warning, then quiet
    clock["t"] += 3601
    later = n.submit(s, "info", "event 5", "x")
    assert n.flush(5) and later["telegram"] == "sent" and len(reqs) == 4


def test_info_traffic_never_uses_up_the_warning_budget(tmp_path, live, caplog, isolated):
    """Trailing stops, fills and closes (info) fill their budget; a management error (warn) still goes out."""
    transport, reqs = recorder()
    n = nt.Notifier(transport=transport, monotonic=lambda: 1000.0)
    s = settings(tmp_path, toast=False, rate_per_hour=2, dedupe_minutes=0)
    with caplog.at_level(logging.DEBUG, logger="notify"):
        infos = [n.submit(s, "info", f"stop moved {i}", "x") for i in range(4)]
        warns = [n.submit(s, "warn", f"management error {i}", "x") for i in range(3)]
        crit = n.submit(s, "critical", "Drawdown stop", "x")
        assert n.flush(5)
    assert [r["telegram"] for r in infos] == ["sent"] * 2 + ["rate limit 2 info/hour: log only"] * 2
    assert [r["telegram"] for r in warns] == ["sent"] * 2 + ["rate limit 2 warn/hour: log only"]
    assert crit["telegram"] == "sent" and not crit["rate_limited"] and len(reqs) == 5
    warned = [r.getMessage() for r in notify_records(caplog) if r.levelno == logging.WARNING
              and "rate limit" in r.getMessage()]
    assert len(warned) == 2 and "2 info/hour" in warned[0] and "2 warn/hour" in warned[1]   # one per level


def test_a_deduped_notification_costs_no_rate(tmp_path, live, isolated):
    transport, reqs = recorder()
    n = nt.Notifier(transport=transport, clock=lambda: T0)
    s = settings(tmp_path, toast=False, rate_per_hour=2)
    res = [n.submit(s, "warn", "Stale", "x", key=k) for k in ("a", "a", "a", "b")]
    assert n.flush(5)
    assert [r["telegram"] for r in res] == ["sent", "deduped (key sent within 30 min)",
                                            "deduped (key sent within 30 min)", "sent"]
    assert len(reqs) == 2


def test_dedupe_across_two_notifiers_through_the_state_file(tmp_path, live, isolated):
    """Two notifiers stand for two processes (the BTC and ETH systems): one key, one message."""
    wall = {"ms": T0}
    transport, reqs = recorder()
    n1 = nt.Notifier(transport=transport, clock=lambda: wall["ms"])
    n2 = nt.Notifier(transport=transport, clock=lambda: wall["ms"])
    s = settings(tmp_path, toast=False, dedupe_minutes=30)
    first = n1.submit(s, "warn", "Usage gauge", "level 0 -> 1", key="gauge:1")
    assert n1.flush(5)
    second = n2.submit(s, "warn", "Usage gauge", "level 0 -> 1", key="gauge:1")
    other = n2.submit(s, "warn", "Usage gauge", "level 1 -> 2", key="gauge:2")
    keyless = [n2.submit(s, "info", "Order filled", "x") for _ in range(2)]
    assert n2.flush(5)
    assert first["telegram"] == "sent" and second["deduped"] and not first["deduped"]
    assert other["telegram"] == "sent" and [r["telegram"] for r in keyless] == ["sent", "sent"]
    state = json.loads((tmp_path / "data" / "shared" / nt.STATE_FILE).read_text(encoding="utf-8"))
    assert state["keys"] == {"gauge:1": T0, "gauge:2": T0}
    assert (tmp_path / "data" / "shared" / "locks" / "notify_state.lock").exists()
    wall["ms"] += 29 * MS_PER_MINUTE
    within = n1.submit(s, "warn", "Usage gauge", "again", key="gauge:1")
    assert n1.flush(5) and within["deduped"] and within["telegram"] == "deduped (key sent within 30 min)"
    wall["ms"] += 2 * MS_PER_MINUTE                                       # 31 min after the first
    again = n2.submit(s, "warn", "Usage gauge", "still level 1", key="gauge:1")
    assert n2.flush(5) and again["telegram"] == "sent"
    assert len(reqs) == 5


def test_no_dedupe_window_writes_no_state(tmp_path, live, isolated):
    transport, reqs = recorder()
    n = nt.Notifier(transport=transport)
    s = settings(tmp_path, toast=False, dedupe_minutes=0)
    res = [n.submit(s, "warn", "t", "x", key="same") for _ in range(2)]
    assert n.flush(5)
    assert [r["telegram"] for r in res] == ["sent", "sent"]
    assert not (tmp_path / "data" / "shared" / nt.STATE_FILE).exists()


def test_a_corrupt_state_file_is_rebuilt(tmp_path, live, isolated):
    shared = tmp_path / "data" / "shared"
    shared.mkdir(parents=True)
    (shared / nt.STATE_FILE).write_text('{"keys": {"old": ', encoding="utf-8")
    transport, reqs = recorder()
    n = nt.Notifier(transport=transport, clock=lambda: T0)
    res = n.submit(settings(tmp_path, toast=False), "warn", "t", "x", key="k")
    assert n.flush(5) and res["telegram"] == "sent"
    assert json.loads((shared / nt.STATE_FILE).read_text(encoding="utf-8"))["keys"] == {"k": T0}


def test_trailing_stop_moves_collapse_into_one_notification(tmp_path, live, isolated):
    """executor._emit: a trailing rule re-plans the stop on every decision bar; the new value is not part of the
    mgmt_applied key, so the moves of one rule on one leg are one notification per dedupe window."""
    from types import SimpleNamespace

    from tradingsystem.execution.executor import Executor

    transport, reqs = recorder()
    n = nt.Notifier(transport=transport, clock=lambda: T0)
    s = settings(tmp_path, toast=False)
    events: list[str] = []
    results: list[dict] = []
    fake = SimpleNamespace(appdb=SimpleNamespace(add_event=lambda src, kind, detail: events.append(kind)),
                           _notify=lambda level, title, text, *, key=None, pair=None: results.append(
                               n.submit(s, level, title, text, key=key, pair=pair)))

    def applied(leg: str, rule: str, value: float) -> dict:
        return {"pair": "BTCUSDT", "decision": "d1", "leg": leg, "tp_index": 1, "rule": rule, "op": "set_sl",
                "value": value, "text": f"BTCUSDT leg 1: stop → {value} ({rule})"}

    for payload in (applied("L1", "trail_atr", 65000.0), applied("L1", "trail_atr", 65100.0),
                    applied("L1", "trail_atr", 65200.0), applied("L2", "trail_atr", 65100.0),
                    applied("L1", "move_sl_to_breakeven", 64900.0)):
        Executor._emit(fake, "mgmt_applied", payload)
    Executor._emit(fake, "mgmt_error", {"pair": "BTCUSDT", "decision": "d1", "leg": "L1", "rule": "trail_atr",
                                        "text": "BTCUSDT trail_atr failed 3×: retcode 10016"})
    assert n.flush(5)
    assert [r["key"] for r in results[:3]] == ["mgmt_applied:d1:L1:trail_atr"] * 3
    assert [r["deduped"] for r in results] == [False, True, True, False, False, False]
    assert len(reqs) == 4 and events == ["mgmt_applied"] * 5 + ["mgmt_error"]      # every move is still an event
    assert "65000.0" in json.loads(reqs[0].content)["text"]
    assert results[-1]["key"].startswith("mgmt_error:d1:L1:BTCUSDT trail_atr failed")

# --------------------------------------------------------------------------- toast
@pytest.mark.skipif(os.name != "nt", reason="the toast is Windows-only")
def test_toast_runner_gets_base64_args_and_no_window(tmp_path, monkeypatch, isolated):
    monkeypatch.delenv(nt.DISABLE_ENV, raising=False)
    n = nt.Notifier(transport=recorder()[0])
    title = 'BTCUSDT: "SL" ≥ 1.5 → <ok> & done'
    text = "line 1\nline 2 — café"
    res = n.submit(settings(tmp_path), "critical", title, text)
    assert n.flush(5)
    assert res["toast"] == "shown" and res["telegram"].startswith("skipped")
    ((cmd, kw),) = isolated["runs"]
    assert Path(cmd[0]).name.lower() == "powershell.exe" and cmd[cmd.index("-File") + 1] == str(nt.SCRIPT)
    assert nt.SCRIPT.exists() and all(a.strip() for a in cmd)              # PS 5.1 drops empty native args
    decode = lambda flag: base64.b64decode(cmd[cmd.index(flag) + 1]).decode("utf-8")    # noqa: E731
    assert decode("-TitleB64") == "CRITICAL: " + title and decode("-TextB64") == text
    assert cmd[cmd.index("-Level") + 1] == "critical"
    assert all(a.isascii() for a in cmd[cmd.index("-TitleB64"):])
    assert kw["creationflags"] == subprocess.CREATE_NO_WINDOW and kw["timeout"] >= nt.TOAST_MIN_TIMEOUT_S
    assert kw["capture_output"] is True


@pytest.mark.skipif(os.name != "nt", reason="the toast is Windows-only")
def test_toast_level_filter_and_failure(tmp_path, monkeypatch, caplog, isolated):
    monkeypatch.delenv(nt.DISABLE_ENV, raising=False)
    n = nt.Notifier(transport=recorder()[0])
    quiet = n.submit(settings(tmp_path, toast_min_level="warn"), "info", "Order filled", "x")
    assert n.flush(5)
    assert quiet["toast"] == "off (below notify.toast_min_level warn)" and not isolated["runs"]
    monkeypatch.setattr(nt.subprocess, "run",
                        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, b"", b"toast failed: no desktop\r\n"))
    with caplog.at_level(logging.DEBUG, logger="notify"):
        bad = n.submit(settings(tmp_path), "warn", "Stale heartbeat", "x")
        assert n.flush(5)
    assert bad["toast"] == "failed: exit 1: toast failed: no desktop"
    assert any(r.levelno == logging.WARNING and "toast failed" in r.getMessage() for r in notify_records(caplog))

    def hang(cmd, **kw):                              # noqa: ANN001
        raise subprocess.TimeoutExpired(cmd, kw["timeout"])

    monkeypatch.setattr(nt.subprocess, "run", hang)
    slow = n.submit(settings(tmp_path), "warn", "Stale heartbeat", "x")
    assert n.flush(5) and slow["toast"].startswith("failed: timed out after")


@pytest.mark.skipif(os.name != "nt", reason="the toast is Windows-only")
def test_the_toast_goes_out_before_a_stalled_telegram_request(tmp_path, live, monkeypatch, isolated):
    """A black-holed network: Telegram hangs until its timeout, the local toast must not wait for it (a
    short-lived tool's flush may end first, and the monitor counts a shown toast as delivered)."""
    order: list[str] = []
    entered, release = threading.Event(), threading.Event()

    def stalled(req: httpx.Request) -> httpx.Response:
        order.append("telegram")
        entered.set()
        release.wait(10)
        return ok_response(req)

    monkeypatch.setattr(nt.subprocess, "run",
                        lambda cmd, **kw: order.append("toast") or subprocess.CompletedProcess(cmd, 0, b"", b""))
    n = nt.Notifier(transport=httpx.MockTransport(stalled))
    res = [n.submit(settings(tmp_path), "warn", f"Stale quote {i}", "x") for i in range(2)]
    assert entered.wait(5)
    assert not n.flush(0.3)                                          # still waiting on the first Telegram call
    assert order == ["toast", "telegram"]
    assert res[0]["toast"] == "shown" and res[0]["telegram"] == "pending"
    release.set()
    assert n.flush(5)
    assert [r["toast"] for r in res] == ["shown"] * 2 and [r["telegram"] for r in res] == ["sent"] * 2
    assert order == ["toast", "telegram"] * 2


# --------------------------------------------------------------------------- robustness
def test_never_raises(tmp_path, monkeypatch, live, caplog, isolated):
    class Broken:
        def __str__(self) -> str:
            raise ValueError("no text")

    def explode(req: httpx.Request) -> httpx.Response:
        raise RuntimeError("socket gone")

    monkeypatch.setattr(nt, "_DEFAULT", nt.Notifier(transport=httpx.MockTransport(explode)))
    monkeypatch.setattr(nt.subprocess, "run", lambda cmd, **kw: (_ for _ in ()).throw(OSError("no powershell")))
    s = settings(tmp_path)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "shared").write_text("a file where the shared dir should be", encoding="utf-8")
    with caplog.at_level(logging.DEBUG):
        nt.notify(None, "warn", "no settings", "x")
        nt.notify(s, None, None, None, key=Broken(), pair=5)
        nt.notify(s, "critical", Broken(), Broken(), key="k")
        nt.notify(s, "info", "fine", "x", key="k2")
        nt.flush(5)
    assert nt.pending() == 0
    last = nt.last_result()
    assert last["toast"] == "failed: OSError: no powershell" and last["telegram"] == "failed: RuntimeError: socket gone"
    assert len(notify_records(caplog)) >= 4


def test_flush_waits_for_the_worker(tmp_path, live, monkeypatch, isolated):
    reqs: list[httpx.Request] = []

    def slow(req: httpx.Request) -> httpx.Response:
        time.sleep(0.3)
        reqs.append(req)
        return ok_response(req)

    monkeypatch.setattr(nt, "_DEFAULT", nt.Notifier(transport=httpx.MockTransport(slow)))
    s = settings(tmp_path, toast=False)
    nt.notify(s, "info", "Review summary", "x")
    assert not reqs                                   # the caller never waits for the network
    nt.flush(5)
    assert len(reqs) == 1 and nt.pending() == 0 and nt.last_result()["telegram"] == "sent"

    gate = threading.Event()
    monkeypatch.setattr(nt, "_DEFAULT", nt.Notifier(transport=httpx.MockTransport(
        lambda r: gate.wait(10) and ok_response(r))))
    nt.notify(s, "info", "Blocked", "x")
    t0 = time.monotonic()
    nt.flush(0.2)
    assert time.monotonic() - t0 < 2 and nt.pending() == 1                 # a bounded wait
    gate.set()
    nt.flush(5)
    assert nt.pending() == 0 and nt.last_result()["telegram"] == "sent"


def test_the_queue_is_bounded(tmp_path, live, isolated):
    entered, release = threading.Event(), threading.Event()
    reqs: list[httpx.Request] = []

    def blocked(req: httpx.Request) -> httpx.Response:
        reqs.append(req)
        entered.set()
        release.wait(10)
        return ok_response(req)

    n = nt.Notifier(transport=httpx.MockTransport(blocked), queue_size=2)
    s = settings(tmp_path, toast=False)
    first = n.submit(s, "info", "one", "x")
    assert entered.wait(5)                             # the worker holds #1; the queue has room for 2
    queued = [n.submit(s, "info", t, "x") for t in ("two", "three")]
    dropped = n.submit(s, "info", "four", "x")
    assert dropped["done"] and dropped["why"] == "queue full: log only" and dropped["log"]
    release.set()
    assert n.flush(5)
    assert [r["telegram"] for r in (first, *queued)] == ["sent"] * 3 and len(reqs) == 3


# --------------------------------------------------------------------------- tools/notify.py
def tool():
    spec = importlib.util.spec_from_file_location("notify_tool", ROOT / "tools" / "notify.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_tool_sends_prints_the_outcome_and_never_the_secrets(tmp_path, live, monkeypatch, capsys, isolated):
    t = tool()
    monkeypatch.setattr(t, "setup_log", lambda s: None)
    monkeypatch.setattr(nt, "_DEFAULT", nt.Notifier(transport=httpx.MockTransport(
        lambda r: httpx.Response(400, json={"ok": False, "description": f"Bad Request: chat {CHAT} not found"}))))
    s = settings(tmp_path, toast=False)
    rc = t.main(["--level", "warn", "--title", "Daily review", "--text", "2 changes; 1 proposal", "--key",
                 "review:daily", "--pair", "btcusdt"], settings=s)
    out = capsys.readouterr().out
    assert rc == 0                                     # a failed sink is not a failed notification
    assert 'notification warn: "Daily review" - log: written; toast: off (notify.toast: false); ' \
           "telegram: failed: HTTP 400 Bad Request: chat **** not found" in out
    assert TOKEN not in out and CHAT not in out
    assert nt.last_result()["pair"] == "BTCUSDT" and nt.last_result()["key"] == "cli:review:daily"   # namespaced

    monkeypatch.setenv(nt.DISABLE_ENV, "1")
    assert t.main(["--level", "info", "--title", "t", "--text", ""], settings=s) == 0
    assert "log only (TS_NOTIFY_DISABLE)" in capsys.readouterr().out


@pytest.mark.parametrize("argv", [
    ["--level", "loud", "--title", "t", "--text", "x"],
    ["--level", "info", "--title", "  ", "--text", "x"],
    ["--level", "info", "--title", "t"],
    ["--level", "info", "--title", "t", "--text", "x", "--key", "has space"],
    ["--level", "info", "--title", "t", "--text", "x", "--pair", "BTC/USDT"],
])
def test_tool_refuses_invalid_requests(tmp_path, monkeypatch, capsys, argv):
    t = tool()
    monkeypatch.setattr(t, "setup_log", lambda s: None)
    assert t.main(argv, settings=settings(tmp_path)) == 3
    assert capsys.readouterr().out.startswith("invalid:")
