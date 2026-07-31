"""
core/metadata.py — Cached loaders for runtime metadata files.

Provides a single, canonical source for reading the three metadata files
written by the container entrypoint at startup. All results are cached via
``config_cached`` and can be invalidated with ``refresh_config()``.

Usage::

    from core.metadata import load_sandbox_metadata, load_ph_metadata

    thread_id = load_sandbox_metadata().get("thread_id")
    sandbox_id = load_ph_metadata().get("sandbox_id")
"""

from __future__ import annotations

import json

from constants import DEFAULT_MODEL, PH_METADATA_PATH, SANDBOX_METADATA_PATH
from core.config import config_cached


@config_cached("sandbox_metadata")
def load_sandbox_metadata() -> dict:
    """
    Load and cache ``/dev/shm/sandbox_metadata.json``.

    Returns the full parsed dict from the file, or ``{}`` on any error
    (file absent, unreadable, or malformed JSON).

    Common keys: ``thread_id``, ``environment``, ``use_agent_event_cache``,
    ``litellm_selected_model``.
    """
    try:
        return json.loads(SANDBOX_METADATA_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def get_selected_model() -> str:
    """
    Read litellm_selected_model from /dev/shm/sandbox_metadata.json if present.
    Falls back to DEFAULT_MODEL ('claude-opus-4-8') if the file
    doesn't exist, is unreadable, or doesn't contain litellm_selected_model.

    Returns:
        Model name string
    """
    meta = load_sandbox_metadata()
    if not meta:
        return DEFAULT_MODEL

    model = meta.get("litellm_selected_model", "").strip()
    if model:
        return model
    return DEFAULT_MODEL


@config_cached("ph_metadata")
def load_ph_metadata() -> dict:
    """
    Load and cache ``/dev/shm/ph_metadata.json``.

    Returns the full parsed dict from the file, or ``{}`` on any error
    (file absent, unreadable, or malformed JSON).

    Common keys: ``posthog_host``, ``posthog_key``, ``sandbox_id``.
    """
    try:
        return json.loads(PH_METADATA_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
