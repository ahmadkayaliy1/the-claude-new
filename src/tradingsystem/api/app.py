"""Dashboard backend (P10.1, spec §8): ``python -m tradingsystem api``.

* Read endpoints over app.db and the market-data stores (hot + cold).
* ``/ws`` pushes live quotes, collector/engine/executor status and new decisions every second
  (server-side DB polling, D-007 → browser gets push, never polls).
* ``POST /api/decisions/{id}/execute`` queues a recommendation for the executor (manual mode). Protected by a
  token that must travel in the ``X-Dashboard-Token`` header (a custom header forces a CORS preflight, which
  this server never grants → other websites cannot trigger it) and by an Origin check. Bound to 127.0.0.1.
* Host-header allow-list (loopback names only): a DNS-rebinding page served under another name cannot read
  the dashboard (nor the token embedded in ``/``, which is also never cached).
* Phase 4 (§3.8 component 7): read-only tabs Operator / Tuning / Proposals / Reviews, and ``POST /api/kill_switch``
  (ON only, same token + Origin protection as Execute Now; OFF stays ``scripts\\kill_switch_off.bat`` on purpose).
  Every Phase 4 read goes through a read-only connection and tolerates a database the engine / executor have not
  migrated yet (missing table or column → empty); files are read whole and briefly (never a FileResponse: a held
  handle makes ``os.replace`` by tune.py / the review runner fail on Windows), and a file name never comes from the
  request (reviews are served only when the name matches the review pattern *and* is in the directory listing).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import os
import re
import secrets
import shutil
import sqlite3
import threading
import zlib
from pathlib import Path
from typing import Any

import psutil
import uvicorn
import yaml
from fastapi import Body, FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from ..ai.budget import usage_db
from ..analysis.registry import capability_matrix
from ..core.adaptive import MAX_YAML_CHARS, has_alias
from ..core.instruments import InstrumentRegistry
from ..core.killswitch import read_reason, set_kill_switch
from ..core.settings import PROJECT_ROOT, Settings, load_settings
from ..core.timeframes import Timeframe
from ..core.timeutil import iso, now_ms, parse_date_spec
from ..storage.reader import InstrumentReader
from ..storage.tablespec import spec_for

WEB = PROJECT_ROOT / "web"
log = logging.getLogger("api")

# Phase 4 file reads: bounded, so a runaway file cannot stall the API or the browser
REVIEW_NAME_RE = re.compile(r"\d{8}T\d{6}Z?_(daily|weekly|diagnose|adhoc)(\.session)?\.(md|json)")
MAX_REVIEW_BYTES = 512 * 1024          # a session.json with the CLI's result can be large
MAX_REVIEWS_LISTED = 500
MAX_YAML_BYTES = 4 * MAX_YAML_CHARS    # UTF-8: a file past this surely has more characters than the services accept
MAX_PLAYBOOK_BYTES = 64 * 1024
MAX_JSONL_BYTES = 2 * 1024 * 1024      # changes.jsonl / proposals.jsonl: only the tail beyond this
MAX_JSON_NODES = 200_000               # _plain(): YAML aliases can describe exponentially large documents
MODEL_ACTIONS_SCAN = 50                # valid decisions searched for the model's last position_actions
SESSION_KEYS = ("review_id", "kind", "status", "started", "ended", "elapsed_s", "model", "effort", "summary",
                "summary_level", "error", "error_kind", "diff_guard", "cli", "reason")


def _ro(db: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    return con


def _rows(con: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict]:
    try:
        return [dict(r) for r in con.execute(sql, params).fetchall()]
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return []
        raise


def _rows_opt(con: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict]:
    """``_rows`` for the Phase 3/4 tables: the API starts in parallel with the engine and the executor (restart_all),
    so a table or a column their migration adds may not exist yet — that is "nothing yet", not an error."""
    try:
        return _rows(con, sql, params)
    except sqlite3.OperationalError as exc:
        if "no such column" in str(exc):
            return []
        raise


def _parse_json_cols(rows: list[dict], *cols: str) -> list[dict]:
    """JSON text columns → objects in place (a value that does not parse is kept as text)."""
    for r in rows:
        for k in cols:
            if isinstance(r.get(k), str) and r[k]:
                try:
                    r[k] = json.loads(r[k])
                except ValueError:
                    pass
    return rows


def _plain(value: Any, max_nodes: int = MAX_JSON_NODES) -> Any:
    """A JSON-safe copy: NaN/Infinity (Python's json and YAML accept them; the response encoder refuses them) and any
    non-JSON type become text; bounded so a YAML alias bomb or a self-referencing document cannot hang the API."""
    budget = [max_nodes]

    def walk(v: Any, depth: int) -> Any:
        budget[0] -= 1
        if budget[0] < 0 or depth > 40:
            return "…"
        if v is None or isinstance(v, (bool, int, str)):
            return v
        if isinstance(v, float):
            return v if math.isfinite(v) else str(v)
        if isinstance(v, dict):
            return {str(k): walk(x, depth + 1) for k, x in v.items()}
        if isinstance(v, (list, tuple, set)):
            return [walk(x, depth + 1) for x in v]
        return str(v)

    return walk(value, 0)


def _read_capped(path: Path, cap: int, *, tail: bool = False) -> tuple[str | None, bool]:
    """(text, truncated) of a small file, read in one short call (never a handle left open: tune.py and the review
    runner replace these files atomically, which fails on Windows while a reader holds them). ``tail``: keep the end
    of a longer file (append-only jsonl), dropping the first, partial line. (None, False) when missing/unreadable.
    Line ends come back as LF (the page shows the text as it is)."""
    try:
        size = path.stat().st_size
        if size <= cap:
            return path.read_bytes().decode("utf-8", errors="replace").replace("\r\n", "\n"), False
        with open(path, "rb") as fh:
            if tail:
                fh.seek(size - cap)
            data = fh.read(cap)
    except OSError:
        return None, False
    text = data.decode("utf-8", errors="replace").replace("\r\n", "\n")
    if tail:
        text = text.split("\n", 1)[1] if "\n" in text else ""
    return text, True


def _jsonl(path: Path) -> list[dict]:
    """An append-only jsonl file, oldest first; a partial last line (a writer mid-append) and bad lines are skipped.
    Records are split on ``\\n`` only (not ``splitlines()``: a raw U+2028 / U+2029 / U+0085 inside a JSON string of an
    older line is part of the record, not a line break)."""
    text, _ = _read_capped(path, MAX_JSONL_BYTES, tail=True)
    out = []
    for line in (text or "").split("\n"):
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def _limit(n: int, hi: int = 1000) -> int:
    return max(1, min(int(n), hi))


def _notify(s: Settings, level: str, title: str, text: str, *, key: str | None = None,
            pair: str | None = None) -> None:
    """core.notify (log line + toast + Telegram); a missing or failing notifier never fails the request."""
    try:
        from ..core.notify import notify
        notify(s, level, title, text, key=key, pair=pair)
    except Exception:  # noqa: BLE001
        log.warning("notification not sent: %s — %s", title, text, exc_info=True)


def create_app(s: Settings) -> FastAPI:
    app = FastAPI(title="Trading System", docs_url=None, redoc_url=None)
    reg = InstrumentRegistry.from_settings(s)
    data = s.paths.data()                      # market data (shared stores)
    app_db = s.paths.state() / "app.db"         # this system's decisions, status, events
    usage_path = usage_db(s)                    # AI usage (shared ledger when running as an instance)
    token = os.environ.get(s.api.token_env) or secrets.token_urlsafe(32)
    readers: dict[str, InstrumentReader] = {}
    allowed_origins = {f"http://127.0.0.1:{s.api.port}", f"http://localhost:{s.api.port}"}
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=sorted({"127.0.0.1", "localhost", "[::1]", s.api.host}))
    pair_names = tuple(s.enabled_pairs())
    known = {i.key for p in pair_names for i in reg.for_pair(p)}
    max_age_ms = s.risk.max_recommendation_age_s * 1000
    # every switch that stops this system (what Executor.kill_switch() tests); only existence matters
    switches = tuple(dict.fromkeys([data / "KILL_SWITCH", s.paths.state() / "KILL_SWITCH",
                                    *(data / "instances" / p / "KILL_SWITCH" for p in pair_names)]))
    # the file the ON button writes (this system's own switch: the global one in all-pairs mode) plus the global one:
    # only while one of them exists has the button nothing left to engage. A pair's switch in the all-pairs system
    # leaves the other pairs trading, so it must not disable the global stop (target_on).
    own_switches = tuple(dict.fromkeys([data / "KILL_SWITCH", s.paths.state() / "KILL_SWITCH"]))
    reviews_dir = data / "reviews"               # shared by every system of the data root (like adaptive/)

    def reader(key: str) -> InstrumentReader:
        if key not in readers:
            readers[key] = InstrumentReader(reg.get(key), data)
        return readers[key]

    def snapshot() -> dict:
        return _shared_snapshot(app_db, pair_names, known, usage_path, switches, own_switches)

    def guard(request: Request, x_dashboard_token: str | None) -> None:
        """The write endpoints' protection (Execute Now, kill switch): a foreign Origin → 403; the token must travel
        in the custom header (a cross-site page cannot send it without a preflight this server never grants) → 401.
        A request without an Origin (curl, a script on this machine) needs the token alone."""
        origin = request.headers.get("origin")
        if origin is not None and origin not in allowed_origins:
            raise HTTPException(403, "cross-origin request refused")
        if not x_dashboard_token or not secrets.compare_digest(x_dashboard_token.encode("utf-8"),
                                                               token.encode("utf-8")):
            raise HTTPException(401, "missing or invalid dashboard token")

    def chart_list(pair: str) -> list[dict]:
        if not s.ai.charts.enabled:
            return []
        d = s.paths.state() / "charts" / pair
        out = []
        for tf in s.ai.charts.timeframes:
            try:
                out.append({"tf": tf, "updated_ms": int((d / f"{tf}.png").stat().st_mtime * 1000)})
            except OSError:                       # not rendered yet (or being replaced right now)
                continue
        return out

    def review_files() -> dict[str, os.DirEntry]:
        """The review files by name — the only names ``/api/reviews/{name}`` serves (regular files, no links)."""
        try:
            with os.scandir(reviews_dir) as it:
                return {e.name: e for e in it
                        if REVIEW_NAME_RE.fullmatch(e.name) and e.is_file(follow_symlinks=False)}
        except OSError:
            return {}

    # ------------------------------------------------------------------ pages
    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        html = (WEB / "index.html").read_text(encoding="utf-8")
        html = html.replace("__DASHBOARD_TOKEN__", token).replace("__ASSET_VERSION__", _asset_version())
        return HTMLResponse(html, headers={
            "Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": "frame-ancestors 'none'"})

    if (WEB / "static").exists():
        app.mount("/static", StaticFiles(directory=WEB / "static"), name="static")

    # ------------------------------------------------------------------ read API
    @app.get("/api/pairs")
    def pairs() -> list[dict]:
        out = []
        for p, cfg in s.enabled_pairs().items():
            caps = capability_matrix(s, reg, p)
            out.append({"pair": p, "asset_class": cfg.asset_class, "decision_timeframe": cfg.decision_timeframe.value,
                        "price_decimals": cfg.price_decimals, "max_recommendation_age_s": s.risk.max_recommendation_age_s,
                        "instruments": [{"key": i.key, "roles": list(i.roles), "timeframes": [t.value for t in i.timeframes],
                                         "datatypes": list(i.datatypes)} for i in reg.for_pair(p)],
                        "capabilities": {k: {"quality": v.quality, "reason": v.reason} for k, v in caps.items()}})
        return out

    @app.get("/api/status")
    def status() -> dict:
        return snapshot()

    @app.get("/api/candles")
    def candles(instrument: str, tf: str = "15m", start: int | None = None, end: int | None = None,
                limit: int = 1500) -> dict:
        inst = reg.get(instrument)
        spec = spec_for(inst, "candles", Timeframe.parse(tf))
        if start is None:
            start = (end or now_ms()) - limit * Timeframe.parse(tf).ms * (2 if inst.venue == "mt5" else 1)
        c = reader(instrument).read_range(spec, start, end)
        n = len(c["open_time"])
        sl = slice(max(0, n - limit), n)
        vol = c["tick_volume"] if "tick_volume" in c else c["volume"]
        return {"instrument": instrument, "tf": tf, "volume_kind": "tick" if "tick_volume" in c else "traded",
                "t": (c["open_time"][sl] // 1000).tolist(), "o": c["open"][sl].tolist(), "h": c["high"][sl].tolist(),
                "l": c["low"][sl].tolist(), "c": c["close"][sl].tolist(), "v": vol[sl].tolist()}

    @app.get("/api/decisions")
    def decisions(pair: str | None = None, limit: int = 100, before: int | None = None) -> list[dict]:
        q = ("SELECT id, ts, pair, mode, trigger, provider, model, status, decision, order_type, confidence, "
             "rr_computed, valid_until, execution_state, outcome, outcome_pnl_usd, outcome_pnl_pct, outcome_pips, "
             "virtual_outcome, virtual_r, cost_usd, latency_ms FROM ai_decisions WHERE 1=1")
        params: list[Any] = []
        if pair:
            q += " AND pair=?"
            params.append(pair)
        if before:
            q += " AND ts<?"
            params.append(before)
        q += " ORDER BY ts DESC LIMIT ?"
        params.append(min(limit, 500))
        if not app_db.exists():
            return []
        with _ro(app_db) as con:
            return _rows(con, q, tuple(params))

    @app.get("/api/decisions/{decision_id}")
    def decision(decision_id: str) -> dict:
        with _ro(app_db) as con:
            d = _rows(con, "SELECT * FROM ai_decisions WHERE id=?", (decision_id,))
            if not d:
                raise HTTPException(404, "decision not found")
            d = d[0]
            for k in ("recommendation", "errors", "execution_detail"):
                if d.get(k):
                    d[k] = json.loads(d[k])
            d["sub_outputs"] = _rows(con, "SELECT role, label, provider, model, ok, output, errors, cost_usd "
                                          "FROM ai_sub_outputs WHERE decision_id=? ORDER BY id", (decision_id,))
            for so in d["sub_outputs"]:
                so["output"] = json.loads(so["output"]) if so["output"] else None
            p = _rows(con, "SELECT payload_z FROM ai_payloads WHERE payload_hash=?", (d.get("payload_hash"),))
            d["payload"] = json.loads(zlib.decompress(p[0]["payload_z"])) if p else None
            d["paper_legs"] = _rows(con, "SELECT * FROM paper_legs WHERE decision_id=? ORDER BY id", (decision_id,))
            # Phase 3: what the system did on this trade (rules) and what this decision asked for (model actions)
            d["position_actions"] = _rows(con, "SELECT * FROM position_actions WHERE source_decision=? OR "
                                               "target_decision=? ORDER BY id", (decision_id, decision_id))
            for a in d["position_actions"]:
                for k in ("requested", "detail"):
                    if a.get(k):
                        try:
                            a[k] = json.loads(a[k])
                        except ValueError:
                            pass
        return d

    @app.get("/api/charts/{pair}")
    def charts_list(pair: str) -> list[dict]:
        """The latest chart images rendered for the model for ``pair`` (Phase 3); none while charts are off."""
        if pair not in pair_names:
            raise HTTPException(404, "unknown pair")
        return chart_list(pair)

    @app.get("/api/charts/{pair}/{tf}")
    def chart(pair: str, tf: str) -> FileResponse:
        if pair not in pair_names or tf not in s.ai.charts.timeframes:      # never a path from the request
            raise HTTPException(404, "unknown chart")
        f = s.paths.state() / "charts" / pair / f"{tf}.png"
        if not f.exists():
            raise HTTPException(404, "no chart yet")
        return FileResponse(f, media_type="image/png", headers={"Cache-Control": "no-store"})

    @app.get("/api/performance")
    def performance() -> dict:
        return _performance(app_db, usage_path, s.paths.instance)

    @app.get("/api/events")
    def events(limit: int = 200) -> list[dict]:
        with _ro(app_db) as con:
            return _rows(con, "SELECT * FROM ingestion_events ORDER BY id DESC LIMIT ?", (min(limit, 1000),))

    # ------------------------------------------------------------------ Phase 4 tabs (read-only)
    @app.get("/api/operator/{pair}")
    def operator(pair: str, limit: int = 100) -> dict:
        """One pair's live-trade picture: the model's memory notes, the last position actions it asked for, what the
        system did (``position_actions``), the management rules' state and the chart images the model received."""
        if pair not in pair_names:
            raise HTTPException(404, "unknown pair")
        n = _limit(limit, 500)
        out: dict[str, Any] = {"pair": pair, "memory": {}, "last_model_actions": None, "position_actions": [],
                               "management_state": [], "charts": chart_list(pair)}
        if not app_db.exists():
            return out
        with _ro(app_db) as con:
            out["memory"] = _memory(con, pair)
            out["last_model_actions"] = _last_model_actions(con, pair)
            out["position_actions"] = _parse_json_cols(_rows_opt(
                con, "SELECT * FROM position_actions WHERE pair=? ORDER BY id DESC LIMIT ?", (pair, n)),
                "requested", "detail")
            out["management_state"] = _management_state(con, pair, n)
        return _plain(out)

    @app.get("/api/position_actions")
    def position_actions(pair: str | None = None, limit: int = 200) -> list[dict]:
        """Every action taken on live trades (model- and rule-sourced), newest first."""
        if not app_db.exists():
            return []
        q, params = "SELECT * FROM position_actions", []
        if pair:
            q += " WHERE pair=?"
            params.append(pair)
        with _ro(app_db) as con:
            rows = _rows_opt(con, q + " ORDER BY id DESC LIMIT ?", (*params, _limit(limit)))
        return _plain(_parse_json_cols(rows, "requested", "detail"))

    @app.get("/api/adaptive")
    def adaptive() -> dict:
        """Per pair: the overlay file as written (values, set/expiry, expired flag), whether the services accept it
        and the effective values, the playbook text; plus the freeze flag and ``adaptive.enabled``."""
        now = now_ms()
        freeze = data / "TUNING_FREEZE"
        freeze_text, _ = _read_capped(freeze, 2000)
        return _plain({"enabled": s.adaptive.enabled, "tuning_freeze": freeze.exists(),
                       "freeze_note": (freeze_text or "").strip()[:500] or None, "server_time": now,
                       "pairs": {p: _adaptive_pair(s, p, now) for p in pair_names}})

    @app.get("/api/tuning_changes")
    def tuning_changes(pair: str | None = None, limit: int = 200) -> dict:
        """The ``tuning_changes`` table of this system's app.db and each pair's ``changes.jsonl`` (newest first)."""
        if pair is not None and pair not in pair_names:
            raise HTTPException(404, "unknown pair")
        n = _limit(limit)
        table: list[dict] = []
        if app_db.exists():
            q, params = "SELECT * FROM tuning_changes", []
            if pair:
                q += " WHERE pair=?"
                params.append(pair)
            with _ro(app_db) as con:
                table = _parse_json_cols(_rows_opt(con, q + " ORDER BY id DESC LIMIT ?", (*params, n)), "evidence")
        changes = {p: _jsonl(data / "adaptive" / p / "changes.jsonl")[::-1][:n]
                   for p in ((pair,) if pair else pair_names)}
        return _plain({"table": table, "changes": changes})

    @app.get("/api/proposals")
    def proposals(limit: int = 200) -> list[dict]:
        """``data/shared/proposals.jsonl`` (tools/propose.py), newest first."""
        return _plain(_jsonl(s.paths.shared() / "proposals.jsonl")[::-1][:_limit(limit)])

    @app.get("/api/reviews")
    def reviews(limit: int = 200) -> list[dict]:
        """The review packs and session records under ``data/reviews``, newest first (names sort by time)."""
        out = []
        files = review_files()
        for name in sorted(files, reverse=True)[:_limit(limit, MAX_REVIEWS_LISTED)]:
            try:
                st = files[name].stat(follow_symlinks=False)
            except OSError:
                continue
            m = REVIEW_NAME_RE.fullmatch(name)
            out.append({"name": name, "stamp": name.split("_", 1)[0], "kind": m.group(1) if m else None,
                        "session": name.endswith(".session.json"), "size": st.st_size,
                        "mtime_ms": int(st.st_mtime * 1000)})
        return out

    @app.get("/api/reviews/{name}")
    def review(name: str) -> dict:
        """One review file as text (the page shows it escaped in a <pre>). Only a name that matches the review
        pattern and is in the listing: a request string is never joined into a path."""
        if not REVIEW_NAME_RE.fullmatch(name):
            raise HTTPException(404, "unknown review")
        entry = review_files().get(name)
        if entry is None:
            raise HTTPException(404, "unknown review")
        text, truncated = _read_capped(Path(entry.path), MAX_REVIEW_BYTES)
        if text is None:
            raise HTTPException(404, "review not readable")
        m = REVIEW_NAME_RE.fullmatch(name)
        out: dict[str, Any] = {"name": name, "kind": m.group(1) if m else None, "truncated": truncated,
                               "max_bytes": MAX_REVIEW_BYTES, "text": text, "session": None}
        if name.endswith(".session.json") and not truncated:
            try:
                rec = json.loads(text)
            except ValueError:
                rec = None
            if isinstance(rec, dict):
                out["session"] = {k: rec.get(k) for k in SESSION_KEYS if rec.get(k) is not None}
        return _plain(out)

    # ------------------------------------------------------------------ kill switch (ON only)
    @app.get("/api/kill_switch")
    def kill_switch_state() -> dict:
        """What the ON button would write, and every switch that currently stops this system. ``target_on``: that
        file or the global switch already exists (the button has nothing left to engage); ``on``: any switch does."""
        target = s.paths.state() / "KILL_SWITCH"
        return {"on": any(p.exists() for p in switches), "target_on": any(p.exists() for p in own_switches),
                "target": str(target), "scope": s.paths.instance or "all", "global": s.paths.instance is None,
                "files": [{"path": str(p), "set_by": read_reason(p)} for p in switches if p.exists()]}

    @app.post("/api/kill_switch")
    def kill_switch_on(request: Request, x_dashboard_token: str | None = Header(default=None),
                       reason: str | None = Body(default=None, embed=True)) -> JSONResponse:
        """Turn THIS system's kill switch ON: ``data/instances/<PAIR>/KILL_SWITCH`` for a pair instance; in
        all-pairs mode (no instance) that is the GLOBAL ``data/KILL_SWITCH`` — every system stops opening trades.
        Idempotent (an existing switch keeps its first reason). There is deliberately no OFF route."""
        guard(request, x_dashboard_token)
        pair = s.paths.instance
        why = " ".join(str(reason or "").split())[:300] or "engaged from the dashboard"
        try:
            path, created = set_kill_switch(s, pair, reason=why, actor="dashboard")
        except OSError as exc:
            log.error("kill switch %s could not be written: %s", pair or "ALL", exc)
            _notify(s, "critical", f"kill switch {pair or 'ALL'} could NOT be engaged",
                    f"{exc} — from the dashboard: {why}")
            raise HTTPException(500, f"the kill switch could not be written: {exc}") from None
        scope = pair or "all"
        if created:
            log.warning("kill switch ON (%s) from the dashboard: %s", scope, why)
            _notify(s, "critical", f"kill switch ON: {pair or 'ALL'}", f"by dashboard: {why}",
                    key=f"kill_switch_{pair or 'all'}", pair=pair)
        note = ("ALL-PAIRS mode: this is the GLOBAL switch — every system on this data root stops opening trades"
                if pair is None else f"{pair} only — the other pairs' systems are not affected")
        note += (". It was already on (left unchanged)" if not created else "")
        note += (". Open trades keep their SL/TP and protective actions. OFF: scripts\\kill_switch_off.bat"
                 + (f" {pair}" if pair else ""))
        return JSONResponse({"on": True, "created": created, "scope": scope, "global": pair is None,
                             "file": str(path), "set_by": read_reason(path), "note": note})

    # ------------------------------------------------------------------ execute (manual mode)
    @app.post("/api/decisions/{decision_id}/execute")
    def execute(decision_id: str, request: Request, x_dashboard_token: str | None = Header(default=None)) -> JSONResponse:
        guard(request, x_dashboard_token)
        if not app_db.exists():                   # connect() would create an empty app.db
            raise HTTPException(404, "decision not found")
        con = sqlite3.connect(app_db, timeout=10)
        try:
            row = con.execute("SELECT status, decision, execution_state, valid_until, ts, recommendation FROM ai_decisions "
                              "WHERE id=?", (decision_id,)).fetchone()
            if not row:
                raise HTTPException(404, "decision not found")
            status, dec, state, valid_until, ts, rec_json = row
            if status != "valid" or dec not in ("BUY", "SELL"):
                raise HTTPException(409, f"not executable (status={status}, decision={dec})")
            if valid_until and now_ms() >= valid_until:
                raise HTTPException(409, "recommendation expired")
            try:        # the risk gate ages a recommendation from its timestamp (= the cycle's data as-of), not the row
                ts = parse_date_spec(json.loads(rec_json)["timestamp"])
            except (TypeError, ValueError, KeyError):
                pass
            if state == "not_executed" and now_ms() - ts > max_age_ms:      # the risk gate's recommendation_age rule
                raise HTTPException(409, f"too old for execution ({(now_ms() - ts) // 1000}s > "
                                         f"{max_age_ms // 1000}s risk.max_recommendation_age_s)")
            if state != "not_executed":
                return JSONResponse({"id": decision_id, "execution_state": state, "note": "already handled (idempotent)"})
            cur = con.execute("UPDATE ai_decisions SET execution_state='queued' WHERE id=? AND execution_state='not_executed'",
                              (decision_id,))
            con.commit()
            return JSONResponse({"id": decision_id, "execution_state": "queued" if cur.rowcount else "unchanged",
                                 "note": "the executor re-validates everything (risk gate) before any order"})
        finally:
            con.close()

    # ------------------------------------------------------------------ live push
    @app.websocket("/ws")
    async def ws(sock: WebSocket) -> None:
        origin = sock.headers.get("origin")
        if origin is not None and origin not in allowed_origins:
            await sock.close(code=1008)
            return
        await sock.accept()
        last_decision_ts = 0
        try:
            while True:
                snap = dict(await asyncio.to_thread(snapshot))       # shared by all clients — copy before adding
                new = []
                if app_db.exists():
                    with _ro(app_db) as con:
                        new = _rows(con, "SELECT id, ts, pair, status, decision, order_type, confidence, execution_state "
                                         "FROM ai_decisions WHERE ts>? ORDER BY ts", (last_decision_ts,))
                if new:
                    last_decision_ts = new[-1]["ts"]
                snap["new_decisions"] = new
                await sock.send_text(json.dumps(snap, default=str))
                await asyncio.sleep(1.0)
        except (WebSocketDisconnect, RuntimeError):
            return

    return app


_PROC_CACHE: dict[str, Any] = {"ts": 0, "rows": []}
_PROC_TTL_MS = 30_000
_SNAP_CACHE: dict[str, Any] = {"ts": 0, "key": None, "snap": None}
_SNAP_TTL_MS = 900
_SNAP_LOCK = threading.Lock()
QUOTE_STALE_S = 600


def _process_list() -> list[dict]:
    """Our processes and their RSS (cached 30 s). Only python processes are opened for their command line — reading
    every process's command line costs seconds of CPU on Windows."""
    if now_ms() - _PROC_CACHE["ts"] < _PROC_TTL_MS:
        return _PROC_CACHE["rows"]
    rows = []
    for p in psutil.process_iter(["pid", "name"]):
        if not (p.info.get("name") or "").lower().startswith("python"):
            continue
        try:
            cmd = " ".join(p.cmdline() or [])
            if "tradingsystem" in cmd or "recorder.py" in cmd or ("multiprocessing" in cmd and "Python312" in cmd):
                rows.append({"pid": p.info["pid"], "rss_mb": round(p.memory_info().rss / 2**20), "cmd": cmd[-80:]})
        except (psutil.Error, TypeError):
            continue
    _PROC_CACHE.update(ts=now_ms(), rows=rows)
    return rows


def _asset_version() -> str:
    """``?v=`` of app.js / style.css in ``/``: changes whenever either file changes, so a browser never pairs a new
    index.html (new tabs) with a heuristically cached old script (StaticFiles sends no Cache-Control)."""
    parts = []
    for name in ("app.js", "style.css"):
        try:
            st = (WEB / "static" / name).stat()
            parts.append(f"{st.st_mtime_ns:x}{st.st_size:x}")
        except OSError:
            parts.append("0")
    return format(zlib.crc32("-".join(parts).encode()), "08x")


# ---------------------------------------------------------------------- Phase 4 readers (read-only connection)
def _memory(con: sqlite3.Connection, pair: str) -> dict:
    """``DecisionStore.memory`` on a read-only connection (its constructor migrates the schema — not in the API)."""
    try:
        r = _rows(con, "SELECT id, ts, decision, execution_state, recommendation FROM ai_decisions WHERE pair=? AND "
                       "status='valid' AND COALESCE(json_extract(recommendation, '$.operator_notes'), '') != '' "
                       "ORDER BY ts DESC LIMIT 1", (pair,))
    except sqlite3.OperationalError:          # a malformed recommendation row (json_extract refuses it)
        return {}
    if not r:
        return {}
    try:
        rec = json.loads(r[0]["recommendation"] or "{}")
    except ValueError:
        return {}
    notes = str((rec.get("operator_notes") if isinstance(rec, dict) else None) or "").strip()
    if not notes:
        return {}
    return {"id": r[0]["id"], "ts": r[0]["ts"], "time": iso(r[0]["ts"]), "decision": r[0]["decision"],
            "execution_state": r[0]["execution_state"], "notes": notes}


def _last_model_actions(con: sqlite3.Connection, pair: str) -> dict | None:
    """The latest valid decision (of the last ``MODEL_ACTIONS_SCAN``) whose recommendation asked for position
    actions — most decisions ask for none, so "the latest decision's actions" would nearly always be empty."""
    rows = _rows(con, "SELECT id, ts, decision, execution_state, recommendation FROM ai_decisions WHERE pair=? AND "
                      "status='valid' ORDER BY ts DESC LIMIT ?", (pair, MODEL_ACTIONS_SCAN))
    for r in rows:
        try:
            rec = json.loads(r["recommendation"] or "{}")
        except ValueError:
            continue
        acts = rec.get("position_actions") if isinstance(rec, dict) else None
        if isinstance(acts, list) and acts:
            return {"id": r["id"], "ts": r["ts"], "time": iso(r["ts"]), "decision": r["decision"],
                    "execution_state": r["execution_state"], "actions": acts}
    return None


def _management_state(con: sqlite3.Connection, pair: str, limit: int) -> list[dict]:
    """``management_state`` rows of ``pair``'s decisions (the table has no pair column), newest decision first, each
    with the rule it tracks (``recommendation.management[rule_idx]`` of that decision)."""
    rows = _parse_json_cols(_rows_opt(
        con, "SELECT m.*, d.ts AS decision_ts, d.decision AS decision FROM management_state m JOIN ai_decisions d "
             "ON d.id = m.decision_id WHERE d.pair=? ORDER BY d.ts DESC, m.rule_idx, m.leg LIMIT ?", (pair, limit)),
        "detail")
    plans: dict[str, list] = {}
    for r in rows:
        did = r["decision_id"]
        if did not in plans:
            got = _rows(con, "SELECT recommendation FROM ai_decisions WHERE id=?", (did,))
            try:
                rec = json.loads(got[0]["recommendation"] or "{}") if got else {}
            except ValueError:
                rec = {}
            plan = rec.get("management") if isinstance(rec, dict) else None
            plans[did] = plan if isinstance(plan, list) else []
        idx = r.get("rule_idx")
        r["rule"] = plans[did][idx] if isinstance(idx, int) and 0 <= idx < len(plans[did]) else None
    return rows


def _overlay_entries(doc: Any, now: int) -> list[dict]:
    """The entries of an ``adaptive.yaml`` document as written (dotted keys: ``trigger.weak_min``), each with its
    expired flag. Display only — whether the services accept the file is :func:`_adaptive_check`'s answer."""
    out: list[dict] = []
    if not isinstance(doc, dict):
        return out

    def ms(v: Any) -> int | None:
        return v if isinstance(v, int) and not isinstance(v, bool) else None

    def walk(node: dict, prefix: str) -> None:
        for k, v in node.items():
            key = f"{prefix}{k}"
            if not prefix and k == "version":
                continue
            if isinstance(v, dict) and "value" in v:
                exp = ms(v.get("expires_ms"))
                out.append({"key": key, "value": v.get("value"), "set_ms": ms(v.get("set_ms")), "expires_ms": exp,
                            "expired": exp is not None and exp <= now, "reason": v.get("reason"),
                            "evidence": v.get("evidence"), "window_hours": v.get("window_hours"),
                            "review_id": v.get("review_id")})
            elif isinstance(v, dict) and not prefix:          # a group: trigger / pair
                walk(v, f"{key}.")
            else:
                out.append({"key": key, "value": v, "problem": "not an entry {value, set_ms, expires_ms, …}"})

    walk(doc, "")
    return out


def _adaptive_check(s: Settings, pair: str, now: int) -> dict:
    """Whether the services would accept the pair's overlay now, and the effective values if so — through the
    services' own reader (core.adaptive), called without its side effects: no lock, no event, no ``expired`` line,
    no ``tuning_changes`` update (those belong to the engine / executor). An invalid file means the services keep
    their LAST GOOD values, which only they know. ``valid`` None when the validator is unavailable."""
    try:
        from ..core import adaptive as ad
        store = ad.AdaptiveStore(s, pair)             # plain attributes, no I/O
        try:
            cfg, playbook = store._read()             # noqa: SLF001 — the exact validation the services apply
        except (ValueError, yaml.YAMLError, RecursionError) as exc:
            return {"valid": False, "problem": " ".join(str(exc).split())[:600], "effective": None}
        return {"valid": True, "problem": None, "effective": ad.compute_effective(s, cfg, playbook, now).as_detail()}
    except Exception as exc:  # noqa: BLE001 — display only: the raw file is still shown
        log.warning("adaptive validation unavailable for %s: %s", pair, exc)
        return {"valid": None, "problem": f"validator unavailable: {type(exc).__name__}", "effective": None}


ALIAS_PROBLEM = "adaptive.yaml uses YAML anchors/aliases (never written by tools/tune.py)"


def _adaptive_pair(s: Settings, pair: str, now: int) -> dict:
    d = s.paths.data() / "adaptive" / pair
    text, truncated = _read_capped(d / "adaptive.yaml", MAX_YAML_BYTES)
    playbook, pb_truncated = _read_capped(d / "playbook.md", MAX_PLAYBOOK_BYTES)
    out: dict[str, Any] = {"dir": str(d), "file": text is not None, "yaml_error": None, "entries": [],
                           "playbook": playbook, "playbook_truncated": pb_truncated}
    # size and alias guards BEFORE any parsing, in the services' order (core.adaptive.load_cfg_text): safe_load
    # copies merge keys eagerly (``<<: [*a, *a, …]`` over 7 levels, ~500 bytes: minutes and gigabytes in a worker
    # thread nobody can cancel), while the alias check only reads the event stream (nothing is constructed)
    alias = False
    if text is not None and (truncated or len(text) > MAX_YAML_CHARS):
        out["yaml_error"] = f"adaptive.yaml larger than {MAX_YAML_CHARS} characters — not parsed"
    elif text is not None:
        try:
            alias = has_alias(text)
            doc = None if alias else yaml.safe_load(text)
        except RecursionError:                           # the composer recurses: deep nesting, not a YAMLError
            out["yaml_error"] = "adaptive.yaml is nested too deeply"
        except (yaml.YAMLError, ValueError) as exc:      # ValueError: e.g. an impossible date (2001-13-01)
            out["yaml_error"] = " ".join(str(exc).split())[:400]
        else:
            if alias:
                out["yaml_error"] = f"{ALIAS_PROBLEM} — not parsed"
            else:
                if doc is not None and not isinstance(doc, dict):
                    out["yaml_error"] = "adaptive.yaml is not a mapping"
                # its own budget: a hand-made document cannot crowd the flags below out of the response
                out["entries"] = _plain(_overlay_entries(doc, now), MAX_JSON_NODES // 10)
    if alias:
        # the services refuse it the same way (load_cfg_text): not handed to the validator at all
        out.update(valid=False, effective=None, problem=f"{ALIAS_PROBLEM} — not validated")
    else:           # no files at all → valid, the config defaults; an oversized file is refused before any parse
        out.update(_adaptive_check(s, pair, now))
    eff = out.get("effective") or {}
    # a playbook.md that adaptive.yaml does not reference (or that expired) is not in the prompt
    out["playbook_in_force"] = bool(eff.get("playbook_chars")) if out.get("valid") else None
    return out


def _shared_snapshot(app_db: Path, pairs: tuple[str, ...] = (), known: set[str] | None = None,
                     usage_path: Path | None = None, switches: tuple[Path, ...] = (),
                     targets: tuple[Path, ...] = ()) -> dict:
    """One status snapshot per ~second, shared by ``/api/status`` and every WebSocket client (read-only: copy it)."""
    with _SNAP_LOCK:
        key = (str(app_db), pairs, str(usage_path), tuple(str(p) for p in switches), tuple(str(p) for p in targets))
        if _SNAP_CACHE["key"] != key or now_ms() - _SNAP_CACHE["ts"] >= _SNAP_TTL_MS:
            _SNAP_CACHE.update(snap=_status_snapshot(app_db, pairs, known, usage_path, switches, targets), key=key,
                               ts=now_ms())
        return _SNAP_CACHE["snap"]


def _usage_rows(usage_path: Path, sql: str, params: tuple = ()) -> list[dict]:
    """AI usage lives in app.db (single system) or in the shared ledger data/shared/ai_usage.db (instances)."""
    if not usage_path.exists():
        return []
    try:
        with _ro(usage_path) as con:
            return _rows(con, sql, params)
    except sqlite3.OperationalError:          # ledger not created yet (no AI call so far)
        return []


def _status_snapshot(app_db: Path, pairs: tuple[str, ...] = (), known: set[str] | None = None,
                     usage_path: Path | None = None, switches: tuple[Path, ...] = (),
                     targets: tuple[Path, ...] = ()) -> dict:
    out: dict[str, Any] = {"server_time": now_ms(), "server_time_iso": iso(now_ms())}
    # the switch files themselves (the executor's heartbeat also reports it, but only while the executor runs);
    # target_on: the file the dashboard's ON button writes, or the global one, exists (the button's disabled state)
    on = [str(p) for p in switches if p.exists()]
    out["kill_switch"] = {"on": bool(on), "target_on": any(p.exists() for p in targets), "files": on}
    if not app_db.exists():
        return out
    with _ro(app_db) as con:
        out["collectors"] = _rows(con, "SELECT * FROM collector_status ORDER BY collector")
        for c in out["collectors"]:
            c["detail"] = json.loads(c["detail"]) if c.get("detail") else None
            c["heartbeat_age_s"] = round((now_ms() - c["updated_ms"]) / 1000, 1)
            if c["state"] not in ("stopped",) and c["heartbeat_age_s"] > 60:
                c["state"] = "stale"          # the process stopped reporting (crashed or not running)
        out["quotes"] = _rows(con, "SELECT * FROM latest_quote ORDER BY instrument")
        for q in out["quotes"]:
            q["age_s"] = round(max(0, now_ms() - q["ts"]) / 1000, 1)
            q["stale"] = q["age_s"] > QUOTE_STALE_S
            q["configured"] = known is None or q["instrument"] in known     # rows of feeds no longer ingested
        # latest decision per pair with its execution state + reason: the Live card follows queued → gate result
        out["latest_decisions"] = {}
        for p in pairs:
            r = _rows(con, "SELECT id, ts, status, decision, execution_state, execution_detail FROM ai_decisions "
                           "WHERE pair=? ORDER BY ts DESC LIMIT 1", (p,))
            if r:
                det = r[0].pop("execution_detail")
                r[0]["execution_reason"] = (json.loads(det) or {}).get("reason") if det else None
                out["latest_decisions"][p] = r[0]
        out["paper_account"] = _rows(con, "SELECT * FROM paper_account")
    day0 = now_ms() // 86_400_000 * 86_400_000
    # the whole account's usage today (every instance shares the subscription's limits)
    out["ai_today"] = _usage_rows(usage_path or app_db,
                                  "SELECT provider, count(*) AS calls, COALESCE(sum(cost_usd),0) AS cost, "
                                  "COALESCE(sum(ok),0) AS ok FROM ai_usage WHERE ts>=? GROUP BY provider", (day0,))
    du = shutil.disk_usage(app_db.anchor)
    out["disk_free_gb"] = round(du.free / 2**30, 1)
    procs = _process_list()
    out["processes"] = procs
    out["processes_rss_mb"] = sum(p["rss_mb"] for p in procs)
    return out


def _performance(app_db: Path, usage_path: Path | None = None, pair: str | None = None) -> dict:
    if not app_db.exists():
        return {}
    usage_path = usage_path or app_db
    if pair:
        cost = _usage_rows(usage_path, "SELECT COALESCE(sum(cost_usd),0) AS total, count(*) AS calls FROM ai_usage "
                                       "WHERE pair=?", (pair,))
    else:
        cost = _usage_rows(usage_path, "SELECT COALESCE(sum(cost_usd),0) AS total, count(*) AS calls FROM ai_usage")
    with _ro(app_db) as con:
        by_status = _rows(con, "SELECT status, decision, count(*) AS n FROM ai_decisions GROUP BY status, decision")
        outcomes = _rows(con, "SELECT outcome, count(*) AS n, COALESCE(sum(outcome_pnl_usd),0) AS pnl FROM ai_decisions "
                              "WHERE outcome IS NOT NULL GROUP BY outcome")
        virtual = _rows(con, "SELECT virtual_outcome, count(*) AS n, COALESCE(avg(virtual_r),0) AS avg_r FROM ai_decisions "
                             "WHERE virtual_outcome IS NOT NULL GROUP BY virtual_outcome")
        per_pair = _rows(con, "SELECT pair, count(*) AS decisions, SUM(decision!='NO_TRADE') AS trades, "
                              "COALESCE(sum(outcome_pnl_usd),0) AS pnl FROM ai_decisions WHERE status='valid' GROUP BY pair")
    wins = sum(o["n"] for o in outcomes if o["outcome"] == "closed_profit")
    losses = sum(o["n"] for o in outcomes if o["outcome"] == "closed_loss")
    pnl = sum(o["pnl"] for o in outcomes)
    ai_cost = cost[0]["total"] if cost else 0.0
    return {"by_status": by_status, "outcomes": outcomes, "virtual": virtual, "per_pair": per_pair,
            "win_rate": round(wins / (wins + losses), 3) if wins + losses else None, "realized_pnl_usd": round(pnl, 2),
            "ai_cost_usd": round(ai_cost, 4), "ai_calls": cost[0]["calls"] if cost else 0,
            "ai_cost_to_profit": round(ai_cost / pnl, 3) if pnl > 0 else None}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="tradingsystem api")
    ap.parse_args(argv)
    s = load_settings()
    from ..core.logsetup import setup_from_settings
    setup_from_settings("api", s)
    uvicorn.run(create_app(s), host=s.api.host, port=s.api.port, log_level="warning")
    return 0
