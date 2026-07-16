#!/usr/bin/env python3
"""
Regression tests for scrape_official.py's per-provider parsers.

No network / no browser — each test feeds a small snippet of the rendered page
text (as the browser skill returns it) and pins the parsed prices. Run with
`pytest` or standalone: `python test_scrape_official.py`.
"""

import scrape_official as so


def test_openai_flagship_row():
    text = "\n".join([
        "Model\tInput\tCached input\tCache writes\tOutput",
        "gpt-5.6-sol", "", "$5.00", "", "$0.50", "", "$6.25", "", "$30.00",
        "", "$10.00", "", "$1.00", "", "$12.50", "", "$45.00",
        "gpt-5.4-pro", "", "$30.00", "", "-", "", "-", "", "$180.00",
    ])
    rows = so.parse_openai(text)
    assert rows["gpt56sol"] == {"name": "gpt-5.6-sol", "input": 5.0, "cached": 0.5, "output": 30.0}
    # short-context standard: input, cached, output (cache-write ignored); '-' -> None
    assert rows["gpt54pro"]["input"] == 30.0
    assert rows["gpt54pro"]["cached"] is None
    assert rows["gpt54pro"]["output"] == 180.0


def test_anthropic_main_table_only():
    text = "\n".join([
        "Claude Haiku 4.5\t$1 / MTok\t$1.25 / MTok\t$2 / MTok\t$0.10 / MTok\t$5 / MTok",
        "Claude Opus 4.8 (deprecated)\t$5 / MTok\t$6.25 / MTok\t$10 / MTok\t$0.50 / MTok\t$25 / MTok",
        # 2-column batch table row must be IGNORED (not 5 '/ MTok' cells):
        "Claude Haiku 4.5\t$0.50 / MTok\t$2.50 / MTok",
    ])
    rows = so.parse_anthropic(text)
    assert rows["claudehaiku45"] == {"name": "Claude Haiku 4.5", "input": 1.0, "cached": 0.10, "output": 5.0}
    # parenthetical stripped from the name
    assert rows["claudeopus48"]["name"] == "Claude Opus 4.8"
    assert rows["claudeopus48"]["output"] == 25.0


def test_xai_standard_tier():
    text = "grok-4.5\t500k\t$2.00\t$0.50\t$6.00\t$4.00\t$1.00\t$12.00"
    rows = so.parse_xai(text)
    assert rows["grok45"] == {"name": "grok-4.5", "input": 2.0, "cached": 0.5, "output": 6.0}


def test_deepseek_transposed_with_rowspan_label():
    text = "\n".join([
        "MODEL\tdeepseek-v4-flash(1)\tdeepseek-v4-pro",
        "PRICING\t1M INPUT TOKENS (CACHE HIT)\t$0.0028\t$0.003625",
        "1M INPUT TOKENS (CACHE MISS)\t$0.14\t$0.435",
        "1M OUTPUT TOKENS\t$0.28\t$0.87",
    ])
    rows = so.parse_deepseek(text)
    assert rows["deepseekv4flash"] == {"name": "deepseek-v4-flash", "input": 0.14, "cached": 0.0028, "output": 0.28}
    assert rows["deepseekv4pro"] == {"name": "deepseek-v4-pro", "input": 0.435, "cached": 0.003625, "output": 0.87}


def test_google_section_first_dollar():
    text = "\n".join([
        "Gemini 3.5 Flash",
        "gemini-3.5-flash",
        "Free Tier\tPaid Tier, per 1M tokens in USD",
        "Input price\tFree of charge\t$1.50",
        "Output price (including thinking tokens)\tFree of charge\t$9.00",
        "Context caching price\tFree of charge\t$0.15",
        "Gemini 3.1 Pro Preview",
        # tiered input: base <=200k must win over >200k
        "Input price\tNot available\t$2.00, prompts <= 200k tokens\t$4.00, prompts > 200k",
        "Output price (including thinking tokens)\tNot available\t$12.00, <= 200k",
    ])
    rows = so.parse_google(text)
    assert rows["gemini35flash"] == {"name": "Gemini 3.5 Flash", "input": 1.5, "cached": 0.15, "output": 9.0}
    assert rows["gemini31propreview"]["input"] == 2.0  # base tier, not $4.00


def test_money_helper():
    assert so._money("$1.50") == 1.5
    assert so._money("$2.00, prompts <= 200k") == 2.0
    assert so._money("Free of charge") is None
    assert so._money("-") is None
    assert so._money("$1,250") == 1250.0


def _run_standalone():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for fn in fns:
        try:
            fn()
            print(f"  ok   {fn.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"  FAIL {fn.__name__}: {e}")
    print(f"\n{passed}/{len(fns)} passed")
    return passed == len(fns)


if __name__ == "__main__":
    import sys
    sys.exit(0 if _run_standalone() else 1)
