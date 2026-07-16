"use strict";

let DATA = null;
let OFFERINGS = [];
let sortKey = "hourly_usd";
let sortDir = 1; // 1 asc, -1 desc
const charts = {};

const WL_LABELS = {
  "small-models": "Small models",
  "lora-fine-tuning": "LoRA fine-tuning",
  "training": "Training",
  "distributed-training": "Distributed training",
  "batch-inference": "Batch inference",
  "realtime-inference": "Real-time inference",
};

const $ = (s) => document.querySelector(s);
const fmt$ = (v) => (v == null ? null : "$" + v.toFixed(v < 1 ? 3 : 2));

// ---- recommendation engine ------------------------------------------------
// Each recommender filters the catalogue to offerings that meet the workload's
// requirements, then picks the cheapest. Reasoning is generated from the pick.
const RECS = [
  {
    key: "small",
    title: "Running small open-source models",
    note: "Models up to ~13B fit comfortably in 24 GB. We want the cheapest capable GPU.",
    filter: (o) => o.gpu_memory_gb >= 16 && o.gpu_memory_gb <= 48,
    rank: (o) => o.hourly_usd,
    metric: (o) => `${fmt$(o.hourly_usd)}/GPU-hr · ${o.gpu_memory_gb} GB`,
  },
  {
    key: "serve70b",
    title: "Serving a 70B model",
    note: "A quantized 70B model needs ≈80 GB of GPU memory in a single addressable pool. Cheapest config with ≥80 GB total wins.",
    filter: (o) => o.total_gpu_memory_gb >= 80,
    rank: (o) => o.hourly_usd_total,
    metric: (o) => `${fmt$(o.hourly_usd_total)}/node-hr · ${o.total_gpu_memory_gb} GB total`,
  },
  {
    key: "lora",
    title: "LoRA fine-tuning",
    note: "LoRA/QLoRA fine-tuning of mid-size models needs ≥24 GB. Prioritise cost per GPU-hour.",
    filter: (o) => o.workloads.includes("lora-fine-tuning"),
    rank: (o) => o.hourly_usd,
    metric: (o) => `${fmt$(o.hourly_usd)}/GPU-hr · ${o.gpu_memory_gb} GB`,
  },
  {
    key: "distributed",
    title: "Large-scale distributed training",
    note: "Needs many interconnected data-centre GPUs (≥4× A100/H100/B200 class). Cheapest full node by total price.",
    filter: (o) => o.workloads.includes("distributed-training"),
    rank: (o) => o.hourly_usd_total,
    metric: (o) => `${fmt$(o.hourly_usd_total)}/node-hr · ${o.gpu_count}× ${o.gpu_model}`,
  },
  {
    key: "batch",
    title: "Cheap batch inference",
    note: "Latency-insensitive; interruptible/spot is fine. We take the absolute lowest GPU-hour price.",
    filter: (o) => true,
    rank: (o) => (o.spot_usd != null ? Math.min(o.spot_usd, o.hourly_usd) : o.hourly_usd),
    metric: (o) => {
      const p = o.spot_usd != null ? Math.min(o.spot_usd, o.hourly_usd) : o.hourly_usd;
      return `${fmt$(p)}/GPU-hr${o.spot_usd != null ? " (spot)" : ""}`;
    },
  },
  {
    key: "experiment",
    title: "Short-term experimentation",
    note: "Quick throwaway runs — want the cheapest on-demand, no-commitment GPU that can hold a real model (≥24 GB).",
    filter: (o) => o.billing_models.includes("on-demand") && o.gpu_memory_gb >= 24,
    rank: (o) => o.hourly_usd,
    metric: (o) => `${fmt$(o.hourly_usd)}/GPU-hr · ${o.gpu_model}`,
  },
];

function pickRec(rec) {
  const cands = OFFERINGS.filter(rec.filter).sort((a, b) => rec.rank(a) - rec.rank(b));
  return cands.length ? { rec, pick: cands[0], runnerUp: cands[1] } : null;
}

// ---- boot -----------------------------------------------------------------
async function boot() {
  DATA = await (await fetch("data/gpus.json")).json();
  OFFERINGS = DATA.offerings;
  renderMeta();
  populateFilters();
  renderRecs();
  renderCharts();
  bindUI();
  render();
}

function renderMeta() {
  const d = new Date(DATA.last_collected);
  $("#meta").innerHTML =
    `<b>${DATA.offering_count}</b> offerings · <b>${DATA.provider_count}</b> providers · ` +
    `<b>${DATA.published_count}</b> published, <b>${DATA.estimated_count}</b> estimated · ` +
    `last collected <b>${d.toISOString().slice(0, 16).replace("T", " ")} UTC</b>`;
  $("#footer-meta").textContent =
    `Data collected ${d.toISOString().slice(0, 10)}. Prices change frequently — re-run build_dataset.py to refresh. ` +
    `Scraped from public sources only; no accounts or API keys used.`;
  const sl = $("#sources-list");
  sl.innerHTML = DATA.sources
    .map((s) => `<li><a href="${s.url}" target="_blank" rel="noopener">${s.name}</a></li>`)
    .join("");
}

function populateFilters() {
  const uniq = (k) => [...new Set(OFFERINGS.map((o) => o[k]))].sort();
  fill("#f-provider", uniq("provider"));
  fill("#f-gpu", uniq("gpu_model"));
  const wls = [...new Set(OFFERINGS.flatMap((o) => o.workloads))].sort();
  fill("#f-workload", wls, (w) => WL_LABELS[w] || w);
  const bills = [...new Set(OFFERINGS.flatMap((o) => o.billing_models))].sort();
  fill("#f-billing", bills);
}
function fill(sel, vals, label = (v) => v) {
  const el = $(sel);
  vals.forEach((v) => {
    const o = document.createElement("option");
    o.value = v;
    o.textContent = label(v);
    el.appendChild(o);
  });
}

// ---- recommendations UI ---------------------------------------------------
function renderRecs() {
  const grid = $("#rec-grid");
  grid.innerHTML = "";
  RECS.forEach((rec) => {
    const r = pickRec(rec);
    if (!r) return;
    const { pick } = r;
    const card = document.createElement("div");
    card.className = "rec-card";
    card.innerHTML =
      `<div class="wl">${rec.title}</div>` +
      `<div class="pick">${pick.provider} · ${pick.gpu_model}</div>` +
      `<div class="prov">${pick.product}</div>` +
      `<div class="price">${rec.metric(pick)}</div>`;
    card.addEventListener("click", () => showRecModal(r));
    grid.appendChild(card);
  });
}

function showRecModal(r) {
  const { rec, pick, runnerUp } = r;
  const est = pick.provenance === "estimated";
  const bullets = [
    rec.note,
    `<b>${pick.provider} ${pick.product}</b> — ${pick.gpu_count}× ${pick.gpu_model}, ${pick.gpu_memory_gb} GB each (${pick.total_gpu_memory_gb} GB total), ${pick.vcpus} vCPU, ${pick.system_memory_gb} GB RAM.`,
    `Selected because it is the lowest-cost offering that meets the requirement (${rec.metric(pick)}).`,
    runnerUp ? `Next best: ${runnerUp.provider} ${runnerUp.gpu_model} at ${rec.metric(runnerUp)}.` : "",
    est ? "⚠ This price is an <b>estimated / fallback</b> value from public docs — verify before committing." : "Price mirrors the provider's published pricing.",
  ].filter(Boolean);
  $("#modal-body").innerHTML =
    `<h3>${rec.title}</h3>` +
    `<div class="m-price">${rec.metric(pick)}</div>` +
    `<ul>${bullets.map((b) => `<li>${b}</li>`).join("")}</ul>` +
    `<p><a class="src" href="${pick.source_url}" target="_blank" rel="noopener">Source: ${pick.source_name} ↗</a></p>`;
  $("#modal").classList.remove("hidden");
}

// ---- charts ---------------------------------------------------------------
function cheapestBy(key) {
  const m = {};
  OFFERINGS.forEach((o) => {
    const k = o[key];
    if (m[k] == null || o.hourly_usd < m[k]) m[k] = o.hourly_usd;
  });
  return m;
}
const CHART_OPTS = (unit) => ({
  indexAxis: "y",
  responsive: true,
  plugins: {
    legend: { display: false },
    tooltip: { callbacks: { label: (c) => `${unit}${c.parsed.x}` } },
  },
  scales: {
    x: { ticks: { color: "#97a3b8" }, grid: { color: "#2b3448" } },
    y: { ticks: { color: "#e7ecf4" }, grid: { display: false } },
  },
});

function bar(id, labels, values, unit) {
  charts[id] = new Chart($("#" + id), {
    type: "bar",
    data: { labels, datasets: [{ data: values, backgroundColor: "#5b9bff", borderRadius: 4 }] },
    options: CHART_OPTS(unit),
  });
}

function renderCharts() {
  // by GPU model (cheapest hourly), sorted asc
  const byModel = cheapestBy("gpu_model");
  const modelEntries = Object.entries(byModel).sort((a, b) => a[1] - b[1]);
  bar("chart-model", modelEntries.map((e) => e[0]), modelEntries.map((e) => +e[1].toFixed(3)), "$");

  // by provider (cheapest hourly)
  const byProv = cheapestBy("provider");
  const provEntries = Object.entries(byProv).sort((a, b) => a[1] - b[1]);
  bar("chart-provider", provEntries.map((e) => e[0]), provEntries.map((e) => +e[1].toFixed(3)), "$");

  // best $/GB by GPU model
  const perGB = {};
  OFFERINGS.forEach((o) => {
    if (perGB[o.gpu_model] == null || o.usd_per_gpu_gb < perGB[o.gpu_model]) perGB[o.gpu_model] = o.usd_per_gpu_gb;
  });
  const gbEntries = Object.entries(perGB).sort((a, b) => a[1] - b[1]);
  bar("chart-pergb", gbEntries.map((e) => e[0]), gbEntries.map((e) => +e[1].toFixed(4)), "$");

  // single vs multi-GPU average $/GPU-hr per provider
  const groups = {};
  OFFERINGS.forEach((o) => {
    const g = (groups[o.provider] = groups[o.provider] || { single: [], multi: [] });
    (o.gpu_count > 1 ? g.multi : g.single).push(o.hourly_usd);
  });
  const provs = Object.keys(groups).sort();
  const avg = (a) => (a.length ? a.reduce((x, y) => x + y, 0) / a.length : null);
  charts["chart-multi"] = new Chart($("#chart-multi"), {
    type: "bar",
    data: {
      labels: provs,
      datasets: [
        { label: "Single-GPU", data: provs.map((p) => round3(avg(groups[p].single))), backgroundColor: "#3fb950", borderRadius: 4 },
        { label: "Multi-GPU (per GPU)", data: provs.map((p) => round3(avg(groups[p].multi))), backgroundColor: "#e3a008", borderRadius: 4 },
      ],
    },
    options: {
      indexAxis: "y",
      responsive: true,
      plugins: { legend: { labels: { color: "#e7ecf4" } } },
      scales: { x: { ticks: { color: "#97a3b8" }, grid: { color: "#2b3448" } }, y: { ticks: { color: "#e7ecf4" }, grid: { display: false } } },
    },
  });
}
const round3 = (v) => (v == null ? null : +v.toFixed(3));

// ---- table ----------------------------------------------------------------
function applyFilters(rows) {
  const q = $("#f-search").value.trim().toLowerCase();
  const prov = $("#f-provider").value;
  const gpu = $("#f-gpu").value;
  const wl = $("#f-workload").value;
  const bill = $("#f-billing").value;
  const mem = parseFloat($("#f-mem").value) || 0;
  const price = parseFloat($("#f-price").value) || Infinity;
  return rows.filter((o) => {
    if (q && !`${o.provider} ${o.product} ${o.gpu_model}`.toLowerCase().includes(q)) return false;
    if (prov && o.provider !== prov) return false;
    if (gpu && o.gpu_model !== gpu) return false;
    if (wl && !o.workloads.includes(wl)) return false;
    if (bill && !o.billing_models.includes(bill)) return false;
    if (mem && o.gpu_memory_gb < mem) return false;
    if (o.hourly_usd > price) return false;
    return true;
  });
}

function visibleRows() {
  const rows = applyFilters(OFFERINGS.slice());
  rows.sort((a, b) => {
    let x = a[sortKey], y = b[sortKey];
    if (Array.isArray(x)) x = x.join();
    if (Array.isArray(y)) y = y.join();
    if (x == null) return 1;
    if (y == null) return -1;
    if (typeof x === "string") return x.localeCompare(y) * sortDir;
    return (x - y) * sortDir;
  });
  return rows;
}

function render() {
  const rows = visibleRows();
  $("#count").textContent = `${rows.length} of ${OFFERINGS.length} offerings`;
  const tb = $("#tbody");
  tb.innerHTML = "";
  rows.forEach((o) => tb.appendChild(rowEl(o)));
}

function rowEl(o) {
  const tr = document.createElement("tr");
  if (o.provenance === "estimated") tr.className = "est";
  const dot = `<span class="pdot pdot-${o.provenance === "published" ? "pub" : "est"}" title="${o.provenance}"></span>`;
  const norm = o.price_normalized ? ` <span class="pnorm" title="per-GPU price derived from a multi-GPU node">÷</span>` : "";
  const chips = (a) => a.map((x) => `<span class="chip">${WL_LABELS[x] || x}</span>`).join("");
  const na = (v, suf = "") => (v == null ? `<span class="na">—</span>` : v + suf);
  tr.innerHTML =
    `<td>${dot} ${o.provider}</td>` +
    `<td>${o.product}</td>` +
    `<td>${o.gpu_model}</td>` +
    `<td class="num">${o.gpu_count}</td>` +
    `<td class="num">${o.gpu_memory_gb} GB</td>` +
    `<td class="num">${o.total_gpu_memory_gb} GB</td>` +
    `<td class="num">${na(o.vcpus)}</td>` +
    `<td class="num">${na(o.system_memory_gb, " GB")}</td>` +
    `<td class="num price-cell">${fmt$(o.hourly_usd)}${norm}</td>` +
    `<td class="num">${fmt$(o.hourly_usd_total)}</td>` +
    `<td class="num">${o.spot_usd == null ? '<span class="na">—</span>' : fmt$(o.spot_usd)}</td>` +
    `<td class="num">${fmt$(o.usd_per_gpu_gb)}</td>` +
    `<td>${o.region}</td>` +
    `<td>${chips(o.billing_models)}</td>` +
    `<td>${chips(o.workloads)}</td>` +
    `<td><a class="src" href="${o.source_url}" target="_blank" rel="noopener" title="${o.source_name}">source ↗</a></td>`;
  return tr;
}

// ---- export ---------------------------------------------------------------
const CSV_COLS = ["provider", "product", "gpu_model", "gpu_count", "gpu_memory_gb", "total_gpu_memory_gb",
  "vcpus", "system_memory_gb", "hourly_usd", "hourly_usd_total", "spot_usd", "usd_per_gpu_gb", "region",
  "billing_models", "workloads", "provenance", "price_normalized", "source_url"];

function toCSV(rows) {
  const esc = (v) => {
    if (v == null) return "";
    if (Array.isArray(v)) v = v.join("; ");
    v = String(v);
    return /[",\n]/.test(v) ? `"${v.replace(/"/g, '""')}"` : v;
  };
  const head = `# cloud GPU pricing — collected ${DATA.last_collected}, exported ${new Date().toISOString()}\n`;
  return head + [CSV_COLS.join(","), ...rows.map((o) => CSV_COLS.map((c) => esc(o[c])).join(","))].join("\n");
}

function download(name, text, type) {
  const url = URL.createObjectURL(new Blob([text], { type }));
  const a = document.createElement("a");
  a.href = url; a.download = name; a.click();
  URL.revokeObjectURL(url);
}

// ---- UI wiring ------------------------------------------------------------
function bindUI() {
  ["f-search", "f-provider", "f-gpu", "f-workload", "f-billing", "f-mem", "f-price"].forEach((id) =>
    $("#" + id).addEventListener("input", render)
  );
  $("#f-reset").addEventListener("click", () => {
    ["f-search", "f-provider", "f-gpu", "f-workload", "f-billing", "f-mem", "f-price"].forEach((id) => ($("#" + id).value = ""));
    render();
  });
  $("#hl-est").addEventListener("change", (e) => document.body.classList.toggle("hl-est", e.target.checked));
  document.querySelectorAll("thead th[data-k]").forEach((th) =>
    th.addEventListener("click", () => {
      const k = th.dataset.k;
      if (sortKey === k) sortDir *= -1;
      else { sortKey = k; sortDir = 1; }
      document.querySelectorAll("thead th").forEach((h) => (h.textContent = h.textContent.replace(/ [▲▼]$/, "")));
      th.textContent += sortDir === 1 ? " ▲" : " ▼";
      render();
    })
  );
  $("#exp-csv").addEventListener("click", () => download("gpu-pricing.csv", toCSV(visibleRows()), "text/csv"));
  $("#exp-json").addEventListener("click", () =>
    download("gpu-pricing.json", JSON.stringify({ ...DATA, offerings: visibleRows() }, null, 2), "application/json")
  );
  $("#modal-x").addEventListener("click", () => $("#modal").classList.add("hidden"));
  $("#modal").addEventListener("click", (e) => { if (e.target.id === "modal") $("#modal").classList.add("hidden"); });
}

boot();
