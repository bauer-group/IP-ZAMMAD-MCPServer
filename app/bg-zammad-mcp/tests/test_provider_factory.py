"""
Provider factory dispatch tests.

We monkeypatch each concrete provider builder so the test stays pure - no
network, no FastMCP construction. The dispatch logic (AUTH_MODE -> which
builder) is what we're verifying.
"""

from __future__ import annotations

import pytest

from auth.provider_factory import build_auth_provider
from config import AuthMode, Environment, Settings


@pytest.fixture
def fake_builders(monkeypatch):
    """Replace each builder with a sentinel-returning stub."""
    calls: dict[str, int] = {"zammad": 0, "oidc": 0}

    def fake_zammad(_settings):
        calls["zammad"] += 1
        return ("zammad-provider", _settings)

    def fake_oidc(_settings):
        calls["oidc"] += 1
        return ("oidc-provider", _settings)

    import auth.zammad_oauth as zo
    import auth.generic_oidc as go

    monkeypatch.setattr(zo, "build_zammad_oauth_provider", fake_zammad)
    monkeypatch.setattr(go, "build_generic_oidc_provider", fake_oidc)
    return calls


def test_none_mode_in_development_returns_none(base_none_env, fake_builders):
    settings = Settings()
    assert settings.environment is Environment.DEVELOPMENT
    assert settings.auth_mode is AuthMode.NONE
    assert build_auth_provider(settings) is None
    assert fake_builders == {"zammad": 0, "oidc": 0}


def test_zammad_mode_dispatches_to_zammad_builder(base_zammad_env, fake_builders):
    settings = Settings()
    result = build_auth_provider(settings)
    assert result[0] == "zammad-provider"
    assert fake_builders["zammad"] == 1
    assert fake_builders["oidc"] == 0


def test_oidc_mode_dispatches_to_oidc_builder(clean_env, fake_builders, jwt_key):
    clean_env.setenv("ENVIRONMENT", "development")
    clean_env.setenv("PUBLIC_BASE_URL", "https://mcp.example.com")
    clean_env.setenv("ZAMMAD_URL", "https://zammad.example.com")
    clean_env.setenv("AUTH_MODE", "oidc")
    clean_env.setenv("AUTH_JWT_SIGNING_KEY", jwt_key)
    clean_env.setenv("ZAMMAD_API_TOKEN", "static")
    clean_env.setenv(
        "OIDC_DISCOVERY_URL", "https://idp.example.com/.well-known/openid-configuration"
    )
    clean_env.setenv("OIDC_CLIENT_ID", "x")
    clean_env.setenv("OIDC_CLIENT_SECRET", "y")
    settings = Settings()
    result = build_auth_provider(settings)
    assert result[0] == "oidc-provider"
    assert fake_builders["oidc"] == 1
    assert fake_builders["zammad"] == 0
