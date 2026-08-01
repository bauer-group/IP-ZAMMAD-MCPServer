"""Tests for the Zammad OAuth2 userinfo token verifier.

``_ZammadUserInfoVerifier.verify_token`` is the inbound trust boundary: it
validates an opaque Zammad bearer token by calling ``/api/v1/users/me`` and maps
the response into a FastMCP ``AccessToken`` (or ``None`` = rejected). These tests
pin every branch with a mocked Zammad so a regression in the rejection logic
cannot silently let an invalid token through, and so the forwarded
``upstream_access_token`` claim (consumed by the ``per_user_token`` resolver)
stays wired.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import respx

from auth.zammad_oauth import _ZammadUserInfoVerifier
from config import ZAMMAD_OAUTH_SCOPE

USERINFO_URL = "https://zammad.example.com/api/v1/users/me"

_VALID_USER = {
    "id": 42,
    "login": "agent@example.com",
    "email": "agent@example.com",
    "firstname": "Aya",
    "lastname": "Agent",
    "role_ids": [2],
    "roles": ["Agent"],
    "active": True,
    "organization_id": 7,
}


# Claim names verify_token puts on the AccessToken. `upstream_access_token` is
# the one the profile's per_user_token resolver reads — see the pin test below.
_VALID_CLAIM_KEYS = {
    "sub",
    "preferred_username",
    "email",
    "name",
    "role_ids",
    "roles",
    "upstream_access_token",
    "zammad_user",
}


def _verifier() -> _ZammadUserInfoVerifier:
    return _ZammadUserInfoVerifier(
        userinfo_url=USERINFO_URL,
        timeout=5.0,
        verify_tls=True,
        required_scopes=[ZAMMAD_OAUTH_SCOPE],
    )


# ── success path ─────────────────────────────────────────────────────────────


@respx.mock
async def test_verify_token_success_maps_claims() -> None:
    respx.get(USERINFO_URL).mock(return_value=httpx.Response(200, json=_VALID_USER))

    token = await _verifier().verify_token("opaque-abc")

    assert token is not None
    assert token.token == "opaque-abc"
    assert token.client_id == "42"
    assert token.scopes == [ZAMMAD_OAUTH_SCOPE]
    claims = token.claims
    assert claims["sub"] == "42"
    assert claims["preferred_username"] == "agent@example.com"
    assert claims["roles"] == ["Agent"]
    assert claims["role_ids"] == [2]
    # The upstream token is carried forward for the per_user_token resolver - if
    # this claim name drifts, on-behalf-of outbound auth silently breaks.
    assert claims["upstream_access_token"] == "opaque-abc"


# ── rejection paths (every one must return None, never raise) ─────────────────


@respx.mock
async def test_verify_token_401_rejected() -> None:
    respx.get(USERINFO_URL).mock(return_value=httpx.Response(401))
    assert await _verifier().verify_token("bad") is None


@respx.mock
async def test_verify_token_server_error_rejected() -> None:
    respx.get(USERINFO_URL).mock(return_value=httpx.Response(503))
    assert await _verifier().verify_token("t") is None


@respx.mock
async def test_verify_token_unexpected_status_rejected() -> None:
    respx.get(USERINFO_URL).mock(return_value=httpx.Response(403))
    assert await _verifier().verify_token("t") is None


@respx.mock
async def test_verify_token_non_json_rejected() -> None:
    respx.get(USERINFO_URL).mock(
        return_value=httpx.Response(200, text="<html>not json</html>")
    )
    assert await _verifier().verify_token("t") is None


@respx.mock
async def test_verify_token_missing_id_rejected() -> None:
    respx.get(USERINFO_URL).mock(
        return_value=httpx.Response(200, json={"login": "nobody@example.com"})
    )
    assert await _verifier().verify_token("t") is None


@respx.mock
async def test_verify_token_network_error_rejected() -> None:
    respx.get(USERINFO_URL).mock(side_effect=httpx.ConnectError("refused"))
    assert await _verifier().verify_token("t") is None


# ── metadata exposure ─────────────────────────────────────────────────────────


@respx.mock
async def test_profile_obo_claim_matches_the_claim_the_verifier_emits() -> None:
    """The profile and the verifier must agree on the on-behalf-of claim name.

    ``per_user_token`` resolves the caller's Zammad token from the claim names
    listed in ``profiles/zammad.json``. If the verifier renames its claim (or
    the profile drops it), the resolver finds nothing and — depending on
    configuration — either fails closed or falls back to a shared token.
    Neither failure is loud, so pin the contract from both sides at once:
    read the profile, then check it against the claims a real verification
    actually produces.
    """
    profile = json.loads(
        (Path(__file__).resolve().parent.parent / "src" / "profiles" / "zammad.json").read_text(
            encoding="utf-8"
        )
    )
    declared = profile["auth"]["outbound"]["claims"]

    respx.get(USERINFO_URL).mock(return_value=httpx.Response(200, json=_VALID_USER))
    token = await _verifier().verify_token("opaque-abc")
    assert token is not None

    emitted = set(token.claims)
    assert emitted == _VALID_CLAIM_KEYS, "verify_token's claim set changed"
    # At least one declared claim must actually be emitted, and the first one —
    # the cheapest lookup path in the resolver — must be the one carrying the token.
    assert set(declared) & emitted, f"none of the profile claims {declared} are emitted"
    assert token.claims[declared[0]] == "opaque-abc"


def test_required_scopes_property_returns_a_copy() -> None:
    verifier = _verifier()
    scopes = verifier.required_scopes
    assert scopes == [ZAMMAD_OAUTH_SCOPE]
    scopes.append("admin")  # mutating the returned list must not leak back
    assert verifier.required_scopes == [ZAMMAD_OAUTH_SCOPE]
