"""Request-shape tests for the organization tools.

Same reasoning as the user tools: the payload builders are conditional
field by field, and a dropped field produces a successful-looking update that
changed nothing.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastmcp import FastMCP

from tests.test_tools_inventory import RecordingCtx
from zammad.tools import organizations


@pytest.fixture
def org_tools() -> tuple[FastMCP, RecordingCtx]:
    mcp: FastMCP = FastMCP("orgs-test")
    ctx = RecordingCtx()
    organizations.register(mcp, ctx)
    return mcp, ctx


async def _call(mcp: FastMCP, name: str, /, **kwargs: Any) -> Any:
    # positional-only: several tools have their own `name` parameter
    # (create_organization, for one), which would collide otherwise.
    tools = {t.name: t for t in await mcp.list_tools(run_middleware=False)}
    return await tools[name].run(kwargs)


async def test_create_organization_defaults(org_tools) -> None:  # type: ignore[no-untyped-def]
    mcp, ctx = org_tools
    await _call(mcp, "create_organization", name="ACME")

    assert ctx.last["method"] == "POST"
    assert ctx.last["path"] == "/organizations"
    # shared=True is Zammad's own default and means members see each other's
    # tickets — worth pinning rather than leaving to a later refactor.
    assert ctx.last["json"] == {
        "name": "ACME",
        "active": True,
        "shared": True,
        "domain_assignment": False,
    }


async def test_create_organization_with_domain_assignment(org_tools) -> None:  # type: ignore[no-untyped-def]
    mcp, ctx = org_tools
    await _call(
        mcp,
        "create_organization",
        name="ACME",
        domain="acme.example",
        domain_assignment=True,
        note="via MCP",
        shared=False,
        active=False,
    )
    payload = ctx.last["json"]
    assert payload["domain"] == "acme.example"
    assert payload["domain_assignment"] is True
    assert payload["note"] == "via MCP"
    assert payload["shared"] is False
    assert payload["active"] is False


async def test_update_organization_sends_only_what_changed(org_tools) -> None:  # type: ignore[no-untyped-def]
    mcp, ctx = org_tools
    await _call(mcp, "update_organization", organization_id=2, note="merged")
    assert ctx.last["method"] == "PUT"
    assert ctx.last["path"] == "/organizations/2"
    assert ctx.last["json"] == {"note": "merged"}


@pytest.mark.parametrize("field", ["shared", "active", "domain_assignment"])
async def test_update_organization_can_turn_a_flag_off(org_tools, field: str) -> None:  # type: ignore[no-untyped-def]
    """False must survive the "only if not None" filter. `shared` in particular
    controls whether an organization's members can read each other's tickets, so
    a silently dropped False is a privacy problem, not a cosmetic one."""
    mcp, ctx = org_tools
    await _call(mcp, "update_organization", organization_id=2, **{field: False})
    assert ctx.last["json"] == {field: False}


async def test_update_organization_full_field_set(org_tools) -> None:  # type: ignore[no-untyped-def]
    mcp, ctx = org_tools
    await _call(
        mcp,
        "update_organization",
        organization_id=2,
        name="ACME GmbH",
        domain="acme.de",
        domain_assignment=True,
        note="n",
        active=True,
        shared=True,
    )
    assert set(ctx.last["json"]) == {
        "name",
        "domain",
        "domain_assignment",
        "note",
        "active",
        "shared",
    }


async def test_update_organization_without_fields_raises(org_tools) -> None:  # type: ignore[no-untyped-def]
    mcp, ctx = org_tools
    with pytest.raises(Exception, match="at least one field"):
        await _call(mcp, "update_organization", organization_id=2)
    assert ctx.calls == []


async def test_search_organizations_paginates(org_tools) -> None:  # type: ignore[no-untyped-def]
    mcp, ctx = org_tools
    await _call(mcp, "search_organizations", query="acme", page=4)
    params = ctx.last["params"]
    assert params["page"] == 4
    assert params["with_total_count"] == "true"
