# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""The OCR probe's fixtures are what they claim to be.

A "scanned" fixture with a text layer would let the probe compare two
docling versions without ever running OCR — the one thing a docling bump
changes that the suite cannot see. So the scanned fixtures must read empty
to pypdf, and the native one must not.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from localmail.search.extractor import LightweightExtractor
from tests.acceptance.probe_docling_ocr import (
    FIXTURES,
    TABLE_ROWS,
    main,
    native_table_pdf,
    scanned_invoice_pdf,
    scanned_table_pdf,
)


def _lightweight_text(tmp_path: Path, pdf: bytes) -> str:
    blob = tmp_path / ("ab" * 32)
    blob.write_bytes(pdf)
    return LightweightExtractor().extract(blob, "application/pdf", filename="x.pdf").text


@pytest.mark.parametrize("build", [scanned_invoice_pdf, scanned_table_pdf])
def test_a_scanned_fixture_has_no_text_layer(tmp_path: Path, build) -> None:
    assert _lightweight_text(tmp_path, build()) == ""


def test_the_native_table_has_a_text_layer(tmp_path: Path) -> None:
    text = _lightweight_text(tmp_path, native_table_pdf())
    assert all(cell in text for row in TABLE_ROWS for cell in row)


def test_every_fixture_is_probed() -> None:
    assert set(FIXTURES) == {"scanned_invoice", "scanned_table", "native_table"}


def test_a_failing_engine_is_reported_not_raised(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The probe's output is the evidence; a raise would lose the other cells."""
    import json

    import tests.acceptance.probe_docling_ocr as probe_mod

    def _boom(self, *args, **kwargs):
        raise RuntimeError("engine exploded")

    monkeypatch.setattr(probe_mod.DoclingExtractor, "extract", _boom)
    assert main(["--engine", "auto"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert set(report["results"]) == {f"{name}/auto" for name in FIXTURES}
    assert all(r == {"error": "RuntimeError: engine exploded"}
               for r in report["results"].values())
