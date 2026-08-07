"""One-off live verification of the theme toggle (issue #131, --url in #134).

Loads the dashboard from a local server, clicks the theme toggle, checks
that data-theme flips + persists in localStorage, and captures a light-theme
screenshot. Leaves the browser back on the default (dark) theme.

    python verify_theme.py                    # against the systemd-served :8899
    python verify_theme.py --url http://127.0.0.1:PORT/   # any local server
"""

import argparse
import sys
import time
from pathlib import Path

_APP_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _APP_DIR.parent.parent
_BROWSER_DIR = _REPO_ROOT / "browser"
for p in (str(_BROWSER_DIR), str(_REPO_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from browser_interface import BrowserInterface  # noqa: E402

DEFAULT_URL = "http://localhost:8899/index.html"
OUT = _APP_DIR / "screenshot-light.png"


def main() -> int:
    ap = argparse.ArgumentParser(description="Live-verify the theme toggle.")
    ap.add_argument("--url", default=DEFAULT_URL,
                    help=f"dashboard URL (default: {DEFAULT_URL})")
    ap.add_argument("-o", "--out", default=str(OUT),
                    help="light-theme screenshot path (default: screenshot-light.png)")
    args = ap.parse_args()

    b = BrowserInterface.connect_cdp()
    b.goto(args.url)
    time.sleep(3)  # settle: fetch + charts render

    state0 = b.evaluate("document.documentElement.dataset.theme || '(unset)'")
    print("initial theme:", state0)

    b.click("#theme-toggle")
    time.sleep(2)  # charts redraw
    state1 = b.evaluate("document.documentElement.dataset.theme")
    saved = b.evaluate("localStorage.getItem('mpd-theme')")
    label = b.evaluate("document.getElementById('theme-toggle').textContent")
    print("after toggle:", state1, "| localStorage:", saved, "| button:", label)
    assert state1 == "light" and saved == "light", \
        "toggle did not apply/persist light theme"

    b.screenshot(args.out, full_page=True)
    print("wrote", args.out)

    # Reload -> persistence check (preference survives a fresh page load).
    b.goto(args.url)
    time.sleep(3)
    state2 = b.evaluate("document.documentElement.dataset.theme")
    print("after reload:", state2)
    assert state2 == "light", "theme preference did not persist across reload"

    # Back to default dark for the checked-in screenshot / other users.
    b.evaluate("localStorage.removeItem('mpd-theme')")
    print("OK — theme toggle verified; localStorage cleared back to default dark")
    return 0


if __name__ == "__main__":
    sys.exit(main())
