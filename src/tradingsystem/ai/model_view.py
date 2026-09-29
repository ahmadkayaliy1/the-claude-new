"""The model's view of a snapshot payload (Phase 1): the same information in fewer tokens.

The internal payload — stored, hashed, and read by the trigger policy, the data gate and the dashboard — is not
changed; only the text the model reads is compacted:

* timestamps ``MM-DD HH:MM`` (UTC, the year of ``meta.as_of``; another year is written in full);
* one ``data`` status per timeframe instead of the full quality record (details only when not ok);
* column tables for zones and structure events (the column names are in the cached system prompt);
* fewer raw candles on the higher timeframes (structure, zones and indicators summarise the analysed history);
* capability flags grouped by quality; null fields omitted; fixed explanations moved to
  ``prompts/shared/payload_legend.md`` (part of the cached system prompt).

``VIEW_VERSION`` changes whenever the rendering does (it is part of what the model saw, next to the payload hash).
"""
from __future__ import annotations

import re
from typing import Any

from ..core.timeutil import iso

VIEW_VERSION = "2"
RECENT = {"1w": 4, "1d": 6, "4h": 8, "1h": 8, "15m": 16, "5m": 12, "1m": 10}
ZONE_COLS = ("dir", "top", "bottom", "formed", "fill_pct", "touched", "strength_atr", "age_bars")
EVENT_COLS = ("time", "kind", "dir", "level")
QUALITY_KEEP = ("status", "coverage", "last_bar_end_lag_s", "stale", "missing_last_bar", "short_history", "gaps")
HISTORY_SUMMARY_CHARS = 140
# null in these fields means "nothing detected", not "unknown": rendered as "none" instead of being dropped
NONE_MEANS_NOTHING = frozenset({"divergence", "absorption", "killzone", "last_swing_high", "last_swing_low",
                                "last_bar", "prev_bar", "sweep", "regime"})
_ISO = re.compile(r"^(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d)(?::\d\d(?:\.\d+)?)?Z$")


def short_time(s: str, year: str) -> str:
    m = _ISO.match(s)
    if not m:
        return s
    y, mo, d, h, mi = m.groups()
    return f"{mo}-{d} {h}:{mi}" if y == year else f"{y}-{mo}-{d} {h}:{mi}"


def _compact(obj: Any, year: str) -> Any:
    """Short timestamps everywhere; null dict fields dropped (positions inside rows are kept)."""
    if isinstance(obj, dict):
        return {k: ("none" if v is None else _compact(v, year)) for k, v in obj.items()
                if v is not None or k in NONE_MEANS_NOTHING}
    if isinstance(obj, list):
        return [_compact(v, year) for v in obj]
    if isinstance(obj, str):
        return short_time(obj, year)
    return obj


def _flags(obj: Any) -> Any:
    """true / false as 1 / 0 (fewer tokens), like ``fits_now``."""
    if isinstance(obj, bool):
        return int(obj)
    if isinstance(obj, dict):
        return {a: _flags(b) for a, b in obj.items()}
    if isinstance(obj, list):
        return [_flags(b) for b in obj]
    return obj


def _daily_profiles(block: dict, year: str) -> dict:
    """The daily profile rows keep their date as ``MM-DD`` (the year of ``meta.as_of``); the column names are in the legend."""
    out = {a: b for a, b in block.items() if a != "columns"}
    if isinstance(out.get("days"), list):
        out["days"] = [[(r[0][5:] if isinstance(r[0], str) and r[0][:4] == year else r[0]), *r[1:]] for r in out["days"]]
    return out


def _row(d: dict, cols: tuple[str, ...]) -> list:
    return [(int(d[c]) if isinstance(d.get(c), bool) else d.get(c)) for c in cols]


def _timeframe(tf: str, t: dict, trim: bool = True) -> dict:
    t = dict(t)
    q = t.pop("quality", None) or {}
    t.pop("recent_columns", None)
    if q.get("status", "ok") == "ok":
        t["data"] = "ok"
    else:
        t["data"] = {k: q[k] for k in QUALITY_KEEP
                     if not (q.get(k) is None or q.get(k) is False or q.get(k) == [] or q.get(k) == {})}
        if isinstance(t["data"].get("gaps"), list):          # [start_ms, missing_bars] -> [time, missing_bars]
            t["data"]["gaps"] = [[iso(int(g[0])), g[1]] if isinstance(g, (list, tuple)) and g else g
                                 for g in t["data"]["gaps"]]
    if "recent" in t and tf in RECENT and trim:
        t["recent"] = t["recent"][-RECENT[tf]:]
    st = t.get("structure")
    if isinstance(st, dict) and st.get("events"):
        t["structure"] = {**st, "events": [_row(e, EVENT_COLS) for e in st["events"]]}
    z = t.get("zones")
    if isinstance(z, dict):
        t["zones"] = {k: [_row(x, ZONE_COLS) for x in v] for k, v in z.items()}
    return t


def _capabilities(caps: dict) -> dict:
    out: dict[str, Any] = {}
    for name, v in caps.items():
        q = (v or {}).get("quality")
        if q == "real":
            out.setdefault("real", []).append(name)
        else:
            out.setdefault(q or "unknown", {})[name] = (v or {}).get("reason")
    return out


def model_view(payload: dict) -> dict:
    """The compact rendering of one payload (or of a partial one, e.g. a timeframe slice)."""
    meta = dict(payload.get("meta") or {})
    as_of = str(meta.get("as_of") or "")
    year = as_of[:4]
    meta["view_version"] = VIEW_VERSION
    out: dict[str, Any] = {}
    for k, v in payload.items():
        if k == "meta":
            continue
        if k in ("timeframes", "timeframe") and isinstance(v, dict):   # a single-TF analyst slice keeps its candles
            v = {tf: (_timeframe(tf, t, trim=k == "timeframes") if isinstance(t, dict) else t) for tf, t in v.items()}
        elif k == "capabilities" and isinstance(v, dict):
            v = _capabilities(v)
        elif k == "account" and isinstance(v, dict):
            v = {a: b for a, b in v.items() if a != "equity_source"}
            if isinstance(v.get("min_position_risk"), dict):          # fits_now as 1/0, like the other flags
                v["min_position_risk"] = {a: (int(b) if isinstance(b, bool) else b)
                                          for a, b in v["min_position_risk"].items()}
        elif k == "orderflow" and isinstance(v, dict):
            v = dict(v)
            if isinstance(v.get("depth"), dict):
                v["depth"] = {a: b for a, b in v["depth"].items() if a != "band_columns"}   # named in the legend
            if isinstance(v.get("daily_profiles"), dict):
                v["daily_profiles"] = _daily_profiles(v["daily_profiles"], year)
        elif k == "market" and isinstance(v, dict):
            v = {a: b for a, b in v.items() if a != "note"}
            if isinstance(v.get("session_stats"), dict):                # column names and window are in the legend
                v["session_stats"] = {a: b for a, b in v["session_stats"].items()
                                      if a not in ("columns", "now_columns", "days")}
            if isinstance(v.get("gold_clock"), dict):
                v["gold_clock"] = _flags(v["gold_clock"])
        elif k == "history" and isinstance(v, list):
            v = [{**h, "summary": (h.get("summary") or "")[:HISTORY_SUMMARY_CHARS]} if isinstance(h, dict) else h
                 for h in v]
        out[k] = _compact(v, year)
    meta_view = _compact({a: b for a, b in meta.items() if a != "as_of"}, year)
    return {"meta": {"as_of": as_of, **meta_view}, **out} if payload.get("meta") is not None else out


def view(obj: Any) -> Any:
    """``model_view`` for a payload-like dict or a list of them; anything else unchanged."""
    if isinstance(obj, dict) and ("meta" in obj or "timeframes" in obj or "timeframe" in obj):
        return model_view(obj)
    if isinstance(obj, list):
        return [view(x) for x in obj]
    return obj
