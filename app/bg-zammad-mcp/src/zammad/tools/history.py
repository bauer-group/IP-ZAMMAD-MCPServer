"""
Audit + correction tools - what an agent reaches for when something went wrong.

Endpoints (all under /api/v1/):
  GET    /ticket_history/{ticket_id}
      Full change log of a ticket. Permission: ticket.agent (declared by
      Controllers::TicketsControllerPolicy) PLUS read access to the ticket's
      group (the action re-checks TicketPolicy#show?). Customers get 403.
  PUT    /ticket_articles/{article_id}
      Flip an article's `internal` flag. Permission: ticket.agent with CHANGE
      access on the ticket's group (Ticket::ArticlePolicy#update? delegates to
      TicketPolicy#agent_update_access?).
  DELETE /ticket_articles/{article_id}
      Delete an article. Permission: ticket.agent, and the caller must be the
      article's author - see the constraint list on the tool itself.
  GET    /users/me
      Resolve the caller's own user id (no special permission).
  GET    /mentions?mentionable_type=Ticket&mentionable_id={ticket_id}
      List a ticket's subscriptions. Permission: agent READ access on the
      ticket (Mention.mentionable? -> TicketPolicy#agent_read_access?).
  DELETE /mentions/{mention_id}
      Drop one subscription. Permission: the mention must belong to the caller
      (Controllers::MentionsControllerPolicy#destroy? -> mentioned_user?).

Why the history payload is reshaped
-----------------------------------
``Ticket#history_get(true)`` returns ``{"history": [...], "assets": {...}}``.
The assets half is a fully serialised object graph - every user, article,
group and checklist the log touches - and is routinely several times larger
than the log itself, while carrying nothing about *what changed*. We consume
it for one purpose (turning ``created_by_id`` into a human name) and then drop
it. Every entry in the log itself survives, in Zammad's own order
(``reorder(:created_at, :id)``, oldest first), because this is the tool that
answers "who closed this and when" - a missing row is a wrong answer.

Why unsubscribing takes three requests
--------------------------------------
Zammad models a ticket subscription as a ``Mention`` row with its own id, and
exposes only ``resources :mentions, only: %i[index create destroy]``. There is
no "unsubscribe me from ticket N" route, so the caller's own mention has to be
looked up first. ``notifications.py`` owns the read/subscribe half
(``list_ticket_subscribers``, ``subscribe_to_ticket``); the removal half lives
here with the rest of the correction tooling.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any

from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from ..projection import envelope
from . import ToolContext
from .tickets import VISIBILITY

if TYPE_CHECKING:
    from fastmcp import FastMCP


def _people(assets: Any) -> dict[str, Any]:
    """Index the assets blob's ``User`` section by stringified id.

    Zammad keys assets by integer id in Ruby; JSON turns those into strings, so
    a lookup with the raw integer ``created_by_id`` would always miss.
    """
    if not isinstance(assets, dict):
        return {}
    users = assets.get("User")
    if not isinstance(users, dict):
        return {}
    return {str(key): value for key, value in users.items()}


def _display_name(user: Any) -> str | None:
    """Best human label for a Zammad user asset.

    Zammad's user assets carry no precomputed ``fullname``, so build one and
    fall back to the identifiers that always exist for an account that can act
    on a ticket.
    """
    if not isinstance(user, dict):
        return None
    parts = [str(user[key]) for key in ("firstname", "lastname") if user.get(key)]
    if parts:
        return " ".join(parts)
    for fallback in ("email", "login"):
        value = user.get(fallback)
        if value:
            return str(value)
    return None


def _trim_history_entry(entry: Any, people: dict[str, Any]) -> dict[str, Any]:
    """One raw ``History`` row -> one readable line of the change log."""
    if not isinstance(entry, dict):
        # A shape we do not recognise is passed through rather than dropped:
        # losing an audit entry is a worse failure than an ugly one.
        return {"raw": entry}

    actor_id = entry.get("created_by_id")
    trimmed: dict[str, Any] = {
        "at": entry.get("created_at"),
        "by": _display_name(people.get(str(actor_id))),
        "by_id": actor_id,
        "action": entry.get("type"),
        "object": entry.get("object"),
        # For a Ticket row this repeats the ticket id, but for the folded-in
        # Ticket::Article / Mention / Checklist rows it is the only pointer to
        # WHICH article or checklist the entry is about.
        "object_id": entry.get("o_id"),
    }
    if entry.get("attribute") is not None:
        trimmed["field"] = entry["attribute"]
    # Zammad omits value_from/value_to entirely when both are nil and sends ""
    # for a field that was previously empty. Emit both sides whenever either
    # key is present, so "unset -> Aya Nguyen" cannot read as "no change".
    if "value_from" in entry or "value_to" in entry:
        trimmed["from"] = entry.get("value_from")
        trimmed["to"] = entry.get("value_to")
    if entry.get("id_from") is not None:
        trimmed["from_id"] = entry["id_from"]
    if entry.get("id_to") is not None:
        trimmed["to_id"] = entry["id_to"]
    # A non-human actor. Zammad records the trigger / scheduler / postmaster
    # filter / AI agent that caused the change while still stamping the user it
    # ran as, so without this the log blames a person for an automation.
    if entry.get("sourceable_name"):
        trimmed["via"] = entry["sourceable_name"]
    if entry.get("sourceable_type"):
        trimmed["via_type"] = entry["sourceable_type"]
    if entry.get("related_object"):
        trimmed["related_object"] = entry["related_object"]
        trimmed["related_id"] = entry.get("related_o_id")
    return trimmed


def register(mcp: FastMCP, ctx: ToolContext) -> int:
    @mcp.tool(
        name="get_ticket_history",
        description=(
            "Read the complete audit trail of a ticket - the tool that answers "
            "'who closed this and when', 'who reassigned it', 'when did the "
            "priority change', 'was that a human or a trigger'. Needs the "
            "ticket.agent permission plus read access to the ticket's group; a "
            "customer gets HTTP 403. Returns every entry, unpaginated and "
            "oldest-first, each with the timestamp, the acting user resolved to "
            "a name, the action (created / updated / removed / added), the "
            "object and attribute touched, and the before/after values. "
            "Zammad folds related objects into the ticket log, so article, "
            "mention and checklist changes appear here too, each carrying the "
            "id of the object it refers to. Trimmed for readability: Zammad's "
            "raw response also ships an 'assets' blob - every user, article and "
            "group serialised in full, usually far larger than the log - which "
            "this tool uses only to turn user IDs into names and then discards, "
            "along with each entry's internal history-row id. No log entry is "
            "dropped, reordered or truncated. For the article TEXT rather than "
            "the record of it, use `list_ticket_articles`."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=True, destructiveHint=False, openWorldHint=True
        ),
    )
    async def get_ticket_history(
        ticket_id: Annotated[int, Field(ge=1, description="Numeric ticket ID")],
    ) -> dict[str, Any]:
        body: Any = await ctx.request("GET", f"/ticket_history/{ticket_id}")
        raw = body.get("history") if isinstance(body, dict) else None
        people = _people(body.get("assets")) if isinstance(body, dict) else {}
        entries = raw if isinstance(raw, list) else []
        history = [_trim_history_entry(entry, people) for entry in entries]
        # /ticket_history has no pagination: this is always the whole trail.
        return envelope(history, ticket_id=ticket_id)

    @mcp.tool(
        name="set_article_visibility",
        description=(
            "Change who can see an existing article on a ticket - the fix for a "
            "message filed with the wrong audience. The case this exists for: an "
            "e-mail that WAS delivered to the customer but was flagged internal, "
            "so the customer cannot find it in their own ticket view and the "
            "thread looks like it was never answered. Pass "
            "`visibility='customer_visible'` to put such an article back in "
            "front of the customer, or 'internal' to pull a note that should "
            "never have been public out of their view. "
            "Sharp edges: (1) Zammad accepts NOTHING else on this endpoint - "
            "body, subject, type, recipients are silently ignored and the call "
            "still returns HTTP 200, so wrong CONTENT is corrected with "
            "`delete_ticket_article` plus a fresh `reply_to_customer` or "
            "`add_internal_note`, not here; (2) this only moves a curtain - an "
            "e-mail already sent stays sent, and making an article visible does "
            "not deliver anything. Needs the ticket.agent permission with change "
            "access on the ticket's group."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=True,  # overwrites who may see an existing article
            idempotentHint=True,
            openWorldHint=True,
        ),
    )
    async def set_article_visibility(
        article_id: Annotated[int, Field(ge=1, description="Numeric article ID")],
        visibility: Annotated[
            str,
            Field(
                description=(
                    "'customer_visible' or 'internal' - the same vocabulary "
                    "create_ticket, update_ticket and update_tickets use. This "
                    "sets the article's own visibility flag, the one reported "
                    "as `internal` by list_ticket_articles."
                )
            ),
        ],
    ) -> Any:
        if visibility not in VISIBILITY:
            raise ToolError(
                f"visibility must be one of {', '.join(VISIBILITY)} "
                f"(got {visibility!r})."
            )
        # A JSON body carries a real boolean - only query params need the
        # lowercase string form Zammad's param casting expects.
        return await ctx.request(
            "PUT",
            f"/ticket_articles/{article_id}",
            json={"internal": visibility == "internal"},
        )

    @mcp.tool(
        name="delete_ticket_article",
        description=(
            "Permanently delete one article from a ticket. Zammad restricts "
            "this hard and answers HTTP 403 unless EVERY condition holds: (1) "
            "the caller has the ticket.agent permission and may see the "
            "article; (2) the caller wrote it - you can only delete your own "
            "articles, there is no API to delete a colleague's; (3) it is a "
            "note, or an INTERNAL article of a communication type - a "
            "customer-facing e-mail, phone, web, chat, sms, fax or social "
            "article can never be deleted, at any age, by anyone; (4) it is "
            "younger than the instance's delete window (Zammad setting "
            "ui_ticket_zoom_article_delete_timeframe, 600 seconds out of the "
            "box; only an admin can widen it, or set it to 0 for no limit). "
            "None of that is negotiable from the API, so do not offer a user a "
            "deletion that will fail. To take a wrongly-published article out "
            "of the customer's view instead - which works whoever wrote it and "
            "however old it is - use `set_article_visibility`."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=False,
            openWorldHint=True,
        ),
    )
    async def delete_ticket_article(
        article_id: Annotated[int, Field(ge=1, description="Numeric article ID")],
    ) -> dict[str, Any]:
        # Zammad answers 200 with an empty object; report the id back so the
        # model can state what it removed.
        await ctx.request("DELETE", f"/ticket_articles/{article_id}")
        return {"deleted": True, "article_id": article_id}

    @mcp.tool(
        name="unsubscribe_from_ticket",
        description=(
            "Stop the currently authenticated user receiving notifications "
            "about a ticket - the counterpart to `subscribe_to_ticket`. Zammad "
            "has no 'unsubscribe me' route: a subscription is a mention record "
            "with its own id, so this tool resolves the caller the way `get_me` "
            "does, reads the ticket's mentions the way `list_ticket_subscribers` "
            "does, and deletes the one belonging to the caller. It can only ever "
            "remove the CALLER's own subscription - Zammad rejects deleting "
            "another user's mention with HTTP 403, so there is no way to "
            "unsubscribe a colleague. If the caller was not subscribed, nothing "
            "changes and the result says so rather than failing. Needs agent "
            "read access to the ticket."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=False,
            # Not destructive: the record removed belongs solely to the calling
            # user and subscribe_to_ticket puts it straight back. Prompting for
            # this would be friction without a decision behind it — the same
            # reasoning that leaves mark_notification_read additive.
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
    )
    async def unsubscribe_from_ticket(
        ticket_id: Annotated[int, Field(ge=1, description="Numeric ticket ID")],
    ) -> dict[str, Any]:
        me: Any = await ctx.request("GET", "/users/me")
        user_id = me.get("id") if isinstance(me, dict) else None
        if not isinstance(user_id, int):
            raise ToolError(
                "Could not determine the calling Zammad user, so the right "
                "subscription cannot be identified. Call get_me to check the "
                "connection is authenticated as a real user, then retry."
            )

        listing: Any = await ctx.request(
            "GET",
            "/mentions",
            params={"mentionable_type": "Ticket", "mentionable_id": ticket_id},
        )
        mentions = listing.get("mentions") if isinstance(listing, dict) else None
        mine: dict[str, Any] | None = None
        if isinstance(mentions, list):
            for mention in mentions:
                if isinstance(mention, dict) and mention.get("user_id") == user_id:
                    mine = mention
                    break

        # Already the desired state. Not an error - reporting it lets the model
        # answer "you were not subscribed" instead of retrying a no-op.
        if mine is None:
            return {
                "unsubscribed": False,
                "ticket_id": ticket_id,
                "user_id": user_id,
                "reason": "the calling user has no subscription on this ticket",
            }

        mention_id = mine.get("id")
        await ctx.request("DELETE", f"/mentions/{mention_id}")
        return {
            "unsubscribed": True,
            "ticket_id": ticket_id,
            "user_id": user_id,
            "mention_id": mention_id,
        }

    return 4
