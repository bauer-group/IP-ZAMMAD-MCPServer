"""
User tools - customers, agents, admins, and the caller themselves.

Endpoints (all under /api/v1/):
  GET  /users                       paginated list
  GET  /users/search?query=...      full-text search
  GET  /users/{id}                  get one
  GET  /users/me                    current authenticated user (always allowed)
  POST /users                       create
  PUT  /users/{id}                  update

`get_me` returns the caller's own profile - the canonical way for an MCP
client to verify which Zammad identity it is operating as. Note: like every
tool it is subject to the MCP role allowlist (MCP_ALLOWED_ROLES); a caller
whose roles are not on the allowlist is rejected before any tool runs.
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
        name="get_me",
        description=(
            "Return the currently-authenticated Zammad user (the caller). "
            "Includes role names, organization, e-mail, and active flag - "
            "use this to verify identity before any privileged action."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=True, destructiveHint=False, openWorldHint=True
        ),
    )
    async def get_me(
        expand: Annotated[bool, Field(description="Inline role names")] = True,
    ) -> Any:
        return await ctx.request(
            "GET",
            "/users/me",
            params={"expand": str(expand).lower()},
        )

    @mcp.tool(
        name="list_users",
        description=(
            "List Zammad users (customers + agents + admins), paginated. "
            "Use `search_users` for filtering by query."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=True, destructiveHint=False, openWorldHint=True
        ),
    )
    async def list_users(
        page: Annotated[int, Field(ge=1)] = 1,
        per_page: Annotated[int, Field(ge=1, le=100)] = 25,
        expand: Annotated[bool, Field(description="Inline role names")] = True,
    ) -> Any:
        return await ctx.request(
            "GET",
            "/users",
            params={
                "page": page,
                "per_page": per_page,
                "expand": str(expand).lower(),
            },
        )

    @mcp.tool(
        name="search_users",
        description=(
            "Search Zammad users by name, e-mail, login, or other indexed "
            "fields. Same query syntax as `search_tickets`."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=True, destructiveHint=False, openWorldHint=True
        ),
    )
    async def search_users(
        query: Annotated[str, Field(min_length=1)],
        limit: Annotated[int, Field(ge=1, le=100)] = 25,
        expand: Annotated[bool, Field(description="Inline role names")] = True,
    ) -> Any:
        return await ctx.request(
            "GET",
            "/users/search",
            params={
                "query": query,
                "limit": limit,
                "expand": str(expand).lower(),
            },
        )

    @mcp.tool(
        name="get_user",
        description="Fetch a single Zammad user by numeric ID.",
        annotations=ToolAnnotations(
            readOnlyHint=True, destructiveHint=False, openWorldHint=True
        ),
    )
    async def get_user(
        user_id: Annotated[int, Field(ge=1)],
        expand: Annotated[bool, Field(description="Inline role names")] = True,
    ) -> Any:
        return await ctx.request(
            "GET",
            f"/users/{user_id}",
            params={"expand": str(expand).lower()},
        )

    @mcp.tool(
        name="create_user",
        description=(
            "Create a new Zammad user. Restricted to Admin / Agent roles by "
            "Zammad's permission system. Provide at minimum `email` OR "
            "(`firstname` AND `lastname`); typical fields: `email`, "
            "`firstname`, `lastname`, `phone`, `organization_id`, `roles`."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=False,
            openWorldHint=True,
        ),
    )
    async def create_user(
        email: Annotated[
            str | None, Field(description="Primary e-mail address (recommended)")
        ] = None,
        firstname: Annotated[str | None, Field(max_length=200)] = None,
        lastname: Annotated[str | None, Field(max_length=200)] = None,
        login: Annotated[str | None, Field(max_length=200)] = None,
        phone: Annotated[str | None, Field(max_length=100)] = None,
        organization_id: Annotated[int | None, Field(ge=1)] = None,
        roles: Annotated[
            str | None,
            Field(
                description=(
                    "Comma-separated role names, e.g. 'Customer' or 'Agent,Admin'. "
                    "Defaults to Customer if omitted."
                )
            ),
        ] = None,
        active: Annotated[bool, Field(description="User is active (can log in)")] = True,
    ) -> Any:
        if not (email or (firstname and lastname) or login):
            raise ValueError(
                "create_user requires at least `email`, `login`, or both "
                "`firstname` and `lastname`"
            )
        payload: dict[str, Any] = {"active": active}
        if email is not None:
            payload["email"] = email
        if firstname is not None:
            payload["firstname"] = firstname
        if lastname is not None:
            payload["lastname"] = lastname
        if login is not None:
            payload["login"] = login
        if phone is not None:
            payload["phone"] = phone
        if organization_id is not None:
            payload["organization_id"] = organization_id
        if roles is not None:
            payload["roles"] = [r.strip() for r in roles.split(",") if r.strip()]
        return await ctx.request("POST", "/users", json=payload)

    @mcp.tool(
        name="update_user",
        description=(
            "Update fields on an existing Zammad user. Only supplied fields "
            "are changed. Restricted to Admin / Agent by Zammad permissions."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=True,
            openWorldHint=True,
        ),
    )
    async def update_user(
        user_id: Annotated[int, Field(ge=1)],
        email: Annotated[str | None, Field()] = None,
        firstname: Annotated[str | None, Field(max_length=200)] = None,
        lastname: Annotated[str | None, Field(max_length=200)] = None,
        phone: Annotated[str | None, Field(max_length=100)] = None,
        organization_id: Annotated[int | None, Field(ge=1)] = None,
        roles: Annotated[
            str | None,
            Field(description="Comma-separated role names to set"),
        ] = None,
        active: Annotated[bool | None, Field()] = None,
    ) -> Any:
        payload: dict[str, Any] = {}
        if email is not None:
            payload["email"] = email
        if firstname is not None:
            payload["firstname"] = firstname
        if lastname is not None:
            payload["lastname"] = lastname
        if phone is not None:
            payload["phone"] = phone
        if organization_id is not None:
            payload["organization_id"] = organization_id
        if roles is not None:
            payload["roles"] = [r.strip() for r in roles.split(",") if r.strip()]
        if active is not None:
            payload["active"] = active
        if not payload:
            raise ValueError("update_user called with no fields to update")
        return await ctx.request("PUT", f"/users/{user_id}", json=payload)

    return 6
