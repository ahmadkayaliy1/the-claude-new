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
let SKEW = 0;                                  // server clock − browser clock (ms), refreshed on every push
const serverNow = () => Date.now() + SKEW;
const latestDecision = {};
const setHTML = (el, html) => { if (el && el._html !== html) { el.innerHTML = html; el._html = html; } };   // no churn → no lost clicks
const killSwitchOn = () => !!(STATUS.collectors || []).find((c) => c.collector === "executor")?.detail?.kill_switch;

/* Execute Now is possible until the earlier of valid_until and timestamp + max_recommendation_age_s (the risk gate's
   rule: the recommendation's timestamp is the cycle's data as-of, earlier than the row's ts for a slow AI call). */
function execDeadline(p, rec) {
  const r = rec?.recommendation || {};
  if (!p || !rec || rec.status !== "valid" || !["BUY", "SELL"].includes(r.decision) || rec.execution_state !== "not_executed") return null;
  const asOf = Date.parse(r.timestamp) || rec.ts;
  return Math.min(rec.valid_until || Infinity, asOf + (p.max_recommendation_age_s ?? 300) * 1000);
}

function updateCountdowns() {
  document.querySelectorAll("[data-deadline]").forEach((el) => {
    const s = Math.max(0, (Number(el.dataset.deadline) - serverNow()) / 1000);
    el.textContent = s >= 60 ? `${Math.floor(s / 60)}m ${String(Math.floor(s % 60)).padStart(2, "0")}s` : `${Math.floor(s)}s`;
  });
}

function execControls(p, rec) {
  const deadline = execDeadline(p, rec);
  if (deadline == null) return "";
  const kill = killSwitchOn(), open = serverNow() < deadline;
  const note = kill ? "kill switch engaged — the executor blocks every new order"
    : open ? `executable for <span data-deadline="${deadline}"></span> · the executor re-checks every risk rule before any order`
      : "too old to execute (validity / recommendation-age limit passed)";
  return `<div style="margin-top:8px"><button class="primary" data-exec="${esc(rec.id)}" ${open && !kill ? "" : "disabled"}>Execute Now</button>
    <span class="muted small">${note}</span></div>`;
}

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
/* Cards are built once; each part is replaced only when its HTML changes, so the Execute button is not recreated
   every second (a click spanning a re-render used to be lost). Countdowns tick in place. */
function ensureCards() {
  const root = $("#pair-cards"), sig = PAIRS.map((p) => p.pair).join(",");
  if (root.dataset.pairs === sig) return;
  root.dataset.pairs = sig;
  root.innerHTML = PAIRS.map((p) => `<div class="card" data-pair="${esc(p.pair)}">
      <h3>${esc(p.pair)} <span class="muted small">${esc(p.asset_class)} · decision ${esc(p.decision_timeframe)}</span></h3>
      <div class="kv" data-part="quotes"></div><div data-part="rec"></div></div>`).join("");
}

function renderLive() {
  ensureCards();
  const quotes = Object.fromEntries((STATUS.quotes || []).map((q) => [q.instrument, q]));
  const qAge = (q) => `<span class="small ${q.stale ? "fail" : "muted"}">${age(q.age_s)}${q.stale ? " · stale" : ""}</span>`;
  PAIRS.forEach((p, i) => {
    const card = $("#pair-cards").children[i];
    const prim = p.instruments.find((x) => x.roles.includes("analysis_primary"));
    const exe = p.instruments.find((x) => x.roles.includes("execution"));
    const pq = quotes[prim.key], eq = quotes[exe.key];
    const d = p.price_decimals;
    const basis = pq && eq && prim.key !== exe.key ? (eq.bid + eq.ask) / 2 - (pq.bid + pq.ask) / 2 : null;
    setHTML(card.querySelector('[data-part="quotes"]'), `
        <div>Analysis ${esc(prim.key)}</div><div class="price">${pq ? num((pq.bid + pq.ask) / 2, d) : "—"} ${pq ? qAge(pq) : ""}</div>
        <div>Execution ${esc(exe.key)}</div><div>${eq ? `${num(eq.bid, d)} / ${num(eq.ask, d)} · spread ${num(eq.ask - eq.bid, d)}` : "—"} ${eq ? qAge(eq) : ""}</div>
        ${basis != null ? `<div>Basis (exec − analysis)</div><div>${num(basis, d)}</div>` : ""}`);
    setHTML(card.querySelector('[data-part="rec"]'), renderRecCard(p, latestDecision[p.pair], d));
  });
  updateCountdowns();
  const tb = $("#collectors tbody");
  setHTML(tb, (STATUS.collectors || []).map((c) => {
    const det = c.detail || {};
    const keys = ["messages", "reconnects", "rows_written", "rows_rejected", "downloaded_mb", "mode", "policy", "governor_level", "equity", "open_positions", "open_orders", "errors_last_5min"];
    const brief = keys.filter((k) => det[k] != null).map((k) => `${k}=${esc(det[k])}`).join(" · ");
    const lastData = c.last_data_ms ? age(Math.max(0, (STATUS.server_time - c.last_data_ms) / 1000)) : "—";
    const errAge = c.last_error && c.last_error_ms ? ` <span class="muted">(${age(Math.max(0, (STATUS.server_time - c.last_error_ms) / 1000))} ago)</span>` : "";
    return `<tr><td>${esc(c.collector)}</td><td class="state-${esc(c.state)}">${esc(c.state)}</td><td>${lastData}</td>
      <td>${age(c.heartbeat_age_s)}</td><td class="small">${brief}</td><td class="small fail">${esc(c.last_error || "")}${errAge}</td></tr>`;
  }).join(""));
  const ex = (STATUS.collectors || []).find((c) => c.collector === "executor");
  const mode = ex?.detail?.mode || "—";
  $("#mode-badge").textContent = `${mode.toUpperCase()} · ${ex?.detail?.trigger || ""}`;
  $("#mode-badge").className = `badge ${mode}`;
  $("#kill-badge").classList.toggle("hidden", !killSwitchOn());
  const eng = (STATUS.collectors || []).find((c) => c.collector === "engine");
  $("#ai-badge").textContent = eng?.detail ? `AI ${eng.detail.provider} · ${eng.detail.mode} · gov ${eng.detail.governor_level}` : "AI engine not running";
}

function renderRecCard(p, rec, d) {
  if (!rec) return `<div class="rec muted">No recommendation yet.</div>`;
  const r = rec.recommendation || {};
  const cls = r.decision === "BUY" ? "buy" : r.decision === "SELL" ? "sell" : "notrade";
  const entry = r.entry ? (r.entry.range_min != null ? `${num(r.entry.range_min, d)}–${num(r.entry.range_max, d)}` : num(r.entry.price, d)) : "—";
  const reason = rec.execution_detail?.reason;
  return `<div class="rec">
    <div><b class="${cls}">${esc(r.decision || rec.status)}</b> ${esc(r.order_type || "")} · conf ${esc(r.confidence ?? "—")} · RR ${num(rec.rr_computed, 2)}
      <span class="muted small">· ${fmtT(rec.ts)} · <span class="state-${esc(rec.execution_state)}">${esc(rec.execution_state)}</span></span></div>
    ${reason ? `<div class="small fail">${esc(reason)}</div>` : ""}
    ${["BUY", "SELL"].includes(r.decision) ? `<div class="small">entry ${entry} · SL ${num(r.stop_loss, d)} · TP ${(r.take_profits || []).map((t) => num(t.price, d)).join(" / ")}</div>` : ""}
    <div class="small muted">${esc((r.market_summary || "").slice(0, 220))}</div>
    ${execControls(p, rec)}
  </div>`;
}

async function executeNow(id) {
  if (!confirm("Queue this recommendation for execution? The risk gate re-validates it with live prices first.")) return;
  try {
    const r = await api(`/api/decisions/${encodeURIComponent(id)}/execute`, { method: "POST", headers: { "X-Dashboard-Token": TOKEN } });
    alert(`Execution state: ${r.execution_state}${r.note ? " — " + r.note : ""}`);
  } catch (e) { alert(`Refused: ${e.message}`); }
  refreshLatestDecisions();
  if ($("#dec-detail").dataset.id === id) showDecision(id, false);
}

let refreshing = false;
/* which: {pair: decision id} (from the push) or null = every pair's latest via the list endpoint */
async function refreshLatestDecisions(which = null) {
  if (refreshing) return;
  refreshing = true;
  try {
    const targets = which || Object.fromEntries(PAIRS.map((p) => [p.pair, null]));
    for (const [pair, known] of Object.entries(targets)) {
      const id = known || (await api(`/api/decisions?pair=${encodeURIComponent(pair)}&limit=1`))[0]?.id;
      if (id) latestDecision[pair] = await api(`/api/decisions/${encodeURIComponent(id)}`);
    }
  } catch (e) { console.warn("decision refresh failed", e); }
  finally { refreshing = false; }
  renderLive();
}

/* ------------------------------------------------------------------ websocket */
function connectWS() {
  const ws = new WebSocket(`ws://${location.host}/ws`);
  ws.onopen = () => $("#ws-dot").className = "dot on";
  ws.onclose = () => { $("#ws-dot").className = "dot off"; setTimeout(connectWS, 2000); };
  ws.onmessage = (ev) => {
    STATUS = JSON.parse(ev.data);
    if (STATUS.server_time) SKEW = STATUS.server_time - Date.now();
    $("#clock").textContent = `${STATUS.server_time_iso?.slice(11, 19) || ""} UTC`;
    // follow new decisions *and* state transitions (queued → executing → executed / rejected with its reason)
    const changed = {};
    for (const p of PAIRS) {
      const l = (STATUS.latest_decisions || {})[p.pair], cur = latestDecision[p.pair];
      if (l && (!cur || cur.id !== l.id || cur.execution_state !== l.execution_state)) changed[p.pair] = l.id;
    }
    renderLive();
    if (Object.keys(changed).length) refreshLatestDecisions(changed);
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

async function showDecision(id, scroll = true) {
  const d = await api(`/api/decisions/${encodeURIComponent(id)}`);
  const r = d.recommendation || {};
  const gate = (d.execution_detail?.gate || []).map((g) => `<tr><td>${esc(g.check)}</td><td class="${g.ok ? "pass" : "fail"}">${g.ok ? "pass" : "FAIL"}</td><td>${esc(g.detail)}</td></tr>`).join("");
  const caps = d.payload ? Object.entries(d.payload.capabilities || {}).map(([k, v]) => `<span class="q-${esc(v.quality)}">${esc(k)}: ${esc(v.quality)}</span>`).join(" · ") : "";
  const el = $("#dec-detail");
  el.classList.remove("hidden");
  el.dataset.id = d.id;
  el.innerHTML = `<h3>${esc(d.pair)} · ${esc(r.decision || d.status)} ${esc(r.order_type || "")} <span class="muted small">${fmtT(d.ts)} · ${esc(d.provider)}/${esc(d.model)} · prompt ${esc(d.prompt_hash)} · payload ${esc(d.payload_hash)}</span></h3>
    <p class="small">Execution: <span class="state-${esc(d.execution_state)}">${esc(d.execution_state)}</span>${d.execution_detail?.reason ? ` — <span class="fail">${esc(d.execution_detail.reason)}</span>` : ""}</p>
    ${execControls(PAIRS.find((p) => p.pair === d.pair), d)}
    <p>${esc(r.market_summary || "")}</p>
    <p class="small"><b>Reasoning:</b> ${esc(r.reasoning_trace || "")}</p>
    ${r.instructions ? `<p class="small"><b>Instructions:</b> ${esc(r.instructions)}</p>` : ""}
    ${(r.data_quality_notes || []).length ? `<p class="small"><b>Data quality:</b> ${esc(r.data_quality_notes.join(" · "))}</p>` : ""}
    ${r.operator_notes ? `<p class="small"><b>Operator notes:</b> ${esc(r.operator_notes)}</p>` : ""}
    ${(r.management || []).length ? `<p class="small"><b>Management plan:</b> ${esc(r.management.map((m) => `${m.action} on ${m.trigger}${m.value != null ? " " + m.value : ""}${Object.keys(m.params || {}).length ? " " + JSON.stringify(m.params) : ""}`).join(" · "))}</p>` : ""}
    ${(r.position_actions || []).length ? `<p class="small"><b>Actions on live trades:</b> ${esc(r.position_actions.map((a) => `${a.action} ${a.target.kind} ${a.target.decision}${a.value != null ? " → " + a.value : ""}${a.fraction != null ? " (" + Math.round(a.fraction * 100) + " %)" : ""} — ${a.reason}`).join(" · "))}</p>` : ""}
    ${(d.position_actions || []).length ? `<h2>Actions applied</h2><table class="grid"><tr><th>time</th><th>source</th><th>action</th><th>leg</th><th>status</th><th>detail</th></tr>${d.position_actions.map((a) => `<tr><td>${fmtT(a.ts)}</td><td>${esc(a.source)}</td><td>${esc(a.action)}</td><td>${esc(a.leg)}</td><td class="${a.status === "applied" ? "pass" : a.status === "deferred" || a.status === "skipped" ? "" : "fail"}">${esc(a.status)}</td><td class="small">${esc(typeof a.detail === "string" ? a.detail : JSON.stringify(a.detail || {}).slice(0, 240))}</td></tr>`).join("")}</table>` : ""}
    <div class="charts-strip" data-pair="${esc(d.pair)}"></div>
    <p class="small"><b>Capabilities used:</b> ${caps}</p>
    ${d.errors ? `<p class="small fail">${esc(JSON.stringify(d.errors))}</p>` : ""}
    ${gate ? `<h2>Risk gate (${esc(d.execution_state)})</h2><table class="grid"><tbody>${gate}</tbody></table>` : ""}
    <details><summary>Recommendation JSON</summary><pre>${esc(JSON.stringify(r, null, 1))}</pre></details>
    ${d.sub_outputs?.length ? `<details><summary>Sub-agent outputs (${d.sub_outputs.length})</summary><pre>${esc(JSON.stringify(d.sub_outputs, null, 1))}</pre></details>` : ""}
    ${d.paper_legs?.length ? `<details open><summary>Paper legs</summary><pre>${esc(JSON.stringify(d.paper_legs, null, 1))}</pre></details>` : ""}
    <details><summary>Payload sent to the model</summary><pre>${esc(JSON.stringify(d.payload, null, 1))}</pre></details>
    <details><summary>Raw model output</summary><pre>${esc(d.raw_text || "")}</pre></details>`;
  updateCountdowns();
  loadCharts(el.querySelector(".charts-strip"));
  if (scroll) el.scrollIntoView({ behavior: "smooth" });
}

/* the latest chart images the model received (Phase 3) */
async function loadCharts(box) {
  if (!box) return;
  try {
    const list = await api(`/api/charts/${encodeURIComponent(box.dataset.pair)}`);
    if (!list.length) return;
    box.innerHTML = `<h2>Charts sent to the model (latest)</h2>` + list.map((c) =>
      `<figure class="chart-thumb"><img loading="lazy" alt="${esc(c.tf)} chart" src="/api/charts/${encodeURIComponent(box.dataset.pair)}/${encodeURIComponent(c.tf)}?t=${c.updated_ms}"><figcaption class="small muted">${esc(c.tf)} · ${fmtT(c.updated_ms)}</figcaption></figure>`).join("");
  } catch (e) { /* charts are optional */ }
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
  // one delegated listener each: survives re-renders (no per-render re-binding)
  ["#pair-cards", "#dec-detail"].forEach((sel) => $(sel).addEventListener("click", (e) => {
    const b = e.target.closest("button[data-exec]");
    if (b && !b.disabled) executeNow(b.dataset.exec);
  }));
  setInterval(updateCountdowns, 1000);
  PAIRS = await api("/api/pairs");
  $("#dec-pair").innerHTML += PAIRS.map((p) => `<option>${esc(p.pair)}</option>`).join("");
  $("#dec-pair").addEventListener("change", loadDecisions);
  $("#dec-refresh").addEventListener("click", loadDecisions);
  initChartControls();
  STATUS = await api("/api/status");
  await refreshLatestDecisions();
  connectWS();
})();
