"""Dashboard backend (P10.1, spec §8): ``python -m tradingsystem api``.

* Read endpoints over app.db and the market-data stores (hot + cold).
* ``/ws`` pushes live quotes, collector/engine/executor status and new decisions every second
  (server-side DB polling, D-007 → browser gets push, never polls).
* ``POST /api/decisions/{id}/execute`` queues a recommendation for the executor (manual mode). Protected by a
  token that must travel in the ``X-Dashboard-Token`` header (a custom header forces a CORS preflight, which
  this server never grants → other websites cannot trigger it) and by an Origin check. Bound to 127.0.0.1.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import shutil
import sqlite3
import zlib
from pathlib import Path
from typing import Any

import psutil
import uvicorn
from fastapi import FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from ..analysis.registry import capability_matrix
from ..core.instruments import InstrumentRegistry
from ..core.settings import PROJECT_ROOT, Settings, load_settings
from ..core.timeframes import Timeframe
from ..core.timeutil import iso, now_ms
from ..storage.reader import InstrumentReader
from ..storage.tablespec import spec_for

WEB = PROJECT_ROOT / "web"


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


def create_app(s: Settings) -> FastAPI:
    app = FastAPI(title="Trading System", docs_url=None, redoc_url=None)
    reg = InstrumentRegistry.from_settings(s)
    data = s.paths.data()
    app_db = data / "app.db"
    token = os.environ.get(s.api.token_env) or secrets.token_urlsafe(32)
    readers: dict[str, InstrumentReader] = {}
    allowed_origins = {f"http://127.0.0.1:{s.api.port}", f"http://localhost:{s.api.port}"}

    def reader(key: str) -> InstrumentReader:
        if key not in readers:
            readers[key] = InstrumentReader(reg.get(key), data)
        return readers[key]

    # ------------------------------------------------------------------ pages
    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        html = (WEB / "index.html").read_text(encoding="utf-8")
        return html.replace("__DASHBOARD_TOKEN__", token)

    if (WEB / "static").exists():
        app.mount("/static", StaticFiles(directory=WEB / "static"), name="static")

    # ------------------------------------------------------------------ read API
    @app.get("/api/pairs")
    def pairs() -> list[dict]:
        out = []
        for p, cfg in s.enabled_pairs().items():
            caps = capability_matrix(s, reg, p)
            out.append({"pair": p, "asset_class": cfg.asset_class, "decision_timeframe": cfg.decision_timeframe.value,
                        "price_decimals": cfg.price_decimals,
                        "instruments": [{"key": i.key, "roles": list(i.roles), "timeframes": [t.value for t in i.timeframes],
                                         "datatypes": list(i.datatypes)} for i in reg.for_pair(p)],
                        "capabilities": {k: {"quality": v.quality, "reason": v.reason} for k, v in caps.items()}})
        return out

    @app.get("/api/status")
    def status() -> dict:
        return _status_snapshot(app_db)

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
        return d

    @app.get("/api/performance")
    def performance() -> dict:
        return _performance(app_db)

    @app.get("/api/events")
    def events(limit: int = 200) -> list[dict]:
        with _ro(app_db) as con:
            return _rows(con, "SELECT * FROM ingestion_events ORDER BY id DESC LIMIT ?", (min(limit, 1000),))

    # ------------------------------------------------------------------ execute (manual mode)
    @app.post("/api/decisions/{decision_id}/execute")
    def execute(decision_id: str, request: Request, x_dashboard_token: str | None = Header(default=None)) -> JSONResponse:
        origin = request.headers.get("origin")
        if origin is not None and origin not in allowed_origins:
            raise HTTPException(403, "cross-origin request refused")
        if not x_dashboard_token or not secrets.compare_digest(x_dashboard_token, token):
            raise HTTPException(401, "missing or invalid dashboard token")
        con = sqlite3.connect(app_db, timeout=10)
        try:
            row = con.execute("SELECT status, decision, execution_state, valid_until FROM ai_decisions WHERE id=?",
                              (decision_id,)).fetchone()
            if not row:
                raise HTTPException(404, "decision not found")
            status, dec, state, valid_until = row
            if status != "valid" or dec not in ("BUY", "SELL"):
                raise HTTPException(409, f"not executable (status={status}, decision={dec})")
            if valid_until and now_ms() >= valid_until:
                raise HTTPException(409, "recommendation expired")
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
                snap = await asyncio.to_thread(_status_snapshot, app_db)
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


def _process_list() -> list[dict]:
    """Our processes and their RSS (cached 10 s — enumerating command lines is slow on Windows)."""
    if now_ms() - _PROC_CACHE["ts"] < 10_000:
        return _PROC_CACHE["rows"]
    rows = []
    for p in psutil.process_iter(["pid", "name", "cmdline", "memory_info"]):
        try:
            cmd = " ".join(p.info.get("cmdline") or [])
        except (psutil.Error, TypeError):
            continue
        if "tradingsystem" in cmd or "recorder.py" in cmd or ("multiprocessing" in cmd and "Python312" in cmd):
            rows.append({"pid": p.info["pid"], "rss_mb": round(p.info["memory_info"].rss / 2**20), "cmd": cmd[-80:]})
    _PROC_CACHE.update(ts=now_ms(), rows=rows)
    return rows


def _status_snapshot(app_db: Path) -> dict:
    out: dict[str, Any] = {"server_time": now_ms(), "server_time_iso": iso(now_ms())}
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
        day0 = now_ms() // 86_400_000 * 86_400_000
        u = _rows(con, "SELECT provider, count(*) AS calls, COALESCE(sum(cost_usd),0) AS cost, "
                       "COALESCE(sum(ok),0) AS ok FROM ai_usage WHERE ts>=? GROUP BY provider", (day0,))
        out["ai_today"] = u
        out["paper_account"] = _rows(con, "SELECT * FROM paper_account")
    du = shutil.disk_usage(app_db.anchor)
    out["disk_free_gb"] = round(du.free / 2**30, 1)
    procs = _process_list()
    out["processes"] = procs
    out["processes_rss_mb"] = sum(p["rss_mb"] for p in procs)
    return out


def _performance(app_db: Path) -> dict:
    if not app_db.exists():
        return {}
    with _ro(app_db) as con:
        by_status = _rows(con, "SELECT status, decision, count(*) AS n FROM ai_decisions GROUP BY status, decision")
        outcomes = _rows(con, "SELECT outcome, count(*) AS n, COALESCE(sum(outcome_pnl_usd),0) AS pnl FROM ai_decisions "
                              "WHERE outcome IS NOT NULL GROUP BY outcome")
        virtual = _rows(con, "SELECT virtual_outcome, count(*) AS n, COALESCE(avg(virtual_r),0) AS avg_r FROM ai_decisions "
                             "WHERE virtual_outcome IS NOT NULL GROUP BY virtual_outcome")
        cost = _rows(con, "SELECT COALESCE(sum(cost_usd),0) AS total, count(*) AS calls FROM ai_usage")
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
