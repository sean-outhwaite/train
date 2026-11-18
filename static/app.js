// trAIn front end: plain JS + Chart.js. Colours come from CSS tokens so light/dark both work.
const $ = (sel) => document.querySelector(sel);
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const charts = new Map(); // canvas id/element -> Chart
let dashData = null;
let insightData = null;
let conversationId = null;
let sending = false;
let discoverTried = false;
let activityData = null;
let routeMap = null;

// Colour follows the entity: each sport keeps its slot no matter which sports are present.
const SPORT_SLOT = { running: 1, cycling: 2, strength: 3, swimming: 4, walking: 5, other: 6 };
const sportColor = (s) => css(`--series-${SPORT_SLOT[s] || 6}`);
const withAlpha = (hex, a) => hex + Math.round(a * 255).toString(16).padStart(2, "0");
// Optional numeric design tokens, with the default design's value as the fallback.
const cssNum = (name, fallback) => { const v = parseFloat(css(name)); return Number.isNaN(v) ? fallback : v; };

// YYYY-MM-DD in local time (toISOString would shift it to UTC).
const localISO = (dt) => `${dt.getFullYear()}-${String(dt.getMonth() + 1).padStart(2, "0")}-${String(dt.getDate()).padStart(2, "0")}`;
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
  TE: "Training Effect: Garmin's 0 to 5 score for how much a session improved your aerobic or anaerobic fitness.",
  SpO2: "Blood oxygen saturation, measured at the wrist while you sleep. Wrist readings are noisy, so look at the trend rather than single nights.",
  "VO2 max": "The most oxygen your body can use during hard exercise, in ml/kg/min. A common marker of aerobic fitness.",
};
const TERM_RE = new RegExp(`\\b(${Object.keys(TERMS).join("|")})\\b`, "g");
// Takes text that's already escaped.
const withTerms = (html) => html.replace(TERM_RE, (t) => `<abbr class="term" data-term="${t}" tabindex="0">${t}</abbr>`);

// One floating element rather than CSS on each term, so scrolling containers can't clip it.
function showTip(text, rect) {
  const tip = $("#tip");
  ($("dialog[open]") || document.body).append(tip); // a modal dialog covers everything outside it
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
  type: "line", label, data, borderColor: color, backgroundColor: color, borderWidth: cssNum("--chart-line", 2),
  pointRadius: 0, pointHoverRadius: 4, pointHoverBorderWidth: 2, pointHoverBorderColor: css("--surface-1"),
  tension: 0.25, spanGaps: true, ...extra,
});
const bar = (label, data, color, extra = {}) => ({
  type: "bar", label, data, backgroundColor: color, borderRadius: cssNum("--chart-radius", 4), borderSkipped: "start",
  borderColor: css("--surface-1"), borderWidth: { top: 2 }, maxBarThickness: 28, ...extra,
});

function draw(target, config) {
  applyChartFont();
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
  // Stages when the watch recorded them, total hours otherwise.
  const hasStages = col("sleep_deep_h").some((v) => v != null);
  $("#sleep-note").textContent = hasStages ? "hours per night, by stage" : "hours per night";
  drawIfData("c-sleep", "sleep_h", () => hasStages ? {
    data: { labels, datasets: [
      ["Deep", "sleep_deep_h", "--series-5"], ["Light", "sleep_light_h", "--series-1"],
      ["REM", "sleep_rem_h", "--series-8"], ["Awake", "sleep_awake_h", "--series-6"],
    ].map(([label, key, color]) => bar(label, col(key), css(color), { maxBarThickness: 8, borderWidth: 0, borderRadius: 0 }))
      // Nights the watch timed but didn't split into stages.
      .concat(d.daily.some((r) => r.sleep_h != null && r.sleep_deep_h == null)
        ? [bar("Not staged", d.daily.map((r) => (r.sleep_deep_h == null ? r.sleep_h : null)), withAlpha(css("--text-muted"), 0.5), { maxBarThickness: 8, borderWidth: 0, borderRadius: 0 })]
        : []) },
    options: baseOptions({ stacked: true, unit: "h" }),
  } : { data: { labels, datasets: [bar("Sleep", col("sleep_h"), css("--series-1"), { maxBarThickness: 8 })] }, options: singleSeries({ unit: "h" }) });
  drawIfData("c-resp", "respiration_avg", () => dailyPlusAvg("respiration_avg", "Breathing", "brpm"));
  drawIfData("c-spo2", "spo2_avg", () => dailyPlusAvg("spo2_avg", "SpO2", "%"));
  drawIntensity(d.daily);
  drawIfData("c-stress", "stress_avg", () => ({ data: { labels, datasets: [line("Stress", col("stress_avg"), css("--series-1"))] }, options: singleSeries() }));

  drawReadiness(d, labels, col("training_readiness"));
  drawIfData("c-garmin-load", "acute_load", () => ({ data: { labels, datasets: [
    line("Acute (7-day)", col("acute_load"), css("--series-2")),
    line("Chronic (28-day)", col("chronic_load"), css("--series-1")),
  ] }, options: baseOptions() }));
  drawIfData("c-endurance", "endurance_score", () => ({ data: { labels, datasets: [line("Endurance score", col("endurance_score"), css("--series-1"))] }, options: singleSeries() }));
  drawIfData("c-hill", "hill_score", () => ({ data: { labels, datasets: [line("Hill score", col("hill_score"), css("--series-1"))] }, options: singleSeries() }));
  renderRacePredictions(d.daily);
  renderBenchmarks(d.benchmarks);
  renderGear(d.gear);
  renderYear(d.year);
}

// Calendar year to date. Tiles compare with the same dates last year; zero totals are hidden as not recorded.
function renderYear(y) {
  $("#year-card").hidden = !y;
  if (!y) return;
  $("#year-title").textContent = `This year · ${y.year}`;
  $("#year-note").textContent = `1 January to ${fmtLongDate(y.through)}` + (y.previous ? ", against the same dates last year" : "");
  const tiles = [
    ["hours", "Training time", "h", 1], ["distance_km", "Distance", "km", 0], ["elev_m", "Elevation gain", "m", 0],
    ["activities", "Activities", "", 0], ["active_days", "Active days", "", 0],
  ].filter(([key]) => y.totals[key]).map(([key, label, unit, digits]) => {
    const prev = y.previous?.[key];
    let delta = "";  // the note already says when there's nothing to compare with
    if (prev) {
      const pct = Math.round((y.totals[key] / prev - 1) * 100);
      delta = pct === 0 ? "■ same as last year" : `${pct > 0 ? "▲" : "▼"} ${Math.abs(pct)}% vs last year`;
    }
    return `<div class="kpi"><div class="label">${label}</div>
      <div class="value">${fmtNum(y.totals[key], digits)}${unit ? `<small>${unit}</small>` : ""}</div>
      <div class="delta" title="${prev ? `${fmtNum(prev, digits)} ${unit} by ${fmtLongDate(y.previous_through)}` : ""}">${delta}</div></div>`;
  }).join("");
  $("#year-tiles").innerHTML = tiles;

  // Columns appear only when some sport recorded them.
  const cols = [
    ["Activities", (s) => s.activities && fmtNum(s.activities, 0)],
    ["Time", (s) => s.hours && `${fmtNum(s.hours, 1)} h`],
    ["Distance", (s) => s.distance_km && `${fmtNum(s.distance_km, 0)} km`],
    ["Elevation", (s) => s.elev_m && `${fmtNum(s.elev_m, 0)} m`],
    ["Longest", (s) => s.longest_km && `${fmtNum(s.longest_km, 1)} km`],
  ].filter(([, get]) => y.sports.some(get));
  $("#year-sports").innerHTML = `<thead><tr><th>Sport</th>${cols.map(([h]) => `<th class="num">${h}</th>`).join("")}</tr></thead>
    <tbody>${y.sports.map((s) => `<tr><td><span class="pill"><span class="dot" style="background:${sportColor(s.sport)}"></span>${escapeHtml(titleCase(s.sport))}</span></td>
      ${cols.map(([, get]) => `<td class="num">${get(s) || "–"}</td>`).join("")}</tr>`).join("")}</tbody>`;

  const c = y.cumulative;
  const datasets = [line(String(y.year), c.hours, css("--series-1"))];
  if (c.previous_hours) datasets.push(line(String(y.year - 1), c.previous_hours, withAlpha(css("--text-muted"), 0.7), { borderDash: [5, 4] }));
  const opts = baseOptions({ unit: "h" });
  if (datasets.length === 1) opts.plugins.legend.display = false;
  $("#year-chart-note").textContent = c.previous_hours ? "hours, this year against last" : "hours";
  draw("c-year", { data: { labels: c.dates.map(fmtDate), datasets }, options: opts });
}

const fmtLongDate = (iso) => new Date(iso + "T00:00:00").toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });

// Threshold tiles compare the latest value with the earliest in the last year. Pace is shown per km.
function renderBenchmarks(b) {
  const tiles = (b?.thresholds || []).map((t) => {
    const isPace = t.metric === "lt_speed";
    const [value, unit] = isPace ? speedParts(t.latest, "running") : [fmtNum(t.latest, 0), t.unit];
    let delta = "no earlier value";
    if (t.first_date !== t.latest_date) {
      if (isPace) {
        const secs = 1000 / t.latest - 1000 / t.first;
        delta = Math.abs(secs) < 1 ? "■ no change" : `${secs < 0 ? "▲" : "▼"} ${fmtDuration(Math.abs(secs))} /km ${secs < 0 ? "faster" : "slower"}`;
      } else {
        const diff = t.latest - t.first;
        delta = Math.abs(diff) < 0.5 ? "■ no change" : `${diff > 0 ? "▲" : "▼"} ${fmtNum(Math.abs(diff), 0)} ${t.unit}`;
      }
      delta += ` since ${fmtLongDate(t.first_date)}`;
    }
    return `<div class="kpi"><div class="label">${t.label}</div><div class="value">${value}<small>${unit}</small></div>
      <div class="delta">${delta}</div></div>`;
  }).join("");
  $("#bench-tiles").hidden = !tiles;
  $("#bench-tiles").innerHTML = tiles;

  $("#bench-card").hidden = !tiles;
  renderRecords(b?.records || []);
}

// Personal records as cards grouped by sport. Timed records at a fixed distance also show the pace.
const RECORD_GROUPS = ["running", "cycling", "swimming", "steps"];
const RECORD_NEW_DAYS = 30;
const recordParts = (r) => ({
  time: () => [fmtDuration(r.value), ""], distance: () => [fmtNum(r.value / 1000, 2), "km"],
  elevation: () => [fmtNum(r.value, 0), "m"], power: () => [fmtNum(r.value, 0), "W"],
  count: () => [fmtNum(r.value, 0), ""], days: () => [fmtNum(r.value, 0), "days"],
}[r.kind] || (() => [fmtNum(r.value), ""]))();

function daysAgo(iso) {
  const today = new Date(localISO(new Date()) + "T00:00:00");
  return Math.round((today - new Date(iso + "T00:00:00")) / 86_400_000);
}
function fmtAgo(days) {
  const rtf = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });
  if (days < 45) return rtf.format(-days, "day");
  if (days < 365) return rtf.format(-Math.round(days / 30.44), "month");
  return rtf.format(-Math.round(days / 365.25), "year");
}

function renderRecords(records) {
  $("#records-card").hidden = !records.length;
  const sports = [...new Set(records.map((r) => r.sport))]
    .sort((x, y) => (RECORD_GROUPS.indexOf(x) + 1 || 99) - (RECORD_GROUPS.indexOf(y) + 1 || 99));
  $("#records").innerHTML = sports.map((sport) => {
    const color = sportColor(sport === "steps" ? "walking" : sport);
    const cards = records.filter((r) => r.sport === sport).map((r) => {
      const [value, unit] = recordParts(r);
      const age = r.date != null ? daysAgo(r.date) : null;
      const pace = r.kind === "time" && r.distance_m ? fmtSpeed(r.distance_m / r.value, r.sport) : null;
      const when = age == null ? null : `<time datetime="${r.date}" title="${fmtLongDate(r.date)}">${fmtAgo(age)}</time>`;
      return `<article class="record-card">
        <div class="record-top"><h4>${escapeHtml(r.label)}</h4>${age != null && age <= RECORD_NEW_DAYS ? '<span class="badge-new">New</span>' : ""}</div>
        <div class="record-value">${value}${unit ? `<small>${unit}</small>` : ""}</div>
        <div class="record-meta">${[pace, when].filter(Boolean).join(" · ")}</div>
        ${r.source_id ? `<button class="btn link" data-id="${escapeHtml(r.source_id)}">${escapeHtml(r.activity_name || "View activity")}<span class="arrow" aria-hidden="true">→</span></button>` : ""}
      </article>`;
    }).join("");
    return `<section class="record-group" style="--sport: ${color}">
      <h3 class="record-group-head"><span>${titleCase(sport)}</span></h3>
      <div class="record-grid">${cards}</div>
    </section>`;
  }).join("");
}

// Active gear as a slim row of chips above the activity log: distance, plus a small bar against the
// retirement distance when one is set in Garmin. The rest of the detail is in the tooltip.
function renderGear(g) {
  const items = g?.active || [];
  $("#gear-card").hidden = !items.length;
  $("#gear-list").innerHTML = items.map((i) => {
    const km = fmtNum((i.distance_m || 0) / 1000, 0);
    const of = i.max_m ? ` / ${fmtNum(i.max_m / 1000, 0)}` : "";
    // The flag states how worn it is, not what to do: there's nothing to act on in the app.
    const over = i.max_m && i.distance_m > i.max_m ? (i.distance_m - i.max_m) / 1000 : 0;
    const flag = !i.warn ? "" : over ? `${fmtNum(over, 0)} km over` : `${Math.round(i.worn * 100)}% worn`;
    const flagNote = !i.warn ? "" : over ? "past retirement distance" : "close to retirement distance";
    const detail = [i.type, i.activities && `${i.activities} activit${i.activities === 1 ? "y" : "ies"}`, i.date_begin && `since ${fmtLongDate(i.date_begin)}`, flagNote]
      .filter(Boolean).join(" · ");
    return `<li class="gear-item${i.warn ? " warn" : ""}" title="${escapeHtml(detail)}">
      <span class="name">${escapeHtml(i.name || i.type || "Gear")}</span>
      <span class="km">${km}${of} km</span>
      ${i.max_m ? `<span class="track" aria-hidden="true"><span class="fill" style="width: ${Math.min(100, (i.worn || 0) * 100)}%"></span></span>` : ""}
      ${flag ? `<span class="flag">${flag}</span>` : ""}
    </li>`;
  }).join("");
}

// Weekly intensity minutes (vigorous counted double) against the common 150-minute guideline.
function drawIntensity(daily) {
  const canvas = $("#c-intensity");
  const weeks = [];
  for (const r of daily) {
    const day = new Date(r.date + "T00:00:00");
    const monday = new Date(day); monday.setDate(day.getDate() - ((day.getDay() + 6) % 7));
    const key = localISO(monday);
    if (!weeks.length || weeks.at(-1).key !== key) weeks.push({ key, label: fmtDate(key), total: null });
    if (r.intensity_min != null) weeks.at(-1).total = (weeks.at(-1).total || 0) + r.intensity_min;
  }
  const card = canvas.closest(".chart-card");
  card.hidden = !weeks.some((w) => w.total != null);
  if (card.hidden) { charts.get(canvas)?.destroy(); charts.delete(canvas); return; }
  draw(canvas, {
    data: { labels: weeks.map((w) => w.label), datasets: [
      bar("Intensity minutes", weeks.map((w) => w.total), css("--series-3")),
      line("150-minute guideline", weeks.map(() => 150), css("--text-muted"), { borderDash: [5, 4], borderWidth: 1.5, pointHoverRadius: 0 }),
    ] },
    options: baseOptions({ unit: "min" }),
  });
}

// Garmin's readiness levels, as background bands on the trend. Colour carries the level everywhere.
const READINESS_BANDS = [
  { from: 0, to: 25, label: "Poor", color: "--critical" },
  { from: 25, to: 50, label: "Low", color: "--serious" },
  { from: 50, to: 75, label: "Moderate", color: "--warning" },
  { from: 75, to: 100, label: "High", color: "--good" },
];
const LEVEL_COLOR = { POOR: "--critical", LOW: "--serious", MODERATE: "--warning", HIGH: "--good", PRIME: "--good" };
const FEEDBACK_COLOR = { POOR: "--serious", MODERATE: "--warning", GOOD: "--good", VERY_GOOD: "--good" };

const bandsPlugin = {
  id: "bands",
  beforeDatasetsDraw(chart) {
    const { ctx, chartArea: area, scales: { y } } = chart;
    ctx.save();
    ctx.font = `500 10.5px ${css("--font")}`;
    ctx.textAlign = "right";
    for (const b of READINESS_BANDS) {
      const top = y.getPixelForValue(b.to), bottom = y.getPixelForValue(b.from);
      ctx.fillStyle = withAlpha(css(b.color), 0.1);
      ctx.fillRect(area.left, top, area.right - area.left, bottom - top);
      ctx.fillStyle = withAlpha(css(b.color), 0.9);
      ctx.fillText(b.label, area.right - 6, top + 13);
    }
    ctx.restore();
  },
};

function drawReadiness(d, labels, values) {
  const card = $("#readiness-card");
  const canvas = $("#c-readiness");
  card.hidden = !values.some((v) => v != null);
  if (card.hidden) { charts.get(canvas)?.destroy(); charts.delete(canvas); return; }

  const status = [...d.daily].reverse().find((r) => r.training_status)?.training_status;
  $("#readiness-note").textContent = "Garmin morning score, 0–100" + (status ? ` · training status: ${titleCase(status)}` : "");
  renderReadinessNow(d.readiness);

  const options = baseOptions();
  options.scales.y.min = 0;
  options.scales.y.max = 100;
  options.scales.y.grid.display = false;
  draw(canvas, {
    data: { labels, datasets: [
      line("7-day average", rolling(values), css("--text-primary")),
      line("Daily", values, withAlpha(css("--text-secondary"), 0.5), { borderWidth: 1, tension: 0, pointRadius: 2, pointBackgroundColor: withAlpha(css("--text-secondary"), 0.7) }),
    ] },
    options,
    plugins: [bandsPlugin],
  });
}

// This morning's score, and the factors behind it with the weakest one marked.
function renderReadinessNow(r) {
  const box = $("#readiness-now");
  if (!r) { box.innerHTML = ""; return; }
  const level = LEVEL_COLOR[r.level] || "--text-primary";
  const when = r.date === localISO(new Date()) ? "This morning" : `Morning of ${fmtDate(r.date)}`;
  const factors = r.factors || [];
  const weakest = factors.length ? factors.reduce((a, b) => (b.percent < a.percent ? b : a)) : null;
  box.innerHTML = `
    <div class="readiness-score" style="--level: var(${level})">
      <span class="muted">${when}</span>
      <span class="big">${fmtNum(r.score, 0)}<small>/100</small></span>
      <span class="level">${r.level ? titleCase(r.level) : ""}${r.feedback ? ` · ${escapeHtml(titleCase(r.feedback))}` : ""}</span>
      ${weakest && !["GOOD", "VERY_GOOD"].includes(weakest.feedback) ? `<span class="muted">Held back most by ${weakest.label.toLowerCase()}</span>` : ""}
      ${r.recovery_time_h ? `<span class="muted">Recovery time left: ${fmtNum(r.recovery_time_h, 0)} h</span>` : ""}
    </div>
    <ul class="factors" aria-label="What made up the score">${factors.map((f) => `
      <li class="factor${f === weakest && factors.length > 1 ? " weakest" : ""}" style="--level: var(${FEEDBACK_COLOR[f.feedback] || "--text-muted"})">
        <span class="name">${f.label}</span>
        <span class="track"><span class="fill" style="width: ${Math.max(2, f.percent || 0)}%"></span></span>
        <span class="word">${titleCase(f.feedback)}</span>
      </li>`).join("")}
    </ul>`;
}

// Latest Garmin prediction per distance, against the first one in the period. Lower is faster.
function renderRacePredictions(daily) {
  const races = [["race_5k_min", "5K"], ["race_10k_min", "10K"], ["race_half_min", "Half marathon"], ["race_marathon_min", "Marathon"]];
  const tiles = races.map(([key, label]) => {
    const rows = daily.filter((r) => r[key] != null);
    if (!rows.length) return "";
    const first = rows[0], last = rows.at(-1);
    const change = (last[key] - first[key]) * 60;
    const delta = Math.abs(change) < 1 ? `■ no change since ${fmtDate(first.date)}`
      : `${change < 0 ? "▼" : "▲"} ${fmtDuration(Math.abs(change))} ${change < 0 ? "faster" : "slower"} since ${fmtDate(first.date)}`;
    return `<div class="kpi"><div class="label">${label}</div><div class="value">${fmtDuration(last[key] * 60)}</div>
      <div class="delta">${rows.length > 1 ? delta : "first prediction"}</div></div>`;
  }).join("");
  $("#race-card").hidden = !tiles;
  $("#race-preds").innerHTML = tiles;
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

const RECENT_ACTIVITIES = 5;
let sportFilter = "";

function activityTable(acts) {
  const rows = acts.map((a) => `<tr data-id="${escapeHtml(a.source_id)}">
    <td>${new Date(a.start_time).toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" })}</td>
    <td><span class="pill"><span class="dot" style="background:${sportColor(a.sport)}"></span>${a.sport}</span></td>
    <td><button class="row-link">${escapeHtml(a.name || a.sport)}</button></td>
    <td class="num">${a.distance_m ? fmtNum(a.distance_m / 1000, 2) + " km" : "–"}</td>
    <td class="num">${fmtNum((a.duration_s || 0) / 60, 0)} min</td>
    <td class="num">${a.avg_hr ? fmtNum(a.avg_hr, 0) + " bpm" : "–"}</td>
    <td class="num">${a.training_effect == null ? "–" : Number(a.training_effect).toFixed(1)}</td>
  </tr>`).join("");
  return `<thead><tr><th>Date</th><th>Sport</th><th>Name</th><th class="num">Distance</th>
    <th class="num">Time</th><th class="num">Avg HR</th><th class="num">${withTerms("Aerobic TE")}</th></tr></thead><tbody>${rows}</tbody>`;
}

function renderActivities(acts) {
  $("#acts-recent").innerHTML = activityTable(acts.slice(0, RECENT_ACTIVITIES));
  const sports = [...new Set(acts.map((a) => a.sport))].sort();
  if (!sports.includes(sportFilter)) sportFilter = "";
  // A filter is only worth showing when there's more than one sport to pick between.
  $("#sport-filter").innerHTML = sports.length < 2 ? "" : ["", ...sports].map((sp) =>
    `<button class="chip" data-sport="${escapeHtml(sp)}" aria-pressed="${sp === sportFilter}">${sp
      ? `<span class="dot" style="background:${sportColor(sp)}"></span>${escapeHtml(titleCase(sp))}` : "All"}</button>`).join("");
  const shown = sportFilter ? acts.filter((a) => a.sport === sportFilter) : acts;
  $("#acts").innerHTML = activityTable(shown);
  $("#acts-note").textContent = `${shown.length} most recent${sportFilter ? ` ${sportFilter}` : ""} activit${shown.length === 1 ? "y" : "ies"}`;
}

// ---------- tabs ----------
const TABS = ["overview", "training", "recovery", "progress", "activities"];
const RENAMED_TABS = { performance: "progress" };  // old links keep working
function tabFromHash() {
  let t = location.hash.slice(1);
  if (RENAMED_TABS[t]) { t = RENAMED_TABS[t]; history.replaceState(null, "", `#${t}`); }
  return TABS.includes(t) ? t : "overview";
}

// A tab with nothing recorded is hidden, like an empty chart.
function updateTabs() {
  for (const name of TABS) {
    const hasContent = name === "overview" || $(`#tab-${name}`).querySelector(".panel:not([hidden])");
    $(`#tabbtn-${name}`).hidden = !hasContent;
  }
  showTab(tabFromHash());
}

let currentTab = null;

function showTab(name) {
  if ($(`#tabbtn-${name}`).hidden) name = "overview";
  for (const t of TABS) {
    const active = t === name;
    $(`#tab-${t}`).hidden = !active;
    const btn = $(`#tabbtn-${t}`);
    btn.setAttribute("aria-selected", active);
    btn.tabIndex = active ? 0 : -1;
  }
  // On narrow screens the tab bar scrolls sideways, so bring the current tab into view.
  const nav = $(".tabs"), btn = $(`#tabbtn-${name}`);
  const off = btn.getBoundingClientRect().left - nav.getBoundingClientRect().left;
  if (off < 0 || off + btn.offsetWidth > nav.clientWidth) nav.scrollLeft += off - (nav.clientWidth - btn.offsetWidth) / 2;
  moveRoute(name, currentTab != null && currentTab !== name);
  currentTab = name;
  // Charts drawn while their tab was hidden have no size yet.
  const panel = $(`#tab-${name}`);
  for (const chart of charts.values()) if (panel.contains(chart.canvas)) chart.resize();
}

// Pulls away slowly and brakes into the stop. Stops light up when the line reaches them, so their delay comes
// from this same curve: find the point on it where the line has covered `progress` of the trip.
const ROUTE_EASE = [0.5, 0, 0.25, 1];
function routeTimeAt(progress) {
  const [x1, y1, x2, y2] = ROUTE_EASE;
  const bez = (a, b, t) => 3 * (1 - t) ** 2 * t * a + 3 * (1 - t) * t ** 2 * b + t ** 3;
  let lo = 0, hi = 1;
  for (let i = 0; i < 20; i++) { const mid = (lo + hi) / 2; if (bez(y1, y2, mid) < progress) lo = mid; else hi = mid; }
  return bez(x1, x2, (lo + hi) / 2);
}

// The route line runs between the first and last visible stops. Its pink part (and the train) travel to the
// current stop, and each stop on the way changes colour as the line reaches it.
function moveRoute(name, animate) {
  const nav = $(".tabs"), route = $(".route");
  const visible = TABS.filter((t) => !$(`#tabbtn-${t}`).hidden);
  const origin = nav.getBoundingClientRect().left - nav.scrollLeft;
  const centre = (t) => { const r = $(`#tabbtn-${t} .stop`).getBoundingClientRect(); return r.left + r.width / 2 - origin; };
  const start = centre(visible[0]);
  const from = parseFloat(route.style.getPropertyValue("--fill")) || 0;
  const to = centre(name) - start;
  // Longer trips take longer, like a train: about a third of a second plus a little per stop.
  const ms = animate ? Math.min(1100, 320 + Math.abs(to - from) * 1.1) : 0;
  nav.style.setProperty("--ms", `${ms}ms`);
  nav.style.setProperty("--ease", `cubic-bezier(${ROUTE_EASE.join(", ")})`);
  route.style.left = `${start}px`;
  route.style.width = `${centre(visible.at(-1)) - start}px`;
  route.style.setProperty("--fill", `${to}px`);
  if (to !== from) route.classList.toggle("reverse", to < from);
  for (const t of visible) {
    const x = centre(t) - start;
    const reached = animate && to !== from ? Math.min(1, Math.max(0, (x - from) / (to - from))) : 0;
    const stop = $(`#tabbtn-${t} .stop`);
    stop.style.transitionDelay = `${Math.round(routeTimeAt(reached) * ms)}ms`;
    $(`#tabbtn-${t}`).classList.toggle("past", x < to);
  }
}
// Stops move when the window resizes, so put the line back without animating.
new ResizeObserver(() => { if (currentTab) moveRoute(currentTab, false); }).observe($(".tabs"));

// ---------- activity detail ----------
const fmtDuration = (seconds) => {
  const t = Math.round(seconds);
  const h = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60), s = String(t % 60).padStart(2, "0");
  return h ? `${h}:${String(m).padStart(2, "0")}:${s}` : `${m}:${s}`;
};
const hasPace = (sport) => ["running", "walking", "swimming"].includes(sport);
// Returns [value, unit].
const speedParts = (mps, sport) => {
  if (sport === "swimming") return [fmtDuration(100 / mps), "/100 m"];
  if (hasPace(sport)) return [fmtDuration(1000 / mps), "/km"];
  return [fmtNum(mps * 3.6), "km/h"];
};
const fmtSpeed = (mps, sport) => (mps ? speedParts(mps, sport).join(" ") : null);
const withUnit = (v, unit, d = 0) => (v ? `${fmtNum(v, d)} ${unit}`.trim() : null);
const titleCase = (s) => s.toLowerCase().replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());
const range = (lo, hi, unit) => (lo == null || hi == null ? null : `${fmtNum(lo, 0)}–${fmtNum(hi, 0)} ${unit}`);

function drawZones(card, canvas, zones, color) {
  $(card).hidden = !zones;
  if (!zones) return;
  const total = zones.reduce((a, b) => a + b, 0);
  const opts = baseOptions();
  opts.indexAxis = "y";
  opts.interaction.axis = "y";
  opts.plugins.legend.display = false;
  opts.plugins.tooltip.callbacks.label = (c) => ` ${fmtNum((c.parsed.x / total) * 100, 0)}% of zone time`;
  opts.scales = {
    x: { beginAtZero: true, grid: { display: false }, border: { display: false }, ticks: { display: false } },
    y: { grid: { display: false }, border: { color: css("--border") }, ticks: { color: css("--text-secondary"), autoSkip: false } },
  };
  draw(canvas, {
    type: "bar",
    data: { labels: zones.map((s, i) => `Z${i + 1}  ${fmtDuration(s)}`), datasets: [bar("Time", zones, color)] },
    options: opts,
  });
}

function drawRoute(a) {
  routeMap?.remove();
  routeMap = null;
  const points = a.track;
  $("#act-map-card").hidden = !a.has_route || points?.length === 0;
  $("#act-map").hidden = !points;
  $("#act-map-note").textContent = a.routeError ? "Couldn't load the route from Garmin" : points ? "" : "Loading…";
  if (!points?.length) return;

  const color = sportColor(a.sport);
  routeMap = L.map("act-map", { scrollWheelZoom: false });
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
  }).addTo(routeMap);
  const route = L.polyline(points, { color, weight: 3 }).addTo(routeMap);
  const marker = (point, label, fillColor) => L.circleMarker(point,
    { radius: 6, color: css("--surface-1"), weight: 2, fillColor, fillOpacity: 1 }).bindTooltip(label).addTo(routeMap);
  marker(points.at(-1), "Finish", css("--text-primary"));
  marker(points[0], "Start", color);
  routeMap.fitBounds(route.getBounds(), { padding: [16, 16] });
}

async function loadRoute(a) {
  try {
    a.track = await api(`/api/activities/${encodeURIComponent(a.source_id)}/track`);
  } catch {
    a.routeError = true;
  }
  if (a === activityData && $("#activity-dialog").open) drawRoute(a);
}

function renderActivity(a) {
  const speed = a.avg_speed || (a.distance_m && a.duration_s ? a.distance_m / a.duration_s : null);
  const when = new Date(a.start_time).toLocaleString(undefined, { dateStyle: "full", timeStyle: "short" });
  $("#act-title").textContent = a.name || titleCase(a.sport);
  $("#act-meta").textContent = [titleCase(a.sport), when, a.location].filter(Boolean).join(" · ");

  const tiles = [
    ["Distance", a.distance_m && fmtNum(a.distance_m / 1000, 2), "km"],
    ["Time", a.duration_s && fmtDuration(a.duration_s), ""],
    [hasPace(a.sport) ? "Avg pace" : "Avg speed", ...(speed ? speedParts(speed, a.sport) : [null, ""])],
    ["Avg HR", a.avg_hr && fmtNum(a.avg_hr, 0), "bpm"],
    ["Avg power", a.avg_power && fmtNum(a.avg_power, 0), "W"],
    ["Elevation gain", a.elev_gain_m && fmtNum(a.elev_gain_m, 0), "m"],
  ].filter(([, value]) => value);
  $("#act-tiles").innerHTML = tiles.map(([label, value, unit]) =>
    `<div class="kpi"><div class="label">${label}</div><div class="value">${value}<small>${unit}</small></div></div>`).join("");

  drawRoute(a);
  drawZones("#act-hr-card", "act-hr", a.hr_zones, sportColor(a.sport));
  drawZones("#act-power-card", "act-power", a.power_zones, sportColor(a.sport));

  const groups = [
    ["Effort", [
      ["Max HR", withUnit(a.max_hr, "bpm")],
      ["Aerobic TE", a.training_effect && Number(a.training_effect).toFixed(1)],
      ["Anaerobic TE", a.anaerobic_te && Number(a.anaerobic_te).toFixed(1)],
      ["Main benefit", a.te_label && a.te_label !== "UNKNOWN" && titleCase(a.te_label)],
      ["Training load", withUnit(a.training_load, "")],
      ["Calories", withUnit(a.calories, "kcal")],
      ["VO2 max", withUnit(a.vo2max, "")],
      ["Body Battery", a.body_battery_change && `${a.body_battery_change > 0 ? "+" : ""}${a.body_battery_change}`],
    ]],
    [hasPace(a.sport) ? "Pace" : "Speed", [
      ["Peak", fmtSpeed(a.max_speed, a.sport)],
      ["Grade adjusted", fmtSpeed(a.gap_speed, a.sport)],
      ...a.fastest.map((f) => [`Fastest ${f.label}`, fmtDuration(f.seconds)]),
      ["Moving time", a.moving_s && fmtDuration(a.moving_s)],
    ]],
    ["Power", [["Normalized", withUnit(a.norm_power, "W")], ["Max", withUnit(a.max_power, "W")]]],
    ["Form", [
      ["Avg cadence", withUnit(a.avg_cadence_spm, "spm") || withUnit(a.avg_cadence_rpm, "rpm")],
      ["Max cadence", withUnit(a.max_cadence_spm, "spm") || withUnit(a.max_cadence_rpm, "rpm")],
      ["Steps", withUnit(a.steps, "")],
      ["Stride length", withUnit(a.stride_cm, "cm")],
      ["Ground contact", withUnit(a.ground_contact_ms, "ms")],
      ["Vertical oscillation", withUnit(a.vertical_osc_cm, "cm", 1)],
      ["Vertical ratio", withUnit(a.vertical_ratio, "%", 1)],
    ]],
    ["Conditions", [
      ["Temperature", range(a.min_temp_c, a.max_temp_c, "°C")],
      ["Elevation range", range(a.min_elev_m, a.max_elev_m, "m")],
      ["Elevation loss", withUnit(a.elev_loss_m, "m")],
    ]],
  ];
  $("#act-groups").innerHTML = groups.map(([title, rows]) => {
    const shown = rows.filter(([, value]) => value);
    if (!shown.length) return "";
    return `<section><h3>${title}</h3><dl>${shown.map(([label, value]) =>
      `<div><dt>${withTerms(label)}</dt><dd>${escapeHtml(value)}</dd></div>`).join("")}</dl></section>`;
  }).join("");
}

async function openActivity(id) {
  try {
    activityData = await api(`/api/activities/${encodeURIComponent(id)}`);
  } catch (err) {
    showBanner(`Couldn't load that activity: ${escapeHtml(err.message)}`);
    return;
  }
  const dialog = $("#activity-dialog");
  dialog.showModal();
  dialog.scrollTop = 0;
  $("#act-laps-card").hidden = true;  // until this activity's laps arrive
  renderActivity(activityData);
  if (activityData.has_route) loadRoute(activityData);
  loadExtras(activityData);
}

async function loadExtras(a) {
  try {
    a.extras = await api(`/api/activities/${encodeURIComponent(a.source_id)}/extras`);
  } catch {
    return;
  }
  if (a === activityData && $("#activity-dialog").open) renderExtras(a);
}

// Weather joins the stat groups; laps get their own table, with only the columns that have data.
function renderExtras(a) {
  const w = a.extras.weather || {};
  const rows = [
    ["Conditions", w.conditions],
    ["Air temperature", w.temp_c != null && `${fmtNum(w.temp_c, 0)} °C${w.feels_like_c != null && Math.abs(w.feels_like_c - w.temp_c) >= 1 ? ` (feels ${fmtNum(w.feels_like_c, 0)} °C)` : ""}`],
    ["Humidity", withUnit(w.humidity_pct, "%")],
    ["Dew point", w.dew_point_c != null && `${fmtNum(w.dew_point_c, 0)} °C`],
    ["Wind", w.wind_kph != null && `${fmtNum(w.wind_kph, 0)} km/h${w.wind_dir ? ` ${w.wind_dir}` : ""}`],
  ].filter(([, v]) => v);
  $("#act-weather")?.remove();
  if (rows.length) {
    $("#act-groups").insertAdjacentHTML("beforeend", `<section id="act-weather"><h3>Weather</h3><dl>${rows.map(([label, value]) =>
      `<div><dt>${label}</dt><dd>${escapeHtml(value)}</dd></div>`).join("")}</dl></section>`);
  }

  const laps = a.extras.laps;
  $("#act-laps-card").hidden = !laps.length;
  if (!laps.length) return;
  const pace = hasPace(a.sport);
  const cols = [
    ["#", (l) => l.lap_index],
    ["Distance", (l) => l.distance_m != null && `${fmtNum(l.distance_m / 1000, 2)} km`],
    ["Time", (l) => l.duration_s != null && fmtDuration(l.duration_s)],
    [pace ? "Pace" : "Speed", (l) => fmtSpeed(l.avg_speed, a.sport)],
    ["Grade adj.", (l) => fmtSpeed(l.gap_speed, a.sport)],
    ["Avg HR", (l) => l.avg_hr != null && fmtNum(l.avg_hr, 0)],
    ["Max HR", (l) => l.max_hr != null && fmtNum(l.max_hr, 0)],
    ["Power", (l) => l.avg_power != null && `${fmtNum(l.avg_power, 0)} W`],
    ["Cadence", (l) => l.avg_cadence != null && fmtNum(l.avg_cadence, 0)],
    ["Climb", (l) => l.elev_gain_m != null && `${fmtNum(l.elev_gain_m, 0)} m`],
  ].filter(([, get]) => laps.some((l) => get(l)));
  const types = new Set(laps.map((l) => l.intensity).filter(Boolean));
  const showType = types.size > 1;  // only worth a column for structured workouts
  $("#act-laps-note").textContent = `${laps.length} laps`;
  $("#act-laps").innerHTML = `<thead><tr>${cols.map(([h], i) => `<th class="${i ? "num" : ""}">${h}</th>`).join("")}${showType ? "<th>Type</th>" : ""}</tr></thead>
    <tbody>${laps.map((l) => `<tr>${cols.map(([, get], i) => `<td class="${i ? "num" : ""}">${escapeHtml(String(get(l) || "–"))}</td>`).join("")}${showType ? `<td>${l.intensity ? titleCase(l.intensity) : ""}</td>` : ""}</tr>`).join("")}</tbody>`;
}

async function loadDashboard() {
  dashData = await api("/api/dashboard");
  $("#empty").hidden = !dashData.empty;
  $("#dash").hidden = dashData.empty;
  if (dashData.empty) return;
  renderKpis(dashData.kpis);
  renderCharts(dashData);
  renderActivities(dashData.activities);
  updateTabs();
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
      <button class="btn link ask" data-i="${i}">Ask about this<span class="arrow" aria-hidden="true">→</span></button>`;
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
  document.body.classList.toggle("chat-open", open);
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
    syncRow(s.data.synced_at),
    { ok: s.strava.tools > 0, text: s.strava.tools > 0 ? "Strava connector" : "Strava connector not found" },
  ];
  renderSettings(s.settings);
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

// Yellow once Garmin hasn't synced for a day, so stale data is easy to spot.
function syncRow(syncedAt) {
  if (!syncedAt) return { ok: false, text: "Garmin not synced" };
  const hours = (Date.now() - new Date(syncedAt)) / 36e5;
  const ago = hours < 1 ? "under an hour ago" : hours < 48 ? `${Math.floor(hours)}h ago` : `${Math.floor(hours / 24)} days ago`;
  return { ok: hours <= 24, text: `Garmin synced ${ago}` };
}

const fmtTokens = (n) => (n >= 1000 ? `${fmtNum(n / 1000, 1)}k` : String(n));

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// ---------- wiring ----------
$("#btn-insights").addEventListener("click", generateInsights);
for (const table of document.querySelectorAll("table.acts")) {
  table.addEventListener("click", (e) => {
    const row = e.target.closest("tr[data-id]");
    if (row) openActivity(row.dataset.id);
  });
}
$("#records").addEventListener("click", (e) => {
  const btn = e.target.closest("[data-id]");
  if (btn) openActivity(btn.dataset.id);
});
$("#sport-filter").addEventListener("click", (e) => {
  const chip = e.target.closest(".chip");
  if (!chip) return;
  sportFilter = chip.dataset.sport;
  renderActivities(dashData.activities);
});
window.addEventListener("hashchange", () => {
  showTab(tabFromHash());
  // Keep the tab bar in view when switching from far down a long tab.
  if (window.scrollY > $(".tabs").offsetTop) window.scrollTo({ top: 0 });
});
// The logo goes back to the overview. Already there, the hash doesn't change, so just scroll to the top.
$(".brand-link").addEventListener("click", () => {
  if (location.hash === "#overview") window.scrollTo({ top: 0 });
});
// Arrow keys move between tabs, as in other tab lists.
$(".tabs").addEventListener("keydown", (e) => {
  const step = { ArrowRight: 1, ArrowLeft: -1 }[e.key];
  if (!step) return;
  const visible = [...document.querySelectorAll(".tab:not([hidden])")];
  const next = visible[(visible.indexOf(document.activeElement) + step + visible.length) % visible.length];
  next.focus();
  next.click();
});
$("#activity-dialog").addEventListener("click", (e) => {
  if (e.target === e.currentTarget) e.currentTarget.close();
});
$("#status-pop").addEventListener("beforetoggle", (e) => {
  if (e.newState !== "open") return;
  if (settings) renderSettings(settings);  // a setting left mid-change goes back to its saved value
  const r = $("#btn-status").getBoundingClientRect();
  e.target.style.top = `${r.bottom + 6}px`;
  e.target.style.right = `${document.documentElement.clientWidth - r.right}px`;
});
// A set value shows as text with a Change button. The control shows only while unset or being changed.
let settings = null;
function showSetting(id, value, editing) {
  const box = $(id);
  box.classList.toggle("is-set", !!value);
  for (const el of box.querySelectorAll(".setting-value, .setting-change")) el.hidden = !value || editing;
  box.querySelector("select, input").hidden = !!value && !editing;
}
function renderSettings(s) {
  settings = s;
  const hr = $("#hr-zone-basis");
  hr.value = s.hr_zone_basis;
  $("#setting-hr .setting-value").textContent = hr.selectedOptions[0]?.dataset.short || "";
  showSetting("#setting-hr", s.hr_zone_basis, false);
  $("#training-plan").value = s.training_plan;
  $("#setting-plan .setting-value").href = s.training_plan || "#";
  showSetting("#setting-plan", s.training_plan, false);
}
async function saveSetting(body) {
  try {
    renderSettings(await api("/api/settings", { method: "POST", body: JSON.stringify(body) }));
  } catch (err) {
    showBanner(`<b>Settings:</b> ${escapeHtml(err.message)}`);
    loadStatus();
  }
}
for (const [box, key] of [["#setting-hr", "hr_zone_basis"], ["#setting-plan", "training_plan"]]) {
  $(`${box} .setting-change`).addEventListener("click", () => {
    showSetting(box, settings[key], true);
    $(box).querySelector("select, input").focus();
  });
}
$("#hr-zone-basis").addEventListener("change", (e) => saveSetting({ hr_zone_basis: e.target.value }));
$("#training-plan").addEventListener("change", (e) => saveSetting({ training_plan: e.target.value.trim() }));
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
function rerender() {
  if (dashData && !dashData.empty) renderCharts(dashData);
  if (insightData) renderInsights(insightData);
  if ($("#activity-dialog").open) renderActivity(activityData);
}
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", rerender);
// Canvas text can't swap fonts in later, so redraw once any web fonts have loaded.
document.fonts?.ready.then(rerender);

function applyChartFont() {
  Chart.defaults.font.family = css("--font-chart") || getComputedStyle(document.body).fontFamily;
  Chart.defaults.font.size = cssNum("--chart-font-size", 12);
}

loadStatus();
loadDashboard();
api("/api/insights").then((d) => renderInsights(d.insights ? d : null));
