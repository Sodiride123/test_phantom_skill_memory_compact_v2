#!/usr/bin/env python3
"""Keeps the orchestrator's Claude working until the issue queue is empty.

Claude Code runs this script every time the agent tries to stop. We check
GitHub and answer one of two ways: print {"decision": "block", "reason":
"<next instruction>"} and Claude keeps going in the SAME process (no relaunch,
no prompt-cache re-write) — or print nothing, which lets it stop.

Only launches armed with NINJA_CYCLE_RUN=1 are affected (see run_agent());
monitor batches and --task runs are left alone.
"""

import json
import os
import sys
from datetime import datetime
from pathlib import Path

# Reset by run_agent() before each cycle launch.
STATE_FILE = Path("/tmp/ninja_cycle_state.json")
LOG_FILE = Path("/workspace/logs/stop_hook.log")
# Max forced continuations per run (~2 per issue). Unlimited for now —
# lower it if runs ever need a hard cap.
MAX_BLOCKS_PER_RUN = float("inf")


def _log(msg: str) -> None:
    try:
        with open(LOG_FILE, "a") as f:
            f.write(f"{datetime.now().isoformat(timespec='seconds')} {msg}\n")
    except OSError:
        pass  # no log dir outside the sandbox


def _allow(msg: str) -> None:
    _log(f"allow: {msg}")
    sys.exit(0)


def _block(reason: str, state: dict) -> None:
    try:
        STATE_FILE.write_text(json.dumps(state))
    except OSError as e:
        # Can't remember the phase -> we'd repeat this instruction forever.
        # End the run instead; the monitor recovers.
        _allow(f"state write failed ({e}) — fail open")
    _log(f"block #{state['blocks']} next-phase={state['phase']}: {reason[:100]}")
    print(json.dumps({"decision": "block", "reason": reason}))
    sys.exit(0)


def main() -> None:
    if os.environ.get("NINJA_CYCLE_RUN") != "1":
        sys.exit(0)  # not a cycle run — never interfere

    try:
        hook_input = json.load(sys.stdin)
    except ValueError:
        hook_input = {}
    _log(f"stop: last_msg={str(hook_input.get('last_assistant_message', ''))[:80]!r}")

    try:
        state = json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        state = {"phase": "work", "blocks": 0}

    if state.get("blocks", 0) >= MAX_BLOCKS_PER_RUN:
        _allow(f"block budget ({MAX_BLOCKS_PER_RUN}) exhausted")

    # The open-issue count decides whether to chain another cycle or stop.
    # If we can't read it (gh down, auth expired), end the run — same as the
    # pre-hook behavior.
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from tools import issues

        open_count = issues.count_actionable()
    except Exception as e:  # noqa: BLE001
        _allow(f"issue count failed ({e!r}) — fail open")

    state["blocks"] = state.get("blocks", 0) + 1

    if state.get("phase") == "work":
        # Even with an empty queue, reflect still runs — it may file the
        # issues that keep the loop going.
        state["phase"] = "reflect"
        _block(
            "Phase 1 checkpoint. If the issue you picked is not yet closed or "
            "blocked, finish that first. Then do Loop Phase 2 — REFLECT, PLAN "
            "& LEARN — exactly as described in your instructions, and stop.",
            state,
        )

    # phase == "reflect": start the next cycle, or end the run.
    if open_count > 0:
        state["phase"] = "work"
        _block(
            f"Reflect checkpoint passed. {open_count} actionable issue(s) in "
            "the queue — start the next cycle now: Loop Phase 1 (work the "
            "single highest-priority open issue to completion), then stop.",
            state,
        )
    _allow("queue empty after reflect — run complete")


if __name__ == "__main__":
    main()
