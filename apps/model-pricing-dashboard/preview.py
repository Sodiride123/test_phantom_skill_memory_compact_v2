#!/usr/bin/env python3
"""
Preview the dashboard in one step: serve this folder on an ephemeral port,
render it in the browser skill, write screenshot.png, print the data-health
line as a quick sanity check, and shut the server down cleanly.

    python preview.py                  # -> screenshot.png + prints #data-health
    python preview.py -o out.png       # custom screenshot path
    python preview.py --theme light    # render the light theme instead
    python preview.py --theme both     # dark -> out.png, light -> out-light.png
    python preview.py --no-shot        # just print #data-health, don't write png

Theme switching sets the dashboard's own localStorage key (mpd-theme) and
reloads, so the render exercises the real pre-paint theme path (issue #131/#134).
The browser profile is ephemeral — the saved choice does not leak elsewhere.

No new deps — stdlib http.server (threaded) + the repo's browser module. This
replaces the manual serve / connect / screenshot / kill dance (see issue #94).
"""

import argparse
import contextlib
import functools
import http.server
import socketserver
import sys
import threading
from pathlib import Path

_APP_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _APP_DIR.parents[1]
_BROWSER_DIR = _REPO_ROOT / "browser"
for p in (str(_REPO_ROOT), str(_BROWSER_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args, **kwargs):  # silence per-request logging
        pass


@contextlib.contextmanager
def _serve(directory):
    """Serve `directory` on an ephemeral localhost port in a daemon thread;
    yields the port and tears the server down on exit."""
    handler = functools.partial(_QuietHandler, directory=str(directory))
    # port 0 -> OS picks a free port; avoids clashes with stray servers.
    httpd = socketserver.TCPServer(("127.0.0.1", 0), handler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        yield port
    finally:
        httpd.shutdown()
        httpd.server_close()


def _light_path(dark_path: str) -> str:
    """screenshot.png -> screenshot-light.png (stem + '-light', same dir/ext)."""
    p = Path(dark_path)
    return str(p.with_name(p.stem + "-light" + p.suffix))


def _render(browser, url, theme, settle):
    """Load the dashboard in `theme` and return the #data-health text.

    The preference key is always (re)set and the page reloaded: the persistent
    CDP profile may carry a stale mpd-theme from an earlier run, and this way
    the render deterministically exercises the real pre-paint theme path a
    returning user would hit (issue #131/#134).
    """
    browser.goto(url, wait_until="load")
    if theme == "dark":
        browser.evaluate("localStorage.removeItem('mpd-theme')")
    else:
        browser.evaluate(f"localStorage.setItem('mpd-theme', '{theme}')")
    browser.goto(url, wait_until="load")  # reload -> pre-paint apply
    browser.sleep(settle)
    got = browser.evaluate("document.documentElement.dataset.theme")
    if got != theme:
        raise RuntimeError(f"theme did not apply: expected {theme!r}, got {got!r}")
    return (browser.text("#data-health") or "").strip()


def main():
    ap = argparse.ArgumentParser(description="Serve + screenshot the dashboard.")
    ap.add_argument("-o", "--out", default=str(_APP_DIR / "screenshot.png"),
                    help="screenshot path (default: screenshot.png); with "
                         "--theme both the light shot is <stem>-light.<ext>")
    ap.add_argument("--theme", choices=("dark", "light", "both"), default="dark",
                    help="theme to render (default: dark); 'both' writes two files")
    ap.add_argument("--no-shot", action="store_true",
                    help="don't write a screenshot, just print #data-health")
    ap.add_argument("--settle", type=float, default=2.0,
                    help="seconds to wait for the SPA to render (default 2)")
    args = ap.parse_args()

    # Validate the shipped dataset before rendering — surface integrity issues
    # (bad provenance, negative price, collision smell) as a one-line summary.
    try:
        import validate_dataset as vd
        vd.check_and_report(vd.load_dataset())
    except Exception as e:  # noqa: BLE001 — never let validation block a preview
        print(f"dataset validation: skipped ({e})")

    from browser_interface import BrowserInterface

    themes = ["dark", "light"] if args.theme == "both" else [args.theme]
    shots = {"dark": args.out, "light": _light_path(args.out)}

    with _serve(_APP_DIR) as port:
        url = f"http://127.0.0.1:{port}/"
        print(f"Serving {_APP_DIR.name} at {url}")
        browser = BrowserInterface.connect_cdp()
        try:
            health = None
            for theme in themes:
                health = _render(browser, url, theme, args.settle)
                if not args.no_shot:
                    browser.screenshot(shots[theme], full_page=True)
                    print(f"Wrote {shots[theme]} ({theme})")
            print("\n#data-health:")
            for line in (health or "").splitlines():
                print(f"  {line}")
        finally:
            browser.stop()  # disconnects only; server is closed by the context
    return 0


if __name__ == "__main__":
    sys.exit(main())
