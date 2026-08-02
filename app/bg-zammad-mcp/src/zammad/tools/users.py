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

from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from ..projection import USER_FIELDS, collection, parse_fields
from . import ToolContext

if TYPE_CHECKING:
    from fastmcp import FastMCP

# Zammad's server-side ceiling on the /search endpoints.
SEARCH_MAX_LIMIT = 200


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
        fields: Annotated[
            str | None,
            Field(
                description=(
                    "Comma-separated whitelist of fields to keep, e.g. "
                    "'id,login,firstname,lastname,email'. Overrides the default projection."
                )
            ),
        ] = None,
        full: Annotated[
            bool,
            Field(description="Return Zammad's untrimmed records (large)"),
        ] = False,
    ) -> Any:
        payload = await ctx.request(
            "GET",
            "/users",
            params={
                "page": page,
                "per_page": per_page,
                "expand": str(expand).lower(),
            },
        )
        return collection(
            payload,
            parse_fields(fields) or USER_FIELDS,
            page=page,
            per_page=per_page,
            full=full,
        )

    @mcp.tool(
        name="search_users",
        description=(
            "Search Zammad users by name, e-mail, login, or other indexed "
            "fields, using the same Elasticsearch-backed query syntax as "
            "`search_tickets`. Pass `page` to go beyond the first `per_page` "
            "results."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=True, destructiveHint=False, openWorldHint=True
        ),
    )
    async def search_users(
        query: Annotated[str, Field(min_length=1)],
        page: Annotated[int, Field(ge=1, description="1-indexed page number")] = 1,
        per_page: Annotated[
            int, Field(ge=1, le=SEARCH_MAX_LIMIT, description="Results per page (max 200)")
        ] = 25,
        expand: Annotated[bool, Field(description="Inline role names")] = True,
        fields: Annotated[
            str | None,
            Field(
                description=(
                    "Comma-separated whitelist of fields to keep, e.g. "
                    "'id,login,firstname,lastname,email'. Overrides the default projection."
                )
            ),
        ] = None,
        full: Annotated[
            bool,
            Field(description="Return Zammad's untrimmed records (large)"),
        ] = False,
    ) -> Any:
        params: dict[str, Any] = {
            "query": query,
            "page": page,
            "per_page": per_page,
            "expand": str(expand).lower(),
            "with_total_count": "true",
        }
        payload = await ctx.request("GET", "/users/search", params=params)
        return collection(
            payload,
            parse_fields(fields) or USER_FIELDS,
            page=page,
            per_page=per_page,
            full=full,
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
            destructiveHint=False,  # additive
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
        note: Annotated[str | None, Field(max_length=2000)] = None,
        extra_fields: Annotated[
            dict[str, Any] | None,
            Field(
                description=(
                    "Custom Object-Manager attributes to set, as a name/value "
                    "map. Use `list_object_attributes` to discover which exist "
                    "on this instance."
                )
            ),
        ] = None,
        active: Annotated[bool, Field(description="User is active (can log in)")] = True,
    ) -> Any:
        if not (email or (firstname and lastname) or login):
            raise ToolError(
                "create_user requires at least `email`, `login`, or both "
                "`firstname` and `lastname`"
            )
        # extra_fields first so a named argument always wins, exactly as in
        # create_ticket — one merge rule for the whole surface.
        payload: dict[str, Any] = dict(extra_fields or {})
        payload["active"] = active
        if note is not None:
            payload["note"] = note
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
        note: Annotated[str | None, Field(max_length=2000)] = None,
        extra_fields: Annotated[
            dict[str, Any] | None,
            Field(
                description=(
                    "Custom Object-Manager attributes to set, as a name/value "
                    "map. Use `list_object_attributes` to discover which exist "
                    "on this instance."
                )
            ),
        ] = None,
        active: Annotated[bool | None, Field()] = None,
    ) -> Any:
        payload: dict[str, Any] = dict(extra_fields or {})
        if note is not None:
            payload["note"] = note
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
            raise ToolError(
                "update_user needs at least one field to change. Pass e.g. "
                "email, phone, organization_id, note or active."
            )
        return await ctx.request("PUT", f"/users/{user_id}", json=payload)

    return 6
