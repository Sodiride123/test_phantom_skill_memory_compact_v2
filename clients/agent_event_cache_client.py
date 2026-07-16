"""
Agent Event Cache Client
========================

HTTP client for the agent-event-cache service that provides Slack message
history. Replaces the S3-backed _read_channel_mirror path when enabled.

Base URL:     https://agent-event-cache.public.<environment>.myninja.ai
Auth:         Bearer ANTHROPIC_AUTH_TOKEN (from /root/.claude/settings.json)
Feature flag: "use_agent_event_cache": true in /dev/shm/sandbox_metadata.json

Endpoint:
    GET /db/messages
        ?workspace_id=T123
        &channel_id=C123
        &limit=50
        &start_ts=...        (optional)
        &end_ts=...          (optional)
        &thread_ts=...       (optional)
        &next_token=...      (optional)

Response:
    {
        "workspace_id": str,
        "channel_id": str,
        "messages": [...],
        "total": int,
        "next_token": str | null
    }
"""

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

logger = logging.getLogger(__name__)

import requests
from core.metadata import load_sandbox_metadata
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SETTINGS_PATHS = [
    Path("/root/.claude/settings.json"),
    Path(__file__).resolve().parent.parent / "settings.json",
]
_RETRIABLE_STATUS = {429, 500, 502, 503, 504}


def _is_retriable(exc: BaseException) -> bool:
    # Network-level errors
    if isinstance(
        exc, (requests.exceptions.ConnectionError, requests.exceptions.Timeout)
    ):
        return True
    # Transient HTTP status codes
    if isinstance(exc, requests.exceptions.HTTPError) and exc.response is not None:
        return exc.response.status_code in _RETRIABLE_STATUS
    return False


def is_event_cache_enabled() -> bool:
    """True if sandbox_metadata.json has "use_agent_event_cache": true."""
    return load_sandbox_metadata().get("use_agent_event_cache", False) is True


def _get_base_url() -> str:
    """
    Build service base URL from sandbox_metadata environment field.

    Returns: https://agent-event-cache.public.<env>.myninja.ai
    Raises RuntimeError if environment is unavailable.
    """
    local_mode = os.environ.get("LOCAL_DEVELOPMENT_MODE", "").lower() in (
        "true",
        "1",
        "yes",
    )
    override = os.environ.get("AGENT_EVENT_CACHE_BASE_URL", "").strip()
    if local_mode and override:
        return override

    meta = load_sandbox_metadata()
    environment = meta.get("environment", "").strip()
    if not environment:
        raise RuntimeError(
            "Cannot determine agent-event-cache URL: "
            "'environment' missing from /dev/shm/sandbox_metadata.json"
        )
    return f"https://agent-event-cache.public.{environment}.myninja.ai"


def _get_auth_token() -> str:
    """
    Read ANTHROPIC_AUTH_TOKEN from settings.json.
    Raises RuntimeError if no token found.
    """
    # Read ANTHROPIC_AUTH_TOKEN from settings files
    for path in SETTINGS_PATHS:
        if path.exists():
            try:
                with open(path) as f:
                    data = json.load(f)
                token = data.get("env", {}).get("ANTHROPIC_AUTH_TOKEN", "")
                if token:
                    return token.strip().removeprefix("Bearer ").removeprefix("bearer ")
            except (json.JSONDecodeError, IOError):
                continue

    raise RuntimeError("ANTHROPIC_AUTH_TOKEN not found in settings.json")


# ---------------------------------------------------------------------------
# Pydantic Models
# ---------------------------------------------------------------------------


class GetMessagesRequest(BaseModel):
    """Request parameters for GET /db/messages."""

    workspace_id: str = Field(description="Slack workspace/team ID (e.g. T123)")
    channel_id: str = Field(description="Slack channel or DM ID (e.g. C123)")
    start_ts: Optional[float] = Field(
        default=None, description="Lower-bound Unix epoch timestamp (inclusive)"
    )
    end_ts: Optional[float] = Field(
        default=None, description="Upper-bound Unix epoch timestamp (inclusive)"
    )
    limit: int = Field(
        default=50, ge=1, le=200, description="Max messages per page (1-200)"
    )
    next_token: Optional[str] = Field(
        default=None, description="Pagination cursor from a previous response"
    )
    thread_ts: Optional[str] = Field(
        default=None, description="Return only messages belonging to this thread"
    )

    def to_params(self) -> Dict[str, Any]:
        """Convert to query params dict, excluding None values."""
        return {k: v for k, v in self.model_dump().items() if v is not None}


class GetMessagesResponse(BaseModel):
    """Response schema for GET /db/messages."""

    workspace_id: str
    channel_id: str
    messages: List[Dict[str, Any]]
    total: int
    next_token: Optional[str] = None


# ---------------------------------------------------------------------------
# Channel / Workspace Models
# ---------------------------------------------------------------------------


class ChannelSummary(BaseModel):
    """Summary of a single channel."""

    channel_id: str
    name: Optional[str] = None
    workspace_id: Optional[str] = None
    is_private: bool = False
    is_archived: bool = False
    topic: Optional[str] = None
    purpose: Optional[str] = None


class GetChannelsResponse(BaseModel):
    """Response schema for GET /db/channels."""

    channels: List[ChannelSummary]
    total: int


class GetChannelInfoResponse(BaseModel):
    """Response schema for GET /db/channel-info."""

    channel_id: str
    workspace_id: Optional[str] = None
    name: Optional[str] = None
    is_private: bool = False
    is_archived: bool = False
    topic: Optional[str] = None
    purpose: Optional[str] = None
    synced_at: Optional[str] = None


class MemberInfo(BaseModel):
    """Profile information for a channel member."""

    user_id: str
    user_name: Optional[str] = None
    real_name: Optional[str] = None
    is_bot: Optional[bool] = None
    is_deleted: Optional[bool] = None


class GetChannelMembersResponse(BaseModel):
    """Response schema for GET /db/channel-members."""

    channel_id: str
    members: List[MemberInfo]
    total: int


class GetWorkspaceResponse(BaseModel):
    """Response schema for GET /db/workspace."""

    workspace_id: str
    name: Optional[str] = None
    domain: Optional[str] = None


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class AgentEventCacheClient:
    """
    Client for the agent-event-cache GET /db/messages endpoint.

    Usage:
        from clients.agent_event_cache_client import (
            AgentEventCacheClient, GetMessagesRequest, is_event_cache_enabled
        )

        if is_event_cache_enabled():
            client = AgentEventCacheClient()
            request = GetMessagesRequest(
                workspace_id="T0A9Q27KD1T",
                channel_id="C0AAAAMBR1R",
                limit=50,
            )
            response = client.get_messages(request)
            messages = response.messages
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        auth_token: Optional[str] = None,
        timeout: int = 15,
    ):
        self._base_url = (base_url or _get_base_url()).rstrip("/")
        self._auth_token = auth_token or _get_auth_token()
        self._timeout = timeout

    def _headers(self) -> Dict[str, str]:
        """Return standard request headers."""
        return {
            "Authorization": f"Bearer {self._auth_token}",
            "Content-Type": "application/json",
        }

    @retry(
        stop=stop_after_attempt(3),
        retry=retry_if_exception(_is_retriable),
        wait=wait_exponential(multiplier=1, max=10),
        reraise=True,
    )
    def get_messages(self, request: GetMessagesRequest) -> GetMessagesResponse:
        """
        Fetch messages from the agent-event-cache service.

        Args:
            request: Validated GetMessagesRequest with query parameters

        Returns:
            GetMessagesResponse with messages and pagination info

        Raises:
            requests.HTTPError: On 4xx/5xx responses
            pydantic.ValidationError: If response doesn't match schema
            RuntimeError: On configuration errors
        """
        resp = requests.get(
            f"{self._base_url}/db/messages",
            params=request.to_params(),
            headers=self._headers(),
            timeout=self._timeout,
        )
        resp.raise_for_status()
        result = GetMessagesResponse.model_validate(resp.json())
        logger.info(
            "agent-event-cache get_messages succeeded",
            extra={
                "channel_id": request.channel_id,
                "workspace_id": request.workspace_id,
                "message_count": len(result.messages),
                "total": result.total,
                "has_next_page": result.next_token is not None,
            },
        )
        return result

    def get_channels(self) -> GetChannelsResponse:
        """
        List all channels the authenticated caller is authorized to access.

        Returns:
            GetChannelsResponse with list of channels and total count

        Raises:
            requests.HTTPError: On 4xx/5xx responses
            RuntimeError: On configuration errors
        """
        resp = requests.get(
            f"{self._base_url}/db/channels",
            headers=self._headers(),
            timeout=self._timeout,
        )
        resp.raise_for_status()
        result = GetChannelsResponse.model_validate(resp.json())
        logger.info(
            "agent-event-cache get_channels succeeded",
            extra={"total": result.total},
        )
        return result

    def get_channel_info(self, channel_id: str) -> GetChannelInfoResponse:
        """
        Get metadata for a single channel from the database.

        Args:
            channel_id: Slack channel ID (e.g. "C0AAAAMBR1R")

        Returns:
            GetChannelInfoResponse with channel metadata

        Raises:
            requests.HTTPError: On 4xx/5xx responses
            RuntimeError: On configuration errors
        """
        resp = requests.get(
            f"{self._base_url}/db/channel-info",
            params={"channel_id": channel_id},
            headers=self._headers(),
            timeout=self._timeout,
        )
        resp.raise_for_status()
        result = GetChannelInfoResponse.model_validate(resp.json())
        logger.info(
            "agent-event-cache get_channel_info succeeded",
            extra={"channel_id": channel_id, "name": result.name},
        )
        return result

    def get_channel_members(self, channel_id: str) -> GetChannelMembersResponse:
        """
        List active members of a channel with decrypted user profiles.

        Args:
            channel_id: Slack channel ID (e.g. "C0AAAAMBR1R")

        Returns:
            GetChannelMembersResponse with member list and total count

        Raises:
            requests.HTTPError: On 4xx/5xx responses
            RuntimeError: On configuration errors
        """
        resp = requests.get(
            f"{self._base_url}/db/channel-members",
            params={"channel_id": channel_id},
            headers=self._headers(),
            timeout=self._timeout,
        )
        resp.raise_for_status()
        result = GetChannelMembersResponse.model_validate(resp.json())
        logger.info(
            "agent-event-cache get_channel_members succeeded",
            extra={"channel_id": channel_id, "total": result.total},
        )
        return result

    def get_workspace(self, channel_id: str) -> GetWorkspaceResponse:
        """
        Get workspace name and domain for the workspace that owns a channel.

        Args:
            channel_id: Slack channel ID — workspace_id is derived from
                        the channel_info row server-side.

        Returns:
            GetWorkspaceResponse with workspace_id, name, and domain

        Raises:
            requests.HTTPError: On 4xx/5xx responses
            RuntimeError: On configuration errors
        """
        resp = requests.get(
            f"{self._base_url}/db/workspace",
            params={"channel_id": channel_id},
            headers=self._headers(),
            timeout=self._timeout,
        )
        resp.raise_for_status()
        result = GetWorkspaceResponse.model_validate(resp.json())
        logger.info(
            "agent-event-cache get_workspace succeeded",
            extra={
                "channel_id": channel_id,
                "workspace_id": result.workspace_id,
                "name": result.name,
            },
        )
        return result
