"""Config / Settings validation tests.

Pydantic Settings is the security boundary for AUTH_MODE selection, so
every branch in the model_validator is exercised here.
"""

from __future__ import annotations

import pytest

from config import AuthMode, Environment, Settings, get_settings


def test_defaults_reject_production_with_no_auth(clean_env):
    """Default Settings() has AUTH_MODE=none + ENVIRONMENT=production -> must reject."""
    with pytest.raises(ValueError, match="AUTH_MODE=none is forbidden in production"):
        Settings()


def test_none_mode_in_dev_requires_api_token(clean_env):
    """AUTH_MODE=none in development still needs a Zammad token to call the API."""
    clean_env.setenv("ENVIRONMENT", "development")
    clean_env.setenv("ZAMMAD_URL", "https://zammad.example.com")
    clean_env.setenv("AUTH_MODE", "none")
    with pytest.raises(ValueError, match="ZAMMAD_API_TOKEN is required when AUTH_MODE=none"):
        Settings()


def test_none_mode_in_dev_with_token_validates(base_none_env):
    settings = Settings()
    assert settings.auth_mode is AuthMode.NONE
    assert settings.environment is Environment.DEVELOPMENT
    assert settings.zammad_api_token is not None


def test_zammad_mode_requires_oauth_client_id_and_secret(clean_env, jwt_key):
    clean_env.setenv("ENVIRONMENT", "production")
    clean_env.setenv("ZAMMAD_URL", "https://zammad.example.com")
    clean_env.setenv("PUBLIC_BASE_URL", "https://mcp.example.com")
    clean_env.setenv("AUTH_MODE", "zammad")
    clean_env.setenv("AUTH_JWT_SIGNING_KEY", jwt_key)
    # Missing ZAMMAD_OAUTH_CLIENT_ID + ZAMMAD_OAUTH_CLIENT_SECRET.
    with pytest.raises(ValueError) as exc:
        Settings()
    assert "ZAMMAD_OAUTH_CLIENT_ID" in str(exc.value)
    assert "ZAMMAD_OAUTH_CLIENT_SECRET" in str(exc.value)


def test_zammad_mode_validates(base_zammad_env):
    settings = Settings()
    assert settings.auth_mode is AuthMode.ZAMMAD
    assert settings.zammad_oauth_client_id == "test-client-id"
    # Default allowlist is Admin,Agent.
    assert settings.allowed_roles_lower == {"admin", "agent"}


def test_zammad_mode_requires_jwt_signing_key(clean_env):
    clean_env.setenv("ENVIRONMENT", "development")
    clean_env.setenv("PUBLIC_BASE_URL", "https://mcp.example.com")
    clean_env.setenv("ZAMMAD_URL", "https://zammad.example.com")
    clean_env.setenv("AUTH_MODE", "zammad")
    clean_env.setenv("ZAMMAD_OAUTH_CLIENT_ID", "x")
    clean_env.setenv("ZAMMAD_OAUTH_CLIENT_SECRET", "y")
    # No JWT signing key supplied.
    with pytest.raises(ValueError, match="AUTH_JWT_SIGNING_KEY is required"):
        Settings()


def test_zammad_mode_rejects_changeme_jwt_key(base_zammad_env):
    base_zammad_env.setenv("AUTH_JWT_SIGNING_KEY", "CHANGE_ME_AUTH_JWT_SIGNING_KEY")
    with pytest.raises(ValueError, match="CHANGE_ME"):
        Settings()


def test_oidc_mode_requires_discovery_or_explicit_uris(clean_env, jwt_key):
    clean_env.setenv("ENVIRONMENT", "development")
    clean_env.setenv("PUBLIC_BASE_URL", "https://mcp.example.com")
    clean_env.setenv("ZAMMAD_URL", "https://zammad.example.com")
    clean_env.setenv("AUTH_MODE", "oidc")
    clean_env.setenv("AUTH_JWT_SIGNING_KEY", jwt_key)
    clean_env.setenv("ZAMMAD_API_TOKEN", "static-token")
    # Missing both OIDC_DISCOVERY_URL and explicit endpoints.
    with pytest.raises(ValueError, match="OIDC_DISCOVERY_URL"):
        Settings()


def test_oidc_mode_requires_zammad_api_token(clean_env, jwt_key):
    """In OIDC mode the static Zammad token is the only path to the API."""
    clean_env.setenv("ENVIRONMENT", "development")
    clean_env.setenv("PUBLIC_BASE_URL", "https://mcp.example.com")
    clean_env.setenv("ZAMMAD_URL", "https://zammad.example.com")
    clean_env.setenv("AUTH_MODE", "oidc")
    clean_env.setenv("AUTH_JWT_SIGNING_KEY", jwt_key)
    clean_env.setenv("OIDC_DISCOVERY_URL", "https://idp.example.com/.well-known/openid-configuration")
    clean_env.setenv("OIDC_CLIENT_ID", "x")
    clean_env.setenv("OIDC_CLIENT_SECRET", "y")
    # No ZAMMAD_API_TOKEN.
    with pytest.raises(ValueError, match="ZAMMAD_API_TOKEN is required when AUTH_MODE=oidc"):
        Settings()


def test_redis_storage_requires_encryption_key(base_zammad_env):
    base_zammad_env.setenv("AUTH_REDIS_URL", "redis://localhost:6379/0")
    # No AUTH_STORAGE_ENCRYPTION_KEY supplied.
    with pytest.raises(ValueError, match="AUTH_STORAGE_ENCRYPTION_KEY is required"):
        Settings()


def test_redis_storage_rejects_invalid_fernet_key(base_zammad_env):
    base_zammad_env.setenv("AUTH_REDIS_URL", "redis://localhost:6379/0")
    base_zammad_env.setenv("AUTH_STORAGE_ENCRYPTION_KEY", "not-a-real-fernet-key")
    with pytest.raises(ValueError, match="not a valid Fernet key"):
        Settings()


def test_redis_storage_accepts_valid_fernet_key(base_zammad_env, fernet_key):
    base_zammad_env.setenv("AUTH_REDIS_URL", "redis://localhost:6379/0")
    base_zammad_env.setenv("AUTH_STORAGE_ENCRYPTION_KEY", fernet_key)
    settings = Settings()
    assert settings.auth_storage_encryption_key is not None
    assert settings.auth_storage_encryption_key.get_secret_value() == fernet_key


# ── Role allowlist parsing ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Admin,Agent", {"admin", "agent"}),
        ("admin, AGENT, Customer", {"admin", "agent", "customer"}),
        ("Admin", {"admin"}),
        ("", set()),
        ("   ", set()),
        ("Admin,,Agent", {"admin", "agent"}),
    ],
)
def test_role_allowlist_parsing(base_zammad_env, raw, expected):
    base_zammad_env.setenv("MCP_ALLOWED_ROLES", raw)
    settings = Settings()
    assert settings.allowed_roles_lower == expected


def test_role_allowlist_default_is_admin_agent(base_zammad_env):
    settings = Settings()
    assert settings.allowed_roles_lower == {"admin", "agent"}


# ── Convenience accessors ──────────────────────────────────────────────────


def test_zammad_api_base_strips_trailing_slash(base_none_env):
    base_none_env.setenv("ZAMMAD_URL", "https://zammad.example.com/")
    settings = Settings()
    assert settings.zammad_api_base == "https://zammad.example.com/api/v1"


def test_zammad_authorize_url_uses_default_path(base_zammad_env):
    settings = Settings()
    assert settings.zammad_authorize_url == "https://zammad.example.com/oauth/authorize"
    assert settings.zammad_token_url == "https://zammad.example.com/oauth/token"
    assert settings.zammad_userinfo_url == "https://zammad.example.com/api/v1/users/me"


def test_get_settings_returns_singleton(base_none_env):
    a = get_settings()
    b = get_settings()
    assert a is b


def test_get_settings_force_reload_picks_up_env_changes(base_none_env, base_zammad_env):
    """`base_zammad_env` clears and resets env. Singleton must re-read."""
    first = get_settings()
    assert first.auth_mode is AuthMode.ZAMMAD  # base_zammad_env wins (last fixture)
    second = get_settings(force_reload=True)
    assert second.auth_mode is AuthMode.ZAMMAD
