"""
Ticket tools - the core of the Zammad MCP surface.

Endpoints exercised (all under /api/v1/):
  GET    /tickets                          paginated list (ID ascending!)
  GET    /tickets/search?query=...         full-text search
  GET    /tickets/{id}                     get one
  POST   /tickets                          create
  PUT    /tickets/{id}                     update
  DELETE /tickets/{id}                     delete (Admin/owner-only)

All tools forward the authenticated user's bearer token, so Zammad's own
permission system gates which tickets the caller can see / edit / delete.

Pagination
----------
Zammad computes ``offset = (page - 1) * limit`` and defaults ``page`` to 1
(``CanPaginate::Pagination``). A tool that sends only ``limit`` is therefore
structurally pinned to the first page - it can never reach result 26. Every
list/search tool here sends ``page`` explicitly, and requests
``with_total_count`` so the caller can tell "25 matches" from "25 of 4000".

Hint annotations
----------------
Read-only tools (`list_*`, `search_*`, `get_*`) are marked `readOnlyHint=True`
so MCP clients can auto-run them without prompting. Per the MCP spec,
`destructiveHint` means "may perform destructive updates" - it is therefore
False for `create_ticket` (purely additive) and True only for `update_ticket`
(overwrites existing field values) and `delete_ticket`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any

from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from . import ToolContext

if TYPE_CHECKING:
    from fastmcp import FastMCP

# Zammad's own server-side ceiling: paginate_with(max: 200, default: 50) on the
# search endpoints, paginate_with(max: 100) on the plain index.
SEARCH_MAX_LIMIT = 200
INDEX_MAX_PER_PAGE = 100


def register(mcp: FastMCP, ctx: ToolContext) -> int:
    @mcp.tool(
        name="list_tickets",
        description=(
            "List Zammad tickets, paginated. NOTE: Zammad returns these in "
            "ascending ID order - OLDEST FIRST - and the endpoint accepts no "
            "sort parameter. To find recent or relevant tickets use "
            "`search_tickets`; to see an agent's actual worklist use the "
            "ticket overviews. This tool is mainly useful for exhaustive "
            "enumeration."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=True, destructiveHint=False, openWorldHint=True
        ),
    )
    async def list_tickets(
        page: Annotated[int, Field(ge=1, description="1-indexed page number")] = 1,
        per_page: Annotated[
            int,
            Field(ge=1, le=INDEX_MAX_PER_PAGE, description="Items per page (max 100)"),
        ] = 25,
        expand: Annotated[
            bool,
            Field(description="Inline state/priority/owner names instead of IDs"),
        ] = True,
    ) -> Any:
        return await ctx.request(
            "GET",
            "/tickets",
            params={"page": page, "per_page": per_page, "expand": str(expand).lower()},
        )

    @mcp.tool(
        name="search_tickets",
        description=(
            "Full-text search Zammad tickets. IMPORTANT: field-scoped queries "
            "like `state.name:open`, `owner.email:a@b.c` or "
            "`created_at:>=now-7d` only work when the Zammad instance runs "
            "Elasticsearch. Without it Zammad falls back to a plain SQL LIKE "
            "over title and number, so a field-scoped query returns an EMPTY "
            "list rather than an error - if a search you expected to match "
            "comes back empty, retry with plain keywords before concluding "
            "there are no such tickets. Results are paginated: pass `page` to "
            "go beyond the first `limit` results."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=True, destructiveHint=False, openWorldHint=True
        ),
    )
    async def search_tickets(
        query: Annotated[str, Field(min_length=1, description="Zammad search query")],
        page: Annotated[int, Field(ge=1, description="1-indexed page number")] = 1,
        limit: Annotated[
            int, Field(ge=1, le=SEARCH_MAX_LIMIT, description="Results per page (max 200)")
        ] = 25,
        sort_by: Annotated[
            str | None,
            Field(description="Sort field, e.g. 'created_at' or 'updated_at'"),
        ] = None,
        order_by: Annotated[str | None, Field(description="'asc' or 'desc'")] = None,
        expand: Annotated[bool, Field(description="Inline names instead of IDs")] = True,
        with_total_count: Annotated[
            bool,
            Field(
                description=(
                    "Include the total number of matches so you can tell a "
                    "complete result from a truncated one. Wraps the response "
                    "in an object with a total_count field."
                )
            ),
        ] = True,
    ) -> Any:
        params: dict[str, Any] = {
            "query": query,
            "page": page,
            "limit": limit,
            "expand": str(expand).lower(),
        }
        if with_total_count:
            params["with_total_count"] = "true"
        if sort_by:
            params["sort_by"] = sort_by
        if order_by:
            params["order_by"] = order_by
        return await ctx.request("GET", "/tickets/search", params=params)

    @mcp.tool(
        name="get_ticket",
        description="Fetch a single Zammad ticket by its numeric ID.",
        annotations=ToolAnnotations(
            readOnlyHint=True, destructiveHint=False, openWorldHint=True
        ),
    )
    async def get_ticket(
        ticket_id: Annotated[int, Field(ge=1)],
        expand: Annotated[bool, Field(description="Inline names instead of IDs")] = True,
    ) -> Any:
        return await ctx.request(
            "GET",
            f"/tickets/{ticket_id}",
            params={"expand": str(expand).lower()},
        )

    @mcp.tool(
        name="create_ticket",
        description=(
            "Create a new Zammad ticket. Requires `title`, `group` (group name "
            "or ID), `customer` (customer e-mail or user ID), and an initial "
            "`article_body`. The opening article is customer-visible by "
            "default, matching how a ticket raised by a customer looks - set "
            "`article_internal=true` for a ticket you are raising purely for "
            "internal tracking. Other useful fields: `priority_id`, "
            "`state_id`, `owner_id`, `ticket_type`, `tags`."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,  # additive: creates a new ticket
            idempotentHint=False,
            openWorldHint=True,
        ),
    )
    async def create_ticket(
        title: Annotated[str, Field(min_length=1, max_length=255)],
        group: Annotated[
            str, Field(description="Group name (preferred) or numeric ID as string")
        ],
        customer: Annotated[
            str,
            Field(description="Customer e-mail address (preferred) or numeric user ID"),
        ],
        article_body: Annotated[
            str, Field(min_length=1, description="Initial article body (plain text or HTML)")
        ],
        article_type: Annotated[
            str,
            Field(
                description=(
                    "How the request arrived: 'note' (default), 'email', "
                    "'phone', 'web', 'chat'."
                )
            ),
        ] = "note",
        article_internal: Annotated[
            bool,
            Field(
                description=(
                    "Hide the opening article from the customer. Defaults to "
                    "false (customer-visible)."
                )
            ),
        ] = False,
        priority_id: Annotated[int | None, Field(ge=1)] = None,
        state_id: Annotated[int | None, Field(ge=1)] = None,
        owner_id: Annotated[int | None, Field(ge=1)] = None,
        ticket_type: Annotated[
            str | None,
            Field(
                description=(
                    "Free-form TICKET type label (an Object-Manager field, "
                    "unrelated to article_type)."
                )
            ),
        ] = None,
        tags: Annotated[
            str | None,
            Field(description="Comma-separated tag list, e.g. 'urgent,external'"),
        ] = None,
    ) -> Any:
        payload: dict[str, Any] = {
            "title": title,
            "group": group,
            "customer": customer,
            "article": {
                "body": article_body,
                "type": article_type,
                "internal": article_internal,
            },
        }
        if priority_id is not None:
            payload["priority_id"] = priority_id
        if state_id is not None:
            payload["state_id"] = state_id
        if owner_id is not None:
            payload["owner_id"] = owner_id
        if ticket_type is not None:
            payload["type"] = ticket_type
        if tags is not None:
            payload["tags"] = tags
        return await ctx.request("POST", "/tickets", json=payload)

    @mcp.tool(
        name="update_ticket",
        description=(
            "Update fields on an existing Zammad ticket. Only the supplied "
            "fields are changed; omit a field to leave it untouched. To add "
            "a reply or note, use `reply_to_customer` / `add_internal_note`."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=True,  # overwrites existing field values
            idempotentHint=True,
            openWorldHint=True,
        ),
    )
    async def update_ticket(
        ticket_id: Annotated[int, Field(ge=1)],
        title: Annotated[str | None, Field(max_length=255)] = None,
        state_id: Annotated[int | None, Field(ge=1)] = None,
        priority_id: Annotated[int | None, Field(ge=1)] = None,
        owner_id: Annotated[int | None, Field(ge=1)] = None,
        group_id: Annotated[int | None, Field(ge=1)] = None,
        customer_id: Annotated[int | None, Field(ge=1)] = None,
        ticket_type: Annotated[
            str | None, Field(description="Free-form ticket type label")
        ] = None,
    ) -> Any:
        payload: dict[str, Any] = {}
        if title is not None:
            payload["title"] = title
        if state_id is not None:
            payload["state_id"] = state_id
        if priority_id is not None:
            payload["priority_id"] = priority_id
        if owner_id is not None:
            payload["owner_id"] = owner_id
        if group_id is not None:
            payload["group_id"] = group_id
        if customer_id is not None:
            payload["customer_id"] = customer_id
        if ticket_type is not None:
            payload["type"] = ticket_type
        if not payload:
            raise ToolError(
                "update_ticket needs at least one field to change. Pass e.g. "
                "state_id, priority_id or owner_id."
            )
        return await ctx.request("PUT", f"/tickets/{ticket_id}", json=payload)

    @mcp.tool(
        name="delete_ticket",
        description=(
            "Permanently delete a Zammad ticket and all its articles. "
            "Restricted to users with the appropriate Zammad permission "
            "(typically admins or the ticket owner). USE WITH CAUTION - "
            "this cannot be undone."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=False,
            openWorldHint=True,
        ),
    )
    async def delete_ticket(
        ticket_id: Annotated[int, Field(ge=1)],
    ) -> dict[str, Any]:
        await ctx.request("DELETE", f"/tickets/{ticket_id}")
        return {"deleted": True, "ticket_id": ticket_id}

    return 6
