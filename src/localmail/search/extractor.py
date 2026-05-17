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
from typing import Callable, Protocol, cast, runtime_checkable

from localmail.config import SearchConfig


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

    def supports(self, mime_type: str | None, filename: str | None) -> bool:
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
# Pure-Python, no OCR. The MIME/extension allowlists below are derived from
# SearchConfig defaults so there is a single source of truth. Per-format
# extraction is added in Tasks 7-10; for now .extract() raises
# NotImplementedError to make the gap obvious.

# Derived from SearchConfig defaults — single source of truth.
_LW_MIME_PREFIXES: frozenset[str] = frozenset(
    cast(Callable[[], list[str]],
         SearchConfig.model_fields["extractor_mime_allowlist"].default_factory)()
)
_LW_EXTENSIONS: frozenset[str] = frozenset(
    cast(Callable[[], list[str]],
         SearchConfig.model_fields["extractor_extension_allowlist"].default_factory)()
)


class LightweightExtractor:
    """Pure-Python extractor for documents that don't require OCR.

    Supports the 11 MIME/extension pairs in the Phase 2 allowlist.
    Per-format dispatch is added in subsequent tasks (7-10); .extract()
    currently raises NotImplementedError.
    """

    name = "lightweight"
    version = "1.0"

    def supports(self, mime_type: str | None, filename: str | None) -> bool:
        """True iff the MIME type or filename extension is allowlisted."""
        if mime_type and mime_type.lower() in _LW_MIME_PREFIXES:
            return True
        ext = Path(filename).suffix.lower() if filename else ""
        return ext in _LW_EXTENSIONS

    def extract(
        self, blob_path: Path, mime_type: str | None
    ) -> ExtractedText:
        """Extract text from `blob_path`.

        Dispatches per format. Returns ExtractedText with text='' when
        the file is structurally valid but contains no extractable text
        (e.g. a scanned PDF with no native text stream). Raises
        ExtractorError on irrecoverable parse failures (corrupt bytes,
        encryption, etc.).
        """
        ext = blob_path.suffix.lower()
        mt = (mime_type or "").lower()

        if mt == "application/pdf" or ext == ".pdf":
            return self._extract_pdf(blob_path)
        if "wordprocessingml" in mt or ext == ".docx":
            return self._extract_docx(blob_path)
        if "spreadsheetml" in mt or ext == ".xlsx":
            return self._extract_xlsx(blob_path)
        if "presentationml" in mt or ext == ".pptx":
            return self._extract_pptx(blob_path)

        raise NotImplementedError(
            f"per-format dispatch for {mt!r}/{ext!r} added in subsequent tasks"
        )

    def _extract_pdf(self, blob_path: Path) -> ExtractedText:
        """Extract text from a PDF blob using pypdf.

        Returns ExtractedText(text='', ...) on PDFs whose only content
        is rasterized images (scanned documents). Raises ExtractorError
        on encrypted/password-protected PDFs and on irrecoverable parse
        failures.
        """
        # Logging deferred to extract_worker (Task 13): it catches
        # ExtractorError, records the blob in failed_extractions, and
        # emits the log line. Keeping this method silent matches the
        # Phase 1 chunking.py / embed_worker.py separation of concerns.
        import pypdf
        try:
            reader = pypdf.PdfReader(str(blob_path))
        except Exception as exc:
            raise ExtractorError(f"pypdf failed to open: {exc}") from exc

        if reader.is_encrypted:
            raise ExtractorError(
                "pypdf: encrypted PDF (no password supplied)"
            )

        try:
            pages = [page.extract_text() or "" for page in reader.pages]
        except Exception as exc:
            raise ExtractorError(f"pypdf failed to extract: {exc}") from exc

        text = "\n".join(pages).strip()
        return ExtractedText(
            text=text,
            page_count=len(reader.pages),
            extractor=f"{self.name}@{self.version}",
        )

    def _extract_docx(self, blob_path: Path) -> ExtractedText:
        """Extract text from a DOCX blob using python-docx.

        Logging deferred to extract_worker (Task 13): silent on success
        and on raise; the worker catches ExtractorError and reports.
        """
        import docx
        try:
            d = docx.Document(str(blob_path))
        except Exception as exc:
            raise ExtractorError(f"python-docx failed to open: {exc}") from exc
        paras = [p.text for p in d.paragraphs if p.text]
        text = "\n".join(paras).strip()
        return ExtractedText(
            text=text, page_count=None,
            extractor=f"{self.name}@{self.version}",
        )

    def _extract_xlsx(self, blob_path: Path) -> ExtractedText:
        """Extract text from an XLSX blob using openpyxl.

        Reads all sheets; each row is joined into a single text line.
        Logging deferred to extract_worker (Task 13).
        """
        import contextlib
        import openpyxl
        try:
            wb = openpyxl.load_workbook(
                str(blob_path), read_only=True, data_only=True
            )
        except Exception as exc:
            raise ExtractorError(f"openpyxl failed to open: {exc}") from exc

        parts: list[str] = []
        with contextlib.closing(wb):
            for ws in wb.worksheets:
                for row in ws.iter_rows(values_only=True):
                    row_text = " ".join(str(c) for c in row if c is not None)
                    if row_text:
                        parts.append(row_text)
        text = "\n".join(parts).strip()
        return ExtractedText(
            text=text, page_count=None,
            extractor=f"{self.name}@{self.version}",
        )

    def _extract_pptx(self, blob_path: Path) -> ExtractedText:
        """Extract text from a PPTX blob using python-pptx.

        Captures shape-frame text (including field elements like slide
        numbers and date fields) plus speaker notes. Logging deferred
        to extract_worker (Task 13).
        """
        from pptx import Presentation
        try:
            prs = Presentation(str(blob_path))
        except Exception as exc:
            raise ExtractorError(f"python-pptx failed to open: {exc}") from exc

        parts: list[str] = []
        for slide in prs.slides:
            for shape in slide.shapes:
                if shape.has_text_frame:
                    t = shape.text_frame.text.strip()
                    if t:
                        parts.append(t)
            if slide.has_notes_slide:
                ntf = slide.notes_slide.notes_text_frame
                if ntf is not None:
                    notes = ntf.text
                    if notes:
                        parts.append(notes)

        text = "\n".join(parts).strip()
        return ExtractedText(
            text=text, page_count=None,
            extractor=f"{self.name}@{self.version}",
        )
