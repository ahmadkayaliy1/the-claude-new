"""Phase 5 checkpoint A, step A0: the new config keys load with safe defaults, validate their bounds, and the four
dead resource knobs are gone (nothing read them; an old config naming one is refused like any unknown key)."""
from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from tradingsystem.core.settings import (
    AICfg,
    BackupCfg,
    EvaluationCfg,
    MonitorCfg,
    ResourceProfileCfg,
    StorageCfg,
    load_settings,
)

NO_ENV = Path("nope.env")


def test_the_shipped_config_carries_every_phase5_a_key():
    s = load_settings(env_path=NO_ENV)
    assert s.ai.skip_closed_market is True and s.ai.transient_retry_s == 60
    assert s.ai.quota_reserve_share == 0.4 and s.ai.quota_reserve_hours_utc == [12, 21]
    m = s.monitor
    assert (m.battery_warn_pct, m.battery_critical_pct, m.on_battery_warn_min) == (30, 15, 5)
    assert (m.commit_warn_pct, m.recorder_stall_min) == (85, 30)
    assert (m.diagnose_max_per_day, m.diagnose_max_gauge_level) == (2, 0)
    assert s.backup.enabled and s.backup.dir == "backups" and s.backup.keep == 14
    assert s.evaluation.demo_start_utc == "2026-09-27T21:50:00Z" and s.evaluation.demo_days == 5
    assert s.storage.cold_archive_min_free_gb == 2
    for inst in ("BTCUSDT", "ETHUSDT", "XAUUSD"):             # every pair system validates too
        assert load_settings(env_path=NO_ENV, extra_env={"TS_INSTANCE": inst}).paths.instance == inst


def test_defaults_without_the_config_are_safe():
    assert BackupCfg().keep == 14 and EvaluationCfg().demo_start_utc is None
    assert StorageCfg().cold_archive_min_free_gb == 2.0
    m = MonitorCfg()
    assert m.diagnose_max_per_day == 2 and m.recorder_stall_min == 30


@pytest.mark.parametrize("knob", ["duckdb_memory_mb", "duckdb_threads", "engine_cycle_in_subprocess",
                                  "vision_download_concurrency"])
def test_the_dead_resource_knobs_are_gone(knob):
    with pytest.raises(ValidationError):
        ResourceProfileCfg.model_validate({knob: 1})
    assert set(ResourceProfileCfg.model_fields) == {"sqlite_cache_mb", "parse_chunk_rows"}


@pytest.mark.parametrize("hours", [[21, 12], [0, 25], [5], [3, 3]])
def test_the_quota_reserve_window_must_be_a_forward_utc_range(hours):
    base = load_settings(env_path=NO_ENV).ai.model_dump()
    with pytest.raises(ValidationError, match="quota_reserve_hours_utc"):
        AICfg.model_validate({**base, "quota_reserve_hours_utc": hours})


def test_battery_thresholds_and_the_demo_start_are_checked():
    with pytest.raises(ValidationError, match="battery_critical_pct"):
        MonitorCfg(battery_warn_pct=20, battery_critical_pct=25)
    with pytest.raises(ValidationError, match="UTC offset"):
        EvaluationCfg(demo_start_utc="2026-09-27T21:50:00")      # naive: refused (all times are UTC)
    assert EvaluationCfg(demo_start_utc="2026-09-27T21:50:00+00:00").demo_days == 5
