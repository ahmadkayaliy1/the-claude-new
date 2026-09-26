"""Decision persistence (P8.7, spec §8.3) in ``app.db``.

For every AI decision the chain is stored and linked: payload (zlib-compressed JSON, keyed by payload hash) →
prompt hash / provider / model / config hash / git SHA → raw model text → validated recommendation (or the
validation errors) → execution state → outcome (filled by the execution layer).
Sub-agent outputs (timeframe analysts, risk reviewer, consensus members) are stored alongside.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import threading
import uuid
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.settings import PROJECT_ROOT
from ..core.timeutil import MS_PER_DAY, now_ms
from ..storage.sqlite_store import connect

_DDL = [
    """CREATE TABLE IF NOT EXISTS ai_payloads (
        payload_hash TEXT PRIMARY KEY, ts INTEGER NOT NULL, pair TEXT NOT NULL, payload_z BLOB NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS ai_decisions (
        id TEXT PRIMARY KEY, ts INTEGER NOT NULL, pair TEXT NOT NULL, mode TEXT NOT NULL, trigger TEXT,
        provider TEXT, model TEXT, prompt_hash TEXT, payload_hash TEXT, config_hash TEXT, git_sha TEXT,
        status TEXT NOT NULL, decision TEXT, order_type TEXT, confidence INTEGER, rr_computed REAL,
        valid_until INTEGER, recommendation TEXT, raw_text TEXT, errors TEXT, cost_usd REAL, latency_ms INTEGER,
        input_tokens INTEGER, output_tokens INTEGER,
        execution_state TEXT NOT NULL DEFAULT 'not_executed', execution_detail TEXT,
        outcome TEXT, outcome_pnl_usd REAL, outcome_pnl_pct REAL, outcome_pips REAL, outcome_ts INTEGER,
        virtual_outcome TEXT, virtual_r REAL)""",
    "CREATE INDEX IF NOT EXISTS ai_decisions_pair_ts ON ai_decisions(pair, ts)",
    "CREATE INDEX IF NOT EXISTS ai_decisions_exec ON ai_decisions(execution_state)",
    """CREATE TABLE IF NOT EXISTS ai_sub_outputs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, decision_id TEXT NOT NULL, role TEXT NOT NULL, label TEXT,
        provider TEXT, model TEXT, prompt_hash TEXT, ok INTEGER, output TEXT, errors TEXT, cost_usd REAL)""",
    "CREATE INDEX IF NOT EXISTS ai_sub_outputs_decision ON ai_sub_outputs(decision_id)",
]

# Phase 3 columns (added in place on existing databases; old code ignores them)
EXTRA_COLUMNS = (("actions_state", "TEXT"), ("library_hash", "TEXT"), ("trigger_strength", "TEXT"))
ACTION_STATES = ("pending", "done")

EXECUTION_STATES = ("not_executed", "queued", "executing", "executed", "rejected", "expired", "cancelled")
STATUSES = ("valid", "invalid", "refused", "budget_blocked", "error", "skipped")


def git_sha() -> str | None:
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=PROJECT_ROOT, capture_output=True,
                             text=True, timeout=5)
        return out.stdout.strip() or None
    except Exception:  # noqa: BLE001
        return None


@dataclass
class DecisionRecord:
    pair: str
    mode: str
    trigger: str
    status: str
    provider: str | None = None
    model: str | None = None
    prompt_hash: str | None = None
    payload_hash: str | None = None
    recommendation: dict | None = None
    raw_text: str | None = None
    errors: list[str] = field(default_factory=list)
    cost_usd: float = 0.0
    latency_ms: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    rr_computed: float | None = None
    sub_outputs: list[dict] = field(default_factory=list)
    trigger_strength: str | None = None      # strong | weak | review | event | idle | close | manual
    library_hash: str | None = None          # the whole prompt library's hash (prompt versions in force)
    actions_state: str | None = None         # 'pending' while position_actions await the executor
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    ts: int = field(default_factory=now_ms)


class DecisionStore:
    def __init__(self, app_db: Path, config_hash: str = "") -> None:
        self._con = connect(app_db, cache_mb=4)
        self._lock = threading.Lock()
        self.config_hash = config_hash
        self.git_sha = git_sha()
        with self._lock:
            for s in _DDL:
                self._con.execute(s)
            have = {r[1] for r in self._con.execute("PRAGMA table_info(ai_decisions)")}
            for col, typ in EXTRA_COLUMNS:          # several services open the store concurrently
                if col not in have:
                    try:
                        self._con.execute(f"ALTER TABLE ai_decisions ADD COLUMN {col} {typ}")
                    except sqlite3.OperationalError as exc:
                        if "duplicate column" not in str(exc):
                            raise

    def save_payload(self, payload_hash: str, pair: str, payload: dict) -> None:
        blob = zlib.compress(json.dumps(payload, separators=(",", ":"), default=str).encode(), 6)
        with self._lock:
            self._con.execute("INSERT OR IGNORE INTO ai_payloads VALUES (?,?,?,?)", (payload_hash, now_ms(), pair, blob))

    def load_payload(self, payload_hash: str) -> dict | None:
        with self._lock:
            r = self._con.execute("SELECT payload_z FROM ai_payloads WHERE payload_hash=?", (payload_hash,)).fetchone()
        return json.loads(zlib.decompress(r[0])) if r else None

    def save(self, rec: DecisionRecord) -> str:
        r = rec.recommendation or {}
        vu = r.get("valid_until")
        valid_until = None
        if vu:
            from ..core.timeutil import parse_date_spec
            valid_until = parse_date_spec(vu)
        with self._lock:
            self._con.execute(
                """INSERT INTO ai_decisions(id, ts, pair, mode, trigger, provider, model, prompt_hash, payload_hash,
                   config_hash, git_sha, status, decision, order_type, confidence, rr_computed, valid_until,
                   recommendation, raw_text, errors, cost_usd, latency_ms, input_tokens, output_tokens,
                   trigger_strength, library_hash, actions_state)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (rec.id, rec.ts, rec.pair, rec.mode, rec.trigger, rec.provider, rec.model, rec.prompt_hash,
                 rec.payload_hash, self.config_hash, self.git_sha, rec.status, r.get("decision"), r.get("order_type"),
                 r.get("confidence"), rec.rr_computed, valid_until,
                 json.dumps(r, default=str) if r else None, rec.raw_text, json.dumps(rec.errors) if rec.errors else None,
                 rec.cost_usd, rec.latency_ms, rec.input_tokens, rec.output_tokens, rec.trigger_strength,
                 rec.library_hash, rec.actions_state))
            for s in rec.sub_outputs:
                self._con.execute(
                    "INSERT INTO ai_sub_outputs(decision_id, role, label, provider, model, prompt_hash, ok, output, errors, cost_usd) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (rec.id, s.get("role"), s.get("label"), s.get("provider"), s.get("model"), s.get("prompt_hash"),
                     int(bool(s.get("ok"))), json.dumps(s.get("output"), default=str) if s.get("output") is not None else None,
                     json.dumps(s.get("errors")) if s.get("errors") else None, s.get("cost_usd", 0.0)))
        return rec.id

    def set_execution_state(self, decision_id: str, state: str, detail: dict | None = None) -> None:
        if state not in EXECUTION_STATES:
            raise ValueError(state)
        with self._lock:
            self._con.execute("UPDATE ai_decisions SET execution_state=?, execution_detail=? WHERE id=?",
                              (state, json.dumps(detail, default=str) if detail else None, decision_id))

    def pending_actions(self, pair: str, since_ms: int) -> list[dict[str, Any]]:
        """Valid decisions of ``pair`` whose ``position_actions`` the executor has not handled yet."""
        with self._lock:
            rows = self._con.execute(
                "SELECT id, ts, recommendation FROM ai_decisions WHERE pair=? AND status='valid' "
                "AND actions_state='pending' AND ts>=? ORDER BY ts", (pair, since_ms)).fetchall()
        return [{"id": r[0], "ts": r[1], "rec": json.loads(r[2]) if r[2] else {}} for r in rows]

    def set_actions_state(self, decision_id: str, state: str) -> None:
        if state not in ACTION_STATES:
            raise ValueError(state)
        with self._lock:
            self._con.execute("UPDATE ai_decisions SET actions_state=? WHERE id=?", (state, decision_id))

    def set_outcome(self, decision_id: str, outcome: str, pnl_usd: float | None, pnl_pct: float | None,
                    pips: float | None) -> None:
        with self._lock:
            self._con.execute("UPDATE ai_decisions SET outcome=?, outcome_pnl_usd=?, outcome_pnl_pct=?, outcome_pips=?, "
                              "outcome_ts=? WHERE id=?", (outcome, pnl_usd, pnl_pct, pips, now_ms(), decision_id))

    def recent(self, pair: str, limit: int = 5) -> list[dict[str, Any]]:
        """Compact history for the snapshot (spec rule 10: consistency with recent decisions)."""
        with self._lock:     # 'skipped' rows never reached the model (data gate) — not part of its history
            rows = self._con.execute(
                "SELECT ts, status, decision, order_type, confidence, recommendation, execution_state, outcome, "
                "outcome_pnl_pct, virtual_outcome, execution_detail FROM ai_decisions WHERE pair=? AND status!='skipped' "
                "ORDER BY ts DESC LIMIT ?", (pair, limit)).fetchall()
        out = []
        for ts, status, dec, ot, conf, rec, ex, outc, pnl, vo, det in rows:
            r = json.loads(rec) if rec else {}
            h = {"time": _iso(ts), "status": status, "decision": dec, "order_type": ot, "confidence": conf,
                 "entry": r.get("entry"), "stop_loss": r.get("stop_loss"),
                 "take_profits": [tp.get("price") for tp in r.get("take_profits", [])],
                 "summary": (r.get("market_summary") or "")[:200], "execution_state": ex, "outcome": outc,
                 "outcome_pnl_pct": pnl, "virtual_outcome": vo}
            if ex == "rejected" and det:
                by, why = _rejection(det)
                h["rejected_by"] = by
                if why:
                    h["gate_reason" if by == "gate" else "reject_reason"] = why[:160]
            out.append(h)
        return out

    def memory(self, pair: str) -> dict[str, Any]:
        """The model's own notes from its latest valid decision on ``pair`` (Phase 1 operator memory)."""
        with self._lock:     # the latest valid decision that carries notes (a later one without notes keeps them)
            r = self._con.execute(
                "SELECT ts, decision, execution_state, recommendation FROM ai_decisions WHERE pair=? AND status='valid' "
                "AND COALESCE(json_extract(recommendation, '$.operator_notes'), '') != '' ORDER BY ts DESC LIMIT 1",
                (pair,)).fetchone()
        if not r or not r[3]:
            return {}
        notes = (json.loads(r[3]).get("operator_notes") or "").strip()
        return {"time": _iso(r[0]), "decision": r[1], "execution_state": r[2], "notes": notes} if notes else {}

    def performance(self, pair: str, days: int = 30, now: int | None = None) -> dict[str, Any]:
        """The model's record on ``pair`` over ``days``: cycles, answers, trade ideas, gate results, broker and
        virtual outcomes (every idea is scored on real prices, executed or not)."""
        since = (now or now_ms()) - days * MS_PER_DAY
        with self._lock:
            rows = self._con.execute(
                "SELECT status, decision, execution_state, outcome, outcome_pnl_usd, virtual_outcome, virtual_r, "
                "execution_detail FROM ai_decisions WHERE pair=? AND ts>=? AND status!='skipped'", (pair, since)).fetchall()
        if not rows:
            return {}
        ideas = [r for r in rows if r[0] == "valid" and r[1] in ("BUY", "SELL")]
        vo = [r for r in ideas if r[5]]
        closed = [r for r in ideas if r[3] in ("closed_profit", "closed_loss", "closed_breakeven")]
        rs = [r[6] for r in vo if r[5] in ("tp1_first", "sl_first") and r[6] is not None]
        return {
            "days": days, "cycles": len(rows), "answered": sum(r[0] in ("valid", "invalid", "refused") for r in rows),
            "no_trade": sum(r[0] == "valid" and r[1] == "NO_TRADE" for r in rows), "trade_ideas": len(ideas),
            "gate_rejected": sum(r[2] == "rejected" and _rejection(r[7])[0] == "gate" for r in ideas),
            "not_placed": sum(r[2] == "rejected" and _rejection(r[7])[0] != "gate" for r in ideas),
            "expired": sum(r[2] == "expired" for r in ideas), "executed": sum(r[2] == "executed" for r in ideas),
            "virtual": {k: sum(r[5] == k for r in vo) for k in ("tp1_first", "sl_first", "not_triggered", "unresolved_24h")}
            | ({"mean_r": round(sum(rs) / len(rs), 2)} if rs else {}),
            "broker": {"closed": len(closed), "pnl_usd": round(sum(r[4] or 0.0 for r in closed), 2)} if closed else {},
        }

    def last_decision(self, pair: str) -> dict | None:
        with self._lock:
            r = self._con.execute("SELECT id, ts, recommendation FROM ai_decisions WHERE pair=? AND status='valid' "
                                  "ORDER BY ts DESC LIMIT 1", (pair,)).fetchone()
        if not r:
            return None
        return {"id": r[0], "ts": r[1], "recommendation": json.loads(r[2]) if r[2] else None}

    def last_attempt_ts(self, pair: str, *, answered: bool = False) -> int | None:
        """Time of the latest stored cycle for ``pair`` in any status (spacing survives restarts and failed cycles
        count — F1); ``answered``: only cycles where the model answered (valid / invalid / refused) — those consume
        a pending review, a failed call or a data-gate skip does not."""
        q = "SELECT max(ts) FROM ai_decisions WHERE pair=?" + (
            " AND status IN ('valid','invalid','refused')" if answered else "")
        with self._lock:
            r = self._con.execute(q, (pair,)).fetchone()
        return int(r[0]) if r and r[0] is not None else None

    def realised_since(self, since_ms: int) -> tuple[float, int]:
        """(realised PnL USD, closed trades) since ``since_ms`` — the Cost Governor's ROI input (D-004, F13)."""
        with self._lock:
            r = self._con.execute(
                "SELECT COALESCE(sum(outcome_pnl_usd),0), count(*) FROM ai_decisions "
                "WHERE outcome IN ('closed_profit','closed_loss','closed_breakeven') AND outcome_ts>=?",
                (since_ms,)).fetchone()
        return float(r[0]), int(r[1])

    def close(self) -> None:
        with self._lock:
            self._con.close()


def _rejection(detail_json: str | None) -> tuple[str, str]:
    """(who rejected — gate / broker / system, why) from an execution_detail: the risk gate records its check list
    and never reaches the backend; a broker refusal carries the backend's result."""
    try:
        d = json.loads(detail_json) if detail_json else {}
    except ValueError:
        return "system", ""
    if not isinstance(d, dict):
        return "system", ""
    if d.get("backend"):
        return "broker", str((d["backend"] or {}).get("reason") or d.get("reason") or "")
    if d.get("gate"):
        return "gate", str(d.get("reason") or "")
    return "system", str(d.get("reason") or "")


def _iso(ms: int) -> str:
    from ..core.timeutil import iso
    return iso(ms)
