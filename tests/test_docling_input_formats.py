# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""DoclingExtractor converts PDFs and nothing else.

docling sniffs an extension-less path's bytes to pick a backend, and its
``DocumentConverter`` admits every format unless told otherwise. A blob's
MIME type and filename are sender-supplied, so an HTML or ODT file labelled
``invoice.pdf`` fails pypdf, falls back to docling, and used to reach the
HTML/ODF backends — the ones carrying Dependabot #116 and #118–#131.
"""

from __future__ import annotations

import io
import sys
import types
import zipfile
from enum import Enum
from pathlib import Path

import pytest

import localmail.search.extractor as ext_mod
from localmail.search.extractor import (
    DoclingExtractor,
    ExtractorConfigurationError,
    ExtractorError,
    TransientExtractorError,
)

_HTML = (
    b"<html><body><table><tr><td>cell</td></tr></table>"
    b"<p>hello from html</p></body></html>"
)


def _odt_bytes() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("mimetype", "application/vnd.oasis.opendocument.text")
        z.writestr(
            "content.xml",
            '<?xml version="1.0"?><office:document-content '
            'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
            'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0">'
            "<office:body><office:text><text:p>hello from odt</text:p>"
            "</office:text></office:body></office:document-content>",
        )
    return buf.getvalue()


@pytest.mark.parametrize(
    "payload", [_HTML, _odt_bytes()], ids=["html", "odt"]
)
def test_non_pdf_bytes_labelled_pdf_are_refused_as_a_bad_blob(
    payload: bytes, tmp_path: Path
) -> None:
    """End to end against real docling. The refusal must be a plain
    ExtractorError (a poison pill): a configuration or transient
    classification would retry a hostile file instead of giving up on it."""
    pytest.importorskip("docling")
    blob = tmp_path / ("ab" * 32)  # content-addressed: no extension
    blob.write_bytes(payload)

    with pytest.raises(ExtractorError) as excinfo:
        DoclingExtractor().extract(blob, "application/pdf", filename="invoice.pdf")
    assert not isinstance(
        excinfo.value, (ExtractorConfigurationError, TransientExtractorError)
    )


def test_a_real_pdf_still_converts(tmp_path: Path) -> None:
    """Positive control: the restriction must not turn away the format it
    admits, or every refusal above passes for a converter refusing all."""
    pytest.importorskip("docling")
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.drawString(72, 720, "Invoice 4711 total 250 EUR")
    c.showPage()
    c.save()
    blob = tmp_path / ("cd" * 32)
    blob.write_bytes(buf.getvalue())

    result = DoclingExtractor().extract(blob, "application/pdf", filename="a.pdf")
    assert "Invoice 4711" in result.text


class _FakeInputFormat(Enum):
    PDF = "pdf"
    HTML = "html"


def _install_fake_docling(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``docling.datamodel.base_models`` / ``docling.document_converter``
    importable without docling, so the wiring is pinned in CI too."""
    base_models = types.ModuleType("docling.datamodel.base_models")
    base_models.InputFormat = _FakeInputFormat  # type: ignore[attr-defined]
    converter_mod = types.ModuleType("docling.document_converter")
    converter_mod.PdfFormatOption = lambda **kw: kw  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "docling", types.ModuleType("docling"))
    monkeypatch.setitem(
        sys.modules, "docling.datamodel", types.ModuleType("docling.datamodel")
    )
    monkeypatch.setitem(sys.modules, "docling.datamodel.base_models", base_models)
    monkeypatch.setitem(sys.modules, "docling.document_converter", converter_mod)


@pytest.mark.parametrize("with_pipeline_options", [True, False])
def test_every_converter_is_built_pdf_only(
    with_pipeline_options: bool, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Both construction paths — with pipeline options, and the bare
    fallback — restrict the converter to PDF."""
    _install_fake_docling(monkeypatch)
    built: list[dict[str, object]] = []

    class _Doc:
        pages = [object()]

        def export_to_markdown(self) -> str:
            return "text"

    class _Result:
        document = _Doc()

    class _Converter:
        def __init__(self, **kwargs: object) -> None:
            built.append(kwargs)

        def convert(self, source: str, **kwargs: object) -> _Result:
            return _Result()

    monkeypatch.setattr(ext_mod, "_try_import_docling", lambda: _Converter)
    monkeypatch.setattr(
        ext_mod,
        "_build_pdf_pipeline_options",
        lambda cfg: object() if with_pipeline_options else None,
    )
    blob = tmp_path / "x"
    blob.write_bytes(b"%PDF-1.4 dummy")

    DoclingExtractor().extract(blob, "application/pdf")

    assert len(built) == 1
    assert built[0]["allowed_formats"] == [_FakeInputFormat.PDF]
    assert ("format_options" in built[0]) is with_pipeline_options
