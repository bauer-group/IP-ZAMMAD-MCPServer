"""
Macro tools - the organisation's own pre-approved ticket workflows.

Endpoints (all under /api/v1/):
  GET  /macros              macros the caller may apply (policy-scoped)
  POST /tickets/mass_macro  apply one macro to a batch of tickets

A macro bundles a whole edit - state, owner, priority, tags, an article, a
pending time - into one server-side action an admin has already sanctioned.
Applying one is therefore both safer and more faithful to how the team works
than reproducing the same changes field by field.

Permissions
-----------
GET /macros
    ``admin.macro`` OR ``ticket.agent`` (Controllers::MacrosControllerPolicy).
    The body is policy-scoped by MacroPolicy::Scope: an admin sees every macro
    including inactive ones, an agent sees only ACTIVE macros that are either
    unrestricted or bound to a group they hold change/create access on.
POST /tickets/mass_macro
    No blanket permission check; TicketsMassController authorises every ticket
    in the batch individually with ``agent_update_access?``.

Why there is no single-ticket variant
-------------------------------------
``PUT /tickets/{id}`` does accept a macro, but only as the PAIR ``macro.id`` +
``macro.perform_changes``. ``TicketsController#handle_macro_perform_changes``
returns nil - quietly skipping the macro - whenever ``perform_changes`` is
blank, and when it is present it filters ``macro.perform`` down to just the
listed keys, because the legacy web frontend had already applied the rest
client-side before submitting. An API caller sending only a macro id gets
HTTP 200, a normal ticket update, and no macro. Rather than ship that trap, a
single ticket goes through mass_macro as a one-element batch.

Batch cap
---------
mass_macro runs inside one ActiveRecord transaction: either every ticket is
changed or none is. That makes partial failure easy to reason about but also
makes an oversized batch an all-or-nothing bulk mutation, so the tool refuses
more than MAX_TICKETS_PER_MACRO ids per call rather than let an agent fire an
unbounded write unattended.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any

from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from ..errors import ZammadValidationError
from . import ToolContext

if TYPE_CHECKING:
    from fastmcp import FastMCP

MAX_TICKETS_PER_MACRO = 100

# model_index_render pins /macros to paginate_with(default: 500) and CanPaginate's
# fallback ceiling of 1000.
MACROS_MAX_PER_PAGE = 1000


def _refusal_message(macro_id: int, exc: ZammadValidationError) -> str:
    """Turn Zammad's 422 into something the model can repeat back to a user.

    TicketsMassController reports a refusal in two shapes and the generic
    decoder can use neither: the group-restriction case hides the ticket IDs in
    ``blocking_tickets`` where nothing looks for them, and the access-denied
    case sets ``error`` to the boolean true, which stringifies to a bare "True".
    Naming the blocked tickets is the difference between the model saying "the
    macro failed" and "tickets 41 and 44 are in a group this macro does not
    cover".
    """
    blocking = exc.body.get("blocking_tickets")
    if blocking:
        ids = ", ".join(str(ticket_id) for ticket_id in blocking)
        return (
            f"Macro {macro_id} is restricted to groups that do not cover every "
            f"ticket in the batch. Blocked ticket IDs: {ids}. NOTHING was "
            "changed - the batch is a single transaction. Retry without those "
            "tickets, or use list_macros to find a macro whose groups cover them."
        )
    refused = exc.body.get("ticket_id")
    if refused is not None:
        return (
            f"Ticket {refused} refused macro {macro_id}: either the caller has no "
            "agent update access to that ticket, or applying the macro failed "
            "validation on it. NOTHING was changed - the batch is a single "
            "transaction. Retry without that ticket."
        )
    return f"Zammad rejected the macro run: {exc}"


def register(mcp: FastMCP, ctx: ToolContext) -> int:
    @mcp.tool(
        name="list_macros",
        description=(
            "List the macros available to the current user. A macro is a "
            "workflow the organisation pre-approved: one named action that can "
            "set state, owner, priority and tags and add an article in a single "
            "audited step. PREFER a macro over reproducing the same edit field "
            "by field with `update_ticket` - the macro is what the team "
            "actually sanctioned for that situation. Needs `admin.macro` or "
            "`ticket.agent`. Zammad already filters the list to what the caller "
            "may apply (an agent sees only active macros covering a group they "
            "can edit), so an empty result means none are available to them, "
            "not that something failed. Apply one with `apply_macro_to_tickets`."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=True, destructiveHint=False, openWorldHint=True
        ),
    )
    async def list_macros(
        page: Annotated[int, Field(ge=1, description="1-indexed page number")] = 1,
        per_page: Annotated[
            int,
            Field(ge=1, le=MACROS_MAX_PER_PAGE, description="Items per page (max 1000)"),
        ] = 100,
        expand: Annotated[
            bool,
            Field(description="Inline group/role names instead of IDs"),
        ] = True,
    ) -> Any:
        return await ctx.request(
            "GET",
            "/macros",
            params={"page": page, "per_page": per_page, "expand": str(expand).lower()},
        )

    @mcp.tool(
        name="apply_macro_to_tickets",
        description=(
            "Run one pre-approved macro over up to 100 tickets as a SINGLE "
            "server-side transaction: if any one ticket is refused, nothing at "
            "all is changed. Take `macro_id` from `list_macros`. This is also "
            "the right tool for a SINGLE ticket - pass a one-element "
            "`ticket_ids` list; Zammad's per-ticket route ignores a macro "
            "unless extra internal fields are supplied, so it is not offered. "
            "Zammad refuses the batch when the macro is restricted to groups "
            "that do not cover every ticket, or when the caller lacks agent "
            "update access on one of them; either way this tool names the "
            "offending ticket IDs in the error so you can tell the user exactly "
            "which ones were blocked. On success it returns only the affected "
            "ticket IDs, not the updated tickets - read one back with "
            "`get_ticket` if you need to confirm the outcome."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=False,
            # A macro exists to overwrite state, owner and priority, and can
            # close tickets - destructive under the MCP spec's "may perform
            # destructive updates", regardless of the additive-sounding verb.
            destructiveHint=True,
            # Macros routinely append an article, so a second run is not a
            # no-op even when the field changes settle.
            idempotentHint=False,
            openWorldHint=True,
        ),
    )
    async def apply_macro_to_tickets(
        macro_id: Annotated[int, Field(ge=1, description="Macro ID from list_macros")],
        ticket_ids: Annotated[
            list[int],
            Field(
                min_length=1,
                description="Ticket IDs to run the macro on - 1 to 100 per call",
            ),
        ],
    ) -> dict[str, Any]:
        # Rails' Ticket.find(ids) raises RecordNotFound when the row count does
        # not match the id count, so a repeated id would 404 the whole batch for
        # no real reason. dict.fromkeys de-duplicates in the caller's order.
        unique_ids = list(dict.fromkeys(ticket_ids))
        if len(unique_ids) > MAX_TICKETS_PER_MACRO:
            raise ToolError(
                f"apply_macro_to_tickets accepts at most {MAX_TICKETS_PER_MACRO} "
                f"ticket_ids per call (got {len(unique_ids)}). Split the work into "
                "smaller batches and confirm them with the user - a bulk mutation "
                "this large should not run unattended."
            )
        try:
            await ctx.request(
                "POST",
                "/tickets/mass_macro",
                json={"macro_id": macro_id, "ticket_ids": unique_ids},
            )
        except ZammadValidationError as exc:
            # The only place in this server where swallowing a typed error pays:
            # the ticket IDs Zammad blocked are in the body, not the message.
            raise ToolError(_refusal_message(macro_id, exc)) from exc
        return {"applied": True, "macro_id": macro_id, "ticket_ids": unique_ids}

    return 2
