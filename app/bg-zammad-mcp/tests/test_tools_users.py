"""Request-shape tests for the user tools.

These were the last write paths with no coverage at all. The payload builders
are the part worth pinning: every field is conditional, so a misplaced branch
silently sends a smaller payload than intended and Zammad accepts it — the
update "succeeds" and simply does not change the field the caller asked about.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastmcp import FastMCP

from tests.test_tools_inventory import RecordingCtx
from zammad.tools import users


@pytest.fixture
def user_tools() -> tuple[FastMCP, RecordingCtx]:
    mcp: FastMCP = FastMCP("users-test")
    ctx = RecordingCtx()
    users.register(mcp, ctx)
    return mcp, ctx


async def _call(mcp: FastMCP, name: str, /, **kwargs: Any) -> Any:
    # positional-only: several tools have their own `name` parameter
    # (create_organization, for one), which would collide otherwise.
    tools = {t.name: t for t in await mcp.list_tools(run_middleware=False)}
    return await tools[name].run(kwargs)


# ── create_user ──────────────────────────────────────────────────────────────


async def test_create_user_sends_only_the_supplied_fields(user_tools) -> None:  # type: ignore[no-untyped-def]
    mcp, ctx = user_tools
    await _call(mcp, "create_user", email="new@example.com", firstname="Nia")

    payload = ctx.last["json"]
    assert ctx.last["method"] == "POST"
    assert ctx.last["path"] == "/users"
    assert payload == {"active": True, "email": "new@example.com", "firstname": "Nia"}


async def test_create_user_splits_the_role_csv(user_tools) -> None:  # type: ignore[no-untyped-def]
    """Zammad wants a list; the tool takes a CSV because an LLM writes CSV."""
    mcp, ctx = user_tools
    await _call(mcp, "create_user", email="a@b.c", roles=" Agent , Admin ,")
    assert ctx.last["json"]["roles"] == ["Agent", "Admin"]


async def test_create_user_passes_every_optional_field_through(user_tools) -> None:  # type: ignore[no-untyped-def]
    mcp, ctx = user_tools
    await _call(
        mcp,
        "create_user",
        email="a@b.c",
        firstname="Nia",
        lastname="Nowak",
        login="nnowak",
        phone="+49 123",
        organization_id=7,
        active=False,
    )
    payload = ctx.last["json"]
    assert payload["login"] == "nnowak"
    assert payload["phone"] == "+49 123"
    assert payload["organization_id"] == 7
    assert payload["active"] is False


async def test_create_user_needs_something_to_identify_the_person(user_tools) -> None:  # type: ignore[no-untyped-def]
    """A user with neither an address nor a name is not a user Zammad can use."""
    mcp, ctx = user_tools
    with pytest.raises(Exception, match="requires at least"):
        await _call(mcp, "create_user", phone="+49 123")
    assert ctx.calls == [], "no request should be made for an invalid call"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"email": "a@b.c"},
        {"login": "nnowak"},
        {"firstname": "Nia", "lastname": "Nowak"},
    ],
)
async def test_each_accepted_identity_combination(user_tools, kwargs: dict[str, Any]) -> None:  # type: ignore[no-untyped-def]
    mcp, ctx = user_tools
    await _call(mcp, "create_user", **kwargs)
    assert ctx.last["path"] == "/users"


async def test_firstname_alone_is_not_enough(user_tools) -> None:  # type: ignore[no-untyped-def]
    mcp, _ = user_tools
    with pytest.raises(Exception, match="requires at least"):
        await _call(mcp, "create_user", firstname="Nia")


# ── update_user ──────────────────────────────────────────────────────────────


async def test_update_user_sends_only_what_changed(user_tools) -> None:  # type: ignore[no-untyped-def]
    mcp, ctx = user_tools
    await _call(mcp, "update_user", user_id=4, phone="+49 999")
    assert ctx.last["method"] == "PUT"
    assert ctx.last["path"] == "/users/4"
    assert ctx.last["json"] == {"phone": "+49 999"}


async def test_update_user_can_deactivate(user_tools) -> None:  # type: ignore[no-untyped-def]
    """active=False must survive the "only if not None" filter — a plain
    truthiness check here would silently drop the whole point of the call."""
    mcp, ctx = user_tools
    await _call(mcp, "update_user", user_id=4, active=False)
    assert ctx.last["json"] == {"active": False}


async def test_update_user_sets_the_full_field_set(user_tools) -> None:  # type: ignore[no-untyped-def]
    mcp, ctx = user_tools
    await _call(
        mcp,
        "update_user",
        user_id=4,
        email="new@example.com",
        firstname="Nia",
        lastname="Nowak",
        phone="+49 1",
        organization_id=9,
        roles="Agent",
        active=True,
    )
    payload = ctx.last["json"]
    assert payload["email"] == "new@example.com"
    assert payload["organization_id"] == 9
    assert payload["roles"] == ["Agent"]
    assert payload["active"] is True


async def test_update_user_without_fields_raises(user_tools) -> None:  # type: ignore[no-untyped-def]
    mcp, ctx = user_tools
    with pytest.raises(Exception, match="at least one field"):
        await _call(mcp, "update_user", user_id=4)
    assert ctx.calls == []


# ── reads ────────────────────────────────────────────────────────────────────


async def test_get_me_expands_roles(user_tools) -> None:  # type: ignore[no-untyped-def]
    """The role names only appear with expand=true, and they are what the
    access gate matches against."""
    mcp, ctx = user_tools
    await _call(mcp, "get_me")
    assert ctx.last["path"] == "/users/me"
    assert ctx.last["params"]["expand"] == "true"


async def test_search_users_honours_an_explicit_field_whitelist(user_tools) -> None:  # type: ignore[no-untyped-def]
    """End-to-end through the tool, not just the projection helper: this is the
    only place that proves `fields` is actually wired into the return path."""
    import json

    mcp, ctx = user_tools
    ctx._response = [{"id": 1, "login": "a", "note": "drop me"}]
    result = await _call(mcp, "search_users", query="a", fields="id,login")

    # A list return has no structuredContent — FastMCP puts the JSON in content.
    assert json.loads(result.content[0].text) == [{"id": 1, "login": "a"}]
