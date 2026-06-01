"""Zammad server wiring on top of the shared bg-mcpcore framework.

bg-mcpcore (a GitHub-pinned dependency) provides the cross-cutting machinery:
settings base, inbound auth, encrypted OAuth-state storage, structured logging,
rate limiting, the operational routes, and the outbound HTTP client. THIS module
holds the two profile-referenced seams that stay Zammad-specific:

* ``make_obo_resolver`` — the outbound ``AuthHeaderSource`` (profile
  ``auth.outbound.type: python``). Replicates the previous ``ZammadClient``
  auth-header policy EXACTLY:
    - oidc / none modes  -> static ``Authorization: Token token=<api_token>``
    - zammad mode        -> per-user ``Authorization: Bearer <token>``, resolved
      per call and FAIL-CLOSED (raises when no token is available, with a
      static-token fallback only when one is configured).
* ``register`` — the ``tools.source: python`` callable. Wraps bg-mcpcore's
  ``ToolContext`` in a decoding shim so the unchanged Zammad tool modules keep
  receiving decoded JSON bodies on success and typed ``ZammadError`` on failure,
  then registers all eight tool modules.

The custom Zammad OAuth2 inbound provider (``auth.zammad_oauth``) and the
role-allowlist gate (``auth.role_middleware``) are wired config-driven via the
``bg_mcpcore.auth_providers`` / ``bg_mcpcore.auth_middleware`` entry points
declared in pyproject.toml.
"""

from __future__ import annotations

from typing import Any

import structlog

logger = structlog.stdlib.get_logger("bg-zammad-mcp.server")


# ── Outbound auth: per-user on-behalf-of, fail-closed ────────────────────────


class _ZammadOutboundResolver:
    """Outbound ``AuthHeaderSource`` mirroring ``ZammadClient._build_auth_header``."""

    def __init__(self, settings: Any) -> None:
        self._settings = settings

    def _static_token_header(self) -> dict[str, str]:
        token = self._settings.zammad_api_token
        if token and token.get_secret_value():
            # Zammad Personal Access Token format (NOT a Bearer token).
            return {"Authorization": f"Token token={token.get_secret_value()}"}
        return {}

    def default_headers(self) -> dict[str, str]:
        # oidc / none: a static Personal Access Token, baked in at construction.
        if str(self._settings.auth_mode) != "zammad":
            return self._static_token_header()
        # zammad mode: nothing static — the per-user bearer is resolved per call.
        return {}

    async def auth_headers(self, _ctx: Any) -> dict[str, str]:
        if str(self._settings.auth_mode) != "zammad":
            return {}  # the static Token header is supplied by default_headers()
        from auth.upstream_token import MissingUpstreamToken, get_zammad_user_token

        try:
            token = await get_zammad_user_token()
            return {"Authorization": f"Bearer {token}"}
        except MissingUpstreamToken:
            # Mirror the old ToolContext fallback: in zammad mode a missing
            # per-user token falls back to the static API token if configured,
            # otherwise it fails closed (re-raise) — no unauthenticated calls.
            static = self._static_token_header()
            if static:
                logger.warning("server.upstream_token_missing_falling_back_to_api_token")
                return static
            raise


def make_obo_resolver(_cfg: Any) -> _ZammadOutboundResolver:
    """Factory referenced by the profile's ``auth.outbound.resolver``."""
    from bg_mcpcore.settings import get_settings

    from config import Settings

    return _ZammadOutboundResolver(get_settings(Settings))


# ── Tool registration: decoding shim → unchanged tool modules ────────────────


class _DecodingCtx:
    """Wrap bg-mcpcore's ``ToolContext`` to preserve the old tool I/O contract.

    bg-mcpcore's ``ctx.request`` returns an ``httpx.Response``; the Zammad tools
    were written against a context whose ``request`` returned the decoded body
    on success and raised a typed ``ZammadError`` on failure. This shim
    reproduces both, so the eight tool modules need no changes.
    """

    def __init__(self, ctx: Any) -> None:
        self._ctx = ctx
        self.settings = ctx.settings

    async def request(self, method: str, path: str, **kwargs: Any) -> Any:
        response = await self._ctx.request(method, path, **kwargs)
        if 200 <= response.status_code < 300:
            if response.headers.get("content-type", "").startswith("application/json"):
                try:
                    return response.json()
                except ValueError:
                    return response.text
            return response.text
        # Non-2xx -> the same typed exception the ZammadClient used to raise.
        from zammad.errors import from_status

        body: dict[str, Any] = {}
        if "json" in response.headers.get("content-type", ""):
            try:
                parsed = response.json()
                if isinstance(parsed, dict):
                    body = parsed
            except ValueError:
                pass
        raise from_status(response.status_code, body=body)


def register(mcp: Any, ctx: Any) -> int:
    """``tools.source: python`` entrypoint — register the Zammad tool surface."""
    shim = _DecodingCtx(ctx)

    from zammad.tools import (
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
        registered = module.register(mcp, shim)
        count += registered
        logger.debug("server.module_registered", module=module.__name__, count=registered)

    logger.info("server.tools_registered", count=count)
    return count


__all__ = ["make_obo_resolver", "register"]
