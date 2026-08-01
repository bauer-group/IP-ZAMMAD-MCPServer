"""
Zammad as OAuth2 provider via FastMCP's OAuthProxy.

Operator setup (Zammad-side)
----------------------------
1. Open Zammad as an administrator.
2. Admin -> System -> API  (the ``#system/api`` page, permission ``admin.api``).
3. Under "Applications", click "New Application". The form has exactly two
   fields - there is no scope and no "confidential" toggle:
     * Name:          BAUER GROUP MCP (or your preferred display name)
     * Callback URL:  ${PUBLIC_BASE_URL}/auth/callback
4. Save, then use the row's "View" action to read the client ID and client
   secret into `ZAMMAD_OAUTH_CLIENT_ID` / `ZAMMAD_OAUTH_CLIENT_SECRET`. Both
   stay retrievable later - this is not a one-time reveal.

Scopes
------
Zammad runs plain Doorkeeper with ``default_scopes :full`` and no optional
scopes, and the form above never sets an application scope. Doorkeeper
therefore validates every authorize request against ``server_scopes ==
["full"]``: any other value (``read write``, say) is rejected with
``invalid_scope`` after the user has already logged in. ``full`` is the only
working value; Zammad's own per-request scope check is commented out, so
nothing is lost. ``Settings.validate_provider_auth`` enforces this at boot.

Inbound trust model
-------------------
* OAuthProxy issues its OWN FastMCP JWT to the MCP client (signed with
  AUTH_JWT_SIGNING_KEY). MCP clients see a single, version-stable JWT
  shape regardless of which IdP they came from.
* The Zammad-issued access token is stored encrypted in `client_storage`
  keyed by the FastMCP JWT's JTI - this is what bg-mcpcore's `per_user_token`
  outbound resolver retrieves at tool-call time so the outbound Zammad call
  carries the user's identity end-to-end.
* Tokens are validated at exchange time by hitting Zammad's
  `/api/v1/users/me` endpoint (cheap, returns 401 on invalid token,
  returns the role set the `access_control` gate matches against anyway).
"""

from __future__ import annotations

from typing import Any

import httpx
import structlog

from config import Settings

logger = structlog.stdlib.get_logger("bg-zammad-mcp.auth.zammad")


class _ZammadUserInfoVerifier:
    """Verify a Zammad access token by calling /api/v1/users/me.

    Implements the FastMCP TokenVerifier protocol's `verify(token)` contract:
      - Returns an `AccessToken` (FastMCP type) on success.
      - Returns None / raises on failure - either is treated as "rejected".

    Why not JWTVerifier?
      Zammad's OAuth2 Applications issue opaque (non-JWT) bearer tokens; the
      JWKS-style verification path doesn't apply. A userinfo round-trip is
      the documented validation strategy for this token format.
    """

    def __init__(
        self,
        *,
        userinfo_url: str,
        timeout: float = 10.0,
        verify_tls: bool = True,
        required_scopes: list[str] | None = None,
    ) -> None:
        self._userinfo_url = userinfo_url
        self._timeout = timeout
        self._verify_tls = verify_tls
        self._required_scopes = list(required_scopes or [])

    @property
    def required_scopes(self) -> list[str]:
        # FastMCP reads this attribute when emitting the
        # /.well-known/oauth-protected-resource metadata.
        return list(self._required_scopes)

    async def verify_token(self, token: str) -> Any | None:
        """Return an AccessToken-shaped object or None when the token is invalid."""
        from mcp.server.auth.provider import AccessToken

        async with httpx.AsyncClient(
            timeout=self._timeout,
            verify=self._verify_tls,
            follow_redirects=False,
        ) as client:
            try:
                response = await client.get(
                    self._userinfo_url,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Accept": "application/json",
                    },
                    params={"expand": "true"},
                )
            except httpx.HTTPError as exc:
                logger.warning("zammad.userinfo_unreachable", error=str(exc))
                return None

        if response.status_code == 401:
            return None
        if response.status_code >= 500:
            logger.warning(
                "zammad.userinfo_server_error",
                status=response.status_code,
            )
            return None
        if response.status_code != 200:
            logger.warning(
                "zammad.userinfo_unexpected_status",
                status=response.status_code,
            )
            return None

        try:
            payload: dict[str, Any] = response.json()
        except ValueError:
            return None

        sub = _stringify(payload.get("id"))
        login = payload.get("login") or payload.get("email") or sub or "unknown"
        if sub is None:
            return None

        # We carry the upstream token forward in the AccessToken claims so
        # tools can retrieve it without a storage round-trip. The token
        # itself never appears in FastMCP-issued JWTs sent to clients - it
        # only lives on the server-side AccessToken object during request
        # processing.
        return AccessToken(
            token=token,
            client_id=sub,
            scopes=list(self._required_scopes),
            expires_at=None,
            resource=None,
            claims={
                "sub": sub,
                "preferred_username": login,
                "email": payload.get("email"),
                "name": payload.get("firstname"),
                "role_ids": payload.get("role_ids") or [],
                "roles": payload.get("roles") or [],
                "upstream_access_token": token,
                "zammad_user": {
                    "id": payload.get("id"),
                    "login": login,
                    "email": payload.get("email"),
                    "firstname": payload.get("firstname"),
                    "lastname": payload.get("lastname"),
                    "role_ids": payload.get("role_ids") or [],
                    "roles": payload.get("roles") or [],
                    "active": payload.get("active"),
                    "organization_id": payload.get("organization_id"),
                },
            },
        )


def _stringify(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def build_zammad_oauth_provider(settings: Settings, inbound: Any | None = None) -> Any:
    """Construct the FastMCP OAuthProxy backed by Zammad's OAuth2 Applications.

    Registered as a ``bg_mcpcore.auth_providers`` entry point keyed ``zammad``;
    the framework's ``build_auth_provider(settings, inbound)`` calls it with the
    profile's ``auth.inbound`` block (unused here — config comes from settings).
    """
    from bg_mcpcore.auth.storage import build_client_storage
    from fastmcp.server.auth.oauth_proxy import OAuthProxy

    if not settings.zammad_oauth_client_id or not settings.zammad_oauth_client_secret:
        raise ValueError(
            "ZAMMAD_OAUTH_CLIENT_ID and ZAMMAD_OAUTH_CLIENT_SECRET are required for "
            "AUTH_MODE=zammad. Create an OAuth2 application: "
            "Zammad Admin -> Manage -> OAuth2 Applications -> Add."
        )

    scopes = settings.zammad_oauth_scopes.split()
    base_url = str(settings.public_base_url)
    signing_key = settings.auth_jwt_signing_key.get_secret_value() or None
    client_storage = build_client_storage(settings)

    token_verifier = _ZammadUserInfoVerifier(
        userinfo_url=settings.zammad_userinfo_url,
        timeout=float(settings.zammad_http_timeout),
        verify_tls=settings.zammad_verify_tls,
        required_scopes=scopes,
    )

    kwargs: dict[str, Any] = {
        "upstream_authorization_endpoint": settings.zammad_authorize_url,
        "upstream_token_endpoint": settings.zammad_token_url,
        "upstream_client_id": settings.zammad_oauth_client_id,
        "upstream_client_secret": settings.zammad_oauth_client_secret.get_secret_value(),
        "token_verifier": token_verifier,
        "base_url": base_url,
        "valid_scopes": scopes,
        "client_storage": client_storage,
    }
    if signing_key:
        kwargs["jwt_signing_key"] = signing_key

    provider = OAuthProxy(**kwargs)
    logger.info(
        "auth.zammad_oauth_configured",
        authorize_endpoint=settings.zammad_authorize_url,
        token_endpoint=settings.zammad_token_url,
        scopes=scopes,
    )
    return provider


__all__ = ["build_zammad_oauth_provider"]
