"""Request-shape tests for the ticket write path and the Zammad 7 additions.

The generic inventory suite proves every tool exists and is annotated correctly.
This file pins the details that are easy to get wrong and impossible to notice:
a query parameter that Zammad silently ignores, a payload key that lands under
the wrong name, or a precedence rule in Zammad's own controller that quietly
turns a request into a different one.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastmcp import FastMCP

from tests.test_tools_inventory import RecordingCtx
from zammad.tools import articles, bulk, tickets


@pytest.fixture
def ticket_tools() -> tuple[FastMCP, RecordingCtx]:
    mcp: FastMCP = FastMCP("tickets-test")
    ctx = RecordingCtx()
    tickets.register(mcp, ctx)
    articles.register(mcp, ctx)
    return mcp, ctx


@pytest.fixture
def write_tools() -> tuple[FastMCP, RecordingCtx]:
    """Every tool that can create a ticket article, in one server.

    The visibility vocabulary is only worth anything if it is the SAME across
    them, so the check has to span modules rather than stay inside one.
    """
    mcp: FastMCP = FastMCP("write-tools-test")
    ctx = RecordingCtx()
    tickets.register(mcp, ctx)
    articles.register(mcp, ctx)
    bulk.register(mcp, ctx)
    return mcp, ctx


async def _call(mcp: FastMCP, name: str, **kwargs: Any) -> None:
    tools = {t.name: t for t in await mcp.list_tools(run_middleware=False)}
    await tools[name].run(kwargs)


# ── get_ticket_full: the one-call triage path ────────────────────────────────


async def test_get_ticket_full_must_not_send_expand(ticket_tools) -> None:  # type: ignore[no-untyped-def]
    """Zammad's show action takes the FIRST of expand, full, all that is set.

    Sending expand=true alongside all=true - which every other tool in the
    module does by default - would win the precedence check and return a plain
    ticket with no articles, while still looking like a success.
    """
    mcp, ctx = ticket_tools
    await _call(mcp, "get_ticket_full", ticket_id=7)

    assert ctx.last["path"] == "/tickets/7"
    params = ctx.last["params"]
    assert params == {"all": "true"}, "get_ticket_full must send only all=true"


# ── pending states ───────────────────────────────────────────────────────────


async def test_update_ticket_can_set_a_pending_time(ticket_tools) -> None:  # type: ignore[no-untyped-def]
    """Pending states were unreachable: Zammad rejects them without a
    pending_time, and the tool had no way to send one."""
    mcp, ctx = ticket_tools
    await _call(
        mcp,
        "update_ticket",
        ticket_id=7,
        state="pending reminder",
        pending_time="2026-08-12T09:00:00Z",
    )
    payload = ctx.last["json"]
    assert payload["state"] == "pending reminder"
    assert payload["pending_time"] == "2026-08-12T09:00:00Z"


# ── custom Object-Manager attributes ─────────────────────────────────────────


async def test_extra_fields_reach_the_payload(ticket_tools) -> None:  # type: ignore[no-untyped-def]
    mcp, ctx = ticket_tools
    await _call(
        mcp,
        "update_ticket",
        ticket_id=7,
        extra_fields={"cost_centre": "4711", "contract_type": "premium"},
    )
    payload = ctx.last["json"]
    assert payload["cost_centre"] == "4711"
    assert payload["contract_type"] == "premium"


async def test_named_arguments_win_over_extra_fields(ticket_tools) -> None:  # type: ignore[no-untyped-def]
    """An explicit parameter is the stronger statement of intent, so the merge
    order must be deterministic rather than dict-insertion luck."""
    mcp, ctx = ticket_tools
    await _call(
        mcp,
        "update_ticket",
        ticket_id=7,
        title="the real title",
        extra_fields={"title": "smuggled"},
    )
    assert ctx.last["json"]["title"] == "the real title"


async def test_create_ticket_merges_extra_fields_the_same_way(ticket_tools) -> None:  # type: ignore[no-untyped-def]
    mcp, ctx = ticket_tools
    await _call(
        mcp,
        "create_ticket",
        title="real",
        group="Support",
        customer="c@example.com",
        article_body="body",
        extra_fields={"title": "smuggled", "cost_centre": "4711"},
    )
    payload = ctx.last["json"]
    assert payload["title"] == "real"
    assert payload["cost_centre"] == "4711"


# ── close-with-a-note in one request ─────────────────────────────────────────


async def test_update_ticket_can_attach_a_note_atomically(ticket_tools) -> None:  # type: ignore[no-untyped-def]
    mcp, ctx = ticket_tools
    await _call(mcp, "update_ticket", ticket_id=7, state="closed", article_body="resolved")
    payload = ctx.last["json"]
    assert payload["state"] == "closed"
    assert payload["article"] == {"body": "resolved", "type": "note", "internal": True}


async def test_update_ticket_still_rejects_an_empty_change(ticket_tools) -> None:  # type: ignore[no-untyped-def]
    mcp, _ = ticket_tools
    with pytest.raises(Exception, match="at least one field"):
        await _call(mcp, "update_ticket", ticket_id=7)


# ── Zammad 7 dedicated endpoints ─────────────────────────────────────────────


async def test_update_ticket_title_uses_the_dedicated_endpoint(ticket_tools) -> None:  # type: ignore[no-untyped-def]
    """Service::Ticket::ForcedUpdate bypasses Core Workflow restrictions that
    can silently block the same change made through the generic PUT."""
    mcp, ctx = ticket_tools
    await _call(mcp, "update_ticket_title", ticket_id=7, title="renamed")
    assert ctx.last["method"] == "PUT"
    assert ctx.last["path"] == "/tickets/7/update_title"
    assert ctx.last["json"] == {"title": "renamed"}


async def test_reassign_ticket_customer_uses_the_dedicated_endpoint(ticket_tools) -> None:  # type: ignore[no-untyped-def]
    mcp, ctx = ticket_tools
    await _call(mcp, "reassign_ticket_customer", ticket_id=7, customer_id=99, organization_id=3)
    assert ctx.last["path"] == "/tickets/7/update_customer"
    assert ctx.last["json"] == {"customer_id": 99, "organization_id": 3}


async def test_reassign_omits_organization_when_unchanged(ticket_tools) -> None:  # type: ignore[no-untyped-def]
    mcp, ctx = ticket_tools
    await _call(mcp, "reassign_ticket_customer", ticket_id=7, customer_id=99)
    assert ctx.last["json"] == {"customer_id": 99}


# ── index-independent search ─────────────────────────────────────────────────


async def test_condition_search_posts_a_structured_body(ticket_tools) -> None:  # type: ignore[no-untyped-def]
    """The whole point of this tool is that it does not need Elasticsearch, so
    the condition must travel as a JSON body rather than a flattened query."""
    mcp, ctx = ticket_tools
    condition = {"ticket.state_id": {"operator": "is", "value": [1, 2]}}
    await _call(mcp, "search_tickets_by_condition", condition=condition, page=2)

    assert ctx.last["method"] == "POST"
    assert ctx.last["path"] == "/tickets/search"
    payload = ctx.last["json"]
    assert payload["condition"] == condition
    assert payload["page"] == 2
    # A JSON body carries real booleans; only query params need the string form.
    assert payload["expand"] is True
    assert payload["with_total_count"] is True


async def test_condition_search_rejects_an_empty_condition(ticket_tools) -> None:  # type: ignore[no-untyped-def]
    mcp, _ = ticket_tools
    with pytest.raises(Exception, match="non-empty condition"):
        await _call(mcp, "search_tickets_by_condition", condition={})


async def test_count_tickets_asks_for_the_count_only(ticket_tools) -> None:  # type: ignore[no-untyped-def]
    mcp, ctx = ticket_tools
    await _call(mcp, "count_tickets", query="state.name:open")
    assert ctx.last["params"] == {"query": "state.name:open", "only_total_count": "true"}


# ── articles ─────────────────────────────────────────────────────────────────


async def test_get_article_plain_hits_the_plain_route(ticket_tools) -> None:  # type: ignore[no-untyped-def]
    mcp, ctx = ticket_tools
    await _call(mcp, "get_article_plain", article_id=12)
    assert ctx.last["method"] == "GET"
    assert ctx.last["path"] == "/ticket_article_plain/12"


# ── one vocabulary for article visibility ────────────────────────────────────


async def test_every_article_writing_tool_speaks_the_same_visibility(write_tools) -> None:  # type: ignore[no-untyped-def]
    """`internal` used to be a bare boolean whose default differed per tool —
    False on create_ticket, True in bulk, and hardcoded True on update_ticket
    with no parameter at all. That is the trap articles.py was split in two to
    close, reintroduced through a side door."""
    mcp, _ = write_tools
    tools = {t.name: t for t in await mcp.list_tools(run_middleware=False)}
    for name in ("create_ticket", "update_ticket", "update_tickets"):
        props = (tools[name].parameters or {}).get("properties", {})
        assert "article_visibility" in props, f"{name} does not use the shared vocabulary"
        assert "article_internal" not in props, f"{name} still exposes the old boolean"


@pytest.mark.parametrize(
    ("visibility", "expected_internal"),
    [("customer_visible", False), ("internal", True)],
)
async def test_visibility_maps_to_zammads_internal_flag(  # type: ignore[no-untyped-def]
    ticket_tools, visibility: str, expected_internal: bool
) -> None:
    mcp, ctx = ticket_tools
    await _call(mcp, "update_ticket", ticket_id=7, article_body="x", article_visibility=visibility)
    assert ctx.last["json"]["article"]["internal"] is expected_internal


async def test_an_unknown_visibility_is_refused(ticket_tools) -> None:  # type: ignore[no-untyped-def]
    mcp, ctx = ticket_tools
    with pytest.raises(Exception, match="article_visibility must be one of"):
        await _call(mcp, "update_ticket", ticket_id=7, article_body="x", article_visibility="secret")
    assert ctx.calls == []


# ── one rule for identifiers: name OR id, never silently both ────────────────


@pytest.mark.parametrize("field", ["state", "priority"])
async def test_name_and_id_together_are_refused(ticket_tools, field: str) -> None:  # type: ignore[no-untyped-def]
    """Zammad accepts both and silently applies one, so the caller gets a
    success for a change that was half discarded."""
    mcp, ctx = ticket_tools
    with pytest.raises(Exception, match="not both"):
        await _call(mcp, "update_ticket", ticket_id=7, **{field: "x", f"{field}_id": 3})
    assert ctx.calls == []


async def test_create_ticket_accepts_associations_by_id_too(ticket_tools) -> None:  # type: ignore[no-untyped-def]
    """create_ticket used to take group/customer by name ONLY while update_ticket
    took them by id only — the same five associations, split the opposite way."""
    mcp, ctx = ticket_tools
    await _call(
        mcp,
        "create_ticket",
        title="t",
        group="ignored",
        customer="ignored@example.com",
        article_body="b",
        group_id=3,
        customer_id=9,
    )
    payload = ctx.last["json"]
    assert payload["group_id"] == 3
    assert payload["customer_id"] == 9
    # Only the chosen form travels, so Zammad never has to break a tie.
    assert "group" not in payload
    assert "customer" not in payload


# ── tags: replacing is not adding ────────────────────────────────────────────


async def test_replace_tags_is_named_after_what_it_does(ticket_tools) -> None:  # type: ignore[no-untyped-def]
    """`tags` sounded additive and wiped the list. `add_tag` exists for adding;
    the destructive one now says so in its name."""
    mcp, _ = ticket_tools
    tools = {t.name: t for t in await mcp.list_tools(run_middleware=False)}
    update = (tools["update_ticket"].parameters or {}).get("properties", {})
    assert "replace_tags" in update
    assert "tags" not in update
    # The alternative is named where the choice is actually made — in the
    # parameter's own description, not buried in the tool blurb.
    assert "add_tag" in update["replace_tags"]["description"]
    assert "REPLACES" in update["replace_tags"]["description"]
