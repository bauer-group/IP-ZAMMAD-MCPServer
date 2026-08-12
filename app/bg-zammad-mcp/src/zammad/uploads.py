"""Assembling the ``attachments`` array for POST /ticket_articles.

Zammad has no endpoint that attaches a file to an EXISTING article: attachments
are created only alongside an article. Every write therefore rides on an
article-creating tool, and this module turns whatever the caller supplied into
the one payload Zammad accepts::

    {"filename": "…", "data": "<base64>", "mime-type": "text/csv"}

Note ``mime-type`` with a HYPHEN. Zammad ignores an unrecognised key without
complaining, so the underscore spelling delivers the file to the customer as
application/octet-stream with no error anywhere to notice it by.

Three sources, one shape
------------------------
``text`` costs the fewest tokens and covers the common case (a report the agent
just wrote). ``data_base64`` carries arbitrary bytes for a programmatic caller.
``copy_from`` is the interesting one: the bytes move server-side only, so
carrying a data sheet from one ticket to another is free in tokens, exact, and
unbounded by the model's context. Reading the source runs with the caller's own
Zammad permissions, exactly as a download would.
"""

from __future__ import annotations

import base64
import binascii
from typing import TYPE_CHECKING, Annotated, Any, Final

from fastmcp.exceptions import ToolError
from pydantic import BaseModel, Field, model_validator

from . import media

if TYPE_CHECKING:
    from .tools import ToolContext

# Used when the context carries no settings (the recording test harness).
# The per-file limit is the SAME setting the read path uses: one transfer limit
# for both directions, because two could only ever disagree - and the state they
# disagreed into (attach 10 MiB, refuse to read it back at 5 MiB) is the one
# nobody wants.
FALLBACK_MAX_TRANSFER_BYTES: Final = 10 * 1024 * 1024
FALLBACK_MAX_ARTICLE_BYTES: Final = 25 * 1024 * 1024

# NOT virus scanning, and it must never be described as one: a .zip containing
# an .exe passes. It is a tripwire against the obvious accident - an unattended
# agent putting an executable into a customer's inbox under the helpdesk's name.
#
# Deliberately asymmetric with the read path, which happily returns a .js file
# as text. Reading what a customer sent is not the risk; re-sending an
# executable under the helpdesk's name is.
DENIED_EXTENSIONS: Final = frozenset(
    {
        ".bat",
        ".cmd",
        ".com",
        ".exe",
        ".hta",
        ".jar",
        ".js",
        ".jse",
        ".lnk",
        ".msi",
        ".pif",
        ".ps1",
        ".reg",
        ".scr",
        ".vbe",
        ".vbs",
        ".wsf",
    }
)
DENIED_MAGIC: Final = ((b"MZ", "a Windows executable"), (b"\x7fELF", "a Linux executable"))

# A cap on how many files one article may carry, independent of their size. Ten
# is generous for a helpdesk reply and small enough that a runaway loop cannot
# assemble a thousand upstream reads before the size limit notices.
MAX_ATTACHMENTS_PER_ARTICLE: Final = 10


class CopyRef(BaseModel):
    """Points at an attachment that already exists in Zammad."""

    ticket_id: Annotated[int, Field(ge=1)]
    article_id: Annotated[int, Field(ge=1)]
    attachment_id: Annotated[int, Field(ge=1)]


class AttachmentInput(BaseModel):
    """One file to attach, from exactly one of three sources.

    Flat rather than a discriminated union on purpose: a flat schema is easier
    for a model to fill in than a oneOf, and the validator can explain the
    mistake in a sentence instead of emitting a schema error.
    """

    filename: Annotated[
        str | None,
        Field(
            default=None,
            max_length=255,
            description=(
                "Name the file will carry. Required, except with copy_from, "
                "which inherits it from the source."
            ),
        ),
    ] = None
    text: Annotated[
        str | None,
        Field(
            default=None,
            description="Literal text content - the server base64-encodes it. Cheapest source.",
        ),
    ] = None
    data_base64: Annotated[
        str | None,
        Field(default=None, description="Base64-encoded bytes, for binary content."),
    ] = None
    copy_from: Annotated[
        CopyRef | None,
        Field(
            default=None,
            description=(
                "Copy an attachment that already exists in Zammad. The bytes "
                "never enter the conversation, so this costs no tokens."
            ),
        ),
    ] = None
    mime_type: Annotated[
        str | None,
        Field(default=None, description="Content type. Derived from the extension if omitted."),
    ] = None

    @model_validator(mode="after")
    def _exactly_one_source(self) -> AttachmentInput:
        sources = [self.text is not None, self.data_base64 is not None, self.copy_from is not None]
        if sum(sources) != 1:
            raise ValueError(
                "each attachment needs exactly one of text, data_base64 or copy_from "
                f"(got {sum(sources)})"
            )
        if self.copy_from is None and not self.filename:
            raise ValueError("filename is required unless copy_from supplies it")
        return self


def _limits(ctx: ToolContext) -> tuple[int, int]:
    settings = getattr(ctx, "settings", None)
    per_file = getattr(settings, "zammad_attachment_max_transfer_bytes", None)
    per_article = getattr(settings, "zammad_attachment_max_article_bytes", None)
    return (
        per_file if isinstance(per_file, int) else FALLBACK_MAX_TRANSFER_BYTES,
        per_article if isinstance(per_article, int) else FALLBACK_MAX_ARTICLE_BYTES,
    )


def _reject_executables(filename: str, data: bytes) -> None:
    lowered = filename.lower()
    for extension in DENIED_EXTENSIONS:
        if lowered.endswith(extension):
            raise ToolError(
                f"Refusing to attach {filename!r}: {extension} is an executable type. "
                "This server does not attach executables to tickets."
            )
    for signature, description in DENIED_MAGIC:
        if data.startswith(signature):
            raise ToolError(
                f"Refusing to attach {filename!r}: its content is {description}, "
                "whatever the file is called. This server does not attach "
                "executables to tickets."
            )


async def _resolve(ctx: ToolContext, item: AttachmentInput) -> tuple[str, bytes, str]:
    """Turn one input into (filename, bytes, mime_type)."""
    if item.text is not None:
        data = item.text.encode("utf-8")
        filename = item.filename or "attachment.txt"
        mime = item.mime_type or media.mime_for_filename(filename) or "text/plain"
        return filename, data, media.normalise_mime(mime)

    if item.data_base64 is not None:
        try:
            data = base64.b64decode(item.data_base64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ToolError(
                f"The data_base64 value for {item.filename!r} is not valid base64: {exc}"
            ) from exc
        filename = item.filename or "attachment.bin"
        # No label and no useful extension: let the bytes speak, the same way
        # the read path does.
        mime = (
            item.mime_type
            or media.mime_for_filename(filename)
            or media.detect(data, filename=filename).mime_type
        )
        return filename, data, media.normalise_mime(mime)

    ref = item.copy_from
    if ref is None:  # unreachable: the model validator guarantees a source
        raise ToolError("an attachment reached the server with no source")
    article = await ctx.request("GET", f"/ticket_articles/{ref.article_id}")
    source: dict[str, Any] | None = None
    for candidate in (article or {}).get("attachments") or []:
        if isinstance(candidate, dict) and str(candidate.get("id")) == str(ref.attachment_id):
            source = candidate
            break
    if source is None:
        raise ToolError(
            f"Article {ref.article_id} has no attachment with id {ref.attachment_id}. "
            "Call list_ticket_attachments on the source ticket and use an "
            "article_id / attachment_id pair from its result."
        )
    response = await ctx.request_raw(
        "GET", f"/ticket_attachment/{ref.ticket_id}/{ref.article_id}/{ref.attachment_id}"
    )
    data = response.content
    filename = item.filename or str(source.get("filename") or "attachment.bin")
    mime = item.mime_type or media.mime_from_preferences(source.get("preferences"))
    return filename, data, media.normalise_mime(mime)


async def build_attachment_payload(
    ctx: ToolContext,
    inputs: list[AttachmentInput] | None,
) -> list[dict[str, str]] | None:
    """Resolve every input, enforce the limits, and return Zammad's payload.

    Returns None when there is nothing to attach, so the caller can omit the
    key entirely rather than sending an empty array on every ordinary reply.
    """
    if not inputs:
        return None
    if len(inputs) > MAX_ATTACHMENTS_PER_ARTICLE:
        raise ToolError(
            f"{len(inputs)} attachments is over the {MAX_ATTACHMENTS_PER_ARTICLE} "
            "file limit for one article."
        )

    per_file, per_article = _limits(ctx)
    payload: list[dict[str, str]] = []
    total = 0

    for item in inputs:
        filename, data, mime = await _resolve(ctx, item)
        if len(data) > per_file:
            raise ToolError(
                f"Attachment {filename!r} is {len(data)} bytes, over the {per_file} "
                "byte transfer limit (ZAMMAD_ATTACHMENT_MAX_TRANSFER_BYTES)."
            )
        _reject_executables(filename, data)
        total += len(data)
        if total > per_article:
            raise ToolError(
                f"The attachments together are {total} bytes, over the {per_article} "
                "byte limit for one article. Send fewer files, or send them in "
                "separate messages."
            )
        payload.append(
            {
                "filename": filename,
                "data": base64.b64encode(data).decode(),
                # HYPHEN. Zammad ignores mime_type without a word.
                "mime-type": mime or media.DEFAULT_MIME_TYPE,
            }
        )
    return payload


__all__ = [
    "DENIED_EXTENSIONS",
    "DENIED_MAGIC",
    "FALLBACK_MAX_ARTICLE_BYTES",
    "FALLBACK_MAX_TRANSFER_BYTES",
    "MAX_ATTACHMENTS_PER_ARTICLE",
    "AttachmentInput",
    "CopyRef",
    "build_attachment_payload",
]
