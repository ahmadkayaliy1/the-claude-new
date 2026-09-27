"""Command-line entry point: ``python -m tradingsystem <command>``.

Sub-commands import their implementation lazily so each process loads only what it needs
(RAM budget on 4 GB machines).
"""
from __future__ import annotations

import argparse
import importlib
import os
import runpy
import sys
from pathlib import Path

from .core.settings import INSTANCE_ENV, PROJECT_ROOT

# command -> ("module:function", phase that implements it)
_TARGETS: dict[str, tuple[str, str]] = {
    "ingest": ("tradingsystem.ingest.main:main", "P3/P4"),
    "engine": ("tradingsystem.analysis.engine:main", "P6/P8"),
    "executor": ("tradingsystem.execution.executor:main", "P9"),
    "api": ("tradingsystem.api.app:main", "P10"),
    "run": ("tradingsystem.supervisor.supervisor:main", "P5"),
}


def _dispatch(command: str, argv: list[str]) -> int:
    target, phase = _TARGETS[command]
    module_name, func_name = target.split(":")
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        if exc.name and module_name.startswith(exc.name):
            print(f"`{command}` is not implemented yet (planned in phase {phase}; see PROJECT_STATUS.md).")
            return 2
        raise
    return int(getattr(module, func_name)(argv) or 0)


def _run_research_script(folder: str, prefix: str, args: argparse.Namespace) -> int:
    scripts_dir = PROJECT_ROOT / "research" / folder
    scripts = sorted(p.stem for p in scripts_dir.glob(f"{prefix}*.py"))
    if not args.name:
        print(f"available {folder}:\n  " + "\n  ".join(scripts or ["(none yet)"]))
        return 0
    name = args.name if args.name.startswith(prefix) else f"{prefix}{args.name}"
    path = scripts_dir / f"{name}.py"
    if not path.exists():
        print(f"unknown script {args.name!r}; available: {scripts}")
        return 2
    sys.argv = [str(path), *args.rest]
    runpy.run_path(str(path), run_name="__main__")
    return 0


def _cmd_config(args: argparse.Namespace) -> int:
    from .core.instruments import InstrumentRegistry
    from .core.settings import load_settings

    settings = load_settings(Path(args.config) if args.config else None)
    if args.instances:                      # scripts/*_all.bat: one configured pair per line, nothing else
        print("\n".join(settings.instances))
        return 0
    print(f"config OK  hash={settings.config_hash}  profile={settings.profile}")
    print(f"AI: provider={settings.ai.active_provider} model={settings.provider_model(settings.ai.active_provider)} "
          f"mode={settings.ai.agent_mode} trigger={settings.ai.trigger_policy}")
    print(f"execution: mode={settings.execution.mode} trigger={settings.execution.trigger}")
    a, x = settings.ai, settings.execution          # Phase 3 switches: what is really in force after the merges
    print(f"phase 3: charts={'on' if a.charts.enabled else 'OFF'} escalation={'on' if a.escalation.enabled else 'off'} "
          f"({a.models.escalation.model}/{a.models.escalation.effort}) management="
          f"{'on' if x.management.enabled else 'OFF'}{' (DRY RUN)' if x.management.dry_run else ''} "
          f"position_actions={'on' if x.position_actions.enabled else 'OFF'} "
          f"calls/pair/day={a.daily_calls_per_pair}")
    print(_phase4_line(settings))
    reg = InstrumentRegistry.from_settings(settings)
    for pair in reg.pairs():
        print(f"  {pair}:")
        for inst in reg.for_pair(pair):
            print(f"    {inst.key:<24} roles={','.join(inst.roles):<32} data={','.join(inst.datatypes)}")
    return 0


def _phase4_line(settings) -> str:
    """Phase 4 switches in force (docs/ops_windows.md §8). Telegram shows only whether both values are set — never
    the values."""
    from .ai.providers.base import secret
    from .core.settings import TELEGRAM_SECRET_ENV

    n, m, o = settings.notify, settings.monitor, settings.operator
    freeze = (settings.paths.data() / "TUNING_FREEZE").exists()
    telegram = "configured" if all(secret(k) for k in TELEGRAM_SECRET_ENV) else "not configured"
    return (f"phase 4: adaptive={'on' if settings.adaptive.enabled else 'OFF'}{' (TUNING_FREEZE)' if freeze else ''} "
            f"notify={'on' if n.enabled else 'OFF'} toast={'on' if n.toast else 'off'} "
            f"telegram={'off' if not n.telegram else telegram} min_level={n.min_level} "
            f"monitor={'on' if m.enabled else 'OFF'} diagnose={'on' if m.diagnose_enabled else 'off'} "
            f"sessions={'on' if o.enabled else 'OFF'} "
            f"gauge={'ENFORCED' if settings.ai.usage.enforce else 'observe only'}")


def _cmd_status(_: argparse.Namespace) -> int:
    path = PROJECT_ROOT / "tools" / "check_status.py"
    sys.argv = [str(path)]
    runpy.run_path(str(path), run_name="__main__")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tradingsystem", description="AI-driven automated trading system",
                                epilog="--instance PAIR (anywhere on the line): run as the independent system of that "
                                       "pair (config 'instances:', D-042)")
    sub = p.add_subparsers(dest="command", required=True)

    pr = sub.add_parser("probe", help="run a data-exploration probe (research/probes)")
    pr.add_argument("name", nargs="?")
    pr.add_argument("rest", nargs=argparse.REMAINDER)
    pr.set_defaults(func=lambda a: _run_research_script("probes", "probe_", a))

    bn = sub.add_parser("bench", help="run a benchmark (research/bench), e.g. `bench storage`")
    bn.add_argument("name", nargs="?")
    bn.add_argument("rest", nargs=argparse.REMAINDER)
    bn.set_defaults(func=lambda a: _run_research_script("bench", "bench_", a))

    cf = sub.add_parser("config", help="validate and summarise the configuration")
    cf.add_argument("--config", help="path to an alternative config.yaml")
    cf.add_argument("--instances", action="store_true", help="list the pairs configured as independent systems")
    cf.set_defaults(func=_cmd_config)

    st = sub.add_parser("status", help="validate PROJECT_STATUS.md structure")
    st.set_defaults(func=_cmd_status)

    helps = {
        "ingest": "data ingestion service (--source binance|mt5|all)",
        "engine": "quantitative analysis + AI decision engine",
        "executor": "risk gate + execution service",
        "api": "dashboard backend (FastAPI) + web UI",
        "run": "supervisor: run everything with one command (`run all`; --detach | --stop | --status, scripts/*.bat)",
    }
    for name, text in helps.items():
        sp = sub.add_parser(name, help=text, add_help=False)
        sp.add_argument("rest", nargs=argparse.REMAINDER)
        sp.set_defaults(func=None)
    return p


def take_instance(argv: list[str]) -> tuple[list[str], str | None]:
    """Remove ``--instance PAIR`` / ``--instance=PAIR`` from ``argv`` (any position) → (rest, PAIR upper-cased)."""
    out, inst, i = [], None, 0
    while i < len(argv):
        a = argv[i]
        if a == "--instance":
            if i + 1 >= len(argv):
                raise SystemExit("--instance needs a pair, e.g. --instance BTCUSDT")
            inst, i = argv[i + 1], i + 2
            continue
        if a.startswith("--instance="):
            inst = a.split("=", 1)[1]
        else:
            out.append(a)
        i += 1
    inst = (inst or "").strip().upper() or None
    return out, inst


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    argv, instance = take_instance(list(sys.argv[1:] if argv is None else argv))
    if instance:            # every command (and every child process it starts) runs as that pair's system (D-042)
        os.environ[INSTANCE_ENV] = instance
    if argv and argv[0] in _TARGETS:            # service commands own their argument parsing
        return _dispatch(argv[0], argv[1:])
    args = parser.parse_args(argv)
    if args.func is not None:
        return int(args.func(args) or 0)
    return _dispatch(args.command, list(args.rest))
