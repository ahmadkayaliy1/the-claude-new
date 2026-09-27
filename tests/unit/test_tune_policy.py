"""tools/tune.py — the only writer of the adaptive overlay (§3.8): min samples (20 strategy / 10 activity), one
change per pair and UTC day, the per-key cooldown, the freeze when > 25 % of the window was unhealthy, the direction
rules, TUNING_FREEZE / adaptive.enabled, a playbook the services reject, revert, dry-run, the path allow-list, what it
reads (a session: --text only; FILE: a plain file under the data root or the checkout), no secret ever stored, exit
codes 0 / 2 / 3."""
import functools
import importlib.util
import io
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from tradingsystem.ai.store import DecisionStore
from tradingsystem.core import adaptive as ad
from tradingsystem.core.filelock import FileLock
from tradingsystem.core.settings import INSTANCE_ENV, load_settings
from tradingsystem.core.timeutil import MS_PER_DAY, MS_PER_HOUR, MS_PER_MINUTE
from tradingsystem.ingest.common.appdb import AppDB

ROOT = Path(__file__).resolve().parents[2]
PAIR, OTHER = "BTCUSDT", "ETHUSDT"
NOW = 1790334600000                        # 2026-09-25 11:10 UTC
H = MS_PER_HOUR
EVIDENCE = '{"virtual": {"tp1_first": 9, "sl_first": 14}, "note": "low-confidence longs lose"}'


def _tool():
    spec = importlib.util.spec_from_file_location("tune_under_test", ROOT / "tools" / "tune.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


tune = _tool()


@functools.lru_cache(maxsize=None)
def _settings(instance: str):
    return load_settings(env_path=Path("nope.env"), extra_env={INSTANCE_ENV: instance})


class Env:
    def __init__(self, tmp_path: Path):
        self.root = tmp_path
        self.data = tmp_path / "data"
        self.adaptive_overrides: dict = {}

    def loader(self, pair):
        s = _settings(pair or "")
        upd = {"paths": s.paths.model_copy(update={"data_dir": str(self.data)})}
        if self.adaptive_overrides:
            upd["adaptive"] = s.adaptive.model_copy(update=self.adaptive_overrides)
        return s.model_copy(update=upd)

    @property
    def s(self):
        return self.loader(PAIR)

    def db(self, pair=PAIR) -> Path:
        return self.data / "instances" / pair / "app.db"

    def run(self, *argv, now=NOW, stdin=None):
        out = io.StringIO()
        rc = tune.run(list(argv), loader=self.loader, now_ms=now, stdin=stdin, out=out)
        self.out = out.getvalue()
        return rc

    def set(self, key, value, *extra, now=NOW):
        return self.run("--pair", PAIR, "set", key, str(value), "--reason", "review found it", "--evidence-json",
                        EVIDENCE, "--review-id", "r-test", *extra, now=now)

    def sql(self, q, *args, pair=PAIR):
        con = sqlite3.connect(self.db(pair))
        try:
            return con.execute(q, args).fetchall()
        finally:
            con.close()

    def seed(self, pair=PAIR, *, resolved=25, at=NOW):
        """A pair's app.db as its services create it, with ``resolved`` resolved virtual outcomes in the last 5 days."""
        db = self.db(pair)
        DecisionStore(db).close()
        AppDB(db).close()
        step = 5 * MS_PER_DAY // max(resolved, 1)
        rows = [(f"{pair}-v{at}-{i}", at - 30 * MS_PER_MINUTE - i * step, pair, "valid",
                 "tp1_first" if i % 2 else "sl_first") for i in range(resolved)]
        self.decisions(rows, pair=pair)

    def decisions(self, rows, pair=PAIR, warnings=None):
        con = sqlite3.connect(self.db(pair))
        con.executemany("INSERT INTO ai_decisions(id, ts, pair, mode, status, virtual_outcome, data_warnings) "
                        "VALUES (?,?,?,'agent_per_pair',?,?,?)",
                        [(i, ts, p, st, vo, warnings) for i, ts, p, st, vo in rows])
        con.commit()
        con.close()

    def events(self, rows, pair=PAIR):
        con = sqlite3.connect(self.db(pair))
        con.executemany("INSERT INTO ingestion_events(ts, collector, event, detail, duration_ms) VALUES (?,?,?,?,?)",
                        rows)
        con.commit()
        con.close()

    def overlay(self):
        p = ad.adaptive_dir(self.s, PAIR) / ad.YAML_FILE
        return yaml.safe_load(p.read_text(encoding="utf-8")) if p.exists() else None


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    e.seed()
    return e


@pytest.fixture(autouse=True)
def _no_dotenv_and_no_session(tmp_path, monkeypatch):
    """tune.py looks up the configured secrets' values (the environment, else .env): never a real .env here — and
    never an operator-session marker inherited from the environment."""
    from tradingsystem.ai.providers import base
    monkeypatch.setattr(base, "ENV_FILE", tmp_path / "no_such_env_file")
    monkeypatch.setattr(base, "_dotenv", (float("-inf"), {}))
    monkeypatch.delenv(tune.SESSION_ENV, raising=False)


# ------------------------------------------------------------------ applied
def test_a_change_is_applied_and_recorded_in_every_place(env):
    assert env.set("min_confidence_floor", 60) == 0, env.out
    assert env.out.startswith("applied: BTCUSDT min_confidence_floor 55 -> 60 until")
    e = env.overlay()["min_confidence_floor"]
    assert (e["value"], e["set_ms"], e["expires_ms"], e["window_hours"], e["review_id"]) == (
        60, NOW, NOW + 14 * MS_PER_DAY, 168, "r-test")
    assert e["evidence"]["virtual"]["sl_first"] == 14
    rows = env.sql("SELECT pair, key, old_value, new_value, window_hours, expires_ms, review_id, actor, reverted_ms "
                   "FROM tuning_changes")
    assert rows == [(PAIR, "min_confidence_floor", "55", "60", 168, NOW + 14 * MS_PER_DAY, "r-test", "operator", None)]
    line = ad.read_changes(env.s, PAIR)[-1]
    assert (line["action"], line["key"], line["old"], line["new"], line["change_id"]) == (
        "set", "min_confidence_floor", 55, 60, 1)
    eff = ad.AdaptiveStore(env.s, PAIR).effective(NOW + 1)
    assert eff.min_confidence == 60 and eff.adaptive_hash


def test_every_key_can_be_set_within_its_rules(env):
    day = 0
    for key, value in [("min_minutes_between_calls", 30), ("max_idle_minutes", 180), ("review_floor_minutes", 10),
                       ("trigger.weak_min", 3), ("trigger.liquidity_atr", 0.25), ("pair.ai_paused_until", "+6h"),
                       ("tp_hint", "TP1 at the prior 15m swing when the 1h trend disagrees")]:
        now = NOW + day * MS_PER_DAY
        env.seed(resolved=0, at=now)                      # tables exist already; samples come from the first seed
        env.decisions([(f"d{day}-{i}", now - (i + 1) * H, PAIR, "valid", "tp1_first") for i in range(20)])
        assert env.set(key, value, now=now) == 0, (key, env.out)
        day += 1
    eff = ad.AdaptiveStore(env.s, PAIR).effective(NOW + 6 * MS_PER_DAY + 1)
    assert (eff.min_minutes_between_calls, eff.max_idle_minutes, eff.review_floor_minutes, eff.weak_min,
            eff.liquidity_atr) == (30, 180, 10, 3, 0.25)
    assert eff.ai_paused_until_ms is None                                     # the 6-h pause is over
    assert ad.AdaptiveStore(env.s, PAIR).effective(NOW + 5 * MS_PER_DAY + H).ai_paused_until_ms == (
        NOW + 5 * MS_PER_DAY + 6 * H)
    assert eff.tp_hint.startswith("TP1 at the prior")


# ------------------------------------------------------------------ min samples
@pytest.mark.parametrize("key,value,need", [("min_confidence_floor", 60, 20), ("trigger.weak_min", 3, 20),
                                            ("trigger.liquidity_atr", 0.25, 20), ("tp_hint", "TP1 at the swing", 20),
                                            ("min_minutes_between_calls", 30, 10), ("max_idle_minutes", 180, 10),
                                            ("review_floor_minutes", 10, 10), ("pair.ai_paused_until", "+2h", 10)])
def test_min_samples(tmp_path, key, value, need):
    env = Env(tmp_path)
    env.seed(resolved=need - 1)
    # rows that must not count: unresolved, another pair's, outside the window
    env.decisions([("u1", NOW - H, PAIR, "valid", "not_triggered"), ("u2", NOW - H, PAIR, "valid", None),
                   ("old", NOW - 200 * H, PAIR, "valid", "tp1_first"), ("eth", NOW - H, OTHER, "valid", "sl_first")])
    assert env.set(key, value) == 2
    assert f"{need - 1} resolved virtual outcomes" in env.out and f"needs {need}" in env.out
    assert env.overlay() is None and env.sql("SELECT count(*) FROM tuning_changes") == [(0,)]
    env.decisions([("last", NOW - 2 * H, PAIR, "valid", "tp1_first")])
    assert env.set(key, value) == 0, env.out


def test_the_window_decides_which_outcomes_count(env):
    assert env.set("min_confidence_floor", 60, "--window-hours", "24") == 2      # 25 outcomes over 5 days
    assert "in the last 24 h" in env.out


# ------------------------------------------------------------------ one per day, cooldown
def test_one_change_per_pair_per_utc_day(env):
    assert env.set("min_confidence_floor", 60) == 0
    assert env.set("max_idle_minutes", 180, now=NOW + H) == 2
    assert "1 change(s) of BTCUSDT today already (max 1 per UTC day)" in env.out
    midnight = NOW - NOW % MS_PER_DAY + MS_PER_DAY
    env.decisions([(f"n{i}", midnight - (i + 1) * H, PAIR, "valid", "tp1_first") for i in range(10)])
    assert env.set("max_idle_minutes", 180, now=midnight + 1) == 0, env.out


def test_another_pair_has_its_own_day(env):
    assert env.set("min_confidence_floor", 60) == 0
    env.seed(OTHER)
    out = io.StringIO()
    rc = tune.run(["--pair", OTHER, "set", "min_confidence_floor", "60", "--reason", "same finding",
                   "--evidence-json", "{}"], loader=env.loader, now_ms=NOW + 1, out=out)
    assert rc == 0, out.getvalue()
    assert env.sql("SELECT count(*) FROM tuning_changes", pair=OTHER) == [(1,)]
    assert env.sql("SELECT count(*) FROM tuning_changes") == [(1,)]


def test_cooldown_per_key(env):
    assert env.set("min_confidence_floor", 60) == 0
    later = NOW + 2 * MS_PER_DAY
    env.decisions([(f"c{i}", later - (i + 1) * H, PAIR, "valid", "sl_first") for i in range(25)])
    assert env.set("min_confidence_floor", 62, now=later) == 2
    assert "cooldown: min_confidence_floor changed" in env.out
    assert env.set("max_idle_minutes", 180, now=later) == 0, env.out          # another key is free
    week = NOW + 7 * MS_PER_DAY
    env.decisions([(f"w{i}", week - (i + 1) * H, PAIR, "valid", "sl_first") for i in range(25)])
    assert env.set("min_confidence_floor", 62, now=week) == 0, env.out


# ------------------------------------------------------------------ freeze when unhealthy
def _hours(n, start=NOW):
    return [start - (i + 1) * H + 17 * MS_PER_MINUTE for i in range(n)]


@pytest.mark.parametrize("hours,frozen", [(42, False), (43, True)])      # 42/168 = 25 % is allowed, more is not
def test_freeze_on_errored_decisions(env, hours, frozen):
    env.decisions([(f"e{i}", ts, PAIR, ["error", "skipped", "budget_blocked"][i % 3], None)
                   for i, ts in enumerate(_hours(hours))])
    assert env.set("min_confidence_floor", 60) == (2 if frozen else 0), env.out
    if frozen:
        assert f"frozen: {hours} of the last 168 h" in env.out


def test_data_warnings_count_only_when_they_name_the_decision_timeframe(env):
    tf = env.s.pairs[PAIR].decision_timeframe.value
    assert tf == "15m"
    env.decisions([(f"w1-{i}", ts, PAIR, "valid", None) for i, ts in enumerate(_hours(50))],
                  warnings=json.dumps(["1m: gaps (3 bars)", "4h: short history"]))
    assert env.set("min_confidence_floor", 60) == 0, env.out
    env2 = Env(env.root / "b")
    env2.seed()
    env2.decisions([(f"w15-{i}", ts, PAIR, "valid", None) for i, ts in enumerate(_hours(50))],
                   warnings=json.dumps([f"{tf}: gaps (2 bars)"]))
    assert env2.set("min_confidence_floor", 60) == 2 and "frozen" in env2.out


@pytest.mark.parametrize("case", ["killed", "suspend", "stopped", "stale"])
def test_freeze_on_service_trouble_and_heartbeat_gaps(env, case):
    if case == "killed":
        env.events([(ts, ["supervisor:engine", "supervisor:executor", "supervisor:ingest-mt5"][i % 3],
                     "killed" if i % 2 else "exited", "x", None) for i, ts in enumerate(_hours(45))]
                   + [(NOW - H, "supervisor:api", "exited", "x", None)])
    elif case == "suspend":
        env.events([(NOW - H, "supervisor:all", "system_suspend", "x", 46 * H),
                    (NOW - 60 * H, "supervisor:all", "stall", "x", 14 * MS_PER_MINUTE)])   # a short one: healthy
    elif case == "stopped":
        env.events([(NOW - 100 * H, "supervisor:all", "stopped", "", None),
                     (NOW - 50 * H, "supervisor:engine", "started", "pid 1", None)])
    else:
        con = sqlite3.connect(env.db())
        con.execute("INSERT OR REPLACE INTO collector_status(collector, state, updated_ms) "
                    "VALUES ('engine', 'live', ?)", (NOW - 50 * H,))
        con.commit()
        con.close()
    assert env.set("min_confidence_floor", 60) == 2
    assert "frozen:" in env.out


def test_short_gaps_and_other_services_do_not_freeze(env):
    env.events([(NOW - 20 * H, "supervisor:all", "stopped", "", None),
                (NOW - 20 * H + 10 * MS_PER_MINUTE, "supervisor:engine", "started", "pid 1", None)]
               + [(ts, "supervisor:api", "exited", "x", None) for ts in _hours(60)])
    con = sqlite3.connect(env.db())
    con.execute("INSERT OR REPLACE INTO collector_status(collector, state, updated_ms) VALUES ('engine','live',?)",
                (NOW - 5 * MS_PER_MINUTE,))
    con.commit()
    con.close()
    assert env.set("min_confidence_floor", 60) == 0, env.out


# ------------------------------------------------------------------ direction and bounds
def test_direction_rules_against_the_value_in_force(env):
    assert env.set("min_confidence_floor", 62) == 0
    later = NOW + 8 * MS_PER_DAY
    env.decisions([(f"x{i}", later - (i + 1) * H, PAIR, "valid", "tp1_first") for i in range(25)])
    assert env.set("min_confidence_floor", 58, now=later) == 2                 # raise only: 62 in force
    assert "may only be raised: it is 62 now" in env.out
    assert env.set("min_confidence_floor", 62, now=later) == 0, env.out        # the same value renews it
    later2 = later + MS_PER_DAY
    assert env.set("trigger.liquidity_atr", 0.35, now=later2) == 2             # down only: config 0.3
    assert "may only be lowered: it is 0.3 now" in env.out
    assert env.set("trigger.liquidity_atr", 0.3, now=later2) == 2              # equal to the config: no effect
    assert "no effect" in env.out
    assert env.set("trigger.liquidity_atr", 0.25, now=later2) == 0, env.out


def test_direction_uses_a_stricter_config(env):
    env.adaptive_overrides = {}
    strict = env.loader

    def loader(pair):
        s = strict(pair)
        return s.model_copy(update={"ai": s.ai.model_copy(update={"min_minutes_between_calls": 45})})
    env.loader = loader
    assert env.set("min_minutes_between_calls", 30) == 2 and "it is 45 now" in env.out


@pytest.mark.parametrize("key,value", [("min_confidence_floor", 81), ("min_confidence_floor", 54),
                                       ("min_minutes_between_calls", 61), ("max_idle_minutes", 241),
                                       ("review_floor_minutes", 31), ("trigger.weak_min", 4),
                                       ("trigger.liquidity_atr", 0.15), ("pair.ai_paused_until", "+8d")])
def test_out_of_bounds_is_refused(env, key, value):
    assert env.set(key, value) == 2
    assert "outside" in env.out or "at most 7 days" in env.out
    assert env.overlay() is None


def test_a_pause_in_the_past_is_refused(env):
    assert env.set("pair.ai_paused_until", "2026-09-01T00:00:00Z") == 2 and "in the future" in env.out


# ------------------------------------------------------------------ invalid requests (exit 3)
@pytest.mark.parametrize("argv", [
    ["--pair", PAIR, "set", "risk_per_trade_pct", "0.2", "--reason", "rrr", "--evidence-json", "{}"],
    ["--pair", PAIR, "set", "min_confidence_floor", "sixty", "--reason", "rrr", "--evidence-json", "{}"],
    ["--pair", PAIR, "set", "trigger.liquidity_atr", "nan", "--reason", "rrr", "--evidence-json", "{}"],
    ["--pair", PAIR, "set", "min_confidence_floor", "60", "--reason", "rrr", "--evidence-json", "{not json"],
    ["--pair", PAIR, "set", "min_confidence_floor", "60", "--reason", "rrr", "--evidence-json",
     "[" * 9 + "]" * 9],                                                  # deeper than MAX_EVIDENCE_DEPTH
    ["--pair", PAIR, "set", "min_confidence_floor", "60", "--reason", "rrr", "--evidence-json",
     "[" * 1500 + "]" * 1500],                                            # json.loads would hit the recursion limit
    ["--pair", PAIR, "set", "min_confidence_floor", "60", "--reason", "rrr"],
    ["--pair", PAIR, "set", "min_confidence_floor", "60", "--evidence-json", "{}"],
    ["--pair", PAIR, "set", "min_confidence_floor", "60", "--reason", "rrr", "--evidence-json", "{}",
     "--window-hours", "12"],
    ["--pair", PAIR, "set", "min_confidence_floor", "60", "--reason", "rrr", "--evidence-json", "{}",
     "--expires-days", "30"],
    ["--pair", PAIR, "set", "pair.ai_paused_until", "2026-09-26T10:00", "--reason", "rrr", "--evidence-json", "{}"],
    ["--pair", PAIR, "set", "playbook", "x", "--reason", "rrr", "--evidence-json", "{}"],
    ["--pair", PAIR, "--actor", "bad actor!", "set", "min_confidence_floor", "60", "--reason", "rrr",
     "--evidence-json", "{}"],
    ["set", "min_confidence_floor", "60", "--reason", "rrr", "--evidence-json", "{}"],
    ["--pair", "NOTAPAIR", "set", "min_confidence_floor", "60", "--reason", "rrr", "--evidence-json", "{}"],
    ["--pair", "../ETHUSDT", "set", "min_confidence_floor", "60", "--reason", "rrr", "--evidence-json", "{}"],
    ["--pair", "BTCUSDT/../ETHUSDT", "revert", "min_confidence_floor"],
    ["--pair", PAIR, "frobnicate"],
    ["--pair", PAIR, "revert", "nokey"],
])
def test_invalid_requests_exit_3_and_write_nothing(env, argv):
    before = _files(env.root)
    assert env.run(*argv) == 3, env.out
    assert env.out.startswith("invalid:")
    assert _files(env.root) == before


# ------------------------------------------------------------------ freeze file, disabled, lock
def test_tuning_freeze_file_and_disabled_refuse_but_revert_still_works(env):
    assert env.set("min_confidence_floor", 60) == 0
    (env.data / "TUNING_FREEZE").write_text("user", encoding="utf-8")
    assert env.set("max_idle_minutes", 180, now=NOW + MS_PER_DAY) == 2 and "frozen by the user" in env.out
    (env.data / "TUNING_FREEZE").unlink()
    env.adaptive_overrides = {"enabled": False}
    assert env.set("max_idle_minutes", 180, now=NOW + MS_PER_DAY) == 2 and "adaptive.enabled is false" in env.out
    (env.data / "TUNING_FREEZE").write_text("user", encoding="utf-8")
    assert env.run("--pair", PAIR, "revert", "min_confidence_floor", now=NOW + MS_PER_DAY) == 0, env.out


def test_a_busy_lock_refuses(env, monkeypatch):
    monkeypatch.setattr(tune, "LOCK_TIMEOUT_S", 0.2)
    lock = FileLock(ad.lock_path(env.s, PAIR))
    assert lock.acquire()
    try:
        assert env.set("min_confidence_floor", 60) == 2 and "busy" in env.out
    finally:
        lock.release()


# ------------------------------------------------------------------ revert
def test_revert_is_logged_and_is_not_a_change_of_the_day(env):
    assert env.set("min_confidence_floor", 60) == 0
    day2 = NOW + MS_PER_DAY
    assert env.run("--pair", PAIR, "revert", "min_confidence_floor", "--reason", "made it worse", now=day2) == 0
    assert env.out.startswith("reverted: BTCUSDT min_confidence_floor 60 -> config value")
    assert "min_confidence_floor" not in (env.overlay() or {})
    rows = env.sql("SELECT ts, old_value, new_value, reason, reverted_ms FROM tuning_changes ORDER BY id")
    assert rows == [(NOW, "55", "60", "review found it", day2), (day2, "60", None, "revert: made it worse", None)]
    assert ad.read_changes(env.s, PAIR)[-1]["action"] == "revert"
    assert ad.AdaptiveStore(env.s, PAIR).effective(day2 + 1).min_confidence == env.s.risk.min_confidence
    env.decisions([(f"r{i}", day2 - (i + 1) * H, PAIR, "valid", "tp1_first") for i in range(12)])
    assert env.set("max_idle_minutes", 180, now=day2 + H) == 0, env.out      # the revert used no daily slot
    assert env.run("--pair", PAIR, "revert", "tp_hint", now=day2 + 2 * H) == 0
    assert "nothing to revert" in env.out


def test_revert_on_the_same_day_and_again_after_a_revert(env):
    assert env.set("min_confidence_floor", 60) == 0
    assert env.run("--pair", PAIR, "revert", "min_confidence_floor", now=NOW + 1) == 0
    assert env.set("min_confidence_floor", 60, now=NOW + 2) == 2                 # the set still used the day
    assert "today already" in env.out and "cooldown" in env.out


# ------------------------------------------------------------------ dry run
def test_dry_run_writes_nothing(env):
    before = _files(env.root)
    assert env.run("--pair", PAIR, "--dry-run", "set", "min_confidence_floor", "60", "--reason", "rrr",
                   "--evidence-json", "{}") == 0
    assert env.out.startswith("dry-run: would apply: BTCUSDT min_confidence_floor 55 -> 60")
    assert env.set("min_confidence_floor", 90, "--dry-run") == 2                 # still refused
    after = _files(env.root)
    assert {k: v for k, v in after.items() if "locks" not in k} == before
    assert env.sql("SELECT count(*) FROM tuning_changes") == [(0,)]


# ------------------------------------------------------------------ playbook and hint
PLAYBOOK = """- Asia range: fade the first sweep of the high only after a 5m CHoCH back inside.
- In a 1h downtrend, longs need a 15m BOS plus a reclaimed VWAP.
- TP1 at the opposite range edge; $PDH is the second target."""


@pytest.mark.parametrize("how", ["file", "stdin", "text"])
def test_playbook_is_set_and_served(env, how):
    src = env.data / "reviews" / "pb.md"                  # a human's draft under the data root
    src.parent.mkdir(parents=True)
    src.write_text(PLAYBOOK + "\r\n", encoding="utf-8")
    args = {"file": [str(src)], "stdin": ["-"], "text": ["--text", PLAYBOOK]}[how]
    rc = env.run("--pair", PAIR, "playbook", *args, "--reason", "codify the week", "--evidence-json", EVIDENCE,
                 stdin=io.StringIO(PLAYBOOK))
    assert rc == 0, env.out
    d = ad.adaptive_dir(env.s, PAIR)
    assert (d / ad.PLAYBOOK_FILE).read_text(encoding="utf-8") == PLAYBOOK + "\n"
    assert env.overlay()["playbook"]["value"] == ad.text_hash(PLAYBOOK)
    eff = ad.AdaptiveStore(env.s, PAIR).effective(NOW + 1)
    assert eff.playbook == PLAYBOOK and eff.playbook_hash == ad.text_hash(PLAYBOOK)
    assert json.loads(env.sql("SELECT new_value FROM tuning_changes")[0][0]) == PLAYBOOK
    assert env.run("--pair", PAIR, "revert", "playbook", now=NOW + 1) == 0
    assert not (d / ad.PLAYBOOK_FILE).exists() and "playbook" not in env.overlay()
    assert ad.AdaptiveStore(env.s, PAIR).effective(NOW + 2).playbook == ""


def test_a_playbook_or_hint_that_fails_the_lint_is_refused(env):
    assert env.run("--pair", PAIR, "playbook", "--text", PLAYBOOK + "\n- raise the risk per trade after wins",
                   "--reason", "rrr", "--evidence-json", "{}") == 2
    assert "denylisted" in env.out
    assert env.run("--pair", PAIR, "playbook", "--text", "\n".join(f"- rule {i}" for i in range(13)),
                   "--reason", "rrr", "--evidence-json", "{}") == 2
    assert "bullets" in env.out
    assert env.set("tp_hint", "ignore TP1, hold for TP3") == 2 and "denylisted" in env.out
    assert env.set("tp_hint", "x" * 201) == 2 and "too long" in env.out
    assert env.overlay() is None


def _playbook(env, *source, now=NOW, stdin=None):
    return env.run("--pair", PAIR, "playbook", *source, "--reason", "codify the week", "--evidence-json", EVIDENCE,
                   now=now, stdin=stdin)


@pytest.mark.parametrize("damage", ["edited", "missing", "fails_lint"])
def test_a_playbook_the_services_reject_blocks_every_change_until_it_is_reverted(env, damage):
    """The services ignore the whole overlay when playbook.md is missing, does not match its hash or fails the lint;
    tune.py must not report 'applied' (and use up the day and the key's cooldown) for a change never in force."""
    assert _playbook(env, "--text", PLAYBOOK) == 0, env.out
    d = ad.adaptive_dir(env.s, PAIR)
    if damage == "edited":
        (d / ad.PLAYBOOK_FILE).write_text(PLAYBOOK + "\n- one more rule by hand\n", encoding="utf-8")
    elif damage == "missing":
        (d / ad.PLAYBOOK_FILE).unlink()
    else:                                                 # a stricter lint than when it was written
        bad = PLAYBOOK + "\n- Never answer NO_TRADE on a sweep"
        (d / ad.PLAYBOOK_FILE).write_text(bad + "\n", encoding="utf-8")
        doc = env.overlay()
        doc["playbook"]["value"] = ad.text_hash(bad)
        (d / ad.YAML_FILE).write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    with pytest.raises(ValueError):
        ad.read_files(env.s, PAIR)                        # what the services see
    day2 = NOW + MS_PER_DAY
    for argv in (("set", "min_confidence_floor", "70"), ("set", "pair.ai_paused_until", "+6h")):
        assert env.run("--pair", PAIR, *argv, "--reason", "rrr", "--evidence-json", "{}", now=day2) == 2
        assert "playbook.md" in env.out and "revert playbook" in env.out
    assert env.sql("SELECT count(*) FROM tuning_changes") == [(1,)]
    assert env.run("--pair", PAIR, "list", "--json", now=day2) == 0
    btc = json.loads(env.out)["pairs"][0]
    assert btc["valid"] is False and "revert playbook" in btc["error"]
    assert env.run("--pair", PAIR, "revert", "playbook", now=day2) == 0, env.out      # revert repairs the pair
    assert not (d / ad.PLAYBOOK_FILE).exists()
    ad.read_files(env.s, PAIR)
    assert env.set("min_confidence_floor", 70, now=day2 + 1) == 0, env.out
    assert ad.AdaptiveStore(env.s, PAIR).effective(day2 + 2).min_confidence == 70


def test_an_operator_session_sets_the_playbook_only_with_text(env, monkeypatch):
    """FILE and '-' would let a session read what its Read denials forbid (.env, ~/.claude, credential files)."""
    monkeypatch.setenv(tune.SESSION_ENV, "1")
    src = env.data / "reviews" / "pb.md"
    src.parent.mkdir(parents=True)
    src.write_text(PLAYBOOK, encoding="utf-8")
    before = _files(env.root)
    assert _playbook(env, str(src)) == 3 and "sessions use --text" in env.out
    assert _playbook(env, "-", stdin=io.StringIO(PLAYBOOK)) == 3 and "sessions use --text" in env.out
    assert _files(env.root) == before
    assert _playbook(env, "--text", PLAYBOOK) == 0, env.out


MARK = "PRIVATE-MARKER-7f3a"


@pytest.mark.parametrize("where,why", [
    ("data/.env", "secrets file"), ("data/reviews/.env.local", "secrets file"),
    ("data/reviews/Claude_Credentials.json", "secrets file"), ("home/.claude/.credentials.json", "secrets file"),
    ("home/.claude/settings.json", "only a file under"), ("outside/pb.md", "only a file under"),
    ("data/reviews/../../outside/pb.md", "only a file under"), ("//server/share/pb.md", "network"),
    ("\\\\server\\share\\pb.md", "network"), ("data/reviews", "not a regular file"),
    ("data/reviews/missing.md", "not found")])
def test_the_file_form_reads_only_a_plain_draft_under_the_data_root_or_the_checkout(env, where, why):
    """Refused before anything is opened (the secrets-file names and the profile paths are never created or touched
    here), exit 3, and nothing of a file's content is echoed."""
    for p in (env.data / "reviews" / "Claude_Credentials.json", env.root / "outside" / "pb.md"):
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"- {MARK} one\n- two", encoding="utf-8")
    if where.startswith("home/"):
        source = str(Path.home() / where[len("home/"):])
    elif where.startswith("/") or where.startswith("\\"):
        source = where
    else:
        source = str(env.root / where)
    before = _files(env.root)
    assert _playbook(env, source) == 3, env.out
    assert env.out.startswith("invalid:") and why in env.out and MARK not in env.out
    assert _files(env.root) == before


def test_the_file_form_accepts_a_file_in_the_checkout(env):
    assert _playbook(env, str(tune.CHECKOUT / "docs" / "learning_loop.md")) == 2       # read, then linted
    assert env.out.startswith("refused:") and "too long" in env.out


def _link_dir(link: Path, target: Path) -> None:
    if sys.platform == "win32":
        import _winapi
        _winapi.CreateJunction(str(target), str(link))       # no privilege needed, unlike a symlink
    else:
        os.symlink(target, link, target_is_directory=True)


@pytest.mark.parametrize("to", ["outside", "inside"])
def test_the_file_form_refuses_a_path_through_a_junction(env, to):
    target = env.root / "elsewhere" if to == "outside" else env.data / "drafts"
    target.mkdir(parents=True)
    (target / "pb.md").write_text(PLAYBOOK, encoding="utf-8")
    (env.data / "reviews").mkdir(parents=True)
    _link_dir(env.data / "reviews" / "link", target)
    assert _playbook(env, str(env.data / "reviews" / "link" / "pb.md")) == 3, env.out
    assert "symlink or junction" in env.out and env.overlay() is None


def test_the_file_form_refuses_a_symlinked_file(env):
    target = env.data / "drafts" / "pb.md"
    target.parent.mkdir(parents=True)
    target.write_text(PLAYBOOK, encoding="utf-8")
    link = env.data / "reviews" / "pb.md"
    link.parent.mkdir(parents=True)
    try:
        os.symlink(target, link)
    except OSError:
        pytest.skip("creating a symlink needs a privilege here")
    assert _playbook(env, str(link)) == 3 and "symlink or junction" in env.out


SECRET = "Zq7-secret-VALUE-for-tests"
BOT_TOKEN = "123456789:" + "A1b2C3d4E5" * 4                  # a Telegram bot token's shape
ANTHROPIC_KEY = "sk-ant-" + "x7Y" * 10                        # an API key's shape


@pytest.mark.parametrize("case", ["text", "file", "stdin", "hint", "reason", "evidence", "evidence_escaped",
                                  "review_id", "actor", "revert_reason", "key_shape", "bot_token", "dotenv_value"])
def test_a_secret_or_a_key_is_never_stored_or_echoed(env, monkeypatch, tmp_path, case):
    """Every free text tune.py stores ends up in adaptive.yaml / playbook.md / changes.jsonl / tuning_changes, the
    trader prompt and the dashboard: a configured secret's value (environment or .env) or a key / token shape the
    log redactor masks is refused (exit 3) and not shown."""
    monkeypatch.setenv("TS_TEST_API_PASSWORD", SECRET)            # a *PASSWORD name: one of secret_env_names()
    hidden = SECRET
    draft = env.data / "reviews" / "notes.md"
    draft.parent.mkdir(parents=True)
    draft.write_text(f"- MT5_PASSWORD={SECRET}\n- TELEGRAM_BOT_TOKEN={BOT_TOKEN}", encoding="utf-8")
    ok = ("--reason", "codify the week", "--evidence-json", EVIDENCE)
    if case == "text":
        argv = ("playbook", "--text", PLAYBOOK + f"\n- note {SECRET}", *ok)
    elif case == "file":
        argv = ("playbook", str(draft), *ok)
    elif case == "stdin":
        argv = ("playbook", "-", *ok)
    elif case == "hint":
        argv = ("set", "tp_hint", f"TP1 at {SECRET}", *ok)
    elif case == "reason":
        argv = ("set", "min_confidence_floor", "60", "--reason", f"because {SECRET}", "--evidence-json", "{}")
    elif case == "evidence":
        argv = ("set", "min_confidence_floor", "60", "--reason", "rrr", "--evidence-json", json.dumps({"n": SECRET}))
    elif case == "evidence_escaped":                              # the raw argument holds Z…, the value SECRET
        raw = '{"n": "' + chr(92) + "u%04x" % ord(SECRET[0]) + SECRET[1:] + '"}'
        assert SECRET not in raw and json.loads(raw)["n"] == SECRET
        argv = ("set", "min_confidence_floor", "60", "--reason", "rrr", "--evidence-json", raw)
    elif case == "review_id":
        argv = ("set", "min_confidence_floor", "60", *ok, "--review-id", SECRET)
    elif case == "actor":
        argv = ("--actor", SECRET, "set", "min_confidence_floor", "60", *ok)
    elif case == "revert_reason":
        assert env.set("min_confidence_floor", 60) == 0
        argv = ("revert", "min_confidence_floor", "--reason", f"undo {SECRET}")
    elif case == "key_shape":
        hidden = ANTHROPIC_KEY
        argv = ("playbook", "--text", PLAYBOOK + f"\n- key {ANTHROPIC_KEY}", *ok)
    elif case == "bot_token":
        hidden = BOT_TOKEN
        argv = ("set", "tp_hint", f"TP1 at the swing {BOT_TOKEN}", *ok)
    else:                                                         # a value only in .env, not in the environment
        from tradingsystem.ai.providers import base
        hidden = "abcdEFGH1234zzzz"
        env_file = tmp_path / "test_env_file"
        env_file.write_text(f"TELEGRAM_BOT_TOKEN={hidden}\n", encoding="utf-8")
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        monkeypatch.setattr(base, "ENV_FILE", env_file)
        argv = ("playbook", "--text", PLAYBOOK + f"\n- note {hidden}", *ok)
    before = _files(env.root)
    assert env.run("--pair", PAIR, *argv, stdin=io.StringIO(draft.read_text(encoding="utf-8"))) == 3, env.out
    assert env.out.startswith("invalid:") and "secret" in env.out and hidden not in env.out
    assert _files(env.root) == before
    assert not any(hidden in r for r in map(json.dumps, ad.read_changes(env.s, PAIR)))


def test_list_shows_a_playbook_change_as_hash_and_length_only(env):
    assert _playbook(env, "--text", PLAYBOOK) == 0, env.out
    assert ad.read_changes(env.s, PAIR)[-1]["text"] == PLAYBOOK        # the history keeps the text ...
    assert env.run("--pair", PAIR, "list", "--json", now=NOW + H) == 0
    assert "Asia range" not in env.out                                  # ... `list` (run by the session) never
    change = json.loads(env.out)["pairs"][0]["recent_changes"][-1]
    assert "text" not in change and change["new"] == {"hash": ad.text_hash(PLAYBOOK), "chars": len(PLAYBOOK)}
    assert env.run("--pair", PAIR, "list", now=NOW + H) == 0
    assert "Asia range" not in env.out and ad.text_hash(PLAYBOOK) in env.out


def test_a_line_separator_in_a_reason_keeps_the_changes_line_whole(env):
    reason = "fewer\N{LINE SEPARATOR}losers"
    assert env.run("--pair", PAIR, "set", "min_confidence_floor", "60", "--reason", reason, "--evidence-json",
                   "{}") == 0, env.out
    line = ad.read_changes(env.s, PAIR)[-1]
    assert (line["action"], line["reason"]) == ("set", reason)
    assert (ad.adaptive_dir(env.s, PAIR) / ad.CHANGES_FILE).read_bytes().isascii()
    assert env.set("tp_hint", "TP1 at the prior high\N{LINE SEPARATOR}trail the rest", now=NOW + MS_PER_DAY) == 2
    assert "separators" in env.out


# ------------------------------------------------------------------ the path allow-list
def _files(root: Path) -> dict[str, tuple[int, int]]:
    return {p.relative_to(root).as_posix(): (p.stat().st_mtime_ns, p.stat().st_size)
            for p in root.rglob("*") if p.is_file()}


def test_writes_stay_inside_the_pairs_adaptive_dir_and_its_app_db(env):
    env.seed(OTHER)
    src = env.data / "reviews" / "pb.md"
    src.parent.mkdir(parents=True)
    src.write_text(PLAYBOOK, encoding="utf-8")
    before = _files(env.root)
    assert env.set("min_confidence_floor", 60) == 0
    assert env.run("--pair", PAIR, "revert", "min_confidence_floor", now=NOW + 1) == 0
    assert env.run("--pair", PAIR, "playbook", str(src), "--reason", "rrr", "--evidence-json", "{}",
                   now=NOW + MS_PER_DAY) == 0
    after = _files(env.root)
    changed = {k for k in after if before.get(k) != after[k]} | (set(before) - set(after))
    allowed = ("data/adaptive/BTCUSDT/", "data/instances/BTCUSDT/app.db", "data/shared/locks/adaptive_BTCUSDT.lock")
    assert changed and all(k.startswith(allowed) for k in changed), changed
    assert env.sql("SELECT count(*) FROM tuning_changes", pair=OTHER) == [(0,)]


def test_the_writers_refuse_any_other_path(env):
    t = tune.target(PAIR, env.loader)
    outside = [env.data / "KILL_SWITCH", env.data / "adaptive" / OTHER / "adaptive.yaml", env.data / "adaptive",
               ad.adaptive_dir(env.s, PAIR) / ".." / OTHER / "adaptive.yaml", env.root / "config.local.yaml"]
    for p in outside:
        with pytest.raises(tune.PathRefused):
            tune._inside(t, p)
    with pytest.raises(tune.PathRefused):
        tune._write(t, "../ETHUSDT/adaptive.yaml", "x")
    with pytest.raises(tune.PathRefused):
        tune._write(t, "../../KILL_SWITCH", "x")
    assert not (env.data / "KILL_SWITCH").exists() and not (env.data / "adaptive" / "ETHUSDT").exists()
    wrong = tune.Target(PAIR, t.s, t.dir, env.db(OTHER))
    with pytest.raises(tune.PathRefused):
        tune._db(wrong)
    assert tune._inside(t, t.dir / ad.YAML_FILE) == (t.dir / ad.YAML_FILE).resolve()


def test_no_app_db_means_no_change_and_none_is_created(tmp_path):
    env = Env(tmp_path)
    assert env.set("max_idle_minutes", 180) == 2 and "no decision history" in env.out
    assert not env.db().exists()


# ------------------------------------------------------------------ list
def test_list_shows_values_policy_and_changes(env):
    assert env.set("min_confidence_floor", 60) == 0
    assert env.run("--pair", PAIR, "list", now=NOW + H) == 0
    assert "## BTCUSDT" in env.out and "min_confidence_floor" in env.out and "overlay 60" in env.out
    assert "25 resolved virtual outcomes" in env.out and "changes today 1/1" in env.out
    assert "cooldown: min_confidence_floor until" in env.out
    assert env.run("list", "--json", now=NOW + H) == 0
    doc = json.loads(env.out)
    btc = next(p for p in doc["pairs"] if p["pair"] == PAIR)
    assert btc["effective"]["min_confidence"] == 60 and btc["policy"]["changes_today"] == 1
    assert {p["pair"] for p in doc["pairs"]} >= {PAIR}


def test_help_exits_0(env):
    assert env.run("--help") == 0


def test_the_script_runs_on_the_config_named_by_tradingsystem_config(tmp_path):
    """The operator session runs it as a script: its own src on sys.path, TRADINGSYSTEM_CONFIG honoured (the scratch
    live run), UTF-8 output, exit codes from main()."""
    raw = yaml.safe_load((ROOT / "config" / "config.yaml").read_text(encoding="utf-8"))
    raw.setdefault("paths", {})["data_dir"] = str(tmp_path / "data")
    raw["paths"]["logs_dir"] = str(tmp_path / "logs")
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(yaml.safe_dump(raw), encoding="utf-8")
    DecisionStore(tmp_path / "data" / "instances" / PAIR / "app.db").close()
    env = {**__import__("os").environ, "TRADINGSYSTEM_CONFIG": str(cfg), "TS_NOTIFY_DISABLE": "1"}
    env.pop(INSTANCE_ENV, None)

    def cli(*args):
        return subprocess.run([sys.executable, str(ROOT / "tools" / "tune.py"), *args], capture_output=True,
                              env=env, cwd=tmp_path, timeout=60)

    r = cli("--pair", PAIR, "set", "tp_hint", "TP1 → the prior swing", "--reason", "script check",
            "--evidence-json", "{}")
    assert r.returncode == 2, r
    assert r.stdout.decode("utf-8").startswith('refused: BTCUSDT tp_hint "TP1 → the prior swing": 0 resolved')
    r = cli("--pair", PAIR, "set", "nokey", "1", "--reason", "script check", "--evidence-json", "{}")
    assert r.returncode == 3 and r.stdout.startswith(b"invalid:")
    r = cli("list", "--json")
    assert r.returncode == 0, r
    doc = json.loads(r.stdout.decode("utf-8"))
    assert Path(doc["pairs"][0]["dir"]).parent == tmp_path / "data" / "adaptive"
    assert not (tmp_path / "data" / "adaptive").exists()                     # refused / listed: nothing written
