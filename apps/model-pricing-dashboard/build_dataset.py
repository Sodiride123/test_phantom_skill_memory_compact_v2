#!/usr/bin/env python3
"""
build_dataset.py — Normalizer for the AI Model Pricing Dashboard.

Turns collected pricing data into a single normalized JSON dataset with
per-field provenance and a `last_collected` timestamp.

Data provenance
---------------
- PRICING (input / cached / output, per 1M tokens) was scraped from the
  public aggregator aipricing.guru (advanced extraction of its markdown
  pricing tables) and cross-checked with public search results. Flagged
  provenance = "aggregator".
- CONTEXT WINDOW / MODALITIES / RELEASE DATE / TAGS are NOT published in the
  aggregator pricing tables, so they come from public provider docs / model
  cards as a reasonable fallback. Flagged provenance = "fallback".

Why not the official provider pricing pages directly?
  openai.com/api/pricing, anthropic.com/pricing, claude.com/pricing, etc. are
  JS-rendered single-page apps. Tavily `extract()` (even extract_depth=
  "advanced") returns marketing/nav copy, not the live token-price tables.
  They could NOT be reliably scraped — see README. Aggregators that mirror the
  same public prices in server-rendered HTML tables were used instead.

Run:
    python build_dataset.py           # writes data/models.json
"""

import json
from datetime import datetime, timezone
from pathlib import Path

LAST_COLLECTED = "2026-07-16T00:00:00Z"

# Provenance tags per field group.
AGG = "aggregator"      # scraped from aipricing.guru pricing tables
FB = "fallback"         # public provider docs / model cards

SOURCES = {
    "aggregator": {
        "name": "aipricing.guru",
        "urls": [
            "https://www.aipricing.guru/openai-pricing",
            "https://www.aipricing.guru/anthropic-pricing",
            "https://www.aipricing.guru/google-ai-pricing",
            "https://www.aipricing.guru/xai-pricing",
            "https://www.aipricing.guru/mistral-pricing",
            "https://www.aipricing.guru/deepseek-pricing",
        ],
        "note": "Server-rendered public pricing tables, last synced 2026-07-12..15. "
                "Cross-checked with Tavily web search.",
        "scraped_via": "Tavily extract (extract_depth=advanced)",
    },
    "fallback": {
        "name": "public provider docs / model cards",
        "note": "Context window, modalities, release date and suitability tags "
                "are not in the aggregator price tables; taken from public docs "
                "as reasonable fallback values.",
    },
    "unreliable": {
        "name": "official provider pricing pages (NOT used for values)",
        "urls": [
            "https://openai.com/api/pricing",
            "https://www.anthropic.com/pricing",
            "https://ai.google.dev/pricing",
            "https://x.ai/api",
            "https://mistral.ai/pricing",
            "https://api-docs.deepseek.com/quick_start/pricing",
        ],
        "note": "JS-rendered SPAs — could not be reliably scraped for live token "
                "prices. Documented per task requirements.",
    },
}

# Each row: provider, name, family, input, cached (None if n/a), output,
# context_window, modalities, release_date, tags
# Prices are USD per 1,000,000 tokens.
MODELS = [
    # ---------------- OpenAI ----------------
    ("OpenAI", "GPT-4o mini", "GPT-4o", 0.15, 0.075, 0.60, 128000,
     ["text", "vision"], "2024-07", ["cheap", "general", "vision"]),
    ("OpenAI", "GPT-5.4 nano", "GPT-5.4", 0.20, 0.02, 1.25, 400000,
     ["text"], "2026", ["cheap", "general"]),
    ("OpenAI", "GPT-4.1 mini", "GPT-4.1", 0.40, 0.10, 1.60, 1000000,
     ["text", "vision"], "2025-04", ["coding", "general"]),
    ("OpenAI", "GPT-5.4 mini", "GPT-5.4", 0.75, 0.075, 4.50, 400000,
     ["text", "vision"], "2026", ["coding", "general"]),
    ("OpenAI", "GPT-5.6 Luna", "GPT-5.6", 1.00, 0.10, 6.00, 400000,
     ["text", "vision"], "2026", ["general", "reasoning"]),
    ("OpenAI", "GPT-4.1", "GPT-4.1", 2.00, 0.50, 8.00, 1000000,
     ["text", "vision"], "2025-04", ["coding", "long-context"]),
    ("OpenAI", "GPT-4o", "GPT-4o", 2.50, 1.25, 10.00, 128000,
     ["text", "vision", "audio"], "2024-05", ["multimodal", "general"]),
    ("OpenAI", "GPT-5.4", "GPT-5.4", 2.50, 0.25, 15.00, 400000,
     ["text", "vision"], "2026", ["reasoning", "coding"]),
    ("OpenAI", "GPT-5.6 Terra", "GPT-5.6", 2.50, 0.25, 15.00, 400000,
     ["text", "vision"], "2026", ["reasoning", "general"]),
    ("OpenAI", "GPT-5.5", "GPT-5.5", 5.00, 0.50, 30.00, 400000,
     ["text", "vision"], "2026-04-24", ["reasoning", "coding"]),
    ("OpenAI", "GPT-5.6 Sol", "GPT-5.6", 5.00, 0.50, 30.00, 400000,
     ["text", "vision"], "2026", ["reasoning"]),
    ("OpenAI", "GPT-5.4 Pro", "GPT-5.4", 30.00, None, 180.00, 400000,
     ["text", "vision"], "2026", ["reasoning"]),
    ("OpenAI", "GPT-5.5 Pro", "GPT-5.5", 30.00, None, 180.00, 400000,
     ["text", "vision"], "2026", ["reasoning"]),

    # ---------------- Anthropic ----------------
    ("Anthropic", "Claude Haiku 4.5", "Claude Haiku", 1.00, 0.10, 5.00, 200000,
     ["text", "vision"], "2025", ["cheap", "general"]),
    ("Anthropic", "Claude Sonnet 5", "Claude Sonnet", 2.00, 0.20, 10.00, 1000000,
     ["text", "vision"], "2026", ["coding", "reasoning", "long-context"]),
    ("Anthropic", "Claude Opus 4.8", "Claude Opus", 5.00, 0.50, 25.00, 200000,
     ["text", "vision"], "2026", ["reasoning", "coding"]),

    # ---------------- Google Gemini ----------------
    ("Google", "Gemini 2.5 Flash-Lite", "Gemini 2.5", 0.10, 0.01, 0.40, 1000000,
     ["text", "vision", "audio"], "2025", ["cheap", "multimodal"]),
    ("Google", "Gemini 2.5 Flash", "Gemini 2.5", 0.30, 0.03, 2.50, 1000000,
     ["text", "vision", "audio"], "2025", ["cheap", "multimodal", "general"]),
    ("Google", "Gemini 2.5 Pro", "Gemini 2.5", 1.25, 0.125, 10.00, 1000000,
     ["text", "vision", "audio"], "2025", ["reasoning", "long-context", "multimodal"]),
    ("Google", "Gemini 3.5 Flash", "Gemini 3.5", 1.50, 0.15, 9.00, 1000000,
     ["text", "vision", "audio"], "2026", ["general", "multimodal"]),
    ("Google", "Gemini 3 Pro", "Gemini 3", 2.00, 0.20, 12.00, 1000000,
     ["text", "vision", "audio"], "2026", ["reasoning", "long-context", "multimodal"]),

    # ---------------- xAI ----------------
    ("xAI", "Grok 4.1 Fast", "Grok 4.1", 0.20, None, 0.50, 256000,
     ["text"], "2026", ["cheap", "general"]),
    ("xAI", "Grok 3 Mini", "Grok 3", 0.30, None, 0.50, 131072,
     ["text"], "2025", ["cheap"]),
    ("xAI", "Grok 4.20", "Grok 4.20", 2.00, 0.20, 6.00, 2000000,
     ["text", "vision"], "2026", ["coding", "long-context"]),
    ("xAI", "Grok 4.5", "Grok 4.5", 2.00, 0.50, 6.00, 256000,
     ["text", "vision"], "2026", ["reasoning", "general"]),
    ("xAI", "Grok 4", "Grok 4", 3.00, 0.75, 15.00, 256000,
     ["text", "vision"], "2026", ["reasoning", "coding"]),
    ("xAI", "Grok 3", "Grok 3", 2.00, None, 10.00, 131072,
     ["text"], "2025", ["general"]),
    ("xAI", "Grok 2 Vision", "Grok 2", 2.00, None, 10.00, 32768,
     ["text", "vision"], "2024", ["vision"]),
    ("xAI", "Grok Beta", "Grok", 5.00, None, 15.00, 131072,
     ["text"], "2024", ["general"]),

    # ---------------- Mistral ----------------
    ("Mistral", "Mistral Small 4", "Mistral Small", 0.10, None, 0.30, 128000,
     ["text"], "2026", ["cheap", "general"]),
    ("Mistral", "Devstral Small 2", "Devstral", 0.10, None, 0.30, 128000,
     ["text"], "2026", ["coding", "cheap"]),
    ("Mistral", "Mistral NeMo", "Mistral NeMo", 0.15, None, 0.15, 128000,
     ["text"], "2024", ["cheap"]),
    ("Mistral", "Ministral 14B", "Ministral", 0.20, None, 0.20, 128000,
     ["text"], "2026", ["cheap"]),
    ("Mistral", "Magistral Small", "Magistral", 0.50, None, 1.50, 128000,
     ["text"], "2025", ["reasoning", "cheap"]),
    ("Mistral", "Mixtral 8x7B", "Mixtral", 0.70, None, 0.70, 32768,
     ["text"], "2023", ["cheap"]),
    ("Mistral", "Mistral Medium 3.5", "Mistral Medium", 1.50, None, 7.50, 128000,
     ["text", "vision"], "2026", ["general", "coding"]),
    ("Mistral", "Pixtral Large", "Pixtral", 2.00, None, 6.00, 128000,
     ["text", "vision"], "2024", ["vision", "multimodal"]),

    # ---------------- DeepSeek ----------------
    ("DeepSeek", "DeepSeek V4 Flash", "DeepSeek V4", 0.14, 0.0028, 0.28, 128000,
     ["text"], "2026", ["cheap", "general"]),
    ("DeepSeek", "DeepSeek V4 Pro", "DeepSeek V4", 0.435, 0.0036, 0.87, 128000,
     ["text"], "2026", ["reasoning", "coding", "cheap"]),
]

FIELD_PROVENANCE = {
    "input_price": AGG,
    "cached_price": AGG,
    "output_price": AGG,
    "context_window": FB,
    "modalities": FB,
    "release_date": FB,
    "tags": FB,
}

OUT_PATH = Path(__file__).parent / "data" / "models.json"


def build_catalog():
    """Return the model catalog as a list of normalized model dicts.

    Prices are the last-known hand-encoded values. `scrape_pricing.py` reuses
    this catalog and overwrites the price fields with freshly scraped values,
    keeping the capability fields (context/modalities/release/tags) as fallback.
    """
    catalog = []
    for (provider, name, family, inp, cached, out, ctx, modalities,
         release, tags) in MODELS:
        catalog.append({
            "provider": provider,
            "name": name,
            "family": family,
            "input_price": inp,
            "cached_price": cached,
            "output_price": out,
            "price_unit": "USD per 1M tokens",
            "context_window": ctx,
            "modalities": modalities,
            "release_date": release,
            "tags": tags,
            "provenance": dict(FIELD_PROVENANCE),  # per-model copy (may change)
            "price_stale": False,
        })
    return catalog


def assemble(models, *, last_collected=LAST_COLLECTED, refresh=None):
    """Wrap a list of model dicts into the top-level dataset structure."""
    dataset = {
        "last_collected": last_collected,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "currency": "USD",
        "price_unit": "per 1,000,000 tokens",
        "provider_count": len(set(m["provider"] for m in models)),
        "model_count": len(models),
        "sources": SOURCES,
        "provenance_legend": {
            "aggregator": "Scraped from public aggregator aipricing.guru "
                          "(cross-checked with web search).",
            "fallback": "Public provider docs / model cards, or a last-known "
                        "price kept because the live scrape didn't match "
                        "(see price_stale).",
        },
        "models": models,
    }
    if refresh is not None:
        dataset["refresh"] = refresh
    return dataset


def write(dataset, path=OUT_PATH):
    Path(path).write_text(json.dumps(dataset, indent=2))
    print(f"Wrote {path} — {dataset['model_count']} models, "
          f"{dataset['provider_count']} providers.")


def build():
    write(assemble(build_catalog()))


if __name__ == "__main__":
    build()
