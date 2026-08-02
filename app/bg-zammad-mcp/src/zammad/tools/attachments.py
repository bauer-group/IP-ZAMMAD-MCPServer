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

Why binary attachments are refused
----------------------------------
``ctx.request`` decodes a 2xx response as JSON when the content type says JSON
and as httpx's ``Response.text`` otherwise - a UTF-8 decode with
errors='replace'. For a PNG or a PDF that is lossy and irreversible: every byte
that is not valid UTF-8 becomes U+FFFD, so what reaches the model is corrupt
data that still looks like a file. This context has no byte-preserving path,
and the two ways to fake one (reaching into the private httpx response, or
base64-ing the already-mangled text) would both hand the model something that
looks valid and is not.

``download_ticket_attachment`` therefore serves text-typed attachments only and
raises a ToolError naming the MIME type for anything else, so the model reports
"I cannot read that file" instead of hallucinating over replacement characters.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Annotated, Any

from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from . import ToolContext

if TYPE_CHECKING:
    from fastmcp import FastMCP

# Resolution order taken from ApplicationController::HasDownload::DownloadFile
# ('Content-Type' then 'Mime-Type'), widened by the two lowercase spellings
# Store#generate_previews also accepts.
MIME_PREFERENCE_KEYS = ("Content-Type", "Mime-Type", "content_type", "mime_type")

# What Zammad itself falls back to (ActiveStorage.binary_content_type) when a
# stored file carries no usable content type at all.
DEFAULT_MIME_TYPE = "application/octet-stream"

# Media types that survive a UTF-8 decode. Everything outside this set is
# refused rather than silently mangled - see the module docstring.
TEXT_MIME_EXACT = frozenset(
    {
        "application/csv",
        "application/ecmascript",
        "application/javascript",
        "application/json",
        "application/sql",
        "application/x-sh",
        "application/x-yaml",
        "application/xml",
        "application/yaml",
        "message/delivery-status",
        "message/rfc822",
    }
)
TEXT_MIME_SUFFIXES = ("+json", "+xml", "+yaml")

DEFAULT_MAX_BYTES = 5 * 1024 * 1024
# A hard ceiling on max_bytes, not just a default: without it an LLM that hits
# the size guard can simply retry with max_bytes=200_000_000 and blow up the
# response it was being protected from.
MAX_ALLOWED_BYTES = 20 * 1024 * 1024


def _as_int(raw: Any) -> int | None:
    """Coerce a Zammad scalar to int - sizes arrive as strings, ids as numbers."""
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


def _mime_type(preferences: Any) -> str:
    """Best-effort content type for a stored file, lower-cased and unparameterised."""
    if not isinstance(preferences, dict):
        return DEFAULT_MIME_TYPE
    for key in MIME_PREFERENCE_KEYS:
        value = preferences.get(key)
        if isinstance(value, str) and value.strip():
            # Zammad keeps the raw header, so a charset parameter can be attached.
            return value.split(";")[0].strip().lower()
    return DEFAULT_MIME_TYPE


def _is_text_mime(mime: str) -> bool:
    return mime.startswith("text/") or mime in TEXT_MIME_EXACT or mime.endswith(TEXT_MIME_SUFFIXES)


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
        "mime_type": _mime_type(preferences),
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


def register(mcp: FastMCP, ctx: ToolContext) -> int:
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
    ) -> list[dict[str, Any]]:
        articles = await ctx.request("GET", f"/ticket_articles/by_ticket/{ticket_id}")
        rows: list[dict[str, Any]] = []
        for article in articles or []:
            for attachment in article.get("attachments") or []:
                rows.append(_attachment_row(ticket_id, article, attachment))
        return rows

    @mcp.tool(
        name="download_ticket_attachment",
        description=(
            "Read the CONTENT of one ticket attachment. Call "
            "`list_ticket_attachments` first to get a valid "
            "`article_id` / `attachment_id` pair - guessing them returns 403, "
            "because Zammad verifies that the attachment belongs to the "
            "article and the article to the ticket. TEXT ONLY: this server "
            "decodes every response as text, so binary files (images, PDFs, "
            "Office documents, archives) would arrive corrupted and are "
            "refused with an error naming their MIME type - report that to the "
            "user rather than guessing at the contents. Files larger than "
            "`max_bytes` are refused before any data is transferred. Needs the "
            "same access as reading the ticket."
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
        max_bytes: Annotated[
            int,
            Field(
                ge=1,
                le=MAX_ALLOWED_BYTES,
                description="Refuse anything larger (default 5 MB, ceiling 20 MB)",
            ),
        ] = DEFAULT_MAX_BYTES,
    ) -> dict[str, Any]:
        # The metadata round-trip is deliberate: size and MIME type are only
        # knowable from the article, and knowing them BEFORE the download is
        # what lets an oversized or binary file be refused without transferring
        # it. Both requests need the identical permission, so this cannot fail
        # in a way the download would not.
        article = await ctx.request("GET", f"/ticket_articles/{article_id}")
        attachment = _find_attachment(article, attachment_id)
        if attachment is None:
            raise ToolError(
                f"Article {article_id} has no attachment with id {attachment_id}. "
                "Call list_ticket_attachments for this ticket and use an "
                "article_id / attachment_id pair from its result."
            )

        filename = attachment.get("filename")
        mime_type = _mime_type(attachment.get("preferences"))
        size = _as_int(attachment.get("size"))

        if size is not None and size > max_bytes:
            raise ToolError(
                f"Attachment {filename!r} is {size} bytes, over the {max_bytes} byte "
                f"limit. Raise max_bytes if the content is really needed (hard "
                f"ceiling {MAX_ALLOWED_BYTES})."
            )
        if not _is_text_mime(mime_type):
            raise ToolError(
                f"Attachment {filename!r} is {mime_type}, which is binary. This server "
                "can only return text-decodable attachments - binary bytes would be "
                "corrupted in transit. Tell the user the file name and type and let "
                "them open it in Zammad."
            )

        content = await ctx.request(
            "GET", f"/ticket_attachment/{ticket_id}/{article_id}/{attachment_id}"
        )
        # A .json attachment comes back already parsed, because the context
        # decodes by content type. Re-serialise it so `content` is always a
        # string and the caller never has to branch on the payload's type.
        text = content if isinstance(content, str) else json.dumps(content, indent=2)
        if size is None:
            # stores.size is nullable, so the pre-flight guard above could not
            # run - measure what actually arrived instead.
            size = len(text.encode("utf-8", "replace"))
            if size > max_bytes:
                raise ToolError(
                    f"Attachment {filename!r} decoded to {size} bytes, over the "
                    f"{max_bytes} byte limit. Zammad reported no size for it, so this "
                    "could only be checked after the transfer."
                )

        return {
            "ticket_id": ticket_id,
            "article_id": article_id,
            "attachment_id": attachment_id,
            "filename": filename,
            "mime_type": mime_type,
            "size_bytes": size,
            "content": text,
        }

    return 2
