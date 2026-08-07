"""Pytest shim for the price-change badge regression tests (issue #114).

The badge derivation lives in app.js (deriveChanges) and is exercised by the
no-DOM/no-network Node tests in test_changes.js. This wrapper runs that file
via `node` so the JS logic is covered by the same `pytest` invocation as the
Python scraper tests. Skips cleanly if Node isn't installed.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

_APP = Path(__file__).resolve().parent


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_derive_changes_node():
    res = subprocess.run(
        ["node", "test_changes.js"],
        cwd=_APP, capture_output=True, text=True, timeout=60,
    )
    assert res.returncode == 0, (
        f"node test_changes.js failed (rc={res.returncode})\n{res.stdout}\n{res.stderr}"
    )
    assert "test(s) passed" in res.stdout
