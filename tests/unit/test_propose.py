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
    """Just enough git: the common dir of the stand-in main checkout; branches as lists of commit ids (``rev-parse``
    of a ref gives its tip's sha, ``rev-list --count main..X`` the commits X has that main has not); ``worktree add``
    from a sha (a copy of the checkout's PROJECT_STATUS.md, like a real checkout of main); add; ``commit`` adds
    ``commit_adds`` commits to the worktree's branch. Records every call with its cwd."""

    def __init__(self, main: Path, *, fail_on: str | None = None) -> None:
        self.main, self.fail_on = main, fail_on
        self.commits: dict[str, list[str]] = {"main": ["m0", "m1"]}
        self.worktrees: dict[Path, str] = {}
        self.commit_adds = 1
        self.calls: list[tuple[tuple[str, ...], Path]] = []

    @property
    def branches(self) -> set[str]:
        return set(self.commits)

    def tip(self, branch: str) -> str:
        return hashlib.sha1(self.commits[branch][-1].encode()).hexdigest()

    def history(self, rev: str) -> list[str]:
        if rev.startswith("refs/heads/"):
            return self.commits[rev.removeprefix("refs/heads/")]
        return next(c for b, c in self.commits.items() if self.tip(b) == rev)

    def __call__(self, args: list[str], cwd: Path) -> subprocess.CompletedProcess:
        self.calls.append((tuple(args), Path(cwd)))
        ok = subprocess.CompletedProcess(args, 0, "", "")
        if self.fail_on and args[0] == self.fail_on:
            return subprocess.CompletedProcess(args, 1, "", f"fatal: {self.fail_on} failed")
        if args[:3] == ["rev-parse", "--path-format=absolute", "--git-common-dir"]:
            return subprocess.CompletedProcess(args, 0, str(self.main / ".git") + "\n", "")
        if args[:3] == ["rev-parse", "--verify", "--quiet"]:
            ref = args[3].removeprefix("refs/heads/").removesuffix("^{commit}")
            return subprocess.CompletedProcess(args, 0, self.tip(ref) + "\n", "") if ref in self.commits else \
                subprocess.CompletedProcess(args, 1, "", "")
        if args[:2] == ["rev-parse", "--short"]:
            full = self.tip(self.worktrees[Path(cwd)]) if args[2] == "HEAD" else args[2]
            return subprocess.CompletedProcess(args, 0, full[:7] + "\n", "")
        if args[:2] == ["rev-list", "--count"]:
            assert args[-1] == "--"
            a, b = args[2].split("..")
            seen = set(self.history(a))
            return subprocess.CompletedProcess(args, 0, f"{sum(c not in seen for c in self.history(b))}\n", "")
        if args[:2] == ["worktree", "add"]:
            _, _, _, branch, wt, base = args
            Path(wt).mkdir(parents=True)
            shutil.copy(self.main / "PROJECT_STATUS.md", Path(wt) / "PROJECT_STATUS.md")
            self.commits[branch] = list(self.history(base))
            self.worktrees[Path(wt)] = branch
            return ok
        if args[0] == "commit":
            branch = self.worktrees[Path(cwd)]
            self.commits[branch] += [f"{branch}#{len(self.commits[branch]) + i}" for i in range(self.commit_adds)]
            return ok
        if args[:2] == ["worktree", "remove"]:
            shutil.rmtree(args[-1], ignore_errors=True)
            return ok
        if args[:2] == ["branch", "-D"]:
            self.commits.pop(args[2], None)
            return ok
        return ok                                            # add


def _tree(root: Path) -> dict[str, str]:
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob("*") if p.is_file()}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.delenv("TS_OPERATOR_SESSION", raising=False)          # a human's call unless a test says otherwise
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
    assert rec["worktree"] == str(wt) and rec["status"] == "awaiting_user"
    assert rec["commit"] == git.tip(rec["branch"])[:7] and rec["commits_not_in_main"] == 1
    assert rec["base"] == "main" and rec["base_sha"] == git.tip("main")[:7]
    assert _tree(main) == before                                   # the production checkout is untouched
    doc = (wt / "docs" / "proposals" / f"{name}.md").read_text(encoding="utf-8")
    assert doc.startswith("# Proposal: Skip the Asia session for XAU") and "## Test plan" in doc and "XAUUSD" in doc
    assert f"| base | `main` ({git.tip('main')[:7]}) |" in doc and "| commits not in main | 1 (" in doc
    status = (wt / "PROJECT_STATUS.md").read_text(encoding="utf-8").splitlines()
    i = status.index("| H2 | second | P2 | ⏳ |")
    assert status[i + 1].startswith(f"| P-{name} | Proposal \"Skip the Asia session for XAU\" (XAUUSD)")
    assert status[i + 2] == ""                                     # the row closes the table, nothing else moved
    # every git call that writes runs in the new worktree (commit) or creates it (worktree add) — never a commit in main
    for args, cwd in git.calls:
        if args[0] in ("add", "commit"):
            assert cwd == wt
    # from main's commit as it was counted (a sha, so the base cannot move in between)
    assert any(args[:2] == ("worktree", "add") and args[-1] == git.tip("main") for args, _ in git.calls)
    line = json.loads((s.paths.shared() / "proposals.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert line["id"] == name and line["status"] == "awaiting_user" and line["pair"] == "XAUUSD"
    assert line["review_id"] == "20260927T043000Z_weekly"
    assert line["base"] == "main" and line["base_sha"] == rec["base_sha"] and line["commits_not_in_main"] == 1
    assert len(sent) == 1 and sent[0][0] == "info" and name in sent[0][2]
    assert f"from main ({rec['base_sha']}), 1 commit(s) not in main" in sent[0][2]


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


# --------------------------------------------------------------------------- the base, sessions, a missing record
def test_a_session_proposes_from_main_only(env, monkeypatch, capsys):
    pp, s, main, git, sent = env
    git.commits["feat/wip"] = ["m0", "m1", "w1", "w2"]            # a feature branch with unmerged commits
    monkeypatch.setenv("TS_OPERATOR_SESSION", "1")
    for base in ("feat/wip", "master", "refs/heads/main"):
        assert pp.main(["--slug", "from-elsewhere", "--title", "t", "--body", BODY, "--base", base],
                       settings=s, root=main) == 3
        assert "proposes from main only" in capsys.readouterr().out
    assert not any(a[:2] == ("worktree", "add") for a, _ in git.calls) and not sent
    assert pp.main(["--slug", "from-main", "--title", "t", "--body", BODY], settings=s, root=main) == 0
    assert "from main (" in capsys.readouterr().out


def test_another_base_is_stated_in_the_document_the_notification_and_the_record(env, capsys):
    """A human may start from another branch, but what the merge would bring into main is never hidden."""
    pp, s, main, git, sent = env
    git.commits["feat/wip"] = ["m0", "m1", "w1", "w2"]
    assert pp.main(["--slug", "on-wip", "--title", "t", "--body", BODY, "--base", "feat/wip"], settings=s,
                   root=main) == 0
    assert "from feat/wip (" in capsys.readouterr().out
    line = json.loads((s.paths.shared() / "proposals.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert line["base"] == "feat/wip" and line["base_sha"] == git.tip("feat/wip")[:7]
    assert line["commits_not_in_main"] == 3                       # w1, w2 and the proposal's own commit
    wt = Path(line["worktree"])
    doc = (wt / line["doc"]).read_text(encoding="utf-8")
    assert f"| base | `feat/wip` ({line['base_sha']}) |" in doc
    assert "| commits not in main | 3: this document's commit and 2 from the base `feat/wip`" in doc
    assert "the merge also brings 2 commit(s) of `feat/wip` into main" in doc
    assert sent[-1][0] == "warn" and "3 commit(s) not in main - 2 of them from feat/wip" in sent[-1][2]


def test_a_session_branch_with_more_than_its_own_commit_is_removed_and_refused(env, monkeypatch, capsys):
    pp, s, main, git, sent = env
    monkeypatch.setenv("TS_OPERATOR_SESSION", "1")
    git.commit_adds = 2                                            # e.g. a hook that commits once more
    assert pp.main(["--slug", "two-commits", "--title", "t", "--body", BODY], settings=s, root=main) == 2
    assert "2 commits not in main" in capsys.readouterr().out
    removed = [a for a, _ in git.calls if a[:2] in (("worktree", "remove"), ("branch", "-D"))]
    assert len(removed) == 2 and all("two-commits" in " ".join(a) for a in removed)
    assert not list((main.parent / "the_claude_new_wt").glob("proposal-*")) and git.branches == {"main"}
    assert not (s.paths.shared() / "proposals.jsonl").exists() and not sent


def test_a_session_never_reads_a_body_file(env, monkeypatch, tmp_path, capsys):
    pp, s, main, git, sent = env
    f = tmp_path / "body.md"
    f.write_text(BODY, encoding="utf-8")
    monkeypatch.setenv("TS_OPERATOR_SESSION", "1")
    opened: list[Path] = []
    monkeypatch.setattr(pp.Path, "read_text", lambda self, *a, **k: opened.append(self) or "")
    assert pp.main(["--slug", "from-file", "--title", "t", "--body-file", str(f)], settings=s, root=main) == 3
    assert "refused in an operator session" in capsys.readouterr().out and not opened
    assert not any(a[:2] == ("worktree", "add") for a, _ in git.calls) and not sent


def test_a_session_proposal_is_recorded_as_the_session_whatever_actor_it_names(env, monkeypatch):
    """The document header, the commit message and proposals.jsonl name operator-session:<review id> (exported by the
    session runner) — never an actor the session chose, such as 'owner' or 'monitor'."""
    pp, s, main, git, sent = env
    monkeypatch.setenv("TS_OPERATOR_SESSION", "1")
    monkeypatch.setenv("TS_OPERATOR_REVIEW_ID", "20260927T043000Z_daily")
    assert pp.main(["--slug", "who-did-it", "--title", "t", "--body", BODY, "--actor", "owner"], settings=s,
                   root=main) == 0
    line = json.loads((s.paths.shared() / "proposals.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert line["actor"] == "operator-session:20260927T043000Z_daily"
    doc = (Path(line["worktree"]) / line["doc"]).read_text(encoding="utf-8")
    assert " by operator-session:20260927T043000Z_daily" in doc and " by owner" not in doc
    commit = next(a for a, _ in git.calls if a[0] == "commit")
    assert "By operator-session:20260927T043000Z_daily." in commit[-1]
    monkeypatch.delenv("TS_OPERATOR_REVIEW_ID")                    # none exported: the plain session actor
    assert pp.propose(s, slug="no-review-id", title="t", body=BODY, actor="monitor", root=main,
                      dry_run=True)["actor"] == "operator-session"
    monkeypatch.setenv("TS_OPERATOR_REVIEW_ID", "not a review id")
    assert pp.propose(s, slug="bad-review-id", title="t", body=BODY, root=main, dry_run=True)["actor"] == \
        "operator-session"
    monkeypatch.delenv("TS_OPERATOR_SESSION")                      # a human's actor is kept, and still checked
    assert pp.propose(s, slug="by-hand", title="t", body=BODY, actor="owner", root=main,
                      dry_run=True)["actor"] == "owner"
    assert pp.main(["--slug", "by-hand", "--title", "t", "--body", BODY, "--actor", "bad actor!"], settings=s,
                   root=main) == 3


@pytest.mark.parametrize("name", [".env", ".env.local", ".ENV", "claude-credentials.md", "Credentials.json"])
def test_a_body_file_named_like_a_secret_is_refused_unread(env, tmp_path, capsys, name):
    pp, s, main, git, sent = env
    f = tmp_path / name
    f.write_text(BODY + "\nSECRET_VALUE_123\n", encoding="utf-8")         # would pass the section check
    assert pp.main(["--slug", "from-secret", "--title", "t", "--body-file", str(f)], settings=s, root=main) == 3
    out = capsys.readouterr().out
    assert out.startswith("invalid") and ".env* or *credential*" in out and "SECRET_VALUE_123" not in out
    assert not any(a[:2] == ("worktree", "add") for a, _ in git.calls) and not sent


def test_a_failed_record_append_keeps_the_proposal_warns_and_notifies(env, monkeypatch, capsys):
    pp, s, main, git, sent = env

    def held(path, record, lock):
        raise OSError(f"{lock} is held - {path.name} not appended")

    monkeypatch.setattr(pp, "append_jsonl", held)
    assert pp.main(["--slug", "no-record", "--title", "t", "--body", BODY], settings=s, root=main) == 0
    out = capsys.readouterr().out
    assert "proposal " in out and " created: branch proposal/" in out
    assert "warning: the proposal exists, but its data/shared/proposals.jsonl record is missing" in out
    wt = next((main.parent / "the_claude_new_wt").glob("proposal-*-no-record"))     # the branch is the proposal
    assert (wt / "docs" / "proposals" / f"{wt.name.removeprefix('proposal-')}.md").exists()
    assert any(b.endswith("-no-record") for b in git.branches)
    assert not any(a[:2] in (("worktree", "remove"), ("branch", "-D")) for a, _ in git.calls)
    assert len(sent) == 1 and sent[0][0] == "warn" and "record is MISSING" in sent[0][2]
