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
/* the switch files themselves (snapshot) or the executor's own report (its heartbeat) */
const killSwitchOn = () => !!STATUS.kill_switch?.on || !!(STATUS.collectors || []).find((c) => c.collector === "executor")?.detail?.kill_switch;

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
    if (b.dataset.tab === "operator") loadOperator();
    if (b.dataset.tab === "tuning") loadTuning();
    if (b.dataset.tab === "proposals") loadProposals();
    if (b.dataset.tab === "reviews") loadReviews();
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
  const killOn = killSwitchOn();
  $("#kill-badge").classList.toggle("hidden", !killOn);
  const ks = STATUS.kill_switch?.files || [];
  $("#kill-badge").title = `${ks.length ? ks.join(" · ") : "reported by the executor"} — the executor blocks all new orders`;
  // the button is an action, never a state (the badge is the header's only indicator): disabled only while the file its
  // POST would write (this system's own switch) or the global switch exists. Not on the badge's "any switch": in the
  // all-pairs system a pair's switch leaves the other pairs trading, so the global stop must stay reachable; never on
  // the executor heartbeat either (a stale row must not block the emergency stop).
  const kb = $("#kill-on");
  kb.dataset.title ??= kb.title;
  const targetOn = !!STATUS.kill_switch?.target_on;
  kb.disabled = targetOn;
  kb.title = targetOn ? "kill switch already engaged for this system (its own switch or the global one)" : kb.dataset.title;
  const eng = (STATUS.collectors || []).find((c) => c.collector === "engine");
  const g = eng?.detail?.usage_gauge;
  const gauge = g && g.level != null ? ` · usage L${g.level}${g.week_pct != null ? ` (${g.week_pct}% wk)` : ""}` : "";
  $("#ai-badge").textContent = eng?.detail ? `AI ${eng.detail.provider} · ${eng.detail.mode} · gov ${eng.detail.governor_level}${gauge}` : "AI engine not running";
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
    ${(r.position_actions || []).length ? `<p class="small"><b>Actions on live trades:</b> ${esc(r.position_actions.map((a) => `${a?.action} ${a?.target?.kind} ${a?.target?.decision}${a?.value != null ? " → " + a.value : ""}${a?.fraction != null ? " (" + Math.round(a.fraction * 100) + " %)" : ""} — ${a?.reason}`).join(" · "))}</p>` : ""}
    ${(d.position_actions || []).length ? `<h2>Actions applied</h2>${actionsTable(d.position_actions)}` : ""}
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
  try { renderCharts(box, await api(`/api/charts/${encodeURIComponent(box.dataset.pair)}`)); } catch (e) { /* charts are optional */ }
}

function renderCharts(box, list) {
  if (!box || !list?.length) return;
  box.innerHTML = `<h2>Charts rendered for the model (latest)</h2>` + list.map((c) =>
    `<figure class="chart-thumb"><img loading="lazy" alt="${esc(c.tf)} chart" src="/api/charts/${encodeURIComponent(box.dataset.pair)}/${encodeURIComponent(c.tf)}?t=${encodeURIComponent(c.updated_ms)}"><figcaption class="small muted">${esc(c.tf)} · ${tSafe(c.updated_ms)}</figcaption></figure>`).join("");
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

/* ------------------------------------------------------------------ Phase 4: operator · tuning · proposals · reviews */
/* Everything written by the model, a review session or a tool goes through esc() and is shown as text (no markdown
   rendering, never a link or a src): the page has no script-src CSP, esc() is the only XSS defence. Loaded on click
   and by the Refresh buttons (D-007: the browser never polls). */
const txt = (v, n = 240) => { if (v == null) return ""; const s = typeof v === "string" ? v : JSON.stringify(v); return s.length > n ? `${s.slice(0, n)}…` : s; };
const tSafe = (ms) => { try { return typeof ms === "number" && ms > 0 ? fmtT(ms) : "—"; } catch (e) { return "—"; } };
const stateSpan = (s) => `<span class="state-${esc(s)}">${esc(s)}</span>`;
const grid = (head, rows, empty) => rows.length
  ? `<table class="grid"><thead><tr>${head.map((h) => `<th>${esc(h)}</th>`).join("")}</tr></thead><tbody>${rows.join("")}</tbody></table>`
  : `<p class="muted small">${esc(empty)}</p>`;
const showError = (box, e) => { box.innerHTML = `<p class="fail small">${esc(e?.message || e)}</p>`; };
const ruleText = (m) => (m ? `${m.action} on ${m.trigger}${m.value != null ? " " + m.value : ""}${Object.keys(m.params || {}).length ? " " + JSON.stringify(m.params) : ""}` : "");

/* position_actions rows (model- and rule-sourced); `full` adds the pair and which decision acted on which */
function actionsTable(list, full = false) {
  const cls = (s) => (s === "applied" ? "pass" : ["deferred", "skipped", "pending"].includes(s) ? "muted" : "fail");
  const head = ["time", ...(full ? ["pair", "from → target"] : []), "source", "action", "leg", "status", "detail"];
  return grid(head, (list || []).map((a) => `<tr><td>${tSafe(a.ts)}</td>${full ? `<td>${esc(a.pair)}</td><td class="small">${esc(a.source_decision)} → ${esc(a.target_decision)}</td>` : ""}
    <td>${esc(a.source)}</td><td>${esc(a.action)}</td><td>${esc(a.leg)}</td><td class="${cls(a.status)}">${esc(a.status)}</td>
    <td class="small">${esc(txt(a.detail, 240))}</td></tr>`), "No action yet.");
}

async function loadOperator() {
  const pair = $("#op-pair").value, box = $("#op-body");
  if (!pair) return;
  let d;
  try { d = await api(`/api/operator/${encodeURIComponent(pair)}?limit=100`); } catch (e) { return showError(box, e); }
  const m = d.memory || {}, la = d.last_model_actions;
  const asked = (la?.actions || []).map((a) => `<tr><td>${esc(a?.action)}</td><td>${esc(a?.target?.kind)} ${esc(a?.target?.decision)}</td>
    <td>${a?.value != null ? esc(txt(a.value, 40)) : ""}${a?.fraction != null ? ` (${esc(Math.round(Number(a.fraction) * 100))} %)` : ""}</td><td class="small">${esc(txt(a?.reason, 400))}</td></tr>`);
  const rules = (d.management_state || []).map((r) => `<tr><td>${tSafe(r.decision_ts)}</td><td class="small">${esc(r.decision_id)}</td><td>${esc(r.decision)}</td>
    <td class="small">#${esc(r.rule_idx)} ${esc(ruleText(r.rule))}</td><td>${esc(r.leg)}</td><td>${stateSpan(r.status)}</td><td>${tSafe(r.applied_ms)}</td>
    <td class="small">${esc(txt(r.detail, 240))}</td></tr>`);
  box.innerHTML = `
    <h2>Operator memory — the model's notes to itself</h2>
    ${m.notes ? `<div class="card"><div class="small muted">${tSafe(m.ts)} · ${esc(m.decision)} · ${stateSpan(m.execution_state)} · decision ${esc(m.id)}</div><p class="prewrap">${esc(m.notes)}</p></div>` : `<p class="muted small">No notes yet.</p>`}
    <h2>Last position actions the model asked for</h2>
    ${la ? `<p class="small muted">${tSafe(la.ts)} · decision ${esc(la.id)} · ${esc(la.decision)} · ${stateSpan(la.execution_state)}</p>${grid(["action", "target", "value", "reason"], asked, "none")}` : `<p class="muted small">None among the latest valid decisions.</p>`}
    <h2>Actions on live trades (${(d.position_actions || []).length})</h2>
    ${actionsTable(d.position_actions, true)}
    <h2>Management rules</h2>
    ${grid(["decision time", "decision", "side", "rule", "leg", "state", "applied", "detail"], rules, "No management rule tracked yet.")}
    <div class="charts-strip" data-pair="${esc(pair)}"></div>`;
  renderCharts(box.querySelector(".charts-strip"), d.charts || []);
}

function tuningPair(pair, x, changes) {
  const valid = x.valid === true ? `<span class="pass">accepted by the services</span>`
    : x.valid === false ? `<span class="fail">INVALID — the services keep their last good values</span>`
      : `<span class="muted">${esc(x.problem || "not validated")}</span>`;
  const val = (e) => (e.key === "pair.ai_paused_until" ? tSafe(e.value) : txt(e.value, 120));
  const entries = (x.entries || []).map((e) => `<tr><td>${esc(e.key)}</td><td>${esc(val(e))}</td><td>${tSafe(e.set_ms)}</td>
    <td class="${e.expired ? "muted" : ""}">${tSafe(e.expires_ms)}${e.expired ? " · expired (default applies)" : ""}</td>
    <td class="small ${e.problem ? "fail" : ""}">${esc(txt(e.problem || e.reason, 400))}</td><td class="small">${esc(e.review_id)}</td></tr>`);
  const eff = Object.entries(x.effective || {}).map(([k, v]) => `<div>${esc(k)}</div><div>${esc(txt(v, 300))}</div>`).join("");
  const pbState = x.playbook == null ? "" : x.playbook_in_force ? `<span class="pass small">in force</span>`
    : `<span class="muted small">not in force (not referenced by adaptive.yaml, expired, invalid or adaptive off)</span>`;
  const ch = (changes || []).map((c) => `<tr><td>${tSafe(c.ts)}</td><td>${stateSpan(c.action)}</td><td>${esc(c.key)}</td>
    <td class="small">${esc(txt(c.old, 80))} → ${esc(txt(c.new ?? c.value, 80))}</td><td>${tSafe(c.expires_ms)}</td>
    <td class="small">${esc(txt(c.reason, 300))}</td><td class="small">${esc(c.actor)}</td><td class="small">${esc(c.review_id)}</td></tr>`);
  return `<div class="card pair-block"><h3>${esc(pair)} <span class="small">${valid}</span></h3>
    ${x.valid === false && x.problem ? `<p class="fail small">${esc(x.problem)}</p>` : ""}
    ${x.yaml_error ? `<p class="fail small">adaptive.yaml: ${esc(x.yaml_error)}</p>` : ""}
    <div class="two-col">
      <div><h2>Overlay (adaptive.yaml)</h2>${x.file ? grid(["key", "value", "set", "expires", "reason", "review"], entries, "adaptive.yaml has no entries.") : `<p class="muted small">No adaptive.yaml — the config values apply.</p>`}</div>
      <div><h2>Effective values</h2>${eff ? `<div class="kv small">${eff}</div>` : `<p class="muted small">—</p>`}</div>
    </div>
    <h2>Playbook ${pbState}</h2>
    ${x.playbook != null ? `<pre class="review">${esc(x.playbook)}</pre>${x.playbook_truncated ? `<p class="small fail">truncated</p>` : ""}` : `<p class="muted small">No playbook.</p>`}
    <details><summary>changes.jsonl (${(changes || []).length})</summary>${grid(["time", "action", "key", "old → new", "expires", "reason", "actor", "review"], ch, "No change yet.")}</details>
  </div>`;
}

async function loadTuning() {
  const box = $("#tu-body");
  let a, t;
  try { [a, t] = await Promise.all([api("/api/adaptive"), api("/api/tuning_changes?limit=200")]); } catch (e) { return showError(box, e); }
  $("#tu-flags").innerHTML = [
    a.enabled ? `<span class="pass">adaptive overlay enabled</span>` : `<span class="fail">adaptive.enabled: false — config defaults everywhere, tune.py refuses</span>`,
    a.tuning_freeze ? `<span class="fail">TUNING_FREEZE on${a.freeze_note ? ": " + esc(a.freeze_note) : ""}</span>` : `<span class="muted">no TUNING_FREEZE</span>`,
  ].join(" · ");
  const rows = (t.table || []).map((r) => `<tr><td>${tSafe(r.ts)}</td><td>${esc(r.pair)}</td><td>${esc(r.key)}</td>
    <td class="small">${esc(txt(r.old_value, 80))} → ${esc(txt(r.new_value, 80))}</td><td>${tSafe(r.expires_ms)}</td>
    <td>${r.reverted_ms ? tSafe(r.reverted_ms) : ""}</td><td class="small">${esc(r.actor)}</td><td class="small">${esc(r.review_id)}</td>
    <td class="small">${esc(txt(r.reason, 300))}</td></tr>`);
  box.innerHTML = Object.entries(a.pairs || {}).map(([pair, x]) => tuningPair(pair, x, (t.changes || {})[pair])).join("")
    + `<h2>tuning_changes (this system's app.db)</h2>`
    + grid(["time", "pair", "key", "old → new", "expires", "reverted", "actor", "review", "reason"], rows, "No tuning change recorded.");
}

/* What a merge of the proposal branch brings in: its base and the commits not in main (1 = the proposal only). */
function baseNote(x) {
  if (x.base == null && x.commits_not_in_main == null) return "";
  const n = x.commits_not_in_main;
  const base = `from ${esc(x.base || "?")}${x.base_sha ? ` @ ${esc(String(x.base_sha).slice(0, 10))}` : ""}`;
  return `<div class="${n != null && n !== 1 ? "fail" : "muted"}">${base} · ${n == null ? "?" : esc(n)} commit(s) not in main</div>`;
}

async function loadProposals() {
  const tb = $("#proposals tbody");
  let p;
  try { p = await api("/api/proposals?limit=200"); } catch (e) { tb.innerHTML = `<tr><td class="fail">${esc(e.message)}</td></tr>`; return; }
  tb.innerHTML = p.length ? `<tr><th>Time (UTC)</th><th>Pair</th><th>Title</th><th>Status</th><th>Branch / worktree</th><th>Document</th><th>Review / commit</th></tr>`
    + p.map((x) => `<tr><td>${tSafe(x.ts)}</td><td>${esc(x.pair || "—")}</td><td><b>${esc(x.title)}</b><div class="small muted">${esc(x.slug)}</div></td>
      <td>${stateSpan(x.status)}</td><td class="small">${esc(x.branch)}<div class="muted">${esc(x.worktree)}</div>${baseNote(x)}</td><td class="small">${esc(x.doc)}</td>
      <td class="small">${esc(x.review_id)}${x.commit ? `<div class="muted">${esc(String(x.commit).slice(0, 10))}</div>` : ""}</td></tr>`).join("")
    : `<tr><td class="muted">No proposal yet.</td></tr>`;
}

const stampT = (s) => (/^\d{8}T\d{6}/.test(s || "") ? `${s.slice(0, 4)}-${s.slice(4, 6)}-${s.slice(6, 8)} ${s.slice(9, 11)}:${s.slice(11, 13)}:${s.slice(13, 15)}` : "—");
const kb = (n) => (typeof n !== "number" ? "—" : n < 1024 ? `${n} B` : `${(n / 1024).toFixed(1)} KB`);

async function loadReviews() {
  const tb = $("#reviews tbody");
  let list;
  try { list = await api("/api/reviews?limit=300"); } catch (e) { tb.innerHTML = `<tr><td colspan="4" class="fail">${esc(e.message)}</td></tr>`; return; }
  tb.innerHTML = list.map((r) => `<tr data-name="${esc(r.name)}"><td>${esc(stampT(r.stamp))}</td><td>${esc(r.kind)}${r.session ? " · session" : ""}</td>
    <td class="small">${esc(r.name)}</td><td>${esc(kb(r.size))}</td></tr>`).join("") || `<tr><td colspan="4" class="muted">No review yet.</td></tr>`;
}

async function showReview(name) {
  const box = $("#rv-detail");
  box.classList.remove("hidden");
  let r;
  try { r = await api(`/api/reviews/${encodeURIComponent(name)}`); } catch (e) { return showError(box, e); }
  const s = r.session;
  const facts = s ? ["status", "kind", "model", "effort", "started", "ended", "elapsed_s", "summary_level", "error", "diff_guard"]
    .filter((k) => s[k] != null).map((k) => `<div>${esc(k)}</div><div>${esc(txt(s[k], 400))}</div>`).join("") : "";
  box.innerHTML = `<h3>${esc(r.name)}</h3>
    ${facts ? `<div class="kv small">${facts}</div>` : ""}
    ${s?.summary ? `<h2>Summary</h2><p class="prewrap">${esc(s.summary)}</p>` : ""}
    ${r.truncated ? `<p class="small fail">Only the first ${esc(Math.round((r.max_bytes || 0) / 1024))} KB are shown.</p>` : ""}
    <pre class="review">${esc(r.text)}</pre>`;
}

/* ON only — OFF stays scripts\kill_switch_off.bat (deliberate friction). What is shown is the POST's own answer: the
   badge follows the file within ~1 s, but it is not the confirmation. */
async function killSwitchNow() {
  let info = null;
  try { info = await api("/api/kill_switch"); } catch (e) { /* the POST reports its own error */ }
  const scope = !info ? "this system" : info.global ? "EVERY system (all-pairs mode: the GLOBAL switch data/KILL_SWITCH)" : `the ${info.scope} system only`;
  if (!confirm(`Turn the kill switch ON for ${scope}?\n\nNo new orders from now on; open trades keep their SL/TP and protective actions.\nThere is no OFF button: OFF is scripts\\kill_switch_off.bat.`)) return;
  const reason = prompt("Reason (optional, stored in the switch file):", "");
  if (reason === null) return;
  try {
    const r = await api("/api/kill_switch", { method: "POST", headers: { "X-Dashboard-Token": TOKEN, "Content-Type": "application/json" }, body: JSON.stringify({ reason }) });
    alert(`${r.created ? "KILL SWITCH ON" : "The kill switch was already ON"} (${r.global ? "GLOBAL" : r.scope})\n${r.file}\n\n${r.note}`);
  } catch (e) { alert(`Kill switch NOT confirmed: ${e.message}`); }
}

/* ------------------------------------------------------------------ boot */
(async function boot() {
  // one delegated listener each: survives re-renders (no per-render re-binding)
  ["#pair-cards", "#dec-detail"].forEach((sel) => $(sel).addEventListener("click", (e) => {
    const b = e.target.closest("button[data-exec]");
    if (b && !b.disabled) executeNow(b.dataset.exec);
  }));
  setInterval(updateCountdowns, 1000);
  $("#kill-on").addEventListener("click", killSwitchNow);
  $("#reviews tbody").addEventListener("click", (e) => {
    const tr = e.target.closest("tr[data-name]");
    if (tr) showReview(tr.dataset.name);
  });
  $("#op-refresh").addEventListener("click", loadOperator);
  $("#tu-refresh").addEventListener("click", loadTuning);
  $("#pr-refresh").addEventListener("click", loadProposals);
  $("#rv-refresh").addEventListener("click", loadReviews);
  PAIRS = await api("/api/pairs");
  $("#dec-pair").innerHTML += PAIRS.map((p) => `<option>${esc(p.pair)}</option>`).join("");
  $("#op-pair").innerHTML = PAIRS.map((p) => `<option>${esc(p.pair)}</option>`).join("");
  $("#op-pair").addEventListener("change", loadOperator);
  $("#dec-pair").addEventListener("change", loadDecisions);
  $("#dec-refresh").addEventListener("click", loadDecisions);
  initChartControls();
  STATUS = await api("/api/status");
  await refreshLatestDecisions();
  connectWS();
})();
