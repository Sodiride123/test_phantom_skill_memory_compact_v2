"""WhatsApp monitor strategy — delegates to the WhatsApp monitor service.

The WhatsApp monitor is a completely self-contained process
(``services/whatsapp/monitor_service.py``) with its own polling loop,
message processing, and state management.  This strategy simply wraps it
to satisfy the ``MonitorStrategy`` ABC.
"""

from __future__ import annotations

from processes.base import MonitorStrategy


class WhatsAppMonitorStrategy(MonitorStrategy):
    """WhatsApp channel monitor — delegates to the WhatsApp monitor service."""

    def run(self) -> int:
        from services.whatsapp.monitor_service import main as wa_main

        return wa_main()
