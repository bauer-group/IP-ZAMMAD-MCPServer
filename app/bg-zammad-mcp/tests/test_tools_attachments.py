"""Request-shape and guard-rail tests for the attachment tools.

The interesting behaviour here is not the two paths - it is everything the
module refuses to do:

* an oversized or binary attachment must be rejected from METADATA alone, with
  no download request issued at all,
* Zammad's string ``size`` and its four competing MIME preference keys must be
  normalised, because a caller that trusts the raw values sorts sizes
  lexicographically and reads every file as application/octet-stream,
* a description must not name a parameter the schema does not publish.

The recording context comes from the inventory suite so these tests exercise
the same ``ToolContext`` shape the real server implements.
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from fastmcp import FastMCP

from tests.test_tools_inventory import EXPECTED_TOOLS, RecordingCtx
from zammad.tools import attachments

TOOL_NAMES = {"list_ticket_attachments", "download_ticket_attachment"}


class ScriptedCtx(RecordingCtx):
    """RecordingCtx that answers each call from a queue.

    ``download_ticket_attachment`` makes two upstream calls with completely
    different bodies (article metadata, then file content), which the single
    fixed response of the base harness cannot express.
    """

    def __init__(self, responses: list[Any]) -> None:
        super().__init__()
        self._queue = list(responses)

    async def request(self, method: str, path: str, **kwargs: Any) -> Any:
        self.calls.append({"method": method, "path": path, **kwargs})
        return self._queue.pop(0) if self._queue else {}


def _article(**overrides: Any) -> dict[str, Any]:
    article: dict[str, Any] = {
        "id": 42,
        "type": "email",
        "created_at": "2026-01-02T03:04:05Z",
        "attachments": [
            {
                "id": 7,
                "store_file_id": 3,
                "filename": "log.txt",
                # Zammad's stores.size column is a varchar, so this is a STRING.
                "size": "12",
                "preferences": {"Content-Type": "text/plain; charset=utf-8"},
            }
        ],
    }
    article.update(overrides)
    return article


def _build(responses: list[Any]) -> tuple[FastMCP, ScriptedCtx]:
    mcp: FastMCP = FastMCP("test-attachments")
    ctx = ScriptedCtx(responses)
    attachments.register(mcp, ctx)
    return mcp, ctx


async def _tools(mcp: FastMCP) -> dict[str, Any]:
    return {tool.name: tool for tool in await mcp.list_tools(run_middleware=False)}


async def _call(mcp: FastMCP, name: str, /, **kwargs: Any) -> Any:
    # positional-only: several tools have their own `name` parameter
    # (create_organization, for one), which would collide otherwise.
    return await (await _tools(mcp))[name].run(kwargs)


# ── inventory / annotations / descriptions ───────────────────────────────────


async def test_registers_exactly_what_it_declares() -> None:
    mcp: FastMCP = FastMCP("test-attachments")
    declared = attachments.register(mcp, RecordingCtx())
    assert declared == len(await mcp.list_tools(run_middleware=False)) == len(TOOL_NAMES)
    assert set(await _tools(mcp)) == TOOL_NAMES


async def test_both_tools_are_read_only_and_non_destructive() -> None:
    mcp, _ = _build([])
    for name, tool in (await _tools(mcp)).items():
        assert tool.annotations is not None, f"{name} has no annotations"
        assert tool.annotations.readOnlyHint is True, f"{name} only ever issues GETs"
        assert tool.annotations.destructiveHint is False


async def test_descriptions_only_name_real_parameters_or_tools() -> None:
    """A backticked identifier that is not a parameter is an instruction the
    model cannot follow - `additionalProperties: false` rejects the call."""
    mcp, _ = _build([])
    problems: list[str] = []
    known = set(EXPECTED_TOOLS) | TOOL_NAMES
    for name, tool in (await _tools(mcp)).items():
        params = set((tool.parameters or {}).get("properties", {}))
        for token in re.findall(r"`([a-z][a-z0-9_]{2,})`", tool.description or ""):
            if token not in params and token not in known:
                problems.append(f"{name}: description references `{token}`")
    assert not problems, "\n".join(problems)


# ── list_ticket_attachments ──────────────────────────────────────────────────


async def test_list_reads_the_ticket_articles_and_flattens_them() -> None:
    mcp, ctx = _build(
        [
            [
                _article(),
                _article(
                    id=43,
                    attachments=[
                        {
                            "id": 8,
                            "filename": "logo.png",
                            "size": "2048",
                            "preferences": {
                                "Mime-Type": "image/png",
                                "Content-Disposition": "inline",
                            },
                        },
                        {"id": 9, "filename": "no-preferences.bin", "size": None},
                    ],
                ),
            ]
        ]
    )
    result = await _call(mcp, "list_ticket_attachments", ticket_id=5)

    assert ctx.last["method"] == "GET"
    assert ctx.last["path"] == "/ticket_articles/by_ticket/5"

    rows = result.structured_content["items"]
    assert [row["attachment_id"] for row in rows] == [7, 8, 9]
    assert rows[0] == {
        "ticket_id": 5,
        "article_id": 42,
        "article_type": "email",
        "article_created_at": "2026-01-02T03:04:05Z",
        "attachment_id": 7,
        "filename": "log.txt",
        # the string "12" became a real integer, and the charset parameter is gone
        "size_bytes": 12,
        "mime_type": "text/plain",
        "inline": False,
    }
    # 'Mime-Type' is honoured when 'Content-Type' is absent, and an inline
    # disposition is surfaced so a signature logo is distinguishable.
    assert rows[1]["mime_type"] == "image/png"
    assert rows[1]["inline"] is True
    # A file with no preferences at all falls back the way Zammad itself does.
    assert rows[2]["mime_type"] == "application/octet-stream"
    assert rows[2]["size_bytes"] is None


async def test_list_returns_empty_for_articles_without_files() -> None:
    mcp, _ = _build([[{"id": 1, "attachments": []}, {"id": 2}]])
    result = await _call(mcp, "list_ticket_attachments", ticket_id=5)
    assert result.structured_content["items"] == []


@pytest.mark.parametrize(
    ("key", "value", "expected"),
    [
        ("Content-Type", "application/pdf", "application/pdf"),
        ("Mime-Type", "IMAGE/JPEG", "image/jpeg"),
        ("content_type", "text/csv", "text/csv"),
        ("mime_type", "application/zip", "application/zip"),
    ],
)
async def test_mime_type_is_read_from_any_of_the_four_preference_keys(
    key: str, value: str, expected: str
) -> None:
    mcp, _ = _build(
        [[_article(attachments=[{"id": 1, "filename": "f", "preferences": {key: value}}])]]
    )
    result = await _call(mcp, "list_ticket_attachments", ticket_id=5)
    assert result.structured_content["items"][0]["mime_type"] == expected


# ── download_ticket_attachment ───────────────────────────────────────────────


async def test_download_fetches_metadata_then_the_file() -> None:
    mcp, ctx = _build([_article(), "line one\nline two"])
    result = await _call(
        mcp, "download_ticket_attachment", ticket_id=5, article_id=42, attachment_id=7
    )

    assert [(call["method"], call["path"]) for call in ctx.calls] == [
        ("GET", "/ticket_articles/42"),
        ("GET", "/ticket_attachment/5/42/7"),
    ]
    assert result.structured_content == {
        "ticket_id": 5,
        "article_id": 42,
        "attachment_id": 7,
        "filename": "log.txt",
        "mime_type": "text/plain",
        "size_bytes": 12,
        "content": "line one\nline two",
    }


async def test_download_rejects_an_attachment_not_on_that_article() -> None:
    mcp, ctx = _build([_article()])
    with pytest.raises(Exception, match="no attachment with id 99"):
        await _call(
            mcp, "download_ticket_attachment", ticket_id=5, article_id=42, attachment_id=99
        )
    assert len(ctx.calls) == 1, "must not guess at a download for an unknown attachment"


async def test_download_refuses_a_binary_type_without_transferring_it() -> None:
    """A PNG would arrive as U+FFFD soup, so it is refused from metadata alone."""
    mcp, ctx = _build(
        [
            _article(
                attachments=[
                    {
                        "id": 7,
                        "filename": "screenshot.png",
                        "size": "2048",
                        "preferences": {"Content-Type": "image/png"},
                    }
                ]
            )
        ]
    )
    with pytest.raises(Exception, match="image/png, which is binary"):
        await _call(
            mcp, "download_ticket_attachment", ticket_id=5, article_id=42, attachment_id=7
        )
    assert len(ctx.calls) == 1, "the file must never be requested"


async def test_download_refuses_an_oversized_file_before_transferring_it() -> None:
    mcp, ctx = _build(
        [
            _article(
                attachments=[
                    {
                        "id": 7,
                        "filename": "huge.log",
                        "size": str(200 * 1024 * 1024),
                        "preferences": {"Content-Type": "text/plain"},
                    }
                ]
            )
        ]
    )
    with pytest.raises(Exception, match="over the 5242880 byte limit"):
        await _call(
            mcp, "download_ticket_attachment", ticket_id=5, article_id=42, attachment_id=7
        )
    assert len(ctx.calls) == 1, "a 200 MB file must not be pulled just to reject it"


async def test_download_measures_the_payload_when_zammad_reports_no_size() -> None:
    mcp, _ = _build(
        [
            _article(
                attachments=[
                    {
                        "id": 7,
                        "filename": "unknown-size.txt",
                        "preferences": {"Content-Type": "text/plain"},
                    }
                ]
            ),
            "abcde",
        ]
    )
    result = await _call(
        mcp, "download_ticket_attachment", ticket_id=5, article_id=42, attachment_id=7
    )
    assert result.structured_content["size_bytes"] == 5


async def test_download_enforces_the_limit_after_the_fact_when_size_was_unknown() -> None:
    mcp, _ = _build(
        [
            _article(
                attachments=[
                    {
                        "id": 7,
                        "filename": "unknown-size.txt",
                        "preferences": {"Content-Type": "text/plain"},
                    }
                ]
            ),
            "x" * 50,
        ]
    )
    with pytest.raises(Exception, match="decoded to 50 bytes"):
        await _call(
            mcp,
            "download_ticket_attachment",
            ticket_id=5,
            article_id=42,
            attachment_id=7,
            max_bytes=10,
        )


async def test_download_reserialises_a_json_attachment_to_text() -> None:
    """The context decodes by content type, so a .json file arrives parsed -
    `content` must still be a string, not sometimes a dict."""
    mcp, _ = _build(
        [
            _article(
                attachments=[
                    {
                        "id": 7,
                        "filename": "payload.json",
                        "size": "20",
                        "preferences": {"Content-Type": "application/json"},
                    }
                ]
            ),
            {"ok": True},
        ]
    )
    result = await _call(
        mcp, "download_ticket_attachment", ticket_id=5, article_id=42, attachment_id=7
    )
    content = result.structured_content["content"]
    assert isinstance(content, str)
    assert '"ok": true' in content


async def test_max_bytes_has_a_hard_ceiling_in_the_schema() -> None:
    """Without it the model just retries the size guard with a bigger number."""
    mcp, _ = _build([])
    schema = (await _tools(mcp))["download_ticket_attachment"].parameters or {}
    assert schema["properties"]["max_bytes"]["maximum"] == attachments.MAX_ALLOWED_BYTES
