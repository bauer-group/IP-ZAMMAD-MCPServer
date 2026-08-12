"""Extraction tests, with the safety controls first.

These parsers see bytes a customer e-mailed in, so the interesting assertions
are the refusals: a DTD or entity declaration is rejected by the parser, a zip
bomb before decompression, and every failure degrades to a described fallback
rather than an empty string that reads like an empty document.
"""

from __future__ import annotations

import io
import time
import zipfile
from typing import Any

import pytest

from zammad import extract, media

DOCX_XML_NS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'

# Tests that exercise a real parser call pytest.importorskip for the package
# they need, the way the openpyxl and pypdf tests below already do. Without it
# a developer who has not installed the 'documents' extra reads
# "assert 'failed' == 'ok'" and has to work out why. CI installs the extra, so
# there these tests always run - the skip is a courtesy, not a get-out.



def _docx(document_xml: bytes) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("word/document.xml", document_xml)
    return buf.getvalue()


def _plain_docx(*paragraphs: str) -> bytes:
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    return _docx(f"<w:document {DOCX_XML_NS}><w:body>{body}</w:body></w:document>".encode())


# ── the safety controls ──────────────────────────────────────────────────────


async def test_docx_with_an_entity_declaration_is_refused() -> None:
    """Measured on CPython 3.14.3: plain ElementTree refuses EXTERNAL entities
    but expands INTERNAL ones, which is the billion-laughs building block.
    defusedxml refuses both, at the parser rather than by inspecting bytes, so
    the guard holds wherever in the document the declaration sits and whatever
    encoding it uses."""
    pytest.importorskip("defusedxml")
    hostile = _docx(
        b'<!DOCTYPE w:document [<!ENTITY a "AAAAAAAAAA">]>'
        b"<w:document " + DOCX_XML_NS.encode() + b"><w:body/></w:document>"
    )
    result = await extract.extract(hostile, mime_type=media.DOCX_MIME)
    assert result.status == "failed"
    assert result.text is None
    # forbid_dtd fires first, so this input is rejected as DTDForbidden rather
    # than EntitiesForbidden. Both are defusedxml refusals; asserting on the
    # class name would pin an implementation detail of which guard wins.
    assert "forbidden" in (result.reason or "").lower()


async def test_docx_with_a_bare_dtd_is_refused_too() -> None:
    """forbid_dtd=True: OOXML never legitimately carries a DTD, so there is no
    reason to accept one even when it declares no entities."""
    pytest.importorskip("defusedxml")
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
    reason = (result.reason or "").lower()
    assert "compress" in reason or "expands" in reason


async def test_a_slow_parser_is_cut_off_by_the_time_budget(monkeypatch: Any) -> None:
    def _slow(_data: bytes) -> str:
        time.sleep(3)
        return "never"

    monkeypatch.setitem(extract._EXTRACTORS, "application/rtf", (_slow, "striprtf"))
    result = await extract.extract(rb"{\rtf1 x}", mime_type="application/rtf", timeout=0.2)
    assert result.status == "failed"
    assert "timed out" in (result.reason or "").lower()


# ── the extractors ───────────────────────────────────────────────────────────


async def test_docx_text_is_extracted_paragraph_by_paragraph() -> None:
    pytest.importorskip("defusedxml")
    result = await extract.extract(
        _plain_docx("Erste Zeile", "Zweite Zeile"), mime_type=media.DOCX_MIME
    )
    assert result.status == "ok"
    assert result.text == "Erste Zeile\nZweite Zeile"
    assert result.tool == "zipfile+defusedxml"


async def test_rtf_control_words_are_stripped() -> None:
    pytest.importorskip("striprtf")
    rtf = rb"{\rtf1\ansi\deff0 {\fonttbl{\f0 Arial;}}\f0 Technische Daten\par}"
    result = await extract.extract(rtf, mime_type="application/rtf")
    assert result.status == "ok"
    assert "Technische Daten" in (result.text or "")
    assert "\\rtf1" not in (result.text or "")


async def test_xlsx_cells_are_extracted_row_by_row() -> None:
    openpyxl = pytest.importorskip("openpyxl")
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Werte"
    sheet.append(["Artikel", "Menge"])
    sheet.append(["Pumpe", 3])
    buf = io.BytesIO()
    workbook.save(buf)

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


# ── degrading honestly ───────────────────────────────────────────────────────


async def test_an_empty_document_reports_partial_not_success() -> None:
    pytest.importorskip("defusedxml")
    result = await extract.extract(_plain_docx(), mime_type=media.DOCX_MIME)
    assert result.status == "partial"
    assert result.text == ""
    assert "no extractable text" in (result.reason or "").lower()


async def test_a_corrupt_document_fails_with_a_reason_not_an_exception() -> None:
    result = await extract.extract(b"not a zip at all", mime_type=media.DOCX_MIME)
    assert result.status == "failed"
    assert result.reason


async def test_an_unsupported_type_is_not_applicable() -> None:
    result = await extract.extract(b"x", mime_type="application/zip")
    assert result.status == "not_applicable"
    assert result.text is None


async def test_a_missing_library_degrades_instead_of_crashing(monkeypatch: Any) -> None:
    monkeypatch.setattr(extract, "_import_pypdf", lambda: None)
    result = await extract.extract(b"%PDF-1.7\n", mime_type="application/pdf")
    assert result.status == "failed"
    assert "documents" in (result.reason or ""), "the reason must name the missing extra"


async def test_a_missing_xml_parser_never_falls_back_to_the_stdlib(monkeypatch: Any) -> None:
    """Degrading to xml.etree would switch control 1 off in exactly the
    deployment that forgot to install the extra."""
    monkeypatch.setattr(extract, "_import_defusedxml", lambda: None)
    result = await extract.extract(_plain_docx("egal"), mime_type=media.DOCX_MIME)
    assert result.status == "failed"
    assert "documents" in (result.reason or "")
