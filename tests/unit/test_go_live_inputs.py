"""Go-live inputs (Phase 5 A8 = §3.9 item 13): every checklist item judged pass / FAIL / n.a. with its value and n."""
from __future__ import annotations

import copy
import importlib.util
import json
import re
from pathlib import Path

import pytest

from tradingsystem.core.settings import INSTANCE_ENV, load_settings
from tradingsystem.core.timeutil import MS_PER_DAY, parse_date_spec

ROOT = Path(__file__).resolve().parents[2]
DOC = ROOT / "docs" / "go_live_checklist.md"
START = parse_date_spec("2026-09-27T21:50:00Z")


@pytest.fixture()
def gl():
    spec = importlib.util.spec_from_file_location("go_live_inputs_under_test", ROOT / "tools" / "go_live_inputs.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def base_at(tmp_path: Path):
    s = load_settings(extra_env={INSTANCE_ENV: ""})
    return s.model_copy(update={"paths": s.paths.model_copy(update={"data_dir": str(tmp_path / "data"),
                                                                     "logs_dir": str(tmp_path / "logs")})})


def pair_ok(pair: str) -> dict:
    return {"realised": {"n": 2, "wins": 1, "losses": 1, "pnl_usd": 0.03},
            "funnel": {"virtual_resolved": 5, "virtual_tp1_first": 3},
            "availability": {"share": 0.97, "open_cycles": 480, "lost_total": 14, "lost": {"data_not_ready": 14}},
            "sl_proof": {"placed": 2, "sl_not_proven": 0, "ids": []},
            "gate": {"rejected": 3, "by_class": {"position_size_min_lot": 2, "rr_after_costs": 1}},
            "cap": {"value": 30, "source": "the engine status (quota_per_day)"},
            "calls_per_day": {"2026-09-28": 30, "2026-09-29": 22},
            "status": {"engine": {"detail": {"snapshot_build_ms": {"median": 560, "n": 20, "max": 2807}}}},
            "hashes_seen": {"git_sha": [{"value": "d0ebc00", "n": 40}]}}


def demo_ok() -> dict:
    """A complete demo window in which every automatic item passes."""
    return {"window": {"since_ms": START, "until_ms": START + 5 * MS_PER_DAY, "since": "2026-09-27T21:50:00.000Z",
                       "planned_until": "2026-10-02T21:50:00.000Z", "complete": True, "days": 5, "days_covered": 5},
            "generated": "2026-10-02T22:00:00.000Z", "per_pair": {p: pair_ok(p) for p in ("BTCUSDT", "ETHUSDT")},
            "incidents": [{"kind": "monitor", "title": "BTCUSDT: connection outage", "level": "warn",
                           "first": "2026-09-28T00:35:46.967Z", "detect_min": 13.5},
                          {"kind": "event", "title": "services started (restart)", "level": "event",
                           "first": "2026-09-27T22:16:33.000Z", "detect_min": None}],
            "account": {"peak_file": {"mt5:WindsorBrokers1-Demo:1": {"peak": 103.87, "tripped_at": None}},
                        "executor": {"equity": 101.33, "drawdown": {"drawdown_pct": 2.45, "tripped": None}},
                        "exposure_without_sl": []},
            "session_limits": {"rows": 0, "events": []},
            "monitor": {"runs": 480, "max_gap_min": 15.4, "ram_min_mb": None},
            "machine": {"available_mb": 2048, "total_mb": 7929, "boot_ms": START - MS_PER_DAY},
            "git": {"sha": "abc1234", "dirty": False},
            "sample_size": "5 days show the absence of catastrophic behaviour and the execution quality, not a "
                           "statistical edge"}


def by_id(items: list[dict]) -> dict[str, dict]:
    return {i["id"]: i for i in items}


def test_every_checklist_row_is_judged_in_the_documents_order_with_a_value_and_n(gl, tmp_path):
    s = base_at(tmp_path)
    items = gl.measure(demo_ok(), s, tmp_path)
    assert [i["id"] for i in items] == ["demo_days", "outcomes", "availability", "no_position_without_sl",
                                        "no_loss_trip", "gate_classes", "calls_cap", "monitor_mttd", "snapshot_build",
                                        "free_ram", "no_sleep_shutdown", "backup_restored", "risk_limits", "p7_2",
                                        "tests_green", "live_refusal", "sample_size", "go_live_scope"]
    rows = [ln for ln in DOC.read_text(encoding="utf-8").splitlines() if re.match(r"^\| \d+ \| ", ln)]
    assert len(rows) == len(items)                                               # one tool row per document row
    st = {i["id"]: i["status"] for i in items}
    auto = ("demo_days", "availability", "no_position_without_sl", "no_loss_trip", "gate_classes", "calls_cap",
            "snapshot_build", "free_ram", "no_sleep_shutdown", "risk_limits")
    assert all(st[k] == gl.PASS for k in auto), st
    owner = ("outcomes", "monitor_mttd", "backup_restored", "p7_2", "tests_green", "live_refusal", "sample_size",
             "go_live_scope")
    assert all(st[k] == gl.NA for k in owner), st
    b = by_id(items)
    assert "BTCUSDT 97.0 % (n 480" in b["availability"]["measured"] and b["availability"]["n"] == 960
    assert b["outcomes"]["n"] == 4 and "REPORTED" in b["outcomes"]["threshold"]
    assert "detection 13.5–13.5 min" in b["monitor_mttd"]["measured"] and b["monitor_mttd"]["n"] == 1


def test_the_thresholds_in_the_code_are_the_checklist_documents(gl):
    doc = DOC.read_text(encoding="utf-8")
    assert "≥ 95 % of the market-open cycles" in doc and gl.AVAILABILITY_MIN == 0.95
    assert "≤ 15 min, proven by one drill" in doc and gl.MTTD_MAX_MIN == 15
    assert "| ≤ 3 s |" in doc and gl.SNAPSHOT_MEDIAN_MAX_MS == 3000
    assert "≥ 1 GB now" in doc and gl.FREE_RAM_MIN_MB == 1024
    assert "≤ 1 subscription-limit event" in doc and gl.SESSION_LIMIT_EVENTS_MAX == 1
    assert "1 % target / 3 % max per trade / 10 % daily incl. worst case / 4 % correlated / 3 open" in doc
    assert gl.RISK_LIMITS == {"risk_per_trade_pct": 1.0, "max_risk_per_trade_pct": 3.0, "max_daily_loss_pct": 10.0,
                              "max_correlated_risk_pct": 4.0, "max_open_positions": 3}
    table = doc.split("### Expected gate rejection classes", 1)[1].split("**Not expected", 1)[0]
    for cls in gl.EXPECTED_GATE_CLASSES:
        assert f"`{cls}`" in table, cls
    for bad in ("stop_loss_present", "sl_side", "is_trade", "quote_fresh", "basis", "daily_loss_limit",
                "account_drawdown", "position_size_other", "not_gated"):
        assert bad not in gl.EXPECTED_GATE_CLASSES and f"`{bad}" in doc, bad


def test_an_incomplete_window_or_fewer_than_five_days_fails(gl, tmp_path):
    s = base_at(tmp_path)
    d = demo_ok()
    d["window"].update(complete=False, days_covered=0.44)
    assert by_id(gl.measure(d, s))["demo_days"]["status"] == gl.FAIL
    d = demo_ok()
    d["window"].update(days=3, days_covered=3)
    assert by_id(gl.measure(d, s))["demo_days"]["status"] == gl.FAIL


def test_availability_below_95_percent_in_any_pair_fails_and_unknown_is_not_a_pass(gl, tmp_path):
    s = base_at(tmp_path)
    d = demo_ok()
    d["per_pair"]["ETHUSDT"]["availability"].update(share=0.293, lost_total=29)       # the first night, measured
    assert by_id(gl.measure(d, s))["availability"]["status"] == gl.FAIL
    d = demo_ok()
    for r in d["per_pair"].values():
        r["availability"] = {"error": "no app.db"}
    assert by_id(gl.measure(d, s))["availability"]["status"] == gl.NA
    d = demo_ok()
    d["per_pair"]["ETHUSDT"]["availability"] = {"error": "no app.db"}
    assert by_id(gl.measure(d, s))["availability"]["status"] == gl.FAIL                # one pair unknown


@pytest.mark.parametrize("breach", ["monitor", "unproven", "open_now"])
def test_a_position_without_sl_anywhere_fails(gl, tmp_path, breach):
    s = base_at(tmp_path)
    d = demo_ok()
    if breach == "monitor":
        d["incidents"].append({"kind": "monitor", "title": "ETHUSDT: position without SL", "level": "critical"})
    elif breach == "unproven":
        d["per_pair"]["ETHUSDT"]["sl_proof"].update(sl_not_proven=1, ids=["abcd1234"])
    else:
        d["account"]["exposure_without_sl"] = ["ETHUSDT:abcd"]
    assert by_id(gl.measure(d, s))["no_position_without_sl"]["status"] == gl.FAIL


@pytest.mark.parametrize("breach", ["peak_file", "executor", "finding", "equity_drop", "gate"])
def test_a_daily_loss_or_drawdown_trip_fails(gl, tmp_path, breach):
    s = base_at(tmp_path)
    d = demo_ok()
    if breach == "peak_file":
        d["account"]["peak_file"]["mt5:WindsorBrokers1-Demo:1"]["tripped_at"] = "2026-09-29T10:00:00.000Z"
    elif breach == "executor":
        d["account"]["executor"]["drawdown"]["tripped"] = 1790600000000
    elif breach == "finding":
        d["incidents"].append({"kind": "monitor", "title": "ETHUSDT: daily loss limit", "level": "critical"})
    elif breach == "equity_drop":
        d["incidents"].append({"kind": "monitor", "title": "Equity drop", "level": "critical"})
    else:
        d["per_pair"]["ETHUSDT"]["gate"]["by_class"]["daily_loss_limit"] = 1
    assert by_id(gl.measure(d, s))["no_loss_trip"]["status"] == gl.FAIL
    d = demo_ok()                                                     # a warn-level equity drop is not a trip
    d["incidents"].append({"kind": "monitor", "title": "Equity drop", "level": "warn"})
    assert by_id(gl.measure(d, s))["no_loss_trip"]["status"] == gl.PASS


def test_a_gate_rejection_outside_the_expected_classes_fails(gl, tmp_path):
    s = base_at(tmp_path)
    d = demo_ok()
    d["per_pair"]["BTCUSDT"]["gate"]["by_class"].update({"daily_loss_worst_case": 1, "correlated_exposure": 2})
    assert by_id(gl.measure(d, s))["gate_classes"]["status"] == gl.PASS                # protective, expected
    for bad in ("quote_fresh", "position_size_other", "not_gated: no execution quote", "stop_loss_present"):
        d = demo_ok()
        d["per_pair"]["BTCUSDT"]["gate"]["by_class"][bad] = 1
        item = by_id(gl.measure(d, s))["gate_classes"]
        assert item["status"] == gl.FAIL and f"unexpected: {bad} 1" in item["measured"], bad


def test_calls_over_the_cap_or_two_session_limit_events_fail(gl, tmp_path):
    s = base_at(tmp_path)
    d = demo_ok()
    d["per_pair"]["BTCUSDT"]["calls_per_day"]["2026-09-30"] = 31
    item = by_id(gl.measure(d, s))["calls_cap"]
    assert item["status"] == gl.FAIL and "OVER on 2026-09-30" in item["measured"]
    d = demo_ok()
    d["session_limits"] = {"rows": 2, "events": [{"time": "a"}]}                       # one hit of both pairs
    assert by_id(gl.measure(d, s))["calls_cap"]["status"] == gl.PASS
    d["session_limits"] = {"rows": 3, "events": [{"time": "a"}, {"time": "b"}]}
    assert by_id(gl.measure(d, s))["calls_cap"]["status"] == gl.FAIL


def test_snapshot_ram_and_sleep_items_fail_on_their_evidence(gl, tmp_path):
    s = base_at(tmp_path)
    d = demo_ok()
    d["per_pair"]["BTCUSDT"]["status"]["engine"]["detail"]["snapshot_build_ms"]["median"] = 3200
    assert by_id(gl.measure(d, s))["snapshot_build"]["status"] == gl.FAIL
    d = demo_ok()
    d["incidents"].append({"kind": "monitor", "title": "BTCUSDT: slow snapshot build", "level": "warn"})
    assert by_id(gl.measure(d, s))["snapshot_build"]["status"] == gl.FAIL
    d = demo_ok()
    d["machine"]["available_mb"] = 817                                                  # measured 2026-09-28
    assert by_id(gl.measure(d, s))["free_ram"]["status"] == gl.FAIL
    d = demo_ok()
    d["incidents"].append({"kind": "monitor", "title": "Low free RAM", "level": "warn"})
    d["monitor"]["ram_min_mb"] = 147
    item = by_id(gl.measure(d, s))["free_ram"]
    assert item["status"] == gl.FAIL and "lowest seen 147 MB" in item["measured"]
    d = demo_ok()
    d["incidents"].append({"kind": "event", "title": "PC asleep ~52 min", "first": "2026-09-29T18:32:50.000Z"})
    assert by_id(gl.measure(d, s))["no_sleep_shutdown"]["status"] == gl.FAIL
    d = demo_ok()
    d["machine"]["boot_ms"] = START + 2 * MS_PER_DAY                                     # a shutdown in the window
    item = by_id(gl.measure(d, s))["no_sleep_shutdown"]
    assert item["status"] == gl.FAIL and "INSIDE the window" in item["measured"]
    d = demo_ok()
    d["incidents"].append({"kind": "event", "title": "supervisor shutdown", "first": "2026-09-30T10:00:00.000Z"})
    assert by_id(gl.measure(d, s))["no_sleep_shutdown"]["status"] == gl.PASS            # a planned restart


def test_a_logged_restore_passes_the_backup_item_else_the_owner_ticks(gl, tmp_path):
    s = base_at(tmp_path)
    d = demo_ok()
    item = by_id(gl.measure(d, s, tmp_path))["backup_restored"]
    assert item["status"] == gl.NA and "owner ticks" in item["measured"]
    (tmp_path / "backups").mkdir()
    (tmp_path / "backups" / "20260928T033000Z.zip").write_bytes(b"x")
    (tmp_path / "backups" / "20260928T040000Z.incomplete.zip").write_bytes(b"x")
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "backup.jsonl").write_text(json.dumps({"ts": "2026-09-28T09:12:00.000+00:00", "level": "INFO",
                                                   "msg": "restored backups\\20260928T033000Z.zip into C:\\scratch\\data "
                                                          "(14 files, 3.2 s)"}) + "\n", encoding="utf-8")
    item = by_id(gl.measure(d, s, tmp_path))["backup_restored"]
    assert item["status"] == gl.PASS and item["n"] == 1 and "backups in" in item["measured"]
    assert ": 1 (newest 20260928T033000Z.zip)" in item["measured"]                     # the incomplete one is not


def test_changed_risk_limits_fail(gl, tmp_path):
    s = base_at(tmp_path)
    raised = s.model_copy(update={"risk": s.risk.model_copy(update={"max_risk_per_trade_pct": 5.0})})
    item = by_id(gl.measure(demo_ok(), raised))["risk_limits"]
    assert item["status"] == gl.FAIL and "max_risk_per_trade_pct 5.0 (want 3.0)" in item["measured"]


def test_the_cli_exits_zero_always_and_prints_the_table_or_json(gl, tmp_path, monkeypatch, capsys):
    s = base_at(tmp_path)
    monkeypatch.setattr(gl, "load_settings", lambda: s)
    dr = gl.demo_module()
    monkeypatch.setattr(dr, "collect", lambda *a, **k: copy.deepcopy(demo_ok()))
    assert gl.main([]) == 0
    out = capsys.readouterr().out
    assert out.startswith("# Go-live inputs — demo window 2026-09-27T21:50:00.000Z → 2026-10-02T21:50:00.000Z "
                          "(complete)") and "| 3 | availability of the 15-min cycles" in out and "**pass**" in out
    assert gl.main(["--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert len(doc["items"]) == 18 and doc["summary"] == {"pass": 10, "FAIL": 0, "n.a.": 8}

    def boom(*a, **k):
        raise RuntimeError("database is locked")
    monkeypatch.setattr(dr, "collect", boom)
    assert gl.main(["--json"]) == 0
    assert json.loads(capsys.readouterr().out)["error"] == "RuntimeError: database is locked"
    assert gl.main(["--bogus-flag"]) == 0 and gl.main(["--help"]) == 0
    capsys.readouterr()
