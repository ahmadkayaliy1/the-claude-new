"""tools/backup_state.py (Phase 5 A2): what is copied (never .env, logs or stderr files), WAL-consistent database
copies from read-only sources, the manifest, --verify, --restore (refusals, move-aside, no .env, no unsafe entries),
retention, the run lock, the disabled switch, and the scheduled task / restart_all.bat wiring. Databases are built with
the project's own stores (the real schema); nothing here touches the checkout's data or production."""
from __future__ import annotations

import importlib.util
import json
import re
import sqlite3
import sys
import zipfile
from pathlib import Path

import pytest

from tradingsystem.ai import store as ai_store
from tradingsystem.ai.budget import UsageStore
from tradingsystem.ai.store import DecisionRecord, DecisionStore
from tradingsystem.core.filelock import FileLock
from tradingsystem.core.settings import load_settings
from tradingsystem.supervisor import procs, winops

ROOT = Path(__file__).resolve().parents[2]
PAIRS = ("BTCUSDT", "ETHUSDT", "XAUUSD")


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


@pytest.fixture()
def bs(monkeypatch, tmp_path):
    m = load("test_ts_backup_state", ROOT / "tools" / "backup_state.py")
    monkeypatch.setattr(m, "setup_log", lambda s: None)
    monkeypatch.setattr(m, "git_sha", lambda root: "abc1234")
    monkeypatch.setattr(m, "CLI_CAPS_OLD", tmp_path / "tempdir" / "tradingsystem-claude-code" / "cli_capabilities.json")
    monkeypatch.setattr(procs, "running_supervisors", lambda older_s=None: {})   # no process scan of this machine
    monkeypatch.setattr(ai_store, "git_sha", lambda: "abc1234")                  # DecisionStore: no git per store
    return m


def settings_at(tmp_path: Path, **backup):
    s = load_settings(extra_env={"TS_INSTANCE": ""})
    s = s.model_copy(update={"paths": s.paths.model_copy(update={"data_dir": str(tmp_path / "data"),
                                                                    "logs_dir": str(tmp_path / "logs")})})
    if backup:
        s = s.model_copy(update={"backup": s.backup.model_copy(update=backup)})
    return s


def decisions(db: Path, pair: str, n: int) -> None:
    st = DecisionStore(db, config_hash="cfg")
    try:
        for i in range(n):
            st.save(DecisionRecord(pair=pair, mode="demo", trigger="setup", status="valid", provider="claude_code",
                                   model="sonnet", input_tokens=20_000 + i, output_tokens=900))
    finally:
        st.close()


def ledger(db: Path, n: int) -> None:
    u = UsageStore(db)
    try:
        for i in range(n):
            u.record(None, provider="claude_code", model="sonnet", purpose="decision", pair=PAIRS[i % 3], ok=True,
                     role="trader")
    finally:
        u.close()


def checkout(tmp_path: Path) -> Path:
    """A checkout with the state of three per-pair systems, the shared files, reviews, adaptive, logs, market data
    and a .env — only the state may end up in a backup."""
    root = tmp_path / "checkout"
    data = tmp_path / "data"
    (root / "config").mkdir(parents=True)
    (root / "config" / "config.local.yaml").write_text("monitor:\n  diagnose_enabled: false\n", encoding="utf-8")
    (root / ".env").write_text("ANTHROPIC_API_KEY=sk-ant-never-in-a-backup-0123456789\n", encoding="utf-8")
    for i, pair in enumerate(PAIRS):
        decisions(data / "instances" / pair / "app.db", pair, 3 + i)
    ledger(data / "shared" / "ai_usage.db", 7)
    shared = data / "shared"
    peak = json.dumps({"mt5:WindsorBrokers1-Demo:1": {"peak": 100.0, "peak_ms": 1, "tripped_ms": None}})
    for name, text in (("account_peak.json", peak), ("account_peak.json.bak", peak), ("monitor_state.json", "{}"),
                       ("notify_state.json", "{}"), ("proposals.jsonl", '{"id": 1}\n'),
                       ("cli_capabilities.json", '{"stream_json_user_shape": "content_list"}')):
        (shared / name).write_text(text, encoding="utf-8")
    (shared / "locks").mkdir()
    (shared / "locks" / "account_peak.lock").write_bytes(b"")
    (data / "adaptive" / "BTCUSDT").mkdir(parents=True)
    (data / "adaptive" / "BTCUSDT" / "overlay.json").write_text('{"min_rr": 2.0}', encoding="utf-8")
    reviews = data / "reviews"
    reviews.mkdir()
    (reviews / "20260928T043003Z_daily.md").write_text("# daily\n", encoding="utf-8")
    (reviews / "20260928T043003Z_daily.cli.stderr.txt").write_text("stderr", encoding="utf-8")
    with open(reviews / "huge.json", "wb") as fh:                     # above the 20 MB file limit
        fh.truncate(21 * 1024 * 1024)
    (reviews / ".env").write_text("TOKEN=x", encoding="utf-8")
    (data / "hot" / "binance_spot").mkdir(parents=True)
    (data / "hot" / "binance_spot" / "BTCUSDT.db").write_bytes(b"market data")
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "engine.jsonl").write_text("{}", encoding="utf-8")
    return root


def only_zip(dest: Path) -> Path:
    zips = sorted(dest.glob("*.zip"))
    assert len(zips) == 1, zips
    return zips[0]


def rows(db: Path, table: str) -> int:
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        return con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    finally:
        con.close()


# ------------------------------------------------------------------ backup
def test_backup_copies_the_state_and_never_env_logs_stderr_or_market_data(bs, tmp_path, capsys):
    s, root, dest = settings_at(tmp_path), checkout(tmp_path), tmp_path / "backups"
    assert bs.main(["--root", str(root), "--dest", str(dest)], settings=s) == 0
    z = only_zip(dest)
    assert bs.ZIP_RE.match(z.name)
    with zipfile.ZipFile(z) as zf:
        names = set(zf.namelist())
        manifest = json.loads(zf.read("manifest.json"))
    assert names == {"manifest.json", "config/config.local.yaml", "data/adaptive/BTCUSDT/overlay.json",
                     "data/reviews/20260928T043003Z_daily.md", "data/shared/ai_usage.db",
                     *(f"data/instances/{p}/app.db" for p in PAIRS),
                     *(f"data/shared/{n}" for n in bs.SHARED_FILES)}
    assert not any(".env" in n or "stderr" in n or n.endswith(".jsonl") and "logs" in n or "hot/" in n for n in names)
    skipped = {d["path"]: d["reason"] for d in manifest["skipped"]}
    assert skipped["data/reviews/20260928T043003Z_daily.cli.stderr.txt"] == "stderr log"
    assert skipped["data/reviews/.env"].startswith("secrets")
    assert skipped["data/reviews/huge.json"].startswith("larger than 20 MB")
    assert manifest["git_sha"] == "abc1234" and manifest["config_hash"] == s.config_hash and not manifest["problems"]
    dbs = {f["path"]: f for f in manifest["files"] if f["kind"] == "db"}
    assert dbs["data/instances/ETHUSDT/app.db"]["rows"]["ai_decisions"] == 4
    assert dbs["data/shared/ai_usage.db"]["rows"]["ai_usage"] == 7
    assert all(f["integrity"] == "ok" and len(f["sha256"]) == 64 for f in dbs.values())
    assert "backup written" in capsys.readouterr().out


def test_backup_copy_is_wal_consistent_and_never_writes_the_source(bs, tmp_path):
    """Rows only in the WAL (a writer that has not checkpointed) are in the copy; the copy is one self-contained
    file (journal_mode delete); the source database file is not modified."""
    s, root, dest = settings_at(tmp_path), checkout(tmp_path), tmp_path / "backups"
    db = tmp_path / "data" / "instances" / "BTCUSDT" / "app.db"
    writer = sqlite3.connect(str(db), isolation_level=None)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        before = db.read_bytes()
        writer.execute("CREATE TABLE IF NOT EXISTS engine_kv (key TEXT PRIMARY KEY, value TEXT NOT NULL, "
                       "updated_ms INTEGER NOT NULL)")
        for i in range(50):
            writer.execute("INSERT OR REPLACE INTO engine_kv VALUES (?, ?, ?)", (f"k{i}", "v", i))
        assert (db.with_name("app.db-wal")).stat().st_size > 0
        assert bs.run_backup(s, root, dest, 14, say=lambda *a: None) == 0
        assert db.read_bytes() == before                           # the main file is untouched (mode=ro, no checkpoint)
    finally:
        writer.close()
    z = only_zip(dest)
    out = tmp_path / "x"
    with zipfile.ZipFile(z) as zf:
        zf.extract("data/instances/BTCUSDT/app.db", out)
    copy = out / "data" / "instances" / "BTCUSDT" / "app.db"
    assert rows(copy, "engine_kv") == 50 and rows(copy, "ai_decisions") == 3
    con = sqlite3.connect(f"file:{copy.as_posix()}?mode=ro", uri=True)
    assert con.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    con.close()


def test_backup_keeps_only_the_newest_backups_and_never_other_files(bs, tmp_path):
    dest = tmp_path / "backups"
    dest.mkdir()
    for stamp in ("20260920T033000Z", "20260921T033000Z", "20260922T033000Z", "20260922T033000Z-1"):
        (dest / f"{stamp}.zip").write_bytes(b"x")
    (dest / "20260919T033000Z.incomplete.zip").write_bytes(b"x")
    (dest / "notes.zip").write_bytes(b"x")
    removed = bs.prune(dest, 2)
    assert sorted(p.name for p in removed) == ["20260920T033000Z.zip", "20260921T033000Z.zip"]
    assert sorted(p.name for p in dest.iterdir()) == ["20260919T033000Z.incomplete.zip", "20260922T033000Z-1.zip",
                                                      "20260922T033000Z.zip", "notes.zip"]
    assert [p.name for p in bs.backups(dest)] == ["20260922T033000Z.zip", "20260922T033000Z-1.zip"]


def test_backup_run_keeps_backup_keep_zips(bs, tmp_path):
    s, root, dest = settings_at(tmp_path, keep=2), checkout(tmp_path), tmp_path / "backups"
    for stamp in ("20260101T000000Z", "20260102T000000Z", "20260103T000000Z"):
        assert bs.run_backup(s, root, dest, s.backup.keep, say=lambda *a: None, stamp=stamp) == 0
    assert [p.name for p in bs.backups(dest)] == ["20260102T000000Z.zip", "20260103T000000Z.zip"]


def test_two_backup_runs_never_overlap(bs, tmp_path):
    s, root, dest = settings_at(tmp_path), checkout(tmp_path), tmp_path / "backups"
    lock = FileLock(dest / bs.LOCK_NAME)
    assert lock.acquire(0)
    try:
        assert bs.main(["--root", str(root), "--dest", str(dest), "--quiet"], settings=s) == bs.EXIT_BUSY
    finally:
        lock.release()
    assert not list(dest.glob("*.zip*"))


def test_disabled_backup_is_a_noop(bs, tmp_path):
    s, root, dest = settings_at(tmp_path, enabled=False), checkout(tmp_path), tmp_path / "backups"
    assert bs.main(["--root", str(root), "--dest", str(dest)], settings=s) == 0
    assert not dest.exists() or not list(dest.iterdir())


def test_dry_run_lists_the_plan_and_writes_nothing(bs, tmp_path, capsys):
    s, root, dest = settings_at(tmp_path), checkout(tmp_path), tmp_path / "backups"
    assert bs.main(["--root", str(root), "--dest", str(dest), "--dry-run"], settings=s) == 0
    out = capsys.readouterr().out
    assert "data/instances/XAUUSD/app.db" in out and "skip data/reviews/.env" in out and "nothing written" in out
    assert not dest.exists()


def test_a_failed_database_copy_keeps_an_incomplete_zip_and_exits_1(bs, tmp_path):
    s, root, dest = settings_at(tmp_path), checkout(tmp_path), tmp_path / "backups"
    (tmp_path / "data" / "app.db").write_bytes(b"this is not a database" * 100)     # the all-pairs system's file
    assert bs.run_backup(s, root, dest, 14, say=lambda *a: None) == bs.EXIT_FAILED
    assert bs.backups(dest) == [] and len(list(dest.glob("*.incomplete.zip"))) == 1
    assert not list(dest.glob("*.tmp")) and not list(dest.glob(".staging-*"))
    bad = next(dest.glob("*.incomplete.zip"))
    assert bs.run_verify(bad, say=lambda *a: None) == bs.EXIT_FAILED        # its manifest carries the problem


def test_config_error_exits_3(bs, monkeypatch, tmp_path):
    monkeypatch.setattr(bs, "load_settings", lambda **kw: (_ for _ in ()).throw(ValueError("duplicate key 'ai'")))
    monkeypatch.setattr(bs, "setup_logging", lambda *a, **k: None)
    assert bs.main(["--dest", str(tmp_path / "b"), "--quiet"]) == bs.EXIT_CONFIG


# ------------------------------------------------------------------ verify
def test_verify_checks_every_database_and_counts_decisions_and_usage(bs, tmp_path, capsys):
    s, root, dest = settings_at(tmp_path), checkout(tmp_path), tmp_path / "backups"
    assert bs.run_backup(s, root, dest, 14, say=lambda *a: None) == 0
    assert bs.main(["--dest", str(dest), "--verify"], settings=s) == 0            # default: the newest zip
    out = capsys.readouterr().out
    assert "data/instances/XAUUSD/app.db: integrity ok, ai_decisions 5 (manifest 5)" in out
    assert "data/shared/ai_usage.db: integrity ok, ai_usage 7 (manifest 7)" in out and "verify ok" in out


def _rewrite(z: Path, out: Path, *, manifest=None, extra: dict | None = None, drop: str | None = None,
             replace: dict | None = None) -> Path:
    with zipfile.ZipFile(z) as src, zipfile.ZipFile(out, "w") as dst:
        for info in src.infolist():
            if info.filename == drop:
                continue
            data = src.read(info.filename)
            if info.filename == "manifest.json" and manifest is not None:
                data = json.dumps(manifest(json.loads(data))).encode()
            if replace and info.filename in replace:
                data = replace[info.filename]
            dst.writestr(info.filename, data)
        for name, data in (extra or {}).items():
            dst.writestr(name, data)
    return out


def test_verify_rejects_a_changed_file_a_missing_file_an_unlisted_file_and_other_row_counts(bs, tmp_path):
    s, root, dest = settings_at(tmp_path), checkout(tmp_path), tmp_path / "backups"
    assert bs.run_backup(s, root, dest, 14, say=lambda *a: None) == 0
    z = only_zip(dest)
    work = tmp_path / "w"

    def bump(m):
        for f in m["files"]:
            if f["path"] == "data/instances/BTCUSDT/app.db":
                f["rows"]["ai_decisions"] += 1
        return m

    cases = {
        "changed": _rewrite(z, tmp_path / "c.zip", replace={"data/shared/account_peak.json": b"{}"}),
        "missing": _rewrite(z, tmp_path / "m.zip", drop="data/shared/notify_state.json"),
        "unlisted": _rewrite(z, tmp_path / "u.zip", extra={"data/shared/extra.json": b"{}"}),
        "rows": _rewrite(z, tmp_path / "r.zip", manifest=bump),
    }
    expect = {"changed": "size/sha256 differ", "missing": "missing from the zip", "unlisted": "not in the manifest",
              "rows": "row counts differ from the manifest in ai_decisions"}
    for key, path in cases.items():
        problems, _ = bs.verify(path, work / key)
        assert any(expect[key] in p for p in problems), (key, problems)
        assert bs.run_verify(path, say=lambda *a: None) == bs.EXIT_FAILED
    assert bs.verify(z, work / "ok")[0] == []


def test_verify_rejects_env_and_path_escapes(bs, tmp_path):
    s, root, dest = settings_at(tmp_path), checkout(tmp_path), tmp_path / "backups"
    assert bs.run_backup(s, root, dest, 14, say=lambda *a: None) == 0
    z = only_zip(dest)
    for arc in ("../evil.txt", "data/../../evil.txt", "C:/Windows/evil.txt", ".env", "data/shared/.env",
                "logs/engine.jsonl"):
        def add(m, arc=arc):
            m["files"].append({"path": arc, "kind": "file", "size": 1, "sha256": "0" * 64})
            return m
        bad = _rewrite(z, tmp_path / "bad.zip", manifest=add, extra={arc: b"x"})
        problems, _ = bs.verify(bad, tmp_path / "w")
        assert any(repr(arc) in p for p in problems), (arc, problems)
        assert not (tmp_path / "evil.txt").exists()


# ------------------------------------------------------------------ restore
def test_restore_round_trip_into_a_scratch_root(bs, tmp_path, capsys):
    s, root, dest = settings_at(tmp_path), checkout(tmp_path), tmp_path / "backups"
    assert bs.run_backup(s, root, dest, 14, say=lambda *a: None) == 0
    target, cfg = tmp_path / "scratch" / "data", tmp_path / "scratch" / "config"
    assert bs.main(["--restore", str(only_zip(dest)), "--target", str(target), "--config-dir", str(cfg)],
                   settings=s) == 0
    for i, pair in enumerate(PAIRS):
        assert rows(target / "instances" / pair / "app.db", "ai_decisions") == 3 + i
    assert rows(target / "shared" / "ai_usage.db", "ai_usage") == 7
    assert (target / "shared" / "account_peak.json").read_text(encoding="utf-8") == \
        (tmp_path / "data" / "shared" / "account_peak.json").read_text(encoding="utf-8")
    assert (cfg / "config.local.yaml").read_text(encoding="utf-8").startswith("monitor:")
    assert (target / "adaptive" / "BTCUSDT" / "overlay.json").exists()
    assert not list((tmp_path / "scratch").rglob(".env*")) and not list((tmp_path / "scratch").rglob("*stderr*"))
    assert "restore done" in capsys.readouterr().out


def test_restore_without_config_dir_leaves_the_config_alone_and_says_so(bs, tmp_path, capsys):
    s, root, dest = settings_at(tmp_path), checkout(tmp_path), tmp_path / "backups"
    assert bs.run_backup(s, root, dest, 14, say=lambda *a: None) == 0
    target = tmp_path / "scratch" / "data"
    assert bs.main(["--restore", str(only_zip(dest)), "--target", str(target)], settings=s) == 0
    assert "config/config.local.yaml NOT restored" in capsys.readouterr().out
    assert not list((tmp_path / "scratch").rglob("config.local.yaml"))


def test_restore_puts_the_old_cli_capability_file_into_data_shared(bs, tmp_path):
    s, root, dest = settings_at(tmp_path), checkout(tmp_path), tmp_path / "backups"
    (tmp_path / "data" / "shared" / "cli_capabilities.json").unlink()            # a machine before Phase 5 A7
    bs.CLI_CAPS_OLD.parent.mkdir(parents=True)
    bs.CLI_CAPS_OLD.write_text('{"stream_json_user_shape": "old"}', encoding="utf-8")
    assert bs.run_backup(s, root, dest, 14, say=lambda *a: None) == 0
    target = tmp_path / "scratch" / "data"
    assert bs.run_restore(s, only_zip(dest), target, config_dir=None, overwrite=False, say=lambda *a: None) == 0
    assert "old" in (target / "shared" / "cli_capabilities.json").read_text(encoding="utf-8")


def test_restore_refuses_existing_files_and_overwrite_moves_them_aside(bs, tmp_path, capsys):
    s, root, dest = settings_at(tmp_path), checkout(tmp_path), tmp_path / "backups"
    assert bs.run_backup(s, root, dest, 14, say=lambda *a: None) == 0
    z = only_zip(dest)
    target = tmp_path / "scratch" / "data"
    db = target / "instances" / "BTCUSDT" / "app.db"
    db.parent.mkdir(parents=True)
    db.write_bytes(b"newer state")
    db.with_name("app.db-wal").write_bytes(b"a stale WAL of another database")
    assert bs.run_restore(s, z, target, config_dir=None, overwrite=False) == bs.EXIT_REFUSED
    assert db.read_bytes() == b"newer state" and "exists:" in capsys.readouterr().out
    assert bs.run_restore(s, z, target, config_dir=None, overwrite=True, stamp="20260928T090000Z",
                          say=lambda *a: None) == 0
    assert rows(db, "ai_decisions") == 3
    assert (db.with_name("app.db.pre-restore-20260928T090000Z")).read_bytes() == b"newer state"
    assert (db.with_name("app.db-wal.pre-restore-20260928T090000Z")).exists()
    assert not db.with_name("app.db-wal").exists()                  # never replayed into the restored database


def test_restore_never_rolls_back_the_account_peak_or_a_drawdown_stop(bs, tmp_path, capsys):
    """A drawdown stop tripped after the backup must survive a restore, even with --overwrite; where no peak file
    exists (a rebuild from zero) the backup's is restored."""
    s, root, dest = settings_at(tmp_path), checkout(tmp_path), tmp_path / "backups"
    assert bs.run_backup(s, root, dest, 14, say=lambda *a: None) == 0
    target = tmp_path / "scratch" / "data"
    tripped = json.dumps({"mt5:WindsorBrokers1-Demo:1": {"peak": 112.0, "peak_ms": 2, "tripped_ms": 3,
                                                          "tripped_equity": 90.0}})
    (target / "shared").mkdir(parents=True)
    (target / "shared" / "account_peak.json").write_text(tripped, encoding="utf-8")
    assert bs.run_restore(s, only_zip(dest), target, config_dir=None, overwrite=True) == 0
    assert (target / "shared" / "account_peak.json").read_text(encoding="utf-8") == tripped
    assert not (target / "shared" / "account_peak.json.bak").exists()       # never an older fallback copy either
    assert not list((target / "shared").glob("account_peak.json*.pre-restore-*"))
    assert "account_peak.json kept as it is" in capsys.readouterr().out
    fresh = tmp_path / "fresh" / "data"
    assert bs.run_restore(s, only_zip(dest), fresh, config_dir=None, overwrite=False, say=lambda *a: None) == 0
    assert json.loads((fresh / "shared" / "account_peak.json").read_text(encoding="utf-8")) == \
        json.loads((tmp_path / "data" / "shared" / "account_peak.json").read_text(encoding="utf-8"))
    assert (fresh / "shared" / "account_peak.json.bak").exists()


def test_restore_refuses_while_a_supervisor_of_the_target_runs(bs, tmp_path, capsys):
    s, root, dest = settings_at(tmp_path), checkout(tmp_path), tmp_path / "backups"
    assert bs.run_backup(s, root, dest, 14, say=lambda *a: None) == 0
    target = tmp_path / "scratch" / "data"
    state = target / "instances" / "ETHUSDT"
    state.mkdir(parents=True)
    name = winops.instance_name("supervisor", state)
    assert winops.acquire_instance(name)
    try:
        assert bs.run_restore(s, only_zip(dest), target, config_dir=None, overwrite=True) == bs.EXIT_REFUSED
        assert "ETHUSDT (single-instance lock held)" in capsys.readouterr().out
    finally:
        winops.release_instance(name)
    assert not (state / "app.db").exists()


def test_restore_refuses_an_older_supervisor_by_its_data_dir(bs, monkeypatch, tmp_path):
    """A supervisor build without the single-instance lock is found by its command line (--data-dir)."""
    s = settings_at(tmp_path)
    target = tmp_path / "scratch" / "data"
    target.mkdir(parents=True)

    class P:
        def __init__(self, pid):
            self.pid = pid

        def cmdline(self):
            return ["python.exe", "-m", "tradingsystem", "run", "all", "--data-dir", str(target)]

        def cwd(self):
            return str(tmp_path)

    monkeypatch.setattr(procs, "running_supervisors", lambda older_s=None: {4242: None})
    monkeypatch.setattr(bs.psutil, "Process", P)
    assert bs.target_supervisors(s, target) == ["pid 4242 (all pairs)"]
    assert bs.target_supervisors(s, tmp_path / "elsewhere") == []


def test_restore_finds_a_supervisor_whose_working_folder_cannot_be_read_by_its_interpreter(bs, monkeypatch, tmp_path):
    """Windows denies reading the working folder of a supervisor started outside our job (seen in production on
    2026-09-28): its checkout is then the one of its interpreter, <checkout>\\.venv\\Scripts\\python.exe."""
    s = load_settings(extra_env={"TS_INSTANCE": ""})                       # paths.data_dir relative: "data"
    exe = {4243: tmp_path / "checkout" / ".venv" / "Scripts" / "python.exe", 4244: tmp_path / "python.exe"}

    class P:
        def __init__(self, pid):
            self.pid = pid

        def cmdline(self):
            return [str(exe[self.pid]), "-m", "tradingsystem", "run", "all", "--instance", "BTCUSDT"]

        def cwd(self):
            raise bs.psutil.AccessDenied(self.pid)

        def exe(self):
            return str(exe[self.pid])

    monkeypatch.setattr(bs.psutil, "Process", P)
    monkeypatch.setattr(procs, "running_supervisors", lambda older_s=None: {4243: "BTCUSDT"})
    assert bs.target_supervisors(s, tmp_path / "checkout" / "data") == ["pid 4243 (BTCUSDT)"]
    assert bs.target_supervisors(s, tmp_path / "scratch" / "data") == []
    monkeypatch.setattr(procs, "running_supervisors", lambda older_s=None: {4244: "BTCUSDT"})
    assert bs.target_supervisors(s, tmp_path / "scratch" / "data") == ["pid 4244 (its data root cannot be read)"]


def test_restore_refuses_a_zip_that_does_not_verify(bs, tmp_path):
    s, root, dest = settings_at(tmp_path), checkout(tmp_path), tmp_path / "backups"
    assert bs.run_backup(s, root, dest, 14, say=lambda *a: None) == 0
    bad = _rewrite(only_zip(dest), tmp_path / "bad.zip", replace={"data/shared/ai_usage.db": b"garbage"})
    target = tmp_path / "scratch" / "data"
    assert bs.run_restore(s, bad, target, config_dir=None, overwrite=True, say=lambda *a: None) == bs.EXIT_FAILED
    assert not target.exists() or not any(target.rglob("*"))


# ------------------------------------------------------------------ wiring
def test_backup_task_runs_daily_at_0330_utc_quietly_with_a_30_minute_limit():
    ops = (ROOT / "scripts" / "install_operator_tasks.ps1").read_text(encoding="ascii")
    assert re.search(r'\[string\]\$BackupUtc = "03:30"', ops) and re.search(r"\[int\]\$BackupLimitMinutes = 30", ops)
    assert '$backupPy = Join-Path $root "tools\\backup_state.py"' in ops
    assert "-Execute $pyw -Argument \"`\"$backupPy`\" --quiet\"" in ops
    assert "$backupTrigger.StartBoundary = $backupAt.ToString($fmt)" in ops           # UTC, no DST shift
    assert "(New-OpsSettings $BackupLimitMinutes)" in ops
    assert "foreach ($name in $names[1], $names[2], $names[3])" in ops                 # the UTC read-back covers it
    assert ops.index('"TradingSystemOps-Backup"') < ops.index("# ---------------------------------------------------------------- uninstall")


def test_restart_all_backs_up_before_the_stop_and_a_failure_never_blocks_it():
    bat = (ROOT / "scripts" / "restart_all.bat").read_text(encoding="ascii")
    raw = (ROOT / "scripts" / "restart_all.bat").read_bytes()
    assert b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b"")                 # CRLF only
    call = bat.index('"%PY%" tools\\backup_state.py --quiet')
    assert call < bat.index("run --stop")
    between = bat[call:bat.index(":stopping")]
    assert 'set "RC=' not in between and "goto end" not in between and "exit /b" not in between
    assert "WARNING: the backup ended with exit code %BRC% - restarting anyway" in between


def test_backup_tool_documents_its_exit_codes(bs):
    doc = bs.__doc__
    for code in ("0 done", "1 failed", "2 bad arguments", "3 the config", "4 another backup run", "5 ``--restore`` refused"):
        assert code in doc
    assert (bs.EXIT_OK, bs.EXIT_FAILED, bs.EXIT_USAGE, bs.EXIT_CONFIG, bs.EXIT_BUSY, bs.EXIT_REFUSED) == (0, 1, 2, 3, 4, 5)
