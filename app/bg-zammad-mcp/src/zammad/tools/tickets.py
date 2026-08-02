"""
Ticket tools - the core of the Zammad MCP surface.

Endpoints exercised (all under /api/v1/):
  GET    /tickets                          paginated list (ID ascending!)
  GET    /tickets/search?query=...         full-text search (needs Elasticsearch)
  POST   /tickets/search                   structured condition search (index-free)
  GET    /tickets/{id}                     get one
  GET    /tickets/{id}?all=true            ticket + articles + related records
  POST   /tickets                          create
  PUT    /tickets/{id}                     update
  PUT    /tickets/{id}/update_title        rename, bypassing Core Workflow
  PUT    /tickets/{id}/update_customer     reassign, bypassing Core Workflow
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

from ..projection import TICKET_FIELDS, parse_fields, project_many
from . import ToolContext

if TYPE_CHECKING:
    from fastmcp import FastMCP

# Zammad's own server-side ceiling: paginate_with(max: 200, default: 50) on the
# search endpoints, paginate_with(max: 100) on the plain index.
SEARCH_MAX_LIMIT = 200
INDEX_MAX_PER_PAGE = 100

# One vocabulary for article visibility across every tool that can create an
# article. It used to be a bare `internal` boolean whose default differed per
# tool — False when creating a ticket, True in bulk update, and hardcoded True
# with no parameter at all on update_ticket. That is the same trap the article
# tools were split in two to close, reintroduced through a side door: a model
# writing "resolved, we replaced the toner" as part of closing a ticket had no
# way to know the customer would never see it. An enum with no default forces
# the choice to be stated wherever it is available at all.
VISIBILITY = ("customer_visible", "internal")


def _article(body: str, visibility: str, article_type: str = "note") -> dict[str, Any]:
    """Build a ticket-article payload from the shared visibility vocabulary."""
    if visibility not in VISIBILITY:
        raise ToolError(
            f"article_visibility must be one of {', '.join(VISIBILITY)} "
            f"(got {visibility!r}). 'customer_visible' is what the customer reads; "
            "'internal' is agents-only."
        )
    return {"body": body, "type": article_type, "internal": visibility == "internal"}


def _reject_name_and_id_conflicts(**pairs: Any) -> None:
    """Refuse a call that supplies both the name and the ID form of a field.

    Zammad accepts both and silently picks one (the ``_id`` form wins in
    ``CanAssociations``), so a caller that sets ``state='open'`` and
    ``state_id=4`` gets a result with no error and no indication that half of
    what they asked for was discarded. Better to fail with a sentence that says
    which two arguments disagree.
    """
    for name in ("state", "priority"):
        if pairs.get(name) is not None and pairs.get(f"{name}_id") is not None:
            raise ToolError(
                f"Pass either {name} or {name}_id, not both — Zammad would "
                f"silently apply one and drop the other. Use {name} for a name "
                f"like 'open', {name}_id for a numeric ID."
            )


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
        fields: Annotated[
            str | None,
            Field(
                description=(
                    "Comma-separated whitelist of fields to keep, e.g. "
                    "'id,number,title,state'. Overrides the default projection."
                )
            ),
        ] = None,
        full: Annotated[
            bool,
            Field(description="Return Zammad's untrimmed records (large)"),
        ] = False,
    ) -> Any:
        payload = await ctx.request(
            "GET",
            "/tickets",
            params={"page": page, "per_page": per_page, "expand": str(expand).lower()},
        )
        return project_many(payload, parse_fields(fields) or TICKET_FIELDS, full=full)

    @mcp.tool(
        name="search_tickets",
        description=(
            "Full-text search Zammad tickets, backed by Elasticsearch. "
            "Supports field-scoped queries such as `state.name:open`, "
            "`owner.email:a@b.c`, `organization.name:ACME` and "
            "`created_at:>=now-7d`, and they can be combined with AND / OR. "
            "This is the right tool for open-ended questions about tickets. "
            "Results are paginated: pass `page` to go beyond the first "
            "`limit` results, and check total_count to see whether more "
            "matched. If a field-scoped query returns an empty list on an "
            "instance without a search index, fall back to "
            "`search_tickets_by_condition`, which never needs one."
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
        fields: Annotated[
            str | None,
            Field(
                description=(
                    "Comma-separated whitelist of fields to keep, e.g. "
                    "'id,number,title,state'. Overrides the default projection."
                )
            ),
        ] = None,
        full: Annotated[
            bool,
            Field(description="Return Zammad's untrimmed records (large)"),
        ] = False,
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
        payload = await ctx.request("GET", "/tickets/search", params=params)
        return project_many(payload, parse_fields(fields) or TICKET_FIELDS, full=full)

    @mcp.tool(
        name="search_tickets_by_condition",
        description=(
            "Search tickets with a STRUCTURED condition instead of free text. "
            "Use it when the filter is exact and mechanical - a fixed set of "
            "states, one organization, a date window - where a phrased query "
            "could be interpreted loosely. It is also index-independent, so it "
            "still works if the search index is rebuilding. The condition is Zammad's "
            "selector format: a map of attribute to {operator, value}, e.g. "
            "{\"ticket.state_id\": {\"operator\": \"is\", \"value\": [1, 2, 3]}, "
            "\"ticket.owner_id\": {\"operator\": \"is\", \"value\": "
            "[\"current_user.id\"]}}. Common operators: 'is', 'is not', "
            "'contains', 'starts with', 'before (relative)', 'within last "
            "(relative)'."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=True, destructiveHint=False, openWorldHint=True
        ),
    )
    async def search_tickets_by_condition(
        condition: Annotated[
            dict[str, Any],
            Field(description="Zammad selector condition (see the description)"),
        ],
        page: Annotated[int, Field(ge=1, description="1-indexed page number")] = 1,
        limit: Annotated[int, Field(ge=1, le=SEARCH_MAX_LIMIT)] = 25,
        sort_by: Annotated[str | None, Field(description="e.g. 'updated_at'")] = None,
        order_by: Annotated[str | None, Field(description="'asc' or 'desc'")] = None,
        expand: Annotated[bool, Field(description="Inline names instead of IDs")] = True,
        fields: Annotated[
            str | None,
            Field(
                description=(
                    "Comma-separated whitelist of fields to keep, e.g. "
                    "'id,number,title,state'. Overrides the default projection."
                )
            ),
        ] = None,
        full: Annotated[
            bool,
            Field(description="Return Zammad's untrimmed records (large)"),
        ] = False,
    ) -> Any:
        if not condition:
            raise ToolError(
                "search_tickets_by_condition needs a non-empty condition. For a "
                "plain keyword search use search_tickets instead."
            )
        # POST rather than GET: the condition is a nested object, and Zammad
        # registers both verbs on /tickets/search for exactly this reason.
        payload: dict[str, Any] = {
            "condition": condition,
            "page": page,
            "limit": limit,
            "expand": expand,
            "with_total_count": True,
        }
        if sort_by:
            payload["sort_by"] = sort_by
        if order_by:
            payload["order_by"] = order_by
        result = await ctx.request("POST", "/tickets/search", json=payload)
        return project_many(result, parse_fields(fields) or TICKET_FIELDS, full=full)

    @mcp.tool(
        name="count_tickets",
        description=(
            "Return ONLY the number of tickets matching a query, without the "
            "tickets themselves. Use this for 'how many ...' questions - it costs "
            "a fraction of the tokens that `search_tickets` would."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=True, destructiveHint=False, openWorldHint=True
        ),
    )
    async def count_tickets(
        query: Annotated[str, Field(min_length=1, description="Zammad search query")],
    ) -> Any:
        return await ctx.request(
            "GET",
            "/tickets/search",
            params={"query": query, "only_total_count": "true"},
        )

    @mcp.tool(
        name="get_ticket",
        description=(
            "Fetch a single Zammad ticket by its numeric ID. Returns the ticket "
            "fields only - use `get_ticket_full` to get the ticket together with "
            "its articles in one call."
        ),
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
        name="get_ticket_full",
        description=(
            "Fetch a ticket together with EVERYTHING needed to understand it in a "
            "single call: the ticket, every article the caller may see, and the "
            "related users, organization, group, state and priority records. This "
            "is the tool to reach for when asked to read, summarise or answer a "
            "ticket - it replaces a `get_ticket` plus a `list_ticket_articles` "
            "round trip. Returns Zammad's asset structure, where related records "
            "are grouped by type and ID."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=True, destructiveHint=False, openWorldHint=True
        ),
    )
    async def get_ticket_full(
        ticket_id: Annotated[int, Field(ge=1)],
    ) -> Any:
        # Zammad's show action checks expand, then full, then all, and takes the
        # FIRST that is set (app/controllers/tickets_controller.rb#show). Sending
        # expand=true here - which every other tool in this module does - would
        # silently win and return the plain ticket without any articles.
        return await ctx.request("GET", f"/tickets/{ticket_id}", params={"all": "true"})

    @mcp.tool(
        name="create_ticket",
        description=(
            "Create a new Zammad ticket. Requires `title`, `group` (group name "
            "or ID), `customer` (customer e-mail or user ID), and an initial "
            "`article_body`. The opening article is customer-visible by "
            "default, matching how a ticket raised by a customer looks - pass "
            "`article_visibility='internal'` for a ticket you are raising "
            "purely for internal tracking. Every association accepts either a "
            "name or an ID: `group`/`group_id`, `customer`/`customer_id`, "
            "`state`/`state_id`, `priority`/`priority_id`. Pass one form or "
            "the other, never both."
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
            str,
            Field(
                description=(
                    "Group NAME, e.g. 'Support'. Zammad resolves this by name only "
                    "- a numeric ID here is looked up as a name and fails. Use "
                    "`group_id` if you have the ID."
                )
            ),
        ],
        customer: Annotated[
            str,
            Field(
                description=(
                    "Customer E-MAIL address. Resolved by e-mail (or login) only; "
                    "use `customer_id` if you have the numeric user ID."
                )
            ),
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
        article_visibility: Annotated[
            str,
            Field(
                description=(
                    "Who may read the opening article: 'customer_visible' "
                    "(default - matches a ticket the customer raised themselves) "
                    "or 'internal' for a ticket you are tracking internally."
                )
            ),
        ] = "customer_visible",
        group_id: Annotated[
            int | None, Field(ge=1, description="Group by ID, instead of `group`")
        ] = None,
        customer_id: Annotated[
            int | None, Field(ge=1, description="Customer by ID, instead of `customer`")
        ] = None,
        state: Annotated[
            str | None, Field(description="State by NAME, e.g. 'open'")
        ] = None,
        priority: Annotated[
            str | None, Field(description="Priority by NAME, e.g. '3 high'")
        ] = None,
        priority_id: Annotated[int | None, Field(ge=1)] = None,
        state_id: Annotated[int | None, Field(ge=1)] = None,
        owner_id: Annotated[int | None, Field(ge=1)] = None,
        pending_time: Annotated[
            str | None,
            Field(
                description=(
                    "ISO 8601 timestamp, required by Zammad when the ticket is "
                    "created directly into a 'pending ...' state."
                )
            ),
        ] = None,
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
        extra_fields: Annotated[
            dict[str, Any] | None,
            Field(
                description=(
                    "Custom Object-Manager attributes, as a name/value map. Use "
                    "`list_ticket_fields` to discover what this instance defines."
                )
            ),
        ] = None,
    ) -> Any:
        # Same merge order as update_ticket: custom attributes first, so an
        # explicit named argument always wins over a same-named custom field.
        _reject_name_and_id_conflicts(
            state=state, state_id=state_id, priority=priority, priority_id=priority_id
        )
        payload: dict[str, Any] = dict(extra_fields or {})
        payload |= {
            "title": title,
            "article": _article(article_body, article_visibility, article_type),
        }
        # Zammad resolves `group`/`customer` by name and `*_id` by id, and the
        # _id form wins when both are present (CanAssociations). Send only what
        # the caller actually chose so that precedence never has to be guessed.
        if group_id is not None:
            payload["group_id"] = group_id
        else:
            payload["group"] = group
        if customer_id is not None:
            payload["customer_id"] = customer_id
        else:
            payload["customer"] = customer
        for key, value in (
            ("state", state),
            ("state_id", state_id),
            ("priority", priority),
            ("priority_id", priority_id),
            ("owner_id", owner_id),
            ("type", ticket_type),
            ("tags", tags),
            ("pending_time", pending_time),
        ):
            if value is not None:
                payload[key] = value
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
        state: Annotated[
            str | None,
            Field(
                description=(
                    "State by NAME, e.g. 'open', 'closed', 'pending reminder'. "
                    "Zammad resolves the name; use `list_ticket_states` if unsure."
                )
            ),
        ] = None,
        state_id: Annotated[int | None, Field(ge=1)] = None,
        pending_time: Annotated[
            str | None,
            Field(
                description=(
                    "When a 'pending ...' state should come back up, as an ISO 8601 "
                    "timestamp (e.g. '2026-08-12T09:00:00Z'). REQUIRED by Zammad "
                    "whenever the state is a pending one - without it the update is "
                    "rejected."
                )
            ),
        ] = None,
        priority: Annotated[
            str | None, Field(description="Priority by NAME, e.g. '3 high'")
        ] = None,
        priority_id: Annotated[int | None, Field(ge=1)] = None,
        owner_id: Annotated[int | None, Field(ge=1)] = None,
        group_id: Annotated[int | None, Field(ge=1)] = None,
        customer_id: Annotated[int | None, Field(ge=1)] = None,
        ticket_type: Annotated[
            str | None, Field(description="Free-form ticket type label")
        ] = None,
        replace_tags: Annotated[
            str | None,
            Field(
                description=(
                    "REPLACES the ticket's entire tag list with this comma-separated "
                    "set, discarding any tag not listed. To add or remove a single "
                    "tag without touching the others, use `add_tag` / `remove_tag`."
                )
            ),
        ] = None,
        article_body: Annotated[
            str | None,
            Field(
                description=(
                    "Optional article to add in the SAME request as the field "
                    "changes, so 'close this with a note' is one atomic update. "
                    "Its audience is set by `article_visibility`."
                )
            ),
        ] = None,
        article_visibility: Annotated[
            str,
            Field(
                description=(
                    "Who may read `article_body`: 'internal' (default, agents only) "
                    "or 'customer_visible'. For a real reply prefer "
                    "`reply_to_customer`, which also sends it."
                )
            ),
        ] = "internal",
        extra_fields: Annotated[
            dict[str, Any] | None,
            Field(
                description=(
                    "Custom Object-Manager attributes to set, as a name/value map. "
                    "Use `list_ticket_fields` to discover which exist on this "
                    "instance and what values they accept."
                )
            ),
        ] = None,
    ) -> Any:
        # extra_fields goes in first so a named argument always wins over a
        # same-named custom attribute - an explicit parameter is the stronger
        # statement of intent, and this keeps the merge order predictable.
        _reject_name_and_id_conflicts(
            state=state, state_id=state_id, priority=priority, priority_id=priority_id
        )
        payload: dict[str, Any] = dict(extra_fields or {})
        if title is not None:
            payload["title"] = title
        if state is not None:
            payload["state"] = state
        if state_id is not None:
            payload["state_id"] = state_id
        if pending_time is not None:
            payload["pending_time"] = pending_time
        if priority is not None:
            payload["priority"] = priority
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
        if replace_tags is not None:
            payload["tags"] = replace_tags
        if article_body is not None:
            payload["article"] = _article(article_body, article_visibility)
        if not payload:
            raise ToolError(
                "update_ticket needs at least one field to change. Pass e.g. "
                "state, priority, owner_id or extra_fields."
            )
        return await ctx.request("PUT", f"/tickets/{ticket_id}", json=payload)

    @mcp.tool(
        name="update_ticket_title",
        description=(
            "Rename a ticket. Zammad exposes a dedicated endpoint for this that "
            "bypasses Core Workflow restrictions, so use it rather than "
            "`update_ticket` when only the title changes - a generic update can be "
            "silently blocked by a workflow rule while this one succeeds."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=True,  # overwrites the existing title
            idempotentHint=True,
            openWorldHint=True,
        ),
    )
    async def update_ticket_title(
        ticket_id: Annotated[int, Field(ge=1)],
        title: Annotated[str, Field(min_length=1, max_length=255)],
    ) -> Any:
        return await ctx.request(
            "PUT", f"/tickets/{ticket_id}/update_title", json={"title": title}
        )

    @mcp.tool(
        name="reassign_ticket_customer",
        description=(
            "Move a ticket to a different customer (and optionally organization). "
            "Like `update_ticket_title` this uses Zammad's dedicated endpoint, "
            "which bypasses Core Workflow restrictions that can silently block the "
            "same change made through `update_ticket`."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=True,  # overwrites the existing customer
            idempotentHint=True,
            openWorldHint=True,
        ),
    )
    async def reassign_ticket_customer(
        ticket_id: Annotated[int, Field(ge=1)],
        customer_id: Annotated[int, Field(ge=1, description="New customer's user ID")],
        organization_id: Annotated[
            int | None,
            Field(ge=1, description="New organization ID, if it changes too"),
        ] = None,
    ) -> Any:
        payload: dict[str, Any] = {"customer_id": customer_id}
        if organization_id is not None:
            payload["organization_id"] = organization_id
        return await ctx.request(
            "PUT", f"/tickets/{ticket_id}/update_customer", json=payload
        )

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

    return 11
