#!/usr/bin/env python3
"""
Health Service — Periodically validates external-service credentials.

This service runs independently of the monitor. It wakes up every 15 minutes
and validates:
  - the active messaging channel credentials (Slack/WhatsApp/Teams via ABC),
  - the GitHub token in /dev/shm/mcp-token (gh auth status),
  - the LiteLLM gateway connection (authenticated GET /v1/models — validates
    the key and connectivity without consuming tokens),
  - the Pipedream Connect credentials (minimal catalog call),
  - the browser VPN (Psiphon tunnel liveness + IP egress check),
  - the monitor process heartbeat (stale file detection).

Each check emits a PostHog metric **only when it fails** (``error=1``); a
healthy check emits nothing. Every emission includes the sandbox ID, so we can
alert on an expired credential or a broken gateway without coupling the check to
the monitor's message-polling loop.

Usage:
    python processes/health_service.py                 # run forever, 15-min interval
    python processes/health_service.py --interval 600  # custom interval in seconds
    python processes/health_service.py --once          # run a single check and exit
"""

import argparse
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

from browser.browser_server import PSIPHON_HOST, PSIPHON_PORT, PSIPHON_PROXY
from clients.litellm_client import (
    _DEFAULT_BACKOFF_BASE,
    _DEFAULT_MAX_RETRIES,
    RETRIABLE_5XX,
    api_url,
    get_config,
    get_headers,
)
from clients.posthog_client import capture
from messaging import get_messaging_interface
from processes.common import MONITOR_HEARTBEAT_FILE
from tools.token_health import check_github_token
from utils.pipedream import PipedreamClient

# How often to check, in seconds.
CHECK_INTERVAL = 15 * 60  # 15 minutes

# Lightweight IP-echo endpoint used to confirm browser traffic routes through
# the Psiphon tunnel and that the egress IP differs from the sandbox's direct IP.
IP_ECHO_URL = "https://api.ipify.org?format=json"

# A heartbeat file older than this (seconds) means the monitor has stalled.
MONITOR_STALE_AFTER = 5 * 60  # 5 minutes


def _print(msg: str) -> None:
    """Print msg to stdout with a timestamp prefix matching other ninja logs."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{ts} | {msg}", flush=True)


# Liveness heartbeat for this service: overwritten with the current unix
# timestamp at the end of every check cycle. Mirrors MONITOR_HEARTBEAT_FILE so
# an external watcher can detect a stalled health service. Lives in /tmp.
HEALTH_HEARTBEAT_FILE = Path("/tmp/ninja_health_heartbeat")


def write_health_heartbeat() -> None:
    """Overwrite HEALTH_HEARTBEAT_FILE with the current unix timestamp.

    Called at the end of every check cycle so supervisord/monitoring can
    detect a stalled health service. Best-effort — never raises.
    """
    try:
        HEALTH_HEARTBEAT_FILE.write_text(str(int(time.time())))
    except OSError:
        pass


def _emit_error(event: str, status: str, **extra) -> None:
    """Emit a health metric with ``error=1`` — only called when a check fails."""
    capture(event, {"error": 1, "status": status, **extra})


def check_messaging_health() -> int:
    """Validate the active messaging channel credentials via the ABC.

    Resolves the active channel from MESSAGING_CHANNEL env-var (default: slack)
    and delegates to the adapter's check_messaging_health() implementation.
    Returns 1 on error, 0 when credentials are valid.
    """
    channel = os.environ.get("MESSAGING_CHANNEL", "slack")
    try:
        result = get_messaging_interface().check_messaging_health()
    except Exception as e:
        result = {"service": channel, "status": "error", "message": str(e)}

    if result["status"] == "ok":
        _print(f"🔑 {channel} token OK")
        return 0

    _emit_error(
        f"ninja {channel} health",
        result["status"],
        message=result.get("message", ""),
    )
    _print(
        f"🔑 {channel} token ERROR (status={result['status']}"
        f"{', ' + result['message'] if result.get('message') else ''})"
    )
    return 1


def check_github_health() -> int:
    """Emit ``ninja github health`` (error=1) only if the GitHub token is bad.

    Returns 1 on error, 0 when the token is valid. A missing token counts as an
    error (logged as "Github token not found").
    """
    result = check_github_token()
    if result["status"] == "ok":
        _print("🔑 GitHub token OK")
        return 0

    if result["status"] == "missing":
        status_message = "Github token not found"
    else:
        status_message = (
            f"GitHub token ERROR (status={result['status']}"
            f"{', ' + result['message'] if result.get('message') else ''})",
        )

    _print(status_message)
    _emit_error(
        "ninja github health", status_message, message=result.get("message", "")
    )
    return 1


def check_litellm_health() -> int:
    """Emit ``ninja litellm health`` (error=1) only if the gateway probe fails.

    Issues an authenticated ``GET /v1/models`` against the gateway. This both
    validates the API key (401/403 on a bad/expired key) and confirms
    connectivity, without invoking a model or consuming any tokens. Returns 1 on
    error, 0 on success.

    Transient 5xx responses (500, 502, 503, 504) and read timeouts are retried
    up to 3 times with exponential backoff (1s, 2s, 4s) before reporting a
    failure. Each attempt allows up to 30 seconds for a response.
    """
    cfg = get_config()
    api_key = cfg.get("api_key")
    base_url = cfg.get("base_url")

    if not api_key or not base_url:
        error_message = (
            "LiteLLM not configured (missing api_key/base_url in settings.json)"
        )
        _print(error_message)
        _emit_error("ninja litellm health", error_message, message=error_message)
        return 1

    req = urllib.request.Request(
        api_url("/v1/models"),
        headers=get_headers(),
        method="GET",
    )

    status = "unknown"
    timed_out = False
    for attempt in range(1, _DEFAULT_MAX_RETRIES + 1):
        timed_out = False
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                status = (
                    "ok"
                    if resp.status < 300
                    or resp.status == 402  # 402 means payment required
                    else f"http_{resp.status}"
                )
        except urllib.error.HTTPError as e:
            status = f"http_{e.code}"
        except (socket.timeout, TimeoutError) as e:
            status = str(e)[:120]
            timed_out = True
        except Exception as e:
            status = str(e)[:120]

        if status == "ok":
            break

        # Retry on transient 5xx codes or timeouts
        try:
            code = int(status.split("_")[1]) if status.startswith("http_") else None
        except (IndexError, ValueError):
            code = None

        if (timed_out or code in RETRIABLE_5XX) and attempt < _DEFAULT_MAX_RETRIES:
            time.sleep(_DEFAULT_BACKOFF_BASE * (2 ** (attempt - 1)))
            continue

        break

    if status == "ok":
        _print("🤖 LiteLLM OK")
        return 0
    _emit_error("ninja litellm health", status, message=status)
    _print(f"🤖 LiteLLM ERROR (status={status})")
    return 1


def check_pipedream_health() -> int:
    """Emit ``ninja pipedream health`` (error=1) only if the Pipedream probe fails.

    GETs /ninja/integrations-gateway/health — a lightweight endpoint that
    requires no auth and has no side effects.
    Returns 1 on error, 0 on success.

    Transient 5xx responses (500, 502, 503, 504) are retried automatically
    by PipedreamClient (via tenacity) before the exception propagates here.
    """
    try:
        PipedreamClient().check_health()
        _print("🔌 Pipedream OK")
        return 0
    except Exception as e:
        err = str(e)[:120]
        _emit_error("ninja pipedream health", "error", message=err)
        _print(f"🔌 Pipedream ERROR ({err})")
        return 1


def _fetch_egress_ip(proxy: str | None) -> str | None:
    """Return the public egress IP via IP_ECHO_URL, optionally through proxy."""
    handler = (
        urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        if proxy
        else urllib.request.ProxyHandler({})
    )
    opener = urllib.request.build_opener(handler)
    try:
        with opener.open(IP_ECHO_URL, timeout=15) as resp:
            return json.loads(resp.read().decode()).get("ip")
    except Exception:
        return None


def check_vpn_health() -> int:
    """Emit ``ninja vpn health`` (error=1) only if the browser VPN is broken.

    Layered probe: port open → traffic routes → egress IP differs from direct IP.
    Returns 1 on error, 0 on success.
    """
    try:
        with socket.create_connection((PSIPHON_HOST, PSIPHON_PORT), timeout=5):
            pass
    except OSError as e:
        _emit_error("ninja vpn health", "VPN proxy_down", message=str(e)[:120])
        _print(f"VPN ERROR (proxy {PSIPHON_PROXY} not listening: {e})")
        return 1

    proxied_ip = _fetch_egress_ip(PSIPHON_PROXY)
    if not proxied_ip:
        error_message = "VPN ERROR (proxy up but no route to internet)"
        _emit_error("ninja vpn health", error_message, message=error_message)
        _print(error_message)
        return 1

    direct_ip = _fetch_egress_ip(None)
    if direct_ip and direct_ip == proxied_ip:
        error_message = "VPN ERROR (egress IP == direct IP; not tunneling)"
        _emit_error("ninja vpn health", error_message, message=error_message)
        _print(error_message)
        return 1

    _print("🛡️ VPN OK")
    return 0


def check_monitor_health() -> int:
    """Emit ``ninja monitor health`` (error=1) only if the monitor heartbeat is stale.

    The monitor overwrites MONITOR_HEARTBEAT_FILE with a unix timestamp on every
    poll tick. A missing or stale file means the monitor has stalled.
    Returns 1 on error, 0 when fresh.
    """
    try:
        with open(MONITOR_HEARTBEAT_FILE) as f:
            last_run_ts = int(f.read().strip())
    except (OSError, ValueError) as e:
        _emit_error(
            "ninja monitor health", "Monitor heartbeat missing", message=str(e)[:120]
        )
        _print(f"📡 Monitor heartbeat missing ({MONITOR_HEARTBEAT_FILE}: {e})")
        return 1

    age_seconds = int(time.time()) - last_run_ts
    if age_seconds > MONITOR_STALE_AFTER:
        _emit_error(
            "ninja monitor health", "Monitor heartbeat stale", age_seconds=age_seconds
        )
        _print(
            f"📡 Monitor heartbeat STALE "
            f"(age={age_seconds}s > {MONITOR_STALE_AFTER}s)"
        )
        return 1

    _print(f"📡 Monitor heartbeat OK (age={age_seconds}s)")
    return 0


def main():
    channel = os.environ.get("MESSAGING_CHANNEL", "slack")

    parser = argparse.ArgumentParser(
        description="Health Service - periodically validate service credentials"
    )
    parser.add_argument(
        "--interval",
        "-i",
        type=int,
        default=CHECK_INTERVAL,
        help=f"Check interval in seconds (default: {CHECK_INTERVAL})",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single check and exit (don't loop)",
    )
    parser.add_argument(
        "--status-file",
        default=None,
        help="With --once: write a JSON map {check: 0|1} (0=ok, 1=fail) here. "
        "Enables an external caller (e.g. ninja-upgrade smoke check) to gate on "
        "a differential regression rather than absolute health.",
    )
    args = parser.parse_args()

    if args.once:
        # Each check returns 0 (ok) / 1 (fail); a crash counts as a failure so
        # the verdict is never silently optimistic.
        def _safe(check):
            try:
                return check()
            except Exception as e:  # noqa: BLE001 — a crashed check is a failed check
                _print(f"⚠️ {check.__name__} crashed: {e}")
                return 1

        results = {
            "messaging": _safe(check_messaging_health),
            "github": _safe(check_github_health),
            "litellm": _safe(check_litellm_health),
            "pipedream": _safe(check_pipedream_health),
            "vpn": _safe(check_vpn_health),
            "monitor": _safe(check_monitor_health),
        }
        write_health_heartbeat()
        if args.status_file:
            try:
                Path(args.status_file).write_text(json.dumps(results))
            except OSError as e:
                _print(f"⚠️ could not write status file: {e}")
        sys.exit(sum(1 for v in results.values() if v))

    _print(
        f"🏥 Health service started — checking {channel}, GitHub, LiteLLM, "
        f"Pipedream, VPN and monitor every {args.interval // 60} min"
    )

    checks = (
        ("messaging", check_messaging_health),
        ("github", check_github_health),
        ("litellm", check_litellm_health),
        ("pipedream", check_pipedream_health),
        ("vpn", check_vpn_health),
        ("monitor", check_monitor_health),
    )

    try:
        while True:
            for name, check in checks:
                try:
                    check()
                except Exception as e:
                    _print(f"⚠️ {name} health check crashed: {e}")
            write_health_heartbeat()
            time.sleep(args.interval)
    except KeyboardInterrupt:
        _print("👋 Health service stopped")


if __name__ == "__main__":
    main()
