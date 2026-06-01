"""Tests for the Zammad Settings (a bg-mcpcore BaseMcpSettings subclass).

Covers the per-AUTH_MODE credential validation (``validate_provider_auth``)
layered on top of bg-mcpcore's universal fail-closed invariants, plus the
Zammad-specific convenience accessors and role-list parsing.
"""

from __future__ import annotations

import pytest

from config import AuthMode, Settings

# ── valid construction ───────────────────────────────────────────────────────


def test_none_mode_dev_constructs(base_none_env) -> None:  # type: ignore[no-untyped-def]
    settings = Settings()
    assert settings.auth_mode is AuthMode.NONE
    assert settings.zammad_api_base == "https://zammad.example.com/api/v1"


def test_zammad_mode_constructs(base_zammad_env) -> None:  # type: ignore[no-untyped-def]
    settings = Settings()
    assert settings.auth_mode is AuthMode.ZAMMAD
    assert settings.zammad_authorize_url == "https://zammad.example.com/oauth/authorize"
    assert settings.zammad_token_url == "https://zammad.example.com/oauth/token"
    assert settings.zammad_userinfo_url == "https://zammad.example.com/api/v1/users/me"


# ── fail-closed invariants (core + provider) ─────────────────────────────────


def test_none_in_production_rejected(clean_env) -> None:  # type: ignore[no-untyped-def]
    clean_env.setenv("ENVIRONMENT", "production")
    clean_env.setenv("PUBLIC_BASE_URL", "https://mcp.example.com")
    clean_env.setenv("ZAMMAD_URL", "https://zammad.example.com")
    clean_env.setenv("AUTH_MODE", "none")
    clean_env.setenv("ZAMMAD_API_TOKEN", "tok")
    with pytest.raises(ValueError, match="forbidden in production"):
        Settings()


def test_none_without_api_token_rejected(clean_env) -> None:  # type: ignore[no-untyped-def]
    clean_env.setenv("ENVIRONMENT", "development")
    clean_env.setenv("PUBLIC_BASE_URL", "http://localhost:8000")
    clean_env.setenv("ZAMMAD_URL", "https://zammad.example.com")
    clean_env.setenv("AUTH_MODE", "none")
    with pytest.raises(ValueError, match="ZAMMAD_API_TOKEN is required"):
        Settings()


def test_zammad_without_oauth_creds_rejected(clean_env) -> None:  # type: ignore[no-untyped-def]
    clean_env.setenv("ENVIRONMENT", "development")
    clean_env.setenv("PUBLIC_BASE_URL", "https://mcp.example.com")
    clean_env.setenv("ZAMMAD_URL", "https://zammad.example.com")
    clean_env.setenv("AUTH_MODE", "zammad")
    clean_env.setenv("AUTH_JWT_SIGNING_KEY", "f" * 64)
    with pytest.raises(ValueError, match="AUTH_MODE=zammad"):
        Settings()


def test_active_mode_requires_jwt_signing_key(clean_env) -> None:  # type: ignore[no-untyped-def]
    clean_env.setenv("ENVIRONMENT", "development")
    clean_env.setenv("PUBLIC_BASE_URL", "https://mcp.example.com")
    clean_env.setenv("ZAMMAD_URL", "https://zammad.example.com")
    clean_env.setenv("AUTH_MODE", "zammad")
    clean_env.setenv("ZAMMAD_OAUTH_CLIENT_ID", "cid")
    clean_env.setenv("ZAMMAD_OAUTH_CLIENT_SECRET", "secret")
    # No AUTH_JWT_SIGNING_KEY -> bg-mcpcore's core invariant fails.
    with pytest.raises(ValueError, match="AUTH_JWT_SIGNING_KEY"):
        Settings()


def test_oidc_without_api_token_rejected(clean_env) -> None:  # type: ignore[no-untyped-def]
    clean_env.setenv("ENVIRONMENT", "development")
    clean_env.setenv("PUBLIC_BASE_URL", "https://mcp.example.com")
    clean_env.setenv("ZAMMAD_URL", "https://zammad.example.com")
    clean_env.setenv("AUTH_MODE", "oidc")
    clean_env.setenv("AUTH_JWT_SIGNING_KEY", "f" * 64)
    clean_env.setenv("OIDC_DISCOVERY_URL", "https://idp.example.com/.well-known/openid-configuration")
    clean_env.setenv("OIDC_CLIENT_ID", "cid")
    clean_env.setenv("OIDC_CLIENT_SECRET", "secret")
    with pytest.raises(ValueError, match="ZAMMAD_API_TOKEN is required"):
        Settings()


# ── role allowlist parsing ────────────────────────────────────────────────────


def test_allowed_roles_csv_parsing(base_none_env) -> None:  # type: ignore[no-untyped-def]
    base_none_env.setenv("MCP_ALLOWED_ROLES", "Admin, Agent ,Customer")
    settings = Settings()
    assert settings.allowed_roles_lower == {"admin", "agent", "customer"}


def test_allowed_roles_default(base_none_env) -> None:  # type: ignore[no-untyped-def]
    settings = Settings()
    assert settings.allowed_roles_lower == {"admin", "agent"}
