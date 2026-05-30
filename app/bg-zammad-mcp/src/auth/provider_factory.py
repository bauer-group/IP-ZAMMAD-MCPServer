"""
Auth provider factory.

Single dispatch point: AUTH_MODE -> concrete FastMCP auth provider (or None).
Lives in its own module so unit tests can monkeypatch one provider builder
without dragging the others into the import graph.
"""

from __future__ import annotations

from typing import Any

import structlog

from config import AuthMode, Environment, Settings

logger = structlog.stdlib.get_logger("bg-zammad-mcp.auth.factory")


def build_auth_provider(settings: Settings) -> Any | None:
    """
    Return a configured FastMCP auth provider, or None for AUTH_MODE=none.

    None is only permitted when ENVIRONMENT=development - Pydantic validates
    this at boot, so by the time we reach here we trust the setting.
    """
    mode = settings.auth_mode

    if mode is AuthMode.NONE:
        if settings.environment is not Environment.DEVELOPMENT:
            # Defensive duplicate of the Pydantic check - never trust callers.
            raise RuntimeError(
                "AUTH_MODE=none requires ENVIRONMENT=development "
                "(refusing to start an unauthenticated MCP in production)"
            )
        logger.warning("auth.none_selected", environment=settings.environment.value)
        return None

    if mode is AuthMode.ZAMMAD:
        from .zammad_oauth import build_zammad_oauth_provider

        return build_zammad_oauth_provider(settings)

    if mode is AuthMode.OIDC:
        from .generic_oidc import build_generic_oidc_provider

        return build_generic_oidc_provider(settings)

    # Should never happen - StrEnum constrains the values.
    raise ValueError(f"Unknown AUTH_MODE: {mode!r}")
