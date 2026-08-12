"""Document-to-text extraction for attachments, behind four safety controls.

These parsers read bytes a customer attached to an e-mail, so they are handed
hostile input by default. Four controls apply to every format:

  1. No DTD, no entity declarations. XML is parsed through ``defusedxml`` with
     ``forbid_dtd=True``, so a document declaring either is refused by the
     PARSER. Measured on CPython 3.14.3, plain ``xml.etree.ElementTree``
     refuses external entities outright but DOES expand internal ones - the
     billion-laughs building block - while defusedxml raises
     ``EntitiesForbidden`` for both. Doing this at the parser rather than by
     scanning the leading bytes matters: a byte-level check misses a
     declaration that sits past its window or arrives UTF-16 encoded. OOXML
     never legitimately carries a DTD, so refusing one costs nothing.
  2. ZIP ratio and absolute cap, checked from the central directory before any
     member is decompressed.
  3. A worker thread. Parsing a 20 MB PDF on the event loop would stall every
     concurrent user of the server.
  4. A wall-clock budget, so a pathological document cannot hold a request
     open indefinitely.

     Note what control 4 does and does not do. ``anyio.to_thread.run_sync`` is
     NOT cancellable unless ``abandon_on_cancel=True`` is passed: without it
     the deadline passes, the await keeps waiting for the thread, the call
     eventually succeeds and the timeout never fires at all - measured, a 2 s
     parse sailed through a 0.2 s budget without raising. With it the timeout
     is real, but the abandoned thread keeps running to completion in the
     background. The budget therefore bounds the REQUEST, not the CPU. What
     bounds the CPU is control 2, which is why the size and ratio caps are the
     primary defence and this is the backstop.

A control that trips produces ``status='failed'`` with a reason. Nothing here
ever returns a silent empty string: an extractor that finds no text reports
``status='partial'``, because an empty document and a failed parse look
identical to a reader and must not.

DOCX is handled with the standard library rather than python-docx; see
``docs/adr/0001-attachment-decoding-safety.md`` for why.

Known limit, stated rather than hidden: control 1 covers the XML *we* parse.
openpyxl does its own parsing with plain ElementTree, so an XLSX carrying an
entity bomb is bounded by controls 2-4 rather than refused outright.
"""

from __future__ import annotations

import io
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final

import anyio
import anyio.to_thread

from . import media

# Wall-clock budget per extraction.
EXTRACT_TIMEOUT_SECONDS: Final = 20.0

# Absolute ceiling on the total uncompressed size of an OOXML archive, and on
# the expansion factor of any single member.
MAX_UNCOMPRESSED_BYTES: Final = 80 * 1024 * 1024
MAX_COMPRESSION_RATIO: Final = 120

# WordprocessingML namespace - the only one needed to find text runs.
_W_NS: Final = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

_MISSING_EXTRA: Final = (
    "the 'documents' extra is not installed, so this format cannot be turned "
    "into text (pip install 'bg-zammad-mcp[documents]')"
)


class ExtractionRefused(Exception):
    """A safety control rejected the input before or during parsing."""


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
    """``defusedxml.ElementTree.fromstring``, or None when the extra is absent.

    Never fall back to the stdlib parser here. Plain ElementTree expands
    internal entities, so silently degrading would switch control 1 off in
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


# ── control 1: no DTD, no entity declarations ────────────────────────────────


def _parse_xml(raw: bytes) -> Any:
    """Parse OOXML markup with DTDs and entity declarations forbidden."""
    fromstring = _import_defusedxml()
    if fromstring is None:
        raise ExtractionRefused(_MISSING_EXTRA)
    try:
        return fromstring(raw, forbid_dtd=True)
    except Exception as exc:
        # DTDForbidden / EntitiesForbidden / ExternalReferenceForbidden derive
        # from defusedxml.common.DefusedXmlException; ParseError does not. Both
        # are refusals as far as a caller is concerned.
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
    media.DOCX_MIME: (_extract_docx, "zipfile+defusedxml"),
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
    tool = entry[1]

    def _run() -> str:
        # Read the table again rather than closing over the extractor, so a
        # test (or a future plugin) that swaps one in is actually honoured.
        return _EXTRACTORS[mime_type][0](data)

    try:
        with anyio.fail_after(timeout):
            # abandon_on_cancel=True is what makes the budget real - see the
            # module docstring. Without it the timeout silently does nothing.
            text = await anyio.to_thread.run_sync(_run, abandon_on_cancel=True)
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
