"""Shared message processing utilities across all messaging adapters."""

import os
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlparse

# Set by the monitor when the pending batch has exactly one message, so the
# say/upload CLI can enforce its thread when the model drops the -t flag.
FORCE_THREAD_ENV = "NINJA_FORCE_THREAD_TS"
_NINJA_EMOJI = "\U0001f977"  # 🥷
_NINJA_SHORTCODE = ":ninja:"

# Whisper-compatible audio MIME types by extension. Voice notes are usually .m4a
# (mobile) or .webm (desktop). The transcription endpoint rejects a file whose
# declared name/type don't match its bytes ("corrupted or unsupported"), so the
# format must be detected — never hardcoded.
_AUDIO_MIME = {
    ".m4a": "audio/mp4",
    ".mp4": "audio/mp4",
    ".mp3": "audio/mpeg",
    ".mpga": "audio/mpeg",
    ".mpeg": "audio/mpeg",
    ".webm": "audio/webm",
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".wav": "audio/wav",
    ".flac": "audio/flac",
}


def audio_upload_part(url: str, resp: Any) -> tuple:
    """Build the multipart ``file`` tuple for a transcription upload.

    Detects the audio format so a voice note is never mislabeled (e.g. an m4a
    sent as webm → rejected). Priority: the URL's file extension (channels like
    Slack encode it in the download URL), then the download's ``Content-Type``,
    defaulting to m4a — the common voice-note format. Always returns a
    whisper-accepted ``audio/*`` type. ``resp`` is a requests-style response
    (``.headers``/``.content``). Returns ``(filename, bytes, mime)``.
    """
    ext = Path(urlparse(url or "").path).suffix.lower()
    if ext not in _AUDIO_MIME:
        # No usable extension on the URL — map the download's declared audio type
        # back to a known extension (reverse lookup, not mimetypes.guess_extension
        # which doesn't know audio/webm etc.). Unknown/generic types default to
        # m4a, the common voice-note format.
        ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        ext = next((e for e, m in _AUDIO_MIME.items() if m == ctype), ".m4a")
    # Extension is now always a known audio format → MIME is always audio/*.
    return (f"audio{ext}", resp.content, _AUDIO_MIME[ext])


# Extensions that are unambiguously audio — safe to classify a mislabeled or
# generically-typed attachment as audio by name alone. Container formats that
# are commonly video (.mp4, .mpeg, .webm) are excluded: those are treated as
# audio only when the content-type explicitly says audio/*.
_UNAMBIGUOUS_AUDIO_EXTS = {".m4a", ".mp3", ".mpga", ".wav", ".flac", ".ogg", ".oga"}


def has_audio_ext(name: str, url: str) -> bool:
    """True if a filename or URL path ends in an unambiguous audio extension."""
    return any(
        Path(urlparse(v or "").path).suffix.lower() in _UNAMBIGUOUS_AUDIO_EXTS
        for v in (name, url)
    )


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
    For Teams with delegated tokens, also detects :ninja: emoji signature.
    """
    # Teams check: official app identity
    if message.get("from_application_id"):
        return True
    # Slack check
    if message.get("bot_profile"):
        return True
    # Teams delegated token: check for :ninja: emoji signature (bot self-marks)
    text = message.get("text", "").strip()
    if _NINJA_EMOJI in text or _NINJA_SHORTCODE in text:
        return True
    return False


def extract_file_attachments(message: Dict[str, Any]) -> Dict[str, list]:
    """Categorize a message's attachments by type (audio/image/pdf/other).

    The single classifier for every adapter. Reads the mimetype from
    ``content_type`` (Teams-normalized) or ``mimetype`` (Slack-raw), and the URL
    from ``content_url`` / ``web_url`` / ``url_private_download``. Audio is
    detected by an ``audio/*`` type, Slack's ``voice_message`` subtype, or an
    unambiguous audio extension — so a voice note mislabeled as video/mp4, sent
    as octet-stream, or with no type at all still routes to transcription.

    Returns a dict with keys: audio_files, image_files, pdf_files, other_files.
    Each entry exposes name, mimetype, size, and url.
    """
    audio_files, image_files, pdf_files, other_files = [], [], [], []
    for f in message.get("files") or []:
        content_type = (f.get("content_type") or f.get("mimetype") or "").lower()
        subtype = f.get("subtype") or ""
        entry = {
            "name": f.get("name") or "unknown",
            "mimetype": content_type,
            "size": f.get("size") or 0,
            "url": (
                f.get("content_url")
                or f.get("web_url")
                or f.get("url_private_download")
                or ""
            ),
        }
        if (
            content_type.startswith("audio/")
            or subtype == "voice_message"
            or has_audio_ext(entry["name"], entry["url"])
        ):
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
