"""
Shared process-layer utilities and implementations — not channel-specific.

This module holds:
  - Constants (poll intervals, backoff settings, heartbeat paths)
  - Rate limit handler
  - Heartbeat / orchestrator launch / GitHub token sync helpers
  - ``PollingMonitorStrategy`` — the concrete poll-based monitor loop that
    Slack and Teams inherit from

Per-channel subclasses live in ``processes/<channel>/`` and are pruned at
build time by ``channel_builder``.  This file is never pruned.

Dependency flow::

    base.py          <- ABCs (no deps)
    common.py        <- shared utilities + PollingMonitorStrategy (imports base.py)
    factory.py       <- imports slack/, teams/, whatsapp/ strategies
    slack/           <- imports common.py
    teams/           <- imports common.py
    whatsapp/        <- imports base.py directly
    monitor.py       <- thin entry point (imports factory.py, no one imports it)
"""

from __future__ import annotations

import argparse
import logging
import random
import subprocess
import sys
import time
from pathlib import Path

from agents_config import AGENTS
from clients.posthog_client import capture
from core.config import (
    install_sighup_handler,
    is_orchestrator_enabled,
    load_agent_config,
    load_agent_messages,
    load_seen_messages,
    save_agent_messages,
    save_seen_messages,
)
from messaging.factory import get_messaging_interface, resolve_messaging_channel
from processes.base import MonitorStrategy
from processes.orchestrator import (
    ORCHESTRATOR_SERVICE,
    count_open_issues,
    is_orchestrator_running,
    login_github_cli,
)
from services.cron_service import claim_cron, get_due_cron_messages
from services.monitor_service import (
    build_welcome_message,
    build_welcome_signature,
    run_batched_response,
)
from tools.token_health import check_github_token
from utils.cost import check_cost_limit

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

POLL_INTERVAL = 60  # base seconds
POLL_JITTER = 5  # random jitter seconds
MAX_RUNTIME = 24 * 60 * 60  # 24 hours in seconds

BACKOFF_INITIAL = 60  # Initial backoff: 1 minute
BACKOFF_MAX = 600  # Max backoff: 10 minutes
BACKOFF_MULTIPLIER = 2

# Liveness heartbeat — overwritten with the current unix timestamp on every poll
# tick. processes/health_service.py reads it to surface monitor liveness to PostHog.
# Lives in /tmp (sandbox-local), mirroring the orchestrator's heartbeat file.
MONITOR_HEARTBEAT_FILE = Path("/tmp/ninja_monitor_heartbeat")

# How often the monitor checks the gh session against the rotated mcp-token.
# Infra rotates /dev/shm/mcp-token, but gh's hosts.yml only gets a copy at
# orchestrator startup, so the gh session goes stale after each rotation.
GH_TOKEN_SYNC_INTERVAL = 5 * 60  # seconds


# ---------------------------------------------------------------------------
# Rate limit handler
# ---------------------------------------------------------------------------


class RateLimitHandler:
    """Handles exponential backoff for rate limiting."""

    def __init__(self):
        self.current_backoff = 0
        self.consecutive_rate_limits = 0
        self.last_rate_limit_time = 0

    def on_rate_limit(self):
        self.consecutive_rate_limits += 1
        self.last_rate_limit_time = time.time()
        if self.current_backoff == 0:
            self.current_backoff = BACKOFF_INITIAL
        else:
            self.current_backoff = min(
                self.current_backoff * BACKOFF_MULTIPLIER, BACKOFF_MAX
            )
        print(
            f"\u26a0\ufe0f Rate limited! Backing off for {self.current_backoff}s "
            f"(attempt #{self.consecutive_rate_limits})",
            flush=True,
        )
        return self.current_backoff

    def on_success(self):
        if self.consecutive_rate_limits > 0:
            print(
                f"\u2705 Rate limit cleared after {self.consecutive_rate_limits} retries",
                flush=True,
            )
        self.current_backoff = 0
        self.consecutive_rate_limits = 0

    def is_backing_off(self) -> bool:
        if self.current_backoff == 0:
            return False
        return (time.time() - self.last_rate_limit_time) < self.current_backoff

    def get_remaining_backoff(self) -> float:
        if not self.is_backing_off():
            return 0
        return max(0, self.current_backoff - (time.time() - self.last_rate_limit_time))


rate_limiter = RateLimitHandler()


# ---------------------------------------------------------------------------
# Orchestrator + heartbeat helpers
# ---------------------------------------------------------------------------


def write_monitor_heartbeat() -> None:
    """Overwrite MONITOR_HEARTBEAT_FILE with the current unix timestamp.

    Called on every poll tick so health_service.py can detect a stalled monitor.
    Best-effort — never raises.
    """
    try:
        MONITOR_HEARTBEAT_FILE.write_text(str(int(time.time())))
    except OSError:
        pass


def maybe_sync_github_token(last_sync_check: float) -> float:
    """Re-sync a rotated mcp-token into gh's hosts.yml when the session is stale.

    health_service.py only observes and reports; this is the remediation half.
    Runs at most every GH_TOKEN_SYNC_INTERVAL seconds. When check_github_token()
    reports "invalid" (hosts.yml token dead but mcp-token holds one), re-runs
    the orchestrator's login_github_cli() to refresh the gh session.

    Returns the timestamp of this check (or last_sync_check if skipped).
    Best-effort — never raises.
    """
    now = time.time()
    if now - last_sync_check < GH_TOKEN_SYNC_INTERVAL:
        return last_sync_check

    try:
        result = check_github_token()
        if result.get("status") == "invalid":
            print(
                "\U0001f504 gh session stale after mcp-token rotation \u2014 re-syncing...",
                flush=True,
            )
            if login_github_cli(logging.getLogger(__name__)):
                print("\u2705 gh session recovered from rotated mcp-token", flush=True)
            else:
                print(
                    "\u26a0\ufe0f gh re-login failed; will retry next interval",
                    flush=True,
                )
    except Exception as e:
        print(f"\u26a0\ufe0f GitHub token sync check failed: {e}", file=sys.stderr)

    return now


def maybe_launch_orchestrator() -> bool:
    """Launch the orchestrator if there is open work and it isn't already running.

    Always launches via systemd (ninja.service). See agent-docs/LOOP.md.
    """
    if not is_orchestrator_enabled():
        print("⏸️ Orchestrator disabled via config — skipping launch", flush=True)
        return False

    if is_orchestrator_running():
        return False
    open_issues = count_open_issues()
    if open_issues <= 0:
        return False

    print(
        f"\U0001f680 {open_issues} open issue(s) and orchestrator idle \u2014 launching",
        flush=True,
    )
    try:
        result = subprocess.run(
            ["systemctl", "start", ORCHESTRATOR_SERVICE],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            print(f"\u2705 Started {ORCHESTRATOR_SERVICE} via systemd", flush=True)
            return True
        print(
            f"\u26a0\ufe0f systemctl start {ORCHESTRATOR_SERVICE} failed "
            f"({result.returncode}): {result.stderr.strip()}",
            flush=True,
        )
        return False
    except (OSError, subprocess.SubprocessError) as e:
        print(f"\u26a0\ufe0f Could not launch {ORCHESTRATOR_SERVICE}: {e}", flush=True)
        return False


def maybe_emit_heartbeat(
    agent_id: str, start_time: float, last_heartbeat: float
) -> float:
    """Emit a monitor-alive heartbeat metric at most once per minute.

    Also refreshes the /tmp liveness file on every call (cheap, not rate-limited)
    so health_service.py tracks the poll loop even between PostHog emissions.
    """
    # Refresh liveness file every tick regardless of rate-limit window
    write_monitor_heartbeat()

    now = time.time()
    if now - last_heartbeat < 60:
        return last_heartbeat
    capture("ninja monitor heartbeat", {"uptime_seconds": int(now - start_time)})
    print(f"\U0001f497 Emitted heartbeat for {agent_id}", flush=True)
    return now


# ---------------------------------------------------------------------------
# Shared polling monitor strategy
# ---------------------------------------------------------------------------


class PollingMonitorStrategy(MonitorStrategy):
    """Poll-based monitor loop shared by channels that use the same pattern.

    Channels like Slack and Teams use an identical poll -> collect -> dispatch
    loop.  This base class provides the full implementation so per-channel
    subclasses (``processes/slack/monitor_strategy.py``, etc.) only need to
    inherit -- no code duplication.

    Channels with a fundamentally different monitor (e.g. WhatsApp) implement
    ``MonitorStrategy`` directly instead of subclassing this.
    """

    def run(self) -> int:  # noqa: C901 — long but linear; splitting hurts readability
        # Wire SIGHUP -> refresh all config caches (config hot-reload without restart)
        install_sighup_handler()

        parser = argparse.ArgumentParser(
            description="Agent Monitor - Watch the messaging channel for mentions"
        )
        parser.add_argument(
            "--agent", "-a", help="Agent to run as (default: from config)"
        )
        parser.add_argument(
            "--interval",
            "-i",
            type=int,
            default=POLL_INTERVAL,
            help="Poll interval in seconds",
        )
        args = parser.parse_args()

        config = load_agent_config()
        agent_id = args.agent or config.get("default_agent", "").lower()

        if not agent_id or agent_id not in AGENTS:
            print("\u274c No valid agent configured!", file=sys.stderr)
            print(f"Available agents: {', '.join(AGENTS.keys())}", file=sys.stderr)
            print("Set 'default_agent' in ~/.agent_settings.json", file=sys.stderr)
            return 1

        agent = AGENTS[agent_id]
        channel = resolve_messaging_channel()

        print(
            f"""
\u2554{'=' * 60}\u2557
\u2551  {agent['emoji']} {agent['name']} Monitor - Watching for mentions
\u2560{'=' * 60}\u2563
\u2551  Agent:    {agent['name']} ({agent['role']})
\u2551  Channel:  {channel}
\u2551  Polling:  Every {args.interval}s (+{POLL_JITTER}s jitter)
\u2551  Runtime:  max {MAX_RUNTIME // 60} minutes
\u2551  Mentions: {', '.join(agent['mentions'])}
\u255a{'=' * 60}\u255d
""",
            flush=True,
        )

        iface = get_messaging_interface()
        seen_messages = load_seen_messages()
        agent_data = load_agent_messages()
        start_time = time.time()
        last_heartbeat = 0.0
        last_gh_sync_check = 0.0

        iface.post_welcome_if_needed(
            agent, build_welcome_message(agent), build_welcome_signature(agent)
        )

        print(
            f"\U0001f4e1 Starting monitor loop (max {MAX_RUNTIME // 60} minutes)...",
            flush=True,
        )

        try:
            while True:
                last_heartbeat = maybe_emit_heartbeat(
                    agent_id, start_time, last_heartbeat
                )
                last_gh_sync_check = maybe_sync_github_token(last_gh_sync_check)

                if time.time() - start_time >= MAX_RUNTIME:
                    print(
                        f"\n\u23f0 Max runtime ({MAX_RUNTIME // 60} minutes) reached."
                        " Stopping.",
                        flush=True,
                    )
                    break

                if rate_limiter.is_backing_off():
                    remaining = rate_limiter.get_remaining_backoff()
                    print(
                        f"\u23f3 Rate limit backoff: {remaining:.0f}s remaining...",
                        flush=True,
                    )
                    time.sleep(min(remaining, 30))
                    continue

                # --- collect messages ---
                try:
                    raw_messages = iface.get_history(limit=50)
                    rate_limiter.on_success()
                except Exception as e:
                    err = str(e).lower()
                    if "ratelimit" in err or "rate" in err:
                        backoff_time = rate_limiter.on_rate_limit()
                        time.sleep(min(backoff_time, 30))
                    else:
                        print(
                            f"\u26a0\ufe0f Error reading messages: {e}",
                            file=sys.stderr,
                        )
                    continue

                print(f"\U0001f4e8 Got {len(raw_messages)} messages", flush=True)

                pending_messages: list = []

                for msg in raw_messages:
                    iface.collect_pending(
                        msg,
                        agent.get("mentions", []),
                        seen_messages,
                        agent_data,
                        pending_messages,
                    )

                # --- inject due cron jobs ---
                for job in get_due_cron_messages(time.time()):
                    if claim_cron(job["id"]):
                        pending_messages.append(
                            {
                                "user": "cron",
                                "text": job["prompt"],
                                "timestamp": f"cron:{job['id']}:{int(time.time())}",
                                "thread_ts": job.get("thread_ts"),
                                "type": "cron",
                                "cron_id": job["id"],
                            }
                        )
                        print(
                            f"  \u23f0 Cron job '{job['id']}' is due"
                            " \u2014 queued for batch",
                            flush=True,
                        )

                # --- dispatch ---
                if pending_messages:
                    capture(
                        "ninja batch processing started",
                        {"message_count": len(pending_messages)},
                    )
                    print(
                        f"\n\U0001f4cb Processing {len(pending_messages)}"
                        " pending message(s)...",
                        flush=True,
                    )
                    blocked_msg = check_cost_limit()
                    if blocked_msg:
                        iface.say(blocked_msg)
                        print(
                            "\U0001f6ab Cost limit exceeded" " \u2014 dispatch blocked",
                            flush=True,
                        )
                    else:
                        run_batched_response(agent, pending_messages, iface.say)
                else:
                    blocked_msg = check_cost_limit()

                if not blocked_msg:
                    maybe_launch_orchestrator()

                save_seen_messages(seen_messages)
                save_agent_messages(agent_data)

                jitter = random.uniform(0, POLL_JITTER)
                sleep_time = args.interval + jitter
                if rate_limiter.consecutive_rate_limits > 0:
                    sleep_time += BACKOFF_INITIAL / 2
                    print(
                        f"\U0001f4a4 Extended sleep due to recent rate limits:"
                        f" {sleep_time:.0f}s",
                        flush=True,
                    )
                time.sleep(sleep_time)

        except KeyboardInterrupt:
            print("\n\n\U0001f44b Monitor stopped")
            save_seen_messages(seen_messages)
            save_agent_messages(agent_data)

        return 0
