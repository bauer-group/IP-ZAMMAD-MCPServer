"""Request-shape and guard-rail tests for the bulk (mass update) tool.

``POST /tickets/mass_update`` is undocumented upstream and behaves unlike the
rest of the API in three ways that only a test can pin down:

* the body nests the change set under ``attributes`` and an optional article
  under ``article`` - get either key wrong and Zammad answers 200 having done
  nothing,
* a failure rolls the WHOLE batch back and reports the offending ticket as
  ``{"error": true, "ticket_id": N}``, whose ``error`` value is the boolean
  ``true`` rather than a message, and
* nothing server-side limits how many tickets one call may lock.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastmcp import FastMCP

from tests.test_tools_inventory import EXPECTED_TOOLS, RecordingCtx
from zammad.errors import ZammadForbidden, ZammadValidationError
from zammad.tools import bulk

MODULE_TOOLS = {"update_tickets"}
# Tools this module's descriptions may point at: the whole registered surface
# plus the two field-discovery tools it is designed to be used with.
KNOWN_TOOLS = set(EXPECTED_TOOLS) | MODULE_TOOLS | {"list_ticket_fields", "list_object_attributes"}


class RaisingCtx(RecordingCtx):
    """Records the call, then fails it the way Zammad would."""

    def __init__(self, error: Exception) -> None:
        super().__init__()
        self._error = error

    async def request(self, method: str, path: str, **kwargs: Any) -> Any:
        await super().request(method, path, **kwargs)
        raise self._error


@pytest.fixture
def mcp_and_ctx() -> tuple[FastMCP, RecordingCtx]:
    mcp: FastMCP = FastMCP("test-bulk")
    ctx = RecordingCtx()
    bulk.register(mcp, ctx)
    return mcp, ctx


async def _tools(mcp: FastMCP) -> dict[str, Any]:
    return {tool.name: tool for tool in await mcp.list_tools(run_middleware=False)}


async def _call(mcp: FastMCP, name: str, **kwargs: Any) -> Any:
    return await (await _tools(mcp))[name].run(kwargs)


# ── inventory, counts, annotations ───────────────────────────────────────────


async def test_module_registers_exactly_its_declared_tools() -> None:
    mcp: FastMCP = FastMCP("test-bulk")
    declared = bulk.register(mcp, RecordingCtx())
    tools = await _tools(mcp)
    assert set(tools) == MODULE_TOOLS
    assert declared == len(tools)


async def test_update_tickets_is_annotated_destructive(mcp_and_ctx) -> None:  # type: ignore[no-untyped-def]
    """It overwrites existing field values on up to 100 tickets at once."""
    mcp, _ = mcp_and_ctx
    annotations = (await _tools(mcp))["update_tickets"].annotations
    assert annotations is not None
    assert annotations.readOnlyHint is False
    assert annotations.destructiveHint is True
    # A riding article is appended again on every retry.
    assert annotations.idempotentHint is False


async def test_description_only_names_real_parameters(mcp_and_ctx) -> None:  # type: ignore[no-untyped-def]
    import re

    mcp, _ = mcp_and_ctx
    for name, tool in (await _tools(mcp)).items():
        params = set((tool.parameters or {}).get("properties", {}))
        for token in re.findall(r"`([a-z][a-z0-9_]{2,})`", tool.description or ""):
            assert token in params or token in KNOWN_TOOLS, (
                f"{name}: description references `{token}`, which is neither a "
                "parameter of this tool nor another tool"
            )


# ── request shape ────────────────────────────────────────────────────────────


async def test_attributes_are_nested_under_the_attributes_key(mcp_and_ctx) -> None:  # type: ignore[no-untyped-def]
    mcp, ctx = mcp_and_ctx
    await _call(mcp, "update_tickets", ticket_ids=[1, 2, 3], attributes={"state_id": 4})
    assert ctx.last["method"] == "POST"
    assert ctx.last["path"] == "/tickets/mass_update"
    assert ctx.last["json"] == {"ticket_ids": [1, 2, 3], "attributes": {"state_id": 4}}


async def test_association_names_and_custom_fields_pass_through_untouched(mcp_and_ctx) -> None:  # type: ignore[no-untyped-def]
    """Zammad's association_name_to_id_convert resolves names server-side, and
    custom Object-Manager fields are ordinary keys - neither may be filtered."""
    mcp, ctx = mcp_and_ctx
    attributes = {"state": "closed", "group": "2nd Level", "cost_center": "CC-42"}
    await _call(mcp, "update_tickets", ticket_ids=[9], attributes=attributes)
    assert ctx.last["json"]["attributes"] == attributes


async def test_article_rides_along_in_the_same_call(mcp_and_ctx) -> None:  # type: ignore[no-untyped-def]
    mcp, ctx = mcp_and_ctx
    await _call(
        mcp,
        "update_tickets",
        ticket_ids=[5],
        attributes={"state": "closed"},
        article_body="Closed after the maintenance window.",
        article_subject="Maintenance",
    )
    assert ctx.last["json"]["article"] == {
        "body": "Closed after the maintenance window.",
        "type": "note",
        "internal": True,
        "subject": "Maintenance",
    }


async def test_an_article_alone_is_a_valid_batch(mcp_and_ctx) -> None:  # type: ignore[no-untyped-def]
    mcp, ctx = mcp_and_ctx
    await _call(mcp, "update_tickets", ticket_ids=[5], article_body="FYI")
    assert "attributes" not in ctx.last["json"]
    assert ctx.last["json"]["article"]["body"] == "FYI"


# ── guard rails ──────────────────────────────────────────────────────────────


async def test_more_than_one_hundred_ids_is_refused(mcp_and_ctx) -> None:  # type: ignore[no-untyped-def]
    mcp, ctx = mcp_and_ctx
    with pytest.raises(Exception, match="at most 100 ticket ids"):
        await _call(mcp, "update_tickets", ticket_ids=list(range(1, 102)), attributes={"x": 1})
    assert not ctx.calls, "the oversized batch must never reach Zammad"


async def test_an_empty_change_set_is_refused(mcp_and_ctx) -> None:  # type: ignore[no-untyped-def]
    """Zammad answers an empty change set with HTTP 200 having done nothing."""
    mcp, ctx = mcp_and_ctx
    with pytest.raises(Exception, match="needs something to do"):
        await _call(mcp, "update_tickets", ticket_ids=[1])
    assert not ctx.calls


async def test_unknown_article_type_is_refused(mcp_and_ctx) -> None:  # type: ignore[no-untyped-def]
    mcp, _ = mcp_and_ctx
    with pytest.raises(Exception, match="article_type must be one of"):
        await _call(mcp, "update_tickets", ticket_ids=[1], article_body="x", article_type="fax")


async def test_internal_email_is_unreachable(mcp_and_ctx) -> None:  # type: ignore[no-untyped-def]
    """The articles.py trap, multiplied by the batch: Zammad DELIVERS an
    internal e-mail and then hides it from the customer who received it."""
    mcp, _ = mcp_and_ctx
    with pytest.raises(Exception, match="sends the mail"):
        await _call(
            mcp,
            "update_tickets",
            ticket_ids=[1, 2],
            article_body="x",
            article_type="email",
            article_internal=True,
        )


async def test_a_visible_bulk_reply_is_allowed(mcp_and_ctx) -> None:  # type: ignore[no-untyped-def]
    mcp, ctx = mcp_and_ctx
    await _call(
        mcp,
        "update_tickets",
        ticket_ids=[1],
        article_body="We are on it.",
        article_type="email",
        article_internal=False,
    )
    assert ctx.last["json"]["article"] == {
        "body": "We are on it.",
        "type": "email",
        "internal": False,
    }


# ── the 422 that says nothing ────────────────────────────────────────────────


async def test_rollback_surfaces_the_failing_ticket_id() -> None:
    """Zammad's 422 body is ``{"error": true, "ticket_id": N}``.

    ``errors.from_status`` reads ``error`` as the message, so without this
    unwrapping the model is told the batch failed with "True" - naming neither
    the cause nor the ticket that caused it.
    """
    mcp: FastMCP = FastMCP("test-bulk")
    ctx = RaisingCtx(
        ZammadValidationError(
            "True", status_code=422, body={"error": True, "ticket_id": 4711}
        )
    )
    bulk.register(mcp, ctx)
    with pytest.raises(Exception, match="4711"):
        await _call(mcp, "update_tickets", ticket_ids=[4710, 4711], attributes={"state": "closed"})


async def test_a_422_without_a_ticket_id_is_left_alone() -> None:
    """Nothing to add - re-raising the typed error keeps Zammad's own wording."""
    mcp: FastMCP = FastMCP("test-bulk")
    ctx = RaisingCtx(
        ZammadValidationError("Group required", status_code=422, body={"error": "Group required"})
    )
    bulk.register(mcp, ctx)
    with pytest.raises(Exception, match="Group required"):
        await _call(mcp, "update_tickets", ticket_ids=[1], attributes={"state": "closed"})


async def test_other_zammad_errors_are_not_swallowed() -> None:
    mcp: FastMCP = FastMCP("test-bulk")
    ctx = RaisingCtx(ZammadForbidden("Not authorized", status_code=403))
    bulk.register(mcp, ctx)
    with pytest.raises(Exception, match="Not authorized"):
        await _call(mcp, "update_tickets", ticket_ids=[1], attributes={"state": "closed"})
