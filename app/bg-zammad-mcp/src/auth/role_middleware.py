"""
Role-based MCP access middleware.

In AUTH_MODE=zammad, the verifier already attaches the user's role set to
the AccessToken claims (`roles` and `role_ids`). This middleware compares
those roles against `MCP_ALLOWED_ROLES` and rejects (or, in audit-only
mode, logs and passes) requests from users whose roles aren't on the list.

This is a coarse gate. Zammad's own permission system still enforces
fine-grained access on every API call - the role allowlist exists for
deployments that want to expose the MCP only to specific role tiers
(e.g. "Agents only - never expose to Customers").

Trust model
-----------
* The role values come from the token-verification step (Zammad's
  /api/v1/users/me). They are NOT taken from claims the MCP client
  supplies - the client cannot self-elevate.
* In AUTH_MODE=oidc / none, this middleware is a no-op because the
  external IdP / static API token doesn't carry Zammad role info.
  Role enforcement in those modes happens entirely on the Zammad side.

Wiring lives in `server.py:build_app` - the middleware is registered only
when `AUTH_MODE=zammad` AND the allowlist is non-empty.
"""

from __future__ import annotations

from typing import Any

import structlog
from fastmcp.server.dependencies import get_access_token
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext

logger = structlog.stdlib.get_logger("bg-zammad-mcp.auth.role")


class RoleNotAllowedError(PermissionError):
    """Raised when a request's user role isn't on the allowlist.

    PermissionError because we want MCP clients to see this as an authz
    failure (not transport or input-validation). FastMCP serializes raised
    exceptions in middleware as JSON-RPC error responses with the
    exception class name in the error payload.
    """


class RoleAllowlistMiddleware(Middleware):
    """Enforce MCP_ALLOWED_ROLES on every authenticated request.

    The check runs in `on_request` so it fires for every JSON-RPC method
    (tools/call, resources/read, prompts/get, tools/list, ...) instead of
    needing per-hook duplication.
    """

    def __init__(
        self,
        *,
        allowed_roles: set[str],
        audit_only: bool = False,
    ) -> None:
        # Canonicalise to lowercase once at construction so the per-request
        # comparison is a single set membership check.
        self._allowed = {r.strip().lower() for r in allowed_roles if r.strip()}
        self._audit_only = audit_only

    async def on_request(
        self,
        context: MiddlewareContext[Any],
        call_next: CallNext[Any, Any],
    ) -> Any:
        token = get_access_token()
        # No token = unauthenticated request. The provider should have
        # rejected it before middleware runs; nothing to gate here.
        if token is None:
            return await call_next(context)

        # Empty allowlist = "any authenticated user is fine". The operator
        # is intentionally not narrowing access; warn at boot but accept here.
        if not self._allowed:
            return await call_next(context)

        user_roles = _extract_role_names(token.claims)
        if _intersects(user_roles, self._allowed):
            return await call_next(context)

        return await _handle_disallowed(
            user_roles=user_roles,
            allowed=self._allowed,
            audit_only=self._audit_only,
            context=context,
            call_next=call_next,
            token_claims=token.claims,
        )


async def _handle_disallowed(
    *,
    user_roles: set[str],
    allowed: set[str],
    audit_only: bool,
    context: MiddlewareContext[Any],
    call_next: CallNext[Any, Any],
    token_claims: dict[str, Any],
) -> Any:
    """Reject (default) or log-and-pass (audit-only) on allowlist miss.

    The log payload contains the user's sub and login but NOT the full
    claims (no PII bleed). That's enough for a security operator to
    correlate without storing identifying data.
    """
    log_kwargs = {
        "sub": token_claims.get("sub"),
        "login": token_claims.get("preferred_username"),
        "user_roles": sorted(user_roles),
        "allowed_roles": sorted(allowed),
        "method": context.method,
    }
    if audit_only:
        logger.warning("auth.role_denied_audit_only_passing_through", **log_kwargs)
        return await call_next(context)
    logger.warning("auth.role_denied", **log_kwargs)
    raise RoleNotAllowedError(
        f"User roles {sorted(user_roles)!r} are not on the allowlist "
        f"{sorted(allowed)!r} for this MCP server"
    )


# ── Helpers ─────────────────────────────────────────────────────────────────


def _extract_role_names(claims: dict[str, Any]) -> set[str]:
    """Pull role names out of the AccessToken claims attached by the verifier.

    Accepts either:
      - claims["roles"] = ["Admin", "Agent"]                (verifier-friendly)
      - claims["zammad_user"]["roles"] = [...]              (legacy shape)
      - claims["roles"] = [{"name": "Admin"}, {"name": "Agent"}]

    All comparisons are case-insensitive (Zammad uses "Admin"; allowlists
    in env files often use "admin").
    """
    raw_roles: Any = claims.get("roles")
    if not raw_roles:
        zammad_user = claims.get("zammad_user")
        if isinstance(zammad_user, dict):
            raw_roles = zammad_user.get("roles") or []

    out: set[str] = set()
    if isinstance(raw_roles, list):
        for item in raw_roles:
            if isinstance(item, str) and item.strip():
                out.add(item.strip().lower())
            elif isinstance(item, dict):
                name = item.get("name")
                if isinstance(name, str) and name.strip():
                    out.add(name.strip().lower())
    return out


def _intersects(user_roles: set[str], allowed: set[str]) -> bool:
    """True when the user has at least one of the allowed roles."""
    return bool(user_roles & allowed)


__all__ = ["RoleAllowlistMiddleware", "RoleNotAllowedError"]
