"""
Ticket relationship tools - merges, links, and same-customer context.

Endpoints (all under /api/v1/):
  PUT    /ticket_merge/{source_ticket_id}/{target_ticket_number}  merge two tickets
  GET    /ticket_related/{ticket_id}                              same-customer + recent
  GET    /ticket_customer?customer_id={id}                        one customer's tickets
  GET    /links?link_object=Ticket&link_object_value={id}         links on a ticket
  POST   /links/add                                               create a link
  DELETE /links/remove                                            delete a link

Permission: the merge and both context routes are listed in
Controllers::TicketsControllerPolicy and need `ticket.agent`; the merge service
additionally demands agent UPDATE access on both tickets. The /links index
needs only authentication - Link.list filters the result down to what the
caller may read - while /links/add and /links/remove need agent update access
on the target ticket (add also needs read access on the source).

ID vs NUMBER - the trap this module exists to defuse
----------------------------------------------------
Three of these routes mix Zammad's two ticket identifiers, and each mixes them
differently:

  ticket_merge  source = ID      target = NUMBER
  links/add     source = NUMBER  target = ID
  links/remove  source = ID      target = ID

Zammad resolves each with a plain find_by, so the wrong identifier does not
raise - it simply matches nothing. Every parameter below is therefore named
after the identifier it carries (`source_ticket_id` vs `source_ticket_number`),
because the name is the only thing standing between the model and a silent
no-op.

Success statuses on failed writes
---------------------------------
Two of these endpoints answer a failed write with a success status:

  * ticket_merge renders {"result": "failed", "message": ...} with HTTP 200
    when either ticket lookup misses (tickets_controller#ticket_merge). Only a
    genuine conflict - merging into an already-merged ticket, or into itself -
    reaches the 422 path.
  * links/add renders the record with HTTP 201 even when the uniqueness
    validation rejected it: Link.create returns a truthy unsaved object and the
    controller never asks whether it persisted, so the body carries "id": null.

Both are parsed here and raised as ToolError. links/remove has the same shape
(HTTP 201 and an empty array when nothing matched) but the state the caller
wanted - no link - holds either way, so it reports a count instead of raising.

No pagination: none of these endpoints paginate. ticket_related is capped at 6
same-customer plus 8 recently-viewed tickets, ticket_customer at 15 per state
category, and /links returns every link there is. There is no page parameter to
send.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any

from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from . import ToolContext

if TYPE_CHECKING:
    from fastmcp import FastMCP

# Link::Type is a lookup table Zammad extends on demand, so these three are a
# convention rather than an enum - see _validate_link_type.
LINK_TYPES = ("normal", "parent", "child")

# Only tickets. Link::Object accepts 'KnowledgeBase::Answer::Translation' too,
# but link_object_get CREATES whatever name it is handed, so exposing the field
# would let one typo add a permanent junk object type to the instance.
LINK_OBJECT = "Ticket"


def _validate_link_type(link_type: str) -> None:
    """Reject an unknown link type before Zammad silently invents it.

    Link.link_type_get is ``find_by(name:) || create(name:)``, so a misspelled
    type does not fail the request - it permanently adds a new link type to the
    instance and files the link under it, where nobody will look for it.
    """
    if link_type not in LINK_TYPES:
        raise ToolError(
            f"link_type must be one of {', '.join(LINK_TYPES)} (got {link_type!r}). "
            "Zammad would CREATE an unknown type rather than reject it, so this is "
            "checked here instead."
        )


def register(mcp: FastMCP, ctx: ToolContext) -> int:
    read_only = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=True)

    @mcp.tool(
        name="merge_tickets",
        description=(
            "Merge one ticket into another: every article moves to the target "
            "ticket and the source is emptied and closed as 'merged'. MIND THE "
            "TWO DIFFERENT IDENTIFIERS - `source_ticket_id` is the numeric ID "
            "of the ticket that DISAPPEARS, `target_ticket_number` is the "
            "human ticket NUMBER (the digit string quoted in mail subjects, "
            "e.g. '67001') of the ticket that survives. Swapping them makes "
            "Zammad merge the wrong way round or match nothing at all. "
            "Requires 'ticket.agent' with update access on BOTH tickets. There "
            "is no un-merge."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=True,  # empties and closes the source ticket
            idempotentHint=False,
            openWorldHint=True,
        ),
    )
    async def merge_tickets(
        source_ticket_id: Annotated[
            int,
            Field(ge=1, description="Numeric ID of the ticket to merge AWAY (it is emptied)"),
        ],
        target_ticket_number: Annotated[
            str,
            Field(
                min_length=1,
                description="Ticket NUMBER (not ID) of the ticket that keeps the articles",
            ),
        ],
    ) -> Any:
        number = target_ticket_number.strip()
        # '#' would truncate the URL at a fragment, and 'Ticket#67001' is the
        # shape an LLM copies out of a subject line - both mean the caller has
        # a display label rather than the number itself.
        if "#" in number or any(char.isspace() for char in number):
            raise ToolError(
                f"target_ticket_number must be the bare ticket number, e.g. '67001' - "
                f"got {target_ticket_number!r}. Strip any ticket hook prefix and "
                "whitespace; if you only have the ticket's ID, look up its number with "
                "get_ticket first."
            )
        result = await ctx.request("PUT", f"/ticket_merge/{source_ticket_id}/{number}")
        # A lookup miss is reported as HTTP 200 with result='failed', so an
        # unchecked call would report a merge that never happened.
        if isinstance(result, dict) and result.get("result") == "failed":
            raise ToolError(
                f"Zammad refused the merge: {result.get('message') or 'no reason given'} "
                f"(reported as HTTP 200 with result='failed'). Verify that "
                f"source_ticket_id={source_ticket_id} is a ticket ID and that "
                f"target_ticket_number={number!r} is a ticket NUMBER - the two are not "
                "interchangeable."
            )
        return result

    @mcp.tool(
        name="find_related_tickets",
        description=(
            "Fetch Zammad's own 'related tickets' context for a ticket: OTHER "
            "OPEN tickets belonging to the SAME CUSTOMER (max 6), plus the "
            "tickets the calling agent viewed most recently (max 8). Be "
            "precise about what this is NOT - there is no similarity or "
            "full-text matching, closed tickets never appear, and the "
            "recently-viewed list is the caller's own browsing history with no "
            "connection to this ticket. For genuine similarity search use "
            "`search_tickets`. The response holds ID lists plus an assets "
            "object with the full ticket records keyed by ID. Requires "
            "'ticket.agent'."
        ),
        annotations=read_only,
    )
    async def find_related_tickets(
        ticket_id: Annotated[int, Field(ge=1)],
    ) -> Any:
        return await ctx.request("GET", f"/ticket_related/{ticket_id}")

    @mcp.tool(
        name="list_customer_tickets",
        description=(
            "List one customer's tickets as two ID lists, open and closed (max "
            "15 each), with the full ticket records in an assets object keyed "
            "by ID. Use it to judge whether a request is a recurring problem. "
            "`customer_id` is the numeric USER id of the customer, never an "
            "e-mail address - resolve an address with `search_users` first. "
            "Requires 'ticket.agent'."
        ),
        annotations=read_only,
    )
    async def list_customer_tickets(
        customer_id: Annotated[int, Field(ge=1, description="Numeric user ID of the customer")],
    ) -> Any:
        return await ctx.request("GET", "/ticket_customer", params={"customer_id": customer_id})

    @mcp.tool(
        name="list_ticket_links",
        description=(
            "List the objects explicitly linked to a ticket - other tickets, "
            "and knowledge base answers. Each entry gives the linked object's "
            "class, its ID and a link type of 'normal', 'parent' or 'child', "
            "with the full records in an assets object. The type is stated "
            "FROM THE QUERIED TICKET'S POINT OF VIEW: an entry typed 'child' "
            "means the listed ticket is a child of this one. Any authenticated "
            "user may call this - Zammad drops links to objects the caller "
            "cannot read."
        ),
        annotations=read_only,
    )
    async def list_ticket_links(
        ticket_id: Annotated[int, Field(ge=1)],
    ) -> Any:
        return await ctx.request(
            "GET",
            "/links",
            params={"link_object": LINK_OBJECT, "link_object_value": ticket_id},
        )

    @mcp.tool(
        name="link_tickets",
        description=(
            "Link two tickets to each other. THE TWO TICKETS ARE IDENTIFIED "
            "DIFFERENTLY and the values are not interchangeable: "
            "`source_ticket_number` is a ticket NUMBER (the digit string from "
            "the mail subject) while `target_ticket_id` is a numeric ID. "
            "`link_type` says what the SOURCE is TO the target - 'parent' "
            "makes the source ticket the parent of the target, 'child' the "
            "reverse, 'normal' is a plain see-also link. An unknown source "
            "number is rejected with 422, but a duplicate link is reported as "
            "success and raised as an error here. Requires 'ticket.agent' with "
            "update access on the target and read access on the source. Undo "
            "with `unlink_tickets`, which identifies the source by ID instead."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,  # additive: adds a relation, changes nothing else
            idempotentHint=False,
            openWorldHint=True,
        ),
    )
    async def link_tickets(
        source_ticket_number: Annotated[
            str, Field(min_length=1, description="Ticket NUMBER of the source ticket")
        ],
        target_ticket_id: Annotated[
            int, Field(ge=1, description="Numeric ID of the target ticket")
        ],
        link_type: Annotated[
            str,
            Field(description="'normal', 'parent' or 'child' - the SOURCE's role"),
        ] = "normal",
    ) -> Any:
        _validate_link_type(link_type)
        result = await ctx.request(
            "POST",
            "/links/add",
            json={
                "link_type": link_type,
                "link_object_source": LINK_OBJECT,
                "link_object_source_number": source_ticket_number,
                "link_object_target": LINK_OBJECT,
                "link_object_target_value": target_ticket_id,
            },
        )
        # Link.create hands back a truthy but unsaved record when the uniqueness
        # validator rejects the row, and the controller renders that as 201. A
        # null id is the only evidence that nothing was written.
        if isinstance(result, dict) and result.get("id") is None:
            raise ToolError(
                f"Zammad answered 201 but stored no link (the returned record has a null "
                f"id), which it only does when the link already exists. Ticket "
                f"{source_ticket_number} and ticket {target_ticket_id} are already linked "
                f"as {link_type!r} - call list_ticket_links to confirm."
            )
        return result

    @mcp.tool(
        name="unlink_tickets",
        description=(
            "Remove a link between two tickets. BOTH tickets are identified by "
            "numeric ID here - `source_ticket_id` is an ID, unlike the number "
            "`link_tickets` takes. `link_type` must match the link being "
            "removed; Zammad matches on the type as well, so the wrong one "
            "deletes nothing. Removing a link that does not exist is not an "
            "error, Zammad answers 201 either way. The returned removed count "
            "is Zammad's own and UNDERCOUNTS: it only reports rows deleted by "
            "its second, mirrored query, so 0 does not prove nothing was "
            "deleted - confirm with `list_ticket_links`. Requires "
            "'ticket.agent' with update access on the target ticket."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=True,  # removes existing state
            idempotentHint=True,
            openWorldHint=True,
        ),
    )
    async def unlink_tickets(
        source_ticket_id: Annotated[
            int, Field(ge=1, description="Numeric ID (NOT number) of the source ticket")
        ],
        target_ticket_id: Annotated[
            int, Field(ge=1, description="Numeric ID of the target ticket")
        ],
        link_type: Annotated[
            str,
            Field(description="'normal', 'parent' or 'child' - must match the stored link"),
        ] = "normal",
    ) -> dict[str, Any]:
        _validate_link_type(link_type)
        result = await ctx.request(
            "DELETE",
            "/links/remove",
            json={
                "link_type": link_type,
                "link_object_source": LINK_OBJECT,
                "link_object_source_value": source_ticket_id,
                "link_object_target": LINK_OBJECT,
                "link_object_target_value": target_ticket_id,
            },
        )
        # Link.remove runs two destroy_all calls - the given orientation, then
        # the mirrored one - and returns only the second. A link stored in the
        # orientation the caller passed is therefore deleted and reported as 0.
        removed = len(result) if isinstance(result, list) else 0
        return {
            "removed": removed,
            "source_ticket_id": source_ticket_id,
            "target_ticket_id": target_ticket_id,
            "link_type": link_type,
        }

    return 6
