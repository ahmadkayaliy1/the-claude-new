"""Notifications (Phase 4, §3.8 component 5, D-043): tell the owner what the systems do without the desktop app.

``notify(s, level, title, text, key=..., pair=...)`` always writes a log line on the caller's thread (logger
``notify``: info → INFO, warn → WARNING, critical → WARNING prefixed ``[CRITICAL]`` — the health report and the review
pack count ERROR/CRITICAL lines as errors, and a notification is not one). Everything else happens in ONE daemon worker
thread per process, so the engine's event loop and the executor's 1-s loop never wait for a network call, a process
start or a file lock:

* **Windows toast** through ``scripts/notify.ps1`` (WinRT ``ToastNotificationManager`` under Windows PowerShell 5.1, no
  extra module). Title and text travel base64-encoded (PS 5.1 mangles native arguments with quotes, newlines or
  non-ASCII); the process gets no window (``CREATE_NO_WINDOW``) and a timeout.
* **Telegram** ``sendMessage`` via ``httpx`` when ``TELEGRAM_BOT_TOKEN`` and ``TELEGRAM_CHAT_ID`` are set (H18, the
  owner's action) — read with ``secret()``, which re-reads ``.env`` at most once a minute, so adding them needs no
  restart; skipped silently while either is missing. The token sits in the request URL and so in httpx's exception
  texts and its INFO log line; both are redacted here (the token and the chat id are replaced before anything is
  logged — the process redactor only knows values that were in ``os.environ`` at start-up).

Rules, in this order: ``TS_NOTIFY_DISABLE=1`` (the unit suite, dry runs) or ``notify.enabled: false`` → the log line
only; a level below ``notify.min_level`` → the log line only; no usable sink → done; more than
``notify.rate_per_hour`` sent by this process in the last hour → the log line only (one warning per hour); the same
``key`` sent by ANY system within ``notify.dedupe_minutes`` → skipped (``data/shared/notify_state.json`` under a file
lock — the three pair systems share one account and one owner). A sink failure is one warning per hour and sink in the
process log; nothing here ever raises into the caller.

Short-lived tools (``tools/notify.py``, the monitor, the session runner) call :func:`flush` before exiting: the worker
is a daemon thread and dies with the interpreter.
"""
from __future__ import annotations

import base64
import collections
import json
import logging
import os
import queue
import re
import subprocess
import threading
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import httpx

from .filelock import FileLock, locks_dir
from .logsetup import get_redactor
from .settings import PROJECT_ROOT, TELEGRAM_SECRET_ENV, Settings
from .timeutil import MS_PER_MINUTE, now_ms

log = logging.getLogger("notify")

LEVELS = ("info", "warn", "critical")
_ALIASES = {"warning": "warn", "crit": "critical", "fatal": "critical"}
DISABLE_ENV = "TS_NOTIFY_DISABLE"
STATE_FILE = "notify_state.json"
SCRIPT = PROJECT_ROOT / "scripts" / "notify.ps1"
TELEGRAM_API = "https://api.telegram.org"
TELEGRAM_MAX = 4000                     # sendMessage takes 4096; leave room for the header line
TITLE_MAX, TEXT_MAX = 200, 4000
TOAST_TEXT_MAX = 600                    # a toast shows a few lines; the rest is in the log and on Telegram
TOAST_MIN_TIMEOUT_S = 10.0              # powershell.exe + WinRT start in 1-3 s
QUEUE_SIZE = 200
LOCK_WAIT_S = 5.0
STATE_KEEP_MS = 1440 * MS_PER_MINUTE    # the longest dedupe window settings allow
STATE_MAX_KEYS = 1000
RATE_WINDOW_S = 3600.0
SINK_WARN_EVERY_S = 3600.0
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_BOT_URL_RE = re.compile(r"/bot[^/\s\"'<>]+")


def disabled() -> bool:
    """``TS_NOTIFY_DISABLE`` set (the unit suite, dry runs): notifications are log lines only."""
    return os.environ.get(DISABLE_ENV, "").strip().lower() not in ("", "0", "false", "no")


def norm_level(level: Any) -> str:
    """``info|warn|critical`` (``warning`` accepted); anything else is treated as ``warn`` (seen, never dropped)."""
    lv = str(level or "").strip().lower()
    lv = _ALIASES.get(lv, lv)
    return lv if lv in LEVELS else "warn"


def _rank(level: str) -> int:
    return LEVELS.index(norm_level(level))


def _clean(value: Any, limit: int) -> str:
    try:
        text = "" if value is None else str(value)
    except Exception:  # noqa: BLE001 — a broken __str__ must not cost the log line
        text = f"<{type(value).__name__}>"
    text = text.strip()
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _redact(text: str, *secrets: str | None) -> str:
    """Replace the given secret values (also URL-encoded) and any ``/bot<token>`` URL part, then apply the process
    redactor (key shapes, secret env values) — never rely on the latter alone."""
    out = str(text)
    for v in sorted({x for x in secrets if x}, key=len, reverse=True):
        for form in (v, urllib.parse.quote(v, safe=""), urllib.parse.quote(v)):
            out = out.replace(form, "****")
    out = _BOT_URL_RE.sub("/bot****", out)
    try:
        out = get_redactor()(out)
    except Exception:  # noqa: BLE001
        pass
    return out


class _BotUrlFilter(logging.Filter):
    """httpx logs every request at INFO with its URL — for Telegram the URL holds the bot token."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001
            return True
        if "/bot" in msg:
            record.msg, record.args = _BOT_URL_RE.sub("/bot****", msg), ()
        return True


def _guard_httpx_logs() -> None:
    lg = logging.getLogger("httpx")
    if not any(isinstance(f, _BotUrlFilter) for f in lg.filters):
        lg.addFilter(_BotUrlFilter())


_guard_httpx_logs()


# --------------------------------------------------------------------------- cross-process dedupe state
def _read_state(path: Path) -> dict[str, int]:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        return {}                            # corrupt or unreadable: start over (a duplicate beats a lost alert)
    keys = doc.get("keys") if isinstance(doc, dict) else None
    if not isinstance(keys, dict):
        return {}
    return {str(k): int(v) for k, v in keys.items() if isinstance(v, (int, float)) and not isinstance(v, bool)}


def _write_state(path: Path, keys: dict[str, int], now: int) -> None:
    """Atomic replace; a reader or writer of another process may hold the file for a moment (Windows)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({"keys": keys, "updated_ms": now}, indent=0, sort_keys=True), encoding="utf-8")
    for attempt in range(5):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.05 * (attempt + 1))


# --------------------------------------------------------------------------- toast
def _powershell() -> str:
    exe = Path(os.environ.get("SystemRoot") or r"C:\Windows") / "System32" / "WindowsPowerShell" / "v1.0" \
        / "powershell.exe"
    return str(exe) if exe.exists() else "powershell.exe"


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def toast_command(level: str, title: str, text: str) -> list[str]:
    """The ``notify.ps1`` command line: title and text base64 (UTF-8), never an empty argument."""
    title = _CONTROL_RE.sub("", title.replace("\n", " ")).strip() or "Trading system"
    text = _CONTROL_RE.sub("", text).strip()
    if len(text) > TOAST_TEXT_MAX:
        text = text[:TOAST_TEXT_MAX - 1] + "…"
    return [_powershell(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(SCRIPT),
            "-TitleB64", _b64(title), "-TextB64", _b64(text or " "), "-Level", norm_level(level)]


# --------------------------------------------------------------------------- the notifier
@dataclass
class _Item:
    s: Settings
    level: str
    title: str
    text: str
    key: str | None
    pair: str | None
    ts: int
    result: dict[str, Any]


class Notifier:
    """One per process in production (:func:`notify` uses the module's); tests build their own with an
    ``httpx.MockTransport`` and fake clocks. ``clock`` = wall clock in ms (dedupe, shared by processes),
    ``monotonic`` = seconds (the per-process rate limit and the sink-warning throttle)."""

    def __init__(self, *, transport: httpx.BaseTransport | None = None, clock: Callable[[], int] = now_ms,
                 monotonic: Callable[[], float] = time.monotonic, queue_size: int = QUEUE_SIZE) -> None:
        self.transport = transport
        self.clock, self.monotonic = clock, monotonic
        self._q: queue.Queue[_Item] = queue.Queue(maxsize=queue_size)
        self._cond = threading.Condition()
        self._pending = 0
        self._thread: threading.Thread | None = None
        self._start_lock = threading.Lock()
        self._rate_lock = threading.Lock()
        self._sent: collections.deque[float] = collections.deque()
        self._warned: dict[str, float] = {}
        self.results: collections.deque[dict[str, Any]] = collections.deque(maxlen=50)

    # ---- caller's thread
    def submit(self, s: Settings, level: str, title: str, text: str, *, key: str | None = None,
               pair: str | None = None) -> dict[str, Any]:
        """The log line now; the sinks later, in the worker. Returns the result record the worker fills in
        (``done`` turns True). Never raises."""
        res: dict[str, Any] = {"level": "warn", "title": "", "key": None, "pair": None, "ts": None, "log": False,
                               "queued": False, "why": None, "deduped": False, "rate_limited": False,
                               "toast": None, "telegram": None, "done": True}
        try:
            lvl = norm_level(level)
            ttl = _clean(title, TITLE_MAX) or "Trading system"
            txt = _clean(text, TEXT_MAX)
            k = _clean(key, 200) or None
            p = _clean(pair, 40) or None
            res.update(level=lvl, title=ttl, key=k, pair=p)
            self.results.append(res)
            res["log"] = self._log_line(lvl, ttl, txt, k, p)
            if disabled():
                res["why"] = f"log only ({DISABLE_ENV})"
                return res
            cfg = s.notify
            if not cfg.enabled:
                res["why"] = "log only (notify.enabled: false)"
                return res
            if _rank(lvl) < _rank(cfg.min_level):
                res["why"] = f"log only (below notify.min_level {cfg.min_level})"
                return res
            res["ts"] = int(self.clock())
            res.update(done=False, toast="pending", telegram="pending")
            self._enqueue(_Item(s, lvl, ttl, txt, k, p, res["ts"], res))
        except Exception as exc:  # noqa: BLE001 — a notification never breaks the caller
            res.update(done=True, why=f"not sent: {type(exc).__name__}")
            try:
                log.debug("notify failed", exc_info=True)
            except Exception:  # noqa: BLE001
                pass
        return res

    @staticmethod
    def _log_line(level: str, title: str, text: str, key: str | None, pair: str | None) -> bool:
        try:
            msg = f"{'[CRITICAL] ' if level == 'critical' else ''}{title}" + (f": {text}" if text else "")
            ctx = {"level": level, **({"key": key} if key else {}), **({"pair": pair} if pair else {})}
            log.log(logging.INFO if level == "info" else logging.WARNING, "%s", msg, extra={"ctx": ctx})
            return True
        except Exception:  # noqa: BLE001
            return False

    def _enqueue(self, item: _Item) -> None:
        with self._cond:
            self._pending += 1
        try:
            self._ensure_worker()
            item.result["queued"] = True                 # before the put: the worker may finish it at once
            self._q.put_nowait(item)
        except queue.Full:
            item.result["queued"] = False
            self._finish(item, why="queue full: log only")
            log.warning("notification queue full (%d waiting) - '%s' went to the log only", self._q.maxsize,
                        item.title)
        except BaseException:
            item.result["queued"] = False
            self._finish(item, why="not queued")
            raise

    def _ensure_worker(self) -> None:
        with self._start_lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._loop, name="notify", daemon=True)
                self._thread.start()

    def _finish(self, item: _Item, *, why: str | None = None) -> None:
        r = item.result
        if why:
            r["why"] = why
            for sink in ("toast", "telegram"):
                if r.get(sink) == "pending":
                    r[sink] = why
        r["done"] = True
        with self._cond:
            self._pending -= 1
            if self._pending <= 0:
                self._pending = 0
                self._cond.notify_all()

    def pending(self) -> int:
        with self._cond:
            return self._pending

    def flush(self, timeout_s: float = 15.0) -> bool:
        """Wait until every queued notification went through its sinks; False after ``timeout_s``."""
        end = time.monotonic() + max(float(timeout_s), 0.0)
        with self._cond:
            while self._pending > 0:
                left = end - time.monotonic()
                if left <= 0:
                    return False
                self._cond.wait(left)
        return True

    # ---- worker thread
    def _loop(self) -> None:
        while True:
            item = self._q.get()
            try:
                self.process(item)
            except Exception:  # noqa: BLE001 — the worker must outlive any one notification
                try:
                    log.debug("notification failed", exc_info=True)
                except Exception:  # noqa: BLE001
                    pass
                self._finish(item, why="failed")
            else:
                self._finish(item)
            finally:
                self._q.task_done()

    def process(self, item: _Item) -> dict[str, Any]:
        """The sinks for one notification, on the calling thread (the worker; tests call it directly)."""
        r, s = item.result, item.s
        cfg = s.notify
        toast_ok, toast_why = self._toast_wanted(item)
        token, chat = self._telegram_creds() if cfg.telegram else (None, None)
        r["toast"] = "pending" if toast_ok else toast_why
        r["telegram"] = ("pending" if token and chat else "off (notify.telegram: false)" if not cfg.telegram
                         else "skipped (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set)")
        if not (toast_ok or (token and chat)):
            return r
        if item.level != "critical" and not self._rate_room(cfg.rate_per_hour):    # a critical one always goes out
            r["rate_limited"] = True
            self._skip(r, f"rate limit {cfg.rate_per_hour}/hour: log only")
            self._note("rate", f"notification rate limit ({cfg.rate_per_hour}/hour in this process) reached - "
                               f"'{item.title}' and the next ones this hour go to the log only")
            return r
        if item.key and cfg.dedupe_minutes > 0 and not self._claim(s, item.key, item.ts, cfg.dedupe_minutes):
            r["deduped"] = True
            self._skip(r, f"deduped (key sent within {cfg.dedupe_minutes} min)")
            log.debug("notification '%s' deduped (key %s)", item.title, item.key)
            return r
        with self._rate_lock:
            self._sent.append(self.monotonic())
        if token and chat:
            r["telegram"] = self._telegram(item, token, chat)
        if toast_ok:
            r["toast"] = self._toast(item)
        return r

    @staticmethod
    def _skip(r: dict[str, Any], why: str) -> None:
        for sink in ("toast", "telegram"):
            if r.get(sink) == "pending":
                r[sink] = why

    def _rate_room(self, per_hour: int) -> bool:
        with self._rate_lock:
            now = self.monotonic()
            while self._sent and now - self._sent[0] >= RATE_WINDOW_S:
                self._sent.popleft()
            return len(self._sent) < per_hour

    def _note(self, kind: str, msg: str) -> None:
        """A sink / limit problem: a warning at most once an hour per kind, debug otherwise (never a flood)."""
        now = self.monotonic()
        last = self._warned.get(kind)
        if last is None or now - last >= SINK_WARN_EVERY_S:
            self._warned[kind] = now
            log.warning("%s", msg)
        else:
            log.debug("%s", msg)

    def _claim(self, s: Settings, key: str, ts: int, minutes: int) -> bool:
        """True = send (and the key is recorded for every system); False = the key went out within ``minutes``."""
        path = s.paths.shared() / STATE_FILE
        try:
            lock = FileLock(locks_dir(s) / "notify_state.lock")
            with lock.hold(timeout=LOCK_WAIT_S) as got:
                if not got:
                    self._note("state", f"{lock.path} busy for {LOCK_WAIT_S:.0f} s - sent without the dedupe check")
                    return True
                keys = _read_state(path)
                last = keys.get(key)
                if last is not None and abs(ts - last) < minutes * MS_PER_MINUTE:
                    return False
                keys[key] = ts
                keys = {k: v for k, v in keys.items() if ts - v < STATE_KEEP_MS}
                if len(keys) > STATE_MAX_KEYS:
                    keys = dict(sorted(keys.items(), key=lambda kv: kv[1])[-STATE_MAX_KEYS:])
                _write_state(path, keys, ts)
                return True
        except Exception as exc:  # noqa: BLE001
            self._note("state", f"notify state {path} unusable ({type(exc).__name__}: {exc}) - sent without it")
            return True

    # ---- sinks
    @staticmethod
    def _toast_wanted(item: _Item) -> tuple[bool, str]:
        cfg = item.s.notify
        if not cfg.toast:
            return False, "off (notify.toast: false)"
        if _rank(item.level) < _rank(cfg.toast_min_level):
            return False, f"off (below notify.toast_min_level {cfg.toast_min_level})"
        if os.name != "nt":
            return False, "off (not Windows)"
        if not SCRIPT.exists():
            return False, "off (scripts/notify.ps1 missing)"
        return True, ""

    def _toast(self, item: _Item) -> str:
        scope = self._scope(item)
        title = f"{'Warning: ' if item.level == 'warn' else 'CRITICAL: ' if item.level == 'critical' else ''}" \
                f"{scope + ' - ' if scope else ''}{item.title}"
        timeout = max(TOAST_MIN_TIMEOUT_S, float(item.s.notify.timeout_s))
        try:
            proc = subprocess.run(toast_command(item.level, _redact(title), _redact(item.text)), capture_output=True,
                                  timeout=timeout, creationflags=_NO_WINDOW)
        except subprocess.TimeoutExpired:
            why = f"timed out after {timeout:.0f} s"
        except Exception as exc:  # noqa: BLE001
            why = f"{type(exc).__name__}: {exc}"
        else:
            if proc.returncode == 0:
                return "shown"
            err = proc.stderr.decode("utf-8", "replace") if isinstance(proc.stderr, bytes) else str(proc.stderr or "")
            why = f"exit {proc.returncode}" + (f": {' '.join(err.split())[:200]}" if err.strip() else "")
        why = _redact(why)[:300]
        self._note("toast", f"toast failed: {why}")
        return f"failed: {why}"

    @staticmethod
    def _telegram_creds() -> tuple[str | None, str | None]:
        try:
            from ..ai.providers import base
            return base.secret(TELEGRAM_SECRET_ENV[0]), base.secret(TELEGRAM_SECRET_ENV[1])
        except Exception:  # noqa: BLE001
            return None, None

    @staticmethod
    def _scope(item: _Item) -> str:
        """The pair (or the instance) when the title does not already name it — three systems share one chat."""
        scope = item.pair or getattr(item.s.paths, "instance", None) or ""
        return "" if not scope or scope.upper() in item.title.upper() else scope

    def telegram_text(self, item: _Item) -> str:
        scope = self._scope(item)
        head = f"[{item.level.upper()}] {scope + ' - ' if scope else ''}{item.title}"
        text = _redact(head + (f"\n{item.text}" if item.text else ""))
        return text if len(text) <= TELEGRAM_MAX else text[:TELEGRAM_MAX - 1] + "…"

    def _telegram(self, item: _Item, token: str, chat: str) -> str:
        _guard_httpx_logs()
        try:
            with httpx.Client(timeout=float(item.s.notify.timeout_s), transport=self.transport) as client:
                resp = client.post(f"{TELEGRAM_API}/bot{token}/sendMessage",
                                   json={"chat_id": chat, "text": self.telegram_text(item),
                                         "disable_web_page_preview": True})
            try:
                body = resp.json()
            except ValueError:
                body = None
            if resp.status_code == 200 and isinstance(body, dict) and body.get("ok") is True:
                return "sent"
            desc = body.get("description") if isinstance(body, dict) else None
            why = f"HTTP {resp.status_code}" + (f" {desc}" if desc else "")
        except Exception as exc:  # noqa: BLE001 — httpx texts carry the URL, i.e. the token
            why = f"{type(exc).__name__}: {exc}"
        why = _redact(why, token, chat)[:300]
        self._note("telegram", f"Telegram sendMessage failed: {why}")
        return f"failed: {why}"


# --------------------------------------------------------------------------- module API (one worker per process)
_DEFAULT = Notifier()


def notify(s: Settings, level: str, title: str, text: str, *, key: str | None = None,
           pair: str | None = None) -> None:
    """Log line now; toast and Telegram in the background (see the module docstring). Never raises, never blocks."""
    try:
        _DEFAULT.submit(s, level, title, text, key=key, pair=pair)
    except Exception:  # noqa: BLE001
        pass


def flush(timeout_s: float = 15.0) -> None:
    """Short-lived tools call this before exiting: waits (≤ ``timeout_s``) for the worker to finish the queue."""
    try:
        _DEFAULT.flush(timeout_s)
    except Exception:  # noqa: BLE001
        pass


def pending() -> int:
    """Notifications of this process still queued or running (after a timed-out :func:`flush`)."""
    try:
        return _DEFAULT.pending()
    except Exception:  # noqa: BLE001
        return 0


def last_result() -> dict[str, Any] | None:
    """What happened to this process's latest notification (``tools/notify.py`` prints it); None before any."""
    try:
        return dict(_DEFAULT.results[-1]) if _DEFAULT.results else None
    except Exception:  # noqa: BLE001
        return None


__all__ = ["LEVELS", "Notifier", "notify", "flush", "pending", "last_result", "disabled", "norm_level",
           "toast_command"]
