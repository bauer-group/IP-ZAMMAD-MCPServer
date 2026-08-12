"""Upload assembly: three sources, one payload, and the refusals in between."""

from __future__ import annotations

import base64
from typing import Any

import httpx
import pytest
from fastmcp.exceptions import ToolError

from tests.test_tools_inventory import RecordingCtx
from zammad import uploads
from zammad.uploads import AttachmentInput, CopyRef


class Ctx(RecordingCtx):
    """RecordingCtx with optional limit settings pinned on the instance."""

    def __init__(
        self,
        *,
        responses: list[Any] | None = None,
        raw_responses: list[Any] | None = None,
        **limits: Any,
    ) -> None:
        super().__init__(responses=responses, raw_responses=raw_responses)
        if limits:
            self.settings = type("S", (), limits)()


def _raw(content: bytes, content_type: str = "application/octet-stream") -> httpx.Response:
    return httpx.Response(
        status_code=200,
        content=content,
        headers={"content-type": content_type},
        request=httpx.Request("GET", "https://zammad.example/x"),
    )


def _source_article(filename: str, mime: str) -> dict[str, Any]:
    return {
        "id": 91,
        "attachments": [
            {"id": 7, "filename": filename, "size": "9", "preferences": {"Content-Type": mime}}
        ],
    }


# ── the three sources ────────────────────────────────────────────────────────


async def test_text_becomes_base64_with_a_derived_mime_type() -> None:
    payload = await uploads.build_attachment_payload(
        Ctx(), [AttachmentInput(filename="werte.csv", text="a;b\n1;2\n")]
    )
    assert payload == [
        {
            "filename": "werte.csv",
            "data": base64.b64encode(b"a;b\n1;2\n").decode(),
            "mime-type": "text/csv",
        }
    ]


async def test_the_payload_key_is_mime_type_with_a_hyphen() -> None:
    """A misspelled key is ignored in silence and the file reaches the customer
    as application/octet-stream - no error anywhere to notice it by."""
    payload = await uploads.build_attachment_payload(
        Ctx(), [AttachmentInput(filename="a.txt", text="x")]
    )
    assert payload is not None
    assert "mime-type" in payload[0]
    assert "mime_type" not in payload[0]


async def test_base64_input_is_decoded_and_re_encoded_intact() -> None:
    raw = bytes(range(256))
    payload = await uploads.build_attachment_payload(
        Ctx(),
        [
            AttachmentInput(
                filename="x.bin",
                data_base64=base64.b64encode(raw).decode(),
                mime_type="application/octet-stream",
            )
        ],
    )
    assert payload is not None
    assert base64.b64decode(payload[0]["data"]) == raw


async def test_invalid_base64_is_refused_with_a_usable_message() -> None:
    with pytest.raises(ToolError, match="not valid base64"):
        await uploads.build_attachment_payload(
            Ctx(), [AttachmentInput(filename="x.bin", data_base64="not!base64!")]
        )


async def test_copy_from_reads_the_source_and_inherits_its_name_and_type() -> None:
    ctx = Ctx(
        responses=[_source_article("datenblatt.pdf", "application/pdf")],
        raw_responses=[_raw(b"%PDF-1.7\n", "application/pdf")],
    )
    payload = await uploads.build_attachment_payload(
        ctx, [AttachmentInput(copy_from=CopyRef(ticket_id=4200, article_id=91, attachment_id=7))]
    )

    assert [(c["method"], c["path"]) for c in ctx.calls] == [
        ("GET", "/ticket_articles/91"),
        ("GET", "/ticket_attachment/4200/91/7"),
    ]
    assert payload is not None
    assert payload[0]["filename"] == "datenblatt.pdf"
    assert payload[0]["mime-type"] == "application/pdf"
    assert base64.b64decode(payload[0]["data"]) == b"%PDF-1.7\n"


async def test_copy_from_can_be_renamed() -> None:
    ctx = Ctx(
        responses=[_source_article("a.pdf", "application/pdf")],
        raw_responses=[_raw(b"%PDF-1.7\n", "application/pdf")],
    )
    payload = await uploads.build_attachment_payload(
        ctx,
        [
            AttachmentInput(
                filename="Datenblatt_Kunde.pdf",
                copy_from=CopyRef(ticket_id=4200, article_id=91, attachment_id=7),
            )
        ],
    )
    assert payload is not None
    assert payload[0]["filename"] == "Datenblatt_Kunde.pdf"


async def test_copy_from_an_unknown_attachment_names_the_listing_tool() -> None:
    ctx = Ctx(responses=[{"id": 91, "attachments": []}])
    with pytest.raises(ToolError, match="list_ticket_attachments"):
        await uploads.build_attachment_payload(
            ctx,
            [AttachmentInput(copy_from=CopyRef(ticket_id=4200, article_id=91, attachment_id=7))],
        )


# ── the input contract ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "kwargs",
    [
        {"filename": "a.txt"},
        {"filename": "a.txt", "text": "x", "data_base64": "eA=="},
        {
            "filename": "a.txt",
            "text": "x",
            "copy_from": CopyRef(ticket_id=1, article_id=2, attachment_id=3),
        },
    ],
)
async def test_exactly_one_source_is_required(kwargs: dict[str, Any]) -> None:
    with pytest.raises(Exception, match="exactly one"):
        AttachmentInput(**kwargs)


async def test_a_source_without_a_filename_is_refused() -> None:
    with pytest.raises(Exception, match="filename"):
        AttachmentInput(text="x")


async def test_no_attachments_produces_no_payload_key() -> None:
    assert await uploads.build_attachment_payload(Ctx(), None) is None
    assert await uploads.build_attachment_payload(Ctx(), []) is None


# ── the guardrails ───────────────────────────────────────────────────────────


async def test_a_file_over_the_per_file_limit_is_refused() -> None:
    ctx = Ctx(zammad_attachment_max_transfer_bytes=10, zammad_attachment_max_article_bytes=100)
    with pytest.raises(ToolError, match="over the 10 byte"):
        await uploads.build_attachment_payload(
            ctx, [AttachmentInput(filename="a.txt", text="x" * 20)]
        )


async def test_the_article_total_is_enforced_across_files() -> None:
    ctx = Ctx(zammad_attachment_max_transfer_bytes=100, zammad_attachment_max_article_bytes=30)
    with pytest.raises(ToolError, match="together"):
        await uploads.build_attachment_payload(
            ctx,
            [
                AttachmentInput(filename="a.txt", text="x" * 20),
                AttachmentInput(filename="b.txt", text="y" * 20),
            ],
        )


@pytest.mark.parametrize("filename", ["setup.exe", "run.BAT", "tool.ps1", "app.jar"])
async def test_executable_extensions_are_refused(filename: str) -> None:
    with pytest.raises(ToolError, match="executable"):
        await uploads.build_attachment_payload(
            Ctx(), [AttachmentInput(filename=filename, text="harmless looking")]
        )


async def test_an_executable_renamed_to_txt_is_caught_by_its_magic_bytes() -> None:
    payload = base64.b64encode(b"MZ\x90\x00" + b"\x00" * 64).decode()
    with pytest.raises(ToolError, match="executable"):
        await uploads.build_attachment_payload(
            Ctx(), [AttachmentInput(filename="harmlos.txt", data_base64=payload)]
        )


async def test_the_denylist_applies_to_copied_files_too() -> None:
    """Already sitting in Zammad is not a reason to forward it to a customer."""
    ctx = Ctx(
        responses=[_source_article("tool.exe", "application/octet-stream")],
        raw_responses=[_raw(b"MZ\x90\x00")],
    )
    with pytest.raises(ToolError, match="executable"):
        await uploads.build_attachment_payload(
            ctx, [AttachmentInput(copy_from=CopyRef(ticket_id=1, article_id=91, attachment_id=7))]
        )
