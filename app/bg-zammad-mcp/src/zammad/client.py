"""
Zammad HTTP client - thin async wrapper around httpx.AsyncClient.

Two distinct auth modes:

* `bearer`  - OAuth 2.0 access token (`Authorization: Bearer <token>`).
              Used when AUTH_MODE=zammad and we forward the upstream user
              token to Zammad. The token is supplied per-call (the client
              instance has no fixed token).

* `token`   - Zammad Personal Access Token (`Authorization: Token token=<token>`).
              Used when AUTH_MODE=oidc or none. The token is configured on
              the client instance and reused for every request.

A single ZammadClient instance can be reused across both modes - per-call
auth always wins over the instance-level token.
"""

from __future__ import annotations

import asyncio
import random
from typing import Any

import httpx

from .errors import (
    ZammadAuthError,
    ZammadError,
    ZammadServerError,
    ZammadTransportError,
    from_status,
)


# Status codes worth retrying. 408/425/429 are transient on the caller's side;
# 500/502/503/504 are transient on Zammad's side.
RETRYABLE_STATUSES: frozenset[int] = frozenset({408, 425, 429, 500, 502, 503, 504})

DEFAULT_MAX_RETRIES = 3
DEFAULT_BACKOFF_BASE = 0.25  # seconds
DEFAULT_BACKOFF_MAX = 4.0  # seconds


class ZammadClient:
    """
    Async client for the Zammad REST API (v6.x / v7.x).

    Wraps a long-lived httpx.AsyncClient.

    Authentication:
      - Pass `bearer_token` on a per-call basis (preferred for AUTH_MODE=zammad).
      - Pass `api_token` at construction for static server-to-server access.
      - When both are present on a call, the per-call bearer token wins.
    """

    def __init__(
        self,
        base_url: str,
        *,
        api_token: str | None = None,
        timeout: float = 30.0,
        verify_tls: bool = True,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_base: float = DEFAULT_BACKOFF_BASE,
        backoff_max: float = DEFAULT_BACKOFF_MAX,
        user_agent: str = "bg-zammad-mcp/0.1.0",
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._backoff_max = backoff_max
        self._api_token = api_token

        self.httpx_client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(timeout, connect=min(10.0, timeout)),
            headers={
                "Accept": "application/json",
                "User-Agent": user_agent,
            },
            verify=verify_tls,
            limits=httpx.Limits(max_connections=64, max_keepalive_connections=16),
            follow_redirects=False,
        )

    # ── Lifecycle ──────────────────────────────────────────────────────────

    async def __aenter__(self) -> "ZammadClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self.httpx_client.aclose()

    # ── Convenience verbs ──────────────────────────────────────────────────

    async def get(self, path: str, **kwargs: Any) -> httpx.Response:
        return await self.request("GET", path, **kwargs)

    async def post(self, path: str, **kwargs: Any) -> httpx.Response:
        return await self.request("POST", path, **kwargs)

    async def put(self, path: str, **kwargs: Any) -> httpx.Response:
        return await self.request("PUT", path, **kwargs)

    async def patch(self, path: str, **kwargs: Any) -> httpx.Response:
        return await self.request("PATCH", path, **kwargs)

    async def delete(self, path: str, **kwargs: Any) -> httpx.Response:
        return await self.request("DELETE", path, **kwargs)

    # ── Core request loop ──────────────────────────────────────────────────

    async def request(
        self,
        method: str,
        path: str,
        *,
        bearer_token: str | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        """
        Send a request with retry + error mapping.

        Raises a `ZammadError` subclass on non-2xx responses. Returns the
        httpx.Response on success - call `.json()` on it as usual.
        """
        # Layer per-call auth on top of any caller-supplied headers.
        headers = dict(kwargs.pop("headers", {}) or {})
        auth_header = self._build_auth_header(bearer_token)
        if auth_header is None:
            raise ZammadAuthError(
                "No Zammad authentication configured: neither a per-call bearer "
                "token nor a client-level API token is available"
            )
        headers["Authorization"] = auth_header

        attempt = 0

        while True:
            attempt += 1
            try:
                response = await self.httpx_client.request(
                    method, path, headers=headers, **kwargs
                )
            except (
                httpx.ConnectError,
                httpx.ReadError,
                httpx.WriteError,
                httpx.PoolTimeout,
            ) as exc:
                if attempt > self._max_retries:
                    raise ZammadTransportError(
                        f"Network failure talking to Zammad after {attempt - 1} retries: {exc}"
                    ) from exc
                await self._sleep_backoff(attempt)
                continue
            except httpx.TimeoutException as exc:
                if attempt > self._max_retries:
                    raise ZammadTransportError(
                        f"Zammad timed out after {attempt - 1} retries: {exc}"
                    ) from exc
                await self._sleep_backoff(attempt)
                continue

            # Success path
            if 200 <= response.status_code < 300:
                return response

            # Retryable error?
            if response.status_code in RETRYABLE_STATUSES and attempt <= self._max_retries:
                await self._sleep_backoff(attempt, response=response)
                continue

            # Permanent failure - map to typed exception.
            raise self._map_error(response)

    # ── Internals ──────────────────────────────────────────────────────────

    def _build_auth_header(self, bearer_token: str | None) -> str | None:
        """Pick the correct Authorization header value.

        Per-call bearer token wins; falls back to the instance-level API token;
        returns None when neither is available (the caller must reject).
        """
        if bearer_token:
            return f"Bearer {bearer_token}"
        if self._api_token:
            # Zammad-specific Personal Access Token format.
            return f"Token token={self._api_token}"
        return None

    def _map_error(self, response: httpx.Response) -> ZammadError:
        body: dict[str, Any] = {}
        content_type = response.headers.get("content-type", "")
        if "json" in content_type:
            try:
                parsed = response.json()
                if isinstance(parsed, dict):
                    body = parsed
            except ValueError:
                pass
        return from_status(response.status_code, body=body)

    async def _sleep_backoff(
        self,
        attempt: int,
        *,
        response: httpx.Response | None = None,
    ) -> None:
        """Exponential backoff with full jitter; honours Retry-After when present."""
        retry_after: float | None = None
        if response is not None:
            raw = response.headers.get("retry-after")
            if raw:
                try:
                    retry_after = float(raw)
                except ValueError:
                    retry_after = None

        if retry_after is not None:
            delay = min(retry_after, self._backoff_max)
        else:
            exp = self._backoff_base * (2 ** (attempt - 1))
            delay = min(exp, self._backoff_max)
            # Full jitter avoids retry storms when many tools fire at once.
            delay = random.uniform(0.0, delay)

        await asyncio.sleep(delay)

    # ── Domain helpers ─────────────────────────────────────────────────────

    async def version(self, *, bearer_token: str | None = None) -> dict[str, Any]:
        """Call /api/v1/version. Used by the v6/v7 probe at startup.

        Note: Zammad gates /version behind auth, so this needs either a
        per-call bearer token or a configured api_token.
        """
        try:
            response = await self.request("GET", "/version", bearer_token=bearer_token)
        except ZammadError:
            raise
        try:
            return response.json()
        except ValueError as exc:
            raise ZammadError(
                f"Zammad /version returned non-JSON: {response.text[:200]}"
            ) from exc

    async def me(self, *, bearer_token: str) -> dict[str, Any]:
        """Call /api/v1/users/me - returns the authenticated user with roles.

        Used by the role-allowlist middleware on every connection. Must be
        called with a bearer token (the static api_token would return the
        configured service user, not the human-facing role).
        """
        response = await self.request(
            "GET",
            "/users/me",
            bearer_token=bearer_token,
            params={"expand": "true"},
        )
        try:
            return response.json()
        except ValueError as exc:
            raise ZammadError(
                f"Zammad /users/me returned non-JSON: {response.text[:200]}"
            ) from exc

    async def health(self, *, bearer_token: str | None = None) -> dict[str, Any]:
        """Call /api/v1/monitoring/health_check.

        Used by the container HEALTHCHECK and by the MCP `zammad://health`
        resource. Never retries - a single 503 should fail fast so Docker
        can restart us.
        """
        try:
            response = await self.httpx_client.get(
                "/monitoring/health_check",
                headers={
                    "Authorization": self._build_auth_header(bearer_token) or "",
                },
            )
        except httpx.HTTPError as exc:
            raise ZammadTransportError(f"Zammad health probe failed: {exc}") from exc
        if response.status_code >= 500:
            raise ZammadServerError(
                f"Zammad reported unhealthy: HTTP {response.status_code}",
                status_code=response.status_code,
            )
        try:
            return response.json()
        except ValueError as exc:
            raise ZammadError(
                f"Zammad /monitoring/health_check returned non-JSON: {response.text[:200]}"
            ) from exc


def build_zammad_client_from_settings(settings: Any) -> ZammadClient:
    """Factory wiring Settings -> ZammadClient.

    Kept separate from __init__ so tests can construct ZammadClient directly
    with a stub token, while production code always routes through Settings.
    """
    api_token = (
        settings.zammad_api_token.get_secret_value()
        if settings.zammad_api_token is not None
        else None
    )
    return ZammadClient(
        base_url=settings.zammad_api_base,
        api_token=api_token,
        timeout=float(settings.zammad_http_timeout),
        verify_tls=settings.zammad_verify_tls,
    )
