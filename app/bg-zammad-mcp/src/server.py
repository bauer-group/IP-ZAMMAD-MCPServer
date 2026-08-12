"""Zammad server wiring on top of the shared bg-mcpcore framework.

bg-mcpcore (a GitHub-pinned dependency) provides the cross-cutting machinery:
settings base, inbound auth, encrypted OAuth-state storage, structured logging,
rate limiting, the operational routes, the outbound HTTP client, the **per-user
on-behalf-of outbound resolver** (profile ``auth.outbound.type: per_user_token``)
and the **role/claim access gate** (profile ``access_control``). THIS module now
holds a single Zammad-specific seam: registering the hand-written tool modules
behind a thin decode shim.

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

    ``request_raw`` is the byte-preserving counterpart. ``request_json`` decodes
    a 2xx body as JSON or as ``response.text`` — a UTF-8 decode with
    ``errors='replace'`` — which destroys binary content irreversibly. Core's
    own ``ctx.request`` already returns the untouched ``httpx.Response``, so the
    bytes were always reachable; only this shim hid them. Attachments are the
    single caller.
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

    async def request_raw(self, method: str, path: str, **kwargs: Any) -> Any:
        """Upstream call whose body is NOT decoded. Raises the same typed errors."""
        from zammad.errors import from_status

        response = await self._ctx.request(method, path, **kwargs)
        if 200 <= response.status_code < 300:
            return response
        body: dict[str, Any] = {}
        if "json" in response.headers.get("content-type", ""):
            try:
                parsed = response.json()
            except ValueError:
                parsed = None
            if isinstance(parsed, dict):
                body = parsed
        raise from_status(response.status_code, body=body)


# Tags applied to each module's tools after registration. With 75 tools the
# published list is itself a meaningful slice of a client's context window, so
# operators and clients need a way to talk about subsets: "the ticket-handling
# half", "everything that only reads". Tagging centrally rather than in each
# @mcp.tool call keeps the grouping visible in one place and stops it drifting
# module by module.
_MODULE_TAGS: dict[str, set[str]] = {
    "overviews": {"tickets", "worklist"},
    "tickets": {"tickets"},
    "articles": {"tickets", "communication"},
    "macros": {"tickets", "bulk"},
    "bulk": {"tickets", "bulk"},
    "links": {"tickets"},
    "checklists": {"tickets"},
    "time_accounting": {"tickets", "reporting"},
    "attachments": {"tickets", "communication"},
    "history": {"tickets", "audit"},
    "knowledge": {"knowledge"},
    "ai": {"knowledge", "ai"},
    "fields": {"reference"},
    "users": {"people"},
    "organizations": {"people"},
    "groups": {"reference"},
    "tags": {"tickets"},
    "reference": {"reference"},
    "notifications": {"worklist"},
}


class _Tagging:
    """A FastMCP stand-in that adds a module's tags to every tool it registers.

    Injecting FastMCP's own ``tags=`` keyword rather than mutating Tool objects
    after the fact: the decorator is public API, the private registry is not.
    Everything else passes straight through, so a tool module cannot tell the
    difference and needs no knowledge of tagging.
    """

    def __init__(self, mcp: Any, tags: set[str]) -> None:
        self._mcp = mcp
        self._tags = tags

    def __getattr__(self, name: str) -> Any:
        return getattr(self._mcp, name)

    def tool(self, *args: Any, **kwargs: Any) -> Any:
        kwargs["tags"] = set(kwargs.get("tags") or set()) | self._tags
        return self._mcp.tool(*args, **kwargs)


def register(mcp: Any, ctx: Any) -> int:
    """``tools.source: python`` entrypoint — register the Zammad tool surface."""
    shim = _DecodingCtx(ctx)

    from zammad.tools import (
        ai,
        articles,
        attachments,
        bulk,
        checklists,
        fields,
        groups,
        history,
        knowledge,
        links,
        macros,
        notifications,
        organizations,
        overviews,
        reference,
        tags,
        tickets,
        time_accounting,
        users,
    )

    # Ordered by how central each group is to day-to-day ticket work rather than
    # alphabetically: the boot log reads as a description of the surface, and a
    # client that truncates a long tool list keeps the important half.
    count = 0
    for module in (
        overviews,
        tickets,
        articles,
        macros,
        bulk,
        links,
        checklists,
        time_accounting,
        attachments,
        history,
        knowledge,
        ai,
        fields,
        users,
        organizations,
        groups,
        tags,
        reference,
        notifications,
    ):
        name = module.__name__.rsplit(".", 1)[-1]
        target = _Tagging(mcp, _MODULE_TAGS.get(name, set())) if _MODULE_TAGS.get(name) else mcp
        registered = module.register(target, shim)
        count += registered
        logger.debug("server.module_registered", module=module.__name__, count=registered)

    logger.info("server.tools_registered", count=count)
    return count


__all__ = ["register"]
