"""One-time move to one independent system per pair (D-042):

    python tools/migrate_instance.py BTCUSDT [ETHUSDT ...]      or      python tools/migrate_instance.py --all

For each pair, ``data/instances/<PAIR>/app.db`` becomes a consistent copy (SQLite backup API) of ``data/app.db``
that keeps only that pair: its decisions (with their payloads and sub-agent outputs — the model's memory, history
and performance continue) and its paper legs (the paper account's realised PnL is recomputed from them). Heartbeats,
latest quotes and the event log are dropped (the services rewrite them). The AI usage of ``data/app.db`` is copied
once into the ledger every instance shares, ``data/shared/ai_usage.db`` (the daily request cap keeps counting).

``data/app.db`` itself is not changed (the all-pairs system can still be started again). Market data (``data/hot``,
``data/cold``) is shared and needs nothing. Refuses while any trading-system process runs; a pair that already has its
own app.db is skipped (``--force`` replaces it after keeping a copy ``app.db.bak-<time>``).
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
import time
from pathlib import Path

import psutil

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tradingsystem.ai.budget import UsageStore  # noqa: E402
from tradingsystem.core.settings import load_settings  # noqa: E402

KEEP_BY_PAIR = ("ai_decisions", "ai_payloads", "paper_legs")
EMPTY = ("collector_status", "latest_quote", "ingestion_events", "ai_usage")


def running_services() -> list[str]:
    """Every ``python -m tradingsystem ...`` process (supervisor or service) but this one."""
    me, out = psutil.Process().pid, []
    for p in psutil.process_iter(["name"]):
        if p.pid == me or not (p.info.get("name") or "").lower().startswith("python"):
            continue
        try:
            cmd = p.cmdline()
        except (psutil.Error, OSError):
            continue
        if "tradingsystem" in cmd and cmd[cmd.index("tradingsystem") - 1] == "-m":
            out.append(f"pid {p.pid}: {' '.join(cmd[cmd.index('tradingsystem'):])[:120]}")
    return out


def tables(con: sqlite3.Connection) -> set[str]:
    return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def copy_for_pair(src: Path, dst: Path, pair: str) -> dict[str, int]:
    """``dst`` = ``src`` restricted to ``pair``. Returns the rows kept per table."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".tmp")
    tmp.unlink(missing_ok=True)
    s = sqlite3.connect(f"file:{src.as_posix()}?mode=ro", uri=True, timeout=30)
    d = sqlite3.connect(tmp)
    try:
        s.backup(d)                               # consistent snapshot, WAL included
    finally:
        s.close()
    try:
        have = tables(d)
        for t in KEEP_BY_PAIR:
            if t in have:
                d.execute(f"DELETE FROM {t} WHERE pair IS NOT ?", (pair,))
        if "ai_sub_outputs" in have and "ai_decisions" in have:
            d.execute("DELETE FROM ai_sub_outputs WHERE decision_id NOT IN (SELECT id FROM ai_decisions)")
        for t in EMPTY:
            if t in have:
                d.execute(f"DELETE FROM {t}")
        if "paper_account" in have and "paper_legs" in have:
            d.execute("UPDATE paper_account SET realized_usd = (SELECT COALESCE(sum(pnl_usd), 0) FROM paper_legs "
                      "WHERE status='closed')")
        d.commit()
        kept = {t: d.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in (*KEEP_BY_PAIR, "ai_sub_outputs")
                if t in have}
        d.execute("VACUUM")
    finally:
        d.close()
    tmp.replace(dst)
    return kept


def seed_ledger(src: Path, ledger: Path) -> int:
    """Copy the AI usage rows of ``src`` into the shared ledger once (only while the ledger has no rows)."""
    UsageStore(ledger).close()                    # creates the schema (with every migrated column)
    con = sqlite3.connect(f"file:{ledger.as_posix()}", uri=True, timeout=30)     # uri: the read-only ATTACH below
    try:
        if con.execute("SELECT count(*) FROM ai_usage").fetchone()[0]:
            return 0
        con.execute("ATTACH DATABASE ? AS src", (f"file:{src.as_posix()}?mode=ro",))
        if "ai_usage" not in {r[0] for r in con.execute("SELECT name FROM src.sqlite_master WHERE type='table'")}:
            return 0
        cols = [r[1] for r in con.execute("PRAGMA src.table_info(ai_usage)") if r[1] != "id"]
        mine = {r[1] for r in con.execute("PRAGMA main.table_info(ai_usage)")}
        cols = [c for c in cols if c in mine]
        n = con.execute(f"INSERT INTO main.ai_usage ({','.join(cols)}) SELECT {','.join(cols)} FROM src.ai_usage "
                        "ORDER BY ts").rowcount
        con.commit()
        return n
    finally:
        con.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("pairs", nargs="*", help="pairs to give their own app.db (config 'instances:')")
    ap.add_argument("--all", action="store_true", help="every pair in config 'instances:'")
    ap.add_argument("--force", action="store_true", help="replace a pair's existing app.db (a backup is kept)")
    a = ap.parse_args(argv)
    s = load_settings()
    configured = list(s.instances)
    pairs = configured if a.all else [p.upper() for p in a.pairs]
    if not pairs:
        ap.error("name the pairs, or --all")
    unknown = [p for p in pairs if p not in configured]
    if unknown:
        print(f"not in config 'instances:': {unknown} (configured: {configured})")
        return 2
    busy = running_services()
    if busy:
        print("trading-system processes are running - stop them first (scripts\\stop.bat, scripts\\stop_all.bat):")
        print("  " + "\n  ".join(busy))
        return 1
    src = s.paths.data() / "app.db"
    if not src.exists():
        print(f"{src} does not exist - nothing to migrate; each pair's system starts with an empty app.db")
        return 0
    for pair in pairs:
        dst = s.paths.data() / "instances" / pair / "app.db"
        if dst.exists() and not a.force:
            print(f"{pair}: already has its own app.db ({dst}) - left as it is (--force replaces it)")
            continue
        if dst.exists():
            bak = dst.with_name(f"app.db.bak-{time.strftime('%Y%m%d-%H%M%S')}")
            shutil.copy2(dst, bak)
            for ext in ("-wal", "-shm"):
                Path(str(dst) + ext).unlink(missing_ok=True)
            print(f"{pair}: previous app.db kept as {bak.name}")
        kept = copy_for_pair(src, dst, pair)
        print(f"{pair}: {dst} - " + ", ".join(f"{t} {n}" for t, n in kept.items()))
    ledger = s.paths.shared() / "ai_usage.db"
    n = seed_ledger(src, ledger)
    print(f"AI usage ledger {ledger}: " + (f"{n} rows copied from {src.name}" if n else "already filled (unchanged)"))
    print("data\\app.db is unchanged (the all-pairs system can still be started with scripts\\start.bat).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
