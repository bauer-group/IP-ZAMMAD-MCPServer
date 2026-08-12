"""Tests for the Zammad Settings (a bg-mcpcore BaseMcpSettings subclass).

Covers the per-AUTH_MODE credential validation (``validate_provider_auth``)
layered on top of bg-mcpcore's universal fail-closed invariants, plus the
Zammad-specific convenience accessors and role-list parsing.
"""

from __future__ import annotations

import pytest

from config import ZAMMAD_OAUTH_SCOPE, AuthMode, Settings

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


# ── Zammad OAuth scope: Doorkeeper accepts `full` and nothing else ───────────


def test_zammad_scope_defaults_to_full(base_zammad_env) -> None:  # type: ignore[no-untyped-def]
    # Regression guard for the original "read write" default, which made every
    # authorization attempt fail with invalid_scope *after* the user logged in.
    assert Settings().zammad_oauth_scopes == ZAMMAD_OAUTH_SCOPE


@pytest.mark.parametrize("scopes", ["read write", "read", "full write", ""])
def test_zammad_rejects_non_full_scopes(base_zammad_env, scopes: str) -> None:  # type: ignore[no-untyped-def]
    base_zammad_env.setenv("ZAMMAD_OAUTH_SCOPES", scopes)
    with pytest.raises(ValueError, match="ZAMMAD_OAUTH_SCOPES must be exactly"):
        Settings()


def test_oidc_mode_leaves_zammad_scopes_alone(clean_env) -> None:  # type: ignore[no-untyped-def]
    # The scope guard is Zammad-mode-only; oidc mode never touches /oauth/authorize.
    clean_env.setenv("ENVIRONMENT", "development")
    clean_env.setenv("PUBLIC_BASE_URL", "https://mcp.example.com")
    clean_env.setenv("ZAMMAD_URL", "https://zammad.example.com")
    clean_env.setenv("AUTH_MODE", "oidc")
    clean_env.setenv("AUTH_JWT_SIGNING_KEY", "f" * 64)
    clean_env.setenv("OIDC_DISCOVERY_URL", "https://idp.example.com/.well-known/openid-configuration")
    clean_env.setenv("OIDC_CLIENT_ID", "cid")
    clean_env.setenv("OIDC_CLIENT_SECRET", "secret")
    clean_env.setenv("ZAMMAD_API_TOKEN", "pat")
    clean_env.setenv("ZAMMAD_OAUTH_SCOPES", "read write")
    assert Settings().auth_mode is AuthMode.OIDC


# ── on-behalf-of guard: a static PAT must not shadow per-user tokens ─────────


def test_zammad_mode_rejects_static_api_token(base_zammad_env) -> None:  # type: ignore[no-untyped-def]
    # The profile's per_user_token resolver would fall back to this token and
    # silently run tool calls as its owner instead of the calling user.
    base_zammad_env.setenv("ZAMMAD_API_TOKEN", "pat-that-would-shadow-obo")
    with pytest.raises(ValueError, match="ZAMMAD_API_TOKEN must be empty"):
        Settings()


def test_zammad_mode_static_token_allowed_with_explicit_opt_in(base_zammad_env) -> None:  # type: ignore[no-untyped-def]
    base_zammad_env.setenv("ZAMMAD_API_TOKEN", "pat")
    base_zammad_env.setenv("MCP_ALLOW_STATIC_FALLBACK", "true")
    settings = Settings()
    assert settings.mcp_allow_static_fallback is True


def test_zammad_mode_blank_api_token_is_fine(base_zammad_env) -> None:  # type: ignore[no-untyped-def]
    # Compose files pass ZAMMAD_API_TOKEN through unconditionally as "".
    base_zammad_env.setenv("ZAMMAD_API_TOKEN", "   ")
    assert Settings().auth_mode is AuthMode.ZAMMAD


# ── role allowlist (now inherited from the bg-mcpcore base; enforced by the
#    declarative access_control gate, parsed by the base's CSV validator) ───────


def test_allowed_roles_csv_parsing(base_none_env) -> None:  # type: ignore[no-untyped-def]
    base_none_env.setenv("MCP_ALLOWED_ROLES", "Admin, Agent ,Customer")
    settings = Settings()
    assert settings.mcp_allowed_roles == ["Admin", "Agent", "Customer"]


def test_allowed_roles_default(base_none_env) -> None:  # type: ignore[no-untyped-def]
    # Zammad overrides the base's empty default with its safer Agents+Admins gate.
    settings = Settings()
    assert settings.mcp_allowed_roles == ["Admin", "Agent"]


# ── dynamic client registration allowlist ────────────────────────────────────


def test_client_redirect_allowlist_is_open_by_default(base_zammad_env) -> None:  # type: ignore[no-untyped-def]
    """Dynamic registration exists because a server cannot know its clients.

    A default allowlist would pin the deployment to whichever agents were known
    when it was configured and refuse the next one at registration time —
    Claude, Copilot, Cursor and Continue all use different callbacks, and
    ChatGPT does not use DCR at all. Open by default is the interoperable
    choice; the consent screen, MCP_ALLOWED_ROLES and per-user Zammad tokens are
    what bound the damage.
    """
    assert Settings().mcp_allowed_client_redirect_uris == []


def test_client_redirect_allowlist_parses_a_csv(base_zammad_env) -> None:  # type: ignore[no-untyped-def]
    base_zammad_env.setenv(
        "MCP_ALLOWED_CLIENT_REDIRECT_URIS",
        "https://claude.ai/api/mcp/auth_callback, http://localhost:* ,",
    )
    assert Settings().mcp_allowed_client_redirect_uris == [
        "https://claude.ai/api/mcp/auth_callback",
        "http://localhost:*",
    ]


# ── attachment limits ────────────────────────────────────────────────────────


def test_attachment_limits_have_the_documented_defaults(base_none_env) -> None:  # type: ignore[no-untyped-def]
    settings = Settings()
    assert settings.zammad_attachment_max_transfer_bytes == 10 * 1024 * 1024
    assert settings.zammad_attachment_max_article_bytes == 25 * 1024 * 1024
    assert settings.zammad_attachment_max_text_bytes == 256 * 1024
    assert settings.zammad_attachment_max_blob_bytes == 2 * 1024 * 1024
    assert settings.zammad_attachment_upload_enabled is True


def test_one_transfer_limit_covers_both_directions(base_none_env) -> None:  # type: ignore[no-untyped-def]
    """Two limits could only ever disagree, and the state they disagreed into -
    attach 10 MiB, refuse to read it back at 5 MiB - is the one nobody wants."""
    base_none_env.setenv("ZAMMAD_ATTACHMENT_MAX_TRANSFER_BYTES", str(7 * 1024 * 1024))
    settings = Settings()
    assert settings.zammad_attachment_max_transfer_bytes == 7 * 1024 * 1024
    assert not hasattr(settings, "zammad_attachment_max_read_bytes")
    assert not hasattr(settings, "zammad_attachment_max_upload_bytes")


def test_article_limit_below_the_transfer_limit_is_rejected(base_none_env) -> None:  # type: ignore[no-untyped-def]
    base_none_env.setenv("ZAMMAD_ATTACHMENT_MAX_TRANSFER_BYTES", str(20 * 1024 * 1024))
    base_none_env.setenv("ZAMMAD_ATTACHMENT_MAX_ARTICLE_BYTES", str(5 * 1024 * 1024))
    with pytest.raises(ValueError, match="ZAMMAD_ATTACHMENT_MAX_ARTICLE_BYTES"):
        Settings()


def test_uploads_can_be_switched_off(base_none_env) -> None:  # type: ignore[no-untyped-def]
    base_none_env.setenv("ZAMMAD_ATTACHMENT_UPLOAD_ENABLED", "false")
    assert Settings().zammad_attachment_upload_enabled is False
