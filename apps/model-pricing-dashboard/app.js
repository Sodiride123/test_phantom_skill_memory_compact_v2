/* AI Model Pricing Dashboard — loads data/models.json, renders table, filters,
   charts and best-value picks. Pure vanilla JS + Chart.js (no build step). */

const PROVIDER_CLASS = {
  OpenAI: "p-openai",
  Anthropic: "p-anthropic",
  Google: "p-google",
  xAI: "p-xai",
  Mistral: "p-mistral",
  DeepSeek: "p-deepseek",
};
const PROVIDER_COLOR = {
  OpenAI: "#10a37f",
  Anthropic: "#d18a5b",
  Google: "#4285f4",
  xAI: "#b06cf0",
  Mistral: "#ff7000",
  DeepSeek: "#4d6bfe",
};

let DATA = null;
let MODELS = [];
let sortKey = "output_price";
let sortDir = "asc";
let chart = null;

// -------------------------------------------------------------------------
init();

async function init() {
  const res = await fetch("data/models.json");
  DATA = await res.json();
  MODELS = DATA.models;

  renderMeta();
  populateFilters();
  renderBestValue();
  renderChart();
  attachEvents();
  render();
}

function renderMeta() {
  const d = new Date(DATA.last_collected);
  document.getElementById("last-collected").textContent =
    "Last collected: " + d.toISOString().slice(0, 10);
  document.getElementById("counts").textContent =
    `${DATA.model_count} models · ${DATA.provider_count} providers`;
}

// ---------------- Filters ----------------
function populateFilters() {
  const providers = [...new Set(MODELS.map((m) => m.provider))].sort();
  const modalities = [...new Set(MODELS.flatMap((m) => m.modalities))].sort();
  const tags = [...new Set(MODELS.flatMap((m) => m.tags))].sort();

  fill("f-provider", providers);
  fill("f-modality", modalities);
  fill("f-tag", tags);
}
function fill(id, values) {
  const sel = document.getElementById(id);
  values.forEach((v) => {
    const o = document.createElement("option");
    o.value = v;
    o.textContent = v;
    sel.appendChild(o);
  });
}

function currentFilters() {
  return {
    provider: val("f-provider"),
    modality: val("f-modality"),
    tag: val("f-tag"),
    context: parseInt(val("f-context"), 10),
    tier: val("f-tier"),
    search: val("f-search").toLowerCase().trim(),
  };
}
const val = (id) => document.getElementById(id).value;

function tierOf(m) {
  if (m.output_price < 1) return "budget";
  if (m.output_price <= 10) return "mid";
  return "premium";
}

function applyFilters(rows) {
  const f = currentFilters();
  return rows.filter((m) => {
    if (f.provider && m.provider !== f.provider) return false;
    if (f.modality && !m.modalities.includes(f.modality)) return false;
    if (f.tag && !m.tags.includes(f.tag)) return false;
    if (f.context && m.context_window < f.context) return false;
    if (f.tier && tierOf(m) !== f.tier) return false;
    if (f.search && !m.name.toLowerCase().includes(f.search)) return false;
    return true;
  });
}

// ---------------- Table ----------------
// The currently filtered + sorted rows (what the user sees / what exports use).
function visibleRows() {
  return applyFilters(MODELS.slice()).sort(comparator);
}

function render() {
  const rows = visibleRows();

  const tb = document.querySelector("#model-table tbody");
  tb.innerHTML = "";
  rows.forEach((m) => tb.appendChild(rowEl(m)));

  document.getElementById("row-count").textContent =
    `Showing ${rows.length} of ${MODELS.length} models`;
  updateSortIndicators();
}

function comparator(a, b) {
  let x = a[sortKey];
  let y = b[sortKey];
  if (Array.isArray(x)) x = x.join(",");
  if (Array.isArray(y)) y = y.join(",");
  if (x === null || x === undefined) x = sortDir === "asc" ? Infinity : -Infinity;
  if (y === null || y === undefined) y = sortDir === "asc" ? Infinity : -Infinity;
  if (typeof x === "string" && typeof y === "string") {
    return sortDir === "asc" ? x.localeCompare(y) : y.localeCompare(x);
  }
  return sortDir === "asc" ? x - y : y - x;
}

// Per-cell provenance marker. Price cells that couldn't be re-scraped are
// flagged "stale" (last-known value kept); otherwise a field's provenance is
// aggregator (scraped) or fallback (public docs).
function provMark(field, m) {
  const priceField =
    field === "input_price" || field === "cached_price" || field === "output_price";
  if (priceField && m.price_stale) {
    return { kind: "stale", title: "Stale — last-known price kept (live scrape didn't match)" };
  }
  if (m.provenance[field] === "aggregator") {
    return { kind: "agg", title: "Scraped from public aggregator (aipricing.guru)" };
  }
  return { kind: "fb", title: "Fallback — public provider docs / model cards" };
}

function rowEl(m) {
  const tr = document.createElement("tr");
  const price = (v) => (v === null || v === undefined ? "—" : "$" + v.toFixed(2));
  const ctx =
    m.context_window >= 1000000
      ? m.context_window / 1000000 + "M"
      : Math.round(m.context_window / 1000) + "K";
  const cls = PROVIDER_CLASS[m.provider] || "";
  if (m.price_stale) tr.classList.add("row-stale");

  // Provenance-bearing cell: adds a coloured dot + tooltip + cell-<kind> class.
  const cell = (field, inner, extra = "") => {
    const mk = provMark(field, m);
    return `<td class="cell-${mk.kind}${extra ? " " + extra : ""}" title="${mk.title}">${inner}<span class="pdot pdot-${mk.kind}"></span></td>`;
  };

  tr.innerHTML = `
    <td><span class="prov-badge ${cls}">${m.provider}</span></td>
    <td><b>${m.name}</b><br /><span class="prov-dot">${m.family}</span></td>
    ${cell("input_price", price(m.input_price), "num")}
    ${cell("cached_price", price(m.cached_price), "num")}
    ${cell("output_price", price(m.output_price), "num")}
    ${cell("context_window", ctx, "num")}
    ${cell("modalities", m.modalities.map((x) => `<span class="pill">${x}</span>`).join(""))}
    ${cell("release_date", m.release_date)}
    ${cell("tags", m.tags.map((x) => `<span class="pill">${x}</span>`).join(""))}`;
  return tr;
}

function updateSortIndicators() {
  document.querySelectorAll("#model-table th").forEach((th) => {
    th.classList.remove("sort-asc", "sort-desc");
    if (th.dataset.key === sortKey)
      th.classList.add(sortDir === "asc" ? "sort-asc" : "sort-desc");
  });
}

// ---------------- Best value ----------------
function renderBestValue() {
  const picks = [
    {
      label: "Coding",
      why: "Lowest output price among coding-tagged models",
      pool: MODELS.filter((m) => m.tags.includes("coding")),
      metric: (m) => m.output_price,
    },
    {
      label: "Long-context analysis",
      why: "Largest context window, tie-break on price",
      pool: MODELS.filter((m) => m.tags.includes("long-context")),
      metric: (m) => -m.context_window * 1e6 + m.output_price,
    },
    {
      label: "Cheap summarization",
      why: "Lowest blended price (heavy input, light output)",
      pool: MODELS,
      metric: (m) => m.input_price * 0.9 + m.output_price * 0.1,
    },
    {
      label: "Multimodal",
      why: "Cheapest model handling vision/audio",
      pool: MODELS.filter(
        (m) => m.modalities.includes("vision") || m.modalities.includes("audio")
      ),
      metric: (m) => m.input_price + m.output_price,
    },
  ];

  const wrap = document.getElementById("best-cards");
  wrap.innerHTML = "";
  picks.forEach((p) => {
    const best = p.pool.slice().sort((a, b) => p.metric(a) - p.metric(b))[0];
    if (!best) return;
    const ctx =
      best.context_window >= 1000000
        ? best.context_window / 1000000 + "M"
        : Math.round(best.context_window / 1000) + "K";
    const card = document.createElement("div");
    card.className = "card";
    card.innerHTML = `
      <div class="label">${p.label}</div>
      <div class="model">${best.name}</div>
      <div class="prov">${best.provider} · ${ctx} context</div>
      <div class="price">$${best.input_price.toFixed(2)} in / $${best.output_price.toFixed(
      2
    )} out per 1M</div>
      <div class="why">${p.why}</div>`;
    wrap.appendChild(card);
  });
}

// ---------------- Chart ----------------
function renderChart() {
  const metric = val("chart-metric");
  const view = val("chart-view");
  const ctx = document.getElementById("price-chart").getContext("2d");

  let labels, values, colors, title;

  const metricFn = (m) => {
    if (metric === "blended") return (m.input_price + m.output_price) / 2;
    return m[metric];
  };

  if (view === "provider-avg") {
    const provs = [...new Set(MODELS.map((m) => m.provider))];
    labels = provs;
    values = provs.map((p) => {
      const list = MODELS.filter((m) => m.provider === p).map(metricFn);
      return +(list.reduce((a, b) => a + b, 0) / list.length).toFixed(2);
    });
    colors = provs.map((p) => PROVIDER_COLOR[p]);
    title = "Average " + metricLabel(metric) + " by provider ($/1M)";
  } else {
    const sorted = MODELS.slice().sort((a, b) => metricFn(a) - metricFn(b)).slice(0, 15);
    labels = sorted.map((m) => m.name);
    values = sorted.map((m) => +metricFn(m).toFixed(2));
    colors = sorted.map((m) => PROVIDER_COLOR[m.provider]);
    title = "Cheapest 15 models by " + metricLabel(metric) + " ($/1M)";
  }

  if (chart) chart.destroy();
  chart = new Chart(ctx, {
    type: "bar",
    data: {
      labels,
      datasets: [{ label: metricLabel(metric) + " $/1M", data: values, backgroundColor: colors }],
    },
    options: {
      indexAxis: "y",
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        title: { display: true, text: title, color: "#e6ebf5", font: { size: 15 } },
      },
      scales: {
        x: { ticks: { color: "#94a2bd" }, grid: { color: "#2a3550" } },
        y: { ticks: { color: "#e6ebf5" }, grid: { color: "#2a3550" } },
      },
    },
  });
}
function metricLabel(m) {
  return m === "output_price" ? "output price" : m === "input_price" ? "input price" : "blended price";
}

// ---------------- Export ----------------
const EXPORT_COLUMNS = [
  "provider", "name", "family",
  "input_price", "cached_price", "output_price",
  "context_window", "modalities", "release_date", "tags",
];

function csvCell(v) {
  if (v === null || v === undefined) return "";
  if (Array.isArray(v)) v = v.join("; ");
  const s = String(v);
  // Quote if it contains comma, quote, or newline; escape embedded quotes.
  return /[",\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
}

function toCSV(rows) {
  const header = [...EXPORT_COLUMNS, "price_provenance", "price_stale"];
  const lines = [
    `# AI model pricing — last_collected: ${DATA.last_collected}; exported: ${new Date().toISOString()}`,
    `# prices in ${DATA.price_unit || "USD per 1M tokens"}; rows reflect active filters + sort`,
    header.join(","),
  ];
  rows.forEach((m) => {
    const cells = EXPORT_COLUMNS.map((c) => csvCell(m[c]));
    cells.push(csvCell(m.provenance ? m.provenance.output_price : ""));
    cells.push(csvCell(m.price_stale ? "true" : "false"));
    lines.push(cells.join(","));
  });
  return lines.join("\n");
}

function downloadBlob(filename, text, type) {
  const blob = new Blob([text], { type });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

function stamp() {
  return (DATA.last_collected || new Date().toISOString()).slice(0, 10);
}

function exportCSV() {
  const rows = visibleRows();
  downloadBlob(`ai-model-pricing-${stamp()}.csv`, toCSV(rows), "text/csv;charset=utf-8");
}

function exportJSON() {
  const rows = visibleRows();
  const payload = {
    last_collected: DATA.last_collected,
    exported_at: new Date().toISOString(),
    price_unit: DATA.price_unit,
    row_count: rows.length,
    models: rows,
  };
  downloadBlob(
    `ai-model-pricing-${stamp()}.json`,
    JSON.stringify(payload, null, 2),
    "application/json"
  );
}

// ---------------- Events ----------------
function attachEvents() {
  document.querySelectorAll("#model-table th").forEach((th) => {
    th.addEventListener("click", () => {
      const key = th.dataset.key;
      if (sortKey === key) sortDir = sortDir === "asc" ? "desc" : "asc";
      else {
        sortKey = key;
        sortDir = ["input_price", "cached_price", "output_price", "context_window"].includes(key)
          ? "asc"
          : "asc";
      }
      render();
    });
  });

  ["f-provider", "f-modality", "f-tag", "f-context", "f-tier", "f-search"].forEach((id) =>
    document.getElementById(id).addEventListener("input", render)
  );
  document.getElementById("reset-filters").addEventListener("click", () => {
    ["f-provider", "f-modality", "f-tag", "f-tier", "f-search"].forEach(
      (id) => (document.getElementById(id).value = "")
    );
    document.getElementById("f-context").value = "0";
    render();
  });

  ["chart-metric", "chart-view"].forEach((id) =>
    document.getElementById(id).addEventListener("change", renderChart)
  );

  document.getElementById("hl-fallback").addEventListener("change", (e) => {
    document.body.classList.toggle("hl-fallback", e.target.checked);
  });

  document.getElementById("export-csv").addEventListener("click", exportCSV);
  document.getElementById("export-json").addEventListener("click", exportJSON);
}
