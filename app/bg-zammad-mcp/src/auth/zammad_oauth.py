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
* Tokens are validated by hitting Zammad's `/api/v1/users/me` (cheap, returns
  401 on an invalid token, and returns the role set the `access_control` gate
  matches against anyway). ``expand=true`` is what turns Zammad's numeric
  ``role_ids`` into the ``roles`` name array that gate compares against.

Verification cost
-----------------
Zammad issues opaque tokens, so there is no offline (JWKS) verification path -
every MCP request costs a round trip. Three things keep that affordable:

* one pooled ``httpx.AsyncClient`` for the lifetime of the verifier, so
  keep-alive actually applies (a per-call ``async with`` client meant a fresh
  TLS handshake on every single request),
* a short TTL cache of SUCCESSFUL verifications keyed on a hash of the token
  (``MCP_ROLE_CACHE_TTL_SECONDS``); failures are never cached, so a rejection
  is always live,
* one bounded retry for connect-phase errors and 5xx only - never for 401 -
  so a momentary Zammad hiccup does not 401 every connected user at once.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from typing import Any

import httpx
import structlog

from config import Settings

logger = structlog.stdlib.get_logger("bg-zammad-mcp.auth.zammad")

# One retry, briefly delayed. Enough to ride out a Zammad restart blip or a
# dropped keep-alive connection; short enough not to stack up under real load.
_RETRY_DELAY_SECONDS = 0.25
# Upper bound on the verification cache, so a token-spraying client cannot grow
# it without limit. Comfortably above any realistic concurrent-user count.
_CACHE_MAX_ENTRIES = 2048


class _ZammadUserInfoVerifier:
    """Verify a Zammad access token by calling /api/v1/users/me.

    Implements the FastMCP TokenVerifier protocol's `verify_token(token)`
    contract: returns an `AccessToken` on success, or None when the token is
    rejected (never raises - a raise and a None are both "rejected", but None
    keeps the failure out of the traceback path).

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
        cache_ttl_seconds: int = 0,
    ) -> None:
        self._userinfo_url = userinfo_url
        self._timeout = timeout
        self._verify_tls = verify_tls
        self._required_scopes = list(required_scopes or [])
        self._cache_ttl = max(0, cache_ttl_seconds)
        # token-hash -> (expires_at_monotonic, AccessToken)
        self._cache: dict[str, tuple[float, Any]] = {}
        self._client: httpx.AsyncClient | None = None
        self._client_lock = asyncio.Lock()

    @property
    def required_scopes(self) -> list[str]:
        # FastMCP reads this attribute when emitting the
        # /.well-known/oauth-protected-resource metadata.
        return list(self._required_scopes)

    # ── HTTP client lifecycle ────────────────────────────────────────────────

    async def _get_client(self) -> httpx.AsyncClient:
        """One pooled client, created on first use.

        Built lazily rather than in ``__init__`` because httpx binds its
        transport to the running event loop, and the provider is constructed
        during settings assembly - before uvicorn's loop exists.
        """
        if self._client is not None and not self._client.is_closed:
            return self._client
        async with self._client_lock:
            if self._client is None or self._client.is_closed:
                self._client = httpx.AsyncClient(
                    timeout=self._timeout,
                    verify=self._verify_tls,
                    follow_redirects=False,
                    limits=httpx.Limits(
                        max_connections=32,
                        max_keepalive_connections=16,
                        keepalive_expiry=30.0,
                    ),
                    headers={"Accept": "application/json"},
                )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()

    # ── cache ────────────────────────────────────────────────────────────────

    @staticmethod
    def _cache_key(token: str) -> str:
        # Hash rather than store the raw token: the cache is an in-process dict
        # that could end up in a heap dump or a debugger frame.
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def _cache_get(self, key: str) -> Any | None:
        entry = self._cache.get(key)
        if entry is None:
            return None
        expires_at, access_token = entry
        if expires_at <= time.monotonic():
            self._cache.pop(key, None)
            return None
        return access_token

    def _cache_put(self, key: str, access_token: Any) -> None:
        if self._cache_ttl <= 0:
            return
        if len(self._cache) >= _CACHE_MAX_ENTRIES:
            # Cheap eviction: drop everything already expired, and if that frees
            # nothing, drop the oldest insertion. Not an LRU, but this bound is
            # a safety valve rather than a hot path.
            now = time.monotonic()
            for stale in [k for k, (exp, _) in self._cache.items() if exp <= now]:
                self._cache.pop(stale, None)
            if len(self._cache) >= _CACHE_MAX_ENTRIES:
                self._cache.pop(next(iter(self._cache)), None)
        self._cache[key] = (time.monotonic() + self._cache_ttl, access_token)

    # ── verification ─────────────────────────────────────────────────────────

    async def verify_token(self, token: str) -> Any | None:
        """Return an AccessToken-shaped object or None when the token is invalid."""
        key = self._cache_key(token)
        cached = self._cache_get(key)
        if cached is not None:
            return cached

        response = await self._fetch_userinfo(token)
        if response is None:
            return None

        if response.status_code == 401:
            # Definitive rejection - never cached, never retried.
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
            logger.warning("zammad.userinfo_not_json")
            return None

        access_token = self._to_access_token(token, payload)
        if access_token is None:
            return None
        self._cache_put(key, access_token)
        return access_token

    async def _fetch_userinfo(self, token: str) -> httpx.Response | None:
        """GET /users/me, retrying once on a transport error or 5xx.

        A 401 is a verdict, not a fault, so it is returned immediately. Anything
        else that could be transient (connection refused during a Zammad
        restart, a 502 from the reverse proxy) gets exactly one more chance -
        without it, a few seconds of Zammad unavailability logs out every
        connected user simultaneously.
        """
        client = await self._get_client()
        headers = {"Authorization": f"Bearer {token}"}
        params = {"expand": "true"}

        for attempt in (1, 2):
            try:
                response = await client.get(self._userinfo_url, headers=headers, params=params)
            except httpx.HTTPError as exc:
                if attempt == 1:
                    logger.info("zammad.userinfo_retrying", reason="transport", error=str(exc))
                    await asyncio.sleep(_RETRY_DELAY_SECONDS)
                    continue
                logger.warning("zammad.userinfo_unreachable", error=str(exc))
                return None

            if response.status_code >= 500 and attempt == 1:
                logger.info("zammad.userinfo_retrying", reason="upstream_5xx",
                            status=response.status_code)
                await asyncio.sleep(_RETRY_DELAY_SECONDS)
                continue
            if response.status_code >= 500:
                logger.warning("zammad.userinfo_server_error", status=response.status_code)
                return None
            return response
        return None

    def _to_access_token(self, token: str, payload: dict[str, Any]) -> Any | None:
        from mcp.server.auth.provider import AccessToken

        sub = _stringify(payload.get("id"))
        if sub is None:
            return None
        login = payload.get("login") or payload.get("email") or sub

        # Only meaningful when caching: it states how long this assertion is
        # good for. With caching off it must stay None - `now + 0` would read
        # as already expired.
        expires_at = int(time.time()) + self._cache_ttl if self._cache_ttl > 0 else None

        # We carry the upstream token forward in the AccessToken claims so the
        # per_user_token resolver can retrieve it without a storage round-trip.
        # The token itself never appears in FastMCP-issued JWTs sent to clients -
        # it only lives on the server-side AccessToken during request processing.
        return AccessToken(
            token=token,
            client_id=sub,
            subject=sub,
            scopes=list(self._required_scopes),
            expires_at=expires_at,
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
            "Zammad Admin -> System -> API -> Applications -> New Application."
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
        cache_ttl_seconds=settings.mcp_role_cache_ttl_seconds,
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
    # Dynamic client registration is unauthenticated by design; without this an
    # arbitrary party can register a client whose redirect_uri they control.
    if settings.mcp_allowed_client_redirect_uris:
        kwargs["allowed_client_redirect_uris"] = list(settings.mcp_allowed_client_redirect_uris)

    provider = OAuthProxy(**kwargs)
    logger.info(
        "auth.zammad_oauth_configured",
        authorize_endpoint=settings.zammad_authorize_url,
        token_endpoint=settings.zammad_token_url,
        scopes=scopes,
        verification_cache_ttl=settings.mcp_role_cache_ttl_seconds,
        allowed_client_redirect_uris=len(settings.mcp_allowed_client_redirect_uris) or "any",
    )
    return provider


__all__ = ["build_zammad_oauth_provider"]
