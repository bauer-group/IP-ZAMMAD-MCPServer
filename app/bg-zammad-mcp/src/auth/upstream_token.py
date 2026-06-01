"""
Upstream Zammad token retrieval.

In AUTH_MODE=zammad, the user authenticates against Zammad via the OAuth2
proxy. FastMCP issues its own JWT to the MCP client; the Zammad-issued
access token lives in `client_storage` keyed by the FastMCP JWT's JTI/sub.

This module is the single point where tools resolve "who am I talking
to Zammad as on behalf of this MCP call?". It returns the upstream Zammad
bearer token for the currently-authenticated MCP request.

Lookup strategy
---------------
FastMCP's OAuthProxy persists the upstream token under a well-known key
shape. We probe a small set of conventional keys (the surface narrowed
slightly between 2.x and 3.x) and return the first hit. If FastMCP wires
the upstream token directly into the AccessToken claims on the bound
context (`upstream_token` / `upstream_access_token`), that wins because
it's the cheapest path and skips the storage round-trip.

Fallback (AUTH_MODE=oidc / none)
--------------------------------
In modes where there is no per-user Zammad token, `get_zammad_user_token`
raises `MissingUpstreamToken` and callers fall back to the client-level
static API token (built into the ZammadClient at construction time).
"""

from __future__ import annotations

from typing import Any

import structlog

logger = structlog.stdlib.get_logger("bg-zammad-mcp.auth.upstream")


class MissingUpstreamToken(RuntimeError):
    """Raised when no upstream Zammad token can be located for this request."""


# Storage-key prefixes that FastMCP 3.x uses for the upstream-token persistence
# layer. We probe each in order; the first hit wins. Empty-string prefix means
# "the JTI itself" (some store wrappers don't add a namespace).
_UPSTREAM_KEY_CANDIDATES: tuple[str, ...] = (
    "upstream_tokens/",
    "oauth_proxy/upstream_tokens/",
    "tokens/upstream/",
    "",
)


async def get_zammad_user_token(*, client_storage: Any | None = None) -> str:
    """Retrieve the Zammad access token tied to the current MCP request.

    Lookup order
    ------------
    1. Token embedded in the FastMCP AccessToken claims (synchronous, no I/O).
    2. Token stored in `client_storage` keyed by the JWT JTI.
    3. Token stored in `client_storage` keyed by the JWT subject.

    Raises
    ------
    MissingUpstreamToken
        When no token can be resolved - the caller should either reject the
        request (when AUTH_MODE=zammad is intended) or fall back to the
        configured static API token.
    """
    # Import here to keep the auth submodule import-graph free of fastmcp at
    # tooling-time (lets unit tests stub the dependency in conftest).
    from fastmcp.server.dependencies import get_access_token

    access_token = get_access_token()
    if access_token is None:
        raise MissingUpstreamToken("No authenticated MCP request in context")

    claims = getattr(access_token, "claims", {}) or {}

    # Path 1: token in claims. Some OAuthProxy configurations attach the
    # upstream access_token directly to the JWT (signed locally - never
    # leaves this server). Cheapest path; no storage I/O.
    embedded = (
        claims.get("upstream_access_token")
        or claims.get("upstream_token")
        or claims.get("zammad_access_token")
    )
    if isinstance(embedded, str) and embedded:
        return embedded

    # Path 2/3: storage round-trip. We accept either a client_storage that
    # was injected (production: the same one used by the OAuthProxy) or fall
    # back to extracting it from FastMCP's bound server context.
    if client_storage is None:
        client_storage = _resolve_client_storage_from_context()

    if client_storage is None:
        raise MissingUpstreamToken(
            "No upstream-token store is bound to the current request context"
        )

    jti = claims.get("jti") or ""
    sub = claims.get("sub") or ""

    for identifier in (jti, sub):
        if not identifier:
            continue
        for prefix in _UPSTREAM_KEY_CANDIDATES:
            value = await _safe_get(client_storage, f"{prefix}{identifier}")
            extracted = _extract_access_token(value)
            if extracted:
                return extracted

    raise MissingUpstreamToken(
        f"No upstream Zammad token stored for jti={jti!r} sub={sub!r}"
    )


# ── Helpers ─────────────────────────────────────────────────────────────────


def _resolve_client_storage_from_context() -> Any | None:
    """Best-effort: pull the storage instance from FastMCP's bound context.

    FastMCP exposes the active server / app via `get_context()` in 3.x.
    Different builds put the storage in different places (auth provider's
    `client_storage`, the app's `lifespan_state`, etc.). We probe a couple
    of conventional locations and return the first non-None match - or None
    if we can't find one (tests, mocks, ...).
    """
    try:
        from fastmcp.server.dependencies import get_context

        context = get_context()
    except (ImportError, RuntimeError):
        return None
    if context is None:
        return None

    # Common shapes across FastMCP versions.
    for attr_path in (
        ("request_context", "lifespan_context", "client_storage"),
        ("lifespan_context", "client_storage"),
        ("fastmcp", "auth", "client_storage"),
        ("auth", "client_storage"),
    ):
        node: Any = context
        ok = True
        for part in attr_path:
            node = _safe_attr(node, part)
            if node is None:
                ok = False
                break
        if ok and node is not None:
            return node
    return None


def _safe_attr(node: Any, name: str) -> Any | None:
    """Tolerant attr/dict access - returns None if the lookup misfires."""
    if node is None:
        return None
    if isinstance(node, dict):
        return node.get(name)
    return getattr(node, name, None)


async def _safe_get(storage: Any, key: str) -> Any | None:
    """Wrapper around AsyncKeyValue.get that swallows lookup failures.

    AsyncKeyValue raises on backend errors; for an upstream-token probe we
    treat any failure as "not found here, try the next probe".
    """
    try:
        return await storage.get(key)
    except Exception as exc:
        logger.debug("upstream_token.storage_lookup_failed", key=key, error=str(exc))
        return None


def _extract_access_token(value: Any) -> str | None:
    """Pull `access_token` out of a stored upstream-token record.

    Stored values are typically dicts with `access_token`, `refresh_token`,
    `expires_at`, etc. Some adapters wrap the payload in a wrapper object;
    we accept the most common shapes.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value or None
    if isinstance(value, dict):
        for key in ("access_token", "upstream_access_token", "token", "value"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate:
                return candidate
    return None


__all__ = ["MissingUpstreamToken", "get_zammad_user_token"]
