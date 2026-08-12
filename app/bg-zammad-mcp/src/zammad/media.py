"""File-type detection for attachments — the bytes decide, the label is a hint.

Zammad stores whatever content type the uploading client claimed. A file sent
by a mail client as ``application/msword`` may be RTF; a screenshot pasted into
a web form may arrive as ``application/octet-stream``. Deciding what a file is
from that label alone is how a plain-text document ends up refused as binary.

Resolution order, most trustworthy first:

  1. magic bytes      - what the file actually starts with
  2. file extension   - what the sender named it
  3. declared type    - what the upload claimed
  4. content shape    - printable bytes with no NULs are text

Nothing here does I/O or imports a third-party package, so the whole decision
table is unit-testable without a server.

Why the extension table is explicit
-----------------------------------
``mimetypes.guess_type`` consults the platform: on Windows it reads the
registry, which on a normal developer machine answers ``.csv`` with
``application/vnd.ms-excel`` and - the irony is not lost - ``.rtf`` with
``application/msword``, the very mislabelling this module exists to survive.
The same file would then be classified differently on a developer's laptop and
in the Alpine production image. ``_EXTENSION_MIME`` pins the types a helpdesk
actually receives so the answer is the same everywhere; ``mimetypes`` still
handles the long tail, where being platform-dependent costs nothing.
"""

from __future__ import annotations

import io
import mimetypes
import zipfile
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

# Zammad's own fallback (ActiveStorage.binary_content_type) for a stored file
# with no usable content type.
DEFAULT_MIME_TYPE: Final = "application/octet-stream"

# Resolution order from ApplicationController::HasDownload::DownloadFile
# ('Content-Type' then 'Mime-Type'), widened by the two lowercase spellings
# Store#generate_previews also accepts.
MIME_PREFERENCE_KEYS: Final = ("Content-Type", "Mime-Type", "content_type", "mime_type")

DOCX_MIME: Final = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX_MIME: Final = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
PPTX_MIME: Final = "application/vnd.openxmlformats-officedocument.presentationml.presentation"

# Only the four formats MCP clients reliably render. A TIFF or a BMP is a valid
# image and still useless as an ImageContent block, so it goes out as a blob.
IMAGE_MIME_TYPES: Final = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})

# Types extract.py knows how to turn into text.
DOCUMENT_MIME_TYPES: Final = frozenset(
    {"application/pdf", DOCX_MIME, XLSX_MIME, "application/rtf", "text/rtf"}
)

# Documents that are themselves text. RTF is prose wrapped in control words:
# extracting it is an improvement, refusing it is a bug. If extraction is
# unavailable the raw text is still readable, and ``textual`` is what tells the
# read path to fall back that way instead of to an opaque blob.
TEXTUAL_DOCUMENT_MIME_TYPES: Final = frozenset({"application/rtf", "text/rtf"})

# Media types that survive a text decode.
TEXT_MIME_EXACT: Final = frozenset(
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
TEXT_MIME_SUFFIXES: Final = ("+json", "+xml", "+yaml")

# (offset, signature, mime). GIF and the RIFF family are handled separately.
_MAGIC: Final[tuple[tuple[int, bytes, str], ...]] = (
    (0, b"\x89PNG\r\n\x1a\n", "image/png"),
    (0, b"\xff\xd8\xff", "image/jpeg"),
    (0, b"GIF87a", "image/gif"),
    (0, b"GIF89a", "image/gif"),
    (0, b"%PDF-", "application/pdf"),
    (0, b"{\\rtf", "application/rtf"),
    (0, b"\x1f\x8b\x08", "application/gzip"),
    (0, b"7z\xbc\xaf\x27\x1c", "application/x-7z-compressed"),
    (0, b"Rar!\x1a\x07", "application/vnd.rar"),
    (0, b"%!PS", "application/postscript"),
)

# Extensions answered without asking the platform - see the module docstring.
_EXTENSION_MIME: Final[dict[str, str]] = {
    # text a helpdesk actually receives
    ".txt": "text/plain",
    ".log": "text/plain",
    ".csv": "text/csv",
    ".tsv": "text/tab-separated-values",
    ".md": "text/markdown",
    ".json": "application/json",
    ".xml": "application/xml",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
    ".html": "text/html",
    ".htm": "text/html",
    ".ini": "text/plain",
    ".conf": "text/plain",
    ".cfg": "text/plain",
    ".sql": "application/sql",
    ".sh": "application/x-sh",
    ".eml": "message/rfc822",
    # documents
    ".rtf": "application/rtf",
    ".pdf": "application/pdf",
    ".docx": DOCX_MIME,
    ".xlsx": XLSX_MIME,
    ".pptx": PPTX_MIME,
    # images
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    # archives
    ".zip": "application/zip",
    ".gz": "application/gzip",
}

# How many leading bytes the content-shape heuristic looks at.
_SNIFF_WINDOW: Final = 8192

# Tried in order by decode_text. latin-1 is terminal: it maps every byte, so it
# never raises, which is exactly why a result that needed it is flagged lossy.
_CHARSET_LADDER: Final = ("utf-8", "cp1252", "latin-1")


class Kind(StrEnum):
    """What the read path should do with a file."""

    IMAGE = "image"
    TEXT = "text"
    DOCUMENT = "document"
    OPAQUE = "opaque"


@dataclass(frozen=True, slots=True)
class Detection:
    """The effective type of a file, and how we arrived at it."""

    mime_type: str
    declared_mime_type: str
    kind: Kind
    textual: bool
    source: str  # "magic" | "extension" | "declared" | "content"


def normalise_mime(raw: str | None) -> str:
    """Lower-case, strip parameters. ``TEXT/Plain; charset=utf-8`` -> ``text/plain``."""
    if not isinstance(raw, str) or not raw.strip():
        return DEFAULT_MIME_TYPE
    return raw.split(";")[0].strip().lower()


def mime_from_preferences(preferences: object) -> str:
    """Best-effort content type from a Zammad attachment's preferences hash."""
    if not isinstance(preferences, dict):
        return DEFAULT_MIME_TYPE
    for key in MIME_PREFERENCE_KEYS:
        value = preferences.get(key)
        if isinstance(value, str) and value.strip():
            return normalise_mime(value)
    return DEFAULT_MIME_TYPE


def mime_for_filename(filename: str | None) -> str | None:
    """Content type from the extension, or None if unknown.

    The pinned table wins over the platform's, so the same file classifies the
    same way on a developer's machine and in the container.
    """
    if not filename:
        return None
    _, dot, extension = filename.rpartition(".")
    if dot:
        pinned = _EXTENSION_MIME.get(f".{extension.lower()}")
        if pinned:
            return pinned
    guessed, _encoding = mimetypes.guess_type(filename, strict=False)
    return normalise_mime(guessed) if guessed else None


def is_text_mime(mime: str) -> bool:
    """True for media types that survive a text decode."""
    return mime.startswith("text/") or mime in TEXT_MIME_EXACT or mime.endswith(TEXT_MIME_SUFFIXES)


def _match_magic(data: bytes) -> str | None:
    for offset, signature, mime in _MAGIC:
        if data[offset : offset + len(signature)] == signature:
            return mime
    # RIFF containers carry their real type at offset 8.
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:4] == b"PK\x03\x04":
        return _zip_flavour(data)
    return None


def _zip_flavour(data: bytes) -> str:
    """Distinguish OOXML from a plain ZIP by looking at member names.

    Only the central directory is read - nothing is decompressed here, so a zip
    bomb costs nothing at this stage. A truncated or damaged archive is simply
    a plain ZIP as far as detection is concerned.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = archive.namelist()
    except (zipfile.BadZipFile, OSError, ValueError):
        return "application/zip"
    if any(name.startswith("word/") for name in names):
        return DOCX_MIME
    if any(name.startswith("xl/") for name in names):
        return XLSX_MIME
    if any(name.startswith("ppt/") for name in names):
        return PPTX_MIME
    return "application/zip"


def _looks_textual(data: bytes) -> bool:
    """True when the leading bytes decode cleanly and carry no NULs."""
    window = data[:_SNIFF_WINDOW]
    if b"\x00" in window:
        return False
    for charset in ("utf-8", "cp1252"):
        try:
            window.decode(charset)
        except UnicodeDecodeError:
            continue
        return True
    return False


def _classify(mime: str) -> tuple[Kind, bool]:
    if mime in IMAGE_MIME_TYPES:
        return Kind.IMAGE, False
    if mime in DOCUMENT_MIME_TYPES:
        return Kind.DOCUMENT, mime in TEXTUAL_DOCUMENT_MIME_TYPES
    if is_text_mime(mime):
        return Kind.TEXT, True
    return Kind.OPAQUE, False


def detect(
    data: bytes,
    *,
    filename: str | None = None,
    declared: str | None = None,
) -> Detection:
    """Determine what a file really is. See the module docstring for the order."""
    declared_mime = normalise_mime(declared)

    candidates: tuple[tuple[str | None, str], ...] = (
        (_match_magic(data), "magic"),
        (mime_for_filename(filename), "extension"),
        (declared_mime if declared_mime != DEFAULT_MIME_TYPE else None, "declared"),
    )
    for mime, source in candidates:
        if mime is None:
            continue
        kind, textual = _classify(mime)
        if kind is Kind.OPAQUE and source != "magic" and _looks_textual(data):
            # An unhelpful label on something that is plainly text.
            continue
        return Detection(mime, declared_mime, kind, textual, source)

    if _looks_textual(data):
        return Detection("text/plain", declared_mime, Kind.TEXT, True, "content")
    return Detection(DEFAULT_MIME_TYPE, declared_mime, Kind.OPAQUE, False, "content")


def decode_text(data: bytes, *, charset: str | None = None) -> tuple[str, str, bool]:
    """Decode bytes to text and say honestly which charset worked.

    Returns ``(text, charset_used, lossy)``. ``lossy`` is True only when every
    strict attempt failed and latin-1 was used as the terminal fallback, where
    each byte maps to a character but not necessarily the intended one. The
    previous implementation used ``errors='replace'`` silently, so a caller
    could not distinguish a clean decode from a mangled one.
    """
    ladder: tuple[str, ...] = _CHARSET_LADDER
    if charset:
        try:
            "".encode(charset)  # reject an unknown charset from a broken header
        except LookupError:
            pass
        else:
            ladder = (charset, *(c for c in _CHARSET_LADDER if c != charset))

    for candidate in ladder[:-1]:
        try:
            return data.decode(candidate), candidate, False
        except (UnicodeDecodeError, LookupError):
            continue
    terminal = ladder[-1]
    return data.decode(terminal, errors="replace"), terminal, True


__all__ = [
    "DEFAULT_MIME_TYPE",
    "DOCUMENT_MIME_TYPES",
    "DOCX_MIME",
    "IMAGE_MIME_TYPES",
    "MIME_PREFERENCE_KEYS",
    "PPTX_MIME",
    "TEXTUAL_DOCUMENT_MIME_TYPES",
    "TEXT_MIME_EXACT",
    "TEXT_MIME_SUFFIXES",
    "XLSX_MIME",
    "Detection",
    "Kind",
    "decode_text",
    "detect",
    "is_text_mime",
    "mime_for_filename",
    "mime_from_preferences",
    "normalise_mime",
]
