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

import base64
import re
from typing import Any

import httpx
import pytest
from fastmcp import FastMCP
from mcp.types import EmbeddedResource, ImageContent

from tests.test_tools_inventory import EXPECTED_TOOLS, RecordingCtx
from zammad.tools import attachments

TOOL_NAMES = {"list_ticket_attachments", "download_ticket_attachment"}


class ScriptedCtx(RecordingCtx):
    """RecordingCtx that answers each call from a queue.

    ``download_ticket_attachment`` makes two upstream calls with completely
    different bodies (article metadata, then file content), which the single
    fixed response of the base harness cannot express.
    """

    def __init__(self, responses: list[Any], raw: list[Any] | None = None) -> None:
        super().__init__()
        self._queue = list(responses)
        self._raw_queue = list(raw or [])

    async def request(self, method: str, path: str, **kwargs: Any) -> Any:
        self.calls.append({"method": method, "path": path, **kwargs})
        return self._queue.pop(0) if self._queue else {}

    async def request_raw(self, method: str, path: str, **kwargs: Any) -> Any:
        self.calls.append({"method": method, "path": path, **kwargs})
        assert self._raw_queue, f"unexpected request_raw({method} {path})"
        return self._raw_queue.pop(0)


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


def _raw(content: bytes, content_type: str) -> httpx.Response:
    return httpx.Response(
        status_code=200,
        content=content,
        headers={"content-type": content_type},
        request=httpx.Request("GET", "https://zammad.example/x"),
    )


def _build_raw(responses: list[Any], raw: list[Any]) -> tuple[FastMCP, ScriptedCtx]:
    mcp: FastMCP = FastMCP("test-attachments")
    ctx = ScriptedCtx(responses, raw)
    attachments.register(mcp, ctx)
    return mcp, ctx


def _one(filename: str, mime: str, size: int | None) -> dict[str, Any]:
    """A one-attachment article, for the download tests."""
    return _article(
        attachments=[
            {
                "id": 7,
                "filename": filename,
                **({} if size is None else {"size": str(size)}),
                "preferences": {"Content-Type": mime},
            }
        ]
    )


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
    mcp, ctx = _build_raw([_article()], [_raw(b"line one\nline two", "text/plain")])
    result = await _call(
        mcp, "download_ticket_attachment", ticket_id=5, article_id=42, attachment_id=7
    )

    assert [(call["method"], call["path"]) for call in ctx.calls] == [
        ("GET", "/ticket_articles/42"),
        ("GET", "/ticket_attachment/5/42/7"),
    ]
    # Exhaustive on purpose: this is the published contract, and an added key
    # is as much a change to it as a removed one.
    assert result.structured_content == {
        "ticket_id": 5,
        "article_id": 42,
        "attachment_id": 7,
        "filename": "log.txt",
        "mime_type": "text/plain",
        "detected_mime_type": "text/plain",
        "size_bytes": 12,
        "content": "line one\nline two",
        "content_kind": "text",
        "extraction": {"status": "not_applicable", "tool": None, "reason": None},
        "decoding": {"charset": "utf-8", "lossy": False},
    }


async def test_download_rejects_an_attachment_not_on_that_article() -> None:
    mcp, ctx = _build([_article()])
    with pytest.raises(Exception, match="no attachment with id 99"):
        await _call(
            mcp, "download_ticket_attachment", ticket_id=5, article_id=42, attachment_id=99
        )
    assert len(ctx.calls) == 1, "must not guess at a download for an unknown attachment"


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
    mcp, _ = _build_raw(
        [_one("unknown-size.txt", "text/plain", None)], [_raw(b"abcde", "text/plain")]
    )
    result = await _call(
        mcp, "download_ticket_attachment", ticket_id=5, article_id=42, attachment_id=7
    )
    assert result.structured_content["size_bytes"] == 5


async def test_download_enforces_the_limit_after_the_fact_when_size_was_unknown() -> None:
    mcp, _ = _build_raw(
        [_one("unknown-size.txt", "text/plain", None)], [_raw(b"x" * 50, "text/plain")]
    )
    with pytest.raises(Exception, match="is 50 bytes"):
        await _call(
            mcp,
            "download_ticket_attachment",
            ticket_id=5,
            article_id=42,
            attachment_id=7,
            max_bytes=10,
        )


async def test_a_json_attachment_arrives_as_its_literal_file_text() -> None:
    """The raw path no longer parses by content type, so a .json file is the
    bytes on disk rather than a re-serialised dict."""
    payload = b'{\n  "ok": true\n}'
    mcp, _ = _build_raw(
        [_one("payload.json", "application/json", len(payload))],
        [_raw(payload, "application/json")],
    )
    result = await _call(
        mcp, "download_ticket_attachment", ticket_id=5, article_id=42, attachment_id=7
    )
    content = result.structured_content["content"]
    assert isinstance(content, str)
    assert '"ok": true' in content


async def test_max_bytes_ceiling_and_default_come_from_settings() -> None:
    """Without a ceiling the model just retries the size guard with a bigger
    number; hardcoding it put the knob out of an operator's reach."""

    ctx = ScriptedCtx([])
    # RecordingCtx.__init__ sets self.settings = None, so a class attribute
    # would be shadowed - assign on the instance.
    ctx.settings = type(
        "S",
        (),
        {
            "zammad_attachment_max_read_bytes": 1234,
            "zammad_attachment_read_ceiling_bytes": 5678,
        },
    )()

    mcp: FastMCP = FastMCP("test-attachments")
    attachments.register(mcp, ctx)
    schema = (await _tools(mcp))["download_ticket_attachment"].parameters or {}
    assert schema["properties"]["max_bytes"]["maximum"] == 5678
    assert schema["properties"]["max_bytes"]["default"] == 1234


# ── what the read path returns now ───────────────────────────────────────────

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + bytes(range(256))


async def test_a_png_comes_back_as_an_image_block() -> None:
    """The whole point: the model must be able to SEE a screenshot."""
    mcp, _ = _build_raw(
        [_one("screenshot.png", "image/png", len(PNG_BYTES))],
        [_raw(PNG_BYTES, "image/png")],
    )
    result = await _call(
        mcp, "download_ticket_attachment", ticket_id=5, article_id=42, attachment_id=7
    )

    images = [block for block in result.content if isinstance(block, ImageContent)]
    assert len(images) == 1
    assert images[0].mimeType == "image/png"
    assert base64.b64decode(images[0].data) == PNG_BYTES, "bytes must be intact"
    assert result.structured_content["content_kind"] == "image"
    assert result.structured_content["content"] is None


async def test_rtf_mislabelled_as_msword_is_no_longer_refused() -> None:
    """The regression that started this: refused as binary, while being text."""
    rtf = rb"{\rtf1\ansi Technische Daten\par}"
    mcp, _ = _build_raw(
        [_one("Technische-Daten-Liquid-Liquid.rtf", "application/msword", len(rtf))],
        [_raw(rtf, "application/msword")],
    )
    result = await _call(
        mcp, "download_ticket_attachment", ticket_id=5, article_id=42, attachment_id=7
    )
    sc = result.structured_content
    assert sc["mime_type"] == "application/msword", "the declared label is still reported"
    assert sc["detected_mime_type"] == "application/rtf"
    assert "Technische Daten" in sc["content"]


async def test_an_unknown_binary_comes_back_as_a_blob_not_an_error() -> None:
    blob = b"\x00\x01\x02\x03garbage\xff"
    mcp, _ = _build_raw(
        [_one("thing.bin", "application/octet-stream", len(blob))],
        [_raw(blob, "application/octet-stream")],
    )
    result = await _call(
        mcp, "download_ticket_attachment", ticket_id=5, article_id=42, attachment_id=7
    )
    embedded = [b for b in result.content if isinstance(b, EmbeddedResource)]
    assert len(embedded) == 1
    assert base64.b64decode(embedded[0].resource.blob) == blob
    assert result.structured_content["content_kind"] == "blob"


async def test_mode_raw_forces_a_blob_for_a_text_file() -> None:
    mcp, _ = _build_raw([_article()], [_raw(b"line one", "text/plain")])
    result = await _call(
        mcp,
        "download_ticket_attachment",
        ticket_id=5,
        article_id=42,
        attachment_id=7,
        mode="raw",
    )
    assert result.structured_content["content_kind"] == "blob"


async def test_mode_text_forces_a_decode_for_an_unrecognised_file() -> None:
    mcp, _ = _build_raw(
        [_one("weird", "application/x-nonsense", 5)],
        [_raw(b"hallo", "application/x-nonsense")],
    )
    result = await _call(
        mcp,
        "download_ticket_attachment",
        ticket_id=5,
        article_id=42,
        attachment_id=7,
        mode="text",
    )
    assert result.structured_content["content"] == "hallo"
    assert result.structured_content["content_kind"] == "text"


async def test_an_unknown_mode_is_refused_before_any_request() -> None:
    mcp, ctx = _build_raw([], [])
    with pytest.raises(Exception, match="mode must be one of"):
        await _call(
            mcp,
            "download_ticket_attachment",
            ticket_id=5,
            article_id=42,
            attachment_id=7,
            mode="sideways",
        )
    assert ctx.calls == []


async def test_a_lossy_decode_is_reported_as_such() -> None:
    mcp, _ = _build_raw(
        [_one("broken.txt", "text/plain", 3)], [_raw(b"\x81\x8d\x90", "text/plain")]
    )
    result = await _call(
        mcp, "download_ticket_attachment", ticket_id=5, article_id=42, attachment_id=7
    )
    assert result.structured_content["decoding"]["lossy"] is True
    assert result.structured_content["decoding"]["charset"] == "latin-1"


async def test_the_charset_from_the_response_header_wins() -> None:
    mcp, _ = _build_raw(
        [_one("umlaut.txt", "text/plain", 6)],
        [_raw("grüße".encode("cp1252"), "text/plain; charset=windows-1252")],
    )
    result = await _call(
        mcp, "download_ticket_attachment", ticket_id=5, article_id=42, attachment_id=7
    )
    assert result.structured_content["content"] == "grüße"
    assert result.structured_content["decoding"]["charset"] == "windows-1252"


# ── documents come back as text ──────────────────────────────────────────────


def _docx_bytes(*paragraphs: str) -> bytes:
    import io
    import zipfile

    ns = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("word/document.xml", f"<w:document {ns}><w:body>{body}</w:body></w:document>")
    return buf.getvalue()


async def test_a_docx_comes_back_as_extracted_text() -> None:
    docx = _docx_bytes("Angebot 4711")
    mcp, _ = _build_raw(
        # Declared as octet-stream: the type is found in the bytes, not the label.
        [_one("angebot.docx", "application/octet-stream", len(docx))],
        [_raw(docx, "application/octet-stream")],
    )
    result = await _call(
        mcp, "download_ticket_attachment", ticket_id=5, article_id=42, attachment_id=7
    )
    sc = result.structured_content
    assert sc["content_kind"] == "extracted_text"
    assert sc["content"] == "Angebot 4711"
    assert sc["extraction"]["status"] == "ok"
    assert sc["extraction"]["tool"] == "zipfile+defusedxml"


async def test_rtf_is_stripped_rather_than_returned_raw() -> None:
    rtf = rb"{\rtf1\ansi\deff0 {\fonttbl{\f0 Arial;}}\f0 Technische Daten\par}"
    mcp, _ = _build_raw(
        [_one("daten.rtf", "application/msword", len(rtf))],
        [_raw(rtf, "application/msword")],
    )
    result = await _call(
        mcp, "download_ticket_attachment", ticket_id=5, article_id=42, attachment_id=7
    )
    sc = result.structured_content
    assert sc["content_kind"] == "extracted_text"
    assert "Technische Daten" in sc["content"]
    assert "\rtf1" not in sc["content"]


async def test_a_failed_extraction_of_a_textual_document_falls_back_to_text(
    monkeypatch: Any,
) -> None:
    """RTF is text underneath. Losing the stripper must not lose the file."""
    from zammad import extract as extract_module

    def _boom(_data: bytes) -> str:
        raise extract_module.ExtractionRefused("stripper unavailable")

    monkeypatch.setitem(extract_module._EXTRACTORS, "application/rtf", (_boom, "striprtf"))

    rtf = rb"{\rtf1\ansi Technische Daten\par}"
    mcp, _ = _build_raw(
        [_one("daten.rtf", "application/rtf", len(rtf))], [_raw(rtf, "application/rtf")]
    )
    result = await _call(
        mcp, "download_ticket_attachment", ticket_id=5, article_id=42, attachment_id=7
    )
    sc = result.structured_content
    assert sc["content_kind"] == "text", "a textual document degrades to text, not to a blob"
    assert "Technische Daten" in sc["content"]
    assert sc["extraction"]["status"] == "failed"


async def test_a_failed_extraction_of_a_binary_document_falls_back_to_a_blob() -> None:
    pdf = b"%PDF-1.7\nbroken"
    mcp, _ = _build_raw(
        [_one("kaputt.pdf", "application/pdf", len(pdf))], [_raw(pdf, "application/pdf")]
    )
    result = await _call(
        mcp, "download_ticket_attachment", ticket_id=5, article_id=42, attachment_id=7
    )
    sc = result.structured_content
    assert sc["content_kind"] == "blob"
    assert sc["extraction"]["status"] == "failed"
    assert sc["extraction"]["reason"], "the model must be able to say WHY"


async def test_mode_raw_skips_extraction_entirely() -> None:
    docx = _docx_bytes("Angebot 4711")
    mcp, _ = _build_raw(
        [_one("angebot.docx", "application/octet-stream", len(docx))],
        [_raw(docx, "application/octet-stream")],
    )
    result = await _call(
        mcp,
        "download_ticket_attachment",
        ticket_id=5,
        article_id=42,
        attachment_id=7,
        mode="raw",
    )
    assert result.structured_content["extraction"]["status"] == "not_applicable"
    assert result.structured_content["content_kind"] == "blob"
