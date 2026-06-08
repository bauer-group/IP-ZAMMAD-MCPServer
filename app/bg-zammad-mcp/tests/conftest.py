"""
Pytest fixtures shared across the suite.

The conftest deliberately resets the Settings singleton and any leaked env
vars before each test - Pydantic Settings is a process-global, so a test
that loads a partial env can poison later tests if not isolated.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

# Make `src/` importable so `from config import Settings` works regardless of
# whether the suite is run via `pytest` from the package root or from the repo
# root.
SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))


# A Fernet key (44-char url-safe base64 of 32 zero bytes - good enough for
# tests, never use in production).
_TEST_FERNET_KEY = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="

# A 64-char hex string (32 random bytes) - structurally valid JWT signing key.
_TEST_JWT_KEY = "f" * 64


@pytest.fixture(autouse=True)
def _reset_settings_singleton() -> Iterator[None]:
    """Clear the bg-mcpcore per-class settings cache so each test parses fresh env."""
    from bg_mcpcore.settings import reset_settings_cache

    reset_settings_cache()
    yield
    reset_settings_cache()


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """Strip every env var the Settings model reads so each test starts blank."""
    keys = [
        # General
        "ENVIRONMENT",
        "PUBLIC_BASE_URL",
        "LOG_FORMAT",
        "LOG_LEVEL",
        # Zammad backend
        "ZAMMAD_URL",
        "ZAMMAD_API_TOKEN",
        "ZAMMAD_HTTP_TIMEOUT",
        "ZAMMAD_VERIFY_TLS",
        # MCP transport
        "MCP_TRANSPORT",
        "MCP_HOST",
        "MCP_PORT",
        "MCP_DISPLAY_NAME",
        "MCP_ICON_URL",
        "MCP_WEBSITE_URL",
        # Auth
        "AUTH_MODE",
        "AUTH_JWT_SIGNING_KEY",
        "AUTH_REDIS_URL",
        "AUTH_STORAGE_ENCRYPTION_KEY",
        "AUTH_DISK_STORAGE_PATH",
        # Zammad OAuth
        "ZAMMAD_OAUTH_CLIENT_ID",
        "ZAMMAD_OAUTH_CLIENT_SECRET",
        "ZAMMAD_OAUTH_SCOPES",
        "ZAMMAD_OAUTH_AUTHORIZE_PATH",
        "ZAMMAD_OAUTH_TOKEN_PATH",
        "ZAMMAD_USERINFO_PATH",
        # OIDC
        "OIDC_DISCOVERY_URL",
        "OIDC_ISSUER",
        "OIDC_AUTH_URI",
        "OIDC_TOKEN_URI",
        "OIDC_JWKS_URI",
        "OIDC_USERINFO_URI",
        "OIDC_CLIENT_ID",
        "OIDC_CLIENT_SECRET",
        "OIDC_SCOPES",
        "OIDC_USERNAME_CLAIM",
        # Roles
        "MCP_ALLOWED_ROLES",
        "MCP_ROLE_CHECK_AUDIT_ONLY",
        "MCP_ROLE_CACHE_TTL_SECONDS",
        # Rate limit
        "RATE_LIMITER_ENABLED",
        "RATE_LIMITER_MAX_REQUESTS_PER_SECOND",
        "RATE_LIMITER_BURST_CAPACITY",
        "RATE_LIMITER_GLOBAL",
        "RATE_LIMITER_TRUSTED_PROXY_HOPS",
        # Sentry
        "SENTRY_DSN",
        "SENTRY_ENVIRONMENT",
        "SENTRY_TRACES_SAMPLE_RATE",
    ]
    for key in keys:
        monkeypatch.delenv(key, raising=False)
    # Pydantic Settings looks at the current working directory for .env -
    # cd into a temp dir to avoid picking up the operator's local .env.
    monkeypatch.chdir(os.path.dirname(os.path.abspath(__file__)))
    return monkeypatch


@pytest.fixture
def base_zammad_env(clean_env: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """A minimal AUTH_MODE=zammad env that passes validation."""
    env = clean_env
    env.setenv("ENVIRONMENT", "development")
    env.setenv("PUBLIC_BASE_URL", "https://mcp.example.com")
    env.setenv("ZAMMAD_URL", "https://zammad.example.com")
    env.setenv("AUTH_MODE", "zammad")
    env.setenv("AUTH_JWT_SIGNING_KEY", _TEST_JWT_KEY)
    env.setenv("ZAMMAD_OAUTH_CLIENT_ID", "test-client-id")
    env.setenv("ZAMMAD_OAUTH_CLIENT_SECRET", "test-client-secret")
    return env


@pytest.fixture
def base_none_env(clean_env: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """A minimal AUTH_MODE=none + dev env that passes validation."""
    env = clean_env
    env.setenv("ENVIRONMENT", "development")
    env.setenv("PUBLIC_BASE_URL", "http://localhost:8000")
    env.setenv("ZAMMAD_URL", "https://zammad.example.com")
    env.setenv("AUTH_MODE", "none")
    env.setenv("ZAMMAD_API_TOKEN", "dev-api-token")
    return env


@pytest.fixture
def fernet_key() -> str:
    return _TEST_FERNET_KEY


@pytest.fixture
def jwt_key() -> str:
    return _TEST_JWT_KEY
