# Ticket Attachment Support Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make ticket attachments fully usable — read images, PDF, DOCX, XLSX, RTF and text regardless of how the upload mislabelled them, and attach files to tickets from text, base64, or an existing Zammad attachment.

**Architecture:** Type detection moves from the declared MIME label to the actual bytes and lives in a new pure module (`zammad/media.py`). Document-to-text conversion lives in a second new module (`zammad/extract.py`) behind four safety controls. `server._DecodingCtx` gains a byte-preserving `request_raw`, because the existing shim discards bytes by routing everything through `request_json`. Writing rides on the three article-creating tools via a shared upload helper — Zammad has no endpoint that attaches a file to an existing article.

**Tech Stack:** Python 3.14, FastMCP 3.3.1, bg-mcpcore, httpx, pydantic v2, pytest + pytest-asyncio. New optional extra `documents`: `pypdf`, `openpyxl`, `striprtf`, `defusedxml` (all pure Python — the production image is Alpine/musl, where `lxml` would compile from source).

**Spec:** `docs/superpowers/specs/2026-08-12-ticket-attachment-support-design.md`

## Global Constraints

- Working directory for all commands: `app/bg-zammad-mcp`. All paths below are relative to it unless prefixed with `../`.
- Python 3.14; `requires-python = ">=3.14"`.
- Ruff: `line-length = 100`, rules `E, F, W, I, B, UP, SIM, RUF`, `E501` ignored.
- Mypy strict. Every new function is fully annotated.
- Pytest `asyncio_mode = auto` — async tests need no decorator.
- Coverage gate `fail_under = 60`.
- **Tool count stays at 75.** No tool is added or renamed, so `EXPECTED_TOOLS` in `tests/test_tools_inventory.py` is not edited. Only parameters and return fields change.
- **Never commit `.env`** (it is gitignored and holds live secrets). It is edited by hand in Task 3 and never staged.
- Commit messages: Conventional Commits, English, **past tense** subject, mandatory body, no AI attribution, no `Co-Authored-By`.
- Do not push until the final task. One push per session.
- The full suite must be green before every commit: `.venv/Scripts/python.exe -m pytest -q -m "not integration"` (Windows) — 371 tests pass today.
- New dependencies must be pure Python. The production image is `python:3.14-alpine`.

---

### Task 1: File-type detection (`media.py`)

Pure, dependency-free, no I/O. This is the module that fixes the triggering bug: an RTF file uploaded as `application/msword` must be recognised as text from its bytes.

**Files:**
- Create: `src/zammad/media.py`
- Test: `tests/test_media.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `class Kind(StrEnum)` with members `IMAGE`, `TEXT`, `DOCUMENT`, `OPAQUE`
  - `@dataclass(frozen=True, slots=True) class Detection` with fields `mime_type: str`, `declared_mime_type: str`, `kind: Kind`, `textual: bool`, `source: str`
  - `def detect(data: bytes, *, filename: str | None = None, declared: str | None = None) -> Detection`
  - `def decode_text(data: bytes, *, charset: str | None = None) -> tuple[str, str, bool]` returning `(text, charset_used, lossy)`
  - `def mime_from_preferences(preferences: object) -> str`
  - `def normalise_mime(raw: str | None) -> str`
  - `def mime_for_filename(filename: str | None) -> str | None`
  - Constants `DEFAULT_MIME_TYPE`, `MIME_PREFERENCE_KEYS`, `IMAGE_MIME_TYPES`, `DOCUMENT_MIME_TYPES`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_media.py`:

```python
"""Type-detection tests — bytes decide, the upload label does not.

The named regression at the bottom is the case that triggered this work: a
customer RTF uploaded as application/msword, refused as binary while being
plain text.
"""

from __future__ import annotations

import pytest

from zammad import media


def test_png_magic_beats_a_wrong_label() -> None:
    data = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    d = media.detect(data, filename="thing.txt", declared="text/plain")
    assert d.mime_type == "image/png"
    assert d.kind is media.Kind.IMAGE
    assert d.source == "magic"
    assert d.textual is False


def test_pdf_magic_is_a_document() -> None:
    d = media.detect(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n", declared="application/octet-stream")
    assert d.mime_type == "application/pdf"
    assert d.kind is media.Kind.DOCUMENT
    assert d.textual is False


def test_docx_is_recognised_from_its_zip_members() -> None:
    d = media.detect(_ooxml(b"word/document.xml"), filename="brief.docx", declared=None)
    assert d.mime_type == (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    assert d.kind is media.Kind.DOCUMENT


def test_xlsx_is_recognised_from_its_zip_members() -> None:
    d = media.detect(_ooxml(b"xl/workbook.xml"), filename="zahlen.xlsx", declared=None)
    assert d.mime_type == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    assert d.kind is media.Kind.DOCUMENT


def test_plain_zip_stays_opaque() -> None:
    d = media.detect(_ooxml(b"readme.txt"), filename="bundle.zip", declared=None)
    assert d.mime_type == "application/zip"
    assert d.kind is media.Kind.OPAQUE


def test_extension_decides_when_there_is_no_magic() -> None:
    d = media.detect(b"a;b\n1;2\n", filename="werte.csv", declared="application/octet-stream")
    assert d.mime_type == "text/csv"
    assert d.kind is media.Kind.TEXT
    assert d.source == "extension"


def test_declared_type_is_the_last_resort() -> None:
    d = media.detect(b"payload", filename=None, declared="application/json")
    assert d.mime_type == "application/json"
    assert d.kind is media.Kind.TEXT
    assert d.source == "declared"


def test_unlabelled_printable_bytes_are_treated_as_text() -> None:
    d = media.detect(b"just some notes\nsecond line\n", filename="notes", declared=None)
    assert d.kind is media.Kind.TEXT
    assert d.source == "content"


def test_bytes_with_nuls_stay_opaque() -> None:
    d = media.detect(b"abc\x00\x01\x02def", filename="mystery", declared=None)
    assert d.kind is media.Kind.OPAQUE
    assert d.mime_type == media.DEFAULT_MIME_TYPE


def test_mime_type_is_read_from_any_preference_key_and_normalised() -> None:
    assert media.mime_from_preferences({"Content-Type": "TEXT/Plain; charset=utf-8"}) == "text/plain"
    assert media.mime_from_preferences({"Mime-Type": "image/PNG"}) == "image/png"
    assert media.mime_from_preferences({"content_type": "text/csv"}) == "text/csv"
    assert media.mime_from_preferences({"mime_type": "application/zip"}) == "application/zip"
    assert media.mime_from_preferences(None) == media.DEFAULT_MIME_TYPE
    assert media.mime_from_preferences({}) == media.DEFAULT_MIME_TYPE


@pytest.mark.parametrize(
    ("data", "charset", "expected_text", "expected_charset", "expected_lossy"),
    [
        (b"hallo", None, "hallo", "utf-8", False),
        ("grüße".encode(), None, "grüße", "utf-8", False),
        ("grüße".encode("cp1252"), None, "grüße", "cp1252", False),
        ("grüße".encode("cp1252"), "windows-1252", "grüße", "windows-1252", False),
        (b"\x81\x8d\x90", None, "\x81\x8d\x90", "latin-1", True),
    ],
)
def test_decode_text_reports_what_it_actually_did(
    data: bytes, charset: str | None, expected_text: str,
    expected_charset: str, expected_lossy: bool,
) -> None:
    text, used, lossy = media.decode_text(data, charset=charset)
    assert text == expected_text
    assert used == expected_charset
    assert lossy is expected_lossy


def test_a_broken_header_charset_does_not_break_decoding() -> None:
    text, used, lossy = media.decode_text(b"hallo", charset="not-a-charset")
    assert text == "hallo"
    assert used == "utf-8"
    assert lossy is False


# ── the regression this whole feature exists for ─────────────────────────────


def test_rtf_declared_as_msword_is_read_as_text() -> None:
    """Technische-Daten-Liquid-Liquid.rtf: uploaded as application/msword,
    refused as binary, while being text with control words."""
    data = rb"{\rtf1\ansi\deff0 {\fonttbl{\f0 Arial;}}\f0 Technische Daten\par}"
    d = media.detect(data, filename="Technische-Daten-Liquid-Liquid.rtf",
                     declared="application/msword")
    assert d.mime_type == "application/rtf"
    assert d.declared_mime_type == "application/msword"
    assert d.kind is media.Kind.DOCUMENT
    assert d.textual is True, "RTF must never be refused as binary"
    assert d.source == "magic"


def _ooxml(member: bytes) -> bytes:
    """A minimal real ZIP containing one named member."""
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(member.decode(), "x")
    return buf.getvalue()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_media.py -q`
Expected: collection error — `ModuleNotFoundError: No module named 'zammad.media'`

- [ ] **Step 3: Write the implementation**

Create `src/zammad/media.py`:

```python
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

# Types that extract.py knows how to turn into text.
DOCUMENT_MIME_TYPES: Final = frozenset(
    {"application/pdf", DOCX_MIME, XLSX_MIME, "application/rtf", "text/rtf"}
)

# Documents that are themselves text. RTF is prose wrapped in control words:
# extracting it is an improvement, refusing it is a bug. If extraction is
# unavailable the raw text is still readable, and `textual` is what tells the
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

# (offset, signature, mime). Ordered longest-signature-first within an offset so
# a more specific match wins; GIF and the RIFF family are handled separately.
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
    """Content type guessed from the extension, or None if unknown."""
    if not filename:
        return None
    guessed, _ = mimetypes.guess_type(filename, strict=False)
    return normalise_mime(guessed) if guessed else None


def is_text_mime(mime: str) -> bool:
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_media.py -q`
Expected: PASS, 22 tests.

If `test_declared_type_is_the_last_resort` fails because `application/json` was reached via the content heuristic rather than the declared label, check that `_looks_textual(b"payload")` is not short-circuiting the loop — the `continue` only applies when `kind is Kind.OPAQUE`, and `application/json` classifies as `TEXT`.

- [ ] **Step 5: Run the full suite and lint**

Run:
```bash
.venv/Scripts/python.exe -m pytest -q -m "not integration"
.venv/Scripts/python.exe -m ruff check src/zammad/media.py tests/test_media.py
.venv/Scripts/python.exe -m mypy src/zammad/media.py
```
Expected: 393 passed; no ruff findings; no mypy errors.

- [ ] **Step 6: Commit**

```bash
git add src/zammad/media.py tests/test_media.py
git commit -F - <<'EOF'
feat(attachments): added byte-first file-type detection

Attachment type was decided from the content type the uploading
client claimed, so a file mislabelled at upload was classified
wrongly with no way to correct it. A customer RTF sent as
application/msword was refused as binary while being plain text.

Detection now resolves magic bytes first, then the file extension,
then the declared label, and finally the shape of the content
itself. The module is pure and dependency-free, so the whole
decision table is testable without a server.

Also replaces the silent errors='replace' decode: decode_text walks
utf-8, cp1252 and latin-1 and reports which one worked and whether
the result is lossy, so a caller can tell a clean decode from a
mangled one.
EOF
```

---

### Task 2: Byte-preserving transport (`request_raw`)

**Files:**
- Modify: `src/server.py:32-52` (`_DecodingCtx`)
- Modify: `src/zammad/tools/__init__.py:25-38` (`ToolContext` Protocol)
- Test: `tests/test_request_raw.py` (create)

**Interfaces:**
- Consumes: `zammad.errors.from_status` (already used by `_DecodingCtx.request`).
- Produces: `async def request_raw(self, method: str, path: str, **kwargs: Any) -> httpx.Response` on both the Protocol and `_DecodingCtx`. Non-2xx raises the same `ZammadError` subclasses `request` raises.

- [ ] **Step 1: Write the failing test**

Create `tests/test_request_raw.py`:

```python
"""The byte-preserving path.

_DecodingCtx.request routes everything through bg-mcpcore's request_json,
which falls back to response.text - a lossy UTF-8 decode. That is why binary
attachments were unreachable. request_raw hands back the untouched response so
the bytes survive, while raising the same typed errors as request.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from server import _DecodingCtx
from zammad.errors import ZammadForbidden, ZammadNotFound

PNG = b"\x89PNG\r\n\x1a\n\x00\x01\x02\xff\xfe"


class FakeCoreCtx:
    """Stands in for bg-mcpcore's ToolContext: request returns httpx.Response."""

    def __init__(self, response: httpx.Response) -> None:
        self.settings = None
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self._response = response

    async def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        self.calls.append((method, path, kwargs))
        return self._response

    async def request_json(self, method: str, path: str, **kwargs: Any) -> Any:
        raise AssertionError("request_raw must not go through request_json")


def _response(status: int, content: bytes, content_type: str) -> httpx.Response:
    return httpx.Response(
        status_code=status,
        content=content,
        headers={"content-type": content_type},
        request=httpx.Request("GET", "https://zammad.example/api/v1/x"),
    )


async def test_raw_bytes_survive_intact() -> None:
    core = FakeCoreCtx(_response(200, PNG, "image/png"))
    ctx = _DecodingCtx(core)

    response = await ctx.request_raw("GET", "/ticket_attachment/5/42/7")

    assert response.content == PNG, "every byte must survive"
    assert core.calls == [("GET", "/ticket_attachment/5/42/7", {})]


async def test_headers_are_reachable_for_the_charset() -> None:
    core = FakeCoreCtx(_response(200, b"hallo", "text/plain; charset=iso-8859-1"))
    response = await _DecodingCtx(core).request_raw("GET", "/x")
    assert response.headers["content-type"] == "text/plain; charset=iso-8859-1"


@pytest.mark.parametrize(
    ("status", "expected"),
    [(403, ZammadForbidden), (404, ZammadNotFound)],
)
async def test_non_2xx_raises_the_same_typed_errors_as_request(
    status: int, expected: type[Exception]
) -> None:
    core = FakeCoreCtx(_response(status, b'{"error":"nope"}', "application/json"))
    with pytest.raises(expected):
        await _DecodingCtx(core).request_raw("GET", "/x")


async def test_a_non_json_error_body_does_not_break_the_error_path() -> None:
    core = FakeCoreCtx(_response(404, b"<html>gone</html>", "text/html"))
    with pytest.raises(ZammadNotFound):
        await _DecodingCtx(core).request_raw("GET", "/x")
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_request_raw.py -q`
Expected: FAIL with `AttributeError: '_DecodingCtx' object has no attribute 'request_raw'`

- [ ] **Step 3: Add `request_raw` to `_DecodingCtx`**

In `src/server.py`, extend the `_DecodingCtx` docstring and add the method after `request`:

```python
class _DecodingCtx:
    """Adapt bg-mcpcore's ``ToolContext`` to the Zammad tools' decode-or-raise I/O.

    Delegates to ``ctx.request_json``, binding Zammad's typed-error factory so a
    non-2xx response raises the same ``ZammadError`` subclass the eight tool
    modules already expect — they need no changes.

    ``request_raw`` is the byte-preserving counterpart. ``request_json`` decodes
    a 2xx body as JSON or as ``response.text`` — a UTF-8 decode with
    ``errors='replace'`` — which destroys binary content irreversibly. Core's
    own ``ctx.request`` already returns the untouched ``httpx.Response``, so the
    bytes were always reachable; only this shim hid them. Attachments are the
    single caller.
    """

    def __init__(self, ctx: Any) -> None:
        self._ctx = ctx
        self.settings = ctx.settings

    async def request(self, method: str, path: str, **kwargs: Any) -> Any:
        from zammad.errors import from_status

        return await self._ctx.request_json(
            method,
            path,
            error_factory=lambda status, body: from_status(status, body=body),
            **kwargs,
        )

    async def request_raw(self, method: str, path: str, **kwargs: Any) -> Any:
        """Upstream call whose body is NOT decoded. Raises the same typed errors."""
        from zammad.errors import from_status

        response = await self._ctx.request(method, path, **kwargs)
        if 200 <= response.status_code < 300:
            return response
        body: dict[str, Any] = {}
        if "json" in response.headers.get("content-type", ""):
            try:
                parsed = response.json()
            except ValueError:
                parsed = None
            if isinstance(parsed, dict):
                body = parsed
        raise from_status(response.status_code, body=body)
```

- [ ] **Step 4: Declare it on the Protocol**

In `src/zammad/tools/__init__.py`, extend the `ToolContext` Protocol:

```python
class ToolContext(Protocol):
    """Structural type the tool modules call against.

    Implemented at runtime by ``server._DecodingCtx``. ``request`` returns the
    decoded body (dict/list/str) on a 2xx response and raises a typed
    ``zammad.errors.ZammadError`` on any non-2xx response. ``request_raw``
    returns the undecoded ``httpx.Response`` for callers that must not lose
    bytes — attachments, and nothing else.
    """

    settings: Any

    async def request(self, method: str, path: str, **kwargs: Any) -> Any: ...

    async def request_raw(self, method: str, path: str, **kwargs: Any) -> Any: ...
```

Note: the return type stays `Any` rather than `httpx.Response` so the Protocol
module keeps its zero imports; the concrete type is documented instead.

- [ ] **Step 5: Give the test harness a `request_raw`**

In `tests/test_tools_inventory.py`, add to `RecordingCtx` (after `request`):

```python
    async def request_raw(self, method: str, path: str, **kwargs: Any) -> Any:
        """Byte-preserving counterpart. Answers from ``raw_responses`` in turn."""
        self.calls.append({"method": method, "path": path, **kwargs})
        if self._raw_queue:
            return self._raw_queue.pop(0)
        raise AssertionError(
            f"unexpected request_raw({method} {path}) - pass raw_responses= to RecordingCtx"
        )
```

and extend its `__init__` signature and body:

```python
    def __init__(
        self,
        response: Any = None,
        *,
        responses: list[Any] | None = None,
        raw_responses: list[Any] | None = None,
    ) -> None:
        self.settings = None
        self.calls: list[dict[str, Any]] = []
        self._queue = list(responses) if responses is not None else None
        self._response = {} if response is None else response
        self._raw_queue = list(raw_responses) if raw_responses is not None else []
```

Update the class docstring's final paragraph to mention `raw_responses`.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_request_raw.py -q`
Expected: PASS, 5 tests.

- [ ] **Step 7: Run the full suite and lint**

Run:
```bash
.venv/Scripts/python.exe -m pytest -q -m "not integration"
.venv/Scripts/python.exe -m ruff check src tests
```
Expected: 398 passed; no ruff findings.

- [ ] **Step 8: Commit**

```bash
git add src/server.py src/zammad/tools/__init__.py tests/test_request_raw.py tests/test_tools_inventory.py
git commit -F - <<'EOF'
feat(server): added a byte-preserving upstream call

The attachment module documented binary downloads as impossible
because "this context has no byte-preserving path". That was true of
the shim and false of the layer beneath it: bg-mcpcore's
ToolContext.request already returns the raw httpx.Response, and only
_DecodingCtx hid it by routing everything through request_json,
which falls back to response.text.

request_raw exposes the untouched response while raising the same
typed ZammadError subclasses, so error handling stays uniform. No
change to bg-mcpcore was needed.

The recording test context gained a matching raw_responses queue.
EOF
```

---

### Task 3: Attachment limits as settings (+ `.env.example` and `.env`)

**Files:**
- Modify: `src/config.py` (add fields after `zammad_verify_tls`)
- Modify: `../../.env.example` (new section before `# Rate Limiting`)
- Modify: `../../.env` (same section — **hand-edited, never staged**)
- Test: `tests/test_config.py` (append)

**Interfaces:**
- Consumes: nothing.
- Produces: five `Settings` fields — `zammad_attachment_max_read_bytes: int`, `zammad_attachment_read_ceiling_bytes: int`, `zammad_attachment_upload_enabled: bool`, `zammad_attachment_max_upload_bytes: int`, `zammad_attachment_max_article_bytes: int`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_config.py`:

```python
# ── attachment limits ────────────────────────────────────────────────────────


def test_attachment_limits_have_the_documented_defaults(monkeypatch) -> None:
    monkeypatch.setenv("AUTH_MODE", "none")
    monkeypatch.setenv("ZAMMAD_API_TOKEN", "t")
    s = get_settings(force_reload=True)
    assert s.zammad_attachment_max_read_bytes == 5 * 1024 * 1024
    assert s.zammad_attachment_read_ceiling_bytes == 20 * 1024 * 1024
    assert s.zammad_attachment_upload_enabled is True
    assert s.zammad_attachment_max_upload_bytes == 10 * 1024 * 1024
    assert s.zammad_attachment_max_article_bytes == 25 * 1024 * 1024


def test_read_ceiling_below_the_read_default_is_refused(monkeypatch) -> None:
    """A ceiling under the default would make the default itself unreachable."""
    monkeypatch.setenv("AUTH_MODE", "none")
    monkeypatch.setenv("ZAMMAD_API_TOKEN", "t")
    monkeypatch.setenv("ZAMMAD_ATTACHMENT_MAX_READ_BYTES", str(20 * 1024 * 1024))
    monkeypatch.setenv("ZAMMAD_ATTACHMENT_READ_CEILING_BYTES", str(5 * 1024 * 1024))
    with pytest.raises(ValidationError, match="READ_CEILING"):
        get_settings(force_reload=True)


def test_article_limit_below_the_per_file_limit_is_refused(monkeypatch) -> None:
    monkeypatch.setenv("AUTH_MODE", "none")
    monkeypatch.setenv("ZAMMAD_API_TOKEN", "t")
    monkeypatch.setenv("ZAMMAD_ATTACHMENT_MAX_UPLOAD_BYTES", str(20 * 1024 * 1024))
    monkeypatch.setenv("ZAMMAD_ATTACHMENT_MAX_ARTICLE_BYTES", str(5 * 1024 * 1024))
    with pytest.raises(ValidationError, match="MAX_ARTICLE"):
        get_settings(force_reload=True)
```

Check the existing imports at the top of `tests/test_config.py`. If `pytest` or
`ValidationError` are not already imported, add:

```python
import pytest
from pydantic import ValidationError
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_config.py -q -k attachment`
Expected: FAIL with `AttributeError: 'Settings' object has no attribute 'zammad_attachment_max_read_bytes'`

- [ ] **Step 3: Add the fields and the cross-field validator**

In `src/config.py`, add after `zammad_verify_tls: bool = True`:

```python
    # ── Attachment limits ────────────────────────────────────────────────────
    # These were hardcoded constants inside a tool module, which put them out of
    # an operator's reach entirely. All four byte limits measure DECODED bytes:
    # base64 inflates by 4/3, so a limit on the encoded string would silently
    # pass only three quarters of its nominal value and be inexplicable to the
    # caller. Zammad's own body limit applies to the inflated size, so the
    # article ceiling keeps headroom (25 MiB of payload is roughly 33 MiB of
    # request body).
    zammad_attachment_max_read_bytes: int = Field(
        default=5 * 1024 * 1024,
        ge=1,
        le=100 * 1024 * 1024,
        description="Default max_bytes for download_ticket_attachment.",
    )
    zammad_attachment_read_ceiling_bytes: int = Field(
        default=20 * 1024 * 1024,
        ge=1,
        le=100 * 1024 * 1024,
        description=(
            "Hard ceiling on max_bytes. Without it a model that hits the size "
            "guard simply retries with a larger number."
        ),
    )
    zammad_attachment_upload_enabled: bool = Field(
        default=True,
        description=(
            "Allow attaching files. When false the attachments parameter is "
            "removed from the tool schemas rather than rejected at call time."
        ),
    )
    zammad_attachment_max_upload_bytes: int = Field(
        default=10 * 1024 * 1024,
        ge=1,
        le=100 * 1024 * 1024,
        description="Max decoded size of a single uploaded attachment.",
    )
    zammad_attachment_max_article_bytes: int = Field(
        default=25 * 1024 * 1024,
        ge=1,
        le=200 * 1024 * 1024,
        description="Max decoded size of all attachments in one article.",
    )
```

Add the cross-field check inside `validate_provider_auth`, at the very top so it
runs in every auth mode:

```python
    def validate_provider_auth(self) -> None:
        """Per-mode credential checks (core invariants already ran)."""
        if self.zammad_attachment_read_ceiling_bytes < self.zammad_attachment_max_read_bytes:
            raise ValueError(
                "ZAMMAD_ATTACHMENT_READ_CEILING_BYTES must be >= "
                "ZAMMAD_ATTACHMENT_MAX_READ_BYTES, otherwise the default read "
                "size is above its own hard ceiling and every read fails."
            )
        if self.zammad_attachment_max_article_bytes < self.zammad_attachment_max_upload_bytes:
            raise ValueError(
                "ZAMMAD_ATTACHMENT_MAX_ARTICLE_BYTES must be >= "
                "ZAMMAD_ATTACHMENT_MAX_UPLOAD_BYTES, otherwise a single file at "
                "the per-file limit can never be attached."
            )
        if self.auth_mode is AuthMode.NONE:
            ...  # unchanged from here
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_config.py -q`
Expected: PASS.

If the two failure tests report `ValueError` rather than `ValidationError`,
check how `test_config.py`'s existing failure tests assert — match that
project convention and adjust the two new tests to use the same exception type.

- [ ] **Step 5: Add the section to `.env.example`**

Insert into `../../.env.example`, immediately before the `# Rate Limiting`
section header:

```bash
# =============================================================================
# Attachments
# =============================================================================
# All byte limits measure DECODED bytes. Base64 inflates by 4/3, so a limit on
# the encoded payload would pass only three quarters of its nominal value.
# Zammad's own request-body limit applies to the INFLATED size, so keep the
# article limit comfortably below it (25 MiB payload is roughly 33 MiB body).

# Reading: default and hard ceiling for download_ticket_attachment's max_bytes.
# The ceiling exists because a model that hits the default simply retries with
# a bigger number; it must be >= the default.
ZAMMAD_ATTACHMENT_MAX_READ_BYTES=5242880
ZAMMAD_ATTACHMENT_READ_CEILING_BYTES=20971520

# Writing: set to false to remove the `attachments` parameter from
# reply_to_customer, add_internal_note and create_ticket entirely. The
# parameter disappears from the published schema rather than failing at call
# time, so a client never discovers the restriction by trying it.
ZAMMAD_ATTACHMENT_UPLOAD_ENABLED=true

# Per file, and per article across all of its files. The article limit must be
# >= the per-file limit.
ZAMMAD_ATTACHMENT_MAX_UPLOAD_BYTES=10485760
ZAMMAD_ATTACHMENT_MAX_ARTICLE_BYTES=26214400


```

- [ ] **Step 6: Add the same section to `.env`**

Insert the identical block into `../../.env` at the same position (immediately
before its `# Rate Limiting` header).

**Do this by hand — do NOT run `scripts/generate-env.py`.** That script renders
`.env` *from* `.env.example` and would overwrite the live file, destroying the
real `AUTH_JWT_SIGNING_KEY`, `AUTH_STORAGE_ENCRYPTION_KEY` and OAuth secrets it
holds.

Verify afterwards that `.env` is still ignored and that no secret moved:

```bash
git check-ignore -v ../../.env
git status --porcelain
```
Expected: `.gitignore:6:.env` on the first command; the second must not list
`.env`.

- [ ] **Step 7: Commit (`.env.example` only)**

```bash
git add src/config.py tests/test_config.py ../../.env.example
git commit -F - <<'EOF'
feat(config): exposed the attachment limits as settings

The read limits were constants inside a tool module, so an operator
could not change them without editing source. Uploading needed
limits of its own. Five settings now cover both directions, with
cross-field validation that refuses a ceiling below its own default
and an article limit below the per-file limit - both configurations
that fail every call at runtime instead of at boot.

All four byte limits measure decoded bytes. Base64 inflates by 4/3,
so a limit on the encoded string would pass three quarters of its
nominal value and be inexplicable to the caller.
EOF
```

Confirm `.env` is not in the commit: `git show --stat HEAD` must list exactly
three files.

---

### Task 4: Read path — images, text and blobs

Delivers the triggering fix and image reading. Document extraction follows in
Tasks 5–6; until then PDF/DOCX/XLSX return as blobs, and RTF returns as raw
text.

**Files:**
- Modify: `src/zammad/tools/attachments.py` (rewrite `download_ticket_attachment`; `list_ticket_attachments` keeps its behaviour but sources MIME logic from `media`)
- Test: `tests/test_tools_attachments.py` (extend)

**Interfaces:**
- Consumes: `zammad.media.{detect, decode_text, mime_from_preferences, Kind}`; `ctx.request_raw`.
- Produces: `download_ticket_attachment` returning `fastmcp.tools.ToolResult`; module constants `DEFAULT_MAX_BYTES` and `MAX_ALLOWED_BYTES` are replaced by `_read_limits(ctx) -> tuple[int, int]`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_tools_attachments.py`. Also add these imports at the top:

```python
import httpx
from mcp.types import EmbeddedResource, ImageContent
```

and this helper next to `_article`:

```python
def _raw(content: bytes, content_type: str) -> httpx.Response:
    return httpx.Response(
        status_code=200,
        content=content,
        headers={"content-type": content_type},
        request=httpx.Request("GET", "https://zammad.example/x"),
    )


def _build_raw(responses: list[Any], raw: list[Any]) -> tuple[FastMCP, ScriptedCtx]:
    mcp: FastMCP = FastMCP("test-attachments")
    ctx = ScriptedCtx(responses)
    ctx.raw_queue = list(raw)
    attachments.register(mcp, ctx)
    return mcp, ctx
```

and extend `ScriptedCtx` with:

```python
    raw_queue: list[Any] = []

    async def request_raw(self, method: str, path: str, **kwargs: Any) -> Any:
        self.calls.append({"method": method, "path": path, **kwargs})
        assert self.raw_queue, f"unexpected request_raw({method} {path})"
        return self.raw_queue.pop(0)
```

The tests:

```python
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + bytes(range(256))


async def test_a_png_comes_back_as_an_image_block() -> None:
    """The whole point: the model must be able to SEE a screenshot."""
    mcp, _ = _build_raw(
        [
            _article(
                attachments=[
                    {"id": 7, "filename": "screenshot.png", "size": str(len(PNG_BYTES)),
                     "preferences": {"Content-Type": "image/png"}}
                ]
            )
        ],
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
        [
            _article(
                attachments=[
                    {"id": 7, "filename": "Technische-Daten-Liquid-Liquid.rtf",
                     "size": str(len(rtf)),
                     "preferences": {"Content-Type": "application/msword"}}
                ]
            )
        ],
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
        [
            _article(
                attachments=[
                    {"id": 7, "filename": "thing.bin", "size": str(len(blob)),
                     "preferences": {"Content-Type": "application/octet-stream"}}
                ]
            )
        ],
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
        mcp, "download_ticket_attachment", ticket_id=5, article_id=42,
        attachment_id=7, mode="raw",
    )
    assert result.structured_content["content_kind"] == "blob"


async def test_mode_text_forces_a_decode_for_an_unrecognised_file() -> None:
    mcp, _ = _build_raw(
        [
            _article(
                attachments=[
                    {"id": 7, "filename": "weird", "size": "5",
                     "preferences": {"Content-Type": "application/x-nonsense"}}
                ]
            )
        ],
        [_raw(b"hallo", "application/x-nonsense")],
    )
    result = await _call(
        mcp, "download_ticket_attachment", ticket_id=5, article_id=42,
        attachment_id=7, mode="text",
    )
    assert result.structured_content["content"] == "hallo"
    assert result.structured_content["content_kind"] == "text"


async def test_a_lossy_decode_is_reported_as_such() -> None:
    mcp, _ = _build_raw(
        [
            _article(
                attachments=[
                    {"id": 7, "filename": "broken.txt", "size": "3",
                     "preferences": {"Content-Type": "text/plain"}}
                ]
            )
        ],
        [_raw(b"\x81\x8d\x90", "text/plain")],
    )
    result = await _call(
        mcp, "download_ticket_attachment", ticket_id=5, article_id=42, attachment_id=7
    )
    assert result.structured_content["decoding"]["lossy"] is True
    assert result.structured_content["decoding"]["charset"] == "latin-1"


async def test_the_charset_from_the_response_header_wins() -> None:
    mcp, _ = _build_raw(
        [
            _article(
                attachments=[
                    {"id": 7, "filename": "umlaut.txt", "size": "6",
                     "preferences": {"Content-Type": "text/plain"}}
                ]
            )
        ],
        [_raw("grüße".encode("cp1252"), "text/plain; charset=windows-1252")],
    )
    result = await _call(
        mcp, "download_ticket_attachment", ticket_id=5, article_id=42, attachment_id=7
    )
    assert result.structured_content["content"] == "grüße"
    assert result.structured_content["decoding"]["charset"] == "windows-1252"
```

Add `import base64` to the test module's imports.

Then **update the two existing tests that assert the old flat return shape**:

- `test_download_fetches_metadata_then_the_file` — the second call is now
  `request_raw`, so give it `_build_raw([_article()], [_raw(b"line one\nline two", "text/plain")])`
  and extend the expected `structured_content` with
  `"detected_mime_type": "text/plain"`, `"content_kind": "text"`,
  `"extraction": {"status": "not_applicable", "tool": None, "reason": None}`,
  `"decoding": {"charset": "utf-8", "lossy": False}`.
- `test_download_reserialises_a_json_attachment_to_text` — the raw path no
  longer parses JSON, so the body arrives as bytes and `content` is the literal
  file text. Assert `'"ok": true' in content` against
  `_raw(b'{\n  "ok": true\n}', "application/json")`.

**Delete** `test_download_refuses_a_binary_type_without_transferring_it` and
`test_max_bytes_has_a_hard_ceiling_in_the_schema` in the same commit — the first
asserts exactly the behaviour being removed, the second referenced the deleted
`attachments.MAX_ALLOWED_BYTES` constant. Their replacements are
`test_an_unknown_binary_comes_back_as_a_blob_not_an_error` and
`test_max_bytes_ceiling_comes_from_settings` below:

```python
async def test_max_bytes_ceiling_comes_from_settings() -> None:
    class Ctx(ScriptedCtx):
        settings = type("S", (), {
            "zammad_attachment_max_read_bytes": 1234,
            "zammad_attachment_read_ceiling_bytes": 5678,
        })()

    mcp: FastMCP = FastMCP("test-attachments")
    attachments.register(mcp, Ctx([]))
    schema = (await _tools(mcp))["download_ticket_attachment"].parameters or {}
    assert schema["properties"]["max_bytes"]["maximum"] == 5678
    assert schema["properties"]["max_bytes"]["default"] == 1234
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_tools_attachments.py -q`
Expected: FAIL — the new tests error on `request_raw` / missing keys.

- [ ] **Step 3: Rewrite the read path**

Replace the module docstring's "Why binary attachments are refused" section and
the download tool in `src/zammad/tools/attachments.py`. The new docstring
section:

```
How a file's type is decided
----------------------------
Not by the label. Zammad stores whatever content type the uploading client
claimed, and clients lie: a customer RTF arrived as application/msword and was
refused as binary while being plain text. ``zammad.media.detect`` resolves
magic bytes first, then the extension, then the declared label, then the shape
of the content. The declared value is still reported as ``mime_type`` so a
caller can see the disagreement; the effective one is ``detected_mime_type``.

What comes back
---------------
Images return as an MCP ImageContent block, so the model actually sees the
screenshot rather than a base64 string. Text returns decoded, with the charset
that worked and whether the result is lossy. Documents return as extracted
text. Everything else returns as metadata plus a base64 blob. ``mode`` overrides
the routing: 'text' forces a decode, 'raw' forces the blob. Nothing is refused
for being binary any more.
```

Replace the constants block and the tool. Keep `_as_int`, `_find_attachment`
and `_attachment_row`, but have `_attachment_row` call
`media.mime_from_preferences` instead of the local `_mime_type`, and delete the
local `_mime_type`, `_is_text_mime`, `MIME_PREFERENCE_KEYS`, `DEFAULT_MIME_TYPE`,
`TEXT_MIME_EXACT`, `TEXT_MIME_SUFFIXES`, `DEFAULT_MAX_BYTES` and
`MAX_ALLOWED_BYTES`:

```python
import base64
from typing import TYPE_CHECKING, Annotated, Any

from fastmcp.exceptions import ToolError
from fastmcp.tools import ToolResult
from mcp.types import (
    BlobResourceContents,
    EmbeddedResource,
    ImageContent,
    TextContent,
)
from mcp.types import ToolAnnotations
from pydantic import AnyUrl, Field

from .. import media
from ..projection import envelope
from . import ToolContext

# Fallbacks for a context without settings — the recording test harness, and any
# future caller that constructs the tools directly.
FALLBACK_MAX_READ_BYTES = 5 * 1024 * 1024
FALLBACK_READ_CEILING_BYTES = 20 * 1024 * 1024

READ_MODES = ("auto", "text", "raw")


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
```

The tool body, replacing the old `download_ticket_attachment`:

```python
    default_max_bytes, ceiling_bytes = _read_limits(ctx)

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
            "Files larger than `max_bytes` are refused before any data is "
            "transferred. Needs the same access as reading the ticket."
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
            Field(ge=1, le=ceiling_bytes, description="Refuse anything larger"),
        ] = default_max_bytes,
        mode: Annotated[
            str,
            Field(description="'auto' (default), 'text' to force a decode, 'raw' for bytes"),
        ] = "auto",
    ) -> ToolResult:
        if mode not in READ_MODES:
            raise ToolError(f"mode must be one of {', '.join(READ_MODES)} (got {mode!r}).")

        # The metadata round-trip is deliberate: size is only knowable from the
        # article, and knowing it BEFORE the download is what lets an oversized
        # file be refused without transferring it. Both requests need identical
        # permissions, so this cannot fail in a way the download would not.
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
            # stores.size is nullable, so the pre-flight guard could not run.
            size = len(data)
            if size > max_bytes:
                raise ToolError(
                    f"Attachment {filename!r} is {size} bytes, over the {max_bytes} "
                    "byte limit. Zammad reported no size for it, so this could only "
                    "be checked after the transfer."
                )

        detection = media.detect(data, filename=filename, declared=declared)
        return _build_result(
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
```

And the result builder at module level:

```python
def _build_result(
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
        "mime_type": declared,
        "detected_mime_type": detection.mime_type,
        "size_bytes": size,
        "content": None,
        "content_kind": kind.value,
        "extraction": {"status": "not_applicable", "tool": None, "reason": None},
        "decoding": None,
    }

    if kind is media.Kind.IMAGE:
        block: Any = ImageContent(
            type="image",
            data=base64.b64encode(data).decode(),
            mimeType=detection.mime_type,
        )
        summary = f"{filename} ({detection.mime_type}, {size} bytes)"
        return ToolResult(content=[TextContent(type="text", text=summary), block],
                          structured_content=base)

    if kind is media.Kind.TEXT:
        text, used, lossy = media.decode_text(data, charset=charset)
        base["content"] = text
        base["content_kind"] = "text"
        base["decoding"] = {"charset": used, "lossy": lossy}
        return ToolResult(content=[TextContent(type="text", text=text)],
                          structured_content=base)

    # DOCUMENT is routed here too until Task 6 wires extraction in.
    base["content_kind"] = "blob"
    resource = BlobResourceContents(
        uri=AnyUrl(f"zammad://ticket/{ticket_id}/article/{article_id}/attachment/{attachment_id}"),
        mimeType=detection.mime_type,
        blob=base64.b64encode(data).decode(),
    )
    summary = (
        f"{filename} ({detection.mime_type}, {size} bytes) returned as raw bytes; "
        "its content could not be turned into text."
    )
    return ToolResult(
        content=[TextContent(type="text", text=summary),
                 EmbeddedResource(type="resource", resource=resource)],
        structured_content=base,
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_tools_attachments.py -q`
Expected: PASS.

If FastMCP rejects `structured_content` because the tool's inferred output
schema no longer matches (the return annotation is now `ToolResult`), confirm
that `mcp.tool(...)` on a `ToolResult`-returning function publishes no output
schema — FastMCP treats `ToolResult` as a pass-through. If a schema is still
inferred, add `output_schema=None` to the `@mcp.tool(...)` call.

- [ ] **Step 5: Run the full suite and lint**

Run:
```bash
.venv/Scripts/python.exe -m pytest -q -m "not integration"
.venv/Scripts/python.exe -m ruff check src tests
```
Expected: all green. `tests/test_tools_inventory.py` must still pass unchanged —
the tool count is untouched.

- [ ] **Step 6: Commit**

```bash
git add src/zammad/tools/attachments.py tests/test_tools_attachments.py
git commit -F - <<'EOF'
feat(attachments): read images, text and unknown binaries

download_ticket_attachment refused anything whose declared MIME type
was not text, so a screenshot could not be looked at and a file
mislabelled at upload could not be read at all.

Images now return as an MCP ImageContent block, so the model sees
the picture instead of a base64 string. Text returns decoded, with
the charset that worked and an explicit lossy flag. Everything else
returns as metadata plus a blob rather than an error. The `mode`
parameter forces a text decode or the raw bytes, so no file can end
up unreachable.

The size guard still runs on metadata before any transfer - being
able to move bytes is not a reason to pull 200 MB in order to
reject it.

Read limits now come from settings; the two module constants are
gone. Removed the test that asserted binary refusal, which is the
behaviour being replaced.
EOF
```

---

### Task 5: Document extraction (`extract.py`)

**Files:**
- Create: `src/zammad/extract.py`
- Create: `tests/test_extract.py`
- Modify: `pyproject.toml` (new `documents` optional dependency group)
- Modify: `Dockerfile:30` and `Dockerfile:51` (install the extra)

**Interfaces:**
- Consumes: `zammad.media.{DOCX_MIME, XLSX_MIME, decode_text}`.
- Produces:
  - `@dataclass(frozen=True, slots=True) class Extraction` with `text: str | None`, `status: str`, `tool: str | None`, `reason: str | None`
  - `async def extract(data: bytes, *, mime_type: str, timeout: float = EXTRACT_TIMEOUT_SECONDS) -> Extraction`
  - `class ExtractionRefused(Exception)`
  - Constants `EXTRACT_TIMEOUT_SECONDS`, `MAX_UNCOMPRESSED_BYTES`, `MAX_COMPRESSION_RATIO`

- [ ] **Step 1: Add the dependencies**

In `pyproject.toml`, add to `[project.optional-dependencies]`:

```toml
documents = [
    # Attachment text extraction. All pure Python on purpose: the production
    # image is python:3.14-alpine, and musl has no manylinux wheels, so a
    # package with a C extension would be compiled at image-build time.
    # python-docx is deliberately absent - see docs/adr/0001.
    "pypdf>=5.1.0,<7.0.0",
    "openpyxl>=3.1.5,<4.0.0",
    "striprtf>=0.0.29,<1.0.0",
    # Refuses DTDs and entity declarations at the PARSER, so the guard holds
    # wherever the declaration sits in the document and whatever encoding it
    # uses - which a byte-level check on the first few KB does not.
    "defusedxml>=0.7.1,<1.0.0",
]
```

and add `documents` to the `test` group's install so CI exercises the real
parsers — change the `test` extra to include the same three packages, or install
`.[test,dev,documents]` in the Dockerfile test stage (Step 8 below).

In `Dockerfile`, change line 30 from `pip install --no-cache-dir --prefix=/install .`
to `pip install --no-cache-dir --prefix=/install ".[documents]"`, and line 51
from `pip install --no-cache-dir ".[test,dev]"` to
`pip install --no-cache-dir ".[test,dev,documents]"`.

Install locally: `.venv/Scripts/python.exe -m pip install -e ".[test,dev,documents]"`

- [ ] **Step 2: Write the failing tests**

Create `tests/test_extract.py`:

```python
"""Extraction tests, with the safety controls first.

These parsers see bytes a customer e-mailed in, so the interesting assertions
are the refusals: a DTD is rejected before parsing, a zip bomb before
decompression, and every failure degrades to a described fallback rather than
an empty string that reads like an empty document.
"""

from __future__ import annotations

import io
import zipfile

import pytest

from zammad import extract, media

DOCX_XML_NS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def _docx(document_xml: bytes) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("word/document.xml", document_xml)
    return buf.getvalue()


def _plain_docx(*paragraphs: str) -> bytes:
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    return _docx(f"<w:document {DOCX_XML_NS}><w:body>{body}</w:body></w:document>".encode())


async def test_docx_text_is_extracted_paragraph_by_paragraph() -> None:
    result = await extract.extract(
        _plain_docx("Erste Zeile", "Zweite Zeile"), mime_type=media.DOCX_MIME
    )
    assert result.status == "ok"
    assert result.text == "Erste Zeile\nZweite Zeile"
    assert result.tool == "zipfile+ElementTree"


async def test_docx_with_an_entity_declaration_is_refused() -> None:
    """Measured on CPython 3.14.3: plain ElementTree refuses EXTERNAL entities
    but expands INTERNAL ones, which is the billion-laughs building block.
    defusedxml refuses both (EntitiesForbidden), at the parser rather than by
    inspecting bytes, so the guard holds wherever in the document the
    declaration sits and whatever encoding it uses."""
    hostile = _docx(
        b'<!DOCTYPE w:document [<!ENTITY a "AAAAAAAAAA">]>'
        b"<w:document " + DOCX_XML_NS.encode() + b"><w:body/></w:document>"
    )
    result = await extract.extract(hostile, mime_type=media.DOCX_MIME)
    assert result.status == "failed"
    assert result.text is None
    assert "entit" in (result.reason or "").lower()


async def test_docx_with_a_bare_dtd_is_refused_too() -> None:
    """forbid_dtd=True: OOXML never legitimately carries a DTD, so there is no
    reason to accept one even when it declares no entities."""
    hostile = _docx(
        b"<!DOCTYPE w:document>"
        b"<w:document " + DOCX_XML_NS.encode() + b"><w:body/></w:document>"
    )
    result = await extract.extract(hostile, mime_type=media.DOCX_MIME)
    assert result.status == "failed"
    assert "dtd" in (result.reason or "").lower()


async def test_a_zip_bomb_is_refused_before_decompression() -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("word/document.xml", b"\x00" * (200 * 1024 * 1024))
    result = await extract.extract(buf.getvalue(), mime_type=media.DOCX_MIME)
    assert result.status == "failed"
    assert "compress" in (result.reason or "").lower() or "large" in (result.reason or "").lower()


async def test_an_empty_document_reports_partial_not_success() -> None:
    result = await extract.extract(_plain_docx(), mime_type=media.DOCX_MIME)
    assert result.status == "partial"
    assert result.text == ""
    assert "no text" in (result.reason or "").lower()


async def test_rtf_control_words_are_stripped() -> None:
    rtf = rb"{\rtf1\ansi\deff0 {\fonttbl{\f0 Arial;}}\f0 Technische Daten\par}"
    result = await extract.extract(rtf, mime_type="application/rtf")
    assert result.status == "ok"
    assert "Technische Daten" in (result.text or "")
    assert "\\rtf1" not in (result.text or "")


async def test_xlsx_cells_are_extracted_row_by_row() -> None:
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    sheet = wb.active
    sheet.title = "Werte"
    sheet.append(["Artikel", "Menge"])
    sheet.append(["Pumpe", 3])
    buf = io.BytesIO()
    wb.save(buf)

    result = await extract.extract(buf.getvalue(), mime_type=media.XLSX_MIME)
    assert result.status == "ok"
    assert "Artikel\tMenge" in (result.text or "")
    assert "Pumpe\t3" in (result.text or "")
    assert "Werte" in (result.text or ""), "the sheet name orients the reader"


async def test_pdf_text_is_extracted() -> None:
    pypdf = pytest.importorskip("pypdf")
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    writer.write(buf)
    result = await extract.extract(buf.getvalue(), mime_type="application/pdf")
    # A blank page yields no text - the point is that it degrades honestly.
    assert result.status in {"ok", "partial"}
    assert result.tool == "pypdf"


async def test_a_corrupt_document_fails_with_a_reason_not_an_exception() -> None:
    result = await extract.extract(b"not a zip at all", mime_type=media.DOCX_MIME)
    assert result.status == "failed"
    assert result.reason


async def test_an_unsupported_type_is_not_applicable() -> None:
    result = await extract.extract(b"x", mime_type="application/zip")
    assert result.status == "not_applicable"
    assert result.text is None


async def test_a_missing_library_degrades_instead_of_crashing(monkeypatch) -> None:
    monkeypatch.setattr(extract, "_import_pypdf", lambda: None)
    result = await extract.extract(b"%PDF-1.7\n", mime_type="application/pdf")
    assert result.status == "failed"
    assert "documents" in (result.reason or ""), "the reason must name the missing extra"


async def test_a_slow_parser_is_cut_off_by_the_time_budget(monkeypatch) -> None:
    import time

    def _slow(_data: bytes) -> str:
        time.sleep(2)
        return "never"

    monkeypatch.setattr(extract, "_extract_rtf", _slow)
    result = await extract.extract(rb"{\rtf1 x}", mime_type="application/rtf", timeout=0.1)
    assert result.status == "failed"
    assert "timed out" in (result.reason or "").lower()
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_extract.py -q`
Expected: collection error — `ModuleNotFoundError: No module named 'zammad.extract'`

- [ ] **Step 4: Write the implementation**

Create `src/zammad/extract.py`:

```python
"""Document-to-text extraction for attachments, behind four safety controls.

These parsers read bytes a customer attached to an e-mail, so they are handed
hostile input by default. Four controls apply to every format:

  1. No DTD, no entity declarations. XML is parsed through ``defusedxml`` with
     ``forbid_dtd=True``, so a document declaring either is refused by the
     PARSER. Measured on CPython 3.14.3, plain ``xml.etree.ElementTree`` refuses
     external entities outright but DOES expand internal ones - the
     billion-laughs building block - while defusedxml raises
     ``EntitiesForbidden`` for both. Doing this at the parser rather than by
     scanning the leading bytes matters: a byte-level check misses a declaration
     that sits past its window or arrives UTF-16 encoded. OOXML never
     legitimately carries a DTD, so refusing one costs nothing.
  2. ZIP ratio and absolute cap, checked from the central directory before any
     member is decompressed.
  3. A worker thread. Parsing a 20 MB PDF on the event loop would stall every
     concurrent user of the server.
  4. A wall-clock budget, which is also the practical answer to quadratic
     blowup inputs that survive the checks above.

A control that trips produces ``status='failed'`` with a reason. Nothing here
ever returns a silent empty string: an extractor that finds no text reports
``status='partial'``, because an empty document and a failed parse look
identical to a reader and must not.

DOCX is handled with the standard library rather than python-docx; see
``docs/adr/0001-attachment-decoding-safety.md`` for why.
"""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass
from typing import Any, Callable, Final

import anyio

from . import media

# Wall-clock budget per extraction.
EXTRACT_TIMEOUT_SECONDS: Final = 20.0

# Absolute ceiling on the total uncompressed size of an OOXML archive, and on
# the expansion factor of any single member.
MAX_UNCOMPRESSED_BYTES: Final = 80 * 1024 * 1024
MAX_COMPRESSION_RATIO: Final = 120

# WordprocessingML namespace - the only one we need to find text runs.
_W_NS: Final = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

_MISSING_EXTRA = (
    "install the 'documents' extra to extract text from this format "
    "(pip install 'bg-zammad-mcp[documents]')"
)


class ExtractionRefused(Exception):
    """A safety control rejected the input before parsing."""


@dataclass(frozen=True, slots=True)
class Extraction:
    """What extraction produced, and honestly what it did not."""

    text: str | None
    status: str  # "ok" | "partial" | "failed" | "not_applicable"
    tool: str | None = None
    reason: str | None = None


# ── optional imports, isolated so a missing extra is a reason, not a crash ────


def _import_pypdf() -> Any:
    try:
        import pypdf
    except ImportError:
        return None
    return pypdf


def _import_openpyxl() -> Any:
    try:
        import openpyxl
    except ImportError:
        return None
    return openpyxl


def _import_striprtf() -> Any:
    try:
        from striprtf.striprtf import rtf_to_text
    except ImportError:
        return None
    return rtf_to_text


def _import_defusedxml() -> Any:
    """defusedxml.ElementTree.fromstring, or None when the extra is absent.

    Never fall back to the stdlib parser here. Plain ElementTree expands
    internal entities, so silently degrading would turn the control off in
    exactly the deployment that forgot to install the extra.
    """
    try:
        from defusedxml.ElementTree import fromstring
    except ImportError:
        return None
    return fromstring


# ── control 2: the archive is safe to open ───────────────────────────────────


def _open_ooxml(data: bytes) -> zipfile.ZipFile:
    """Open an OOXML archive after checking it cannot be a decompression bomb."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, OSError, ValueError) as exc:
        raise ExtractionRefused(f"not a readable archive: {exc}") from exc

    total = sum(info.file_size for info in archive.infolist())
    if total > MAX_UNCOMPRESSED_BYTES:
        raise ExtractionRefused(
            f"archive expands to {total} bytes, over the {MAX_UNCOMPRESSED_BYTES} "
            "byte limit - refused before decompression"
        )
    for info in archive.infolist():
        if info.compress_size and info.file_size / info.compress_size > MAX_COMPRESSION_RATIO:
            raise ExtractionRefused(
                f"member {info.filename!r} has a compression ratio of "
                f"{info.file_size // max(info.compress_size, 1)}:1, over the "
                f"{MAX_COMPRESSION_RATIO}:1 limit - refused before decompression"
            )
    return archive


# ── control 1: no DTD ────────────────────────────────────────────────────────


def _parse_xml(raw: bytes) -> Any:
    """Parse OOXML markup with DTDs and entity declarations forbidden.

    The refusal happens in the parser, not by scanning the leading bytes: a
    byte-level check misses a declaration that sits past its window or arrives
    UTF-16 encoded. OOXML never legitimately carries a DTD, so refusing one
    costs nothing and closes the internal-entity-expansion vector that plain
    ElementTree leaves open.
    """
    fromstring = _import_defusedxml()
    if fromstring is None:
        raise ExtractionRefused(_MISSING_EXTRA)
    try:
        return fromstring(raw, forbid_dtd=True)
    except Exception as exc:
        # DTDForbidden / EntitiesForbidden / ExternalReferenceForbidden all
        # derive from defusedxml.common.DefusedXmlException; ParseError does
        # not. Both are refusals as far as a caller is concerned.
        raise ExtractionRefused(f"{type(exc).__name__}: {exc}") from exc


# ── per-format extractors (synchronous; always called in a worker thread) ─────


def _extract_docx(data: bytes) -> str:
    with _open_ooxml(data) as archive:
        try:
            raw = archive.read("word/document.xml")
        except KeyError as exc:
            raise ExtractionRefused("archive has no word/document.xml") from exc
    root = _parse_xml(raw)
    paragraphs: list[str] = []
    for para in root.iter(f"{_W_NS}p"):
        runs = [node.text or "" for node in para.iter(f"{_W_NS}t")]
        paragraphs.append("".join(runs))
    return "\n".join(paragraphs).strip()


def _extract_xlsx(data: bytes) -> str:
    openpyxl = _import_openpyxl()
    if openpyxl is None:
        raise ExtractionRefused(_MISSING_EXTRA)
    _open_ooxml(data).close()  # bomb check before openpyxl touches it
    workbook = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    try:
        blocks: list[str] = []
        for sheet in workbook.worksheets:
            rows = [
                "\t".join("" if cell is None else str(cell) for cell in row)
                for row in sheet.iter_rows(values_only=True)
            ]
            body = "\n".join(row for row in rows if row.strip())
            if body:
                blocks.append(f"# {sheet.title}\n{body}")
        return "\n\n".join(blocks).strip()
    finally:
        workbook.close()


def _extract_pdf(data: bytes) -> str:
    pypdf = _import_pypdf()
    if pypdf is None:
        raise ExtractionRefused(_MISSING_EXTRA)
    reader = pypdf.PdfReader(io.BytesIO(data))
    if reader.is_encrypted:
        raise ExtractionRefused("the PDF is encrypted")
    return "\n\n".join((page.extract_text() or "").strip() for page in reader.pages).strip()


def _extract_rtf(data: bytes) -> str:
    rtf_to_text = _import_striprtf()
    if rtf_to_text is None:
        raise ExtractionRefused(_MISSING_EXTRA)
    text, _charset, _lossy = media.decode_text(data)
    return str(rtf_to_text(text, errors="ignore")).strip()


_EXTRACTORS: Final[dict[str, tuple[Callable[[bytes], str], str]]] = {
    "application/pdf": (_extract_pdf, "pypdf"),
    media.DOCX_MIME: (_extract_docx, "zipfile+ElementTree"),
    media.XLSX_MIME: (_extract_xlsx, "openpyxl"),
    "application/rtf": (_extract_rtf, "striprtf"),
    "text/rtf": (_extract_rtf, "striprtf"),
}


async def extract(
    data: bytes,
    *,
    mime_type: str,
    timeout: float = EXTRACT_TIMEOUT_SECONDS,
) -> Extraction:
    """Turn a document into text, or say precisely why that did not happen."""
    entry = _EXTRACTORS.get(mime_type)
    if entry is None:
        return Extraction(None, "not_applicable")
    _extractor, tool = entry

    def _run() -> str:
        # Re-read from the table so a monkeypatched extractor is honoured.
        return _EXTRACTORS[mime_type][0](data)

    try:
        with anyio.fail_after(timeout):
            text = await anyio.to_thread.run_sync(_run)
    except TimeoutError:
        return Extraction(None, "failed", tool, f"extraction timed out after {timeout:g}s")
    except ExtractionRefused as exc:
        return Extraction(None, "failed", tool, str(exc))
    except Exception as exc:  # a parser meeting input it cannot handle
        return Extraction(None, "failed", tool, f"{type(exc).__name__}: {exc}")

    if not text:
        return Extraction(
            "", "partial", tool, "the document parsed but contained no extractable text"
        )
    return Extraction(text, "ok", tool)


__all__ = [
    "EXTRACT_TIMEOUT_SECONDS",
    "MAX_COMPRESSION_RATIO",
    "MAX_UNCOMPRESSED_BYTES",
    "Extraction",
    "ExtractionRefused",
    "extract",
]
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_extract.py -q`
Expected: PASS, 13 tests.

Two likely adjustments:
- `test_a_missing_library_degrades_instead_of_crashing` monkeypatches
  `_import_pypdf`. If the patch does not take because `_extract_pdf` captured
  the function at import time, confirm `_extract_pdf` calls `_import_pypdf()`
  by name at call time — it does above.
- `test_a_slow_parser_is_cut_off_by_the_time_budget` monkeypatches
  `_extract_rtf`, which is why `_run` re-reads `_EXTRACTORS[mime_type][0]`
  rather than closing over `_extractor`. If the timeout does not fire, verify
  `anyio.fail_after` is imported and that `_EXTRACTORS` was rebuilt — since the
  table holds a direct reference, also `monkeypatch.setitem(extract._EXTRACTORS,
  "application/rtf", (_slow, "striprtf"))` in the test.

Apply whichever form works and keep the assertion identical.

- [ ] **Step 6: Run the full suite and lint**

Run:
```bash
.venv/Scripts/python.exe -m pytest -q -m "not integration"
.venv/Scripts/python.exe -m ruff check src/zammad/extract.py tests/test_extract.py
.venv/Scripts/python.exe -m mypy src/zammad/extract.py
```
Expected: all green.

- [ ] **Step 7: Commit**

```bash
git add src/zammad/extract.py tests/test_extract.py pyproject.toml Dockerfile
git commit -F - <<'EOF'
feat(attachments): added document text extraction

PDF, DOCX, XLSX and RTF are the formats a helpdesk actually
receives, and none of them could be read at all. They now convert to
text behind four controls, because these bytes arrive from customers
by e-mail and are hostile input by default:

* XML is parsed through defusedxml with forbid_dtd=True, so a DTD
  or an entity declaration is refused by the parser. Measured on
  CPython 3.14.3, plain ElementTree refuses external entities but
  expands internal ones; defusedxml refuses both, and doing it in
  the parser rather than by scanning leading bytes means the guard
  survives a declaration that sits past a byte window or arrives
  UTF-16 encoded
* OOXML archives are checked for total expanded size and per-member
  compression ratio from the central directory, before any member is
  decompressed
* parsing runs in a worker thread, so a large PDF cannot stall the
  event loop for every other user
* a wall-clock budget bounds anything that survives the above

Nothing degrades silently: a parse that yields no text reports
'partial', a refusal reports 'failed' with the reason, and a missing
optional dependency names the extra to install.

DOCX uses the standard library rather than python-docx, which pulls
lxml. Reasons in docs/adr/0001 (arriving with the documentation
commit): lxml resolves entities by default, and the production image
is Alpine, where it has no wheel and would compile from source. All
three new dependencies are pure Python.
EOF
```

---

### Task 6: Wire extraction into the read path

**Files:**
- Modify: `src/zammad/tools/attachments.py` (`_build_result` becomes async and consults `extract`)
- Test: `tests/test_tools_attachments.py` (extend)

**Interfaces:**
- Consumes: `zammad.extract.extract`, `zammad.media.Kind.DOCUMENT`.
- Produces: `content_kind == "extracted_text"` and a populated `extraction` block for documents.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_tools_attachments.py`:

```python
async def test_a_docx_comes_back_as_extracted_text() -> None:
    import io
    import zipfile

    ns = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    body = "<w:p><w:r><w:t>Angebot 4711</w:t></w:r></w:p>"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("word/document.xml", f"<w:document {ns}><w:body>{body}</w:body></w:document>")
    docx = buf.getvalue()

    mcp, _ = _build_raw(
        [
            _article(
                attachments=[
                    {"id": 7, "filename": "angebot.docx", "size": str(len(docx)),
                     "preferences": {"Content-Type": "application/octet-stream"}}
                ]
            )
        ],
        [_raw(docx, "application/octet-stream")],
    )
    result = await _call(
        mcp, "download_ticket_attachment", ticket_id=5, article_id=42, attachment_id=7
    )
    sc = result.structured_content
    assert sc["content_kind"] == "extracted_text"
    assert sc["content"] == "Angebot 4711"
    assert sc["extraction"]["status"] == "ok"
    assert sc["extraction"]["tool"] == "zipfile+ElementTree"


async def test_rtf_is_stripped_rather_than_returned_raw() -> None:
    rtf = rb"{\rtf1\ansi\deff0 {\fonttbl{\f0 Arial;}}\f0 Technische Daten\par}"
    mcp, _ = _build_raw(
        [
            _article(
                attachments=[
                    {"id": 7, "filename": "daten.rtf", "size": str(len(rtf)),
                     "preferences": {"Content-Type": "application/msword"}}
                ]
            )
        ],
        [_raw(rtf, "application/msword")],
    )
    result = await _call(
        mcp, "download_ticket_attachment", ticket_id=5, article_id=42, attachment_id=7
    )
    sc = result.structured_content
    assert sc["content_kind"] == "extracted_text"
    assert "Technische Daten" in sc["content"]
    assert "\\rtf1" not in sc["content"]


async def test_a_failed_extraction_of_a_textual_document_falls_back_to_text() -> None:
    """RTF is text underneath. Losing the stripper must not lose the file."""
    from zammad import extract as extract_module

    rtf = rb"{\rtf1\ansi Technische Daten\par}"
    mcp, _ = _build_raw(
        [
            _article(
                attachments=[
                    {"id": 7, "filename": "daten.rtf", "size": str(len(rtf)),
                     "preferences": {"Content-Type": "application/rtf"}}
                ]
            )
        ],
        [_raw(rtf, "application/rtf")],
    )
    original = extract_module._EXTRACTORS["application/rtf"]
    def _boom(_data: bytes) -> str:
        raise extract_module.ExtractionRefused("stripper unavailable")
    extract_module._EXTRACTORS["application/rtf"] = (_boom, "striprtf")
    try:
        result = await _call(
            mcp, "download_ticket_attachment", ticket_id=5, article_id=42, attachment_id=7
        )
    finally:
        extract_module._EXTRACTORS["application/rtf"] = original

    sc = result.structured_content
    assert sc["content_kind"] == "text", "a textual document degrades to text, not to a blob"
    assert "Technische Daten" in sc["content"]
    assert sc["extraction"]["status"] == "failed"


async def test_a_failed_extraction_of_a_binary_document_falls_back_to_a_blob() -> None:
    pdf = b"%PDF-1.7\nbroken"
    mcp, _ = _build_raw(
        [
            _article(
                attachments=[
                    {"id": 7, "filename": "kaputt.pdf", "size": str(len(pdf)),
                     "preferences": {"Content-Type": "application/pdf"}}
                ]
            )
        ],
        [_raw(pdf, "application/pdf")],
    )
    result = await _call(
        mcp, "download_ticket_attachment", ticket_id=5, article_id=42, attachment_id=7
    )
    sc = result.structured_content
    assert sc["content_kind"] == "blob"
    assert sc["extraction"]["status"] == "failed"
    assert sc["extraction"]["reason"]


async def test_mode_raw_skips_extraction_entirely() -> None:
    pdf = b"%PDF-1.7\n"
    mcp, _ = _build_raw(
        [
            _article(
                attachments=[
                    {"id": 7, "filename": "x.pdf", "size": str(len(pdf)),
                     "preferences": {"Content-Type": "application/pdf"}}
                ]
            )
        ],
        [_raw(pdf, "application/pdf")],
    )
    result = await _call(
        mcp, "download_ticket_attachment", ticket_id=5, article_id=42,
        attachment_id=7, mode="raw",
    )
    assert result.structured_content["extraction"]["status"] == "not_applicable"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_tools_attachments.py -q -k "docx or rtf or extraction"`
Expected: FAIL — `content_kind` is `blob`/`text`, `extraction.status` is `not_applicable`.

- [ ] **Step 3: Make `_build_result` async and add the DOCUMENT branch**

In `src/zammad/tools/attachments.py`, add `from .. import extract as extract_module`
to the imports, change `def _build_result(` to `async def _build_result(`, change
the call site to `return await _build_result(`, and insert the DOCUMENT branch
between the TEXT branch and the blob fallback:

```python
    if kind is media.Kind.DOCUMENT:
        result = await extract_module.extract(data, mime_type=detection.mime_type)
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
            # lose the file - the raw text is degraded, not unusable.
            text, used, lossy = media.decode_text(data, charset=charset)
            base["content"] = text
            base["content_kind"] = "text"
            base["decoding"] = {"charset": used, "lossy": lossy}
            return ToolResult(
                content=[TextContent(type="text", text=text)], structured_content=base
            )
        # fall through to the blob branch below
```

The blob branch's summary line gains the reason when there is one:

```python
    reason = base["extraction"].get("reason")
    summary = (
        f"{filename} ({detection.mime_type}, {size} bytes) returned as raw bytes; "
        + (f"text extraction failed: {reason}" if reason
           else "its content could not be turned into text.")
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_tools_attachments.py -q`
Expected: PASS.

- [ ] **Step 5: Run the full suite**

Run: `.venv/Scripts/python.exe -m pytest -q -m "not integration"`
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add src/zammad/tools/attachments.py tests/test_tools_attachments.py
git commit -F - <<'EOF'
feat(attachments): returned documents as extracted text

PDF, DOCX, XLSX and RTF attachments now come back as text instead of
an opaque blob, which is what makes an offer or a data sheet on a
ticket answerable without leaving the conversation.

Two fallbacks, both deliberate. A textual document whose extractor
fails degrades to its raw text rather than to a blob - RTF is prose
under control words, so losing the stripper must not lose the file.
A binary document that fails degrades to a blob carrying the reason,
so the model reports why instead of speculating over an empty
string. mode='raw' skips extraction altogether.
EOF
```

---

### Task 7: Upload helper — three sources, guardrails, denylist

Pure logic plus one upstream read. No tool signature changes yet, so this task
is reviewable on its own.

**Files:**
- Create: `src/zammad/uploads.py`
- Create: `tests/test_uploads.py`

**Interfaces:**
- Consumes: `zammad.media.{detect, mime_for_filename, normalise_mime, DEFAULT_MIME_TYPE}`; `ctx.request` and `ctx.request_raw`.
- Produces:
  - `class CopyRef(BaseModel)` with `ticket_id: int`, `article_id: int`, `attachment_id: int`
  - `class AttachmentInput(BaseModel)` with `filename`, `text`, `data_base64`, `copy_from`, `mime_type`
  - `async def build_attachment_payload(ctx: ToolContext, inputs: list[AttachmentInput] | None) -> list[dict[str, str]] | None`
  - Constants `DENIED_EXTENSIONS`, `DENIED_MAGIC`, `FALLBACK_MAX_UPLOAD_BYTES`, `FALLBACK_MAX_ARTICLE_BYTES`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_uploads.py`:

```python
"""Upload assembly: three sources, one payload, and the refusals in between."""

from __future__ import annotations

import base64
from typing import Any

import pytest
from fastmcp.exceptions import ToolError

from tests.test_tools_inventory import RecordingCtx
from zammad import uploads
from zammad.uploads import AttachmentInput, CopyRef


class Ctx(RecordingCtx):
    def __init__(self, *, responses: list[Any] | None = None,
                 raw_responses: list[Any] | None = None, **limits: Any) -> None:
        super().__init__(responses=responses, raw_responses=raw_responses)
        self.settings = type("S", (), limits)() if limits else None


async def test_text_becomes_base64_with_a_derived_mime_type() -> None:
    payload = await uploads.build_attachment_payload(
        Ctx(), [AttachmentInput(filename="werte.csv", text="a;b\n1;2\n")]
    )
    assert payload == [
        {
            "filename": "werte.csv",
            "data": base64.b64encode(b"a;b\n1;2\n").decode(),
            # The hyphen matters: Zammad ignores a mime_type key without a word.
            "mime-type": "text/csv",
        }
    ]


async def test_the_payload_key_is_mime_type_with_a_hyphen() -> None:
    """A misspelled key is ignored in silence and the file reaches the customer
    as application/octet-stream."""
    payload = await uploads.build_attachment_payload(
        Ctx(), [AttachmentInput(filename="a.txt", text="x")]
    )
    assert "mime-type" in payload[0]
    assert "mime_type" not in payload[0]


async def test_base64_input_is_decoded_and_re_encoded_intact() -> None:
    raw = bytes(range(256))
    payload = await uploads.build_attachment_payload(
        Ctx(),
        [AttachmentInput(filename="x.bin", data_base64=base64.b64encode(raw).decode(),
                         mime_type="application/octet-stream")],
    )
    assert base64.b64decode(payload[0]["data"]) == raw


async def test_invalid_base64_is_refused_with_a_usable_message() -> None:
    with pytest.raises(ToolError, match="not valid base64"):
        await uploads.build_attachment_payload(
            Ctx(), [AttachmentInput(filename="x.bin", data_base64="not!base64!")]
        )


async def test_copy_from_reads_the_source_and_inherits_its_name_and_type() -> None:
    import httpx

    source_article = {
        "id": 91,
        "attachments": [
            {"id": 7, "filename": "datenblatt.pdf", "size": "9",
             "preferences": {"Content-Type": "application/pdf"}}
        ],
    }
    raw = httpx.Response(
        200, content=b"%PDF-1.7\n", headers={"content-type": "application/pdf"},
        request=httpx.Request("GET", "https://z/x"),
    )
    ctx = Ctx(responses=[source_article], raw_responses=[raw])

    payload = await uploads.build_attachment_payload(
        ctx, [AttachmentInput(copy_from=CopyRef(ticket_id=4200, article_id=91, attachment_id=7))]
    )

    assert [(c["method"], c["path"]) for c in ctx.calls] == [
        ("GET", "/ticket_articles/91"),
        ("GET", "/ticket_attachment/4200/91/7"),
    ]
    assert payload[0]["filename"] == "datenblatt.pdf"
    assert payload[0]["mime-type"] == "application/pdf"
    assert base64.b64decode(payload[0]["data"]) == b"%PDF-1.7\n"


async def test_copy_from_can_be_renamed() -> None:
    import httpx

    ctx = Ctx(
        responses=[{"id": 91, "attachments": [{"id": 7, "filename": "a.pdf", "size": "9",
                                               "preferences": {"Content-Type": "application/pdf"}}]}],
        raw_responses=[httpx.Response(200, content=b"%PDF-1.7\n",
                                      headers={"content-type": "application/pdf"},
                                      request=httpx.Request("GET", "https://z/x"))],
    )
    payload = await uploads.build_attachment_payload(
        ctx,
        [AttachmentInput(filename="Datenblatt_Kunde.pdf",
                         copy_from=CopyRef(ticket_id=4200, article_id=91, attachment_id=7))],
    )
    assert payload[0]["filename"] == "Datenblatt_Kunde.pdf"


async def test_copy_from_an_unknown_attachment_names_the_listing_tool() -> None:
    ctx = Ctx(responses=[{"id": 91, "attachments": []}])
    with pytest.raises(ToolError, match="list_ticket_attachments"):
        await uploads.build_attachment_payload(
            ctx, [AttachmentInput(copy_from=CopyRef(ticket_id=4200, article_id=91, attachment_id=7))]
        )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"filename": "a.txt"},
        {"filename": "a.txt", "text": "x", "data_base64": "eA=="},
        {"filename": "a.txt", "text": "x",
         "copy_from": CopyRef(ticket_id=1, article_id=2, attachment_id=3)},
    ],
)
async def test_exactly_one_source_is_required(kwargs: dict[str, Any]) -> None:
    with pytest.raises(Exception, match="exactly one"):
        AttachmentInput(**kwargs)


async def test_a_source_without_a_filename_is_refused() -> None:
    with pytest.raises(Exception, match="filename"):
        AttachmentInput(text="x")


async def test_a_file_over_the_per_file_limit_is_refused() -> None:
    ctx = Ctx(zammad_attachment_max_upload_bytes=10, zammad_attachment_max_article_bytes=100)
    with pytest.raises(ToolError, match="over the 10 byte"):
        await uploads.build_attachment_payload(
            ctx, [AttachmentInput(filename="a.txt", text="x" * 20)]
        )


async def test_the_article_total_is_enforced_across_files() -> None:
    ctx = Ctx(zammad_attachment_max_upload_bytes=100, zammad_attachment_max_article_bytes=30)
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
    import httpx

    ctx = Ctx(
        responses=[{"id": 91, "attachments": [{"id": 7, "filename": "tool.exe", "size": "4",
                                               "preferences": {"Content-Type": "application/octet-stream"}}]}],
        raw_responses=[httpx.Response(200, content=b"MZ\x90\x00",
                                      headers={"content-type": "application/octet-stream"},
                                      request=httpx.Request("GET", "https://z/x"))],
    )
    with pytest.raises(ToolError, match="executable"):
        await uploads.build_attachment_payload(
            ctx, [AttachmentInput(copy_from=CopyRef(ticket_id=1, article_id=91, attachment_id=7))]
        )


async def test_no_attachments_produces_no_payload_key() -> None:
    assert await uploads.build_attachment_payload(Ctx(), None) is None
    assert await uploads.build_attachment_payload(Ctx(), []) is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_uploads.py -q`
Expected: collection error — `ModuleNotFoundError: No module named 'zammad.uploads'`

- [ ] **Step 3: Write the implementation**

Create `src/zammad/uploads.py`:

```python
"""Assembling the ``attachments`` array for POST /ticket_articles.

Zammad has no endpoint that attaches a file to an EXISTING article: attachments
are created only alongside an article. Every write therefore rides on an
article-creating tool, and this module turns whatever the caller supplied into
the one payload Zammad accepts::

    {"filename": "…", "data": "<base64>", "mime-type": "text/csv"}

Note ``mime-type`` with a HYPHEN. Zammad ignores an unrecognised key without
complaining, so the underscore spelling delivers the file to the customer as
application/octet-stream with no error anywhere.

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
FALLBACK_MAX_UPLOAD_BYTES: Final = 10 * 1024 * 1024
FALLBACK_MAX_ARTICLE_BYTES: Final = 25 * 1024 * 1024

# Not virus scanning, and it must never be described as one: a .zip containing
# an .exe passes. It is a tripwire against the obvious accident - an unattended
# agent putting an executable into a customer's inbox under the helpdesk's name.
DENIED_EXTENSIONS: Final = frozenset(
    {
        ".bat", ".cmd", ".com", ".exe", ".hta", ".jar", ".jse", ".js", ".lnk",
        ".msi", ".pif", ".ps1", ".reg", ".scr", ".vbe", ".vbs", ".wsf",
    }
)
DENIED_MAGIC: Final = ((b"MZ", "a Windows executable"), (b"\x7fELF", "a Linux executable"))

# Deliberately asymmetric with the read path, which happily returns a .js file
# as text. Reading what a customer sent is not the risk; re-sending an
# executable under the helpdesk's name is.


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
        Field(default=None, max_length=255,
              description="Name the file will carry. Inherited from the source with copy_from."),
    ] = None
    text: Annotated[
        str | None,
        Field(default=None, description="Literal text content - the server base64-encodes it."),
    ] = None
    data_base64: Annotated[
        str | None,
        Field(default=None, description="Base64-encoded bytes, for binary content."),
    ] = None
    copy_from: Annotated[
        CopyRef | None,
        Field(default=None,
              description="Copy an attachment that already exists in Zammad. Costs no tokens."),
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
    per_file = getattr(settings, "zammad_attachment_max_upload_bytes", None)
    per_article = getattr(settings, "zammad_attachment_max_article_bytes", None)
    return (
        per_file if isinstance(per_file, int) else FALLBACK_MAX_UPLOAD_BYTES,
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
                "whatever the file is called. This server does not attach executables "
                "to tickets."
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
        mime = item.mime_type or media.mime_for_filename(filename)
        if not mime:
            mime = media.detect(data, filename=filename).mime_type
        return filename, data, media.normalise_mime(mime)

    ref = item.copy_from
    assert ref is not None  # guaranteed by the model validator
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
    """Resolve every input, enforce the limits, and return Zammad's payload."""
    if not inputs:
        return None

    per_file, per_article = _limits(ctx)
    payload: list[dict[str, str]] = []
    total = 0

    for item in inputs:
        filename, data, mime = await _resolve(ctx, item)
        if len(data) > per_file:
            raise ToolError(
                f"Attachment {filename!r} is {len(data)} bytes, over the {per_file} byte "
                "per-file limit for uploads."
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
    "FALLBACK_MAX_UPLOAD_BYTES",
    "AttachmentInput",
    "CopyRef",
    "build_attachment_payload",
]
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_uploads.py -q`
Expected: PASS, 17 tests.

If `test_exactly_one_source_is_required` reports `ValidationError` rather than
matching "exactly one", note that pydantic wraps the `ValueError` message —
`pytest.raises(Exception, match="exactly one")` matches the wrapped text, which
is why the test uses `Exception` rather than `ToolError`.

- [ ] **Step 5: Run the full suite and lint**

Run:
```bash
.venv/Scripts/python.exe -m pytest -q -m "not integration"
.venv/Scripts/python.exe -m ruff check src/zammad/uploads.py tests/test_uploads.py
.venv/Scripts/python.exe -m mypy src/zammad/uploads.py
```
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add src/zammad/uploads.py tests/test_uploads.py
git commit -F - <<'EOF'
feat(attachments): added upload assembly with three sources

Nothing in the tool surface could attach a file to a ticket. This
turns literal text, base64 bytes, or a pointer at an attachment that
already exists in Zammad into the single payload the article
endpoint accepts.

copy_from is the notable source: the bytes travel server-side only,
so carrying a data sheet from one ticket to another costs no tokens,
stays byte-identical and is not bounded by the model's context. It
reads the source with the caller's own permissions, exactly as a
download would.

Guardrails: per-file and per-article size limits measured on decoded
bytes, and a refusal for executables by extension and by magic bytes
- applied to copied files too, because a file already sitting in
Zammad is not a reason for an unattended agent to mail it onward.
Explicitly not virus scanning: a .zip containing an .exe passes.

The payload key is mime-type with a hyphen. Zammad ignores the
underscore spelling in silence and the file reaches the customer as
application/octet-stream, so it has its own test.
EOF
```

---

### Task 8: `attachments` parameter on the three write tools

**Files:**
- Modify: `src/zammad/tools/articles.py` (`reply_to_customer`, `add_internal_note`)
- Modify: `src/zammad/tools/tickets.py` (`create_ticket`)
- Create: `src/zammad/tools/_uploads_wiring.py` — the shared `hide_attachments_arg` helper
- Test: `tests/test_tools_articles_attachments.py` (create)

**Interfaces:**
- Consumes: `zammad.uploads.{AttachmentInput, build_attachment_payload}`.
- Produces: `def hide_attachments_arg(mcp: Any, tool_name: str) -> None` in `src/zammad/tools/_uploads_wiring.py`; an `attachments: list[AttachmentInput] | None = None` parameter on the three tools.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_tools_articles_attachments.py`:

```python
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
        self.settings = type("S", (), {
            "zammad_attachment_upload_enabled": uploads_enabled,
            "zammad_attachment_max_upload_bytes": 10 * 1024 * 1024,
            "zammad_attachment_max_article_bytes": 25 * 1024 * 1024,
        })()


async def _tools(mcp: FastMCP) -> dict[str, Any]:
    return {tool.name: tool for tool in await mcp.list_tools(run_middleware=False)}


async def _call(mcp: FastMCP, name: str, /, **kwargs: Any) -> Any:
    return await (await _tools(mcp))[name].run(kwargs)


def _build(module: Any, uploads_enabled: bool = True) -> tuple[FastMCP, Ctx]:
    mcp: FastMCP = FastMCP("test")
    ctx = Ctx(uploads_enabled)
    module.register(mcp, ctx)
    return mcp, ctx


async def test_a_customer_reply_carries_its_file_in_the_same_article() -> None:
    mcp, ctx = _build(articles)
    await _call(
        mcp, "reply_to_customer", ticket_id=4711, body="Anbei die Auswertung.",
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
        mcp, "add_internal_note", ticket_id=4711, body="Log vom Kunden.",
        attachments=[{"filename": "debug.log", "text": "line\n"}],
    )
    payload = ctx.last["json"]
    assert payload["internal"] is True
    assert payload["attachments"][0]["filename"] == "debug.log"


async def test_create_ticket_can_open_with_an_attachment() -> None:
    mcp, ctx = _build(tickets)
    await _call(
        mcp, "create_ticket", title="Angebot", group="Support",
        customer="kunde@example.com", article_body="Anbei.",
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


async def test_disabling_uploads_removes_the_parameter_from_the_schema() -> None:
    """Not a runtime rejection the model discovers by trying."""
    mcp, _ = _build(articles, uploads_enabled=False)
    for name in ("reply_to_customer", "add_internal_note"):
        schema = (await _tools(mcp))[name].parameters or {}
        assert "attachments" not in schema.get("properties", {}), name


async def test_disabling_uploads_keeps_the_tools_working() -> None:
    mcp, ctx = _build(articles, uploads_enabled=False)
    await _call(mcp, "reply_to_customer", ticket_id=4711, body="Antwort.")
    assert ctx.last["json"]["body"] == "Antwort."
    assert "attachments" not in ctx.last["json"]


async def test_the_tools_keep_their_module_tags_after_the_hide_transform() -> None:
    """The transform rebuilds the Tool object; tags must survive it."""
    from server import _MODULE_TAGS, _Tagging

    mcp: FastMCP = FastMCP("test")
    articles.register(_Tagging(mcp, _MODULE_TAGS["articles"]), Ctx(uploads_enabled=False))
    tool = (await _tools(mcp))["reply_to_customer"]
    assert _MODULE_TAGS["articles"] <= set(tool.tags or set())
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_tools_articles_attachments.py -q`
Expected: FAIL — `attachments` is not an accepted parameter.

- [ ] **Step 3: Write the hide helper**

Create `src/zammad/tools/_uploads_wiring.py`:

```python
"""Removing the ``attachments`` parameter when an operator disabled uploads.

Rejecting the argument at call time would publish a capability the server does
not have: the model reads the schema, sends a file, and learns only from the
error. Hiding the argument means the tool never advertises it.

FastMCP's ``ArgTransform(hide=True)`` rebuilds the tool without the parameter
and passes None through to the original function, so the tool body needs no
branch. Verified against FastMCP 3.3.1.
"""

from __future__ import annotations

from typing import Any

from fastmcp.tools import Tool
from fastmcp.tools.tool_transform import ArgTransform


def hide_attachments_arg(mcp: Any, tool_name: str) -> None:
    """Republish ``tool_name`` without its ``attachments`` parameter."""
    provider = getattr(mcp, "local_provider", None)
    original = provider.get_tool(tool_name) if provider else None
    if original is None:
        return
    hidden = Tool.from_tool(
        original,
        name=tool_name,
        transform_args={"attachments": ArgTransform(hide=True)},
    )
    hidden.tags = set(original.tags or set())
    provider.remove_tool(tool_name)
    mcp.add_tool(hidden)


__all__ = ["hide_attachments_arg"]
```

Note: `local_provider.get_tool` may be async in some FastMCP builds. Verify with
`.venv/Scripts/python.exe -c "import inspect, fastmcp; from fastmcp import FastMCP; print(inspect.iscoroutinefunction(FastMCP('x').local_provider.get_tool))"`.
If it prints `True`, make `hide_attachments_arg` async and `await` that call,
and `await` it at the two call sites in Step 4 — `register()` is synchronous, so
in that case do the hiding by looking the tool up from
`provider._tools[tool_name]` synchronously instead, matching whatever attribute
the installed version exposes. Confirm the chosen form with the tag test above.

- [ ] **Step 4: Add the parameter to the two article tools**

In `src/zammad/tools/articles.py`, add the imports:

```python
from ..uploads import AttachmentInput, build_attachment_payload
from ._uploads_wiring import hide_attachments_arg
```

Add to the module docstring, after the "Why two write tools instead of one"
section:

```
Attachments ride along
----------------------
Zammad creates attachments only alongside an article - there is no endpoint
that adds a file to an existing one. The ``attachments`` parameter therefore
sits on these tools rather than in a tool of its own: visibility stays encoded
in the tool NAME, and a reply with a file stays ONE article, so the customer
receives one mail instead of two. ZAMMAD_ATTACHMENT_UPLOAD_ENABLED=false
removes the parameter from the published schema entirely.
```

Add the parameter to `reply_to_customer`, after `content_type`:

```python
        attachments: Annotated[
            list[AttachmentInput] | None,
            Field(
                default=None,
                max_length=10,
                description=(
                    "Files to send with this reply. Each entry needs exactly one "
                    "of: `text` (literal content, cheapest), `data_base64` (raw "
                    "bytes), or `copy_from` (an attachment already in Zammad - "
                    "costs no tokens and stays byte-identical)."
                ),
            ),
        ] = None,
```

and before the `return`:

```python
        attachment_payload = await build_attachment_payload(ctx, attachments)
        if attachment_payload:
            payload["attachments"] = attachment_payload
        return await ctx.request("POST", "/ticket_articles", json=payload)
```

Do the same for `add_internal_note`, with the description
`"Files to attach to this internal note. Each entry needs exactly one of: "
"`text`, `data_base64`, or `copy_from`."`.

At the end of `register()`, before `return 5`:

```python
    if not getattr(getattr(ctx, "settings", None), "zammad_attachment_upload_enabled", True):
        hide_attachments_arg(mcp, "reply_to_customer")
        hide_attachments_arg(mcp, "add_internal_note")

    return 5
```

- [ ] **Step 5: Add the parameter to `create_ticket`**

In `src/zammad/tools/tickets.py`, add the same two imports, add an identical
`attachments` parameter to `create_ticket` (description: `"Files to attach to
the opening article. Each entry needs exactly one of: `text`, `data_base64`, or
`copy_from`."`), attach it to the nested article payload:

```python
        attachment_payload = await build_attachment_payload(ctx, attachments)
        if attachment_payload:
            payload["article"]["attachments"] = attachment_payload
```

placed immediately before `create_ticket`'s existing `return`, and add the hide
call at the end of `register()`:

```python
    if not getattr(getattr(ctx, "settings", None), "zammad_attachment_upload_enabled", True):
        hide_attachments_arg(mcp, "create_ticket")
```

Read `create_ticket`'s existing body first to find the exact name of the
variable holding the article sub-dict — the snippet above assumes
`payload["article"]`.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_tools_articles_attachments.py -q`
Expected: PASS, 7 tests.

- [ ] **Step 7: Run the full suite**

Run: `.venv/Scripts/python.exe -m pytest -q -m "not integration"`
Expected: all green, **including `tests/test_tools_inventory.py`**. Confirm the
tool count is still 75:

```bash
.venv/Scripts/python.exe -m pytest tests/test_tools_inventory.py -q
```

- [ ] **Step 8: Commit**

```bash
git add src/zammad/tools/articles.py src/zammad/tools/tickets.py src/zammad/tools/_uploads_wiring.py tests/test_tools_articles_attachments.py
git commit -F - <<'EOF'
feat(attachments): attached files from the article-creating tools

Zammad creates attachments only alongside an article, so the
parameter belongs on the tools that already create one rather than
in a tool of its own. Two properties follow from that placement:
visibility stays encoded in the tool NAME, so the internal-email
trap articles.py was split in two to close stays closed, and a reply
with a file remains one article - the customer gets one mail, not
two.

ZAMMAD_ATTACHMENT_UPLOAD_ENABLED=false removes the parameter from
the published schema via FastMCP's ArgTransform, rather than
rejecting the call. A capability the server does not have should not
appear in its schema at all.

Tool count is unchanged at 75.
EOF
```

---

### Task 9: Audit the attachment writes

**Files:**
- Modify: `src/audit.py`
- Test: `tests/test_audit_attachments.py` (create)

**Interfaces:**
- Consumes: nothing new.
- Produces: `attachment_count: int` and `attachment_filenames: list[str]` in the audit event for a write carrying attachments.

- [ ] **Step 1: Write the failing test**

Create `tests/test_audit_attachments.py`:

```python
"""What the audit log must say about a file leaving the helpdesk.

audit.py records identifiers and deliberately no content. Without an addition,
a call that mailed a document to a customer is logged identically to one that
did not - and "what did the agent send out" is exactly the question the trail
exists to answer.
"""

from __future__ import annotations

from zammad_audit_helpers import identifiers  # see Step 2


def test_attachment_count_and_names_are_recorded() -> None:
    fields = identifiers(
        {
            "ticket_id": 4711,
            "body": "Anbei die Auswertung.",
            "attachments": [
                {"filename": "auswertung.csv", "text": "a;b"},
                {"filename": "datenblatt.pdf", "copy_from": {"ticket_id": 1,
                                                             "article_id": 2,
                                                             "attachment_id": 3}},
            ],
        }
    )
    assert fields["ticket_id"] == 4711
    assert fields["attachment_count"] == 2
    assert fields["attachment_filenames"] == ["auswertung.csv", "datenblatt.pdf"]


def test_attachment_contents_are_never_recorded() -> None:
    fields = identifiers(
        {"ticket_id": 1, "attachments": [{"filename": "a.csv", "text": "SECRET-PAYLOAD"}]}
    )
    serialised = repr(fields)
    assert "SECRET-PAYLOAD" not in serialised
    assert "body" not in fields


def test_a_write_without_attachments_gains_no_attachment_fields() -> None:
    fields = identifiers({"ticket_id": 1, "body": "nur Text"})
    assert "attachment_count" not in fields
    assert "attachment_filenames" not in fields


def test_a_long_filename_list_is_truncated() -> None:
    fields = identifiers(
        {"ticket_id": 1, "attachments": [{"filename": f"f{i}.txt"} for i in range(30)]}
    )
    assert fields["attachment_count"] == 30
    assert len(fields["attachment_filenames"]) == 10
    assert fields["attachment_filenames_truncated_from"] == 30
```

Replace the import line with the real one once Step 2 fixes the module path:
`from audit import _identifiers as identifiers`.

- [ ] **Step 2: Fix the import and run the test to verify it fails**

Change the test's import to:

```python
from audit import _identifiers as identifiers
```

Run: `.venv/Scripts/python.exe -m pytest tests/test_audit_attachments.py -q`
Expected: FAIL with `KeyError: 'attachment_count'`

- [ ] **Step 3: Extend `_identifiers`**

In `src/audit.py`, extend the module docstring's second paragraph:

```
What is recorded: who (the token subject), what tool, the identifier of the
primary object it touched, and — for a write that carries files — how many and
what they are called. Attachment FILENAMES are a judgement call, because a name
like Kuendigung_Mueller.pdf carries personal information. They are recorded
anyway: "what did the agent send to the customer" is precisely the question
this trail exists to answer, and a count alone cannot answer it. What is
deliberately NOT recorded stays as before: article bodies, customer e-mail
addresses, note text, search queries, and attachment CONTENT.
```

Add the constant and the branch:

```python
# How many filenames to keep before truncating - the count is always exact.
_MAX_LOGGED_FILENAMES = 10


def _attachment_fields(arguments: dict[str, Any]) -> dict[str, Any]:
    """Count and name the files a write carries. Never their content."""
    raw = arguments.get("attachments")
    if not isinstance(raw, list) or not raw:
        return {}
    names: list[str] = []
    for entry in raw:
        if isinstance(entry, dict):
            name = entry.get("filename")
            names.append(name if isinstance(name, str) else "<unnamed>")
        else:
            names.append("<unnamed>")
    out: dict[str, Any] = {
        "attachment_count": len(raw),
        "attachment_filenames": names[:_MAX_LOGGED_FILENAMES],
    }
    if len(names) > _MAX_LOGGED_FILENAMES:
        out["attachment_filenames_truncated_from"] = len(names)
    return out
```

and call it at the end of `_identifiers`, replacing `return out`:

```python
    out.update(_attachment_fields(arguments))
    return out
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_audit_attachments.py -q`
Expected: PASS, 4 tests.

- [ ] **Step 5: Run the full suite and lint**

Run:
```bash
.venv/Scripts/python.exe -m pytest -q -m "not integration"
.venv/Scripts/python.exe -m ruff check src tests
```
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add src/audit.py tests/test_audit_attachments.py
git commit -F - <<'EOF'
docs(audit): recorded which files a write sent

A call that mailed a document to a customer was logged identically
to one that only wrote text, which leaves the audit trail unable to
answer the question it exists for.

Writes carrying files now record the count and the filenames,
truncated after ten with the true count kept. Filenames are a
judgement call - Kuendigung_Mueller.pdf carries personal information
- and are recorded anyway, because a count alone cannot say what
left the helpdesk. File CONTENT stays unlogged, consistent with the
module's existing rule.
EOF
```

- [ ] **Step 7: Correct the commit type**

The commit above is a `feat`, not `docs` — it changes behaviour. Amend before
moving on:

```bash
git commit --amend -m "$(git log -1 --pretty=%B | sed '1s/^docs/feat/')"
git log -1 --pretty=%s
```
Expected: `feat(audit): recorded which files a write sent`

- [ ] **Step 8: Verify nothing sensitive reaches the log**

Run: `.venv/Scripts/python.exe -m pytest tests/test_audit_attachments.py::test_attachment_contents_are_never_recorded -q`
Expected: PASS.

---

### Task 10: Documentation, ADR and release

**Files:**
- Create: `../../docs/adr/0001-attachment-decoding-safety.md`
- Modify: `../../docs/zammad-7.md:49`
- Modify: `../../docs/cookbook.md`
- Modify: `../../docs/tools.md` (regenerated)
- Modify: `../../README.md` (attachment capability line, if one exists)

**Interfaces:**
- Consumes: everything above.
- Produces: no code.

- [ ] **Step 1: Write the ADR**

Create `../../docs/adr/0001-attachment-decoding-safety.md`:

```markdown
# ADR 0001 — Decoding attachment documents safely

Status: accepted
Date: 2026-08-12
Context: `app/bg-zammad-mcp/src/zammad/extract.py`

## Context

Attachment bytes reach this server from customers, usually by e-mail. Turning
them into text means running parsers over untrusted input inside a process that
holds per-user Zammad credentials. The obvious library for DOCX is
`python-docx`.

## Decision

1. **DOCX is extracted with the standard library** — `zipfile` plus
   `xml.etree.ElementTree`, collecting `w:t` nodes from `word/document.xml` —
   rather than with `python-docx`.
2. **PDF, XLSX and RTF use libraries** (`pypdf`, `openpyxl`, `striprtf`), all
   pure Python.
3. **Our own XML parsing goes through `defusedxml`**, not through
   `xml.etree.ElementTree` directly.
4. **Every parser runs behind four controls**, listed below.

## Reasons

**Security surface.** `python-docx` depends on `lxml`, whose default parser runs
with `resolve_entities=True`. A crafted DOCX could then resolve an external
entity against a `file://` target — a local-file-read vector on a process
holding credentials. Measured on this repository's runtime, CPython 3.14.3,
`xml.etree.ElementTree` refuses external entities outright:

```
>>> ET.fromstring('<!DOCTYPE r [<!ENTITY x SYSTEM "file:///etc/hostname">]><r>&x;</r>')
xml.etree.ElementTree.ParseError: undefined entity &x;
```

The same measurement showed ElementTree **does** expand *internal* entities,
which is the billion-laughs building block. Dropping `lxml` therefore closes
the file-read vector but not the denial-of-service one, so our own parsing runs
through `defusedxml` with `forbid_dtd=True` — measured on defusedxml 0.7.1,
both the internal and the external form raise `EntitiesForbidden`, and a bare
DTD raises `DTDForbidden`. Doing this in the parser rather than by scanning the
first few kilobytes matters: a byte-level check misses a declaration placed
past its window or encoded as UTF-16.

**Build.** The production image is `python:3.14-alpine`. musl has no manylinux
wheels, so `lxml` would be compiled from source at image-build time. The three
chosen libraries are pure Python.

**Scope.** We need a document's text, not its object model.

## Controls

1. **No DTD, no entity declarations.** XML is parsed through `defusedxml` with
   `forbid_dtd=True`, so the parser itself refuses both. OOXML never
   legitimately carries a DTD, so this costs nothing. A missing `defusedxml` is
   a refusal, never a fall back to the stdlib parser — degrading silently would
   switch the control off in exactly the deployment that forgot to install the
   extra.
2. **ZIP ratio and absolute cap.** The sum of `ZipInfo.file_size` and each
   member's expansion factor are checked from the central directory before any
   member is decompressed. Nothing is ever extracted to disk.
3. **Worker thread.** Extraction runs under `anyio.to_thread.run_sync`, so a
   large document cannot stall the event loop for other users.
4. **Wall-clock budget.** A hard timeout per extraction, which also bounds
   quadratic-blowup inputs that survive the checks above.

A control that trips yields `extraction.status = "failed"` with a reason. No
control failure is silent.

## Consequences

- Roughly 50 lines of our own DOCX code to maintain.
- Text in text boxes, SmartArt and footnotes may be missed. An extractor that
  finds nothing reports `partial`, not an empty success, so the gap is visible.
- `.doc`, the pre-2007 binary format, is not covered and returns as a blob.
- **Residual risk, stated rather than hidden:** control 1 covers the XML *we*
  parse. `openpyxl` does its own parsing with plain `ElementTree`, so an XLSX
  carrying an internal entity bomb is bounded by controls 2–4 (archive size,
  compression ratio, worker thread, wall-clock budget) rather than refused
  outright. Accepted: hand-rolling XLSX extraction to close it would trade a
  narrow denial-of-service window for a much wider correctness surface —
  shared strings, inline strings and cached formula values.
- This is **not** virus scanning, and the executable denylist on the write path
  is not either: a `.zip` containing an `.exe` passes both.
```

- [ ] **Step 2: Update `docs/zammad-7.md`**

Replace line 49's table row:

```markdown
| Attachments | Read images, text, PDF, Word and Excel; attach files from text, base64 or another ticket. Type is detected from the bytes, so a mislabelled upload still reads correctly. Not virus scanning — see [ADR 0001](adr/0001-attachment-decoding-safety.md). |
```

- [ ] **Step 3: Add two cookbook recipes**

Append to `../../docs/cookbook.md`:

````markdown
## Attach a generated file to a ticket

Ask for the analysis and the delivery in one turn — the file and the message
become a single article, so the customer receives one mail:

> "Werte die Fehlerzahlen aus Ticket 4711 aus und schick dem Kunden die
> Tabelle als CSV mit."

The agent calls `reply_to_customer` once:

```json
{
  "ticket_id": 4711,
  "body": "Anbei die Auswertung der Fehlerzahlen.",
  "attachments": [
    {"filename": "fehlerzahlen.csv", "text": "Datum;Anzahl\n2026-08-01;12\n"}
  ]
}
```

## Carry a file from one ticket to another

`copy_from` moves the bytes server-side, so the file never passes through the
model's context: no token cost, byte-identical, and no size limit beyond the
configured one.

> "Nimm das Datenblatt aus #4711 mit in die Antwort auf #4890."

```json
{
  "ticket_id": 4890,
  "body": "Das Datenblatt aus dem Vorgang 4711, wie besprochen.",
  "attachments": [
    {"copy_from": {"ticket_id": 4711, "article_id": 91, "attachment_id": 7}}
  ]
}
```

Get the `article_id` / `attachment_id` pair from `list_ticket_attachments` on
the source ticket — guessing them returns 403.
````

- [ ] **Step 4: Regenerate `docs/tools.md`**

Run from the repository root:

```bash
python scripts/generate-tools-doc.py
git diff --stat docs/tools.md
```
Expected: `download_ticket_attachment`'s description line changes; no tool is
added or removed.

If the script needs a running server or import path setup, read its docstring
and follow it. If it cannot run, hand-edit the two attachment rows in
`docs/tools.md` to match the new tool descriptions and note in the commit body
that the file was edited by hand.

- [ ] **Step 5: Check the README**

Run: `grep -n -i "attachment\|75 tools" ../../README.md`

Update any line that says attachments are read-only or text-only. Leave the
tool count at 75 — it is unchanged.

- [ ] **Step 6: Run the full suite one final time**

Run:
```bash
.venv/Scripts/python.exe -m pytest -q -m "not integration"
.venv/Scripts/python.exe -m ruff check src tests
.venv/Scripts/python.exe -m mypy src
```
Expected: all green.

- [ ] **Step 7: Commit**

```bash
cd ../..
git add docs/adr/0001-attachment-decoding-safety.md docs/zammad-7.md docs/cookbook.md docs/tools.md README.md
git commit -F - <<'EOF'
docs(attachments): documented the attachment surface and its limits

Records the decision to extract DOCX with the standard library
rather than python-docx, with the measurement it rests on: on
CPython 3.14.3 ElementTree refuses external entities outright but
expands internal ones, so the DTD rejection is what closes the
class. The Alpine build is the second, independent reason - lxml has
no musl wheel.

Also states plainly what this is NOT: neither the document parsers
nor the executable denylist are virus scanning, and a .zip
containing an .exe passes both. A capability nobody claimed cannot
disappoint anyone later.

Adds two cookbook recipes, including copy_from, which is the
non-obvious one - the bytes never enter the model's context.

docs/adr/ is new and is where further decision records go.
EOF
```

- [ ] **Step 8: Push the whole session**

```bash
git log --oneline origin/main..HEAD
git pull origin main
git push
```
Expected: nine commits pushed. If the push is rejected, pull and retry — do not
force.

- [ ] **Step 9: Verify the release pipeline**

Run: `gh run list --limit 3`

The `feat:` commits make this a MINOR release and the changed `Dockerfile`
makes it image-affecting, so semantic-release cuts a version. Watch the run to
green; if the image build fails on the new dependencies, the likely cause is a
non-pure-Python transitive dependency of `pypdf` or `openpyxl` — check the build
log for a `gcc` invocation.

---

## Self-Review

**Spec coverage:**

| Spec section | Task |
| --- | --- |
| Byte-preserving transport | 2 |
| Type detection from bytes | 1 |
| Routing table (image / text / document / blob) | 4, 6 |
| Return shape, additive `structured_content` | 4 |
| Text decoding ladder and `lossy` | 1, 4 |
| `mode` escape hatch | 4 |
| Document extraction, four formats | 5 |
| DOCX decision record | 5 (code), 10 (ADR) |
| Four safety controls | 5 |
| Write path, three sources | 7 |
| `mime-type` hyphen | 7 |
| Guardrail settings | 3 |
| Executable denylist | 7 |
| Audit fields | 9 |
| Error-handling table | 4, 5, 6, 7 |
| Testing table | every task |
| Docs and release | 10 |
| `.env` update (user request) | 3, Step 6 |

No gaps.

**Placeholder scan:** No TBD/TODO. Every code step carries runnable code. The
three "if this fails, do that" notes (Task 4 Step 4, Task 5 Step 5, Task 8
Step 3) name a specific check and a specific alternative rather than deferring
a decision.

**Type consistency:** `Detection` fields (`mime_type`, `declared_mime_type`,
`kind`, `textual`, `source`) are used identically in Tasks 1, 4 and 6.
`Extraction` fields (`text`, `status`, `tool`, `reason`) match between Task 5's
definition and Task 6's consumption. `AttachmentInput` field names match between
Task 7's model and Task 8's tool parameters. `build_attachment_payload` returns
`list[dict[str, str]] | None` in both its definition and its two call sites.
Settings names are spelled identically in Tasks 3, 4, 7 and 8.
