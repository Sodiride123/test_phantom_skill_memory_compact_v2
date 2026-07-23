"""
PostHog analytics client.

Provides a cached Posthog client instance and a convenience ``capture()``
function configured from ``/dev/shm/ph_metadata.json``.

Usage:
    from clients.posthog_client import capture

    capture(
        event="my_event_name",
        properties={"key": "value"},
    )

The ``distinct_id`` is resolved automatically from
``/dev/shm/sandbox_metadata.json`` (``thread_id`` field).
``sandbox_id`` is resolved from ``/dev/shm/ph_metadata.json``
"""

import logging
import os
from functools import cache
from typing import Any, Dict, Optional, Union

from core.metadata import load_ph_metadata, load_sandbox_metadata
from posthog import Posthog

logger = logging.getLogger(__name__)


def _is_local() -> bool:
    """Return True when running in local / docker-compose dev mode."""
    return os.environ.get("LOCAL_DEVELOPMENT_MODE", "").lower() in (
        "true",
        "1",
        "yes",
    )


@cache
def get_posthog_client() -> Posthog:
    """Return a cached Posthog client.

    Reads POSTHOG_KEY and POSTHOG_HOST from /dev/shm/ph_metadata.json.
    Raises AssertionError if no POSTHOG_KEY is available.
    """
    ph_meta = load_ph_metadata()
    key = ph_meta.get("posthog_key")
    host = ph_meta.get("posthog_host", "https://us.i.posthog.com")
    assert key, "POSTHOG_KEY is not configured in ph_metadata.json"
    return Posthog(project_api_key=key, host=host)


def capture(
    event: str,
    properties: Optional[Dict[str, Any]] = None,
) -> None:
    """Emit a PostHog event identified by the sandbox ``thread_id``.

    Silently no-ops when:
    - ``posthog_key`` is absent or empty in ``ph_metadata.json``
    - Sandbox metadata is unavailable (no ``thread_id``) in non-local mode

    Args:
        event:      Event name (e.g. ``"task_started"``).
        properties: Optional dict of metadata to attach to the event.
    """
    # Require a PostHog key from ph_metadata.json.
    ph_meta = load_ph_metadata()
    key = ph_meta.get("posthog_key")
    if not key:
        return

    props = {**(properties or {})}

    if _is_local():
        props["ninja_sandbox_id"] = "local_dev"
        distinct_id = f"local-{os.environ.get('NINJA_USER_ID', 'unknown')}"
        print(
            f"[posthog] capture(distinct_id={distinct_id!r}, event={event!r}, properties={props})"
        )
        return

    # Resolve the distinct_id from sandbox metadata.
    metadata = load_sandbox_metadata()
    if not metadata.get("thread_id"):
        return

    user_id = metadata.get("user_id", "unknown")
    props["ninja_sandbox_id"] = ph_meta.get("sandbox_id", "")
    props["ninja_sandbox_provider"] = metadata.get("sandbox_provider", "unknown")
    props["ninja_thread_id"] = metadata["thread_id"]
    props["ninja_user_id"] = user_id
    distinct_id = user_id

    get_posthog_client().capture(
        distinct_id=distinct_id,
        event=event,
        properties=props,
    )


def _flag_context() -> Optional[tuple[str, Dict[str, Any]]]:
    """Resolve (distinct_id, person_properties) for flag evaluation."""
    ph_meta = load_ph_metadata()
    if not ph_meta.get("posthog_key"):
        return None

    if _is_local():
        return None

    metadata = load_sandbox_metadata()
    user_id = metadata.get("user_id")
    thread_id = metadata.get("thread_id")
    distinct_id = user_id or thread_id

    if not distinct_id:
        return None

    person_properties = {
        "ninja_sandbox_id": ph_meta.get("sandbox_id", ""),
        "ninja_sandbox_provider": metadata.get("sandbox_provider", "unknown"),
    }

    if user_id:
        person_properties["ninja_user_id"] = user_id
    if thread_id:
        person_properties["ninja_thread_id"] = thread_id

    return distinct_id, person_properties


def is_feature_enabled(flag_key: str, default: bool = False) -> bool:
    """Return whether a boolean PostHog feature flag is enabled.
    Args:
        flag_key: The PostHog feature flag key.
        default:  Value returned when the flag cannot be evaluated.
    """
    ctx = _flag_context()
    if ctx is None:
        return default
    distinct_id, person_properties = ctx
    try:
        result = get_posthog_client().feature_enabled(
            flag_key,
            distinct_id,
            person_properties=person_properties,
        )
    except Exception:
        logger.warning("posthog feature_enabled(%s) failed", flag_key, exc_info=True)
        return default
    return default if result is None else bool(result)


def get_feature_flag(
    flag_key: str,
    default: Optional[Union[bool, str]] = None,
) -> Optional[Union[bool, str]]:
    """Return the value of a (possibly multivariate) PostHog feature flag.

    Args:
        flag_key: The PostHog feature flag key.
        default:  Value returned when the flag cannot be evaluated.
    """
    ctx = _flag_context()
    if ctx is None:
        return default
    distinct_id, person_properties = ctx
    try:
        result = get_posthog_client().get_feature_flag(
            flag_key,
            distinct_id,
            person_properties=person_properties,
        )
    except Exception:
        logger.warning("posthog get_feature_flag(%s) failed", flag_key, exc_info=True)
        return default
    return default if result is None else result
