"""
Reference-data tools - lookup tables that change rarely.

  GET /ticket_states      list all ticket states (open, closed, pending, ...)
  GET /ticket_priorities  list all priorities (1 low, 2 normal, 3 high)
  GET /roles              list all roles  (Admin / Agent / Customer / custom)
  GET /version            Zammad version (also used by the v6/v7 probe)

These are all read-only. Like every tool they pass through the MCP role
allowlist (MCP_ALLOWED_ROLES) first; beyond that, Zammad hides custom
states / priorities that the caller's role can't see.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from mcp.types import ToolAnnotations

from . import ToolContext

if TYPE_CHECKING:
    from fastmcp import FastMCP


def register(mcp: FastMCP, ctx: ToolContext) -> int:
    read_only = ToolAnnotations(
        readOnlyHint=True, destructiveHint=False, openWorldHint=True
    )

    @mcp.tool(
        name="list_ticket_states",
        description=(
            "List all ticket states defined in this Zammad instance "
            "(open, closed, pending reminder, pending close, ...). "
            "Useful before creating or updating tickets to pick the right "
            "state_id."
        ),
        annotations=read_only,
    )
    async def list_ticket_states() -> Any:
        return await ctx.request("GET", "/ticket_states")

    @mcp.tool(
        name="list_ticket_priorities",
        description=(
            "List all ticket priorities defined in this Zammad instance "
            "(typically 1 low, 2 normal, 3 high)."
        ),
        annotations=read_only,
    )
    async def list_ticket_priorities() -> Any:
        return await ctx.request("GET", "/ticket_priorities")

    @mcp.tool(
        name="list_roles",
        description=(
            "List all roles (Admin / Agent / Customer / custom). Useful for "
            "checking permission names before creating a user."
        ),
        annotations=read_only,
    )
    async def list_roles() -> Any:
        return await ctx.request("GET", "/roles")

    @mcp.tool(
        name="get_zammad_version",
        description=(
            "Return the live Zammad version string. Use to confirm whether "
            "you're talking to v6.x or v7.x before invoking version-specific "
            "behaviour."
        ),
        annotations=read_only,
    )
    async def get_zammad_version() -> Any:
        return await ctx.request("GET", "/version")

    return 4
