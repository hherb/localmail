"""Attachment extractors.

Protocol + LightweightExtractor (pure-Python, no OCR, covers 11 allowlisted
formats) + DoclingExtractor (lazy-imported, OCR-capable, added later).
The extract_worker picks LightweightExtractor by default; if it returns
empty/raises on a PDF, the worker falls back to DoclingExtractor when
docling is importable.

Exports:
- ExtractedText: frozen dataclass returned by every successful extraction.
- ExtractorError: raised on irrecoverable failure.
- AttachmentExtractor: Protocol that all extractors implement.
- LightweightExtractor: pure-Python dispatch across PDF/Office/text/ODT/ICS.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol, cast, runtime_checkable

from localmail.config import SearchConfig

_LOG = logging.getLogger(__name__)


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


class TransientExtractorError(ExtractorError):
    """Raised by an extractor when the failure is *not* the blob's fault.

    Transient conditions — model-download blips, OCR backend OOM, retried-
    once IO hiccups — must not increment a blob's ``failed_extractions``
    retry_count. The worker classifies these via ``_is_transient`` and
    rolls back the SAVEPOINT without recording, leaving the blob eligible
    for the next sweep.

    Extractors may raise this directly when they detect the cause is
    transient. Otherwise the worker falls back to walking the exception's
    cause chain for built-in transient classes (ConnectionError,
    TimeoutError, MemoryError).
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
# SearchConfig defaults so there is a single source of truth.

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

    Dispatches across all 11 allowlisted formats via extract():
    PDF (pypdf), DOCX (python-docx), XLSX (openpyxl), PPTX (python-pptx),
    TXT/MD (chardet fallback), HTML (html2text), CSV (stdlib csv),
    RTF (striprtf), ODT (odfpy), ICS (icalendar).
    """

    name = "lightweight"
    version = "1.0"

    def __init__(self, cfg: "SearchConfig | None" = None) -> None:
        """Construct with an optional SearchConfig.

        If cfg is None, defaults to SearchConfig(). Configs are only
        read for numeric tunables (e.g. chardet confidence threshold) —
        extractor behaviour otherwise stays config-agnostic.
        """
        self._cfg = cfg if cfg is not None else SearchConfig()

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
        if mt == "text/plain" or ext == ".txt":
            return self._extract_txt(blob_path)
        if mt == "text/markdown" or ext == ".md":
            return self._extract_md(blob_path)
        if mt == "text/html" or ext in (".html", ".htm"):
            return self._extract_html(blob_path)
        if mt == "text/csv" or ext == ".csv":
            return self._extract_csv(blob_path)
        if mt == "application/rtf" or ext == ".rtf":
            return self._extract_rtf(blob_path)
        if "opendocument.text" in mt or ext == ".odt":
            return self._extract_odt(blob_path)
        if mt == "text/calendar" or ext == ".ics":
            return self._extract_ics(blob_path)

        raise ExtractorError(f"no extractor for {mt!r}/{ext!r}")

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

        Note: blob paths have no extension (SHA-256 hex names), so we pass
        the bytes via io.BytesIO rather than the file path — openpyxl uses
        the filename extension to detect the format when given a path string,
        which would raise an error for extensionless blob files.
        """
        import contextlib
        import io
        import openpyxl
        try:
            wb = openpyxl.load_workbook(
                io.BytesIO(blob_path.read_bytes()), read_only=True, data_only=True
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

    def _extract_txt(self, blob_path: Path) -> ExtractedText:
        """Extract text from a plain-text blob.

        Encoding detection order:
          1. UTF-8 (strict) — covers the vast majority of modern files.
          2. chardet heuristic — handles multi-byte encodings (Shift-JIS,
             GB18030, etc.) that are unambiguously detectable by chardet.
             A CHARDET_CONFIDENCE_MIN threshold guards against low-confidence
             guesses where chardet cannot distinguish between similar
             single-byte encodings (e.g. latin-1 vs cp1250 for short files).
          3. latin-1 hard fallback — IANA-registered default encoding for
             text; maps all 256 byte values without error, so this path
             never raises UnicodeDecodeError.

        errors='replace' on the chardet path absorbs any remaining
        mis-detections without raising. Logging deferred to extract_worker
        (Task 13).
        """
        import chardet

        raw = blob_path.read_bytes()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            det = chardet.detect(raw) or {}
            conf = det.get("confidence") or 0.0
            enc = (
                det.get("encoding")
                if conf >= self._cfg.extractor_chardet_confidence_min
                else None
            )
            encoding = enc or "latin-1"
            try:
                text = raw.decode(encoding, errors="replace")
            except Exception as exc:
                raise ExtractorError(f"txt decode failed: {exc}") from exc
        return ExtractedText(
            text=text.strip(),
            page_count=None,
            extractor=f"{self.name}@{self.version}",
        )

    def _extract_md(self, blob_path: Path) -> ExtractedText:
        """Extract text from a Markdown blob.

        Markdown is left as-is — chunking and embeddings handle its
        structure well without stripping. Delegates to _extract_txt for
        encoding detection (DRY: Markdown is plain text with structure
        markers we don't interpret). Logging deferred to extract_worker.
        """
        return self._extract_txt(blob_path)

    def _extract_html(self, blob_path: Path) -> ExtractedText:
        """Extract text from an HTML blob using html2text.

        Returns Markdown-ish output: markup is stripped, paragraphs and
        headings are preserved as plain text. ignore_images drops
        '![alt](url)' noise; body_width=0 disables line-wrapping so full
        sentences stay on one line for better snippet quality. Logging
        deferred to extract_worker (Task 13).
        """
        import html2text
        try:
            html = blob_path.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            raise ExtractorError(f"html read failed: {exc}") from exc
        h = html2text.HTML2Text()
        h.ignore_images = True
        h.body_width = 0
        text = h.handle(html).strip()
        return ExtractedText(
            text=text, page_count=None,
            extractor=f"{self.name}@{self.version}",
        )

    def _extract_csv(self, blob_path: Path) -> ExtractedText:
        """Extract text from a CSV blob using stdlib csv.

        Each row's cells are space-joined; rows are newline-joined. The
        newline='' argument lets csv.reader handle cross-platform line
        endings inside quoted cells correctly. Logging deferred to
        extract_worker (Task 13).
        """
        import csv
        try:
            with blob_path.open(
                "r", encoding="utf-8", errors="replace", newline=""
            ) as f:
                rows = [" ".join(r) for r in csv.reader(f)]
        except Exception as exc:
            raise ExtractorError(f"csv read failed: {exc}") from exc
        return ExtractedText(
            text="\n".join(rows).strip(),
            page_count=None,
            extractor=f"{self.name}@{self.version}",
        )

    def _extract_rtf(self, blob_path: Path) -> ExtractedText:
        """Extract text from an RTF blob using striprtf.

        Reads as UTF-8 with replacement (RTF files are nominally ASCII
        with backslash-escaped non-ASCII; replacement chars are harmless
        for the rare malformed case). Logging deferred to extract_worker
        (Task 13).
        """
        from striprtf.striprtf import rtf_to_text
        try:
            raw = blob_path.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            raise ExtractorError(f"rtf read failed: {exc}") from exc
        try:
            text = rtf_to_text(raw)
        except Exception as exc:
            raise ExtractorError(f"striprtf failed: {exc}") from exc
        return ExtractedText(
            text=text.strip(),
            page_count=None,
            extractor=f"{self.name}@{self.version}",
        )

    def _extract_odt(self, blob_path: Path) -> ExtractedText:
        """Extract text from an ODT blob using odfpy.

        Walks all <text:p> paragraph elements and concatenates their
        character data. Logging deferred to extract_worker (Task 13).
        """
        from odf.opendocument import load
        from odf.text import P
        try:
            doc = load(str(blob_path))
        except Exception as exc:
            raise ExtractorError(f"odfpy failed to load: {exc}") from exc
        paras = doc.getElementsByType(P)
        text = "\n".join(
            "".join(node.data for node in p.childNodes if hasattr(node, "data"))
            for p in paras
        ).strip()
        return ExtractedText(
            text=text, page_count=None,
            extractor=f"{self.name}@{self.version}",
        )

    def _extract_ics(self, blob_path: Path) -> ExtractedText:
        """Extract text from an ICS calendar blob using icalendar.

        For each VEVENT, concatenates SUMMARY, DESCRIPTION, LOCATION,
        DTSTART, and ATTENDEE values as text. page_count carries the
        event count (or None when no events). Logging deferred to
        extract_worker (Task 13).
        """
        from icalendar import Calendar
        try:
            raw = blob_path.read_bytes()
            cal = Calendar.from_ical(raw)
        except Exception as exc:
            raise ExtractorError(f"icalendar parse failed: {exc}") from exc

        parts: list[str] = []
        event_count = 0
        for component in cal.walk():
            if component.name == "VEVENT":
                event_count += 1
                for field in ("SUMMARY", "DESCRIPTION", "LOCATION"):
                    val = component.get(field)
                    if val:
                        parts.append(str(val))
                dtstart = component.get("DTSTART")
                if dtstart:
                    parts.append(str(dtstart.dt))
                attendees = component.get("ATTENDEE", [])
                if attendees:
                    # ATTENDEE may be a single value or a list
                    if not isinstance(attendees, list):
                        attendees = [attendees]
                    for attendee in attendees:
                        parts.append(str(attendee))

        return ExtractedText(
            text="\n".join(parts).strip(),
            page_count=event_count or None,
            extractor=f"{self.name}@{self.version}",
        )


# --- Docling (optional, OCR-capable) -----------------------------------------
#
# Docling is in the [extraction] uv extra — installed via
# `uv sync --extra extraction`. The extractor lazy-imports it on first
# call. When not installed, `warn_docling_missing()` emits exactly one
# WARN per process pointing at the install hint, and .extract() raises
# ExtractorError so the extract_worker records the failure cleanly.

_DOCLING_WARNED = False


def _try_import_docling() -> "type | None":
    """Return docling's `DocumentConverter` class, or `None` if docling
    is not installed.

    Indirected for test monkeypatching: tests replace this function to
    simulate the "missing" state without uninstalling docling.
    """
    try:
        from docling.document_converter import DocumentConverter
        return DocumentConverter
    except ImportError:
        return None


def warn_docling_missing() -> None:
    """Emit a one-shot WARN per process pointing at the docling install hint.

    The extract_worker calls this on the first PDF where
    lightweight extraction returned empty/raised AND docling is not
    importable. Subsequent calls in the same process are silent so a
    large archive sync doesn't flood the log.
    """
    global _DOCLING_WARNED
    if _DOCLING_WARNED:
        return
    _DOCLING_WARNED = True
    _LOG.warning(
        "docling is not installed; PDFs that lightweight cannot read "
        "will be marked as lightweight-empty. Install with "
        "`uv sync --extra extraction` to enable OCR fallback for "
        "scanned PDFs."
    )


class DoclingExtractor:
    """PDF-only extractor using docling for OCR + complex-PDF layout.

    Triggered by the extract_worker only when LightweightExtractor
    returned empty text or raised on a PDF. Lazy-imports docling so
    the package stays in the [extraction] optional dependency group.
    """

    name = "docling"
    version = "1.0"  # overwritten by importlib.metadata at extract time.

    def __init__(self, cfg: "SearchConfig | None" = None) -> None:
        """Construct with an optional SearchConfig.

        Defaults to SearchConfig() when omitted. The configured
        extractor_docling_max_pages and extractor_ocr_languages are read
        when .extract() builds the docling pipeline.
        """
        self._cfg = cfg if cfg is not None else SearchConfig()

    def supports(self, mime_type: str | None, filename: str | None) -> bool:
        """True iff the blob is a PDF (by MIME or by filename extension)."""
        ext = Path(filename).suffix.lower() if filename else ""
        mt = (mime_type or "").lower()
        return mt == "application/pdf" or ext == ".pdf"

    def extract(
        self, blob_path: Path, mime_type: str | None
    ) -> ExtractedText:
        """Extract text from a PDF blob via docling.

        Raises ExtractorError when docling is not installed. Returns
        ExtractedText with text='' when docling produces no text (e.g.,
        the OCR pipeline fails to find any glyphs on a blank scan).

        Passes extractor_docling_max_pages and extractor_ocr_languages
        from SearchConfig into PdfPipelineOptions when the installed
        docling version exposes those option classes. Falls back to a
        default DocumentConverter() when they are unavailable (older
        docling builds) — the page cap and language list are best-effort.
        """
        DocumentConverter = _try_import_docling()
        if DocumentConverter is None:
            raise ExtractorError(
                "docling not installed; install via "
                "`uv sync --extra extraction`"
            )

        try:
            from importlib.metadata import PackageNotFoundError, version as pkg_version
            try:
                self_version = pkg_version("docling")
            except PackageNotFoundError:
                self_version = self.version
        except Exception:
            self_version = self.version

        # Build pipeline options if the docling option-classes are importable.
        # If the installed docling version doesn't expose these names, fall back
        # to default DocumentConverter() so older builds still work.
        converter = None
        try:
            from docling.datamodel.base_models import InputFormat
            from docling.datamodel.pipeline_options import (
                EasyOcrOptions,
                PdfPipelineOptions,
            )
            from docling.document_converter import PdfFormatOption

            pipeline_options = PdfPipelineOptions(
                do_ocr=True,
                ocr_options=EasyOcrOptions(lang=self._cfg.extractor_ocr_languages),
            )
            try:
                pipeline_options.max_num_pages = self._cfg.extractor_docling_max_pages
            except Exception:
                # Older docling versions may not expose this attribute.
                pass

            converter = DocumentConverter(
                format_options={
                    InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options),
                },
            )
        except Exception:
            # Fall back to default converter if option classes are unavailable.
            converter = DocumentConverter()

        try:
            result = converter.convert(str(blob_path))
        except Exception as exc:
            raise ExtractorError(f"docling.convert failed: {exc}") from exc

        try:
            text = result.document.export_to_markdown()
        except Exception as exc:
            raise ExtractorError(
                f"docling export_to_markdown failed: {exc}"
            ) from exc

        page_count = None
        if hasattr(result.document, "pages"):
            try:
                page_count = len(result.document.pages)
            except Exception:
                page_count = None

        return ExtractedText(
            text=(text or "").strip(),
            page_count=page_count,
            extractor=f"{self.name}@{self_version}",
        )
