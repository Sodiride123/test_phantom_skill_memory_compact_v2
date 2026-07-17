#!/usr/bin/env python3
"""
Tests for validate_dataset — the dataset-integrity checker.

A hand-built minimal-but-valid dataset fixture is mutated per test to trip each
individual invariant, so a regression in one check can't hide behind another.

No network, no files: everything runs against in-memory dicts (plus one check
that the real shipped data/models.json currently passes).

Run:
    pytest test_validate_dataset.py -q     # or: python test_validate_dataset.py
"""

import sys
from pathlib import Path

_APP_DIR = Path(__file__).resolve().parent
if str(_APP_DIR) not in sys.path:
    sys.path.insert(0, str(_APP_DIR))

import validate_dataset as vd  # noqa: E402


def _model(name, provider, family, inp, cached, out, ctx, mods, tags,
           prov=None, stale=False):
    prov = prov or {
        "input_price": "aggregator", "cached_price": "aggregator",
        "output_price": "aggregator", "context_window": "fallback",
        "modalities": "fallback", "release_date": "fallback", "tags": "fallback",
    }
    return {
        "provider": provider, "name": name, "family": family,
        "input_price": inp, "cached_price": cached, "output_price": out,
        "price_unit": "USD per 1M tokens", "context_window": ctx,
        "modalities": mods, "release_date": "2026", "tags": tags,
        "provenance": prov, "price_stale": stale,
    }


def _good_dataset():
    """A minimal dataset satisfying every ERROR invariant with no warnings."""
    models = [
        _model("Alpha", "OpenAI", "Alpha", 1.0, 0.1, 2.0, 128000,
               ["text", "vision"], ["coding", "general"]),
        _model("Beta", "Anthropic", "Beta", 0.5, None, 1.5, 200000,
               ["text", "audio"], ["cheap", "long-context"]),
    ]
    return {
        "last_collected": "2026-07-17T00:00:00Z",
        "model_count": len(models),
        "provider_count": len({m["provider"] for m in models}),
        "models": models,
    }


# --- happy paths -------------------------------------------------------------

def test_good_dataset_passes_clean():
    r = vd.validate(_good_dataset())
    assert r["ok"] is True
    assert r["errors"] == []
    assert r["warnings"] == []


def test_shipped_dataset_passes():
    # The real data/models.json must always validate.
    r = vd.validate(vd.load_dataset())
    assert r["ok"] is True, r["errors"]


# --- error invariants --------------------------------------------------------

def test_negative_price_is_error():
    d = _good_dataset()
    d["models"][0]["input_price"] = -1.0
    r = vd.validate(d)
    assert not r["ok"]
    assert any("input_price" in e for e in r["errors"])


def test_none_price_is_allowed():
    d = _good_dataset()
    d["models"][0]["cached_price"] = None
    assert vd.validate(d)["ok"]


def test_bad_provenance_value_is_error():
    d = _good_dataset()
    d["models"][0]["provenance"]["input_price"] = "guessed"
    r = vd.validate(d)
    assert not r["ok"]
    assert any("provenance[input_price]" in e for e in r["errors"])


def test_zero_context_window_is_error():
    d = _good_dataset()
    d["models"][0]["context_window"] = 0
    r = vd.validate(d)
    assert not r["ok"]
    assert any("context_window" in e for e in r["errors"])


def test_empty_modalities_is_error():
    d = _good_dataset()
    d["models"][0]["modalities"] = []
    r = vd.validate(d)
    assert not r["ok"]
    assert any("modalities" in e for e in r["errors"])


def test_stale_must_be_fallback_priced():
    d = _good_dataset()
    d["models"][0]["price_stale"] = True  # but provenance still 'aggregator'
    r = vd.validate(d)
    assert not r["ok"]
    assert any("price_stale" in e for e in r["errors"])


def test_stale_with_fallback_provenance_ok():
    d = _good_dataset()
    m = d["models"][0]
    m["price_stale"] = True
    for f in ("input_price", "cached_price", "output_price"):
        m["provenance"][f] = "fallback"
    assert vd.validate(d)["ok"]


def test_duplicate_name_is_error():
    d = _good_dataset()
    d["models"][1]["name"] = d["models"][0]["name"]
    r = vd.validate(d)
    assert not r["ok"]
    assert any("duplicate" in e for e in r["errors"])


def test_count_mismatch_is_error():
    d = _good_dataset()
    d["model_count"] = 99
    r = vd.validate(d)
    assert not r["ok"]
    assert any("model_count" in e for e in r["errors"])


def test_bad_timestamp_is_error():
    d = _good_dataset()
    d["last_collected"] = "last thursday"
    r = vd.validate(d)
    assert not r["ok"]
    assert any("last_collected" in e for e in r["errors"])


def test_empty_models_is_error():
    assert not vd.validate({"models": []})["ok"]


# --- warnings (non-failing) --------------------------------------------------

def test_collision_smell_is_warning_not_error():
    d = _good_dataset()
    # Add a same-family sibling with an identical price triple.
    d["models"].append(
        _model("Alpha Pro", "OpenAI", "Alpha", 1.0, 0.1, 2.0, 128000,
               ["text", "vision"], ["coding"])
    )
    d["model_count"] = len(d["models"])
    r = vd.validate(d)
    assert r["ok"] is True  # warning only, still valid
    assert any("collision smell" in w for w in r["warnings"])


def test_different_prices_same_family_no_warning():
    d = _good_dataset()
    d["models"].append(
        _model("Alpha Pro", "OpenAI", "Alpha", 30.0, None, 180.0, 128000,
               ["text", "vision"], ["coding"])
    )
    d["model_count"] = len(d["models"])
    r = vd.validate(d)
    assert r["ok"] is True
    assert not any("collision smell" in w for w in r["warnings"])


def test_missing_best_value_tag_warns():
    d = _good_dataset()
    for m in d["models"]:
        m["tags"] = ["general"]  # drop coding / long-context
    r = vd.validate(d)
    assert r["ok"] is True
    assert any("coding" in w for w in r["warnings"])


# --- standalone runner -------------------------------------------------------

if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
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
