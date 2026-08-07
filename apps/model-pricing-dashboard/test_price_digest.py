#!/usr/bin/env python3
"""
Regression tests for price_digest.digest_from_history() — the weekly Slack
price-change digest (issue #116). No Slack, no network, no filesystem: the pure
function is exercised directly. Runs under pytest or standalone.
"""

import price_digest as pd


def _snap(date, prices):
    return {"date": date, "prices": prices}


def test_silent_when_nothing_changed():
    s = _snap("2026-08-07", {"A/X": {"input": 1, "cached": 0.1, "output": 2, "provenance": "official"}})
    assert pd.digest_from_history([s, dict(s)]) is None
    assert pd.digest_from_history([s]) is None      # fewer than 2 snapshots
    assert pd.digest_from_history([]) is None
    assert pd.digest_from_history(None) is None


def test_movers_sorted_biggest_first_and_dirs():
    prev = _snap("2026-08-01", {
        "xAI/Grok 4": {"input": 3.0, "cached": 0.75, "output": 15.0, "provenance": "fallback"},
    })
    cur = _snap("2026-08-07", {
        "xAI/Grok 4": {"input": 1.25, "cached": 0.2, "output": 2.5, "provenance": "official"},
    })
    d = pd.digest_from_history([prev, cur])
    assert d is not None
    assert d["date"] == "2026-08-07" and d["prev_date"] == "2026-08-01"
    # Biggest absolute % move first: output 15 -> 2.5 = -83.3%.
    assert d["movers"][0]["field"] == "output"
    assert d["movers"][0]["pct"] == -83.3
    assert d["movers"][0]["dir"] == "down"
    assert all(m["dir"] == "down" for m in d["movers"])
    assert {m["field"] for m in d["movers"]} == {"input", "cached", "output"}


def test_up_mover_detected():
    prev = _snap("d1", {"OpenAI/GPT": {"input": 2.0, "output": 8.0, "provenance": "official"}})
    cur = _snap("d2", {"OpenAI/GPT": {"input": 2.0, "output": 12.0, "provenance": "official"}})
    d = pd.digest_from_history([prev, cur])
    assert len(d["movers"]) == 1
    assert d["movers"][0]["dir"] == "up" and d["movers"][0]["pct"] == 50.0


def test_new_and_removed_models():
    prev = _snap("d1", {"A/Old": {"input": 1, "output": 2, "provenance": "official"}})
    cur = _snap("d2", {"B/New": {"input": 1, "output": 2, "provenance": "official"}})
    d = pd.digest_from_history([prev, cur])
    assert d["new"] == ["B/New"] and d["removed"] == ["A/Old"]


def test_fallback_share_change_and_flat():
    prev = _snap("d1", {
        "A/X": {"input": 1, "output": 2, "provenance": "official"},
        "A/Y": {"input": 1, "output": 2, "provenance": "fallback"},
    })
    # Y upgraded fallback -> official: fallback share 50% -> 0%.
    cur = _snap("d2", {
        "A/X": {"input": 1, "output": 2, "provenance": "official"},
        "A/Y": {"input": 1, "output": 2, "provenance": "official"},
    })
    d = pd.digest_from_history([prev, cur])
    assert d["fallback_share"]["from"] == 50.0
    assert d["fallback_share"]["to"] == 0.0
    assert d["fallback_share"]["delta_pp"] == -50.0


def test_null_prices_skipped_in_movers():
    prev = _snap("d1", {"A/X": {"input": 1, "cached": None, "output": 2, "provenance": "fallback"}})
    cur = _snap("d2", {"A/X": {"input": 1, "cached": 0.1, "output": 3, "provenance": "official"}})
    d = pd.digest_from_history([prev, cur])
    # cached None -> 0.1 skipped; only output mover (and fallback-share change) counted.
    assert all(m["field"] != "cached" for m in d["movers"])
    assert any(m["field"] == "output" for m in d["movers"])


def test_format_digest_renders():
    prev = _snap("2026-08-01", {"xAI/Grok 4": {"input": 3.0, "output": 15.0, "provenance": "fallback"}})
    cur = _snap("2026-08-07", {"xAI/Grok 4": {"input": 1.25, "output": 2.5, "provenance": "official"},
                               "B/New": {"input": 1, "output": 1, "provenance": "official"}})
    d = pd.digest_from_history([prev, cur])
    text = pd.format_digest(d)
    assert "Weekly AI model price digest" in text
    assert "price decrease" in text
    assert "new model" in text
    assert "Fallback share" in text


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in fns:
        try:
            fn()
            print(f"  ok   {fn.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"  FAIL {fn.__name__} :: {e}")
    print(f"\n{passed}/{len(fns)} passed")
    raise SystemExit(0 if passed == len(fns) else 1)
