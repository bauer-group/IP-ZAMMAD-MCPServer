"""Request-shape tests for the checklist tools.

Checklists are the one part of the surface where the tool does more than pass a
call through: ``get_ticket_checklist`` walks ticket -> checklist_id ->
/checklists/{id}, and both read tools reduce Zammad's asset envelope to the
items. Those two behaviours are what these tests pin down, along with the usual
verb/path/payload contract and every ToolError branch.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastmcp import FastMCP

from tests.test_tools_inventory import RecordingCtx
from zammad.tools import checklists

EXPECTED_TOOLS = sorted(
    [
        "get_ticket_checklist",
        "create_ticket_checklist",
        "list_checklist_templates",
        "add_checklist_items",
        "set_checklist_item",
    ]
)

DESTRUCTIVE_TOOLS = {"set_checklist_item"}

# Backticked words in a description that are values or Zammad-side names rather
# than parameters of the tool they appear in.
_PROSE_ALLOWLIST = {"true", "false", "checked", "text"}


class SequenceCtx(RecordingCtx):
    """A RecordingCtx that answers a scripted sequence of responses.

    ``get_ticket_checklist`` makes two different calls, so a single canned
    response cannot exercise it.
    """

    def __init__(self, responses: list[Any]) -> None:
        super().__init__()
        self._queue = list(responses)

    async def request(self, method: str, path: str, **kwargs: Any) -> Any:
        self.calls.append({"method": method, "path": path, **kwargs})
        return self._queue.pop(0) if self._queue else {}


def _build(ctx: RecordingCtx) -> FastMCP:
    mcp: FastMCP = FastMCP("test-checklists")
    checklists.register(mcp, ctx)
    return mcp


async def _tools(mcp: FastMCP) -> dict[str, Any]:
    return {tool.name: tool for tool in await mcp.list_tools(run_middleware=False)}


async def _call(mcp: FastMCP, name: str, **kwargs: Any) -> Any:
    return await (await _tools(mcp))[name].run(kwargs)


@pytest.fixture
def mcp_and_ctx() -> tuple[FastMCP, RecordingCtx]:
    ctx = RecordingCtx()
    return _build(ctx), ctx


# ── inventory, descriptions, annotations ─────────────────────────────────────


async def test_tool_inventory(mcp_and_ctx) -> None:  # type: ignore[no-untyped-def]
    mcp, _ = mcp_and_ctx
    assert sorted(await _tools(mcp)) == EXPECTED_TOOLS


async def test_declared_count_matches_registrations() -> None:
    mcp: FastMCP = FastMCP("count-checklists")
    declared = checklists.register(mcp, RecordingCtx())
    assert declared == len(await mcp.list_tools(run_middleware=False))


async def test_descriptions_only_name_real_parameters(mcp_and_ctx) -> None:  # type: ignore[no-untyped-def]
    """A description that names a parameter the schema does not publish sends
    the model into a call `additionalProperties: false` rejects."""
    import re

    mcp, _ = mcp_and_ctx
    problems: list[str] = []
    for name, tool in (await _tools(mcp)).items():
        params = set((tool.parameters or {}).get("properties", {}))
        for token in re.findall(r"`([a-z][a-z0-9_]{2,})`", tool.description or ""):
            if token in params or token in _PROSE_ALLOWLIST or token in EXPECTED_TOOLS:
                continue
            problems.append(f"{name}: description references `{token}`, not a parameter")
    assert not problems, "\n".join(problems)


async def test_annotations(mcp_and_ctx) -> None:  # type: ignore[no-untyped-def]
    mcp, _ = mcp_and_ctx
    for name, tool in (await _tools(mcp)).items():
        assert tool.annotations is not None, f"{name} has no annotations"
        read_only = name.startswith(("get_", "list_"))
        assert tool.annotations.readOnlyHint is read_only, f"{name}: wrong readOnlyHint"
        assert tool.annotations.destructiveHint is (name in DESTRUCTIVE_TOOLS), (
            f"{name}: only tools that overwrite existing state are destructive"
        )


# ── get_ticket_checklist: the two-step walk + flattening ─────────────────────

_FULL_CHECKLIST = {
    "id": 6,
    "assets": {
        "Checklist": {
            "6": {
                "id": 6,
                "name": "Return order",
                # Zammad's sorted order, ids as strings; item_ids as integers.
                "sorted_item_ids": ["20", "18", "19"],
                "item_ids": [18, 19, 20],
            }
        },
        "ChecklistItem": {
            "18": {"id": 18, "text": "Prepare shipment", "checked": True, "ticket_id": None},
            "19": {"id": 19, "text": "Inform customer", "checked": False, "ticket_id": None},
            "20": {"id": 20, "text": "See #16007", "checked": False, "ticket_id": 42},
        },
        # The blob also carries the whole ticket, its group and every user -
        # the tool must drop all of that.
        "Ticket": {"7": {"id": 7, "title": "noise"}},
        "User": {"3": {"id": 3, "login": "noise"}},
    },
}


async def test_get_ticket_checklist_walks_ticket_then_checklist() -> None:
    ctx = SequenceCtx([{"id": 7, "checklist_id": 6}, _FULL_CHECKLIST])
    mcp = _build(ctx)

    result = await _call(mcp, "get_ticket_checklist", ticket_id=7)

    assert ctx.calls[0]["method"] == "GET"
    assert ctx.calls[0]["path"] == "/tickets/7"
    assert ctx.calls[1]["method"] == "GET"
    assert ctx.calls[1]["path"] == "/checklists/6"
    # Without full=true the response carries item ids but no item text.
    assert ctx.calls[1]["params"] == {"full": "true"}

    payload = result.structured_content
    assert payload["checklist_id"] == 6
    assert payload["name"] == "Return order"
    assert payload["total"] == 3
    assert payload["open"] == 2
    # sorted_item_ids wins over item_ids, and the noise assets are gone.
    assert [item["item_id"] for item in payload["items"]] == [20, 18, 19]
    assert payload["items"][0]["text"] == "See #16007"
    assert payload["items"][0]["linked_ticket_id"] == 42
    assert "Ticket" not in payload


async def test_get_ticket_checklist_without_a_checklist_makes_one_call() -> None:
    """A ticket with no checklist is a normal state, not a 404 to work around."""
    ctx = SequenceCtx([{"id": 7, "checklist_id": None}])
    mcp = _build(ctx)

    result = await _call(mcp, "get_ticket_checklist", ticket_id=7)

    assert len(ctx.calls) == 1
    payload = result.structured_content
    assert payload["checklist_id"] is None
    assert payload["items"] == []
    assert payload["total"] == 0


async def test_get_ticket_checklist_falls_back_to_the_raw_body() -> None:
    """An unrecognised shape must not be reported as an empty checklist."""
    ctx = SequenceCtx([{"id": 7, "checklist_id": 6}, {"id": 6, "name": "no assets here"}])
    mcp = _build(ctx)

    payload = (await _call(mcp, "get_ticket_checklist", ticket_id=7)).structured_content

    assert payload["raw"] == {"id": 6, "name": "no assets here"}


async def test_get_ticket_checklist_keeps_items_missing_from_sorted_ids() -> None:
    """sorted_item_ids is maintained by callbacks and can lag behind item_ids."""
    body = {
        "id": 6,
        "assets": {
            "Checklist": {"6": {"id": 6, "sorted_item_ids": ["18"], "item_ids": [18, 19]}},
            "ChecklistItem": {
                "18": {"id": 18, "text": "first", "checked": False},
                "19": {"id": 19, "text": "appended", "checked": False},
            },
        },
    }
    ctx = SequenceCtx([{"id": 7, "checklist_id": 6}, body])
    mcp = _build(ctx)

    payload = (await _call(mcp, "get_ticket_checklist", ticket_id=7)).structured_content

    assert [item["item_id"] for item in payload["items"]] == [18, 19]


# ── writes ───────────────────────────────────────────────────────────────────


async def test_create_ticket_checklist_without_template() -> None:
    ctx = SequenceCtx([{"id": 13, "assets": {"Checklist": {"13": {"id": 13}}}}])
    mcp = _build(ctx)

    payload = (
        await _call(mcp, "create_ticket_checklist", ticket_id=7)
    ).structured_content

    assert ctx.last["method"] == "POST"
    assert ctx.last["path"] == "/checklists"
    assert ctx.last["json"] == {"ticket_id": 7}
    assert payload["checklist_id"] == 13
    assert payload["ticket_id"] == 7


async def test_create_ticket_checklist_from_a_template(mcp_and_ctx) -> None:  # type: ignore[no-untyped-def]
    mcp, ctx = mcp_and_ctx
    await _call(mcp, "create_ticket_checklist", ticket_id=7, template_id=3)
    assert ctx.last["json"] == {"ticket_id": 7, "template_id": 3}


async def test_list_checklist_templates_sends_page(mcp_and_ctx) -> None:  # type: ignore[no-untyped-def]
    """Zammad computes offset = (page - 1) * limit; without page the tool is
    pinned to the first page forever."""
    mcp, ctx = mcp_and_ctx
    await _call(mcp, "list_checklist_templates", page=3, per_page=10)
    assert ctx.last["method"] == "GET"
    assert ctx.last["path"] == "/checklist_templates"
    assert ctx.last["params"] == {"page": 3, "per_page": 10}


async def test_add_checklist_items_by_ticket_uses_the_bulk_route(mcp_and_ctx) -> None:  # type: ignore[no-untyped-def]
    mcp, ctx = mcp_and_ctx
    await _call(mcp, "add_checklist_items", ticket_id=7, items=["one", "two"])
    assert ctx.last["method"] == "POST"
    assert ctx.last["path"] == "/checklist_items/create_bulk"
    assert ctx.last["json"] == {
        "items": [{"text": "one"}, {"text": "two"}],
        "ticket_id": 7,
    }


async def test_add_checklist_items_by_checklist(mcp_and_ctx) -> None:  # type: ignore[no-untyped-def]
    mcp, ctx = mcp_and_ctx
    await _call(mcp, "add_checklist_items", checklist_id=6, items=["one"])
    assert ctx.last["json"] == {"items": [{"text": "one"}], "checklist_id": 6}


@pytest.mark.parametrize(
    "kwargs",
    [
        {"items": ["one"]},
        {"items": ["one"], "ticket_id": 7, "checklist_id": 6},
    ],
)
async def test_add_checklist_items_needs_exactly_one_target(mcp_and_ctx, kwargs) -> None:  # type: ignore[no-untyped-def]
    mcp, _ = mcp_and_ctx
    with pytest.raises(Exception, match="exactly one of ticket_id or checklist_id"):
        await _call(mcp, "add_checklist_items", **kwargs)


async def test_add_checklist_items_rejects_blank_text(mcp_and_ctx) -> None:  # type: ignore[no-untyped-def]
    mcp, _ = mcp_and_ctx
    with pytest.raises(Exception, match="blank item text"):
        await _call(mcp, "add_checklist_items", ticket_id=7, items=["ok", "   "])


async def test_set_checklist_item_ticks_with_a_real_boolean(mcp_and_ctx) -> None:  # type: ignore[no-untyped-def]
    mcp, ctx = mcp_and_ctx
    await _call(mcp, "set_checklist_item", item_id=20, checked=True)
    assert ctx.last["method"] == "PATCH"
    assert ctx.last["path"] == "/checklist_items/20"
    assert ctx.last["json"] == {"checked": True}


async def test_set_checklist_item_can_untick_and_rename(mcp_and_ctx) -> None:  # type: ignore[no-untyped-def]
    """checked=False must survive: an `if checked:` guard would silently drop it."""
    mcp, ctx = mcp_and_ctx
    await _call(mcp, "set_checklist_item", item_id=20, checked=False, text="redo this")
    assert ctx.last["json"] == {"checked": False, "text": "redo this"}


async def test_set_checklist_item_without_changes_raises_tool_error(mcp_and_ctx) -> None:  # type: ignore[no-untyped-def]
    mcp, _ = mcp_and_ctx
    with pytest.raises(Exception, match="needs checked, text, or both"):
        await _call(mcp, "set_checklist_item", item_id=20)
