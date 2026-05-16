# Hybrid search — Phase 2 design (attachment extraction + Arm 4)

Spec for the Phase 2 increment of the localmail hybrid search subsystem.
Builds on the Phase 1 design at
[`2026-05-16-hybrid-search-design.md`](2026-05-16-hybrid-search-design.md)
and the implementation it shipped under
[`../plans/2026-05-16-hybrid-search-phase1.md`](../plans/2026-05-16-hybrid-search-phase1.md).
Phase 1 is **done** and **accepted** — all gated languages cleared
recall@20 ≥ 0.80 and MRR@20 ≥ 0.50 on the multilingual corpus
(commit `8969255`).

## Scope

Phase 2 makes the **text content of email attachments** searchable. After
Phase 2 ships, a query whose answer lives only inside an attached PDF,
Word doc, spreadsheet, or calendar invite surfaces the carrying message.

In scope:

- Extraction worker that converts blobs in `attachment_blobs` into
  plain text rows in a new `attachment_text` table.
- A two-extractor strategy: `LightweightExtractor` (pure-Python, no OCR)
  with a `DoclingExtractor` fallback for PDFs that lightweight cannot
  read (typically scanned PDFs).
- MIME allowlist + size/page/length limits, all driven by `SearchConfig`.
- Chunking + embedding of attachment text via the **existing**
  `embed_worker` (no code duplication; the `chunk_table` discriminator
  added in Phase 1 already supports `'attachment_chunks'`).
- A fourth retrieval arm — vector cosine over `attachment_chunks`,
  JOINed to messages via `messages.attachments` JSONB. Integrated into
  the existing RRF fusion pipeline.
- CLI commands mirroring the Phase 1 `embed-*` family
  (`extract-backfill`, `list-failed-extractions`, `retry-failed-extractions`)
  and an extension to `search-status`.
- Daemon spawns a third worker thread (`extract_worker`) alongside the
  existing IDLE + poll threads and the existing `embed_worker`.
- Acceptance harness: a synthetic attachment fixture corpus +
  ground-truth queries, plus a non-regression check that Phase 1's
  multilingual gates still pass.

Out of scope (deferred to later phases or explicit non-goals):

- **Arm 5** (BM25 over `attachment_chunks.text`). Deferred to Phase 5
  per the Phase 1 spec — OCR'd / converted attachment text is noisier
  than message bodies; whether lexical retrieval helps is an empirical
  question we defer until we have real-world query traces.
- **Image attachments** (`image/*`). No OCR on JPG/PNG/HEIC/etc. The
  Phase 2 user decision said this would mostly add noise (newsletter
  banners, signature logos). If/when we want it, an `extractor_image_ocr`
  flag and a docling-image path are a tractable Phase 5 addition.
- **Archives** (`.zip`, `.tar`, `.7z`). Recursive unpacking + per-member
  extraction is a separate feature. Out of Phase 2.
- **Media / binaries** (`audio/*`, `video/*`, `application/octet-stream`
  without a documenty extension, etc.). Always skipped.
- **In-place migration of existing blobs**: there is none required. The
  extract_worker discovers existing blobs via `LEFT JOIN attachment_text`
  on its first sweep and processes them in batches.

## Architecture

Two new pieces of work happen between `sync.py` writing a blob to disk
and the blob's text being searchable:

1. **`extract_worker`** — new daemon thread, **one per process**
   (account-agnostic, like `embed_worker`). Polls `attachment_blobs`
   `LEFT JOIN attachment_text` for blobs whose MIME or filename
   extension matches `extractor_mime_allowlist` /
   `extractor_extension_allowlist` and that have no `attachment_text`
   row yet. Per-blob flow:

   1. Run `LightweightExtractor.extract(path, mime_type)`. Classify
      the outcome as **text**, **empty**, or **raised**.
   2. If **text** → INSERT `attachment_text` (`extractor='lightweight@VER'`)
      and done.
   3. If **empty** or **raised**, and blob is a PDF, and docling is
      importable → run `DoclingExtractor.extract()` and classify
      similarly:
      - Docling **text** → INSERT `attachment_text` (`extractor='docling@VER'`).
      - Docling **raised** → record in `failed_extractions` with the
        docling traceback.
      - Docling **empty**: if lightweight had **raised** → record in
        `failed_extractions` with the lightweight traceback (real
        error, no successful fallback). If lightweight had returned
        **empty** → INSERT sentinel (`extractor='lightweight-empty'`,
        `extracted_text=''`) — both extractors agree there's no text.
   4. If **empty** or **raised**, and (blob is non-PDF or docling is
      missing):
      - Lightweight **empty** → INSERT sentinel
        (`extractor='lightweight-empty'`, `extracted_text=''`). If
        docling was missing AND blob is a PDF, emit a one-shot WARN
        with the `uv sync --extra extraction` install hint.
      - Lightweight **raised** → record in `failed_extractions` (no
        fallback exists to override the failure).

   Per-blob `SAVEPOINT` around the work; nested `SAVEPOINT` around
   the failure-recording path; mirrors `failed_chunkings` /
   `failed_embeddings`.

2. **`embed_worker` extension** — on its next sweep, sees
   `attachment_text` rows with `extracted_text != ''` and **no
   corresponding `attachment_chunks`**. Calls
   `chunk_attachment_text(sha256, text, cfg)` (pure function in
   `search/chunking.py`), INSERTs chunks with `embedding_v1 IS NULL`.
   Subsequent sweeps embed them via the per-chunk SAVEPOINT path
   already used for `message_chunks`. **No code duplication** — the
   `chunk_table` discriminator (`'message_chunks' | 'attachment_chunks'`)
   shipped in Phase 1. Sentinel rows with `extracted_text=''` produce
   zero chunks and are silently skipped.

Search-time, **Arm 4** runs alongside Arms 1-3 in `Searcher`:

- Vector cosine `ORDER BY embedding_v1 <=> :query_vec` over
  `attachment_chunks`.
- JOINed to `messages` via JSONB containment on
  `messages.attachments` (GIN-indexed; see migration 0013).
- A single blob attached to **N messages fans out to N candidate rows**
  capped at `arm4_fanout_cap` per chunk to prevent newsletter blobs
  (attached to hundreds of list recipients) monopolizing the candidate
  budget. Fan-out is ordered by `messages.date_sent DESC`.

The sync daemon depends on neither worker. Disabling extraction →
search still works, just doesn't surface attachment-only matches.
Disabling embedding → vector arms return nothing but lexical arms
still work.

## Data flow

### Ingestion

```
sync.py
  writes message + blobs as today (Phase 1 unchanged)
                │
                ▼
attachment_blobs ─────────────────────────────────────┐
                                                      │
extract_worker (every extract_worker_poll_interval_s) │
  SELECT b.sha256, b.path, b.mime_type, b.size_bytes  │
  FROM attachment_blobs b                             │
  LEFT JOIN attachment_text t USING (sha256)          │
  LEFT JOIN failed_extractions f USING (sha256)       │
  WHERE t.sha256 IS NULL                              │
    AND (f.sha256 IS NULL OR f.retry_count < cfg.extract_worker_max_retries)
    AND mime_in_allowlist(b.mime_type, b.path)        │
    AND b.size_bytes <= cfg.extractor_max_blob_bytes  │
  ORDER BY b.first_seen_at                            │
  LIMIT cfg.extract_worker_batch_size                 │
                │                                     │
                ▼                                     │
  for each blob:                                      │
    SAVEPOINT extract_blob                            │
    text = LightweightExtractor.extract(path, mime)   │
    if not text and is_pdf(path) and docling_avail:   │
      text = DoclingExtractor.extract(path, mime)     │
    INSERT attachment_text (sha256, extractor, text)  │
    RELEASE SAVEPOINT                                 │
  COMMIT                                              │
                                                      │
embed_worker (existing thread, extended)              │
  - existing: chunks/embeds message_chunks            │
  - new: chunks/embeds attachment_chunks where        │
    extracted_text != '' and no chunks yet            │
```

### Query

Identical to Phase 1, with one extra arm:

1. `Searcher.search(query)` parses operators, builds filters.
2. Four arms run sequentially against the same connection:
   - Arm 1: BM25 over `messages` (Phase 1)
   - Arm 2: BM25 over `message_chunks` (Phase 1)
   - Arm 3: vector cosine over `message_chunks` (Phase 1)
   - **Arm 4: vector cosine over `attachment_chunks`, JOINed to
     `messages.attachments` JSONB (new)**
3. `rrf_fuse(arms, k=rrf_k)` dedupes by `message_id`. Per-arm rank is
   what the RRF formula consumes; fan-out from Arm 4 is normal RRF
   input.
4. Reranker (if enabled) cross-encodes `(query, snippet_text)` pairs.
5. Page returned. Snippets from Arm 4 hits carry `snippet_source='attachment'`
   and `attachment_filename` populated from the matched JSONB element.

## Schema

Three new migrations, monotonically numbered from current head
(`0010_failed_chunkings.sql`). Per CLAUDE.md convention, gaps in the
existing 0005 / 0008 slots are **not** backfilled — new migrations get
new numbers.

### `0011_attachment_text.sql` (transactional)

```sql
CREATE TABLE attachment_text (
    sha256          BYTEA       PRIMARY KEY
                                REFERENCES attachment_blobs(sha256) ON DELETE CASCADE,
    extractor       TEXT        NOT NULL,
    extracted_text  TEXT        NOT NULL,        -- '' allowed as sentinel
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

`extracted_text` is `NOT NULL` but `''` is a legitimate sentinel value
meaning "we tried, got nothing, don't retry". The embed_worker treats
empty strings as zero chunks and silently skips, avoiding the loop
trap.

`attachment_chunks.sha256` (not `message_id`) is the natural key: the
content-addressable blob design means one set of chunks per unique
byte sequence, regardless of how many messages reference it.

### `0012_failed_extractions.sql` (transactional)

```sql
CREATE TABLE failed_extractions (
    sha256          BYTEA       PRIMARY KEY
                                REFERENCES attachment_blobs(sha256) ON DELETE CASCADE,
    extractor       TEXT        NOT NULL,        -- most-recent extractor that failed
    error_class     TEXT        NOT NULL,
    error_message   TEXT        NOT NULL,
    traceback       TEXT,
    retry_count     INT         NOT NULL DEFAULT 0,
    failed_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_retry_at   TIMESTAMPTZ
);
```

One row per failed blob (not per `(blob, extractor)`). On retry the row
is upserted and `retry_count` bumped. The `extractor` column records
the **most recent** failing extractor — sufficient for diagnostics; the
order in which extractors failed is not load-bearing.

### `0013_attachment_search_indexes.sql` (`@non-transactional`)

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

**No FTS GIN on `attachment_chunks`** — Arm 5 stays deferred to Phase
5. Adding the STORED `fts` generated column once `attachment_chunks`
is large would force a full table rewrite, so we explicitly punt that
cost until we know it's worth paying.

## Configuration

New fields on `SearchConfig` (in [`src/localmail/config.py`](../../../src/localmail/config.py)).
**All numeric/policy tunables in `SearchConfig`. No magic numbers in
extraction or search code.**

```python
# Extraction worker
extract_worker_enabled: bool = True
extract_worker_poll_interval_s: int = 30
extract_worker_batch_size: int = 20
extract_worker_max_retries: int = 3

# Extractor policy
extractor_mime_allowlist: list[str] = [
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
extractor_extension_allowlist: list[str] = [
    ".pdf", ".docx", ".xlsx", ".pptx", ".odt", ".rtf",
    ".txt", ".md", ".html", ".htm", ".csv", ".ics",
]
extractor_max_blob_bytes: int = 50 * 1024 * 1024          # 50 MB
extractor_max_extracted_chars: int = 1_000_000            # 1 MB chars
extractor_docling_max_pages: int = 200
extractor_ocr_languages: list[str] = ["en"]

# Arm 4
arm4_fanout_cap: int = 10
```

A blob is eligible for extraction iff
`(mime_type in mime_allowlist) OR (Path(filename).suffix.lower() in extension_allowlist)`.
The OR exists because mail clients frequently mis-set MIME type
(e.g., PDFs sent as `application/octet-stream`).

## Extractor protocol

In `src/localmail/search/extractor.py`:

```python
@dataclass(frozen=True)
class ExtractedText:
    text: str                  # may be '' (sentinel)
    page_count: int | None     # PDFs/Office; None for TXT/MD/HTML/CSV/ICS
    extractor: str             # 'lightweight@1.0' / 'docling@X.Y.Z' /
                               # 'lightweight-empty' / 'size-skipped'

class ExtractorError(Exception):
    """Raised by extractors on irrecoverable failure (parsing, decoding).
    Caller records in failed_extractions and continues to next blob."""

class AttachmentExtractor(Protocol):
    name: str
    version: str
    def supports(self, mime_type: str | None, filename: str) -> bool: ...
    def extract(self, blob_path: Path, mime_type: str | None) -> ExtractedText: ...
```

### `LightweightExtractor`

Pure-Python, no OCR. Per-format dispatch by MIME (with filename-extension
fallback). Implementations are thin wrappers around well-maintained
libraries:

| MIME / ext | Library |
|---|---|
| `application/pdf` / `.pdf` | `pypdf` |
| `…wordprocessingml…` / `.docx` | `python-docx` |
| `…spreadsheetml…` / `.xlsx` | `openpyxl` |
| `…presentationml…` / `.pptx` | `python-pptx` |
| `…opendocument.text` / `.odt` | `odfpy` |
| `application/rtf` / `.rtf` | `striprtf` |
| `text/plain` / `.txt` | stdlib + `chardet` |
| `text/markdown` / `.md` | stdlib (identity) |
| `text/html` / `.html` / `.htm` | `html2text` |
| `text/csv` / `.csv` | stdlib `csv` (rows joined) |
| `text/calendar` / `.ics` | `icalendar` (SUMMARY + DESCRIPTION + LOCATION + DTSTART + attendees, concatenated) |

Total install footprint: ~5-10 MB. Bundled by default with localmail.

### `DoclingExtractor`

Heavy, OCR-capable. Triggered only on PDF blobs where
`LightweightExtractor` returned empty or raised. Lazy-imported via
`try: from docling.document_converter import DocumentConverter`. On
first ImportError, the module emits a **one-shot WARN per process**
(gated by a module-level boolean) with the install hint
`uv sync --extra extraction`. OCR languages from
`SearchConfig.extractor_ocr_languages`.

Optional install via the `[extraction]` uv extra:

```toml
[project.optional-dependencies]
extraction = ["docling>=X.Y"]
```

`uv sync` ships lightweight only; `uv sync --extra extraction` adds
docling.

## Chunking module additions

Existing `src/localmail/search/chunking.py` already has the
`ChunkSpec` type with `kind: Literal["header", "body", "attachment"]`.
Phase 2 adds:

```python
def chunk_attachment_text(
    sha256: bytes,
    text: str,
    cfg: SearchConfig,
) -> list[ChunkSpec]:
    """Token-aware chunking for attachment text. Pure function; no IO.

    - Uses cfg.chunk_target_tokens / chunk_overlap_tokens (existing).
    - Normalizes whitespace (collapses runs of \\n and spaces).
    - Truncates input at cfg.extractor_max_extracted_chars before chunking
      with a marker line "[truncated]" appended to the last chunk.
    """
```

## Search arm

In `src/localmail/search/arms.py`:

```python
def arm_vector_attachment_chunks(
    conn,
    parsed: ParsedQuery,
    cfg: SearchConfig,
    qvec: list[float],
    limit: int,
) -> list[ArmHit]: ...
```

SQL shape:

```sql
WITH ranked AS (
    SELECT c.id, c.sha256, c.text,
           c.embedding_v1 <=> %(qvec)s::halfvec(768) AS dist
    FROM attachment_chunks c
    WHERE c.embedding_v1 IS NOT NULL
    ORDER BY c.embedding_v1 <=> %(qvec)s::halfvec(768)
    LIMIT %(chunk_limit)s
),
fanned AS (
    SELECT m.id AS message_id,
           r.id AS chunk_id,
           r.dist,
           r.text,
           elem ->> 'filename' AS filename,
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
SELECT message_id, chunk_id, dist, text, filename
FROM fanned
WHERE rn <= %(fanout_cap)s
ORDER BY dist
LIMIT %(limit)s
```

- `chunk_limit` is `limit` × a small factor (e.g. 3) to give fan-out
  headroom while keeping the HNSW scan bounded.
- `filter_sql` from `_filter_sql(filters)` (Phase 1) — applies all
  existing operators (`account:`, `folder:`, `after:`, `before:`,
  `from:`, `to:`, `subject:`, `label:`, `has:attachment`) unchanged.
  The CTE already aliases `messages m`, matching what `_filter_sql`
  expects.

### `has:attachment` semantics

**Unchanged from Phase 1.** Filter remains
`messages.attachments != '[]'::jsonb` — "the message has at least one
attachment row". No new "has-extractable-attachment" operator. The
implicit semantic of "this message surfaced via Arm 4" already conveys
that an extractable attachment matched.

### Snippet generation

When an Arm 4 hit wins post-RRF:

- `SearchResult.snippet` = the matched `attachment_chunks.text` (via
  `make_snippet`, same as message-chunk snippets).
- `SearchResult.snippet_source` = `'attachment'`.
- `SearchResult.attachment_filename` = the `filename` from the matched
  JSONB element. The CLI text formatter already prints `[filename]`
  when set; no UX changes required.

### Reranker

`FastEmbedReranker.rerank()` cross-encodes `(query, snippet_text)`
pairs. Attachment chunks plug in identically as text snippets; no
rerank changes.

## Failure handling

Mirrors the existing `embed_worker` pattern exactly. See the
**Architecture** section above for the full per-blob decision tree
covering text / empty / raised outcomes from lightweight and docling.
The transactional discipline around that flow is:

1. `SAVEPOINT extract_blob`.
2. Run the per-blob decision tree (lightweight, optional docling
   fallback for PDFs).
3. On success (text from either extractor): INSERT `attachment_text`,
   `RELEASE SAVEPOINT`, continue.
4. On sentinel outcome (no extractor found text but neither raised an
   unexpected error): INSERT sentinel row, `RELEASE SAVEPOINT`,
   continue.
5. On failure outcome (irrecoverable raise with no successful
   fallback): `ROLLBACK TO SAVEPOINT extract_blob` to discard any
   partial writes, then open a **nested** SAVEPOINT around
   `record_failed_extraction()` (so a logging failure can't kill the
   outer transaction). UPSERT the `failed_extractions` row, bump
   `retry_count`. `RELEASE` the nested SAVEPOINT, continue to the next
   blob.

`retry_count >= cfg.extract_worker_max_retries` excludes the blob from
future sweeps. Recovery: `localmail retry-failed-extractions`.

**Batch-level backend errors do NOT mark blobs as failed.** Transient
IO / disk / connection errors roll back the whole batch and back off
(exponential, 1s → 60s cap). The blobs get re-claimed on the next
sweep. Permanently-broken paths surface via repeated WARNINGs rather
than silently poisoning the queue. This matches Phase 1's stated
discipline for `embed_worker`.

## CLI additions

In [`src/localmail/cli.py`](../../../src/localmail/cli.py):

```
localmail extract-backfill [--no-progress]
    Drain the extraction queue in the foreground; exit when empty.
    Account-agnostic. Useful for one-shot catch-up against a freshly-
    enabled Phase 2 install.

localmail list-failed-extractions [--limit N] [--format text|json]
    Show recent failed_extractions rows.

localmail retry-failed-extractions [--sha256 HEX]
    Clear failed_extractions rows (one specific blob if --sha256 given,
    otherwise all) so extract_worker re-attempts on its next sweep.

localmail search-status [--format text|json]   (extended)
    Adds counts:
      blobs_eligible          # MIME-allowlist matches
      blobs_extracted         # attachment_text rows with text != ''
      blobs_pending           # in allowlist, no attachment_text yet
      attachment_chunks_total
      attachment_chunks_embedded
      failed_extractions
```

## Daemon integration

`Daemon` spawns **one** `extract_worker` thread per process (account-
agnostic, like `embed_worker`). Lifecycle:

- Shares the existing `threading.Event` stop signal.
- Exponential backoff on connection errors: 1s → 60s cap (same as
  IDLE / poll / embed threads).
- SIGTERM / SIGINT clean shutdown via the existing handlers.
- `cfg.extract_worker_enabled = False` lets users disable extraction
  entirely (e.g. on a low-resource machine while keeping vector
  search over message bodies).

If a daemon is already running when migrations 0011-0013 apply, the
new thread spawns on next daemon restart — no in-place migration
coordination is required.

## Acceptance gates

Two gates. Phase 1 also gets re-run as a regression check.

### Gate A — Extraction functional (≥ 95% success on allowlisted blobs)

Fixture corpus built programmatically (CLAUDE.md: no fixture files on
disk). New module `tests/_attachment_corpus.py` produces in-memory bytes
for each fixture, writes them to a temp `attachments_root` per test,
and inserts `attachment_blobs` rows directly.

Coverage:

| Fixture | Builder | What it tests |
|---|---|---|
| Native-text PDF | `reportlab` | lightweight PDF happy path |
| Scanned PDF (text rasterized via PIL, embedded as image in PDF) | `reportlab` + `Pillow` | lightweight returns `''`, docling OCRs |
| DOCX | `python-docx` | lightweight `.docx` |
| XLSX (multi-sheet) | `openpyxl` | lightweight `.xlsx` |
| PPTX (text + speaker notes) | `python-pptx` | lightweight `.pptx` |
| RTF | `striprtf` builder shim | lightweight `.rtf` |
| TXT (UTF-8 + Latin-1) | stdlib | encoding detect |
| MD | stdlib | identity path |
| HTML | `html2text` builder shim | markup stripping |
| CSV | stdlib `csv` | rows-to-text |
| ICS (3 events) | `icalendar` | event-text concat |
| Encrypted PDF | `pikepdf` | **negative**: lands in `failed_extractions` |
| Corrupt PDF (truncated bytes) | manual | **negative**: lands in `failed_extractions` |
| Empty file | `b""` | **negative**: sentinel row |
| 60 MB PDF (size-padded) | manual | **skipped**: size-skipped sentinel |

Tautology caveat: for Office formats we build with the same library we
extract with. That's acceptable — the gate measures "the pipeline
correctly extracts, chunks, and embeds round-trippable canonical
text". The scanned-PDF and encrypted-PDF fixtures exercise the
non-trivial paths.

**Gate**: ≥ 95% of allowlisted, non-negative-test blobs produce a
non-sentinel `attachment_text` row.

### Gate B — Retrieval quality (recall@20 ≥ 0.80, MRR@20 ≥ 0.50)

Each successful fixture is **embedded in a synthetic email** whose
subject and body deliberately do **not** mention the attachment's
distinctive content. Example:

- Subject: `"FYI"`
- Body: `"see attached"`
- Attachment text: `"quarterly performance bonus eligibility criteria"`

20-25 such emails total, plus ~10 "noise" emails (no attachments,
different topics) so the corpus has distinguishable signal-to-noise
versus Phase 1's pure-message corpus.

**Query suite** at `tests/fixtures/attachment_queries.json`, ~25
queries authored against the attachment-only distinctive content (one
or two per attachment-bearing email). Schema matches Phase 1:

```json
{
  "queries": [
    {"lang": "en", "query": "bonus eligibility criteria",
     "relevant_subjects": ["FYI"]}
  ]
}
```

**Harness**: new script `tests/acceptance/run_attachment_eval.py`. Pattern
follows Phase 1's `run_recall_eval.py`:

1. Apply migrations to `LOCALMAIL_TEST_DSN`.
2. TRUNCATE all relevant tables.
3. Seed corpus via `tests._attachment_corpus.build_corpus(conn)` —
   inserts messages, writes blob bytes to a temp attachments root,
   inserts `attachment_blobs` rows.
4. Run `extract_worker` in a loop until extraction queue is empty.
5. Run `embed_worker` in a loop until embedding queue is empty.
6. Run each query through `Searcher(reranker=None)` (un-reranked
   baseline, same as Phase 1).
7. Print per-language table + PASS/FAIL on Gate B.

**Gates**: recall@20 ≥ 0.80, MRR@20 ≥ 0.50.

### Phase 1 non-regression

After Phase 2 lands, `tests/acceptance/run_recall_eval.py` (the
existing harness) must continue to pass. Adding Arm 4 must not
degrade Phase 1's recall@20 = 1.000 / MRR@20 ≥ 0.93 on the
multilingual corpus.

## File layout

```
src/localmail/search/
  arms.py                  # + arm_vector_attachment_chunks
  chunking.py              # + chunk_attachment_text
  embed_worker.py          # extend lazy chunking sweep to attachment_text
  extractor.py             # NEW — protocol + LightweightExtractor + DoclingExtractor
  extract_worker.py        # NEW — run_extract_worker_once + run_extract_worker
  searcher.py              # add arm_vector_attachment_chunks to the arms list
                           #   in Searcher.search(); attachment snippet wiring
migrations/
  0011_attachment_text.sql
  0012_failed_extractions.sql
  0013_attachment_search_indexes.sql
tests/
  _attachment_corpus.py    # synthetic in-memory attachment fixture builder
  acceptance/
    run_attachment_eval.py # Phase 2 acceptance harness
  fixtures/
    attachment_queries.json
  test_extractor.py        # unit tests per extractor format
  test_extract_worker.py   # SAVEPOINT discipline, fallback flow
  test_arm_vector_attachment_chunks.py
  test_arm4_integration.py # fan-out, RRF, snippet wiring
```

CLI changes in `src/localmail/cli.py` (additions only; no rewrites).
Daemon thread spawn in `src/localmail/daemon.py`.

## Open questions

None. All decisions resolved in brainstorm:

1. ~~Default extractor strategy~~ → Lightweight first, docling fallback
   on empty/raise (PDF-only).
2. ~~MIME scope~~ → Documents allowlist + ICS. Images, archives,
   media, binaries skipped.
3. ~~Docling install model~~ → Optional `[extraction]` uv extra +
   auto-suggest at runtime via one-shot WARN.
4. ~~Acceptance gates~~ → Both functional (≥ 95% extraction) and
   retrieval quality (recall@20 ≥ 0.80, MRR@20 ≥ 0.50).
5. ~~`has:attachment` filter semantics~~ → Unchanged from Phase 1.
6. ~~Arm 4 fan-out~~ → SQL-level cap via `arm4_fanout_cap` (default 10),
   ordered by `date_sent DESC`.

## Forward-looking notes

- **Arm 5** (BM25 over `attachment_chunks.text`) — Phase 5 candidate.
  Will require a migration that adds a STORED `fts` generated column
  to `attachment_chunks` (table rewrite) + a GIN index. Cost will be
  paid when query traces show it's worth it.
- **Image OCR** — Phase 5 candidate. Adds `image/*` to the MIME
  allowlist, gated by a new `extractor_image_ocr` flag, routed through
  docling.
- **Archive recursion** (`.zip`, `.tar`) — separate feature, no
  current owner. Likely needs a per-blob extraction-tree model
  (parent_sha256, member_path) which is non-trivial.
- **MCP server (Phase 3)** — independent of Phase 2; will expose Arm 4
  results via the same `search.search()` API surface used by CLI and
  Python consumers.
- **Smart query rewriter (Phase 4)** — independent of Phase 2.
