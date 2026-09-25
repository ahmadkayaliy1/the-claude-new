"""Per-process logging: JSON lines to a rotating file + readable console output.

Every emitted line passes through :class:`Redactor`, which masks the values of all secret
environment variables and well-known API-key shapes (spec §9: never print keys in full).
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import logging.handlers
import os
import re
import sys
import threading
from pathlib import Path
from typing import Iterable

_KEY_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"AIza[0-9A-Za-z_\-]{30,}"),
    re.compile(r"xai-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"gsk_[A-Za-z0-9_\-]{16,}"),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{16,}"),
]
_MIN_SECRET_LEN = 6


def mask(value: str) -> str:
    """Keep a short prefix so humans can tell keys apart, hide the rest."""
    return f"{value[:4]}****" if len(value) > 8 else "****"


class Redactor:
    def __init__(self, secret_env_names: Iterable[str] = ()) -> None:
        self._values: list[str] = []
        self.refresh(secret_env_names)

    def refresh(self, secret_env_names: Iterable[str]) -> None:
        values = {os.environ.get(n, "") for n in secret_env_names}
        # longest first so a secret that contains another is masked whole
        self._values = sorted((v for v in values if len(v) >= _MIN_SECRET_LEN), key=len, reverse=True)

    def __call__(self, text: str) -> str:
        for v in self._values:
            if v in text:
                text = text.replace(v, mask(v))
        for pat in _KEY_PATTERNS:
            text = pat.sub(lambda m: (m.group(1) if m.groups() else "") + "****", text)
        return text


class JsonFormatter(logging.Formatter):
    def __init__(self, process_name: str, redactor: Redactor) -> None:
        super().__init__()
        self._process = process_name
        self._redact = redactor

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": _dt.datetime.fromtimestamp(record.created, tz=_dt.timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "proc": self._process,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        ctx = getattr(record, "ctx", None)
        if ctx:
            payload["ctx"] = ctx
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return self._redact(json.dumps(payload, default=str, ensure_ascii=False))


class ConsoleFormatter(logging.Formatter):
    def __init__(self, process_name: str, redactor: Redactor) -> None:
        super().__init__("%(asctime)s %(levelname)-7s [" + process_name + "] %(name)s: %(message)s")
        self._redact = redactor

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:  # noqa: N802
        return _dt.datetime.fromtimestamp(record.created, tz=_dt.timezone.utc).strftime("%H:%M:%S.%f")[:-3] + "Z"

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        ctx = getattr(record, "ctx", None)
        if ctx:
            text += " " + json.dumps(ctx, default=str, ensure_ascii=False)
        return self._redact(text)


_REDACTOR = Redactor()


def get_redactor() -> Redactor:
    return _REDACTOR


def setup_logging(
    process_name: str,
    *,
    logs_dir: Path,
    level: str = "INFO",
    max_bytes: int = 20 * 1024 * 1024,
    backups: int = 10,
    console: bool = True,
    secret_env_names: Iterable[str] = (),
) -> logging.Logger:
    """Configure the root logger for this process. Safe to call more than once."""
    _REDACTOR.refresh(secret_env_names)
    logs_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
        h.close()
    root.setLevel(level.upper())

    file_handler = logging.handlers.RotatingFileHandler(
        logs_dir / f"{process_name}.jsonl", maxBytes=max_bytes, backupCount=backups, encoding="utf-8"
    )
    file_handler.setFormatter(JsonFormatter(process_name, _REDACTOR))
    root.addHandler(file_handler)

    if console:
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(ConsoleFormatter(process_name, _REDACTOR))
        root.addHandler(stream)

    for noisy in ("httpx", "httpcore", "websockets", "urllib3", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    logging.captureWarnings(True)        # warnings → redacting handlers, not raw stderr
    _install_excepthooks()
    return logging.getLogger(process_name)


def _install_excepthooks() -> None:
    """Uncaught exceptions (main thread + threads) are logged through the redacting handlers (OPS-06/F8).

    Raw stderr of supervised children goes to ``logs/<service>.stderr.log``, which is not redacted.
    """
    crash = logging.getLogger("uncaught")

    def hook(t, v, tb) -> None:  # noqa: ANN001
        if issubclass(t, KeyboardInterrupt):
            sys.__excepthook__(t, v, tb)
            return
        crash.critical("uncaught %s", t.__name__, exc_info=(t, v, tb))

    def thread_hook(a: threading.ExceptHookArgs) -> None:
        if a.exc_type is SystemExit:
            return
        crash.critical("uncaught %s in thread %s", a.exc_type.__name__, getattr(a.thread, "name", "?"),
                       exc_info=(a.exc_type, a.exc_value, a.exc_traceback))

    sys.excepthook = hook
    threading.excepthook = thread_hook


def setup_from_settings(process_name: str, settings) -> logging.Logger:  # noqa: ANN001 (avoid import cycle)
    lc = settings.logging
    return setup_logging(
        process_name,
        logs_dir=settings.paths.logs(),
        level=lc.level,
        max_bytes=lc.max_bytes,
        backups=lc.backups,
        # TS_LOG_CONSOLE=0: set by the supervisor for its children (their stderr is a file; the .jsonl has it all)
        console=lc.console and os.environ.get("TS_LOG_CONSOLE", "").strip() != "0",
        secret_env_names=settings.secret_env_names(),
    )
