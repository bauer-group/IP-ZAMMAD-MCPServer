"""Request-shape + reshaping tests for the audit/correction tools.

Three of the four tools here are one-request wrappers whose only real risk is a
wrong verb or path. The other two carry actual logic that has to be pinned:

* ``get_ticket_history`` reshapes Zammad's payload. If the trimming silently
  drops an entry, the tool answers "who closed this" with the wrong name - so
  the tests assert entry-for-entry survival and ordering, not just the shape.
* ``unsubscribe_from_ticket`` spans three requests, because Zammad has no
  "unsubscribe me" route. The tests pin the sequence, the caller-matching, and
  the not-subscribed branch that must NOT issue a DELETE.
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from fastmcp import FastMCP

from tests.test_tools_inventory import RecordingCtx
from zammad.tools import history


class ScriptedCtx(RecordingCtx):
    """A RecordingCtx that answers each call from a queue.

    ``unsubscribe_from_ticket`` makes three requests with three different
    response shapes, which the single-response base class cannot express.
    """

    def __init__(self, responses: list[Any]) -> None:
        super().__init__()
        self._queue = list(responses)

    async def request(self, method: str, path: str, **kwargs: Any) -> Any:
        self.calls.append({"method": method, "path": path, **kwargs})
        return self._queue.pop(0) if self._queue else {}


def _register(ctx: RecordingCtx) -> FastMCP:
    mcp: FastMCP = FastMCP("history-test")
    declared = history.register(mcp, ctx)
    assert declared == 4, "history.register() must report what it registered"
    return mcp


@pytest.fixture
def history_tools() -> tuple[FastMCP, RecordingCtx]:
    ctx = RecordingCtx()
    return _register(ctx), ctx


async def _tools(mcp: FastMCP) -> dict[str, Any]:
    return {tool.name: tool for tool in await mcp.list_tools(run_middleware=False)}


async def _call(mcp: FastMCP, name: str, /, **kwargs: Any) -> Any:
    # positional-only: several tools have their own `name` parameter
    # (create_organization, for one), which would collide otherwise.
    return await (await _tools(mcp))[name].run(kwargs)


def _structured(result: Any) -> Any:
    """The tool's own return value, unwrapped from the MCP result envelope."""
    return result.structured_content


# ── inventory + annotations ──────────────────────────────────────────────────

MODULE_TOOLS = {
    "get_ticket_history",
    "set_article_visibility",
    "delete_ticket_article",
    "unsubscribe_from_ticket",
}

# Tools this module's descriptions are allowed to point at, on top of its own.
SIBLING_TOOLS = {
    "list_ticket_articles",
    "reply_to_customer",
    "add_internal_note",
    "get_me",
    "list_ticket_subscribers",
    "subscribe_to_ticket",
}

# Backticked words that are values or Zammad-side field names, not parameters.
PROSE_ALLOWLIST = {"note", "email", "internal", "true", "false"}


async def test_module_registers_exactly_its_four_tools(history_tools) -> None:  # type: ignore[no-untyped-def]
    mcp, _ = history_tools
    assert set(await _tools(mcp)) == MODULE_TOOLS


async def test_descriptions_only_name_real_parameters(history_tools) -> None:  # type: ignore[no-untyped-def]
    """A description that names a parameter the schema does not publish sends
    the model into a call that ``additionalProperties: false`` rejects."""
    mcp, _ = history_tools
    problems: list[str] = []
    for name, tool in (await _tools(mcp)).items():
        params = set((tool.parameters or {}).get("properties", {}))
        allowed = params | MODULE_TOOLS | SIBLING_TOOLS | PROSE_ALLOWLIST
        for token in re.findall(r"`([a-z][a-z0-9_]{2,})`", tool.description or ""):
            if token not in allowed:
                problems.append(f"{name}: description references `{token}`")
    assert not problems, "\n".join(problems)


async def test_read_and_write_tools_are_annotated_correctly(history_tools) -> None:  # type: ignore[no-untyped-def]
    """Only the reader may be read-only; all three writers overwrite or remove
    existing state, so all three must be flagged destructive."""
    mcp, _ = history_tools
    tools = await _tools(mcp)

    reader = tools["get_ticket_history"].annotations
    assert reader is not None
    assert reader.readOnlyHint is True
    assert reader.destructiveHint is False

    # These two change what other people see, and nothing in this surface undoes
    # them — a client should ask a human first.
    for name in ("set_article_visibility", "delete_ticket_article"):
        annotations = tools[name].annotations
        assert annotations is not None, f"{name} has no annotations"
        assert annotations.readOnlyHint is False, f"{name} is a write"
        assert annotations.destructiveHint is True, f"{name} removes/overwrites shared state"

    # Unsubscribing removes a record that belongs solely to the caller, and
    # subscribe_to_ticket puts it straight back, so it does not warrant a
    # confirmation prompt — same reasoning as mark_notification_read.
    unsubscribe = tools["unsubscribe_from_ticket"].annotations
    assert unsubscribe is not None
    assert unsubscribe.readOnlyHint is False
    assert unsubscribe.destructiveHint is False


# ── get_ticket_history ───────────────────────────────────────────────────────

# A realistic slice of Ticket#history_get(true): the ticket's own rows, a
# folded-in article row, and a trigger-driven close. Note that Zammad omits
# value_from/value_to on the 'created' row and stamps the acting user even when
# an automation made the change.
HISTORY_PAYLOAD: dict[str, Any] = {
    "history": [
        {
            "id": 900,
            "o_id": 7,
            "created_by_id": 3,
            "created_at": "2026-07-01T08:00:00.000Z",
            "sourceable_type": None,
            "sourceable_id": None,
            "sourceable_name": None,
            "object": "Ticket",
            "type": "created",
        },
        {
            "id": 901,
            "o_id": 42,
            "related_o_id": 7,
            "created_by_id": 3,
            "created_at": "2026-07-01T08:05:00.000Z",
            "object": "Ticket::Article",
            "related_object": "Ticket",
            "type": "created",
        },
        {
            "id": 902,
            "o_id": 7,
            "created_by_id": 1,
            "created_at": "2026-07-02T09:30:00.000Z",
            "sourceable_type": "Trigger",
            "sourceable_id": 5,
            "sourceable_name": "auto-close after 24h",
            "object": "Ticket",
            "type": "updated",
            "attribute": "state",
            "value_from": "open",
            "value_to": "closed",
            "id_from": 2,
            "id_to": 4,
        },
    ],
    "assets": {
        "User": {
            "1": {"id": 1, "login": "-", "firstname": "-", "lastname": "", "email": ""},
            "3": {
                "id": 3,
                "login": "aya",
                "firstname": "Aya",
                "lastname": "Nguyen",
                "email": "aya@example.com",
            },
        },
        # The half this tool exists to throw away.
        "Ticket": {"7": {"id": 7, "title": "Printer smokes", "note": "x" * 500}},
        "TicketArticle": {"42": {"id": 42, "body": "y" * 500}},
    },
}


async def test_get_ticket_history_hits_the_verified_route() -> None:
    ctx = RecordingCtx(HISTORY_PAYLOAD)
    mcp = _register(ctx)
    await _call(mcp, "get_ticket_history", ticket_id=7)
    assert ctx.last["method"] == "GET"
    assert ctx.last["path"] == "/ticket_history/7"
    # The action reads only params[:id]; anything else would be dead weight.
    assert "params" not in ctx.last


async def test_history_keeps_every_entry_in_order() -> None:
    """Ordering and completeness are the product here - a dropped row is a
    wrong answer to 'who closed this and when'."""
    ctx = RecordingCtx(HISTORY_PAYLOAD)
    mcp = _register(ctx)
    result = _structured(await _call(mcp, "get_ticket_history", ticket_id=7))

    assert result["ticket_id"] == 7
    assert result["total_count"] == 3
    assert [entry["at"] for entry in result["items"]] == [
        "2026-07-01T08:00:00.000Z",
        "2026-07-01T08:05:00.000Z",
        "2026-07-02T09:30:00.000Z",
    ]


async def test_history_resolves_the_actor_and_the_change() -> None:
    ctx = RecordingCtx(HISTORY_PAYLOAD)
    mcp = _register(ctx)
    result = _structured(await _call(mcp, "get_ticket_history", ticket_id=7))
    close = result["items"][2]

    assert close["by_id"] == 1
    assert close["action"] == "updated"
    assert close["object"] == "Ticket"
    assert close["field"] == "state"
    assert close["from"] == "open"
    assert close["to"] == "closed"
    assert close["from_id"] == 2
    assert close["to_id"] == 4
    # The change came from a trigger, not from the user it was stamped with.
    assert close["via"] == "auto-close after 24h"
    assert close["via_type"] == "Trigger"


async def test_history_names_the_user_from_the_assets_blob() -> None:
    """JSON stringifies the asset keys, so an int lookup would silently yield
    a nameless log even though the data is right there."""
    ctx = RecordingCtx(HISTORY_PAYLOAD)
    mcp = _register(ctx)
    result = _structured(await _call(mcp, "get_ticket_history", ticket_id=7))
    assert result["items"][0]["by"] == "Aya Nguyen"


async def test_history_points_at_the_related_article() -> None:
    ctx = RecordingCtx(HISTORY_PAYLOAD)
    mcp = _register(ctx)
    result = _structured(await _call(mcp, "get_ticket_history", ticket_id=7))
    article_row = result["items"][1]
    assert article_row["object"] == "Ticket::Article"
    assert article_row["object_id"] == 42
    assert article_row["related_object"] == "Ticket"
    assert article_row["related_id"] == 7


async def test_history_drops_the_assets_blob() -> None:
    ctx = RecordingCtx(HISTORY_PAYLOAD)
    mcp = _register(ctx)
    result = _structured(await _call(mcp, "get_ticket_history", ticket_id=7))
    assert "assets" not in result
    assert all("id" not in entry for entry in result["items"]), (
        "the history row's own id is not addressable by any endpoint"
    )


async def test_history_survives_an_empty_log() -> None:
    ctx = RecordingCtx({"history": [], "assets": {}})
    mcp = _register(ctx)
    result = _structured(await _call(mcp, "get_ticket_history", ticket_id=7))
    assert result["items"] == []
    assert result["ticket_id"] == 7
    assert result["total_count"] == 0


# ── set_article_visibility ───────────────────────────────────────────────────


@pytest.mark.parametrize("internal", [True, False])
async def test_set_article_visibility_puts_a_real_boolean(  # type: ignore[no-untyped-def]
    history_tools, internal: bool
) -> None:
    """``internal`` travels in a JSON body, so it stays a real boolean - the
    lowercase-string dance only applies to query params."""
    mcp, ctx = history_tools
    await _call(mcp, "set_article_visibility", article_id=42, internal=internal)
    assert ctx.last["method"] == "PUT"
    assert ctx.last["path"] == "/ticket_articles/42"
    assert ctx.last["json"] == {"internal": internal}


async def test_set_article_visibility_sends_nothing_else(history_tools) -> None:  # type: ignore[no-untyped-def]
    """Zammad's update action ignores every field but internal (and
    preferences.highlight) outside import mode, and still returns 200 - so a
    stray key would be a silent lie about what was changed."""
    mcp, ctx = history_tools
    await _call(mcp, "set_article_visibility", article_id=42, internal=False)
    assert set(ctx.last["json"]) == {"internal"}


async def test_visibility_is_the_only_editable_field(history_tools) -> None:  # type: ignore[no-untyped-def]
    mcp, _ = history_tools
    props = ((await _tools(mcp))["set_article_visibility"].parameters or {}).get(
        "properties", {}
    )
    assert set(props) == {"article_id", "internal"}


# ── delete_ticket_article ────────────────────────────────────────────────────


async def test_delete_ticket_article_hits_the_verified_route(history_tools) -> None:  # type: ignore[no-untyped-def]
    mcp, ctx = history_tools
    result = _structured(await _call(mcp, "delete_ticket_article", article_id=42))
    assert ctx.last["method"] == "DELETE"
    assert ctx.last["path"] == "/ticket_articles/42"
    assert result == {"deleted": True, "article_id": 42}


# ── unsubscribe_from_ticket ──────────────────────────────────────────────────

ME = {"id": 3, "login": "aya", "firstname": "Aya", "lastname": "Nguyen"}
MENTIONS = {
    "mentions": [
        {"id": 11, "user_id": 9, "mentionable_type": "Ticket", "mentionable_id": 7},
        {"id": 12, "user_id": 3, "mentionable_type": "Ticket", "mentionable_id": 7},
    ]
}


async def test_unsubscribe_resolves_the_caller_then_deletes_their_mention() -> None:
    """Zammad exposes no 'unsubscribe me' route, so the caller's own mention id
    has to be looked up before it can be deleted."""
    ctx = ScriptedCtx([ME, MENTIONS, True])
    mcp = _register(ctx)
    result = _structured(await _call(mcp, "unsubscribe_from_ticket", ticket_id=7))

    assert [(call["method"], call["path"]) for call in ctx.calls] == [
        ("GET", "/users/me"),
        ("GET", "/mentions"),
        ("DELETE", "/mentions/12"),
    ]
    assert ctx.calls[1]["params"] == {"mentionable_type": "Ticket", "mentionable_id": 7}
    assert result == {
        "unsubscribed": True,
        "ticket_id": 7,
        "user_id": 3,
        "mention_id": 12,
    }


async def test_unsubscribe_never_touches_another_users_subscription() -> None:
    """Mention 11 belongs to user 9. Zammad would answer 403, but the tool must
    not even offer the request - picking the wrong row would read as success."""
    ctx = ScriptedCtx([ME, MENTIONS, True])
    mcp = _register(ctx)
    await _call(mcp, "unsubscribe_from_ticket", ticket_id=7)
    assert ctx.last["path"] != "/mentions/11"


async def test_unsubscribe_when_not_subscribed_is_a_reported_no_op() -> None:
    """Already the desired state. Issuing a DELETE anyway would 403; failing
    would make the model retry something that is already true."""
    ctx = ScriptedCtx([ME, {"mentions": []}])
    mcp = _register(ctx)
    result = _structured(await _call(mcp, "unsubscribe_from_ticket", ticket_id=7))

    assert result["unsubscribed"] is False
    assert result["ticket_id"] == 7
    assert result["reason"]
    assert [call["method"] for call in ctx.calls] == ["GET", "GET"]


async def test_unsubscribe_raises_when_the_caller_cannot_be_identified() -> None:
    ctx = ScriptedCtx([{"error": "no session"}, MENTIONS])
    mcp = _register(ctx)
    with pytest.raises(Exception, match="Could not determine the calling Zammad user"):
        await _call(mcp, "unsubscribe_from_ticket", ticket_id=7)
    assert len(ctx.calls) == 1, "no follow-up request should be made"
