"""
Ticket-article tools - replies, internal notes, and message inspection.

Endpoints (all under /api/v1/):
  GET  /ticket_articles/by_ticket/{ticket_id}    all articles for a ticket
  GET  /ticket_articles/{id}                     one article
  POST /ticket_articles                          create (reply or note)
"""

from __future__ import annotations

from typing import Annotated, Any

from mcp.types import ToolAnnotations
from pydantic import Field

from . import ToolContext


def register(mcp: Any, ctx: ToolContext) -> int:
    @mcp.tool(
        name="list_ticket_articles",
        description=(
            "List all articles (messages, notes, replies) for a given ticket. "
            "Articles include the body text, sender, article type, and timing "
            "metadata - useful to summarise ticket history."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=True, destructiveHint=False, openWorldHint=True
        ),
    )
    async def list_ticket_articles(
        ticket_id: Annotated[int, Field(ge=1)],
        expand: Annotated[bool, Field(description="Inline sender/type names")] = True,
    ) -> Any:
        return await ctx.request(
            "GET",
            f"/ticket_articles/by_ticket/{ticket_id}",
            params={"expand": str(expand).lower()},
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
        name="create_ticket_article",
        description=(
            "Add a new article (reply, internal note, phone log, ...) to an "
            "existing ticket. Set `internal=True` for notes that customers "
            "should not see. Set `type='email'` (and `to`/`cc`) to send an "
            "outbound e-mail to the customer."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=False,
            openWorldHint=True,
        ),
    )
    async def create_ticket_article(
        ticket_id: Annotated[int, Field(ge=1)],
        body: Annotated[str, Field(min_length=1)],
        article_type: Annotated[
            str,
            Field(
                description=(
                    "Article type: 'note' (internal, default), 'email', "
                    "'phone', 'web', 'chat'."
                )
            ),
        ] = "note",
        internal: Annotated[
            bool, Field(description="Hide from customers (default True for notes)")
        ] = True,
        subject: Annotated[str | None, Field(max_length=200)] = None,
        to: Annotated[
            str | None,
            Field(description="Recipient(s) - only meaningful for type='email'"),
        ] = None,
        cc: Annotated[str | None, Field(description="CC recipient(s) - email only")] = None,
        content_type: Annotated[
            str,
            Field(description="'text/plain' (default) or 'text/html'"),
        ] = "text/plain",
    ) -> Any:
        payload: dict[str, Any] = {
            "ticket_id": ticket_id,
            "body": body,
            "type": article_type,
            "internal": internal,
            "content_type": content_type,
        }
        if subject is not None:
            payload["subject"] = subject
        if to is not None:
            payload["to"] = to
        if cc is not None:
            payload["cc"] = cc
        return await ctx.request("POST", "/ticket_articles", json=payload)

    return 3
