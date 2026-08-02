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
from zammad.tools import articles, tickets


@pytest.fixture
def ticket_tools() -> tuple[FastMCP, RecordingCtx]:
    mcp: FastMCP = FastMCP("tickets-test")
    ctx = RecordingCtx()
    tickets.register(mcp, ctx)
    articles.register(mcp, ctx)
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
