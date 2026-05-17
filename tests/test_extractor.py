"""Tests for the extractor protocol and LightweightExtractor."""

from __future__ import annotations

import dataclasses
import io
import logging
from pathlib import Path

import pytest

from localmail.search.extractor import (
    AttachmentExtractor,
    ExtractedText,
    ExtractorError,
    LightweightExtractor,
)


def test_extracted_text_is_frozen_dataclass() -> None:
    et = ExtractedText(text="hello", page_count=1, extractor="x@1")
    with pytest.raises(dataclasses.FrozenInstanceError):
        et.text = "world"  # type: ignore[misc]


def test_lightweight_supports_pdf_mime_and_ext() -> None:
    lw = LightweightExtractor()
    assert lw.supports("application/pdf", "foo.pdf")
    assert lw.supports(None, "foo.pdf")
    assert lw.supports("application/pdf", "")
    assert not lw.supports("video/mp4", "foo.mp4")
    assert not lw.supports(None, None)


def test_lightweight_does_not_support_image() -> None:
    lw = LightweightExtractor()
    assert not lw.supports("image/png", "logo.png")


def test_extractor_protocol_runtime_checkable() -> None:
    """AttachmentExtractor is @runtime_checkable so workers can verify
    a configured class implements the interface."""
    lw = LightweightExtractor()
    assert isinstance(lw, AttachmentExtractor)


def _build_native_pdf(text: str) -> bytes:
    """Build a single-page text PDF in memory using reportlab."""
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.drawString(72, 720, text)
    c.showPage()
    c.save()
    return buf.getvalue()


def test_lightweight_extracts_native_pdf(tmp_path) -> None:
    pdf_bytes = _build_native_pdf("hello attachment world")
    p = tmp_path / "a.pdf"
    p.write_bytes(pdf_bytes)

    lw = LightweightExtractor()
    result = lw.extract(p, "application/pdf")

    assert "hello attachment world" in result.text
    assert result.extractor == "lightweight@1.0"
    assert result.page_count == 1


def test_lightweight_returns_empty_on_scanned_pdf(tmp_path) -> None:
    """A PDF whose only content is a rasterized image of text returns ''
    from pypdf — the docling fallback exists for this case."""
    from PIL import Image, ImageDraw
    from reportlab.pdfgen import canvas
    from reportlab.lib.utils import ImageReader

    img = Image.new("RGB", (400, 80), "white")
    ImageDraw.Draw(img).text((10, 30), "scanned text content", fill="black")
    img_buf = io.BytesIO()
    img.save(img_buf, format="PNG")
    img_buf.seek(0)

    pdf_buf = io.BytesIO()
    c = canvas.Canvas(pdf_buf)
    c.drawImage(ImageReader(img_buf), 72, 600, width=400, height=80)
    c.showPage()
    c.save()

    p = tmp_path / "scan.pdf"
    p.write_bytes(pdf_buf.getvalue())

    lw = LightweightExtractor()
    result = lw.extract(p, "application/pdf")

    assert result.text == ""


def test_lightweight_raises_on_encrypted_pdf(tmp_path) -> None:
    import pikepdf
    pdf_bytes = _build_native_pdf("secret")
    src = tmp_path / "src.pdf"
    src.write_bytes(pdf_bytes)

    enc = tmp_path / "enc.pdf"
    with pikepdf.open(src) as p:
        p.save(enc, encryption=pikepdf.Encryption(owner="o", user="u", R=4))

    lw = LightweightExtractor()
    with pytest.raises(ExtractorError):
        lw.extract(enc, "application/pdf")


def test_lightweight_extracts_docx(tmp_path) -> None:
    import docx
    p = tmp_path / "a.docx"
    d = docx.Document()
    d.add_paragraph("docx paragraph one")
    d.add_paragraph("docx paragraph two")
    d.save(str(p))

    lw = LightweightExtractor()
    result = lw.extract(
        p,
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
    assert "docx paragraph one" in result.text
    assert "docx paragraph two" in result.text
    assert result.extractor == "lightweight@1.0"


def test_lightweight_extracts_xlsx(tmp_path) -> None:
    import openpyxl
    p = tmp_path / "a.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    ws["A1"] = "row one cell A"
    ws["B1"] = "row one cell B"
    ws2 = wb.create_sheet(title="Sheet2")
    ws2["A1"] = "second sheet content"
    wb.save(str(p))

    lw = LightweightExtractor()
    result = lw.extract(
        p,
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    assert "row one cell A" in result.text
    assert "row one cell B" in result.text
    assert "second sheet content" in result.text
    assert result.extractor == "lightweight@1.0"


def test_lightweight_extracts_pptx(tmp_path) -> None:
    from pptx import Presentation
    p = tmp_path / "a.pptx"
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[5])  # title only
    slide.shapes.title.text = "Slide title here"
    notes = slide.notes_slide.notes_text_frame
    notes.text = "speaker note content"
    prs.save(str(p))

    lw = LightweightExtractor()
    result = lw.extract(
        p,
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    )
    assert "Slide title here" in result.text
    assert "speaker note content" in result.text


def test_lightweight_extracts_txt_utf8(tmp_path) -> None:
    p = tmp_path / "a.txt"
    p.write_text("plain text content über alles", encoding="utf-8")

    lw = LightweightExtractor()
    result = lw.extract(p, "text/plain")
    assert "plain text content über alles" == result.text


def test_lightweight_extracts_txt_latin1(tmp_path) -> None:
    p = tmp_path / "a.txt"
    p.write_bytes("naïve résumé".encode("latin-1"))

    lw = LightweightExtractor()
    result = lw.extract(p, "text/plain")
    assert "naïve résumé" == result.text


def test_lightweight_extracts_md(tmp_path) -> None:
    p = tmp_path / "a.md"
    p.write_text("# Header\n\nbody text", encoding="utf-8")

    lw = LightweightExtractor()
    result = lw.extract(p, "text/markdown")
    assert "# Header" in result.text
    assert "body text" in result.text


def test_lightweight_extracts_html(tmp_path) -> None:
    p = tmp_path / "a.html"
    p.write_text(
        "<html><body><h1>title</h1><p>paragraph</p></body></html>",
        encoding="utf-8",
    )

    lw = LightweightExtractor()
    result = lw.extract(p, "text/html")
    # html2text emits markdown-ish; both strings should be present.
    assert "title" in result.text
    assert "paragraph" in result.text


def test_lightweight_extracts_csv(tmp_path) -> None:
    p = tmp_path / "a.csv"
    p.write_text("name,city\nalice,Berlin\nbob,Madrid\n", encoding="utf-8")

    lw = LightweightExtractor()
    result = lw.extract(p, "text/csv")
    assert "alice" in result.text
    assert "Berlin" in result.text


def test_lightweight_extracts_rtf(tmp_path) -> None:
    p = tmp_path / "a.rtf"
    p.write_text(
        r"{\rtf1\ansi\deff0 {\fonttbl {\f0 Helvetica;}}"
        r"\f0\fs24 RTF body content here.\par}",
        encoding="ascii",
    )

    lw = LightweightExtractor()
    result = lw.extract(p, "application/rtf")
    assert "RTF body content here" in result.text


def test_lightweight_extracts_odt(tmp_path: Path) -> None:
    from odf.opendocument import OpenDocumentText
    from odf.text import P
    p = tmp_path / "a.odt"
    doc = OpenDocumentText()
    doc.text.addElement(P(text="odt paragraph one"))
    doc.text.addElement(P(text="odt paragraph two"))
    doc.save(str(p))

    lw = LightweightExtractor()
    result = lw.extract(p, "application/vnd.oasis.opendocument.text")
    assert "odt paragraph one" in result.text
    assert "odt paragraph two" in result.text


def test_lightweight_extracts_ics(tmp_path: Path) -> None:
    import datetime as dt
    from icalendar import Calendar, Event
    cal = Calendar()
    cal.add("prodid", "-//Test//Test//EN")
    cal.add("version", "2.0")
    ev = Event()
    ev.add("summary", "Annual review")
    ev.add("description", "Discuss quarterly bonus criteria")
    ev.add("location", "Conf room Berlin")
    ev.add("dtstart", dt.datetime(2026, 6, 1, 14, 0, tzinfo=dt.timezone.utc))
    cal.add_component(ev)

    p = tmp_path / "a.ics"
    p.write_bytes(cal.to_ical())

    lw = LightweightExtractor()
    result = lw.extract(p, "text/calendar")
    assert "Annual review" in result.text
    assert "quarterly bonus criteria" in result.text
    assert "Conf room Berlin" in result.text
    assert result.page_count == 1
    assert result.extractor == "lightweight@1.0"


def test_docling_import_warning_one_shot(caplog, monkeypatch) -> None:
    """When docling is missing, warn_docling_missing() emits exactly one
    WARN per process, even when called multiple times."""
    import localmail.search.extractor as ext_mod

    monkeypatch.setattr(ext_mod, "_DOCLING_WARNED", False, raising=False)
    monkeypatch.setattr(ext_mod, "_try_import_docling", lambda: None)

    with caplog.at_level(logging.WARNING, logger="localmail.search.extractor"):
        ext_mod.warn_docling_missing()
        ext_mod.warn_docling_missing()
        ext_mod.warn_docling_missing()

    warn_messages = [
        r for r in caplog.records
        if r.levelno == logging.WARNING
        and "extraction" in r.getMessage().lower()
    ]
    assert len(warn_messages) == 1


def test_docling_extractor_supports_pdf_only() -> None:
    """DoclingExtractor.supports() matches PDFs by MIME or extension only."""
    from localmail.search.extractor import DoclingExtractor
    de = DoclingExtractor()
    assert de.supports("application/pdf", "x.pdf")
    assert de.supports(None, "x.pdf")
    assert not de.supports("text/plain", "x.txt")
    assert not de.supports("image/png", "x.png")


def test_docling_extractor_raises_when_missing(monkeypatch, tmp_path) -> None:
    """If docling is not importable, .extract() raises ExtractorError."""
    import localmail.search.extractor as ext_mod
    from localmail.search.extractor import DoclingExtractor

    monkeypatch.setattr(ext_mod, "_try_import_docling", lambda: None)
    p = tmp_path / "x.pdf"
    p.write_bytes(b"%PDF-1.4 dummy")

    de = DoclingExtractor()
    with pytest.raises(ExtractorError):
        de.extract(p, "application/pdf")


def test_docling_extractor_accepts_config() -> None:
    """DoclingExtractor.__init__ accepts a SearchConfig; defaults to
    SearchConfig() when omitted. The config is stored on the instance
    for the pipeline-options builder."""
    from localmail.config import SearchConfig
    from localmail.search.extractor import DoclingExtractor

    de = DoclingExtractor()
    assert de._cfg.extractor_docling_max_pages == 200
    assert de._cfg.extractor_ocr_languages == ["en"]

    custom = SearchConfig(
        extractor_docling_max_pages=50,
        extractor_ocr_languages=["en", "de", "ja"],
    )
    de2 = DoclingExtractor(custom)
    assert de2._cfg.extractor_docling_max_pages == 50
    assert de2._cfg.extractor_ocr_languages == ["en", "de", "ja"]
