#!/usr/bin/env python3
"""PostToolUse hook: warn monitor session once when turn budget is exceeded."""

import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path

from clients.posthog_client import is_feature_enabled
from constants import (
    HANDOFF_CONTEXT,
    MONITOR_LENGTH_HOOK_FEATURE_FLAG,
    MONITOR_TURNS_THRESHOLD,
)
from core.config import is_orchestrator_enabled
from utils.agent_files_logs import (
    count_turns_since_last_user_message,
    get_session_name_from_transcript,
)

LOG_FILE = Path("/workspace/logs/monitor_length_hook.log")


def _log(msg: str) -> None:
    try:
        with open(LOG_FILE, "a") as handle:
            handle.write(f"{datetime.now().isoformat(timespec='seconds')} {msg}\n")
    except OSError:
        pass


def _sentinel_path(session_key: str) -> Path:
    return Path(f"/tmp/monitor_length_warned_{session_key}.flag")


def _session_key(hook_input: dict, *, transcript_path: str) -> str:
    return (
        hook_input.get("session_id")
        or hashlib.sha256(transcript_path.encode()).hexdigest()[:16]
    )


def build_hook_output(*, hook_input: dict) -> dict:
    transcript_path = hook_input.get("transcript_path")
    if not transcript_path:
        return {}

    if not is_feature_enabled(MONITOR_LENGTH_HOOK_FEATURE_FLAG, default=False):
        return {}

    if not is_orchestrator_enabled():
        return {}

    path = Path(transcript_path)
    if get_session_name_from_transcript(path) != "monitor":
        return {}

    session_key = _session_key(hook_input, transcript_path=transcript_path)
    sentinel = _sentinel_path(session_key)
    turns = count_turns_since_last_user_message(path)

    if turns < MONITOR_TURNS_THRESHOLD:
        if sentinel.exists():
            sentinel.unlink(missing_ok=True)
            _log(
                f"clear session={session_key} turns={turns} "
                f"threshold={MONITOR_TURNS_THRESHOLD}"
            )
        return {}

    if sentinel.exists():
        _log(f"skip session={session_key} turns={turns} (already warned)")
        return {}

    sentinel.touch()
    _log(
        f"warn session={session_key} turns={turns} "
        f"threshold={MONITOR_TURNS_THRESHOLD}"
    )
    return {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": HANDOFF_CONTEXT,
        }
    }


def main() -> None:
    try:
        hook_input = json.load(sys.stdin)
    except ValueError:
        hook_input = {}

    try:
        output = build_hook_output(hook_input=hook_input)
    except Exception:  # noqa: BLE001 — fail open
        _log("build_hook_output failed — fail open")
        output = {}

    if output:
        print(json.dumps(output))
    sys.exit(0)


if __name__ == "__main__":
    main()
