"""
Ticket-overview tools - the agent worklist, and it needs no Elasticsearch.

Endpoints (all under /api/v1/):
  GET /ticket_overviews              the caller's overviews, each with a count
  GET /ticket_overviews?view={slug}  the tickets inside one overview

Both are the same Rails action (``TicketOverviewsController#show``); the
presence of ``view`` switches the response shape entirely. Note the singular
``/ticket_overview`` route is a different action (``#data``, bulk-edit form
metadata) and is deliberately not exposed here.

Permission: ``ticket.agent`` or ``ticket.customer``. There is no explicit
authorize! on the action - ``Ticket::OverviewsPolicy::Scope`` restricts the
result to overviews the caller's roles grant, so an agent sees exactly the
queues their Zammad sidebar shows.

Why this module carries so much weight
--------------------------------------
The obvious way to answer "what is on my plate?" is `search_tickets`, but a
field-scoped query only works when the instance runs Elasticsearch - without
it Zammad degrades to a SQL LIKE over title and number and returns an EMPTY
list, not an error. Overviews are evaluated in SQL from the conditions an
admin already configured, so they work on every instance, and the count-only
listing answers the question in a handful of tokens.

Response shapes (read off TicketOverviewsController#show)
---------------------------------------------------------
Without ``view``, Zammad returns a plain list of
``{id, name, prio, link, count}`` - already model-friendly, so it is passed
through untouched.

With ``view``, it returns ``{"assets": {...}, "index": {...}}`` instead. The
index holds only ``{id, updated_at}`` stubs; the real ticket objects live in
``assets["Ticket"]`` keyed by id. Joining those two is exactly the step an LLM
gets wrong, so `list_queue_tickets` performs the join and hands back a plain
ordered list.

Paging
------
This endpoint is the one list route in Zammad that takes no paging parameters
at all: ``Ticket::Overviews.index`` applies only the instance-wide
``ui_ticket_overview_ticket_limit`` cap (2000 by default) and the controller
forwards nothing. Sending ``page`` upstream would be a no-op, so
`list_queue_tickets` slices the single response locally instead - and reports
the true queue size separately so a slice is never mistaken for the whole
queue.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any

from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from ..projection import envelope
from . import ToolContext

if TYPE_CHECKING:
    from fastmcp import FastMCP

# Local slice size only - Zammad ignores paging on this route, so this exists
# purely to keep one queue from flooding the model's context.
MAX_PER_PAGE = 200


def _join_tickets(payload: dict[str, Any]) -> list[Any]:
    """Resolve the index's ``{id, updated_at}`` stubs against the assets blob.

    Ruby keys ``assets["Ticket"]`` by integer id; JSON serialisation turns those
    into strings, so both forms are tried rather than assuming one. A stub with
    no matching asset degrades to the stub itself - Zammad omits assets for
    records the caller may not see, and silently dropping such a ticket would
    make the returned list disagree with the count next to it.
    """
    by_id = (payload.get("assets") or {}).get("Ticket") or {}
    stubs = (payload.get("index") or {}).get("tickets") or []
    return [by_id.get(str(stub.get("id"))) or by_id.get(stub.get("id")) or stub for stub in stubs]


def register(mcp: FastMCP, ctx: ToolContext) -> int:
    read_only = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=True)

    @mcp.tool(
        name="list_my_queues",
        description=(
            "List the ticket overviews - the work queues - visible to the "
            "current user, each with a live ticket count. This is Zammad's own "
            "answer to 'what is on my plate?', 'what is unassigned?' or 'what "
            "is escalated?', and it should be preferred over inventing a "
            "`search_tickets` query for those: overviews are evaluated in SQL, "
            "so they also work on instances without Elasticsearch, where a "
            "field-scoped search silently returns nothing. Needs only "
            "`ticket.agent` or `ticket.customer`, and the result is already "
            "scoped to the caller. Each entry carries an id, a name, a ticket "
            "count and a link slug; pass that slug to `list_queue_tickets` to "
            "see the tickets themselves."
        ),
        annotations=read_only,
    )
    async def list_my_queues() -> Any:
        return await ctx.request("GET", "/ticket_overviews")

    @mcp.tool(
        name="list_queue_tickets",
        description=(
            "List the tickets inside one ticket overview, in the overview's own "
            "sort order. `view` must be the overview's link SLUG (the link "
            "value returned by `list_my_queues`, e.g. 'my_assigned'), not its "
            "display name. WARNING: Zammad does not treat an unknown slug as an "
            "error - it answers HTTP 200 with an empty envelope - so this tool "
            "raises rather than let an empty result be read as an empty queue. "
            "Zammad also accepts no paging parameters here and returns the "
            "whole queue in one response, so `page` and `per_page` slice that "
            "response locally; the total_count field is always the true queue "
            "size, so compare it against the tickets you got back before "
            "concluding you have seen everything. Tickets come back as full "
            "objects, but with state, priority, group and owner as numeric IDs "
            "- use `get_ticket` when you need one ticket with names resolved."
        ),
        annotations=read_only,
    )
    async def list_queue_tickets(
        view: Annotated[
            str,
            Field(
                min_length=1,
                description="Overview link slug from list_my_queues, e.g. 'my_assigned'",
            ),
        ],
        page: Annotated[int, Field(ge=1, description="1-indexed page number")] = 1,
        per_page: Annotated[
            int,
            Field(ge=1, le=MAX_PER_PAGE, description="Tickets per page, sliced locally"),
        ] = 25,
    ) -> dict[str, Any]:
        payload = await ctx.request("GET", "/ticket_overviews", params={"view": view})
        index = payload.get("index") if isinstance(payload, dict) else None
        if not index:
            raise ToolError(
                f"No overview has the link slug {view!r}. Zammad answers an unknown "
                "view with HTTP 200 and an empty body instead of a 404, so this is "
                "almost always a wrong slug rather than an empty queue. Call "
                "list_my_queues and pass the 'link' value of the entry you want."
            )
        tickets = _join_tickets(payload)
        start = (page - 1) * per_page
        # `fetched_count` used to report len(tickets) — the whole joined queue,
        # not the slice actually returned — so a 5-ticket page out of a
        # 200-ticket queue claimed to have fetched 200. `returned` in the
        # envelope is computed from what actually ships.
        return envelope(
            tickets[start : start + per_page],
            page=page,
            per_page=per_page,
            total_count=index.get("count"),
            overview=index.get("overview"),
        )

    return 2
