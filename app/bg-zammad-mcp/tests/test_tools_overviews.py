"""Tests for the ticket-overview tools.

The interesting behaviour is not the two request shapes - it is what
``list_queue_tickets`` does with the answer. Zammad hands back an index of bare
``{id, updated_at}`` stubs plus a separate assets dictionary, and answers an
unknown overview slug with HTTP 200 and an empty envelope. Both are joins and
judgements an LLM cannot be trusted with, so both are pinned here.
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from fastmcp import FastMCP

from tests.test_tools_inventory import RecordingCtx
from zammad.tools import overviews

EXPECTED_TOOLS = ["list_my_queues", "list_queue_tickets"]

# Tools from other modules that these descriptions may legitimately point at.
KNOWN_OTHER_TOOLS = {"search_tickets", "get_ticket"}

# One realistic ?view= response: two tickets, out of a queue of 137, with the
# real objects living in the assets blob under string keys (Ruby integer keys
# survive JSON serialisation as strings).
VIEW_PAYLOAD: dict[str, Any] = {
    "assets": {
        "Ticket": {
            "42": {"id": 42, "title": "Printer smokes", "state_id": 2},
            "7": {"id": 7, "title": "VPN down", "state_id": 1},
        },
        "User": {"3": {"id": 3, "login": "aya"}},
    },
    "index": {
        "overview": {"id": 5, "name": "My Assigned Tickets", "view": "my_assigned"},
        "tickets": [{"id": 42, "updated_at": "2026-07-01T09:00:00Z"},
                    {"id": 7, "updated_at": "2026-06-30T09:00:00Z"}],
        "count": 137,
    },
}


def _build(response: Any = None) -> tuple[FastMCP, RecordingCtx]:
    mcp: FastMCP = FastMCP("test-overviews")
    ctx = RecordingCtx(response)
    overviews.register(mcp, ctx)
    return mcp, ctx


async def _tools(mcp: FastMCP) -> dict[str, Any]:
    return {tool.name: tool for tool in await mcp.list_tools(run_middleware=False)}


async def _run(mcp: FastMCP, name: str, **kwargs: Any) -> Any:
    result = await (await _tools(mcp))[name].run(kwargs)
    return result.structured_content


# ── inventory + annotations ──────────────────────────────────────────────────


async def test_registers_exactly_the_declared_tools() -> None:
    mcp: FastMCP = FastMCP("test-overviews")
    declared = overviews.register(mcp, RecordingCtx())
    assert sorted(await _tools(mcp)) == EXPECTED_TOOLS
    assert declared == len(EXPECTED_TOOLS)


async def test_both_tools_are_read_only() -> None:
    mcp, _ = _build()
    for name, tool in (await _tools(mcp)).items():
        assert tool.annotations is not None, f"{name} has no annotations"
        assert tool.annotations.readOnlyHint is True
        assert tool.annotations.destructiveHint is False


async def test_descriptions_only_name_real_parameters() -> None:
    """A backticked identifier the schema does not publish makes the model
    invent an argument that `additionalProperties: false` then rejects."""
    mcp, _ = _build()
    problems: list[str] = []
    for name, tool in (await _tools(mcp)).items():
        params = set((tool.parameters or {}).get("properties", {}))
        for token in re.findall(r"`([a-z][a-z0-9_]{2,})`", tool.description or ""):
            if token in params or token in EXPECTED_TOOLS or token in KNOWN_OTHER_TOOLS:
                continue
            problems.append(f"{name}: description references `{token}`, not a parameter")
    assert not problems, "\n".join(problems)


# ── list_my_queues ───────────────────────────────────────────────────────────


async def test_list_my_queues_hits_the_plural_route_without_a_view() -> None:
    """The count listing and the ticket listing are the same Rails action;
    omitting ``view`` is what selects the cheap one. Sending an empty view
    would fall through to the assets branch instead."""
    mcp, ctx = _build([{"id": 5, "name": "My Assigned", "link": "my_assigned", "count": 3}])
    await _run(mcp, "list_my_queues")
    assert ctx.last["method"] == "GET"
    assert ctx.last["path"] == "/ticket_overviews"
    assert "params" not in ctx.last


# ── list_queue_tickets ───────────────────────────────────────────────────────


async def test_list_queue_tickets_sends_the_view_slug() -> None:
    mcp, ctx = _build(VIEW_PAYLOAD)
    await _run(mcp, "list_queue_tickets", view="my_assigned")
    assert ctx.last["method"] == "GET"
    assert ctx.last["path"] == "/ticket_overviews"
    assert ctx.last["params"] == {"view": "my_assigned"}


async def test_list_queue_tickets_flattens_assets_in_index_order() -> None:
    mcp, _ = _build(VIEW_PAYLOAD)
    result = await _run(mcp, "list_queue_tickets", view="my_assigned")
    assert [t["id"] for t in result["items"]] == [42, 7]
    assert result["items"][0]["title"] == "Printer smokes"
    assert result["total_count"] == 137
    assert result["returned"] == 2
    assert result["overview"]["view"] == "my_assigned"


async def test_list_queue_tickets_accepts_integer_asset_keys() -> None:
    """JSON turns Ruby's integer asset keys into strings, but a mock, a proxy or
    a future serializer need not - the join must survive either."""
    payload = {
        "assets": {"Ticket": {42: {"id": 42, "title": "Printer smokes"}}},
        "index": {"overview": {"id": 5}, "tickets": [{"id": 42}], "count": 1},
    }
    mcp, _ = _build(payload)
    result = await _run(mcp, "list_queue_tickets", view="my_assigned")
    assert result["items"][0]["title"] == "Printer smokes"


async def test_ticket_missing_from_assets_degrades_to_its_stub() -> None:
    """Zammad omits assets for records the caller may not see. Dropping such a
    ticket would make the list silently disagree with total_count."""
    payload = {
        "assets": {"Ticket": {}},
        "index": {"overview": {"id": 5}, "tickets": [{"id": 99, "updated_at": "x"}], "count": 1},
    }
    mcp, _ = _build(payload)
    result = await _run(mcp, "list_queue_tickets", view="my_assigned")
    assert result["items"] == [{"id": 99, "updated_at": "x"}]


async def test_unknown_view_raises_instead_of_looking_like_an_empty_queue() -> None:
    """Zammad answers an unmatched slug with HTTP 200 and ``{assets:{}, index:{}}``.
    Passing that through would tell the user their queue is empty."""
    mcp, _ = _build({"assets": {}, "index": {}})
    with pytest.raises(Exception, match="link slug"):
        await _run(mcp, "list_queue_tickets", view="my_assigend")


async def test_pagination_is_local_and_never_sent_upstream() -> None:
    """``Ticket::Overviews.index`` takes no page/per_page - the controller
    forwards nothing - so a ``page`` query param would be a silent no-op."""
    mcp, ctx = _build(VIEW_PAYLOAD)
    result = await _run(mcp, "list_queue_tickets", view="my_assigned", page=2, per_page=1)
    assert ctx.last["params"] == {"view": "my_assigned"}
    assert [t["id"] for t in result["items"]] == [7]
    assert result["page"] == 2
    assert result["per_page"] == 1
    # The true queue size must survive slicing, or a page reads as the whole queue.
    assert result["total_count"] == 137


async def test_page_beyond_the_end_returns_no_tickets_not_an_error() -> None:
    mcp, _ = _build(VIEW_PAYLOAD)
    result = await _run(mcp, "list_queue_tickets", view="my_assigned", page=9, per_page=25)
    assert result["items"] == []
    assert result["total_count"] == 137
