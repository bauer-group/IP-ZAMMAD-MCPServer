"""
Upstream-token resolver tests.

The resolver checks (1) embedded claims, (2) a passed-in storage, (3) a
storage pulled from the FastMCP context. We patch `get_access_token` and
test each branch in isolation.
"""

from __future__ import annotations

import pytest

from auth.upstream_token import (
    MissingUpstreamToken,
    _extract_access_token,
    get_zammad_user_token,
)


class _StubToken:
    def __init__(self, claims: dict) -> None:
        self.claims = claims


class _StubStorage:
    """Toy AsyncKeyValue impl - just an in-memory dict + .get()."""

    def __init__(self, data: dict | None = None) -> None:
        self._data = data or {}

    async def get(self, key):  # noqa: ANN001
        return self._data.get(key)


@pytest.fixture
def patch_access_token(monkeypatch):
    holder: dict = {"token": None}

    def _set(token):
        holder["token"] = token

    def _fake():
        return holder["token"]

    # The resolver imports get_access_token lazily from
    # fastmcp.server.dependencies inside the function body (see
    # upstream_token.py), so we patch it at its real source - patching
    # auth.upstream_token would miss the late import.
    monkeypatch.setattr("fastmcp.server.dependencies.get_access_token", _fake)
    return _set


# ── _extract_access_token ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, None),
        ("plain-token", "plain-token"),
        ("", None),
        ({"access_token": "abc"}, "abc"),
        ({"upstream_access_token": "abc"}, "abc"),
        ({"token": "abc"}, "abc"),
        ({"value": "abc"}, "abc"),
        ({"unrelated": "xyz"}, None),
        (42, None),
    ],
)
def test_extract_access_token(value, expected):
    assert _extract_access_token(value) == expected


# ── Resolver branches ──────────────────────────────────────────────────────


async def test_raises_when_no_access_token(patch_access_token):
    patch_access_token(None)
    with pytest.raises(MissingUpstreamToken):
        await get_zammad_user_token()


async def test_returns_embedded_upstream_access_token(patch_access_token):
    patch_access_token(_StubToken({"sub": "u1", "upstream_access_token": "zmd-1"}))
    token = await get_zammad_user_token()
    assert token == "zmd-1"


async def test_returns_embedded_upstream_token_alias(patch_access_token):
    patch_access_token(_StubToken({"sub": "u1", "upstream_token": "zmd-2"}))
    token = await get_zammad_user_token()
    assert token == "zmd-2"


async def test_returns_embedded_zammad_access_token(patch_access_token):
    patch_access_token(_StubToken({"sub": "u1", "zammad_access_token": "zmd-3"}))
    token = await get_zammad_user_token()
    assert token == "zmd-3"


async def test_falls_back_to_storage_lookup_by_jti(patch_access_token):
    patch_access_token(_StubToken({"sub": "u1", "jti": "jti-abc"}))
    storage = _StubStorage({"upstream_tokens/jti-abc": {"access_token": "stored-token"}})
    token = await get_zammad_user_token(client_storage=storage)
    assert token == "stored-token"


async def test_falls_back_to_storage_lookup_by_sub(patch_access_token):
    patch_access_token(_StubToken({"sub": "u1"}))  # no jti
    storage = _StubStorage({"upstream_tokens/u1": {"access_token": "by-sub"}})
    token = await get_zammad_user_token(client_storage=storage)
    assert token == "by-sub"


async def test_raises_when_storage_has_no_match(patch_access_token):
    patch_access_token(_StubToken({"sub": "u1", "jti": "jti-x"}))
    storage = _StubStorage({})
    with pytest.raises(MissingUpstreamToken):
        await get_zammad_user_token(client_storage=storage)


async def test_storage_exception_does_not_crash(patch_access_token):
    """Backend errors during probe are treated as 'not found here'."""
    patch_access_token(_StubToken({"sub": "u1"}))

    class _BrokenStorage:
        async def get(self, key):  # noqa: ANN001
            raise RuntimeError("backend exploded")

    with pytest.raises(MissingUpstreamToken):
        await get_zammad_user_token(client_storage=_BrokenStorage())
