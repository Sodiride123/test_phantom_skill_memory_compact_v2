"""Regression tests for processes/health_service.check_model_pricing_health (#135).

No systemd, no network, no PostHog — subprocess.run / urllib.request.urlopen /
capture are monkeypatched at the module seam.
"""

import subprocess
import urllib.error

import pytest

from processes import health_service as hs


@pytest.fixture
def emitted(monkeypatch):
    """Capture PostHog events instead of sending them."""
    events = []
    monkeypatch.setattr(hs, "capture", lambda event, props: events.append((event, props)))
    return events


def _mock_systemctl(monkeypatch, active="active", nrestarts=0, raises=None):
    def fake_run(*a, **kw):
        if raises:
            raise raises
        return subprocess.CompletedProcess(
            a[0], 0, stdout=f"ActiveState={active}\nNRestarts={nrestarts}\n",
            stderr="")
    monkeypatch.setattr(hs.subprocess, "run", fake_run)


class _FakeResp:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _mock_urlopen(monkeypatch, resp=None, raises=None):
    def fake_urlopen(url, timeout=None):
        if raises:
            raise raises
        return resp or _FakeResp()
    monkeypatch.setattr(hs.urllib.request, "urlopen", fake_urlopen)


def test_ok_when_unit_active_and_http_200(monkeypatch, emitted):
    _mock_systemctl(monkeypatch)
    _mock_urlopen(monkeypatch)
    assert hs.check_model_pricing_health() == 0
    assert emitted == []  # fail-only: healthy emits nothing


def test_fails_when_unit_inactive(monkeypatch, emitted):
    _mock_systemctl(monkeypatch, active="failed", nrestarts=42)
    _mock_urlopen(monkeypatch)
    assert hs.check_model_pricing_health() == 1
    assert len(emitted) == 1
    event, props = emitted[0]
    assert event == "ninja model-pricing health"
    assert props["error"] == 1
    assert props["status"] == "unit_failed"
    assert props["nrestarts"] == 42  # crash-loop context rides along


def test_fails_when_systemctl_errors(monkeypatch, emitted):
    _mock_systemctl(monkeypatch, raises=subprocess.TimeoutExpired("systemctl", 10))
    _mock_urlopen(monkeypatch)
    assert hs.check_model_pricing_health() == 1
    assert emitted[0][1]["status"] == "systemctl_error"


def test_fails_on_http_error(monkeypatch, emitted):
    _mock_systemctl(monkeypatch)
    _mock_urlopen(monkeypatch, raises=urllib.error.URLError("connection refused"))
    assert hs.check_model_pricing_health() == 1
    assert emitted[0][1]["status"] == "http_error"


def test_fails_on_http_500(monkeypatch, emitted):
    _mock_systemctl(monkeypatch)

    class _Resp500(_FakeResp):
        status = 500
        headers = None

    _mock_urlopen(monkeypatch, resp=_Resp500())
    assert hs.check_model_pricing_health() == 1
    assert emitted[0][1]["status"] == "http_error"


def test_handles_missing_nrestarts(monkeypatch, emitted):
    monkeypatch.setattr(
        hs.subprocess, "run",
        lambda *a, **kw: subprocess.CompletedProcess(
            a[0], 0, stdout="ActiveState=active\n", stderr=""))
    _mock_urlopen(monkeypatch)
    assert hs.check_model_pricing_health() == 0  # None nrestarts must not crash
