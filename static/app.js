// trAIn front end: plain JS + Chart.js. Colours come from CSS tokens so light/dark both work.
const $ = (sel) => document.querySelector(sel);
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const charts = new Map(); // canvas id/element -> Chart
let dashData = null;
let insightData = null;
let conversationId = null;
let sending = false;
let discoverTried = false;

// Colour follows the entity: each sport keeps its slot no matter which sports are present.
const SPORT_SLOT = { running: 1, cycling: 2, strength: 3, swimming: 4, walking: 5, other: 6 };
const sportColor = (s) => css(`--series-${SPORT_SLOT[s] || 6}`);
const withAlpha = (hex, a) => hex + Math.round(a * 255).toString(16).padStart(2, "0");

const fmtDate = (iso) => new Date(iso + "T00:00:00").toLocaleDateString(undefined, { month: "short", day: "numeric" });
const fmtNum = (v, d = 1) => (v == null ? "–" : Number(v).toLocaleString(undefined, { maximumFractionDigits: d }));
const rolling = (vals, n = 7) => vals.map((_, i) => {
  const w = vals.slice(Math.max(0, i - n + 1), i + 1).filter((v) => v != null);
  return w.length ? w.reduce((a, b) => a + b, 0) / w.length : null;
});

// ---------- glossary tooltips ----------
const TERMS = {
  CTL: "Chronic Training Load: your fitness. A 42-day weighted average of daily training load.",
  ATL: "Acute Training Load: your fatigue. A 7-day weighted average of daily training load.",
  TSB: "Training Stress Balance: your form. CTL minus ATL going into the day. Negative means you're carrying fatigue.",
  TRIMP: "Training Impulse: training load worked out from a session's duration and average heart rate.",
  HRV: "Heart Rate Variability: how much the time between heartbeats varies, in milliseconds. Compare it with your own baseline, not other people's.",
  TE: "Training Effect: Garmin's 0 to 5 score for how much a session improved your aerobic fitness.",
  "VO2 max": "The most oxygen your body can use during hard exercise, in ml/kg/min. A common marker of aerobic fitness.",
};
const TERM_RE = new RegExp(`\\b(${Object.keys(TERMS).join("|")})\\b`, "g");
// Takes text that's already escaped.
const withTerms = (html) => html.replace(TERM_RE, (t) => `<abbr class="term" data-term="${t}" tabindex="0">${t}</abbr>`);

// One floating element rather than CSS on each term, so scrolling containers can't clip it.
function showTip(text, rect) {
  const tip = $("#tip");
  tip.textContent = text;
  tip.hidden = false;
  const { width, height } = tip.getBoundingClientRect();
  const left = rect.left + rect.width / 2 - width / 2;
  tip.style.left = `${Math.max(8, Math.min(left, innerWidth - width - 8))}px`;
  tip.style.top = `${rect.top - height - 8 >= 8 ? rect.top - height - 8 : rect.bottom + 8}px`;
}
const hideTip = () => ($("#tip").hidden = true);

for (const [on, off] of [["mouseover", "mouseout"], ["focusin", "focusout"]]) {
  document.addEventListener(on, (e) => {
    const term = e.target.closest?.(".term");
    if (term) showTip(TERMS[term.dataset.term], term.getBoundingClientRect());
  });
  document.addEventListener(off, (e) => { if (e.target.closest?.(".term")) hideTip(); });
}
document.addEventListener("keydown", (e) => { if (e.key === "Escape") hideTip(); });
document.addEventListener("scroll", hideTip, true);

// Chart legends are drawn on canvas, so they need their own hover hook.
function legendTip(e, item) {
  const term = item.text.match(TERM_RE)?.[0];
  if (term) showTip(TERMS[term], { left: e.native.clientX, width: 0, top: e.native.clientY - 6, bottom: e.native.clientY + 12 });
}

async function api(path, opts = {}) {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.detail || res.statusText);
  return body;
}

// Reads a server-sent event stream, calling onEvent(name, data) for each event.
async function readSSE(res, onEvent) {
  if (!res.ok) throw new Error(res.statusText);
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) return;
    buf += decoder.decode(value, { stream: true });
    let idx;
    while ((idx = buf.indexOf("\n\n")) >= 0) {
      const raw = buf.slice(0, idx);
      buf = buf.slice(idx + 2);
      onEvent(/^event: (.*)$/m.exec(raw)?.[1], JSON.parse(/^data: (.*)$/m.exec(raw)?.[1] || "{}"));
    }
  }
}

// ---------- charts ----------
function baseOptions({ stacked = false, unit = "" } = {}) {
  const tick = css("--text-secondary");
  return {
    responsive: true, maintainAspectRatio: false, animation: false,
    interaction: { mode: "index", intersect: false },
    plugins: {
      legend: { position: "top", align: "end", onHover: legendTip, onLeave: hideTip, labels: { color: tick, boxWidth: 10, boxHeight: 10, usePointStyle: true, pointStyle: "rectRounded" } },
      tooltip: {
        backgroundColor: css("--surface-1"), titleColor: css("--text-primary"), bodyColor: css("--text-secondary"),
        borderColor: css("--border"), borderWidth: 1, padding: 10, boxPadding: 4, usePointStyle: true,
        callbacks: { label: (c) => ` ${c.dataset.label}: ${fmtNum(c.parsed.y)}${unit ? " " + unit : ""}` },
      },
    },
    scales: {
      x: { stacked, grid: { display: false }, border: { color: css("--border") }, ticks: { color: tick, maxRotation: 0, autoSkipPadding: 16 } },
      y: { stacked, grid: { color: css("--grid") }, border: { display: false }, ticks: { color: tick, maxTicksLimit: 5 } },
    },
  };
}

const line = (label, data, color, extra = {}) => ({
  type: "line", label, data, borderColor: color, backgroundColor: color, borderWidth: 2,
  pointRadius: 0, pointHoverRadius: 4, pointHoverBorderWidth: 2, pointHoverBorderColor: css("--surface-1"),
  tension: 0.25, spanGaps: true, ...extra,
});
const bar = (label, data, color, extra = {}) => ({
  type: "bar", label, data, backgroundColor: color, borderRadius: 4, borderSkipped: "start",
  borderColor: css("--surface-1"), borderWidth: { top: 2 }, maxBarThickness: 28, ...extra,
});

function draw(target, config) {
  const canvas = typeof target === "string" ? document.getElementById(target) : target;
  charts.get(canvas)?.destroy();
  const chart = new Chart(canvas, config);
  charts.set(canvas, chart);
  return chart;
}

function renderCharts(d) {
  const labels = d.daily.map((r) => fmtDate(r.date));
  const col = (k) => d.daily.map((r) => r[k]);

  const singleSeries = (opts) => { const o = baseOptions(opts); o.plugins.legend.display = false; return o; };

  draw("c-weekly", {
    data: { labels: d.weekly.weeks.map(fmtDate), datasets: d.weekly.sports.map((s) => bar(s.sport[0].toUpperCase() + s.sport.slice(1), s.hours, sportColor(s.sport))) },
    options: baseOptions({ stacked: true, unit: "h" }),
  });

  draw("c-load", {
    data: { labels, datasets: [
      line("Fitness (CTL)", col("ctl"), css("--series-1")),
      line("Fatigue (ATL)", col("atl"), css("--series-2")),
      bar("Form (TSB)", col("tsb"), withAlpha(css("--series-3"), 0.55), { borderWidth: 0, borderRadius: 2 }),
    ] },
    options: baseOptions(),
  });

  const dailyPlusAvg = (key, label, unit) => ({
    data: { labels, datasets: [
      line(`${label} (7-day avg)`, rolling(col(key)), css("--series-1")),
      line(`${label} (daily)`, col(key), withAlpha(css("--series-1"), 0.35), { borderWidth: 1.5, tension: 0 }),
    ] },
    options: baseOptions({ unit }),
  });
  // Cards for metrics the watch didn't record stay hidden, and come back if data appears.
  const drawIfData = (id, key, config) => {
    const canvas = document.getElementById(id);
    const hasData = col(key).some((v) => v != null);
    canvas.closest(".chart-card").hidden = !hasData;
    if (hasData) draw(canvas, config());
    else { charts.get(canvas)?.destroy(); charts.delete(canvas); }
  };
  drawIfData("c-rhr", "resting_hr", () => dailyPlusAvg("resting_hr", "Resting HR", "bpm"));
  drawIfData("c-hrv", "hrv_avg", () => dailyPlusAvg("hrv_avg", "HRV", "ms"));
  drawIfData("c-sleep", "sleep_h", () => ({ data: { labels, datasets: [bar("Sleep", col("sleep_h"), css("--series-1"), { maxBarThickness: 8 })] }, options: singleSeries({ unit: "h" }) }));
  drawIfData("c-stress", "stress_avg", () => ({ data: { labels, datasets: [line("Stress", col("stress_avg"), css("--series-1"))] }, options: singleSeries() }));
}

// Claude-authored chart specs: one panel per unit, sharing the time axis (never dual-axis).
function renderSpecChart(container, data, height = 140) {
  container.innerHTML = "";
  const byUnit = {};
  data.series.forEach((s, i) => (byUnit[s.unit] ||= []).push({ ...s, slot: i + 1 }));
  for (const [unit, series] of Object.entries(byUnit)) {
    const box = document.createElement("div");
    box.className = "panel-chart";
    box.style.position = "relative";
    box.style.height = `${height}px`;
    const canvas = document.createElement("canvas");
    box.appendChild(canvas);
    container.appendChild(box);
    const opts = baseOptions({ unit });
    // A lone panel is named by the chart title; split panels each need their own legend.
    if (series.length === 1 && Object.keys(byUnit).length === 1) opts.plugins.legend.display = false;
    const labels = data.dates.map(fmtDate);
    draw(canvas, {
      data: { labels, datasets: series.map((s) => {
        const color = css(`--series-${s.slot}`);
        const label = s.label + (unit ? ` (${unit})` : "");
        return data.type === "bar" ? bar(label, s.values, color, { maxBarThickness: 8 }) : line(label, s.values, color);
      }) },
      options: opts,
    });
  }
}

// ---------- dashboard ----------
function renderKpis(kpis) {
  $("#kpis").innerHTML = kpis.map((k) => {
    const delta = k.current != null && k.previous != null ? k.current - k.previous : null;
    const arrow = delta == null ? "" : delta > 0 ? "▲" : delta < 0 ? "▼" : "■";
    return `<div class="kpi">
      <div class="label">${withTerms(k.label)}</div>
      <div class="value">${fmtNum(k.current)}<small>${k.unit}</small></div>
      <div class="delta">${delta == null ? "no prior data" : `${arrow} ${fmtNum(Math.abs(delta))} vs prior 7 days`}</div>
    </div>`;
  }).join("");
}

function renderActivities(acts) {
  const rows = acts.map((a) => `<tr>
    <td>${new Date(a.start_time).toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" })}</td>
    <td><span class="pill"><span class="dot" style="background:${sportColor(a.sport)}"></span>${a.sport}</span></td>
    <td>${escapeHtml(a.name || "")}</td>
    <td class="num">${a.distance_m ? fmtNum(a.distance_m / 1000, 2) + " km" : "–"}</td>
    <td class="num">${fmtNum((a.duration_s || 0) / 60, 0)} min</td>
    <td class="num">${a.avg_hr ? fmtNum(a.avg_hr, 0) + " bpm" : "–"}</td>
    <td class="num">${a.training_effect == null ? "–" : Number(a.training_effect).toFixed(1)}</td>
  </tr>`).join("");
  $("#acts").innerHTML = `<thead><tr><th>Date</th><th>Sport</th><th>Name</th><th class="num">Distance</th>
    <th class="num">Time</th><th class="num">Avg HR</th><th class="num">${withTerms("Aerobic TE")}</th></tr></thead><tbody>${rows}</tbody>`;
}

async function loadDashboard() {
  dashData = await api("/api/dashboard");
  $("#empty").hidden = !dashData.empty;
  $("#dash").hidden = dashData.empty;
  if (dashData.empty) return;
  renderKpis(dashData.kpis);
  renderCharts(dashData);
  renderActivities(dashData.activities);
}

// ---------- insights ----------
const SEVERITY = {
  positive: { icon: "✓", label: "Positive", color: "--good" },
  info: { icon: "i", label: "Info", color: "--text-muted" },
  watch: { icon: "!", label: "Watch", color: "--warning" },
  concern: { icon: "!!", label: "Concern", color: "--serious" },
};

function renderInsights(data) {
  insightData = data;
  const grid = $("#insights");
  if (!data || !data.insights) {
    grid.innerHTML = `<div class="insights-empty">No insights yet. Click <b>Generate insights</b> to have Claude review your data. This uses your Claude plan.</div>`;
    $("#insights-headline").textContent = "";
    return;
  }
  $("#insights-headline").textContent = data.headline || "";
  if (data.created_at) $("#insights-meta").textContent = `Generated ${new Date(data.created_at + "Z").toLocaleString()} · ${data.period?.start} → ${data.period?.end}`;
  grid.innerHTML = "";
  data.insights.forEach((ins, i) => {
    const sev = SEVERITY[ins.severity] || SEVERITY.info;
    const card = document.createElement("article");
    card.className = "insight";
    card.innerHTML = `
      <span class="tag"><span class="sev" style="background:${css(sev.color)}">${sev.icon}</span>${sev.label} · ${ins.category}</span>
      <h3>${escapeHtml(ins.title)}</h3>
      <p>${withTerms(escapeHtml(ins.body))}</p>
      <div class="evidence">${withTerms(escapeHtml(ins.evidence))}</div>
      ${ins.chart_data ? `<div class="mini-wrap"></div>` : ""}
      <button class="btn ask" data-i="${i}">Ask about this</button>`;
    grid.appendChild(card);
    if (ins.chart_data) renderSpecChart(card.querySelector(".mini-wrap"), ins.chart_data, 130);
  });
}

// Claude can't say how far through it is, so this shows real steps and elapsed time, not a percentage.
function startProgress() {
  const started = Date.now();
  const tick = () => {
    const s = Math.floor((Date.now() - started) / 1000);
    $("#progress-time").textContent = `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
  };
  tick();
  const timer = setInterval(tick, 1000);
  const grid = $("#insights");
  grid.classList.add("loading");
  if (!grid.querySelector(".insight")) {
    grid.innerHTML = `<div class="skeleton-card"><i></i><i></i><i></i></div>`.repeat(3);
  }
  $("#progress-done").innerHTML = "";
  $("#progress-step").textContent = "";
  $("#insights-progress").hidden = false;
  return {
    step(text) {
      const current = $("#progress-step");
      if (text === current.textContent) return;
      if (current.textContent) {
        const done = document.createElement("li");
        done.textContent = current.textContent.replace(/…$/, "");
        $("#progress-done").prepend(done);
        $("#progress-done").querySelector("li:nth-child(5)")?.remove();
      }
      current.textContent = text;
    },
    stop() {
      clearInterval(timer);
      grid.classList.remove("loading");
      $("#insights-progress").hidden = true;
    },
  };
}

async function generateInsights() {
  const btn = $("#btn-insights");
  btn.disabled = true;
  btn.textContent = "Generating…";
  const progress = startProgress();
  try {
    let report = null, failure = null;
    await readSSE(await fetch("/api/insights", { method: "POST" }), (event, data) => {
      if (event === "status") progress.step(data.text);
      else if (event === "report") report = data;
      else if (event === "error") failure = data.message;
    });
    if (!report) throw new Error(failure || "Claude returned no report");
    renderInsights(report);
    $("#insights-meta").textContent = "Generated just now";
  } catch (e) {
    renderInsights(insightData);
    showBanner(`Insights failed: ${escapeHtml(e.message)}`);
  } finally {
    progress.stop();
    btn.disabled = false;
    btn.textContent = insightData?.insights ? "Regenerate insights" : "Generate insights";
    loadStatus();
  }
}

// ---------- chat ----------
function openDrawer(open = true) {
  $("#drawer").classList.toggle("open", open);
  $("#drawer").setAttribute("aria-hidden", String(!open));
  if (open) $("#input").focus();
}

function newChat() {
  conversationId = null;
  $("#messages").innerHTML = "";
}

function renderMarkdown(el, text) {
  el.innerHTML = DOMPurify.sanitize(marked.parse(text));
}

async function sendMessage(text) {
  text = text.trim();
  if (!text || sending) return;
  sending = true;
  const messages = $("#messages");
  messages.querySelector(".hint")?.remove();
  messages.insertAdjacentHTML("beforeend", `<div class="msg user">${escapeHtml(text)}</div>`);
  const reply = document.createElement("div");
  reply.className = "msg assistant";
  messages.appendChild(reply);
  const status = document.createElement("div");
  status.className = "status";
  status.textContent = "Thinking…";
  reply.appendChild(status);

  let seg = null, segText = "", pending = false;
  const scroll = () => (messages.scrollTop = messages.scrollHeight);
  const flush = () => { pending = false; if (seg) renderMarkdown(seg, segText); scroll(); };
  const appendText = (delta) => {
    status.remove();
    if (!seg) { seg = document.createElement("div"); reply.appendChild(seg); segText = ""; }
    segText += delta;
    if (!pending) { pending = true; requestAnimationFrame(flush); }
  };
  const breakSegment = () => { if (seg) flush(); seg = null; };

  $("#btn-send").disabled = true;
  try {
    const res = await fetch("/api/chat", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: text, conversation_id: conversationId }),
    });
    await readSSE(res, (event, data) => {
      if (event === "conversation") conversationId = data.id;
      else if (event === "text") appendText(data.delta);
      else if (event === "status") {
        breakSegment();
        status.textContent = data.text;
        reply.appendChild(status);
        scroll();
      } else if (event === "chart") {
        breakSegment();
        const box = document.createElement("div");
        box.className = "chat-chart";
        box.innerHTML = `<h4>${escapeHtml(data.title || "")}</h4><div class="charts"></div>`;
        reply.appendChild(box);
        renderSpecChart(box.querySelector(".charts"), data, 170);
        scroll();
      } else if (event === "error") {
        breakSegment();
        status.remove();
        reply.insertAdjacentHTML("beforeend", `<p class="error">${escapeHtml(data.message || "Something went wrong")}</p>`);
      }
    });
  } catch (e) {
    status.remove();
    reply.insertAdjacentHTML("beforeend", `<p class="error">${escapeHtml(e.message)}</p>`);
  } finally {
    breakSegment();
    if (status.textContent.endsWith("…") && status.isConnected && reply.lastChild === status) status.remove();
    sending = false;
    $("#btn-send").disabled = false;
    loadStatus();
  }
}

// ---------- status / setup ----------
let bannerTimer = null;
function showBanner(html, { autoHide = false } = {}) {
  clearTimeout(bannerTimer);
  $("#banner").innerHTML = html;
  $("#banner").hidden = false;
  // Success notes clear themselves; problems stay until something replaces them.
  if (autoHide) bannerTimer = setTimeout(() => ($("#banner").hidden = true), 8000);
}

async function loadStatus() {
  const s = await api("/api/status");
  const rows = [
    { ok: s.claude.ok, text: s.claude.ok ? "Claude connected" : "Claude logged out" },
    { ok: s.usage.runs < s.usage.limit, text: `${s.usage.runs}/${s.usage.limit} Claude runs today · ${fmtTokens(s.usage.tokens_in + s.usage.tokens_out)} tokens` },
    { ok: s.data.activities > 0, text: `${s.data.activities} activities · ${s.data.days} days${s.data.demo ? " (demo data)" : ""}` },
    { ok: s.strava.tools > 0, text: s.strava.tools > 0 ? "Strava connector" : "Strava connector not found" },
  ];
  const dot = (ok) => css(ok ? "--good" : "--warning");
  $("#status-list").innerHTML = rows.map((r) => `<li><span class="dot" style="background:${dot(r.ok)}"></span>${r.text}</li>`).join("");
  // The header stays quiet unless something needs attention.
  const issues = rows.filter((r) => !r.ok);
  $("#status-dot").style.background = dot(!issues.length);
  $("#status-label").textContent = issues.length > 1 ? `${issues.length} issues` : issues[0]?.text || (s.data.demo ? "Demo data" : "Status");
  if (!s.claude.ok) showBanner(`<b>Claude:</b> ${escapeHtml(s.claude.message)} Charts still work; insights and chat need it.`);
  if (s.claude.ok && !s.strava.discovered && !discoverTried) {
    discoverTried = true; // once per page load, even if it fails
    api("/api/claude/discover", { method: "POST" }).then(loadStatus).catch(() => {});
  }
}

const fmtTokens = (n) => (n >= 1000 ? `${fmtNum(n / 1000, 1)}k` : String(n));

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// ---------- wiring ----------
$("#btn-insights").addEventListener("click", generateInsights);
$("#status-pop").addEventListener("beforetoggle", (e) => {
  if (e.newState !== "open") return;
  const r = $("#btn-status").getBoundingClientRect();
  e.target.style.top = `${r.bottom + 6}px`;
  e.target.style.right = `${document.documentElement.clientWidth - r.right}px`;
});
$("#btn-chat").addEventListener("click", () => openDrawer(true));
$("#btn-close").addEventListener("click", () => openDrawer(false));
$("#btn-newchat").addEventListener("click", newChat);
$("#composer").addEventListener("submit", (e) => {
  e.preventDefault();
  if (sending) return;
  const text = $("#input").value;
  $("#input").value = "";
  sendMessage(text);
});
$("#input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("#composer").requestSubmit(); }
});
$("#insights").addEventListener("click", (e) => {
  const btn = e.target.closest(".ask");
  if (!btn || sending) return;
  const ins = insightData.insights[btn.dataset.i];
  newChat();
  openDrawer(true);
  sendMessage(`Tell me more about this insight: "${ins.title}" - ${ins.body}\nEvidence: ${ins.evidence}\n\nWhat's driving it, and what should I do about it?`);
});
$("#btn-sync").addEventListener("click", async (e) => {
  e.target.disabled = true;
  e.target.textContent = "Syncing…";
  try {
    const r = await api("/api/garmin/sync", { method: "POST" });
    showBanner(`Garmin sync done: ${r.activities} activities, ${r.days} days since ${r.from}.`, { autoHide: true });
    await Promise.all([loadDashboard(), loadStatus()]);
  } catch (err) {
    showBanner(`<b>Garmin sync:</b> ${escapeHtml(err.message)}`);
  } finally {
    e.target.disabled = false;
    e.target.textContent = "Sync Garmin";
  }
});
$("#btn-import").addEventListener("click", () => $("#import-dialog").showModal());
$("#import-dialog").addEventListener("close", async () => {
  if ($("#import-dialog").returnValue !== "ok") return;
  try {
    const r = await api("/api/garmin/import", { method: "POST", body: JSON.stringify({ path: $("#import-path").value }) });
    showBanner(`Imported ${r.activities + r.fit} activities, ${r.wellness_days} wellness days, ${r.sleep_days} sleep nights.`, { autoHide: true });
    await Promise.all([loadDashboard(), loadStatus()]);
  } catch (err) {
    showBanner(`<b>Import:</b> ${escapeHtml(err.message)}`);
  }
});
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
  if (dashData && !dashData.empty) renderCharts(dashData);
  if (insightData) renderInsights(insightData);
});

Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;
loadStatus();
loadDashboard();
api("/api/insights").then((d) => renderInsights(d.insights ? d : null));
