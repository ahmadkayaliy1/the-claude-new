"""``python -m tradingsystem ingest --source binance|mt5|all [--backfill|--no-backfill] [--data-dir DIR]``

Each source runs in its own OS process (D-005/D-019): an MT5 stall can never block Binance and vice versa.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import multiprocessing as mp
import signal
import sys
from pathlib import Path

from ..core.instruments import InstrumentRegistry
from ..core.logsetup import setup_from_settings
from ..core.settings import PathsCfg, Settings, load_settings

log = logging.getLogger("ingest")


def _settings(data_dir: str | None) -> Settings:
    s = load_settings()
    if data_dir:
        s = s.model_copy(update={"paths": PathsCfg(data_dir=data_dir, logs_dir=s.paths.logs_dir)})
    return s


def run_binance(data_dir: str | None = None, backfill: bool = True) -> None:
    from .binance.service import BinanceLiveService
    from .common.appdb import AppDB

    s = _settings(data_dir)
    setup_from_settings("ingest-binance", s)
    appdb = AppDB(s.paths.data() / "app.db")
    svc = BinanceLiveService(s, InstrumentRegistry.from_settings(s), appdb)
    worker = None
    if backfill:
        from .binance.backfill import start_worker
        worker = start_worker(data_dir)

    async def main() -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, svc.stop.set)
            except NotImplementedError:    # Windows: fall back to KeyboardInterrupt
                pass
        await svc.run()

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("interrupted")
    finally:
        if worker is not None and worker.is_alive():
            worker.terminate()
        appdb.close()


def run_mt5(data_dir: str | None = None, backfill: bool = True) -> None:
    from .mt5.service import main as mt5_main
    mt5_main(data_dir=data_dir, backfill=backfill)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="tradingsystem ingest")
    ap.add_argument("--source", choices=["binance", "mt5", "all"], default="all")
    ap.add_argument("--no-backfill", action="store_true", help="live capture + short gap-fill only")
    ap.add_argument("--data-dir", help="override paths.data_dir (e.g. for a dry test)")
    args = ap.parse_args(argv)
    backfill = not args.no_backfill
    if args.source == "binance":
        run_binance(args.data_dir, backfill)
    elif args.source == "mt5":
        run_mt5(args.data_dir, backfill)
    else:
        ctx = mp.get_context("spawn")
        procs = [ctx.Process(target=run_binance, args=(args.data_dir, backfill), name="ingest-binance"),
                 ctx.Process(target=run_mt5, args=(args.data_dir, backfill), name="ingest-mt5")]
        for p in procs:
            p.start()
        try:
            for p in procs:
                p.join()
        except KeyboardInterrupt:
            for p in procs:
                p.terminate()
    return 0


if __name__ == "__main__":
    sys.exit(main())
