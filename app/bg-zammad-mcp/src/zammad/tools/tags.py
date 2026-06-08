"""
Tag tools.

Endpoints (all under /api/v1/):
  GET    /tags?object=Ticket&o_id={id}  tags for one object
  POST   /tags/add                      add tag (any user with object access)
  DELETE /tags/remove                   remove tag
  GET    /tag_list                      enumerate all tags
  GET    /tag_search?term=...           search tags

Object-attached tools default to `object=Ticket` because tickets are by
far the most common target in an AI workflow. To tag other object types
(User, Organization, KnowledgeBaseAnswer, ...) pass `object_type`.
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
        name="list_object_tags",
        description=(
            "List tags currently attached to a specific Zammad object "
            "(default: a ticket)."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=True, destructiveHint=False, openWorldHint=True
        ),
    )
    async def list_object_tags(
        object_id: Annotated[int, Field(ge=1, description="ID of the object")],
        object_type: Annotated[
            str,
            Field(description="Object type, e.g. 'Ticket', 'KnowledgeBase::Answer'"),
        ] = "Ticket",
    ) -> Any:
        return await ctx.request(
            "GET",
            "/tags",
            params={"object": object_type, "o_id": object_id},
        )

    @mcp.tool(
        name="list_all_tags",
        description="Enumerate all tags defined in this Zammad instance.",
        annotations=ToolAnnotations(
            readOnlyHint=True, destructiveHint=False, openWorldHint=True
        ),
    )
    async def list_all_tags() -> Any:
        return await ctx.request("GET", "/tag_list")

    @mcp.tool(
        name="search_tags",
        description="Search tag names by prefix term.",
        annotations=ToolAnnotations(
            readOnlyHint=True, destructiveHint=False, openWorldHint=True
        ),
    )
    async def search_tags(
        term: Annotated[str, Field(min_length=1)],
    ) -> Any:
        return await ctx.request("GET", "/tag_search", params={"term": term})

    @mcp.tool(
        name="add_tag",
        description=(
            "Attach a tag to a Zammad object (default: a ticket). Creates the "
            "tag if it doesn't already exist."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=True,
            openWorldHint=True,
        ),
    )
    async def add_tag(
        object_id: Annotated[int, Field(ge=1)],
        item: Annotated[str, Field(min_length=1, description="Tag name")],
        object_type: Annotated[str, Field(description="Object type")] = "Ticket",
    ) -> dict[str, Any]:
        await ctx.request(
            "POST",
            "/tags/add",
            params={"object": object_type, "o_id": object_id, "item": item},
        )
        return {"added": True, "object_type": object_type, "object_id": object_id, "tag": item}

    @mcp.tool(
        name="remove_tag",
        description="Remove a tag from a Zammad object (default: a ticket).",
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=True,
            openWorldHint=True,
        ),
    )
    async def remove_tag(
        object_id: Annotated[int, Field(ge=1)],
        item: Annotated[str, Field(min_length=1, description="Tag name")],
        object_type: Annotated[str, Field(description="Object type")] = "Ticket",
    ) -> dict[str, Any]:
        await ctx.request(
            "DELETE",
            "/tags/remove",
            params={"object": object_type, "o_id": object_id, "item": item},
        )
        return {"removed": True, "object_type": object_type, "object_id": object_id, "tag": item}

    return 5
