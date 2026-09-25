/* Trading Desk dashboard — vanilla JS, live via WebSocket (no polling of the browser). */
"use strict";
const TOKEN = document.querySelector('meta[name="dashboard-token"]').content;
const $ = (s, el = document) => el.querySelector(s);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmtT = (ms) => (ms ? new Date(ms).toISOString().replace("T", " ").slice(0, 19) : "—");
const age = (s) => (s == null ? "—" : s < 60 ? `${s.toFixed(1)}s` : s < 3600 ? `${(s / 60).toFixed(1)}m` : `${(s / 3600).toFixed(1)}h`);
const num = (v, d = 2) => (v == null || Number.isNaN(v) ? "—" : Number(v).toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d }));
const api = async (path, opts = {}) => {
  const r = await fetch(path, opts);
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  return r.json();
};

let PAIRS = [];
let STATUS = {};
const latestDecision = {};

/* ------------------------------------------------------------------ tabs */
document.querySelectorAll("#tabs button").forEach((b) =>
  b.addEventListener("click", () => {
    document.querySelectorAll("#tabs button").forEach((x) => x.classList.toggle("active", x === b));
    document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.id === `tab-${b.dataset.tab}`));
    if (b.dataset.tab === "decisions") loadDecisions();
    if (b.dataset.tab === "performance") loadPerformance();
    if (b.dataset.tab === "health") loadEvents();
    if (b.dataset.tab === "charts" && !chart) loadChart();
  })
);

/* ------------------------------------------------------------------ live */
function renderLive() {
  const quotes = Object.fromEntries((STATUS.quotes || []).map((q) => [q.instrument, q]));
  const cards = PAIRS.map((p) => {
    const prim = p.instruments.find((i) => i.roles.includes("analysis_primary"));
    const exe = p.instruments.find((i) => i.roles.includes("execution"));
    const pq = quotes[prim.key], eq = quotes[exe.key];
    const d = p.price_decimals;
    const basis = pq && eq && prim.key !== exe.key ? (eq.bid + eq.ask) / 2 - (pq.bid + pq.ask) / 2 : null;
    const rec = latestDecision[p.pair];
    return `<div class="card">
      <h3>${esc(p.pair)} <span class="muted small">${esc(p.asset_class)} · decision ${esc(p.decision_timeframe)}</span></h3>
      <div class="kv">
        <div>Analysis ${esc(prim.key)}</div><div class="price">${pq ? num((pq.bid + pq.ask) / 2, d) : "—"} <span class="muted small">${pq ? age(pq.age_s) : ""}</span></div>
        <div>Execution ${esc(exe.key)}</div><div>${eq ? `${num(eq.bid, d)} / ${num(eq.ask, d)} · spread ${num(eq.ask - eq.bid, d)}` : "—"} <span class="muted small">${eq ? age(eq.age_s) : ""}</span></div>
        ${basis != null ? `<div>Basis (exec − analysis)</div><div>${num(basis, d)}</div>` : ""}
      </div>
      ${renderRecCard(p.pair, rec, d)}
    </div>`;
  });
  $("#pair-cards").innerHTML = cards.join("");
  document.querySelectorAll("button[data-exec]").forEach((b) => b.addEventListener("click", () => executeNow(b.dataset.exec)));
  const tb = $("#collectors tbody");
  tb.innerHTML = (STATUS.collectors || []).map((c) => {
    const det = c.detail || {};
    const keys = ["messages", "reconnects", "rows_written", "rows_rejected", "downloaded_mb", "mode", "policy", "governor_level", "equity", "open_positions"];
    const brief = keys.filter((k) => det[k] != null).map((k) => `${k}=${esc(det[k])}`).join(" · ");
    const lastData = c.last_data_ms ? age(Math.max(0, (STATUS.server_time - c.last_data_ms) / 1000)) : "—";
    return `<tr><td>${esc(c.collector)}</td><td class="state-${esc(c.state)}">${esc(c.state)}</td><td>${lastData}</td>
      <td>${age(c.heartbeat_age_s)}</td><td class="small">${brief}</td><td class="small fail">${esc(c.last_error || "")}</td></tr>`;
  }).join("");
  const ex = (STATUS.collectors || []).find((c) => c.collector === "executor");
  const mode = ex?.detail?.mode || "—";
  $("#mode-badge").textContent = `${mode.toUpperCase()} · ${ex?.detail?.trigger || ""}`;
  $("#mode-badge").className = `badge ${mode}`;
  const eng = (STATUS.collectors || []).find((c) => c.collector === "engine");
  $("#ai-badge").textContent = eng?.detail ? `AI ${eng.detail.provider} · ${eng.detail.mode} · gov ${eng.detail.governor_level}` : "AI engine not running";
}

function renderRecCard(pair, rec, d) {
  if (!rec) return `<div class="rec muted">No recommendation yet.</div>`;
  const r = rec.recommendation || {};
  const cls = r.decision === "BUY" ? "buy" : r.decision === "SELL" ? "sell" : "notrade";
  const canExec = rec.status === "valid" && ["BUY", "SELL"].includes(r.decision) && rec.execution_state === "not_executed" && (!rec.valid_until || rec.valid_until > Date.now());
  const entry = r.entry ? (r.entry.range_min != null ? `${num(r.entry.range_min, d)}–${num(r.entry.range_max, d)}` : num(r.entry.price, d)) : "—";
  return `<div class="rec">
    <div><b class="${cls}">${esc(r.decision || rec.status)}</b> ${esc(r.order_type || "")} · conf ${esc(r.confidence ?? "—")} · RR ${num(rec.rr_computed, 2)}
      <span class="muted small">· ${fmtT(rec.ts)} · <span class="state-${esc(rec.execution_state)}">${esc(rec.execution_state)}</span></span></div>
    ${["BUY", "SELL"].includes(r.decision) ? `<div class="small">entry ${entry} · SL ${num(r.stop_loss, d)} · TP ${(r.take_profits || []).map((t) => num(t.price, d)).join(" / ")}</div>` : ""}
    <div class="small muted">${esc((r.market_summary || "").slice(0, 220))}</div>
    <div style="margin-top:8px"><button class="primary" data-exec="${esc(rec.id)}" ${canExec ? "" : "disabled"}>Execute Now</button>
      <span class="muted small">the executor re-checks every risk rule before any order</span></div>
  </div>`;
}

async function executeNow(id) {
  if (!confirm("Queue this recommendation for execution? The risk gate re-validates it with live prices first.")) return;
  try {
    const r = await api(`/api/decisions/${id}/execute`, { method: "POST", headers: { "X-Dashboard-Token": TOKEN } });
    alert(`Execution state: ${r.execution_state}${r.note ? " — " + r.note : ""}`);
    refreshLatestDecisions();
  } catch (e) { alert(`Refused: ${e.message}`); }
}

async function refreshLatestDecisions() {
  for (const p of PAIRS) {
    const list = await api(`/api/decisions?pair=${p.pair}&limit=1`);
    if (list.length) latestDecision[p.pair] = await api(`/api/decisions/${list[0].id}`);
  }
  renderLive();
}

/* ------------------------------------------------------------------ websocket */
function connectWS() {
  const ws = new WebSocket(`ws://${location.host}/ws`);
  ws.onopen = () => $("#ws-dot").className = "dot on";
  ws.onclose = () => { $("#ws-dot").className = "dot off"; setTimeout(connectWS, 2000); };
  ws.onmessage = (ev) => {
    STATUS = JSON.parse(ev.data);
    $("#clock").textContent = `${STATUS.server_time_iso?.slice(11, 19) || ""} UTC`;
    if ((STATUS.new_decisions || []).length) refreshLatestDecisions(); else renderLive();
    if ($("#tab-health").classList.contains("active")) renderHealth();
  };
}

/* ------------------------------------------------------------------ charts */
let chart = null, series = null, priceLines = [];
function initChartControls() {
  const inst = $("#ch-inst"), tf = $("#ch-tf");
  inst.innerHTML = PAIRS.flatMap((p) => p.instruments.map((i) => `<option value="${esc(i.key)}" data-pair="${esc(p.pair)}">${esc(p.pair)} · ${esc(i.key)} (${esc(i.roles.join(","))})</option>`)).join("");
  const fillTf = () => {
    const i = PAIRS.flatMap((p) => p.instruments).find((x) => x.key === inst.value);
    tf.innerHTML = (i?.timeframes || []).map((t) => `<option ${t === "15m" ? "selected" : ""}>${t}</option>`).join("");
  };
  inst.addEventListener("change", fillTf);
  fillTf();
  $("#ch-load").addEventListener("click", loadChart);
}

async function loadChart() {
  const key = $("#ch-inst").value, tf = $("#ch-tf").value;
  const from = $("#ch-from").value ? Date.parse($("#ch-from").value + "Z") : null;
  const to = $("#ch-to").value ? Date.parse($("#ch-to").value + "Z") : null;
  const q = new URLSearchParams({ instrument: key, tf, limit: 2000 });
  if (from) q.set("start", from); if (to) q.set("end", to);
  const c = await api(`/api/candles?${q}`);
  if (!chart) {
    chart = LightweightCharts.createChart($("#chart"), {
      layout: { background: { color: "#161b22" }, textColor: "#d7dde8" },
      grid: { vertLines: { color: "#1f2633" }, horzLines: { color: "#1f2633" } },
      timeScale: { timeVisible: true, secondsVisible: false }, rightPriceScale: { borderColor: "#2a3140" },
      crosshair: { mode: 0 },
    });
    series = chart.addCandlestickSeries({ upColor: "#26a269", downColor: "#e5484d", borderVisible: false, wickUpColor: "#26a269", wickDownColor: "#e5484d" });
    new ResizeObserver(() => chart.applyOptions({ width: $("#chart").clientWidth })).observe($("#chart"));
  }
  series.setData(c.t.map((t, i) => ({ time: t, open: c.o[i], high: c.h[i], low: c.l[i], close: c.c[i] })));
  priceLines.forEach((l) => series.removePriceLine(l)); priceLines = [];
  const pair = $("#ch-inst").selectedOptions[0]?.dataset.pair;
  const rec = latestDecision[pair]?.recommendation;
  let note = `${c.t.length} bars · volume = ${c.volume_kind}`;
  if ($("#ch-levels").checked && rec && ["BUY", "SELL"].includes(rec.decision)) {
    if (rec.price_reference === key) {
      const add = (p, color, title) => p != null && priceLines.push(series.createPriceLine({ price: p, color, lineWidth: 1, lineStyle: 2, title }));
      const e = rec.entry || {};
      add(e.price, "#4c8dff", "entry"); add(e.range_min, "#4c8dff", "zone"); add(e.range_max, "#4c8dff", "zone");
      add(rec.stop_loss, "#e5484d", "SL");
      (rec.take_profits || []).forEach((t, i) => add(t.price, "#26a269", `TP${i + 1}`));
    } else note += ` · recommendation levels are in ${rec.price_reference} prices (select that instrument to overlay them)`;
  }
  $("#ch-note").textContent = note;
  chart.timeScale().fitContent();
}

/* ------------------------------------------------------------------ decisions */
async function loadDecisions() {
  const pair = $("#dec-pair").value;
  const rows = await api(`/api/decisions?limit=200${pair ? `&pair=${pair}` : ""}`);
  $("#decisions tbody").innerHTML = rows.map((r) => `<tr data-id="${esc(r.id)}">
    <td>${fmtT(r.ts)}</td><td>${esc(r.pair)}</td><td class="state-${esc(r.status)}">${esc(r.status)}</td>
    <td class="${r.decision === "BUY" ? "buy" : r.decision === "SELL" ? "sell" : "notrade"}">${esc(r.decision || "—")}</td>
    <td>${esc(r.order_type || "")}</td><td>${esc(r.confidence ?? "")}</td><td>${num(r.rr_computed, 2)}</td>
    <td class="state-${esc(r.execution_state)}">${esc(r.execution_state)}</td>
    <td>${esc(r.outcome || "")}${r.outcome_pnl_usd != null ? ` (${num(r.outcome_pnl_usd)}$)` : ""}</td>
    <td>${esc(r.virtual_outcome || "")}</td><td>${num(r.cost_usd, 4)}</td>
    <td class="small muted">${esc(r.mode)} · ${esc((r.trigger || "").slice(0, 60))}</td></tr>`).join("") || `<tr><td colspan="12" class="muted">No decisions yet.</td></tr>`;
  document.querySelectorAll("#decisions tbody tr[data-id]").forEach((tr) => tr.addEventListener("click", () => showDecision(tr.dataset.id)));
}

async function showDecision(id) {
  const d = await api(`/api/decisions/${id}`);
  const r = d.recommendation || {};
  const gate = (d.execution_detail?.gate || []).map((g) => `<tr><td>${esc(g.check)}</td><td class="${g.ok ? "pass" : "fail"}">${g.ok ? "pass" : "FAIL"}</td><td>${esc(g.detail)}</td></tr>`).join("");
  const caps = d.payload ? Object.entries(d.payload.capabilities || {}).map(([k, v]) => `<span class="q-${esc(v.quality)}">${esc(k)}: ${esc(v.quality)}</span>`).join(" · ") : "";
  const el = $("#dec-detail");
  el.classList.remove("hidden");
  el.innerHTML = `<h3>${esc(d.pair)} · ${esc(r.decision || d.status)} ${esc(r.order_type || "")} <span class="muted small">${fmtT(d.ts)} · ${esc(d.provider)}/${esc(d.model)} · prompt ${esc(d.prompt_hash)} · payload ${esc(d.payload_hash)}</span></h3>
    <p>${esc(r.market_summary || "")}</p>
    <p class="small"><b>Reasoning:</b> ${esc(r.reasoning_trace || "")}</p>
    ${r.instructions ? `<p class="small"><b>Instructions:</b> ${esc(r.instructions)}</p>` : ""}
    ${(r.data_quality_notes || []).length ? `<p class="small"><b>Data quality:</b> ${esc(r.data_quality_notes.join(" · "))}</p>` : ""}
    <p class="small"><b>Capabilities used:</b> ${caps}</p>
    ${d.errors ? `<p class="small fail">${esc(JSON.stringify(d.errors))}</p>` : ""}
    ${gate ? `<h2>Risk gate (${esc(d.execution_state)})</h2><table class="grid"><tbody>${gate}</tbody></table>` : ""}
    <details><summary>Recommendation JSON</summary><pre>${esc(JSON.stringify(r, null, 1))}</pre></details>
    ${d.sub_outputs?.length ? `<details><summary>Sub-agent outputs (${d.sub_outputs.length})</summary><pre>${esc(JSON.stringify(d.sub_outputs, null, 1))}</pre></details>` : ""}
    ${d.paper_legs?.length ? `<details open><summary>Paper legs</summary><pre>${esc(JSON.stringify(d.paper_legs, null, 1))}</pre></details>` : ""}
    <details><summary>Payload sent to the model</summary><pre>${esc(JSON.stringify(d.payload, null, 1))}</pre></details>
    <details><summary>Raw model output</summary><pre>${esc(d.raw_text || "")}</pre></details>`;
  el.scrollIntoView({ behavior: "smooth" });
}

/* ------------------------------------------------------------------ performance */
async function loadPerformance() {
  const p = await api("/api/performance");
  const kpi = (v, l) => `<div class="card"><div class="v">${v}</div><div class="l">${l}</div></div>`;
  $("#kpis").innerHTML = [
    kpi((p.by_status || []).reduce((a, x) => a + x.n, 0), "AI decisions"),
    kpi(p.win_rate == null ? "—" : `${(p.win_rate * 100).toFixed(1)}%`, "win rate (executed)"),
    kpi(`${num(p.realized_pnl_usd)}$`, "realised PnL"),
    kpi(`${num(p.ai_cost_usd, 4)}$`, `AI cost (${p.ai_calls || 0} calls)`),
    kpi(p.ai_cost_to_profit == null ? "—" : num(p.ai_cost_to_profit, 3), "AI cost / profit"),
  ].join("");
  const t = (rows, cols) => rows.length ? `<tr>${cols.map((c) => `<th>${c}</th>`).join("")}</tr>` + rows.map((r) => `<tr>${cols.map((c) => `<td>${esc(typeof r[c] === "number" ? num(r[c], c === "n" || c === "decisions" || c === "trades" ? 0 : 2) : r[c])}</td>`).join("")}</tr>`).join("") : `<tr><td class="muted">no data yet</td></tr>`;
  $("#perf-outcomes tbody").innerHTML = t(p.outcomes || [], ["outcome", "n", "pnl"]);
  $("#perf-virtual tbody").innerHTML = t(p.virtual || [], ["virtual_outcome", "n", "avg_r"]);
  $("#perf-pairs tbody").innerHTML = t(p.per_pair || [], ["pair", "decisions", "trades", "pnl"]);
}

/* ------------------------------------------------------------------ health */
function renderHealth() {
  const kpi = (v, l) => `<div class="card"><div class="v">${v}</div><div class="l">${l}</div></div>`;
  $("#health-kpis").innerHTML = [
    kpi(`${STATUS.processes_rss_mb ?? "—"} MB`, "our processes RSS"),
    kpi(`${STATUS.disk_free_gb ?? "—"} GB`, "free disk"),
    kpi((STATUS.collectors || []).filter((c) => ["live", "market_closed", "backfilling"].includes(c.state)).length + "/" + (STATUS.collectors || []).length, "collectors healthy"),
  ].join("");
  $("#procs tbody").innerHTML = `<tr><th>PID</th><th>RSS MB</th><th>Command</th></tr>` + (STATUS.processes || []).map((p) => `<tr><td>${p.pid}</td><td>${p.rss_mb}</td><td class="small">${esc(p.cmd)}</td></tr>`).join("");
  $("#ai-usage tbody").innerHTML = `<tr><th>Provider</th><th>Calls</th><th>OK</th><th>Cost $</th></tr>` + ((STATUS.ai_today || []).map((u) => `<tr><td>${esc(u.provider)}</td><td>${u.calls}</td><td>${u.ok}</td><td>${num(u.cost, 4)}</td></tr>`).join("") || `<tr><td colspan="4" class="muted">no AI calls today</td></tr>`);
}

async function loadEvents() {
  renderHealth();
  const ev = await api("/api/events?limit=200");
  $("#events tbody").innerHTML = ev.map((e) => `<tr><td>${fmtT(e.ts)}</td><td>${esc(e.collector)}</td><td>${esc(e.event)}</td><td class="small">${esc(e.detail || "")}</td><td>${e.duration_ms ? age(e.duration_ms / 1000) : ""}</td></tr>`).join("");
}

/* ------------------------------------------------------------------ boot */
(async function boot() {
  PAIRS = await api("/api/pairs");
  $("#dec-pair").innerHTML += PAIRS.map((p) => `<option>${esc(p.pair)}</option>`).join("");
  $("#dec-pair").addEventListener("change", loadDecisions);
  $("#dec-refresh").addEventListener("click", loadDecisions);
  initChartControls();
  STATUS = await api("/api/status");
  await refreshLatestDecisions();
  connectWS();
})();
