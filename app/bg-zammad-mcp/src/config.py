"""Zammad MCP Server configuration — a bg-mcpcore ``BaseMcpSettings`` subclass.

The cross-cutting settings (environment, transport, MCP identity, auth
persistence, OIDC, rate limiting, observability) all come from
``bg_mcpcore.BaseMcpSettings``. This subclass adds only the Zammad-specific
backend + OAuth2 + role fields, narrows ``auth_mode`` to the Zammad-supported
set, and enforces the per-mode credential requirements (the universal
fail-closed invariants — none-in-prod, JWT key, Fernet storage key — are
already enforced in core, before ``validate_provider_auth`` runs).

Two trust boundaries:
- inbound (AI client -> MCP)   - OAuth 2.1 + PKCE via Zammad / external OIDC
- outbound (MCP -> Zammad API) - per-user Bearer token (zammad mode) or a
                                 static Token (oidc / none modes)
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from bg_mcpcore import BaseMcpSettings
from bg_mcpcore.settings import get_settings as _core_get_settings
from bg_mcpcore.settings.enums import Environment as Environment  # re-exported for callers
from pydantic import Field, HttpUrl, SecretStr, field_validator
from pydantic_settings import NoDecode


class AuthMode(StrEnum):
    # Primary mode: Zammad itself is the OAuth2 provider; the user's upstream
    # token is forwarded to every Zammad API call so Zammad's own role system
    # enforces fine-grained access in the caller's context.
    ZAMMAD = "zammad"
    # Secondary mode: an external OIDC IdP authenticates; Zammad calls use a
    # static ZAMMAD_API_TOKEN.
    OIDC = "oidc"
    # Development only: no auth on the MCP endpoint (requires a ZAMMAD_API_TOKEN).
    NONE = "none"


class ZammadRole(StrEnum):
    ADMIN = "admin"
    AGENT = "agent"
    CUSTOMER = "customer"


def _split_csv(raw: object) -> list[str]:
    """Parse a comma-separated env value into a list. Tolerates whitespace."""
    if raw is None or raw == "":
        return []
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    return [item.strip() for item in str(raw).split(",") if item.strip()]


def _has_value(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, SecretStr):
        return bool(value.get_secret_value().strip())
    if isinstance(value, str):
        return bool(value.strip())
    return bool(value)


class Settings(BaseMcpSettings):
    """Zammad-specific settings on top of the shared bg-mcpcore base."""

    # Narrow the generic ``auth_mode`` (a free str on the base) to Zammad's set.
    auth_mode: AuthMode = AuthMode.NONE

    # This server's consent-screen name (the base leaves it required).
    mcp_display_name: str = "BAUER GROUP Zammad"

    # ── Zammad backend ───────────────────────────────────────────────────────
    zammad_url: HttpUrl = Field(
        default="http://zammad:3000",  # type: ignore[assignment]
        description="Base URL of the Zammad instance (without /api/v1).",
    )
    zammad_api_token: SecretStr | None = Field(
        default=None,
        description="Static Zammad Personal Access Token used in oidc / none modes.",
    )
    zammad_http_timeout: int = Field(default=30, ge=1, le=300)
    zammad_verify_tls: bool = True
    zammad_version_hint: Literal["auto", "v6", "v7"] = "auto"

    # ── Zammad OAuth2 (AUTH_MODE=zammad) ──────────────────────────────────────
    zammad_oauth_client_id: str | None = None
    zammad_oauth_client_secret: SecretStr | None = None
    zammad_oauth_scopes: str = "read write"
    zammad_oauth_authorize_path: str = "/oauth/authorize"
    zammad_oauth_token_path: str = "/oauth/token"
    zammad_userinfo_path: str = Field(
        default="/api/v1/users/me",
        description="Endpoint used to validate the upstream token + read the role set.",
    )

    # ── Role-based MCP access ─────────────────────────────────────────────────
    mcp_allowed_roles: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["Admin", "Agent"],
        description="Zammad role names allowed to use this MCP (case-insensitive).",
    )
    mcp_role_check_audit_only: bool = False
    mcp_role_cache_ttl_seconds: int = Field(default=60, ge=0, le=3600)

    # ── Validators ─────────────────────────────────────────────────────────────

    @field_validator("mcp_allowed_roles", mode="before")
    @classmethod
    def _parse_csv_list(cls, value: object) -> list[str]:
        return _split_csv(value)

    def validate_provider_auth(self) -> None:
        """Per-mode credential checks (core invariants already ran)."""
        if self.auth_mode is AuthMode.NONE:
            if not _has_value(self.zammad_api_token):
                raise ValueError(
                    "ZAMMAD_API_TOKEN is required when AUTH_MODE=none "
                    "(no upstream user tokens are issued in this mode)"
                )
        elif self.auth_mode is AuthMode.ZAMMAD:
            missing = [
                name
                for name in ("zammad_oauth_client_id", "zammad_oauth_client_secret")
                if not _has_value(getattr(self, name))
            ]
            if missing:
                raise ValueError(
                    f"{', '.join(n.upper() for n in missing)} required for AUTH_MODE=zammad. "
                    "Create an OAuth2 application in Zammad: "
                    "Admin -> Manage -> OAuth2 Applications -> Add."
                )
        elif self.auth_mode is AuthMode.OIDC:
            has_discovery = bool(self.oidc_discovery_url)
            has_explicit = all(
                _has_value(getattr(self, name))
                for name in ("oidc_auth_uri", "oidc_token_uri", "oidc_jwks_uri")
            )
            if not (has_discovery or has_explicit):
                raise ValueError(
                    "AUTH_MODE=oidc requires OIDC_DISCOVERY_URL "
                    "or all of OIDC_AUTH_URI / OIDC_TOKEN_URI / OIDC_JWKS_URI"
                )
            missing = [
                name
                for name in ("oidc_client_id", "oidc_client_secret")
                if not _has_value(getattr(self, name))
            ]
            if missing:
                raise ValueError(f"{', '.join(n.upper() for n in missing)} required for AUTH_MODE=oidc")
            if not _has_value(self.zammad_api_token):
                raise ValueError(
                    "ZAMMAD_API_TOKEN is required when AUTH_MODE=oidc "
                    "(external OIDC tokens cannot be used to call Zammad's API)"
                )

    # ── Convenience accessors ──────────────────────────────────────────────────

    @property
    def zammad_api_base(self) -> str:
        return str(self.zammad_url).rstrip("/") + "/api/v1"

    @property
    def zammad_authorize_url(self) -> str:
        return str(self.zammad_url).rstrip("/") + self.zammad_oauth_authorize_path

    @property
    def zammad_token_url(self) -> str:
        return str(self.zammad_url).rstrip("/") + self.zammad_oauth_token_path

    @property
    def zammad_userinfo_url(self) -> str:
        return str(self.zammad_url).rstrip("/") + self.zammad_userinfo_path

    @property
    def allowed_roles_lower(self) -> set[str]:
        return {role.strip().lower() for role in self.mcp_allowed_roles if role.strip()}


def get_settings(force_reload: bool = False) -> Settings:
    """Lazy singleton (delegates to bg-mcpcore's per-class settings cache)."""
    return _core_get_settings(Settings, force_reload=force_reload)


__all__ = ["AuthMode", "Environment", "Settings", "ZammadRole", "get_settings"]
