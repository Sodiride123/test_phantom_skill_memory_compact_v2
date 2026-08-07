"""Slack-specific token resolution.

Provides ``resolve_slack_token()`` — the Slack leg of the 3-tier resolver.
Lives inside ``messaging/slack/`` so it is pruned by ``channel_builder.py``
for non-Slack builds (WhatsApp, Teams, etc.).
"""

from __future__ import annotations

import json

import requests
from clients.token_resolver import (
    TokenProvider,
    fetch_from_token_proxy,
    mcp_to_resolved,
    write_mcp_token,
)
from constants import AGENT_SETTINGS_PATH
from core.config import load_agent_config, refresh_config
from core.logging import get_logger

logger = get_logger("slack.token")


def _validate_slack_token(bot_token: str) -> bool:
    """Validate a Slack bot token via auth.test."""
    try:
        resp = requests.post(
            "https://slack.com/api/auth.test",
            headers={
                "Authorization": f"Bearer {bot_token}",
                "Content-Type": "application/json",
            },
            timeout=10,
        )
        return resp.json().get("ok", False) is True
    except Exception:
        return False


def _read_cached_slack_token() -> str | None:
    """Read bot_token from cached agent config."""
    return load_agent_config().get("bot_token") or None


def _cache_slack_token(bot_token: str, expired: str | None = None) -> None:
    """Write bot_token to ~/.agent_settings.json (merge, don't overwrite)."""
    try:
        data = dict(load_agent_config())
        data["bot_token"] = bot_token
        if expired:
            data["bot_token_expired"] = expired
        AGENT_SETTINGS_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
        refresh_config("agent_config")
    except Exception as e:
        logger.warning(f"Could not cache Slack token: {e}")


def resolve_slack_token() -> str | None:
    """3-tier resolution for Slack bot_token."""
    # TIER 1: cached
    cached = _read_cached_slack_token()
    if cached:
        logger.info("[Slack] tier-1 (cache): found cached bot_token")
        return cached

    logger.info("[Slack] tier-1 (cache): no cached token")

    # TIER 2: /dev/shm/mcp-token
    mcp_token = mcp_to_resolved(TokenProvider.SLACK)
    mcp_bot = mcp_token.bot_token if mcp_token else None
    if mcp_bot and mcp_bot != cached:
        if mcp_token.is_expired:
            logger.info("[Slack] tier-2 (mcp-token): token expired, skipping")
        elif _validate_slack_token(mcp_bot):
            logger.info("[Slack] tier-2 (mcp-token): valid, propagating to tier-1")
            _cache_slack_token(
                mcp_bot, str(mcp_token.expires_at) if mcp_token.expires_at else None
            )
            return mcp_bot
        else:
            logger.info("[Slack] tier-2 (mcp-token): auth.test failed")
    elif mcp_bot == cached:
        logger.info("[Slack] tier-2 (mcp-token): same token as tier-1, skipping")
    else:
        logger.info("[Slack] tier-2 (mcp-token): no Slack entry")

    # TIER 3: token proxy — last resort, use whatever it returns
    proxy_token = fetch_from_token_proxy(TokenProvider.SLACK)
    if not proxy_token or not proxy_token.bot_token:
        logger.info("[Slack] tier-3 (token-proxy): unavailable or no bot_token")
        return None

    logger.info(
        "[Slack] tier-3 (token-proxy): got token, propagating to tier-2 + tier-1"
    )
    write_mcp_token(TokenProvider.SLACK, proxy_token)
    _cache_slack_token(
        proxy_token.bot_token,
        str(proxy_token.expires_at) if proxy_token.expires_at else None,
    )
    return proxy_token.bot_token
