"""
Ticket-article tools - replies, internal notes, and message inspection.

Endpoints (all under /api/v1/):
  GET  /ticket_articles/by_ticket/{ticket_id}    all articles for a ticket
  GET  /ticket_articles/{id}                     one article
  GET  /ticket_article_plain/{id}                raw e-mail source
  POST /ticket_articles                          create (reply or note)

Why two write tools instead of one
----------------------------------
Zammad models "who can see this" (``internal``) and "how was it delivered"
(``type``) as two independent fields, and the combination that looks harmless
is the dangerous one: ``{"type": "email", "internal": true}`` **sends the mail
to the customer and then hides the article from them** in their own ticket
view. The agent's history then misrepresents what the customer has actually
seen. A single ``create_ticket_article`` tool with an ``internal`` flag put
that trap one forgotten argument away, and an LLM has no way to notice - the
call returns HTTP 201 either way.

So visibility is encoded in the tool name instead of in a parameter:

  * ``reply_to_customer``  - always ``internal=false``. The customer sees it.
  * ``add_internal_note``  - always ``internal=true``, always ``type=note``.
                             The customer never sees it.

Neither tool exposes ``internal``, so the wrong combination is unreachable.

Attachments ride along
----------------------
Zammad creates attachments only alongside an article - there is no endpoint
that adds a file to an existing one. The ``attachments`` parameter therefore
sits on these tools rather than in a tool of its own, which buys two things:
visibility stays encoded in the tool NAME, so the trap above stays closed, and
a reply with a file remains ONE article, so the customer receives one mail
instead of two. ``ZAMMAD_ATTACHMENT_UPLOAD_ENABLED=false`` removes the
parameter from the published schema entirely.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any

from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from ..projection import trim_articles
from ..uploads import AttachmentInput, build_attachment_payload
from . import ToolContext
from ._uploads_wiring import register_write_tool, uploads_enabled

if TYPE_CHECKING:
    from fastmcp import FastMCP

ATTACHMENTS_DESCRIPTION = (
    "Files to send with this message. Each entry needs exactly one of: "
    "`text` (literal content - cheapest), `data_base64` (raw bytes), or "
    "`copy_from` (an attachment already in Zammad, identified by "
    "ticket_id / article_id / attachment_id - costs no tokens and stays "
    "byte-identical)."
)

# Delivery channels a customer-visible article can use. 'note' is included
# because a NON-internal note is a legitimate customer-visible entry; the
# internal variant lives in add_internal_note.
CUSTOMER_FACING_TYPES = ("email", "phone", "web", "chat", "note")


def register(mcp: FastMCP, ctx: ToolContext) -> int:
    @mcp.tool(
        name="list_ticket_articles",
        description=(
            "List the articles (messages, notes, replies) on a ticket: body, "
            "sender, type, timing, and whether the article is internal (hidden "
            "from the customer). Bodies are converted from HTML to plain text "
            "and shortened to `max_body_chars`; raise it when you need a verbatim "
            "quote. Zammad does not paginate this endpoint, so a long e-mail "
            "thread arrives in one response - use `per_page` with "
            "`newest_first=True` with `per_page` to read just the recent end of a "
            "conversation. "
            "Set `full=True` for Zammad's untouched payload."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=True, destructiveHint=False, openWorldHint=True
        ),
    )
    async def list_ticket_articles(
        ticket_id: Annotated[int, Field(ge=1)],
        page: Annotated[int, Field(ge=1, description="1-indexed page number")] = 1,
        per_page: Annotated[
            int | None,
            Field(ge=1, description="Articles per page (default: the whole thread)"),
        ] = None,
        newest_first: Annotated[
            bool,
            Field(description="Read the recent end of the thread first"),
        ] = False,
        max_body_chars: Annotated[
            int,
            Field(ge=0, le=100_000, description="Per-article body cap; 0 disables"),
        ] = 4000,
        full: Annotated[
            bool, Field(description="Return Zammad's raw payload, untrimmed")
        ] = False,
        expand: Annotated[bool, Field(description="Inline sender/type names")] = True,
    ) -> Any:
        payload = await ctx.request(
            "GET",
            f"/ticket_articles/by_ticket/{ticket_id}",
            params={"expand": str(expand).lower()},
        )
        return trim_articles(
            payload,
            max_body_chars=max_body_chars,
            page=page,
            per_page=per_page,
            newest_first=newest_first,
            full=full,
        )

    @mcp.tool(
        name="get_ticket_article",
        description="Fetch a single article (message) by its ID.",
        annotations=ToolAnnotations(
            readOnlyHint=True, destructiveHint=False, openWorldHint=True
        ),
    )
    async def get_ticket_article(
        article_id: Annotated[int, Field(ge=1)],
        expand: Annotated[bool, Field(description="Inline sender/type names")] = True,
    ) -> Any:
        return await ctx.request(
            "GET",
            f"/ticket_articles/{article_id}",
            params={"expand": str(expand).lower()},
        )

    @mcp.tool(
        name="get_article_plain",
        description=(
            "Fetch the raw source of an e-mail article - the original message "
            "with its headers, as Zammad received it. Use this when the parsed "
            "body is ambiguous and you need to see who was really on the "
            "conversation, or which client sent it. For ordinary reading, "
            "`get_ticket_article` is smaller and easier."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=True, destructiveHint=False, openWorldHint=True
        ),
    )
    async def get_article_plain(
        article_id: Annotated[int, Field(ge=1)],
    ) -> Any:
        return await ctx.request("GET", f"/ticket_article_plain/{article_id}")

    async def reply_to_customer(
        ticket_id: Annotated[int, Field(ge=1)],
        body: Annotated[str, Field(min_length=1, description="The reply text")],
        article_type: Annotated[
            str,
            Field(
                description=(
                    "Delivery channel: 'email' (default, actually sends the mail), "
                    "'phone', 'web', 'chat', or 'note' (visible, not delivered)."
                )
            ),
        ] = "email",
        subject: Annotated[str | None, Field(max_length=200)] = None,
        to: Annotated[
            str | None,
            Field(
                description=(
                    "Recipient(s) for article_type='email'. Omit to let Zammad "
                    "use the ticket's customer."
                )
            ),
        ] = None,
        cc: Annotated[str | None, Field(description="CC recipient(s) - e-mail only")] = None,
        content_type: Annotated[
            str,
            Field(description="'text/plain' (default) or 'text/html'"),
        ] = "text/plain",
        attachments: Annotated[
            list[AttachmentInput] | None,
            Field(default=None, max_length=10, description=ATTACHMENTS_DESCRIPTION),
        ] = None,
    ) -> Any:
        if article_type not in CUSTOMER_FACING_TYPES:
            raise ToolError(
                f"article_type must be one of {', '.join(CUSTOMER_FACING_TYPES)} "
                f"(got {article_type!r}). To write something the customer cannot "
                "see, use add_internal_note."
            )
        payload: dict[str, Any] = {
            "ticket_id": ticket_id,
            "body": body,
            "type": article_type,
            # The whole point of this tool. Not a parameter, so it cannot be
            # flipped by accident.
            "internal": False,
            "content_type": content_type,
        }
        if subject is not None:
            payload["subject"] = subject
        if to is not None:
            payload["to"] = to
        if cc is not None:
            payload["cc"] = cc
        attachment_payload = await build_attachment_payload(ctx, attachments)
        if attachment_payload:
            payload["attachments"] = attachment_payload
        return await ctx.request("POST", "/ticket_articles", json=payload)

    async def add_internal_note(
        ticket_id: Annotated[int, Field(ge=1)],
        body: Annotated[str, Field(min_length=1, description="The note text")],
        subject: Annotated[str | None, Field(max_length=200)] = None,
        content_type: Annotated[
            str,
            Field(description="'text/plain' (default) or 'text/html'"),
        ] = "text/plain",
        attachments: Annotated[
            list[AttachmentInput] | None,
            Field(default=None, max_length=10, description=ATTACHMENTS_DESCRIPTION),
        ] = None,
    ) -> Any:
        payload: dict[str, Any] = {
            "ticket_id": ticket_id,
            "body": body,
            "type": "note",
            "internal": True,
            "content_type": content_type,
        }
        if subject is not None:
            payload["subject"] = subject
        attachment_payload = await build_attachment_payload(ctx, attachments)
        if attachment_payload:
            payload["attachments"] = attachment_payload
        return await ctx.request("POST", "/ticket_articles", json=payload)

    # Registered explicitly rather than by decorator: when uploads are disabled
    # the tool has to be published WITHOUT its attachments argument, and that
    # decision has to be made before registration - every tool lookup on
    # FastMCP's provider is async, and this function is not.
    enabled = uploads_enabled(ctx)
    register_write_tool(
        mcp,
        reply_to_customer,
        enabled=enabled,
        name="reply_to_customer",
        description=(
            "Send a CUSTOMER-VISIBLE reply on a ticket. Use this for anything "
            "the customer should read. With the default `article_type='email'` "
            "Zammad delivers the message by e-mail to the ticket's customer "
            "(override the recipients with `to` / `cc`). Use "
            "`article_type='phone'` to log what was said on a call, or "
            "'note' for a visible note without delivery. Files passed in "
            "`attachments` travel in this same article, so the customer "
            "receives one message rather than two. This article is never "
            "internal - for something the customer must NOT see, use "
            "`add_internal_note` instead."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,  # additive: creates a new article, destroys nothing
            idempotentHint=False,
            openWorldHint=True,
        ),
    )
    register_write_tool(
        mcp,
        add_internal_note,
        enabled=enabled,
        name="add_internal_note",
        description=(
            "Add an INTERNAL note to a ticket - visible to agents only, never "
            "to the customer, and never delivered anywhere. Use it for "
            "investigation notes, hand-over context, or anything you would not "
            "want the customer to read, including files passed in "
            "`attachments`. To write to the customer, use `reply_to_customer`."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,  # additive
            idempotentHint=False,
            openWorldHint=True,
        ),
    )

    return 5
