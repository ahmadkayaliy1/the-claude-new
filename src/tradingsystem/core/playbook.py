"""Lint for the texts the adaptive overlay puts in front of the model (§3.8): the per-pair playbook (``$playbook``)
and the take-profit hint (``$tp_hint``).

Both are written autonomously by a review session through ``tools/tune.py`` and read by the trader on every cycle, so
they may shape *how* it reads the market, never *what the system risks*: sizing, leverage, stop distances, the gate's
thresholds and the kill switch stay in the reviewed config. The denylist is the spec's regex (§3.8) widened to the
usual phrasings (a confidence of 80-99 a few words before or after the word within one clause, "85+ confidence", any
"never / don't / avoid … NO_TRADE" in one clause, "disregard", "0.05lots") — while not flagging innocent words that
merely contain a fragment ("slot", "pilot", "plot") or a number that is a price or a unit ("95,000", "95k", "the 80
EMA", "0.8 ATR"). It is matched on a normalised copy of the text: case folded; compatibility forms folded (NFKC:
full-width and mathematical letters); accents and every other combining mark, variation selectors and invisible
format characters (zero-width space / joiner, BOM, soft hyphen: categories Mn, Me, Cf) removed; a few Latin letters
without a decomposition that look like ASCII ones ("ı", "ł", "ø", …) folded; markdown emphasis inside a word removed;
line breaks between the words of the adjacent forms allowed. NFKC does not fold look-alike letters of other scripts
("іgnore" with a Cyrillic "і", "cօnfidence" with an Armenian "օ", "iɡnore" with the IPA "ɡ"), so a word that has ASCII
letters and also a letter outside Latin-1 / Latin Extended-A is refused as such — except a few Greek letters used as
math symbols that look like no Latin letter ("ΔOI", "σ"). A word written wholly in another script passes, and so does
any rephrasing: the denylist is a backstop, not the bound (the overlay has no risk keys, and the gate's other checks
stay in force).

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
# 80-99 or 0.8-0.99 — not part of 1.85 or 850, and not a price (95,000 / 90,500 / 95k / 85.5k) ...
_HIGH = (r"(?<![\d.,])(?:[89]\d|0?\.[89]\d?)(?!\d|,\d|(?:\.\d+)?\s?k\b)"
         # ... nor a number with a unit (the 80 EMA, 0.8 ATR, 0.8R, 85 pips) or a share ("85% of the sweeps"); a bare
         # "85%" stays a confidence ("85% confidence", "confidence above 85%")
         r"(?!\s?(?:ATR|EMA|SMA|R|x|pips?|points?)\b|\s?%\s*of\b)")
_CLAUSE_SEP = r"[^\w\n.,;:!?]+"                            # between two words of one clause on one line
_CLAUSE_GAP = r"[^\w\n.,;:!?]*"
_NT_SEP = r"[^\w\n.,;:!?\-\u2010-\u2015]+"                 # … where a dash starts a new clause too (NO_TRADE rule)
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
    # "never answer NO_TRADE", "don't output a no-trade", "avoid NO_TRADE" (not "avoid chasing: no trade after …",
    # "do not chase — no trade after …": a rule that adds a no-trade condition)
    rf"|\b(?:never|don['’`]?t|do\s+not|avoid){_NT_SEP}(?:\w+{_NT_SEP}){{0,3}}?no[\s_-]?trade"
    rf"|confidence\s*(?:of|at|[:=>≥]|>=)?\s*{_HIGH}"
    rf"|confidence{_CLAUSE_SEP}(?:\w+{_CLAUSE_SEP}){{0,3}}?{_HIGH}"     # "confidence above 85", "confidence (85-90)"
    rf"|{_HIGH}{_CLAUSE_GAP}(?:\w+{_CLAUSE_SEP}){{0,2}}?confidence"     # "85+ confidence", "90 or higher confidence"
    r"|daily\s+loss"
    r"|kill.?switch"
    r"|min[\s_-]?rr",
    re.IGNORECASE,
)
_BULLET = re.compile(r"^\s*(?:[-*+•]|\d{1,2}[.)])\s+")
_EMPHASIS = re.compile(r"[*`~]")                  # markdown inside a word ("ig**no**re"); '_' is kept (min_rr)
_CONTROL_OK = {"\n", "\t"}
_WORD = re.compile(r"\w+")
_INVISIBLE = {"Mn", "Me", "Cf"}                   # combining marks, variation selectors, zero-width / format characters
# Latin letters without a decomposition that look like ASCII ones (NFKD leaves them whole)
_FOLD = str.maketrans("ıĸłŁøØđĐðÐħĦŧŦ", "iklLoOdDdDhHtT")
# Greek letters used as math symbols ("ΔOI", "2σ") that look like no Latin letter — allowed inside a Latin word
_MATH_GREEK = frozenset("ΔδΣσΠπΩωΦφΨψΛλΘθβμξζΓΞ")


def _normal(text: str) -> str:
    """The copy the lint matches (never stored): NFKD, combining marks and format characters removed (accents,
    the combining grapheme joiner, variation selectors, zero-width space / joiner, BOM, soft hyphen), NFKC (full-width
    and mathematical letters → ASCII), then look-alike Latin letters folded to ASCII."""
    t = "".join(ch for ch in unicodedata.normalize("NFKD", text) if unicodedata.category(ch) not in _INVISIBLE)
    return unicodedata.normalize("NFKC", t).translate(_FOLD)


def _foreign(ch: str) -> bool:
    """A letter that may stand for an ASCII one without NFKC folding it: any letter outside ASCII and Latin-1 /
    Latin Extended-A (U+00C0-U+017F) — Cyrillic, Greek, Armenian, Coptic, Cherokee, IPA, small capitals, ..."""
    return ch.isalpha() and not ch.isascii() and not "\u00c0" <= ch <= "\u017f" and ch not in _MATH_GREEK


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
    """Words that have ASCII letters and also a :func:`_foreign` letter ("cоnfidence" with a Cyrillic "о", "iɡnore"
    with the IPA "ɡ", "cօnfidence" with an Armenian "օ") — look-alike evasions NFKC leaves alone. A word wholly in
    another script ("Вход") is fine."""
    found: list[str] = []
    for word in _WORD.findall(_EMPHASIS.sub("", _normal(text))):
        if word.isascii():
            continue
        if (any(ch.isascii() and ch.isalpha() for ch in word) and any(_foreign(ch) for ch in word)
                and word not in found):
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
        problems.append("words that mix Latin letters with letters of another script not allowed (look-alike "
                        f"letters): {', '.join(repr(w) for w in mixed[:3])}")
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
