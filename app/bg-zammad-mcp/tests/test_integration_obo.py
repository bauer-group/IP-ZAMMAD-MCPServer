"""Integration proof that the server acts as the OAuth user, against a live Zammad.

Everything else in this suite mocks the network, which can prove the server
*sends* the right request but not that Zammad *treats* it as the right person.
The on-behalf-of chain is the security property this server exists for, so it
gets a test that runs the real components against a real instance:

    verify_token(user's Zammad token)   ->  AccessToken claims
        -> PerUserTokenResolver          ->  Authorization header
            -> outbound HTTP             ->  Zammad answers AS THAT USER

The decisive assertion is not "a call succeeded" — a shared admin token would
also succeed. It is that two different OAuth users get **different answers to
the same call**, matching their own Zammad permissions.

Skipped unless the environment points at an instance:

    ZAMMAD_TEST_URL=http://localhost:8080
    ZAMMAD_TEST_TOKEN_RESTRICTED=<OAuth token of an agent with limited groups>
    ZAMMAD_TEST_TOKEN_ADMIN=<OAuth token of an agent who sees more>

Tokens come from Zammad's real authorization-code flow; see CONTRIBUTING.md.
"""

from __future__ import annotations

import os
from typing import Any

import httpx
import pytest

from auth.zammad_oauth import _ZammadUserInfoVerifier
from config import ZAMMAD_OAUTH_SCOPE

ZAMMAD_URL = os.getenv("ZAMMAD_TEST_URL", "")
TOKEN_RESTRICTED = os.getenv("ZAMMAD_TEST_TOKEN_RESTRICTED", "")
TOKEN_ADMIN = os.getenv("ZAMMAD_TEST_TOKEN_ADMIN", "")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (ZAMMAD_URL and TOKEN_RESTRICTED and TOKEN_ADMIN),
        reason="set ZAMMAD_TEST_URL + ZAMMAD_TEST_TOKEN_RESTRICTED/_ADMIN to run",
    ),
]


def _verifier() -> _ZammadUserInfoVerifier:
    return _ZammadUserInfoVerifier(
        userinfo_url=f"{ZAMMAD_URL}/api/v1/users/me",
        timeout=15.0,
        verify_tls=False,
        required_scopes=[ZAMMAD_OAUTH_SCOPE],
    )


def _profile_claims() -> list[str]:
    """The claim names the shipped profile tells the resolver to look at."""
    import json
    from pathlib import Path

    profile = json.loads(
        (Path(__file__).resolve().parent.parent / "src" / "profiles" / "zammad.json").read_text(
            encoding="utf-8"
        )
    )
    return list(profile["auth"]["outbound"]["claims"])


async def _call_as(access_token: Any, method: str, path: str, **kwargs: Any) -> httpx.Response:
    """Perform an outbound call the way the per_user_token resolver would.

    The token is looked up through the claim names the PROFILE declares, not
    taken from the raw variable, so a break anywhere in that contract — a
    renamed claim, a profile edit — surfaces here instead of silently falling
    back to a shared credential in production.
    """
    forwarded = next(
        (
            value
            for name in _profile_claims()
            if isinstance(value := access_token.claims.get(name), str) and value
        ),
        None,
    )
    assert forwarded, "no claim carried the upstream token — OBO would fall back or fail"

    async with httpx.AsyncClient(base_url=f"{ZAMMAD_URL}/api/v1", timeout=20.0) as client:
        return await client.request(
            method, path, headers={"Authorization": f"Bearer {forwarded}"}, **kwargs
        )


# ── the chain, link by link ──────────────────────────────────────────────────


async def test_a_real_oauth_token_verifies_and_identifies_its_owner() -> None:
    token = await _verifier().verify_token(TOKEN_RESTRICTED)
    assert token is not None, "Zammad rejected a token its own OAuth flow just issued"
    assert token.claims["preferred_username"] == "restricted@example.com"
    # expand=true is what turns numeric role_ids into the names the access gate
    # matches against; without it the gate silently stops enforcing.
    assert token.claims["roles"], "no role names — the access gate would be a no-op"


async def test_the_upstream_token_is_carried_on_the_claim_the_profile_reads() -> None:
    token = await _verifier().verify_token(TOKEN_RESTRICTED)
    assert token is not None
    assert token.claims["upstream_access_token"] == TOKEN_RESTRICTED


async def test_a_garbage_token_is_rejected_by_the_live_instance() -> None:
    assert await _verifier().verify_token("definitely-not-a-token") is None


# ── the property that matters ────────────────────────────────────────────────


async def test_the_outbound_call_runs_as_the_token_owner() -> None:
    """Zammad must see the call as coming from the person who logged in."""
    token = await _verifier().verify_token(TOKEN_RESTRICTED)
    assert token is not None

    response = await _call_as(token, "GET", "/users/me")
    assert response.status_code == 200
    assert response.json()["email"] == "restricted@example.com"


async def test_two_oauth_users_get_different_answers_to_the_same_call() -> None:
    """The decisive one.

    A shared service token would make both callers see exactly the same thing,
    no matter who logged in. Two OAuth users whose Zammad group access differs
    must therefore get different ticket lists — and specifically, neither may
    see tickets belonging to a group they have no access to.

    The assertion is deliberately about *difference and non-overlap of the
    restricted user's view*, not about one set containing the other: whether the
    views nest or are disjoint depends on how the groups happen to be assigned,
    and pinning that would test the fixture rather than the property.
    """
    restricted = await _verifier().verify_token(TOKEN_RESTRICTED)
    admin = await _verifier().verify_token(TOKEN_ADMIN)
    assert restricted is not None and admin is not None
    assert restricted.claims["sub"] != admin.claims["sub"]

    r_tickets = (await _call_as(restricted, "GET", "/tickets", params={"per_page": 100})).json()
    a_tickets = (await _call_as(admin, "GET", "/tickets", params={"per_page": 100})).json()

    r_ids = {t["id"] for t in r_tickets}
    a_ids = {t["id"] for t in a_tickets}

    assert r_ids != a_ids, (
        "both OAuth users saw the same tickets — the outbound call is NOT running "
        "in the caller's context (a shared token would look exactly like this)"
    )
    assert r_ids, "the restricted agent saw nothing at all; the fixture proves nothing"
    assert a_ids, "the wider agent saw nothing at all; the fixture proves nothing"
    # The security-relevant half: there is at least one ticket the wider agent
    # can see and the restricted one cannot, and it is genuinely hidden from them.
    hidden = a_ids - r_ids
    assert hidden, "the wider agent sees nothing the restricted one cannot"
    assert r_ids.isdisjoint(hidden)


async def test_a_restricted_user_is_refused_where_they_lack_permission() -> None:
    """Zammad's own authorization still applies — the MCP grants nothing extra."""
    restricted = await _verifier().verify_token(TOKEN_RESTRICTED)
    assert restricted is not None

    # /tag_list requires admin.tag; a plain agent must not get it.
    response = await _call_as(restricted, "GET", "/tag_list")
    assert response.status_code in (401, 403), (
        f"a plain agent reached an admin route (HTTP {response.status_code}) — "
        "the call is not running with that user's rights"
    )
