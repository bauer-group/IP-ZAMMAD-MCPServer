"""
Field-discovery tools - what a ticket actually looks like in THIS Zammad.

Endpoints (all under /api/v1/):
  GET /ticket_create              the blank ticket-CREATE screen. Policy:
                                  ``permit! %i[ticket_create create], to:
                                  ['ticket.agent', 'ticket.customer']`` - so
                                  every caller who may open a ticket may call it.
  GET /object_manager_attributes  every Object-Manager attribute definition.
                                  Policy: ``default_permit!('admin.object')`` -
                                  403 for a plain agent.

Neither endpoint paginates (``list_full`` and the screen options both render
one complete document), so neither tool takes a page argument.

Why this module exists
----------------------
Every production Zammad grows custom ticket attributes through the Object
Manager - "Cost center", "Affected system", a tree_select of sites. They are
invisible in the generic API surface, so a model writing a create or update
call has no way to learn they exist, let alone that one of them is mandatory.

What GET /ticket_create really returns
--------------------------------------
``tickets#ticket_create`` calls ``Ticket::ScreenOptions.attributes_to_change``
with view 'ticket_create', screen 'create_middle' and NO ticket, and renders::

    {"assets":    {...},
     "form_meta": {"filter":               {"type_id": []},
                   "dependencies":         null,
                   "configure_attributes": null,
                   "core_workflow":        {...}}}

Two of those keys are structurally null on this route: "dependencies" is only
built for view 'ticket_overview', and "configure_attributes" - the typed
attribute list the endpoint's name suggests - is only built when a ticket
instance is passed in. So this route does NOT hand back data types.

What it does hand back is the Core Workflow evaluation of a blank create
screen, and that IS keyed by attribute name over every Object-Manager
attribute on the screen: ``CoreWorkflow::Attributes#visibility_default`` and
``#mandatory_default`` iterate ``object_elements``, not just workflow-touched
fields. That answers "which fields exist here, including the custom ones, and
which must I fill in" - which is the question worth answering.

What list_ticket_fields drops
-----------------------------
  * ``assets`` - a blob of serialised Group/User/... records for the web UI
  * ``form_meta.filter`` - the article type_id filter, empty without a ticket
  * ``form_meta.dependencies`` / ``configure_attributes`` - always null here
  * ``core_workflow.request_id`` / ``rerun_count`` / ``matched_workflows`` /
    ``eval`` / ``select`` / ``fill_in`` / ``flags`` - UI plumbing an API
    caller cannot act on

and what it cannot give at all: the DATA TYPE of each field, plus the option
list of a plain static select. ``CoreWorkflow::Result#filter_restrict_values``
keeps ``restrict_values`` only for relation-backed fields, filtered fields and
fields a workflow restricts; everything else is stripped before the response.
``list_object_attributes`` is the complete answer, at the price of admin.object.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any

from mcp.types import ToolAnnotations
from pydantic import Field

from ..projection import envelope
from . import ToolContext

if TYPE_CHECKING:
    from fastmcp import FastMCP

# Core Workflow marks each field 'show' (on the screen), 'hide' (present but
# collapsed) or 'remove' (not on this screen at all). Only 'show' is safe to
# send on a create call.
SHOWN = "show"


def _trim_create_screen(payload: Any, *, include_hidden: bool) -> Any:
    """Fold the create-screen payload into one compact row per ticket field."""
    form_meta = payload.get("form_meta") if isinstance(payload, dict) else None
    workflow = form_meta.get("core_workflow") if isinstance(form_meta, dict) else None
    if not isinstance(workflow, dict):
        # A Zammad build whose shape we do not recognise. Reporting zero fields
        # would read as "this instance has no ticket fields" - a confident lie.
        # Handing back what we could not parse lets the caller see the truth.
        return payload

    visibility: dict[str, Any] = workflow.get("visibility") or {}
    mandatory: dict[str, Any] = workflow.get("mandatory") or {}
    readonly: dict[str, Any] = workflow.get("readonly") or {}
    restrict: dict[str, Any] = workflow.get("restrict_values") or {}

    fields: list[dict[str, Any]] = []
    for name in sorted(set(visibility) | set(mandatory) | set(restrict)):
        shown = visibility.get(name, SHOWN)
        if shown != SHOWN and not include_hidden:
            continue
        field: dict[str, Any] = {"name": name, "required": bool(mandatory.get(name))}
        if shown != SHOWN:
            field["visibility"] = shown
        if readonly.get(name):
            field["readonly"] = True
        if name in restrict:
            field["allowed_values"] = restrict[name]
        fields.append(field)
    return envelope(fields)


def register(mcp: FastMCP, ctx: ToolContext) -> int:
    read_only = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=True)

    @mcp.tool(
        name="list_ticket_fields",
        description=(
            "Discover which fields a ticket has in THIS Zammad instance, "
            "including the custom Object-Manager attributes almost every "
            "production instance adds - nothing else on this server reveals "
            "them. Returns one row per field of the ticket-CREATE screen: its "
            "name, whether it is required, and its allowed values where Zammad "
            "supplies them. Feed those names straight into `create_ticket`, "
            "`update_ticket` or `update_tickets`. Needs only ticket.agent or "
            "ticket.customer, so this is the field-discovery call to reach for; "
            "`list_object_attributes` is richer but admin-only. LIMITS worth "
            "knowing before you trust the output: this route carries no DATA "
            "TYPE per field, and it lists allowed values only for "
            "relation-backed fields (group, owner, state, priority), filtered "
            "fields, and fields a Core Workflow restricts - a plain custom "
            "select can come back with no value list at all. Fields the create "
            "screen hides are omitted unless you pass `include_hidden`; pass "
            "`raw` for Zammad's untrimmed screen payload."
        ),
        annotations=read_only,
    )
    async def list_ticket_fields(
        include_hidden: Annotated[
            bool,
            Field(
                description=(
                    "Also return fields the create screen hides or removes "
                    "(Core Workflow visibility 'hide' / 'remove'). Those "
                    "usually must NOT be sent when creating a ticket."
                )
            ),
        ] = False,
        raw: Annotated[
            bool,
            Field(
                description=(
                    "Return Zammad's full create-screen document untrimmed, "
                    "including the assets blob. Large."
                )
            ),
        ] = False,
    ) -> Any:
        payload = await ctx.request("GET", "/ticket_create")
        if raw:
            return payload
        return _trim_create_screen(payload, include_hidden=include_hidden)

    @mcp.tool(
        name="list_object_attributes",
        description=(
            "Return the COMPLETE Object-Manager attribute definitions: name, "
            "display label, data type (input, select, tree_select, boolean, "
            "date, datetime, integer, ...), the data_option block with the "
            "option list and default, which screens the attribute appears on, "
            "and whether it is active. ADMIN-ONLY: Zammad guards this route "
            "with the admin.object permission and answers a plain agent with "
            "HTTP 403 - if you only need field names and required flags, call "
            "`list_ticket_fields` instead, which answers the same question for "
            "any agent. This route covers every object, not just tickets, so "
            "narrow it with `object_name`. Attributes still awaiting a schema "
            "migration are included: check their to_create / to_delete flags "
            "before relying on one."
        ),
        annotations=read_only,
    )
    async def list_object_attributes(
        object_name: Annotated[
            str | None,
            Field(
                description=(
                    "Keep only attributes of one object: 'Ticket', 'User', "
                    "'Organization', 'Group'. Applied here, not by Zammad - "
                    "the endpoint always returns every object."
                )
            ),
        ] = None,
        include_inactive: Annotated[
            bool,
            Field(description="Also return attributes an admin has deactivated"),
        ] = False,
    ) -> Any:
        rows = await ctx.request("GET", "/object_manager_attributes")
        if not isinstance(rows, list):
            return rows
        if object_name is not None:
            wanted = object_name.casefold()
            rows = [row for row in rows if str(row.get("object", "")).casefold() == wanted]
        if not include_inactive:
            rows = [row for row in rows if row.get("active", True)]
        # /object_manager_attributes ignores page and per_page entirely (59
        # attributes still come back for per_page=1 on 7.1.1), so this is always
        # the complete set — which the envelope states as page=None,
        # has_more=False rather than leaving the caller to assume it.
        return envelope(rows)

    return 2
