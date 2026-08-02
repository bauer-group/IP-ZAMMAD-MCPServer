"""Request-shape tests for the time-accounting tools.

Besides the usual verb/path/payload contract, these pin the one place the module
adds behaviour: Zammad answers "the feature is switched off" and "you may not
touch this ticket" with the same 403, so ``add_ticket_time_entry`` re-raises it
with both causes named.
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from fastmcp import FastMCP

from tests.test_tools_inventory import RecordingCtx
from zammad.errors import ZammadForbidden
from zammad.tools import time_accounting

EXPECTED_TOOLS = sorted(["list_ticket_time_entries", "add_ticket_time_entry"])

# Backticked words in a description that are Zammad-side names or values rather
# than parameters of the tool they appear in.
_PROSE_ALLOWLIST: set[str] = set()


class RaisingCtx(RecordingCtx):
    """A RecordingCtx whose request always raises - for the 403 branch."""

    def __init__(self, error: Exception) -> None:
        super().__init__()
        self._error = error

    async def request(self, method: str, path: str, **kwargs: Any) -> Any:
        self.calls.append({"method": method, "path": path, **kwargs})
        raise self._error


def _build(ctx: RecordingCtx) -> FastMCP:
    mcp: FastMCP = FastMCP("test-time-accounting")
    time_accounting.register(mcp, ctx)
    return mcp


async def _tools(mcp: FastMCP) -> dict[str, Any]:
    return {tool.name: tool for tool in await mcp.list_tools(run_middleware=False)}


async def _call(mcp: FastMCP, name: str, /, **kwargs: Any) -> Any:
    # positional-only: several tools have their own `name` parameter
    # (create_organization, for one), which would collide otherwise.
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
    mcp: FastMCP = FastMCP("count-time-accounting")
    declared = time_accounting.register(mcp, RecordingCtx())
    assert declared == len(await mcp.list_tools(run_middleware=False))


async def test_descriptions_only_name_real_parameters(mcp_and_ctx) -> None:  # type: ignore[no-untyped-def]
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
    """Booking time appends to a log - nothing here overwrites existing state."""
    mcp, _ = mcp_and_ctx
    tools = await _tools(mcp)
    listing = tools["list_ticket_time_entries"].annotations
    booking = tools["add_ticket_time_entry"].annotations
    assert listing is not None and booking is not None
    assert listing.readOnlyHint is True
    assert listing.destructiveHint is False
    assert booking.readOnlyHint is False
    assert booking.destructiveHint is False
    assert booking.idempotentHint is False


# ── request shapes ───────────────────────────────────────────────────────────


async def test_list_ticket_time_entries_is_ticket_scoped_and_paginated(mcp_and_ctx) -> None:  # type: ignore[no-untyped-def]
    """The unscoped /time_accountings index is admin-only; the ticket-scoped
    one is what an agent may read. It paginates, so `page` must be sent."""
    mcp, ctx = mcp_and_ctx
    await _call(mcp, "list_ticket_time_entries", ticket_id=50, page=2, per_page=10)
    assert ctx.last["method"] == "GET"
    assert ctx.last["path"] == "/tickets/50/time_accountings"
    assert ctx.last["params"] == {"page": 2, "per_page": 10}


async def test_add_ticket_time_entry_minimal_payload(mcp_and_ctx) -> None:  # type: ignore[no-untyped-def]
    """ticket_id travels in the URL - the scoped controller sets it itself."""
    mcp, ctx = mcp_and_ctx
    await _call(mcp, "add_ticket_time_entry", ticket_id=50, time_unit=15)
    assert ctx.last["method"] == "POST"
    assert ctx.last["path"] == "/tickets/50/time_accountings"
    assert ctx.last["json"] == {"time_unit": 15.0}


async def test_add_ticket_time_entry_with_article_and_type(mcp_and_ctx) -> None:  # type: ignore[no-untyped-def]
    mcp, ctx = mcp_and_ctx
    await _call(
        mcp,
        "add_ticket_time_entry",
        ticket_id=50,
        time_unit=7.5,
        ticket_article_id=88,
        type_id=4,
    )
    assert ctx.last["json"] == {
        "time_unit": 7.5,
        "ticket_article_id": 88,
        "type_id": 4,
    }


async def test_add_ticket_time_entry_rejects_non_positive_time(mcp_and_ctx) -> None:  # type: ignore[no-untyped-def]
    mcp, _ = mcp_and_ctx
    with pytest.raises(Exception):  # noqa: B017 - pydantic validation, wrapped by FastMCP
        await _call(mcp, "add_ticket_time_entry", ticket_id=50, time_unit=0)


async def test_add_ticket_time_entry_explains_a_403() -> None:
    """Zammad's 403 for a disabled feature and for a missing ticket ACL are
    indistinguishable - the tool must name both so the model stops retrying."""
    ctx = RaisingCtx(
        ZammadForbidden("Time Accounting is not enabled", status_code=403)
    )
    mcp = _build(ctx)

    with pytest.raises(ZammadForbidden) as excinfo:
        await _call(mcp, "add_ticket_time_entry", ticket_id=50, time_unit=15)

    message = str(excinfo.value)
    assert "Time Accounting is not enabled" in message  # original detail kept
    assert "write access to this ticket" in message  # the second cause named
    assert excinfo.value.status_code == 403


async def test_add_ticket_time_entry_does_not_swallow_other_errors() -> None:
    """Only the ambiguous 403 is enriched; everything else passes through."""
    from zammad.errors import ZammadNotFound

    ctx = RaisingCtx(ZammadNotFound("No such ticket", status_code=404))
    mcp = _build(ctx)

    with pytest.raises(ZammadNotFound, match="No such ticket"):
        await _call(mcp, "add_ticket_time_entry", ticket_id=999, time_unit=15)
