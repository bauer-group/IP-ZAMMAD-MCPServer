"""
Zammad MCP Server - Configuration

Pydantic Settings loaded from environment variables. Two trust boundaries:
- inbound (AI client -> MCP)   - OAuth 2.1 + PKCE via Zammad / external OIDC
- outbound (MCP -> Zammad API) - user's Bearer token (zammad mode) or
                                 static Token (oidc / none modes)

The model_validator below is the only thing that prevents production
deployments from silently slipping into AUTH_MODE=none. Do not relax it.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import (
    Field,
    HttpUrl,
    SecretStr,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Environment(StrEnum):
    PRODUCTION = "production"
    STAGING = "staging"
    DEVELOPMENT = "development"


class AuthMode(StrEnum):
    # Primary mode: Zammad itself is the OAuth2 provider. The user's upstream
    # access token is forwarded to every Zammad API call, so Zammad's own
    # role/permission system enforces fine-grained access in the caller's
    # context. Configure in Zammad: Admin -> Manage -> OAuth2 Applications -> Add.
    ZAMMAD = "zammad"
    # Secondary mode: external OIDC IdP (Entra, Keycloak, Authentik, ...)
    # handles authentication. Zammad API calls use a static ZAMMAD_API_TOKEN.
    # Role enforcement is best-effort (the static token has fixed permissions)
    # and ZAMMAD_API_TOKEN should be scoped to the most restrictive role that
    # still covers the intended workflows.
    OIDC = "oidc"
    # Development only: no auth on the MCP endpoint. Requires
    # ENVIRONMENT=development and a ZAMMAD_API_TOKEN.
    NONE = "none"


class ZammadRole(StrEnum):
    """Canonical Zammad role names (the strings returned in `User.role_ids`
    are numeric, but the role objects' `name` field uses these labels).

    The role middleware compares the user's role names against this set
    after lower-casing both sides, so casing in MCP_ALLOWED_ROLES is
    user-friendly.
    """

    ADMIN = "admin"
    AGENT = "agent"
    CUSTOMER = "customer"


def _split_csv(raw: str | list[str] | None) -> list[str]:
    """Parse a comma-separated env value into a list. Tolerates whitespace."""
    if raw is None or raw == "":
        return []
    if isinstance(raw, list):
        return [item.strip() for item in raw if str(item).strip()]
    return [item.strip() for item in str(raw).split(",") if item.strip()]


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── General ────────────────────────────────────────────────────────────
    environment: Environment = Environment.PRODUCTION
    public_base_url: HttpUrl = Field(
        default="http://localhost:8000",
        description="Public origin used in OAuth redirect URIs - MUST match IdP registration",
    )
    log_format: Literal["console", "json"] = "json"
    log_level: str = "INFO"

    # ── Zammad backend ─────────────────────────────────────────────────────
    zammad_url: HttpUrl = Field(
        default="http://zammad:3000",
        description=(
            "Base URL of the Zammad instance (without /api/v1). The OAuth2 "
            "endpoints (/oauth/authorize, /oauth/token) and the REST API "
            "are both reached from here."
        ),
    )
    zammad_api_token: SecretStr | None = Field(
        default=None,
        description=(
            "Static Zammad API token (Personal Access Token) used in oidc / "
            "none auth modes. Generate in Zammad: User Profile -> Token "
            "Access -> Create. Not used when AUTH_MODE=zammad (user tokens "
            "are forwarded instead)."
        ),
    )
    zammad_http_timeout: int = Field(
        default=30,
        ge=1,
        le=300,
        description="Per-request timeout for outbound calls to Zammad",
    )
    zammad_verify_tls: bool = Field(
        default=True,
        description=(
            "Verify Zammad's TLS certificate. Set to false only when "
            "developing against a self-signed staging instance."
        ),
    )
    zammad_version_hint: Literal["auto", "v6", "v7"] = Field(
        default="auto",
        description=(
            "Informational hint about the target Zammad major version. "
            "'auto' probes /api/v1/version at startup; the result is used "
            "for log context only - tool behaviour is shared across v6/v7."
        ),
    )

    # ── MCP transport ──────────────────────────────────────────────────────
    mcp_transport: Literal["streamable-http", "stdio"] = "streamable-http"
    # Empty = bind to any stack, any interface (dual-stack via main.py
    # patch). Set explicitly to pin: "0.0.0.0", "::", "127.0.0.1", "::1".
    mcp_host: str = ""
    mcp_port: int = Field(default=8000, ge=1, le=65535)

    # ── MCP server identity (consent screen branding) ──────────────────────
    mcp_display_name: str = Field(
        default="BAUER GROUP Zammad",
        description=(
            "Friendly name shown on the OAuth consent screen. Plain text, "
            "not the FastMCP internal identifier (which stays 'bg-zammad-mcp')."
        ),
    )
    mcp_icon_url: str | None = Field(
        default=None,
        description=(
            "Absolute URL to the icon shown on the consent screen. Leave unset "
            "to use the BAUER GROUP logo served by this server at "
            "${PUBLIC_BASE_URL}/logo.svg. Override with any HTTPS URL."
        ),
    )
    mcp_website_url: str | None = Field(
        default="https://go.bauer-group.com/mcp-server",
        description=(
            "Website link rendered behind the server name on the consent screen. "
            "Set to empty string to disable the hyperlink."
        ),
    )

    # ── Auth (MCP-side) ────────────────────────────────────────────────────
    auth_mode: AuthMode = AuthMode.NONE
    auth_jwt_signing_key: SecretStr = Field(
        default=SecretStr(""),
        description="32-byte hex key used to sign FastMCP-issued JWTs",
    )
    auth_redis_url: str | None = None
    auth_storage_encryption_key: SecretStr | None = None
    # When AUTH_REDIS_URL is empty, OAuth state (DCR clients, refresh tokens,
    # auth codes, JTI mappings) is persisted to this directory as an encrypted
    # DiskStore. Encryption key is derived from AUTH_JWT_SIGNING_KEY via HKDF.
    auth_disk_storage_path: str = Field(
        default="/app/data/oauth-storage",
        description=(
            "Filesystem path for the encrypted OAuth state store when "
            "AUTH_REDIS_URL is unset. Mount as a Docker volume in production."
        ),
    )

    # ── Zammad OAuth2 (when AUTH_MODE=zammad) ──────────────────────────────
    # Configure in Zammad: Admin -> Manage -> OAuth2 Applications -> Add.
    # Redirect URI: ${PUBLIC_BASE_URL}/auth/callback
    zammad_oauth_client_id: str | None = None
    zammad_oauth_client_secret: SecretStr | None = None
    zammad_oauth_scopes: str = Field(
        default="read write",
        description=(
            "Zammad OAuth2 scopes requested from the user. Zammad's default "
            "scopes are 'read' and 'write'; some installations expose finer "
            "scopes via API tokens but not via OAuth2 applications."
        ),
    )
    # Zammad doesn't publish a /.well-known/openid-configuration document for
    # OAuth2 Applications, so endpoints are derived from ZAMMAD_URL. Override
    # only if your reverse proxy mounts Zammad on a non-default path.
    zammad_oauth_authorize_path: str = Field(default="/oauth/authorize")
    zammad_oauth_token_path: str = Field(default="/oauth/token")
    zammad_userinfo_path: str = Field(
        default="/api/v1/users/me",
        description=(
            "Endpoint used to (a) validate the upstream token at token-exchange "
            "time and (b) read the user's role set for the role allowlist. "
            "/api/v1/users/me works on both v6 and v7."
        ),
    )

    # ── Generic OIDC (when AUTH_MODE=oidc) ────────────────────────────────
    # Use an external IdP (Entra, Keycloak, Authentik, Zitadel, Auth0, Okta, ...)
    # for inbound auth. Outbound Zammad calls then use the static ZAMMAD_API_TOKEN.
    oidc_discovery_url: str | None = None
    oidc_issuer: str | None = None
    oidc_auth_uri: str | None = None
    oidc_token_uri: str | None = None
    oidc_jwks_uri: str | None = None
    oidc_userinfo_uri: str | None = None
    oidc_client_id: str | None = None
    oidc_client_secret: SecretStr | None = None
    oidc_scopes: str = "openid profile email"
    oidc_username_claim: str = "preferred_username"

    # ── Role-based MCP access ──────────────────────────────────────────────
    # Coarse gate: which Zammad roles may use the MCP at all. Zammad's own
    # permission system still enforces fine-grained access per API call -
    # this allowlist is defense in depth (e.g., "this MCP is for Agents only,
    # never expose to Customers").
    mcp_allowed_roles: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["Admin", "Agent"],
        description=(
            "Comma-separated list of Zammad role names allowed to use this "
            "MCP server. Case-insensitive. Empty list = allow ANY authenticated "
            "Zammad user (NOT recommended in production)."
        ),
    )
    mcp_role_check_audit_only: bool = Field(
        default=False,
        description=(
            "If true, role denials are logged but the request is allowed "
            "through. Use during rollout to verify the allowlist before "
            "switching to enforcement."
        ),
    )
    mcp_role_cache_ttl_seconds: int = Field(
        default=60,
        ge=0,
        le=3600,
        description=(
            "How long to cache /users/me responses per upstream token. "
            "Set to 0 to disable caching. Role changes in Zammad take effect "
            "after at most this many seconds."
        ),
    )

    # ── Rate limiting ──────────────────────────────────────────────────────
    rate_limiter_enabled: bool = True
    rate_limiter_max_requests_per_second: float = Field(
        default=10.0,
        gt=0.0,
        description="Sustained throughput per client (tokens refilled per second).",
    )
    rate_limiter_burst_capacity: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Maximum burst per client. None -> 2x max_requests_per_second "
            "(FastMCP default)."
        ),
    )
    rate_limiter_global: bool = Field(
        default=False,
        description=(
            "If true, one bucket for the whole server (DoS shield). "
            "If false (default), one bucket per client identity."
        ),
    )
    rate_limiter_trusted_proxy_hops: int = Field(
        default=1,
        ge=0,
        le=10,
        description=(
            "How many trusted reverse-proxy hops sit in front of this server. "
            "Used to extract the client IP from X-Forwarded-For. "
            "1 = direct Traefik (default). 2 = Cloudflare -> Traefik. "
            "0 = no proxy; XFF is ignored."
        ),
    )

    # ── Observability ──────────────────────────────────────────────────────
    sentry_dsn: str | None = None
    sentry_environment: str | None = None
    sentry_traces_sample_rate: float = Field(default=0.05, ge=0.0, le=1.0)

    # ── Validators ─────────────────────────────────────────────────────────

    @field_validator(
        "mcp_allowed_roles",
        mode="before",
    )
    @classmethod
    def _parse_csv_list(cls, value: object) -> list[str]:
        return _split_csv(value)  # type: ignore[arg-type]

    @model_validator(mode="after")
    def _validate_auth(self) -> "Settings":
        """Enforce per-AUTH_MODE requirements."""

        if self.auth_mode is AuthMode.NONE:
            if self.environment is Environment.PRODUCTION:
                raise ValueError(
                    "AUTH_MODE=none is forbidden in production "
                    "(set ENVIRONMENT=development if this is intentional)"
                )
            # In none mode we still need SOME way to reach Zammad. The
            # ZAMMAD_API_TOKEN provides server-to-server access.
            if not self.zammad_api_token or not self.zammad_api_token.get_secret_value():
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
                    f"Create an OAuth2 application in Zammad: "
                    f"Admin -> Manage -> OAuth2 Applications -> Add."
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
                raise ValueError(
                    f"{', '.join(n.upper() for n in missing)} required for AUTH_MODE=oidc"
                )
            # In OIDC mode we still need to call Zammad somehow - the static
            # API token is the only path because external OIDC tokens cannot
            # be exchanged for Zammad bearer tokens.
            if not self.zammad_api_token or not self.zammad_api_token.get_secret_value():
                raise ValueError(
                    "ZAMMAD_API_TOKEN is required when AUTH_MODE=oidc "
                    "(external OIDC tokens cannot be used to call Zammad's API)"
                )

        # JWT signing key is required for any active auth mode.
        if self.auth_mode is not AuthMode.NONE:
            signing = self.auth_jwt_signing_key.get_secret_value()
            if not signing or signing.startswith("CHANGE_ME"):
                raise ValueError(
                    "AUTH_JWT_SIGNING_KEY is required and must not be a CHANGE_ME placeholder"
                )

        # Redis-backed client store requires an explicit encryption key.
        if self.auth_redis_url and self.auth_mode is not AuthMode.NONE:
            if (
                not self.auth_storage_encryption_key
                or not self.auth_storage_encryption_key.get_secret_value()
            ):
                raise ValueError(
                    "AUTH_STORAGE_ENCRYPTION_KEY is required when AUTH_REDIS_URL is set"
                )
            _validate_fernet_key(self.auth_storage_encryption_key.get_secret_value())

        return self

    # ── Convenience accessors ──────────────────────────────────────────────

    @property
    def zammad_api_base(self) -> str:
        """REST v1 base URL (Zammad convention - prepended to every request)."""
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
    def is_development(self) -> bool:
        return self.environment is Environment.DEVELOPMENT

    @property
    def allowed_roles_lower(self) -> set[str]:
        """Lowercase canonical view of the role allowlist (for comparison)."""
        return {role.strip().lower() for role in self.mcp_allowed_roles if role.strip()}


def _has_value(value: object) -> bool:
    """True if a config value is present (handles SecretStr + str + None)."""
    if value is None:
        return False
    if isinstance(value, SecretStr):
        return bool(value.get_secret_value())
    if isinstance(value, str):
        return bool(value.strip())
    return bool(value)


def _validate_fernet_key(raw: str) -> None:
    """Ensure AUTH_STORAGE_ENCRYPTION_KEY parses as a Fernet key.

    Fernet expects 32 url-safe base64-encoded bytes (44 chars including
    `=` padding). Operators sometimes paste a hex string or a raw 32-byte
    blob - both are rejected here so the boot fails loudly instead of
    surfacing as an InvalidToken deep inside FastMCP on first login.
    """
    from cryptography.fernet import Fernet

    try:
        Fernet(raw.encode("ascii"))
    except (ValueError, TypeError) as exc:
        raise ValueError(
            "AUTH_STORAGE_ENCRYPTION_KEY is not a valid Fernet key. "
            "Generate one with: python -c \"from cryptography.fernet import "
            "Fernet; print(Fernet.generate_key().decode())\""
        ) from exc


_settings: Settings | None = None


def get_settings(force_reload: bool = False) -> Settings:
    """Lazy singleton - parses env only once unless force_reload=True."""
    global _settings
    if _settings is None or force_reload:
        _settings = Settings()
    return _settings
