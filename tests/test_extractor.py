# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

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


def test_iter_exc_chain_yields_self_then_cause() -> None:
    """The chain walk yields the exception, then its __cause__, in order."""
    from localmail.search.extractor import iter_exc_chain

    try:
        try:
            raise ValueError("root")
        except ValueError as inner:
            raise RuntimeError("wrap") from inner
    except RuntimeError as exc:
        chain = list(iter_exc_chain(exc))
    assert [type(e) for e in chain] == [RuntimeError, ValueError]


def test_iter_exc_chain_falls_back_to_context() -> None:
    """When __cause__ is None, the walk follows the implicit __context__."""
    from localmail.search.extractor import iter_exc_chain

    try:
        try:
            raise ValueError("root")
        except ValueError:
            raise RuntimeError("during handling")
    except RuntimeError as exc:
        chain = list(iter_exc_chain(exc))
    assert [type(e) for e in chain] == [RuntimeError, ValueError]


def test_iter_exc_chain_stops_on_suppress_context() -> None:
    """``raise X from None`` sets __suppress_context__ — the walk stops at X."""
    from localmail.search.extractor import iter_exc_chain

    try:
        try:
            raise ValueError("root")
        except ValueError:
            raise RuntimeError("masked") from None
    except RuntimeError as exc:
        chain = list(iter_exc_chain(exc))
    assert [type(e) for e in chain] == [RuntimeError]


# --- Third-party transient classification (#47) ------------------------------
#
# docling pulls models over the network (huggingface_hub) and raises
# third-party exception classes (requests / httpx / urllib3 / huggingface_hub)
# that are NOT in the builtin ConnectionError/TimeoutError hierarchy, so
# extract_worker._is_transient cannot recognise them. DoclingExtractor.extract
# opts these into TransientExtractorError so a model-download blip retries on
# the next sweep instead of poison-pilling a perfectly good PDF.


def _fake_converter_raising(exc_factory):
    """Build a fake docling ``DocumentConverter`` class whose ``convert()``
    raises ``exc_factory()``. Used to drive DoclingExtractor.extract without
    docling installed."""

    class _FakeConverter:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def convert(self, source):
            raise exc_factory()

    return _FakeConverter


def _exc_class(name: str, module: str) -> type[Exception]:
    """Create a fresh Exception subclass whose ``__module__`` mimics a
    third-party package (e.g. ``requests.exceptions``)."""
    cls = type(name, (Exception,), {})
    cls.__module__ = module
    return cls


def test_exc_chain_has_transient_module_detects_top_level_package() -> None:
    """The pure helper matches on the top-level package, ignoring submodules."""
    from localmail.search.extractor import _exc_chain_has_transient_module

    httpx_err = _exc_class("ConnectError", "httpx._exceptions")
    assert _exc_chain_has_transient_module(httpx_err("refused"))


def test_exc_chain_has_transient_module_walks_cause_chain() -> None:
    """A transient third-party class anywhere in the cause chain matches."""
    from localmail.search.extractor import _exc_chain_has_transient_module

    hf_err = _exc_class("HfHubHTTPError", "huggingface_hub.errors")
    try:
        try:
            raise hf_err("502 fetching model")
        except Exception as inner:
            raise RuntimeError("docling pipeline failed") from inner
    except RuntimeError as exc:
        assert _exc_chain_has_transient_module(exc)


def test_exc_chain_has_transient_module_rejects_unknown_module() -> None:
    """A builtin / unknown-module exception is not opted in here (the builtin
    transient classes are extract_worker._is_transient's job)."""
    from localmail.search.extractor import _exc_chain_has_transient_module

    assert not _exc_chain_has_transient_module(ValueError("malformed bytes"))


def test_exc_chain_has_transient_module_respects_suppress_context() -> None:
    """``raise X from None`` stops the walk, matching _is_transient."""
    from localmail.search.extractor import _exc_chain_has_transient_module

    req_err = _exc_class("ConnectionError", "requests.exceptions")
    try:
        try:
            raise req_err("would otherwise be transient")
        except Exception:
            raise RuntimeError("deliberately masked") from None
    except RuntimeError as exc:
        assert not _exc_chain_has_transient_module(exc)


def test_docling_extract_classifies_requests_connection_error_transient(
    monkeypatch, tmp_path
) -> None:
    """A requests.exceptions.ConnectionError from docling.convert (model
    download blip) is raised as TransientExtractorError, not ExtractorError."""
    import localmail.search.extractor as ext_mod
    from localmail.search.extractor import (
        DoclingExtractor,
        TransientExtractorError,
    )

    req_err = _exc_class("ConnectionError", "requests.exceptions")
    monkeypatch.setattr(
        ext_mod,
        "_try_import_docling",
        lambda: _fake_converter_raising(lambda: req_err("max retries exceeded")),
    )
    p = tmp_path / "x.pdf"
    p.write_bytes(b"%PDF-1.4 dummy")

    de = DoclingExtractor()
    with pytest.raises(TransientExtractorError):
        de.extract(p, "application/pdf")


def test_docling_extract_classifies_transient_in_cause_chain(
    monkeypatch, tmp_path
) -> None:
    """A generic docling exception whose __cause__ is a huggingface_hub error
    is still classified transient via the cause-chain walk."""
    import localmail.search.extractor as ext_mod
    from localmail.search.extractor import (
        DoclingExtractor,
        TransientExtractorError,
    )

    hf_err = _exc_class("HfHubHTTPError", "huggingface_hub.errors")

    def _raise_wrapped():
        try:
            raise hf_err("502 fetching model")
        except Exception as inner:
            raise RuntimeError("docling pipeline failed") from inner

    monkeypatch.setattr(
        ext_mod,
        "_try_import_docling",
        lambda: _fake_converter_raising(_raise_wrapped),
    )
    p = tmp_path / "x.pdf"
    p.write_bytes(b"%PDF-1.4 dummy")

    de = DoclingExtractor()
    with pytest.raises(TransientExtractorError):
        de.extract(p, "application/pdf")


def test_docling_extract_keeps_value_error_as_permanent(
    monkeypatch, tmp_path
) -> None:
    """A genuine parse failure (ValueError, no transient module in the chain)
    stays a permanent ExtractorError — NOT TransientExtractorError — so a
    corrupt PDF is poison-pilled after retries."""
    import localmail.search.extractor as ext_mod
    from localmail.search.extractor import (
        DoclingExtractor,
        TransientExtractorError,
    )

    monkeypatch.setattr(
        ext_mod,
        "_try_import_docling",
        lambda: _fake_converter_raising(lambda: ValueError("corrupt PDF")),
    )
    p = tmp_path / "x.pdf"
    p.write_bytes(b"%PDF-1.4 dummy")

    de = DoclingExtractor()
    with pytest.raises(ExtractorError) as excinfo:
        de.extract(p, "application/pdf")
    assert not isinstance(excinfo.value, TransientExtractorError)
