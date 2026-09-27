"""Kill switch ON (§3.8, for a diagnosis session and for scripts): new orders stop; OFF stays a deliberate user action.

    python tools/kill_switch.py --pair BTCUSDT --reason "order burst: 5 orders in 40 min"   → that pair's system
    python tools/kill_switch.py --all --reason "..."                                          → every system
    python tools/kill_switch.py --status                                                      → which switches are on

Writes ``data/instances/<PAIR>/KILL_SWITCH`` (or ``data/KILL_SWITCH`` with ``--all``) through ``core.killswitch`` under
the data root of the loaded settings (``TRADINGSYSTEM_CONFIG`` honoured — a scratch run never touches production), with
who and why in the file; an existing switch is kept as it is. Open positions keep their stop-loss/take-profit and the
protective actions keep running. The owner switches it off with ``scripts\\kill_switch_off.bat [PAIR]`` — never from
here (deliberate friction). A notification (critical) says what was engaged.

The global switch needs ``--all`` explicitly: a forgotten ``--pair`` must never stop every system. An operator session
(``TS_OPERATOR_SESSION=1`` in its environment, set by the session runner) may engage one pair's switch only: ``--all``
is refused there — the global switch is the monitor's decision (§3.8) or the owner's — and so is, with nothing
written, a second pair's switch in the same review (another pair's file already carries this session's actor) and a
switch that would leave no pair trading (the global switch on, or every other pair's switch on: the configured
instances, else the enabled pairs). A session's switch records the actor ``operator-session:<review id>`` (the id the
runner exports as ``TS_OPERATOR_REVIEW_ID``; ``operator-session`` without one) — ``--actor`` is ignored there, so
the owner can tell a session's stop from their own or the monitor's.
Exit codes: 0 engaged (or already on; --status), 1 the file could not be written, 2 refused (in a session: ``--all``,
a second pair, the last pair trading), 3 invalid request.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tradingsystem.core.killswitch import kill_switch_path, read_reason, set_kill_switch  # noqa: E402
from tradingsystem.core.settings import Settings, load_settings  # noqa: E402

EXIT_OK, EXIT_ERROR, EXIT_REFUSED, EXIT_INVALID = 0, 1, 2, 3
PAIR_RE = re.compile(r"^[A-Z0-9]{2,20}$")
ACTOR_RE = re.compile(r"^[\w.@:+-]{1,40}$")
MAX_REASON = 500
SESSION_ENV = "TS_OPERATOR_SESSION"             # "1" in every operator session's environment (the session runner)
REVIEW_ENV = "TS_OPERATOR_REVIEW_ID"            # the session's review id (exported by the session runner)
SESSION_ACTOR = "operator-session"              # a session's actor: operator-session:<review id>
REVIEW_ID_RE = re.compile(r"^[\w.:+-]{1,80}$")


class Invalid(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:           # argparse would exit 2
        raise Invalid(f"{self.prog}: {message}")


def setup_log(s: Settings) -> None:
    """The notifier's log line goes to logs/kill-switch.jsonl (replaced in tests)."""
    from tradingsystem.core.logsetup import setup_logging
    setup_logging("kill-switch", logs_dir=s.paths.logs(), console=False, secret_env_names=s.secret_env_names())


def notify(s: Settings, level: str, title: str, text: str, key: str | None = None) -> None:
    try:
        from tradingsystem.core import notify as nt
    except Exception:  # noqa: BLE001 — the switch is what matters; the notifier ships with the same phase
        return
    try:
        nt.notify(s, level, title, text, key=key)
        nt.flush(15.0)
    except Exception:  # noqa: BLE001
        pass


def in_session() -> bool:
    return os.environ.get(SESSION_ENV) == "1"


def session_actor() -> str:
    """The actor a session's switch records: ``operator-session:<review id>`` (the id the session runner exported),
    ``operator-session`` without a valid one — never what ``--actor`` says."""
    rid = (os.environ.get(REVIEW_ENV) or "").strip()
    return f"{SESSION_ACTOR}:{rid}" if REVIEW_ID_RE.fullmatch(rid) else SESSION_ACTOR


def trading_pairs(s: Settings) -> list[str]:
    """The pairs whose systems may trade: the configured instances (one system per pair, D-042 — with ``TS_INSTANCE``
    set only that instance's pair flag is on, so the instances list counts), else the enabled pairs."""
    if s.instances:
        return sorted(p for p in s.instances if p in s.pairs and (s.paths.instance or s.pairs[p].enabled))
    return sorted(s.enabled_pairs())


def session_refusal(s: Settings, pair: str, actor: str) -> str | None:
    """Why a session may not engage ``pair``'s switch (None: it may). One pair per review at most: refused when
    another pair's switch already carries this session's actor; and never the last pair still trading (the global
    switch on, or every other pair's switch on) — stopping everything is the monitor's decision or the owner's."""
    mine = [p for p in sorted(s.pairs) if p != pair and read_reason(kill_switch_path(s, p)).get("actor") == actor]
    if mine:
        return (f"this session ({actor}) already engaged the switch of {', '.join(mine)} - a session stops one pair "
                "at most; say in the summary what else the owner should stop")
    if kill_switch_path(s, None).exists():
        return "the global switch is on already (nothing trades) - a session adds no pair switch to it"
    if not [p for p in trading_pairs(s) if p != pair and not kill_switch_path(s, p).exists()]:
        return (f"{pair} is the last pair still trading - a session never stops every system (the monitor's decision "
                "or the owner's); say in the summary why the owner should")
    return None


def status(s: Settings) -> list[dict]:
    rows = [{"scope": "all", "path": kill_switch_path(s, None)}]
    rows += [{"scope": p, "path": kill_switch_path(s, p)} for p in sorted(s.pairs)]
    return [{"scope": r["scope"], "on": r["path"].exists(), "path": str(r["path"]),
             **({"reason": read_reason(r["path"])} if r["path"].exists() else {})} for r in rows]


def main(argv: list[str] | None = None, *, settings: Settings | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = _Parser(prog="kill_switch.py", description="engage a kill switch (ON only)")
    who = ap.add_mutually_exclusive_group(required=True)
    who.add_argument("--pair", help="that pair's system only")
    who.add_argument("--all", action="store_true", help="every system (data/KILL_SWITCH)")
    who.add_argument("--status", action="store_true", help="show which switches are on")
    ap.add_argument("--reason", help="why (required to engage; stored in the file and sent)")
    ap.add_argument("--actor", help="who (default operator; ignored in an operator session)")
    try:
        a = ap.parse_args(argv)
        session = in_session()
        if a.all and session:
            print("refused: a session may engage one pair's switch only (--pair PAIR); the global switch is the "
                  "monitor's decision or the owner's")
            return EXIT_REFUSED
        s = settings or load_settings()
        if a.status:
            print(json.dumps(status(s), indent=1, ensure_ascii=False))
            return EXIT_OK
        pair = None
        if a.pair is not None:
            pair = a.pair.strip().upper()
            if not PAIR_RE.fullmatch(pair) or pair not in s.pairs:
                raise Invalid(f"--pair {a.pair!r}: not a configured pair ({', '.join(sorted(s.pairs))})")
        reason = " ".join((a.reason or "").split())
        if not reason:
            raise Invalid("--reason is required")
        if session:
            actor = session_actor()                    # the audit trail shows where the stop came from
        else:
            actor = "operator" if a.actor is None else a.actor
            if not ACTOR_RE.fullmatch(actor):
                raise Invalid(f"--actor {a.actor!r}: letters, digits and . @ : + - only")
    except Invalid as exc:
        print(f"invalid: {exc}")
        return EXIT_INVALID
    except SystemExit as exc:                          # --help
        return int(exc.code or 0)
    if session and a.actor is not None and a.actor != actor:
        print(f"note: --actor is ignored in an operator session - recorded as {actor}")
    if session and pair is not None and not kill_switch_path(s, pair).exists():
        why = session_refusal(s, pair, actor)
        if why:
            print(f"refused: {why}")
            return EXIT_REFUSED
    try:
        setup_log(s)
    except OSError:
        pass
    try:
        path, created = set_kill_switch(s, pair, reason=reason[:MAX_REASON], actor=actor)
    except OSError as exc:
        print(f"error: cannot write the kill switch: {exc}")
        notify(s, "critical", f"kill switch {pair or 'ALL'} could NOT be engaged", f"{exc} — reason: {reason[:300]}")
        return EXIT_ERROR
    scope = pair or "every system"
    if created:
        print(f"KILL SWITCH ON for {scope}: {path} — no new orders; open trades keep their SL/TP. "
              f"Off: scripts\\kill_switch_off.bat{' ' + pair if pair else ''}")
        notify(s, "critical", f"kill switch ON: {pair or 'ALL'}", f"by {actor}: {reason[:600]}",
               key=f"kill_switch_{pair or 'all'}")
    else:
        print(f"kill switch for {scope} was already on ({path}): {json.dumps(read_reason(path), ensure_ascii=False)}")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
