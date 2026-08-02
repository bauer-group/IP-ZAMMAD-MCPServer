"""Tests for the macro tools.

Two things carry real risk here and both are pinned: that a single ticket still
goes through the mass endpoint (the per-ticket route silently ignores a macro),
and that a 422 refusal is turned into a message naming the blocked tickets
rather than Zammad's stringified boolean ``true``.
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from fastmcp import FastMCP

from tests.test_tools_inventory import RecordingCtx
from zammad.errors import ZammadValidationError
from zammad.tools import macros

EXPECTED_TOOLS = ["apply_macro_to_tickets", "list_macros"]

# Tools from other modules that these descriptions may legitimately point at.
KNOWN_OTHER_TOOLS = {"update_ticket", "get_ticket"}


class RaisingCtx(RecordingCtx):
    """A RecordingCtx whose ``request`` raises, to exercise the 422 branches."""

    def __init__(self, error: Exception) -> None:
        super().__init__()
        self._error = error

    async def request(self, method: str, path: str, **kwargs: Any) -> Any:
        await super().request(method, path, **kwargs)
        raise self._error


def _build(ctx: RecordingCtx | None = None) -> tuple[FastMCP, RecordingCtx]:
    mcp: FastMCP = FastMCP("test-macros")
    ctx = ctx if ctx is not None else RecordingCtx()
    macros.register(mcp, ctx)
    return mcp, ctx


async def _tools(mcp: FastMCP) -> dict[str, Any]:
    return {tool.name: tool for tool in await mcp.list_tools(run_middleware=False)}


async def _run(mcp: FastMCP, name: str, **kwargs: Any) -> Any:
    result = await (await _tools(mcp))[name].run(kwargs)
    return result.structured_content


# ── inventory + annotations ──────────────────────────────────────────────────


async def test_registers_exactly_the_declared_tools() -> None:
    mcp: FastMCP = FastMCP("test-macros")
    declared = macros.register(mcp, RecordingCtx())
    assert sorted(await _tools(mcp)) == EXPECTED_TOOLS
    assert declared == len(EXPECTED_TOOLS)


async def test_annotations_mark_the_macro_run_destructive() -> None:
    """A macro overwrites state, owner and priority and can close a ticket, so
    an MCP client must not auto-run it the way it may auto-run a read."""
    mcp, _ = _build()
    tools = await _tools(mcp)

    listing = tools["list_macros"].annotations
    assert listing is not None
    assert listing.readOnlyHint is True
    assert listing.destructiveHint is False

    applying = tools["apply_macro_to_tickets"].annotations
    assert applying is not None
    assert applying.readOnlyHint is False
    assert applying.destructiveHint is True
    # Macros routinely append an article, so a repeat run is not a no-op.
    assert applying.idempotentHint is False


async def test_descriptions_only_name_real_parameters() -> None:
    mcp, _ = _build()
    problems: list[str] = []
    for name, tool in (await _tools(mcp)).items():
        params = set((tool.parameters or {}).get("properties", {}))
        for token in re.findall(r"`([a-z][a-z0-9_]{2,})`", tool.description or ""):
            if token in params or token in EXPECTED_TOOLS or token in KNOWN_OTHER_TOOLS:
                continue
            problems.append(f"{name}: description references `{token}`, not a parameter")
    assert not problems, "\n".join(problems)


# ── list_macros ──────────────────────────────────────────────────────────────


async def test_list_macros_paginates_and_expands() -> None:
    mcp, ctx = _build()
    await _run(mcp, "list_macros", page=3, per_page=10, expand=False)
    assert ctx.last["method"] == "GET"
    assert ctx.last["path"] == "/macros"
    # A Python False would reach Zammad as the string "False", which its
    # boolean cast does not recognise.
    assert ctx.last["params"] == {"page": 3, "per_page": 10, "expand": "false"}


async def test_list_macros_defaults_send_page_one() -> None:
    mcp, ctx = _build()
    await _run(mcp, "list_macros")
    assert ctx.last["params"]["page"] == 1
    assert ctx.last["params"]["expand"] == "true"


# ── apply_macro_to_tickets ───────────────────────────────────────────────────


async def test_apply_posts_macro_id_and_ticket_ids_to_mass_macro() -> None:
    mcp, ctx = _build()
    result = await _run(mcp, "apply_macro_to_tickets", macro_id=4, ticket_ids=[11, 12])
    assert ctx.last["method"] == "POST"
    assert ctx.last["path"] == "/tickets/mass_macro"
    assert ctx.last["json"] == {"macro_id": 4, "ticket_ids": [11, 12]}
    assert result == {"applied": True, "macro_id": 4, "ticket_ids": [11, 12]}


async def test_a_single_ticket_still_goes_through_mass_macro() -> None:
    """PUT /tickets/{id} accepts macro.id but ignores it unless
    macro.perform_changes is supplied too - it returns HTTP 200 having applied
    nothing. The one-element batch is the only reliable single-ticket path."""
    mcp, ctx = _build()
    await _run(mcp, "apply_macro_to_tickets", macro_id=4, ticket_ids=[11])
    assert ctx.last["path"] == "/tickets/mass_macro"
    assert ctx.last["json"]["ticket_ids"] == [11]


async def test_duplicate_ids_are_collapsed_in_caller_order() -> None:
    """Rails' Ticket.find(ids) compares result size against id size, so a
    repeated id 404s the entire batch."""
    mcp, ctx = _build()
    result = await _run(mcp, "apply_macro_to_tickets", macro_id=4, ticket_ids=[12, 11, 12])
    assert ctx.last["json"]["ticket_ids"] == [12, 11]
    assert result["ticket_ids"] == [12, 11]


async def test_batch_larger_than_the_cap_is_refused_before_the_request() -> None:
    mcp, ctx = _build()
    with pytest.raises(Exception, match="at most 100"):
        await _run(
            mcp,
            "apply_macro_to_tickets",
            macro_id=4,
            ticket_ids=list(range(1, macros.MAX_TICKETS_PER_MACRO + 2)),
        )
    assert ctx.calls == [], "an oversized batch must never reach Zammad"


async def test_exactly_the_cap_is_allowed() -> None:
    mcp, ctx = _build()
    await _run(
        mcp,
        "apply_macro_to_tickets",
        macro_id=4,
        ticket_ids=list(range(1, macros.MAX_TICKETS_PER_MACRO + 1)),
    )
    assert len(ctx.last["json"]["ticket_ids"]) == macros.MAX_TICKETS_PER_MACRO


async def test_empty_ticket_list_is_rejected_by_the_schema() -> None:
    mcp, ctx = _build()
    with pytest.raises(Exception):  # noqa: B017 - pydantic/FastMCP wrap the error type
        await _run(mcp, "apply_macro_to_tickets", macro_id=4, ticket_ids=[])
    assert ctx.calls == []


async def test_group_restriction_refusal_names_the_blocking_tickets() -> None:
    """Zammad puts the useful part - which tickets blocked the run - in
    ``blocking_tickets``, where the generic error decoder never looks."""
    error = ZammadValidationError(
        "Macro group restrictions do not cover all tickets",
        status_code=422,
        body={
            "error": "Macro group restrictions do not cover all tickets",
            "blocking_tickets": [41, 44],
        },
    )
    mcp, ctx = _build(RaisingCtx(error))
    with pytest.raises(Exception, match="41, 44"):
        await _run(mcp, "apply_macro_to_tickets", macro_id=4, ticket_ids=[41, 42, 44])
    assert ctx.last["path"] == "/tickets/mass_macro"


async def test_access_refusal_names_the_ticket_despite_a_boolean_error_field() -> None:
    """This body sets ``error`` to the boolean true, which the typed error
    stringifies to a bare "True" - useless to the model on its own."""
    error = ZammadValidationError(
        "True", status_code=422, body={"error": True, "ticket_id": 5}
    )
    mcp, _ = _build(RaisingCtx(error))
    with pytest.raises(Exception, match="Ticket 5 refused macro 4"):
        await _run(mcp, "apply_macro_to_tickets", macro_id=4, ticket_ids=[5, 6])


async def test_an_unrecognised_422_body_still_surfaces_zammads_message() -> None:
    error = ZammadValidationError("Some other problem", status_code=422, body={})
    mcp, _ = _build(RaisingCtx(error))
    with pytest.raises(Exception, match="Some other problem"):
        await _run(mcp, "apply_macro_to_tickets", macro_id=4, ticket_ids=[5])
