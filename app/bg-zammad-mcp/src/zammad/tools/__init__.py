"""
MCP tool registration.

Hand-curated tool surface for the Zammad REST API. Each submodule registers
a logically-coherent group of tools against a passed-in FastMCP instance,
sharing one `ToolContext` that knows how to (a) acquire the right Zammad
bearer token for the current MCP request and (b) call the upstream API.

Why hand-written instead of OpenAPI-generated?
  Zammad doesn't ship a maintained machine-readable OpenAPI spec (the
  third-party "zammad-oas" projects lag the live API by several minor
  releases). Hand curation also lets us:
    * Annotate destructive tools as such (MCP `destructiveHint`)
    * Default `expand=true` so the LLM gets human-friendly role / state
      / organization NAMES instead of opaque IDs
    * Phrase descriptions in the second person, optimised for LLM intent
      mapping (vs auto-generated `getUserById`-style stubs)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import structlog

from config import AuthMode, Settings

from ..client import ZammadClient

logger = structlog.stdlib.get_logger("bg-zammad-mcp.tools")


@dataclass
class ToolContext:
    """Shared state passed to each tool registration submodule.

    Holds the lazily-built ZammadClient and a settings reference so each
    tool can decide whether to forward the user's upstream token or fall
    back to the configured static API token.
    """

    client: ZammadClient
    settings: Settings

    async def request(
        self,
        method: str,
        path: str,
        **kwargs: Any,
    ) -> Any:
        """Make an authenticated Zammad call with the right token source.

        Token-source policy
        -------------------
        * AUTH_MODE=zammad -> resolve the upstream user bearer token from
          the FastMCP AccessToken claims / client_storage and forward it.
          If lookup fails, the call falls back to the static API token
          (if configured), otherwise it raises.
        * AUTH_MODE=oidc / none -> use the static API token directly.
        """
        bearer_token = await self._resolve_bearer_token()
        response = await self.client.request(
            method, path, bearer_token=bearer_token, **kwargs
        )
        if response.headers.get("content-type", "").startswith("application/json"):
            try:
                return response.json()
            except ValueError:
                return response.text
        return response.text

    async def _resolve_bearer_token(self) -> str | None:
        """Pick the Zammad token to attach to an outbound call.

        Returns None when we want the ZammadClient to use its configured
        static API token. Returns a string when we want to forward the
        user's upstream token instead.
        """
        if self.settings.auth_mode is not AuthMode.ZAMMAD:
            return None

        from auth.upstream_token import MissingUpstreamToken, get_zammad_user_token

        try:
            return await get_zammad_user_token()
        except MissingUpstreamToken as exc:
            # In ZAMMAD mode we expect every authenticated request to carry
            # an upstream token. Missing one is a programming / configuration
            # error - log loudly and fall back to the static token if any.
            if self.settings.zammad_api_token is not None:
                logger.warning(
                    "tools.upstream_token_missing_falling_back_to_api_token",
                    error=str(exc),
                )
                return None
            raise


def register_all_tools(
    mcp: Any,
    *,
    client: ZammadClient,
    settings: Settings,
) -> int:
    """Register every Zammad tool with the given FastMCP instance.

    Returns the number of tools registered (used for the startup log line).
    Each submodule reports its own count; we sum them here.
    """
    ctx = ToolContext(client=client, settings=settings)

    from . import (
        articles,
        groups,
        notifications,
        organizations,
        reference,
        tags,
        tickets,
        users,
    )

    count = 0
    for module in (
        tickets,
        articles,
        users,
        organizations,
        groups,
        tags,
        reference,
        notifications,
    ):
        registered = module.register(mcp, ctx)
        count += registered
        logger.debug("tools.module_registered", module=module.__name__, count=registered)

    logger.info("tools.registered", count=count)
    return count


__all__ = ["ToolContext", "register_all_tools"]
