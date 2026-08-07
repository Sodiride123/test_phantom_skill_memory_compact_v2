"""Regression tests for health-check repository layout validation."""

from pathlib import Path

from tools import health_check as hc


def _create_required_files(root: Path) -> None:
    for relative in hc.REQUIRED_FILES:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()


def test_check_files_accepts_current_checkout():
    result = hc.check_files()
    assert result["status"] == "ok"
    assert str(len(hc.REQUIRED_FILES)) in result["message"]


def test_check_files_reports_current_relative_missing_paths(tmp_path, monkeypatch):
    _create_required_files(tmp_path)
    missing = hc.REQUIRED_FILES[2]
    (tmp_path / missing).unlink()
    monkeypatch.setattr(hc, "REPO_ROOT", tmp_path)

    result = hc.check_files()

    assert result["status"] == "error"
    assert result["missing"] == [missing]


def test_all_ok_components_produce_healthy_overall(tmp_path, monkeypatch):
    _create_required_files(tmp_path)
    monkeypatch.setattr(hc, "REPO_ROOT", tmp_path)
    ok = lambda: {"status": "ok", "message": "ok"}
    monkeypatch.setattr(hc, "check_browser", ok)
    monkeypatch.setattr(hc, "check_messaging_token", ok)
    monkeypatch.setattr(hc, "check_github_token", ok)
    monkeypatch.setattr(hc, "check_settings", ok)
    monkeypatch.setattr(hc, "check_pipedream_health", ok)
    monkeypatch.setattr(hc, "check_claude_cli", ok)

    result = hc.run_health_check()

    assert result["files"]["status"] == "ok"
    assert result["overall"] == "healthy"
