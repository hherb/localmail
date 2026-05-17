"""Attachment extractors.

Protocol + LightweightExtractor (pure-Python, no OCR) + DoclingExtractor
(lazy-imported, OCR-capable). The extract_worker picks LightweightExtractor
by default; if it returns empty/raises on a PDF, the worker falls back to
DoclingExtractor when docling is importable.

This module currently defines only:
- ExtractedText: frozen dataclass returned by every successful extraction.
- ExtractorError: raised on irrecoverable failure.
- AttachmentExtractor: Protocol that all extractors implement.
- LightweightExtractor: skeleton class. Per-format dispatch added in
  subsequent tasks (Tasks 7-10).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class ExtractedText:
    """The result of extracting text from a blob.

    Attributes:
        text: The extracted plain-text. May be '' (sentinel meaning "we
            tried, got nothing, don't retry").
        page_count: Optional logical page count (PDFs, Office docs). None
            for plain-text formats like TXT/MD/HTML/CSV/ICS.
        extractor: Identifier of the extractor that produced the text,
            including version. Values used elsewhere in the codebase:
            'lightweight@1.0', 'docling@X.Y', 'lightweight-empty',
            'size-skipped'.
    """

    text: str
    page_count: int | None
    extractor: str


class ExtractorError(Exception):
    """Raised by an extractor on irrecoverable failure.

    The caller (extract_worker) records the blob in failed_extractions
    and continues processing the next blob in the batch.
    """


@runtime_checkable
class AttachmentExtractor(Protocol):
    """The contract every attachment extractor must implement.

    `@runtime_checkable` lets the extract_worker verify a configured
    extractor instance satisfies the protocol via isinstance() at startup.
    """

    name: str
    version: str

    def supports(self, mime_type: str | None, filename: str) -> bool:
        """Return True iff this extractor can process the given blob.

        Implementations should accept either a known MIME type OR a
        matching filename extension (mail clients commonly mis-set MIME).
        """
        ...

    def extract(
        self, blob_path: Path, mime_type: str | None
    ) -> ExtractedText:
        """Extract text from the blob at `blob_path`.

        Returns an ExtractedText. May return ExtractedText(text='', ...)
        when the file is structurally valid but contains no extractable
        text (e.g. a scanned PDF for a non-OCR extractor). Raises
        ExtractorError on irrecoverable failure (parse error, encryption,
        truncated bytes, etc.).
        """
        ...


# --- Lightweight extractor ---------------------------------------------------
#
# Pure-Python, no OCR. The MIME/extension allowlists below mirror the
# SearchConfig.extractor_*_allowlist defaults so .supports() works without
# the worker passing config in. Per-format extraction is added in Tasks
# 7-10; for now .extract() raises NotImplementedError to make the gap
# obvious.

_LW_MIME_PREFIXES = frozenset({
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/vnd.oasis.opendocument.text",
    "application/rtf",
    "text/plain",
    "text/markdown",
    "text/html",
    "text/csv",
    "text/calendar",
})

_LW_EXTENSIONS = frozenset({
    ".pdf", ".docx", ".xlsx", ".pptx", ".odt", ".rtf",
    ".txt", ".md", ".html", ".htm", ".csv", ".ics",
})


class LightweightExtractor:
    """Pure-Python extractor for documents that don't require OCR.

    Supports the 11 MIME/extension pairs in the Phase 2 allowlist.
    Per-format dispatch is added in subsequent tasks (7-10); .extract()
    currently raises NotImplementedError.
    """

    name = "lightweight"
    version = "1.0"

    def supports(self, mime_type: str | None, filename: str) -> bool:
        """True iff the MIME type or filename extension is allowlisted."""
        if mime_type and mime_type.lower() in _LW_MIME_PREFIXES:
            return True
        ext = Path(filename).suffix.lower() if filename else ""
        return ext in _LW_EXTENSIONS

    def extract(
        self, blob_path: Path, mime_type: str | None
    ) -> ExtractedText:
        """Extract text from `blob_path`. Stub — per-format dispatch
        is added in Tasks 7-10."""
        raise NotImplementedError(
            "per-format dispatch is added in Tasks 7-10"
        )
