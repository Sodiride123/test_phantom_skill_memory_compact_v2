#!/usr/bin/env python3
"""
scrape_official.py — Enrich the dashboard dataset with prices read from each
provider's OWN pricing page, rendered in the browser skill.

Why this exists
---------------
`scrape_pricing.py` sources prices from the public aggregator aipricing.guru
because the official provider pages are JS-rendered SPAs that plain HTTP
`extract()` can't parse. But the *browser skill* renders the JS, so the live
token-price tables ARE reachable from the rendered DOM. This script drives the
persistent browser, reads each official page's text, parses the pricing tables,
and overlays the values onto `data/models.json`:

- Prices confirmed against a provider's own page are flagged provenance
  ``official`` (highest confidence) and take precedence over the aggregator.
- Matching is STRICT (exact normalized model name) to avoid mismatching model
  variants — a model not found on the official page keeps its existing
  aggregator / fallback price untouched.
- Mistral's official page (mistral.ai/pricing) only lists subscription plans,
  not API token prices, so Mistral rows are left on the aggregator/fallback.

Public sources only. No API keys, no paid logins.

Run:
    python scrape_official.py            # overlay official prices onto data/models.json
    python scrape_official.py --dry-run  # scrape + report, don't write
"""

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

_APP_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _APP_DIR.parents[1]
_BROWSER_DIR = _REPO_ROOT / "browser"
for p in (str(_REPO_ROOT), str(_BROWSER_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

import build_dataset as bd  # noqa: E402

DATA_PATH = _APP_DIR / "data" / "models.json"

# Official pricing pages (rendered). Mistral omitted on purpose — see module docstring.
OFFICIAL_PAGES = {
    "OpenAI": "https://developers.openai.com/api/docs/pricing",
    "Anthropic": "https://docs.claude.com/en/docs/about-claude/pricing",
    "Google": "https://ai.google.dev/gemini-api/docs/pricing",
    "xAI": "https://docs.x.ai/developers/pricing",
    "DeepSeek": "https://api-docs.deepseek.com/quick_start/pricing",
}

MONEY = re.compile(r"\$\s*([\d,]+(?:\.\d+)?)")


def norm(name: str) -> str:
    """Lowercase, strip everything but [a-z0-9] for robust name matching."""
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _money(cell: str):
    """First dollar amount in a string -> float, else None. '-' -> None."""
    cell = (cell or "").strip()
    if cell in ("", "-", "—", "n/a", "N/A"):
        return None
    m = MONEY.search(cell)
    return float(m.group(1).replace(",", "")) if m else None


# ---------------------------------------------------------------------------
# Per-provider parsers. Each takes the rendered page text and returns
# {normalized_name: {"name": display, "input": float|None,
#                    "cached": float|None, "output": float|None}}.
# ---------------------------------------------------------------------------

def parse_openai(text: str):
    """Flagship table: a model-id line (gpt-…) followed by up to 8 price cells
    on their own lines — short context (input, cached, cache-write, output)
    then long context. We take short-context standard: input, cached, output."""
    rows = {}
    lines = text.splitlines()
    i = 0
    is_id = re.compile(r"^gpt[a-z0-9.\-]+$")
    while i < len(lines):
        s = lines[i].strip()
        if is_id.match(s.lower()):
            name = s
            cells, j = [], i + 1
            while j < len(lines) and len(cells) < 8:
                c = lines[j].strip()
                if c == "":
                    j += 1
                    continue
                if is_id.match(c.lower()):
                    break
                if c == "-":
                    cells.append(None)
                    j += 1
                    continue
                m = re.fullmatch(r"\$?([\d,]+(?:\.\d+)?)", c)
                if m:
                    cells.append(float(m.group(1).replace(",", "")))
                    j += 1
                    continue
                break  # hit prose / next section
            if len(cells) >= 4:
                rows[norm(name)] = {
                    "name": name,
                    "input": cells[0],
                    "cached": cells[1],
                    "output": cells[3],
                }
            i = j
            continue
        i += 1
    return rows


def parse_anthropic(text: str):
    """Main API table rows: 'Claude X<tab>$in / MTok<tab>$5m<tab>$1h<tab>$read<tab>$out'.
    Exactly 5 '$… / MTok' cells => input=1st, cached(read)=4th, output=5th.
    Later tables (batch, 2 cells) are ignored by the ==5 guard."""
    rows = {}
    for line in text.splitlines():
        if not line.strip().startswith("Claude"):
            continue
        vals = MONEY.findall(line)
        if line.count("/ MTok") == 5 and len(vals) >= 5:
            name = re.split(r"\t", line)[0].strip()
            name = re.sub(r"\s*\(.*?\)\s*$", "", name)  # drop "(deprecated)" etc.
            rows[norm(name)] = {
                "name": name,
                "input": float(vals[0].replace(",", "")),
                "cached": float(vals[3].replace(",", "")),
                "output": float(vals[4].replace(",", "")),
            }
    return rows


def parse_xai(text: str):
    """Rows: 'grok-x<tab>context<tab>$in<tab>$cache<tab>$out<tab>…higher tier'.
    Take standard tier: input, cached, output (cells 2,3,4)."""
    rows = {}
    for line in text.splitlines():
        cells = [c.strip() for c in line.split("\t")]
        if len(cells) >= 5 and cells[0].lower().startswith("grok") and MONEY.search(cells[2]):
            rows[norm(cells[0])] = {
                "name": cells[0],
                "input": _money(cells[2]),
                "cached": _money(cells[3]),
                "output": _money(cells[4]),
            }
    return rows


def parse_deepseek(text: str):
    """Transposed table: a 'MODEL\\t<name1>\\t<name2>' header, then rows
    'CACHE HIT', 'CACHE MISS' (=input), 'OUTPUT TOKENS' (=output)."""
    lines = text.splitlines()
    names, hit, miss, out = [], [], [], []
    for line in lines:
        cells = [c.strip() for c in line.split("\t")]
        head = cells[0].upper()
        # The first pricing row carries an extra 'PRICING' rowspan label cell,
        # so align money values from the right (last len(names) of them).
        def tail(vals):
            return [float(v.replace(",", "")) for v in MONEY.findall(line)][-len(names):] if names else []
        if head == "MODEL" and len(cells) >= 3:
            names = [re.sub(r"\(.*?\)", "", c).strip() for c in cells[1:]]
        elif "CACHE HIT" in line.upper():
            hit = tail(cells)
        elif "CACHE MISS" in line.upper():
            miss = tail(cells)
        elif "OUTPUT TOKEN" in line.upper():
            out = tail(cells)
    rows = {}
    for idx, name in enumerate(names):
        if not name:
            continue
        rows[norm(name)] = {
            "name": name,
            "input": miss[idx] if idx < len(miss) else None,
            "cached": hit[idx] if idx < len(hit) else None,
            "output": out[idx] if idx < len(out) else None,
        }
    return rows


def parse_google(text: str):
    """Per-model sections: a 'Gemini …' heading, then 'Input price … $x',
    'Output price … $y', 'Context caching price … $z' lines (first $ = base
    paid tier). Flushes a model once an input price has been seen."""
    rows = {}
    heading = re.compile(r"^Gemini [\w.\-/ ]+$")
    cur, rec = None, {}

    def flush():
        if cur and rec.get("input") is not None:
            rows[norm(cur)] = {
                "name": cur,
                "input": rec.get("input"),
                "cached": rec.get("cached"),
                "output": rec.get("output"),
            }

    for line in text.splitlines():
        s = line.strip()
        if heading.match(s) and "$" not in s and len(s) < 45 and not s.lower().endswith("price"):
            flush()
            cur, rec = s, {}
            continue
        if cur is None:
            continue
        if s.startswith("Input price"):
            rec["input"] = _money(s)
        elif s.startswith("Output price"):
            rec["output"] = _money(s)
        elif s.startswith("Context caching price"):
            rec["cached"] = _money(s)
    flush()
    return rows


PARSERS = {
    "OpenAI": parse_openai,
    "Anthropic": parse_anthropic,
    "Google": parse_google,
    "xAI": parse_xai,
    "DeepSeek": parse_deepseek,
}


# ---------------------------------------------------------------------------
# Browser scrape
# ---------------------------------------------------------------------------

def _get_text(browser, url, settle=5):
    browser.goto(url, wait_until="load")
    browser.sleep(settle)
    return browser.text("body") or ""


def scrape(verbose=True):
    """Render each official page and parse it. Returns {provider: {norm: row}}."""
    from browser_interface import BrowserInterface
    browser = BrowserInterface.connect_cdp()
    out = {}
    for provider, url in OFFICIAL_PAGES.items():
        try:
            text = _get_text(browser, url)
            rows = PARSERS[provider](text)
        except Exception as e:  # noqa: BLE001
            rows = {}
            if verbose:
                print(f"  ! {provider}: {str(e).splitlines()[0]}")
        out[provider] = rows
        if verbose:
            print(f"  ✓ {provider}: parsed {len(rows)} models from {url}")
    browser.stop()
    return out


# ---------------------------------------------------------------------------
# Overlay
# ---------------------------------------------------------------------------

def overlay(dry_run=False):
    if not DATA_PATH.exists():
        print(f"ERROR: {DATA_PATH} not found — run scrape_pricing.py first.")
        return 1

    dataset = json.loads(DATA_PATH.read_text())
    print("Rendering official provider pricing pages…")
    official = scrape()

    per_provider = {}
    confirmed_names = []
    total_matched = 0

    for m in dataset["models"]:
        prov = m["provider"]
        rows = official.get(prov, {})
        hit = rows.get(norm(m["name"]))
        stats = per_provider.setdefault(prov, {"matched": 0, "catalog": 0})
        stats["catalog"] += 1
        if hit and hit.get("input") is not None:
            m["input_price"] = hit.get("input")
            m["cached_price"] = hit.get("cached")
            m["output_price"] = hit.get("output")
            for f in ("input_price", "cached_price", "output_price"):
                m["provenance"][f] = bd.OFFICIAL
            m["price_stale"] = False
            m["official_source"] = OFFICIAL_PAGES[prov]
            stats["matched"] += 1
            total_matched += 1
            confirmed_names.append(f"{prov}/{m['name']}")

    # Refresh top-level provenance metadata (adds the 'official' tier).
    fresh = bd.assemble(dataset["models"])
    dataset["sources"] = fresh["sources"]
    dataset["provenance_legend"] = fresh["provenance_legend"]

    now = datetime.now(timezone.utc)
    dataset["last_collected"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    dataset["generated_at"] = now.isoformat()
    dataset["official_refresh"] = {
        "method": "browser skill (rendered DOM) of official provider pricing pages",
        "pages": OFFICIAL_PAGES,
        "matched": total_matched,
        "by_provider": {p: s for p, s in per_provider.items()},
        "confirmed": confirmed_names,
        "not_scraped": {
            "Mistral": "mistral.ai/pricing lists subscription plans only, "
                       "not API token prices — kept on aggregator/fallback",
        },
    }

    print(f"\nConfirmed {total_matched} models against official pages:")
    for prov, s in per_provider.items():
        print(f"  {prov:10s} {s['matched']}/{s['catalog']} models -> official")
    print("  Mistral    0 (official page has no API token prices)")

    if dry_run:
        print("\n--dry-run: not writing data/models.json")
        return 0

    DATA_PATH.write_text(json.dumps(dataset, indent=2))
    print(f"\nWrote {DATA_PATH} — {total_matched} models upgraded to 'official'.")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Overlay official-page prices onto the dashboard dataset.")
    ap.add_argument("--dry-run", action="store_true", help="scrape + report, don't write")
    args = ap.parse_args()
    sys.exit(overlay(dry_run=args.dry_run))
