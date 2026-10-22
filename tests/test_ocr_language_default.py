# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""The default OCR language reaches each engine in a form it accepts (#398).

docling >= 2.132 passes a bare language value to the engine untouched and maps
only an ``iso:`` tag. With the old default ``"en"``, 2.141's ``auto`` skipped
ocrmac on the Mac for rapidocr (logged at INFO only) and an explicit ocrmac
failed every pass. Real docling, so these skip without the ``[extraction]``
extra or the engine.
"""

from __future__ import annotations

import sys

import pytest

from localmail.config import SearchConfig
from localmail.search.extractor import _build_pdf_pipeline_options

pytest.importorskip("docling")
from docling.datamodel.accelerator_options import AcceleratorOptions  # noqa: E402


def _default_options(engine: str):
    return _build_pdf_pipeline_options(SearchConfig(extractor_ocr_engine=engine)).ocr_options


@pytest.mark.skipif(sys.platform != "darwin", reason="ocrmac is macOS-only")
def test_auto_selects_ocrmac_on_the_mac() -> None:
    pytest.importorskip("ocrmac")
    from docling.models.stages.ocr.auto_ocr_model import OcrAutoModel

    model = OcrAutoModel(enabled=True, artifacts_path=None,
                         options=_default_options("auto"),
                         accelerator_options=AcceleratorOptions())
    assert type(model._engine).__name__ == "OcrMacModel"


@pytest.mark.skipif(sys.platform != "darwin", reason="ocrmac is macOS-only")
def test_an_explicit_ocrmac_accepts_the_default_language() -> None:
    pytest.importorskip("ocrmac")
    from docling.models.stages.ocr.ocr_mac_model import OcrMacModel

    OcrMacModel(enabled=True, artifacts_path=None, options=_default_options("ocrmac"),
                accelerator_options=AcceleratorOptions())


def test_rapidocr_accepts_the_default_language() -> None:
    pytest.importorskip("rapidocr")
    from docling.models.stages.ocr.rapid_ocr_model import RapidOcrModel

    RapidOcrModel(enabled=True, artifacts_path=None, options=_default_options("rapidocr"),
                  accelerator_options=AcceleratorOptions())
