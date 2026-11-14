// Belt-health dashboard: vanilla JS + SVG, no build step.
// Text from the API is always inserted with textContent (labels are untrusted data).

const SITE_OFFSET_MIN = 330; // IST, the site's local time
const REFRESH_MS = 5000;
const STATUS = {
  normal: { cls: "good", icon: "✓", label: "Normal" },
  abnormal: { cls: "warning", icon: "!", label: "Abnormal" },
  predicted_failure: { cls: "critical", icon: "✕", label: "Predicted failure" },
  no_data: { cls: "nodata", icon: "?", label: "No data" },
};
const SEVERITY = {
  low: { cls: "warning", icon: "!", label: "Low" },
  medium: { cls: "serious", icon: "!", label: "Medium" },
  high: { cls: "critical", icon: "!", label: "High" },
  critical: { cls: "critical", icon: "✕", label: "Critical" },
};
const ANOMALY_LABEL = {
  bearing_hf_envelope: "Bearing defect (HF envelope)",
  bearing_vibration: "Bearing vibration",
  bearing_temperature: "Bearing over-temperature",
  belt_mistracking: "Belt mistracking",
  gearbox_vibration: "Gearbox vibration",
  gearbox_temperature: "Gearbox oil temperature",
  belt_slip: "Belt slip on drive pulley",
  motor_overtemperature: "Motor over-temperature",
  station_offline: "Station offline",
  sensor_stuck: "Sensor stuck",
  sensor_noisy: "Sensor errors / spikes",
  temperature_uncorroborated: "Suspected sensor drift",
};
const MODE_SHORT = { bearing: "Bearing failure", misalignment: "Mistracking trip", gearbox: "Gearbox failure", slip: "Belt slip trip" };

let CFG = null;
let selected = null;
let lastNow = null;
let CURRENT_STOPPED = [];

// ------------------------------------------------------------------ helpers
const $ = (id) => document.getElementById(id);
function el(tag, attrs = {}, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined) continue;
    if (k === "class") e.className = v;
    else if (k === "text") e.textContent = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else e.setAttribute(k, v);
  }
  for (const k of kids.flat()) if (k !== null && k !== undefined) e.append(k instanceof Node ? k : document.createTextNode(String(k)));
  return e;
}
const SVGNS = "http://www.w3.org/2000/svg";
function sv(tag, attrs = {}, text) {
  const e = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs)) if (v !== null && v !== undefined) e.setAttribute(k, v);
  if (text !== undefined) e.textContent = text;
  return e;
}
// Replay mode (static hosting, e.g. GitHub Pages): the page sets window.BELT_REPLAY = {base: "data/"} and every
// API call is answered from a recorded frame instead of the live backend. Same rendering code either way.
const REPLAY = window.BELT_REPLAY || null;
const R = { index: null, idx: 0, frames: {}, series: {}, timer: null, fps: 1 };
async function loadJSON(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${url}: ${r.status}`);
  return r.json();
}
async function frame(i) {
  if (!R.frames[i]) R.frames[i] = loadJSON(`${REPLAY.base}frames/${String(i).padStart(4, "0")}.json`);
  return R.frames[i];
}
function sliceSeries(s, now, win) {
  const t0 = now - win;
  const cut = (d) => {
    const keep = d.t.map((t, i) => (t >= t0 && t <= now ? i : -1)).filter((i) => i >= 0);
    return { t: keep.map((i) => d.t[i]), v: keep.map((i) => d.v[i]) };
  };
  const out = { station: s.station, now, series: {}, predictions: {}, anomalies: [], stopped: [] };
  for (const [k, d] of Object.entries(s.series)) out.series[k] = cut(d);
  for (const [k, d] of Object.entries(s.predictions)) out.predictions[k] = cut(d);
  out.stopped = (s.stopped || []).filter(([a, b]) => b >= t0 && a <= now).map(([a, b]) => [Math.max(a, t0), Math.min(b, now)]);
  return out;
}
async function replayApi(path) {
  if (path.startsWith("/api/config")) return loadJSON(`${REPLAY.base}config.json`);
  const f = await frame(R.idx);
  const m = path.match(/^\/api\/stations\/([A-Z0-9]+)\/series/);
  if (m) {
    if (!R.series[m[1]]) R.series[m[1]] = loadJSON(`${REPLAY.base}series/${m[1]}.json`);
    return sliceSeries(await R.series[m[1]], f.overview.now, 72 * 3600);
  }
  const map = [["/api/overview", "overview"], ["/api/stations", "stations"], ["/api/predictions", "predictions"],
    ["/api/anomalies", "anomalies"], ["/api/vision/tracks", "vision_tracks"], ["/api/vision/summary", "vision_summary"],
    ["/api/data-quality", "dq"], ["/api/maintenance", "maintenance"]];
  for (const [prefix, key] of map) if (path.startsWith(prefix)) return f[key];
  throw new Error(`replay: no recording for ${path}`);
}
async function api(path) {
  if (REPLAY) return replayApi(path);
  const r = await fetch(path, { cache: "no-store" });
  if (!r.ok) throw new Error(`${path}: ${r.status}`);
  return r.json();
}
const cropUrl = (t) => REPLAY ? `${REPLAY.base}crops/${t.track_id}.jpg` : `/api/vision/tracks/${t.track_id}/crop.jpg?v=${t.last_ts}`;
function local(ts) { return new Date((ts + SITE_OFFSET_MIN * 60) * 1000); }
const DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const pad = (n) => String(n).padStart(2, "0");
function fmtTime(ts, withDay = true) {
  if (ts === null || ts === undefined) return "–";
  const d = local(ts);
  const hm = `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}`;
  return withDay ? `${DAYS[d.getUTCDay()]} ${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]} ${hm}` : hm;
}
function fmtDur(s) {
  if (s === null || s === undefined || !isFinite(s)) return "–";
  s = Math.max(0, s);
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  if (d) return `${d} d ${h} h`;
  if (h) return `${h} h ${m} min`;
  return `${m} min`;
}
const fmtN = (x, d = 0) => (x === null || x === undefined || !isFinite(x)) ? "–" :
  Number(x).toLocaleString("en-US", { maximumFractionDigits: d, minimumFractionDigits: d });
function chip(meta, text) {
  return el("span", { class: "chip" }, el("span", { class: `ic ${meta.cls}`, "aria-hidden": "true", text: meta.icon }),
    text ?? meta.label);
}
function nextPlannedStop(now) {
  // planned stop 00:00-06:00 site time: next local midnight
  const d = local(now);
  const secOfDay = d.getUTCHours() * 3600 + d.getUTCMinutes() * 60;
  if (secOfDay < 6 * 3600) return now - secOfDay; // already inside today's window
  return now + (86400 - secOfDay);
}
const stationById = (id) => CFG.stations.find((s) => s.id === id);

// tooltip
const tip = $("tooltip");
function showTip(evt, rows) {
  tip.replaceChildren(...rows.map((r) => el("div", { class: r.cls || "" }, r.text)));
  tip.hidden = false;
  const pad = 14, w = tip.offsetWidth, h = tip.offsetHeight;
  let x = evt.clientX + pad, y = evt.clientY + pad;
  if (x + w > window.innerWidth - 8) x = evt.clientX - w - pad;
  if (y + h > window.innerHeight - 8) y = evt.clientY - h - pad;
  tip.style.left = `${x}px`;
  tip.style.top = `${y}px`;
}
const hideTip = () => { tip.hidden = true; };

// ------------------------------------------------------------------ KPIs & clock
function renderHeader(ov, stations, preds, vis, dq) {
  lastNow = ov.now;
  $("clock-time").textContent = `${fmtTime(ov.now)} site time (IST)`;
  const run = ov.running ? `Running · ${fmtN(ov.throughput_tph)} t/h · ${fmtN(ov.belt_speed_m_s, 2)} m/s`
    : "Stopped (planned stop 00:00–06:00, trip, or repair)";
  $("clock-state").textContent = run;
  const c = ov.status_counts;
  const pf = preds.filter((p) => p.alert);
  const conf = (vis.tracks || []).filter((t) => t.status === "confirmed");
  const tent = (vis.tracks || []).filter((t) => t.status === "tentative");
  const sev = ov.active_anomalies_by_severity || {};
  const comp = stations.map((s) => s.completeness_24h).filter((x) => x !== null && x !== undefined);
  const compMean = comp.length ? comp.reduce((a, b) => a + b, 0) / comp.length : null;
  const issues = stations.reduce((n, s) => n + (s.sensor_issues || []).length, 0);
  const tiles = [
    { label: "Predicted failures (next 72 h)", value: pf.length, icon: pf.length ? STATUS.predicted_failure : STATUS.normal,
      note: pf.slice(0, 2).map((p) => `${p.station_id} ${MODE_SHORT[p.mode].toLowerCase()}${p.ttf_p50_h != null ? ` ~${fmtN(p.ttf_p50_h)} h` : ""}`).join(" · ") || "none" },
    { label: "Active equipment anomalies", value: Object.values(sev).reduce((a, b) => a + b, 0),
      icon: sev.critical ? SEVERITY.critical : sev.high ? SEVERITY.high : sev.medium ? SEVERITY.medium : sev.low ? SEVERITY.low : STATUS.normal,
      note: (["critical", "high", "medium", "low"].filter((k) => sev[k]).map((k) => `${sev[k]} ${k}`).join(" · ") || "none") +
        (c.no_data ? ` · ${c.no_data} station${c.no_data > 1 ? "s" : ""} without data` : "") },
    { label: "Belt defects confirmed (vision)", value: conf.length, icon: conf.some((t) => ["high", "critical"].includes(t.severity)) ? SEVERITY.high : STATUS.normal,
      note: `${tent.length} unconfirmed (awaiting 2nd sighting)` },
    { label: "Sensor data completeness (24 h)", value: compMean === null ? "–" : `${fmtN(compMean * 100, 1)}%`, icon: null,
      note: `${issues} sensor issue${issues === 1 ? "" : "s"} · ${fmtN(dq.ingest.duplicates)} duplicates dropped` },
    { label: "Throughput now", value: ov.running ? `${fmtN(ov.throughput_tph)} t/h` : "0 t/h", icon: null,
      note: ov.running ? "belt running" : "belt stopped" },
  ];
  $("kpis").replaceChildren(...tiles.map((t) => el("div", { class: "tile" },
    el("div", { class: "label", text: t.label }),
    el("div", { class: "value" }, t.icon ? el("span", { class: `ic ${t.icon.cls}`, "aria-hidden": "true", text: t.icon.icon }) : null, String(t.value)),
    el("div", { class: "note", text: t.note }))));
}

// ------------------------------------------------------------------ belt map
function renderMap(stations) {
  const W = 1200, H = 262, x0 = 70, x1 = 1150;
  const X = (ch) => x0 + (ch / CFG.route_length_m) * (x1 - x0);
  const E = (e) => 112 - (e / 140) * 76;
  const g = sv("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Belt route with sensor stations coloured by status" });
  // curve zones
  for (const [a, b, r] of CFG.curves) {
    g.append(sv("rect", { x: X(a), y: 24, width: X(b) - X(a), height: 222, fill: "var(--hover)" }));
    g.append(sv("text", { x: (X(a) + X(b)) / 2, y: 18, "text-anchor": "middle", class: "lbl" }, `Horizontal curve R ${fmtN(r)} m`));
  }
  // elevation (context)
  for (const e of [0, 50, 100]) {
    g.append(sv("line", { x1: x0, x2: x1, y1: E(e), y2: E(e), class: "gridline" }));
    g.append(sv("text", { x: x0 - 8, y: E(e) + 4, "text-anchor": "end", class: "axis-text" }, e === 100 ? "100 m" : String(e)));
  }
  const prof = CFG.elevation_profile.map(([c, e]) => `${X(c)},${E(e)}`).join(" ");
  g.append(sv("polygon", { points: `${X(0)},${E(0)} ${prof} ${X(CFG.route_length_m)},${E(0)}`, fill: "var(--wash-1)" }));
  g.append(sv("polyline", { points: prof, fill: "none", stroke: "var(--series-1)", "stroke-width": 2, "stroke-linejoin": "round" }));
  g.append(sv("text", { x: X(1800), y: E(54) - 8, "text-anchor": "middle", class: "lbl" }, "8 % incline"));
  // strands
  const yC = 194, yR = 232;
  g.append(sv("line", { x1: x0, x2: x1, y1: yC, y2: yC, stroke: "var(--text-secondary)", "stroke-width": 3, "stroke-linecap": "round" }));
  g.append(sv("line", { x1: x0, x2: x1, y1: yR, y2: yR, stroke: "var(--axis)", "stroke-width": 3, "stroke-linecap": "round" }));
  g.append(sv("text", { x: x0 - 8, y: yC + 4, "text-anchor": "end", class: "axis-text" }, "carry"));
  g.append(sv("text", { x: x0 - 8, y: yR + 4, "text-anchor": "end", class: "axis-text" }, "return"));
  for (let c = 0; c <= CFG.route_length_m; c += 1000) {
    g.append(sv("line", { x1: X(c), x2: X(c), y1: yC + 3, y2: yC + 7, class: "axisline" }));
    g.append(sv("text", { x: X(c), y: yC + 19, "text-anchor": "middle", class: "axis-text" }, `${c / 1000} km`));
  }
  g.append(sv("text", { x: x0, y: yR + 22, class: "lbl" }, "Tail · loading"));
  g.append(sv("text", { x: (x0 + x1) / 2, y: yR + 22, "text-anchor": "middle", class: "axis-text" },
    "material flows → to the head on the carry strand · the belt returns ← underneath"));
  g.append(sv("text", { x: x1, y: yR + 22, "text-anchor": "end", class: "lbl" }, "Head drive · discharge"));
  // camera on the return strand just after the head pulley
  const cx = X(5945);
  g.append(sv("rect", { x: cx - 9, y: yR - 17, width: 18, height: 12, rx: 2, fill: "var(--text-secondary)" }));
  g.append(sv("circle", { cx: cx, cy: yR - 11, r: 3.5, fill: "var(--surface-1)" }));
  g.append(sv("text", { x: cx - 14, y: yR - 8, "text-anchor": "end", class: "lbl" }, "camera + RFID reader"));
  // station markers: spread so none overlap, with a leader to the true chainage
  const items = stations.map((s) => ({ s, x: X(s.chainage_m) })).sort((a, b) => a.x - b.x);
  const MIN = 27;
  for (let i = 1; i < items.length; i++) items[i].px = Math.max(items[i].x, (items[i - 1].px ?? items[i - 1].x) + MIN);
  items[0].px = items[0].x;
  const over = items[items.length - 1].px - x1;
  if (over > 0) for (let i = items.length - 1; i >= 0; i--) {
    const lim = i === items.length - 1 ? x1 : items[i + 1].px - MIN;
    items[i].px = Math.min(items[i].px, lim);
  }
  const yM = 160;
  for (const it of items) {
    const st = STATUS[it.s.status] || STATUS.no_data;
    if (Math.abs(it.px - it.x) > 1) g.append(sv("line", { x1: it.px, y1: yM + 10, x2: it.x, y2: yC, stroke: "var(--axis)", "stroke-width": 1 }));
    else g.append(sv("line", { x1: it.x, y1: yM + 10, x2: it.x, y2: yC, stroke: "var(--axis)", "stroke-width": 1 }));
    const m = sv("g", { class: "station-mark", tabindex: 0, role: "button",
      "aria-label": `${it.s.id} ${it.s.name}: ${st.label}` });
    m.append(sv("circle", { cx: it.px, cy: yM, r: 10, class: "ring", fill: `var(--${st.cls === "nodata" ? "nodata" : st.cls})`,
      stroke: it.s.id === selected ? "var(--text-primary)" : "var(--surface-1)", "stroke-width": 2 }));
    m.append(sv("text", { x: it.px, y: yM + 4, "text-anchor": "middle", "font-size": 11, "font-weight": 700,
      fill: st.cls === "warning" ? "#0b0b0b" : "#ffffff" }, st.icon));
    m.append(sv("text", { x: it.px, y: yM - 15, "text-anchor": "middle", class: it.s.id === selected ? "lbl-strong" : "lbl" }, it.s.id));
    const rows = () => [
      { text: `${it.s.id} · ${it.s.name}`, cls: "tv" },
      { text: `${st.label} · km ${fmtN(it.s.chainage_m / 1000, 2)} · ${it.s.zone}`, cls: "tl" },
      ...(it.s.reasons || []).slice(0, 4).map((r) => ({ text: r })),
      ...(it.s.sensor_issues || []).slice(0, 2).map((r) => ({ text: `Sensor: ${r}`, cls: "tl" })),
    ];
    m.addEventListener("pointermove", (e) => showTip(e, rows()));
    m.addEventListener("pointerleave", hideTip);
    m.addEventListener("focus", (e) => { const r = m.getBoundingClientRect(); showTip({ clientX: r.right, clientY: r.bottom }, rows()); });
    m.addEventListener("blur", hideTip);
    m.addEventListener("click", () => selectStation(it.s.id));
    m.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") selectStation(it.s.id); });
    g.append(m);
  }
  $("beltmap").replaceChildren(g);
  $("map-legend").replaceChildren(...Object.values(STATUS).map((s) => chip(s)));
  // table view twin
  const t = el("table", {}, el("thead", {}, el("tr", {}, ...["Station", "Location", "Status", "Why", "Data 24 h"].map((h) => el("th", { text: h })))),
    el("tbody", {}, ...stations.map((s) => el("tr", { class: "clickable", tabindex: 0, onclick: () => selectStation(s.id) },
      el("td", { text: `${s.id} ${s.name}` }), el("td", { text: `km ${fmtN(s.chainage_m / 1000, 2)}` }),
      el("td", {}, chip(STATUS[s.status] || STATUS.no_data)), el("td", { class: "evid", text: (s.reasons || [])[0] || "" }),
      el("td", { class: "num", text: s.completeness_24h == null ? "–" : `${fmtN(s.completeness_24h * 100, 1)}%` })))));
  $("station-table").replaceChildren(t);
}

// ------------------------------------------------------------------ predictions & anomalies
function renderPredictions(preds) {
  if (!preds.length) { $("predictions").replaceChildren(el("p", { class: "empty", text: "No component is trending towards failure." })); return; }
  const stop = nextPlannedStop(lastNow);
  const rows = preds.slice(0, 12).map((p) => {
    const alert = p.alert;
    const st = stationById(p.station_id);
    let when = "–", action = alert ? "Inspect" : "Watch";
    if (p.ttf_p50_h != null) {
      when = `${fmtTime(p.ts + p.ttf_p50_h * 3600)} (in ${fmtN(p.ttf_p50_h)} h)`;
      const early = p.ts + (p.ttf_p10_h ?? p.ttf_p50_h) * 3600;
      if (alert) action = early > stop + 6 * 3600 ? `Fix in planned stop ${fmtTime(stop)}` : "Inspect now: may fail before the next planned stop";
    }
    const range = (p.ttf_p10_h != null && p.ttf_p90_h != null) ? `likely ${fmtN(p.ttf_p10_h)}–${fmtN(p.ttf_p90_h)} h` : "";
    return el("tr", { class: `clickable${p.station_id === selected ? " selected" : ""}`, tabindex: 0, onclick: () => selectStation(p.station_id) },
      el("td", {}, el("span", { class: `meter${alert ? " crit" : ""}` }, el("i", { style: `width:${Math.round(p.probability * 100)}%` })),
        el("span", { class: "num", text: `${fmtN(p.probability * 100)}%` })),
      el("td", {}, alert ? chip(STATUS.predicted_failure, MODE_SHORT[p.mode]) : el("span", { text: MODE_SHORT[p.mode] }),
        el("div", { class: "evid", text: p.method === "ml" ? `ML · confidence ${p.confidence}` : "trend to limit" })),
      el("td", { text: `${p.station_id} ${st ? st.name : ""}` }, el("div", { class: "evid", text: `km ${fmtN(p.chainage_m / 1000, 2)}` })),
      el("td", { text: when }, el("div", { class: "evid", text: range })),
      el("td", {}, el("div", { text: action }), el("div", { class: "evid", text: p.evidence || "" })));
  });
  $("predictions").replaceChildren(el("div", { class: "scroll" }, el("table", {},
    el("thead", {}, el("tr", {}, ...["P(fail ≤ 72 h)", "What", "Where", "When (median)", "Action · evidence"].map((h) => el("th", { text: h })))),
    el("tbody", {}, ...rows))),
    el("p", { class: "muted small", text: "An alert needs two consecutive hourly predictions above the model's threshold (chosen on a validation run to keep false alarms ≤ ~1 per 10 days). Rows without the ✕ are on watch. Times are when the component is expected to fail if nothing is done." }));
}

function renderAnomalies(list) {
  const eq = list.filter((a) => a.category === "equipment");
  if (!eq.length) { $("anomalies").replaceChildren(el("p", { class: "empty", text: "No active equipment anomalies." })); return; }
  const rows = eq.map((a) => el("tr", { class: `clickable${a.station_id === selected ? " selected" : ""}`, tabindex: 0, onclick: () => selectStation(a.station_id) },
    el("td", {}, chip(SEVERITY[a.severity] || SEVERITY.low)),
    el("td", { text: ANOMALY_LABEL[a.type] || a.type }),
    el("td", { text: a.station_id }, el("div", { class: "evid", text: `km ${fmtN(a.chainage_m / 1000, 2)}` })),
    el("td", { text: fmtDur(lastNow - a.started_ts) }),
    el("td", { class: "evid", text: a.message })));
  $("anomalies").replaceChildren(el("div", { class: "scroll" }, el("table", {},
    el("thead", {}, el("tr", {}, ...["Severity", "Type", "Where", "Active for", "Evidence"].map((h) => el("th", { text: h })))),
    el("tbody", {}, ...rows))));
}

// ------------------------------------------------------------------ vision
function renderVision(vis, summ) {
  const tracks = vis.tracks || [];
  const conf = tracks.filter((t) => t.status === "confirmed").length;
  const raw = summ.raw_detections || 0;
  const tent = tracks.filter((t) => t.status === "tentative").length;
  $("vision-summary").textContent = summ.passes
    ? `${summ.passes} camera passes (one per belt revolution) · ${fmtN(raw)} raw detections → ${conf} confirmed defect${conf === 1 ? "" : "s"}` +
      ` (+${tent} unconfirmed, awaiting a second sighting). ` +
      `Without tracking in belt coordinates every raw detection would have been an alert. RFID splice-tag read rate ${fmtN((summ.tag_read_rate || 0) * 100, 1)}%.`
    : "Waiting for the first camera pass…";
  const L = summ.loop_length_m || CFG.loop_length_m;
  const W = 1200, H = 96, x0 = 40, x1 = 1170;
  const X = (p) => x0 + (p / L) * (x1 - x0);
  const g = sv("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Belt loop with defect positions" });
  g.append(sv("rect", { x: x0, y: 34, width: x1 - x0, height: 18, rx: 9, fill: "var(--hover)", stroke: "var(--axis)" }));
  (summ.splices || []).forEach((p, i) => {
    g.append(sv("line", { x1: X(p), x2: X(p), y1: 30, y2: 56, stroke: "var(--axis)", "stroke-width": 1 }));
    if (i % 5 === 0) g.append(sv("text", { x: X(p), y: 70, "text-anchor": "middle", class: "axis-text" }, `splice ${i}`));
  });
  g.append(sv("text", { x: x0, y: 88, class: "axis-text" }, "Belt coordinate (m from splice 0) — the loop wraps: the right end continues at the left"));
  g.append(sv("text", { x: x1, y: 88, "text-anchor": "end", class: "axis-text" }, `${fmtN(L)} m`));
  for (const t of tracks) {
    const sev = SEVERITY[t.severity] || SEVERITY.low;
    const x = X(t.belt_pos_m), y = 43;
    const d = `M ${x} ${y - 9} L ${x + 8} ${y} L ${x} ${y + 9} L ${x - 8} ${y} Z`;
    const mk = sv("path", { d, fill: t.status === "confirmed" ? `var(--${sev.cls})` : "var(--surface-1)",
      stroke: `var(--${sev.cls})`, "stroke-width": 2, tabindex: 0 });
    const rows = [{ text: `D-${t.track_id} · ${t.cls} · ${sev.label}`, cls: "tv" },
      { text: `${t.status} · seen ${t.n_seen} of ${t.n_imaged} passes`, cls: "tl" },
      { text: `splice ${t.segment} + ${fmtN(t.seg_offset_m, 1)} m · ${fmtN(Math.abs(t.lateral_m), 2)} m ${t.lateral_m >= 0 ? "right" : "left"} of centre` }];
    mk.addEventListener("pointermove", (e) => showTip(e, rows));
    mk.addEventListener("pointerleave", hideTip);
    g.append(mk);
  }
  $("loopstrip").replaceChildren(g);
  $("loop-legend").replaceChildren(chip(SEVERITY.high, "High / critical"), chip(SEVERITY.medium, "Medium"),
    chip(SEVERITY.low, "Low"), el("span", { class: "chip" }, el("span", { class: "ic sq", style: "background:transparent;border:2px solid var(--muted)" }), "hollow = unconfirmed"));
  const listed = tracks.filter((t) => t.status === "confirmed");
  if (!listed.length) { $("vision-table").replaceChildren(el("p", { class: "empty", text: "No confirmed defects yet." })); return; }
  const rows = listed.map((t) => {
    const sev = SEVERITY[t.severity] || SEVERITY.low;
    const where = t.where_if_stopped ? `${t.where_if_stopped.strand} strand, km ${fmtN(t.where_if_stopped.chainage_m / 1000, 2)}` : "–";
    return el("tr", {},
      el("td", {}, t.has_crop ? el("img", { class: "thumb", src: cropUrl(t), alt: `${t.cls} crop` }) : el("div", { class: "thumb" })),
      el("td", { text: `D-${t.track_id}` }, el("div", { class: "evid", text: t.status === "confirmed" ? "confirmed" : "unconfirmed" })),
      el("td", { text: t.cls }, el("div", { class: "evid", text: `${fmtN(t.length_m * 1000)} mm long · conf ${fmtN(t.conf_mean, 2)}` })),
      el("td", {}, chip(sev)),
      el("td", { text: `splice ${t.segment} + ${fmtN(t.seg_offset_m, 1)} m` }, el("div", { class: "evid", text: `${fmtN(Math.abs(t.lateral_m), 2)} m ${t.lateral_m >= 0 ? "right" : "left"} of centre` })),
      el("td", { class: "num", text: `${t.n_seen} / ${t.n_imaged}` }),
      el("td", { text: fmtTime(t.first_ts) }, el("div", { class: "evid", text: `last ${fmtTime(t.last_ts, false)}` })),
      el("td", { text: where }));
  });
  $("vision-table").replaceChildren(el("div", { class: "scroll" }, el("table", {},
    el("thead", {}, el("tr", {}, ...["", "Defect", "Class", "Severity", "Position on the belt", "Seen / imaged", "First seen", "If stopped now"].map((h) => el("th", { text: h })))),
    el("tbody", {}, ...rows))),
    el("p", { class: "muted small", text: "Positions are in belt coordinates (distance after a splice), re-zeroed at every RFID-tagged splice. A defect is confirmed once it is seen at the same belt position on two revolutions; transient dirt is dropped; a confirmed defect not seen for 3 imaged revolutions is marked repaired. “If stopped now” says where on the structure a fitter would find it." }));
}

// ------------------------------------------------------------------ station detail (small multiples)
function niceTicks(lo, hi, n = 4) {
  if (!isFinite(lo) || !isFinite(hi)) return [0, 1];
  if (hi - lo < 1e-9) { hi = lo + 1; }
  const step0 = (hi - lo) / n, mag = 10 ** Math.floor(Math.log10(step0));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= step0);
  const t = [];
  let v = Math.floor(lo / step) * step;
  t.push(+v.toFixed(10));
  while (v < hi - step * 1e-6) { v += step; t.push(+v.toFixed(10)); }   // last tick always >= max
  return t;
}
function miniChart(title, series, opts = {}) {
  opts = { stopped: CURRENT_STOPPED, ...opts };
  // series: [{name, color, t:[], v:[]}]; one y-axis, shared time window
  const W = 440, H = 128, ml = 40, mr = 8, mt = 6, mb = 20;
  const t0 = lastNow - 72 * 3600, t1 = lastNow;
  const all = series.flatMap((s) => s.v.filter((v) => v !== null && isFinite(v)));
  if (opts.ref) all.push(...opts.ref.map((r) => r.v));
  let lo = Math.min(...all), hi = Math.max(...all);
  if (opts.min !== undefined) lo = Math.min(lo, opts.min);
  if (opts.max !== undefined) hi = Math.max(hi, opts.max);
  if (!all.length) { lo = 0; hi = 1; }
  const ticks = niceTicks(lo, hi);
  lo = ticks[0]; hi = ticks[ticks.length - 1];
  const X = (t) => ml + ((t - t0) / (t1 - t0)) * (W - ml - mr);
  const Y = (v) => mt + (1 - (v - lo) / (hi - lo || 1)) * (H - mt - mb);
  const g = sv("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": title });
  for (const tk of ticks) {
    g.append(sv("line", { x1: ml, x2: W - mr, y1: Y(tk), y2: Y(tk), class: "gridline" }));
    g.append(sv("text", { x: ml - 6, y: Y(tk) + 4, "text-anchor": "end", class: "axis-text" }, fmtN(tk, Math.abs(hi - lo) < 3 ? 1 : 0)));
  }
  for (let t = Math.ceil(t0 / 86400) * 86400 - SITE_OFFSET_MIN * 60; t <= t1; t += 86400) {
    if (t < t0) continue;
    g.append(sv("line", { x1: X(t), x2: X(t), y1: mt, y2: H - mb, class: "gridline" }));
    const d = local(t);
    g.append(sv("text", { x: X(t), y: H - 6, "text-anchor": "middle", class: "axis-text" }, `${DAYS[d.getUTCDay()]} 00:00`));
  }
  for (const [a, b] of opts.stopped || []) {
    if (b < t0) continue;
    g.append(sv("rect", { x: X(Math.max(a, t0)), y: mt, width: Math.max(1, X(Math.min(b, t1)) - X(Math.max(a, t0))),
      height: H - mt - mb, fill: "var(--hover)" }));
  }
  for (const r of opts.ref || []) {
    g.append(sv("line", { x1: ml, x2: W - mr, y1: Y(r.v), y2: Y(r.v), stroke: "var(--muted)", "stroke-width": 1 }));
    g.append(sv("text", { x: W - mr - 2, y: Y(r.v) - 3, "text-anchor": "end", class: "axis-text" }, r.label));
  }
  for (const s of series) {
    let d = "", pen = false;
    s.t.forEach((t, i) => {
      const v = s.v[i];
      if (v === null || !isFinite(v) || t < t0) { pen = false; return; }
      if (pen && t - s.t[i - 1] > 3 * 3600) pen = false;  // gap -> break the line
      d += `${pen ? "L" : "M"}${X(t).toFixed(1)},${Y(v).toFixed(1)} `;
      pen = true;
    });
    g.append(sv("path", { d, fill: "none", stroke: s.color, "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round" }));
  }
  // crosshair + tooltip
  const cross = sv("line", { y1: mt, y2: H - mb, stroke: "var(--muted)", "stroke-width": 1, visibility: "hidden" });
  g.append(cross);
  const hit = sv("rect", { x: ml, y: mt, width: W - ml - mr, height: H - mt - mb, fill: "transparent" });
  hit.addEventListener("pointermove", (e) => {
    const r = g.getBoundingClientRect();
    const t = t0 + ((e.clientX - r.left) / r.width * W - ml) / (W - ml - mr) * (t1 - t0);
    let best = null;
    for (const s of series) s.t.forEach((tt) => { if (best === null || Math.abs(tt - t) < Math.abs(best - t)) best = tt; });
    if (best === null) return;
    cross.setAttribute("x1", X(best)); cross.setAttribute("x2", X(best)); cross.setAttribute("visibility", "visible");
    const rows = [{ text: fmtTime(best), cls: "tl" }];
    for (const s of series) {
      const i = s.t.indexOf(best);
      rows.push({ text: `${i >= 0 && s.v[i] !== null ? fmtN(s.v[i], opts.dp ?? 2) : "–"} ${opts.unit || ""}  ${series.length > 1 ? s.name : ""}`, cls: "tv" });
    }
    showTip(e, rows);
  });
  hit.addEventListener("pointerleave", () => { cross.setAttribute("visibility", "hidden"); hideTip(); });
  g.append(hit);
  const keys = series.length > 1 ? series.map((s) => el("span", {}, el("span", { class: "key-line", style: `background:${s.color}` }), s.name)) : [];
  return el("div", { class: "mini" }, el("h3", { text: title }), el("div", { class: "keys" }, ...keys), g);
}

async function renderDetail() {
  if (!selected) return;
  const [d, stations] = await Promise.all([api(`/api/stations/${selected}/series?hours=72`), api("/api/stations")]);
  const st = stations.find((s) => s.id === selected);
  const meta = STATUS[st.status] || STATUS.no_data;
  $("detail-title").replaceChildren(el("span", { text: `${st.id} · ${st.name} · km ${fmtN(st.chainage_m / 1000, 2)}  ` }), chip(meta));
  const S = d.series;
  CURRENT_STOPPED = d.stopped || [];
  const ser = (ch, name, color) => S[ch] ? { name, color, t: S[ch].t, v: S[ch].v } : null;
  const blue = "var(--series-1)", orange = "var(--series-2)", gray = "var(--muted)";
  const charts = [];
  if (st.kind === "drive") {
    charts.push(miniChart("Gearbox vibration (mm/s)", [ser("gearbox_vib", "gearbox", blue)].filter(Boolean), { unit: "mm/s", ref: [{ v: 7.1, label: "ISO zone D 7.1" }], min: 0 }));
    charts.push(miniChart("Gearbox oil vs ambient (°C)", [ser("gearbox_temp", "oil", blue), ser("ambient_temp", "ambient", gray)].filter(Boolean), { unit: "°C", dp: 1 }));
    if (S.drive_speed && S.belt_speed) {
      const v = S.drive_speed.v.map((ds, i) => (ds > 0.5 && S.belt_speed.v[i] !== undefined) ? (ds - S.belt_speed.v[i]) / ds * 100 : null);
      charts.push(miniChart("Belt slip on the drive pulley (%)", [{ name: "slip", color: blue, t: S.drive_speed.t, v }], { unit: "%", ref: [{ v: 2, label: "slip alarm 2 %" }], min: 0 }));
    }
    charts.push(miniChart("Motor current (A)", [ser("motor_current", "current", blue)].filter(Boolean), { unit: "A", dp: 0, min: 0 }));
  } else {
    charts.push(miniChart("Bearing HF envelope (g) — earliest warning", [ser("vib_env", "envelope", blue)].filter(Boolean), { unit: "g", min: 0 }));
    charts.push(miniChart("Vibration RMS (mm/s)", [ser("vib_rms", "RMS", blue)].filter(Boolean), { unit: "mm/s", ref: [{ v: 4.5, label: "ISO zone C 4.5" }], min: 0 }));
    charts.push(miniChart("Bearing vs ambient temperature (°C)", [ser("bearing_temp", "bearing", blue), ser("ambient_temp", "ambient", gray)].filter(Boolean), { unit: "°C", dp: 1 }));
    const off = S.belt_offset ? S.belt_offset.v.filter((v) => v !== null) : [0];
    const side = off.length && off.reduce((a, b) => a + b, 0) / off.length < 0 ? -1 : 1;
    charts.push(miniChart("Belt offset from centre (mm)", [ser("belt_offset", "offset", blue)].filter(Boolean), { unit: "mm", dp: 0, ref: [{ v: 110 * side, label: "sway switch trips" }], min: side > 0 ? -20 : undefined, max: side < 0 ? 20 : undefined }));
  }
  const modes = Object.keys(d.predictions);
  if (modes.length) {
    const colors = [blue, orange, "var(--series-3)"];
    charts.push(miniChart("Predicted P(failure within 72 h)", modes.map((m, i) => ({ name: MODE_SHORT[m], color: colors[i % 3], t: d.predictions[m].t, v: d.predictions[m].v })), { unit: "", min: 0, max: 1 }));
  }
  const reasons = el("ul", { class: "reasons" }, ...(st.reasons || []).map((r) => el("li", { text: r })),
    ...(st.sensor_issues || []).map((r) => el("li", { text: `Sensor: ${r}` })));
  $("detail").replaceChildren(reasons, el("p", { class: "muted small", text: "Shaded bands: belt stopped (planned 00:00–06:00 window, trips, repairs). Running-only signals have gaps there." }),
    el("div", { class: "multiples" }, ...charts));
}
function selectStation(id) {
  selected = id;
  renderDetail().catch(console.error);
  refresh().catch(console.error);
  $("detail-card").scrollIntoView({ behavior: "smooth", block: "start" });
}

// ------------------------------------------------------------------ data quality & maintenance
function renderDQ(dq) {
  const i = dq.ingest;
  const items = [["Readings received", i.received], ["Stored", i.accepted], ["Duplicates dropped", i.duplicates],
    ["Rejected → dead letter", i.rejected], ["Error codes nulled", i.error_codes], ["Out of physical range", i.out_of_range],
    ["Units converted", i.unit_converted], ["Late (store-and-forward)", i.late]];
  const grid = el("div", { class: "dq-grid" }, ...items.map(([l, n]) => el("div", {}, el("div", { class: "n", text: fmtN(n) }), el("div", { class: "l", text: l }))));
  const reasons = Object.entries(dq.reject_reasons || {});
  const rr = reasons.length ? el("p", { class: "small muted", text: "Rejected: " + reasons.map(([k, v]) => `${k.replaceAll("_", " ")} ${fmtN(v)}`).join(" · ") }) : null;
  const issues = (dq.active_issues || []).map((a) => el("tr", { class: "clickable", tabindex: 0, onclick: () => selectStation(a.station_id) },
    el("td", { text: a.station_id }), el("td", { text: ANOMALY_LABEL[a.type] || a.type }),
    el("td", { class: "evid", text: a.message }), el("td", { text: fmtDur(lastNow - a.started_ts) })));
  const comp = (dq.stations || []).map((s) => el("tr", {},
    el("td", { text: s.station_id }),
    el("td", {}, el("span", { class: "meter" }, el("i", { style: `width:${Math.round((s.completeness_24h || 0) * 100)}%` })),
      el("span", { class: "num", text: `${fmtN((s.completeness_24h || 0) * 100, 1)}%` }))));
  const half = Math.ceil(comp.length / 2);
  const compTables = el("div", { class: "grid2", style: "gap:12px" },
    el("table", {}, el("tbody", {}, ...comp.slice(0, half))), el("table", {}, el("tbody", {}, ...comp.slice(half))));
  $("dq").replaceChildren(...[grid, rr,
    el("h3", { class: "small", text: "Active sensor issues (not equipment faults)" }),
    issues.length ? el("div", { class: "scroll" }, el("table", {}, el("tbody", {}, ...issues))) : el("p", { class: "empty", text: "None." }),
    el("details", { class: "tableview" }, el("summary", { text: "Data completeness per station, last 24 h" }), compTables)].filter(Boolean));
}
function renderMaint(log) {
  if (!log.length) { $("maint").replaceChildren(el("p", { class: "empty", text: "No maintenance events yet." })); return; }
  $("maint").replaceChildren(el("div", { class: "scroll" }, el("table", {},
    el("thead", {}, el("tr", {}, ...["When", "Station", "Event", "Note"].map((h) => el("th", { text: h })))),
    el("tbody", {}, ...log.map((m) => el("tr", {}, el("td", { text: fmtTime(m.ts) }), el("td", { text: m.station_id || "" }),
      el("td", {}, m.kind === "failure" ? chip(SEVERITY.critical, "Failure") : chip(STATUS.normal, "Repaired")),
      el("td", { class: "evid", text: m.note || "" })))))));
}

// ------------------------------------------------------------------ main loop
async function refresh() {
  const ov = await api("/api/overview");
  if (!ov.now) return;
  const [stations, preds, anoms, vis, summ, dq, maint] = await Promise.all([
    api("/api/stations"), api("/api/predictions"), api("/api/anomalies?status=active"),
    api("/api/vision/tracks"), api("/api/vision/summary"), api("/api/data-quality"), api("/api/maintenance?limit=30")]);
  renderHeader(ov, stations, preds, vis, dq);
  renderMap(stations);
  renderPredictions(preds);
  renderAnomalies(anoms);
  renderVision(vis, summ);
  renderDQ(dq);
  renderMaint(maint);
  if (!selected) {
    const top = preds.find((p) => p.alert) || preds[0];
    selected = top ? top.station_id : (stations.find((s) => s.status !== "normal") || stations[0]).id;
    renderDetail().catch(console.error);
  }
  const a = ov.analysis || {};
  $("footer").textContent = REPLAY
    ? `Recorded replay · frame ${R.idx + 1} of ${R.index.frames.length} · data up to ${fmtTime(ov.now)}`
    : `Analysis cycle ${a.last ? `${a.last.seconds}s` : "–"}${a.error ? ` · analysis error: ${a.error}` : ""} · data up to ${fmtTime(ov.now)} · refreshes every ${REFRESH_MS / 1000}s`;
}

async function showFrame(i) {
  R.idx = Math.max(0, Math.min(i, R.index.frames.length - 1));
  $("rp-slider").value = R.idx;
  await refresh();
  await renderDetail();
  if (R.idx + 1 < R.index.frames.length) frame(R.idx + 1).catch(() => {});   // prefetch the next frame
}
function setPlaying(on) {
  clearInterval(R.timer);
  R.timer = null;
  $("rp-play").textContent = on ? "❚❚ Pause" : "▶ Play";
  if (!on) return;
  R.timer = setInterval(() => {
    if (R.idx >= R.index.frames.length - 1) { setPlaying(false); return; }
    showFrame(R.idx + 1).catch(console.error);
  }, 1000 / R.fps);
}
async function initReplay() {
  R.index = await loadJSON(`${REPLAY.base}index.json`);
  const n = R.index.frames.length;
  const bar = el("div", { class: "replay-bar", role: "region", "aria-label": "Replay controls" },
    el("span", { class: "rp-tag", text: "Recorded replay" }),
    el("span", { class: "muted small", text: `of a ${R.index.hours}-hour run of the simulator (not live). Clone the repo and run make demo for the live system.` }),
    el("a", { class: "small", href: "../inspect/", text: "Inspect your own belt photos →" }),
    el("button", { id: "rp-play", type: "button", onclick: () => setPlaying(!R.timer) }, "▶ Play"),
    el("input", { id: "rp-slider", type: "range", min: 0, max: n - 1, value: 0, "aria-label": "Replay position",
      oninput: (e) => { setPlaying(false); showFrame(Number(e.target.value)).catch(console.error); } }),
    el("label", { class: "muted small" }, "speed ",
      (() => { const sel = el("select", { onchange: (e) => { R.fps = Number(e.target.value); if (R.timer) setPlaying(true); } },
        ...[["0.5", "slow"], ["1", "1 frame/s"], ["2", "2 frames/s"], ["4", "4 frames/s"]].map(([v, l]) => el("option", { value: v, text: l })));
      sel.value = "1"; return sel; })()));
  document.querySelector(".viz-root").prepend(bar);
  // ?frame=N opens a specific moment, ?play=0 starts paused, ?station=S18 preselects a station (shareable links)
  const q = new URLSearchParams(location.search);
  if (q.get("station")) selected = q.get("station").toUpperCase();
  await showFrame(Number(q.get("frame") || 0));
  setPlaying(q.get("play") !== "0");
}
async function main() {
  CFG = await api("/api/config");
  if (REPLAY) { await initReplay(); return; }
  const loop = async () => {
    try { await refresh(); } catch (e) { console.error(e); }
    setTimeout(loop, REFRESH_MS);
  };
  loop();
  setInterval(() => renderDetail().catch(console.error), 15000);
}
main();
