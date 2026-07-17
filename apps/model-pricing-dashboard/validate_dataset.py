#!/usr/bin/env python3
"""
validate_dataset.py — Integrity checks for the AI Model Pricing Dashboard data.

Validates the *assembled* dataset (data/models.json) against the invariants the
dashboard relies on, so a scraper/layout regression or a hand-edit that produces
malformed data fails loudly here rather than shipping to the UI.

It complements the parser tests (test_scrape_pricing.py) and matcher tests: those
pin how raw pages are read; this pins what the finished dataset must look like.

Two severities:
  - ERROR    — a hard invariant violation. Makes the dataset invalid (exit 1).
  - WARNING  — a smell worth surfacing but not necessarily wrong (exit 0). The
               chief one is the "collision smell": two DISTINCT models in the
               same family carrying an identical (input, cached, output) price
               triple — the shape of the #97 mis-match (GPT-5.4 Pro == GPT-5.4).

Run:
    python validate_dataset.py            # validate data/models.json (human)
    python validate_dataset.py --json     # machine-readable report
    python validate_dataset.py -f x.json  # validate a specific file

Exit code: 0 if no ERRORs (warnings allowed), 1 otherwise.

Python API:
    from validate_dataset import validate, load_dataset
    report = validate(load_dataset())      # -> {"ok", "errors", "warnings", ...}
"""

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

_APP_DIR = Path(__file__).resolve().parent
DEFAULT_PATH = _APP_DIR / "data" / "models.json"

VALID_PROVENANCE = {"official", "aggregator", "fallback"}
PRICE_FIELDS = ("input_price", "cached_price", "output_price")
# Provenance keys every model row must carry (mirrors build_dataset.FIELD_PROVENANCE).
PROVENANCE_KEYS = (
    "input_price", "cached_price", "output_price",
    "context_window", "modalities", "release_date", "tags",
)
# Tags / modalities the best-value pick logic (app.js) filters on. If none of the
# models carry one, that pick card silently vanishes — worth a warning.
BEST_VALUE_TAGS = ("coding", "long-context")
BEST_VALUE_MODALITIES = ("vision", "audio")


def norm(name: str) -> str:
    """Normalize a model name: lowercase, keep only [a-z0-9] (matches scrapers)."""
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


def load_dataset(path=DEFAULT_PATH):
    """Load and JSON-parse the dataset file."""
    return json.loads(Path(path).read_text())


def _is_number(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def validate(dataset):
    """Validate an assembled dataset dict.

    Returns {"ok": bool, "errors": [str], "warnings": [str],
             "model_count": int}. `ok` is True when there are no errors
    (warnings do not affect it).
    """
    errors, warnings = [], []

    def err(msg):
        errors.append(msg)

    def warn(msg):
        warnings.append(msg)

    if not isinstance(dataset, dict):
        return {"ok": False, "errors": ["dataset is not a JSON object"],
                "warnings": [], "model_count": 0}

    models = dataset.get("models")
    if not isinstance(models, list) or not models:
        return {"ok": False, "errors": ["dataset.models missing or empty"],
                "warnings": [], "model_count": 0}

    # ---- top-level invariants ------------------------------------------------
    lc = dataset.get("last_collected")
    if not isinstance(lc, str) or not _parse_iso(lc):
        err(f"last_collected is not an ISO timestamp: {lc!r}")

    mc = dataset.get("model_count")
    if mc != len(models):
        err(f"model_count ({mc}) != len(models) ({len(models)})")

    providers = {m.get("provider") for m in models if isinstance(m, dict)}
    pc = dataset.get("provider_count")
    if pc != len(providers):
        err(f"provider_count ({pc}) != distinct providers ({len(providers)})")

    # ---- per-model invariants ------------------------------------------------
    seen_names = {}
    for idx, m in enumerate(models):
        where = f"model[{idx}]"
        if not isinstance(m, dict):
            err(f"{where} is not an object")
            continue
        name = m.get("name")
        where = f"{name!r}" if name else where

        if not (isinstance(name, str) and name.strip()):
            err(f"{where}: empty/invalid name")
        else:
            if name in seen_names:
                err(f"duplicate model name: {name!r}")
            seen_names[name] = idx

        if not (isinstance(m.get("provider"), str) and m["provider"].strip()):
            err(f"{where}: empty/invalid provider")

        for f in PRICE_FIELDS:
            v = m.get(f, "__missing__")
            if v == "__missing__":
                err(f"{where}: missing {f}")
            elif v is not None and not (_is_number(v) and v >= 0):
                err(f"{where}: {f} must be None or a number >= 0, got {v!r}")

        ctx = m.get("context_window")
        if not (isinstance(ctx, int) and not isinstance(ctx, bool) and ctx > 0):
            err(f"{where}: context_window must be a positive int, got {ctx!r}")

        for f in ("modalities", "tags"):
            v = m.get(f)
            if not (isinstance(v, list) and v and all(isinstance(x, str) for x in v)):
                err(f"{where}: {f} must be a non-empty list of strings, got {v!r}")

        # provenance map
        prov = m.get("provenance")
        if not isinstance(prov, dict):
            err(f"{where}: provenance must be an object")
        else:
            for k in PROVENANCE_KEYS:
                pv = prov.get(k)
                if pv not in VALID_PROVENANCE:
                    err(f"{where}: provenance[{k}]={pv!r} not in {sorted(VALID_PROVENANCE)}")
            # stale rows must not claim fresh pricing provenance
            if m.get("price_stale") is True:
                for f in PRICE_FIELDS:
                    if prov.get(f) != "fallback":
                        err(f"{where}: price_stale=True but provenance[{f}]="
                            f"{prov.get(f)!r} (expected 'fallback')")

    # ---- best-value coverage (warnings) -------------------------------------
    all_tags = {t for m in models if isinstance(m, dict)
                for t in (m.get("tags") or [])}
    all_mods = {t for m in models if isinstance(m, dict)
                for t in (m.get("modalities") or [])}
    for t in BEST_VALUE_TAGS:
        if t not in all_tags:
            warn(f"no model carries tag {t!r} — its best-value card will be empty")
    if not (all_mods & set(BEST_VALUE_MODALITIES)):
        warn("no model has vision/audio — the multimodal best-value card will be empty")

    # ---- collision smell (warning) ------------------------------------------
    # Two DISTINCT models in the same family with an identical price triple is
    # the shape of a substring mis-match (#97). Group by family; compare triples.
    by_family = {}
    for m in models:
        if not isinstance(m, dict):
            continue
        fam = m.get("family") or norm(m.get("name", ""))
        by_family.setdefault(fam, []).append(m)
    for fam, group in by_family.items():
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                a, b = group[i], group[j]
                ta = tuple(a.get(f) for f in PRICE_FIELDS)
                tb = tuple(b.get(f) for f in PRICE_FIELDS)
                if ta == tb and a.get("name") != b.get("name"):
                    warn(f"collision smell: {a.get('name')!r} and {b.get('name')!r} "
                         f"(family {fam!r}) share identical prices {ta} — "
                         f"possible mis-match")

    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "model_count": len(models),
    }


def _parse_iso(s):
    """Return a datetime if `s` parses as ISO-8601 (accepting a trailing Z), else None."""
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def main(argv=None):
    ap = argparse.ArgumentParser(description="Validate the dashboard dataset.")
    ap.add_argument("-f", "--file", default=str(DEFAULT_PATH),
                    help="dataset JSON to validate (default: data/models.json)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args(argv)

    try:
        dataset = load_dataset(args.file)
    except (OSError, json.JSONDecodeError) as e:
        report = {"ok": False, "errors": [f"could not load {args.file}: {e}"],
                  "warnings": [], "model_count": 0}
    else:
        report = validate(dataset)

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        n_e, n_w = len(report["errors"]), len(report["warnings"])
        status = "PASS" if report["ok"] else "FAIL"
        print(f"{status}: {report['model_count']} models · "
              f"{n_e} error(s) · {n_w} warning(s)")
        for e in report["errors"]:
            print(f"  ✗ ERROR   {e}")
        for w in report["warnings"]:
            print(f"  ⚠ WARNING {w}")

    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
