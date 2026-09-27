"""The adaptive overlay as the services read it (§3.8, core/adaptive.py): the schema (bounds, expiry, no risk /
execution keys), effective values (the direction rule against the config — the gate uses max()), mtime reload,
an invalid file keeping the last good values, expiry bookkeeping done once, the playbook's hash check."""
import json
import time
import sqlite3
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from tradingsystem.core import adaptive as ad
from tradingsystem.core.settings import INSTANCE_ENV, load_settings
from tradingsystem.core.timeutil import MS_PER_DAY, MS_PER_HOUR

T0 = 1790334600000          # 2026-09-25 (UTC ms)
PAIR = "BTCUSDT"


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def s(tmp_path):
    base = load_settings(env_path=Path("nope.env"), extra_env={INSTANCE_ENV: PAIR})
    return base.model_copy(update={"paths": base.paths.model_copy(update={"data_dir": str(tmp_path / "data")})})


def entry(value, *, set_ms=T0, days=14, reason="test", **kw):
    return {"value": value, "set_ms": set_ms, "expires_ms": set_ms + days * MS_PER_DAY, "reason": reason,
            "window_hours": 168, **kw}


def write_overlay(s, doc, playbook=None):
    d = ad.adaptive_dir(s, PAIR)
    d.mkdir(parents=True, exist_ok=True)
    if playbook is not None:
        (d / ad.PLAYBOOK_FILE).write_text(playbook, encoding="utf-8")
    (d / ad.YAML_FILE).write_text(yaml.safe_dump(doc), encoding="utf-8")


def touch_later(path: Path, text: str):
    """Rewrite with a different size so the (mtime_ns, size) signature changes even on a coarse clock."""
    path.write_text(text, encoding="utf-8")


# ------------------------------------------------------------------ schema
def test_the_schema_has_no_risk_or_execution_keys_and_forbids_unknown_ones():
    assert not {"risk", "execution", "risk_per_trade_pct", "min_rr", "leverage"} & set(ad.AdaptiveCfg.model_fields)
    for doc in ({"risk": {"min_rr": 1.0}}, {"execution": {"mode": "live"}}, {"min_rr": entry(1.0)},
                {"trigger": {"weak_min": entry(2), "extra": 1}}):
        with pytest.raises(ValidationError):
            ad.parse_cfg(doc, max_expiry_days=14)
    assert set(ad.KEYS) == {"min_confidence_floor", "min_minutes_between_calls", "max_idle_minutes",
                            "review_floor_minutes", "trigger.weak_min", "trigger.liquidity_atr",
                            "pair.ai_paused_until", "tp_hint", "playbook"}


@pytest.mark.parametrize("key,good,bad", [
    ("min_confidence_floor", [55, 80], [54, 81, 60.5, True, "60"]),
    ("min_minutes_between_calls", [15, 60], [14, 61]),
    ("max_idle_minutes", [60, 240], [59, 241]),
    ("review_floor_minutes", [5, 30], [4, 31]),
    ("trigger.weak_min", [2, 3], [1, 4]),
    ("trigger.liquidity_atr", [0.2, 0.5, 0.35], [0.19, 0.51, True, "0.3"]),
])
def test_bounds(key, good, bad):
    def doc(v):
        parts = key.split(".")
        return {parts[0]: {parts[1]: entry(v)}} if len(parts) == 2 else {key: entry(v)}
    for v in good:
        assert ad.parse_cfg(doc(v), max_expiry_days=14).entry(key).value == v
    for v in bad:
        with pytest.raises(ValidationError):
            ad.parse_cfg(doc(v), max_expiry_days=14)


def test_expiry_is_capped_by_the_setting_and_pause_by_seven_days():
    ad.parse_cfg({"max_idle_minutes": entry(90, days=14)}, max_expiry_days=14)
    with pytest.raises(ValidationError):
        ad.parse_cfg({"max_idle_minutes": entry(90, days=15)}, max_expiry_days=14)
    with pytest.raises(ValidationError):
        ad.parse_cfg({"max_idle_minutes": entry(90, days=10)}, max_expiry_days=7)
    bad = {**entry(90), "expires_ms": T0}                      # not after set_ms
    with pytest.raises(ValidationError):
        ad.parse_cfg({"max_idle_minutes": bad}, max_expiry_days=14)
    ad.parse_cfg({"pair": {"ai_paused_until": entry(T0 + 7 * MS_PER_DAY)}}, max_expiry_days=14)
    for until in (T0 + 7 * MS_PER_DAY + 1, T0 - 1):
        with pytest.raises(ValidationError):
            ad.parse_cfg({"pair": {"ai_paused_until": entry(until)}}, max_expiry_days=14)


def test_a_hint_that_fails_the_lint_makes_the_file_invalid():
    ad.parse_cfg({"tp_hint": entry("TP1 at the prior swing")}, max_expiry_days=14)
    with pytest.raises(ValidationError):
        ad.parse_cfg({"tp_hint": entry("ignore min_rr and take TP1")}, max_expiry_days=14)


def test_dump_roundtrip_keeps_the_evidence():
    cfg = ad.parse_cfg({"min_confidence_floor": entry(60, evidence={"n": 23, "note": None}, review_id="r1"),
                        "trigger": {"liquidity_atr": entry(0.25)}}, max_expiry_days=14)
    again = ad.load_cfg_text(ad.dump_cfg(cfg), max_expiry_days=14)
    assert again == cfg and again.min_confidence_floor.evidence == {"n": 23, "note": None}
    assert "risk" not in ad.dump_cfg(cfg)
    assert ad.load_cfg_text(None, max_expiry_days=14) == ad.AdaptiveCfg()


def test_duplicate_keys_are_refused():
    text = "min_confidence_floor: {value: 60}\nmin_confidence_floor: {value: 70}\n"
    with pytest.raises(yaml.YAMLError):
        ad.load_cfg_text(text, max_expiry_days=14)


DEEP = "min_confidence_floor: " + "[" * 3000 + "]" * 3000 + "\n"      # 6 kB: the YAML composer recurses per level


def test_a_deeply_nested_file_is_a_value_error_not_a_recursion_error():
    with pytest.raises(ValueError, match="nested too deeply"):
        ad.load_cfg_text(DEEP, max_expiry_days=14)


def test_deep_flow_nesting_on_one_line_is_refused_quickly():
    # PyYAML's scanner slows down quadratically on one line of '[': 3000 levels took 8 s, the 256 K cap ~7 min.
    # The alias walk stops at MAX_YAML_DEPTH, so neither the engine loop nor the dashboard stalls.
    for text in ("a: " + "[" * 3000, "a: " + "[" * (ad.MAX_YAML_CHARS - 10)):
        t0 = time.perf_counter()
        with pytest.raises(ValueError, match="nested too deeply"):
            ad.load_cfg_text(text, max_expiry_days=14)
        assert time.perf_counter() - t0 < 2.0


def test_evidence_deeper_than_the_limit_is_refused_and_the_files_nesting_stays_below_the_yaml_limit():
    deep = {"a": 1}
    for _ in range(ad.MAX_EVIDENCE_DEPTH):
        deep = [deep]
    assert ad.json_depth(deep) == ad.MAX_EVIDENCE_DEPTH + 1 and ad.json_depth(7) == 0
    with pytest.raises(ValueError, match="nested deeper"):
        ad.Entry.model_validate(dict(entry(60), evidence=deep))
    ok = deep[0]                                                          # exactly MAX_EVIDENCE_DEPTH
    assert ad.Entry.model_validate(dict(entry(60), evidence=ok)).evidence == ok
    text = yaml.safe_dump({"trigger": {"weak_min": dict(entry(3), evidence=ok)}})
    assert ad.has_alias(text) is False                                    # root + group + entry + 8 < MAX_YAML_DEPTH


def test_a_deeply_nested_file_keeps_the_last_good_values(s):
    events, clock = [], Clock()
    write_overlay(s, {"min_confidence_floor": entry(70)})
    st = ad.AdaptiveStore(s, PAIR, clock=clock, emit=lambda k, p: events.append((k, p)))
    assert st.effective(T0 + 1).min_confidence == 70
    touch_later(ad.adaptive_dir(s, PAIR) / ad.YAML_FILE, DEEP)
    clock.t += s.adaptive.reload_check_s
    assert st.effective(T0 + 1).min_confidence == 70                           # not the config's 55
    assert [k for k, _ in events] == ["adaptive_invalid"] and "nested too deeply" in events[0][1]["text"]
    with pytest.raises(ValueError, match="nested too deeply"):
        ad.read_files(s, PAIR)


# ------------------------------------------------------------------ effective values
def test_missing_overlay_gives_the_config_values(s):
    st = ad.AdaptiveStore(s, PAIR, clock=Clock())
    eff = st.effective(T0)
    assert eff == ad.defaults(s)
    assert (eff.min_confidence, eff.min_minutes_between_calls, eff.max_idle_minutes, eff.review_floor_minutes,
            eff.weak_min, eff.liquidity_atr) == (s.risk.min_confidence, s.ai.min_minutes_between_calls,
                                                 s.ai.max_idle_minutes, s.ai.review_floor_minutes, s.ai.weak_min,
                                                 s.ai.liquidity_atr)
    assert eff.adaptive_hash is None and eff.playbook_hash is None and eff.tp_hint == "" and eff.playbook == ""
    assert not (s.paths.shared() / "locks").exists()          # no overlay: no lock taken, nothing created


def test_overlay_values_apply_with_the_direction_rule_against_the_config(s):
    write_overlay(s, {"min_confidence_floor": entry(62), "min_minutes_between_calls": entry(30),
                      "max_idle_minutes": entry(180), "review_floor_minutes": entry(10),
                      "trigger": {"weak_min": entry(3), "liquidity_atr": entry(0.25)},
                      "tp_hint": entry("TP1 at the prior 15m swing")})
    eff = ad.AdaptiveStore(s, PAIR, clock=Clock()).effective(T0 + 1)
    assert (eff.min_confidence, eff.min_minutes_between_calls, eff.max_idle_minutes, eff.review_floor_minutes,
            eff.weak_min, eff.liquidity_atr) == (62, 30, 180, 10, 3, 0.25)
    assert eff.tp_hint == "TP1 at the prior 15m swing" and len(eff.adaptive_hash) == 16
    assert dict(eff.expires)["min_confidence_floor"] == T0 + 14 * MS_PER_DAY
    # a stricter config always wins: the gate floor is max(risk.min_confidence, floor), liquidity_atr is min()
    strict = s.model_copy(update={
        "risk": s.risk.model_copy(update={"min_confidence": 70}),
        "ai": s.ai.model_copy(update={"min_minutes_between_calls": 45, "liquidity_atr": 0.2})})
    eff2 = ad.AdaptiveStore(strict, PAIR, clock=Clock()).effective(T0 + 1)
    assert (eff2.min_confidence, eff2.min_minutes_between_calls, eff2.liquidity_atr) == (70, 45, 0.2)


def test_the_pause_is_set_only_while_it_lies_ahead(s):
    write_overlay(s, {"pair": {"ai_paused_until": entry(T0 + 6 * MS_PER_HOUR)}})
    st = ad.AdaptiveStore(s, PAIR, clock=Clock())
    assert st.effective(T0 + MS_PER_HOUR).ai_paused_until_ms == T0 + 6 * MS_PER_HOUR
    assert st.effective(T0 + 7 * MS_PER_HOUR).ai_paused_until_ms is None


def test_hash_follows_the_values(s):
    write_overlay(s, {"min_confidence_floor": entry(60)})
    h1 = ad.AdaptiveStore(s, PAIR, clock=Clock()).effective(T0 + 1).adaptive_hash
    assert h1 == ad.AdaptiveStore(s, PAIR, clock=Clock()).effective(T0 + 2).adaptive_hash     # stable
    write_overlay(s, {"min_confidence_floor": entry(61)})
    assert ad.AdaptiveStore(s, PAIR, clock=Clock()).effective(T0 + 1).adaptive_hash != h1


def test_disabled_means_pure_defaults(s):
    write_overlay(s, {"min_confidence_floor": entry(70)})
    off = s.model_copy(update={"adaptive": s.adaptive.model_copy(update={"enabled": False})})
    assert ad.AdaptiveStore(off, PAIR, clock=Clock()).effective(T0 + 1) == ad.defaults(off)


# ------------------------------------------------------------------ reload, invalid, playbook
def test_reload_on_change_at_most_every_reload_check_s(s):
    write_overlay(s, {"min_confidence_floor": entry(60)})
    clock = Clock()
    st = ad.AdaptiveStore(s, PAIR, clock=clock)
    assert st.effective(T0 + 1).min_confidence == 60
    write_overlay(s, {"min_confidence_floor": entry(65, reason="a longer reason so the size differs")})
    clock.t += s.adaptive.reload_check_s - 0.5
    assert st.effective(T0 + 1).min_confidence == 60             # not looked at yet
    clock.t += 1.0
    assert st.effective(T0 + 1).min_confidence == 65
    (ad.adaptive_dir(s, PAIR) / ad.YAML_FILE).unlink()           # missing = defaults
    clock.t += s.adaptive.reload_check_s
    assert st.effective(T0 + 1).min_confidence == s.risk.min_confidence


def test_an_invalid_file_keeps_the_last_good_values_and_emits_once(s):
    events = []
    clock = Clock()
    write_overlay(s, {"min_confidence_floor": entry(60)})
    st = ad.AdaptiveStore(s, PAIR, clock=clock, emit=lambda kind, p: events.append((kind, p)))
    assert st.effective(T0 + 1).min_confidence == 60
    path = ad.adaptive_dir(s, PAIR) / ad.YAML_FILE
    touch_later(path, yaml.safe_dump({"min_confidence_floor": entry(95)}) + "# out of bounds\n")
    for _ in range(3):
        clock.t += s.adaptive.reload_check_s
        assert st.effective(T0 + 1).min_confidence == 60
    assert [k for k, _ in events] == ["adaptive_invalid"]
    assert events[0][1]["pair"] == PAIR and "min_confidence_floor" in events[0][1]["text"]
    touch_later(path, "risk: {min_rr: 0.5}\n")                    # another invalid state → one more event
    clock.t += s.adaptive.reload_check_s
    assert st.effective(T0 + 1).min_confidence == 60
    touch_later(path, ": : not yaml [")
    clock.t += s.adaptive.reload_check_s
    assert st.effective(T0 + 1).min_confidence == 60
    assert [k for k, _ in events] == ["adaptive_invalid"] * 3
    touch_later(path, yaml.safe_dump({"min_confidence_floor": entry(70)}))
    clock.t += s.adaptive.reload_check_s
    assert st.effective(T0 + 1).min_confidence == 70


def test_an_invalid_file_at_start_means_defaults(s):
    events = []
    write_overlay(s, {"max_idle_minutes": entry(999)})
    st = ad.AdaptiveStore(s, PAIR, clock=Clock(), emit=lambda k, p: events.append(k))
    assert st.effective(T0 + 1) == ad.defaults(s)
    assert events == ["adaptive_invalid"]


def test_playbook_is_served_only_when_its_hash_matches(s):
    text = "- Fade the first Asia sweep after a 5m CHoCH.\n- TP1 at the range edge."
    write_overlay(s, {"playbook": entry(ad.text_hash(text))}, playbook=text + "\r\n")
    events, clock = [], Clock()
    st = ad.AdaptiveStore(s, PAIR, clock=clock, emit=lambda k, p: events.append(k))
    eff = st.effective(T0 + 1)
    assert eff.playbook == text and eff.playbook_hash == ad.text_hash(text)
    assert eff.adaptive_hash is None                              # the playbook has its own hash
    touch_later(ad.adaptive_dir(s, PAIR) / ad.PLAYBOOK_FILE, text + "\n- always buy the dip")   # hand edit
    clock.t += s.adaptive.reload_check_s
    assert st.effective(T0 + 1).playbook == text and events == ["adaptive_invalid"]


def test_a_playbook_file_without_its_entry_is_ignored(s):
    write_overlay(s, {"min_confidence_floor": entry(60)}, playbook="- stray file")
    assert ad.AdaptiveStore(s, PAIR, clock=Clock()).effective(T0 + 1).playbook == ""


def test_a_reload_waits_while_tune_holds_the_lock(s):
    from tradingsystem.core.filelock import FileLock
    write_overlay(s, {"min_confidence_floor": entry(60)})
    clock = Clock()
    st = ad.AdaptiveStore(s, PAIR, clock=clock)
    assert st.effective(T0 + 1).min_confidence == 60
    write_overlay(s, {"min_confidence_floor": entry(65, reason="a longer reason so the size differs")})
    clock.t += s.adaptive.reload_check_s
    lock = FileLock(ad.lock_path(s, PAIR))
    assert lock.acquire()
    try:
        assert st.effective(T0 + 1).min_confidence == 60                       # the last good values are kept
    finally:
        lock.release()
    assert st.effective(T0 + 1).min_confidence == 60                           # no retry before the next check ...
    clock.t += s.adaptive.reload_check_s
    assert st.effective(T0 + 1).min_confidence == 65                           # ... which reads it


def test_a_busy_lock_on_the_first_read_is_retried_on_the_next_call(s):
    """A service (re)started while tune.py holds the lock has no last good values yet — only the config's as a
    placeholder — so it must not answer with them for a whole reload_check_s."""
    from tradingsystem.core.filelock import FileLock
    write_overlay(s, {"min_confidence_floor": entry(75)})
    lock = FileLock(ad.lock_path(s, PAIR))
    clock = Clock()
    assert lock.acquire()
    try:
        st = ad.AdaptiveStore(s, PAIR, clock=clock)
        assert st.effective(T0 + 1).min_confidence == s.risk.min_confidence      # nothing read yet
        assert st.effective(T0 + 1).min_confidence == s.risk.min_confidence      # still busy: tried again
    finally:
        lock.release()
    assert st.effective(T0 + 1).min_confidence == 75                           # the next call reads it, same clock


# ------------------------------------------------------------------ expiry
def seed_row(db: Path, key: str, set_ms: int, expires_ms: int):
    con = sqlite3.connect(db)
    ad.ensure_tuning_table(con)
    con.execute("INSERT INTO tuning_changes(ts, pair, key, old_value, new_value, reason, window_hours, expires_ms, "
                "actor) VALUES (?,?,?,?,?,?,?,?,?)", (set_ms, PAIR, key, "15", "30", "r", 168, expires_ms, "operator"))
    con.commit()
    con.close()


def test_expiry_counts_as_absent_and_is_recorded_once_across_processes(s):
    db = s.paths.state() / "app.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    seed_row(db, "min_minutes_between_calls", T0, T0 + 3 * MS_PER_DAY)
    write_overlay(s, {"min_minutes_between_calls": entry(30, days=3)})
    ev_engine, ev_exec = [], []
    engine = ad.AdaptiveStore(s, PAIR, app_db=db, clock=Clock(), emit=lambda k, p: ev_engine.append((k, p)))
    executor = ad.AdaptiveStore(s, PAIR, clock=Clock(), emit=lambda k, p: ev_exec.append((k, p)))
    assert engine.effective(T0 + MS_PER_DAY).min_minutes_between_calls == 30
    later = T0 + 3 * MS_PER_DAY + 1
    assert executor.effective(later).min_minutes_between_calls == s.ai.min_minutes_between_calls
    assert engine.effective(later).min_minutes_between_calls == s.ai.min_minutes_between_calls
    for _ in range(3):
        engine.effective(later + 5)
        executor.effective(later + 5)
    lines = [r for r in ad.read_changes(s, PAIR) if r["action"] == "expired"]
    assert len(lines) == 1 and lines[0]["key"] == "min_minutes_between_calls" and lines[0]["set_ms"] == T0
    assert [k for k, _ in ev_exec + ev_engine] == ["adaptive_expired"]
    con = sqlite3.connect(db)
    assert con.execute("SELECT reverted_ms FROM tuning_changes").fetchone()[0] == later
    con.close()
    # a restarted engine does not repeat it either
    again = []
    ad.AdaptiveStore(s, PAIR, app_db=db, clock=Clock(), emit=lambda k, p: again.append(k)).effective(later + 10)
    assert again == [] and len([r for r in ad.read_changes(s, PAIR) if r["action"] == "expired"]) == 1


def test_never_raises(s, monkeypatch):
    write_overlay(s, {"min_confidence_floor": entry(60, days=1)})
    bad_db = s.paths.state() / "app.db"
    bad_db.parent.mkdir(parents=True, exist_ok=True)
    bad_db.write_bytes(b"not a database at all" * 100)

    def boom(kind, payload):
        raise RuntimeError("sink down")

    st = ad.AdaptiveStore(s, PAIR, app_db=bad_db, clock=Clock(), emit=boom)
    assert st.effective(T0 + 2 * MS_PER_DAY) == ad.defaults(s)                # expired; db + sink errors swallowed
    monkeypatch.setattr(ad, "compute_effective", lambda *a, **k: 1 / 0)
    assert st.effective(T0 + 1) == ad.defaults(s)


def test_changes_reader_skips_a_torn_line(s):
    d = ad.adaptive_dir(s, PAIR)
    d.mkdir(parents=True)
    (d / ad.CHANGES_FILE).write_text('{"action":"set","key":"a"}\n{"action":"se', encoding="utf-8")
    ad.append_jsonl(d / ad.CHANGES_FILE, {"action": "revert", "key": "a"})
    assert [r["action"] for r in ad.read_changes(s, PAIR)] == ["set", "revert"]
    assert json.loads((d / ad.CHANGES_FILE).read_text(encoding="utf-8").splitlines()[-1])["action"] == "revert"


def test_a_line_separator_in_a_record_does_not_split_it(s):
    d = ad.adaptive_dir(s, PAIR)
    d.mkdir(parents=True)
    # an older line written raw (before the lines were ASCII): a splitlines() reader would cut it in two
    (d / ad.CHANGES_FILE).write_text(json.dumps({"action": "set", "reason": "a\u2028b"}, ensure_ascii=False) + "\n",
                                     encoding="utf-8")
    ad.append_jsonl(d / ad.CHANGES_FILE, {"action": "revert", "reason": "c\u2029d \u00e9 \u0085"})
    raw = (d / ad.CHANGES_FILE).read_bytes()
    assert raw.splitlines()[-1].isascii()                                       # the new line is escaped
    assert [(r["action"], r["reason"]) for r in ad.read_changes(s, PAIR)] == [
        ("set", "a\u2028b"), ("revert", "c\u2029d \u00e9 \u0085")]


def test_an_expired_entry_with_non_ascii_text_is_recorded_once(s):
    write_overlay(s, {"tp_hint": entry("TP1 at the prior swing \u2192 then trail", days=1)})
    for _ in range(3):                                                        # three processes / restarts
        ad.AdaptiveStore(s, PAIR, clock=Clock()).effective(T0 + 2 * MS_PER_DAY)
    assert len([r for r in ad.read_changes(s, PAIR) if r["action"] == "expired"]) == 1
