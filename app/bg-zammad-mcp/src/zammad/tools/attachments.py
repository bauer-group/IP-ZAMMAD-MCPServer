"""
Attachment tools - listing and reading the files carried by ticket articles.

Endpoints (all under /api/v1/):
  GET /ticket_articles/by_ticket/{ticket_id}             articles + attachment metadata
  GET /ticket_articles/{article_id}                      one article + attachment metadata
  GET /ticket_attachment/{ticket_id}/{article_id}/{id}   the file itself

Permission
----------
Zammad has no attachment-specific permission. All three routes are gated by
Pundit alone - TicketPolicy#show? plus Ticket::ArticlePolicy#show? - so
`ticket.agent` on the ticket's group, or being the ticket's customer, is
enough. The download route additionally re-checks that the attachment belongs
to the article and the article to the ticket, and answers 403 (not 404) when it
does not.

There is no attachment index
----------------------------
Zammad exposes no /attachments endpoint; attachment metadata exists only inside
an article, where Store#attributes_for_display slices id, store_file_id,
filename, size and preferences onto the article's `attachments` array.
`list_ticket_attachments` therefore reads the ticket's articles and flattens
that array, normalising two quirks on the way:

  * `size` arrives as a STRING - the stores.size column is a varchar - so a
    caller comparing it numerically gets lexicographic order ("9" > "10000").
    We publish `size_bytes` as a real integer.
  * the MIME type hides under one of four preference keys. Zammad's own
    downloader reads 'Content-Type' then 'Mime-Type'; its preview generator
    also accepts the lowercase 'content_type' / 'mime_type'. We try all four.

Inline attachments are listed too, because Zammad lists them: both the expanded
and the plain article representation of by_ticket call
attributes_with_association_names, which deliberately keeps images embedded in
the message body (zammad#6254). They carry inline=true so the caller can tell a
signature logo from a file the sender meant to send.

How a file's type is decided
----------------------------
Not by the label. Zammad stores whatever content type the uploading client
claimed, and clients lie: a customer RTF arrived as application/msword and was
refused as binary while being plain text. ``zammad.media.detect`` resolves magic
bytes first, then the extension, then the declared label, then the shape of the
content. The declared value is still reported as ``mime_type`` so a caller can
see the disagreement; the effective one is ``detected_mime_type``.

What comes back
---------------
Images return as an MCP ImageContent block, so the model actually sees the
screenshot rather than a base64 string. Text returns decoded, with the charset
that worked and whether the result is lossy. PDF, DOCX, XLSX and RTF return as
extracted text, with an ``extraction`` block saying what happened. Everything
else returns as metadata plus a base64 blob. ``mode`` overrides the routing:
'text' forces a decode, 'raw' forces the blob. Nothing is refused for being
binary any more.

Two fallbacks worth knowing about. A document whose extraction fails but which
is TEXT underneath - RTF - degrades to its raw text rather than to a blob:
losing the stripper must not lose the file. A binary document that fails
degrades to a blob carrying the reason, so the model can say why instead of
speculating over an empty string.

Two limits, on two different quantities
---------------------------------------
TRANSFER bounds bytes on the wire and is checked from the article's metadata,
before anything is fetched - that is what lets a 200 MB file be refused without
moving it. It is deliberately ONE setting shared with the upload path: two
numbers could only ever disagree, and the state they disagreed into (upload
10 MiB, refuse to read it back at 5 MiB) is the one nobody wants.

RESPONSE bounds what actually reaches the caller, which is a different quantity.
A 9 MB PDF may extract to 20 KB of text and is cheap to return; a 4 MB log file
passes every file-size guard and then costs roughly a million tokens. Guarding
the file size alone lets the expensive case through and blocks the free one.
Text over the limit is truncated and flagged ``truncated``; a binary over the
blob limit comes back as metadata only, because a base64 payload nothing can
read is pure cost.

The bytes reach us through ``ctx.request_raw``. The ordinary ``ctx.request``
decodes a 2xx body as JSON or as httpx's ``Response.text`` - a UTF-8 decode
with errors='replace' - which turns every non-UTF-8 byte into U+FFFD
irreversibly. That decode, not any platform limit, is what made binary
attachments unreadable before.
"""

from __future__ import annotations

import base64
from typing import TYPE_CHECKING, Annotated, Any

from fastmcp.exceptions import ToolError
from fastmcp.tools import ToolResult
from mcp.types import (
    BlobResourceContents,
    EmbeddedResource,
    ImageContent,
    TextContent,
    ToolAnnotations,
)
from pydantic import AnyUrl, Field

from .. import extract, media
from ..projection import envelope
from . import ToolContext

if TYPE_CHECKING:
    from fastmcp import FastMCP

# Used when the context carries no settings - the recording test harness, and
# any future caller that constructs the tools directly.
FALLBACK_MAX_TRANSFER_BYTES = 10 * 1024 * 1024
FALLBACK_MAX_TEXT_BYTES = 256 * 1024
FALLBACK_MAX_BLOB_BYTES = 2 * 1024 * 1024

READ_MODES = ("auto", "text", "raw")

_NO_EXTRACTION: dict[str, Any] = {"status": "not_applicable", "tool": None, "reason": None}


def _as_int(raw: Any) -> int | None:
    """Coerce a Zammad scalar to int - sizes arrive as strings, ids as numbers."""
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


def _limit(ctx: ToolContext, name: str, fallback: int) -> int:
    value = getattr(getattr(ctx, "settings", None), name, None)
    return value if isinstance(value, int) else fallback


def _charset_of(response: Any) -> str | None:
    """The charset parameter from a Content-Type header, if it carries one."""
    header = response.headers.get("content-type", "")
    for part in header.split(";")[1:]:
        key, _, value = part.partition("=")
        if key.strip().lower() == "charset":
            return value.strip().strip('"') or None
    return None


def _attachment_row(ticket_id: int, article: Any, attachment: Any) -> dict[str, Any]:
    preferences = attachment.get("preferences")
    if not isinstance(preferences, dict):
        preferences = {}
    return {
        "ticket_id": ticket_id,
        "article_id": article.get("id"),
        "article_type": article.get("type"),
        "article_created_at": article.get("created_at"),
        "attachment_id": attachment.get("id"),
        "filename": attachment.get("filename"),
        "size_bytes": _as_int(attachment.get("size")),
        "mime_type": media.mime_from_preferences(preferences),
        # Store#inline? - the file is embedded in the message body rather than
        # appended to it (signature logo, pasted screenshot).
        "inline": preferences.get("Content-Disposition") == "inline",
    }


def _find_attachment(article: Any, attachment_id: int) -> dict[str, Any] | None:
    if not isinstance(article, dict):
        return None
    for attachment in article.get("attachments") or []:
        if isinstance(attachment, dict) and _as_int(attachment.get("id")) == attachment_id:
            return attachment
    return None


def _truncate(text: str, max_text_bytes: int) -> tuple[str, bool, int]:
    """Cut a text body to the response limit. Returns (text, truncated, full_bytes).

    Encoding first and slicing bytes, then decoding with errors='ignore', so the
    cut cannot land inside a multi-byte character and produce mojibake at the
    seam. Truncation is always REPORTED - a silently shortened document reads
    exactly like a complete one, which is the failure mode worth avoiding.
    """
    encoded = text.encode("utf-8")
    if len(encoded) <= max_text_bytes:
        return text, False, len(encoded)
    return encoded[:max_text_bytes].decode("utf-8", "ignore"), True, len(encoded)


def _blob_result(
    base: dict[str, Any],
    data: bytes,
    detection: media.Detection,
    size: int,
    max_blob_bytes: int,
) -> ToolResult:
    """Metadata plus the untouched bytes, for anything that is not text.

    Above ``max_blob_bytes`` the bytes are withheld. A base64 blob larger than
    the caller can do anything with is pure cost: it inflates by 4/3 on the way
    out and no model reads it. Metadata plus a sentence is more useful.
    """
    base["content_kind"] = "blob"
    reason = base["extraction"].get("reason")
    detail = (
        f"text extraction failed: {reason}"
        if reason
        else "its content could not be turned into text."
    )

    if size > max_blob_bytes:
        base["content_kind"] = "metadata_only"
        summary = (
            f"{base['filename']} ({detection.mime_type}, {size} bytes) is over the "
            f"{max_blob_bytes} byte limit for returning raw bytes, so only its "
            f"metadata is here - {detail} Tell the user the file name and type "
            "and let them open it in Zammad."
        )
        return ToolResult(
            content=[TextContent(type="text", text=summary)], structured_content=base
        )

    resource = BlobResourceContents(
        uri=AnyUrl(
            f"zammad://ticket/{base['ticket_id']}/article/{base['article_id']}"
            f"/attachment/{base['attachment_id']}"
        ),
        mimeType=detection.mime_type,
        blob=base64.b64encode(data).decode(),
    )
    summary = (
        f"{base['filename']} ({detection.mime_type}, {size} bytes) returned as raw "
        f"bytes; {detail}"
    )
    return ToolResult(
        content=[
            TextContent(type="text", text=summary),
            EmbeddedResource(type="resource", resource=resource),
        ],
        structured_content=base,
    )


def _text_result(
    base: dict[str, Any], data: bytes, charset: str | None, max_text_bytes: int
) -> ToolResult:
    """A decoded text body, with the charset that worked stated openly."""
    decoded, used, lossy = media.decode_text(data, charset=charset)
    text, truncated, full_bytes = _truncate(decoded, max_text_bytes)
    base["content"] = text
    base["content_kind"] = "text"
    base["decoding"] = {"charset": used, "lossy": lossy}
    base["truncated"] = truncated
    if truncated:
        base["full_text_bytes"] = full_bytes
    return ToolResult(
        content=[TextContent(type="text", text=text)], structured_content=base
    )


async def _build_result(
    *,
    ticket_id: int,
    article_id: int,
    attachment_id: int,
    filename: Any,
    declared: str,
    size: int,
    data: bytes,
    detection: media.Detection,
    charset: str | None,
    mode: str,
    max_text_bytes: int,
    max_blob_bytes: int,
) -> ToolResult:
    """Route bytes onto the right MCP content block and describe what happened."""
    kind = detection.kind
    if mode == "raw":
        kind = media.Kind.OPAQUE
    elif mode == "text":
        kind = media.Kind.TEXT

    base: dict[str, Any] = {
        "ticket_id": ticket_id,
        "article_id": article_id,
        "attachment_id": attachment_id,
        "filename": filename,
        # The label Zammad stores, kept so a caller can see it disagree with
        # what the bytes say.
        "mime_type": declared,
        "detected_mime_type": detection.mime_type,
        "size_bytes": size,
        "content": None,
        "content_kind": kind.value,
        "extraction": dict(_NO_EXTRACTION),
        "decoding": None,
        "truncated": False,
    }

    if kind is media.Kind.IMAGE:
        base["content_kind"] = "image"
        summary = f"{filename} ({detection.mime_type}, {size} bytes)"
        return ToolResult(
            content=[
                TextContent(type="text", text=summary),
                ImageContent(
                    type="image",
                    data=base64.b64encode(data).decode(),
                    mimeType=detection.mime_type,
                ),
            ],
            structured_content=base,
        )

    if kind is media.Kind.TEXT:
        return _text_result(base, data, charset, max_text_bytes)

    if kind is media.Kind.DOCUMENT:
        result = await extract.extract(data, mime_type=detection.mime_type)
        base["extraction"] = {
            "status": result.status,
            "tool": result.tool,
            "reason": result.reason,
        }
        if result.status in {"ok", "partial"}:
            text, truncated, full_bytes = _truncate(result.text or "", max_text_bytes)
            base["content"] = text
            base["content_kind"] = "extracted_text"
            base["truncated"] = truncated
            if truncated:
                base["full_text_bytes"] = full_bytes
            return ToolResult(
                content=[TextContent(type="text", text=text)],
                structured_content=base,
            )
        if detection.textual:
            # RTF and friends are text underneath. Losing the stripper must not
            # lose the file: the raw text is degraded but perfectly readable,
            # and a blob here would reintroduce exactly the dead end this
            # feature exists to remove.
            return _text_result(base, data, charset, max_text_bytes)

    return _blob_result(base, data, detection, size, max_blob_bytes)


def register(mcp: FastMCP, ctx: ToolContext) -> int:
    max_transfer_bytes = _limit(
        ctx, "zammad_attachment_max_transfer_bytes", FALLBACK_MAX_TRANSFER_BYTES
    )
    max_text_bytes = _limit(ctx, "zammad_attachment_max_text_bytes", FALLBACK_MAX_TEXT_BYTES)
    max_blob_bytes = _limit(ctx, "zammad_attachment_max_blob_bytes", FALLBACK_MAX_BLOB_BYTES)

    @mcp.tool(
        name="list_ticket_attachments",
        description=(
            "List every file attached to a ticket, flattened across all of its "
            "articles. Zammad publishes no attachment index - the metadata "
            "exists only inside articles - so this reads the ticket's articles "
            "and returns one row per file, each carrying the article id and "
            "attachment id that `download_ticket_attachment` needs, plus "
            "filename, size in bytes and MIME type. Rows flagged inline are "
            "images embedded in the message body (signature logos, pasted "
            "screenshots), not files the sender consciously attached. Needs "
            "the same access as reading the ticket: 'ticket.agent' on its "
            "group, or being its customer. Returns an empty list for a ticket "
            "whose articles carry no files."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=True, destructiveHint=False, openWorldHint=True
        ),
    )
    async def list_ticket_attachments(
        ticket_id: Annotated[int, Field(ge=1)],
    ) -> dict[str, Any]:
        articles = await ctx.request("GET", f"/ticket_articles/by_ticket/{ticket_id}")
        rows: list[dict[str, Any]] = []
        for article in articles or []:
            for attachment in article.get("attachments") or []:
                rows.append(_attachment_row(ticket_id, article, attachment))
        # Synthesised from the whole thread, which Zammad never paginates.
        return envelope(rows, ticket_id=ticket_id)

    @mcp.tool(
        name="download_ticket_attachment",
        description=(
            "Read the CONTENT of one ticket attachment. Call "
            "`list_ticket_attachments` first to get a valid "
            "`article_id` / `attachment_id` pair - guessing them returns 403, "
            "because Zammad verifies that the attachment belongs to the "
            "article and the article to the ticket. Images come back as "
            "viewable images; text, PDF, Word and Excel files come back as "
            "text; anything else comes back as metadata plus the raw bytes. "
            "The file type is determined from the bytes themselves, so a file "
            "mislabelled at upload is still read correctly. Set `mode` to "
            "'text' to force a text decode or 'raw' for the untouched bytes. "
            "A very long text body is truncated and the result says so; a very "
            "large binary comes back as metadata only. Needs the same access "
            "as reading the ticket."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=True, destructiveHint=False, openWorldHint=True
        ),
    )
    async def download_ticket_attachment(
        ticket_id: Annotated[int, Field(ge=1)],
        article_id: Annotated[
            int, Field(ge=1, description="ID of the article the file hangs on")
        ],
        attachment_id: Annotated[
            int, Field(ge=1, description="ID of the file within that article")
        ],
        mode: Annotated[
            str,
            Field(
                description="'auto' (default), 'text' to force a decode, 'raw' for bytes"
            ),
        ] = "auto",
    ) -> ToolResult:
        if mode not in READ_MODES:
            raise ToolError(f"mode must be one of {', '.join(READ_MODES)} (got {mode!r}).")

        # The metadata round-trip is deliberate: size is only knowable from the
        # article, and knowing it BEFORE the download is what lets an oversized
        # file be refused without transferring it. Being able to move bytes is
        # not a reason to pull 200 MB in order to reject it. Both requests need
        # identical permissions, so this cannot fail in a way the download would
        # not.
        article = await ctx.request("GET", f"/ticket_articles/{article_id}")
        attachment = _find_attachment(article, attachment_id)
        if attachment is None:
            raise ToolError(
                f"Article {article_id} has no attachment with id {attachment_id}. "
                "Call list_ticket_attachments for this ticket and use an "
                "article_id / attachment_id pair from its result."
            )

        filename = attachment.get("filename")
        declared = media.mime_from_preferences(attachment.get("preferences"))
        size = _as_int(attachment.get("size"))

        if size is not None and size > max_transfer_bytes:
            raise ToolError(
                f"Attachment {filename!r} is {size} bytes, over the "
                f"{max_transfer_bytes} byte transfer limit "
                "(ZAMMAD_ATTACHMENT_MAX_TRANSFER_BYTES). Tell the user the file "
                "name and size and let them open it in Zammad."
            )

        response = await ctx.request_raw(
            "GET", f"/ticket_attachment/{ticket_id}/{article_id}/{attachment_id}"
        )
        data: bytes = response.content
        if size is None:
            # stores.size is nullable, so the pre-flight guard could not run -
            # measure what actually arrived instead.
            size = len(data)
            if size > max_transfer_bytes:
                raise ToolError(
                    f"Attachment {filename!r} is {size} bytes, over the "
                    f"{max_transfer_bytes} byte transfer limit. Zammad reported no "
                    "size for it, so this could only be checked after the transfer."
                )

        detection = media.detect(data, filename=filename, declared=declared)
        return await _build_result(
            ticket_id=ticket_id,
            article_id=article_id,
            attachment_id=attachment_id,
            filename=filename,
            declared=declared,
            size=size,
            data=data,
            detection=detection,
            charset=_charset_of(response),
            mode=mode,
            max_text_bytes=max_text_bytes,
            max_blob_bytes=max_blob_bytes,
        )

    return 2
