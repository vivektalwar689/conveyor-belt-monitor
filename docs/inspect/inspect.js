// Belt wear & tear detector running in the browser (ONNX Runtime Web, WebAssembly).
// Pre- and post-processing mirror Ultralytics' ONNX inference exactly: letterbox to 512 with grey (114) padding,
// argmax class, per-class NMS at IoU 0.5, rescale, clip. vision/export_web.py --check runs the same steps in numpy
// and reproduces Ultralytics' boxes, classes and confidences exactly on 78 test images. In Chromium the JPEG decode
// is bit-identical to OpenCV's; the canvas resize differs by under one grey level on 99% of pixels, which moves
// confidences by about 0.01 (same detections at the 0.35 threshold on 66 of 69 test images of four shapes).
// User text never goes through innerHTML (file names are untrusted).

const CLASSES = ["Hole", "Impact Damage", "Puncture", "Tear", "Wear"];
const SEVERITY = { Tear: "high", Hole: "high", Puncture: "medium", "Impact Damage": "medium", Wear: "low" };
const SEV_META = {
  high: { cls: "critical", icon: "!", label: "High", rank: 2 },
  medium: { cls: "serious", icon: "!", label: "Medium", rank: 1 },
  low: { cls: "warning", icon: "!", label: "Low", rank: 0 },
};
const CLASS_COLOR = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4"];   // categorical slots, one per class
const LABEL_TEXT = ["#ffffff", "#0b0b0b", "#0b0b0b", "#0b0b0b", "#0b0b0b"];     // higher-contrast text on each fill
const S = 512, IOU = 0.5, FLOOR = 0.10;
const MODELS = { deployed: "models/deployed.onnx", baseline: "models/baseline.onnx" };
// held-out test scenes: tear x2, hole, puncture+tear, impact damage+tear, wear, and two defect-free belts
const SAMPLES = ["train_0598.jpg", "valid_1982.jpg", "test_2264.jpg", "train_0293.jpg", "train_0087.jpg",
  "test_2261.jpg", "train_0560.jpg", "valid_1787.jpg"];

// must be an absolute URL: the runtime import()s its loader from here, and bare paths are not valid module specifiers
ort.env.wasm.wasmPaths = new URL("vendor/", import.meta.url).href;
ort.env.wasm.numThreads = 1;        // GitHub Pages is not cross-origin isolated: no SharedArrayBuffer threads

const $ = (id) => document.getElementById(id);
// gen increments on Clear, so analyses still in flight drop their results instead of adding them back
const state = { model: "deployed", thr: 0.35, showGt: true, sessions: {}, loadS: {}, items: [], busy: 0, gen: 0,
  fatal: null };
window.__inspect = state;           // used by the end-to-end test

function el(tag, attrs = {}, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined) continue;
    if (k === "class") e.className = v;
    else if (k === "text") e.textContent = v;
    else e.setAttribute(k, v);
  }
  for (const k of kids.flat()) if (k !== null && k !== undefined) e.append(k instanceof Node ? k : document.createTextNode(String(k)));
  return e;
}
const chip = (meta, text) => el("span", { class: "chip" },
  el("span", { class: `ic ${meta.cls}`, "aria-hidden": "true", text: meta.icon }), text ?? meta.label);

// ------------------------------------------------------------------ model
function session(name) {
  if (!state.sessions[name]) {
    const t0 = performance.now();
    state.sessions[name] = ort.InferenceSession.create(MODELS[name], { executionProviders: ["wasm"] }).then((s) => {
      state.loadS[name] = (performance.now() - t0) / 1000;
      render();
      return s;
    });
  }
  return state.sessions[name];
}

function letterbox(bitmap) {
  const W = bitmap.width, H = bitmap.height;
  const r = Math.min(S / H, S / W);
  const nw = Math.round(W * r), nh = Math.round(H * r);
  const left = Math.round((S - nw) / 2 - 0.1), top = Math.round((S - nh) / 2 - 0.1);
  const c = document.createElement("canvas");
  c.width = S; c.height = S;
  const ctx = c.getContext("2d", { willReadFrequently: true });
  ctx.fillStyle = "rgb(114,114,114)";
  ctx.fillRect(0, 0, S, S);
  ctx.imageSmoothingEnabled = true;
  ctx.imageSmoothingQuality = "low";     // bilinear: closest to OpenCV INTER_LINEAR ("medium"/"high" are worse on big photos)
  ctx.drawImage(bitmap, left, top, nw, nh);
  const px = ctx.getImageData(0, 0, S, S).data;
  const plane = S * S, t = new Float32Array(3 * plane);
  for (let i = 0, p = 0; i < plane; i++, p += 4) {
    t[i] = px[p] / 255; t[i + plane] = px[p + 1] / 255; t[i + 2 * plane] = px[p + 2] / 255;
  }
  return { tensor: new ort.Tensor("float32", t, [1, 3, S, S]), r, left, top, W, H };
}

function iou(a, b) {
  const w = Math.max(0, Math.min(a.x1, b.x1) - Math.max(a.x0, b.x0));
  const h = Math.max(0, Math.min(a.y1, b.y1) - Math.max(a.y0, b.y0));
  const inter = w * h;
  return inter / ((a.x1 - a.x0) * (a.y1 - a.y0) + (b.x1 - b.x0) * (b.y1 - b.y0) - inter + 1e-9);
}

function decode(out, lb) {
  const d = out.data, N = out.dims[2], nc = out.dims[1] - 4;
  const cand = [];
  for (let i = 0; i < N; i++) {
    let best = 0, bc = -1;
    for (let c = 0; c < nc; c++) { const s = d[(4 + c) * N + i]; if (s > best) { best = s; bc = c; } }
    if (best < FLOOR) continue;
    const cx = d[i], cy = d[N + i], w = d[2 * N + i], h = d[3 * N + i];
    cand.push({ x0: cx - w / 2, y0: cy - h / 2, x1: cx + w / 2, y1: cy + h / 2, conf: best, cls: bc });
  }
  cand.sort((a, b) => b.conf - a.conf);
  const kept = [];
  // Greedy NMS processes boxes in descending confidence, so running it once at the 0.10 floor and filtering
  // later by the slider gives exactly the result of running it at the slider's value.
  for (const b of cand) if (!kept.some((k) => k.cls === b.cls && iou(k, b) > IOU)) kept.push(b);
  const clip = (v, hi) => Math.min(Math.max(v, 0), hi);
  return kept.map((b) => ({
    cls: CLASSES[b.cls], conf: b.conf,
    x0: clip((b.x0 - lb.left) / lb.r, lb.W), y0: clip((b.y0 - lb.top) / lb.r, lb.H),
    x1: clip((b.x1 - lb.left) / lb.r, lb.W), y1: clip((b.y1 - lb.top) / lb.r, lb.H),
  }));
}

async function detect(item, name) {
  if (item.dets[name]) return item.dets[name];
  const s = await session(name);
  const lb = letterbox(item.bitmap);
  const t0 = performance.now();
  const out = await s.run({ [s.inputNames[0]]: lb.tensor });
  item.ms[name] = performance.now() - t0;
  item.dets[name] = decode(out[s.outputNames[0]], lb);
  return item.dets[name];
}

// ------------------------------------------------------------------ input
async function addBlobs(entries) {
  const gen = state.gen;
  state.busy += entries.length;
  render();
  for (const { name, blob, gt } of entries) {
    let item;
    try {
      item = { name, gt, dets: {}, ms: {}, bitmap: await createImageBitmap(blob, { imageOrientation: "from-image" }) };
    } catch (e) {
      item = { name, error: "Could not read this file as an image.", dets: {}, ms: {} };
    }
    if (!item.error) {
      try {
        while (!item.dets[state.model]) await detect(item, state.model);   // repeats if the model changed meanwhile
      } catch (e) {
        console.error(e);
        item = { name, error: `Detection failed: ${e.message}`, dets: {}, ms: {} };
      }
    }
    if (gen !== state.gen) return;
    state.items.push(item);
    state.busy -= 1;
    render();
  }
}
async function loadSamples() {
  const labels = await (await fetch("samples/labels.json")).json();     // the dataset's boxes: [class, x0, y0, x1, y1]
  const todo = SAMPLES.filter((f) => !state.items.some((i) => i.name === `sample: ${f}`));
  const entries = await Promise.all(todo.map(async (f) => ({ name: `sample: ${f}`, gt: labels[f],
    blob: await (await fetch(`samples/${f}`)).blob() })));
  await addBlobs(entries);
}

// ------------------------------------------------------------------ output
function visible(item) {
  return (item.dets[state.model] || []).filter((d) => d.conf >= state.thr);
}
function countText(classes) {
  const n = {};
  for (const c of classes) n[c] = (n[c] || 0) + 1;
  return CLASSES.filter((c) => n[c]).map((c) => `${n[c]} ${c}`).join(" · ");
}
function worst(dets) {
  return dets.reduce((w, d) => (!w || SEV_META[SEVERITY[d.cls]].rank > SEV_META[SEVERITY[w.cls]].rank ? d : w), null);
}

function drawCard(item) {
  if (item.error) return el("article", { class: "card result" }, el("h3", { text: item.name }), el("p", { class: "muted", text: item.error }));
  const dets = visible(item);
  // drawn at device resolution so boxes and labels stay sharp; u = one CSS pixel in canvas pixels
  const u = Math.min(window.devicePixelRatio || 1, 3), cssW = Math.min(440, item.bitmap.width);
  const scale = (cssW * u) / item.bitmap.width;
  const c = el("canvas", { width: Math.round(item.bitmap.width * scale), height: Math.round(item.bitmap.height * scale),
    style: `width:${cssW}px`, role: "img", "aria-label": `${item.name}: ${dets.length} detections` });
  const ctx = c.getContext("2d");
  ctx.drawImage(item.bitmap, 0, 0, c.width, c.height);
  if (item.gt && state.showGt) {            // dataset label: white dashes on a dark outline, visible on any belt
    for (const [, x0, y0, x1, y1] of item.gt) {
      const x = x0 * scale, y = y0 * scale, w = (x1 - x0) * scale, h = (y1 - y0) * scale;
      ctx.setLineDash([]); ctx.lineWidth = 3 * u; ctx.strokeStyle = "rgba(0,0,0,0.7)"; ctx.strokeRect(x, y, w, h);
      ctx.setLineDash([5 * u, 4 * u]); ctx.lineWidth = 1.5 * u; ctx.strokeStyle = "#ffffff"; ctx.strokeRect(x, y, w, h);
    }
    ctx.setLineDash([]);
  }
  ctx.font = `600 ${12 * u}px system-ui, -apple-system, Segoe UI, sans-serif`;
  for (const d of dets) {
    const k = CLASSES.indexOf(d.cls), col = CLASS_COLOR[k];
    const x = d.x0 * scale, y = d.y0 * scale, w = (d.x1 - d.x0) * scale, h = (d.y1 - d.y0) * scale;
    ctx.lineWidth = 2 * u; ctx.strokeStyle = col; ctx.strokeRect(x, y, w, h);
    const label = `${d.cls} ${d.conf.toFixed(2)}`;
    const tw = ctx.measureText(label).width + 8 * u, th = 18 * u, ty = y > th ? y - th : y;
    ctx.fillStyle = col; ctx.fillRect(x, ty, tw, th);
    ctx.fillStyle = LABEL_TEXT[k];
    ctx.fillText(label, x + 4 * u, ty + 13 * u);
  }
  const w = worst(dets);
  const verdict = w
    ? chip(SEV_META[SEVERITY[w.cls]], `Damage found: ${w.cls} (${SEV_META[SEVERITY[w.cls]].label.toLowerCase()} severity)`)
    : chip({ cls: "good", icon: "✓" }, "No damage above the threshold");
  const rows = dets.map((d) => el("tr", {},
    el("td", {}, el("span", { class: "sw", style: `background:${CLASS_COLOR[CLASSES.indexOf(d.cls)]}` }), d.cls),
    el("td", { class: "num", text: d.conf.toFixed(2) }),
    el("td", {}, chip(SEV_META[SEVERITY[d.cls]])),
    el("td", { class: "num", text: `${Math.round(((d.x1 - d.x0) * (d.y1 - d.y0)) / (item.bitmap.width * item.bitmap.height) * 100)} %` })));
  const table = dets.length ? el("table", {}, el("thead", {}, el("tr", {}, ...["Class", "Confidence", "Severity", "Area of image"].map((t) => el("th", { text: t })))),
    el("tbody", {}, ...rows)) : null;
  const below = (item.dets[state.model] || []).filter((d) => d.conf < state.thr).length;
  const gtLine = item.gt ? el("p", { class: "small gt", text: item.gt.length
    ? `Dataset label${state.showGt ? " (dashed)" : ""}: ${countText(item.gt.map((b) => b[0]))}`
    : "Dataset label: no damage" }) : null;
  return el("article", { class: "card result" },
    el("div", { class: "card-head" }, el("h3", { text: item.name }), verdict), gtLine, c, table,
    el("p", { class: "muted small", text: `${item.bitmap.width}×${item.bitmap.height} px · ${Math.round(item.ms[state.model] || 0)} ms` +
      (below ? ` · ${below} weaker detection${below > 1 ? "s" : ""} below the threshold` : "") }));
}

function render() {
  $("thrv").textContent = state.thr.toFixed(2);
  const items = state.items.filter((i) => !i.error && i.dets[state.model]);
  $("summary-card").hidden = state.items.length === 0;
  if (items.length) {
    const withDamage = items.filter((i) => visible(i).length);
    const counts = {};
    for (const i of items) for (const d of visible(i)) counts[d.cls] = (counts[d.cls] || 0) + 1;
    const ms = items.reduce((a, i) => a + (i.ms[state.model] || 0), 0) / items.length;
    $("summary").replaceChildren(el("div", { class: "stats" },
      el("div", {}, el("div", { class: "n", text: String(items.length) }), el("div", { class: "l", text: "images" })),
      el("div", {}, el("div", { class: "n", text: String(withDamage.length) }), el("div", { class: "l", text: `with damage at ≥ ${state.thr.toFixed(2)}` })),
      el("div", {}, el("div", { class: "n", text: String(Object.values(counts).reduce((a, b) => a + b, 0)) }), el("div", { class: "l", text: "detections" })),
      el("div", {}, el("div", { class: "n", text: `${Math.round(ms)} ms` }), el("div", { class: "l", text: "per image, on this device" }))),
      el("p", { class: "muted small", text: Object.keys(counts).length
        ? "By class: " + CLASSES.filter((c) => counts[c]).map((c) => `${c} ${counts[c]}`).join(" · ")
        : "No detections at this threshold." }));
  } else {
    $("summary").replaceChildren(el("p", { class: "muted small", text: state.busy ? "Analysing…" : "No image could be analysed." }));
  }
  $("legend").replaceChildren(...CLASSES.map((c, k) => el("span", { class: "chip" }, el("span", { class: "sw", style: `background:${CLASS_COLOR[k]}` }), c)),
    ...(state.showGt && state.items.some((i) => i.gt) ? [el("span", { class: "chip" }, el("span", { class: "sw gt" }), "Dataset label")] : []));
  $("results").replaceChildren(...state.items.filter((i) => i.error || i.dets[state.model]).map(drawCard));
  const n = items.length, ld = state.loadS[state.model];
  $("status").textContent = state.fatal ? state.fatal
    : state.busy ? `Analysing… ${state.busy} image${state.busy > 1 ? "s" : ""} left`
    : ld === undefined ? `Loading the ${state.model} model…`
    : n ? `${n} image${n > 1 ? "s" : ""} analysed with the ${state.model} model, on this device.`
    : `The ${state.model} model loaded in ${ld.toFixed(1)} s. Images are processed on this device.`;
}

function downloadCsv() {
  const q = (v) => {                       // file names are untrusted: quote, and defuse spreadsheet formulas
    const t = String(v);
    return `"${(/^[=+\-@\t\r]/.test(t) ? "'" + t : t).replaceAll('"', '""')}"`;
  };
  const rows = [["image", "model", "threshold", "class", "confidence", "severity", "x0", "y0", "x1", "y1", "dataset_label"]];
  for (const i of state.items) {
    if (i.error || !i.dets[state.model]) continue;
    const gt = i.gt ? (i.gt.length ? countText(i.gt.map((b) => b[0])) : "no damage") : "";
    const dets = visible(i);
    if (!dets.length) rows.push([i.name, state.model, state.thr, "", "", "", "", "", "", "", gt]);
    for (const d of dets) rows.push([i.name, state.model, state.thr, d.cls, d.conf.toFixed(3), SEVERITY[d.cls],
      ...[d.x0, d.y0, d.x1, d.y1].map((v) => v.toFixed(1)), gt]);
  }
  const a = el("a", { href: URL.createObjectURL(new Blob([rows.map((r) => r.map(q).join(",")).join("\r\n")], { type: "text/csv" })),
    download: `belt_inspection_${state.model}.csv` });
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}

// ------------------------------------------------------------------ wiring
$("drop").addEventListener("click", () => $("file").click());
$("drop").addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") $("file").click(); });
$("file").addEventListener("change", (e) => addBlobs([...e.target.files].map((f) => ({ name: f.name, blob: f }))));
for (const ev of ["dragover", "dragenter"]) $("drop").addEventListener(ev, (e) => { e.preventDefault(); $("drop").classList.add("over"); });
$("drop").addEventListener("dragleave", () => $("drop").classList.remove("over"));
$("drop").addEventListener("drop", (e) => {
  e.preventDefault(); $("drop").classList.remove("over");
  addBlobs([...e.dataTransfer.files].filter((f) => f.type.startsWith("image/")).map((f) => ({ name: f.name, blob: f })));
});
$("samples").addEventListener("click", () => loadSamples().catch(console.error));
$("clear").addEventListener("click", () => { state.gen += 1; state.items = []; state.busy = 0; $("file").value = ""; render(); });
$("csv").addEventListener("click", downloadCsv);
$("gt").addEventListener("change", (e) => { state.showGt = e.target.checked; render(); });
$("thr").addEventListener("input", (e) => { state.thr = Number(e.target.value); render(); });
for (const r of document.querySelectorAll("input[name=model]")) {
  r.addEventListener("change", async (e) => {
    state.model = e.target.value;
    const gen = state.gen, model = state.model;
    const todo = state.items.filter((i) => !i.error && !i.dets[model]);
    state.busy += todo.length;
    render();
    session(model).catch(() => {});        // start loading even when there is nothing to re-run yet
    for (const i of todo) {
      try { await detect(i, model); } catch (err) { console.error(err); i.error = `Detection failed: ${err.message}`; }
      if (gen !== state.gen) return;
      state.busy -= 1;
      render();
    }
  });
}
state.ready = session("deployed").catch((e) => {
  state.fatal = `Could not load the model in this browser (${e.message}). WebAssembly is required.`;
  render();
  throw e;
});
render();
