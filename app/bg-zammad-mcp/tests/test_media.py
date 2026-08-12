"""Type-detection tests — bytes decide, the upload label does not.

The named regression at the bottom is the case that triggered this work: a
customer RTF uploaded as application/msword, refused as binary while being
plain text.
"""

from __future__ import annotations

import io
import zipfile

import pytest

from zammad import media


def _ooxml(member: bytes) -> bytes:
    """A minimal real ZIP containing one named member."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(member.decode(), "x")
    return buf.getvalue()


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
    data: bytes,
    charset: str | None,
    expected_text: str,
    expected_charset: str,
    expected_lossy: bool,
) -> None:
    text, used, lossy = media.decode_text(data, charset=charset)
    assert text == expected_text
    assert used == expected_charset
    assert lossy is expected_lossy


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        # Windows answers .csv with application/vnd.ms-excel and .rtf with
        # application/msword from the registry. The container answers neither.
        ("werte.csv", "text/csv"),
        ("bericht.rtf", "application/rtf"),
        ("server.log", "text/plain"),
        ("SCHREIEN.TXT", "text/plain"),
    ],
)
def test_the_extension_table_does_not_depend_on_the_host(
    filename: str, expected: str
) -> None:
    assert media.mime_for_filename(filename) == expected


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
    d = media.detect(
        data, filename="Technische-Daten-Liquid-Liquid.rtf", declared="application/msword"
    )
    assert d.mime_type == "application/rtf"
    assert d.declared_mime_type == "application/msword"
    assert d.kind is media.Kind.DOCUMENT
    assert d.textual is True, "RTF must never be refused as binary"
    assert d.source == "magic"
