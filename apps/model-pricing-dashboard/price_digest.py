#!/usr/bin/env python3
"""
price_digest.py — Weekly Slack digest of AI model price changes (issue #116).

Diffs the two most recent snapshots in data/price_history.json and produces a
short digest: top price movers (up/down), new/removed models, and the change in
fallback share (how many models are NOT on a confirmed 'official' price). It is
SILENT when nothing changed between the two runs (no movers, no new/removed
models, fallback share flat).

Run standalone:
    python price_digest.py            # print digest (or nothing if no changes)
    python price_digest.py --json     # print the digest dict
    python price_digest.py --post     # post to Slack via messaging/slack/interface.py
    python price_digest.py --post --dry-run  # print, don't post

The weekly-pricing-refresh cron runs this with --post after scrape_official.py
+ validate_dataset.py. Deterministic pure function digest_from_history() is
unit-tested (test_price_digest.py) without touching Slack or the filesystem.
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

_APP_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _APP_DIR.parents[1]
HISTORY_PATH = _APP_DIR / "data" / "price_history.json"
SLACK_CLI = _REPO_ROOT / "messaging" / "slack" / "interface.py"

PRICE_FIELDS = ("input", "cached", "output")  # history record field names


def _load_history(path=HISTORY_PATH):
    if path.exists():
        try:
            data = json.loads(path.read_text())
            if isinstance(data, list):
                return data
        except (ValueError, OSError):
            pass
    return []


def _num(v):
    return v if isinstance(v, (int, float)) else None


def _fallback_share(prices):
    """Fraction (0..1) of models NOT on an 'official' price."""
    if not prices:
        return None
    non_official = sum(1 for r in prices.values() if r.get("provenance") != "official")
    return non_official / len(prices)


def digest_from_history(history):
    """Build the digest from the two most recent snapshots.

    Returns None when there is nothing to report (fewer than 2 snapshots, or no
    movers / new / removed / fallback-share change). Otherwise a dict:
      {date, prev_date, movers: [{model, field, from, to, pct, dir}],
       new: [model], removed: [model],
       fallback_share: {from, to, delta_pp}}
    """
    if not isinstance(history, list) or len(history) < 2:
        return None
    prev, cur = history[-2], history[-1]
    prev_p, cur_p = (prev or {}).get("prices", {}), (cur or {}).get("prices", {})
    if not prev_p or not cur_p:
        return None

    # New / removed models (by "Provider/Name" key).
    new = sorted(k for k in cur_p if k not in prev_p)
    removed = sorted(k for k in prev_p if k not in cur_p)

    # Price movers across the three price fields.
    movers = []
    for key in cur_p:
        if key not in prev_p:
            continue
        for f in PRICE_FIELDS:
            old, new_v = _num(prev_p[key].get(f)), _num(cur_p[key].get(f))
            if old is None or new_v is None or old == new_v:
                continue
            pct = round((new_v - old) / old * 100, 1) if old else None
            movers.append({
                "model": key, "field": f, "from": old, "to": new_v,
                "pct": pct, "dir": "up" if new_v > old else "down",
            })
    # Biggest absolute % moves first; None pct (from == 0) sorts last.
    movers.sort(key=lambda m: -(abs(m["pct"]) if m["pct"] is not None else -1))

    # Fallback-share change (percentage points).
    fs_prev, fs_cur = _fallback_share(prev_p), _fallback_share(cur_p)
    fallback_share = None
    if fs_prev is not None and fs_cur is not None:
        delta_pp = round((fs_cur - fs_prev) * 100, 1)
        fallback_share = {
            "from": round(fs_prev * 100, 1), "to": round(fs_cur * 100, 1),
            "delta_pp": delta_pp,
        }

    if not movers and not new and not removed and (fallback_share is None or fallback_share["delta_pp"] == 0):
        return None  # nothing changed -> silent no-op

    return {
        "date": cur.get("date", ""),
        "prev_date": prev.get("date", ""),
        "movers": movers,
        "new": new,
        "removed": removed,
        "fallback_share": fallback_share,
    }


def format_digest(d, top=5):
    """Render the digest dict as a short Slack-ready Markdown message."""
    date = (d.get("date") or "")[:10]
    lines = [f"💹 *Weekly AI model price digest* — run {date}"]
    movers = d.get("movers", [])
    if movers:
        ups = [m for m in movers if m["dir"] == "up"][:top]
        downs = [m for m in movers if m["dir"] == "down"][:top]
        if ups:
            lines.append(f"📈 *{len([m for m in movers if m['dir']=='up'])} price increase(s):*")
            for m in ups:
                lines.append(f"  • {m['model']} {m['field']}: ${m['from']:g} → ${m['to']:g} ({m['pct']:+.1f}%)")
        if downs:
            lines.append(f"📉 *{len([m for m in movers if m['dir']=='down'])} price decrease(s):*")
            for m in downs:
                lines.append(f"  • {m['model']} {m['field']}: ${m['from']:g} → ${m['to']:g} ({m['pct']:+.1f}%)")
    if d.get("new"):
        lines.append(f"🆕 *{len(d['new'])} new model(s):* " + ", ".join(d["new"][:top]))
    if d.get("removed"):
        lines.append(f"🗑️ *{len(d['removed'])} removed:* " + ", ".join(d["removed"][:top]))
    fs = d.get("fallback_share")
    if fs and fs["delta_pp"] != 0:
        arrow = "⬆" if fs["delta_pp"] > 0 else "⬇"
        lines.append(f"🧭 Fallback share: {fs['from']}% → {fs['to']}% "
                     f"({arrow} {abs(fs['delta_pp'])}pp vs last run)")
    return "\n".join(lines)


def post_to_slack(text):
    """Post via the repo's Slack CLI. Returns (ok, stdout/stderr)."""
    if not SLACK_CLI.exists():
        return False, f"slack cli not found at {SLACK_CLI}"
    res = subprocess.run(
        [sys.executable, str(SLACK_CLI), "say", text],
        cwd=_REPO_ROOT, capture_output=True, text=True, timeout=60,
    )
    return res.returncode == 0, (res.stdout + res.stderr).strip()


def main():
    ap = argparse.ArgumentParser(description="Weekly AI model price-change digest.")
    ap.add_argument("--json", action="store_true", help="print the digest dict")
    ap.add_argument("--post", action="store_true", help="post to Slack")
    ap.add_argument("--dry-run", action="store_true", help="with --post: print, don't send")
    args = ap.parse_args()

    history = _load_history()
    d = digest_from_history(history)

    if d is None:
        print("No price changes since the last run — silent no-op.")
        return 0

    if args.json:
        print(json.dumps(d, indent=2))
    text = format_digest(d)
    print(text)

    if args.post:
        if args.dry_run:
            print("\n--dry-run: not posting to Slack")
        else:
            ok, out = post_to_slack(text)
            print(f"\nSlack post: {'OK' if ok else 'FAILED'}")
            if out:
                print(out)
            return 0 if ok else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
