# AI Model Pricing & Capabilities Dashboard

An interactive, **fully local** dashboard comparing the latest AI model pricing
and capabilities across six major providers: **OpenAI, Anthropic, Google Gemini,
xAI, Mistral, and DeepSeek**.

![dashboard screenshot](screenshot.png)

## What it shows

- **Sortable comparison table** — click any column header to sort (provider,
  model, input/cached/output price, context window, modalities, release date,
  tags). Click again to reverse.
- **Filters** — provider, modality, suitability tag, minimum context window,
  pricing tier (budget / mid / premium), and a name search.
- **Price-comparison charts** (Chart.js) — cheapest 15 models or per-provider
  averages, by input / output / blended price.
- **"Best value" picks** — recommended models for coding, long-context
  analysis, cheap summarization, and multimodal tasks.
- **Visible "last collected" timestamp** and per-field provenance badges.

All prices are normalized to **USD per 1,000,000 tokens**.

## Run it locally

No build step, no dependencies to install. Just serve the folder statically:

```bash
cd apps/model-pricing-dashboard
python -m http.server 8000
```

Then open <http://localhost:8000> in a browser.

(Chart.js loads from a CDN, so keep an internet connection for the charts. The
data itself is served from the local `data/models.json`.)

## Refresh the data

**Live refresh (recommended)** — scrape fresh prices from the public aggregator
and regenerate `data/models.json` with a new `last_collected` timestamp:

```bash
python scrape_pricing.py            # scrape + write data/models.json
python scrape_pricing.py --dry-run  # scrape + report, don't write
```

`scrape_pricing.py` fetches each provider's aggregator page via Tavily
`extract(extract_depth="advanced")`, parses the pricing tables (columns are
located by header name, since some pages omit the "Tier" column), and updates
the price fields on the catalog defined in `build_dataset.py`. Capability fields
(context / modalities / release / tags) stay hand-maintained and keep their
`fallback` provenance.

If a model can't be matched in the live table (name drift, row removed,
transient fetch error), its **last-known price is kept** and it is flagged
`price_stale: true` with pricing provenance downgraded to `fallback` — the
dashboard keeps showing the row rather than dropping it. A `refresh` block in
the JSON records how many models matched vs. were kept stale.

**Offline baseline** — regenerate from the hand-encoded values only (no network):

```bash
python build_dataset.py
```

**Automated weekly refresh** — a cron job (`weekly-pricing-refresh`, schedule
`0 9 * * 1` — Mondays 09:00 local) runs `scrape_pricing.py` and posts a short
summary to Slack, so the snapshot and `last_collected` timestamp stay current
without manual intervention. Manage it with `python tools/cron.py list|show|disable weekly-pricing-refresh`.

## Data sources & provenance

Every model row carries a `provenance` map so you can see which fields are fresh
scrapes vs. reasonable fallbacks.

### Pricing — `aggregator` (scraped)

Input / cached / output token prices were scraped from the public aggregator
**[aipricing.guru](https://www.aipricing.guru/)** (server-rendered HTML pricing
tables, "last synced" 2026-07-12…15), using Tavily `extract(extract_depth="advanced")`
and cross-checked against public web-search results. Pages used:

| Provider  | Source page |
|-----------|-------------|
| OpenAI    | `aipricing.guru/openai-pricing` |
| Anthropic | `aipricing.guru/anthropic-pricing` |
| Google    | `aipricing.guru/google-ai-pricing` |
| xAI       | `aipricing.guru/xai-pricing` |
| Mistral   | `aipricing.guru/mistral-pricing` |
| DeepSeek  | `aipricing.guru/deepseek-pricing` |

### Capabilities — `fallback` (public docs)

Context window, modalities, release date and suitability tags are **not** listed
in the aggregator price tables, so they were filled from **public provider docs
and model cards** as reasonable fallback values. These are flagged `fallback` in
the dataset and with an amber badge in the UI.

### Sources that could NOT be reliably scraped

The **official provider pricing pages** are JavaScript-rendered single-page apps.
Tavily `extract()` (even with `extract_depth="advanced"`) returned marketing/nav
copy rather than the live token-price tables, so they could not be reliably
scraped for values:

- `openai.com/api/pricing`
- `anthropic.com/pricing`, `claude.com/pricing`
- `ai.google.dev/pricing`
- `x.ai/api`
- `mistral.ai/pricing`
- `api-docs.deepseek.com/quick_start/pricing`

Per the task requirement, this limitation is documented here and we fell back to
the reputable aggregator above (which mirrors the same public prices in
server-rendered HTML). No API keys or paid logins were used — **public sources
only**.

## Files

| File | Purpose |
|------|---------|
| `index.html` | Dashboard markup |
| `styles.css` | Styling (dark theme) |
| `app.js` | Table, filters, charts, best-value logic |
| `build_dataset.py` | Model catalog + normalizer — generates `data/models.json` |
| `scrape_pricing.py` | Live pricing refresh — scrapes aggregator, regenerates dataset |
| `test_scrape_pricing.py` | Regression tests for the table parser (`pytest` or standalone; no network) |
| `data/models.json` | Normalized dataset (prices + capabilities + provenance) |
| `screenshot.png` | Screenshot of the running dashboard |
