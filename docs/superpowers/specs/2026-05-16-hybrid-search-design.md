# Hybrid search for localmail — design

**Status:** Approved, ready for implementation planning
**Date:** 2026-05-16
**Author:** Horst Herb, with Claude (brainstorming session)

## Goal

Add a sophisticated search subsystem to localmail that consistently surfaces
the right message even when the user remembers only fuzzy details. "Find the
needle in the haystack even when the user does not recall any exact wordings."
Must be better than Gmail's built-in search for the user's personal archive,
across multiple languages (German, English, Spanish, Norwegian, Japanese as
the user's set; reasonable broader coverage for general users).

The system fuses lexical (BM25) and semantic (vector) retrieval, reranks
candidates with a cross-encoder, and exposes results through a Python API, a
CLI, and an MCP server. Attachments (PDFs, Office documents, etc.) are text-
extracted and searchable on equal footing with message bodies.

## Constraints

These shaped every decision below.

- **Privacy-first, local-only.** No SaaS APIs (no OpenAI, Cohere, Voyage,
  Anthropic for embeddings/reranking/rewriting). Personal mail never leaves
  the host.
- **AGPL-3.0 license.** All third-party deps must be license-compatible. Model
  weights distributed under restrictive terms (e.g. Gemma Terms of Use) may
  be downloaded at runtime but not bundled into the AGPL repository.
- **Cross-platform.** Must work identically on Apple Silicon macOS and Linux
  (with optional CUDA for performance).
- **Postgres-native where reasonable.** The existing stack is psycopg v3 +
  raw SQL + numbered migrations + no ORM. The search subsystem follows the
  same conventions.
- **Read-only with respect to upstream IMAP.** The existing sync invariant
  (localmail never modifies/sends/deletes mail) is preserved; the search
  subsystem only writes to its own new tables.
- **Sync is the critical path.** Embedding, extraction, and search are
  background concerns that must not block or destabilize sync.
- **Golden Rules** (`docs/llm/GOLDEN_RULES.md`):
  1. Clean separation of concerns in modules.
  2. Prefer reusable pure functions over complex classes.
  3. Docstrings on every function/class/method.
  4. No magic numbers — all tunables in a settings module.
  5. Unit tests on every outside-facing function/class/method.
  6. No truncation of data without explicit user approval.
  7. Network/LLM/API calls retry with exponential backoff.
  8. All errors handled, logged, and reported.
  9. Research unfamiliar libraries' documentation before use.
  10. Cross-platform compatibility always in mind.
  11. AGPL-3.0-compatible third-party libraries only.

## Stack decisions (locked)

| Concern | Choice | Alternative (configurable) |
|---|---|---|
| Embedding model | EmbeddingGemma-300M @ 768d | snowflake-arctic-embed2 (Apache-2.0), bge-m3 |
| Embedding backend | `fastembed` (in-process ONNX) | `ollama` (HTTP, added in Phase 4) |
| BM25 backend | `tsvector + ts_rank_cd` (PG built-in, with `setweight` A/B/C/D field weights) | `pg_search` (ParadeDB) as a Phase 5+ upgrade if recall demands it |
| Vector index | `pgvector` HNSW + `halfvec(768)` | — |
| Fusion | Reciprocal Rank Fusion, k=60 | weighted score combo (future) |
| Reranker | `bge-reranker-v2-m3` via `fastembed` | `mxbai-rerank-large-v2` (GPU) |
| Query rewriting (`--smart`) | `qwen2.5:3b` via `ollama`, opt-in | — |
| Attachment extraction | `docling` (canonical) | lightweight combo (pypdf + python-docx + markitdown + striprtf) |
| Surfaces | CLI + Python API + MCP server | — |

License confirmations performed during design:
- `fastembed` Apache-2.0, `docling` MIT, `bge-m3` MIT, EmbeddingGemma under
  Gemma Terms of Use (weights downloaded at runtime, README NOTICE entry
  required, no field-of-use restriction affecting this project).

BM25-backend decision (revised 2026-05-16): Original spec selected
`pg_search` (ParadeDB, AGPL-3.0, verified PG18-compatible). Switched to
PG built-in `tsvector` after install fragility surfaced — ParadeDB
prebuilt binaries don't cover current macOS releases, and the pgrx
source build re-breaks on each PG upgrade. Net quality cost is small
(<3% recall in expectation) once the reranker is on top of the candidate
pool. `pg_search` remains documented as a Phase 5+ upgrade path if
acceptance recall ever proves limited by BM25 quality.

## Architecture overview

Five new modules under `src/localmail/search/`, one new optional package
under `src/localmail/mcp/`, plus additive changes to `cli.py`, `config.py`,
`daemon.py`, and `db.py`.

```
src/localmail/
  search/
    __init__.py             # exports: create_searcher, Searcher, SearchPage, SearchResult,
                            #          ParsedQuery, SearchFilters, QueryParseError
    chunking.py             # pure: chunk_message, chunk_attachment_text,
                            #       strip_quoted_replies, strip_signature, split_by_tokens
    embeddings.py           # EmbeddingBackend protocol; FastEmbedBackend; OllamaBackend (Phase 4)
    extractor.py            # AttachmentExtractor protocol; DoclingExtractor; LightweightExtractor (Phase 2)
    reranker.py             # Reranker protocol; FastEmbedReranker
    query.py                # parse query string -> ParsedQuery (operators + free text)
    rewriter.py             # QueryRewriter protocol; OllamaLLMRewriter (Phase 4)
    searcher.py             # Searcher class; rrf_fuse, make_snippet pure helpers
    embed_worker.py         # background + foreground worker for embedding chunks
    extract_worker.py       # background + foreground worker for extracting attachment text (Phase 2)
  mcp/                      # Phase 3; gated by [mcp] uv extra
    __init__.py
    server.py               # FastMCP server with 5 tools
  cli.py                    # adds: search, search-page, search-grow, embed-backfill,
                            #       extract-backfill, search-status, list-failed-*, retry-failed-*, mcp
  config.py                 # adds SearchConfig pydantic model
  daemon.py                 # adds embed_worker / extract_worker thread spawn
  db.py                     # adds @non-transactional migration header support
migrations/
  0004_search_chunks.sql
  0005_attachment_text.sql                # Phase 2
  0006_search_indexes.sql                 # multilingual tsvector FTS + HNSW for messages
  0007_failed_embeddings.sql
  0008_failed_extractions.sql             # Phase 2
  0009_search_state.sql                   # embedding_models registry
  0010_search_misc_indexes.sql            # Phase 2 — JSONB GIN on messages.attachments
  0011_attachment_search_indexes.sql      # Phase 2 — BM25 + HNSW for attachment_chunks
tests/
  _multilingual_corpus.py                 # synthetic fixture corpus (de/en/es/no/ja)
  acceptance/                             # phase-gate eval scripts
```

### Query data flow

1. Caller (CLI / Python API / MCP) invokes
   `Searcher.search(query, **filters)`.
2. `query.parse()` extracts operators (`from:`, `subject:`, `after:`,
   `has:attachment`, `account:`, `label:`, `folder:`) and free text.
3. If `smart=True`, `rewriter.rewrite()` calls a local LLM (Ollama) to
   produce a cleaned semantic query, BM25 expansion terms, and structured
   filters extracted from natural language ("last summer" → `after`). Bounded
   by `rewriter_timeout_s`; on timeout, falls through to the un-rewritten
   query and logs a warning.
4. `Searcher` issues four arms sequentially against the same DB connection:
   - **Arm 1**: BM25 over `messages` (subject, from, body fields, with
     per-field boosts).
   - **Arm 2**: BM25 over `message_chunks.text`.
   - **Arm 3**: vector cosine over `message_chunks.embedding_v1`.
   - **Arm 4**: vector cosine over `attachment_chunks.embedding_v1`, joined
     to `messages.attachments` JSONB.
   - (**Arm 5**: BM25 over `attachment_chunks.text`. Deferred to Phase 5
     pending query-trace evidence it would help — OCR-laden attachment text
     is typically noisier for lexical match.)
5. Each arm returns up to `candidates_per_arm` hits with
   `(message_id, chunk_id, chunk_table, score, rank)`.
6. `rrf_fuse(arms, k=rrf_k)` reciprocal-rank-fuses the arms, deduplicates
   to one row per `message_id`, keeps the chunk that contributed most for
   snippet generation. Output capped at `rerank_pool_size`.
7. `reranker.rerank(query, [snippet_texts])` cross-encoder-scores each
   `(query, snippet)` pair; results re-sorted by reranker score.
8. Top `page_size` returned as a `SearchPage`, plus a `search_token` keyed
   into a bounded LRU page cache for follow-up `continue_page` /
   `grow_pool` calls without re-running the pipeline.

### Ingestion data flow

- `sync.py` writes messages and attachment blobs unchanged.
- `embed_worker` polls (`SearchConfig.embed_worker_poll_interval_s`):
  - For messages without chunks: runs `chunk_message()`, INSERTs into
    `message_chunks`.
  - For chunks without embeddings (`embedding_v1 IS NULL`): claims a batch
    via `FOR UPDATE SKIP LOCKED`, calls `backend.embed_documents()`,
    UPDATEs in the same transaction. Per-chunk SAVEPOINT isolates failures.
  - Excludes chunks recorded in `failed_embeddings` past
    `embed_worker_max_chunk_retries`.
- `extract_worker` polls (Phase 2; default 30 s):
  - For `attachment_blobs` without `attachment_text` row: claims a batch,
    calls `extractor.extract()`, INSERTs `attachment_text`.
  - Per-blob SAVEPOINT and failure recording into `failed_extractions`.
  - The embed_worker's next sweep then chunks the new `attachment_text`
    into `attachment_chunks` and proceeds to embed those (no LISTEN/NOTIFY
    needed; polling is sufficient at this latency).

The sync daemon depends on neither worker. Disabling both yields a system
that still mirrors mail correctly; search just returns nothing useful until
the workers are turned back on and catch up.

## Schema

Six new migrations. None mutate existing tables; all changes are additive.

### `0004_search_chunks.sql`

```sql
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE message_chunks (
    id              BIGSERIAL    PRIMARY KEY,
    message_id      BIGINT       NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    kind            TEXT         NOT NULL CHECK (kind IN ('header', 'body')),
    chunk_idx       INT          NOT NULL,
    text            TEXT         NOT NULL,
    token_count     INT          NOT NULL,
    embedding_v1    halfvec(768),
    embedded_at     TIMESTAMPTZ,
    UNIQUE (message_id, kind, chunk_idx)
);

CREATE INDEX message_chunks_msg_idx ON message_chunks (message_id);
CREATE INDEX message_chunks_pending_idx
    ON message_chunks (id) WHERE embedding_v1 IS NULL;
```

The `_v1` suffix on `embedding_v1` is intentional: swapping to a new model
with different dimensions means adding `embedding_v2 halfvec(N)` plus a new
HNSW index plus re-backfill, with old/new columns coexisting during the
transition. The `embedding_models` table (0009) tracks which column is live.

### `0005_attachment_text.sql` (Phase 2)

```sql
CREATE TABLE attachment_text (
    sha256          BYTEA       PRIMARY KEY REFERENCES attachment_blobs(sha256) ON DELETE CASCADE,
    extractor       TEXT        NOT NULL,         -- e.g. 'docling@2.20.0'
    extracted_text  TEXT        NOT NULL,
    page_count      INT,
    extracted_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE attachment_chunks (
    id              BIGSERIAL    PRIMARY KEY,
    sha256          BYTEA        NOT NULL REFERENCES attachment_blobs(sha256) ON DELETE CASCADE,
    chunk_idx       INT          NOT NULL,
    text            TEXT         NOT NULL,
    token_count     INT          NOT NULL,
    embedding_v1    halfvec(768),
    embedded_at     TIMESTAMPTZ,
    UNIQUE (sha256, chunk_idx)
);

CREATE INDEX attachment_chunks_blob_idx     ON attachment_chunks (sha256);
CREATE INDEX attachment_chunks_pending_idx  ON attachment_chunks (id) WHERE embedding_v1 IS NULL;
```

Attachment chunks key on `sha256`, not `message_id` — the existing
content-addressable blob design means one chunk row per unique blob,
regardless of how many messages reference it.

### `0006_search_indexes.sql` (`@non-transactional`)

```sql
-- @non-transactional
SET maintenance_work_mem = '2048MB';

-- Drop the original 'english'-only FTS index from 0001; replaced by the
-- multi-field multilingual generated column below.
DROP INDEX IF EXISTS messages_fts_idx;

-- Weighted multilingual tsvector per message:
--   A = subject (highest weight)   B = from address + display name
--   C = body text                  D = to addresses (lowest weight)
-- The 'simple' configuration tokenizes by Unicode word boundaries without
-- per-language stemming — works across DE/EN/ES/JA/NO uniformly.
ALTER TABLE messages
    ADD COLUMN IF NOT EXISTS fts_v2 tsvector
    GENERATED ALWAYS AS (
        setweight(to_tsvector('simple', coalesce(subject, '')), 'A') ||
        setweight(to_tsvector('simple',
            coalesce(from_addr, '') || ' ' || coalesce(from_name, '')), 'B') ||
        setweight(to_tsvector('simple', coalesce(body_text, '')), 'C') ||
        setweight(to_tsvector('simple',
            coalesce(array_to_string(to_addrs, ' '), '')), 'D')
    ) STORED;

CREATE INDEX IF NOT EXISTS messages_fts_v2_idx ON messages USING GIN (fts_v2);

-- Per-chunk tsvector for the chunk-level BM25-ish arm.
ALTER TABLE message_chunks
    ADD COLUMN IF NOT EXISTS fts tsvector
    GENERATED ALWAYS AS (to_tsvector('simple', text)) STORED;

CREATE INDEX IF NOT EXISTS message_chunks_fts_idx ON message_chunks USING GIN (fts);

-- pgvector HNSW (concurrent so live writes don't block).
CREATE INDEX CONCURRENTLY IF NOT EXISTS message_chunks_embedding_v1_hnsw
    ON message_chunks USING hnsw (embedding_v1 halfvec_cosine_ops)
    WITH (m=16, ef_construction=64);
```

This migration is marked non-transactional so the runner can use
`CREATE INDEX CONCURRENTLY` for HNSW (which is incompatible with explicit
transactions). The runner sets `maintenance_work_mem` at the session level
before applying the file (per Task 11 in the Phase 1 plan); 2 GB is enough
for halfvec(768) at ~500 k chunks.

`tsvector` with the `'simple'` configuration is used instead of `pg_search`
to keep Phase 1 install-portable across macOS/Linux without external
extensions. `setweight()` gives the arm field-level boosting via
`ts_rank_cd(ARRAY[D_w, C_w, B_w, A_w]::float4[], fts_v2, query)`, which is
how `SearchConfig.bm25_field_boosts` is passed through. CJK content
(Japanese) tokenizes per Unicode word boundaries — adequate for most
queries; vector arm carries CJK semantic match weight. `pg_search` swap-in
is a Phase 5+ option if recall acceptance falls short.

### `0007_failed_embeddings.sql`, `0008_failed_extractions.sql`

Mirror `failed_messages` shape (see existing `migrations/0003_failed_messages.sql`):

```sql
CREATE TABLE failed_embeddings (
    id              BIGSERIAL    PRIMARY KEY,
    chunk_table     TEXT         NOT NULL CHECK (chunk_table IN ('message_chunks','attachment_chunks')),
    chunk_id        BIGINT       NOT NULL,
    error_class     TEXT         NOT NULL,
    error_message   TEXT         NOT NULL,
    error_traceback TEXT,
    failed_at       TIMESTAMPTZ  NOT NULL DEFAULT now(),
    retry_count     INT          NOT NULL DEFAULT 0,
    last_retry_at   TIMESTAMPTZ,
    UNIQUE (chunk_table, chunk_id)
);

CREATE TABLE failed_extractions (
    sha256          BYTEA        PRIMARY KEY,
    error_class     TEXT         NOT NULL,
    error_message   TEXT         NOT NULL,
    error_traceback TEXT,
    failed_at       TIMESTAMPTZ  NOT NULL DEFAULT now(),
    retry_count     INT          NOT NULL DEFAULT 0,
    last_retry_at   TIMESTAMPTZ
);
```

### `0009_search_state.sql`

```sql
CREATE TABLE embedding_models (
    column_name     TEXT         PRIMARY KEY,     -- e.g. 'embedding_v1'
    backend         TEXT         NOT NULL,        -- 'fastembed' / 'ollama'
    model_name      TEXT         NOT NULL,        -- e.g. 'embeddinggemma'
    dimension       INT          NOT NULL,        -- 768
    activated_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
    retired_at      TIMESTAMPTZ
);
```

Written by the embed worker on first successful batch; read by `Searcher`
at startup to bind to the active column.

### `0010_search_misc_indexes.sql` (Phase 2)

GIN index on `messages.attachments` JSONB so Arm 4's join doesn't
sequential-scan:

```sql
CREATE INDEX messages_attachments_gin ON messages USING GIN (attachments);
```

### `0011_attachment_search_indexes.sql` (Phase 2, `@non-transactional`)

```sql
CREATE INDEX CONCURRENTLY attachment_chunks_embedding_v1_hnsw
    ON attachment_chunks USING hnsw (embedding_v1 halfvec_cosine_ops)
    WITH (m=16, ef_construction=64);
-- Arm 5 (BM25 over attachment_chunks) deferred to Phase 5; index added then.
```

### Storage estimate

For ~100 k messages, average ~5 chunks/message, ~30 k unique attachment blobs
× avg 8 chunks each:
- `message_chunks`: 500 k rows × (avg 600 B text + 1536 B halfvec + overhead) ≈ 1.1 GB
- `attachment_chunks`: 240 k rows × similar ≈ 540 MB
- HNSW indexes (halfvec, m=16): ~30% of vector storage ≈ ~500 MB
- tsvector GIN indexes (messages.fts_v2 + message_chunks.fts): ~8–15 % of indexed text ≈ ~150 MB

**Total search overhead: ~2.5 GB** on top of the existing archive.

## `SearchConfig` — all tunables in one place

A new `SearchConfig` pydantic model is added to `config.py` and referenced
as `LocalmailConfig.search`. Every numeric or strategy parameter in the
design lives here — no magic numbers in any other module (Rule 4).

```python
class SearchConfig(BaseModel):
    """All tunables for the search subsystem. No magic numbers elsewhere."""

    # --- embedding ---
    embedding_backend: Literal["fastembed", "ollama", "auto"] = "fastembed"
    embedding_model: str = "embeddinggemma"
    embedding_dim: int = 768
    ollama_host: str = "http://localhost:11434"
    ollama_retry_max_attempts: int = 5
    ollama_retry_initial_backoff_s: float = 1.0
    ollama_retry_max_backoff_s: float = 60.0
    fastembed_cache_dir: Path | None = None
    fastembed_threads: int = Field(
        default_factory=lambda: min(4, max(1, (os.cpu_count() or 2) // 2))
    )

    # --- chunking ---
    chunk_size_tokens: int = 512
    chunk_overlap_tokens: int = 64
    chunk_strip_quoted_replies: bool = True
    chunk_strip_signatures: bool = True

    # --- BM25 ---
    bm25_field_boosts: dict[str, float] = Field(default_factory=lambda: {
        "subject": 3.0, "from": 2.0, "body": 1.0, "to": 0.5,
    })

    # --- retrieval / fusion ---
    candidates_per_arm: int = 50
    rrf_k: int = 60
    rerank_pool_size: int = 50
    page_size_default: int = 20
    page_size_max: int = 200
    hnsw_ef_search: int = 64
    snippet_width_chars: int = 200

    # --- reranker ---
    reranker_enabled: bool = True
    reranker_backend: Literal["fastembed"] = "fastembed"
    reranker_model: str = "Xenova/bge-reranker-v2-m3"

    # --- query rewriter (--smart) ---
    rewriter_enabled_by_default: bool = False
    rewriter_backend: Literal["ollama"] = "ollama"
    rewriter_model: str = "qwen2.5:3b"
    rewriter_timeout_s: float = 10.0

    # --- attachment extraction ---
    extractor_backend: Literal["docling", "lightweight"] = "docling"
    extractor_max_file_size_mb: int = 100
    extractor_per_blob_timeout_s: int = 300
    extractor_ocr_languages: list[str] = Field(
        default_factory=lambda: ["eng", "deu", "spa", "nor", "jpn"]
    )

    # --- workers ---
    run_embed_worker: bool = True
    embed_worker_batch_size: int = 100
    embed_worker_poll_interval_s: float = 5.0
    embed_worker_max_chunk_retries: int = 3

    run_extract_worker: bool = True
    extract_worker_batch_size: int = 4
    extract_worker_poll_interval_s: float = 30.0
    extract_worker_max_blob_retries: int = 2

    # --- index build ---
    index_build_maintenance_work_mem_mb: int = 2048

    # --- pagination cache ---
    page_cache_size: int = 16
    page_cache_ttl_s: int = 1200

    # --- evaluation / logging ---
    log_queries: bool = False              # Phase 5
```

TOML override example:

```toml
[search]
embedding_backend = "ollama"
embedding_model = "snowflake-arctic-embed2"
candidates_per_arm = 100

[search.bm25_field_boosts]
subject = 4.0
```

## Backend abstractions

### `EmbeddingBackend` protocol

```python
class EmbeddingBackend(Protocol):
    """Embeds batches of texts into fixed-dim float vectors.

    Implementations: FastEmbedBackend (in-process ONNX, default),
    OllamaBackend (HTTP, Phase 4). Both honour the same
    SearchConfig.embedding_model + embedding_dim contract; a dim mismatch
    at startup raises EmbeddingConfigError.
    """

    name: str            # 'fastembed' / 'ollama'
    model: str
    dimension: int

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...
    def embed_query(self, text: str) -> list[float]: ...
    def health_check(self) -> None: ...   # raises on failure; called at startup
```

Two methods because modern embedding models use task-specific instruction
prefixes (`task: search result | query:` vs `task: search result |
document:`); the backend applies them internally.

### `FastEmbedBackend` (default)

- Wraps `fastembed.TextEmbedding(model_name="google/embeddinggemma-300m",
  cache_dir=cfg.fastembed_cache_dir)`.
- First call downloads ~250 MB (qat-q4_0) or ~600 MB (full bf16) to
  `~/.cache/fastembed/`. Not retried — fail loud if download fails.
- Inference releases the GIL; worker can run it on a thread pool.
  `fastembed_threads` defaults to `min(4, cpu_count // 2)`.

### `OllamaBackend` (Phase 4)

- HTTP `POST /api/embed` with native batching.
- Tenacity exponential backoff (Rule 7): `ollama_retry_*` parameters from
  `SearchConfig`. Retries on connection errors, read timeouts, 5xx. Does
  not retry on 4xx (model not pulled, malformed request) — fail-loud config
  errors.
- `health_check()` calls `/api/show`; 404 raises with hint to run
  `ollama pull embeddinggemma`.

### `Reranker` protocol

```python
class Reranker(Protocol):
    """Cross-encoder reranker scoring (query, candidate) pairs."""
    name: str
    model: str

    def rerank(self, query: str, candidates: list[str]) -> list[float]: ...
```

`FastEmbedReranker` wraps fastembed's cross-encoder API (the exact class
name is `TextCrossEncoder` in recent fastembed versions; verify at
implementation time per Rule 9). Runs ONNX on CPU; ~200 ms for 50 pairs on
M-series.

### `AttachmentExtractor` protocol (Phase 2)

```python
@dataclass(frozen=True)
class ExtractedText:
    text: str
    page_count: int | None
    extractor: str    # 'docling@2.20.0' / 'lightweight@1.0' — stored in attachment_text.extractor

class AttachmentExtractor(Protocol):
    name: str
    version: str
    def supports(self, mime_type: str | None, filename: str) -> bool: ...
    def extract(self, blob_path: Path, mime_type: str | None) -> ExtractedText: ...
```

`DoclingExtractor`: handles PDF/DOCX/PPTX/XLSX/HTML/MD/TXT/RTF and scanned
PDFs (EasyOCR with OCR languages from `extractor_ocr_languages`). Lazy
import — `docling` is in an optional `[extraction]` uv extra. First call
downloads ~500 MB of layout/OCR models; not retried.

`LightweightExtractor`: `pypdf` + `python-docx` + `markitdown` + `striprtf`.
No OCR; scanned PDFs return empty text and are recorded in
`failed_extractions` for re-attempt under docling later.

### `QueryRewriter` protocol (Phase 4)

```python
@dataclass(frozen=True)
class RewriteResult:
    rewritten_text: str
    expansion_terms: list[str]
    extracted_filters: SearchFilters

class QueryRewriter(Protocol):
    name: str
    model: str
    def rewrite(self, free_text: str) -> RewriteResult: ...
```

`OllamaLLMRewriter` uses a fixed JSON-output prompt sent to
`rewriter_model` (default `qwen2.5:3b`); parsed against a Pydantic
schema. Timeout (`rewriter_timeout_s`, default 10 s) enforced; on timeout
the caller falls through to the un-rewritten query (Rule 8: logged as
warning, surfaced in `SearchPage.timing_ms`).

## Chunking module

`src/localmail/search/chunking.py` — pure functions only.

```python
@dataclass(frozen=True)
class ChunkSpec:
    kind: Literal["header", "body", "attachment"]
    chunk_idx: int
    text: str
    token_count: int

@dataclass(frozen=True)
class MessageRow:
    """Minimal shape chunking needs from a messages row.

    Hydrated by embed_worker / embed-backfill from the columns it
    selects; never holds the full row. Keeps chunking decoupled from
    the DB read path.
    """
    id: int
    subject: str | None
    from_addr: str | None
    from_name: str | None
    to_addrs: list[str] | None
    date_sent: datetime | None
    body_text: str | None

def chunk_message(msg: MessageRow, cfg: SearchConfig) -> list[ChunkSpec]: ...
def chunk_attachment_text(sha256: bytes, text: str, cfg: SearchConfig) -> list[ChunkSpec]: ...
def strip_quoted_replies(body: str) -> str: ...
def strip_signature(body: str) -> str: ...
def normalize_whitespace(text: str) -> str: ...
def split_by_tokens(text: str, size: int, overlap: int) -> list[str]: ...
```

Token counting uses `tiktoken.encoding_for_model("gpt-4")` as a neutral
approximation across embedding models (chunk size is a soft target, not
hard).

**Per-message policy:**

- **Header chunk** (always exactly one):
  `Subject: ... | From: <name> <addr> | To: ... | Date: ... | <first ~200 tokens of body>`.
  Structured prefix anchors the embedding on metadata.
- **Body chunks** (zero or more): rest of body, `chunk_size_tokens` (512)
  with `chunk_overlap_tokens` (64). Quoted replies and signatures stripped
  by default (the original is intact in `messages.body_text`).
- Body shorter than ~200 tokens: header chunk only; no body chunks.

**Per-attachment policy:** same chunk size/overlap; keyed on
`attachment_blobs.sha256` so the dedup property holds (one blob in N
messages → one chunk row).

Quote-strip regex pack handles:
- `> ` line prefixes (Outlook, classic MUAs).
- `On <date>, <person> wrote:` (Gmail, English).
- `Am <date> schrieb <person>:` (German).
- `El <date>, <person> escribió:` (Spanish).
- Apple Mail nested `<blockquote>` (HTML body, after html2text).
- `-- \n<signature>` and common variants.

## `Searcher` — the engine

```python
class Searcher:
    """Orchestrates the hybrid search pipeline.

    Created once per process; thread-safe (SQL goes through the shared
    psycopg_pool; backends are stateless after init). Methods:
      - search(query, filters, page_size, candidates_per_arm,
               rerank_pool_size, smart=False) -> SearchPage
      - continue_page(search_token, page) -> SearchPage
      - grow_pool(search_token, candidates_per_arm) -> SearchPage
    """

    def __init__(
        self,
        pool: psycopg_pool.ConnectionPool,
        cfg: SearchConfig,
        embeddings: EmbeddingBackend,
        reranker: Reranker | None,
        rewriter: QueryRewriter | None,
    ) -> None: ...
```

Backends are injected so tests can mock them.

### Result types

```python
@dataclass(frozen=True)
class SearchResult:
    message_id: int
    account_id: int
    rank: int                                       # 1-based, page-relative
    score: float                                    # final post-rerank
    rrf_score: float                                # pre-rerank
    subject: str | None
    from_addr: str | None
    from_name: str | None
    date_sent: datetime | None
    snippet: str                                    # ~200 chars around the win
    snippet_source: Literal["header", "body", "attachment"]
    attachment_filename: str | None                 # populated when source == 'attachment'
    matched_chunk_id: int
    matched_chunk_table: Literal["message_chunks", "attachment_chunks"]

@dataclass(frozen=True)
class SearchPage:
    results: list[SearchResult]
    page: int
    page_size: int
    pool_size: int
    candidates_per_arm: int
    has_more_in_pool: bool
    can_grow_pool: bool
    search_token: str | None
    query: ParsedQuery
    timing_ms: dict[str, float]                     # parse, rewrite, retrieve, rerank, total
```

`timing_ms` is non-optional; "why was this slow" is the most common search
question, so we record it on every result.

### Query parsing

```python
@dataclass(frozen=True)
class ParsedQuery:
    free_text: str
    rewritten_text: str | None
    expansion_terms: list[str]
    filters: SearchFilters

@dataclass(frozen=True)
class SearchFilters:
    accounts: list[int] | None         # resolved from account names
    folders: list[str] | None
    from_substr: str | None
    to_substr: str | None
    subject_substr: str | None
    after: date | None
    before: date | None
    has_attachment: bool | None
    label: str | None
    languages: list[str] | None        # reserved, parsed harmlessly
```

Supported operators (Gmail-shaped):
```
from:alice@example.com            from:"Anna Schmidt"
to:bob                            subject:invoice
after:2025-01-01                  before:2025-12-31
has:attachment                    label:work
account:gmail-personal            folder:"[Gmail]/Sent Mail"
```

Parsed with a small hand-rolled tokenizer; tested with a fixture pack
including malformed inputs (raises `QueryParseError` with column number).

### The four arms

Each arm is a pure function `(conn, parsed, cfg) -> list[ArmHit]`. SQL
sketches:

**Arm 1 — tsvector full-text over messages** (`fts_v2` is the weighted
multi-field generated column from migration 0006):
```sql
SELECT m.id AS message_id, NULL::bigint AS chunk_id, 'message' AS chunk_table,
       ts_rank_cd(
           ARRAY[%(d_w)s, %(c_w)s, %(b_w)s, %(a_w)s]::float4[],
           m.fts_v2,
           plainto_tsquery('simple', %(q)s)
       ) AS score,
       ROW_NUMBER() OVER (ORDER BY score DESC) AS rank
FROM messages m
WHERE m.fts_v2 @@ plainto_tsquery('simple', %(q)s)
  AND <filters>
ORDER BY score DESC LIMIT %(k)s;
```
Weight order is PG-standard `[D, C, B, A]`. Searcher maps
`SearchConfig.bm25_field_boosts["to"|"body"|"from"|"subject"]` onto the
four float4 slots; A=subject is the strongest signal by default (3.0).

**Arm 2 — tsvector full-text over message_chunks** (per-chunk `fts`
column from migration 0006):
```sql
SELECT mc.message_id, mc.id AS chunk_id, 'message_chunks' AS chunk_table,
       ts_rank_cd(mc.fts, plainto_tsquery('simple', %(q)s)) AS score,
       ROW_NUMBER() OVER (ORDER BY score DESC) AS rank
FROM message_chunks mc
JOIN messages m ON m.id = mc.message_id
WHERE mc.fts @@ plainto_tsquery('simple', %(q)s)
  AND <filters on m>
ORDER BY score DESC LIMIT %(k)s;
```

**Arm 3 — vector over message_chunks:**
```sql
SET LOCAL hnsw.ef_search = %(ef)s;
SELECT mc.message_id, mc.id AS chunk_id, 'message_chunks' AS chunk_table,
       1.0 - (mc.embedding_v1 <=> %(qvec)s::halfvec) AS score,
       ROW_NUMBER() OVER (ORDER BY mc.embedding_v1 <=> %(qvec)s::halfvec) AS rank
FROM message_chunks mc
JOIN messages m ON m.id = mc.message_id
WHERE mc.embedding_v1 IS NOT NULL AND <filters on m>
ORDER BY mc.embedding_v1 <=> %(qvec)s::halfvec
LIMIT %(k)s;
```

**Arm 4 — vector over attachment_chunks** (Phase 2):
```sql
SET LOCAL hnsw.ef_search = %(ef)s;
SELECT m.id AS message_id, ac.id AS chunk_id, 'attachment_chunks' AS chunk_table,
       1.0 - (ac.embedding_v1 <=> %(qvec)s::halfvec) AS score,
       ROW_NUMBER() OVER (ORDER BY ac.embedding_v1 <=> %(qvec)s::halfvec) AS rank
FROM attachment_chunks ac
JOIN messages m ON m.attachments @> jsonb_build_array(
    jsonb_build_object('sha256', encode(ac.sha256, 'hex')))
WHERE ac.embedding_v1 IS NOT NULL AND <filters on m>
ORDER BY ac.embedding_v1 <=> %(qvec)s::halfvec
LIMIT %(k)s;
```

The `messages.attachments` JSONB join uses the GIN index from
`0010_search_misc_indexes.sql`.

### RRF fusion (pure function)

```python
def rrf_fuse(arms: list[list[ArmHit]], k: int) -> list[FusedHit]:
    """Reciprocal Rank Fusion across arms.

    For each (message_id, chunk_id) appearing in any arm, sums
    1/(k + rank_in_arm). Deduplicates to one FusedHit per message_id by
    keeping the chunk with the highest contribution to the fused score
    (so the snippet comes from the chunk that 'earned' the rank).
    Returns hits sorted by rrf_score descending, length up to
    rerank_pool_size.
    """
```

### Snippet generation (pure function)

```python
def make_snippet(chunk_text: str, query_terms: list[str], width: int) -> str:
    """~`width`-char window around the strongest query-term match.

    For chunks with no query-term match (pure vector hits), returns the
    first `width` chars. Header chunks shorter than `width` are returned
    in full. For attachment hits, the caller prepends '[<filename>]'.
    """
```

`width` defaults to `cfg.snippet_width_chars` (200) — wider than Gmail's
~150, narrower than full-message preview, calibrated against human
judgment effort.

### Pagination & page cache

```python
class _PageCache:
    """Bounded LRU + TTL cache of reranked candidate pools.

    Entry: (parsed_query, candidates_per_arm, rerank_pool_size,
            reranked_hits). Pages slice into reranked_hits.
    Eviction: LRU on size (default 16), TTL on age (default 1200 s).
    """
```

- `search()` populates an entry, returns `SearchPage` with
  `search_token = uuid4().hex[:16]`.
- `continue_page(token, page)` slices the cached entry. Raises
  `PageOutOfPoolError` if `page > ceil(pool_size / page_size)` — the
  caller's signal to either accept the end or call `grow_pool`.
- `grow_pool(token, candidates_per_arm)` re-runs the pipeline with the
  larger pool, replaces the cache entry, returns page 1. **Explicit, not
  auto** — needle-in-haystack queries are intentional; the latency
  cost (~1–2 s) deserves to be visible.

Three reasons for caching: no silent truncation (Rule 6), reranker cost,
embedding cost.

`--no-cache` returns `search_token = None`; subsequent paging requires
re-running.

## Surfaces

### Python API (canonical)

```python
from localmail.search import (
    create_searcher, Searcher, SearchPage, SearchResult,
    ParsedQuery, SearchFilters, QueryParseError,
)

def create_searcher(cfg: LocalmailConfig | None = None) -> Searcher:
    """Build a Searcher using config defaults. Convenience factory.

    Holds long-lived backend handles; reuse the returned instance across
    queries. For tests or custom DI, construct Searcher directly with
    explicit backends.
    """
```

That's the entire public surface. Internal modules are accessible but not
re-exported.

### CLI verbs (new)

```
localmail search "query" [options]
localmail search-page <token> <page>
localmail search-grow <token> [--candidates N]
localmail embed-backfill [options]
localmail extract-backfill [options]              # Phase 2
localmail search-status
localmail list-failed-embeddings [options]
localmail retry-failed-embeddings
localmail list-failed-extractions [options]      # Phase 2
localmail retry-failed-extractions               # Phase 2
localmail mcp                                     # Phase 3, requires [mcp] extra
```

`localmail search` flags:
```
--account NAME              --folder NAME
--after YYYY-MM-DD          --before YYYY-MM-DD
--from STR                  --to STR
--subject STR               --label NAME
--has-attachment
--page-size INT             --candidates-per-arm INT
--rerank-pool INT           --no-rerank
--smart                     --no-cache
--format text|json|jsonl    --verbose
```

Default text output is tabular with snippets and includes pagination
hints (`localmail search-page TOKEN 2` and `localmail search-grow TOKEN
--candidates 200`). JSON output emits one `SearchPage` dict.

`localmail embed-backfill` runs `run_embed_worker` synchronously until both
chunk queues are drained, then exits. Progress to stderr (rich progress
bar). Same code as the daemon's background thread.

### MCP server (Phase 3, `[mcp]` extra)

`localmail mcp` spawns a FastMCP stdio server with five tools:

```python
@app.tool() def search(query, smart=False, page_size=20,
                       candidates_per_arm=None, rerank_pool_size=None,
                       account=None, folder=None, after=None, before=None,
                       from_addr=None, to=None, subject=None,
                       has_attachment=None, label=None) -> dict: ...
@app.tool() def search_page(search_token, page) -> dict: ...
@app.tool() def search_grow(search_token, candidates_per_arm) -> dict: ...
@app.tool() def get_message(message_id, include_body=True,
                            include_attachments=False) -> dict: ...
@app.tool() def get_attachment(sha256, mode: Literal["text","metadata"]) -> dict: ...
```

The MCP layer is a thin translation to/from `SearchPage`; same `Searcher`
semantics as CLI. Cache is per-process, so a client gets a consistent pool
across page calls within a session.

### Deliberately not shipped

- No HTTP/JSON API (MCP serves agents; future FastAPI layer would reuse
  the same thin wrappers).
- No web UI (per existing project posture).
- No persistent search history table (page cache is in-memory only;
  history is a UX feature for consumers, not an engine feature).
- No multi-account result-merging UX, no thread grouping, no saved
  searches/alerts, no write-back to IMAP, no GPU-specific tuning paths.

## Worker design

### `embed_worker`

```python
def run_embed_worker(ctx: WorkerContext) -> None:
    """Background worker: fills embeddings for chunks where embedding_v1 IS NULL.

    Pattern mirrors run_poll_loop / run_inbox_idle_loop:
      - read SearchConfig.embed_worker_* once
      - loop until ctx.stop.is_set()
      - lazily chunk any messages that have no chunks yet
      - claim a batch of chunks with FOR UPDATE SKIP LOCKED
      - embed batch, write back in the same transaction
      - per-chunk SAVEPOINT to isolate poison chunks
      - on per-chunk exception: ROLLBACK TO SAVEPOINT chunk;
        record_failed_embedding; continue
      - sleep poll_interval_s between sweeps
    """
```

Claim query excludes chunks past `embed_worker_max_chunk_retries` so
poison chunks don't loop:

```sql
WITH claimed AS (
    SELECT id, text FROM {chunks_table}
    WHERE embedding_v1 IS NULL
      AND NOT EXISTS (
          SELECT 1 FROM failed_embeddings fe
          WHERE fe.chunk_table = %s AND fe.chunk_id = {chunks_table}.id
            AND fe.retry_count >= %s
      )
    ORDER BY id LIMIT %s FOR UPDATE SKIP LOCKED
)
SELECT id, text FROM claimed;
```

To force retry after a fix: `localmail retry-failed-embeddings` clears
`failed_embeddings` rows.

One worker total (not one per account) — embedding throughput is
backend-bound, not IMAP-bound.

### `extract_worker` (Phase 2)

```python
def run_extract_worker(ctx: WorkerContext) -> None:
    """Background worker: extract text for attachment_blobs missing attachment_text.

    Claims via LEFT JOIN; per-blob SAVEPOINT; per-blob timeout
    (extractor_per_blob_timeout_s, default 300 s); records failures into
    failed_extractions with the same UNIQUE-on-sha256 idempotency.
    """
```

Smaller batches (`extract_worker_batch_size = 4`) because docling is
heavy. Oversized blobs (`> extractor_max_file_size_mb`) recorded with
`error_class = "OversizedBlobError"`.

### Reactivity (extraction → chunking → embedding)

Pure polling. After `attachment_text` is INSERTed, the next
`embed_worker` sweep (within `embed_worker_poll_interval_s`, default 5 s)
discovers it, runs `chunk_attachment_text`, INSERTs `attachment_chunks`,
then embeds them on subsequent sweeps. No LISTEN/NOTIFY — two fewer
moving parts and 5 s latency is fine for a background pipeline.

## Phasing

Each phase leaves the system in a coherent, usable state. End-of-phase
gates are concrete acceptance criteria (Rule 6 in spirit: no truncated
success claims).

### Phase 1 — Hybrid message search

**Scope:**
- Migrations: `0004`, `0006`, `0007`, `0009`.
- New modules: `search/{chunking,embeddings,reranker,query,searcher,
  embed_worker,__init__}.py`. `embeddings.py` ships `FastEmbedBackend`
  only.
- `SearchConfig` added to `config.py`.
- Migration runner learns `-- @non-transactional` header in `db.py`.
- Daemon spawns `embed_worker` thread when `run_embed_worker` is true.
- CLI: `search`, `search-page`, `search-grow`, `embed-backfill`,
  `search-status`, `list-failed-embeddings`, `retry-failed-embeddings`.
- Python API: `localmail.search.create_searcher`, `Searcher`.
- Test fixtures: `tests/_multilingual_corpus.py` builds ~50 synthetic
  emails across de/en/es/no/ja using existing `_eml.py` patterns.

**Acceptance:**
- All existing tests pass; no regression in sync/daemon.
- Unit tests pass: chunking pure functions, query parser, RRF function,
  snippet generation, FastEmbedBackend health check, SearchConfig
  validation.
- Integration tests pass against `localmail_test` DB: embed_worker fills
  chunks, Searcher returns results, pagination round-trips through cache,
  `grow_pool` re-runs correctly.
- **Multilingual recall spot-check**: 20 ground-truth queries per
  language for de/en/es/ja (Norwegian: full-text-only baseline is
  acceptable per user constraint; no embedding threshold). Targets:
  **recall@20 ≥ 80%** and **MRR@20 ≥ 0.5** for de/en/es/ja. If recall
  below target, document the gap before moving to Phase 2 — never
  silently accept.
- **Latency budget**: p50 < 800 ms, p95 < 1.5 s for default settings on
  a 50 k-message corpus (M-series Mac, fastembed qat-q4_0, no rerank
  cache miss). Reported by `search-status --benchmark`.
- README updated: "Search" section with installation hint, Gemma Terms
  note, OSI-alternative model swap instructions.

### Phase 2 — Attachment search

**Scope:**
- Migrations: `0005`, `0008`, `0010`, `0011`.
- New modules: `search/{extractor,extract_worker}.py`.
- pyproject: `[project.optional-dependencies] extraction = ["docling>=2.20"]`;
  lightweight extractor deps (`pypdf`, `python-docx`, `markitdown`,
  `striprtf`) in main deps.
- Daemon spawns `extract_worker` thread when `run_extract_worker` is
  true and an extractor backend is importable.
- Embed worker also chunks `attachment_text` rows it sees first.
- Searcher gains Arm 4; snippet generator handles attachment hits with
  filename + page reference.
- CLI: `extract-backfill`, `list-failed-extractions`,
  `retry-failed-extractions`.

**Acceptance:**
- All Phase 1 tests still pass.
- New unit tests: extractor protocol (mock), lightweight + docling
  backend smoke tests (docling test marked `pytest.mark.slow`, opt-in),
  extract_worker idempotency, attachment chunk dedup (same blob in two
  messages → one row).
- **Attachment retrieval acceptance**: 10 ground-truth queries where
  relevant content lives in a PDF/DOCX, not the body. Target: recall@20
  ≥ 80%, snippet correctly identifies source attachment filename.
- **Docling smoke**: known-good PDF (corporate invoice, 5 pages, mixed
  text+table) extracts in < 30 s on M-series CPU with reasonable text
  quality (manual eyeball, recorded as a regression fixture).
- **Lightweight backend works without docling**: explicit test that
  uninstalled docling falls through to lightweight extractor when
  configured.

### Phase 3 — MCP server

**Scope:**
- New package: `src/localmail/mcp/{__init__,server}.py`.
- pyproject: `[project.optional-dependencies] mcp = ["mcp>=1.0"]`.
- CLI verb `localmail mcp` registered when `mcp` extra is installed;
  otherwise prints install hint.
- Five tools: `search`, `search_page`, `search_grow`, `get_message`,
  `get_attachment`.
- Searcher process-local, lazy-initialized on first tool call.

**Acceptance:**
- All Phase 1 + 2 tests still pass.
- MCP integration test: spin up the FastMCP server via
  `mcp.client.stdio`, call each tool, verify JSON shape matches
  `SearchPage` / message / attachment schemas.
- **End-to-end manual test** (recorded as runbook in
  `docs/mcp-usage.md`): Claude Desktop configured with localmail MCP,
  asks a vague natural-language question, returns hits, drills in with
  `get_message`, returns attachment text via `get_attachment`. One
  round-trip transcript captured for regression.

### Phase 4 — `--smart` query rewriting + Ollama embedding alternative

**Scope:**
- New module: `search/rewriter.py` (`QueryRewriter` protocol,
  `OllamaLLMRewriter`, fixed JSON-output prompt template).
- `OllamaBackend` added to `embeddings.py` (was deferred from Phase 1).
- `--smart` flag on `localmail search`; `smart=True` param on MCP
  `search` tool.
- Tenacity retry wrappers shared between Ollama embeddings/rewriter
  calls (Rule 7).
- Timeout enforcement with graceful fallback (logged warning, falls
  through to un-rewritten query, marks `timing_ms.rewrite = None`).
- pyproject: add `tenacity` to main deps.

**Acceptance:**
- All prior tests still pass.
- New unit tests: rewriter prompt produces valid JSON for a fixture pack
  of 20 vague queries (mocked Ollama responses); timeout path returns
  un-rewritten query and logs warning; Ollama-unreachable raises a
  clear error on `--smart` startup health check.
- Integration test (in `pytest.mark.integration`): real Ollama returns
  schema-valid JSON for the fixture queries.
- **Smart retrieval acceptance**: 15 vague queries (e.g. "that thing
  about the vacation in Italy last summer") with known-relevant
  messages. Target: smart mode achieves **recall@20 ≥ 70%** where
  un-rewritten retrieval achieves < 40%. Per-query absolute delta
  recorded in `tests/fixtures/smart_eval_results.json`.
- **Ollama embedding swap works**: `embedding_backend = "ollama"` +
  `embedding_model = "embeddinggemma"` produces working search without
  reinstalling anything else; full Phase 1 multilingual eval re-run
  shows recall within 2% of fastembed baseline.

### Phase 5 — Optional Arm 5, evaluation harness, polish

**Scope:**
- Optional query trace logging (off by default;
  `cfg.search.log_queries = True` writes anonymized parsed queries +
  timings to a `query_log` table for offline analysis).
- Evaluation harness: `localmail eval --suite multilingual|attachments|
  smart|all` re-runs ground-truth queries and reports
  recall/MRR/latency vs baseline JSON.
- Conditional Arm 5 (BM25 over `attachment_chunks`): only added if
  query logs show ≥10 % of misses would have been caught by it.
- Documentation: full README rewrite for the search subsystem, example
  queries, MCP usage runbook, embedding-model-swap guide.

**Acceptance:**
- Eval harness re-runs all prior acceptance suites with one command.
- Documentation complete and accurate to current behavior.
- No open `failed_embeddings` / `failed_extractions` rows in author's
  personal archive after a full backfill (proof of failure-path
  correctness).

## Testing posture

- **Unit tests** for every pure function (`chunking`, `rrf_fuse`,
  `make_snippet`, query parsing, `SearchConfig` validation) — Rule 5.
- **Integration tests** opt in via `LOCALMAIL_TEST_INTEGRATION=1`,
  matching the existing `LOCALMAIL_TEST_DSN` pattern. Real fastembed,
  real Ollama (when relevant), real Postgres at
  `localmail_test`.
- **Backend protocols mocked in unit tests**; real backends exercised
  only in integration tests.
- **Acceptance suites** in `tests/acceptance/` produce machine-readable
  reports. CI runs unit + integration; acceptance runs manually before
  phase sign-off.
- **No `.eml` fixtures on disk** — extend `tests/_eml.py` and
  `tests/_multilingual_corpus.py` programmatically per existing
  convention.

## Open questions for the implementation phase

These are documented here because they were noted during design but
deliberately not resolved (premature without code in hand):

1. **Exact `fastembed` API for cross-encoder rerank.** Recent versions
   expose `TextCrossEncoder`; older expose `Reranker`. Verify against
   the installed version at implementation start (Rule 9). Wrap behind
   `FastEmbedReranker` so the choice is internal.
2. **`tsvector` multilingual coverage in practice.** The `'simple'`
   configuration tokenizes on Unicode word boundaries without stemming —
   this works well for DE/EN/ES/NO but is suboptimal for CJK. Japanese
   queries depend more heavily on the vector arm + reranker; if recall
   is unacceptable, add a `pg_trgm` substring-similarity arm as a
   Phase 5 enhancement, or revisit `pg_search` (deferred from Phase 1
   due to install fragility on current macOS).
3. **`tiktoken` vs model-native tokenizer for chunk-size budgeting.**
   Going with `tiktoken` for portability; if chunk sizes systematically
   over/undershoot the embedding model's true token budget by > 10 %,
   switch to model-native tokenization in a follow-up.
4. **Quote-stripping regex coverage.** The starting pack handles common
   Western and German/Spanish forms; if Norwegian or Japanese mail in
   the user's archive surfaces patterns the regex misses, extend in a
   follow-up rather than blocking Phase 1.
