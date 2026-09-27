"""tools/propose.py (§3.8): git is faked — no worktree or branch is ever created in the real repository, and the
"production checkout" (a tmp directory standing in for it) is never written."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tradingsystem.core.settings import load_settings
from tradingsystem.core.timeutil import now_ms

ROOT = Path(__file__).resolve().parents[2]
BODY = ("## Problem\nXAU loses in the Asia session.\n\n## Numbers\n24 resolved, TP1 first 5, mean R -0.4 (168 h).\n\n"
        "## Proposed change\n`execution.sessions.XAUUSD: [london, newyork]`\n\n## Risk\nFewer trades.\n\n"
        "## Test plan\nReplay 2 weeks; unit test for the session filter.\n")
STATUS = ("# Project Status\n\n## Overview\ntext\n\n## Human actions pending (in the order they will be needed)\n"
          "| # | Action | Needed by | Status |\n|---|---|---|---|\n| H1 | first | P1 | ✅ |\n| H2 | second | P2 | ⏳ |\n\n"
          "## Environment facts\nfacts\n")


def tool(name: str):
    spec = importlib.util.spec_from_file_location(f"test_{name}", ROOT / "tools" / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class FakeGit:
    """Just enough git: the common dir of the stand-in main checkout, refs, ``worktree add`` (a copy of the checkout's
    PROJECT_STATUS.md, like a real checkout of main), add/commit/rev-parse. Records every call with its cwd."""

    def __init__(self, main: Path, *, fail_on: str | None = None) -> None:
        self.main, self.fail_on = main, fail_on
        self.branches = {"main"}
        self.calls: list[tuple[tuple[str, ...], Path]] = []

    def __call__(self, args: list[str], cwd: Path) -> subprocess.CompletedProcess:
        self.calls.append((tuple(args), Path(cwd)))
        ok = subprocess.CompletedProcess(args, 0, "", "")
        if self.fail_on and args[0] == self.fail_on:
            return subprocess.CompletedProcess(args, 1, "", f"fatal: {self.fail_on} failed")
        if args[:3] == ["rev-parse", "--path-format=absolute", "--git-common-dir"]:
            return subprocess.CompletedProcess(args, 0, str(self.main / ".git") + "\n", "")
        if args[:3] == ["rev-parse", "--verify", "--quiet"]:
            return subprocess.CompletedProcess(args, 0 if args[3].removeprefix("refs/heads/") in self.branches else 1, "", "")
        if args[:2] == ["worktree", "add"]:
            _, _, _, branch, wt, base = args
            assert base in self.branches
            Path(wt).mkdir(parents=True)
            shutil.copy(self.main / "PROJECT_STATUS.md", Path(wt) / "PROJECT_STATUS.md")
            self.branches.add(branch)
            return ok
        if args[:2] == ["rev-parse", "--short"]:
            return subprocess.CompletedProcess(args, 0, "abc1234\n", "")
        if args[:2] == ["worktree", "remove"]:
            shutil.rmtree(args[-1], ignore_errors=True)
            return ok
        if args[:2] == ["branch", "-D"]:
            self.branches.discard(args[2])
            return ok
        return ok                                            # add, commit


def _tree(root: Path) -> dict[str, str]:
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob("*") if p.is_file()}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    pp = tool("propose")
    main = tmp_path / "the_claude_new"
    (main / ".git").mkdir(parents=True)
    (main / "PROJECT_STATUS.md").write_text(STATUS, encoding="utf-8")
    git = FakeGit(main)
    monkeypatch.setattr(pp, "run_git", git)
    sent: list[tuple] = []
    monkeypatch.setattr(pp, "notify", lambda s, level, title, text, key=None: sent.append((level, title, text, key)))
    s = load_settings()
    s = s.model_copy(update={"paths": s.paths.model_copy(update={"data_dir": str(tmp_path / "data"),
                                                                 "logs_dir": str(tmp_path / "logs")})})
    return pp, s, main, git, sent


def test_a_proposal_is_a_branch_a_document_and_a_status_row_outside_the_checkout(env):
    pp, s, main, git, sent = env
    before = _tree(main)
    now = now_ms()
    rec = pp.propose(s, slug="xau-asia-filter", title="Skip the Asia session for XAU", body=BODY, pair="xauusd",
                     review_id="20260927T043000Z_weekly", root=main, now=now)
    name = rec["id"]
    assert name.endswith("-xau-asia-filter") and rec["branch"] == f"proposal/{name}"
    wt = main.parent / "the_claude_new_wt" / f"proposal-{name}"
    assert rec["worktree"] == str(wt) and rec["status"] == "awaiting_user" and rec["commit"] == "abc1234"
    assert _tree(main) == before                                   # the production checkout is untouched
    doc = (wt / "docs" / "proposals" / f"{name}.md").read_text(encoding="utf-8")
    assert doc.startswith("# Proposal: Skip the Asia session for XAU") and "## Test plan" in doc and "XAUUSD" in doc
    status = (wt / "PROJECT_STATUS.md").read_text(encoding="utf-8").splitlines()
    i = status.index("| H2 | second | P2 | ⏳ |")
    assert status[i + 1].startswith(f"| P-{name} | Proposal \"Skip the Asia session for XAU\" (XAUUSD)")
    assert status[i + 2] == ""                                     # the row closes the table, nothing else moved
    # every git call that writes runs in the new worktree (commit) or creates it (worktree add) — never a commit in main
    for args, cwd in git.calls:
        if args[0] in ("add", "commit"):
            assert cwd == wt
    assert any(args[:2] == ("worktree", "add") and args[-1] == "main" for args, _ in git.calls)
    line = json.loads((s.paths.shared() / "proposals.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert line["id"] == name and line["status"] == "awaiting_user" and line["pair"] == "XAUUSD"
    assert line["review_id"] == "20260927T043000Z_weekly"
    assert len(sent) == 1 and sent[0][0] == "info" and name in sent[0][2]


def test_an_existing_worktree_or_branch_is_refused(env, capsys):
    pp, s, main, git, sent = env
    args = ["--slug", "same-slug", "--title", "t", "--body", BODY]
    assert pp.main(args, settings=s, root=main) == 0
    assert pp.main(args, settings=s, root=main) == 2                # the worktree exists
    assert "refused" in capsys.readouterr().out
    name = json.loads((s.paths.shared() / "proposals.jsonl").read_text(encoding="utf-8").splitlines()[-1])["id"]
    shutil.rmtree(main.parent / "the_claude_new_wt" / f"proposal-{name}")
    assert pp.main(args, settings=s, root=main) == 2                # … and the branch still exists
    assert len((s.paths.shared() / "proposals.jsonl").read_text(encoding="utf-8").splitlines()) == 1


@pytest.mark.parametrize("args, why", [
    (["--slug", "Bad_Slug", "--title", "t", "--body", BODY], "slug"),
    (["--slug", "ab", "--title", "t", "--body", BODY], "slug"),
    (["--slug", "-lead", "--title", "t", "--body", BODY], "slug"),
    (["--slug", "ok-slug", "--title", "t", "--body", "## Problem\nonly this"], "section"),
    (["--slug", "ok-slug", "--title", "t", "--body", BODY, "--pair", "DOGEUSDT"], "pair"),
    (["--slug", "ok-slug", "--title", "", "--body", BODY], "title"),
    (["--slug", "ok-slug", "--title", "t"], "body"),
])
def test_invalid_requests_exit_3_and_touch_nothing(env, args, why, capsys):
    pp, s, main, git, sent = env
    assert pp.main(args, settings=s, root=main) == 3
    assert capsys.readouterr().out.startswith("invalid")
    assert not any(a[:2] == ("worktree", "add") for a, _ in git.calls)
    assert not (s.paths.shared() / "proposals.jsonl").exists() and not sent


def test_a_failed_commit_removes_only_what_this_run_created(env, tmp_path):
    pp, s, main, _, sent = env
    git = FakeGit(main, fail_on="commit")
    pp.run_git = git
    before = _tree(main)
    assert pp.main(["--slug", "will-fail", "--title", "t", "--body", BODY], settings=s, root=main) == 1
    removed = [a for a, _ in git.calls if a[:2] in (("worktree", "remove"), ("branch", "-D"))]
    assert len(removed) == 2 and all("will-fail" in " ".join(a) for a in removed)
    assert not list((main.parent / "the_claude_new_wt").glob("proposal-*"))
    assert _tree(main) == before and not (s.paths.shared() / "proposals.jsonl").exists() and not sent


def test_dry_run_and_escaped_line_breaks(env, capsys):
    pp, s, main, git, sent = env
    one_line = BODY.replace("\n", "\\n")
    assert pp.main(["--slug", "dry-one", "--title", "t", "--body", one_line, "--dry-run"], settings=s, root=main) == 0
    assert "would create" in capsys.readouterr().out
    assert not any(a[:2] == ("worktree", "add") for a, _ in git.calls) and not sent
    assert pp.normalize_body(one_line) == BODY.strip()
    assert pp.missing_sections(pp.normalize_body(one_line)) == []


def test_body_file_and_the_status_section_fallback(env, tmp_path):
    pp, s, main, git, sent = env
    f = tmp_path / "body.md"
    f.write_text(BODY, encoding="utf-8")
    (main / "PROJECT_STATUS.md").write_text("# Project Status\r\n\r\n## Overview\r\nx\r\n", encoding="utf-8", newline="")
    assert pp.main(["--slug", "from-file", "--title", "t", "--body-file", str(f)], settings=s, root=main) == 0
    wt = next((main.parent / "the_claude_new_wt").glob("proposal-*"))
    text = (wt / "PROJECT_STATUS.md").read_bytes().decode("utf-8")
    assert "## Proposals awaiting the owner\r\n| # | Action | Needed by | Status |" in text and "\n| P-" in text
    assert "\r\n" in text and "\n\n" not in text.replace("\r\n", "")                  # CRLF kept
