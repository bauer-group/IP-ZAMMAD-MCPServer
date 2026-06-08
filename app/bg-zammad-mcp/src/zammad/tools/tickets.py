"""
Ticket tools - the core of the Zammad MCP surface.

Endpoints exercised (all under /api/v1/):
  GET    /tickets                          paginated list
  GET    /tickets/search?query=...         full-text search
  GET    /tickets/{id}                     get one
  POST   /tickets                          create
  PUT    /tickets/{id}                     update
  DELETE /tickets/{id}                     delete (Admin/owner-only)

All tools forward the authenticated user's bearer token, so Zammad's own
permission system gates which tickets the caller can see / edit / delete.

Hint annotations
----------------
Read-only tools (`list_*`, `search_*`, `get_*`) are marked
`readOnlyHint=True` so MCP clients can auto-run them without prompting.
Mutating tools are marked `destructiveHint=True`; `delete_ticket` is
flagged as well as `idempotentHint=False` for emphasis.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any

from mcp.types import ToolAnnotations
from pydantic import Field

from . import ToolContext

if TYPE_CHECKING:
    from fastmcp import FastMCP


def register(mcp: FastMCP, ctx: ToolContext) -> int:
    """Register ticket tools and return the count."""

    @mcp.tool(
        name="list_tickets",
        description=(
            "List Zammad tickets, paginated. Returns the most recent tickets first "
            "by default. Use `search_tickets` for full-text filtering."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=True, destructiveHint=False, openWorldHint=True
        ),
    )
    async def list_tickets(
        page: Annotated[int, Field(ge=1, description="1-indexed page number")] = 1,
        per_page: Annotated[
            int, Field(ge=1, le=100, description="Items per page (max 100)")
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
            "Full-text search Zammad tickets. The query supports Zammad's "
            "Lucene-like search syntax (e.g. `state:open priority:3 normal`, "
            "`owner.email:agent@example.com`, `created_at:>=now-7d`)."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=True, destructiveHint=False, openWorldHint=True
        ),
    )
    async def search_tickets(
        query: Annotated[str, Field(min_length=1, description="Zammad search query")],
        limit: Annotated[int, Field(ge=1, le=100)] = 25,
        sort_by: Annotated[
            str | None,
            Field(description="Sort field, e.g. 'created_at' or 'updated_at'"),
        ] = None,
        order_by: Annotated[str | None, Field(description="'asc' or 'desc'")] = None,
        expand: Annotated[bool, Field(description="Inline names instead of IDs")] = True,
    ) -> Any:
        params: dict[str, Any] = {
            "query": query,
            "limit": limit,
            "expand": str(expand).lower(),
        }
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
            "`article` body. Other fields are optional but commonly useful: "
            "`priority_id`, `state_id`, `owner_id`, `type`, `tags`."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=True,
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
                    "Article type: 'note' (internal), 'email' (out-/inbound), "
                    "'phone', 'web', 'chat'. Defaults to 'note'."
                )
            ),
        ] = "note",
        article_internal: Annotated[
            bool, Field(description="If True, article is hidden from customers")
        ] = True,
        priority_id: Annotated[int | None, Field(ge=1)] = None,
        state_id: Annotated[int | None, Field(ge=1)] = None,
        owner_id: Annotated[int | None, Field(ge=1)] = None,
        ticket_type: Annotated[
            str | None,
            Field(alias="type", description="Free-form ticket type label"),
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
            "a reply or note, use `create_ticket_article` instead."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=True,
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
        ticket_type: Annotated[str | None, Field(alias="type")] = None,
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
            raise ValueError("update_ticket called with no fields to update")
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
