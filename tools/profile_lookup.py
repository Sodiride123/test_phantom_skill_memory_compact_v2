#!/usr/bin/env python3
"""
profile_lookup — recover *verifiable* public facts about a person via Tavily.

Motivation: LinkedIn (and similar) profiles are login-gated, so the browser
scrape hits an auth wall (see issue #107). The reliable fallback proven in the
#105 CV build is to query Tavily and read the result *snippets*, which expose a
person's headline, current company, education, and location without login.

This tool systematises that pattern. Given a name plus optional hints, it runs
a few targeted Tavily searches, then extracts fields **only when they appear in
a returned snippet** — every emitted field carries the exact snippet and source
URL it came from. It never invents data, and it flags common-name ambiguity
(multiple distinct LinkedIn profiles) instead of guessing.

Usage:
    python tools/profile_lookup.py "Yu Yan" --company "NinjaTech AI" --hint "Sydney"
    python tools/profile_lookup.py "Yu Yan" --slug yu-y-967989179 --json

Output (human table by default, or --json):
    {
      "name": "Yu Yan",
      "confidence": "high|medium|low",
      "headline":  {"value": ..., "source": <url>, "snippet": ...} | null,
      "company":   {...} | null,
      "education": {...} | null,
      "location":  {...} | null,
      "sources":   [ {url, title, snippet, score} ],
      "notes":     [ "ambiguity / caveat messages" ]
    }

Design guarantee: a field is emitted ONLY if backed by a snippet, and the
snippet + source URL are always attached so the caller can cite/verify.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

# Education keywords used to spot a degree/institution mention in a snippet.
_EDU_RE = re.compile(
    r"(Ph\.?D\.?|Doctor(?:ate)?|Master(?:'s)?|Bachelor(?:'s)?|MBA|B\.?Sc|M\.?Sc|"
    r"B\.?Eng|M\.?Eng|Master of [A-Z][A-Za-z ]+|Bachelor of [A-Z][A-Za-z ]+|"
    r"University of [A-Z][A-Za-z ]+|[A-Z][A-Za-z]+ University|"
    r"[A-Z][A-Za-z ]+ Institute of Technology)",
)

_LINKEDIN_IN_RE = re.compile(r"linkedin\.com/(?:[a-z]{2}/)?in/([A-Za-z0-9\-]+)")


def _get_tavily():
    from clients.tavily_client import Tavily

    return Tavily()


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


def _snippet_around(text: str, match_start: int, width: int = 160) -> str:
    lo = max(0, match_start - width // 3)
    hi = min(len(text), match_start + width)
    seg = text[lo:hi].strip()
    return ("…" if lo > 0 else "") + seg + ("…" if hi < len(text) else "")


def _run_searches(t, name, company, location, slug):
    """Run a handful of targeted queries; return de-duped results (by URL)."""
    queries = []
    if slug:
        queries.append(f'"{slug}" {name}')
    base = name
    if company:
        queries.append(f"{name} {company}")
        base = f"{name} {company}"
    if location:
        queries.append(f"{name} {company or ''} {location}".strip())
    queries.append(f"{name} LinkedIn")
    if not company and not location and not slug:
        queries.append(name)

    seen: dict[str, dict] = {}
    for q in dict.fromkeys(queries):  # preserve order, drop dupes
        try:
            resp = t.search(q, max_results=6)
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(f"WARN: search failed for {q!r}: {e}\n")
            continue
        for r in (resp or {}).get("results", []) if isinstance(resp, dict) else []:
            url = r.get("url", "")
            if not url:
                continue
            # Keep the highest-scoring instance of each URL.
            if url not in seen or (r.get("score", 0) > seen[url].get("score", 0)):
                seen[url] = r
    return list(seen.values())


def _score_result(r, name, company, location, slug):
    """Heuristic relevance score for how well a result matches the target."""
    text = f"{r.get('title','')} {r.get('content','')}".lower()
    score = float(r.get("score", 0) or 0)
    name_tokens = [tok for tok in re.split(r"\s+", name.lower()) if tok]
    if name_tokens and all(tok in text for tok in name_tokens):
        score += 0.5
    if company and company.lower() in text:
        score += 0.6
    if location and location.lower() in text:
        score += 0.4
    url = r.get("url", "").lower()
    if slug and slug.lower() in url:
        score += 2.0  # exact profile match dominates
    if "linkedin.com/in/" in url:
        score += 0.3
    return score


# Generic titles that are not real professional headlines.
_JUNK_HEADLINE_RE = re.compile(
    r"^(official website|home ?page|home|profile|linkedin|about|resume|cv|"
    r"curriculum vitae|portfolio|contact)$",
    re.I,
)


def _extract_headline(cluster, name):
    """Pull a headline from a LinkedIn '/in/' title: 'Name - Headline ...'.

    Only LinkedIn profile pages carry a real headline; other sites' titles
    (e.g. 'Name - Official Website') are rejected to avoid junk values.
    """
    for src in cluster:
        url = (src.get("url") or "").lower()
        if "linkedin.com/in/" not in url:
            continue
        title = _norm(src.get("title", ""))
        title = re.sub(r"\s*[-|]\s*LinkedIn\s*$", "", title, flags=re.I)
        first = name.split()[0]
        m = re.match(rf"\s*{re.escape(first)}[^-–|]*[-–|]\s*(.+)$", title)
        headline = m.group(1).strip() if m else None
        if headline and 3 <= len(headline) <= 120 and not _JUNK_HEADLINE_RE.match(headline):
            return {"value": headline, "source": src.get("url"),
                    "snippet": _norm(src.get("title", ""))}
    return None


def _extract_field_from_hint(cluster, hint, label):
    """Corroborate a hint (company/location) only within the primary cluster."""
    if not hint:
        return None
    for r in cluster:
        text = f"{r.get('title','')} {r.get('content','')}"
        idx = text.lower().find(hint.lower())
        if idx >= 0:
            return {"value": hint, "source": r.get("url"),
                    "snippet": _snippet_around(text, idx)}
    return None


def _extract_education(cluster):
    for r in cluster:
        content = _norm(r.get("content", ""))
        m = _EDU_RE.search(content)
        if m:
            return {"value": m.group(0).strip(), "source": r.get("url"),
                    "snippet": _snippet_around(content, m.start())}
    return None


def _url_slug(url: str):
    m = _LINKEDIN_IN_RE.search(url or "")
    return m.group(1).lower() if m else None


def _primary_cluster(ranked, name, slug):
    """Return (cluster, primary) — results that plausibly refer to ONE person.

    All field extraction happens within this cluster so we never stitch facts
    from different people who share the name (the #108 misattribution risk).
    """
    if not ranked:
        return [], None
    if slug:
        cluster = [r for r in ranked if slug.lower() in (r.get("url", "").lower())]
        return cluster, (cluster[0] if cluster else None)
    # No slug: anchor on the top result; include others sharing its LinkedIn slug.
    primary = ranked[0]
    pslug = _url_slug(primary.get("url", ""))
    if pslug:
        cluster = [r for r in ranked if _url_slug(r.get("url", "")) == pslug]
    else:
        cluster = [primary]
    return cluster, primary


def _detect_ambiguity(results, name, slug):
    """Flag when several distinct LinkedIn /in/ profiles match the name."""
    slugs = set()
    for r in results:
        for m in _LINKEDIN_IN_RE.finditer(r.get("url", "")):
            slugs.add(m.group(1).lower())
    notes = []
    if slug:
        return notes  # caller pinned a specific profile
    if len(slugs) > 1:
        notes.append(
            f"Ambiguous: {len(slugs)} distinct LinkedIn profiles matched "
            f"({', '.join(sorted(slugs)[:5])}{'…' if len(slugs) > 5 else ''}). "
            "Pass --slug or a stronger hint to disambiguate."
        )
    return notes


def lookup(name, company=None, location=None, slug=None):
    """Return a dict of verifiable public facts about the person (see module doc)."""
    t = _get_tavily()
    results = _run_searches(t, name, company, location, slug)
    ranked = sorted(
        results, key=lambda r: _score_result(r, name, company, location, slug),
        reverse=True,
    )
    # Anchor everything to ONE person so we never merge same-name strangers.
    cluster, primary = _primary_cluster(ranked, name, slug)
    notes = _detect_ambiguity(ranked, name, slug)

    if slug and not cluster:
        # The pinned profile did not appear in results — refuse to guess.
        notes.append(
            f"Pinned profile '{slug}' was not present in search results; "
            "not extracting fields from other same-name profiles."
        )
        headline = company_f = location_f = education = None
    else:
        headline = _extract_headline(cluster, name)
        company_f = _extract_field_from_hint(cluster, company, "company")
        location_f = _extract_field_from_hint(cluster, location, "location")
        education = _extract_education(cluster)

    # Confidence heuristic.
    corroborated = sum(bool(x) for x in (headline, company_f, location_f, education))
    slug_matched = bool(slug and cluster)
    if slug_matched and corroborated >= 1:
        confidence = "high"
    elif corroborated >= 2 and not notes:
        confidence = "high"
    elif corroborated >= 1:
        confidence = "medium"
    else:
        confidence = "low"
    if notes and confidence == "high":
        confidence = "medium"
    # If the name is ambiguous and nothing pins the entity (no slug match and no
    # hint corroborated), don't imply MEDIUM trust in fields drawn from the
    # top-ranked stranger — headline/education alone can't disambiguate people.
    hint_corroborated = bool(company_f or location_f)
    if notes and confidence == "medium" and not slug_matched and not hint_corroborated:
        confidence = "low"
    if not ranked:
        notes.append("No search results — cannot verify anything.")

    cluster_urls = {r.get("url") for r in cluster}
    sources = [
        {"url": r.get("url"), "title": _norm(r.get("title", "")),
         "snippet": _norm(r.get("content", ""))[:240],
         "score": round(float(r.get("score", 0) or 0), 3),
         "primary": r.get("url") in cluster_urls}
        for r in ranked[:6]
    ]

    return {
        "name": name,
        "confidence": confidence,
        "headline": headline,
        "company": company_f,
        "education": education,
        "location": location_f,
        "sources": sources,
        "notes": notes,
        "query": {"name": name, "company": company, "location": location, "slug": slug},
    }


def _print_human(res: dict) -> None:
    print(f"Name:       {res['name']}")
    print(f"Confidence: {res['confidence'].upper()}")
    for field in ("headline", "company", "education", "location"):
        v = res.get(field)
        if v:
            print(f"{field.capitalize()+':':11} {v['value']}")
            print(f"{'':11} ↳ {v['source']}")
        else:
            print(f"{field.capitalize()+':':11} (not verified)")
    if res.get("notes"):
        print("\nNotes:")
        for n in res["notes"]:
            print(f"  ⚠️  {n}")
    if res.get("sources"):
        print("\nSources:")
        for s in res["sources"]:
            print(f"  [{s['score']}] {s['url']}")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="profile_lookup",
        description="Recover verifiable public facts about a person via Tavily (no fabrication).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("name", help="Person's full name")
    p.add_argument("--company", default=None, help="Company hint (corroborated if found in a snippet)")
    p.add_argument("--hint", "--location", dest="location", default=None, help="Location hint")
    p.add_argument("--slug", default=None, help="LinkedIn profile slug/URL fragment to pin the match")
    p.add_argument("--json", action="store_true", help="Output JSON")
    a = p.parse_args(argv)
    try:
        res = lookup(a.name, company=a.company, location=a.location, slug=a.slug)
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"ERROR: {e}\n")
        return 1
    if a.json:
        print(json.dumps(res, indent=2))
    else:
        _print_human(res)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
