"""Pytest shim for the theme-toggle chart-palette regression tests (issue #131).

The palette lives in app.js (themeChartColors) and is exercised by the
no-DOM/no-network Node tests in test_theme.js. This wrapper runs that file
via `node` so the JS logic is covered by the same `pytest` invocation as the
Python scraper tests. Skips cleanly if Node isn't installed.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

_APP = Path(__file__).resolve().parent


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_theme_chart_colors_node():
    res = subprocess.run(
        ["node", "test_theme.js"],
        cwd=_APP, capture_output=True, text=True, timeout=60,
    )
    assert res.returncode == 0, (
        f"node test_theme.js failed (rc={res.returncode})\n{res.stdout}\n{res.stderr}"
    )
    assert "test(s) passed" in res.stdout
