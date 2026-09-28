"""State backup (Phase 5 A2): ``python tools/backup_state.py [--dest DIR] [--keep N] [--dry-run] [--quiet]``,
``--verify [ZIP]`` and ``--restore ZIP --target DATA_ROOT [--config-dir DIR] [--overwrite]``.

What exists only on this machine goes into one zip, ``<backup.dir>/<UTC stamp>.zip``: every system's ``app.db`` (one
per pair under ``data/instances``, and the all-pairs system's ``data/app.db`` kept since the D-042 switch), the
shared AI ledger ``data/shared/ai_usage.db``, the account high-water mark ``account_peak.json`` (+ ``.bak``),
``config/config.local.yaml``, the adaptive overlay ``data/adaptive/**``, the review packs and session results
``data/reviews/**``, ``monitor_state.json``, ``notify_state.json``, ``proposals.jsonl`` and the Claude CLI capability
file (``data/shared/cli_capabilities.json``; before Phase 5 A7 it lived in ``%TEMP%\\tradingsystem-claude-code``, kept
in the zip as ``external/cli_capabilities.json``). Market data (hot/cold stores, Vision cache, research recordings)
is not copied: it is downloaded or recorded again. NEVER ``.env`` (the secrets live in the owner's password
manager), never a log, never a ``*stderr*`` file; a plain file above 20 MB is skipped (listed in the manifest).

SQLite databases are copied with the backup API from a read-only connection (``file:…?mode=ro``): a consistent
snapshot that includes the WAL while the systems keep writing. The copy is switched to ``journal_mode=DELETE`` (one
self-contained file), checked with ``PRAGMA integrity_check`` and its rows counted per table. Small files are read
in one go (the account peak's writer replaces its file without a retry, so the read window stays microseconds, as in
the monitor). ``manifest.json`` inside the zip lists every file (size, sha256; for a database the integrity result
and the row count of every table), what was skipped and why, the git sha and the config hash. The zip is written as
``<stamp>.zip.tmp`` and renamed when complete; only the newest ``backup.keep`` zips stay. A copy that failed keeps
the zip as ``<stamp>.incomplete.zip`` (never rotated, never the default for ``--verify``) and exits 1. A lock file in
the backup folder keeps two runs from overlapping; less than 1 GB free there (plus twice the sources) → no backup.
``backup.enabled: false`` makes a backup run a no-op (exit 0); ``--verify`` and ``--restore`` still work.

``--verify`` extracts a zip (default: the newest in the backup folder) to a temporary folder and checks every file
against the manifest (size, sha256), runs ``PRAGMA integrity_check`` on every database and compares its row counts;
it prints the ``ai_decisions`` / ``ai_usage`` rows per database. ``--restore`` verifies the zip the same way first,
then writes its files under ``--target`` (a data root, e.g. ``data``) and ``config.local.yaml`` into ``--config-dir``
(only when given — the output says so otherwise). It refuses while a supervisor of that data root runs (single-instance
locks, ``supervisor.json``, and the command lines of older builds) and never replaces an existing file unless
``--overwrite``: the old file is then moved aside to ``<name>.pre-restore-<stamp>`` (a database's ``-wal``/``-shm``
too), never deleted. An existing ``account_peak.json`` (or ``.bak``) is never replaced, not even with ``--overwrite``:
the account high-water mark only rises and a drawdown stop must survive a restore. It never writes ``.env``; a zip
entry outside ``data/``, ``config/config.local.yaml`` and ``external/`` makes the zip invalid.

``--root DIR`` backs up another checkout's state (its data root and ``config.local.yaml``) with this checkout's
settings — how a worktree rehearses on production, read-only (the lock and the zip live in ``--dest``).

Exit codes: 0 done (also: backups disabled, ``--dry-run``); 1 failed (a copy, the zip, a check; ``--verify``: the zip
is bad; no zip to verify); 2 bad arguments; 3 the config could not be read; 4 another backup run holds the lock
(nothing done); 5 ``--restore`` refused (a supervisor of the target runs, or files exist without ``--overwrite``).
Log: ``logs/backup.jsonl``. Task ``TradingSystemOps-Backup`` (daily 03:30 UTC) and ``scripts\\restart_all.bat`` run
it; docs/ops_windows.md §9.
"""
# No ``from __future__ import annotations``: tools are loaded by file path without a sys.modules entry (tests).
import argparse
import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from urllib.parse import quote

import psutil

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tradingsystem.core.filelock import FileLock  # noqa: E402
from tradingsystem.core.logsetup import setup_logging  # noqa: E402
from tradingsystem.core.settings import INSTANCE_ENV, Settings, load_settings  # noqa: E402
from tradingsystem.core.timeutil import iso, now_ms  # noqa: E402
from tradingsystem.supervisor import control, procs, winops  # noqa: E402

log = logging.getLogger("backup")

EXIT_OK, EXIT_FAILED, EXIT_USAGE, EXIT_CONFIG, EXIT_BUSY, EXIT_REFUSED = 0, 1, 2, 3, 4, 5
FORMAT = 1                                  # manifest layout version
MANIFEST = "manifest.json"
LOCK_NAME = "backup.lock"                   # in the backup folder: one run at a time per folder
MAX_FILE_BYTES = 20 * 1024 * 1024           # a plain file above this is skipped (listed in the manifest)
MIN_FREE_BYTES = 1 << 30                    # never fill the disk production writes to
ZIP_RE = re.compile(r"^(\d{8}T\d{6}Z)(?:-(\d+))?\.zip$")
PEAK_FILES = ("account_peak.json", "account_peak.json.bak")      # execution/drawdown.py: never rolled back
SHARED_FILES = (*PEAK_FILES, "monitor_state.json", "notify_state.json", "proposals.jsonl", "cli_capabilities.json")
TREES = ("adaptive", "reviews")             # under the data root, copied whole (files ≤ 20 MB)
LOCAL_CONFIG_ARC = "config/config.local.yaml"
CAPS_ARC = "external/cli_capabilities.json"
CAPS_NEW_ARC = "data/shared/cli_capabilities.json"
# the CLI capability file before Phase 5 A7 (ai/providers/claude_code.py: the CLI work folder in %TEMP%)
CLI_CAPS_OLD = Path(tempfile.gettempdir()) / "tradingsystem-claude-code" / "cli_capabilities.json"
REPORTED_TABLES = ("ai_decisions", "ai_usage")
DB_SIDE = ("-wal", "-shm", "-journal")
ARC_PREFIXES = ("data/", "external/")


# --------------------------------------------------------------------------- what is copied
def data_root(s: Settings, root: Path) -> Path:
    """The data root of the checkout ``root`` as these settings name it (``paths.data_dir``; an absolute one wins)."""
    p = Path(s.paths.data_dir)
    return p if p.is_absolute() else Path(root) / p


def excluded(name: str) -> str | None:
    """Why a file is never copied (None: it may be)."""
    low = name.lower()
    if low.startswith(".env"):
        return "secrets (.env)"
    if "stderr" in low:
        return "stderr log"
    if low.endswith(".log") or ".log." in low:
        return "log"
    if low.endswith((".tmp", ".lock", *DB_SIDE)):
        return "transient"
    return None


def plan(s: Settings, root: Path) -> tuple[list[dict], list[dict]]:
    """``(entries, skipped)``: entry = ``{arc, src, kind: db|file, size}`` (``arc`` = the path inside the zip)."""
    data = data_root(s, root)
    items: list[dict] = []
    skipped: list[dict] = []

    def add(src: Path, arc: str, kind: str, *, expected: bool = False) -> None:
        why = excluded(src.name)
        if why:
            skipped.append({"path": arc, "reason": why})
            return
        try:
            st = src.stat()
        except FileNotFoundError:
            if expected:
                skipped.append({"path": arc, "reason": "missing"})
            return
        except OSError as exc:
            skipped.append({"path": arc, "reason": f"unreadable: {exc}"})
            return
        if kind == "file" and st.st_size > MAX_FILE_BYTES:
            skipped.append({"path": arc, "reason": f"larger than {MAX_FILE_BYTES >> 20} MB ({st.st_size} bytes)"})
            return
        size = st.st_size
        if kind == "db":                    # the copy includes the WAL's pages: count them for the disk check
            try:
                size += src.with_name(src.name + "-wal").stat().st_size
            except OSError:
                pass
        items.append({"arc": arc, "src": src, "kind": kind, "size": size})

    # databases: every system's state (the all-pairs one and one per pair) and the shared ledger
    add(data / "app.db", "data/app.db", "db")
    inst = data / "instances"
    names = set(s.instances) | ({d.name for d in inst.iterdir() if d.is_dir()} if inst.is_dir() else set())
    for n in sorted(names):
        add(inst / n / "app.db", f"data/instances/{n}/app.db", "db", expected=n in s.instances)
    add(data / "shared" / "ai_usage.db", "data/shared/ai_usage.db", "db", expected=bool(s.instances))
    for name in SHARED_FILES:
        add(data / "shared" / name, f"data/shared/{name}", "file")
    add(Path(root) / "config" / "config.local.yaml", LOCAL_CONFIG_ARC, "file")
    for tree in TREES:
        for dirpath, dirnames, filenames in os.walk(data / tree):
            here = Path(dirpath)
            dirnames[:] = sorted(d for d in dirnames if not ((here / d).is_symlink() or (here / d).is_junction()))
            for fn in sorted(filenames):
                p = here / fn
                add(p, "data/" + p.relative_to(data).as_posix(), "db" if fn.endswith(".db") else "file")
    add(CLI_CAPS_OLD, CAPS_ARC, "file")
    return items, skipped


# --------------------------------------------------------------------------- copies
def ro_uri(path: Path) -> str:
    return "file:" + quote(Path(path).as_posix(), safe="/:") + "?mode=ro"


def db_info(con: sqlite3.Connection) -> dict:
    """``{integrity, rows: {table: n}}`` of an open database."""
    integrity = "; ".join(str(r[0]) for r in con.execute("PRAGMA integrity_check").fetchall())
    tables = [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    rows = {t: int(con.execute('SELECT count(*) FROM "%s"' % t.replace('"', '""')).fetchone()[0]) for t in tables}
    return {"integrity": integrity, "rows": rows}


def snapshot_db(src: Path, dst: Path) -> dict:
    """A consistent copy of a live WAL database through the backup API, read from a ``mode=ro`` connection (the
    source is never written); the copy in rollback-journal mode (one file), checked and counted."""
    s_con = sqlite3.connect(ro_uri(src), uri=True, timeout=30)
    try:
        d_con = sqlite3.connect(str(dst))
        try:
            s_con.backup(d_con)
            d_con.execute("PRAGMA journal_mode=DELETE")
            return db_info(d_con)
        finally:
            d_con.close()
    finally:
        s_con.close()


def _zinfo(arc: str, mtime: float) -> zipfile.ZipInfo:
    zi = zipfile.ZipInfo(arc, date_time=time.gmtime(max(mtime, 315532800))[:6])     # zip times: UTC, ≥ 1980
    zi.compress_type = zipfile.ZIP_DEFLATED
    return zi


def write_entry(zf: zipfile.ZipFile, arc: str, *, path: Path | None = None, data: bytes | None = None,
                mtime: float | None = None) -> tuple[int, str]:
    """Write one entry (streamed from ``path`` or from ``data``); returns ``(size, sha256)`` of what was written."""
    h = hashlib.sha256()
    n = 0
    zi = _zinfo(arc, mtime if mtime is not None else time.time())
    with zf.open(zi, "w", force_zip64=True) as out:
        if data is not None:
            h.update(data)
            out.write(data)
            n = len(data)
        else:
            with open(path, "rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    h.update(chunk)
                    out.write(chunk)
                    n += len(chunk)
    return n, h.hexdigest()


def git_sha(root: Path) -> str | None:
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(root), capture_output=True, text=True, timeout=15,
                             stdin=subprocess.DEVNULL, creationflags=winops.CREATE_NO_WINDOW if winops.WIN else 0)
        return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def stamp_now() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


def backups(dest: Path) -> list[Path]:
    """Complete backups in ``dest``, oldest first."""
    found = []
    for p in Path(dest).glob("*.zip"):
        m = ZIP_RE.match(p.name)
        if m and p.is_file():
            found.append((m.group(1), int(m.group(2) or 0), p))
    return [p for _, _, p in sorted(found)]


def _cleanup_leftovers(dest: Path) -> None:
    """A run killed half-way (the task's time limit, a shutdown) leaves ``*.zip.tmp`` / ``.staging-*``; the lock is
    held, so nothing else writes them now."""
    for p in dest.glob("*.zip.tmp"):
        if ZIP_RE.match(p.name[:-4]):
            p.unlink(missing_ok=True)
    for p in dest.glob(".staging-*"):
        shutil.rmtree(p, ignore_errors=True)


def _mb(n: float) -> str:
    return f"{n / 2**20:.1f} MB" if n >= 2**20 else f"{n / 1024:.1f} KB" if n >= 1024 else f"{int(n)} B"


def run_backup(s: Settings, root: Path, dest: Path, keep: int, *, say=print, stamp: str | None = None) -> int:
    """One backup into ``dest`` (see the module doc). Returns an exit code."""
    t0 = time.monotonic()
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    lock = FileLock(dest / LOCK_NAME)
    if not lock.acquire(0):
        say(f"another backup run holds {dest / LOCK_NAME} - nothing done")
        log.warning("another backup run holds %s - nothing done", dest / LOCK_NAME)
        return EXIT_BUSY
    staging = dest / f".staging-{os.getpid()}"
    try:
        _cleanup_leftovers(dest)
        items, skipped = plan(s, root)
        need = 2 * sum(i["size"] for i in items) + MIN_FREE_BYTES
        free = shutil.disk_usage(dest).free
        if free < need:
            msg = f"only {_mb(free)} free in {dest} (needs {_mb(need)}) - no backup"
            say(msg)
            log.error(msg)
            return EXIT_FAILED
        stamp = stamp or stamp_now()
        final, n = dest / f"{stamp}.zip", 0
        while final.exists() or final.with_name(final.name + ".tmp").exists():
            n += 1
            final = dest / f"{stamp}-{n}.zip"
        tmp = final.with_name(final.name + ".tmp")
        staging.mkdir(parents=True, exist_ok=True)
        files: list[dict] = []
        problems: list[str] = []
        with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
            for idx, it in enumerate(items):
                arc, src = it["arc"], it["src"]
                try:
                    if it["kind"] == "db":
                        snap = staging / f"{idx}.db"            # its own name: a failed copy leaves its file behind
                        info = snapshot_db(src, snap)
                        size, digest = write_entry(zf, arc, path=snap, mtime=snap.stat().st_mtime)
                        snap.unlink()
                        files.append({"path": arc, "kind": "db", "size": size, "sha256": digest, **info})
                        if info["integrity"] != "ok":
                            problems.append(f"{arc}: integrity_check {info['integrity'][:200]}")
                    else:
                        mtime = src.stat().st_mtime
                        size, digest = write_entry(zf, arc, data=src.read_bytes(), mtime=mtime)
                        files.append({"path": arc, "kind": "file", "size": size, "sha256": digest})
                except FileNotFoundError:
                    skipped.append({"path": arc, "reason": "vanished during the backup"})
                except (OSError, sqlite3.Error, zipfile.BadZipFile) as exc:
                    problems.append(f"{arc}: {type(exc).__name__}: {exc}")
            made = now_ms()
            manifest = {
                "format": FORMAT, "created_utc": iso(made), "created_ms": made,
                "tool": "tools/backup_state.py", "root": str(Path(root).resolve()),
                "data_root": str(data_root(s, root).resolve()), "git_sha": git_sha(root),
                "config_hash": s.config_hash, "instances": sorted(s.instances),
                "files": files, "skipped": skipped, "problems": problems,
                "total_bytes": sum(f["size"] for f in files), "seconds": round(time.monotonic() - t0, 2),
            }
            zf.writestr(_zinfo(MANIFEST, time.time()), json.dumps(manifest, indent=1, sort_keys=True))
        with open(tmp, "rb+") as fh:
            os.fsync(fh.fileno())
        if problems:
            bad = final.with_name(final.name[:-4] + ".incomplete.zip")
            os.replace(tmp, bad)
            for p in problems:
                log.error("backup problem: %s", p)
            say(f"backup INCOMPLETE: {bad} ({len(problems)} problem(s): {'; '.join(problems)[:500]})")
            return EXIT_FAILED
        os.replace(tmp, final)
        removed = prune(dest, keep)
        secs = time.monotonic() - t0
        counts = ", ".join(f"{f['path']} {t} {f['rows'][t]}" for f in files if f["kind"] == "db"
                           for t in REPORTED_TABLES if t in f.get("rows", {}))
        log.info("backup written: %s (%d files, %s, %.1f s; %d skipped; %d old removed)", final, len(files),
                 _mb(final.stat().st_size), secs, len(skipped), len(removed),
                 extra={"ctx": {"zip": str(final), "files": len(files), "zip_bytes": final.stat().st_size,
                                "seconds": round(secs, 2), "skipped": skipped, "removed": [p.name for p in removed]}})
        say(f"backup written: {final} ({len(files)} files, zip {_mb(final.stat().st_size)}, {secs:.1f} s)")
        if counts:
            say(f"  rows: {counts}")
        for sk in skipped:
            say(f"  skipped {sk['path']}: {sk['reason']}")
        return EXIT_OK
    except Exception as exc:  # noqa: BLE001 — a scheduled task: log it, exit 1, never a traceback on a hidden console
        log.exception("backup failed")
        say(f"backup FAILED: {type(exc).__name__}: {exc}")
        for p in dest.glob("*.zip.tmp"):
            if ZIP_RE.match(p.name[:-4]):
                p.unlink(missing_ok=True)
        return EXIT_FAILED
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        lock.release()


def prune(dest: Path, keep: int) -> list[Path]:
    """Delete all but the newest ``keep`` complete backups (only names this tool writes). Returns what was removed."""
    removed = []
    for p in backups(dest)[:-keep] if keep > 0 else []:
        try:
            p.unlink()
            removed.append(p)
        except OSError as exc:
            log.warning("could not remove the old backup %s: %s", p, exc)
    return removed


def dry_run(s: Settings, root: Path, dest: Path, keep: int, *, say=print) -> int:
    items, skipped = plan(s, root)
    say(f"backup plan: root {Path(root).resolve()}, data {data_root(s, root)} -> {dest} (keep {keep})"
        + ("" if s.backup.enabled else "  [backup.enabled: false - a run does nothing]"))
    for it in items:
        say(f"  {it['kind']:<4} {_mb(it['size']):>9}  {it['arc']}")
    for sk in skipped:
        say(f"  skip {sk['path']}: {sk['reason']}")
    say(f"{len(items)} file(s), at most {_mb(sum(i['size'] for i in items))} before compression (a database's size "
        "includes its WAL; the copy holds the WAL's pages). DryRun: nothing written.")
    return EXIT_OK


# --------------------------------------------------------------------------- verify
def unsafe_arc(arc: str) -> str | None:
    """Why a zip entry name may not be restored (None: it may)."""
    if not arc or "\\" in arc or arc.startswith("/") or ":" in arc:
        return "not a relative path"
    parts = arc.split("/")
    if any(p in ("", ".", "..") for p in parts):
        return "not a plain relative path"
    if any(p.lower().startswith(".env") for p in parts):
        return "a .env file is never restored"
    if not (arc == LOCAL_CONFIG_ARC or arc.startswith(ARC_PREFIXES)):
        return "outside data/, config/config.local.yaml and external/"
    return None


def _extract(zf: zipfile.ZipFile, arc: str, out: Path) -> tuple[int, str]:
    h = hashlib.sha256()
    n = 0
    out.parent.mkdir(parents=True, exist_ok=True)
    with zf.open(arc) as src, open(out, "wb") as fh:           # ZipExtFile checks the CRC at the end
        for chunk in iter(lambda: src.read(1 << 20), b""):
            h.update(chunk)
            fh.write(chunk)
            n += len(chunk)
    return n, h.hexdigest()


def verify(zip_path: Path, workdir: Path) -> tuple[list[str], dict]:
    """Extract ``zip_path`` into ``workdir`` and check it against its manifest: every listed file present with its
    size and sha256, nothing unlisted, every database ``integrity_check`` ok with the manifest's row counts. Returns
    ``(problems, manifest)``; each database entry of the manifest gets ``verified: {integrity, rows}``."""
    try:
        zf = zipfile.ZipFile(zip_path)
    except (OSError, zipfile.BadZipFile) as exc:
        return [f"not a readable zip: {exc}"], {}
    problems: list[str] = []
    with zf:
        try:
            manifest = json.loads(zf.read(MANIFEST))
            files = list(manifest["files"])
            if not all(isinstance(f, dict) for f in files):
                raise TypeError("a files entry is not an object")
        except (KeyError, TypeError, ValueError, zipfile.BadZipFile) as exc:
            return [f"no readable {MANIFEST}: {exc}"], {}
        if manifest.get("format") != FORMAT:
            problems.append(f"manifest format {manifest.get('format')!r}, this tool reads {FORMAT}")
        problems += [f"the backup itself reported: {p}" for p in manifest.get("problems") or []]
        names = set(zf.namelist())
        listed = {str(f.get("path")) for f in files}
        problems += [f"{n}: in the zip but not in the manifest" for n in sorted(names - listed - {MANIFEST})]
        for f in files:
            arc = str(f.get("path"))
            why = unsafe_arc(arc)
            if why:
                problems.append(f"{arc!r}: {why}")
                continue
            if arc not in names:
                problems.append(f"{arc}: listed in the manifest, missing from the zip")
                continue
            out = Path(workdir).joinpath(*arc.split("/"))
            try:
                size, digest = _extract(zf, arc, out)
            except (OSError, zipfile.BadZipFile) as exc:
                problems.append(f"{arc}: cannot be extracted ({exc})")
                continue
            if size != f.get("size") or digest != f.get("sha256"):
                problems.append(f"{arc}: size/sha256 differ from the manifest")
            if f.get("kind") != "db":
                continue
            try:
                con = sqlite3.connect(ro_uri(out), uri=True)
                try:
                    info = db_info(con)
                finally:
                    con.close()
            except sqlite3.Error as exc:
                problems.append(f"{arc}: cannot be opened as a database ({exc})")
                continue
            f["verified"] = info
            if info["integrity"] != "ok":
                problems.append(f"{arc}: integrity_check {info['integrity'][:200]}")
            want = f.get("rows") or {}
            diff = sorted(t for t in set(want) | set(info["rows"]) if want.get(t) != info["rows"].get(t))
            if diff:
                problems.append(f"{arc}: row counts differ from the manifest in {', '.join(diff)}")
    return problems, manifest


def _db_rows_line(f: dict) -> str:
    rows = (f.get("verified") or {}).get("rows") or {}
    have = [f"{t} {rows[t]} (manifest {(f.get('rows') or {}).get(t)})" for t in REPORTED_TABLES if t in rows]
    return (f"  {f['path']}: integrity {(f.get('verified') or {}).get('integrity', '?')}, "
            + (", ".join(have) if have else f"{len(rows)} tables"))


def run_verify(zip_path: Path, *, say=print) -> int:
    t0 = time.monotonic()
    work = Path(tempfile.mkdtemp(prefix="ts-backup-verify-"))
    try:
        problems, manifest = verify(zip_path, work)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    secs = time.monotonic() - t0
    for f in manifest.get("files", []):
        if f.get("kind") == "db" and "verified" in f:
            say(_db_rows_line(f))
    if problems:
        for p in problems:
            say(f"  BAD {p}")
        log.error("verify %s: %d problem(s): %s", zip_path, len(problems), "; ".join(problems)[:2000])
        say(f"verify FAILED: {zip_path} ({len(problems)} problem(s), {secs:.1f} s)")
        return EXIT_FAILED
    n = len(manifest.get("files", []))
    log.info("verify ok: %s (%d files, %.1f s)", zip_path, n, secs)
    say(f"verify ok: {zip_path} ({n} files, made {manifest.get('created_utc')}, git {manifest.get('git_sha')}, "
        f"{secs:.1f} s)")
    return EXIT_OK


# --------------------------------------------------------------------------- restore
def _arg(cmd: list[str], flag: str) -> str | None:
    for i, a in enumerate(cmd):
        if a == flag and i + 1 < len(cmd):
            return cmd[i + 1]
        if a.startswith(flag + "="):
            return a.split("=", 1)[1]
    return None


def _same(a: Path, b: Path) -> bool:
    return os.path.normcase(str(Path(a).resolve())) == os.path.normcase(str(Path(b).resolve()))


def process_data_root(s: Settings, p: psutil.Process) -> Path | None:
    """The data root a supervisor process uses: its ``--data-dir``, else the data root of its checkout — its working
    folder (``control.detach`` starts it there), else the checkout of its interpreter
    (``<checkout>\\.venv\\Scripts\\python.exe``; Windows denies reading another process's working folder when it was
    started outside our job). None when neither can be read."""
    cmd = p.cmdline()
    dd = _arg(cmd, "--data-dir")
    if dd:
        return Path(dd)
    try:
        return data_root(s, Path(p.cwd()))
    except (psutil.Error, OSError):
        pass
    try:
        exe = Path(p.exe())
    except (psutil.Error, OSError):
        return None
    if exe.parent.name.lower() == "scripts" and exe.parent.parent.name.lower() == ".venv":
        return data_root(s, exe.parents[2])
    return None


def target_supervisors(s: Settings, target: Path) -> list[str]:
    """Supervisors running on the data root ``target`` (they would overwrite a restore or have their databases
    replaced under them): each system's single-instance lock (every build since OPS-03), a live process in its
    ``supervisor.json``, and — for an older build without the lock — a supervisor process whose data root
    (:func:`process_data_root`) is ``target``; one whose data root cannot be read counts."""
    target = Path(target).resolve()
    inst = target / "instances"
    names = set(s.instances) | ({d.name for d in inst.iterdir() if d.is_dir()} if inst.is_dir() else set())
    found: list[str] = []
    for d in [target, *(inst / n for n in sorted(names))]:
        label = "the all-pairs system" if d == target else d.name
        if winops.instance_running(winops.instance_name("supervisor", d)):
            found.append(f"{label} (single-instance lock held)")
            continue
        proc = control.state_process(control.read_state(d))
        if proc is not None:
            found.append(f"{label} (pid {proc.pid})")
    for pid in procs.running_supervisors():
        try:
            p = psutil.Process(pid)
            root = process_data_root(s, p)
        except (psutil.Error, OSError):
            root = None
        if root is None:
            found.append(f"pid {pid} (its data root cannot be read)")
            continue
        if _same(root, target):
            found.append(f"pid {pid} ({procs.cmd_instance(p.cmdline()) or 'all pairs'})")
    return found


def restore_plan(manifest: dict, target: Path, config_dir: Path | None) -> tuple[list[tuple[str, Path, str]],
                                                                                   list[str]]:
    """``([(arc, destination, kind)], notes)`` for a verified manifest.

    The account high-water mark is never replaced: when ``account_peak.json`` or its ``.bak`` exists at the target,
    both stay as they are (the peak only rises and a drawdown stop must survive a restore — an older copy could only
    re-arm trading; ``scripts\\reset_drawdown_stop.bat`` is the one way to do that). It is restored where none exists
    (a rebuild from zero)."""
    arcs = {str(f["path"]): f for f in manifest.get("files", [])}
    out: list[tuple[str, Path, str]] = []
    notes: list[str] = []
    peak_kept = any((Path(target) / "shared" / n).exists() for n in PEAK_FILES)
    for arc, f in arcs.items():
        if arc in {f"data/shared/{n}" for n in PEAK_FILES} and peak_kept:
            notes.append(f"{arc} kept as it is at the target (the account peak and a drawdown stop are never rolled "
                         "back by a restore)")
            continue
        if arc.startswith("data/"):
            dest = Path(target).joinpath(*arc.split("/")[1:])
        elif arc == LOCAL_CONFIG_ARC:
            if config_dir is None:
                notes.append("config/config.local.yaml NOT restored (add --config-dir <checkout>\\config)")
                continue
            dest = Path(config_dir) / "config.local.yaml"
        elif arc == CAPS_ARC:
            if CAPS_NEW_ARC in arcs:
                continue                    # the file in data/shared is the newer location (Phase 5 A7)
            dest = Path(target) / "shared" / "cli_capabilities.json"
        else:
            notes.append(f"{arc}: no place to restore it - skipped")
            continue
        out.append((arc, dest, str(f.get("kind"))))
    return out, notes


def _footprint(dest: Path, kind: str) -> list[Path]:
    """The files a restore of ``dest`` replaces: a database's journal files too (a stale ``-wal`` next to a restored
    database would be replayed into it)."""
    return [dest, *(dest.with_name(dest.name + x) for x in DB_SIDE)] if kind == "db" else [dest]


def run_restore(s: Settings, zip_path: Path, target: Path, *, config_dir: Path | None, overwrite: bool,
                say=print, stamp: str | None = None) -> int:
    t0 = time.monotonic()
    target = Path(target).resolve()
    work = Path(tempfile.mkdtemp(prefix="ts-backup-restore-"))
    try:
        problems, manifest = verify(zip_path, work)
        if problems:
            for p in problems:
                say(f"  BAD {p}")
            say(f"restore REFUSED: {zip_path} did not verify ({len(problems)} problem(s)) - nothing written")
            log.error("restore refused: %s did not verify: %s", zip_path, "; ".join(problems)[:2000])
            return EXIT_FAILED
        running = target_supervisors(s, target)
        if running:
            say(f"restore REFUSED: a supervisor runs on {target}: {', '.join(running)} - stop it first "
                "(scripts\\stop_all.bat)")
            log.error("restore refused: supervisors run on %s: %s", target, running)
            return EXIT_REFUSED
        todo, notes = restore_plan(manifest, target, config_dir)
        exists = [p for _, dest, kind in todo for p in _footprint(dest, kind) if p.exists()]
        if exists and not overwrite:
            for p in exists:
                say(f"  exists: {p}")
            say(f"restore REFUSED: {len(exists)} file(s) exist - add --overwrite to move them aside "
                "(<name>.pre-restore-<stamp>) and restore")
            log.error("restore refused: %d existing file(s) without --overwrite", len(exists))
            return EXIT_REFUSED
        stamp = stamp or stamp_now()
        for arc, dest, kind in todo:
            dest.parent.mkdir(parents=True, exist_ok=True)
            for p in _footprint(dest, kind):
                if p.exists():
                    aside = p.with_name(f"{p.name}.pre-restore-{stamp}")
                    os.replace(p, aside)
                    say(f"  moved aside: {p} -> {aside.name}")
            tmp = dest.with_name(f"{dest.name}.restore-{os.getpid()}.tmp")
            shutil.copyfile(work.joinpath(*arc.split("/")), tmp)
            os.replace(tmp, dest)
        for f in manifest.get("files", []):
            if f.get("kind") == "db" and "verified" in f:
                say(_db_rows_line(f))
        for n in notes:
            say(f"  note: {n}")
        secs = time.monotonic() - t0
        log.info("restored %s into %s (%d files, %.1f s)", zip_path, target, len(todo), secs)
        say(f"restore done: {len(todo)} file(s) from {zip_path} into {target} ({secs:.1f} s)")
        return EXIT_OK
    except Exception as exc:  # noqa: BLE001
        log.exception("restore failed")
        say(f"restore FAILED: {type(exc).__name__}: {exc}")
        return EXIT_FAILED
    finally:
        shutil.rmtree(work, ignore_errors=True)


# --------------------------------------------------------------------------- main
def setup_log(s: Settings) -> None:
    setup_logging("backup", logs_dir=s.paths.logs(), level=s.logging.level, max_bytes=s.logging.max_bytes,
                  backups=s.logging.backups, console=False, secret_env_names=s.secret_env_names())


def backup_dir(s: Settings, root: Path) -> Path:
    p = Path(s.backup.dir)
    return p if p.is_absolute() else Path(root) / p


def main(argv: list[str] | None = None, *, settings: Settings | None = None) -> int:
    ap = argparse.ArgumentParser(description="Back up, verify or restore the state that exists only on this machine "
                                             "(docs/ops_windows.md section 9). Never .env.")
    ap.add_argument("--dest", help="backup folder (default: backup.dir, relative to the checkout)")
    ap.add_argument("--keep", type=int, help="keep the newest N backups (default: backup.keep)")
    ap.add_argument("--root", help="back up this checkout's state instead of this one's (rehearsals from a worktree)")
    ap.add_argument("--dry-run", action="store_true", help="list what would be copied; write nothing")
    ap.add_argument("--quiet", action="store_true", help="no console output (the scheduled task, pythonw)")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--verify", nargs="?", const="", metavar="ZIP",
                      help="check a backup (default: the newest in the backup folder)")
    mode.add_argument("--restore", metavar="ZIP", help="restore a backup into --target (the systems must be stopped)")
    ap.add_argument("--target", metavar="DATA_ROOT", help="--restore: the data root to write into (e.g. data)")
    ap.add_argument("--config-dir", help="--restore: where config.local.yaml goes (e.g. config); omitted = not restored")
    ap.add_argument("--overwrite", action="store_true",
                    help="--restore: move existing files aside (<name>.pre-restore-<stamp>) instead of refusing")
    a = ap.parse_args(argv)
    if a.restore is not None and not a.target:
        ap.error("--restore needs --target DATA_ROOT")
    if a.keep is not None and a.keep < 1:
        ap.error("--keep must be at least 1")
    say = (lambda *_a, **_k: None) if a.quiet else print
    root = Path(a.root).resolve() if a.root else ROOT
    s = settings
    if s is None:
        try:
            s = load_settings(extra_env={INSTANCE_ENV: ""})       # the machine's state, never one pair's view
        except Exception as exc:  # noqa: BLE001
            say(f"the config could not be read - nothing done: {exc}")
            try:
                setup_logging("backup", logs_dir=ROOT / "logs", console=False)
                log.error("the config could not be read - nothing done: %s", exc)
            except Exception:  # noqa: BLE001
                pass
            return EXIT_CONFIG
    setup_log(s)
    dest = Path(a.dest).resolve() if a.dest else backup_dir(s, root)
    keep = a.keep or s.backup.keep
    if a.restore is not None:
        return run_restore(s, Path(a.restore), Path(a.target), overwrite=a.overwrite,
                           config_dir=Path(a.config_dir).resolve() if a.config_dir else None, say=say)
    if a.verify is not None:
        if a.verify:
            z = Path(a.verify)
            if not z.exists() and (dest / a.verify).exists():
                z = dest / a.verify
        else:
            found = backups(dest)
            if not found:
                say(f"no backup to verify in {dest}")
                return EXIT_FAILED
            z = found[-1]
        return run_verify(z, say=say)
    if a.dry_run:
        return dry_run(s, root, dest, keep, say=say)
    if not s.backup.enabled:
        log.info("backup.enabled is false - nothing done")
        say("backups are disabled (backup.enabled: false) - nothing done")
        return EXIT_OK
    return run_backup(s, root, dest, keep, say=say)


if __name__ == "__main__":
    sys.exit(main())
