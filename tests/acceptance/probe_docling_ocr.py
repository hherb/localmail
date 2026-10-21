# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Before/after probe for a docling upgrade: real OCR over three PDFs.

The suite cannot see a docling bump. Only ``test_docling_input_formats.py``
runs real docling, and it asserts refusals and that a native PDF still
converts; nothing compares OCR or table output across versions. This probe runs
a real ``DoclingExtractor`` over a scanned invoice, a scanned table and a
native table, under each OCR engine named, and prints one JSON object. Run it
once in a venv on ``main`` and once on the branch, then diff the two outputs.

    uv run --extra extraction python tests/acceptance/probe_docling_ocr.py \
        --engine auto --engine rapidocr --engine ocrmac

``auto`` should select ocrmac on macOS and rapidocr on Linux, and
``ocr_selection`` records what it actually picked: docling 2.141 skips ocrmac
under ``auto`` when it cannot map the configured language, and logs that only
at INFO. Exits 1 when any cell errored or lacks text its fixture holds, so a
run where OCR never happened cannot pass as a clean comparison.

Kept in the tree because the probes for #397 and #399 lived in a scratchpad
that did not survive the session. It is not a ``run_*.py``: those are
database harnesses and must take the session lock, and this touches no
database.
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import platform
import re
import sys
import tempfile
from collections.abc import Callable
from importlib import metadata
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle

from localmail.config import SearchConfig
from localmail.search import extractor as extractor_mod
from localmail.search.extractor import DoclingExtractor, _build_pdf_pipeline_options

# Large enough that every engine reads it; PIL's bitmap default is ~11 px.
FONT_PX = 28
LINE_PX = 44
MARGIN_PX = 20
TABLE_CELL_W_PX = 220
TABLE_CELL_H_PX = 60
# Rendered at this width on the page; points, not pixels.
SCAN_WIDTH_PT = 450

INVOICE_LINES = (
    "Invoice 4711",
    "Date 2026-10-21",
    "Total 250 EUR",
)
TABLE_ROWS = (
    ("Item", "Qty", "Price"),
    ("Widget", "3", "12.50"),
    ("Gadget", "1", "99.00"),
)
# The fixture builders (pillow, reportlab) are here too: a diff can come from
# the fixture rather than from docling.
PACKAGES = ("docling", "docling-slim", "docling-ibm-models", "docling-parse",
            "docling-core", "rapidocr", "onnxruntime", "ocrmac", "opencv-python",
            "opencv-python-headless", "torch", "pillow", "reportlab")
# docling logs which engine ``auto`` picked, and why it skipped one, here.
OCR_LOGGER = "docling.models.stages.ocr"


def _image_pdf(img: Image.Image) -> bytes:
    """Return a one-page PDF whose only content is ``img`` (no text layer)."""
    png = io.BytesIO()
    img.save(png, format="PNG")
    png.seek(0)
    out = io.BytesIO()
    c = canvas.Canvas(out, pagesize=A4)
    height_pt = SCAN_WIDTH_PT * img.height / img.width
    c.drawImage(ImageReader(png), 72, A4[1] - 72 - height_pt,
                width=SCAN_WIDTH_PT, height=height_pt)
    c.showPage()
    c.save()
    return out.getvalue()


def scanned_invoice_pdf() -> bytes:
    """A rasterised three-line invoice, as a scanner would produce."""
    font = ImageFont.load_default(size=FONT_PX)
    img = Image.new("RGB", (700, 2 * MARGIN_PX + LINE_PX * len(INVOICE_LINES)), "white")
    draw = ImageDraw.Draw(img)
    for i, line in enumerate(INVOICE_LINES):
        draw.text((MARGIN_PX, MARGIN_PX + i * LINE_PX), line, fill="black", font=font)
    return _image_pdf(img)


def scanned_table_pdf() -> bytes:
    """A rasterised ruled 3x3 table — the shape 2.118's region OCR broke."""
    font = ImageFont.load_default(size=FONT_PX)
    cols, rows = len(TABLE_ROWS[0]), len(TABLE_ROWS)
    img = Image.new("RGB", (2 * MARGIN_PX + cols * TABLE_CELL_W_PX,
                            2 * MARGIN_PX + rows * TABLE_CELL_H_PX), "white")
    draw = ImageDraw.Draw(img)
    for r, row in enumerate(TABLE_ROWS):
        for c, cell in enumerate(row):
            x = MARGIN_PX + c * TABLE_CELL_W_PX
            y = MARGIN_PX + r * TABLE_CELL_H_PX
            draw.rectangle((x, y, x + TABLE_CELL_W_PX, y + TABLE_CELL_H_PX),
                           outline="black", width=2)
            draw.text((x + 12, y + 14), cell, fill="black", font=font)
    return _image_pdf(img)


def native_table_pdf() -> bytes:
    """A text-layer table: exercises layout/TableFormer without OCR."""
    out = io.BytesIO()
    table = Table([list(r) for r in TABLE_ROWS])
    table.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 1, "black")]))
    SimpleDocTemplate(out, pagesize=A4).build([table])
    return out.getvalue()


FIXTURES: dict[str, Callable[[], bytes]] = {
    "scanned_invoice": scanned_invoice_pdf,
    "scanned_table": scanned_table_pdf,
    "native_table": native_table_pdf,
}
_TABLE_CELLS = tuple(cell for row in TABLE_ROWS for cell in row)
EXPECTED: dict[str, tuple[str, ...]] = {
    "scanned_invoice": tuple(w for line in INVOICE_LINES for w in line.split()),
    "scanned_table": _TABLE_CELLS,
    "native_table": _TABLE_CELLS,
}


def _version(pkg: str) -> str | None:
    try:
        return metadata.version(pkg)
    except metadata.PackageNotFoundError:
        return None


def _describe(exc: Exception) -> dict[str, str]:
    return {"error": f"{type(exc).__name__}: {exc}"}


def _ocr_options(engine: str) -> object:
    """The OCR options production would hand docling for ``engine``.

    Recorded because an extractor fallback can drop them and still produce
    plausible text (#400); ``None`` means docling's own defaults ran.
    """
    try:
        opts = getattr(_build_pdf_pipeline_options(SearchConfig(extractor_ocr_engine=engine)),
                       "ocr_options", None)
    except Exception as exc:
        return _describe(exc)
    if opts is None:
        return None
    return {"kind": opts.kind, "lang": list(opts.lang), "mode": str(getattr(opts, "mode", None))}


class _Collect(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.INFO)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        line = record.getMessage()
        if line not in self.lines:  # each extract builds a converter, so it repeats per cell
            self.lines.append(line)


def probe(engines: list[str], workdir: Path) -> dict[str, object]:
    """Extract every fixture under every engine; an exception is a result."""
    # First: a metadata error after the slow run would lose every cell.
    report: dict[str, object] = {
        "versions": {p: _version(p) for p in PACKAGES},
        "platform": {"python": platform.python_version(), "system": sys.platform,
                     "machine": platform.machine(), "localmail": extractor_mod.__file__},
        "engines": {engine: _ocr_options(engine) for engine in engines},
    }
    logger = logging.getLogger(OCR_LOGGER)
    collect, old_level = _Collect(), logger.level
    logger.addHandler(collect)
    logger.setLevel(logging.INFO)
    results: dict[str, dict[str, object]] = {}
    try:
        for name, build in FIXTURES.items():
            # Extension-less, like a real content-addressed blob path.
            blob = workdir / name
            blob.write_bytes(build())
            for engine in engines:
                extractor = DoclingExtractor(SearchConfig(extractor_ocr_engine=engine))
                try:
                    text = extractor.extract(blob, "application/pdf", filename=f"{name}.pdf").text
                except Exception as exc:  # the probe reports failures, never hides them
                    results[f"{name}/{engine}"] = _describe(exc)
                    continue
                # Whole tokens: a dropped "1" is still a substring of "12.50".
                words = {w.rstrip(".") for w in re.findall(r"[\w.-]+", text)}
                missing = [t for t in EXPECTED[name] if t not in words]
                results[f"{name}/{engine}"] = {"text": text, "missing": missing}
    finally:
        logger.removeHandler(collect)
        logger.setLevel(old_level)
    report["ocr_selection"] = collect.lines
    report["results"] = results
    report["failures"] = sorted(k for k, r in results.items() if "error" in r or r["missing"])
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--engine", action="append", default=None,
                        help="OCR engine kind (repeatable; default: auto)")
    args = parser.parse_args(argv)
    with tempfile.TemporaryDirectory() as tmp:
        report = probe(args.engine or ["auto"], Path(tmp))
    json.dump(report, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 1 if report["failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
