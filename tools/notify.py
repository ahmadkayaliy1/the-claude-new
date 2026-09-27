"""Send one notification from a script or an operator session (Phase 4, §3.8 component 5; docs/notifications.md).

    python tools/notify.py --level warn --title "BTCUSDT review" --text "..." [--key K] [--pair P]

The same path as every notification of the system (``core/notify.py``): always the log line (``logs/notify.jsonl``,
or ``logs/<PAIR>/notify.jsonl`` for an instance), then a Windows toast and — when ``TELEGRAM_BOT_TOKEN`` and
``TELEGRAM_CHAT_ID`` are in ``.env`` (H18) — a Telegram message, under the same rules (``notify.min_level``, the rate
limit, dedupe by ``--key`` across every system, ``TS_NOTIFY_DISABLE``). The key is namespaced: ``--key K`` is sent as
``cli:K``, so a key chosen here (by a script or an operator session) can never match a system key such as
``kill_switch_<PAIR>`` or ``monitor:<finding>`` and dedupe that system's notification away. It waits for the toast and
Telegram (at most ``--wait`` seconds) and prints what happened to each; never the token or the chat id.
``TRADINGSYSTEM_CONFIG`` is honoured like in every tool (a scratch run logs and dedupes under its own data root).

Exit codes: 0 notified — also when a sink failed or was skipped (the log line is always written; a failed toast or
Telegram message is in the printed line and the log), 1 the settings could not be loaded, 3 invalid request.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tradingsystem.core import notify as nt  # noqa: E402
from tradingsystem.core.logsetup import get_redactor, setup_logging  # noqa: E402
from tradingsystem.core.settings import Settings, load_settings  # noqa: E402

EXIT_OK, EXIT_ERROR, EXIT_INVALID = 0, 1, 3
PAIR_RE = re.compile(r"^[A-Z0-9]{2,20}$")
KEY_MAX = 200
KEY_PREFIX = "cli:"          # this tool's dedupe namespace: never one of the system keys (kill_switch_, monitor:, …)


class Invalid(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:           # argparse would exit 2
        raise Invalid(f"{self.prog}: {message}")


def setup_log(s: Settings) -> None:
    """The notification's log line goes to logs[/<PAIR>]/notify.jsonl (replaced in tests: it resets the root
    logger). Never the console: the session runner reads this tool's stdout."""
    setup_logging("notify", logs_dir=s.paths.logs(), level=s.logging.level, max_bytes=s.logging.max_bytes,
                  backups=s.logging.backups, console=False, secret_env_names=s.secret_env_names())


def _print(text: str) -> None:
    if sys.stdout is None:                           # pythonw
        return
    try:
        print(get_redactor()(text))
    except (OSError, ValueError):
        pass


def summary(res: dict | None, still_pending: int) -> str:
    """One line: what the log, the toast and Telegram did with the notification."""
    if not res:
        return "notification: log line only (no result recorded)"
    head = f"notification {res.get('level')}: \"{res.get('title')}\""
    if res.get("why") and not res.get("ts"):
        return f"{head} - {res['why']}"
    parts = [f"log: {'written' if res.get('log') else 'FAILED'}"]
    for sink in ("toast", "telegram"):
        v = res.get(sink)
        parts.append(f"{sink}: {'still running' if v == 'pending' and still_pending else v}")
    return f"{head} - " + "; ".join(parts)


def main(argv: list[str] | None = None, *, settings: Settings | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = _Parser(prog="notify.py", description="send one notification (log line + toast + Telegram)")
    ap.add_argument("--level", required=True, help="info | warn | critical")
    ap.add_argument("--title", required=True)
    ap.add_argument("--text", required=True, help="the message (Telegram takes up to ~4000 characters)")
    ap.add_argument("--key", help="dedupe key (sent as cli:KEY): the same key is sent once per "
                                  "notify.dedupe_minutes by all systems")
    ap.add_argument("--pair", help="the pair it is about (shown when the title does not name it)")
    ap.add_argument("--wait", type=float, default=15.0, help="seconds to wait for the toast and Telegram (15)")
    try:
        a = ap.parse_args(argv)
        level = (a.level or "").strip().lower()
        if level == "warning":
            level = "warn"
        if level not in nt.LEVELS:
            raise Invalid(f"--level {a.level!r}: one of {', '.join(nt.LEVELS)}")
        title = " ".join((a.title or "").split())
        if not title:
            raise Invalid("--title is empty")
        key = (a.key or "").strip() or None
        if key is not None and (len(key) > KEY_MAX or any(c.isspace() for c in key)):
            raise Invalid(f"--key: at most {KEY_MAX} characters, no spaces")
        if key is not None:
            key = KEY_PREFIX + key
        pair = None
        if a.pair:
            pair = a.pair.strip().upper()
            if not PAIR_RE.fullmatch(pair):
                raise Invalid(f"--pair {a.pair!r}: letters and digits only")
        if not 0 <= a.wait <= 120:
            raise Invalid("--wait: 0 to 120 seconds")
    except Invalid as exc:
        _print(f"invalid: {exc}")
        return EXIT_INVALID
    except SystemExit as exc:                          # --help
        return int(exc.code or 0)
    try:
        s = settings or load_settings()
    except Exception as exc:  # noqa: BLE001
        _print(f"error: cannot load the settings: {type(exc).__name__}: {str(exc)[:300]}")
        return EXIT_ERROR
    try:
        setup_log(s)
    except OSError as exc:
        _print(f"warning: no log file ({exc}); sending anyway")
    nt.notify(s, level, title, a.text or "", key=key, pair=pair)
    nt.flush(a.wait)
    _print(summary(nt.last_result(), nt.pending()))
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
