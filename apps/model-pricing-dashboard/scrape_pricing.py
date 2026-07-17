#!/usr/bin/env python3
"""
scrape_pricing.py — Live pricing refresh for the AI Model Pricing Dashboard.

Fetches the public aggregator pricing tables (aipricing.guru) via Tavily
`extract(extract_depth="advanced")`, parses the per-provider markdown tables,
and regenerates `data/models.json` with a fresh `last_collected` timestamp.

Design
------
- Capability fields (context window / modalities / release date / tags) are NOT
  published in the price tables, so they stay hand-maintained in
  `build_dataset.py`'s catalog and keep their `fallback` provenance.
- Prices (input / cached / output per 1M tokens) are overwritten from the live
  scrape and flagged `aggregator`.
- If a catalog model can't be matched in the scraped table (name drift, row
  removed, parse failure), its last-known price is kept and the model is flagged
  `price_stale: true` with pricing provenance downgraded to `fallback`. This
  keeps the dashboard usable rather than dropping rows.
- Public sources only. No API keys, no paid logins.

After writing, it runs a non-blocking `validate_dataset` integrity check on the
freshly-assembled data and prints a one-line PASS/FAIL summary.

Run:
    python scrape_pricing.py               # refresh data/models.json
    python scrape_pricing.py --dry-run     # scrape + report, don't write
    python scrape_pricing.py --no-validate # skip the post-scrape integrity check
"""

import argparse
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Make repo root importable so `clients.tavily_client` resolves when run from
# this app directory; keep the app dir importable too (validate_dataset /
# build_dataset) regardless of cwd.
_APP_DIR = Path(__file__).resolve().parent
_REPO_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(_APP_DIR), str(_REPO_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from clients.tavily_client import Tavily  # noqa: E402

import build_dataset as bd  # noqa: E402

PROVIDER_PAGES = {
    "OpenAI": "https://www.aipricing.guru/openai-pricing",
    "Anthropic": "https://www.aipricing.guru/anthropic-pricing",
    "Google": "https://www.aipricing.guru/google-ai-pricing",
    "xAI": "https://www.aipricing.guru/xai-pricing",
    "Mistral": "https://www.aipricing.guru/mistral-pricing",
    "DeepSeek": "https://www.aipricing.guru/deepseek-pricing",
}


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def norm(name: str) -> str:
    """Normalize a model name for matching: lowercase, drop spaces/punctuation."""
    return re.sub(r"[^a-z0-9]", "", name.lower())


def parse_price(cell: str):
    """'$0.14' -> 0.14 ; '$1,250' -> 1250.0 ; '—' / '-' / '' -> None."""
    cell = cell.strip().replace("$", "").replace(",", "")
    if cell in ("", "—", "-", "n/a", "N/A"):
        return None
    try:
        return float(cell)
    except ValueError:
        return None


def _split_row(line: str):
    return [c.strip() for c in line.strip().strip("|").split("|")]


def parse_table(markdown: str):
    """Parse every aggregator pricing table on the page into a list of dicts.

    Column layout varies per provider (some pages include a 'Tier' column,
    others don't), so columns are located by header name rather than fixed
    index. The Model cell is 'Display Name  Family' (name + family, 2+ spaces).
    Returns deduped [{name, input, cached, output}] rows across all tables.
    """
    rows = {}
    lines = markdown.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.lstrip().startswith("| Model") and "Input" in line and "Output" in line:
            header = _split_row(line)
            # Locate price columns by header text.
            def col(substr):
                for idx, h in enumerate(header):
                    if substr.lower() in h.lower():
                        return idx
                return None

            ci, cc, co = col("Input"), col("Cached"), col("Output")
            i += 1
            # Skip the markdown separator row if present.
            if i < len(lines) and set(lines[i].strip()) <= {"|", "-", " "}:
                i += 1
            while i < len(lines):
                s = lines[i].strip()
                if not s.startswith("|"):
                    break
                cells = _split_row(lines[i])
                i += 1
                if len(cells) <= max(x for x in (ci, cc, co) if x is not None):
                    continue
                name = re.split(r"\s{2,}", cells[0])[0].strip()
                if not name or set(name) <= {"-", " "}:
                    continue
                rows[norm(name)] = {
                    "name": name,
                    "input": parse_price(cells[ci]) if ci is not None else None,
                    "cached": parse_price(cells[cc]) if cc is not None else None,
                    "output": parse_price(cells[co]) if co is not None else None,
                }
            continue
        i += 1
    return list(rows.values())


def _extract_with_retry(tav, url, attempts=3):
    """Tavily extract with retries — the aggregator occasionally 502s."""
    last_err = None
    for n in range(attempts):
        try:
            res = tav.extract([url], extract_depth="advanced")
            if res.get("results"):
                return res, None
            last_err = res.get("failed_results") or "no content"
        except Exception as e:  # noqa: BLE001
            last_err = str(e).splitlines()[0]
        time.sleep(1.5 * (n + 1))
    return None, last_err


def match_row(catalog_name: str, scraped: dict):
    """Match a catalog model to a scraped aggregator row by EXACT normalized name.

    Matching is STRICT (exact normalized name), mirroring scrape_official.py. A
    variant with no aggregator row of its own (e.g. "GPT-5.4 Pro" when the page
    only lists "GPT-5.4") is intentionally left unmatched — kept as stale
    fallback — rather than inheriting a similarly-named base model's price. An
    earlier bidirectional substring fallback (`key in k or k in key`) caused
    exactly that collision, silently mis-pricing suffixed variants. See #97.
    """
    if not catalog_name:
        return None
    return scraped.get(norm(catalog_name))


def scrape(pages=PROVIDER_PAGES, verbose=True):
    """Scrape every provider page. Returns {normalized_name: price_dict}."""
    tav = Tavily()
    prices = {}
    for provider, url in pages.items():
        res, err = _extract_with_retry(tav, url)
        if res is None:
            if verbose:
                print(f"  ! {provider}: {err}")
            continue
        table = parse_table(res["results"][0].get("raw_content", ""))
        for row in table:
            prices[norm(row["name"])] = row
        if verbose:
            print(f"  ✓ {provider}: {len(table)} rows")
    return prices


# ---------------------------------------------------------------------------
# Refresh
# ---------------------------------------------------------------------------

def _validate_dataset(dataset):
    """Non-blocking integrity check of the freshly-assembled dataset.

    Delegates the summary wording to validate_dataset.check_and_report; a
    validator import/parse problem is swallowed so it never masks an
    otherwise-successful scrape (mirrors scrape_official.py / preview.py)."""
    try:
        import validate_dataset as vd
        return vd.check_and_report(dataset)
    except Exception as e:  # noqa: BLE001 — never let validation mask a scrape
        print(f"\nvalidation skipped ({e})")
        return None


def refresh(dry_run=False, validate=True):
    print("Scraping aggregator pricing pages…")
    scraped = scrape()
    if not scraped:
        print("ERROR: scraped 0 rows — aborting (keeping existing data).")
        return 1

    catalog = bd.build_catalog()
    matched, stale = 0, 0
    stale_names = []

    for m in catalog:
        hit = match_row(m["name"], scraped)
        if hit is not None:
            m["input_price"] = hit["input"]
            m["cached_price"] = hit["cached"]
            m["output_price"] = hit["output"]
            for f in ("input_price", "cached_price", "output_price"):
                m["provenance"][f] = bd.AGG
            m["price_stale"] = False
            matched += 1
        else:
            for f in ("input_price", "cached_price", "output_price"):
                m["provenance"][f] = bd.FB  # last-known value, not fresh
            m["price_stale"] = True
            stale += 1
            stale_names.append(m["name"])

    now = datetime.now(timezone.utc)
    refresh_meta = {
        "method": "Tavily extract (extract_depth=advanced) of aipricing.guru",
        "scraped_rows": len(scraped),
        "matched": matched,
        "stale": stale,
        "stale_models": stale_names,
    }
    dataset = bd.assemble(
        catalog,
        last_collected=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        refresh=refresh_meta,
    )

    print(f"Matched {matched} / {len(catalog)} catalog models "
          f"({stale} kept as stale fallback).")
    if stale_names:
        print("  Stale (last-known price kept): " + ", ".join(stale_names))

    if dry_run:
        print("--dry-run: not writing data/models.json")
    else:
        bd.write(dataset)

    # Integrity gate: validate exactly what we assembled (== what was written).
    # Non-blocking — a FAIL is surfaced but does not fail the refresh.
    if validate:
        _validate_dataset(dataset)
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Refresh dashboard pricing from public aggregator.")
    ap.add_argument("--dry-run", action="store_true", help="scrape + report, don't write")
    ap.add_argument("--no-validate", action="store_true",
                    help="skip the post-scrape validate_dataset integrity check")
    args = ap.parse_args()
    sys.exit(refresh(dry_run=args.dry_run, validate=not args.no_validate))
