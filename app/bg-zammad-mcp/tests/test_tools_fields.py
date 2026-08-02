"""Tests for the field-discovery tools.

``GET /ticket_create`` is the only field-discovery route a plain agent may
call, and its payload is almost entirely UI plumbing: two of its four
``form_meta`` keys are structurally null on this route, and the useful part -
which fields exist, which are mandatory, which values are allowed - is buried
in the Core Workflow evaluation. These tests pin the trimming against a
fixture shaped like the real response, and pin the admin-only route's
client-side filtering.
"""

from __future__ import annotations

import json
import re
from typing import Any

from fastmcp import FastMCP

from tests.test_tools_inventory import EXPECTED_TOOLS, RecordingCtx
from zammad.tools import fields

MODULE_TOOLS = {"list_ticket_fields", "list_object_attributes"}
KNOWN_TOOLS = set(EXPECTED_TOOLS) | MODULE_TOOLS | {"update_tickets"}

# Shaped like a real GET /ticket_create response: the Core Workflow defaults
# cover every attribute on the screen (including the custom `cost_center`),
# `restrict_values` covers only relation-backed and workflow-restricted ones,
# and both `dependencies` and `configure_attributes` are null on this route.
CREATE_SCREEN: dict[str, Any] = {
    "assets": {"Group": {"1": {"id": 1, "name": "Users"}}},
    "form_meta": {
        "filter": {"type_id": []},
        "dependencies": None,
        "configure_attributes": None,
        "core_workflow": {
            "request_id": "default",
            "rerun_count": 0,
            "matched_workflows": [],
            "eval": [],
            "select": {},
            "fill_in": {},
            "flags": {},
            "visibility": {
                "title": "show",
                "group_id": "show",
                "cost_center": "show",
                "pending_time": "remove",
            },
            "mandatory": {
                "title": True,
                "group_id": True,
                "cost_center": False,
                "pending_time": False,
            },
            "readonly": {"title": False, "group_id": False, "cost_center": True},
            "restrict_values": {"group_id": ["1", "2"]},
        },
    },
}

OBJECT_ATTRIBUTES: list[dict[str, Any]] = [
    {"name": "cost_center", "object": "Ticket", "data_type": "input", "active": True},
    {"name": "legacy_ref", "object": "Ticket", "data_type": "input", "active": False},
    {"name": "vip", "object": "User", "data_type": "boolean", "active": True},
]


def _mcp(response: Any) -> tuple[FastMCP, RecordingCtx]:
    mcp: FastMCP = FastMCP("test-fields")
    ctx = RecordingCtx(response)
    fields.register(mcp, ctx)
    return mcp, ctx


async def _tools(mcp: FastMCP) -> dict[str, Any]:
    return {tool.name: tool for tool in await mcp.list_tools(run_middleware=False)}


async def _call(mcp: FastMCP, name: str, /, **kwargs: Any) -> Any:
    # positional-only: several tools have their own `name` parameter
    # (create_organization, for one), which would collide otherwise.
    """Run a tool and return what it produced.

    FastMCP only fills ``structured_content`` for object-shaped results; a bare
    JSON array (what the admin route returns) arrives as text content instead.
    """
    result = await (await _tools(mcp))[name].run(kwargs)
    if result.structured_content is not None:
        return result.structured_content
    return json.loads(result.content[0].text)


# ── inventory, counts, annotations ───────────────────────────────────────────


async def test_module_registers_exactly_its_declared_tools() -> None:
    mcp, _ = _mcp(CREATE_SCREEN)
    declared = fields.register(FastMCP("count-probe"), RecordingCtx())
    tools = await _tools(mcp)
    assert set(tools) == MODULE_TOOLS
    assert declared == len(tools)


async def test_both_tools_are_read_only() -> None:
    mcp, _ = _mcp(CREATE_SCREEN)
    for name, tool in (await _tools(mcp)).items():
        assert tool.annotations is not None, f"{name} has no annotations"
        assert tool.annotations.readOnlyHint is True
        assert tool.annotations.destructiveHint is False


async def test_descriptions_only_name_real_parameters() -> None:
    mcp, _ = _mcp(CREATE_SCREEN)
    for name, tool in (await _tools(mcp)).items():
        params = set((tool.parameters or {}).get("properties", {}))
        for token in re.findall(r"`([a-z][a-z0-9_]{2,})`", tool.description or ""):
            assert token in params or token in KNOWN_TOOLS, (
                f"{name}: description references `{token}`, which is neither a "
                "parameter of this tool nor another tool"
            )


# ── list_ticket_fields ───────────────────────────────────────────────────────


async def test_list_ticket_fields_hits_the_agent_accessible_route() -> None:
    mcp, ctx = _mcp(CREATE_SCREEN)
    await _call(mcp, "list_ticket_fields")
    assert ctx.last["method"] == "GET"
    assert ctx.last["path"] == "/ticket_create"


async def test_custom_attributes_and_required_flags_survive_the_trim() -> None:
    """The whole point: a model cannot otherwise learn `cost_center` exists."""
    mcp, _ = _mcp(CREATE_SCREEN)
    result = await _call(mcp, "list_ticket_fields")
    by_name = {field["name"]: field for field in result["fields"]}
    assert set(by_name) == {"title", "group_id", "cost_center"}
    assert result["count"] == 3
    assert by_name["title"]["required"] is True
    assert by_name["cost_center"]["required"] is False
    assert by_name["cost_center"]["readonly"] is True
    assert by_name["group_id"]["allowed_values"] == ["1", "2"]
    # No value list from Zammad means the key is absent, not an empty list -
    # "unknown" and "nothing is allowed" are very different instructions.
    assert "allowed_values" not in by_name["title"]


async def test_the_ui_blob_is_dropped() -> None:
    mcp, _ = _mcp(CREATE_SCREEN)
    result = await _call(mcp, "list_ticket_fields")
    assert set(result) == {"count", "fields"}


async def test_fields_removed_from_the_screen_are_omitted_by_default() -> None:
    mcp, _ = _mcp(CREATE_SCREEN)
    default = await _call(mcp, "list_ticket_fields")
    assert "pending_time" not in {field["name"] for field in default["fields"]}

    everything = await _call(mcp, "list_ticket_fields", include_hidden=True)
    hidden = {field["name"]: field for field in everything["fields"]}["pending_time"]
    assert hidden["visibility"] == "remove"


async def test_raw_returns_zammads_untrimmed_payload() -> None:
    mcp, _ = _mcp(CREATE_SCREEN)
    assert await _call(mcp, "list_ticket_fields", raw=True) == CREATE_SCREEN


async def test_an_unrecognised_payload_is_passed_through_not_reported_as_empty() -> None:
    """Answering "0 fields" for a shape we failed to parse would be a lie the
    caller cannot detect; handing back the payload is honest."""
    mcp, _ = _mcp({"form_meta": {"configure_attributes": None}})
    result = await _call(mcp, "list_ticket_fields")
    assert result == {"form_meta": {"configure_attributes": None}}


# ── list_object_attributes ───────────────────────────────────────────────────


async def test_list_object_attributes_hits_the_admin_route() -> None:
    mcp, ctx = _mcp(OBJECT_ATTRIBUTES)
    await _call(mcp, "list_object_attributes")
    assert ctx.last["method"] == "GET"
    assert ctx.last["path"] == "/object_manager_attributes"


async def test_object_and_active_filters_are_applied_client_side() -> None:
    """The endpoint takes no query parameters - it always renders list_full."""
    mcp, ctx = _mcp(OBJECT_ATTRIBUTES)
    result = await _call(mcp, "list_object_attributes", object_name="ticket")
    assert "params" not in ctx.last
    assert [row["name"] for row in result] == ["cost_center"]

    with_inactive = await _call(
        mcp, "list_object_attributes", object_name="Ticket", include_inactive=True
    )
    assert [row["name"] for row in with_inactive] == ["cost_center", "legacy_ref"]


async def test_unfiltered_call_spans_every_object() -> None:
    mcp, _ = _mcp(OBJECT_ATTRIBUTES)
    result = await _call(mcp, "list_object_attributes")
    assert [row["name"] for row in result] == ["cost_center", "vip"]


async def test_a_non_list_response_is_returned_untouched() -> None:
    """A 403 body would raise before this, but a proxy can still return junk."""
    mcp, _ = _mcp({"error": "Not authorized"})
    assert await _call(mcp, "list_object_attributes") == {"error": "Not authorized"}
