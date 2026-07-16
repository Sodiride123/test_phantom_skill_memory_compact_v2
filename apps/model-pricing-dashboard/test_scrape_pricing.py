#!/usr/bin/env python3
"""
Regression tests for scrape_pricing's markdown-table parser.

The public aggregator (aipricing.guru) table layout drifts between providers:
column count varies (Mistral/DeepSeek omit the "Tier" column), a page can carry
several "| Model" tables, and prices use $/comma formatting with "—" for n/a.
`parse_table()` locates columns by header name to cope with this — these tests
pin that behaviour with static fixtures so a future layout change fails loudly
here rather than silently producing bad prices.

No network: everything runs against in-file fixture strings.

Run:
    pytest test_scrape_pricing.py -q      # or: python test_scrape_pricing.py
"""

import sys
from pathlib import Path

# Import scrape_pricing from this app dir (it adds repo root to sys.path itself).
_APP_DIR = Path(__file__).resolve().parent
if str(_APP_DIR) not in sys.path:
    sys.path.insert(0, str(_APP_DIR))

import scrape_pricing as sp  # noqa: E402


# --- Fixtures: representative aggregator markdown tables ---------------------

# 6 columns incl. a "Tier" column and a "Cached" column (OpenAI/Anthropic style).
SIX_COL = """\
Some heading and marketing prose.

| Model | Tier | Input | Cached | Output | Context |
| --- | --- | --- | --- | --- | --- |
| GPT-4o  GPT-4o | Standard | $2.50 | $1.25 | $10.00 | 128K |
| GPT-4o mini  GPT-4o | Standard | $0.15 | $0.075 | $0.60 | 128K |

Footer text, not a table.
"""

# 5 columns, no "Tier", no "Cached" (Mistral style).
FIVE_COL_NO_CACHED = """\
| Model | Input | Output | Context |
| --- | --- | --- | --- |
| Mistral Small 4  Mistral Small | $0.10 | $0.30 | 128K |
| Mistral NeMo  Mistral NeMo | $0.15 | $0.15 | 128K |
"""

# Two tables on one page, "—" empties, and a comma-formatted price.
MULTI_TABLE = """\
| Model | Input | Cached | Output |
| --- | --- | --- | --- |
| DeepSeek V4 Flash  DeepSeek V4 | $0.14 | $0.0028 | $0.28 |

Prose separating two tables.

| Model | Input | Cached | Output |
| --- | --- | --- | --- |
| DeepSeek V4 Pro  DeepSeek V4 | $0.435 | — | $0.87 |
| Big Model  Big | $1,250 | — | $2,500 |
"""


# --- parse_price -------------------------------------------------------------

def test_parse_price_basic():
    assert sp.parse_price("$2.50") == 2.50
    assert sp.parse_price("$0.075") == 0.075
    assert sp.parse_price("$1,250") == 1250.0
    assert sp.parse_price(" $10.00 ") == 10.0


def test_parse_price_empty_variants():
    for cell in ("—", "-", "", "n/a", "N/A", "   "):
        assert sp.parse_price(cell) is None


def test_parse_price_garbage_is_none():
    assert sp.parse_price("free") is None


# --- parse_table -------------------------------------------------------------

def _by_name(rows):
    return {r["name"]: r for r in rows}


def test_six_col_with_tier_and_cached():
    rows = _by_name(sp.parse_table(SIX_COL))
    assert set(rows) == {"GPT-4o", "GPT-4o mini"}
    assert rows["GPT-4o"] == {
        "name": "GPT-4o", "input": 2.50, "cached": 1.25, "output": 10.00,
    }
    assert rows["GPT-4o mini"]["input"] == 0.15
    assert rows["GPT-4o mini"]["cached"] == 0.075
    assert rows["GPT-4o mini"]["output"] == 0.60


def test_five_col_no_cached_column():
    rows = _by_name(sp.parse_table(FIVE_COL_NO_CACHED))
    assert set(rows) == {"Mistral Small 4", "Mistral NeMo"}
    # No "Cached" header -> cached must be None, prices still parsed.
    assert rows["Mistral Small 4"] == {
        "name": "Mistral Small 4", "input": 0.10, "cached": None, "output": 0.30,
    }
    assert rows["Mistral NeMo"]["cached"] is None


def test_multi_table_dedup_empties_and_commas():
    rows = _by_name(sp.parse_table(MULTI_TABLE))
    # Both tables are collected.
    assert set(rows) == {"DeepSeek V4 Flash", "DeepSeek V4 Pro", "Big Model"}
    assert rows["DeepSeek V4 Flash"]["cached"] == 0.0028
    # "—" cached -> None.
    assert rows["DeepSeek V4 Pro"]["cached"] is None
    assert rows["DeepSeek V4 Pro"]["output"] == 0.87
    # Comma-formatted prices survive.
    assert rows["Big Model"]["input"] == 1250.0
    assert rows["Big Model"]["output"] == 2500.0


def test_no_table_returns_empty():
    assert sp.parse_table("Just prose, no pricing table here.") == []


# --- standalone runner -------------------------------------------------------

if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  ok   {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL {t.__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
