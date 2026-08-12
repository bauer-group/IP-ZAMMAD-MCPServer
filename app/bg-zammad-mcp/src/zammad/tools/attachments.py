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
FALLBACK_MAX_READ_BYTES = 5 * 1024 * 1024
FALLBACK_READ_CEILING_BYTES = 20 * 1024 * 1024

READ_MODES = ("auto", "text", "raw")

_NO_EXTRACTION: dict[str, Any] = {"status": "not_applicable", "tool": None, "reason": None}


def _as_int(raw: Any) -> int | None:
    """Coerce a Zammad scalar to int - sizes arrive as strings, ids as numbers."""
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


def _read_limits(ctx: ToolContext) -> tuple[int, int]:
    """(default max_bytes, hard ceiling) from settings, with safe fallbacks."""
    settings = getattr(ctx, "settings", None)
    default = getattr(settings, "zammad_attachment_max_read_bytes", None)
    ceiling = getattr(settings, "zammad_attachment_read_ceiling_bytes", None)
    return (
        default if isinstance(default, int) else FALLBACK_MAX_READ_BYTES,
        ceiling if isinstance(ceiling, int) else FALLBACK_READ_CEILING_BYTES,
    )


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


def _blob_result(
    base: dict[str, Any], data: bytes, detection: media.Detection, size: int
) -> ToolResult:
    """Metadata plus the untouched bytes, for anything that is not text."""
    base["content_kind"] = "blob"
    resource = BlobResourceContents(
        uri=AnyUrl(
            f"zammad://ticket/{base['ticket_id']}/article/{base['article_id']}"
            f"/attachment/{base['attachment_id']}"
        ),
        mimeType=detection.mime_type,
        blob=base64.b64encode(data).decode(),
    )
    reason = base["extraction"].get("reason")
    summary = (
        f"{base['filename']} ({detection.mime_type}, {size} bytes) returned as raw bytes; "
        + (
            f"text extraction failed: {reason}"
            if reason
            else "its content could not be turned into text."
        )
    )
    return ToolResult(
        content=[
            TextContent(type="text", text=summary),
            EmbeddedResource(type="resource", resource=resource),
        ],
        structured_content=base,
    )


def _text_result(
    base: dict[str, Any], data: bytes, charset: str | None
) -> ToolResult:
    """A decoded text body, with the charset that worked stated openly."""
    text, used, lossy = media.decode_text(data, charset=charset)
    base["content"] = text
    base["content_kind"] = "text"
    base["decoding"] = {"charset": used, "lossy": lossy}
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
        return _text_result(base, data, charset)

    if kind is media.Kind.DOCUMENT:
        result = await extract.extract(data, mime_type=detection.mime_type)
        base["extraction"] = {
            "status": result.status,
            "tool": result.tool,
            "reason": result.reason,
        }
        if result.status in {"ok", "partial"}:
            base["content"] = result.text
            base["content_kind"] = "extracted_text"
            return ToolResult(
                content=[TextContent(type="text", text=result.text or "")],
                structured_content=base,
            )
        if detection.textual:
            # RTF and friends are text underneath. Losing the stripper must not
            # lose the file: the raw text is degraded but perfectly readable,
            # and a blob here would reintroduce exactly the dead end this
            # feature exists to remove.
            return _text_result(base, data, charset)

    return _blob_result(base, data, detection, size)


def register(mcp: FastMCP, ctx: ToolContext) -> int:
    default_max_bytes, ceiling_bytes = _read_limits(ctx)

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

    # NOT decorated: the max_bytes ceiling is an operator setting, and this
    # module runs under `from __future__ import annotations`, so an
    # `Annotated[..., Field(le=ceiling_bytes)]` written inline would stay a
    # string and be evaluated later against the MODULE globals - where a local
    # of register() does not exist (NameError at schema-build time). The
    # annotation is therefore patched in with the real object below, and the
    # tool is registered by an explicit decorator call. Defaults are unaffected:
    # they are ordinary runtime values.
    async def download_ticket_attachment(
        ticket_id: Annotated[int, Field(ge=1)],
        article_id: Annotated[
            int, Field(ge=1, description="ID of the article the file hangs on")
        ],
        attachment_id: Annotated[
            int, Field(ge=1, description="ID of the file within that article")
        ],
        max_bytes: int = default_max_bytes,
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

        if size is not None and size > max_bytes:
            raise ToolError(
                f"Attachment {filename!r} is {size} bytes, over the {max_bytes} byte "
                f"limit. Raise max_bytes if the content is really needed (hard "
                f"ceiling {ceiling_bytes})."
            )

        response = await ctx.request_raw(
            "GET", f"/ticket_attachment/{ticket_id}/{article_id}/{attachment_id}"
        )
        data: bytes = response.content
        if size is None:
            # stores.size is nullable, so the pre-flight guard could not run -
            # measure what actually arrived instead.
            size = len(data)
            if size > max_bytes:
                raise ToolError(
                    f"Attachment {filename!r} is {size} bytes, over the {max_bytes} "
                    "byte limit. Zammad reported no size for it, so this could only "
                    "be checked after the transfer."
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
        )

    download_ticket_attachment.__annotations__["max_bytes"] = Annotated[
        int,
        Field(
            ge=1,
            le=ceiling_bytes,
            description=(
                f"Refuse anything larger (default {default_max_bytes}, "
                f"ceiling {ceiling_bytes})"
            ),
        ),
    ]
    mcp.tool(
        name="download_ticket_attachment",
        description=(
            "Read the CONTENT of one ticket attachment. Call "
            "`list_ticket_attachments` first to get a valid "
            "`article_id` / `attachment_id` pair - guessing them returns 403, "
            "because Zammad verifies that the attachment belongs to the "
            "article and the article to the ticket. Images come back as "
            "viewable images; text files come back as text; anything else "
            "comes back as metadata plus the raw bytes. The file type is "
            "determined from the bytes themselves, so a file mislabelled at "
            "upload is still read correctly. Set `mode` to 'text' to force a "
            "text decode or 'raw' for the untouched bytes. Files larger than "
            "`max_bytes` are refused before any data is transferred. Needs the "
            "same access as reading the ticket."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=True, destructiveHint=False, openWorldHint=True
        ),
    )(download_ticket_attachment)

    return 2
