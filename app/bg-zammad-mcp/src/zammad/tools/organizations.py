"""
Organization tools.

Endpoints (all under /api/v1/):
  GET  /organizations                 paginated list
  GET  /organizations/search?query=   search
  GET  /organizations/{id}            get one
  POST /organizations                 create
  PUT  /organizations/{id}            update
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
        name="list_organizations",
        description="List Zammad organizations, paginated.",
        annotations=ToolAnnotations(
            readOnlyHint=True, destructiveHint=False, openWorldHint=True
        ),
    )
    async def list_organizations(
        page: Annotated[int, Field(ge=1)] = 1,
        per_page: Annotated[int, Field(ge=1, le=100)] = 25,
        expand: Annotated[bool, Field()] = True,
    ) -> Any:
        return await ctx.request(
            "GET",
            "/organizations",
            params={
                "page": page,
                "per_page": per_page,
                "expand": str(expand).lower(),
            },
        )

    @mcp.tool(
        name="search_organizations",
        description="Search Zammad organizations by name or domain.",
        annotations=ToolAnnotations(
            readOnlyHint=True, destructiveHint=False, openWorldHint=True
        ),
    )
    async def search_organizations(
        query: Annotated[str, Field(min_length=1)],
        limit: Annotated[int, Field(ge=1, le=100)] = 25,
        expand: Annotated[bool, Field()] = True,
    ) -> Any:
        return await ctx.request(
            "GET",
            "/organizations/search",
            params={
                "query": query,
                "limit": limit,
                "expand": str(expand).lower(),
            },
        )

    @mcp.tool(
        name="get_organization",
        description="Fetch a single organization by numeric ID.",
        annotations=ToolAnnotations(
            readOnlyHint=True, destructiveHint=False, openWorldHint=True
        ),
    )
    async def get_organization(
        organization_id: Annotated[int, Field(ge=1)],
        expand: Annotated[bool, Field()] = True,
    ) -> Any:
        return await ctx.request(
            "GET",
            f"/organizations/{organization_id}",
            params={"expand": str(expand).lower()},
        )

    @mcp.tool(
        name="create_organization",
        description=(
            "Create a new Zammad organization. Restricted by Zammad permissions "
            "to Admins (and Agents with `admin.organization` rights)."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=False,
            openWorldHint=True,
        ),
    )
    async def create_organization(
        name: Annotated[str, Field(min_length=1, max_length=200)],
        domain: Annotated[
            str | None,
            Field(description="Organization domain (auto-link customers by e-mail)"),
        ] = None,
        domain_assignment: Annotated[
            bool, Field(description="Auto-assign users with this domain")
        ] = False,
        note: Annotated[str | None, Field(max_length=2000)] = None,
        active: Annotated[bool, Field()] = True,
        shared: Annotated[
            bool,
            Field(description="If True, members can see each other's tickets"),
        ] = True,
    ) -> Any:
        payload: dict[str, Any] = {
            "name": name,
            "active": active,
            "shared": shared,
            "domain_assignment": domain_assignment,
        }
        if domain is not None:
            payload["domain"] = domain
        if note is not None:
            payload["note"] = note
        return await ctx.request("POST", "/organizations", json=payload)

    @mcp.tool(
        name="update_organization",
        description="Update fields on an existing organization.",
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=True,
            openWorldHint=True,
        ),
    )
    async def update_organization(
        organization_id: Annotated[int, Field(ge=1)],
        name: Annotated[str | None, Field(max_length=200)] = None,
        domain: Annotated[str | None, Field()] = None,
        domain_assignment: Annotated[bool | None, Field()] = None,
        note: Annotated[str | None, Field(max_length=2000)] = None,
        active: Annotated[bool | None, Field()] = None,
        shared: Annotated[bool | None, Field()] = None,
    ) -> Any:
        payload: dict[str, Any] = {}
        if name is not None:
            payload["name"] = name
        if domain is not None:
            payload["domain"] = domain
        if domain_assignment is not None:
            payload["domain_assignment"] = domain_assignment
        if note is not None:
            payload["note"] = note
        if active is not None:
            payload["active"] = active
        if shared is not None:
            payload["shared"] = shared
        if not payload:
            raise ValueError("update_organization called with no fields to update")
        return await ctx.request("PUT", f"/organizations/{organization_id}", json=payload)

    return 5
