"""Regression tests for Slack CLI path and module entry points."""

import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "messaging" / "slack" / "interface.py"


@pytest.mark.parametrize("command", ["read", "say", "upload"])
def test_direct_path_subcommand_help(command):
    result = subprocess.run(
        [sys.executable, str(SCRIPT), command, "--help"],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout
    assert "partially initialized module 'logging'" not in result.stderr


def test_module_entrypoint_help_has_no_double_import_warning():
    result = subprocess.run(
        [sys.executable, "-m", "messaging.slack.interface", "--help"],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout
    assert "found in sys.modules" not in result.stderr
