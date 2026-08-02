"""Request-shape tests for the tag and notification tools.

Both modules pass their arguments as QUERY parameters rather than a JSON body,
which is where Zammad's tag API differs from everything else in this server —
worth pinning, because a body would be accepted with a 200 and ignored.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastmcp import FastMCP

from tests.test_tools_inventory import RecordingCtx
from zammad.tools import notifications, tags


@pytest.fixture
def tag_tools() -> tuple[FastMCP, RecordingCtx]:
    mcp: FastMCP = FastMCP("tags-test")
    ctx = RecordingCtx()
    tags.register(mcp, ctx)
    return mcp, ctx


@pytest.fixture
def notification_tools() -> tuple[FastMCP, RecordingCtx]:
    mcp: FastMCP = FastMCP("notifications-test")
    ctx = RecordingCtx()
    notifications.register(mcp, ctx)
    return mcp, ctx


async def _call(mcp: FastMCP, name: str, /, **kwargs: Any) -> Any:
    # positional-only: several tools have their own `name` parameter
    # (create_organization, for one), which would collide otherwise.
    tools = {t.name: t for t in await mcp.list_tools(run_middleware=False)}
    return await tools[name].run(kwargs)


# ── tags ─────────────────────────────────────────────────────────────────────


async def test_list_object_tags_defaults_to_a_ticket(tag_tools) -> None:  # type: ignore[no-untyped-def]
    mcp, ctx = tag_tools
    await _call(mcp, "list_object_tags", object_id=7)
    assert ctx.last["path"] == "/tags"
    assert ctx.last["params"] == {"object": "Ticket", "o_id": 7}


async def test_object_type_can_be_overridden(tag_tools) -> None:  # type: ignore[no-untyped-def]
    mcp, ctx = tag_tools
    await _call(mcp, "list_object_tags", object_id=7, object_type="KnowledgeBase::Answer")
    assert ctx.last["params"]["object"] == "KnowledgeBase::Answer"


async def test_list_all_tags_hits_the_admin_route(tag_tools) -> None:  # type: ignore[no-untyped-def]
    """/tag_list needs admin.tag and 403s for a plain agent — the description
    says so and points at search_tags, so pin the path it actually calls."""
    mcp, ctx = tag_tools
    await _call(mcp, "list_all_tags")
    assert ctx.last["path"] == "/tag_list"


async def test_search_tags_is_the_agent_safe_route(tag_tools) -> None:  # type: ignore[no-untyped-def]
    mcp, ctx = tag_tools
    await _call(mcp, "search_tags", term="urg")
    assert ctx.last["path"] == "/tag_search"
    assert ctx.last["params"] == {"term": "urg"}


async def test_add_tag_uses_query_params_not_a_body(tag_tools) -> None:  # type: ignore[no-untyped-def]
    mcp, ctx = tag_tools
    result = await _call(mcp, "add_tag", object_id=7, tag="urgent")

    assert ctx.last["method"] == "POST"
    assert ctx.last["path"] == "/tags/add"
    assert ctx.last["params"] == {"object": "Ticket", "o_id": 7, "item": "urgent"}
    assert "json" not in ctx.last, "Zammad's tag API reads query params, not a body"
    # Zammad answers with a bare `true`; the tool returns something a model can
    # actually report back to a human.
    assert result.structured_content == {
        "added": True,
        "object_type": "Ticket",
        "object_id": 7,
        "tag": "urgent",
    }


async def test_remove_tag_uses_delete_with_query_params(tag_tools) -> None:  # type: ignore[no-untyped-def]
    mcp, ctx = tag_tools
    result = await _call(mcp, "remove_tag", object_id=7, tag="urgent")

    assert ctx.last["method"] == "DELETE"
    assert ctx.last["path"] == "/tags/remove"
    assert ctx.last["params"]["item"] == "urgent"
    assert result.structured_content["removed"] is True


# ── notifications and mentions ───────────────────────────────────────────────


async def test_list_my_notifications(notification_tools) -> None:  # type: ignore[no-untyped-def]
    mcp, ctx = notification_tools
    await _call(mcp, "list_my_notifications")
    assert ctx.last == {"method": "GET", "path": "/online_notifications"}


async def test_mark_all_notifications_read(notification_tools) -> None:  # type: ignore[no-untyped-def]
    mcp, ctx = notification_tools
    result = await _call(mcp, "mark_all_notifications_read")
    assert ctx.last["method"] == "POST"
    assert ctx.last["path"] == "/online_notifications/mark_all_as_read"
    assert result.structured_content == {"marked_all_read": True}


@pytest.mark.parametrize("seen", [True, False])
async def test_mark_notification_read_can_also_unread(notification_tools, seen: bool) -> None:  # type: ignore[no-untyped-def]
    mcp, ctx = notification_tools
    await _call(mcp, "mark_notification_read", notification_id=3, seen=seen)
    assert ctx.last["method"] == "PUT"
    assert ctx.last["path"] == "/online_notifications/3"
    # A real boolean in the body, not the lowercase string used in query params.
    assert ctx.last["json"] == {"seen": seen}


async def test_list_ticket_subscribers_filters_by_ticket(notification_tools) -> None:  # type: ignore[no-untyped-def]
    mcp, ctx = notification_tools
    await _call(mcp, "list_ticket_subscribers", ticket_id=7)
    assert ctx.last["path"] == "/mentions"
    assert ctx.last["params"] == {"mentionable_type": "Ticket", "mentionable_id": 7}


async def test_subscribe_to_ticket_creates_a_mention(notification_tools) -> None:  # type: ignore[no-untyped-def]
    """A subscription is a mention record — which is also why unsubscribing
    (in the history module) has to look one up before it can delete it."""
    mcp, ctx = notification_tools
    await _call(mcp, "subscribe_to_ticket", ticket_id=7)
    assert ctx.last["method"] == "POST"
    assert ctx.last["path"] == "/mentions"
    assert ctx.last["json"] == {"mentionable_type": "Ticket", "mentionable_id": 7}
