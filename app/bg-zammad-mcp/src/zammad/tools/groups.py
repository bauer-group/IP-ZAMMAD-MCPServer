"""
Group tools - read-only.

Groups are the team / queue concept in Zammad. The MCP only exposes
read operations because group lifecycle (create / rename / delete) is
an admin task that's better done via the Zammad UI and rarely needed
from an AI assistant.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any

from mcp.types import ToolAnnotations
from pydantic import Field

from . import ToolContext

if TYPE_CHECKING:
    from fastmcp import FastMCP


def register(mcp: FastMCP, ctx: ToolContext) -> int:
    @mcp.tool(
        name="list_groups",
        description="List all Zammad groups (teams / queues).",
        annotations=ToolAnnotations(
            readOnlyHint=True, destructiveHint=False, openWorldHint=True
        ),
    )
    async def list_groups(
        page: Annotated[int, Field(ge=1)] = 1,
        per_page: Annotated[int, Field(ge=1, le=100)] = 50,
        expand: Annotated[bool, Field()] = True,
    ) -> Any:
        return await ctx.request(
            "GET",
            "/groups",
            params={
                "page": page,
                "per_page": per_page,
                "expand": str(expand).lower(),
            },
        )

    @mcp.tool(
        name="get_group",
        description="Fetch a single Zammad group by numeric ID.",
        annotations=ToolAnnotations(
            readOnlyHint=True, destructiveHint=False, openWorldHint=True
        ),
    )
    async def get_group(
        group_id: Annotated[int, Field(ge=1)],
        expand: Annotated[bool, Field()] = True,
    ) -> Any:
        return await ctx.request(
            "GET",
            f"/groups/{group_id}",
            params={"expand": str(expand).lower()},
        )

    return 2
