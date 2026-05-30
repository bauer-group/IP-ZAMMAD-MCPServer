"""
Online-notification + mention tools.

Endpoints (all under /api/v1/):
  GET   /online_notifications                       list current user's notifications
  POST  /online_notifications/mark_all_as_read      mark all read
  PUT   /online_notifications/{id}                  mark single read/unread
  GET   /mentions?mentionable_type=...&mentionable_id=...   list mentions on an object
  POST  /mentions                                    subscribe to a ticket
  DELETE /mentions/{id}                              unsubscribe
"""

from __future__ import annotations

from typing import Annotated, Any

from mcp.types import ToolAnnotations
from pydantic import Field

from . import ToolContext


def register(mcp: Any, ctx: ToolContext) -> int:
    read_only = ToolAnnotations(
        readOnlyHint=True, destructiveHint=False, openWorldHint=True
    )

    @mcp.tool(
        name="list_my_notifications",
        description=(
            "List online notifications (in-app alerts) for the currently "
            "authenticated user."
        ),
        annotations=read_only,
    )
    async def list_my_notifications() -> Any:
        return await ctx.request("GET", "/online_notifications")

    @mcp.tool(
        name="mark_all_notifications_read",
        description="Mark every online notification for the current user as read.",
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=True,
            openWorldHint=True,
        ),
    )
    async def mark_all_notifications_read() -> dict[str, Any]:
        await ctx.request("POST", "/online_notifications/mark_all_as_read")
        return {"marked_all_read": True}

    @mcp.tool(
        name="mark_notification_read",
        description="Mark a single online notification as read or unread.",
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=True,
            openWorldHint=True,
        ),
    )
    async def mark_notification_read(
        notification_id: Annotated[int, Field(ge=1)],
        seen: Annotated[bool, Field(description="True = read, False = unread")] = True,
    ) -> Any:
        return await ctx.request(
            "PUT",
            f"/online_notifications/{notification_id}",
            json={"seen": seen},
        )

    @mcp.tool(
        name="list_ticket_subscribers",
        description=(
            "List the users currently subscribed (mentioned) on a ticket. "
            "Subscribed users receive notifications on every change."
        ),
        annotations=read_only,
    )
    async def list_ticket_subscribers(
        ticket_id: Annotated[int, Field(ge=1)],
    ) -> Any:
        return await ctx.request(
            "GET",
            "/mentions",
            params={"mentionable_type": "Ticket", "mentionable_id": ticket_id},
        )

    @mcp.tool(
        name="subscribe_to_ticket",
        description=(
            "Subscribe the currently authenticated user to a ticket so they "
            "receive notifications on changes."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=True,
            openWorldHint=True,
        ),
    )
    async def subscribe_to_ticket(
        ticket_id: Annotated[int, Field(ge=1)],
    ) -> Any:
        return await ctx.request(
            "POST",
            "/mentions",
            json={"mentionable_type": "Ticket", "mentionable_id": ticket_id},
        )

    return 5
