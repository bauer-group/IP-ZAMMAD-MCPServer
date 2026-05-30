"""
Generic OIDC provider via FastMCP's OIDCProxy / OAuthProxy.

Strategy:
  - If OIDC_DISCOVERY_URL is set, use OIDCProxy - it auto-discovers all
    endpoints, picks up JWKS rotations, and is the recommended modern path.
  - If only OIDC_*_URI vars are set, fall back to OAuthProxy with the explicit
    endpoints. Required when the upstream IdP doesn't expose a discovery doc.

Works with anything that speaks standard OIDC: Authentik, Keycloak, Zitadel,
Auth0, Okta, Cognito, Microsoft Entra ID, Google Workspace, ...

Trust model
-----------
In this mode the external OIDC token validates the MCP caller's identity
but is NOT forwarded to Zammad - Zammad doesn't trust the external IdP.
Outbound Zammad API calls fall back to the static ZAMMAD_API_TOKEN.

Use this mode when:
  * You already have SSO via Entra / Keycloak / ... and want to use it
    for the MCP endpoint
  * Your Zammad version pre-dates the OAuth2-Applications feature (< 6.0)
"""

from __future__ import annotations

from typing import Any

import httpx
import structlog

from config import Settings

logger = structlog.stdlib.get_logger("bg-zammad-mcp.auth.oidc")


class OIDCDiscoveryError(RuntimeError):
    """Raised when discovery metadata cannot be loaded or is malformed."""


def discover_endpoints(discovery_url: str, *, timeout: float = 10.0) -> dict[str, Any]:
    """Fetch the OIDC discovery document and validate the required fields."""
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            response = client.get(discovery_url, headers={"Accept": "application/json"})
            response.raise_for_status()
            doc = response.json()
    except httpx.HTTPError as exc:
        raise OIDCDiscoveryError(f"Failed to fetch {discovery_url}: {exc}") from exc
    except ValueError as exc:
        raise OIDCDiscoveryError(f"Discovery doc at {discovery_url} is not valid JSON") from exc

    required = ("authorization_endpoint", "token_endpoint", "jwks_uri", "issuer")
    missing = [key for key in required if not doc.get(key)]
    if missing:
        raise OIDCDiscoveryError(
            f"OIDC discovery doc missing required fields: {', '.join(missing)}"
        )
    return doc


def build_generic_oidc_provider(settings: Settings) -> Any:
    from .client_storage import build_client_storage

    if not settings.oidc_client_id or not settings.oidc_client_secret:
        raise ValueError("OIDC_CLIENT_ID and OIDC_CLIENT_SECRET are required")

    scopes = settings.oidc_scopes.split()
    base_url = str(settings.public_base_url)
    signing_key = settings.auth_jwt_signing_key.get_secret_value() or None
    client_storage = build_client_storage(settings)

    if settings.oidc_discovery_url:
        # Validate the discovery doc up-front so a misconfigured URL fails at
        # boot rather than on the first user login.
        try:
            discover_endpoints(settings.oidc_discovery_url)
        except OIDCDiscoveryError as exc:
            raise ValueError(f"OIDC discovery failed: {exc}") from exc

        from fastmcp.server.auth.oidc_proxy import OIDCProxy

        kwargs: dict[str, Any] = {
            "config_url": settings.oidc_discovery_url,
            "client_id": settings.oidc_client_id,
            "client_secret": settings.oidc_client_secret.get_secret_value(),
            "base_url": base_url,
            "required_scopes": scopes,
            "client_storage": client_storage,
        }
        if settings.oidc_issuer:
            kwargs["issuer_url"] = settings.oidc_issuer
        if signing_key:
            kwargs["jwt_signing_key"] = signing_key

        provider = OIDCProxy(**kwargs)
        logger.info(
            "auth.oidc_configured",
            mode="discovery",
            config_url=settings.oidc_discovery_url,
            scopes=scopes,
        )
        return provider

    # Explicit endpoints path - uses the lower-level OAuthProxy.
    auth_uri = settings.oidc_auth_uri
    token_uri = settings.oidc_token_uri
    jwks_uri = settings.oidc_jwks_uri
    if not (auth_uri and token_uri and jwks_uri):
        raise ValueError(
            "OIDC requires OIDC_DISCOVERY_URL or all of "
            "OIDC_AUTH_URI / OIDC_TOKEN_URI / OIDC_JWKS_URI"
        )

    from fastmcp.server.auth.oauth_proxy import OAuthProxy
    from fastmcp.server.auth.providers.jwt import JWTVerifier

    issuer = settings.oidc_issuer or auth_uri.rsplit("/", 1)[0]
    token_verifier = JWTVerifier(
        jwks_uri=jwks_uri,
        issuer=issuer,
        required_scopes=scopes,
    )
    kwargs = {
        "upstream_authorization_endpoint": auth_uri,
        "upstream_token_endpoint": token_uri,
        "upstream_client_id": settings.oidc_client_id,
        "upstream_client_secret": settings.oidc_client_secret.get_secret_value(),
        "token_verifier": token_verifier,
        "base_url": base_url,
        "valid_scopes": scopes,
        "client_storage": client_storage,
    }
    if signing_key:
        kwargs["jwt_signing_key"] = signing_key

    provider = OAuthProxy(**kwargs)
    logger.info(
        "auth.oidc_configured",
        mode="explicit",
        issuer=issuer,
        scopes=scopes,
    )
    return provider
