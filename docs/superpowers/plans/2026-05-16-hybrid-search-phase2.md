# Hybrid Search Phase 2 — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the text content of email attachments searchable — extract text from blobs, chunk + embed via the existing pipeline, and add a fourth vector-cosine retrieval arm over attachment chunks joined to messages via JSONB.

**Architecture:** A new `extract_worker` daemon thread converts MIME-allowlisted blobs in `attachment_blobs` to plain text in a new `attachment_text` table using `LightweightExtractor` first (pure-Python, no OCR) and `DoclingExtractor` only when lightweight returns empty/raises on a PDF. The existing `embed_worker` is extended to chunk and embed `attachment_text` into `attachment_chunks` via the existing `chunk_table` discriminator. `Searcher` runs a new fourth arm (vector cosine over `attachment_chunks` JOINed to `messages.attachments` JSONB) alongside the three Phase 1 arms; RRF and rerank are unchanged.

**Tech Stack:** Python 3.12, psycopg v3 + raw SQL, pgvector halfvec(768) + HNSW, Postgres GIN. Lightweight extractors: `pypdf`, `python-docx`, `openpyxl`, `python-pptx`, `odfpy`, `striprtf`, `chardet`, `html2text`, `icalendar`. Heavy extraction: `docling` (optional `[extraction]` uv extra). Test fixtures: `reportlab`, `Pillow`, `pikepdf`.

**Spec:** [docs/superpowers/specs/2026-05-16-hybrid-search-phase2-design.md](../specs/2026-05-16-hybrid-search-phase2-design.md)

---

## Prerequisites (one-time, before starting)

- A clean working tree on `main` (Phase 1 merged at `1576e05`, acceptance at `8969255`).
- `localmail` and `localmail_test` databases reachable at the DSN in
  `LOCALMAIL_TEST_DSN` (default `postgresql://localmail:local%40%40mail@localhost:5532/localmail_test`).
- pgvector already installed (Phase 1).
- Shells running tasks should prefix every invocation with `unset VIRTUAL_ENV &&` per CLAUDE.md.

---

### Task 1: Add Phase 2 runtime + dev dependencies

**Files:**
- Modify: `pyproject.toml`

Phase 2 needs lightweight extractor libraries as runtime deps, plus docling as an optional `[extraction]` extra, plus three test-fixture builders as dev deps.

- [ ] **Step 1: Add lightweight runtime deps**

Open `pyproject.toml`, find the `dependencies = [...]` array under `[project]`, and add (alphabetical insertion in the existing list):

```toml
    "chardet>=5.2",
    "html2text>=2024.2.26",
    "icalendar>=5.0",
    "odfpy>=1.4",
    "openpyxl>=3.1",
    "pypdf>=4.0",
    "python-docx>=1.1",
    "python-pptx>=0.6.23",
    "striprtf>=0.0.26",
```

- [ ] **Step 2: Add docling as optional `[extraction]` extra**

Add (or extend if missing) the `[project.optional-dependencies]` section:

```toml
[project.optional-dependencies]
extraction = [
    "docling>=2.20",
]
```

- [ ] **Step 3: Add dev/test-fixture deps**

Find the existing `[dependency-groups]` `dev = [...]` array (or `[tool.uv.dev-dependencies]` depending on uv version) and add:

```toml
    "pikepdf>=9.0",
    "Pillow>=10.3",
    "reportlab>=4.0",
```

- [ ] **Step 4: Sync and verify**

Run:

```bash
unset VIRTUAL_ENV && uv sync
unset VIRTUAL_ENV && uv run python -c "import pypdf, docx, openpyxl, pptx, odf, striprtf, chardet, html2text, icalendar, reportlab, PIL, pikepdf; print('all imports ok')"
```

Expected: `all imports ok`.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "build(phase2): add lightweight extractor + test-fixture deps

Runtime: pypdf python-docx openpyxl python-pptx odfpy striprtf chardet
html2text icalendar. Optional [extraction] extra: docling. Dev:
reportlab Pillow pikepdf for synthetic fixture builders."
```

---

### Task 2: Migration 0011 — `attachment_text` + `attachment_chunks`

**Files:**
- Create: `migrations/0011_attachment_text.sql`
- Modify: `tests/conftest.py` (extend TRUNCATE list)
- Test: `tests/test_search_schema.py` (extend)

- [ ] **Step 1: Write the failing test**

Open `tests/test_search_schema.py`. Add at the bottom:

```python
def test_attachment_text_and_chunks_tables_exist(db_conn):
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT column_name, is_nullable, data_type "
            "FROM information_schema.columns "
            "WHERE table_name = 'attachment_text' ORDER BY ordinal_position"
        )
        cols = cur.fetchall()
    names = [c[0] for c in cols]
    assert names == ["sha256", "extractor", "extracted_text", "page_count", "extracted_at"]

    nullable = {c[0]: c[1] for c in cols}
    assert nullable["extracted_text"] == "NO"
    assert nullable["page_count"] == "YES"

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'attachment_chunks' ORDER BY ordinal_position"
        )
        names = [r[0] for r in cur.fetchall()]
    assert names == [
        "id", "sha256", "chunk_idx", "text", "token_count", "embedding_v1", "embedded_at"
    ]

    # Unique (sha256, chunk_idx)
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM pg_indexes "
            "WHERE tablename = 'attachment_chunks' "
            "AND indexdef ILIKE '%UNIQUE%(sha256, chunk_idx)%'"
        )
        assert cur.fetchone() is not None

    # Partial pending index
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT indexdef FROM pg_indexes "
            "WHERE indexname = 'attachment_chunks_pending_idx'"
        )
        row = cur.fetchone()
    assert row is not None
    assert "WHERE (embedding_v1 IS NULL)" in row[0]
```

- [ ] **Step 2: Run test, verify it fails**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_search_schema.py::test_attachment_text_and_chunks_tables_exist -v
```

Expected: FAIL with `relation "attachment_text" does not exist` (or similar).

- [ ] **Step 3: Write the migration**

Create `migrations/0011_attachment_text.sql`:

```sql
-- Attachment text + chunks tables (Phase 2).
-- Per-blob extracted text and chunk rows keyed on the blob's sha256,
-- not on message_id — the content-addressable blob design means one
-- chunk set per unique byte sequence regardless of how many messages
-- reference it.

CREATE TABLE attachment_text (
    sha256          BYTEA       PRIMARY KEY
                                REFERENCES attachment_blobs(sha256) ON DELETE CASCADE,
    extractor       TEXT        NOT NULL,
    extracted_text  TEXT        NOT NULL,
    page_count      INT,
    extracted_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE attachment_chunks (
    id              BIGSERIAL    PRIMARY KEY,
    sha256          BYTEA        NOT NULL
                                 REFERENCES attachment_blobs(sha256) ON DELETE CASCADE,
    chunk_idx       INT          NOT NULL,
    text            TEXT         NOT NULL,
    token_count     INT          NOT NULL,
    embedding_v1    halfvec(768),
    embedded_at     TIMESTAMPTZ,
    UNIQUE (sha256, chunk_idx)
);

CREATE INDEX attachment_chunks_blob_idx
    ON attachment_chunks (sha256);
CREATE INDEX attachment_chunks_pending_idx
    ON attachment_chunks (id) WHERE embedding_v1 IS NULL;
```

- [ ] **Step 4: Extend conftest's TRUNCATE list**

Modify `tests/conftest.py`, the `db_conn` fixture, replacing the TRUNCATE statement:

```python
            cur.execute(
                "TRUNCATE accounts, mailboxes, messages, message_labels, "
                "attachment_blobs, failed_messages, message_chunks, "
                "failed_embeddings, embedding_models, failed_chunkings, "
                "attachment_text, attachment_chunks "
                "RESTART IDENTITY CASCADE"
            )
```

(Adds `failed_chunkings` — missed in Phase 1 — and the two new Phase 2 tables.)

- [ ] **Step 5: Run test, verify it passes**

```bash
unset VIRTUAL_ENV && uv run localmail init-db   # apply 0011 to localmail_test
unset VIRTUAL_ENV && uv run pytest tests/test_search_schema.py::test_attachment_text_and_chunks_tables_exist -v
```

Expected: `applied 0011_attachment_text` then PASS.

- [ ] **Step 6: Run full schema test file to confirm no regressions**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_search_schema.py -v
```

Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add migrations/0011_attachment_text.sql tests/conftest.py tests/test_search_schema.py
git commit -m "feat(phase2): migration 0011 — attachment_text + attachment_chunks

New per-blob tables for extracted text and chunk+embedding rows.
Conftest TRUNCATE list extended to include the new tables plus
failed_chunkings (missed in Phase 1)."
```

---

### Task 3: Migration 0012 — `failed_extractions`

**Files:**
- Create: `migrations/0012_failed_extractions.sql`
- Modify: `tests/conftest.py` (extend TRUNCATE list)
- Test: `tests/test_search_schema.py` (extend)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_search_schema.py`:

```python
def test_failed_extractions_table_exists(db_conn):
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT column_name, is_nullable "
            "FROM information_schema.columns "
            "WHERE table_name = 'failed_extractions' ORDER BY ordinal_position"
        )
        cols = cur.fetchall()
    names = [c[0] for c in cols]
    assert names == [
        "sha256", "extractor", "error_class", "error_message", "traceback",
        "retry_count", "failed_at", "last_retry_at",
    ]
    nullable = {c[0]: c[1] for c in cols}
    assert nullable["extractor"] == "NO"
    assert nullable["traceback"] == "YES"
    assert nullable["last_retry_at"] == "YES"

    # PK is sha256 alone
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT a.attname FROM pg_index i "
            "JOIN pg_attribute a ON a.attrelid = i.indrelid "
            "AND a.attnum = ANY(i.indkey) "
            "WHERE i.indrelid = 'failed_extractions'::regclass "
            "AND i.indisprimary"
        )
        pk_cols = [r[0] for r in cur.fetchall()]
    assert pk_cols == ["sha256"]
```

- [ ] **Step 2: Run test, verify it fails**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_search_schema.py::test_failed_extractions_table_exists -v
```

Expected: FAIL with `relation "failed_extractions" does not exist`.

- [ ] **Step 3: Write the migration**

Create `migrations/0012_failed_extractions.sql`:

```sql
-- Failed-extractions log (Phase 2). One row per blob (not per
-- (blob, extractor) pair). On retry the row is upserted and
-- retry_count is bumped. The extractor column records the most
-- recent failing extractor — sufficient for diagnostics.

CREATE TABLE failed_extractions (
    sha256          BYTEA       PRIMARY KEY
                                REFERENCES attachment_blobs(sha256) ON DELETE CASCADE,
    extractor       TEXT        NOT NULL,
    error_class     TEXT        NOT NULL,
    error_message   TEXT        NOT NULL,
    traceback       TEXT,
    retry_count     INT         NOT NULL DEFAULT 0,
    failed_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_retry_at   TIMESTAMPTZ
);
```

- [ ] **Step 4: Extend conftest TRUNCATE**

Modify the `db_conn` fixture TRUNCATE statement to add `failed_extractions`:

```python
            cur.execute(
                "TRUNCATE accounts, mailboxes, messages, message_labels, "
                "attachment_blobs, failed_messages, message_chunks, "
                "failed_embeddings, embedding_models, failed_chunkings, "
                "attachment_text, attachment_chunks, failed_extractions "
                "RESTART IDENTITY CASCADE"
            )
```

- [ ] **Step 5: Apply + run test**

```bash
unset VIRTUAL_ENV && uv run localmail init-db
unset VIRTUAL_ENV && uv run pytest tests/test_search_schema.py::test_failed_extractions_table_exists -v
```

Expected: `applied 0012_failed_extractions` then PASS.

- [ ] **Step 6: Commit**

```bash
git add migrations/0012_failed_extractions.sql tests/conftest.py tests/test_search_schema.py
git commit -m "feat(phase2): migration 0012 — failed_extractions

Per-blob failure log keyed on sha256. Retries upsert; extractor column
records the most recent failing extractor."
```

---

### Task 4: Migration 0013 — Arm 4 indexes (HNSW + JSONB GIN)

**Files:**
- Create: `migrations/0013_attachment_search_indexes.sql`
- Test: `tests/test_search_schema.py` (extend)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_search_schema.py`:

```python
def test_attachment_arm4_indexes_exist(db_conn):
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT indexdef FROM pg_indexes "
            "WHERE indexname = 'attachment_chunks_embedding_v1_hnsw'"
        )
        row = cur.fetchone()
    assert row is not None
    assert "USING hnsw" in row[0]
    assert "halfvec_cosine_ops" in row[0]

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT indexdef FROM pg_indexes "
            "WHERE indexname = 'messages_attachments_gin'"
        )
        row = cur.fetchone()
    assert row is not None
    assert "USING gin" in row[0]
    assert "(attachments)" in row[0]
```

- [ ] **Step 2: Run test, verify it fails**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_search_schema.py::test_attachment_arm4_indexes_exist -v
```

Expected: FAIL — neither index exists.

- [ ] **Step 3: Write the migration**

Create `migrations/0013_attachment_search_indexes.sql`:

```sql
-- @non-transactional
-- Arm 4 indexes. CREATE INDEX CONCURRENTLY needs autocommit, hence the
-- @non-transactional header. Note: _split_statements in db.py is naive
-- on semicolons inside strings/dollar-quoted blocks; this migration
-- contains neither, so it is safe.

CREATE INDEX CONCURRENTLY IF NOT EXISTS attachment_chunks_embedding_v1_hnsw
    ON attachment_chunks USING hnsw (embedding_v1 halfvec_cosine_ops)
    WITH (m=16, ef_construction=64);

CREATE INDEX CONCURRENTLY IF NOT EXISTS messages_attachments_gin
    ON messages USING GIN (attachments);
```

- [ ] **Step 4: Apply + verify**

```bash
unset VIRTUAL_ENV && uv run localmail init-db
unset VIRTUAL_ENV && uv run pytest tests/test_search_schema.py::test_attachment_arm4_indexes_exist -v
```

Expected: `applied 0013_attachment_search_indexes` then PASS.

- [ ] **Step 5: Commit**

```bash
git add migrations/0013_attachment_search_indexes.sql tests/test_search_schema.py
git commit -m "feat(phase2): migration 0013 — Arm 4 indexes

HNSW on attachment_chunks.embedding_v1 (halfvec_cosine_ops) for vector
retrieval. GIN on messages.attachments for the JSONB join in Arm 4.
Both built CONCURRENTLY (non-transactional migration)."
```

---

### Task 5: Phase 2 fields on `SearchConfig`

**Files:**
- Modify: `src/localmail/config.py`
- Test: `tests/test_config.py` (extend)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_config.py`:

```python
def test_search_config_phase2_defaults():
    from localmail.config import SearchConfig
    cfg = SearchConfig()

    # Extraction worker
    assert cfg.run_extract_worker is True
    assert cfg.extract_worker_poll_interval_s == 30
    assert cfg.extract_worker_batch_size == 20
    assert cfg.extract_worker_max_retries == 3

    # Extractor policy
    assert "application/pdf" in cfg.extractor_mime_allowlist
    assert "text/calendar" in cfg.extractor_mime_allowlist
    assert ".pdf" in cfg.extractor_extension_allowlist
    assert ".ics" in cfg.extractor_extension_allowlist
    assert cfg.extractor_max_blob_bytes == 50 * 1024 * 1024
    assert cfg.extractor_max_extracted_chars == 1_000_000
    assert cfg.extractor_docling_max_pages == 200
    assert cfg.extractor_ocr_languages == ["en"]

    # Arm 4
    assert cfg.arm4_fanout_cap == 10
```

- [ ] **Step 2: Run test, verify it fails**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_config.py::test_search_config_phase2_defaults -v
```

Expected: FAIL with `AttributeError: ... extract_worker_enabled`.

- [ ] **Step 3: Add fields to SearchConfig**

In `src/localmail/config.py`, find the `class SearchConfig(BaseModel):` block. Add the following fields (preserve any existing fields; insert at the bottom of the class):

```python
    # --- Phase 2: extraction worker ---
    run_extract_worker: bool = True
    extract_worker_poll_interval_s: int = 30
    extract_worker_batch_size: int = 20
    extract_worker_max_retries: int = 3

    # --- Phase 2: extractor policy ---
    extractor_mime_allowlist: list[str] = Field(
        default_factory=lambda: [
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
        ]
    )
    extractor_extension_allowlist: list[str] = Field(
        default_factory=lambda: [
            ".pdf", ".docx", ".xlsx", ".pptx", ".odt", ".rtf",
            ".txt", ".md", ".html", ".htm", ".csv", ".ics",
        ]
    )
    extractor_max_blob_bytes: int = 50 * 1024 * 1024
    extractor_max_extracted_chars: int = 1_000_000
    extractor_docling_max_pages: int = 200
    extractor_ocr_languages: list[str] = Field(default_factory=lambda: ["en"])

    # --- Phase 2: Arm 4 ---
    arm4_fanout_cap: int = 10
```

If `Field` is not already imported in `config.py`, add `from pydantic import Field` (or `from pydantic import BaseModel, Field` if BaseModel is already imported).

- [ ] **Step 4: Run test, verify it passes**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_config.py::test_search_config_phase2_defaults -v
```

Expected: PASS.

- [ ] **Step 5: Run full config test file**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_config.py -v
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add src/localmail/config.py tests/test_config.py
git commit -m "feat(phase2): add SearchConfig fields for extraction + Arm 4

All numeric/policy tunables now live on SearchConfig per Rule 4 (no
magic numbers in extraction code)."
```

---

### Task 6: Extractor protocol module

**Files:**
- Create: `src/localmail/search/extractor.py`
- Test: `tests/test_extractor.py` (new)

This task lays down the type contracts (`ExtractedText`, `ExtractorError`, `AttachmentExtractor` Protocol) and a stub `LightweightExtractor` class skeleton. Subsequent tasks fill in per-format dispatch.

- [ ] **Step 1: Write the failing test**

Create `tests/test_extractor.py`:

```python
from pathlib import Path

import pytest

from localmail.search.extractor import (
    AttachmentExtractor,
    ExtractedText,
    ExtractorError,
    LightweightExtractor,
)


def test_extracted_text_is_frozen_dataclass():
    et = ExtractedText(text="hello", page_count=1, extractor="x@1")
    with pytest.raises(Exception):
        et.text = "world"  # type: ignore[misc]


def test_lightweight_supports_pdf_mime_and_ext():
    lw = LightweightExtractor()
    assert lw.supports("application/pdf", "foo.pdf")
    assert lw.supports(None, "foo.pdf")
    assert lw.supports("application/pdf", "")
    assert not lw.supports("video/mp4", "foo.mp4")


def test_lightweight_does_not_support_image():
    lw = LightweightExtractor()
    assert not lw.supports("image/png", "logo.png")


def test_extractor_protocol_runtime_checkable():
    # AttachmentExtractor should be runtime-checkable so the worker
    # can verify a configured class implements the interface.
    lw = LightweightExtractor()
    assert isinstance(lw, AttachmentExtractor)
```

- [ ] **Step 2: Run test, verify it fails**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_extractor.py -v
```

Expected: FAIL with `ModuleNotFoundError: localmail.search.extractor`.

- [ ] **Step 3: Create the module**

Create `src/localmail/search/extractor.py`:

```python
"""Attachment extractors.

Protocol + LightweightExtractor (pure-Python, no OCR) + DoclingExtractor
(lazy-imported, OCR-capable). The extract_worker picks LightweightExtractor
by default; if it returns empty/raises on a PDF, the worker falls back to
DoclingExtractor when docling is importable.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class ExtractedText:
    text: str               # may be '' (sentinel)
    page_count: int | None  # None for TXT/MD/HTML/CSV/ICS
    extractor: str          # 'lightweight@1.0' / 'docling@X.Y' /
                            # 'lightweight-empty' / 'size-skipped'


class ExtractorError(Exception):
    """Raised by an extractor on irrecoverable failure.

    Caller records in failed_extractions and continues to the next blob.
    """


@runtime_checkable
class AttachmentExtractor(Protocol):
    name: str
    version: str

    def supports(self, mime_type: str | None, filename: str) -> bool: ...

    def extract(
        self, blob_path: Path, mime_type: str | None
    ) -> ExtractedText: ...


# --- Lightweight extractor (per-format dispatch added in subsequent tasks) ---

# Module-level allowlist used for the .supports() check. Mirrors the
# SearchConfig defaults; tasks 7-10 wire actual per-format extraction.
_LW_MIME_PREFIXES = {
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
}
_LW_EXTENSIONS = {
    ".pdf", ".docx", ".xlsx", ".pptx", ".odt", ".rtf",
    ".txt", ".md", ".html", ".htm", ".csv", ".ics",
}


class LightweightExtractor:
    name = "lightweight"
    version = "1.0"

    def supports(self, mime_type: str | None, filename: str) -> bool:
        if mime_type and mime_type.lower() in _LW_MIME_PREFIXES:
            return True
        ext = Path(filename).suffix.lower() if filename else ""
        return ext in _LW_EXTENSIONS

    def extract(
        self, blob_path: Path, mime_type: str | None
    ) -> ExtractedText:
        raise NotImplementedError("per-format dispatch added in tasks 7-10")
```

- [ ] **Step 4: Run test, verify it passes**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_extractor.py -v
```

Expected: all 4 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/localmail/search/extractor.py tests/test_extractor.py
git commit -m "feat(phase2): extractor protocol + LightweightExtractor skeleton

ExtractedText, ExtractorError, AttachmentExtractor Protocol, and a
LightweightExtractor with .supports() wired against the MIME/extension
allowlist. Per-format extraction added in subsequent tasks."
```

---

### Task 7: LightweightExtractor — PDF (pypdf)

**Files:**
- Modify: `src/localmail/search/extractor.py`
- Test: `tests/test_extractor.py` (extend)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_extractor.py`:

```python
import io


def _build_native_pdf(text: str) -> bytes:
    """Build a single-page text PDF in memory using reportlab."""
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.drawString(72, 720, text)
    c.showPage()
    c.save()
    return buf.getvalue()


def test_lightweight_extracts_native_pdf(tmp_path):
    pdf_bytes = _build_native_pdf("hello attachment world")
    p = tmp_path / "a.pdf"
    p.write_bytes(pdf_bytes)

    lw = LightweightExtractor()
    result = lw.extract(p, "application/pdf")

    assert "hello attachment world" in result.text
    assert result.extractor == "lightweight@1.0"
    assert result.page_count == 1


def test_lightweight_returns_empty_on_scanned_pdf(tmp_path):
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


def test_lightweight_raises_on_encrypted_pdf(tmp_path):
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
```

- [ ] **Step 2: Run tests, verify they fail**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_extractor.py -v
```

Expected: the three new tests FAIL with `NotImplementedError`.

- [ ] **Step 3: Implement PDF dispatch**

In `src/localmail/search/extractor.py`, replace `LightweightExtractor.extract` with dispatching logic and add a `_extract_pdf` helper:

```python
    def extract(
        self, blob_path: Path, mime_type: str | None
    ) -> ExtractedText:
        ext = blob_path.suffix.lower()
        mt = (mime_type or "").lower()

        if mt == "application/pdf" or ext == ".pdf":
            return self._extract_pdf(blob_path)

        raise NotImplementedError(
            f"per-format dispatch for {mt!r}/{ext!r} added in subsequent tasks"
        )

    def _extract_pdf(self, blob_path: Path) -> ExtractedText:
        import pypdf
        try:
            reader = pypdf.PdfReader(str(blob_path))
        except Exception as exc:
            raise ExtractorError(f"pypdf failed to open: {exc}") from exc

        if reader.is_encrypted:
            raise ExtractorError("pypdf: encrypted PDF (no password supplied)")

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
```

- [ ] **Step 4: Run tests, verify PASS**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_extractor.py -v
```

Expected: all PDF tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/localmail/search/extractor.py tests/test_extractor.py
git commit -m "feat(phase2): LightweightExtractor — PDF (pypdf)

Native-text PDFs extract via pypdf. Encrypted PDFs raise ExtractorError.
Scanned PDFs return empty text — caller falls back to docling on PDFs
in subsequent tasks."
```

---

### Task 8: LightweightExtractor — Office formats (DOCX, XLSX, PPTX)

**Files:**
- Modify: `src/localmail/search/extractor.py`
- Test: `tests/test_extractor.py` (extend)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_extractor.py`:

```python
def test_lightweight_extracts_docx(tmp_path):
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


def test_lightweight_extracts_xlsx(tmp_path):
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


def test_lightweight_extracts_pptx(tmp_path):
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
```

- [ ] **Step 2: Run tests, verify they fail**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_extractor.py::test_lightweight_extracts_docx tests/test_extractor.py::test_lightweight_extracts_xlsx tests/test_extractor.py::test_lightweight_extracts_pptx -v
```

Expected: three FAIL with `NotImplementedError`.

- [ ] **Step 3: Implement Office dispatch**

In `src/localmail/search/extractor.py`, extend the `extract` method's dispatch and add three private methods:

Change the dispatch chain in `extract` to:

```python
    def extract(
        self, blob_path: Path, mime_type: str | None
    ) -> ExtractedText:
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
```

Add the three private methods:

```python
    def _extract_docx(self, blob_path: Path) -> ExtractedText:
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
        import openpyxl
        try:
            wb = openpyxl.load_workbook(str(blob_path), read_only=True, data_only=True)
        except Exception as exc:
            raise ExtractorError(f"openpyxl failed to open: {exc}") from exc

        parts: list[str] = []
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
        from pptx import Presentation
        try:
            prs = Presentation(str(blob_path))
        except Exception as exc:
            raise ExtractorError(f"python-pptx failed to open: {exc}") from exc

        parts: list[str] = []
        for slide in prs.slides:
            for shape in slide.shapes:
                if shape.has_text_frame:
                    for para in shape.text_frame.paragraphs:
                        for run in para.runs:
                            if run.text:
                                parts.append(run.text)
            if slide.has_notes_slide:
                notes = slide.notes_slide.notes_text_frame.text
                if notes:
                    parts.append(notes)

        text = "\n".join(parts).strip()
        return ExtractedText(
            text=text, page_count=None,
            extractor=f"{self.name}@{self.version}",
        )
```

- [ ] **Step 4: Run tests, verify PASS**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_extractor.py -v
```

Expected: all Office tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/localmail/search/extractor.py tests/test_extractor.py
git commit -m "feat(phase2): LightweightExtractor — Office formats

DOCX (python-docx), XLSX (openpyxl read-only, multi-sheet), PPTX
(python-pptx incl. speaker notes)."
```

---

### Task 9: LightweightExtractor — Text formats (TXT, MD, HTML, CSV, RTF)

**Files:**
- Modify: `src/localmail/search/extractor.py`
- Test: `tests/test_extractor.py` (extend)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_extractor.py`:

```python
def test_lightweight_extracts_txt_utf8(tmp_path):
    p = tmp_path / "a.txt"
    p.write_text("plain text content über alles", encoding="utf-8")

    lw = LightweightExtractor()
    result = lw.extract(p, "text/plain")
    assert "plain text content über alles" == result.text


def test_lightweight_extracts_txt_latin1(tmp_path):
    p = tmp_path / "a.txt"
    p.write_bytes("naïve résumé".encode("latin-1"))

    lw = LightweightExtractor()
    result = lw.extract(p, "text/plain")
    assert "naïve résumé" == result.text


def test_lightweight_extracts_md(tmp_path):
    p = tmp_path / "a.md"
    p.write_text("# Header\n\nbody text", encoding="utf-8")

    lw = LightweightExtractor()
    result = lw.extract(p, "text/markdown")
    assert "# Header" in result.text
    assert "body text" in result.text


def test_lightweight_extracts_html(tmp_path):
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


def test_lightweight_extracts_csv(tmp_path):
    p = tmp_path / "a.csv"
    p.write_text("name,city\nalice,Berlin\nbob,Madrid\n", encoding="utf-8")

    lw = LightweightExtractor()
    result = lw.extract(p, "text/csv")
    assert "alice" in result.text
    assert "Berlin" in result.text


def test_lightweight_extracts_rtf(tmp_path):
    p = tmp_path / "a.rtf"
    p.write_text(
        r"{\rtf1\ansi\deff0 {\fonttbl {\f0 Helvetica;}}"
        r"\f0\fs24 RTF body content here.\par}",
        encoding="ascii",
    )

    lw = LightweightExtractor()
    result = lw.extract(p, "application/rtf")
    assert "RTF body content here" in result.text
```

- [ ] **Step 2: Run tests, verify they fail**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_extractor.py -v -k "txt_ or _md or _html or _csv or _rtf"
```

Expected: six FAIL.

- [ ] **Step 3: Implement text-format dispatch**

In `src/localmail/search/extractor.py`, extend `extract` dispatch:

```python
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
```

(Add these before the final `raise NotImplementedError`.)

Add the private methods:

```python
    def _extract_txt(self, blob_path: Path) -> ExtractedText:
        raw = blob_path.read_bytes()
        import chardet
        det = chardet.detect(raw) or {}
        encoding = det.get("encoding") or "utf-8"
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
        # Markdown is left as-is — chunking/embeddings handle it fine.
        return self._extract_txt(blob_path)

    def _extract_html(self, blob_path: Path) -> ExtractedText:
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
        import csv
        try:
            with blob_path.open("r", encoding="utf-8", errors="replace", newline="") as f:
                rows = [" ".join(r) for r in csv.reader(f)]
        except Exception as exc:
            raise ExtractorError(f"csv read failed: {exc}") from exc
        return ExtractedText(
            text="\n".join(rows).strip(),
            page_count=None,
            extractor=f"{self.name}@{self.version}",
        )

    def _extract_rtf(self, blob_path: Path) -> ExtractedText:
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
```

- [ ] **Step 4: Run tests, verify PASS**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_extractor.py -v
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/localmail/search/extractor.py tests/test_extractor.py
git commit -m "feat(phase2): LightweightExtractor — text formats

TXT (chardet-detected encoding), MD (identity), HTML (html2text), CSV
(stdlib csv), RTF (striprtf)."
```

---

### Task 10: LightweightExtractor — ODT + ICS

**Files:**
- Modify: `src/localmail/search/extractor.py`
- Test: `tests/test_extractor.py` (extend)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_extractor.py`:

```python
def test_lightweight_extracts_odt(tmp_path):
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


def test_lightweight_extracts_ics(tmp_path):
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
```

- [ ] **Step 2: Run tests, verify they fail**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_extractor.py -v -k "_odt or _ics"
```

Expected: both FAIL with `NotImplementedError`.

- [ ] **Step 3: Implement ODT + ICS dispatch**

In `extract` method dispatch, add before the final NotImplementedError:

```python
        if "opendocument.text" in mt or ext == ".odt":
            return self._extract_odt(blob_path)
        if mt == "text/calendar" or ext == ".ics":
            return self._extract_ics(blob_path)
```

Add the two private methods:

```python
    def _extract_odt(self, blob_path: Path) -> ExtractedText:
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
                for attendee in component.get("ATTENDEE", []) or []:
                    parts.append(str(attendee))

        return ExtractedText(
            text="\n".join(parts).strip(),
            page_count=event_count or None,
            extractor=f"{self.name}@{self.version}",
        )
```

- [ ] **Step 4: Run tests, verify PASS**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_extractor.py -v
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/localmail/search/extractor.py tests/test_extractor.py
git commit -m "feat(phase2): LightweightExtractor — ODT + ICS

ODT via odfpy paragraph walk. ICS via icalendar: SUMMARY +
DESCRIPTION + LOCATION + DTSTART + ATTENDEE concatenated as text;
page_count carries event count."
```

---

### Task 11: DoclingExtractor (lazy import + one-shot WARN)

**Files:**
- Modify: `src/localmail/search/extractor.py`
- Test: `tests/test_extractor.py` (extend)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_extractor.py`:

```python
import importlib
import logging


def test_docling_import_warning_one_shot(caplog, monkeypatch):
    """When docling is missing, the warn-once helper fires WARN exactly once
    per process even when called multiple times."""
    import localmail.search.extractor as ext_mod

    monkeypatch.setattr(ext_mod, "_DOCLING_WARNED", False, raising=False)
    monkeypatch.setattr(ext_mod, "_try_import_docling", lambda: None)

    with caplog.at_level(logging.WARNING, logger="localmail.search.extractor"):
        ext_mod.warn_docling_missing()
        ext_mod.warn_docling_missing()
        ext_mod.warn_docling_missing()

    warn_messages = [r for r in caplog.records
                     if r.levelno == logging.WARNING
                     and "extraction" in r.getMessage().lower()]
    assert len(warn_messages) == 1


def test_docling_extractor_supports_pdf_only():
    from localmail.search.extractor import DoclingExtractor
    de = DoclingExtractor()
    assert de.supports("application/pdf", "x.pdf")
    assert de.supports(None, "x.pdf")
    assert not de.supports("text/plain", "x.txt")
    assert not de.supports("image/png", "x.png")
```

- [ ] **Step 2: Run tests, verify they fail**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_extractor.py -v -k "docling"
```

Expected: FAIL with attribute / import errors.

- [ ] **Step 3: Add DoclingExtractor + warn-once helper**

At the bottom of `src/localmail/search/extractor.py`, add:

```python
# --- DoclingExtractor ---

import logging

_LOG = logging.getLogger(__name__)
_DOCLING_WARNED = False


def _try_import_docling():
    """Return the docling DocumentConverter class, or None if unavailable."""
    try:
        from docling.document_converter import DocumentConverter  # type: ignore
        return DocumentConverter
    except ImportError:
        return None


def warn_docling_missing() -> None:
    """Log the install-hint WARN exactly once per process."""
    global _DOCLING_WARNED
    if _DOCLING_WARNED:
        return
    _DOCLING_WARNED = True
    _LOG.warning(
        "docling is not installed; PDFs that lightweight cannot read "
        "will be marked as lightweight-empty. Install with "
        "`uv sync --extra extraction` to enable OCR fallback."
    )


class DoclingExtractor:
    name = "docling"
    version = "1.0"  # bumped automatically from package metadata at .extract() time

    def supports(self, mime_type: str | None, filename: str) -> bool:
        ext = Path(filename).suffix.lower() if filename else ""
        mt = (mime_type or "").lower()
        return mt == "application/pdf" or ext == ".pdf"

    def extract(
        self, blob_path: Path, mime_type: str | None
    ) -> ExtractedText:
        DocumentConverter = _try_import_docling()
        if DocumentConverter is None:
            raise ExtractorError(
                "docling not installed; install via `uv sync --extra extraction`"
            )
        try:
            from importlib.metadata import version as pkg_version
            self_version = pkg_version("docling")
        except Exception:
            self_version = self.version

        try:
            converter = DocumentConverter()
            result = converter.convert(str(blob_path))
        except Exception as exc:
            raise ExtractorError(f"docling.convert failed: {exc}") from exc

        try:
            text = result.document.export_to_markdown()
        except Exception as exc:
            raise ExtractorError(f"docling export_to_markdown failed: {exc}") from exc

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
```

- [ ] **Step 4: Run tests, verify PASS**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_extractor.py -v
```

Expected: all PASS. Note: an end-to-end OCR-pipeline test against a scanned PDF is performed by the acceptance harness (Task 23), since the docling first-run model download is heavy.

- [ ] **Step 5: Commit**

```bash
git add src/localmail/search/extractor.py tests/test_extractor.py
git commit -m "feat(phase2): DoclingExtractor with lazy import + one-shot WARN

Lazy-imports docling so it stays an optional [extraction] uv extra.
Version stamped from package metadata at extract time. One-shot
per-process WARN with install hint when docling is missing."
```

---

### Task 12: `chunk_attachment_text()` in chunking.py

**Files:**
- Modify: `src/localmail/search/chunking.py`
- Test: `tests/test_chunking.py` (extend)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_chunking.py`:

```python
def test_chunk_attachment_text_short_input_one_chunk():
    from localmail.config import SearchConfig
    from localmail.search.chunking import chunk_attachment_text

    cfg = SearchConfig()
    sha = b"\x01" * 32
    chunks = chunk_attachment_text(sha, "short text body", cfg)

    assert len(chunks) == 1
    assert chunks[0].kind == "attachment"
    assert chunks[0].chunk_idx == 0
    assert chunks[0].text == "short text body"
    assert chunks[0].token_count > 0


def test_chunk_attachment_text_long_input_multiple_chunks():
    from localmail.config import SearchConfig
    from localmail.search.chunking import chunk_attachment_text

    cfg = SearchConfig()
    # Build a 5000-word document — comfortably longer than the chunk size.
    long_text = "lorem ipsum dolor sit amet " * 1000
    sha = b"\x02" * 32
    chunks = chunk_attachment_text(sha, long_text, cfg)

    assert len(chunks) > 1
    indices = [c.chunk_idx for c in chunks]
    assert indices == list(range(len(chunks)))
    for c in chunks:
        assert c.kind == "attachment"
        assert c.text  # non-empty


def test_chunk_attachment_text_truncates_at_max_extracted_chars():
    from localmail.config import SearchConfig
    from localmail.search.chunking import chunk_attachment_text

    cfg = SearchConfig(extractor_max_extracted_chars=200)
    sha = b"\x03" * 32
    long_text = "x " * 5000  # 10 000 chars
    chunks = chunk_attachment_text(sha, long_text, cfg)

    full = "\n".join(c.text for c in chunks)
    # Length cap honored (allow some marker-line slack)
    assert len(full) <= cfg.extractor_max_extracted_chars + 50
    # Truncation marker present somewhere
    assert any("[truncated]" in c.text for c in chunks)


def test_chunk_attachment_text_normalizes_whitespace():
    from localmail.config import SearchConfig
    from localmail.search.chunking import chunk_attachment_text

    cfg = SearchConfig()
    sha = b"\x04" * 32
    messy = "line one\n\n\n\n\nline   two\t\t\tline three"
    chunks = chunk_attachment_text(sha, messy, cfg)

    text = chunks[0].text
    assert "\n\n\n" not in text
    assert "   " not in text
```

- [ ] **Step 2: Run tests, verify they fail**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_chunking.py -v -k "chunk_attachment_text"
```

Expected: FAIL with `ImportError: cannot import name 'chunk_attachment_text'`.

- [ ] **Step 3: Implement `chunk_attachment_text`**

In `src/localmail/search/chunking.py`, add (at the bottom, after the existing `chunk_message`):

```python
def _normalize_whitespace(text: str) -> str:
    """Collapse runs of whitespace; preserve paragraph breaks."""
    import re
    text = re.sub(r"\t", " ", text)
    text = re.sub(r" {2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text


def chunk_attachment_text(
    sha256: bytes,
    text: str,
    cfg: SearchConfig,
) -> list[ChunkSpec]:
    """Token-aware chunking for extracted attachment text. Pure function.

    Applies whitespace normalization, truncates at
    `cfg.extractor_max_extracted_chars` (appending a `[truncated]` marker),
    then splits using the same token budget as `chunk_message` body chunks.
    Returns ChunkSpec rows with kind='attachment'.
    """
    text = _normalize_whitespace(text or "").strip()
    if not text:
        return []

    truncated = False
    if len(text) > cfg.extractor_max_extracted_chars:
        text = text[: cfg.extractor_max_extracted_chars]
        truncated = True

    pieces = split_by_tokens(
        text,
        size=cfg.chunk_target_tokens,
        overlap=cfg.chunk_overlap_tokens,
    )
    if truncated and pieces:
        pieces[-1] = pieces[-1] + "\n[truncated]"

    chunks: list[ChunkSpec] = []
    for idx, piece in enumerate(pieces):
        chunks.append(
            ChunkSpec(
                kind="attachment",
                chunk_idx=idx,
                text=piece,
                token_count=_count_tokens(piece),
            )
        )
    return chunks
```

(If `_count_tokens` is not already a private helper in `chunking.py`, use whatever existing helper Phase 1 uses for `chunk_message`'s `token_count`. Search for the existing pattern with `grep -n "token_count" src/localmail/search/chunking.py`. If `_count_tokens` is not defined, replace the call with the existing token-counting expression.)

- [ ] **Step 4: Run tests, verify PASS**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_chunking.py -v
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/localmail/search/chunking.py tests/test_chunking.py
git commit -m "feat(phase2): chunk_attachment_text — pure chunker for attachment text

Whitespace normalization + truncation at extractor_max_extracted_chars
(with [truncated] marker) + token-budget split via the existing
split_by_tokens helper. Emits ChunkSpec rows with kind='attachment'."
```

---

### Task 13: `extract_worker.run_extract_worker_once` — single batch

**Files:**
- Create: `src/localmail/search/extract_worker.py`
- Test: `tests/test_extract_worker.py` (new)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_extract_worker.py`:

```python
"""Tests for extract_worker — text/empty/raised flow + SAVEPOINT discipline."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from localmail.config import SearchConfig
from localmail.search.extract_worker import run_extract_worker_once


def _seed_blob(db_conn, content: bytes, mime_type: str, attachments_root: Path,
               filename: str = "att.bin") -> bytes:
    """Insert a blob row + write the bytes to disk; return sha256."""
    sha = hashlib.sha256(content).digest()
    sub = sha.hex()
    blob_path = attachments_root / "blobs" / sub[:2] / sub[2:4] / sub
    blob_path.parent.mkdir(parents=True, exist_ok=True)
    blob_path.write_bytes(content)

    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO attachment_blobs (sha256, path, mime_type, size_bytes) "
            "VALUES (%s, %s, %s, %s)",
            (sha, str(blob_path), mime_type, len(content)),
        )
    db_conn.commit()
    return sha


def test_extract_worker_processes_plain_text(db_conn, tmp_path):
    sha = _seed_blob(db_conn, b"the quick brown fox", "text/plain", tmp_path, "a.txt")
    cfg = SearchConfig()

    wrote = run_extract_worker_once(db_conn, cfg)

    assert wrote == 1
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT extractor, extracted_text FROM attachment_text WHERE sha256 = %s",
            (sha,),
        )
        row = cur.fetchone()
    assert row is not None
    extractor, text = row
    assert extractor == "lightweight@1.0"
    assert "the quick brown fox" in text


def test_extract_worker_skips_non_allowlist_blob(db_conn, tmp_path):
    sha = _seed_blob(db_conn, b"\x00\x01\x02", "image/png", tmp_path, "logo.png")
    cfg = SearchConfig()

    wrote = run_extract_worker_once(db_conn, cfg)

    assert wrote == 0
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM attachment_text WHERE sha256 = %s", (sha,))
        assert cur.fetchone()[0] == 0


def test_extract_worker_inserts_size_skipped_sentinel(db_conn, tmp_path):
    payload = b"x" * (1024 * 1024)
    cfg = SearchConfig(extractor_max_blob_bytes=100)  # tiny cap to force skip
    sha = _seed_blob(db_conn, payload, "text/plain", tmp_path, "big.txt")

    run_extract_worker_once(db_conn, cfg)

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT extractor, extracted_text FROM attachment_text WHERE sha256 = %s",
            (sha,),
        )
        row = cur.fetchone()
    assert row == ("size-skipped", "")


def test_extract_worker_records_failure_on_lightweight_raise(db_conn, tmp_path):
    # An RTF that won't parse: striprtf typically returns garbage rather than
    # raising, so we use a corrupt PDF (truncated bytes) which DOES raise.
    sha = _seed_blob(
        db_conn, b"%PDF-1.4\nthis is not a valid PDF body",
        "application/pdf", tmp_path, "corrupt.pdf",
    )
    cfg = SearchConfig()

    run_extract_worker_once(db_conn, cfg)

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT extractor, error_class FROM failed_extractions WHERE sha256 = %s",
            (sha,),
        )
        row = cur.fetchone()
    # Either lightweight raised then docling not installed → failed_extractions,
    # OR docling installed and also raised → failed_extractions.
    assert row is not None
    assert row[0] in ("lightweight", "docling")


def test_extract_worker_sentinel_for_lightweight_empty_non_pdf(db_conn, tmp_path):
    sha = _seed_blob(db_conn, b"", "text/plain", tmp_path, "empty.txt")
    cfg = SearchConfig()

    run_extract_worker_once(db_conn, cfg)

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT extractor FROM attachment_text WHERE sha256 = %s", (sha,)
        )
        assert cur.fetchone() == ("lightweight-empty",)


def test_extract_worker_excludes_blobs_with_max_retries(db_conn, tmp_path):
    sha = _seed_blob(
        db_conn, b"%PDF-1.4\nstill broken",
        "application/pdf", tmp_path, "broken.pdf",
    )
    cfg = SearchConfig(extract_worker_max_retries=1)

    # First pass: lands in failed_extractions with retry_count=0
    run_extract_worker_once(db_conn, cfg)
    # Second pass: retry_count bumps to 1 — still eligible since 1 >= 1 excludes
    # only at the next attempt. The condition is `retry_count < max_retries`.
    run_extract_worker_once(db_conn, cfg)

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT retry_count FROM failed_extractions WHERE sha256 = %s", (sha,)
        )
        retry_count = cur.fetchone()[0]

    # After two passes the count is at least 1; the third pass should NOT
    # increment because retry_count >= max_retries excludes the blob.
    third = run_extract_worker_once(db_conn, cfg)
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT retry_count FROM failed_extractions WHERE sha256 = %s", (sha,)
        )
        retry_count_after = cur.fetchone()[0]
    assert retry_count_after == retry_count
```

- [ ] **Step 2: Run tests, verify they fail**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_extract_worker.py -v
```

Expected: FAIL with `ModuleNotFoundError: localmail.search.extract_worker`.

- [ ] **Step 3: Implement `run_extract_worker_once`**

Create `src/localmail/search/extract_worker.py`:

```python
"""Attachment extraction worker.

Polls attachment_blobs for MIME-allowlisted blobs without an
attachment_text row, runs LightweightExtractor first and DoclingExtractor
as a PDF-only fallback, and writes attachment_text rows (or sentinel /
failed_extractions rows per the per-blob decision tree).

Mirrors the existing embed_worker pattern: per-blob SAVEPOINT, nested
SAVEPOINT around failure recording so a logging failure can't kill the
outer transaction.
"""

from __future__ import annotations

import logging
import traceback as tb_mod
from pathlib import Path

from localmail.config import SearchConfig
from localmail.search.extractor import (
    DoclingExtractor,
    ExtractedText,
    ExtractorError,
    LightweightExtractor,
    _try_import_docling,
    warn_docling_missing,
)

_LOG = logging.getLogger(__name__)


def _is_allowlisted(
    mime_type: str | None, path: str, cfg: SearchConfig
) -> bool:
    mt = (mime_type or "").lower()
    if mt in (m.lower() for m in cfg.extractor_mime_allowlist):
        return True
    ext = Path(path).suffix.lower()
    if ext in (e.lower() for e in cfg.extractor_extension_allowlist):
        return True
    return False


def _is_pdf(mime_type: str | None, path: str) -> bool:
    mt = (mime_type or "").lower()
    return mt == "application/pdf" or Path(path).suffix.lower() == ".pdf"


def _claim_batch(conn, cfg: SearchConfig) -> list[tuple]:
    """Select up to extract_worker_batch_size eligible blobs."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT b.sha256, b.path, b.mime_type, b.size_bytes
            FROM attachment_blobs b
            LEFT JOIN attachment_text t USING (sha256)
            LEFT JOIN failed_extractions f USING (sha256)
            WHERE t.sha256 IS NULL
              AND (f.sha256 IS NULL OR f.retry_count < %s)
            ORDER BY b.first_seen_at
            LIMIT %s
            """,
            (cfg.extract_worker_max_retries, cfg.extract_worker_batch_size),
        )
        return list(cur.fetchall())


def _record_failure(
    conn,
    sha256: bytes,
    extractor_name: str,
    exc: BaseException,
) -> None:
    """Upsert into failed_extractions. Nested SAVEPOINT inside the caller."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO failed_extractions
                (sha256, extractor, error_class, error_message, traceback,
                 retry_count, last_retry_at)
            VALUES (%s, %s, %s, %s, %s, 0, now())
            ON CONFLICT (sha256) DO UPDATE
                SET extractor = EXCLUDED.extractor,
                    error_class = EXCLUDED.error_class,
                    error_message = EXCLUDED.error_message,
                    traceback = EXCLUDED.traceback,
                    retry_count = failed_extractions.retry_count + 1,
                    last_retry_at = now()
            """,
            (
                sha256,
                extractor_name,
                type(exc).__name__,
                str(exc),
                "".join(tb_mod.format_exception(type(exc), exc, exc.__traceback__)),
            ),
        )


def _insert_attachment_text(
    conn, sha256: bytes, et: ExtractedText
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO attachment_text "
            "(sha256, extractor, extracted_text, page_count) "
            "VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (sha256) DO NOTHING",
            (sha256, et.extractor, et.text, et.page_count),
        )


def _process_blob(
    conn,
    sha256: bytes,
    path: str,
    mime_type: str | None,
    size_bytes: int,
    cfg: SearchConfig,
    lw: LightweightExtractor,
    dl: DoclingExtractor,
) -> bool:
    """Process one blob. Returns True if attachment_text was written."""
    if size_bytes > cfg.extractor_max_blob_bytes:
        _insert_attachment_text(
            conn, sha256,
            ExtractedText(text="", page_count=None, extractor="size-skipped"),
        )
        return True

    blob_path = Path(path)
    lw_text: ExtractedText | None = None
    lw_raised: BaseException | None = None
    try:
        lw_text = lw.extract(blob_path, mime_type)
    except Exception as exc:
        lw_raised = exc

    if lw_text is not None and lw_text.text:
        _insert_attachment_text(conn, sha256, lw_text)
        return True

    is_pdf = _is_pdf(mime_type, path)
    docling_avail = _try_import_docling() is not None

    if is_pdf and docling_avail:
        try:
            dl_text = dl.extract(blob_path, mime_type)
        except Exception as exc:
            _record_failure(conn, sha256, "docling", exc)
            return False

        if dl_text.text:
            _insert_attachment_text(conn, sha256, dl_text)
            return True

        # Docling returned empty
        if lw_raised is not None:
            _record_failure(conn, sha256, "lightweight", lw_raised)
            return False
        _insert_attachment_text(
            conn, sha256,
            ExtractedText(text="", page_count=None, extractor="lightweight-empty"),
        )
        return True

    # Non-PDF or docling missing
    if is_pdf and not docling_avail:
        warn_docling_missing()

    if lw_raised is not None:
        _record_failure(conn, sha256, "lightweight", lw_raised)
        return False

    _insert_attachment_text(
        conn, sha256,
        ExtractedText(text="", page_count=None, extractor="lightweight-empty"),
    )
    return True


def run_extract_worker_once(conn, cfg: SearchConfig) -> int:
    """Run one pass of the extraction worker. Returns count of blobs touched
    (attachment_text rows written OR failed_extractions rows written)."""
    if not cfg.run_extract_worker:
        return 0

    batch = _claim_batch(conn, cfg)
    if not batch:
        return 0

    lw = LightweightExtractor()
    dl = DoclingExtractor()
    touched = 0

    for sha256, path, mime_type, size_bytes in batch:
        if not _is_allowlisted(mime_type, path, cfg):
            # Skip silently; embed_worker will not pick it up either.
            continue
        with conn.cursor() as cur:
            cur.execute("SAVEPOINT extract_blob")
        try:
            wrote = _process_blob(
                conn, sha256, path, mime_type, size_bytes, cfg, lw, dl
            )
            with conn.cursor() as cur:
                cur.execute("RELEASE SAVEPOINT extract_blob")
            if wrote:
                touched += 1
            else:
                # Failure already recorded inside _process_blob via a separate
                # _record_failure path that runs after the rollback below.
                touched += 1
        except Exception as exc:
            with conn.cursor() as cur:
                cur.execute("ROLLBACK TO SAVEPOINT extract_blob")
                cur.execute("SAVEPOINT extract_fail_log")
            try:
                _record_failure(conn, sha256, "lightweight", exc)
                with conn.cursor() as cur:
                    cur.execute("RELEASE SAVEPOINT extract_fail_log")
                touched += 1
            except Exception:
                with conn.cursor() as cur:
                    cur.execute("ROLLBACK TO SAVEPOINT extract_fail_log")
                _LOG.exception("failed to record extraction failure for %s",
                               sha256.hex())

    conn.commit()
    return touched
```

- [ ] **Step 4: Run tests, verify PASS**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_extract_worker.py -v
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/localmail/search/extract_worker.py tests/test_extract_worker.py
git commit -m "feat(phase2): extract_worker — single-batch run + decision tree

Per-blob SAVEPOINT discipline mirrors embed_worker. text/empty/raised
classification from lightweight, PDF-only docling fallback, sentinel
rows for lightweight-empty + size-skipped, failed_extractions upsert
with retry_count bump, batch-level rollback safety."
```

---

### Task 14: Extend `embed_worker` to chunk attachment_text

**Files:**
- Modify: `src/localmail/search/embed_worker.py`
- Test: `tests/test_embed_worker.py` (extend)

- [ ] **Step 1: Write the failing test**

Open `tests/test_embed_worker.py`. Append:

```python
def test_embed_worker_chunks_attachment_text(db_conn):
    """When attachment_text rows exist without corresponding attachment_chunks,
    the next embed_worker pass chunks them. Embeddings remain NULL until the
    second pass (which is fine — we only verify chunking here)."""
    import hashlib
    from localmail.config import SearchConfig
    from localmail.search.embed_worker import run_embed_worker_once
    from localmail.search.embeddings import FastEmbedBackend

    # Seed an attachment_blob + attachment_text row directly (extract_worker
    # output) without going through extract_worker.
    sha = hashlib.sha256(b"unique blob bytes").digest()
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO attachment_blobs (sha256, path, mime_type, size_bytes) "
            "VALUES (%s, %s, %s, %s)",
            (sha, "/nonexistent/path", "text/plain", 100),
        )
        cur.execute(
            "INSERT INTO attachment_text (sha256, extractor, extracted_text) "
            "VALUES (%s, %s, %s)",
            (sha, "lightweight@1.0", "this is the extracted body of an attachment"),
        )
    db_conn.commit()

    cfg = SearchConfig()
    # Use a fake backend that returns zero vectors so we don't touch fastembed.
    class _FakeBackend:
        def embed_documents(self, texts):
            return [[0.0] * 768 for _ in texts]
        def embed_query(self, text):
            return [0.0] * 768

    run_embed_worker_once(db_conn, cfg, _FakeBackend())

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM attachment_chunks WHERE sha256 = %s", (sha,)
        )
        n = cur.fetchone()[0]
    assert n >= 1


def test_embed_worker_skips_sentinel_attachment_text(db_conn):
    """attachment_text rows with extracted_text='' should produce zero chunks."""
    import hashlib
    from localmail.config import SearchConfig
    from localmail.search.embed_worker import run_embed_worker_once

    sha = hashlib.sha256(b"empty marker").digest()
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO attachment_blobs (sha256, path, mime_type, size_bytes) "
            "VALUES (%s, %s, %s, %s)",
            (sha, "/nope", "text/plain", 0),
        )
        cur.execute(
            "INSERT INTO attachment_text (sha256, extractor, extracted_text) "
            "VALUES (%s, %s, %s)",
            (sha, "lightweight-empty", ""),
        )
    db_conn.commit()

    cfg = SearchConfig()
    class _FakeBackend:
        def embed_documents(self, texts): return [[0.0] * 768 for _ in texts]
        def embed_query(self, text): return [0.0] * 768

    run_embed_worker_once(db_conn, cfg, _FakeBackend())

    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM attachment_chunks WHERE sha256 = %s", (sha,))
        assert cur.fetchone()[0] == 0
```

- [ ] **Step 2: Run tests, verify they fail**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_embed_worker.py -v -k "attachment"
```

Expected: both FAIL (attachment_chunks rows are not created).

- [ ] **Step 3: Extend the lazy-chunking pass in embed_worker**

Open `src/localmail/search/embed_worker.py`. Find `_chunk_messages_lazily` (or the equivalent function that scans `messages LEFT JOIN message_chunks`). Add a sibling function and call it from the worker pass:

```python
def _chunk_attachments_lazily(conn, cfg, batch: int) -> None:
    """Find attachment_text rows whose chunks have not been generated and
    chunk them. Skips sentinel rows where extracted_text=''."""
    from localmail.search.chunking import chunk_attachment_text
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT t.sha256, t.extracted_text
            FROM attachment_text t
            LEFT JOIN attachment_chunks c USING (sha256)
            WHERE t.extracted_text <> ''
              AND c.sha256 IS NULL
            LIMIT %s
            """,
            (batch,),
        )
        rows = cur.fetchall()

    for sha256, text in rows:
        with conn.cursor() as cur:
            cur.execute("SAVEPOINT chunk_blob")
        try:
            specs = chunk_attachment_text(sha256, text, cfg)
            with conn.cursor() as cur:
                for spec in specs:
                    cur.execute(
                        "INSERT INTO attachment_chunks "
                        "(sha256, chunk_idx, text, token_count) "
                        "VALUES (%s, %s, %s, %s)",
                        (sha256, spec.chunk_idx, spec.text, spec.token_count),
                    )
                cur.execute("RELEASE SAVEPOINT chunk_blob")
        except Exception:
            with conn.cursor() as cur:
                cur.execute("ROLLBACK TO SAVEPOINT chunk_blob")
            # Re-raise so the outer worker handles failure-logging consistently.
            raise
    conn.commit()
```

In `run_embed_worker_once`, after the existing `_chunk_messages_lazily(...)` call, add:

```python
    _chunk_attachments_lazily(conn, cfg, batch=max(cfg.embed_worker_batch_size, 50))
```

Also extend the existing embedding sweep so it picks up `attachment_chunks` rows with `embedding_v1 IS NULL`. The current sweep is keyed off `message_chunks`. Look for the SELECT that claims chunks `FOR UPDATE SKIP LOCKED`; duplicate the same pattern for `attachment_chunks`. Simplest implementation:

```python
def _embed_pending_chunks(conn, cfg, backend, *, chunk_table: str) -> int:
    """Embed up to embed_worker_batch_size chunks from the given table. Returns
    count embedded. Per-chunk SAVEPOINT; chunks that error land in
    failed_embeddings (Phase 1 pattern)."""
    # ... existing logic, parameterized on chunk_table ...
```

If the existing `_embed_pending_message_chunks` is hard-coded to `message_chunks`, refactor it into the parameterized version above and then call it twice from `run_embed_worker_once`:

```python
    embedded_msg = _embed_pending_chunks(conn, cfg, backend, chunk_table="message_chunks")
    embedded_att = _embed_pending_chunks(conn, cfg, backend, chunk_table="attachment_chunks")
    return embedded_msg + embedded_att
```

The chunk_table identifier is whitelisted (only literal `"message_chunks"` / `"attachment_chunks"` are passed) so dynamic SQL composition is safe.

- [ ] **Step 4: Run tests, verify PASS**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_embed_worker.py -v
```

Expected: all PASS, including previously-passing message_chunks tests.

- [ ] **Step 5: Commit**

```bash
git add src/localmail/search/embed_worker.py tests/test_embed_worker.py
git commit -m "feat(phase2): extend embed_worker to chunk + embed attachment_text

Lazy chunking sweep added for attachment_text rows. Embedding sweep
parameterized on chunk_table (message_chunks | attachment_chunks).
Sentinel rows (extracted_text='') produce zero chunks and are skipped."
```

---

### Task 15: `extract_worker.run_extract_worker` — daemon loop

**Files:**
- Modify: `src/localmail/search/extract_worker.py`
- Test: `tests/test_extract_worker.py` (extend)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_extract_worker.py`:

```python
import threading
import time


def test_run_extract_worker_drains_queue_then_idles(db_conn, tmp_path):
    """run_extract_worker should drain pending work and then block on the
    poll interval until stopped. We seed two blobs, start the loop, then
    set the stop event after observing both are extracted."""
    from localmail.search.extract_worker import run_extract_worker

    _seed_blob(db_conn, b"blob alpha content", "text/plain", tmp_path, "a.txt")
    _seed_blob(db_conn, b"blob beta content",  "text/plain", tmp_path, "b.txt")

    cfg = SearchConfig(extract_worker_poll_interval_s=1)
    stop = threading.Event()

    def _make_conn():
        import psycopg
        from tests.conftest import TEST_DSN
        return psycopg.connect(TEST_DSN)

    t = threading.Thread(
        target=run_extract_worker,
        kwargs={"conn_factory": _make_conn, "cfg": cfg, "stop_event": stop},
        daemon=True,
    )
    t.start()

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        with db_conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM attachment_text")
            if cur.fetchone()[0] >= 2:
                break
        time.sleep(0.1)

    stop.set()
    t.join(timeout=3)
    assert not t.is_alive()

    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM attachment_text")
        assert cur.fetchone()[0] == 2
```

- [ ] **Step 2: Run test, verify it fails**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_extract_worker.py::test_run_extract_worker_drains_queue_then_idles -v
```

Expected: FAIL with `ImportError: cannot import name 'run_extract_worker'`.

- [ ] **Step 3: Add the daemon loop**

In `src/localmail/search/extract_worker.py`, append at the bottom:

```python
import time
from typing import Callable


def run_extract_worker(
    *,
    conn_factory: Callable[[], "psycopg.Connection"],
    cfg: SearchConfig,
    stop_event,
) -> None:
    """Background loop: drains the extraction queue, then sleeps for
    `cfg.extract_worker_poll_interval_s`. Reconnects with exponential
    backoff (1s → 60s cap) on connection errors. Exits when `stop_event`
    is set."""
    backoff = 1.0
    while not stop_event.is_set():
        try:
            conn = conn_factory()
        except Exception:
            _LOG.warning("extract_worker: connect failed; backing off %.0fs", backoff)
            stop_event.wait(timeout=backoff)
            backoff = min(backoff * 2, 60.0)
            continue
        backoff = 1.0
        try:
            while not stop_event.is_set():
                touched = run_extract_worker_once(conn, cfg)
                if touched == 0:
                    break
            if stop_event.is_set():
                break
            stop_event.wait(timeout=cfg.extract_worker_poll_interval_s)
        except Exception:
            _LOG.exception("extract_worker: error during sweep")
            stop_event.wait(timeout=backoff)
            backoff = min(backoff * 2, 60.0)
        finally:
            try:
                conn.close()
            except Exception:
                pass
```

- [ ] **Step 4: Run test, verify PASS**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_extract_worker.py::test_run_extract_worker_drains_queue_then_idles -v
```

Expected: PASS within ~3 seconds.

- [ ] **Step 5: Commit**

```bash
git add src/localmail/search/extract_worker.py tests/test_extract_worker.py
git commit -m "feat(phase2): extract_worker daemon loop with backoff + clean stop

Drain-then-sleep loop with exponential backoff on connection errors
(1s -> 60s cap). Respects a threading.Event stop signal."
```

---

### Task 16: Arm 4 — `arm_vector_attachment_chunks`

**Files:**
- Modify: `src/localmail/search/arms.py`
- Test: `tests/test_arms.py` (extend)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_arms.py`:

```python
def test_arm_vector_attachment_chunks_returns_message_ids(db_conn):
    """Insert an attachment_blob + message that references it via the
    JSONB attachments column, plus an attachment_chunks row with a known
    embedding. Verify Arm 4 returns the message_id."""
    import hashlib
    import json
    from localmail.config import SearchConfig
    from localmail.search.arms import arm_vector_attachment_chunks
    from localmail.search.query import ParsedQuery, SearchFilters

    sha = hashlib.sha256(b"blob xyz").digest()
    sha_hex = sha.hex()

    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO accounts (name, email_address, imap_host, auth_method) "
            "VALUES ('a','e@x','h','password') RETURNING id"
        )
        acct_id = cur.fetchone()[0]

        cur.execute(
            "INSERT INTO attachment_blobs (sha256, path, mime_type, size_bytes) "
            "VALUES (%s, %s, %s, %s)",
            (sha, "/somewhere/foo.pdf", "application/pdf", 1000),
        )

        attachments = json.dumps([{"filename": "report.pdf", "sha256": sha_hex}])
        cur.execute(
            "INSERT INTO messages (account_id, message_id, raw_sha256, subject,"
            " body_text, headers, raw_bytes, size_bytes, attachments) "
            "VALUES (%s, %s, %s, %s, %s, '{}'::jsonb, %s, %s, %s::jsonb) "
            "RETURNING id",
            (acct_id, "<m1@x>", b"\x10"*32, "FYI", "see attached",
             b"raw", 3, attachments),
        )
        msg_id = cur.fetchone()[0]

        # Use a unit vector so cosine distance is predictable; the test
        # query vector is identical so distance = 0.
        unit = [0.0] * 768
        unit[0] = 1.0
        cur.execute(
            "INSERT INTO attachment_chunks "
            "(sha256, chunk_idx, text, token_count, embedding_v1, embedded_at) "
            "VALUES (%s, 0, %s, 10, %s::halfvec, now())",
            (sha, "attachment chunk text", unit),
        )
    db_conn.commit()

    cfg = SearchConfig()
    parsed = ParsedQuery(text="anything", filters=SearchFilters())
    hits = arm_vector_attachment_chunks(
        db_conn, parsed, cfg, qvec=unit, limit=10
    )

    assert len(hits) >= 1
    assert hits[0].message_id == msg_id


def test_arm_vector_attachment_chunks_fanout_cap_honored(db_conn):
    """A blob attached to N messages fans out to at most arm4_fanout_cap rows."""
    import hashlib, json
    from localmail.config import SearchConfig
    from localmail.search.arms import arm_vector_attachment_chunks
    from localmail.search.query import ParsedQuery, SearchFilters

    sha = hashlib.sha256(b"popular blob").digest()
    sha_hex = sha.hex()
    attachments = json.dumps([{"filename": "x.pdf", "sha256": sha_hex}])

    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO accounts (name, email_address, imap_host, auth_method) "
            "VALUES ('b','e@y','h','password') RETURNING id"
        )
        acct_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO attachment_blobs (sha256, path, mime_type, size_bytes) "
            "VALUES (%s, %s, %s, %s)",
            (sha, "/p", "application/pdf", 100),
        )
        for i in range(25):
            cur.execute(
                "INSERT INTO messages (account_id, message_id, raw_sha256, subject,"
                " body_text, headers, raw_bytes, size_bytes, attachments) "
                "VALUES (%s, %s, %s, %s, %s, '{}'::jsonb, %s, %s, %s::jsonb)",
                (acct_id, f"<m{i}@y>", bytes([i+50])*32, f"S{i}", "",
                 b"r", 1, attachments),
            )
        unit = [0.0]*768; unit[0] = 1.0
        cur.execute(
            "INSERT INTO attachment_chunks (sha256, chunk_idx, text, token_count,"
            " embedding_v1, embedded_at) "
            "VALUES (%s, 0, %s, 10, %s::halfvec, now())",
            (sha, "chunk", unit),
        )
    db_conn.commit()

    cfg = SearchConfig(arm4_fanout_cap=10)
    parsed = ParsedQuery(text="x", filters=SearchFilters())
    hits = arm_vector_attachment_chunks(db_conn, parsed, cfg, qvec=unit, limit=100)
    assert len(hits) <= 10
```

- [ ] **Step 2: Run tests, verify they fail**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_arms.py -v -k "attachment_chunks"
```

Expected: FAIL with `ImportError`.

- [ ] **Step 3: Implement `arm_vector_attachment_chunks`**

Append to `src/localmail/search/arms.py`:

```python
def arm_vector_attachment_chunks(
    conn,
    parsed: "ParsedQuery",
    cfg: SearchConfig,
    qvec: list[float],
    limit: int,
) -> list["ArmHit"]:
    """Arm 4 — vector cosine over attachment_chunks, JOINed to messages via
    JSONB containment on messages.attachments. Fan-out is capped per chunk
    by cfg.arm4_fanout_cap, ordered by message date_sent DESC."""
    from localmail.search.searcher import ArmHit

    filter_sql, filter_params = _filter_sql(parsed.filters)
    # We anchor the existing _filter_sql to the `m` alias used in the JOIN.

    chunk_limit = max(limit, 1) * 3  # fan-out headroom

    sql = f"""
    WITH ranked AS (
        SELECT c.id, c.sha256,
               c.embedding_v1 <=> %s::halfvec(768) AS dist
        FROM attachment_chunks c
        WHERE c.embedding_v1 IS NOT NULL
        ORDER BY c.embedding_v1 <=> %s::halfvec(768)
        LIMIT %s
    ),
    fanned AS (
        SELECT m.id AS message_id,
               r.id AS chunk_id,
               r.dist,
               ROW_NUMBER() OVER (
                   PARTITION BY r.id
                   ORDER BY m.date_sent DESC NULLS LAST
               ) AS rn
        FROM ranked r
        JOIN messages m
          ON m.attachments @> jsonb_build_array(
              jsonb_build_object('sha256', encode(r.sha256, 'hex'))
          )
        CROSS JOIN LATERAL jsonb_array_elements(m.attachments) elem
        WHERE elem ->> 'sha256' = encode(r.sha256, 'hex')
          {filter_sql}
    )
    SELECT message_id, chunk_id, dist
    FROM fanned
    WHERE rn <= %s
    ORDER BY dist
    LIMIT %s
    """

    params = [qvec, qvec, chunk_limit] + list(filter_params) + [
        cfg.arm4_fanout_cap, limit,
    ]

    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()

    out: list[ArmHit] = []
    for rank, (message_id, chunk_id, dist) in enumerate(rows, start=1):
        arm_score = float(1.0 - dist)  # higher is better
        out.append(
            ArmHit(
                message_id=message_id,
                chunk_id=chunk_id,
                chunk_table="attachment_chunks",
                rank=rank,
                arm_score=arm_score,
            )
        )
    return out
```

If `ArmHit` does not currently support `chunk_table='attachment_chunks'`, look in `src/localmail/search/searcher.py` where ArmHit is defined and confirm `chunk_table` is a free-form string. If it's a `Literal`, extend the Literal.

- [ ] **Step 4: Run tests, verify PASS**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_arms.py -v
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/localmail/search/arms.py tests/test_arms.py
git commit -m "feat(phase2): arm_vector_attachment_chunks — Arm 4 retrieval

Vector cosine over attachment_chunks; CROSS JOIN LATERAL fans the
JSONB attachments array out to carrying messages; ROW_NUMBER per chunk
caps fan-out at arm4_fanout_cap ordered by message date_sent DESC."
```

---

### Task 17: Searcher integration — add Arm 4 + attachment snippet wiring

**Files:**
- Modify: `src/localmail/search/searcher.py`
- Test: `tests/test_searcher.py` or `tests/test_search_public_api.py` (extend)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_search_public_api.py` (or `tests/test_searcher.py` if you prefer):

```python
def test_searcher_returns_attachment_snippet(db_conn):
    """When a query is best answered by attachment content, Searcher returns
    a SearchResult with snippet_source='attachment' and attachment_filename set."""
    import hashlib
    import json
    from localmail.config import LocalmailConfig, SearchConfig
    from localmail.db import open_pool
    from localmail.search.embeddings import FastEmbedBackend
    from localmail.search.searcher import Searcher
    from tests.conftest import TEST_DSN

    sha = hashlib.sha256(b"contract details").digest()
    sha_hex = sha.hex()
    attachments = json.dumps([{"filename": "contract.pdf", "sha256": sha_hex}])

    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO accounts (name, email_address, imap_host, auth_method) "
            "VALUES ('c','e@z','h','password') RETURNING id"
        )
        acct_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO attachment_blobs (sha256, path, mime_type, size_bytes) "
            "VALUES (%s, %s, %s, %s)",
            (sha, "/p", "application/pdf", 100),
        )
        cur.execute(
            "INSERT INTO messages (account_id, message_id, raw_sha256, subject,"
            " body_text, headers, raw_bytes, size_bytes, attachments) "
            "VALUES (%s, %s, %s, %s, %s, '{}'::jsonb, %s, %s, %s::jsonb) RETURNING id",
            (acct_id, "<contract@z>", b"\x20"*32, "FYI", "see attached",
             b"r", 1, attachments),
        )

    cfg = SearchConfig()
    backend = FastEmbedBackend(cfg)

    # Seed attachment_chunks with a vector that's close to the query.
    qvec = backend.embed_query("non-disclosure obligations under section 5")
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO attachment_chunks (sha256, chunk_idx, text, token_count, "
            "embedding_v1, embedded_at) VALUES (%s, 0, %s, 10, %s::halfvec, now())",
            (sha, "non-disclosure obligations under section 5", qvec),
        )
    db_conn.commit()

    pool = open_pool(TEST_DSN)
    try:
        searcher = Searcher(pool=pool, cfg=cfg, embeddings=backend,
                            reranker=None, rewriter=None)
        page = searcher.search("non-disclosure obligations", page_size=10)
    finally:
        pool.close()

    # At least one result should carry the attachment metadata.
    att_results = [r for r in page.results if r.snippet_source == "attachment"]
    assert att_results, f"expected at least one attachment snippet, got {[(r.subject, r.snippet_source) for r in page.results]}"
    assert att_results[0].attachment_filename == "contract.pdf"
```

- [ ] **Step 2: Run test, verify it fails**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_search_public_api.py::test_searcher_returns_attachment_snippet -v
```

Expected: FAIL — no attachment result because Searcher doesn't run Arm 4 yet.

- [ ] **Step 3: Wire Arm 4 into Searcher.search and snippet hydration**

In `src/localmail/search/searcher.py`, find the lazy import block where Arms 1-3 are imported and called (around the `arm_bm25_messages` / `arm_bm25_chunks` / `arm_vector_chunks` call site). Extend:

```python
        from localmail.search.arms import (
            arm_bm25_chunks, arm_bm25_messages, arm_vector_chunks,
            arm_vector_attachment_chunks,
        )
        a1 = arm_bm25_messages(conn, parsed, self._cfg, limit=candidates_per_arm)
        a2 = arm_bm25_chunks(conn, parsed, self._cfg, limit=candidates_per_arm)
        qvec = self._embeddings.embed_query(parsed.text)
        a3 = arm_vector_chunks(conn, parsed, self._cfg, qvec, limit=candidates_per_arm)
        a4 = arm_vector_attachment_chunks(conn, parsed, self._cfg, qvec, limit=candidates_per_arm)
        fused = rrf_fuse([a1, a2, a3, a4], k=self._cfg.rrf_k)
```

Now find the snippet-hydration block that translates `chunk_table` + `chunk_id` into snippet text (look for `snippet_source_text` near line 260-290). Currently it handles `chunk_table='message_chunks'`. Extend to also handle `'attachment_chunks'`:

```python
            elif chunk_table == "attachment_chunks":
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT c.text, c.sha256 "
                        "FROM attachment_chunks c WHERE c.id = %s",
                        (chunk_id,),
                    )
                    row = cur.fetchone()
                if row:
                    snip_text, sha_bytes = row
                    item["snippet_source_text"] = snip_text or ""
                    item["snippet_source_kind"] = "attachment"
                    item["attachment_sha256_hex"] = sha_bytes.hex()
```

In the result-construction block (look for the `SearchResult(...)` constructor around line 295-310 where `snippet_source` is set and `attachment_filename=None` is currently hard-coded), update to:

```python
            attachment_filename = None
            if item.get("snippet_source_kind") == "attachment":
                source = "attachment"
                # Look up the filename from the carrying message's attachments JSONB
                sha_hex = item.get("attachment_sha256_hex")
                if sha_hex:
                    with conn.cursor() as cur:
                        cur.execute(
                            "SELECT elem ->> 'filename' "
                            "FROM messages, jsonb_array_elements(attachments) elem "
                            "WHERE messages.id = %s AND elem ->> 'sha256' = %s "
                            "LIMIT 1",
                            (item["message_id"], sha_hex),
                        )
                        row = cur.fetchone()
                        if row and row[0]:
                            attachment_filename = row[0]
```

And pass `attachment_filename=attachment_filename` (rather than `None`) into the `SearchResult(...)` constructor.

- [ ] **Step 4: Run test, verify PASS**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_search_public_api.py::test_searcher_returns_attachment_snippet -v
```

Expected: PASS.

- [ ] **Step 5: Run the full search test suite (Phase 1 non-regression)**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_searcher.py tests/test_search_public_api.py tests/test_arms.py tests/test_rrf.py -v
```

Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add src/localmail/search/searcher.py tests/test_search_public_api.py
git commit -m "feat(phase2): Searcher integrates Arm 4 + attachment snippet wiring

Adds arm_vector_attachment_chunks to the arms list, hydrates snippets
from attachment_chunks.text, and resolves attachment_filename from the
carrying message's JSONB attachments column."
```

---

### Task 18: CLI — `extract-backfill` + `search-status` extension

**Files:**
- Modify: `src/localmail/cli.py`
- Test: `tests/test_cli_extract.py` (new)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_cli_extract.py`:

```python
import hashlib
import json

from click.testing import CliRunner

from localmail.cli import main


def test_cli_extract_backfill_drains_queue(db_conn, tmp_path, monkeypatch):
    sha = hashlib.sha256(b"cli extract content").digest()
    sub = sha.hex()
    blob_path = tmp_path / "blobs" / sub[:2] / sub[2:4] / sub
    blob_path.parent.mkdir(parents=True, exist_ok=True)
    blob_path.write_bytes(b"cli extract content")

    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO attachment_blobs (sha256, path, mime_type, size_bytes) "
            "VALUES (%s, %s, %s, %s)",
            (sha, str(blob_path), "text/plain", 19),
        )
    db_conn.commit()

    monkeypatch.setenv("LOCALMAIL_CONFIG", "/dev/null")  # adjust if cli loads config differently

    runner = CliRunner()
    result = runner.invoke(main, ["extract-backfill", "--no-progress"])
    assert result.exit_code == 0, result.output

    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM attachment_text WHERE sha256 = %s", (sha,))
        assert cur.fetchone()[0] == 1


def test_cli_search_status_reports_attachment_counts(db_conn):
    runner = CliRunner()
    result = runner.invoke(main, ["search-status", "--format", "json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert "blobs_eligible" in payload
    assert "blobs_extracted" in payload
    assert "blobs_pending" in payload
    assert "attachment_chunks_total" in payload
    assert "attachment_chunks_embedded" in payload
    assert "failed_extractions" in payload
```

Note: these tests assume `cli.py`'s `_dsn()` / `load_config()` resolves to the test DSN when run under conftest. If your CLI config loader requires more setup, adapt the monkeypatching to whatever the existing CLI tests do — look at `tests/test_cli_embed_backfill.py` for the pattern.

- [ ] **Step 2: Run tests, verify they fail**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_cli_extract.py -v
```

Expected: FAIL — commands don't exist yet.

- [ ] **Step 3: Add the CLI commands**

In `src/localmail/cli.py`, add (next to the existing `embed_backfill` command):

```python
@main.command("extract-backfill")
@click.option("--no-progress", is_flag=True)
def extract_backfill(no_progress):
    """Drain the attachment-extraction queue in the foreground; exit when empty.

    Account-agnostic — extracts text from all eligible blobs.
    """
    from localmail.db import open_pool
    from localmail.search.extract_worker import run_extract_worker_once
    cfg = load_config()
    pool = open_pool(_dsn())
    try:
        total = 0
        while True:
            with pool.connection() as conn:
                touched = run_extract_worker_once(conn, cfg.search)
            if touched == 0:
                break
            total += touched
            if not no_progress:
                click.echo(f"extracted {touched} blobs (total {total})", err=True)
    finally:
        pool.close()
    click.echo(f"done: {total} blobs processed")
```

Find the existing `search_status` command and extend the payload to add Phase 2 fields. The existing payload dict construction (look around `payload = {"messages_total": ...}`) should be augmented:

```python
            cur.execute(
                "SELECT count(*) FROM attachment_blobs b "
                "WHERE b.mime_type = ANY(%s) OR lower(substring(b.path FROM '\\.[^.]+$')) = ANY(%s)",
                (cfg.search.extractor_mime_allowlist, cfg.search.extractor_extension_allowlist),
            )
            row = cur.fetchone(); assert row is not None
            blobs_eligible = row[0]
            cur.execute(
                "SELECT count(*) FROM attachment_text WHERE extracted_text <> ''"
            )
            row = cur.fetchone(); assert row is not None
            blobs_extracted = row[0]
            blobs_pending = blobs_eligible - blobs_extracted
            cur.execute("SELECT count(*) FROM attachment_chunks")
            row = cur.fetchone(); assert row is not None
            attachment_chunks_total = row[0]
            cur.execute(
                "SELECT count(*) FROM attachment_chunks WHERE embedding_v1 IS NOT NULL"
            )
            row = cur.fetchone(); assert row is not None
            attachment_chunks_embedded = row[0]
            cur.execute("SELECT count(*) FROM failed_extractions")
            row = cur.fetchone(); assert row is not None
            failed_extractions = row[0]
```

Add these to the `payload = {...}` dict:

```python
        "blobs_eligible": blobs_eligible,
        "blobs_extracted": blobs_extracted,
        "blobs_pending": blobs_pending,
        "attachment_chunks_total": attachment_chunks_total,
        "attachment_chunks_embedded": attachment_chunks_embedded,
        "failed_extractions": failed_extractions,
```

(Make sure the `cfg = load_config()` call is at the top of `search_status` so `cfg.search.extractor_mime_allowlist` is available.)

- [ ] **Step 4: Run tests, verify PASS**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_cli_extract.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/localmail/cli.py tests/test_cli_extract.py
git commit -m "feat(phase2): CLI extract-backfill + search-status extension

extract-backfill drains the extraction queue in foreground.
search-status grows attachment-related counters."
```

---

### Task 19: CLI — `list-failed-extractions` + `retry-failed-extractions`

**Files:**
- Modify: `src/localmail/cli.py`
- Test: `tests/test_cli_extract.py` (extend)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_cli_extract.py`:

```python
def test_cli_list_failed_extractions(db_conn):
    sha = hashlib.sha256(b"x").digest()
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO attachment_blobs (sha256, path, mime_type, size_bytes) "
            "VALUES (%s, %s, %s, %s)",
            (sha, "/p", "application/pdf", 10),
        )
        cur.execute(
            "INSERT INTO failed_extractions "
            "(sha256, extractor, error_class, error_message, retry_count) "
            "VALUES (%s, 'lightweight', 'BadFile', 'broken', 0)",
            (sha,),
        )
    db_conn.commit()

    runner = CliRunner()
    result = runner.invoke(main, ["list-failed-extractions", "--format", "json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert len(payload) == 1
    assert payload[0]["error_class"] == "BadFile"
    assert payload[0]["extractor"] == "lightweight"


def test_cli_retry_failed_extractions_clears_rows(db_conn):
    sha = hashlib.sha256(b"y").digest()
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO attachment_blobs (sha256, path, mime_type, size_bytes) "
            "VALUES (%s, %s, %s, %s)",
            (sha, "/p", "application/pdf", 10),
        )
        cur.execute(
            "INSERT INTO failed_extractions "
            "(sha256, extractor, error_class, error_message, retry_count) "
            "VALUES (%s, 'lightweight', 'X', 'X', 0)",
            (sha,),
        )
    db_conn.commit()

    runner = CliRunner()
    result = runner.invoke(main, ["retry-failed-extractions"])
    assert result.exit_code == 0

    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM failed_extractions")
        assert cur.fetchone()[0] == 0
```

- [ ] **Step 2: Run tests, verify they fail**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_cli_extract.py -v -k "failed_extractions"
```

Expected: FAIL — commands don't exist.

- [ ] **Step 3: Add the commands**

In `src/localmail/cli.py`, add next to the existing `list_failed_embeddings` / `retry_failed_embeddings`:

```python
@main.command("list-failed-extractions")
@click.option("--limit", type=int, default=50)
@click.option("--format", "fmt", type=click.Choice(["text", "json"]), default="text")
def list_failed_extractions(limit, fmt):
    """Show recent failed_extractions rows."""
    from localmail.db import open_pool
    pool = open_pool(_dsn())
    try:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT encode(sha256,'hex'), extractor, error_class, "
                "error_message, retry_count, failed_at, last_retry_at "
                "FROM failed_extractions "
                "ORDER BY failed_at DESC LIMIT %s",
                (limit,),
            )
            rows = cur.fetchall()
    finally:
        pool.close()
    cols = ["sha256_hex", "extractor", "error_class", "error_message",
            "retry_count", "failed_at", "last_retry_at"]
    payload = [dict(zip(cols, r, strict=True)) for r in rows]
    if fmt == "json":
        click.echo(_json.dumps(payload, default=str))
    else:
        for p in payload:
            click.echo(
                f"{p['sha256_hex'][:12]}  {p['extractor']}  "
                f"{p['error_class']}  retries={p['retry_count']}  "
                f"{p['failed_at']}"
            )
            click.echo(f"    {p['error_message']}")


@main.command("retry-failed-extractions")
@click.option("--sha256", "sha256_hex", default=None,
              help="restrict to one blob (hex sha256)")
def retry_failed_extractions(sha256_hex):
    """Clear failed_extractions rows so extract_worker re-attempts them."""
    from localmail.db import open_pool
    pool = open_pool(_dsn())
    try:
        with pool.connection() as conn, conn.cursor() as cur:
            if sha256_hex:
                cur.execute(
                    "DELETE FROM failed_extractions WHERE sha256 = decode(%s,'hex')",
                    (sha256_hex,),
                )
            else:
                cur.execute("DELETE FROM failed_extractions")
            n = cur.rowcount
        conn.commit()
    finally:
        pool.close()
    click.echo(f"cleared {n} failed_extractions rows")
```

- [ ] **Step 4: Run tests, verify PASS**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_cli_extract.py -v
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/localmail/cli.py tests/test_cli_extract.py
git commit -m "feat(phase2): CLI list-failed-extractions + retry-failed-extractions

Mirrors the existing failed-embeddings command pair. --sha256 HEX
narrows retry to a single blob."
```

---

### Task 20: Daemon — spawn `extract_worker` thread

**Files:**
- Modify: `src/localmail/daemon.py`
- Test: `tests/test_daemon_extract_thread.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/test_daemon_extract_thread.py` (modeled on the existing `tests/test_daemon_embed_thread.py`):

```python
"""Test that Daemon spawns + cleanly joins the extract_worker thread."""

from __future__ import annotations

import threading
import time

from localmail.config import LocalmailConfig
from localmail.daemon import Daemon


class _E:
    name = "s"; model = "s"; dimension = 768

    def embed_documents(self, t):
        return [[0.5] * 768 for _ in t]

    def embed_query(self, t):
        return [0.5] * 768

    def health_check(self):
        pass


def test_daemon_starts_extract_worker_when_enabled(db_dsn):
    cfg = LocalmailConfig.model_validate({"database": {"dsn": db_dsn}})
    cfg.search.run_extract_worker = True
    d = Daemon(cfg=cfg, dsn=db_dsn, embedding_backend_factory=lambda c: _E())
    d.start()
    time.sleep(0.5)
    names = {t.name for t in threading.enumerate()}
    assert any(n.startswith("extract_worker") for n in names)
    d.stop()
    d.join(timeout=5)
    names_after = {t.name for t in threading.enumerate()}
    assert not any(n.startswith("extract_worker") for n in names_after)


def test_daemon_skips_extract_worker_when_disabled(db_dsn):
    cfg = LocalmailConfig.model_validate({"database": {"dsn": db_dsn}})
    cfg.search.run_extract_worker = False
    d = Daemon(cfg=cfg, dsn=db_dsn, embedding_backend_factory=lambda c: _E())
    d.start()
    time.sleep(0.3)
    names = {t.name for t in threading.enumerate()}
    assert not any(n.startswith("extract_worker") for n in names)
    d.stop()
    d.join(timeout=5)
```

- [ ] **Step 2: Run tests, verify they fail**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_daemon_extract_thread.py -v
```

Expected: FAIL — daemon doesn't spawn an extract thread.

- [ ] **Step 3: Spawn the thread**

In `src/localmail/daemon.py`, find the section where `embed_worker` is spawned (search for `run_embed_worker`). Add a sibling spawn for `extract_worker`:

```python
        if cfg.search.run_extract_worker:
            from localmail.search.extract_worker import run_extract_worker
            extract_thread = threading.Thread(
                target=run_extract_worker,
                kwargs={
                    "conn_factory": lambda: psycopg.connect(cfg.database.dsn),
                    "cfg": cfg.search,
                    "stop_event": self._stop_event,
                },
                name="extract_worker",
                daemon=True,
            )
            extract_thread.start()
            self._threads.append(extract_thread)
```

- [ ] **Step 4: Run tests, verify PASS**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_daemon_extract_thread.py tests/test_daemon.py tests/test_daemon_embed_thread.py -v
```

Expected: all PASS (including no Phase 1 regression).

- [ ] **Step 5: Commit**

```bash
git add src/localmail/daemon.py tests/test_daemon_extract_thread.py
git commit -m "feat(phase2): daemon spawns extract_worker thread

One thread per process (account-agnostic) alongside the existing
embed_worker. Gated by cfg.search.extract_worker_enabled."
```

---

### Task 21: Attachment fixture corpus builder

**Files:**
- Create: `tests/_attachment_corpus.py`
- Test: `tests/test_attachment_corpus.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/test_attachment_corpus.py`:

```python
"""Smoke tests for the attachment fixture builder — verify fixture bytes
roundtrip through LightweightExtractor."""

from pathlib import Path
import hashlib

import pytest

from localmail.search.extractor import LightweightExtractor


def test_builder_produces_native_pdf():
    from tests._attachment_corpus import build_native_pdf
    data = build_native_pdf("hello fixture corpus")
    assert data.startswith(b"%PDF")


def test_builder_produces_docx():
    from tests._attachment_corpus import build_docx
    data = build_docx(["para one", "para two"])
    assert len(data) > 1000  # zip overhead alone


def test_builder_produces_xlsx():
    from tests._attachment_corpus import build_xlsx
    data = build_xlsx({"Sheet1": [["alice", "Berlin"], ["bob", "Madrid"]]})
    assert len(data) > 1000


def test_builder_produces_ics():
    from tests._attachment_corpus import build_ics
    data = build_ics("Annual review", "discuss bonus", "Conf room")
    assert b"BEGIN:VCALENDAR" in data
    assert b"Annual review" in data


def test_build_corpus_seeds_db_messages_and_blobs(db_conn, tmp_path):
    from tests._attachment_corpus import build_corpus
    fixtures = build_corpus(db_conn, attachments_root=tmp_path)
    assert len(fixtures) >= 10
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM messages")
        assert cur.fetchone()[0] >= 10
        cur.execute("SELECT count(*) FROM attachment_blobs")
        assert cur.fetchone()[0] >= 10
```

- [ ] **Step 2: Run tests, verify they fail**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_attachment_corpus.py -v
```

Expected: FAIL — module doesn't exist.

- [ ] **Step 3: Create the fixture corpus module**

Create `tests/_attachment_corpus.py`:

```python
"""Synthetic attachment fixture corpus for Phase 2 acceptance.

Builds in-memory bytes for each fixture format (no .eml or .pdf files
checked into the repo per CLAUDE.md). The acceptance harness writes
these bytes into a temp attachments_root tree and inserts
attachment_blobs + messages rows.

Each successful fixture is wrapped in a synthetic email whose subject
and body deliberately do NOT mention the attachment's distinctive
content. The attachment text contains a unique tag that is the target
of attachment_queries.json.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import io
from pathlib import Path
from typing import Any

_BASE = _dt.datetime(2026, 1, 1, tzinfo=_dt.timezone.utc)


# --- per-format builders ---------------------------------------------------

def build_native_pdf(text: str) -> bytes:
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.drawString(72, 720, text)
    c.showPage()
    c.save()
    return buf.getvalue()


def build_scanned_pdf(text: str) -> bytes:
    """A PDF whose only content is a rasterized image of the text.
    pypdf returns empty text; docling OCRs."""
    from PIL import Image, ImageDraw, ImageFont
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    img = Image.new("RGB", (1000, 100), "white")
    ImageDraw.Draw(img).text((10, 30), text, fill="black")
    img_buf = io.BytesIO()
    img.save(img_buf, format="PNG")
    img_buf.seek(0)

    pdf_buf = io.BytesIO()
    c = canvas.Canvas(pdf_buf)
    c.drawImage(ImageReader(img_buf), 72, 600, width=400, height=80)
    c.showPage()
    c.save()
    return pdf_buf.getvalue()


def build_docx(paragraphs: list[str]) -> bytes:
    import docx
    buf = io.BytesIO()
    d = docx.Document()
    for p in paragraphs:
        d.add_paragraph(p)
    d.save(buf)
    return buf.getvalue()


def build_xlsx(sheets: dict[str, list[list[str]]]) -> bytes:
    import openpyxl
    wb = openpyxl.Workbook()
    # Drop the default sheet
    default_name = wb.sheetnames[0]
    for sheet_name, rows in sheets.items():
        if sheet_name == default_name:
            ws = wb[default_name]
        else:
            ws = wb.create_sheet(title=sheet_name)
        for row in rows:
            ws.append(row)
    # Remove unused default sheet if not used
    if default_name not in sheets and default_name in wb.sheetnames:
        del wb[default_name]
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def build_pptx(title: str, notes: str) -> bytes:
    from pptx import Presentation
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[5])
    slide.shapes.title.text = title
    slide.notes_slide.notes_text_frame.text = notes
    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()


def build_rtf(text: str) -> bytes:
    body = (
        r"{\rtf1\ansi\deff0 {\fonttbl {\f0 Helvetica;}}"
        r"\f0\fs24 " + text + r"\par}"
    )
    return body.encode("ascii", errors="ignore")


def build_txt(text: str, encoding: str = "utf-8") -> bytes:
    return text.encode(encoding)


def build_html(title: str, body: str) -> bytes:
    return (
        f"<html><body><h1>{title}</h1><p>{body}</p></body></html>"
    ).encode("utf-8")


def build_csv(rows: list[list[str]]) -> bytes:
    import csv
    buf = io.StringIO()
    w = csv.writer(buf)
    for r in rows:
        w.writerow(r)
    return buf.getvalue().encode("utf-8")


def build_ics(summary: str, description: str, location: str) -> bytes:
    from icalendar import Calendar, Event
    cal = Calendar()
    cal.add("prodid", "-//Test//Test//EN")
    cal.add("version", "2.0")
    ev = Event()
    ev.add("summary", summary)
    ev.add("description", description)
    ev.add("location", location)
    ev.add("dtstart", _BASE)
    cal.add_component(ev)
    return cal.to_ical()


def build_encrypted_pdf() -> bytes:
    """A password-protected PDF — should land in failed_extractions."""
    import pikepdf
    src = build_native_pdf("secret content")
    out = io.BytesIO()
    with pikepdf.open(io.BytesIO(src)) as p:
        p.save(out, encryption=pikepdf.Encryption(owner="o", user="u", R=4))
    return out.getvalue()


def build_corrupt_pdf() -> bytes:
    return b"%PDF-1.4\nthis is not a valid PDF body"


def build_empty() -> bytes:
    return b""


def build_oversized_pdf(min_bytes: int) -> bytes:
    """Pad a tiny PDF with comment lines until it exceeds min_bytes."""
    base = build_native_pdf("a")
    padding = b"%pad\n" * ((min_bytes // 5) + 1)
    return base + padding


# --- in-DB seeding ---------------------------------------------------------

def _write_blob(content: bytes, attachments_root: Path) -> tuple[bytes, Path]:
    sha = hashlib.sha256(content).digest()
    sub = sha.hex()
    blob_path = attachments_root / "blobs" / sub[:2] / sub[2:4] / sub
    blob_path.parent.mkdir(parents=True, exist_ok=True)
    if not blob_path.exists():
        blob_path.write_bytes(content)
    return sha, blob_path


def build_corpus(
    conn,
    *,
    attachments_root: Path,
) -> list[dict[str, Any]]:
    """Insert messages + attachment_blobs into the DB. Returns the seed
    list of dicts with keys: id (message id), subject, attachment_tag,
    attachment_mime, attachment_filename."""

    fixtures: list[dict[str, Any]] = [
        {
            "subject": "Quarterly review attachment",
            "body": "see attached",
            "tag": "non-disclosure obligations under section 5",
            "filename": "contract.pdf",
            "mime": "application/pdf",
            "bytes": build_native_pdf("non-disclosure obligations under section 5"),
        },
        {
            "subject": "Scanned report",
            "body": "FYI scan",
            "tag": "annual revenue growth fourteen percent",
            "filename": "scan.pdf",
            "mime": "application/pdf",
            "bytes": build_scanned_pdf("annual revenue growth fourteen percent"),
        },
        {
            "subject": "Onboarding pack",
            "body": "details inside",
            "tag": "probation period three months from start date",
            "filename": "onboarding.docx",
            "mime": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "bytes": build_docx([
                "Welcome to the team.",
                "probation period three months from start date",
            ]),
        },
        {
            "subject": "Budget spreadsheet",
            "body": "numbers attached",
            "tag": "marketing line item Berlin office Q4",
            "filename": "budget.xlsx",
            "mime": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "bytes": build_xlsx({
                "Q4": [
                    ["Office", "Line item", "Amount"],
                    ["Berlin", "marketing line item Berlin office Q4", "12000"],
                ]
            }),
        },
        {
            "subject": "All-hands deck",
            "body": "slides",
            "tag": "company-wide hackathon scheduled mid October",
            "filename": "allhands.pptx",
            "mime": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            "bytes": build_pptx(
                "All-Hands",
                "company-wide hackathon scheduled mid October",
            ),
        },
        {
            "subject": "Notes",
            "body": "see notes",
            "tag": "research direction transformer attention sparsity",
            "filename": "notes.rtf",
            "mime": "application/rtf",
            "bytes": build_rtf("research direction transformer attention sparsity"),
        },
        {
            "subject": "Quick note",
            "body": "attached",
            "tag": "vendor SLA monthly recurring fee adjustment",
            "filename": "note.txt",
            "mime": "text/plain",
            "bytes": build_txt("vendor SLA monthly recurring fee adjustment"),
        },
        {
            "subject": "Doc",
            "body": "see md",
            "tag": "rollout plan Tuesday wave one customers",
            "filename": "doc.md",
            "mime": "text/markdown",
            "bytes": build_txt("# Rollout\n\nrollout plan Tuesday wave one customers"),
        },
        {
            "subject": "Web archive",
            "body": "attached html",
            "tag": "policy update privacy controls fourth quarter",
            "filename": "archive.html",
            "mime": "text/html",
            "bytes": build_html(
                "Policy",
                "policy update privacy controls fourth quarter",
            ),
        },
        {
            "subject": "Tabular data",
            "body": "csv attached",
            "tag": "compliance audit findings critical severity",
            "filename": "data.csv",
            "mime": "text/csv",
            "bytes": build_csv([
                ["finding_id", "severity", "summary"],
                ["F-1", "critical", "compliance audit findings critical severity"],
            ]),
        },
        {
            "subject": "Invite",
            "body": "calendar attached",
            "tag": "interview panel candidate ML researcher",
            "filename": "invite.ics",
            "mime": "text/calendar",
            "bytes": build_ics(
                "interview panel candidate ML researcher",
                "ML researcher screening",
                "Conf room A",
            ),
        },
    ]

    negatives = [
        {
            "subject": "Locked file",
            "body": "password protected",
            "tag": None,  # should land in failed_extractions
            "filename": "locked.pdf",
            "mime": "application/pdf",
            "bytes": build_encrypted_pdf(),
        },
        {
            "subject": "Mystery attachment",
            "body": "broken",
            "tag": None,
            "filename": "broken.pdf",
            "mime": "application/pdf",
            "bytes": build_corrupt_pdf(),
        },
        {
            "subject": "Empty placeholder",
            "body": "empty",
            "tag": None,
            "filename": "empty.txt",
            "mime": "text/plain",
            "bytes": build_empty(),
        },
        {
            "subject": "Huge PDF",
            "body": "too big to extract",
            "tag": None,
            "filename": "huge.pdf",
            "mime": "application/pdf",
            "bytes": build_oversized_pdf(60 * 1024 * 1024),  # >50 MB default cap
        },
    ]

    noise = [
        {"subject": "Project status sync", "body": "Recurring weekly sync agenda"},
        {"subject": "Reminder about timezone update", "body": "switch to CET"},
        {"subject": "Sourdough starter discard", "body": "kitchen tips"},
        {"subject": "Library closing hours", "body": "summer schedule"},
        {"subject": "Garden compost run", "body": "weekend"},
    ]

    seeded: list[dict[str, Any]] = []
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO accounts (name, email_address, imap_host, auth_method) "
            "VALUES ('attach_corpus', 'a@b', 'h', 'password') RETURNING id"
        )
        acct = cur.fetchone()[0]

        for i, f in enumerate(fixtures + negatives):
            sha, blob_path = _write_blob(f["bytes"], attachments_root)
            cur.execute(
                "INSERT INTO attachment_blobs (sha256, path, mime_type, size_bytes) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
                (sha, str(blob_path), f["mime"], len(f["bytes"])),
            )
            import json
            attachments = json.dumps([{
                "filename": f["filename"], "sha256": sha.hex()
            }])
            cur.execute(
                "INSERT INTO messages (account_id, message_id, raw_sha256, subject,"
                " body_text, headers, raw_bytes, size_bytes, attachments, date_sent)"
                " VALUES (%s, %s, %s, %s, %s, '{}'::jsonb, %s, %s, %s::jsonb, %s)"
                " RETURNING id",
                (
                    acct, f"<ac{i}@local>", bytes([i + 1]) * 32,
                    f["subject"], f["body"], b"raw", len(f["body"]),
                    attachments, _BASE + _dt.timedelta(days=i),
                ),
            )
            mid = cur.fetchone()[0]
            seeded.append({
                "id": mid, "subject": f["subject"], "tag": f["tag"],
                "mime": f["mime"], "filename": f["filename"],
            })

        for j, n in enumerate(noise):
            cur.execute(
                "INSERT INTO messages (account_id, message_id, raw_sha256, subject,"
                " body_text, headers, raw_bytes, size_bytes, attachments, date_sent)"
                " VALUES (%s, %s, %s, %s, %s, '{}'::jsonb, %s, %s, '[]'::jsonb, %s)"
                " RETURNING id",
                (
                    acct, f"<noise{j}@local>", bytes([j + 200]) * 32,
                    n["subject"], n["body"], b"raw", len(n["body"]),
                    _BASE + _dt.timedelta(days=100 + j),
                ),
            )
    conn.commit()
    return seeded
```

- [ ] **Step 4: Run tests, verify PASS**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_attachment_corpus.py -v
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/_attachment_corpus.py tests/test_attachment_corpus.py
git commit -m "test(phase2): synthetic attachment fixture corpus

Programmatic builders for the 11 allowlisted formats + 4 negative tests
(encrypted/corrupt/empty/oversized). build_corpus() seeds messages and
attachment_blobs rows into the test DB without writing fixture files to
the repo."
```

---

### Task 22: Acceptance query suite

**Files:**
- Create: `tests/fixtures/attachment_queries.json`

This is the ground-truth query set for the Phase 2 retrieval-quality gate. Each entry pairs a query with the subject(s) of the carrying email whose attachment contains the query's target content.

- [ ] **Step 1: Write the query file**

Create `tests/fixtures/attachment_queries.json`:

```json
{
  "_doc": "Phase-2 acceptance query suite. ~25 queries targeting attachment-only content. Each query's relevant_subjects are the subjects of the synthetic emails whose attachments contain the query target (see tests/_attachment_corpus.py). Gates: recall@20 >= 0.80, MRR@20 >= 0.50.",
  "queries": [
    {"lang": "en", "query": "non-disclosure obligations section 5", "relevant_subjects": ["Quarterly review attachment"]},
    {"lang": "en", "query": "NDA contract clauses", "relevant_subjects": ["Quarterly review attachment"]},

    {"lang": "en", "query": "annual revenue growth fourteen percent", "relevant_subjects": ["Scanned report"]},
    {"lang": "en", "query": "revenue growth 14% report", "relevant_subjects": ["Scanned report"]},

    {"lang": "en", "query": "probation period three months start date", "relevant_subjects": ["Onboarding pack"]},
    {"lang": "en", "query": "onboarding probation rules", "relevant_subjects": ["Onboarding pack"]},

    {"lang": "en", "query": "marketing line item Berlin office Q4", "relevant_subjects": ["Budget spreadsheet"]},
    {"lang": "en", "query": "Berlin marketing budget fourth quarter", "relevant_subjects": ["Budget spreadsheet"]},

    {"lang": "en", "query": "company-wide hackathon mid October", "relevant_subjects": ["All-hands deck"]},
    {"lang": "en", "query": "hackathon scheduled October", "relevant_subjects": ["All-hands deck"]},

    {"lang": "en", "query": "transformer attention sparsity research", "relevant_subjects": ["Notes"]},
    {"lang": "en", "query": "ML research attention sparsity", "relevant_subjects": ["Notes"]},

    {"lang": "en", "query": "vendor SLA monthly recurring fee adjustment", "relevant_subjects": ["Quick note"]},
    {"lang": "en", "query": "SLA fee adjustment vendor", "relevant_subjects": ["Quick note"]},

    {"lang": "en", "query": "rollout plan Tuesday wave one customers", "relevant_subjects": ["Doc"]},
    {"lang": "en", "query": "Tuesday wave one rollout", "relevant_subjects": ["Doc"]},

    {"lang": "en", "query": "policy update privacy controls fourth quarter", "relevant_subjects": ["Web archive"]},
    {"lang": "en", "query": "privacy policy Q4 changes", "relevant_subjects": ["Web archive"]},

    {"lang": "en", "query": "compliance audit findings critical severity", "relevant_subjects": ["Tabular data"]},
    {"lang": "en", "query": "critical audit finding", "relevant_subjects": ["Tabular data"]},

    {"lang": "en", "query": "interview panel candidate ML researcher", "relevant_subjects": ["Invite"]},
    {"lang": "en", "query": "ML researcher screening interview", "relevant_subjects": ["Invite"]},

    {"lang": "en", "query": "Berlin office Q4 budget hackathon October", "relevant_subjects": ["Budget spreadsheet", "All-hands deck"]}
  ]
}
```

- [ ] **Step 2: Validate**

```bash
unset VIRTUAL_ENV && uv run python -c "
import json
data = json.load(open('tests/fixtures/attachment_queries.json'))
print('queries:', len(data['queries']))
"
```

Expected: `queries: 25`.

- [ ] **Step 3: Commit**

```bash
git add tests/fixtures/attachment_queries.json
git commit -m "test(phase2): attachment_queries.json — Phase 2 acceptance query set

25 queries targeting attachment-only content in the synthetic fixture
corpus. Each query carries verbatim relevant_subjects for the carrying
email."
```

---

### Task 23: Acceptance harness + end-to-end run

**Files:**
- Create: `tests/acceptance/run_attachment_eval.py`

- [ ] **Step 1: Write the harness**

Create `tests/acceptance/run_attachment_eval.py`:

```python
"""Phase-2 acceptance harness: extraction success + retrieval recall/MRR.

Usage:
    PYTHONPATH=src:. uv run python tests/acceptance/run_attachment_eval.py \\
        --queries tests/fixtures/attachment_queries.json \\
        --k 20

Gates:
    - Extraction success: >= 95% of allowlisted non-negative-test blobs
      produce a non-sentinel attachment_text row.
    - Retrieval: recall@20 >= 0.80, MRR@20 >= 0.50 on the query suite.

Also asserts no regression on Phase 1's run_recall_eval.py (separate run).
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

import psycopg

from localmail.config import SearchConfig
from localmail.db import apply_migrations, open_pool
from localmail.search.embed_worker import run_embed_worker_once
from localmail.search.embeddings import FastEmbedBackend
from localmail.search.extract_worker import run_extract_worker_once
from localmail.search.searcher import Searcher

from tests._attachment_corpus import build_corpus


def _reciprocal_rank(ordered: list[str], relevant: set[str]) -> float:
    for i, s in enumerate(ordered, start=1):
        if s in relevant:
            return 1.0 / i
    return 0.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--queries", required=True, type=Path)
    ap.add_argument("--k", type=int, default=20)
    ap.add_argument("--dsn", default=None)
    args = ap.parse_args()

    import os
    dsn = (
        args.dsn
        or os.environ.get("LOCALMAIL_TEST_DSN")
        or "postgresql://localmail:local%40%40mail@localhost:5532/localmail_test"
    )

    print(f"Applying migrations to {dsn!r} …", file=sys.stderr)
    apply_migrations(dsn)

    with tempfile.TemporaryDirectory() as tmpdir:
        attachments_root = Path(tmpdir)
        print("Seeding corpus …", file=sys.stderr)
        with psycopg.connect(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "TRUNCATE accounts, mailboxes, messages, message_labels,"
                    " attachment_blobs, failed_messages, message_chunks,"
                    " failed_embeddings, embedding_models, failed_chunkings,"
                    " attachment_text, attachment_chunks, failed_extractions"
                    " RESTART IDENTITY CASCADE"
                )
            conn.commit()
            seeded = build_corpus(conn, attachments_root=attachments_root)

            cfg = SearchConfig()
            backend = FastEmbedBackend(cfg)

            print("Running extract_worker …", file=sys.stderr)
            passes = 0
            while True:
                wrote = run_extract_worker_once(conn, cfg)
                passes += 1
                if wrote == 0:
                    break
            print(f"  extract_worker: {passes} pass(es)", file=sys.stderr)

            print("Running embed_worker …", file=sys.stderr)
            passes = 0
            while True:
                wrote = run_embed_worker_once(conn, cfg, backend)
                passes += 1
                if wrote == 0:
                    break
            print(f"  embed_worker: {passes} pass(es)", file=sys.stderr)

            # --- Gate A: extraction success ---
            allowlisted_non_negative = [f for f in seeded if f["tag"] is not None]
            with conn.cursor() as cur:
                shas = []
                for f in allowlisted_non_negative:
                    cur.execute(
                        "SELECT extractor FROM attachment_text t "
                        "JOIN attachment_blobs b USING (sha256) "
                        "JOIN messages m ON m.attachments @> "
                        "  jsonb_build_array(jsonb_build_object('sha256', encode(b.sha256,'hex'))) "
                        "WHERE m.id = %s",
                        (f["id"],),
                    )
                    row = cur.fetchone()
                    shas.append((f["subject"], row[0] if row else None))
            non_sentinel = sum(
                1 for _, e in shas
                if e and not e.startswith("lightweight-empty")
                       and not e.startswith("size-skipped")
            )
            extraction_rate = non_sentinel / len(allowlisted_non_negative)
            print(f"\nGate A (extraction): {non_sentinel}/{len(allowlisted_non_negative)} "
                  f"= {extraction_rate:.3f}  (target >= 0.95)")
            gate_a_pass = extraction_rate >= 0.95

        # --- Gate B: retrieval quality ---
        pool = open_pool(dsn)
        try:
            searcher = Searcher(
                pool=pool, cfg=cfg, embeddings=backend,
                reranker=None, rewriter=None,
            )

            suite = json.loads(args.queries.read_text())
            per_lang_recall: dict[str, list[float]] = defaultdict(list)
            per_lang_mrr: dict[str, list[float]] = defaultdict(list)

            for q in suite["queries"]:
                page = searcher.search(
                    q["query"],
                    page_size=args.k,
                    candidates_per_arm=args.k * 3,
                    rerank_pool_size=args.k * 3,
                )
                ranked = [r.subject for r in page.results]
                relevant = set(q["relevant_subjects"])
                hits = len([s for s in ranked if s in relevant])
                recall = hits / max(1, len(relevant))
                per_lang_recall[q["lang"]].append(min(1.0, recall))
                per_lang_mrr[q["lang"]].append(_reciprocal_rank(ranked, relevant))
        finally:
            pool.close()

        print("\nGate B (retrieval):")
        print(f"{'lang':<6} {'#q':>4} {'recall@K':>10} {'MRR@K':>8}  status")
        print("-" * 40)
        gate_b_failures = []
        for lang in sorted(per_lang_recall):
            recalls = per_lang_recall[lang]
            mrrs = per_lang_mrr[lang]
            r = statistics.fmean(recalls)
            m = statistics.fmean(mrrs)
            ok = r >= 0.80 and m >= 0.50
            status = "PASS" if ok else "FAIL"
            if not ok:
                gate_b_failures.append(f"{lang}: recall={r:.3f}, MRR={m:.3f}")
            print(f"{lang:<6} {len(recalls):>4} {r:>10.3f} {m:>8.3f}  {status}")

        ok = gate_a_pass and not gate_b_failures
        if not ok:
            print("\nFAILURES:", file=sys.stderr)
            if not gate_a_pass:
                print(f"  - Gate A: extraction rate {extraction_rate:.3f} < 0.95",
                      file=sys.stderr)
            for f in gate_b_failures:
                print(f"  - Gate B {f}", file=sys.stderr)
            return 1

        print("\nAll Phase 2 acceptance gates PASS.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Run the harness end-to-end**

```bash
unset VIRTUAL_ENV && PYTHONPATH=src:. uv run python tests/acceptance/run_attachment_eval.py \
    --queries tests/fixtures/attachment_queries.json --k 20
```

Expected on first run:
- First docling invocation downloads layout/OCR models (~500 MB); can take several minutes.
- Final output ends with `All Phase 2 acceptance gates PASS.` and exit code 0.

If either gate fails:
- **Gate A < 0.95**: check `localmail list-failed-extractions` for unexpected failures. The 11 happy-path fixtures + scanned PDF should all extract. If the scanned PDF lands in failures (docling unavailable / OOM), install `[extraction]` or reduce `extractor_docling_max_pages`.
- **Gate B recall < 0.80 or MRR < 0.50**: re-run with `--k 30` to see if the rank is just past 20; tune `arm4_fanout_cap`, `candidates_per_arm`, or revisit query phrasing.

- [ ] **Step 3: Re-run Phase 1 non-regression check**

```bash
unset VIRTUAL_ENV && PYTHONPATH=src:. uv run python tests/acceptance/run_recall_eval.py \
    --queries tests/fixtures/multilingual_queries.json --k 20
```

Expected: same PASS as before Phase 2 — recall@20 = 1.000 across all five languages, MRR@20 ≥ 0.93 for gated languages.

- [ ] **Step 4: Run the full unit test suite**

```bash
unset VIRTUAL_ENV && uv run pytest -q -m "not slow"
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/acceptance/run_attachment_eval.py
git commit -m "test(phase2): acceptance harness — run_attachment_eval.py

Seeds the synthetic attachment corpus, runs extract_worker +
embed_worker to drain queues, computes extraction success rate
(Gate A) and recall/MRR over the query suite (Gate B).

Phase 1 non-regression run is a separate invocation of
run_recall_eval.py."
```

- [ ] **Step 6: Push**

```bash
git push origin main
```

---

## Self-review checklist (reviewer: end of plan)

After all 23 tasks land:

- [ ] **Spec coverage** — every spec section maps to at least one task:
  - Scope → all tasks
  - Architecture → Tasks 13-17, 20
  - Schema (0011, 0012, 0013) → Tasks 2, 3, 4
  - Configuration → Task 5
  - Extractor protocol → Tasks 6-11
  - Chunking → Task 12
  - Workers → Tasks 13-15
  - Search arm + Searcher integration → Tasks 16-17
  - `has:attachment` semantics (unchanged) → covered by Task 17 (Phase 1 filter pushdown works)
  - Snippet generation → Task 17
  - Failure handling → Tasks 13, 15, 19
  - CLI additions → Tasks 18-19
  - Daemon integration → Task 20
  - Acceptance gates (A, B, Phase 1 non-regression) → Tasks 21-23
- [ ] **No placeholders** — every step has runnable code or commands.
- [ ] **Type consistency** — `chunk_attachment_text(sha256, text, cfg)` is called identically from Task 12 (definition) and Task 14 (embed_worker extension). `arm_vector_attachment_chunks` returns `list[ArmHit]` in Task 16 and is consumed in Task 17. `ExtractedText.extractor` strings (`lightweight@1.0` / `docling@VER` / `lightweight-empty` / `size-skipped`) appear consistently across Tasks 7, 11, 13.
