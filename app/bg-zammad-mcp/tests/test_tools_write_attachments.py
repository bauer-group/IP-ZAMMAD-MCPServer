"""Attachments on the article-creating tools.

Zammad has no endpoint that attaches a file to an existing article, so writing
always creates one. Putting the parameter on the existing write tools keeps
visibility encoded in the tool NAME - the trap articles.py was split in two to
close - and keeps message plus file in one article, so the customer gets one
mail rather than two.
"""

from __future__ import annotations

import base64
from typing import Any

from fastmcp import FastMCP

from tests.test_tools_inventory import RecordingCtx
from zammad.tools import articles, tickets


class Ctx(RecordingCtx):
    def __init__(self, uploads_enabled: bool = True) -> None:
        super().__init__()
        self.settings = type(
            "S",
            (),
            {
                "zammad_attachment_upload_enabled": uploads_enabled,
                "zammad_attachment_max_upload_bytes": 10 * 1024 * 1024,
                "zammad_attachment_max_article_bytes": 25 * 1024 * 1024,
            },
        )()


async def _tools(mcp: FastMCP) -> dict[str, Any]:
    return {tool.name: tool for tool in await mcp.list_tools(run_middleware=False)}


async def _call(mcp: FastMCP, name: str, /, **kwargs: Any) -> Any:
    return await (await _tools(mcp))[name].run(kwargs)


def _build(module: Any, uploads_enabled: bool = True) -> tuple[FastMCP, Ctx]:
    mcp: FastMCP = FastMCP("test")
    ctx = Ctx(uploads_enabled)
    module.register(mcp, ctx)
    return mcp, ctx


# ── the files ride along with the message ────────────────────────────────────


async def test_a_customer_reply_carries_its_file_in_the_same_article() -> None:
    mcp, ctx = _build(articles)
    await _call(
        mcp,
        "reply_to_customer",
        ticket_id=4711,
        body="Anbei die Auswertung.",
        attachments=[{"filename": "auswertung.csv", "text": "a;b\n1;2\n"}],
    )
    assert len(ctx.calls) == 1, "message and file must be ONE article, not two"
    payload = ctx.last["json"]
    assert payload["internal"] is False
    assert payload["attachments"] == [
        {
            "filename": "auswertung.csv",
            "data": base64.b64encode(b"a;b\n1;2\n").decode(),
            "mime-type": "text/csv",
        }
    ]


async def test_an_internal_note_carries_its_file_and_stays_internal() -> None:
    mcp, ctx = _build(articles)
    await _call(
        mcp,
        "add_internal_note",
        ticket_id=4711,
        body="Log vom Kunden.",
        attachments=[{"filename": "debug.log", "text": "line\n"}],
    )
    payload = ctx.last["json"]
    assert payload["internal"] is True
    assert payload["attachments"][0]["filename"] == "debug.log"


async def test_create_ticket_can_open_with_an_attachment() -> None:
    mcp, ctx = _build(tickets)
    await _call(
        mcp,
        "create_ticket",
        title="Angebot",
        group="Support",
        customer="kunde@example.com",
        article_body="Anbei.",
        article_visibility="customer_visible",
        attachments=[{"filename": "angebot.txt", "text": "Position 1\n"}],
    )
    article = ctx.last["json"]["article"]
    assert article["attachments"][0]["filename"] == "angebot.txt"


async def test_no_attachments_leaves_the_payload_untouched() -> None:
    mcp, ctx = _build(articles)
    await _call(mcp, "reply_to_customer", ticket_id=4711, body="Kurze Antwort.")
    assert "attachments" not in ctx.last["json"], (
        "an empty key would make every reply look like it carried files"
    )


# ── the kill switch ──────────────────────────────────────────────────────────


async def test_disabling_uploads_removes_the_parameter_from_the_schema() -> None:
    """Not a runtime rejection the model discovers by trying."""
    mcp, _ = _build(articles, uploads_enabled=False)
    for name in ("reply_to_customer", "add_internal_note"):
        schema = (await _tools(mcp))[name].parameters or {}
        assert "attachments" not in schema.get("properties", {}), name


async def test_disabling_uploads_also_covers_create_ticket() -> None:
    mcp, _ = _build(tickets, uploads_enabled=False)
    schema = (await _tools(mcp))["create_ticket"].parameters or {}
    assert "attachments" not in schema.get("properties", {})


async def test_disabling_uploads_keeps_the_tools_working() -> None:
    mcp, ctx = _build(articles, uploads_enabled=False)
    await _call(mcp, "reply_to_customer", ticket_id=4711, body="Antwort.")
    assert ctx.last["json"]["body"] == "Antwort."
    assert "attachments" not in ctx.last["json"]


async def test_the_tools_keep_their_module_tags_when_uploads_are_disabled() -> None:
    """The hide path registers through add_tool, which _Tagging must also cover
    or the tools land untagged - a silent hole rather than an error."""
    from server import _MODULE_TAGS, _Tagging

    mcp: FastMCP = FastMCP("test")
    articles.register(_Tagging(mcp, _MODULE_TAGS["articles"]), Ctx(uploads_enabled=False))
    tool = (await _tools(mcp))["reply_to_customer"]
    assert _MODULE_TAGS["articles"] <= set(tool.tags or set())


async def test_the_tools_keep_their_module_tags_when_uploads_are_enabled() -> None:
    from server import _MODULE_TAGS, _Tagging

    mcp: FastMCP = FastMCP("test")
    articles.register(_Tagging(mcp, _MODULE_TAGS["articles"]), Ctx(uploads_enabled=True))
    tool = (await _tools(mcp))["reply_to_customer"]
    assert _MODULE_TAGS["articles"] <= set(tool.tags or set())
