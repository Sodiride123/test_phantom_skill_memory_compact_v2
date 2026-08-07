"""
3-tier token resolution for Slack and GitHub.

Resolution order:
  1. PRIMARY   — cached token (fast, no I/O beyond a file read)
  2. SECONDARY — /dev/shm/mcp-token file
  3. TERTIARY  — agent-event-cache /tokens?provider_id=<Provider>

On success at tier N, the token is propagated UP to all higher tiers
so subsequent calls hit the cache.

If a tier returns the same token string as the tier above it (which
already failed), validation is skipped and resolution falls through
to the next tier.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from enum import StrEnum
from typing import Optional

from clients.agent_event_cache_client import AgentEventCacheClient
from constants import MCP_TOKEN_PATH
from core.logging import get_logger
from pydantic import BaseModel

logger = get_logger("token_resolver")


# ---------------------------------------------------------------------------
# Resolved token model
# ---------------------------------------------------------------------------


class TokenProvider(StrEnum):
    SLACK = "Slack"
    GITHUB = "Github"


class ResolvedToken(BaseModel):
    """Normalized token from any tier."""

    access_token: str = ""
    bot_token: str = ""
    expires_at: Optional[datetime] = None

    @property
    def is_expired(self) -> bool:
        if not self.expires_at:
            return False
        now = datetime.now(timezone.utc)
        exp = (
            self.expires_at
            if self.expires_at.tzinfo
            else self.expires_at.replace(tzinfo=timezone.utc)
        )
        return now >= exp


# ---------------------------------------------------------------------------
# /dev/shm/mcp-token read/write
# ---------------------------------------------------------------------------


def read_mcp_token(provider_id: str) -> dict | None:
    """Read a provider's JSON entry from /dev/shm/mcp-token."""
    if not MCP_TOKEN_PATH.is_file():
        return None
    try:
        content = MCP_TOKEN_PATH.read_text(encoding="utf-8")
        for line in content.strip().splitlines():
            if line.startswith(f"{provider_id}="):
                return json.loads(line[len(provider_id) + 1 :])
    except (json.JSONDecodeError, OSError):
        pass
    return None


def mcp_to_resolved(provider_id: str) -> ResolvedToken | None:
    """Read a provider's entry from /dev/shm/mcp-token as a ResolvedToken."""
    raw = read_mcp_token(provider_id)
    if not raw:
        return None
    return ResolvedToken(
        access_token=raw.get("access_token", ""),
        bot_token=raw.get("bot_token", ""),
        expires_at=raw.get("expired"),
    )


def write_mcp_token(provider_id: str, token: ResolvedToken) -> None:
    """Merge a provider's entry into /dev/shm/mcp-token, preserving others."""
    raw = token.model_dump()
    if raw.get("expires_at"):
        raw["expired"] = raw.pop("expires_at").isoformat()
    else:
        raw.pop("expires_at", None)
    data = {k: v for k, v in raw.items() if v}
    new_line = f"{provider_id}={json.dumps(data)}"
    prefix = f"{provider_id}="

    try:
        existing = (
            MCP_TOKEN_PATH.read_text(encoding="utf-8").strip().splitlines()
            if MCP_TOKEN_PATH.is_file()
            else []
        )
        lines = [line for line in existing if not line.startswith(prefix)]
        lines.append(new_line)
        MCP_TOKEN_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError as e:
        logger.error(f"Could not write to {MCP_TOKEN_PATH}: {e}")


# ---------------------------------------------------------------------------
# Token proxy (tertiary)
# ---------------------------------------------------------------------------


def fetch_from_token_proxy(provider_id: str) -> ResolvedToken | None:
    """Call agent-event-cache /tokens?provider_id=X. Returns ResolvedToken or None."""
    try:
        client = AgentEventCacheClient()
        resp = client.get_token(provider_id)
        return ResolvedToken(
            access_token=resp.access_token or "",
            bot_token=(resp.extra_field or {}).get("bot_token", ""),
            expires_at=resp.expired or "",
        )
    except Exception as e:
        logger.error(f"Token proxy for {provider_id} failed: {e}")
        return None


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def gh_auth_valid() -> bool:
    """Return True if the gh CLI session is valid."""
    try:
        r = subprocess.run(
            ["gh", "auth", "status"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return r.returncode == 0
    except (subprocess.SubprocessError, OSError):
        return False


def _login_gh(token: str) -> bool:
    """Pipe a token to gh auth login --with-token. Returns True on success."""
    try:
        r = subprocess.run(
            ["gh", "auth", "login", "--with-token"],
            input=token,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return r.returncode == 0
    except (subprocess.SubprocessError, OSError) as e:
        logger.error(f"gh auth login failed: {e}")
        return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def resolve_github_token() -> dict:
    """3-tier resolution for GitHub.

    Returns a dict with at least ``status`` ("ok", "missing", or "invalid")
    and optionally ``message``.  Truthy when ``status == "ok"``.
    """
    # TIER 1: cached gh session
    if gh_auth_valid():
        logger.info("[GitHub] tier-1 (gh session): valid")
        return {"status": "ok"}

    logger.info("[GitHub] tier-1 (gh session): invalid or missing")
    token = None

    # TIER 2: /dev/shm/mcp-token
    mcp_resolved = mcp_to_resolved(TokenProvider.GITHUB)
    mcp_token = mcp_resolved.access_token if mcp_resolved else None
    if mcp_token:
        if mcp_resolved.is_expired:
            logger.info("[GitHub] tier-2 (mcp-token): token expired, skipping")
        else:
            token = mcp_token
            logger.info("[GitHub] tier-2 (mcp-token): found token")
    else:
        logger.info("[GitHub] tier-2 (mcp-token): no Github entry")

    # TIER 3: token proxy (only if tier 2 failed)
    if not token:
        proxy_token = fetch_from_token_proxy(TokenProvider.GITHUB)
        if (
            proxy_token
            and proxy_token.access_token
            and proxy_token.access_token != mcp_token
        ):
            if not proxy_token.is_expired:
                token = proxy_token.access_token
                logger.info(
                    "[GitHub] tier-3 (token-proxy): found token, propagating to tier-2"
                )
                write_mcp_token(TokenProvider.GITHUB, proxy_token)
            else:
                logger.info("[GitHub] tier-3 (token-proxy): token expired")
        elif not proxy_token:
            logger.info("[GitHub] tier-3 (token-proxy): unavailable")
        else:
            logger.info("[GitHub] tier-3 (token-proxy): same token or empty")

    # Propagate to tier 1: gh auth login
    if token and _login_gh(token):
        logger.info("[GitHub] gh auth login succeeded")
        return {"status": "ok"}

    if not token:
        logger.error("[GitHub] no valid token found across all tiers")
        return {
            "status": "missing",
            "message": "No GitHub token in mcp-token or token proxy",
        }

    logger.error("[GitHub] gh auth login failed across all tiers")
    return {
        "status": "invalid",
        "message": "gh auth login failed across all tiers",
    }
