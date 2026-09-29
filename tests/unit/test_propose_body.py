"""B11: a review session creates a proposal with a ONE-line --body whose backslash-n escapes become line breaks
(the session's allow rule ``Bash(.venv/Scripts/python.exe tools/propose.py *)`` does not match a newline)."""
from __future__ import annotations

from pathlib import Path

from tests.unit.test_propose import BODY, env  # noqa: F401  (env is a fixture)

PROMPTS = Path(__file__).resolve().parents[2] / "tools" / "operator" / "prompts"


def test_a_session_proposal_with_a_one_line_body_is_created_with_real_line_breaks(env, monkeypatch, capsys):
    pp, s, main, git, sent = env
    monkeypatch.setenv("TS_OPERATOR_SESSION", "1")
    monkeypatch.setenv("TS_OPERATOR_REVIEW_ID", "20260929T053900Z_daily")
    one_line = BODY.strip().replace("\n", "\\n")
    assert "\n" not in one_line
    rc = pp.main(["--slug", "one-line-body", "--title", "One line", "--pair", "XAUUSD", "--body", one_line],
                 settings=s, root=main)
    assert rc == 0, capsys.readouterr().out
    docs = list((main.parent / "the_claude_new_wt").glob("proposal-*/docs/proposals/*-one-line-body.md"))
    assert len(docs) == 1
    doc = docs[0].read_text(encoding="utf-8")
    assert "\n## Problem\n" in doc and "\n## Test plan\n" in doc and "\\n" not in doc


def test_a_body_with_real_line_breaks_is_left_alone_and_the_prompt_tells_sessions_to_use_one_line(env):
    pp, s, main, git, sent = env
    assert pp.normalize_body("## A\nkeep `\\n` literal\n") == "## A\nkeep `\\n` literal"
    text = (PROMPTS / "_system.md").read_text(encoding="utf-8")
    assert "ONE line" in text and "backslash and n" in text
