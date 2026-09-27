"""A proposal (§3.8): everything a review session may not change itself becomes a branch + a document the owner decides.

    python tools/propose.py --slug tighter-xau-sessions --title "..." --body "..." [--pair XAUUSD]
                            [--review-id 20260927T043000Z_daily] [--base main] [--dry-run]
    (--body-file FILE instead of --body; a body without real line breaks may use the two characters \\n for them)

What it does, from the checkout it lives in (never inside the production working tree):

1. ``git worktree add -b proposal/<date>-<slug> <main checkout>_wt/proposal-<date>-<slug> <base sha>`` — a new branch
   and worktree next to the others (``C:\\the_claude_new_wt\\…``), from the base branch's commit as it was counted;
   the production working tree is not touched;
2. there: ``docs/proposals/<date>-<slug>.md`` (header + the body, which must carry the sections Problem, Numbers,
   Proposed change, Risk, Test plan) and one row in ``PROJECT_STATUS.md`` ("Human actions pending"), committed on the
   branch;
3. one line in ``data/shared/proposals.jsonl`` (``status: awaiting_user``; the dashboard's Proposals tab) and a
   notification.

The document header, the notification and the record name the base, its short sha and the number of commits on the
branch that are not in main (``git rev-list --count main..<branch>``): accepting = merging brings all of them into
main, so a base other than main is stated, never hidden.

An operator session (``TS_OPERATOR_SESSION=1`` in its environment, set by the session runner) proposes from main
only: another ``--base`` is invalid, ``--body-file`` is invalid (a file this tool opened would get around the
session's Read denials — sessions pass ``--body``), and a branch that ends up with anything but its one proposal
commit not in main is removed again and refused. A human's ``--body-file`` may not be named ``.env*`` or
``*credential*``.

The owner accepts by merging the branch (the document becomes a pending task in PROJECT_STATUS.md) or rejects by
``git worktree remove <dir>`` + ``git branch -D proposal/<date>-<slug>``. An existing worktree or branch of the same
name is refused (pick another slug). A failure after the worktree was created removes what this run created; a
failed ``proposals.jsonl`` append does not (the branch is the proposal): it is logged, the notification says the
record is missing, and the success line carries a warning.
Exit codes: 0 created (or would be, with --dry-run; also when only the jsonl record is missing), 1 git or file error,
2 refused (exists; a session branch with more than its own commit), 3 invalid request.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tradingsystem.core.filelock import FileLock, locks_dir  # noqa: E402
from tradingsystem.core.settings import Settings, load_settings  # noqa: E402
from tradingsystem.core.timeutil import iso  # noqa: E402
from tradingsystem.core.timeutil import now_ms as _now_ms  # noqa: E402

EXIT_OK, EXIT_ERROR, EXIT_REFUSED, EXIT_INVALID = 0, 1, 2, 3
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,38}[a-z0-9]$")          # 3-40 characters, no leading/trailing hyphen
PAIR_RE = re.compile(r"^[A-Z0-9]{2,20}$")
REVIEW_RE = re.compile(r"^[\w.:+-]{1,120}$")
ACTOR_RE = re.compile(r"^[\w.@:+-]{1,40}$")
BRANCH_BASE_RE = re.compile(r"^[A-Za-z0-9._/-]{1,80}$")
MAIN_BRANCH = "main"                            # what "not in main" is counted against; a session's only base
SESSION_ENV = "TS_OPERATOR_SESSION"             # "1" in every operator session's environment (the session runner)
MAX_TITLE = 120
MAX_BODY = 20_000
MAX_BODY_FILE_BYTES = 64 * 1024
# the body's required headings (any level), each accepted under a few names
SECTIONS = {"Problem": ("problem",), "Numbers": ("numbers", "evidence"),
            "Proposed change": ("proposed change", "proposed diff", "proposed text", "proposal", "change"),
            "Risk": ("risk",), "Test plan": ("test plan", "tests", "verification")}
HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$", re.M)
STATUS_SECTION = "## Human actions pending"
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class Invalid(Exception):
    """The request cannot be understood (exit 3)."""


class Refused(Exception):
    """A well-formed request that is not done — the branch or worktree exists (exit 2)."""


class GitError(Exception):
    """git failed (exit 1)."""


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:           # argparse would exit 2, which means "refused" here
        raise Invalid(f"{self.prog}: {message}")


# --------------------------------------------------------------------------- git (replaced in tests)
def run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=120, creationflags=_NO_WINDOW)


def _git(args: list[str], cwd: Path) -> str:
    p = run_git(args, cwd)
    if p.returncode != 0:
        raise GitError(f"git {' '.join(args[:3])}: {(p.stderr or p.stdout or '').strip()[:300]}")
    return p.stdout or ""


def _exists_ref(ref: str, cwd: Path) -> bool:
    return run_git(["rev-parse", "--verify", "--quiet", ref], cwd).returncode == 0


def _ref_sha(ref: str, cwd: Path) -> str | None:
    """The full commit sha a ref points at; None when there is no such ref."""
    p = run_git(["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], cwd)
    sha = (p.stdout or "").strip() if p.returncode == 0 else ""
    return sha or None


def not_in_main(rev: str, cwd: Path) -> int:
    """How many commits ``rev`` has that main does not (``git rev-list --count main..rev``): what a merge brings in."""
    out = _git(["rev-list", "--count", f"refs/heads/{MAIN_BRANCH}..{rev}", "--"], cwd).strip()
    try:
        return int(out)
    except ValueError:
        raise GitError(f"git rev-list --count: unexpected output {out[:80]!r}") from None


def in_session() -> bool:
    """Run by an operator session (the session runner sets the marker in the session's environment)."""
    return os.environ.get(SESSION_ENV) == "1"


def body_file_refusal(path: str) -> str | None:
    """Why ``--body-file`` may not read ``path`` (None: it may): never in a session (it passes ``--body``; a file this
    tool opened would get around the session's Read denials), and never a file named like a secret. Decided before
    the file is opened; the reason never shows its content."""
    if in_session():
        return "refused in an operator session - pass the text with --body"
    names = {Path(path).name.lower()}
    try:
        names.add(Path(path).resolve().name.lower())          # a link's target counts too
    except (OSError, RuntimeError):
        pass
    if any(n.startswith(".env") or "credential" in n for n in names):
        return "a file named .env* or *credential* is never a proposal body"
    return None


def main_checkout(root: Path) -> Path:
    """The repository's main working tree (the parent of the shared ``.git``), from any of its worktrees."""
    common = Path(_git(["rev-parse", "--path-format=absolute", "--git-common-dir"], root).strip())
    if common.name != ".git":
        raise GitError(f"unexpected git common dir {common} (a bare repository?)")
    return common.parent


# --------------------------------------------------------------------------- notifications (replaced in tests)
def notify(s: Settings, level: str, title: str, text: str, key: str | None = None) -> None:
    try:
        from tradingsystem.core import notify as nt
    except Exception:  # noqa: BLE001 — the proposal exists; the notifier ships with the same phase
        return
    try:
        nt.notify(s, level, title, text, key=key)
        nt.flush(15.0)
    except Exception:  # noqa: BLE001
        pass


def setup_log(s: Settings) -> None:
    from tradingsystem.core.logsetup import setup_logging
    setup_logging("propose", logs_dir=s.paths.logs(), console=False, secret_env_names=s.secret_env_names())


# --------------------------------------------------------------------------- text
def normalize_body(body: str) -> str:
    """A body passed on one command line may carry ``\\n`` for its line breaks (a Bash argument without them)."""
    if "\n" not in body and "\\n" in body:
        body = body.replace("\\r\\n", "\n").replace("\\n", "\n")
    return body.replace("\r\n", "\n").replace("\r", "\n").strip()


def missing_sections(body: str) -> list[str]:
    heads = [h.strip().lower().rstrip(":") for h in HEADING_RE.findall(body)]
    return [name for name, alts in SECTIONS.items() if not any(h.startswith(a) for h in heads for a in alts)]


def _cell(text: str) -> str:
    return " ".join(text.split()).replace("|", "/")


def commits_note(n: int, base: str) -> str:
    """The "commits not in main" line: 1 is the proposal's own commit; more come from the base."""
    if n == 1:
        return "1 (this document's commit only)"
    return (f"{n}: this document's commit and {n - 1} from the base `{base}` - merging brings ALL of them into "
            f"{MAIN_BRANCH}; review them first (`git log {MAIN_BRANCH}..<branch>`)")


def document(*, name: str, title: str, body: str, pair: str | None, review_id: str | None, branch: str,
             worktree: Path, now: int, actor: str, base: str, base_sha: str, commits: int) -> str:
    return (f"# Proposal: {title}\n\n"
            "| | |\n|---|---|\n"
            f"| id | `{name}` |\n| created | {iso(now)} by {actor}"
            + (f" (review `{review_id}`)" if review_id else "") + " |\n"
            f"| pair | {pair or 'all'} |\n| branch | `{branch}` |\n| base | `{base}` ({base_sha}) |\n"
            f"| commits not in {MAIN_BRANCH} | {commits_note(commits, base)} |\n| status | awaiting the owner |\n\n"
            f"{body}\n\n---\n"
            f"Accept: merge `{branch}` (this document then stands as a pending task in PROJECT_STATUS.md"
            + (f"; the merge also brings {commits - 1} commit(s) of `{base}` into {MAIN_BRANCH}" if commits != 1
               else "") + "). "
            f"Reject: `git worktree remove {worktree}` and `git branch -D {branch}`.\n")


def status_row(*, name: str, title: str, pair: str | None, branch: str, doc_rel: str) -> str:
    return (f"| P-{name} | Proposal \"{_cell(title)}\"{' (' + pair + ')' if pair else ''}: read `{doc_rel}` on branch "
            f"`{branch}`; accept = merge the branch, reject = `git worktree remove` + `git branch -D` | review | "
            "⏳ awaiting the owner |")


def add_status_row(text: str, row: str) -> str:
    """The row goes at the end of the "Human actions pending" table (a section of its own when there is none);
    the file's line endings are kept."""
    nl = "\r\n" if "\r\n" in text else "\n"
    lines = text.split(nl)
    start = next((i for i, ln in enumerate(lines) if ln.strip() == STATUS_SECTION or ln.startswith(STATUS_SECTION)),
                 None)
    if start is not None:
        i = start + 1
        while i < len(lines) and not lines[i].startswith("|"):
            if lines[i].startswith("#"):
                break
            i += 1
        if i < len(lines) and lines[i].startswith("|"):
            while i < len(lines) and lines[i].startswith("|"):
                i += 1
            lines.insert(i, row)
            return nl.join(lines)
    tail = ["", "## Proposals awaiting the owner", "| # | Action | Needed by | Status |", "|---|---|---|---|", row, ""]
    head = lines[:-1] if lines and lines[-1] == "" else lines
    return nl.join([*head, *tail])


def append_jsonl(path: Path, record: dict[str, Any], lock: Path) -> None:
    """One line under a lock; a torn last line (a crash mid-append) is closed first so the new line stays readable."""
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False, default=str, separators=(",", ":")) + "\n"
    with FileLock(lock).hold(timeout=10) as got:
        if not got:
            raise OSError(f"{lock} is held — {path.name} not appended")
        with open(path, "a+b") as fh:
            fh.seek(0, os.SEEK_END)
            if fh.tell() > 0:
                fh.seek(-1, os.SEEK_END)
                if fh.read(1) != b"\n":
                    line = "\n" + line
            fh.seek(0, os.SEEK_END)
            fh.write(line.encode("utf-8"))
            fh.flush()
            os.fsync(fh.fileno())


# --------------------------------------------------------------------------- the proposal
def propose(s: Settings, *, slug: str, title: str, body: str, pair: str | None = None, review_id: str | None = None,
            base: str = MAIN_BRANCH, actor: str = "operator", dry_run: bool = False, root: Path = ROOT,
            now: int | None = None) -> dict[str, Any]:
    """Create the proposal; returns what was (or would be) created. Raises Invalid / Refused / GitError / OSError."""
    slug = (slug or "").strip()
    if not SLUG_RE.fullmatch(slug):
        raise Invalid(f"--slug {slug!r}: 3-40 lowercase letters, digits and hyphens (not at the ends)")
    title = " ".join((title or "").split())
    if not title or len(title) > MAX_TITLE:
        raise Invalid(f"--title: one line of 1-{MAX_TITLE} characters")
    if pair is not None:
        pair = pair.strip().upper()
        if not PAIR_RE.fullmatch(pair) or pair not in s.pairs:
            raise Invalid(f"--pair {pair!r}: not a configured pair ({', '.join(sorted(s.pairs))})")
    if review_id is not None and not REVIEW_RE.fullmatch(review_id):
        raise Invalid(f"--review-id {review_id!r}: letters, digits and _ . : + - only")
    if not ACTOR_RE.fullmatch(actor or ""):
        raise Invalid(f"--actor {actor!r}: letters, digits and . @ : + - only")
    if not BRANCH_BASE_RE.fullmatch(base or "") or base.startswith("-") or ".." in base:
        raise Invalid(f"--base {base!r}: not a branch name")
    session = in_session()
    if session and base != MAIN_BRANCH:
        # accepting = merging: a proposal on another base would bring that branch's unreviewed commits into main
        raise Invalid(f"--base {base!r}: an operator session proposes from {MAIN_BRANCH} only (leave --base out)")
    body = normalize_body(body or "")
    if not body:
        raise Invalid("the body is empty")
    if len(body) > MAX_BODY:
        raise Invalid(f"the body has {len(body)} characters (max {MAX_BODY})")
    missing = missing_sections(body)
    if missing:
        raise Invalid("the body lacks the section heading(s) " + ", ".join(f"'## {m}'" for m in missing))
    now = _now_ms() if now is None else int(now)
    name = f"{time.strftime('%Y-%m-%d', time.gmtime(now / 1000))}-{slug}"
    branch = f"proposal/{name}"
    main = main_checkout(root)
    wt = main.parent / f"{main.name}_wt" / f"proposal-{name}"
    doc_rel = f"docs/proposals/{name}.md"
    plan = {"id": name, "slug": slug, "title": title, "pair": pair, "branch": branch, "worktree": str(wt),
            "doc": doc_rel, "base": base, "review_id": review_id, "actor": actor}
    if wt.exists():
        raise Refused(f"the worktree {wt} exists already — pick another slug")
    if _exists_ref(f"refs/heads/{branch}", root):
        raise Refused(f"the branch {branch} exists already — pick another slug")
    base_full = _ref_sha(f"refs/heads/{base}", root)
    if not base_full:
        raise Invalid(f"--base {base}: no such branch")
    base_sha = _git(["rev-parse", "--short", base_full], root).strip()
    expected = not_in_main(base_full, root) + 1          # the base's own + this proposal's commit
    plan.update(base_sha=base_sha, commits_not_in_main=expected)
    if dry_run:
        return {**plan, "dry_run": True}
    # from the sha just counted, not the name: the base cannot move between the count and the branch
    _git(["worktree", "add", "-b", branch, str(wt), base_full], root)
    try:
        doc = wt / doc_rel
        doc.parent.mkdir(parents=True, exist_ok=True)
        doc.write_text(document(name=name, title=title, body=body, pair=pair, review_id=review_id, branch=branch,
                                worktree=wt, now=now, actor=actor, base=base, base_sha=base_sha, commits=expected),
                       encoding="utf-8", newline="\n")
        paths = [doc_rel]
        st = wt / "PROJECT_STATUS.md"
        if st.exists():
            text = st.read_bytes().decode("utf-8")
            st.write_bytes(add_status_row(text, status_row(name=name, title=title, pair=pair, branch=branch,
                                                           doc_rel=doc_rel)).encode("utf-8"))
            paths.append("PROJECT_STATUS.md")
        _git(["add", "--", *paths], wt)
        msg = [f"proposal: {title}", f"{doc_rel} — awaiting the owner (accept = merge, reject = delete the branch)."
               + (f"\n\nFrom review {review_id} ({actor})." if review_id else f"\n\nBy {actor}.")
               + "\n\nCo-Authored-By: Claude <noreply@anthropic.com>"]
        _git(["commit", "-m", msg[0], "-m", msg[1]], wt)
        sha = _git(["rev-parse", "--short", "HEAD"], wt).strip()
        commits = not_in_main(f"refs/heads/{branch}", root)
        if session and commits != 1:
            raise Refused(f"the branch {branch} has {commits} commits not in {MAIN_BRANCH} (a session's proposal "
                          "carries exactly its own) — removed again")
    except BaseException:
        # only what this run created: the new worktree and its branch
        run_git(["worktree", "remove", "--force", str(wt)], root)
        run_git(["branch", "-D", branch], root)
        raise
    record = {"ts": now, "time": iso(now), **plan, "commits_not_in_main": commits, "commit": sha,
              "status": "awaiting_user"}
    jsonl_error = None
    try:
        append_jsonl(s.paths.shared() / "proposals.jsonl", record, locks_dir(s) / "proposals.lock")
    except OSError as exc:
        # the branch is the proposal: keep it, say the record is missing (a retry would only duplicate the branch)
        jsonl_error = f"{type(exc).__name__}: {exc}"[:300]
        logging.getLogger("propose").error("proposal %s: proposals.jsonl not appended: %s", name, jsonl_error)
    merged = f"{commits} commit(s) not in {MAIN_BRANCH}" + (f" - {commits - 1} of them from {base}" if commits != 1
                                                            else "")
    missing = (f" The proposals.jsonl record is MISSING ({jsonl_error}): the dashboard and the next review pack do not "
               "list this proposal." if jsonl_error else "")
    notify(s, "warn" if jsonl_error or commits != 1 else "info", f"proposal: {title}"[:120],
           f"{pair or 'all pairs'} — branch {branch} ({sha}) from {base} ({base_sha}), {merged}; read {doc_rel} in "
           f"{wt}. Accept = merge, reject = delete.{missing}",
           key=f"proposal_{name}")
    return {**record, "jsonl_error": jsonl_error} if jsonl_error else record


# --------------------------------------------------------------------------- CLI
def main(argv: list[str] | None = None, *, settings: Settings | None = None, root: Path = ROOT) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = _Parser(prog="propose.py", description="create a proposal branch + document for the owner")
    ap.add_argument("--slug", required=True)
    ap.add_argument("--title", required=True)
    body = ap.add_mutually_exclusive_group(required=True)
    body.add_argument("--body", help="the proposal markdown (sections Problem, Numbers, Proposed change, Risk, "
                                     "Test plan)")
    body.add_argument("--body-file", dest="body_file", help="read the markdown from this file (not in an operator "
                                                             "session)")
    ap.add_argument("--pair")
    ap.add_argument("--review-id", dest="review_id")
    ap.add_argument("--base", default=MAIN_BRANCH, help=f"the branch to start from ({MAIN_BRANCH}; an operator "
                                                        "session may not change it)")
    ap.add_argument("--actor", default="operator")
    ap.add_argument("--dry-run", action="store_true")
    try:
        a = ap.parse_args(argv)
        text = a.body
        if a.body_file is not None:
            why = body_file_refusal(a.body_file)
            if why:
                raise Invalid(f"--body-file: {why}")
            f = Path(a.body_file)
            if not f.is_file() or f.stat().st_size > MAX_BODY_FILE_BYTES:
                raise Invalid(f"--body-file {a.body_file}: not a readable file of at most {MAX_BODY_FILE_BYTES} bytes")
            text = f.read_text(encoding="utf-8", errors="replace")
        s = settings or load_settings()
    except Invalid as exc:
        print(f"invalid: {exc}")
        return EXIT_INVALID
    except SystemExit as exc:                          # --help
        return int(exc.code or 0)
    if not a.dry_run and settings is None:
        try:
            setup_log(s)
        except OSError:
            pass
    try:
        rec = propose(s, slug=a.slug, title=a.title, body=text, pair=a.pair, review_id=a.review_id, base=a.base,
                      actor=a.actor, dry_run=a.dry_run, root=root)
    except Invalid as exc:
        print(f"invalid: {exc}")
        return EXIT_INVALID
    except Refused as exc:
        print(f"refused: {exc}")
        return EXIT_REFUSED
    except (GitError, OSError, subprocess.SubprocessError) as exc:
        print(f"error: {exc}")
        return EXIT_ERROR
    if a.dry_run:
        print("would create: " + json.dumps(rec, ensure_ascii=False))
    else:
        print(f"proposal {rec['id']} created: branch {rec['branch']} ({rec['commit']}) from {rec['base']} "
              f"({rec['base_sha']}), {rec['commits_not_in_main']} commit(s) not in {MAIN_BRANCH}, {rec['doc']} in "
              f"{rec['worktree']}")
        if rec.get("jsonl_error"):
            print(f"warning: the proposal exists, but its data/shared/proposals.jsonl record is missing "
                  f"({rec['jsonl_error']}): the dashboard and the next review pack will not list it - do not propose "
                  "it again")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
