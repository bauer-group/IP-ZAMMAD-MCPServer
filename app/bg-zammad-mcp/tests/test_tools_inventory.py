"""Golden inventory + request-shape tests for the whole tool surface.

Before this file, all eight tool modules had zero coverage: every endpoint,
parameter name and annotation was guaranteed by code review alone. These tests
register the real modules against a real ``FastMCP`` instance behind a
recording context, so they catch the failure modes that review misses:

* a tool silently disappearing or being renamed (the golden name list),
* a module's hardcoded ``return N`` drifting from what it registers,
* a description that references a parameter the schema does not publish -
  the exact defect that made ``create_ticket_article`` unusable,
* a request going to the wrong path, verb, query param or JSON key,
* an annotation that mislabels a write as read-only, or a purely additive
  create as destructive.

The recording context implements the same ``ToolContext`` protocol the real
``server._DecodingCtx`` does, so no HTTP layer is involved.
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from fastmcp import FastMCP

from zammad.tools import (
    ai,
    articles,
    attachments,
    bulk,
    checklists,
    fields,
    groups,
    history,
    knowledge,
    links,
    macros,
    notifications,
    organizations,
    overviews,
    reference,
    tags,
    tickets,
    time_accounting,
    users,
)

# Mirrors the registration order in server.register(): most central to
# day-to-day ticket work first.
MODULES = {
    "overviews": overviews,
    "tickets": tickets,
    "articles": articles,
    "macros": macros,
    "bulk": bulk,
    "links": links,
    "checklists": checklists,
    "time_accounting": time_accounting,
    "attachments": attachments,
    "history": history,
    "knowledge": knowledge,
    "ai": ai,
    "fields": fields,
    "users": users,
    "organizations": organizations,
    "groups": groups,
    "tags": tags,
    "reference": reference,
    "notifications": notifications,
}


class RecordingCtx:
    """A ToolContext that records calls instead of performing them.

    ``RecordingCtx(value)`` answers every call with that value. Some tools make
    more than one request — resolving a ticket number from an id, reading a
    checklist back — and giving them the same answer twice tests nothing, so
    ``RecordingCtx(responses=[a, b])`` answers each call in turn.

    The queue is a SEPARATE keyword on purpose: a list is a perfectly ordinary
    response body (most list_* tools return one), so overloading the positional
    argument would silently turn a returned array into a per-call queue.
    """

    def __init__(self, response: Any = None, *, responses: list[Any] | None = None) -> None:
        self.settings = None
        self.calls: list[dict[str, Any]] = []
        self._queue = list(responses) if responses is not None else None
        self._response = {} if response is None else response

    async def request(self, method: str, path: str, **kwargs: Any) -> Any:
        self.calls.append({"method": method, "path": path, **kwargs})
        if self._queue is not None:
            return self._queue.pop(0) if self._queue else {}
        return self._response

    @property
    def last(self) -> dict[str, Any]:
        assert self.calls, "no request was made"
        return self.calls[-1]


@pytest.fixture
def mcp_and_ctx() -> tuple[FastMCP, RecordingCtx]:
    mcp: FastMCP = FastMCP("test")
    ctx = RecordingCtx()
    for module in MODULES.values():
        module.register(mcp, ctx)
    return mcp, ctx


async def _tools(mcp: FastMCP) -> dict[str, Any]:
    """Name -> Tool, from FastMCP's own listing (middleware bypassed)."""
    return {tool.name: tool for tool in await mcp.list_tools(run_middleware=False)}


# ── golden inventory ─────────────────────────────────────────────────────────

# Every tool the server exposes. Adding or renaming a tool is a deliberate act -
# update this list in the same commit, so the change shows up in review.
EXPECTED_TOOLS = sorted(
    [
        # Worklist — the agent's actual queue, and Elasticsearch-free
        "list_my_queues",
        "list_queue_tickets",
        # Tickets
        "list_tickets",
        "search_tickets",
        "search_tickets_by_condition",
        "count_tickets",
        "get_ticket",
        "get_ticket_full",
        "create_ticket",
        "update_ticket",
        "update_ticket_title",
        "reassign_ticket_customer",
        "delete_ticket",
        # Articles
        "list_ticket_articles",
        "get_ticket_article",
        "get_article_plain",
        "reply_to_customer",
        "add_internal_note",
        # Macros and bulk
        "list_macros",
        "apply_macro_to_tickets",
        "update_tickets",
        # Duplicates, links and related context
        "merge_tickets",
        "find_related_tickets",
        "list_customer_tickets",
        "list_ticket_links",
        "link_tickets",
        "unlink_tickets",
        # Checklists
        "get_ticket_checklist",
        "create_ticket_checklist",
        "list_checklist_templates",
        "add_checklist_items",
        "set_checklist_item",
        # Time accounting
        "list_ticket_time_entries",
        "add_ticket_time_entry",
        # Attachments
        "list_ticket_attachments",
        "download_ticket_attachment",
        # Audit and correction
        "get_ticket_history",
        "set_article_visibility",
        "delete_ticket_article",
        "unsubscribe_from_ticket",
        # Knowledge base and house wording
        "search_knowledge_base",
        "get_kb_answer",
        "list_text_modules",
        "search_text_modules",
        # Zammad 7 native AI (feature-gated)
        "summarize_ticket",
        "draft_kb_answer_from_ticket",
        # Field discovery
        "list_ticket_fields",
        "list_object_attributes",
        # Users
        "get_me",
        "list_users",
        "search_users",
        "get_user",
        "create_user",
        "update_user",
        # Organizations
        "list_organizations",
        "search_organizations",
        "get_organization",
        "create_organization",
        "update_organization",
        # Groups
        "list_groups",
        "get_group",
        # Tags
        "list_object_tags",
        "list_all_tags",
        "search_tags",
        "add_tag",
        "remove_tag",
        # Reference data
        "list_ticket_states",
        "list_ticket_priorities",
        "list_roles",
        "get_zammad_version",
        # Notifications and mentions
        "list_my_notifications",
        "mark_notification_read",
        "mark_all_notifications_read",
        "list_ticket_subscribers",
        "subscribe_to_ticket",
    ]
)


async def test_golden_tool_inventory(mcp_and_ctx) -> None:  # type: ignore[no-untyped-def]
    mcp, _ = mcp_and_ctx
    assert sorted(await _tools(mcp)) == EXPECTED_TOOLS


async def test_declared_counts_match_registrations() -> None:
    """Each module's ``return N`` must equal what it actually registered.

    The count feeds the boot log line; a drift there means the operator is told
    a tool count the server does not have.
    """
    for name, module in MODULES.items():
        mcp: FastMCP = FastMCP(f"test-{name}")
        declared = module.register(mcp, RecordingCtx())
        registered = len(await mcp.list_tools(run_middleware=False))
        assert declared == registered, (
            f"{name}.register() returns {declared} but registered {registered}"
        )


# ── descriptions must not reference parameters that do not exist ─────────────

# Words that look like parameters in prose but are values, JSON keys or
# Zammad-side field names rather than tool parameters.
_PROSE_ALLOWLIST = {
    "note",
    "email",
    "phone",
    "web",
    "chat",
    "id",
    "name",
    "type",
    "internal",
    "true",
    "false",
    "text/plain",
    "text/html",
    "cursor",
}


async def test_descriptions_only_name_real_parameters(mcp_and_ctx) -> None:  # type: ignore[no-untyped-def]
    """`create_ticket_article` told the model to pass `type`; the schema had
    `article_type`, and `additionalProperties: false` made every such call fail.
    Catch that class of defect for every tool."""
    mcp, _ = mcp_and_ctx
    problems: list[str] = []
    for tool_name, tool in (await _tools(mcp)).items():
        schema = tool.parameters or {}
        params = set(schema.get("properties", {}))
        # `backticked` identifiers in the description that look like parameters
        for token in re.findall(r"`([a-z][a-z0-9_]{2,})`", tool.description or ""):
            if token in params or token in _PROSE_ALLOWLIST:
                continue
            # A reference to another tool is fine.
            if token in EXPECTED_TOOLS:
                continue
            problems.append(f"{tool_name}: description references `{token}`, not a parameter")
    assert not problems, "\n".join(problems)


# ── annotations ──────────────────────────────────────────────────────────────

READ_ONLY_PREFIXES = ("list_", "search_", "get_", "count_")

# The rule, so this set is derivable rather than a list of opinions:
#
#   destructiveHint=True  — the call modifies or removes state that OTHER people
#                           depend on, and no tool in this surface trivially
#                           undoes it.
#   destructiveHint=False — the call is purely additive, OR it changes state that
#                           belongs solely to the caller and another tool here
#                           puts it straight back (mark_*_read,
#                           unsubscribe_from_ticket).
#
# The distinction is not cosmetic: MCP clients use it to decide whether to ask
# the human first, so a wrong True buries real approvals in noise and a wrong
# False lets an agent overwrite a hundred tickets unattended.
DESTRUCTIVE_TOOLS = {
    # single-ticket overwrites
    "update_ticket",
    "update_ticket_title",
    "reassign_ticket_customer",
    "delete_ticket",
    # bulk overwrites — these are the ones that most need a human in the loop
    "update_tickets",
    "apply_macro_to_tickets",
    # irreversible or cross-object structural changes
    "merge_tickets",
    "unlink_tickets",
    # article corrections, incl. changing what a customer can see
    "set_article_visibility",
    "delete_ticket_article",
    # shared records
    "set_checklist_item",
    "update_user",
    "update_organization",
    "remove_tag",
}


async def test_read_only_tools_are_annotated_read_only(mcp_and_ctx) -> None:  # type: ignore[no-untyped-def]
    mcp, _ = mcp_and_ctx
    for name, tool in (await _tools(mcp)).items():
        if not name.startswith(READ_ONLY_PREFIXES):
            continue
        assert tool.annotations is not None, f"{name} has no annotations"
        assert tool.annotations.readOnlyHint is True, f"{name} should be readOnlyHint"
        assert tool.annotations.destructiveHint is False, f"{name} should not be destructive"


async def test_only_overwriting_tools_are_destructive(mcp_and_ctx) -> None:  # type: ignore[no-untyped-def]
    mcp, _ = mcp_and_ctx
    for name, tool in (await _tools(mcp)).items():
        assert tool.annotations is not None, f"{name} has no annotations"
        expected = name in DESTRUCTIVE_TOOLS
        assert tool.annotations.destructiveHint is expected, (
            f"{name}: destructiveHint should be {expected}. Additive writes "
            "(create_*, add_*, reply_*, subscribe_*, mark_*) are not destructive."
        )


# ── request shapes ───────────────────────────────────────────────────────────


async def _call(mcp: FastMCP, name: str, **kwargs: Any) -> None:
    await (await _tools(mcp))[name].run(kwargs)


@pytest.mark.parametrize(
    ("tool_name", "kwargs", "method", "path"),
    [
        ("list_tickets", {}, "GET", "/tickets"),
        ("search_tickets", {"query": "printer"}, "GET", "/tickets/search"),
        ("get_ticket", {"ticket_id": 7}, "GET", "/tickets/7"),
        ("delete_ticket", {"ticket_id": 7}, "DELETE", "/tickets/7"),
        ("list_ticket_articles", {"ticket_id": 7}, "GET", "/ticket_articles/by_ticket/7"),
        ("get_ticket_article", {"article_id": 3}, "GET", "/ticket_articles/3"),
        ("get_me", {}, "GET", "/users/me"),
        ("list_users", {}, "GET", "/users"),
        ("search_users", {"query": "aya"}, "GET", "/users/search"),
        ("get_user", {"user_id": 4}, "GET", "/users/4"),
        ("list_organizations", {}, "GET", "/organizations"),
        ("search_organizations", {"query": "acme"}, "GET", "/organizations/search"),
        ("get_organization", {"organization_id": 2}, "GET", "/organizations/2"),
        ("list_ticket_states", {}, "GET", "/ticket_states"),
        ("list_ticket_priorities", {}, "GET", "/ticket_priorities"),
        ("list_roles", {}, "GET", "/roles"),
        ("get_zammad_version", {}, "GET", "/version"),
    ],
)
async def test_request_verb_and_path(  # type: ignore[no-untyped-def]
    mcp_and_ctx, tool_name: str, kwargs: dict[str, Any], method: str, path: str
) -> None:
    mcp, ctx = mcp_and_ctx
    await _call(mcp, tool_name, **kwargs)
    assert ctx.last["method"] == method
    assert ctx.last["path"] == path


async def test_search_tools_send_page_so_pagination_works(mcp_and_ctx) -> None:  # type: ignore[no-untyped-def]
    """Zammad computes offset = (page - 1) * limit and defaults page to 1.

    A search that never sends ``page`` is structurally pinned to the first page
    - result 26 is unreachable. Pin the parameter for all three search tools.
    """
    mcp, ctx = mcp_and_ctx
    for tool_name, kwargs in (
        ("search_tickets", {"query": "x", "page": 3}),
        ("search_users", {"query": "x", "page": 3}),
        ("search_organizations", {"query": "x", "page": 3}),
    ):
        await _call(mcp, tool_name, **kwargs)
        params = ctx.last["params"]
        assert params["page"] == 3, f"{tool_name} dropped `page`"
        assert params["with_total_count"] == "true", f"{tool_name} dropped the total count"


async def test_reply_to_customer_is_always_visible(mcp_and_ctx) -> None:  # type: ignore[no-untyped-def]
    """The reason the article tools were split.

    ``{"type": "email", "internal": true}`` makes Zammad send the mail and then
    hide it from the customer in their own ticket view. This tool must make
    that state unreachable.
    """
    mcp, ctx = mcp_and_ctx
    await _call(mcp, "reply_to_customer", ticket_id=7, body="fixed", article_type="email")
    payload = ctx.last["json"]
    assert ctx.last["path"] == "/ticket_articles"
    assert payload["internal"] is False
    assert payload["type"] == "email"

    tool = (await _tools(mcp))["reply_to_customer"]
    assert "internal" not in (tool.parameters or {}).get("properties", {}), (
        "reply_to_customer must not expose `internal` - that is the whole point"
    )


async def test_add_internal_note_is_always_hidden(mcp_and_ctx) -> None:  # type: ignore[no-untyped-def]
    mcp, ctx = mcp_and_ctx
    await _call(mcp, "add_internal_note", ticket_id=7, body="checked the logs")
    payload = ctx.last["json"]
    assert payload["internal"] is True
    assert payload["type"] == "note"

    tool = (await _tools(mcp))["add_internal_note"]
    assert "internal" not in (tool.parameters or {}).get("properties", {})


async def test_reply_rejects_a_non_customer_facing_type(mcp_and_ctx) -> None:  # type: ignore[no-untyped-def]
    mcp, _ = mcp_and_ctx
    with pytest.raises(Exception, match="article_type must be one of"):
        await _call(mcp, "reply_to_customer", ticket_id=7, body="x", article_type="fax")


async def test_create_ticket_opening_article_is_visible_by_default(mcp_and_ctx) -> None:  # type: ignore[no-untyped-def]
    mcp, ctx = mcp_and_ctx
    await _call(
        mcp,
        "create_ticket",
        title="Printer broken",
        group="Support",
        customer="c@example.com",
        article_body="It smokes.",
    )
    payload = ctx.last["json"]
    assert ctx.last["method"] == "POST"
    assert ctx.last["path"] == "/tickets"
    assert payload["article"]["internal"] is False


async def test_create_ticket_publishes_ticket_type_not_type(mcp_and_ctx) -> None:  # type: ignore[no-untyped-def]
    """`Field(alias="type")` used to publish this parameter as `type`.

    That collided with the article type an LLM was told to send, so
    ``create_ticket(..., type='email')`` was silently accepted, set the TICKET
    type, and left the opening article an internal note.
    """
    mcp, _ = mcp_and_ctx
    props = ((await _tools(mcp))["create_ticket"].parameters or {}).get("properties", {})
    assert "ticket_type" in props
    assert "type" not in props


async def test_update_ticket_without_fields_raises_tool_error(mcp_and_ctx) -> None:  # type: ignore[no-untyped-def]
    mcp, _ = mcp_and_ctx
    with pytest.raises(Exception, match="at least one field"):
        await _call(mcp, "update_ticket", ticket_id=7)
