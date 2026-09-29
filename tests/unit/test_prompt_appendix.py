"""Phase 5 B18 (D-049): the gold desk brief ``ai/prompts/desks/xau.md`` is appended to the XAU system prompt through
``render(..., appendix=)`` - no new placeholder, recorded with its version; BTC/ETH prompts are byte-identical."""
from pathlib import Path

import pytest

from tests.unit.test_prompts import SYS, USER
from tradingsystem.ai.orchestrator import Orchestrator
from tradingsystem.ai.prompts import DIR, PromptError, _read_versioned, render
from tradingsystem.core.settings import load_settings

TRADER_ROLES = ("agent_per_pair", "risk_reviewer", "escalation")


def orch():
    o = object.__new__(Orchestrator)
    o.s = load_settings(env_path=Path("nope.env"))
    return o


def test_only_the_desk_pair_gets_a_brief():
    o = orch()
    assert o._brief("XAUUSD") == "desks/xau" and o.s.pairs["XAUUSD"].desk.brief == "desks/xau"
    assert o._brief("BTCUSDT") is None and o._brief("ETHUSDT") is None and o._brief(None) is None


@pytest.mark.parametrize("role", TRADER_ROLES)
def test_without_an_appendix_the_prompt_is_byte_identical(role):
    a, b = render(role, SYS, USER), render(role, SYS, USER, appendix=None)
    assert a.system == b.system and a.user == b.user and a.versions == b.versions and a.prompt_hash == b.prompt_hash
    assert "Gold desk" not in a.system


@pytest.mark.parametrize("role", TRADER_ROLES)
def test_the_brief_is_appended_verbatim_and_versioned(role):
    base = render(role, SYS, USER)
    xau = render(role, SYS, USER, appendix="desks/xau")
    brief, name, version = _read_versioned("desks/xau.md")
    assert name == "desks/xau" and version == 1
    assert xau.system == base.system + "\n\n" + brief                  # appended after the whole system prompt
    assert "levels.round" in xau.system and "### Gold fields" in xau.system   # verbatim, the gold field notes too
    assert xau.user == base.user and xau.versions == {**base.versions, "desks/xau": 1}
    later = render(role, SYS, {**USER, "now_utc": "2026-09-30T10:15:00Z", "payload": '{"x": 1}'}, appendix="desks/xau")
    assert later.prompt_hash == xau.prompt_hash                        # constant per pair: the cache prefix holds


def test_the_brief_is_small_and_says_the_shadow_rules():
    text = _read_versioned("desks/xau.md")[0]
    brief, fields = text.split("### Gold fields")
    assert len(brief) / 3.6 <= 350 and len(text) / 3.6 <= 600       # the brief (B18) + the gold-only field notes
    for must in ("scored in R", "never shrink a stop", "`account.min_position_risk` does not bind", "LIMIT or STOP",
                 "2R after the spread", "market.news", "rollover"):
        assert must in brief, must


@pytest.mark.parametrize("bad", ["desks/nope", "desks/../shared/core_rules", "shared/core_rules", "/etc/x", "desks/XAU"])
def test_an_unknown_or_outside_appendix_is_refused(bad):
    with pytest.raises(PromptError, match="appendix"):
        render("agent_per_pair", SYS, USER, appendix=bad)


def test_the_brief_file_lives_in_the_library():
    assert (DIR / "desks" / "xau.md").is_file()
