"""B14 (D-049): ``pairs.<PAIR>.desk`` (DeskCfg: shadow only, metal pairs only) and the per-instance override rules
(``instances.<PAIR>.overrides.risk`` may only tighten; ``execution.magic`` may not be set)."""
from pathlib import Path

import pytest
from pydantic import ValidationError

from tradingsystem.core.settings import (DEFAULT_CONFIG, RISK_TIGHTEN, DeskCfg, DeskWindowCfg, RiskCfg, Settings,
                                         load_settings)

ENV = Path("nope.env")


def load(tmp_path, local_yaml: str, instance: str = ""):
    local = tmp_path / "config.local.yaml"
    local.write_text(local_yaml, encoding="utf-8")
    extra = {"TS_INSTANCE": instance} if instance else {"TS_INSTANCE": ""}
    return load_settings(DEFAULT_CONFIG, local_path=local, env_path=ENV, extra_env=extra)


def overrides(body: str) -> str:
    return f"instances:\n  XAUUSD: {{overrides: {body}}}\n"


# ------------------------------------------------------------------ DeskCfg
def test_only_gold_has_a_desk_and_it_is_in_shadow():
    s = load_settings(DEFAULT_CONFIG, env_path=ENV)
    assert s.pairs["BTCUSDT"].desk is None and s.pairs["ETHUSDT"].desk is None      # today's behaviour, exactly
    d = s.pairs["XAUUSD"].desk
    assert d.mode == "shadow" and d.brief == "desks/xau" and d.reopen_grace_min == 60
    assert [(w.name, w.tz, w.start, w.end) for w in d.windows] == [
        ("london", "Europe/London", "07:45", "11:00"), ("new_york", "America/New_York", "08:15", "11:30")]
    assert (d.friday_cutoff.time, d.friday_cutoff.tz) == ("12:00", "America/New_York")
    assert s.pairs["XAUUSD"].asset_class == "metal"


def test_the_code_default_is_no_desk_and_the_desk_defaults_are_the_gold_ones():
    assert Settings.model_fields["pairs"].annotation  # the pairs mapping exists; PairCfg.desk is None by default
    from tradingsystem.core.settings import PairCfg
    assert PairCfg.model_fields["desk"].default is None
    d = DeskCfg()
    assert d.mode == "shadow" and d.brief == "" and [w.name for w in d.windows] == ["london", "new_york"]


def test_a_desk_has_no_live_mode_and_no_unknown_keys():
    with pytest.raises(ValidationError):
        DeskCfg(mode="live")
    with pytest.raises(ValidationError):
        DeskCfg(mode="off")
    with pytest.raises(ValidationError):
        DeskCfg(unknown=1)


def test_a_desk_is_for_metal_pairs_only(tmp_path):
    with pytest.raises(Exception, match="desk is only valid for metal pairs"):
        load(tmp_path, "pairs:\n  BTCUSDT:\n    desk: {mode: shadow}\n")
    s = load(tmp_path, "pairs:\n  XAUUSD:\n    desk: {mode: shadow, brief: desks/x, reopen_grace_min: 30}\n")
    assert s.pairs["XAUUSD"].desk.reopen_grace_min == 30 and s.pairs["XAUUSD"].desk.brief == "desks/x"
    with pytest.raises(Exception, match="brief"):                     # B18: a library file under desks/ only
        load(tmp_path, "pairs:\n  XAUUSD:\n    desk: {mode: shadow, brief: ../secrets}\n")
    # no desk at all is still valid for a metal (a desk is opt-in)
    s = load(tmp_path, "pairs:\n  XAUUSD:\n    desk: null\n")
    assert s.pairs["XAUUSD"].desk is None


@pytest.mark.parametrize("bad", [
    {"name": "x", "tz": "Mars/Olympus", "start": "07:45", "end": "11:00"},          # the zone must resolve
    {"name": "x", "tz": "Europe/London", "start": "7:45", "end": "11:00"},           # HH:MM, 24 h
    {"name": "x", "tz": "Europe/London", "start": "07:45", "end": "24:00"},
    {"name": "x", "tz": "Europe/London", "start": "07:45", "end": "07:45"},          # empty window
    {"name": "x", "tz": "Europe/London", "start": "22:00", "end": "02:00"},          # no window crosses midnight
    {"name": "", "tz": "Europe/London", "start": "07:45", "end": "11:00"},
])
def test_windows_must_parse_and_their_zones_resolve(bad):
    with pytest.raises(ValidationError):
        DeskWindowCfg(**bad)
    with pytest.raises(ValidationError):
        DeskCfg(windows=[bad])


def test_friday_cutoff_and_window_names_are_checked():
    with pytest.raises(ValidationError):
        DeskCfg(friday_cutoff={"time": "12:00", "tz": "Nowhere/City"})
    with pytest.raises(ValidationError):
        DeskCfg(friday_cutoff={"time": "25:00", "tz": "America/New_York"})
    w = {"name": "a", "tz": "Europe/London", "start": "07:45", "end": "11:00"}
    with pytest.raises(ValidationError, match="duplicate"):
        DeskCfg(windows=[w, w])
    assert DeskCfg(windows=[]).windows == []


# ------------------------------------------------------------------ overrides: risk may only tighten
def test_every_risk_field_is_classified_or_deliberately_refused():
    """A new RiskCfg field must be given a tightening direction (or refused) before an instance may override it."""
    unclassified = set(RiskCfg.model_fields) - set(RISK_TIGHTEN)
    assert unclassified == {"correlated_groups", "allow_high_risk_display"}       # lists / display flags: refused
    assert set(RISK_TIGHTEN.values()) == {"down", "up"}


@pytest.mark.parametrize("field", sorted(RISK_TIGHTEN))
def test_a_risk_override_may_tighten_or_repeat_but_never_loosen(tmp_path, field):
    base = getattr(RiskCfg(**{k: v for k, v in load_settings(DEFAULT_CONFIG, env_path=ENV).risk.model_dump().items()}), field)
    down = RISK_TIGHTEN[field] == "down"
    step = 1 if isinstance(base, int) else 0.1
    tighter, looser = (base - step, base + step) if down else (base + step, base - step)
    ok_vals = [tighter, base]
    for v in ok_vals:
        if field == "min_confidence" and not 50 <= v <= 90:
            continue
        s = load(tmp_path, overrides(f"{{risk: {{{field}: {v}}}}}"), instance="XAUUSD")
        assert getattr(s.risk, field) == pytest.approx(v)
    if field == "min_confidence" and looser < 50:
        return
    with pytest.raises(ValueError, match="would loosen"):
        load(tmp_path, overrides(f"{{risk: {{{field}: {looser}}}}}"), instance="XAUUSD")
    with pytest.raises(ValueError, match=r"instances\.XAUUSD\.overrides"):         # the all-pairs check (`config`) too
        load(tmp_path, overrides(f"{{risk: {{{field}: {looser}}}}}"))


def test_the_other_instances_keep_the_global_risk(tmp_path):
    y = overrides("{risk: {max_open_positions: 1, min_rr: 2.0}}")
    xau = load(tmp_path, y, instance="XAUUSD")
    btc = load(tmp_path, y, instance="BTCUSDT")
    assert (xau.risk.max_open_positions, xau.risk.min_rr) == (1, 2.0)
    assert (btc.risk.max_open_positions, btc.risk.min_rr) == (3, 1.5)


@pytest.mark.parametrize("body, why", [
    ("{risk: {correlated_groups: [[XAUUSD]]}}", "no tightening direction"),
    ("{risk: {allow_high_risk_display: false}}", "no tightening direction"),
    ("{risk: {no_such_limit: 1}}", "unknown risk setting"),
    ("{risk: {min_rr: true}}", "not a number"),
    ("{risk: {min_rr: '2.0'}}", "not a number"),
    ("{risk: [1]}", "must be a mapping"),
    ("{risk: null}", "must be a mapping"),
])
def test_a_risk_override_without_a_tightening_direction_is_refused(tmp_path, body, why):
    with pytest.raises(ValueError, match=why):
        load(tmp_path, overrides(body), instance="XAUUSD")
    with pytest.raises(ValueError, match=r"instances\.XAUUSD\.overrides"):
        load(tmp_path, overrides(body))


def test_the_tightening_is_measured_against_the_global_value_in_force(tmp_path):
    y = "risk:\n  max_daily_loss_pct: 6.0\n" + overrides("{risk: {max_daily_loss_pct: 8.0}}")     # 8 > the local 6
    with pytest.raises(ValueError, match="would loosen the limit 6.0"):
        load(tmp_path, y, instance="XAUUSD")
    ok = "risk:\n  max_daily_loss_pct: 6.0\n" + overrides("{risk: {max_daily_loss_pct: 5.0}}")
    assert load(tmp_path, ok, instance="XAUUSD").risk.max_daily_loss_pct == 5.0


# ------------------------------------------------------------------ overrides: no magic
def test_an_override_may_not_set_the_magic_number(tmp_path):
    y = overrides("{execution: {magic: 1}}")
    with pytest.raises(ValueError, match="may not set 'magic'"):
        load(tmp_path, y, instance="XAUUSD")
    with pytest.raises(ValueError, match=r"instances\.XAUUSD\.overrides"):
        load(tmp_path, y)
    with pytest.raises(ValueError, match="may not set 'magic'"):
        load(tmp_path, overrides("{execution: null}"), instance="XAUUSD")
    # other execution keys stay allowed, and the instance's magic is still execution.magic + magic_offset
    s = load(tmp_path, overrides("{execution: {management: {dry_run: true}}}"), instance="XAUUSD")
    assert s.execution.management.dry_run and s.execution.magic == 26092501 + 3


def test_the_committed_config_loads_in_every_view():
    for inst in ("", "BTCUSDT", "ETHUSDT", "XAUUSD"):
        s = load_settings(DEFAULT_CONFIG, env_path=ENV, extra_env={"TS_INSTANCE": inst})
        assert s.pairs["XAUUSD"].desk.mode == "shadow"


@pytest.mark.parametrize("value", [".nan", ".NaN", ".inf", "-.inf"])
def test_a_non_finite_risk_override_is_refused(tmp_path, value):
    """Review fix: NaN compares False both ways, so it passed the direction check and removed the ATR stop floor
    (max(stops+spread, nan) keeps the first argument); infinities are no tightening either."""
    with pytest.raises(Exception, match="finite"):
        load(tmp_path, f"instances:\n  XAUUSD:\n    overrides:\n      risk: {{sl_atr_min_mult: {value}}}\n")
