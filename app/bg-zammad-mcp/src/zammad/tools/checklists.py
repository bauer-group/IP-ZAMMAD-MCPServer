"""
Checklist tools - the Zammad 6.4+ per-ticket checklist and its templates.

Endpoints (all under /api/v1/):
  GET    /tickets/{id}                  read the ticket's checklist_id   `ticket.agent` + read access
  GET    /checklists/{id}?full=true     the checklist and its items      `ticket.agent` + read access
  POST   /checklists                    start a checklist on a ticket    `ticket.agent` + update access
  GET    /checklist_templates           list reusable templates          `ticket.agent` or `admin.checklist`
  POST   /checklist_items/create_bulk   add n items in one request       `ticket.agent` + update access
  PATCH  /checklist_items/{id}          tick / untick / rename an item   `ticket.agent` + update access

There is no ticket-scoped checklist route
-----------------------------------------
Zammad models the link the other way round: the CHECKLIST is the parent and the
ticket carries a ``checklist_id`` column (``Checklist has_one :ticket``). So
``/tickets/{id}/checklist`` does not exist - reading a ticket's checklist is a
two-step walk, ticket -> checklist_id -> ``/checklists/{checklist_id}``, which
is exactly what ``get_ticket_checklist`` does in one tool call.

Why the responses are flattened
-------------------------------
``GET /checklists/{id}`` returns only ``item_ids`` - the item TEXT is not in it,
which makes the plain response useless to a model. The texts are only reachable
through Zammad's asset format (``?full=true`` on show, and unconditionally on
create), and that blob also drags in the whole ticket, its group, and every user
that ever touched it - several thousand tokens of noise per call. Both tools
therefore reduce the blob to the checklist plus its items in the order the agent
sees them, and fall back to the raw body if the shape is not what we expect.

Feature gate
------------
All of this is behind Zammad's ``checklist`` setting. While it is switched off
every route here answers 403, with the same generic message a missing ticket ACL
produces - the descriptions name both causes so the model does not retry blindly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any

from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from . import ToolContext

if TYPE_CHECKING:
    from fastmcp import FastMCP

# Checklist::Item#validate_item_count refuses item 101 on a checklist.
MAX_ITEMS_PER_CHECKLIST = 100

# model_index_render paginates with CanPaginate::Pagination, whose ceiling is
# 1000 when a controller does not lower it - ChecklistTemplatesController does not.
INDEX_MAX_PER_PAGE = 1000


def register(mcp: FastMCP, ctx: ToolContext) -> int:
    def compact(body: Any, ticket_id: int) -> dict[str, Any]:
        """Reduce Zammad's ``{id, assets}`` envelope to the checklist and its items."""
        checklist_id = body.get("id") if isinstance(body, dict) else None
        assets = body.get("assets") if isinstance(body, dict) else None
        if not isinstance(assets, dict):
            # An unexpected shape is not the same as an empty checklist - hand
            # the body back rather than silently reporting "no items".
            return {"ticket_id": ticket_id, "checklist_id": checklist_id, "raw": body}

        checklist = (assets.get("Checklist") or {}).get(str(checklist_id)) or {}
        items_by_id = assets.get("ChecklistItem") or {}
        # sorted_item_ids is the order agents see in the sidebar and holds its
        # ids as STRINGS, while item_ids holds the same ids as integers. Walk
        # the sorted list first, then append anything it does not cover so a
        # freshly created item can never vanish from the result.
        order = [str(item_id) for item_id in checklist.get("sorted_item_ids") or []]
        order += [
            str(item_id)
            for item_id in checklist.get("item_ids") or []
            if str(item_id) not in order
        ]

        items: list[dict[str, Any]] = []
        for item_id in order:
            item = items_by_id.get(item_id)
            if not isinstance(item, dict):
                continue
            items.append(
                {
                    "item_id": item.get("id"),
                    "text": item.get("text"),
                    "checked": item.get("checked"),
                    # Set when the item text contains a ticket number; Zammad
                    # then ticks the item automatically once that ticket closes.
                    "linked_ticket_id": item.get("ticket_id"),
                }
            )

        return {
            "ticket_id": ticket_id,
            "checklist_id": checklist_id,
            "name": checklist.get("name"),
            "items": items,
            "total": len(items),
            "open": sum(1 for item in items if not item["checked"]),
        }

    @mcp.tool(
        name="get_ticket_checklist",
        description=(
            "Read the checklist attached to a ticket: its name and every item "
            "with the item's own id, text and tick state, in the order agents "
            "see them. Use it to report progress on a ticket, or to get the "
            "item ids that `set_checklist_item` needs. Needs `ticket.agent` "
            "plus read access to the ticket. Costs two Zammad requests because "
            "a ticket only stores a checklist reference - there is no "
            "/tickets/{id}/checklist route. If the ticket has no checklist the "
            "result comes back with checklist_id null and an empty item list "
            "(NOT an error); start one with `add_checklist_items` or "
            "`create_ticket_checklist`. A 403 here means either the ticket is "
            "not yours to read or checklists are switched off in this Zammad."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=True, destructiveHint=False, openWorldHint=True
        ),
    )
    async def get_ticket_checklist(
        ticket_id: Annotated[int, Field(ge=1)],
    ) -> dict[str, Any]:
        # No expand: we only need the checklist_id column, and the plain
        # response is the cheaper of the two.
        ticket = await ctx.request("GET", f"/tickets/{ticket_id}")
        checklist_id = ticket.get("checklist_id") if isinstance(ticket, dict) else None
        if not checklist_id:
            return {
                "ticket_id": ticket_id,
                "checklist_id": None,
                "name": None,
                "items": [],
                "total": 0,
                "open": 0,
            }
        body = await ctx.request(
            "GET",
            f"/checklists/{checklist_id}",
            params={"full": "true"},
        )
        return compact(body, ticket_id)

    @mcp.tool(
        name="create_ticket_checklist",
        description=(
            "Start a checklist on a ticket, optionally cloning a checklist "
            "TEMPLATE - the way a team encodes a repeatable procedure "
            "(onboarding, RMA, incident review). Pass `template_id` from "
            "`list_checklist_templates` to get that template's items copied "
            "in; omit it for an empty checklist you then fill with "
            "`add_checklist_items`. Needs `ticket.agent` plus UPDATE access to "
            "the ticket. Two sharp edges: an inactive template is rejected "
            "with HTTP 422, and if the ticket ALREADY has a checklist Zammad "
            "does not refuse - it points the ticket at the new checklist and "
            "orphans the old one, losing its state. Call "
            "`get_ticket_checklist` first unless you know there is none."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,  # additive: creates a new checklist
            idempotentHint=False,
            openWorldHint=True,
        ),
    )
    async def create_ticket_checklist(
        ticket_id: Annotated[int, Field(ge=1)],
        template_id: Annotated[
            int | None,
            Field(
                ge=1,
                description=(
                    "Checklist template to clone the items and the name from. "
                    "The template must be active."
                ),
            ),
        ] = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"ticket_id": ticket_id}
        if template_id is not None:
            payload["template_id"] = template_id
        body = await ctx.request("POST", "/checklists", json=payload)
        return compact(body, ticket_id)

    @mcp.tool(
        name="list_checklist_templates",
        description=(
            "List the checklist templates configured in this Zammad, so you "
            "can pick a template_id for `create_ticket_checklist`. Agents may "
            "read this (`ticket.agent` or `admin.checklist`) even though "
            "editing templates is admin-only. Inactive templates are listed "
            "too but cannot be used - check the active flag before cloning "
            "one. The response carries each template's name and item ids, not "
            "the item texts."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=True, destructiveHint=False, openWorldHint=True
        ),
    )
    async def list_checklist_templates(
        page: Annotated[int, Field(ge=1, description="1-indexed page number")] = 1,
        per_page: Annotated[
            int,
            Field(ge=1, le=INDEX_MAX_PER_PAGE, description="Items per page (max 1000)"),
        ] = 50,
    ) -> Any:
        return await ctx.request(
            "GET",
            "/checklist_templates",
            params={"page": page, "per_page": per_page},
        )

    @mcp.tool(
        name="add_checklist_items",
        description=(
            "Append one or more items to a ticket's checklist in a SINGLE "
            "request. Pass `ticket_id` (preferred): Zammad creates the "
            "checklist on the fly if the ticket has none, which makes this the "
            "cheapest way to start one from scratch. Pass `checklist_id` "
            "instead only when you already have it and the ticket is "
            "irrelevant - exactly one of the two is required. Needs "
            "`ticket.agent` plus UPDATE access to the ticket. Zammad caps a "
            "checklist at 100 items and creates the items one after another "
            "WITHOUT a transaction, so hitting the cap mid-list leaves the "
            "earlier items in place - re-read with `get_ticket_checklist` "
            "after a failure instead of retrying the whole list. Also note "
            "that an item whose text contains a ticket NUMBER is auto-linked "
            "to that ticket and ticks itself when that ticket is closed."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,  # additive: appends items, changes none
            idempotentHint=False,  # calling twice appends the items twice
            openWorldHint=True,
        ),
    )
    async def add_checklist_items(
        items: Annotated[
            list[str],
            Field(
                min_length=1,
                max_length=MAX_ITEMS_PER_CHECKLIST,
                description="Item texts, one per checklist entry, in order",
            ),
        ],
        ticket_id: Annotated[int | None, Field(ge=1)] = None,
        checklist_id: Annotated[int | None, Field(ge=1)] = None,
    ) -> Any:
        if (ticket_id is None) == (checklist_id is None):
            raise ToolError(
                "add_checklist_items needs exactly one of ticket_id or "
                "checklist_id. Pass ticket_id when you want the checklist "
                "created if it does not exist yet."
            )
        if any(not text.strip() for text in items):
            raise ToolError(
                "add_checklist_items got a blank item text. Zammad accepts it "
                "and creates an empty, unreadable checklist row - drop the "
                "blank entries and call again."
            )
        payload: dict[str, Any] = {"items": [{"text": text} for text in items]}
        if ticket_id is not None:
            payload["ticket_id"] = ticket_id
        else:
            payload["checklist_id"] = checklist_id
        return await ctx.request("POST", "/checklist_items/create_bulk", json=payload)

    @mcp.tool(
        name="set_checklist_item",
        description=(
            "Tick, untick or rename ONE checklist item. `checked` records "
            "whether the step is done; `text` renames the step. Supply at "
            "least one of them - both are optional individually, and whatever "
            "you omit is left untouched. `item_id` is the item's own id (not "
            "the checklist id): `get_ticket_checklist` returns it as item_id "
            "for each entry. Needs `ticket.agent` plus UPDATE access to the "
            "ticket the checklist belongs to. Renaming an item to something "
            "containing a ticket number makes Zammad link the item to that "
            "ticket."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=True,  # overwrites the item's existing text / state
            idempotentHint=True,
            openWorldHint=True,
        ),
    )
    async def set_checklist_item(
        item_id: Annotated[int, Field(ge=1)],
        checked: Annotated[
            bool | None,
            Field(description="True = done, False = open. Omit to keep as is."),
        ] = None,
        text: Annotated[
            str | None,
            Field(min_length=1, description="New item text. Omit to keep as is."),
        ] = None,
    ) -> Any:
        payload: dict[str, Any] = {}
        if checked is not None:
            # A JSON body keeps real booleans - only query params need the
            # lowercase-string dance.
            payload["checked"] = checked
        if text is not None:
            payload["text"] = text
        if not payload:
            raise ToolError(
                "set_checklist_item needs checked, text, or both. Pass "
                "checked=true to tick the item off."
            )
        return await ctx.request("PATCH", f"/checklist_items/{item_id}", json=payload)

    return 5
