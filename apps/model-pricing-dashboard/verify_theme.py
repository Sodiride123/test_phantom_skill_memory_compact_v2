"""One-off live verification of the theme toggle (issue #131).

Loads the dashboard from the local server, clicks the theme toggle, checks
that data-theme flips + persists in localStorage, and captures a light-theme
screenshot. Leaves the browser back on the default (dark) theme.
"""

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

URL = "http://localhost:8899/index.html"
OUT = _APP_DIR / "screenshot-light.png"

b = BrowserInterface.connect_cdp()
b.goto(URL)
time.sleep(3)  # settle: fetch + charts render

state0 = b.evaluate("document.documentElement.dataset.theme || '(unset)'")
print("initial theme:", state0)

b.click("#theme-toggle")
time.sleep(2)  # charts redraw
state1 = b.evaluate("document.documentElement.dataset.theme")
saved = b.evaluate("localStorage.getItem('mpd-theme')")
label = b.evaluate("document.getElementById('theme-toggle').textContent")
print("after toggle:", state1, "| localStorage:", saved, "| button:", label)
assert state1 == "light" and saved == "light", "toggle did not apply/persist light theme"

b.screenshot(str(OUT), full_page=True)
print("wrote", OUT)

# Reload -> persistence check (preference survives a fresh page load).
b.goto(URL)
time.sleep(3)
state2 = b.evaluate("document.documentElement.dataset.theme")
print("after reload:", state2)
assert state2 == "light", "theme preference did not persist across reload"

# Back to default dark for the checked-in screenshot / other users.
b.evaluate("localStorage.removeItem('mpd-theme')")
print("OK — theme toggle verified; localStorage cleared back to default dark")
