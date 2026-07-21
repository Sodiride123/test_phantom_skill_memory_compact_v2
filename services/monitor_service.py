"""
services.monitor_service — business logic for the monitor process.

This module contains everything the monitor *does* (prompt building,
Claude dispatch, credit-error classification, welcome copy) separated
from everything it *is* (poll loop, rate-limit backoff, SIGHUP wiring).

``processes/monitor.py`` imports from here; nothing else should need to.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from clients.posthog_client import capture, is_feature_enabled
from clients.super_ninja_client import get_super_ninja_url, get_thread_id
from constants import (
    DEFAULT_TASK_TITLE,
    SYSTEM_PROMPT_FEATURE_FLAG,
    SYSTEM_PROMPT_PATH,
    SYSTEM_PROMPT_PATH_SINGLE,
)
from core.config import is_orchestrator_enabled
from messaging.message_utils import FORCE_THREAD_ENV, forced_thread_for_batch
from utils.agent_files_logs import if_session_exists_by_name
from utils.cost import build_custom_headers, generate_task_title, record_task_cost
from utils.system_notification import get_disk_warning

# REPO_ROOT is the src/ninja/ package root — used to locate claude-wrapper.sh
_REPO_ROOT = Path(__file__).parent.parent

# ---------------------------------------------------------------------------
# Credit / subprocess error classification
# ---------------------------------------------------------------------------


class RunOutOfCreditsException(Exception):
    """Raised when Claude's output signals the account has no remaining credits."""


def is_customer_key_error(output: str) -> bool:
    """Return True if Claude's subprocess output indicates out-of-credits."""
    lower = output.lower()
    return ("400" in lower and "budget has been exceeded" in lower) or (
        "402" in lower and "customer keys are not active" in lower
    )


# ---------------------------------------------------------------------------
# Welcome message
# ---------------------------------------------------------------------------


# Distinctive opening phrase used as an idempotency anchor by the adapter's
# post_welcome_if_needed() implementation. Must match the first user-visible
# sentence of build_welcome_message(). Do not change without updating the
# adapter and the test suite.
def build_welcome_signature(agent) -> str:
    name = agent.get("name", "Ninja")
    role = agent.get("role", "Browser Automation Agent")
    return f"Hi, I'm {name} \u2014 your {role}."


def build_welcome_message(agent: dict) -> str:
    """Build the first-run welcome announcement text (channel-agnostic Markdown).

    The first user-visible sentence MUST contain ``WELCOME_SIGNATURE`` — it
    doubles as an invisible idempotency anchor when reading back channel history.
    """
    welcome_signature = build_welcome_signature(agent)
    emoji = agent.get("emoji", "\U0001f47b")
    return (
        f"{emoji} **{welcome_signature}**\n"
        "Think of me as a virtual employee on your team. Brief me in "
        "any language \u2014 by message, voice note, or file \u2014 and I'll "
        "get the work done. No clicking, no copy-pasting, no API keys "
        "for you to manage.\n"
        "\n"
        "**\U0001f4bc What you can ask me to do**\n"
        '- **Research & reports** \u2014 "Pull the top 10 competitors for '
        'X, summarise their pricing, give me a one-pager."\n'
        '- **Lead gen & outreach** \u2014 "Find 50 founders of seed-stage '
        'fintechs in NYC and draft a personalised intro email to each."\n'
        '- **Recruiting** \u2014 "Source 20 senior backend engineers from '
        'LinkedIn matching this JD and message them on my behalf."\n'
        '- **Operations & data entry** \u2014 "Update these 30 Salesforce '
        'records from this spreadsheet," or "file my expense reports '
        'from these receipts."\n'
        '- **Travel & bookings** \u2014 "Book the cheapest direct flight '
        'from SFO to JFK next Friday and add it to my calendar."\n'
        '- **Creative work** \u2014 "Generate a flat-style logo for a '
        'coffee app called Brewly," or edit a photo, design a banner, '
        "or mock up a landing page hero image.\n"
        "- **Reports posted right here** \u2014 I deliver results back to "
        "this channel as a message, file, image, or thread you can act on.\n"
        "\n"
        "**\U0001f9f0 How I get things done \u2014 two complementary tools**\n"
        "- \U0001f30d **Browser** \u2014 I drive a real Chromium browser the "
        "way a human would: navigate any website, fill forms, click "
        "through flows, scrape data, log in to dashboards, download "
        "files. *Best for:* anything without an API \u2014 internal admin "
        "tools, niche SaaS, web search, news, social profiles, "
        "research across many sites.\n"
        "- \U0001f50c **Integrations (3,000+ apps)** \u2014 direct, "
        "authenticated access to Slack, Gmail, Google Calendar, "
        "GitHub, Jira, Linear, Notion, Salesforce, HubSpot, Stripe, "
        "Airtable, Asana, LinkedIn, X/Twitter, AWS, and ~3,000 more. "
        "*Best for:* anything with a stable API \u2014 fast, reliable, "
        "rate-limit-friendly, and works in the background even while "
        "you're offline.\n"
        "I pick the right tool for each step automatically; you "
        "don't have to choose.\n"
        "\n"
        "**\U0001f4ac How to brief me**\n"
        "- Just type a message in this channel \u2014 I reply to every "
        "human message.\n"
        "- Include the word `ninja` anywhere in your message to be "
        "explicit, or to ping me from a thread.\n"
        "- Send a *voice note* in any language and I'll transcribe and "
        "act on it.\n"
        "- Drop a *screenshot, PDF, spreadsheet, or any file* with "
        "your request and I'll use it as context.\n"
        "\n"
        "**\U0001f441 Watch me work**\n"
        "- [**Live Browser**](0.0.0.0:6080/vnc.html?autoconnect=true) "
        "\u2014 watch my Chromium session in real time. Take over with "
        "mouse/keyboard if I get stuck.\n"
        "- [**Activity Dashboard**](0.0.0.0:9000) \u2014 live identity, "
        "logs, reasoning trace, and per-task cost.\n"
        "- [**Connect Apps**](0.0.0.0:9020) \u2014 connect new apps "
        "(one-click OAuth) so I can use them. Anything you connect "
        "here becomes a tool I can call."
    )


# ---------------------------------------------------------------------------
# Prompt / CLI command helpers
# ---------------------------------------------------------------------------


def say_cmd(thread_ts: Optional[str] = None) -> str:
    """Return the channel-appropriate CLI say command for Claude's prompt.

    Reads ``MESSAGING_CHANNEL`` env-var (default: ``slack``) so the command
    embedded in Claude's prompt always matches the active adapter.
    """
    channel = os.environ.get("MESSAGING_CHANNEL", "slack")
    base = f"python messaging/{channel}/interface.py say"
    if thread_ts:
        return f'{base} "message" -t {thread_ts}'
    return f'{base} "message"'


# ---------------------------------------------------------------------------
# Claude batch dispatch
# ---------------------------------------------------------------------------


def run_batched_response(
    agent: dict,
    pending_messages: list,
    messaging_say_fn,
) -> bool:
    """Send all pending messages to Claude in a single prompt.

    Args:
        agent:            Agent configuration dict.
        pending_messages: List of message dicts (user, text, timestamp,
                          thread_ts, type, audio_files).
        messaging_say_fn: Callable used to post the out-of-credits notification
                          (typically ``_get_messaging().say``). Passed in to
                          avoid a circular import with monitor.py.

    Returns:
        True if Claude successfully processed the messages.
    """
    if not pending_messages:
        return True

    agent_name = agent["name"]
    agent_role = agent["role"]
    agent_emoji = agent["emoji"]

    system_prompt_enabled = is_feature_enabled(
        SYSTEM_PROMPT_FEATURE_FLAG, default=False
    )
    print(f"System prompt feature enabled: {system_prompt_enabled}")

    messages_text = ""
    for i, msg in enumerate(pending_messages, 1):
        msg_type = msg.get("type", "mention")

        if msg_type == "cron":
            reply_hint = say_cmd(msg.get("thread_ts"))
            messages_text += (
                f"\n--- Message {i} (cron — scheduled job) ---\n"
                f"Cron ID: {msg.get('cron_id', 'unknown')}\n"
                f"Prompt: {msg.get('text', '')}\n"
            )
            if not system_prompt_enabled:
                messages_text += (
                    f"Post the result with: {reply_hint}\n"
                    "(See agent-docs/CRON.md. Execute the prompt; do not ask for confirmation.)\n"
                )
            continue

        if system_prompt_enabled:
            thread_info = (
                f'\n   Thread: {msg["thread_ts"]}'
                if msg.get("thread_ts")
                else f"\n   Channel: main"
            )
        else:
            thread_info = (
                f'\n   Thread: {msg["thread_ts"]} (reply with: {say_cmd(msg["thread_ts"])})'
                if msg.get("thread_ts")
                else f"\n   Channel: main (reply with: {say_cmd()})"
            )

        attachment_info = ""
        if msg.get("audio_files"):
            attachment_info += (
                "\n   🎤 AUDIO/VOICE MESSAGE — Must transcribe before responding!"
            )
            for af in msg["audio_files"]:
                attachment_info += (
                    f"\n   Audio file: {af.get('name', 'audio')} "
                    f"({af.get('mimetype', 'audio/*')})"
                    f"\n   Download URL: {af.get('url', 'N/A')}"
                )
        if msg.get("image_files"):
            attachment_info += "\n   🖼️  IMAGE ATTACHMENT(S):"
            for img in msg["image_files"]:
                size_kb = img.get("size", 0) // 1024
                attachment_info += (
                    f"\n   Image: {img.get('name', 'image')} "
                    f"({img.get('mimetype', 'image/*')}, {size_kb} KB)"
                    f"\n   Download URL: {img.get('url', 'N/A')}"
                )
        if msg.get("pdf_files"):
            attachment_info += "\n   📄 PDF ATTACHMENT(S):"
            for pdf in msg["pdf_files"]:
                size_kb = pdf.get("size", 0) // 1024
                attachment_info += (
                    f"\n   PDF: {pdf.get('name', 'file.pdf')} ({size_kb} KB)"
                    f"\n   Download URL: {pdf.get('url', 'N/A')}"
                )
        if msg.get("other_files"):
            attachment_info += "\n   📎 OTHER FILE ATTACHMENT(S):"
            for of in msg["other_files"]:
                size_kb = of.get("size", 0) // 1024
                attachment_info += (
                    f"\n   File: {of.get('name', 'file')} "
                    f"({of.get('mimetype', 'application/octet-stream')}, {size_kb} KB)"
                    f"\n   Download URL: {of.get('url', 'N/A')}"
                )

        messages_text += (
            f"\n--- Message {i} ({msg_type}) ---\n"
            f"From: {msg.get('user', 'Unknown')}\n"
            f"Time: {msg.get('timestamp', 'Unknown')}\n"
            f"Text: {msg.get('text', '')}{attachment_info}{thread_info}\n"
        )

    if system_prompt_enabled:
        prompt = messages_text
    else:
        prompt = (
            f"You are {agent_name} {agent_emoji}, the {agent_role}.\n\n"
            "We are running you as a monitor agent, your specification is in "
            "agent-docs/MONITOR.md. For scheduled cron items see agent-docs/CRON.md.\n\n"
            f"The current time is {time.strftime('%Y-%m-%d %H:%M:%S')}. "
            f"You have {len(pending_messages)} message(s) that need your response. "
            f"Read ALL of them and respond to EACH ONE.\n\n"
            f"{messages_text}"
        )

    disk_warning = get_disk_warning()
    if disk_warning:
        prompt += f"\n\n{disk_warning}"

    print(
        f"\n{agent_emoji} Sending {len(pending_messages)} message(s) to Claude...",
        flush=True,
    )

    texts = [msg.get("text", "") for msg in pending_messages if msg.get("text")]
    started_at = datetime.now(timezone.utc).timestamp()
    task_id = str(uuid.uuid4())
    conversation_id = get_thread_id()

    combined = " | ".join(texts) if texts else ""
    title = (
        generate_task_title(combined, task_id=task_id, conversation_id=conversation_id)
        if combined
        else None
    )
    if not title:
        title = (
            (combined[:50] + "…")
            if len(combined) > 50
            else combined or DEFAULT_TASK_TITLE
        )

    custom_headers = build_custom_headers(task_id, title, conversation_id)

    # Single-message batches get deterministic reply routing: export the
    # thread_ts so the say/upload CLI enforces it if the model drops -t.
    subprocess_env = {**os.environ, "ANTHROPIC_CUSTOM_HEADERS": custom_headers}
    forced_thread = forced_thread_for_batch(pending_messages)
    if forced_thread:
        subprocess_env[FORCE_THREAD_ENV] = forced_thread
    else:
        subprocess_env.pop(FORCE_THREAD_ENV, None)

    prompt_file = None
    try:
        if not if_session_exists_by_name("monitor"):
            session_args = ["-n", "monitor"]
        else:
            session_args = ["-r", "monitor"]
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False, encoding="utf-8"
        ) as f:
            f.write(prompt)
            prompt_file = f.name
            subprocess_env["CLAUDE_PROMPT_FILE"] = prompt_file
        if system_prompt_enabled:
            system_prompt_path = (
                SYSTEM_PROMPT_PATH
                if is_orchestrator_enabled()
                else SYSTEM_PROMPT_PATH_SINGLE
            )
            result = subprocess.run(
                [
                    str(_REPO_ROOT / "claude-wrapper.sh"),
                    *session_args,
                    "--system-prompt-file",
                    f"{system_prompt_path}",
                    "--tools",
                    "Bash,Edit,Read,Skill,Write",
                    "-p",
                ],
                cwd=str(_REPO_ROOT),
                capture_output=True,
                text=True,
                timeout=180,
                env=subprocess_env,
            )
        else:
            result = subprocess.run(
                [str(_REPO_ROOT / "claude-wrapper.sh"), *session_args, "-p"],
                cwd=str(_REPO_ROOT),
                capture_output=True,
                text=True,
                timeout=180,
                env=subprocess_env,
            )
        output = result.stdout + result.stderr
        success_count = (
            output.count("Message sent")
            + output.count("✅")
            + output.count("Timestamp:")
        )

        threading.Thread(
            target=record_task_cost,
            args=(texts, started_at, title),
            kwargs={"task_id": task_id, "conversation_id": conversation_id},
            daemon=True,
        ).start()

        if is_customer_key_error(output):
            raise RunOutOfCreditsException

        capture(
            "ninja batch response completed",
            {"success": True, "response_count": success_count},
        )
        if success_count > 0:
            print(
                f"✅ Claude processed batch — {success_count} response indicator(s)",
                flush=True,
            )
        else:
            print(
                f"⚠️ Claude batch response (may have posted): {output[:300]}...",
                flush=True,
            )
        return True

    except RunOutOfCreditsException:
        try:
            top_up_url = f"{get_super_ninja_url()}/dashboard#buy-credits"
            messaging_say_fn(
                f"\U0001f4b3 **You're out of credits**\n"
                f"Your current task has been paused.\n"
                f"Add credits to pick up where you left off — your work is saved.\n\n"
                f"**[Add credits \u2192]({top_up_url})**"
            )
            print("\U0001f4b3 Posted insufficient credit notification", flush=True)
        except Exception as e:
            print(f"\u26a0\ufe0f Credit notification skipped: {e}", file=sys.stderr)
        capture(
            "ninja batch response failed because of the insufficient credits",
            {"success": False, "error": "insufficient credit"},
        )
        return False
    except subprocess.TimeoutExpired:
        capture(
            "ninja batch response completed", {"success": False, "error": "timeout"}
        )
        print("⚠️ Claude batch response timed out", flush=True)
        return False
    except OSError as e:
        print(f"⚠️ OS error running Claude: {e}", file=sys.stderr)
        return False
    except Exception as e:
        capture("ninja batch response completed", {"success": False, "error": str(e)})
        print(f"⚠️ Error: {e}", flush=True)
        return False
    finally:
        if prompt_file:
            os.unlink(prompt_file)
