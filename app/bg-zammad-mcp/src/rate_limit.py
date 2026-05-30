"""
Token-bucket rate limiting for FastMCP requests.

Wraps `fastmcp.server.middleware.rate_limiting.RateLimitingMiddleware` with a
client-identity resolver tailored for this server's deployment topology:

1. Authenticated requests are keyed on the OAuth subject - the most stable
   and least forgeable identity we have. Two parallel calls from the same
   user share one bucket regardless of source IP (mobile + desktop, IP
   roaming, NAT).
2. Anonymous requests are keyed on the source IP. Behind a reverse proxy
   (Traefik / Cloudflare -> Traefik / etc.) we MUST read X-Forwarded-For
   instead of `request.client.host`, otherwise every request looks like
   it came from the proxy and shares one bucket.
3. `request.client.host` is used as a fallback for direct connections
   (no proxy, or `trusted_proxy_hops=0`).
4. Stdio transport has no HTTP request at all - collapse all stdio
   callers to a single sentinel bucket. Stdio is local-only by definition.

X-Forwarded-For trust model
---------------------------
Traefik prepends to XFF, so for "1.2.3.4, 10.0.0.1, 172.16.0.5":
  - 1.2.3.4  is the original (possibly forged) client claim
  - 10.0.0.1 is what hop-1 saw
  - 172.16.0.5 is what hop-2 saw

The value at position `-trusted_proxy_hops` is the outermost address THIS
server can verify - anything to the left was forwarded by a proxy we don't
control and could be a spoofed header.
"""

from __future__ import annotations

from collections.abc import Callable

import structlog
from fastmcp.server.dependencies import (
    get_access_token,
    get_http_headers,
    get_http_request,
)
from fastmcp.server.middleware import MiddlewareContext
from fastmcp.server.middleware.rate_limiting import RateLimitingMiddleware

from config import Settings

logger = structlog.stdlib.get_logger("bg-zammad-mcp.rate_limit")


_STDIO_SENTINEL = "ip:local"
_ANON_SENTINEL = "ip:unknown"


def resolve_client_id(
    *,
    auth_subject: str | None,
    xff_header: str | None,
    direct_remote_ip: str | None,
    trusted_proxy_hops: int,
) -> str:
    """Pure client-ID resolution - no FastMCP context required.

    Kept as a free function so the logic can be unit-tested without
    constructing MiddlewareContext / setting up the FastMCP context vars.
    """
    if auth_subject:
        return f"sub:{auth_subject}"

    if trusted_proxy_hops > 0 and xff_header:
        parts = [p.strip() for p in xff_header.split(",") if p.strip()]
        if parts:
            # Clip if XFF has fewer hops than configured - pick the leftmost
            # value we observed, which is still a trusted-proxy view.
            idx = max(-len(parts), -trusted_proxy_hops)
            return f"ip:{parts[idx]}"

    if direct_remote_ip:
        return f"ip:{direct_remote_ip}"

    return _ANON_SENTINEL


def _get_client_id_from_context(trusted_proxy_hops: int) -> Callable[[MiddlewareContext], str]:
    """Build the get_client_id callable RateLimitingMiddleware expects."""

    def get_client_id(_context: MiddlewareContext) -> str:
        token = get_access_token()
        auth_subject: str | None = None
        if token is not None:
            auth_subject = token.claims.get("sub") or token.claims.get("oid")

        headers = get_http_headers()
        xff = headers.get("x-forwarded-for")

        direct: str | None = None
        try:
            request = get_http_request()
        except RuntimeError:
            # stdio transport - no HTTP request bound to this context.
            if auth_subject:
                return f"sub:{auth_subject}"
            return _STDIO_SENTINEL
        else:
            if request.client is not None:
                direct = request.client.host

        return resolve_client_id(
            auth_subject=auth_subject,
            xff_header=xff,
            direct_remote_ip=direct,
            trusted_proxy_hops=trusted_proxy_hops,
        )

    return get_client_id


def build_rate_limit_middleware(settings: Settings) -> RateLimitingMiddleware | None:
    """Construct the configured middleware, or None when disabled."""
    if not settings.rate_limiter_enabled:
        return None

    burst = settings.rate_limiter_burst_capacity
    if burst is None:
        burst = max(1, int(settings.rate_limiter_max_requests_per_second * 2))

    middleware = RateLimitingMiddleware(
        max_requests_per_second=settings.rate_limiter_max_requests_per_second,
        burst_capacity=burst,
        get_client_id=_get_client_id_from_context(settings.rate_limiter_trusted_proxy_hops),
        global_limit=settings.rate_limiter_global,
    )

    logger.info(
        "rate_limit.enabled",
        max_requests_per_second=settings.rate_limiter_max_requests_per_second,
        burst_capacity=burst,
        scope="global" if settings.rate_limiter_global else "per-client",
        trusted_proxy_hops=settings.rate_limiter_trusted_proxy_hops,
    )
    return middleware


__all__ = [
    "build_rate_limit_middleware",
    "resolve_client_id",
]
