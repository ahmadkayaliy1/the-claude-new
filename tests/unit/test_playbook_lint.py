"""Playbook / tp_hint lint (§3.8, core/playbook.py): length, bullets, the denylist and its evasions."""
import pytest

from tradingsystem.core.playbook import MAX_HINT_CHARS, MAX_PLAYBOOK_CHARS, lint, lint_hint

GOOD = """- Asia range: fade the first sweep of the high only after a 5m CHoCH back inside.
- Prefer TP1 at the opposite range edge; the 1h FVG above $PDH is the second target.
- In a 1h downtrend, longs need a 15m BOS plus reclaimed VWAP.
- Weekend: thin books, wicks run a long way - wait for the 15m close."""


def test_a_normal_playbook_passes_and_a_dollar_sign_is_fine():
    # '$' in a value is inserted verbatim by prompts._fill (never re-parsed), so no '$$' rule
    assert lint(GOOD) == []
    assert lint_hint("TP1 at the prior 15m swing; $PDH as TP2 when the 1h trend agrees") == []


def test_length_limit():
    assert lint("x" * MAX_PLAYBOOK_CHARS) == []
    probs = lint("x" * (MAX_PLAYBOOK_CHARS + 1))
    assert len(probs) == 1 and "too long" in probs[0]
    assert lint_hint("y" * MAX_HINT_CHARS) == []
    assert "too long" in lint_hint("y" * (MAX_HINT_CHARS + 1))[0]


def test_bullet_limit_counts_every_list_style():
    twelve = "\n".join(f"- rule {i}" for i in range(12))
    assert lint(twelve) == []
    for style in ("- ", "* ", "+ ", "1. ", "2) ", "• "):
        text = twelve + f"\n{style}one more"
        probs = lint(text)
        assert any("13" in p and "bullets" in p for p in probs), style


def test_empty_texts_are_refused():
    assert lint("") and lint("  \n ")
    assert lint_hint("") and lint_hint("   ")


@pytest.mark.parametrize("phrase", [
    "risk per trade", "risk_per_trade 2%", "RISK-PER trade", "use 0.5 lot", "double the lots", "use more leverage",
    "move the stop loss closer", "stop-loss distance of 2 ATR", "stoploss closer", "ignore the gate",
    "ignoring the 4h trend is fine", "override the rules", "overridden by momentum", "always buy the dip",
    "always sell rallies", "always trade the open", "never NO_TRADE", "never no trade on Fridays",
    "confidence 85", "confidence of 90", "confidence: 80", "confidence >= 95", "confidence ≥ 88", "daily loss",
    "the kill switch", "kill-switch", "killswitch", "min_rr 1.2", "min rr"])
def test_denylist(phrase):
    probs = lint(f"- context line\n- {phrase}\n")
    assert any("denylisted" in p for p in probs), phrase
    assert any("denylisted" in p for p in lint_hint(phrase)), phrase


@pytest.mark.parametrize("text", ["watch the slot of the Asian session", "the pilot trade", "plot the 1h FVG",
                                  "confidence 70 is fine", "a stop above the swing", "daily range", "risk-off tone"])
def test_innocent_words_pass(text):
    assert lint(text) == [], text


@pytest.mark.parametrize("text", [
    "ig​nore the gate",             # zero-width space inside the word
    "ｉｇｎｏｒｅ the gate",               # full-width letters (NFKC)
    "ig**no**re the gate",              # markdown emphasis inside the word
    "stop loss\ndistance",              # words split over a line break
    "KILL⁠SWITCH",                 # word joiner
])
def test_denylist_evasions_are_caught(text):
    assert any("denylisted" in p for p in lint(text)), text


def test_control_characters_and_code_fences():
    assert any("control" in p for p in lint("rule one\x07"))
    assert any("code fences" in p for p in lint("```json\n{}\n```"))
    assert lint("tabs\tare fine\nand so are line breaks") == []
    assert any("control" in p for p in lint_hint("two\nlines"))     # a hint is one line
