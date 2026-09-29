"""Phase 5 B18 (D-049): the gold desk brief ``ai/prompts/desks/xau.md`` and the gold field notes
``desks/xauusd_fields.md`` are appended to the XAU system prompt through ``render(..., appendix=)`` - no new
placeholder, recorded with their versions; BTC/ETH prompts are byte-identical. The field notes stay attached when the
desk is turned off while the payload still carries the gold blocks (review fix)."""
from pathlib import Path

import pytest

from tests.unit.test_prompts import SYS, USER
from tradingsystem.ai.orchestrator import Orchestrator
from tradingsystem.ai.prompts import DIR, PromptError, _read_versioned, render
from tradingsystem.core.settings import load_settings

TRADER_ROLES = ("agent_per_pair", "risk_reviewer", "escalation")
GOLD = ("desks/xau", "desks/xauusd_fields")


def orch(s=None):
    o = object.__new__(Orchestrator)
    o.s = s or load_settings(env_path=Path("nope.env"))
    return o


def test_the_desk_pair_gets_its_brief_and_its_field_notes_btc_eth_nothing():
    o = orch()
    assert o._brief("XAUUSD") == GOLD and o.s.pairs["XAUUSD"].desk.brief == "desks/xau"
    assert o._brief("BTCUSDT") == () and o._brief("ETHUSDT") == () and o._brief(None) == ()


def test_without_the_desk_the_field_notes_stay_while_the_payload_carries_the_gold_blocks():
    s = load_settings(env_path=Path("nope.env"))
    x = s.pairs["XAUUSD"]
    no_desk = s.model_copy(update={"pairs": {**s.pairs, "XAUUSD": x.model_copy(update={"desk": None})}})
    assert orch(no_desk)._brief("XAUUSD") == ("desks/xauusd_fields",)        # news on + context instruments
    bare = x.model_copy(update={"desk": None, "news_blackout": x.news_blackout.model_copy(update={"enabled": False}),
                                "instruments": [i for i in x.instruments if "cross_context" not in i.roles]})
    assert orch(s.model_copy(update={"pairs": {**s.pairs, "XAUUSD": bare}}))._brief("XAUUSD") == ()


@pytest.mark.parametrize("role", TRADER_ROLES)
def test_without_an_appendix_the_prompt_is_byte_identical(role):
    a, b, c = render(role, SYS, USER), render(role, SYS, USER, appendix=None), render(role, SYS, USER, appendix=())
    assert a.system == b.system == c.system and a.user == b.user and a.versions == b.versions == c.versions
    assert a.prompt_hash == b.prompt_hash and "Gold desk" not in a.system and "Gold fields" not in a.system


@pytest.mark.parametrize("role", TRADER_ROLES)
def test_the_brief_and_the_notes_are_appended_verbatim_in_order_and_versioned(role):
    base = render(role, SYS, USER)
    xau = render(role, SYS, USER, appendix=GOLD)
    brief, name, version = _read_versioned("desks/xau.md")
    notes, name2, version2 = _read_versioned("desks/xauusd_fields.md")
    assert (name, version, name2, version2) == ("desks/xau", 1, "desks/xauusd_fields", 1)
    assert xau.system == base.system + "\n\n" + brief + "\n\n" + notes     # after the whole system prompt
    assert "levels.round" in xau.system and "### Gold fields" in xau.system
    assert xau.user == base.user and xau.versions == {**base.versions, "desks/xau": 1, "desks/xauusd_fields": 1}
    later = render(role, SYS, {**USER, "now_utc": "2026-09-30T10:15:00Z", "payload": '{"x": 1}'}, appendix=GOLD)
    assert later.prompt_hash == xau.prompt_hash                        # constant per pair: the cache prefix holds


def test_the_brief_is_small_and_says_the_shadow_rules():
    brief = _read_versioned("desks/xau.md")[0]
    notes = _read_versioned("desks/xauusd_fields.md")[0]
    assert len(brief) / 3.6 <= 350 and len(brief + notes) / 3.6 <= 620     # the brief (B18) + the gold field notes
    for must in ("scored in R", "never shrink a stop", "`account.min_position_risk` does not bind", "LIMIT or STOP",
                 "2R after the spread", "market.news", "rollover"):
        assert must in brief, must
    for must in ("market.gold_clock", "levels.round", "market.news", "confirms_xau_extreme", "votes", "desk_ok"):
        assert must in notes, must


@pytest.mark.parametrize("bad", ["desks/nope", "desks/../shared/core_rules", "shared/core_rules", "/etc/x", "desks/XAU"])
def test_an_unknown_or_outside_appendix_is_refused(bad):
    with pytest.raises(PromptError, match="appendix"):
        render("agent_per_pair", SYS, USER, appendix=bad)
    with pytest.raises(PromptError, match="appendix"):
        render("agent_per_pair", SYS, USER, appendix=("desks/xau", bad))


def test_the_files_live_in_the_library():
    assert (DIR / "desks" / "xau.md").is_file() and (DIR / "desks" / "xauusd_fields.md").is_file()
