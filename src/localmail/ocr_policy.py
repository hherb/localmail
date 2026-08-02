# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Turn ``search.extractor_ocr_engine`` into an OCR decision (#248).

``DoclingExtractor`` used to hardcode ``ocr_options=EasyOcrOptions(...)``. EasyOCR
is **not** a docling dependency, so on any install without it every scanned PDF
raised ``ImportError`` out of ``convert()`` — on the *poison-pill* path, burning
``failed_extractions.retry_count`` until the blob was given up on. Scanned PDFs
are precisely the documents the docling fallback exists for. 743 such rows had
accumulated on the live Mac archive.

The hardcoding also overrode a better default: docling's own
``PdfPipelineOptions.ocr_options`` is ``OcrAutoOptions``, which probes ocrmac →
rapidocr → easyocr and, when none is installed, logs a warning and passes pages
through **without raising**. A scanned PDF then becomes an honest
``lightweight-empty`` sentinel instead of a failure.

So the engine becomes configurable and defaults to ``auto``. The config value is
docling's own registry key (``factory.create_options(kind=...)``), which is why
there is no name→class mapping table here to drift against a docling upgrade —
the one value this module owns is :data:`OCR_DISABLED`, for which docling has no
kind.

Pure: no docling import, no IO.

Lives at the top level rather than under ``search/`` because ``config.py``
imports :data:`OCR_AUTO` as the field default, and ``localmail.search``'s
package ``__init__`` imports ``query``/``rewriter``, which import ``config`` —
so a ``search/`` home would be a circular import. Same reason
``account_names.py`` and ``fetch_retry.py`` sit here despite belonging to the
admin and sync layers respectively.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

OCR_AUTO = "auto"
"""Default. docling picks whichever OCR engine is installed and degrades to
no-OCR (no raise) when none is — so installing an engine later starts OCR with
no config change, and not having one costs nothing."""

OCR_DISABLED = "none"
"""Our own value, not a docling kind: skip OCR entirely (``do_ocr=False``).
For an operator who *has* an engine installed but does not want to pay OCR over
a large archive. docling still contributes layout and table-structure analysis
on the non-OCR path, so the fallback keeps its value."""

#: Config key quoted back to the operator in :func:`unknown_engine_message`.
_CONFIG_KEY = "search.extractor_ocr_engine"


@dataclass(frozen=True)
class OcrPlan:
    """What to ask docling for.

    Attributes:
        do_ocr: Value for ``PdfPipelineOptions.do_ocr``.
        engine_kind: docling OCR registry kind to resolve via its factory, or
            ``None`` when OCR is off and no engine need be resolved at all.
    """

    do_ocr: bool
    engine_kind: str | None


def plan_ocr(engine: str) -> OcrPlan:
    """Map the configured engine name to an :class:`OcrPlan`.

    ``none`` (and an empty/blank value, which is not a docling kind either)
    disables OCR. Everything else is forwarded as a docling kind — including a
    name docling may not know, because validating against a literal list here
    would go stale the moment docling adds or renames an engine. The extractor
    checks the live registry and reports via :func:`unknown_engine_message`.

    Case and surrounding whitespace are forgiven: this value is hand-edited in
    TOML and a stray capital should not cost an operator a debugging session.
    """
    kind = engine.strip().lower()
    if kind in ("", OCR_DISABLED):
        return OcrPlan(do_ocr=False, engine_kind=None)
    return OcrPlan(do_ocr=True, engine_kind=kind)


def unknown_engine_message(engine: str, known: Sequence[str]) -> str:
    """Operator-facing text for an engine docling does not register.

    Names the offending value, the config key that carries it, what docling
    *does* know in this install, and :data:`OCR_DISABLED` — which never appears
    in docling's registry, so the error is the only place an operator would
    discover it.
    """
    catalogue = (
        f"this docling build registers: {', '.join(known)}"
        if known
        else "this docling build registers no OCR engines"
    )
    return (
        f"unknown OCR engine {engine!r} in {_CONFIG_KEY}; "
        f"{catalogue}; use {OCR_DISABLED!r} to disable OCR."
    )
