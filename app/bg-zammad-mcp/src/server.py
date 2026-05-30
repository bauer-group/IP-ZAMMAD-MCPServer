"""
FastMCP server construction.

Wires Settings -> auth provider -> Zammad client -> hand-written tool surface.
Owns the lifespan (version probe + client teardown + middleware wiring).
"""

from __future__ import annotations

import string
from contextlib import asynccontextmanager
from importlib.metadata import PackageNotFoundError, version as _pkg_version
from pathlib import Path
from typing import TYPE_CHECKING, AsyncIterator

import structlog

from auth.provider_factory import build_auth_provider
from auth.role_middleware import RoleAllowlistMiddleware
from config import AuthMode, Settings, get_settings
from logging_setup import print_banner, setup_logging, warn_no_auth, warn_role_audit_only
from rate_limit import build_rate_limit_middleware
from zammad.client import build_zammad_client_from_settings
from zammad.tools import register_all_tools
from zammad.version_probe import ZammadVersionInfo, probe_zammad_version

if TYPE_CHECKING:
    from fastmcp import FastMCP

logger = structlog.stdlib.get_logger("bg-zammad-mcp.server")

try:
    __version__ = _pkg_version("bg-zammad-mcp")
except PackageNotFoundError:
    # Running from source without `pip install` (tests, local dev via src/ on PYTHONPATH).
    __version__ = "0.0.0+local"

SERVER_INSTRUCTIONS = (
    "Self-hosted Zammad helpdesk. Use these tools to list, search, create, "
    "update, and analyse tickets, articles (messages), users, organizations, "
    "groups, tags, and notifications. All calls execute in the context of "
    "the authenticated Zammad user (when AUTH_MODE=zammad), so Zammad's own "
    "permission system enforces fine-grained access on every API call. "
    "Compatible with Zammad v6.x and v7.x."
)


async def build_app(settings: Settings | None = None) -> "FastMCP":
    """Build a fully wired FastMCP instance. Called once per process."""
    from fastmcp import FastMCP
    from mcp.types import Icon

    settings = settings or get_settings()

    setup_logging(log_format=settings.log_format, log_level=settings.log_level)

    _init_sentry_if_configured(settings)

    auth_provider = build_auth_provider(settings)
    zammad_client = build_zammad_client_from_settings(settings)

    # Best-effort Zammad version detection. Surfaced only in the boot banner +
    # debug logs; tool behaviour is identical across v6/v7.
    version_info = await _probe_version_best_effort(zammad_client, settings)

    print_banner(
        version=__version__,
        environment=settings.environment.value,
        auth_mode=settings.auth_mode.value,
        public_base_url=str(settings.public_base_url),
        zammad_url=str(settings.zammad_url),
        zammad_version=version_info.raw,
    )
    if settings.auth_mode is AuthMode.NONE:
        warn_no_auth()
    if (
        settings.auth_mode is AuthMode.ZAMMAD
        and settings.mcp_role_check_audit_only
        and settings.allowed_roles_lower
    ):
        warn_role_audit_only()

    @asynccontextmanager
    async def lifespan(_app: "FastMCP") -> AsyncIterator[dict[str, object]]:
        try:
            yield {
                "settings": settings,
                "zammad_client": zammad_client,
                "zammad_version": version_info,
            }
        finally:
            await zammad_client.aclose()
            logger.info("server.shutdown_complete")

    base_url = str(settings.public_base_url).rstrip("/")
    icon_url = settings.mcp_icon_url or f"{base_url}/logo.svg"
    website_url = settings.mcp_website_url or None

    kwargs: dict[str, object] = {
        "name": settings.mcp_display_name,
        "instructions": SERVER_INSTRUCTIONS,
        "lifespan": lifespan,
        "tags": {"zammad"},
    }
    if auth_provider is not None:
        kwargs["auth"] = auth_provider
    if icon_url:
        kwargs["icons"] = [Icon(src=icon_url, mimeType="image/svg+xml")]
    if website_url:
        kwargs["website_url"] = website_url

    mcp = FastMCP(**kwargs)

    # Rate limiter goes on FIRST so it's the cheapest possible rejection
    # path under load - no token validation, no role lookup.
    rate_limit_mw = build_rate_limit_middleware(settings)
    if rate_limit_mw is not None:
        mcp.add_middleware(rate_limit_mw)

    # Role allowlist runs AFTER auth (so the AccessToken is available) but
    # BEFORE tool dispatch. Only active for ZAMMAD mode + non-empty
    # allowlist, because external OIDC tokens don't carry Zammad roles
    # and there's nothing to match against.
    if (
        settings.auth_mode is AuthMode.ZAMMAD
        and settings.allowed_roles_lower
    ):
        mcp.add_middleware(
            RoleAllowlistMiddleware(
                allowed_roles=settings.allowed_roles_lower,
                audit_only=settings.mcp_role_check_audit_only,
            )
        )
        logger.info(
            "auth.role_allowlist_active",
            allowed_roles=sorted(settings.allowed_roles_lower),
            audit_only=settings.mcp_role_check_audit_only,
        )
    elif settings.auth_mode is AuthMode.ZAMMAD and not settings.allowed_roles_lower:
        logger.warning(
            "auth.role_allowlist_disabled",
            note="MCP_ALLOWED_ROLES is empty - any authenticated Zammad user can call this MCP",
        )

    # Register the hand-curated tool surface.
    tool_count = register_all_tools(mcp, client=zammad_client, settings=settings)
    logger.info(
        "server.tools_registered",
        tool_count=tool_count,
        zammad_version=version_info.major_label,
    )

    _register_healthz_route(mcp)
    _register_logo_route(mcp)
    _register_index_route(mcp, settings)
    return mcp


async def _probe_version_best_effort(
    client: object, settings: Settings
) -> ZammadVersionInfo:
    """Run the Zammad version probe with appropriate token source.

    The probe is non-fatal - we always return SOMETHING so the rest of
    boot can proceed even when /version is unreachable.
    """
    # Use the static API token for the probe when available. In pure-OAuth
    # mode without ZAMMAD_API_TOKEN, the probe can't authenticate and we
    # skip it (returns ZammadVersionInfo(raw=None, major=None)).
    if settings.zammad_api_token is None:
        logger.debug("zammad.version_probe_skipped_no_static_token")
        return ZammadVersionInfo(raw=None, major=None)
    try:
        return await probe_zammad_version(client)  # type: ignore[arg-type]
    except Exception as exc:  # noqa: BLE001 - intentional fail-soft
        logger.warning("zammad.version_probe_unexpected_error", error=str(exc))
        return ZammadVersionInfo(raw=None, major=None)


def _register_logo_route(mcp: "FastMCP") -> None:
    """Serve /logo.svg so the OAuth consent screen can fetch the brand icon
    from the same origin as the MCP server (no CORS surprises)."""
    from starlette.responses import Response

    logo_path = Path(__file__).parent / "static" / "logo.svg"
    try:
        svg_bytes = logo_path.read_bytes()
    except FileNotFoundError:
        logger.warning("logo.template_missing", path=str(logo_path))
        return

    @mcp.custom_route("/logo.svg", methods=["GET"], include_in_schema=False)
    async def _logo(_request) -> Response:  # type: ignore[no-untyped-def]
        return Response(
            svg_bytes,
            media_type="image/svg+xml",
            headers={"Cache-Control": "public, max-age=86400"},
        )


def _register_index_route(mcp: "FastMCP", settings: Settings) -> None:
    """Serve a human-readable status + quickstart page at /."""
    from starlette.responses import HTMLResponse

    template_path = Path(__file__).parent / "static" / "index.html"
    try:
        raw = template_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        logger.warning("index.template_missing", path=str(template_path))
        return

    base_url = str(settings.public_base_url).rstrip("/")
    rendered = string.Template(raw).safe_substitute(
        version=__version__,
        protocol="MCP / Streamable HTTP",
        environment=settings.environment.value,
        auth_mode=settings.auth_mode.value,
        mcp_url=f"{base_url}/mcp",
        zammad_url=str(settings.zammad_url).rstrip("/"),
    )

    @mcp.custom_route("/", methods=["GET"], include_in_schema=False)
    async def _index(_request) -> HTMLResponse:  # type: ignore[no-untyped-def]
        return HTMLResponse(
            rendered,
            headers={"Cache-Control": "public, max-age=60"},
        )


def _register_healthz_route(mcp: "FastMCP") -> None:
    """Expose /healthz that returns 200 OK as soon as the server is up.

    Distinct from Zammad's /api/v1/monitoring/health_check (which sits behind
    Zammad's own auth wall).
    """
    from starlette.responses import JSONResponse

    @mcp.custom_route("/healthz", methods=["GET"], include_in_schema=False)
    async def _healthz(_request) -> JSONResponse:  # type: ignore[no-untyped-def]
        return JSONResponse({"status": "ok"}, status_code=200)


def _init_sentry_if_configured(settings: Settings) -> None:
    if not settings.sentry_dsn:
        return
    try:
        import sentry_sdk
    except ImportError:
        logger.warning("sentry.sdk_missing", hint="install sentry-sdk to enable error tracking")
        return

    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        environment=settings.sentry_environment or settings.environment.value,
        traces_sample_rate=settings.sentry_traces_sample_rate,
        release=__version__,
        send_default_pii=False,
    )
    logger.info(
        "sentry.initialized",
        environment=settings.sentry_environment or settings.environment.value,
        traces_sample_rate=settings.sentry_traces_sample_rate,
    )
