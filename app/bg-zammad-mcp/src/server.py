"""Zammad server wiring on top of the shared bg-mcpcore framework.

bg-mcpcore (a GitHub-pinned dependency) provides the cross-cutting machinery:
settings base, inbound auth, encrypted OAuth-state storage, structured logging,
rate limiting, the operational routes, the outbound HTTP client, the **per-user
on-behalf-of outbound resolver** (profile ``auth.outbound.type: per_user_token``)
and the **role/claim access gate** (profile ``access_control``). THIS module now
holds a single Zammad-specific seam: registering the eight hand-written tool
modules behind a thin decode shim.

The tools were written against a context whose ``request`` returns the decoded
JSON body on success and raises a typed ``ZammadError`` on failure. bg-mcpcore's
``ctx.request_json(error_factory=...)`` provides exactly that contract, so the
shim is now just a binding of Zammad's ``from_status`` error factory — the decode
logic lives in core.

The custom Zammad OAuth2 inbound provider (``auth.zammad_oauth``) is wired
config-driven via the ``bg_mcpcore.auth_providers`` entry point in pyproject.toml.
The per-user Bearer (zammad mode) and the static ``Token token=<api_token>``
fallback (oidc / none modes) are now expressed declaratively in the profile.
"""

from __future__ import annotations

from typing import Any

import structlog

logger = structlog.stdlib.get_logger("bg-zammad-mcp.server")


class _DecodingCtx:
    """Adapt bg-mcpcore's ``ToolContext`` to the Zammad tools' decode-or-raise I/O.

    Delegates to ``ctx.request_json``, binding Zammad's typed-error factory so a
    non-2xx response raises the same ``ZammadError`` subclass the eight tool
    modules already expect — they need no changes.
    """

    def __init__(self, ctx: Any) -> None:
        self._ctx = ctx
        self.settings = ctx.settings

    async def request(self, method: str, path: str, **kwargs: Any) -> Any:
        from zammad.errors import from_status

        return await self._ctx.request_json(
            method,
            path,
            error_factory=lambda status, body: from_status(status, body=body),
            **kwargs,
        )


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


__all__ = ["register"]
