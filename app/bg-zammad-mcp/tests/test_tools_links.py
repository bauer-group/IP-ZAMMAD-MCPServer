"""Request-shape and guard-rail tests for the merge / link / context tools.

Two classes of defect are pinned here, because both are invisible at runtime:

* the ID-vs-NUMBER asymmetry. ticket_merge takes source by ID and target by
  NUMBER, links/add takes source by NUMBER and target by ID, links/remove takes
  both by ID. Zammad answers a mismatched identifier with a silent no-op, so
  only a test can prove the values land in the right slot.
* the success statuses on failed writes. ticket_merge returns HTTP 200 with
  result='failed', links/add returns HTTP 201 with a null id. Both must surface
  as errors, or the model reports work that never happened.

The recording context comes from the inventory suite so these tests exercise
the same ``ToolContext`` shape the real server implements.
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from fastmcp import FastMCP

from tests.test_tools_inventory import EXPECTED_TOOLS, RecordingCtx
from zammad.tools import links

TOOL_NAMES = {
    "merge_tickets",
    "find_related_tickets",
    "list_customer_tickets",
    "list_ticket_links",
    "link_tickets",
    "unlink_tickets",
}

DESTRUCTIVE = {"merge_tickets", "unlink_tickets"}


# Every number-resolving tool answers its first request with this.
TICKET_11 = {"id": 11, "number": "67001"}


def _build(
    response: Any = None, *, responses: list[Any] | None = None
) -> tuple[FastMCP, RecordingCtx]:
    mcp: FastMCP = FastMCP("test-links")
    ctx = RecordingCtx(response, responses=responses)
    links.register(mcp, ctx)
    return mcp, ctx


async def _tools(mcp: FastMCP) -> dict[str, Any]:
    return {tool.name: tool for tool in await mcp.list_tools(run_middleware=False)}


async def _call(mcp: FastMCP, name: str, /, **kwargs: Any) -> Any:
    # positional-only: several tools have their own `name` parameter
    # (create_organization, for one), which would collide otherwise.
    return await (await _tools(mcp))[name].run(kwargs)


# ── inventory / annotations / descriptions ───────────────────────────────────


async def test_registers_exactly_what_it_declares() -> None:
    mcp: FastMCP = FastMCP("test-links")
    declared = links.register(mcp, RecordingCtx())
    assert declared == len(await mcp.list_tools(run_middleware=False)) == len(TOOL_NAMES)
    assert set(await _tools(mcp)) == TOOL_NAMES


async def test_only_the_overwriting_tools_are_destructive() -> None:
    mcp, _ = _build()
    for name, tool in (await _tools(mcp)).items():
        assert tool.annotations is not None, f"{name} has no annotations"
        assert tool.annotations.destructiveHint is (name in DESTRUCTIVE), (
            f"{name}: link_tickets is additive, merge_tickets and unlink_tickets are not"
        )


async def test_write_tools_are_not_annotated_read_only() -> None:
    mcp, _ = _build()
    tools = await _tools(mcp)
    for name in ("merge_tickets", "link_tickets", "unlink_tickets"):
        assert tools[name].annotations is not None
        assert tools[name].annotations.readOnlyHint is False


async def test_read_tools_are_annotated_read_only() -> None:
    mcp, _ = _build()
    tools = await _tools(mcp)
    for name in ("find_related_tickets", "list_customer_tickets", "list_ticket_links"):
        assert tools[name].annotations is not None
        assert tools[name].annotations.readOnlyHint is True
        assert tools[name].annotations.destructiveHint is False


async def test_descriptions_only_name_real_parameters_or_tools() -> None:
    mcp, _ = _build()
    problems: list[str] = []
    known = set(EXPECTED_TOOLS) | TOOL_NAMES
    for name, tool in (await _tools(mcp)).items():
        params = set((tool.parameters or {}).get("properties", {}))
        for token in re.findall(r"`([a-z][a-z0-9_]{2,})`", tool.description or ""):
            if token not in params and token not in known:
                problems.append(f"{name}: description references `{token}`")
    assert not problems, "\n".join(problems)


# ── read tools ───────────────────────────────────────────────────────────────


async def test_find_related_tickets_hits_the_related_route() -> None:
    mcp, ctx = _build({"ticket_ids_by_customer": [], "ticket_ids_recent_viewed": []})
    await _call(mcp, "find_related_tickets", ticket_id=7)
    assert ctx.last["method"] == "GET"
    assert ctx.last["path"] == "/ticket_related/7"


async def test_list_customer_tickets_sends_the_customer_id_as_a_query_param() -> None:
    mcp, ctx = _build({"ticket_ids_open": [], "ticket_ids_closed": []})
    await _call(mcp, "list_customer_tickets", customer_id=4)
    assert ctx.last["method"] == "GET"
    assert ctx.last["path"] == "/ticket_customer"
    assert ctx.last["params"] == {"customer_id": 4}


async def test_list_ticket_links_scopes_the_query_to_tickets() -> None:
    mcp, ctx = _build({"links": [], "assets": {}})
    await _call(mcp, "list_ticket_links", ticket_id=7)
    assert ctx.last["method"] == "GET"
    assert ctx.last["path"] == "/links"
    assert ctx.last["params"] == {"link_object": "Ticket", "link_object_value": 7}


# ── merge ────────────────────────────────────────────────────────────────────


async def test_merge_puts_the_source_id_and_target_number_in_that_order() -> None:
    """The whole point of the parameter names: /ticket_merge/{id}/{number}."""
    mcp, ctx = _build(responses=[TICKET_11, {"result": "success", "target_ticket": {}, "source_ticket": {}}])
    await _call(mcp, "merge_tickets", source_ticket_id=12, target_ticket_id=11)
    assert ctx.last["method"] == "PUT"
    assert ctx.last["path"] == "/ticket_merge/12/67001"


async def test_merge_raises_on_zammads_http_200_failure() -> None:
    """A lookup miss is reported as success; unchecked, the model would too."""
    mcp, _ = _build(responses=[TICKET_11, {"result": "failed", "message": "not found"}])
    with pytest.raises(Exception, match="Zammad refused the merge"):
        await _call(mcp, "merge_tickets", source_ticket_id=12, target_ticket_id=11)


async def test_merge_resolves_the_target_number_from_its_id() -> None:
    """Zammad's route wants the target as a NUMBER while the source is an ID.

    That asymmetry used to be published as two differently-typed arguments; a
    model had to remember which neighbouring tool wanted which. Now both are
    IDs and the tool does the lookup, so the trap is unreachable rather than
    documented.
    """
    mcp, ctx = _build(responses=[TICKET_11, {"result": "success"}])
    await _call(mcp, "merge_tickets", source_ticket_id=10, target_ticket_id=11)

    lookup, merge = ctx.calls
    assert (lookup["method"], lookup["path"]) == ("GET", "/tickets/11")
    assert merge["path"] == "/ticket_merge/10/67001"


async def test_merge_returns_the_body_unchanged_on_success() -> None:
    body = {"result": "success", "target_ticket": {"id": 3}, "source_ticket": {"id": 12}}
    mcp, _ = _build(responses=[TICKET_11, body])
    result = await _call(mcp, "merge_tickets", source_ticket_id=12, target_ticket_id=11)
    assert result.structured_content == body


# ── link / unlink ────────────────────────────────────────────────────────────


async def test_link_sends_the_source_as_a_number_and_the_target_as_an_id() -> None:
    mcp, ctx = _build(responses=[TICKET_11, {"id": 55}])
    await _call(
        mcp, "link_tickets", source_ticket_id=11, target_ticket_id=12, link_type="parent"
    )
    assert ctx.last["method"] == "POST"
    assert ctx.last["path"] == "/links/add"
    assert ctx.last["json"] == {
        "link_type": "parent",
        "link_object_source": "Ticket",
        # NUMBER for the source ...
        "link_object_source_number": "67001",
        "link_object_target": "Ticket",
        # ... ID for the target. Swapping these is a silent no-op in Zammad.
        "link_object_target_value": 12,
    }


async def test_link_defaults_to_a_normal_link() -> None:
    mcp, ctx = _build(responses=[TICKET_11, {"id": 55}])
    await _call(mcp, "link_tickets", source_ticket_id=11, target_ticket_id=12)
    assert ctx.last["json"]["link_type"] == "normal"


async def test_link_raises_when_zammad_returns_201_with_a_null_id() -> None:
    """Link.create hands back an unsaved record when the link already exists,
    and the controller renders it as 201 - the null id is the only tell."""
    mcp, _ = _build(responses=[TICKET_11, {"id": None, "link_type_id": 1}])
    with pytest.raises(Exception, match="already linked"):
        await _call(mcp, "link_tickets", source_ticket_id=11, target_ticket_id=12)


async def test_unlink_identifies_both_tickets_by_id() -> None:
    # One request only (unlink resolves no number), and Zammad answers with the
    # LIST of deleted rows - which is what removed_count counts.
    mcp, ctx = _build([{"id": 55}])
    result = await _call(
        mcp, "unlink_tickets", source_ticket_id=9, target_ticket_id=12, link_type="child"
    )
    assert ctx.last["method"] == "DELETE"
    assert ctx.last["path"] == "/links/remove"
    assert ctx.last["json"] == {
        "link_type": "child",
        "link_object_source": "Ticket",
        # Zammad wants a VALUE here and a number on links/add; both tools take
        # an ID at the call site and the difference stays in this module.
        "link_object_source_value": 9,
        "link_object_target": "Ticket",
        "link_object_target_value": 12,
    }
    assert result.structured_content["removed_count"] == 1


async def test_unlink_reports_zero_when_zammad_deleted_nothing() -> None:
    mcp, _ = _build([])
    result = await _call(mcp, "unlink_tickets", source_ticket_id=9, target_ticket_id=12)
    assert result.structured_content["removed_count"] == 0
    assert result.structured_content["link_type"] == "normal"


@pytest.mark.parametrize("tool_name", ["link_tickets", "unlink_tickets"])
async def test_an_unknown_link_type_is_rejected_before_zammad_invents_it(tool_name: str) -> None:
    """Link.link_type_get CREATES an unknown type, permanently, without error."""
    mcp, ctx = _build()
    kwargs: dict[str, Any] = (
        {"source_ticket_id": 11, "target_ticket_id": 12}
        if tool_name == "link_tickets"
        else {"source_ticket_id": 9, "target_ticket_id": 12}
    )
    with pytest.raises(Exception, match="link_type must be one of"):
        await _call(mcp, tool_name, link_type="duplicate", **kwargs)
    assert ctx.calls == []
