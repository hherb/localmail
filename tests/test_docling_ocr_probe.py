# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""The OCR probe's fixtures are what they claim to be.

A "scanned" fixture with a text layer would let the probe compare two
docling versions without ever running OCR — the one thing a docling bump
changes that the suite cannot see. So the scanned fixtures must read empty
to pypdf, and the native one must not.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import tests.acceptance.probe_docling_ocr as probe_mod
from localmail.search.extractor import ExtractedText, LightweightExtractor
from tests.acceptance.probe_docling_ocr import (
    EXPECTED,
    FIXTURES,
    PACKAGES,
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


def test_the_fixture_set_is_the_three_shapes() -> None:
    assert set(FIXTURES) == set(EXPECTED) == {"scanned_invoice", "scanned_table", "native_table"}


def _run(capsys: pytest.CaptureFixture[str], *engines: str) -> tuple[int, dict]:
    argv = [arg for engine in engines for arg in ("--engine", engine)]
    status = main(argv)
    return status, json.loads(capsys.readouterr().out)


def test_a_failing_engine_is_reported_not_raised(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The probe's output is the evidence; a raise would lose the other cells."""

    def _boom(self, *args, **kwargs):
        raise RuntimeError("engine exploded")

    monkeypatch.setattr(probe_mod.DoclingExtractor, "extract", _boom)
    status, report = _run(capsys, "auto")
    assert status == 1
    assert set(report["results"]) == {f"{name}/auto" for name in FIXTURES}
    assert all(r == {"error": "RuntimeError: engine exploded"}
               for r in report["results"].values())
    assert report["failures"] == sorted(report["results"])


def _echo_engine(self, path, mime, *, filename):
    """Text naming the engine that ran, plus everything the fixture holds."""
    name = filename.removesuffix(".pdf")
    return ExtractedText(text=" ".join((self._cfg.extractor_ocr_engine, *EXPECTED[name])),
                         page_count=1, extractor="fake")


def test_every_engine_runs_every_fixture_under_its_own_name(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(probe_mod.DoclingExtractor, "extract", _echo_engine)
    status, report = _run(capsys, "auto", "rapidocr")
    assert status == 0 and report["failures"] == []
    assert set(report["results"]) == {f"{n}/{e}" for n in FIXTURES for e in ("auto", "rapidocr")}
    for key, cell in report["results"].items():
        assert cell["text"].split()[0] == key.split("/")[1]
        assert cell["missing"] == []
    assert set(report["engines"]) == {"auto", "rapidocr"}


def test_text_the_fixture_holds_but_ocr_dropped_is_a_failure(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run that never OCR'd reads "" on both sides of a diff; it must not pass."""

    def _drops_qty(self, path, mime, *, filename):
        # "1": a substring of "12.50", so only a whole-token check sees it go.
        text = _echo_engine(self, path, mime, filename=filename).text
        return ExtractedText(text=text.replace(" 1 ", " ") if "table" in filename else text,
                             page_count=1, extractor="fake")

    monkeypatch.setattr(probe_mod.DoclingExtractor, "extract", _drops_qty)
    status, report = _run(capsys, "auto")
    assert status == 1
    assert report["failures"] == ["native_table/auto", "scanned_table/auto"]
    assert report["results"]["scanned_table/auto"]["missing"] == ["1"]


def test_versions_are_read_for_every_package(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The versions block is what says which venv produced an output."""
    monkeypatch.setattr(probe_mod.DoclingExtractor, "extract", _echo_engine)
    monkeypatch.setattr(probe_mod.metadata, "version", lambda pkg: f"v-{pkg}")
    _, report = _run(capsys, "auto")
    assert report["versions"] == {p: f"v-{p}" for p in PACKAGES}
    assert report["platform"]["localmail"] == probe_mod.extractor_mod.__file__
