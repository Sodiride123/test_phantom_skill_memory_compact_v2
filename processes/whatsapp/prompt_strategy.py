"""WhatsApp prompt strategy — provides WhatsApp-specific prompt variables."""

from __future__ import annotations

from pathlib import Path

from processes.base import PromptStrategy, PromptVars

# Present while a turn runs.  services.whatsapp.queued_ack watches this and
# auto-replies "message queued" to messages that arrive mid-turn.  Path MUST
# match services/whatsapp/queued_ack.py:BUSY_MARKER.
BUSY_MARKER = Path("/workspace/.ninja-turn-busy")


class WhatsAppPromptStrategy(PromptStrategy):
    """WhatsApp channel prompt variable provider."""

    def get_prompt_vars(self, config: dict) -> PromptVars:
        wa = config.get("whatsapp") if isinstance(config.get("whatsapp"), dict) else {}
        channel = wa.get("channel_label") or wa.get("bound_chat_jid") or "(unbound)"
        return PromptVars(
            channel=channel,
            default_task=(
                f"Check WhatsApp ({channel}) for new requests via "
                "`cd /workspace && python -m ninja.whatsapp_interface read --json`, "
                "do your work, reply with "
                '`cd /workspace && python -m ninja.whatsapp_interface say "<text>" '
                "--group-jid <jid>` (or `--to <jid>` for DMs), then reflect per "
                "agent-docs/ORCHESTRATOR.md."
            ),
            interface_doc=(
                "3. **WhatsApp Interface Docs:** "
                "`cat agent-docs/WHATSAPP_INTERFACE.md`"
            ),
        )
