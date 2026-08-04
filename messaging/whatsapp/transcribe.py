#!/usr/bin/env python3
"""Transcribe a WhatsApp audio/voice message to text.

Status: stub — implement when WhatsApp Business API credentials are available.

Usage:
    python messaging/whatsapp/transcribe.py <download_url>
"""

import sys


def transcribe(download_url: str) -> str:
    """Download ``download_url`` with WhatsApp auth and return the transcript.

    Raises:
        NotImplementedError: WhatsApp adapter is not yet implemented.
    """
    raise NotImplementedError(
        "WhatsApp audio transcription is not yet implemented. "
        "Implement auth token retrieval and download logic here, "
        "then POST to api_url('/v1/audio/transcriptions') as in "
        "messaging/slack/transcribe.py."
    )


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: transcribe.py <download_url>", file=sys.stderr)
        sys.exit(1)

    try:
        print(transcribe(sys.argv[1]))
    except (NotImplementedError, RuntimeError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
