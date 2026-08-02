"""
Bulk ticket tools - one change set applied to many tickets in one transaction.

Endpoints (all under /api/v1/):
  POST /tickets/mass_update    change attributes on many tickets at once, and
                               optionally append one article to each. Needs
                               ticket.agent with CHANGE access to every listed
                               ticket's group - ``TicketsMassController`` runs
                               ``authorize!(ticket, :agent_update_access?)``
                               per ticket rather than a controller-wide policy,
                               so there is no single permission to check up
                               front. This route is undocumented upstream; the
                               shapes below come from the controller source.

Not paginated - there is nothing to page through, so no page argument.

Transaction semantics
---------------------
The controller wraps the whole batch in one ``ActiveRecord::Base.transaction``.
The first ticket that fails - because the caller may not edit it, or because
its new values fail validation - triggers a rollback, so a partial failure
leaves NOTHING changed. That is the useful behaviour, but it makes the failing
ticket the only actionable piece of information in the response.

Why the 422 is unwrapped here
-----------------------------
On failure Zammad renders ``{"error": true, "ticket_id": 5}``. ``error`` is the
boolean ``true``, not a message, so ``errors.from_status`` picks it up as the
detail and the model is told the request failed with "True" - which names
neither the problem nor the ticket. This is one of the rare cases where
catching the typed error adds real value: we re-raise as a ``ToolError`` that
says which ticket rolled the batch back and what to do about it.

Blank values are dropped by Zammad
----------------------------------
``clean_update_params`` runs ``compact_blank!`` over the attributes, so an
empty string or null is silently removed from the change set. Mass update can
therefore SET a field but never CLEAR one back to empty.
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

# Zammad imposes no server-side ceiling on ticket_ids, which is precisely why we
# do: every listed ticket is loaded, authorized and saved inside one request and
# one database transaction, so a careless 5000-id call is a long-running lock on
# a production helpdesk. 100 keeps a batch comfortably inside a normal HTTP
# timeout and matches the pagination ceiling the rest of the surface uses.
MAX_BULK_TICKETS = 100

# Article channels that make sense for a batch. Zammad's article_create defaults
# to 'note' when no type is given; the others are the delivery channels an agent
# can legitimately record against many tickets at once.
BULK_ARTICLE_TYPES = ("note", "email", "phone", "web")


def register(mcp: FastMCP, ctx: ToolContext) -> int:
    @mcp.tool(
        name="update_tickets",
        description=(
            "Apply ONE change set to MANY tickets in a single Zammad "
            "transaction - the bulk action behind an overview's mass-edit bar, "
            "and far cheaper than looping `update_ticket`. List the targets in "
            "`ticket_ids` and the new values in `attributes`. Zammad converts "
            "association names for you, so both "
            '{"state_id": 4, "owner_id": 12} and {"state": "closed", "owner": '
            '"agent@example.com"} work; custom Object-Manager fields go in the '
            "same object - call `list_ticket_fields` first to learn their "
            "names. Needs ticket.agent with change access to EVERY listed "
            "ticket's group. SHARP EDGES: (1) all-or-nothing - one ticket you "
            "may not edit, or one value that fails validation, rolls the whole "
            "batch back and nothing is changed; (2) Zammad strips blank values, "
            "so this can set a field but never clear one - use `update_ticket` "
            "for that; (3) an unknown ticket id fails the call with HTTP 404. "
            "An optional note or reply can ride along via `article_body`. "
            "At most 100 ids per call."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=False,
            # Overwrites existing field values on up to 100 tickets at once.
            destructiveHint=True,
            # Re-sending the same attributes is a no-op, but a riding article is
            # appended again on every retry - so the call as a whole is not.
            idempotentHint=False,
            openWorldHint=True,
        ),
    )
    async def update_tickets(
        ticket_ids: Annotated[
            list[int],
            Field(
                min_length=1,
                description="Numeric IDs of the tickets to change (max 100 per call)",
            ),
        ],
        attributes: Annotated[
            dict[str, Any] | None,
            Field(
                description=(
                    "Field values to set on every listed ticket. IDs "
                    "(state_id, priority_id, owner_id, group_id) and "
                    "association names (state, priority, owner, group) are "
                    "both accepted, as are custom Object-Manager field names."
                )
            ),
        ] = None,
        article_body: Annotated[
            str | None,
            Field(
                min_length=1,
                description="Optional article text appended to every listed ticket",
            ),
        ] = None,
        article_type: Annotated[
            str,
            Field(
                description=(
                    "Delivery channel for article_body: 'note' (default, "
                    "records nothing outbound), 'email', 'phone' or 'web'."
                )
            ),
        ] = "note",
        article_visibility: Annotated[
            str,
            Field(
                description=(
                    "Who may read `article_body`: 'internal' (default, agents "
                    "only) or 'customer_visible'. Defaults to internal because a "
                    "customer-visible message multiplied by a batch is the "
                    "expensive mistake here. Same vocabulary as create_ticket "
                    "and update_ticket."
                )
            ),
        ] = "internal",
        article_subject: Annotated[str | None, Field(max_length=200)] = None,
    ) -> Any:
        if len(ticket_ids) > MAX_BULK_TICKETS:
            raise ToolError(
                f"update_tickets accepts at most {MAX_BULK_TICKETS} ticket ids per "
                f"call (got {len(ticket_ids)}); the whole batch runs in one Zammad "
                "transaction. Split ticket_ids into chunks of 100 and call the "
                "tool once per chunk."
            )
        if not attributes and article_body is None:
            raise ToolError(
                "update_tickets needs something to do: pass attributes (e.g. "
                '{"state": "closed"}), or article_body, or both. Zammad answers '
                "an empty change set with HTTP 200, so this call would silently "
                "have no effect."
            )
        if article_body is not None:
            if article_type not in BULK_ARTICLE_TYPES:
                raise ToolError(
                    f"article_type must be one of {', '.join(BULK_ARTICLE_TYPES)} "
                    f"(got {article_type!r})."
                )
            # The trap articles.py exists to make unreachable: an internal e-mail
            # is still DELIVERED to the customer and then hidden from them in
            # their own ticket view - here, once per ticket in the batch.
            if article_type == "email" and article_visibility == "internal":
                raise ToolError(
                    "article_type='email' with article_visibility='internal' sends "
                    "the mail to every listed ticket's customer and then hides it "
                    "from them in their own ticket view. Pass "
                    "article_visibility='customer_visible' to send a visible "
                    "reply, or article_type='note' to leave an agent-only note."
                )

        payload: dict[str, Any] = {"ticket_ids": ticket_ids}
        if attributes:
            payload["attributes"] = attributes
        if article_body is not None:
            article: dict[str, Any] = {
                "body": article_body,
                "type": article_type,
                "internal": article_visibility == "internal",
            }
            if article_subject is not None:
                article["subject"] = article_subject
            payload["article"] = article

        try:
            return await ctx.request("POST", "/tickets/mass_update", json=payload)
        except ZammadValidationError as err:
            failed_id = err.body.get("ticket_id")
            if failed_id is None:
                raise
            raise ToolError(
                f"Zammad rolled the entire batch back on ticket {failed_id} - no "
                "ticket was changed. Either the caller lacks change access to "
                "that ticket's group, or the new values fail its validation (a "
                "mandatory field left empty, or a Core Workflow restriction). "
                f"Drop or fix ticket {failed_id}, then retry."
            ) from err

    return 1
