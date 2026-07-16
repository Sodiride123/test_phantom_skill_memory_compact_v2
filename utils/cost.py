"""
Cost calculation for Claude token usage.

Margin is per-model (stored in pricing.py). Anthropic models apply a 2×
markup on top of base price; non-Anthropic models store the customer-facing
price directly with margin 0.0.
"""

import fcntl
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx
from clients.litellm_client import api_url, get_headers
from constants import (
    COST_LIMIT_PATH,
    HEADER_NINJA_CONVERSATION_ID,
    HEADER_NINJA_FEATURE,
    HEADER_NINJA_TASK_ID,
    LABEL_GENERATE_TASK_TITLE,
)
from core.config import load_agent_config
from utils.pricing import get_pricing

TASK_LOG_FILE = Path("/workspace/ninja/.task_log.jsonl")

_TITLE_SYSTEM_PROMPT = "You are a helpful assistant that generates extremely concise titles (2-4 words maximum) for tasks based on the user's message. Respond with only the title, no other text or punctuation."
_TITLE_USER_PROMPT = "Generate an extremely brief title (2-4 words only) for a task that starts with this message:\n{prompt}"


def get_spend_stats() -> dict:
    """Read TASK_LOG_FILE once and return total, monthly, daily spend and task count."""
    now = datetime.now(timezone.utc)
    today = now.date()
    total = monthly = daily = 0.0
    count = 0
    if TASK_LOG_FILE.exists():
        with open(TASK_LOG_FILE, encoding="utf-8", errors="ignore") as f:
            for line in f:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                cost = entry.get("cost", 0.0)
                total += cost
                count += 1
                created_at = entry.get("created_at")
                if not created_at:
                    continue
                try:
                    dt = datetime.fromisoformat(created_at).astimezone(timezone.utc)
                except ValueError:
                    continue
                if dt.year == now.year and dt.month == now.month:
                    monthly += cost
                if dt.date() == today:
                    daily += cost
    return {
        "total_spend": total,
        "monthly_spend": monthly,
        "daily_spend": daily,
        "task_count": count,
    }


def get_last_task_cost(jsonl_path: Path) -> tuple[str | None, str, float]:
    """Extract tokens from the last task in a JSONL file and return (prompt_uuid, model, cost)."""
    (
        prompt_uuid,
        model,
        input_tokens,
        output_tokens,
        cache_write_tokens,
        cache_read_tokens,
    ) = extract_last_task_tokens(jsonl_path)
    cost = compute_cost(
        model, input_tokens, output_tokens, cache_write_tokens, 0, cache_read_tokens
    )
    return prompt_uuid, model, cost


def extract_last_task_tokens(
    jsonl_path: Path,
) -> tuple[str | None, str, int, int, int, int]:
    """Parse a Claude JSONL session file and return the last task's prompt UUID and token counts.

    Returns (prompt_uuid, model, input_tokens, output_tokens, cache_write_tokens, cache_read_tokens).
    Resets counters on each real user message so only the final task's data is returned.
    """

    prompt_uuid: str | None = None
    model = ""
    input_tokens = output_tokens = cache_write_tokens = cache_read_tokens = 0
    seen_message_ids: set[str] = set()

    with open(jsonl_path, encoding="utf-8", errors="ignore") as f:
        for line in f:
            try:
                entry = json.loads(line)
                if entry.get("type") == "user" and isinstance(
                    entry.get("message", {}).get("content"), str
                ):
                    prompt_uuid = entry.get("uuid")
                    model = ""
                    input_tokens = output_tokens = cache_write_tokens = (
                        cache_read_tokens
                    ) = 0
                    seen_message_ids = set()
                elif entry.get("type") == "assistant":
                    msg = entry.get("message", {})
                    msg_id = msg.get("id")
                    if msg_id and msg_id in seen_message_ids:
                        continue
                    if msg_id:
                        seen_message_ids.add(msg_id)
                    if msg.get("model"):
                        model = msg["model"]
                    usage = msg.get("usage", {})
                    input_tokens += usage.get("input_tokens", 0)
                    output_tokens += usage.get("output_tokens", 0)
                    cache_write_tokens += usage.get("cache_creation_input_tokens", 0)
                    cache_read_tokens += usage.get("cache_read_input_tokens", 0)
            except json.JSONDecodeError:
                pass

    return (
        prompt_uuid,
        model,
        input_tokens,
        output_tokens,
        cache_write_tokens,
        cache_read_tokens,
    )


def compute_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_write_5m_tokens: int,
    cache_write_1h_tokens: int,
    cache_read_tokens: int,
) -> float:
    """Return the customer-facing total cost in USD after applying the gateway margin."""
    return sum(
        compute_cost_breakdown(
            model,
            input_tokens,
            output_tokens,
            cache_write_5m_tokens,
            cache_write_1h_tokens,
            cache_read_tokens,
        ).values()
    )


def compute_cost_breakdown(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_write_5m_tokens: int,
    cache_write_1h_tokens: int,
    cache_read_tokens: int,
) -> dict:
    """Return per-category customer-facing costs in USD after applying the gateway margin."""
    pricing = get_pricing(model)
    m = 1 + pricing.get("margin", 1.0)
    return {
        "input": (input_tokens / 1_000_000) * pricing["input"] * m,
        "output": (output_tokens / 1_000_000) * pricing["output"] * m,
        "cache_write_5m": (cache_write_5m_tokens / 1_000_000)
        * pricing["cache_write_5m"]
        * m,
        "cache_write_1h": (cache_write_1h_tokens / 1_000_000)
        * pricing["cache_write_1h"]
        * m,
        "cache_read": (cache_read_tokens / 1_000_000) * pricing["cache_read"] * m,
    }


def _write_task_log(
    prompt_uuid: str,
    cost: float,
    texts: list[str],
    title: str,
    model: str = "",
    task_id: str | None = None,
    conversation_id: str | None = None,
) -> None:
    """Write a task log entry to TASK_LOG_FILE."""
    try:
        entry: dict = {
            "id": prompt_uuid,
            "texts": texts,
            "cost": cost,
            "title": title,
            "model": model,
            "ninja_task_id": task_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        with open(TASK_LOG_FILE, "a", encoding="utf-8") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            f.write(json.dumps(entry) + "\n")
            fcntl.flock(f, fcntl.LOCK_UN)
        print(
            f"📝 Task log written: uuid={prompt_uuid} title={title!r} cost=${cost:.6f}",
            flush=True,
        )
    except Exception as e:
        print(f"⚠️ Could not write task log: {e}", file=sys.stderr)


def record_task_cost(
    texts: list[str],
    started_at: float,
    title: str,
    task_id: str | None = None,
    conversation_id: str | None = None,
) -> None:
    """Compute cost from token usage and write task log."""
    try:
        claude_projects = Path.home() / ".claude" / "projects"
        newest_file, newest_mtime = None, 0.0
        for f in claude_projects.rglob("*.jsonl"):
            mtime = f.stat().st_mtime
            if mtime >= started_at and mtime > newest_mtime:
                newest_file, newest_mtime = f, mtime

        if not newest_file:
            return

        prompt_uuid, model, cost = get_last_task_cost(newest_file)
        if prompt_uuid:
            _write_task_log(
                prompt_uuid,
                cost,
                texts,
                title,
                model=model,
                task_id=task_id,
                conversation_id=conversation_id,
            )
    except Exception as e:
        print(f"⚠️ Could not compute cost: {e}", file=sys.stderr)


def build_custom_headers(
    task_id: str, title: str, conversation_id: str | None = None
) -> str:
    """
    Build custom headers to track costs by thread_id and task_id
    """
    channel = load_agent_config().get("default_channel", "")
    feature = f"{channel} - {title}" if channel else title
    feature = feature.encode("ascii", errors="ignore").decode("ascii")
    headers = f"{HEADER_NINJA_TASK_ID}: {task_id}\n{HEADER_NINJA_FEATURE}: {feature}"
    if conversation_id:
        headers += f"\n{HEADER_NINJA_CONVERSATION_ID}: {conversation_id}"
    return headers


def generate_task_title(
    prompt: str,
    task_id: str | None = None,
    conversation_id: str | None = None,
) -> str | None:
    """
    Generate a concise task title (2-4 words) to show in the SuperNinja usage dashboard.
    """
    try:
        extra_headers = {}
        channel = load_agent_config().get("default_channel", "")
        if task_id:
            extra_headers[HEADER_NINJA_TASK_ID] = task_id
        if conversation_id:
            extra_headers[HEADER_NINJA_CONVERSATION_ID] = conversation_id
        feature = (
            f"{channel} - {LABEL_GENERATE_TASK_TITLE}"
            if channel
            else LABEL_GENERATE_TASK_TITLE
        )
        extra_headers[HEADER_NINJA_FEATURE] = feature.encode(
            "ascii", errors="ignore"
        ).decode("ascii")

        resp = httpx.post(
            api_url("/v1/chat/completions"),
            headers=get_headers(extra_headers),
            json={
                "model": "claude-haiku-4-5-20251001",
                "messages": [
                    {"role": "system", "content": _TITLE_SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": _TITLE_USER_PROMPT.format(prompt=prompt),
                    },
                ],
                "max_tokens": 20,
                "temperature": 0.7,
            },
            timeout=10.0,
        )
        title = (
            resp.json()["choices"][0]["message"]["content"].strip().strip("'\" \n\t")
        )
        return title or None
    except Exception as e:
        print(f"⚠️ Could not generate task title: {e}", file=sys.stderr)
        return None


def check_cost_limit() -> str | None:
    """Return a block message if any cost limit from config is exceeded, otherwise None."""
    try:
        limits = (
            json.loads(COST_LIMIT_PATH.read_text(encoding="utf-8"))
            if COST_LIMIT_PATH.exists()
            else {}
        )
    except Exception:
        limits = {}
    if not limits:
        return None

    stats = get_spend_stats()

    if "total" in limits:
        limit = float(limits["total"])
        spent = stats["total_spend"]
        if spent >= limit:
            return (
                f"\U0001f6ab **Total cost limit reached**\n"
                f"You've spent ${spent:.2f} of your ${limit:.2f} total limit.\n"
                "Your task has been paused. You can increase your limit to resume."
            )

    if "monthly" in limits:
        monthly_limit = float(limits["monthly"])
        monthly_spent = stats["monthly_spend"]
        if monthly_spent >= monthly_limit:
            return (
                f"\U0001f6ab **Monthly cost limit reached**\n"
                f"You've spent ${monthly_spent:.2f} of your ${monthly_limit:.2f} monthly limit.\n"
                "Your task has been paused. The limit resets next month."
            )

    if "daily" in limits:
        daily_limit = float(limits["daily"])
        daily_spent = stats["daily_spend"]
        if daily_spent >= daily_limit:
            return (
                f"\U0001f6ab **Daily cost limit reached**\n"
                f"You've spent ${daily_spent:.2f} of your ${daily_limit:.2f} daily limit.\n"
                "Your task has been paused. The limit resets tomorrow."
            )

    return None
