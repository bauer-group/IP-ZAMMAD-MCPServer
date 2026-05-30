"""
Role allowlist middleware tests.

We test the pure logic (role-name extraction + intersection) directly so the
test doesn't have to stand up the full FastMCP context. The middleware itself
is exercised in two minimal cases - "pass when allowed" and "deny when not".
"""

from __future__ import annotations

import pytest

from auth.role_middleware import (
    RoleAllowlistMiddleware,
    RoleNotAllowedError,
    _extract_role_names,
    _intersects,
)


# ── _extract_role_names ────────────────────────────────────────────────────


def test_extract_roles_from_simple_string_list():
    claims = {"roles": ["Admin", "Agent"]}
    assert _extract_role_names(claims) == {"admin", "agent"}


def test_extract_roles_from_object_list():
    claims = {"roles": [{"name": "Admin"}, {"name": "Agent"}]}
    assert _extract_role_names(claims) == {"admin", "agent"}


def test_extract_roles_falls_back_to_zammad_user():
    claims = {"zammad_user": {"roles": ["Customer"]}}
    assert _extract_role_names(claims) == {"customer"}


def test_extract_roles_empty_when_missing():
    assert _extract_role_names({}) == set()


def test_extract_roles_handles_mixed_list():
    claims = {"roles": ["Admin", {"name": "Agent"}, None, ""]}
    assert _extract_role_names(claims) == {"admin", "agent"}


def test_extract_roles_strips_whitespace():
    claims = {"roles": [" Admin ", "AGENT  "]}
    assert _extract_role_names(claims) == {"admin", "agent"}


# ── _intersects ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "user,allowed,expected",
    [
        ({"admin"}, {"admin", "agent"}, True),
        ({"customer"}, {"admin", "agent"}, False),
        (set(), {"admin"}, False),
        ({"admin", "customer"}, {"agent"}, False),
        ({"admin", "customer"}, {"agent", "customer"}, True),
        ({"admin"}, set(), False),
    ],
)
def test_intersects(user, allowed, expected):
    assert _intersects(user, allowed) is expected


# ── Middleware behaviour ───────────────────────────────────────────────────


class _StubCallNext:
    def __init__(self) -> None:
        self.called = False

    async def __call__(self, _context):
        self.called = True
        return "passed-through"


class _StubContext:
    """Minimal MiddlewareContext-shaped object for the middleware to inspect."""

    def __init__(self, method: str = "tools/call") -> None:
        self.method = method


class _StubToken:
    def __init__(self, claims: dict) -> None:
        self.claims = claims


@pytest.fixture
def patch_access_token(monkeypatch):
    """Patch `get_access_token` to return a token-shaped object on demand."""
    holder: dict[str, _StubToken | None] = {"token": None}

    def _set(token: _StubToken | None) -> None:
        holder["token"] = token

    def _fake_get_access_token():
        return holder["token"]

    import auth.role_middleware as mod

    monkeypatch.setattr(mod, "get_access_token", _fake_get_access_token)
    return _set


async def test_middleware_passes_unauthenticated_request(patch_access_token):
    patch_access_token(None)
    mw = RoleAllowlistMiddleware(allowed_roles={"admin"})
    nxt = _StubCallNext()
    result = await mw.on_request(_StubContext(), nxt)
    assert result == "passed-through"
    assert nxt.called


async def test_middleware_passes_when_role_matches(patch_access_token):
    patch_access_token(_StubToken({"sub": "u1", "roles": ["Admin"]}))
    mw = RoleAllowlistMiddleware(allowed_roles={"admin", "agent"})
    nxt = _StubCallNext()
    result = await mw.on_request(_StubContext(), nxt)
    assert result == "passed-through"
    assert nxt.called


async def test_middleware_rejects_when_role_missing(patch_access_token):
    patch_access_token(_StubToken({"sub": "u2", "roles": ["Customer"]}))
    mw = RoleAllowlistMiddleware(allowed_roles={"admin", "agent"})
    nxt = _StubCallNext()
    with pytest.raises(RoleNotAllowedError):
        await mw.on_request(_StubContext(), nxt)
    assert not nxt.called


async def test_middleware_audit_only_logs_but_passes(patch_access_token):
    patch_access_token(_StubToken({"sub": "u3", "roles": ["Customer"]}))
    mw = RoleAllowlistMiddleware(allowed_roles={"admin"}, audit_only=True)
    nxt = _StubCallNext()
    result = await mw.on_request(_StubContext(), nxt)
    assert result == "passed-through"
    assert nxt.called


async def test_middleware_empty_allowlist_passes_through(patch_access_token):
    """No allowlist configured = any authenticated user is fine."""
    patch_access_token(_StubToken({"sub": "u4", "roles": ["SomeRandomRole"]}))
    mw = RoleAllowlistMiddleware(allowed_roles=set())
    nxt = _StubCallNext()
    result = await mw.on_request(_StubContext(), nxt)
    assert result == "passed-through"
    assert nxt.called


async def test_middleware_lowercases_allowlist_at_construction():
    mw = RoleAllowlistMiddleware(allowed_roles={"Admin", "AGENT"})
    assert mw._allowed == {"admin", "agent"}
