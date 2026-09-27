"""Lint for the texts the adaptive overlay puts in front of the model (§3.8): the per-pair playbook (``$playbook``)
and the take-profit hint (``$tp_hint``).

Both are written autonomously by a review session through ``tools/tune.py`` and read by the trader on every cycle, so
they may shape *how* it reads the market, never *what the system risks*: sizing, leverage, stop distances, the gate's
thresholds and the kill switch stay in the reviewed config. The denylist is the spec's regex (§3.8) widened to the
usual phrasings (a confidence of 80-99 a few words before or after the word, "85+ confidence", any "never / don't /
avoid … NO_TRADE", "disregard", "0.05lots") and made robust against the obvious evasions — case, full-width and other
compatibility forms (NFKC), zero-width characters, markdown emphasis inside a word, line breaks between the words —
while not flagging innocent words that merely contain a fragment ("slot", "pilot", "plot"). NFKC does not fold
Cyrillic or Greek homoglyphs ("іgnore" with a Cyrillic "і") into Latin letters, so a word that mixes Latin with
Cyrillic or Greek letters is refused as such. A denylist is never complete: it is a backstop, not the bound (the
overlay has no risk keys, and the gate's other checks stay in force).

``$`` is allowed as is: ``prompts._fill`` checks the *template* and inserts values verbatim, never re-parsing them
(``test_fill_checks_the_template_only``), so the spec's "``$`` must be ``$$``" rule would only make the model read a
literal ``$$``.
"""
from __future__ import annotations

import re
import unicodedata

MAX_PLAYBOOK_CHARS = 1500
MAX_PLAYBOOK_BULLETS = 12
MAX_HINT_CHARS = 200

# §3.8: (risk[_ ]per|lot|leverage|stop.?loss (distance|closer)|ignore|override|always (buy|sell|trade)|
#        never no_trade|confidence (8|9)\d|daily loss|kill.?switch|min_rr)
_HIGH = r"(?<![\d.,])(?:[89]\d|0?\.[89]\d?)(?!\d)"       # 80-99 or 0.8-0.99 — not part of 1.85 or 850
_SEP = r"[^\w\n]+"                                         # between two words on one line
_CLAUSE_SEP = r"[^\w\n.,;:!?]+"                            # … and within one clause
DENYLIST = re.compile(
    r"risk[\s_-]*per"
    r"|(?:\b|(?<=\d))lot"                            # lot, lots, lot size, 0.05lots — not slot / pilot / plot
    r"|leverage"
    r"|stop.?loss\s+(?:distance|closer)"
    r"|\bignor(?:e|es|ed|ing)\b"
    r"|\boverrid(?:e|es|den|ing)\b"
    r"|\bdisregard"
    r"|always\s+(?:buy|sell|trade|long|short)"
    r"|never\s+(?:a\s+)?no[\s_-]?trade"
    # "never answer NO_TRADE", "don't output a no-trade", "avoid NO_TRADE" (not "avoid chasing: no trade after …")
    rf"|\b(?:never|don['’`]?t|do\s+not|avoid){_CLAUSE_SEP}(?:\w+{_CLAUSE_SEP}){{0,3}}?no[\s_-]?trade"
    r"|confidence\s*(?:of|at|[:=>≥]|>=)?\s*[89]\d"
    rf"|confidence{_SEP}(?:\w+{_SEP}){{0,3}}?{_HIGH}"   # "confidence above 85", "confidence (85-90)"
    rf"|{_HIGH}[^\w\n]*(?:\w+{_SEP}){{0,2}}?confidence"  # "85+ confidence", "90 or higher confidence"
    r"|daily\s+loss"
    r"|kill.?switch"
    r"|min[\s_-]?rr",
    re.IGNORECASE,
)
_BULLET = re.compile(r"^\s*(?:[-*+•]|\d{1,2}[.)])\s+")
_EMPHASIS = re.compile(r"[*`~]")                  # markdown inside a word ("ig**no**re"); '_' is kept (min_rr)
_CONTROL_OK = {"\n", "\t"}
_WORD = re.compile(r"\w+")


def _normal(text: str) -> str:
    """NFKC (full-width letters → ASCII), format characters (zero-width space/joiner, BOM) removed."""
    t = unicodedata.normalize("NFKC", text)
    return "".join(ch for ch in t if unicodedata.category(ch) != "Cf")


def _denied(text: str) -> list[str]:
    t = _normal(text)
    found: list[str] = []
    for variant in (t, _EMPHASIS.sub("", t)):
        for m in DENYLIST.finditer(variant):
            phrase = " ".join(m.group(0).split()).lower()
            if phrase not in found:
                found.append(phrase)
    return found


def _mixed_script(text: str) -> list[str]:
    """Words that mix Latin with Cyrillic or Greek letters ("cоnfidence" with a Cyrillic "о") — homoglyph evasions
    NFKC leaves alone."""
    found: list[str] = []
    for word in _WORD.findall(_EMPHASIS.sub("", _normal(text))):
        if word.isascii():
            continue
        scripts = {unicodedata.name(ch, "").split(" ", 1)[0] for ch in word if ch.isalpha()}
        if "LATIN" in scripts and scripts & {"CYRILLIC", "GREEK"} and word not in found:
            found.append(word)
    return found


def _common(text: str, *, newlines: bool) -> list[str]:
    problems: list[str] = []
    bad = sorted({f"U+{ord(ch):04X}" for ch in text
                  if unicodedata.category(ch) == "Cc" and (ch not in _CONTROL_OK or (ch == "\n" and not newlines))})
    if bad:
        problems.append(f"control characters not allowed ({', '.join(bad[:5])})")
    seps = sorted({f"U+{ord(ch):04X}" for ch in text if unicodedata.category(ch) in ("Zl", "Zp")})
    if seps:                                     # U+2028 / U+2029: a line break to str.splitlines(), not to the lint
        problems.append(f"Unicode line / paragraph separators not allowed ({', '.join(seps)})")
    mixed = _mixed_script(text)
    if mixed:
        problems.append("words that mix Latin with Cyrillic or Greek letters not allowed: "
                        f"{', '.join(repr(w) for w in mixed[:3])}")
    if "```" in text:
        problems.append("code fences not allowed (the payload follows in a fenced block)")
    denied = _denied(text)
    if denied:
        problems.append("denylisted wording (risk, sizing, stops, gate thresholds and the kill switch stay in the "
                        f"config): {', '.join(repr(d) for d in denied[:6])}")
    return problems


def lint(text: str) -> list[str]:
    """Problems of a playbook ([] = ok): not empty, ≤ 1500 characters, ≤ 12 bullets, denylist, no control
    characters, line / paragraph separators, mixed-script words or code fences."""
    if not text or not text.strip():
        return ["empty (to remove the playbook use: tune.py revert playbook)"]
    problems: list[str] = []
    if len(text) > MAX_PLAYBOOK_CHARS:
        problems.append(f"too long: {len(text)} characters (max {MAX_PLAYBOOK_CHARS})")
    bullets = sum(1 for line in text.splitlines() if _BULLET.match(line))
    if bullets > MAX_PLAYBOOK_BULLETS:
        problems.append(f"too many bullets: {bullets} (max {MAX_PLAYBOOK_BULLETS})")
    return problems + _common(text, newlines=True)


def lint_hint(text: str) -> list[str]:
    """Problems of a take-profit hint ([] = ok): one non-empty line of ≤ 200 characters (no U+2028 / U+2029
    either), denylist, no mixed-script words."""
    if not text or not text.strip():
        return ["empty (to remove the hint use: tune.py revert tp_hint)"]
    problems: list[str] = []
    if len(text) > MAX_HINT_CHARS:
        problems.append(f"too long: {len(text)} characters (max {MAX_HINT_CHARS})")
    return problems + _common(text, newlines=False)


__all__ = ["DENYLIST", "MAX_HINT_CHARS", "MAX_PLAYBOOK_BULLETS", "MAX_PLAYBOOK_CHARS", "lint", "lint_hint"]
