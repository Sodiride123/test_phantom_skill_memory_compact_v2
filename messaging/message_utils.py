"""Shared message processing utilities across all messaging adapters."""

import os
from typing import Any, Dict, Optional

# Set by the monitor when the pending batch has exactly one message, so the
# say/upload CLI can enforce its thread when the model drops the -t flag.
FORCE_THREAD_ENV = "NINJA_FORCE_THREAD_TS"


def resolve_reply_thread(explicit_thread: Optional[str]) -> Optional[str]:
    """Return the reply thread: explicit -t wins, else the enforced env value."""
    if explicit_thread:
        return explicit_thread
    forced = (os.environ.get(FORCE_THREAD_ENV) or "").strip()
    return forced or None


def forced_thread_for_batch(pending_messages: list) -> Optional[str]:
    """Return the thread_ts to enforce when the batch has exactly one message.

    collect_pending points top-level messages at themselves and thread replies
    at their thread root, so one rule covers both. Multi-message batches return
    None: routing stays with the model's per-message -t hints.
    """
    if len(pending_messages) != 1:
        return None
    thread_ts = pending_messages[0].get("thread_ts")
    return str(thread_ts) if thread_ts else None


def _first_present(message: Dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        value = message.get(key)
        if value not in (None, ""):
            return value
    return default


def is_bot_message(message: Dict[str, Any]) -> bool:
    """True if message was posted by an application (bot), not a user.

    Works for both Teams (from_application_id) and Slack (bot_profile).
    """
    # Teams check
    if message.get("from_application_id"):
        return True
    # Slack check
    if message.get("bot_profile"):
        return True
    return False


def extract_file_attachments(message: Dict[str, Any]) -> Dict[str, list]:
    """Categorize a normalized message's attachments by type.

    Returns a dict with keys: audio_files, image_files, pdf_files, other_files.
    Each entry exposes name, mimetype, size, and url.
    """
    audio_files, image_files, pdf_files, other_files = [], [], [], []
    for f in message.get("files") or []:
        content_type = (f.get("content_type") or "").lower()
        entry = {
            "name": f.get("name") or "unknown",
            "mimetype": content_type,
            "size": f.get("size") or 0,
            "url": f.get("content_url") or f.get("web_url") or "",
        }
        if content_type.startswith("audio/"):
            audio_files.append(entry)
        elif content_type.startswith("image/"):
            image_files.append(entry)
        elif content_type == "application/pdf":
            pdf_files.append(entry)
        elif entry["name"] != "unknown" or entry["url"]:
            other_files.append(entry)
    return {
        "audio_files": audio_files,
        "image_files": image_files,
        "pdf_files": pdf_files,
        "other_files": other_files,
    }


def classify_message_type(attachments: Dict[str, list], is_reply: bool) -> str:
    """Derive a message type from attachments + position (attachment wins)."""
    if attachments["audio_files"]:
        return "audio_message"
    if (
        attachments["image_files"]
        or attachments["pdf_files"]
        or attachments["other_files"]
    ):
        return "file_message"
    return "thread_reply" if is_reply else "mention"


def normalize_cached_message(item: Dict[str, Any]) -> Dict[str, Any]:
    """Return the monitor-facing message shape for generic cached rows."""
    if not isinstance(item, dict):
        return {}

    attachments = item.get("attachments")
    if not isinstance(attachments, list):
        attachments = item.get("files") if isinstance(item.get("files"), list) else []

    normalized = dict(item)
    normalized.update(
        {
            "id": str(_first_present(item, "id", "message_id", "ts", default="")),
            "created": _first_present(
                item,
                "created",
                "createdDateTime",
                "timestamp",
                "ts",
                default="",
            ),
            "from": _first_present(
                item,
                "from",
                "user_name",
                "username",
                "user",
                default="Unknown",
            ),
            "from_user_id": _first_present(
                item,
                "from_user_id",
                "user_id",
                "user",
            ),
            "text": _first_present(item, "text", "body_text", default=""),
            "web_url": _first_present(item, "web_url", "webUrl"),
            "attachments": attachments,
            "files": attachments,
        }
    )
    normalized.setdefault("raw", item)
    return normalized
