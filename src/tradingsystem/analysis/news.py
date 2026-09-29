"""News blackout from a public weekly economic calendar (Phase 5 B8, D-046 a; gold tiers D-049).

The feed is Forex Factory's own weekly calendar export (``pairs.<PAIR>.news_blackout.source_url``, no key; verified
2026-09-29: a JSON list of ``{title, country, date (ISO with the New York offset), impact, forecast, previous}`` for the
week Sunday → Saturday New York time; the export allows 2 downloads per 5 minutes, so a pair's engine fetches it at
most once per ``refresh_minutes``). The engine of the pair that has the blackout on writes it ATOMICALLY to
``data/shared/news_calendar.json`` (a temp file + replace; the previous file stays when a download fails or is not a
calendar); every reader (engine, executor, snapshot) only reads that file.

Semantics:
* an event counts when its currency is in ``currencies`` and it falls in a tier with a window (:func:`tier_of`):
  ``fomc`` (FOMC statement, rate decision, press conference, economic projections — not the members' speeches),
  ``high`` (every other
  high-impact release), ``medium`` (PPI, retail sales, ISM, JOLTS, GDP when the feed rates them medium);
* the blackout at ``t`` = any counted event with ``time − before ≤ t < time + after``;
* the file is FRESH at ``t`` when ``t`` lies in the week it covers and it was fetched at most ``stale_hours`` before
  ``t`` (a file fetched after ``t`` — a replay of a past instant — counts when it covers ``t``: the calendar is
  published in advance). Not fresh = the capability is unavailable: the gate applies no blackout (fail-open with a
  warning, §3.9.1 a) while a desk pair's entry calls stop (D-049).
Nothing here raises to its caller: an unreadable file is "not fresh".
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from ..core.settings import NewsBlackoutCfg
from ..core.timeutil import MS_PER_HOUR, MS_PER_MINUTE, iso

log = logging.getLogger("engine")
NY = ZoneInfo("America/New_York")
FILE_NAME = "news_calendar.json"
FILE_VERSION = 1
FETCH_TIMEOUT_S = 20
FETCH_RETRY_MS = 15 * MS_PER_MINUTE          # after a failed download (the feed's limit is 2 per 5 minutes)
MAX_BYTES = 2_000_000
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) tradingsystem-news-calendar/1"
# the decision itself (not the members' speeches, not the minutes - those are ordinary high / low releases)
FOMC_RE = re.compile(r"FOMC (Statement|Press Conference|Economic Projections)|Federal Funds Rate", re.I)
MEDIUM_RE = re.compile(r"\bPPI\b|Retail Sales|\bISM\b|JOLTS|\bGDP\b", re.I)
NEXT_N = 2
NO_FETCH_ENV = "TS_NEWS_NO_FETCH"
TITLE_CHARS = 40


@dataclass(frozen=True)
class Event:
    time_ms: int
    title: str
    country: str
    impact: str


@dataclass(frozen=True)
class Calendar:
    fetched_ms: int
    source_url: str
    events: tuple[Event, ...]

    def week(self) -> tuple[int, int]:
        """[start, end) of the New York Sunday-to-Saturday week the file was fetched in (the feed's "this week")."""
        local = dt.datetime.fromtimestamp(self.fetched_ms / 1000, tz=NY)
        sunday = local.date() - dt.timedelta(days=(local.weekday() + 1) % 7)
        start = dt.datetime(sunday.year, sunday.month, sunday.day, tzinfo=NY)
        end_day = sunday + dt.timedelta(days=7)
        end = dt.datetime(end_day.year, end_day.month, end_day.day, tzinfo=NY)
        return int(start.timestamp() * 1000), int(end.timestamp() * 1000)

    def fresh(self, at_ms: int, stale_hours: float) -> bool:
        """``at_ms`` lies in the week of the fetch, the file holds that week's events (a download just after the week
        rolled can still serve last week's list) and it is at most ``stale_hours`` old."""
        lo, hi = self.week()
        if not lo <= at_ms < hi or not any(lo <= e.time_ms < hi for e in self.events):
            return False
        return at_ms - self.fetched_ms <= stale_hours * MS_PER_HOUR


# ------------------------------------------------------------------------------------------------- the feed
def parse_time(text: str) -> int:
    """The feed's ISO time with its offset (``2026-09-30T08:30:00-04:00``) → UTC ms; a naive time is refused."""
    t = dt.datetime.fromisoformat(text)
    if t.tzinfo is None:
        raise ValueError(f"event time without an offset: {text!r}")
    return int(t.timestamp() * 1000)


def parse_feed(raw: bytes | str) -> list[Event]:
    """The feed's JSON → events, soonest first. ValueError when it is not a calendar (e.g. the feed's HTML "Request
    Denied" page) — the caller then keeps the previous file."""
    data = json.loads(raw)
    if not isinstance(data, list):
        raise ValueError("the calendar feed did not return a list")
    out = []
    for e in data:
        if not isinstance(e, dict) or not isinstance(e.get("date"), str):
            continue
        try:
            t = parse_time(e["date"])
        except ValueError:
            continue
        out.append(Event(t, str(e.get("title") or "")[:120], str(e.get("country") or "").upper()[:8],
                         str(e.get("impact") or "")[:16]))
    if data and not out:
        raise ValueError("the calendar feed had no readable event")
    return sorted(out, key=lambda x: x.time_ms)


def tier_of(ev: Event, cfg: NewsBlackoutCfg) -> str | None:
    """The blackout tier of an event, or None when it does not count (another currency, low impact, a tier without a
    window)."""
    if ev.country not in {c.upper() for c in cfg.currencies}:
        return None
    impact = ev.impact.lower()
    if FOMC_RE.search(ev.title) and impact in ("high", "medium"):
        tier = "fomc"
    elif impact == "high":
        tier = "high"
    elif impact == "medium" and MEDIUM_RE.search(ev.title):
        tier = "medium"
    else:
        return None
    return tier if tier in cfg.windows else None


def fetch(cfg: NewsBlackoutCfg, dest: Path, now_ms: int, opener=None) -> Calendar:
    """Download the feed, check it is a calendar and replace ``dest`` atomically. Raises on any failure (the previous
    file is left as it was). ``TS_NEWS_NO_FETCH=1`` (the unit suite) refuses the real network path."""
    if opener is None and os.environ.get(NO_FETCH_ENV) == "1":
        raise RuntimeError(f"calendar downloads are disabled ({NO_FETCH_ENV}=1)")
    req = urllib.request.Request(cfg.source_url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with (opener or urllib.request.urlopen)(req, timeout=FETCH_TIMEOUT_S) as r:
        raw = r.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("the calendar feed is larger than expected")
    events = parse_feed(raw)
    cal = Calendar(now_ms, cfg.source_url, tuple(events))
    doc = {"version": FILE_VERSION, "fetched_ms": now_ms, "fetched": iso(now_ms), "source_url": cfg.source_url,
           "events": [[e.time_ms, e.title, e.country, e.impact] for e in events]}
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".news_", suffix=".tmp", dir=str(dest.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(doc, f, separators=(",", ":"))
        os.replace(tmp, dest)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return cal


def load(path: Path) -> Calendar | None:
    """The stored calendar, or None when there is none or it cannot be read (= not fresh)."""
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        if doc.get("version") != FILE_VERSION:
            return None
        events = tuple(Event(int(t), str(ti), str(c), str(im)) for t, ti, c, im in doc["events"])
        return Calendar(int(doc["fetched_ms"]), str(doc.get("source_url") or ""), events)
    except (OSError, ValueError, KeyError, TypeError):
        return None


def calendar_path(shared_dir: Path) -> Path:
    return shared_dir / FILE_NAME


# ------------------------------------------------------------------------------------------------- the state
@dataclass(frozen=True)
class NewsState:
    fresh: bool
    reason: str                              # why not fresh ("" when fresh)
    blackout: tuple[str, Event, int, int] | None    # (tier, event, window start, window end) covering the instant
    upcoming: tuple[tuple[str, Event], ...]  # the next counted events after the instant

    @property
    def blocked(self) -> bool:
        return self.fresh and self.blackout is not None


def state_at(cfg: NewsBlackoutCfg, cal: Calendar | None, at_ms: int) -> NewsState:
    if cal is None:
        return NewsState(False, "no news calendar file", None, ())
    if not cal.fresh(at_ms, cfg.stale_hours):
        lo, hi = cal.week()
        why = (f"news calendar stale (fetched {iso(cal.fetched_ms)}, covers {iso(lo)} to {iso(hi)})")
        return NewsState(False, why, None, ())
    hit, upcoming = None, []
    for e in cal.events:
        tier = tier_of(e, cfg)
        if tier is None:
            continue
        before, after = cfg.windows[tier]
        lo, hi = e.time_ms - before * MS_PER_MINUTE, e.time_ms + after * MS_PER_MINUTE
        if lo <= at_ms < hi and (hit is None or hi > hit[3]):
            hit = (tier, e, lo, hi)
        if e.time_ms > at_ms and len(upcoming) < NEXT_N:
            upcoming.append((tier, e))
    return NewsState(True, "", hit, tuple(upcoming))


def state_now(cfg: NewsBlackoutCfg, shared_dir: Path, at_ms: int) -> NewsState:
    """:func:`state_at` on the stored file. Never raises."""
    try:
        return state_at(cfg, load(calendar_path(shared_dir)), at_ms)
    except Exception as e:  # noqa: BLE001 - a broken file is "not fresh", never an error on a money path
        log.warning("news calendar: %r", e)
        return NewsState(False, f"news calendar unreadable ({type(e).__name__})", None, ())


def gate_check(st: NewsState) -> tuple[bool, str]:
    """The risk gate's ``news_blackout`` check: refused inside a window while the file is fresh; a stale file applies no
    blackout (it is said so in the detail — §3.9.1 a)."""
    if not st.fresh:
        return True, f"{st.reason} - no blackout applied"
    if st.blackout is None:
        return True, "no scheduled release within its blackout window"
    tier, e, lo, hi = st.blackout
    return False, (f"{e.country} {tier} release '{e.title}' at {iso(e.time_ms)}: no entry from {iso(lo)} to "
                   f"{iso(hi)}")


def block(st: NewsState, at_ms: int) -> dict:
    """``market.news`` for the model (≤ 60 tokens)."""
    if not st.fresh:
        return {"data_quality": "unavailable", "reason": st.reason}
    out: dict = {"data_quality": "real"}
    if st.blackout is not None:
        tier, e, _lo, hi = st.blackout
        out["blackout"] = [tier, e.title[:TITLE_CHARS], iso(e.time_ms), iso(hi)]
    else:
        out["blackout"] = "none"
    out["next"] = [[iso(e.time_ms), tier, e.title[:TITLE_CHARS], round((e.time_ms - at_ms) / MS_PER_MINUTE)]
                   for tier, e in st.upcoming]
    return out


# ------------------------------------------------------------------------------------------------- the fetcher
class Fetcher:
    """The engine's once-per-``refresh_minutes`` download (called from its loop; the download itself runs in a daemon
    thread so the screening never waits for the network)."""

    def __init__(self, cfg: NewsBlackoutCfg, shared_dir: Path, on_event=None) -> None:
        self.cfg, self.path = cfg, calendar_path(shared_dir)
        self.on_event = on_event or (lambda kind, text: None)
        self._next_try = 0
        self._thread = None

    def due(self, now_ms: int) -> bool:
        if now_ms < self._next_try or (self._thread is not None and self._thread.is_alive()):
            return False
        cal = load(self.path)
        if cal is None:
            return True
        return (now_ms - cal.fetched_ms >= self.cfg.refresh_minutes * MS_PER_MINUTE
                or not cal.fresh(now_ms, self.cfg.stale_hours))

    def maybe_fetch(self, now_ms: int, *, background: bool = True) -> bool:
        """Start a download when one is due; True when one was started (or, not in the background, succeeded)."""
        if not self.due(now_ms):
            return False
        self._next_try = now_ms + FETCH_RETRY_MS          # one try per 15 min at most, success or not

        def run() -> bool:
            try:
                cal = fetch(self.cfg, self.path, now_ms)
                log.info("news calendar fetched: %d events (%s)", len(cal.events), self.cfg.source_url)
                return True
            except Exception as e:  # noqa: BLE001 - the previous file stays; staleness is reported by the readers
                log.warning("news calendar download failed: %r", e)
                self.on_event("news_fetch_failed", f"{type(e).__name__}: {e}"[:300])
                return False

        if not background:
            return run()
        import threading
        self._thread = threading.Thread(target=run, name="news-calendar", daemon=True)
        self._thread.start()
        return True
