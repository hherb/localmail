# Hybrid Search Phase 1 — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship hybrid BM25 + vector search over localmail messages (no attachments yet), exposed via Python API and CLI, with a background embed worker filling vectors as new mail arrives.

**Architecture:** Per-arm Python-orchestrated retrieval (4 SQL queries — Phase 1 ships 3 arms over messages only) → RRF fusion → cross-encoder rerank → bounded LRU page cache. All numeric tunables in `SearchConfig`. Embeddings via fastembed (EmbeddingGemma-300M @ 768d, halfvec storage). BM25 via pg_search (ParadeDB). Vector index: pgvector HNSW.

**Tech Stack:** Python 3.12, psycopg v3 + raw SQL, pgvector + pg_search Postgres extensions, fastembed (ONNX), tiktoken (chunk-size budgeting), click, pydantic v2, structlog, pytest.

**Spec:** [docs/superpowers/specs/2026-05-16-hybrid-search-design.md](../specs/2026-05-16-hybrid-search-design.md)

---

## Prerequisites (one-time, before starting)

```bash
# Install pg_search extension (ParadeDB) for your Postgres 18 install.
# macOS Homebrew Postgres 18:
#   curl -L -o /tmp/pg_search.tar.gz https://github.com/paradedb/paradedb/releases/download/v0.23.4/postgresql-18-pg-search_0.23.4-1.macos.13_amd64.deb
# Linux/Ubuntu: install the .deb directly via apt.
# Verify: psql -c 'CREATE EXTENSION pg_search;' in localmail_test.

# pgvector should already be present (used by other Postgres users).
# Verify: psql -c 'CREATE EXTENSION vector;' in localmail_test.
```

Then add the test integration marker so existing pytest runs skip slow fastembed tests by default:

```bash
# add to pyproject.toml [tool.pytest.ini_options]
# markers = [
#     "slow: tests that load real embedding/reranker models (opt-in)",
#     "integration: tests requiring local services (opt-in via env)",
# ]
```

---

## Task 1: Add `@non-transactional` migration header to `db.py`

**Why:** Migration 0006 must create HNSW indexes with `CREATE INDEX CONCURRENTLY`, which Postgres forbids inside an explicit transaction. The runner needs to detect a `-- @non-transactional` header and switch to autocommit for those files.

**Files:**
- Modify: `src/localmail/db.py`
- Test: `tests/test_db_migrations.py`

- [ ] **Step 1: Write failing test**

Create `tests/test_db_migrations.py`:

```python
"""Tests for the migration runner's @non-transactional detection."""

from __future__ import annotations

from pathlib import Path

import pytest

from localmail.db import _is_non_transactional, apply_migrations


def test_non_transactional_header_detected(tmp_path: Path) -> None:
    sql = "-- @non-transactional\nCREATE INDEX CONCURRENTLY foo_idx ON foo (x);\n"
    assert _is_non_transactional(sql) is True


def test_no_header_means_transactional(tmp_path: Path) -> None:
    sql = "-- ordinary migration\nCREATE TABLE foo (id int);\n"
    assert _is_non_transactional(sql) is False


def test_header_must_be_in_leading_comment_block(tmp_path: Path) -> None:
    """A @non-transactional marker buried mid-file is ignored — too risky."""
    sql = "CREATE TABLE foo (id int);\n-- @non-transactional\nCREATE INDEX bar ON foo (id);\n"
    assert _is_non_transactional(sql) is False
```

- [ ] **Step 2: Verify failing**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_db_migrations.py -v
```
Expected: `ImportError: cannot import name '_is_non_transactional'`.

- [ ] **Step 3: Implement in `src/localmail/db.py`**

Add at module level (after the imports):

```python
def _is_non_transactional(sql: str) -> bool:
    """True if the migration's leading comment block contains the marker.

    Format expected at the very top of a .sql file:
        -- @non-transactional
    Lines after the first non-comment, non-blank line are ignored — the
    marker is treated as a directive, not a free-floating comment.
    """
    for raw in sql.splitlines():
        line = raw.strip()
        if not line:
            continue
        if not line.startswith("--"):
            return False
        if "@non-transactional" in line:
            return True
    return False
```

Update `apply_migrations` to honour it. Replace the existing for-loop:

```python
def apply_migrations(dsn: str) -> list[str]:
    """Apply any unapplied .sql migrations. Returns the revisions newly applied.

    A migration whose first comment block contains '@non-transactional'
    runs in autocommit mode on its own connection so it can use
    CREATE INDEX CONCURRENTLY or other transaction-incompatible DDL.
    """
    applied: list[str] = []
    with psycopg.connect(dsn, autocommit=False) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    revision   TEXT        PRIMARY KEY,
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
            cur.execute("SELECT revision FROM schema_migrations")
            done = {row[0] for row in cur.fetchall()}
        conn.commit()

    for path in list_migrations():
        revision = path.stem
        if revision in done:
            continue
        sql = path.read_text()
        if _is_non_transactional(sql):
            with psycopg.connect(dsn, autocommit=True) as nc:
                with nc.cursor() as cur:
                    cur.execute(sql)
                    cur.execute(
                        "INSERT INTO schema_migrations (revision) VALUES (%s)",
                        (revision,),
                    )
        else:
            with psycopg.connect(dsn, autocommit=False) as tc:
                with tc.cursor() as cur:
                    cur.execute(sql)
                    cur.execute(
                        "INSERT INTO schema_migrations (revision) VALUES (%s)",
                        (revision,),
                    )
                tc.commit()
        applied.append(revision)

    return applied
```

- [ ] **Step 4: Verify passing**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_db_migrations.py -v
```
Expected: 3 passing. Then run the full suite — no regressions:
```bash
unset VIRTUAL_ENV && uv run pytest -q
```

- [ ] **Step 5: Commit**

```bash
git add src/localmail/db.py tests/test_db_migrations.py
git commit -m "feat(db): support @non-transactional migrations for CREATE INDEX CONCURRENTLY"
```

---

## Task 2: Add `SearchConfig` to `config.py`

**Files:**
- Modify: `src/localmail/config.py`
- Test: `tests/test_config.py`

- [ ] **Step 1: Read existing `config.py`** to learn how `LocalmailConfig` is wired (the new `SearchConfig` will be a field on it with a default factory).

```bash
sed -n '1,80p' src/localmail/config.py
```

- [ ] **Step 2: Write failing test** — append to `tests/test_config.py`:

```python
from localmail.config import LocalmailConfig, SearchConfig


def test_search_config_has_sane_defaults():
    cfg = SearchConfig()
    assert cfg.embedding_backend == "fastembed"
    assert cfg.embedding_model == "embeddinggemma"
    assert cfg.embedding_dim == 768
    assert cfg.candidates_per_arm == 50
    assert cfg.rrf_k == 60
    assert cfg.rerank_pool_size == 50
    assert cfg.page_size_default == 20
    assert cfg.page_size_max == 200
    assert cfg.snippet_width_chars == 200
    assert cfg.run_embed_worker is True
    assert cfg.chunk_size_tokens == 512
    assert cfg.chunk_overlap_tokens == 64


def test_search_config_attached_to_localmail_config():
    # NOTE: top-level config requires `database`; construct via model_validate.
    cfg = LocalmailConfig.model_validate({"database": {"dsn": "x"}})
    assert isinstance(cfg.search, SearchConfig)
    assert cfg.search.embedding_backend == "fastembed"


def test_search_config_overrides_via_dict():
    cfg = SearchConfig.model_validate({
        "embedding_backend": "ollama",
        "candidates_per_arm": 100,
        "bm25_field_boosts": {"subject": 5.0},
    })
    assert cfg.embedding_backend == "ollama"
    assert cfg.candidates_per_arm == 100
    assert cfg.bm25_field_boosts["subject"] == 5.0
```

- [ ] **Step 3: Verify failing**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_config.py::test_search_config_has_sane_defaults -v
```
Expected: `ImportError`.

- [ ] **Step 4: Implement in `src/localmail/config.py`**

Add to the imports:
```python
import os
from pathlib import Path
from typing import Literal
from pydantic import BaseModel, Field
```

Add the `SearchConfig` class above `LocalmailConfig`:

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

    # --- query rewriter (Phase 4) ---
    rewriter_enabled_by_default: bool = False
    rewriter_backend: Literal["ollama"] = "ollama"
    rewriter_model: str = "qwen2.5:3b"
    rewriter_timeout_s: float = 10.0

    # --- attachment extraction (Phase 2) ---
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

    # --- evaluation / logging (Phase 5) ---
    log_queries: bool = False
```

In `LocalmailConfig`, add the field:
```python
search: SearchConfig = Field(default_factory=SearchConfig)
```

- [ ] **Step 5: Verify passing + no regressions**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_config.py -v
unset VIRTUAL_ENV && uv run pytest -q
```

- [ ] **Step 6: Commit**

```bash
git add src/localmail/config.py tests/test_config.py
git commit -m "feat(config): add SearchConfig with all search tunables"
```

---

## Task 3: Migrations 0004 (message_chunks), 0007 (failed_embeddings), 0009 (embedding_models)

**Files:**
- Create: `migrations/0004_search_chunks.sql`, `migrations/0007_failed_embeddings.sql`, `migrations/0009_search_state.sql`
- Test: `tests/test_search_schema.py`

(Migration 0006 — pg_search BM25 + HNSW — comes in Task 12, after we know the BM25 / vector code shape. Migrations 0005/0008/0010/0011 are Phase 2.)

- [ ] **Step 1: Write failing test** — `tests/test_search_schema.py`:

```python
"""Verify search-related tables/columns/indexes exist after migration."""

from __future__ import annotations


def test_message_chunks_table_shape(db_conn) -> None:
    with db_conn.cursor() as cur:
        cur.execute("""
            SELECT column_name, data_type, is_nullable
            FROM information_schema.columns
            WHERE table_name = 'message_chunks'
            ORDER BY ordinal_position
        """)
        rows = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
    assert rows["id"][0] == "bigint"
    assert rows["message_id"] == ("bigint", "NO")
    assert rows["kind"] == ("text", "NO")
    assert rows["chunk_idx"] == ("integer", "NO")
    assert rows["text"] == ("text", "NO")
    assert rows["token_count"] == ("integer", "NO")
    assert rows["embedded_at"][1] == "YES"
    # halfvec shows up as USER-DEFINED — verify by name
    cur_type = rows["embedding_v1"][0]
    assert cur_type in ("USER-DEFINED", "halfvec")


def test_message_chunks_unique_constraint(db_conn) -> None:
    with db_conn.cursor() as cur:
        cur.execute("""
            INSERT INTO accounts (name, email_address, imap_host, auth_method)
            VALUES ('t', 't@x', 'h', 'password') RETURNING id
        """)
        acct = cur.fetchone()[0]
        cur.execute("""
            INSERT INTO messages (account_id, raw_sha256, headers, raw_bytes, size_bytes)
            VALUES (%s, %s, '{}'::jsonb, %s, 1) RETURNING id
        """, (acct, b'\\x00' * 32, b'x'))
        msg = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO message_chunks (message_id, kind, chunk_idx, text, token_count)"
            " VALUES (%s, 'body', 0, 'hi', 1)", (msg,))
        try:
            cur.execute(
                "INSERT INTO message_chunks (message_id, kind, chunk_idx, text, token_count)"
                " VALUES (%s, 'body', 0, 'hi', 1)", (msg,))
            raise AssertionError("expected unique violation")
        except Exception as exc:
            assert "unique" in str(exc).lower() or "duplicate" in str(exc).lower()


def test_failed_embeddings_table_exists(db_conn) -> None:
    with db_conn.cursor() as cur:
        cur.execute("SELECT to_regclass('failed_embeddings')")
        assert cur.fetchone()[0] == "failed_embeddings"


def test_embedding_models_table_exists(db_conn) -> None:
    with db_conn.cursor() as cur:
        cur.execute("SELECT to_regclass('embedding_models')")
        assert cur.fetchone()[0] == "embedding_models"
```

Update `tests/conftest.py` `db_conn` fixture's TRUNCATE to include the new tables (so existing tests stay clean):

```python
cur.execute(
    "TRUNCATE accounts, mailboxes, messages, message_labels, "
    "attachment_blobs, failed_messages, message_chunks, "
    "failed_embeddings, embedding_models RESTART IDENTITY CASCADE"
)
```

- [ ] **Step 2: Verify failing**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_search_schema.py -v
```
Expected: errors about missing tables.

- [ ] **Step 3: Write `migrations/0004_search_chunks.sql`**

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

- [ ] **Step 4: Write `migrations/0007_failed_embeddings.sql`**

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
```

- [ ] **Step 5: Write `migrations/0009_search_state.sql`**

```sql
CREATE TABLE embedding_models (
    column_name     TEXT         PRIMARY KEY,
    backend         TEXT         NOT NULL,
    model_name      TEXT         NOT NULL,
    dimension       INT          NOT NULL,
    activated_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
    retired_at      TIMESTAMPTZ
);
```

- [ ] **Step 6: Apply + verify**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_search_schema.py -v
```
Expected: 4 passing. Migrations run inside the session-scoped `db_dsn` fixture.

- [ ] **Step 7: Commit**

```bash
git add migrations/0004_search_chunks.sql migrations/0007_failed_embeddings.sql \
        migrations/0009_search_state.sql tests/test_search_schema.py tests/conftest.py
git commit -m "feat(schema): add message_chunks, failed_embeddings, embedding_models tables"
```

---

## Task 4: `chunking.py` — quote/signature stripping + whitespace normalization

**Files:**
- Create: `src/localmail/search/__init__.py` (empty for now), `src/localmail/search/chunking.py`
- Test: `tests/test_chunking.py`

- [ ] **Step 1: Create the empty package**

```bash
mkdir -p src/localmail/search && touch src/localmail/search/__init__.py
```

- [ ] **Step 2: Write failing test** — `tests/test_chunking.py`:

```python
"""Tests for pure chunking helpers."""

from __future__ import annotations

from localmail.search.chunking import (
    normalize_whitespace,
    strip_quoted_replies,
    strip_signature,
)


def test_strip_quoted_replies_gmail_english():
    body = (
        "Hi Anna,\nLooks good — let's meet Tuesday.\nBest, H\n\n"
        "On Tue, Sep 14, 2024 at 10:23, Anna Schmidt <anna@x> wrote:\n"
        "> Hi Horst, I wanted to ask about the Berlin conference\n"
    )
    out = strip_quoted_replies(body)
    assert "Berlin conference" not in out
    assert "Tuesday" in out


def test_strip_quoted_replies_outlook_arrow_lines():
    body = "My answer.\n\n> original line one\n> original line two\n"
    out = strip_quoted_replies(body)
    assert "My answer." in out
    assert "original line" not in out


def test_strip_quoted_replies_german():
    body = (
        "Vielen Dank!\n\n"
        "Am 14. September 2024 um 10:23 schrieb Anna Schmidt <a@x>:\n"
        "> Hallo Horst\n"
    )
    out = strip_quoted_replies(body)
    assert "Vielen Dank!" in out
    assert "Hallo Horst" not in out


def test_strip_quoted_replies_spanish():
    body = (
        "Gracias!\n\nEl 14 de septiembre de 2024, Anna <a@x> escribió:\n> hola\n"
    )
    out = strip_quoted_replies(body)
    assert "Gracias!" in out
    assert "hola" not in out


def test_strip_signature_dash_dash_space():
    body = "Body text here.\nMore body.\n-- \nHorst Herb\nMD\n"
    out = strip_signature(body)
    assert "Body text here." in out
    assert "Horst Herb" not in out


def test_strip_signature_keeps_body_with_no_sig():
    body = "Just a body, no sig at all."
    assert strip_signature(body) == body


def test_normalize_whitespace_collapses_runs():
    assert normalize_whitespace("a   b\n\n\n c") == "a b\n\nc"
    assert normalize_whitespace("   leading \t") == "leading"
```

- [ ] **Step 3: Verify failing**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_chunking.py -v
```

- [ ] **Step 4: Implement** — `src/localmail/search/chunking.py`:

```python
"""Pure-function chunking helpers for the search subsystem.

Splits message bodies into header + body chunks suitable for embedding,
strips email reply chains and signatures so the index doesn't double-count
quoted content. All functions are pure: no IO, no DB, no logging.
"""

from __future__ import annotations

import re

_QUOTE_HEADER_PATTERNS = [
    # English: "On <date>, <name> wrote:"
    re.compile(r"^On .+,\s.+\swrote:\s*$", re.MULTILINE | re.IGNORECASE),
    # German: "Am <date> schrieb <name>:" / "schrieb <name> <addr>:"
    re.compile(r"^Am .+\sschrieb\s.+:\s*$", re.MULTILINE | re.IGNORECASE),
    # Spanish: "El <date>, <name> escribió:"
    re.compile(r"^El .+,\s.+\sescribi[oó]:\s*$", re.MULTILINE | re.IGNORECASE),
    # Generic Outlook divider
    re.compile(r"^-----\s*Original Message\s*-----\s*$", re.MULTILINE | re.IGNORECASE),
    re.compile(r"^From:.+\nSent:.+\nTo:.+", re.MULTILINE),
]

_ARROW_QUOTE_LINE = re.compile(r"^\s*>.*$", re.MULTILINE)
_SIGNATURE_DELIM = re.compile(r"^-- ?$", re.MULTILINE)
_WS_INLINE = re.compile(r"[ \t]+")
_WS_BLANKLINES = re.compile(r"\n{3,}")


def strip_quoted_replies(body: str) -> str:
    """Remove quoted reply chains from an email body.

    Cuts at the first quote-header marker (e.g. 'On ... wrote:', German
    equivalent, Spanish equivalent, '----- Original Message -----'),
    then drops any remaining '>'-prefixed quote lines from what's left.
    """
    earliest: int | None = None
    for pat in _QUOTE_HEADER_PATTERNS:
        m = pat.search(body)
        if m and (earliest is None or m.start() < earliest):
            earliest = m.start()
    truncated = body[:earliest] if earliest is not None else body
    return _ARROW_QUOTE_LINE.sub("", truncated)


def strip_signature(body: str) -> str:
    """Remove an email signature delimited by a '-- ' line (RFC 3676).

    The delimiter is a line containing exactly '-- ' (dash-dash-space) or
    '--'. Everything from the first such delimiter onward is removed.
    """
    m = _SIGNATURE_DELIM.search(body)
    return body[: m.start()] if m else body


def normalize_whitespace(text: str) -> str:
    """Collapse runs of inline whitespace and blank-line runs.

    - Tabs and multi-space runs collapse to single space.
    - Three-or-more consecutive newlines collapse to two (one blank line).
    - Leading/trailing whitespace on each line, and on the whole text, is trimmed.
    """
    lines = [_WS_INLINE.sub(" ", line).strip() for line in text.splitlines()]
    joined = "\n".join(lines)
    return _WS_BLANKLINES.sub("\n\n", joined).strip()
```

- [ ] **Step 5: Verify passing**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_chunking.py -v
```

- [ ] **Step 6: Commit**

```bash
git add src/localmail/search/__init__.py src/localmail/search/chunking.py tests/test_chunking.py
git commit -m "feat(search): add quote/signature stripping + whitespace normalization"
```

---

## Task 5: `chunking.py` — `split_by_tokens`, `ChunkSpec`, `MessageRow`, `chunk_message`

**Files:**
- Modify: `src/localmail/search/chunking.py`
- Modify: `tests/test_chunking.py`
- Modify: `pyproject.toml` (add `tiktoken` dep)

- [ ] **Step 1: Add tiktoken dep**

```bash
unset VIRTUAL_ENV && uv add tiktoken
```

- [ ] **Step 2: Write failing tests** — append to `tests/test_chunking.py`:

```python
from datetime import datetime, timezone

from localmail.config import SearchConfig
from localmail.search.chunking import (
    ChunkSpec,
    MessageRow,
    chunk_message,
    split_by_tokens,
)


def _cfg(**overrides) -> SearchConfig:
    cfg = SearchConfig()
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


def test_split_by_tokens_respects_size_and_overlap():
    text = " ".join(f"word{i}" for i in range(500))
    chunks = split_by_tokens(text, size=100, overlap=20)
    assert len(chunks) >= 5
    # First chunk's last ~20 tokens should appear in chunk 2 (overlap)
    assert chunks[0].split()[-5] in chunks[1]


def test_split_by_tokens_short_input_returns_one_chunk():
    assert split_by_tokens("hello world", size=100, overlap=20) == ["hello world"]


def test_chunk_message_short_body_emits_header_only():
    msg = MessageRow(
        id=1,
        subject="Quick note",
        from_addr="anna@x", from_name="Anna",
        to_addrs=["bob@x"],
        date_sent=datetime(2024, 9, 14, tzinfo=timezone.utc),
        body_text="See you Tuesday.",
    )
    chunks = chunk_message(msg, _cfg())
    assert len(chunks) == 1
    assert chunks[0].kind == "header"
    assert chunks[0].chunk_idx == 0
    assert "Quick note" in chunks[0].text
    assert "See you Tuesday" in chunks[0].text


def test_chunk_message_long_body_emits_header_plus_body_chunks():
    body = " ".join(f"sentence{i}." for i in range(800))
    msg = MessageRow(
        id=2, subject="Long", from_addr=None, from_name=None,
        to_addrs=None, date_sent=None, body_text=body,
    )
    chunks = chunk_message(msg, _cfg(chunk_size_tokens=200, chunk_overlap_tokens=40))
    assert chunks[0].kind == "header"
    assert any(c.kind == "body" for c in chunks)
    body_chunks = [c for c in chunks if c.kind == "body"]
    assert body_chunks[0].chunk_idx == 0
    assert all(c.token_count > 0 for c in chunks)


def test_chunk_message_strips_quoted_reply_when_enabled():
    body = (
        "My fresh content here.\n\n"
        "On Tue, Sep 14, 2024 at 10:23, Anna <a@x> wrote:\n"
        "> the old quoted bits we don't want indexed\n" * 50
    )
    msg = MessageRow(
        id=3, subject="Re:", from_addr=None, from_name=None,
        to_addrs=None, date_sent=None, body_text=body,
    )
    chunks = chunk_message(msg, _cfg(chunk_strip_quoted_replies=True))
    all_text = " ".join(c.text for c in chunks)
    assert "fresh content" in all_text
    assert "old quoted bits" not in all_text


def test_chunk_message_handles_none_body():
    msg = MessageRow(
        id=4, subject="Subject only", from_addr=None, from_name=None,
        to_addrs=None, date_sent=None, body_text=None,
    )
    chunks = chunk_message(msg, _cfg())
    assert len(chunks) == 1
    assert "Subject only" in chunks[0].text
```

- [ ] **Step 3: Verify failing**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_chunking.py -v
```

- [ ] **Step 4: Implement** — append to `src/localmail/search/chunking.py`:

```python
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

import tiktoken

_ENC = tiktoken.get_encoding("cl100k_base")
_HEADER_BODY_INTRO_TOKENS = 200


@dataclass(frozen=True)
class ChunkSpec:
    """One chunk awaiting INSERT into message_chunks or attachment_chunks."""
    kind: Literal["header", "body", "attachment"]
    chunk_idx: int
    text: str
    token_count: int


@dataclass(frozen=True)
class MessageRow:
    """Minimal shape chunk_message needs from a messages row.

    Hydrated from the columns embed_worker selects; keeps chunking
    decoupled from the DB read path.
    """
    id: int
    subject: str | None
    from_addr: str | None
    from_name: str | None
    to_addrs: list[str] | None
    date_sent: datetime | None
    body_text: str | None


def split_by_tokens(text: str, size: int, overlap: int) -> list[str]:
    """Split text into token-windowed chunks of `size` with `overlap` tokens shared.

    Tokenization uses cl100k_base as a neutral approximation across
    embedding models — the resulting chunk sizes are soft targets, not
    hard guarantees on the model's true token budget.
    """
    if size <= 0:
        raise ValueError("size must be > 0")
    if overlap < 0 or overlap >= size:
        raise ValueError("overlap must be in [0, size)")
    tokens = _ENC.encode(text)
    if len(tokens) <= size:
        return [text] if text else []
    out: list[str] = []
    step = size - overlap
    for start in range(0, len(tokens), step):
        window = tokens[start : start + size]
        if not window:
            break
        out.append(_ENC.decode(window))
        if start + size >= len(tokens):
            break
    return out


def _header_text(msg: MessageRow, body_for_intro: str) -> str:
    """Build the structured header-chunk text for one message."""
    parts: list[str] = []
    if msg.subject:
        parts.append(f"Subject: {msg.subject}")
    if msg.from_name or msg.from_addr:
        who = f"{msg.from_name} <{msg.from_addr}>" if msg.from_name else (msg.from_addr or "")
        parts.append(f"From: {who.strip()}")
    if msg.to_addrs:
        parts.append(f"To: {', '.join(msg.to_addrs)}")
    if msg.date_sent:
        parts.append(f"Date: {msg.date_sent.isoformat()}")
    intro_tokens = _ENC.encode(body_for_intro)[:_HEADER_BODY_INTRO_TOKENS]
    if intro_tokens:
        parts.append(_ENC.decode(intro_tokens))
    return " | ".join(parts)


def chunk_message(msg: MessageRow, cfg) -> list[ChunkSpec]:
    """Produce header + body chunks for a message.

    - Header chunk (always exactly one): structured metadata + first ~200 tokens of body.
    - Body chunks: rest of body, split at cfg.chunk_size_tokens with cfg.chunk_overlap_tokens.
    - Quoted reply chains and signatures stripped per cfg.chunk_strip_*.
    - If body is None/empty or shorter than ~200 tokens, only the header chunk is emitted.
    """
    raw = msg.body_text or ""
    if cfg.chunk_strip_quoted_replies:
        raw = strip_quoted_replies(raw)
    if cfg.chunk_strip_signatures:
        raw = strip_signature(raw)
    body = normalize_whitespace(raw)

    header_text = _header_text(msg, body)
    chunks: list[ChunkSpec] = [
        ChunkSpec(
            kind="header",
            chunk_idx=0,
            text=header_text,
            token_count=len(_ENC.encode(header_text)),
        )
    ]
    body_tokens = _ENC.encode(body) if body else []
    if len(body_tokens) <= _HEADER_BODY_INTRO_TOKENS:
        return chunks

    remainder = _ENC.decode(body_tokens[_HEADER_BODY_INTRO_TOKENS:])
    for idx, piece in enumerate(
        split_by_tokens(remainder, cfg.chunk_size_tokens, cfg.chunk_overlap_tokens)
    ):
        chunks.append(
            ChunkSpec(
                kind="body",
                chunk_idx=idx,
                text=piece,
                token_count=len(_ENC.encode(piece)),
            )
        )
    return chunks
```

- [ ] **Step 5: Verify passing**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_chunking.py -v
```

- [ ] **Step 6: Commit**

```bash
git add src/localmail/search/chunking.py tests/test_chunking.py pyproject.toml uv.lock
git commit -m "feat(search): add chunk_message + token-windowed splitter + MessageRow"
```

---

## Task 6: `query.py` — `ParsedQuery`, `SearchFilters`, `QueryParseError`, `parse_query`

**Files:**
- Create: `src/localmail/search/query.py`
- Test: `tests/test_query_parser.py`

- [ ] **Step 1: Write failing tests** — `tests/test_query_parser.py`:

```python
"""Tests for the search query parser."""

from __future__ import annotations

from datetime import date

import pytest

from localmail.search.query import (
    ParsedQuery,
    QueryParseError,
    SearchFilters,
    parse_query,
)


def test_bare_text_query():
    q = parse_query("Berlin conference")
    assert q.free_text == "Berlin conference"
    assert q.filters == SearchFilters()


def test_from_operator():
    q = parse_query("from:anna@example.com Berlin")
    assert q.free_text == "Berlin"
    assert q.filters.from_substr == "anna@example.com"


def test_from_quoted():
    q = parse_query('from:"Anna Schmidt" Berlin')
    assert q.free_text == "Berlin"
    assert q.filters.from_substr == "Anna Schmidt"


def test_date_operators():
    q = parse_query("invoice after:2025-01-01 before:2025-12-31")
    assert q.free_text == "invoice"
    assert q.filters.after == date(2025, 1, 1)
    assert q.filters.before == date(2025, 12, 31)


def test_has_attachment_flag():
    q = parse_query("invoice has:attachment")
    assert q.filters.has_attachment is True


def test_label_account_folder():
    q = parse_query('label:work account:gmail-personal folder:"[Gmail]/Sent"')
    assert q.filters.label == "work"
    # accounts left as list[str] — searcher resolves to IDs later
    assert q.filters.account_names == ["gmail-personal"]
    assert q.filters.folders == ["[Gmail]/Sent"]


def test_multiple_same_operator_last_wins():
    q = parse_query("from:a from:b berlin")
    assert q.filters.from_substr == "b"
    assert q.free_text == "berlin"


def test_malformed_date_raises():
    with pytest.raises(QueryParseError) as exc:
        parse_query("after:not-a-date")
    assert "after" in str(exc.value).lower()
```

- [ ] **Step 2: Verify failing**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_query_parser.py -v
```

- [ ] **Step 3: Implement** — `src/localmail/search/query.py`:

```python
"""Parse a free-text-plus-operators search query into a typed shape.

Supported operators (all optional, in any order, anywhere in the query):
    from:STR / from:"STR"     to:STR / to:"STR"
    subject:STR               label:STR
    account:NAME              folder:STR / folder:"STR"
    after:YYYY-MM-DD          before:YYYY-MM-DD
    has:attachment

Anything not matched by an operator becomes free-text (joined with spaces,
preserved in encounter order).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime


class QueryParseError(ValueError):
    """Raised when an operator value can't be parsed (e.g. malformed date)."""


@dataclass(frozen=True)
class SearchFilters:
    account_names: list[str] = field(default_factory=list)
    accounts: list[int] | None = None  # resolved by Searcher from account_names
    folders: list[str] | None = None
    from_substr: str | None = None
    to_substr: str | None = None
    subject_substr: str | None = None
    after: date | None = None
    before: date | None = None
    has_attachment: bool | None = None
    label: str | None = None
    languages: list[str] | None = None


@dataclass(frozen=True)
class ParsedQuery:
    free_text: str
    rewritten_text: str | None = None
    expansion_terms: list[str] = field(default_factory=list)
    filters: SearchFilters = field(default_factory=SearchFilters)


_OPERATORS = {"from", "to", "subject", "after", "before", "has", "label", "account", "folder"}


def _tokenize(s: str) -> list[str]:
    """Whitespace-split, but keep quoted strings (single or double) intact."""
    out: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    for ch in s:
        if quote:
            if ch == quote:
                quote = None
            else:
                buf.append(ch)
        elif ch in ('"', "'"):
            quote = ch
        elif ch.isspace():
            if buf:
                out.append("".join(buf))
                buf = []
        else:
            buf.append(ch)
    if buf:
        out.append("".join(buf))
    return out


def _parse_date(value: str, field_name: str) -> date:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise QueryParseError(f"{field_name}: expected YYYY-MM-DD, got {value!r}") from exc


def parse_query(query: str) -> ParsedQuery:
    """Decompose a query string into free text + structured filters."""
    free_parts: list[str] = []
    f_account_names: list[str] = []
    f_folders: list[str] = []
    f_from = f_to = f_subject = f_label = None
    f_after = f_before = None
    f_has_attachment: bool | None = None

    for tok in _tokenize(query):
        if ":" in tok:
            op, _, value = tok.partition(":")
            op_l = op.lower()
            if op_l in _OPERATORS and value:
                if op_l == "from":
                    f_from = value
                elif op_l == "to":
                    f_to = value
                elif op_l == "subject":
                    f_subject = value
                elif op_l == "label":
                    f_label = value
                elif op_l == "account":
                    f_account_names.append(value)
                elif op_l == "folder":
                    f_folders.append(value)
                elif op_l == "after":
                    f_after = _parse_date(value, "after")
                elif op_l == "before":
                    f_before = _parse_date(value, "before")
                elif op_l == "has":
                    if value.lower() == "attachment":
                        f_has_attachment = True
                continue
        free_parts.append(tok)

    filters = SearchFilters(
        account_names=f_account_names,
        folders=f_folders or None,
        from_substr=f_from,
        to_substr=f_to,
        subject_substr=f_subject,
        after=f_after,
        before=f_before,
        has_attachment=f_has_attachment,
        label=f_label,
    )
    return ParsedQuery(free_text=" ".join(free_parts), filters=filters)
```

- [ ] **Step 4: Verify passing**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_query_parser.py -v
```

- [ ] **Step 5: Commit**

```bash
git add src/localmail/search/query.py tests/test_query_parser.py
git commit -m "feat(search): query parser with from/to/subject/date/has/label/account/folder operators"
```

---

## Task 7: `embeddings.py` — `EmbeddingBackend` protocol + `FastEmbedBackend`

**Files:**
- Create: `src/localmail/search/embeddings.py`
- Test: `tests/test_embeddings.py`
- Modify: `pyproject.toml` (add fastembed)

- [ ] **Step 1: Add fastembed dep**

```bash
unset VIRTUAL_ENV && uv add fastembed
```

- [ ] **Step 2: Write failing tests** — `tests/test_embeddings.py`:

```python
"""Unit tests for EmbeddingBackend protocol + FastEmbedBackend wrapper.

The actual fastembed model load is slow + heavy (~250 MB); we test the
wrapper behaviour with a stub model and gate the real-model smoke test
under pytest.mark.slow.
"""

from __future__ import annotations

import pytest

from localmail.config import SearchConfig
from localmail.search.embeddings import (
    EmbeddingBackend,
    EmbeddingConfigError,
    FastEmbedBackend,
)


class _StubInner:
    """Stand-in for fastembed.TextEmbedding with deterministic output."""

    def __init__(self, dim: int) -> None:
        self.dim = dim

    def embed(self, texts, **_):
        for i, _t in enumerate(texts):
            yield [(i + 1) / 100.0] * self.dim

    def query_embed(self, texts, **_):
        for i, _t in enumerate(texts):
            yield [(i + 7) / 100.0] * self.dim


def test_fastembed_backend_protocol_attrs():
    be = FastEmbedBackend(cfg=SearchConfig(), inner=_StubInner(dim=768))
    assert be.name == "fastembed"
    assert be.dimension == 768
    assert be.model == "embeddinggemma"


def test_fastembed_backend_embed_documents_shape():
    be = FastEmbedBackend(cfg=SearchConfig(), inner=_StubInner(dim=768))
    vecs = be.embed_documents(["a", "b", "c"])
    assert len(vecs) == 3
    assert all(len(v) == 768 for v in vecs)


def test_fastembed_backend_embed_query_uses_query_path():
    be = FastEmbedBackend(cfg=SearchConfig(), inner=_StubInner(dim=768))
    v = be.embed_query("hello")
    # query_embed seeds with i+7 vs documents i+1 — proves correct path
    assert v[0] == pytest.approx(8 / 100.0)


def test_fastembed_backend_dim_mismatch_raises():
    cfg = SearchConfig(embedding_dim=1024)
    with pytest.raises(EmbeddingConfigError):
        FastEmbedBackend(cfg=cfg, inner=_StubInner(dim=768)).health_check()


def test_protocol_matched_by_backend():
    be: EmbeddingBackend = FastEmbedBackend(cfg=SearchConfig(), inner=_StubInner(dim=768))
    assert callable(be.embed_documents)
    assert callable(be.embed_query)
    assert callable(be.health_check)
```

- [ ] **Step 3: Verify failing**

- [ ] **Step 4: Implement** — `src/localmail/search/embeddings.py`:

```python
"""Embedding backend protocol + FastEmbedBackend (in-process ONNX).

Phase 1 ships fastembed only. OllamaBackend lands in Phase 4 alongside
the --smart query rewriter.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from localmail.config import SearchConfig


class EmbeddingConfigError(RuntimeError):
    """Raised at backend init / health_check when model/dim/config mismatch."""


@runtime_checkable
class EmbeddingBackend(Protocol):
    """Embeds batches of texts into fixed-dim float vectors.

    Document and query paths are distinct because modern embedding models
    use task-specific instruction prefixes; the backend handles that
    internally so callers never pass the wrong one.
    """

    name: str
    model: str
    dimension: int

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...
    def embed_query(self, text: str) -> list[float]: ...
    def health_check(self) -> None: ...


def _build_fastembed_inner(cfg: SearchConfig) -> Any:
    """Build a real fastembed.TextEmbedding from config. Imports lazily."""
    from fastembed import TextEmbedding  # noqa: WPS433

    return TextEmbedding(
        model_name=f"google/{cfg.embedding_model}-300m"
        if cfg.embedding_model == "embeddinggemma"
        else cfg.embedding_model,
        cache_dir=str(cfg.fastembed_cache_dir) if cfg.fastembed_cache_dir else None,
        threads=cfg.fastembed_threads,
    )


class FastEmbedBackend:
    """In-process ONNX embedding via fastembed. Thread-safe after init."""

    name = "fastembed"

    def __init__(self, cfg: SearchConfig, inner: Any | None = None) -> None:
        self._cfg = cfg
        self.model = cfg.embedding_model
        self.dimension = cfg.embedding_dim
        self._inner = inner if inner is not None else _build_fastembed_inner(cfg)

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [list(v) for v in self._inner.embed(texts)]

    def embed_query(self, text: str) -> list[float]:
        # Use query_embed if available (newer fastembed); fall back to embed.
        if hasattr(self._inner, "query_embed"):
            for v in self._inner.query_embed([text]):
                return list(v)
            raise EmbeddingConfigError("query_embed returned no vectors")
        for v in self._inner.embed([text]):
            return list(v)
        raise EmbeddingConfigError("embed returned no vectors")

    def health_check(self) -> None:
        """Verify that backend produces vectors of the configured dimension."""
        v = self.embed_query("health check probe")
        if len(v) != self.dimension:
            raise EmbeddingConfigError(
                f"backend produced dim={len(v)} but SearchConfig.embedding_dim={self.dimension}; "
                "either switch model or update embedding_dim"
            )
```

- [ ] **Step 5: Add slow integration test** — append to `tests/test_embeddings.py`:

```python
@pytest.mark.slow
def test_fastembed_backend_real_model_smoke():
    """Real model load. Opt-in: pytest -m slow."""
    cfg = SearchConfig()  # embeddinggemma default
    be = FastEmbedBackend(cfg=cfg)
    be.health_check()
    v = be.embed_query("the quick brown fox")
    assert len(v) == 768
    docs = be.embed_documents(["alpha", "beta"])
    assert len(docs) == 2 and len(docs[0]) == 768
```

- [ ] **Step 6: Verify passing**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_embeddings.py -v -m "not slow"
```

- [ ] **Step 7: Commit**

```bash
git add src/localmail/search/embeddings.py tests/test_embeddings.py pyproject.toml uv.lock
git commit -m "feat(search): EmbeddingBackend protocol + FastEmbedBackend (fastembed/ONNX)"
```

---

## Task 8: `reranker.py` — `Reranker` protocol + `FastEmbedReranker`

**Files:**
- Create: `src/localmail/search/reranker.py`
- Test: `tests/test_reranker.py`

- [ ] **Step 1: Write failing tests** — `tests/test_reranker.py`:

```python
"""Unit tests for Reranker protocol + FastEmbedReranker wrapper."""

from __future__ import annotations

import pytest

from localmail.config import SearchConfig
from localmail.search.reranker import FastEmbedReranker, Reranker


class _StubInner:
    """Stand-in for fastembed's cross-encoder; returns deterministic scores."""

    def rerank(self, query, documents, **_):
        return [
            {"index": i, "score": 1.0 / (i + 1)}
            for i in range(len(documents))
        ]


def test_fastembed_reranker_protocol_attrs():
    rr = FastEmbedReranker(cfg=SearchConfig(), inner=_StubInner())
    assert rr.name == "fastembed"
    assert rr.model == SearchConfig().reranker_model


def test_fastembed_reranker_returns_scores_in_input_order():
    rr = FastEmbedReranker(cfg=SearchConfig(), inner=_StubInner())
    scores = rr.rerank("q", ["a", "b", "c"])
    assert len(scores) == 3
    assert scores[0] == 1.0
    assert scores[1] == pytest.approx(0.5)
    assert scores[2] == pytest.approx(1 / 3)


def test_protocol_matched():
    rr: Reranker = FastEmbedReranker(cfg=SearchConfig(), inner=_StubInner())
    assert callable(rr.rerank)
```

- [ ] **Step 2: Verify failing**

- [ ] **Step 3: Implement** — `src/localmail/search/reranker.py`:

```python
"""Cross-encoder reranker protocol + FastEmbed implementation.

Used by Searcher after RRF fusion to re-score the candidate pool with a
model that sees (query, candidate) together — much higher quality than
the dual-encoder embeddings on their own.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from localmail.config import SearchConfig


@runtime_checkable
class Reranker(Protocol):
    name: str
    model: str

    def rerank(self, query: str, candidates: list[str]) -> list[float]: ...


def _build_fastembed_inner(cfg: SearchConfig) -> Any:
    """Lazily import + construct the underlying fastembed reranker."""
    from fastembed.rerank.cross_encoder import TextCrossEncoder  # noqa: WPS433

    return TextCrossEncoder(model_name=cfg.reranker_model)


class FastEmbedReranker:
    """ONNX cross-encoder via fastembed. Returns one float per candidate."""

    name = "fastembed"

    def __init__(self, cfg: SearchConfig, inner: Any | None = None) -> None:
        self._cfg = cfg
        self.model = cfg.reranker_model
        self._inner = inner if inner is not None else _build_fastembed_inner(cfg)

    def rerank(self, query: str, candidates: list[str]) -> list[float]:
        if not candidates:
            return []
        # fastembed's API: rerank returns scored results; preserve input order
        raw = list(self._inner.rerank(query, candidates))
        scores = [0.0] * len(candidates)
        for entry in raw:
            scores[entry["index"]] = float(entry["score"])
        return scores
```

- [ ] **Step 4: Verify passing**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_reranker.py -v
```

- [ ] **Step 5: Commit**

```bash
git add src/localmail/search/reranker.py tests/test_reranker.py
git commit -m "feat(search): Reranker protocol + FastEmbedReranker (bge-reranker-v2-m3)"
```

---

## Task 9: `searcher.py` — `ArmHit`, `FusedHit`, `rrf_fuse`

**Files:**
- Create: `src/localmail/search/searcher.py` (will grow over Tasks 9, 10, 14–19)
- Test: `tests/test_rrf.py`

- [ ] **Step 1: Write failing tests** — `tests/test_rrf.py`:

```python
"""Tests for the pure RRF fusion function."""

from __future__ import annotations

from localmail.search.searcher import ArmHit, rrf_fuse


def _hit(mid, cid, table, rank, score=0.0):
    return ArmHit(message_id=mid, chunk_id=cid, chunk_table=table,
                  arm_score=score, rank=rank)


def test_rrf_single_arm_orders_by_rank():
    arm = [_hit(10, 1, "message_chunks", rank=1),
           _hit(20, 2, "message_chunks", rank=2),
           _hit(30, 3, "message_chunks", rank=3)]
    out = rrf_fuse([arm], k=60)
    assert [h.message_id for h in out] == [10, 20, 30]


def test_rrf_two_arms_sum_contributions():
    a = [_hit(10, 1, "message_chunks", rank=1),
         _hit(20, 2, "message_chunks", rank=3)]
    b = [_hit(20, 4, "message_chunks", rank=1),
         _hit(10, 5, "message_chunks", rank=4)]
    out = rrf_fuse([a, b], k=60)
    # Message 20: 1/(60+3) + 1/(60+1) = 0.0322  | Message 10: 1/(60+1) + 1/(60+4) = 0.0320
    assert [h.message_id for h in out] == [20, 10]


def test_rrf_dedupes_to_one_chunk_per_message():
    a = [_hit(10, 1, "message_chunks", rank=1),
         _hit(10, 2, "message_chunks", rank=5)]
    out = rrf_fuse([a], k=60)
    assert len(out) == 1
    # winner chunk = the one with the largest single contribution
    assert out[0].best_chunk_id == 1


def test_rrf_records_contributing_arms():
    a = [_hit(10, 1, "message_chunks", rank=1)]
    b = [_hit(10, 2, "message_chunks", rank=2)]
    c = []  # arm with no hits
    out = rrf_fuse([a, b, c], k=60)
    assert out[0].contributing_arms == [0, 1]


def test_rrf_empty_input():
    assert rrf_fuse([], k=60) == []
    assert rrf_fuse([[], []], k=60) == []
```

- [ ] **Step 2: Verify failing**

- [ ] **Step 3: Implement** — `src/localmail/search/searcher.py`:

```python
"""Search engine orchestrator + pure helpers (RRF, snippets).

Most of this module is the Searcher class (Tasks 14–19); this commit
introduces only the data shapes and rrf_fuse so later tasks can build on
top.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


@dataclass(frozen=True)
class ArmHit:
    """One hit from one retrieval arm."""
    message_id: int
    chunk_id: int | None  # None for Arm 1 (whole-message BM25)
    chunk_table: Literal["message", "message_chunks", "attachment_chunks"]
    arm_score: float
    rank: int  # 1-based, within the arm


@dataclass(frozen=True)
class FusedHit:
    """Post-RRF hit, deduplicated to one row per message_id."""
    message_id: int
    best_chunk_id: int | None
    best_chunk_table: Literal["message", "message_chunks", "attachment_chunks"]
    rrf_score: float
    contributing_arms: list[int] = field(default_factory=list)


def rrf_fuse(arms: list[list[ArmHit]], k: int) -> list[FusedHit]:
    """Reciprocal Rank Fusion across N arms.

    Contribution of arm i to (message_id, chunk_id) is 1 / (k + rank).
    Output is one FusedHit per message_id, keeping the chunk whose own
    single-arm contribution is largest (so the snippet later comes from
    the chunk that 'earned' the rank). Sorted by descending rrf_score.

    `k` is the standard RRF dampening constant (default 60).
    """
    # Per-message aggregated score + per-chunk contributions (for winner pick)
    agg: dict[int, dict] = {}
    for arm_idx, arm in enumerate(arms):
        for hit in arm:
            entry = agg.setdefault(hit.message_id, {
                "score": 0.0,
                "arms": set(),
                "chunks": {},  # (chunk_id, chunk_table) -> best contribution
            })
            contrib = 1.0 / (k + hit.rank)
            entry["score"] += contrib
            entry["arms"].add(arm_idx)
            chkey = (hit.chunk_id, hit.chunk_table)
            if contrib > entry["chunks"].get(chkey, 0.0):
                entry["chunks"][chkey] = contrib

    out: list[FusedHit] = []
    for mid, entry in agg.items():
        (best_cid, best_table), _ = max(entry["chunks"].items(), key=lambda kv: kv[1])
        out.append(FusedHit(
            message_id=mid,
            best_chunk_id=best_cid,
            best_chunk_table=best_table,
            rrf_score=entry["score"],
            contributing_arms=sorted(entry["arms"]),
        ))
    out.sort(key=lambda h: h.rrf_score, reverse=True)
    return out
```

- [ ] **Step 4: Verify passing**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_rrf.py -v
```

- [ ] **Step 5: Commit**

```bash
git add src/localmail/search/searcher.py tests/test_rrf.py
git commit -m "feat(search): ArmHit, FusedHit, rrf_fuse pure function"
```

---

## Task 10: `searcher.py` — `make_snippet`

**Files:**
- Modify: `src/localmail/search/searcher.py`
- Test: `tests/test_snippet.py`

- [ ] **Step 1: Write failing tests** — `tests/test_snippet.py`:

```python
"""Tests for the snippet-windowing pure function."""

from __future__ import annotations

from localmail.search.searcher import make_snippet


def test_snippet_returns_full_text_when_short():
    assert make_snippet("Short body.", ["body"], width=200) == "Short body."


def test_snippet_centers_on_term_match():
    text = "lorem ipsum dolor " * 50 + "the BERLIN conference " + "sit amet " * 50
    out = make_snippet(text, ["berlin"], width=80)
    assert "BERLIN" in out
    assert len(out) <= 100  # width + a little padding for word boundaries


def test_snippet_first_term_wins_when_multiple():
    text = ("alpha " * 100) + "beta " + ("gamma " * 100)
    out = make_snippet(text, ["beta"], width=80)
    assert "beta" in out


def test_snippet_falls_back_to_head_when_no_terms_match():
    text = "no matches here at all, just a long preamble " * 5
    out = make_snippet(text, ["nope", "absent"], width=80)
    # falls back to the leading window
    assert out.startswith("no matches here")


def test_snippet_handles_empty_query_terms():
    assert make_snippet("abc def", [], width=80) == "abc def"


def test_snippet_strips_leading_partial_words():
    text = "_______________________________ the BERLIN conference talk"
    out = make_snippet(text, ["berlin"], width=40)
    assert out.startswith("…") or out.startswith("the") or "BERLIN" in out
```

- [ ] **Step 2: Verify failing**

- [ ] **Step 3: Implement** — append to `src/localmail/search/searcher.py`:

```python
import re

_WORD = re.compile(r"\w+", re.UNICODE)


def make_snippet(chunk_text: str, query_terms: list[str], width: int) -> str:
    """Return a ~`width`-char window around the strongest query-term match.

    - If chunk is shorter than width, returned in full.
    - If no query term matches, returns the leading window.
    - Match is case-insensitive, word-boundary-aware.
    """
    if not chunk_text:
        return ""
    if len(chunk_text) <= width:
        return chunk_text

    best_pos: int | None = None
    lowered = chunk_text.lower()
    for term in query_terms:
        if not term:
            continue
        idx = lowered.find(term.lower())
        if idx != -1 and (best_pos is None or idx < best_pos):
            best_pos = idx
    if best_pos is None:
        # Leading window, snapped to word boundary
        cut = chunk_text[:width]
        m = list(_WORD.finditer(cut))
        if m and m[-1].end() < len(cut):
            cut = cut[: m[-1].end()]
        return cut

    half = width // 2
    start = max(0, best_pos - half)
    end = min(len(chunk_text), start + width)
    snippet = chunk_text[start:end]
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(chunk_text) else ""
    return f"{prefix}{snippet}{suffix}".strip()
```

- [ ] **Step 4: Verify passing**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_snippet.py -v
```

- [ ] **Step 5: Commit**

```bash
git add src/localmail/search/searcher.py tests/test_snippet.py
git commit -m "feat(search): make_snippet term-centered windowing"
```

---

## Task 11: Migration 0006 — pg_search BM25 indexes + HNSW vector index

**Files:**
- Create: `migrations/0006_search_indexes.sql`
- Test: `tests/test_search_schema.py` (extend)

- [ ] **Step 1: Write failing tests** — append to `tests/test_search_schema.py`:

```python
def test_pg_search_extension_installed(db_conn) -> None:
    with db_conn.cursor() as cur:
        cur.execute("SELECT extname FROM pg_extension WHERE extname = 'pg_search'")
        assert cur.fetchone() is not None, "pg_search extension required (install ParadeDB)"


def test_messages_bm25_index_exists(db_conn) -> None:
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM pg_indexes WHERE indexname = 'messages_bm25_idx'"
        )
        assert cur.fetchone() is not None


def test_message_chunks_bm25_index_exists(db_conn) -> None:
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM pg_indexes WHERE indexname = 'message_chunks_bm25_idx'"
        )
        assert cur.fetchone() is not None


def test_message_chunks_hnsw_index_exists(db_conn) -> None:
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM pg_indexes WHERE indexname = 'message_chunks_embedding_v1_hnsw'"
        )
        assert cur.fetchone() is not None
```

- [ ] **Step 2: Verify failing**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_search_schema.py -v
```

- [ ] **Step 3: Write `migrations/0006_search_indexes.sql`**

```sql
-- @non-transactional
-- BM25 (pg_search) on messages + message_chunks, plus HNSW (pgvector) on
-- message_chunks.embedding_v1. The @non-transactional header (Task 1) lets
-- the runner switch to autocommit so CREATE INDEX CONCURRENTLY works.

CREATE EXTENSION IF NOT EXISTS pg_search;

SET LOCAL maintenance_work_mem = '2048MB';

CREATE INDEX IF NOT EXISTS messages_bm25_idx ON messages
USING bm25 (id, subject, body_text, from_addr, from_name, to_addrs)
WITH (key_field='id');

CREATE INDEX IF NOT EXISTS message_chunks_bm25_idx ON message_chunks
USING bm25 (id, text) WITH (key_field='id');

CREATE INDEX CONCURRENTLY IF NOT EXISTS message_chunks_embedding_v1_hnsw
    ON message_chunks USING hnsw (embedding_v1 halfvec_cosine_ops)
    WITH (m=16, ef_construction=64);
```

NOTE: `SET LOCAL maintenance_work_mem` requires being inside a transaction; for the autocommit path the runner sets it per-session before applying the file. Update the autocommit branch in `apply_migrations`:

- [ ] **Step 4: Modify `apply_migrations` to raise `maintenance_work_mem` for non-transactional migrations**

In `src/localmail/db.py`, in the `if _is_non_transactional(sql):` branch, replace with:

```python
if _is_non_transactional(sql):
    with psycopg.connect(dsn, autocommit=True) as nc:
        with nc.cursor() as cur:
            cur.execute("SET maintenance_work_mem = '2048MB'")
            cur.execute(sql)
            cur.execute(
                "INSERT INTO schema_migrations (revision) VALUES (%s)",
                (revision,),
            )
```

(Future Phase 2 will parameterize this from `SearchConfig.index_build_maintenance_work_mem_mb`; Phase 1 hard-codes 2 GB matching the SQL.)

- [ ] **Step 5: Verify passing**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_search_schema.py -v
```
Expected: all 8 tests pass. If `test_pg_search_extension_installed` fails: install pg_search per Prerequisites section.

- [ ] **Step 6: Commit**

```bash
git add migrations/0006_search_indexes.sql src/localmail/db.py tests/test_search_schema.py
git commit -m "feat(schema): pg_search BM25 + pgvector HNSW indexes on messages + chunks"
```

---

## Task 12: `embed_worker.py` — `record_failed_embedding` + `run_embed_worker`

**Files:**
- Create: `src/localmail/search/embed_worker.py`
- Test: `tests/test_embed_worker.py`

- [ ] **Step 1: Write failing tests** — `tests/test_embed_worker.py`:

```python
"""Integration tests for the embed worker against a real Postgres."""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

import pytest

from localmail.config import SearchConfig
from localmail.search.embed_worker import (
    record_failed_embedding,
    run_embed_worker_once,
)


def _seed_message(conn, body="Hello world."):
    """Insert an account + message; return message_id."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO accounts (name, email_address, imap_host, auth_method)"
            " VALUES ('a', 'a@x', 'h', 'password') RETURNING id"
        )
        acct = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO messages (account_id, message_id, raw_sha256, subject,"
            " from_addr, body_text, headers, raw_bytes, size_bytes)"
            " VALUES (%s, %s, %s, %s, %s, %s, '{}'::jsonb, %s, %s) RETURNING id",
            (acct, "<a@x>", b'\\x01' * 32, "Hi", "x@y", body, b"raw", 3),
        )
        mid = cur.fetchone()[0]
    conn.commit()
    return mid


class _StaticEmbedder:
    """Deterministic backend: returns [1.0]*768 for any text."""
    name = "stub"
    model = "stub-768"
    dimension = 768

    def embed_documents(self, texts):
        return [[1.0 / (i + 1)] * 768 for i, _ in enumerate(texts)]

    def embed_query(self, text):
        return [0.5] * 768

    def health_check(self):
        pass


def test_run_embed_worker_chunks_and_embeds_a_message(db_conn):
    mid = _seed_message(db_conn, body="The Berlin conference is next week.")
    cfg = SearchConfig(embed_worker_batch_size=10)
    embedded = run_embed_worker_once(db_conn, cfg, _StaticEmbedder())
    assert embedded >= 1
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM message_chunks WHERE message_id = %s"
            " AND embedding_v1 IS NOT NULL", (mid,))
        assert cur.fetchone()[0] >= 1


def test_run_embed_worker_idempotent(db_conn):
    mid = _seed_message(db_conn)
    cfg = SearchConfig()
    first = run_embed_worker_once(db_conn, cfg, _StaticEmbedder())
    second = run_embed_worker_once(db_conn, cfg, _StaticEmbedder())
    assert first >= 1 and second == 0


def test_record_failed_embedding_inserts_row(db_conn):
    mid = _seed_message(db_conn)
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO message_chunks (message_id, kind, chunk_idx, text, token_count)"
            " VALUES (%s, 'body', 0, 'x', 1) RETURNING id", (mid,))
        cid = cur.fetchone()[0]
    db_conn.commit()
    try:
        raise RuntimeError("boom")
    except RuntimeError as exc:
        with db_conn.cursor() as cur:
            record_failed_embedding(cur, "message_chunks", cid, exc)
        db_conn.commit()
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT error_class, error_message, retry_count FROM failed_embeddings"
            " WHERE chunk_table='message_chunks' AND chunk_id=%s", (cid,))
        row = cur.fetchone()
    assert row[0] == "RuntimeError"
    assert "boom" in row[1]
    assert row[2] == 0


def test_record_failed_embedding_bumps_retry_count(db_conn):
    mid = _seed_message(db_conn)
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO message_chunks (message_id, kind, chunk_idx, text, token_count)"
            " VALUES (%s, 'body', 0, 'x', 1) RETURNING id", (mid,))
        cid = cur.fetchone()[0]
    db_conn.commit()
    for _ in range(3):
        try:
            raise ValueError("again")
        except ValueError as exc:
            with db_conn.cursor() as cur:
                record_failed_embedding(cur, "message_chunks", cid, exc)
            db_conn.commit()
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT retry_count FROM failed_embeddings WHERE chunk_id=%s", (cid,))
        assert cur.fetchone()[0] == 2  # 0 on insert, +1 each subsequent


def test_run_embed_worker_skips_chunks_past_max_retries(db_conn):
    mid = _seed_message(db_conn, body="x")
    cfg = SearchConfig(embed_worker_max_chunk_retries=1)

    class _Boom:
        name = "boom"; model = "boom"; dimension = 768
        def embed_documents(self, texts): raise RuntimeError("boom")
        def embed_query(self, t): return [0.0]*768
        def health_check(self): pass

    # First sweep: chunks are created (lazy), embeddings fail, recorded as failed.
    run_embed_worker_once(db_conn, cfg, _Boom())
    run_embed_worker_once(db_conn, cfg, _Boom())
    # Now retry_count >= 1 → excluded next time. Sweep with a working embedder:
    embedded = run_embed_worker_once(db_conn, cfg, _StaticEmbedder())
    assert embedded == 0  # nothing claimed because excluded by retry filter
```

- [ ] **Step 2: Verify failing**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_embed_worker.py -v
```

- [ ] **Step 3: Implement** — `src/localmail/search/embed_worker.py`:

```python
"""Background worker: fill embeddings for message_chunks where missing.

Phase 1 handles message_chunks only; attachment_chunks come in Phase 2.
The worker is account-agnostic — one instance per process, since embedding
throughput is backend-bound rather than IMAP-bound.
"""

from __future__ import annotations

import logging
import threading
import time
import traceback

import psycopg

from localmail.config import SearchConfig
from localmail.search.chunking import MessageRow, chunk_message
from localmail.search.embeddings import EmbeddingBackend

log = logging.getLogger("localmail.search.embed_worker")


def record_failed_embedding(cur, chunk_table: str, chunk_id: int, exc: Exception) -> None:
    """Upsert a failed_embeddings row, incrementing retry_count on conflict."""
    cur.execute(
        """
        INSERT INTO failed_embeddings (chunk_table, chunk_id, error_class,
                                       error_message, error_traceback)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (chunk_table, chunk_id) DO UPDATE
        SET error_class = EXCLUDED.error_class,
            error_message = EXCLUDED.error_message,
            error_traceback = EXCLUDED.error_traceback,
            retry_count = failed_embeddings.retry_count + 1,
            last_retry_at = now()
        """,
        (
            chunk_table,
            chunk_id,
            type(exc).__name__,
            str(exc),
            traceback.format_exc(),
        ),
    )


def _chunk_messages_lazily(conn: psycopg.Connection, cfg: SearchConfig, batch: int) -> int:
    """Find messages with no chunks; chunk them; INSERT. Returns # chunked."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT m.id, m.subject, m.from_addr, m.from_name, m.to_addrs,
                   m.date_sent, m.body_text
            FROM messages m
            LEFT JOIN message_chunks mc ON mc.message_id = m.id
            WHERE mc.id IS NULL
            ORDER BY m.id
            LIMIT %s
            FOR UPDATE OF m SKIP LOCKED
            """,
            (batch,),
        )
        rows = cur.fetchall()
        if not rows:
            return 0
        for mid, subj, fa, fn, to, ds, body in rows:
            msg = MessageRow(id=mid, subject=subj, from_addr=fa, from_name=fn,
                             to_addrs=to, date_sent=ds, body_text=body)
            for spec in chunk_message(msg, cfg):
                cur.execute(
                    "INSERT INTO message_chunks (message_id, kind, chunk_idx, text,"
                    " token_count) VALUES (%s, %s, %s, %s, %s)"
                    " ON CONFLICT (message_id, kind, chunk_idx) DO NOTHING",
                    (mid, spec.kind, spec.chunk_idx, spec.text, spec.token_count),
                )
    conn.commit()
    return len(rows)


def _claim_unembedded(cur, cfg: SearchConfig) -> list[tuple[int, str]]:
    cur.execute(
        """
        SELECT mc.id, mc.text FROM message_chunks mc
        WHERE mc.embedding_v1 IS NULL
          AND NOT EXISTS (
              SELECT 1 FROM failed_embeddings fe
              WHERE fe.chunk_table = 'message_chunks' AND fe.chunk_id = mc.id
                AND fe.retry_count >= %s
          )
        ORDER BY mc.id
        LIMIT %s
        FOR UPDATE SKIP LOCKED
        """,
        (cfg.embed_worker_max_chunk_retries, cfg.embed_worker_batch_size),
    )
    return cur.fetchall()


def _embed_and_store(conn, cfg, backend, claimed):
    """Embed claimed chunks; UPDATE per chunk inside a SAVEPOINT for poison isolation."""
    texts = [t for _, t in claimed]
    vectors = backend.embed_documents(texts)
    written = 0
    for (cid, _text), vec in zip(claimed, vectors, strict=True):
        with conn.cursor() as cur:
            cur.execute("SAVEPOINT chunk")
            try:
                cur.execute(
                    "UPDATE message_chunks SET embedding_v1 = %s::halfvec,"
                    " embedded_at = now() WHERE id = %s",
                    (vec, cid),
                )
                cur.execute("RELEASE SAVEPOINT chunk")
                written += 1
            except Exception as exc:  # noqa: BLE001 — poison-pill isolation
                cur.execute("ROLLBACK TO SAVEPOINT chunk")
                record_failed_embedding(cur, "message_chunks", cid, exc)
    conn.commit()
    return written


def run_embed_worker_once(
    conn: psycopg.Connection,
    cfg: SearchConfig,
    backend: EmbeddingBackend,
) -> int:
    """One sweep: chunk pending messages, then embed pending chunks.

    Returns number of chunks newly embedded in this sweep. Used both by
    the background daemon thread and the `localmail embed-backfill` CLI.
    """
    _chunk_messages_lazily(conn, cfg, batch=max(cfg.embed_worker_batch_size, 50))
    with conn.cursor() as cur:
        claimed = _claim_unembedded(cur, cfg)
    if not claimed:
        conn.commit()
        return 0
    try:
        return _embed_and_store(conn, cfg, backend, claimed)
    except Exception as exc:  # noqa: BLE001 — batch-level fallback
        log.warning("embed_worker batch failed: %s", exc, exc_info=True)
        # Mark every claimed chunk as failed so they're not re-claimed forever
        with conn.cursor() as cur:
            for cid, _ in claimed:
                record_failed_embedding(cur, "message_chunks", cid, exc)
        conn.commit()
        return 0


def run_embed_worker(
    stop: threading.Event,
    pool,
    cfg: SearchConfig,
    backend: EmbeddingBackend,
) -> None:
    """Background loop: sleep, sweep, sleep. Exits when `stop` is set.

    Re-acquires a fresh connection from the pool each sweep to keep the
    pool's idle-rotation healthy. Backoff on consecutive empty sweeps so
    an empty queue doesn't busy-poll.
    """
    consecutive_empty = 0
    while not stop.is_set():
        try:
            with pool.connection() as conn:
                wrote = run_embed_worker_once(conn, cfg, backend)
        except Exception as exc:  # noqa: BLE001
            log.error("embed_worker sweep error: %s", exc, exc_info=True)
            wrote = 0
        if wrote == 0:
            consecutive_empty = min(consecutive_empty + 1, 6)
        else:
            consecutive_empty = 0
        sleep_s = cfg.embed_worker_poll_interval_s * (1 + consecutive_empty)
        stop.wait(timeout=sleep_s)
```

- [ ] **Step 4: Verify passing**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_embed_worker.py -v
```

- [ ] **Step 5: Commit**

```bash
git add src/localmail/search/embed_worker.py tests/test_embed_worker.py
git commit -m "feat(search): embed_worker with lazy chunking + per-chunk SAVEPOINT + retry skip"
```

---

## Task 13: Retrieval arms 1, 2, 3 — `arms.py`

**Files:**
- Create: `src/localmail/search/arms.py`
- Test: `tests/test_arms.py`

- [ ] **Step 1: Write failing tests** — `tests/test_arms.py`:

```python
"""Integration tests for the three Phase-1 retrieval arms.

These rely on real Postgres + pg_search + pgvector, so all are integration
tests. They populate a tiny corpus, embed deterministically, and verify
each arm returns the expected hit shape and ordering.
"""

from __future__ import annotations

from localmail.config import SearchConfig
from localmail.search.arms import arm_bm25_messages, arm_bm25_chunks, arm_vector_chunks
from localmail.search.query import parse_query
from localmail.search.embed_worker import run_embed_worker_once


class _SeedEmbedder:
    """Returns a different deterministic vector per text so vector arm orders deterministically."""
    name = "seed"; model = "seed"; dimension = 768

    def embed_documents(self, texts):
        out = []
        for t in texts:
            base = (sum(ord(c) for c in t) % 100) / 100.0
            out.append([base] * 768)
        return out

    def embed_query(self, text):
        base = (sum(ord(c) for c in text) % 100) / 100.0
        return [base] * 768

    def health_check(self): pass


def _seed_corpus(conn):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO accounts (name, email_address, imap_host, auth_method)"
            " VALUES ('a', 'a@x', 'h', 'password') RETURNING id"
        )
        acct = cur.fetchone()[0]
        msgs = [
            ("<m1>", "Berlin conference next week", "anna@x", "Anna",
             "Looking forward to the Berlin conference next week."),
            ("<m2>", "Lunch tomorrow", "bob@x", "Bob",
             "Want to grab lunch tomorrow?"),
            ("<m3>", "Conference review", "anna@x", "Anna",
             "How was the conference last week?"),
        ]
        ids = []
        for i, (mid, subj, fa, fn, body) in enumerate(msgs):
            cur.execute(
                "INSERT INTO messages (account_id, message_id, raw_sha256, subject,"
                " from_addr, from_name, body_text, headers, raw_bytes, size_bytes)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, '{}'::jsonb, %s, %s) RETURNING id",
                (acct, mid, bytes([i + 1]) * 32, subj, fa, fn, body, b"r", 1),
            )
            ids.append(cur.fetchone()[0])
    conn.commit()
    return ids


def test_arm_bm25_messages_finds_subject_match(db_conn):
    ids = _seed_corpus(db_conn)
    cfg = SearchConfig()
    run_embed_worker_once(db_conn, cfg, _SeedEmbedder())
    parsed = parse_query("Berlin")
    hits = arm_bm25_messages(db_conn, parsed, cfg, limit=10)
    msg_ids = [h.message_id for h in hits]
    assert ids[0] in msg_ids


def test_arm_bm25_chunks_finds_body_match(db_conn):
    ids = _seed_corpus(db_conn)
    cfg = SearchConfig()
    run_embed_worker_once(db_conn, cfg, _SeedEmbedder())
    parsed = parse_query("lunch")
    hits = arm_bm25_chunks(db_conn, parsed, cfg, limit=10)
    assert ids[1] in [h.message_id for h in hits]


def test_arm_vector_chunks_returns_results(db_conn):
    ids = _seed_corpus(db_conn)
    cfg = SearchConfig()
    run_embed_worker_once(db_conn, cfg, _SeedEmbedder())
    parsed = parse_query("conference")
    qvec = _SeedEmbedder().embed_query("conference")
    hits = arm_vector_chunks(db_conn, parsed, cfg, qvec, limit=10)
    assert len(hits) >= 1
    assert all(h.chunk_table == "message_chunks" for h in hits)


def test_arms_respect_account_filter(db_conn):
    ids = _seed_corpus(db_conn)
    # Insert a second account + message to verify filtering
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO accounts (name, email_address, imap_host, auth_method)"
            " VALUES ('b', 'b@x', 'h', 'password') RETURNING id"
        )
        a2 = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO messages (account_id, message_id, raw_sha256, subject, body_text,"
            " headers, raw_bytes, size_bytes) VALUES (%s, '<m4>', %s, 'Berlin', 'x',"
            " '{}'::jsonb, 'r', 1) RETURNING id",
            (a2, b'\\x04' * 32),
        )
    db_conn.commit()
    cfg = SearchConfig()
    run_embed_worker_once(db_conn, cfg, _SeedEmbedder())

    parsed = parse_query("Berlin")
    parsed.filters.__dict__["accounts"] = [a2]  # resolved by Searcher in prod
    hits = arm_bm25_messages(db_conn, parsed, cfg, limit=10)
    assert all(h.message_id != ids[0] for h in hits)
```

- [ ] **Step 2: Verify failing**

- [ ] **Step 3: Implement** — `src/localmail/search/arms.py`:

```python
"""SQL retrieval arms for Phase 1: BM25 (messages, chunks) + vector (chunks).

Each arm is a pure function `(conn, parsed_query, cfg, ...) -> list[ArmHit]`.
Arm 4 (vector over attachment_chunks) lands in Phase 2.
"""

from __future__ import annotations

from typing import Any

import psycopg

from localmail.config import SearchConfig
from localmail.search.query import ParsedQuery, SearchFilters
from localmail.search.searcher import ArmHit


def _filter_sql(filters: SearchFilters) -> tuple[str, list[Any]]:
    """Build a `m.<col> ... AND ...` WHERE clause fragment + parameter list.

    Returns ("AND ...sql...", [params]) or ("", []) if no filters.
    Callers prepend their own WHERE / AND as needed.
    """
    parts: list[str] = []
    params: list[Any] = []
    if filters.accounts:
        parts.append("m.account_id = ANY(%s)")
        params.append(filters.accounts)
    if filters.from_substr:
        parts.append("(m.from_addr ILIKE %s OR m.from_name ILIKE %s)")
        like = f"%{filters.from_substr}%"
        params.extend([like, like])
    if filters.to_substr:
        parts.append("EXISTS (SELECT 1 FROM unnest(m.to_addrs) t WHERE t ILIKE %s)")
        params.append(f"%{filters.to_substr}%")
    if filters.subject_substr:
        parts.append("m.subject ILIKE %s")
        params.append(f"%{filters.subject_substr}%")
    if filters.after:
        parts.append("m.date_sent >= %s")
        params.append(filters.after)
    if filters.before:
        parts.append("m.date_sent < %s")
        params.append(filters.before)
    if filters.has_attachment is True:
        parts.append("jsonb_array_length(m.attachments) > 0")
    if filters.has_attachment is False:
        parts.append("jsonb_array_length(m.attachments) = 0")
    if filters.folders:
        parts.append(
            "EXISTS (SELECT 1 FROM message_labels ml JOIN mailboxes mb ON mb.id = ml.mailbox_id"
            " WHERE ml.message_id = m.id AND mb.name = ANY(%s))"
        )
        params.append(filters.folders)
    if not parts:
        return "", []
    return " AND " + " AND ".join(parts), params


def arm_bm25_messages(
    conn: psycopg.Connection,
    parsed: ParsedQuery,
    cfg: SearchConfig,
    limit: int,
) -> list[ArmHit]:
    """BM25 over messages: subject + from + body with per-field boosts."""
    if not parsed.free_text.strip():
        return []
    where_extra, where_params = _filter_sql(parsed.filters)
    q = parsed.free_text
    sql = f"""
        WITH ranked AS (
            SELECT m.id, paradedb.score(m.id) AS score
            FROM messages m
            WHERE m.id @@@ paradedb.boolean(must => ARRAY[
                paradedb.parse(%s, boost => %s, fields => ARRAY['subject']),
                paradedb.parse(%s, boost => %s, fields => ARRAY['from_addr','from_name']),
                paradedb.parse(%s, boost => %s, fields => ARRAY['body_text'])
            ]) {where_extra}
            ORDER BY score DESC LIMIT %s
        )
        SELECT id, score, ROW_NUMBER() OVER (ORDER BY score DESC) FROM ranked
    """
    params: list[Any] = [
        q, cfg.bm25_field_boosts.get("subject", 1.0),
        q, cfg.bm25_field_boosts.get("from", 1.0),
        q, cfg.bm25_field_boosts.get("body", 1.0),
        *where_params, limit,
    ]
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    return [
        ArmHit(message_id=mid, chunk_id=None, chunk_table="message",
               arm_score=float(score), rank=int(rank))
        for mid, score, rank in rows
    ]


def arm_bm25_chunks(
    conn: psycopg.Connection,
    parsed: ParsedQuery,
    cfg: SearchConfig,
    limit: int,
) -> list[ArmHit]:
    """BM25 over message_chunks.text."""
    if not parsed.free_text.strip():
        return []
    where_extra, where_params = _filter_sql(parsed.filters)
    sql = f"""
        WITH ranked AS (
            SELECT mc.message_id, mc.id AS chunk_id, paradedb.score(mc.id) AS score
            FROM message_chunks mc JOIN messages m ON m.id = mc.message_id
            WHERE mc.id @@@ %s {where_extra}
            ORDER BY score DESC LIMIT %s
        )
        SELECT message_id, chunk_id, score,
               ROW_NUMBER() OVER (ORDER BY score DESC) FROM ranked
    """
    params: list[Any] = [parsed.free_text, *where_params, limit]
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    return [
        ArmHit(message_id=mid, chunk_id=cid, chunk_table="message_chunks",
               arm_score=float(score), rank=int(rank))
        for mid, cid, score, rank in rows
    ]


def arm_vector_chunks(
    conn: psycopg.Connection,
    parsed: ParsedQuery,
    cfg: SearchConfig,
    query_vector: list[float],
    limit: int,
) -> list[ArmHit]:
    """Cosine-distance vector search over message_chunks.embedding_v1."""
    where_extra, where_params = _filter_sql(parsed.filters)
    sql = f"""
        WITH ranked AS (
            SELECT mc.message_id, mc.id AS chunk_id,
                   1.0 - (mc.embedding_v1 <=> %s::halfvec) AS score
            FROM message_chunks mc JOIN messages m ON m.id = mc.message_id
            WHERE mc.embedding_v1 IS NOT NULL {where_extra}
            ORDER BY mc.embedding_v1 <=> %s::halfvec
            LIMIT %s
        )
        SELECT message_id, chunk_id, score,
               ROW_NUMBER() OVER (ORDER BY score DESC) FROM ranked
    """
    params: list[Any] = [query_vector, *where_params, query_vector, limit]
    with conn.cursor() as cur:
        cur.execute(f"SET LOCAL hnsw.ef_search = {int(cfg.hnsw_ef_search)}")
        cur.execute(sql, params)
        rows = cur.fetchall()
    return [
        ArmHit(message_id=mid, chunk_id=cid, chunk_table="message_chunks",
               arm_score=float(score), rank=int(rank))
        for mid, cid, score, rank in rows
    ]
```

- [ ] **Step 4: Verify passing**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_arms.py -v
```

- [ ] **Step 5: Commit**

```bash
git add src/localmail/search/arms.py tests/test_arms.py
git commit -m "feat(search): retrieval arms 1-3 (BM25 messages, BM25 chunks, vector chunks)"
```

---

## Task 14: `page_cache.py` — bounded LRU + TTL

**Files:**
- Create: `src/localmail/search/page_cache.py`
- Test: `tests/test_page_cache.py`

- [ ] **Step 1: Write failing tests** — `tests/test_page_cache.py`:

```python
"""Tests for the in-memory page cache used for pagination."""

from __future__ import annotations

import time

from localmail.search.page_cache import PageCache, PageOutOfPoolError, CacheMissError


def test_put_get_roundtrip():
    c = PageCache(maxsize=4, ttl_s=60)
    c.put("tok", {"results": list(range(50)), "pool_size": 50})
    e = c.get("tok")
    assert e["pool_size"] == 50


def test_missing_token_raises():
    c = PageCache(maxsize=4, ttl_s=60)
    import pytest
    with pytest.raises(CacheMissError):
        c.get("nope")


def test_ttl_eviction():
    c = PageCache(maxsize=4, ttl_s=0.05)
    c.put("tok", {"results": [1]})
    time.sleep(0.1)
    import pytest
    with pytest.raises(CacheMissError):
        c.get("tok")


def test_lru_eviction_when_full():
    c = PageCache(maxsize=2, ttl_s=60)
    c.put("a", {"results": []})
    c.put("b", {"results": []})
    c.get("a")  # touches a; b becomes LRU
    c.put("c", {"results": []})  # evicts b
    import pytest
    with pytest.raises(CacheMissError):
        c.get("b")
    assert c.get("a") is not None
    assert c.get("c") is not None
```

- [ ] **Step 2: Verify failing**

- [ ] **Step 3: Implement** — `src/localmail/search/page_cache.py`:

```python
"""Bounded LRU + TTL cache for paginated search results.

Keys are opaque search_token strings (the Searcher generates them). Values
are dicts holding the reranked pool plus parsed query metadata.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from typing import Any


class CacheMissError(KeyError):
    """Token not present or expired."""


class PageOutOfPoolError(IndexError):
    """Requested page beyond the cached pool's size."""


class PageCache:
    """Thread-unsafe LRU+TTL store; wrap in a lock if shared across threads.

    For this project the cache is only touched from inside Searcher methods
    that are themselves called from one request at a time per process; if
    that changes (e.g. concurrent MCP tool calls), add a threading.Lock.
    """

    def __init__(self, maxsize: int, ttl_s: float) -> None:
        self._maxsize = maxsize
        self._ttl = ttl_s
        self._data: OrderedDict[str, tuple[float, Any]] = OrderedDict()

    def put(self, key: str, value: Any) -> None:
        if key in self._data:
            self._data.move_to_end(key)
        self._data[key] = (time.monotonic(), value)
        while len(self._data) > self._maxsize:
            self._data.popitem(last=False)

    def get(self, key: str) -> Any:
        if key not in self._data:
            raise CacheMissError(key)
        stamp, value = self._data[key]
        if time.monotonic() - stamp > self._ttl:
            del self._data[key]
            raise CacheMissError(key)
        self._data.move_to_end(key)
        return value

    def invalidate(self, key: str) -> None:
        self._data.pop(key, None)
```

- [ ] **Step 4: Verify passing**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_page_cache.py -v
```

- [ ] **Step 5: Commit**

```bash
git add src/localmail/search/page_cache.py tests/test_page_cache.py
git commit -m "feat(search): PageCache (LRU + TTL) for search pagination"
```

---

## Task 15: `searcher.py` — `SearchResult`, `SearchPage`, `Searcher.__init__`, `Searcher.search`

**Files:**
- Modify: `src/localmail/search/searcher.py`
- Test: `tests/test_searcher.py`

- [ ] **Step 1: Write failing tests** — `tests/test_searcher.py`:

```python
"""End-to-end Searcher tests against real Postgres with stubbed backends."""

from __future__ import annotations

from localmail.config import SearchConfig
from localmail.db import open_pool
from localmail.search.embed_worker import run_embed_worker_once
from localmail.search.searcher import SearchPage, Searcher


class _Embedder:
    name = "stub"; model = "stub"; dimension = 768

    def embed_documents(self, texts):
        return [[1.0 / (i + 1)] * 768 for i, _ in enumerate(texts)]

    def embed_query(self, t): return [0.5] * 768
    def health_check(self): pass


class _Reranker:
    name = "stub"; model = "stub"

    def rerank(self, query, candidates):
        # Prefer candidates containing the query verbatim
        return [1.0 if query.lower() in c.lower() else 0.5 for c in candidates]


def _seed(conn):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO accounts (name, email_address, imap_host, auth_method)"
                    " VALUES ('a','a@x','h','password') RETURNING id")
        acct = cur.fetchone()[0]
        for i, (s, b) in enumerate([
            ("Berlin conference next week", "Looking forward to Berlin"),
            ("Lunch tomorrow", "Want to grab lunch?"),
            ("Conference review", "How was the conference?"),
        ]):
            cur.execute(
                "INSERT INTO messages (account_id, message_id, raw_sha256, subject,"
                " body_text, headers, raw_bytes, size_bytes)"
                " VALUES (%s, %s, %s, %s, %s, '{}'::jsonb, 'r', 1)",
                (acct, f"<m{i}>", bytes([i + 1]) * 32, s, b),
            )
    conn.commit()


def test_searcher_returns_results(db_dsn, db_conn):
    _seed(db_conn)
    cfg = SearchConfig()
    run_embed_worker_once(db_conn, cfg, _Embedder())
    pool = open_pool(db_dsn)
    try:
        s = Searcher(pool=pool, cfg=cfg, embeddings=_Embedder(),
                     reranker=_Reranker(), rewriter=None)
        page = s.search("Berlin")
    finally:
        pool.close()
    assert isinstance(page, SearchPage)
    assert page.page == 1
    assert page.results, "expected at least one result"
    assert any("Berlin" in r.subject for r in page.results)
    assert page.search_token


def test_searcher_timing_ms_populated(db_dsn, db_conn):
    _seed(db_conn)
    cfg = SearchConfig()
    run_embed_worker_once(db_conn, cfg, _Embedder())
    pool = open_pool(db_dsn)
    try:
        s = Searcher(pool=pool, cfg=cfg, embeddings=_Embedder(),
                     reranker=_Reranker(), rewriter=None)
        page = s.search("Berlin")
    finally:
        pool.close()
    assert "parse" in page.timing_ms
    assert "retrieve" in page.timing_ms
    assert "rerank" in page.timing_ms
    assert "total" in page.timing_ms


def test_searcher_no_cache_returns_token_none(db_dsn, db_conn):
    _seed(db_conn)
    cfg = SearchConfig()
    run_embed_worker_once(db_conn, cfg, _Embedder())
    pool = open_pool(db_dsn)
    try:
        s = Searcher(pool=pool, cfg=cfg, embeddings=_Embedder(),
                     reranker=_Reranker(), rewriter=None)
        page = s.search("Berlin", use_cache=False)
    finally:
        pool.close()
    assert page.search_token is None
```

- [ ] **Step 2: Verify failing**

- [ ] **Step 3: Implement** — append to `src/localmail/search/searcher.py`:

```python
import time
import uuid
from datetime import datetime
from typing import Any

import psycopg
from psycopg_pool import ConnectionPool

from localmail.config import SearchConfig
from localmail.search.arms import (
    arm_bm25_chunks, arm_bm25_messages, arm_vector_chunks,
)
from localmail.search.embeddings import EmbeddingBackend
from localmail.search.page_cache import (
    CacheMissError, PageCache, PageOutOfPoolError,
)
from localmail.search.query import ParsedQuery, SearchFilters, parse_query
from localmail.search.reranker import Reranker


@dataclass(frozen=True)
class SearchResult:
    """One ranked search hit, with the snippet that earned the rank."""
    message_id: int
    account_id: int
    rank: int
    score: float
    rrf_score: float
    subject: str | None
    from_addr: str | None
    from_name: str | None
    date_sent: datetime | None
    snippet: str
    snippet_source: Literal["header", "body", "attachment"]
    attachment_filename: str | None
    matched_chunk_id: int | None
    matched_chunk_table: Literal["message", "message_chunks", "attachment_chunks"]


@dataclass(frozen=True)
class SearchPage:
    """One page of results plus pagination metadata."""
    results: list[SearchResult]
    page: int
    page_size: int
    pool_size: int
    candidates_per_arm: int
    has_more_in_pool: bool
    can_grow_pool: bool
    search_token: str | None
    query: ParsedQuery
    timing_ms: dict[str, float]


class Searcher:
    """Orchestrates the hybrid search pipeline.

    Created once per process and reused — holds long-lived backend handles
    and the page cache. Methods:
      - search(query, ...) -> SearchPage  (the entry point)
      - continue_page(token, page) -> SearchPage  (Task 16)
      - grow_pool(token, candidates_per_arm) -> SearchPage  (Task 16)
    """

    def __init__(
        self,
        pool: ConnectionPool,
        cfg: SearchConfig,
        embeddings: EmbeddingBackend,
        reranker: Reranker | None,
        rewriter: Any | None = None,  # QueryRewriter type lands Phase 4
    ) -> None:
        self._pool = pool
        self._cfg = cfg
        self._embeddings = embeddings
        self._reranker = reranker
        self._rewriter = rewriter
        self._cache = PageCache(maxsize=cfg.page_cache_size, ttl_s=cfg.page_cache_ttl_s)

    def _resolve_account_names(self, conn, parsed: ParsedQuery) -> ParsedQuery:
        if not parsed.filters.account_names:
            return parsed
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM accounts WHERE name = ANY(%s)",
                (parsed.filters.account_names,),
            )
            ids = [r[0] for r in cur.fetchall()]
        # Mutate via dataclasses.replace
        from dataclasses import replace
        return replace(parsed, filters=replace(parsed.filters, accounts=ids))

    def _retrieve_pool(
        self, conn, parsed: ParsedQuery,
        candidates_per_arm: int, rerank_pool_size: int,
    ) -> list[FusedHit]:
        a1 = arm_bm25_messages(conn, parsed, self._cfg, limit=candidates_per_arm)
        a2 = arm_bm25_chunks(conn, parsed, self._cfg, limit=candidates_per_arm)
        qvec = self._embeddings.embed_query(parsed.rewritten_text or parsed.free_text)
        a3 = arm_vector_chunks(conn, parsed, self._cfg, qvec, limit=candidates_per_arm)
        fused = rrf_fuse([a1, a2, a3], k=self._cfg.rrf_k)
        return fused[:rerank_pool_size]

    def _hydrate(self, conn, fused: list["FusedHit"]) -> list[dict]:
        """Pull message + chunk text for each fused hit, returned in fused order."""
        if not fused:
            return []
        msg_ids = [h.message_id for h in fused]
        msgs: dict[int, dict] = {}
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, account_id, subject, from_addr, from_name, date_sent,"
                " body_text FROM messages WHERE id = ANY(%s)", (msg_ids,))
            for mid, acct, subj, fa, fn, ds, body in cur.fetchall():
                msgs[mid] = {"account_id": acct, "subject": subj, "from_addr": fa,
                             "from_name": fn, "date_sent": ds, "body_text": body}
            chunk_ids = [h.best_chunk_id for h in fused if h.best_chunk_id]
            chunks: dict[int, str] = {}
            if chunk_ids:
                cur.execute("SELECT id, text FROM message_chunks WHERE id = ANY(%s)",
                            (chunk_ids,))
                chunks = {cid: t for cid, t in cur.fetchall()}
        out = []
        for h in fused:
            m = msgs.get(h.message_id, {})
            snip_text = chunks.get(h.best_chunk_id) if h.best_chunk_id else (m.get("body_text") or "")
            out.append({
                "fused": h, "msg": m, "snippet_source_text": snip_text or "",
            })
        return out

    def _build_results(
        self, hydrated: list[dict], parsed: ParsedQuery, rerank_scores: list[float],
        page: int, page_size: int,
    ) -> list[SearchResult]:
        terms = parsed.free_text.split()
        ordered = sorted(
            zip(hydrated, rerank_scores, strict=True),
            key=lambda x: x[1], reverse=True,
        )
        start = (page - 1) * page_size
        end = start + page_size
        out: list[SearchResult] = []
        for i, (item, score) in enumerate(ordered[start:end], start=1):
            h = item["fused"]
            m = item["msg"]
            snip = make_snippet(
                item["snippet_source_text"], terms,
                width=self._cfg.snippet_width_chars,
            )
            source = (
                "header" if h.best_chunk_table == "message_chunks" and h.best_chunk_id else
                "body" if h.best_chunk_table == "message_chunks" else
                "attachment" if h.best_chunk_table == "attachment_chunks" else
                "header"
            )
            out.append(SearchResult(
                message_id=h.message_id, account_id=m.get("account_id", 0),
                rank=i, score=float(score), rrf_score=h.rrf_score,
                subject=m.get("subject"), from_addr=m.get("from_addr"),
                from_name=m.get("from_name"), date_sent=m.get("date_sent"),
                snippet=snip, snippet_source=source, attachment_filename=None,
                matched_chunk_id=h.best_chunk_id,
                matched_chunk_table=h.best_chunk_table,
            ))
        return out

    def search(
        self,
        query: str,
        *,
        page_size: int | None = None,
        candidates_per_arm: int | None = None,
        rerank_pool_size: int | None = None,
        use_cache: bool = True,
        smart: bool = False,
    ) -> SearchPage:
        """Run the full search pipeline and return page 1."""
        t0 = time.monotonic()
        cfg = self._cfg
        page_size = page_size or cfg.page_size_default
        page_size = min(page_size, cfg.page_size_max)
        cpa = candidates_per_arm or cfg.candidates_per_arm
        rps = rerank_pool_size or cfg.rerank_pool_size
        if smart and self._rewriter is None:
            raise RuntimeError("--smart requires a configured rewriter (Phase 4)")

        timing: dict[str, float] = {}
        t = time.monotonic()
        parsed = parse_query(query)
        timing["parse"] = (time.monotonic() - t) * 1000

        with self._pool.connection() as conn:
            parsed = self._resolve_account_names(conn, parsed)
            t = time.monotonic()
            fused = self._retrieve_pool(conn, parsed, cpa, rps)
            timing["retrieve"] = (time.monotonic() - t) * 1000
            hydrated = self._hydrate(conn, fused)

        t = time.monotonic()
        if self._reranker and hydrated:
            snippets_for_rerank = [
                item["snippet_source_text"][: cfg.snippet_width_chars * 4]
                for item in hydrated
            ]
            scores = self._reranker.rerank(
                parsed.rewritten_text or parsed.free_text, snippets_for_rerank,
            )
        else:
            scores = [item["fused"].rrf_score for item in hydrated]
        timing["rerank"] = (time.monotonic() - t) * 1000

        results = self._build_results(hydrated, parsed, scores, page=1, page_size=page_size)
        timing["total"] = (time.monotonic() - t0) * 1000

        token: str | None = None
        if use_cache:
            token = uuid.uuid4().hex[:16]
            self._cache.put(token, {
                "parsed": parsed, "hydrated": hydrated, "scores": scores,
                "candidates_per_arm": cpa, "rerank_pool_size": rps,
                "page_size": page_size,
            })
        pool_size = len(hydrated)
        return SearchPage(
            results=results, page=1, page_size=page_size, pool_size=pool_size,
            candidates_per_arm=cpa,
            has_more_in_pool=pool_size > page_size,
            can_grow_pool=True,
            search_token=token, query=parsed, timing_ms=timing,
        )
```

- [ ] **Step 4: Verify passing**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_searcher.py -v
```

- [ ] **Step 5: Commit**

```bash
git add src/localmail/search/searcher.py tests/test_searcher.py
git commit -m "feat(search): Searcher.search end-to-end (arms + RRF + rerank + page 1)"
```

---

## Task 16: `Searcher.continue_page` + `Searcher.grow_pool`

**Files:**
- Modify: `src/localmail/search/searcher.py`
- Test: `tests/test_searcher_pagination.py`

- [ ] **Step 1: Write failing tests** — `tests/test_searcher_pagination.py`:

```python
"""Tests for pagination via the page cache + grow_pool re-run."""

from __future__ import annotations

import pytest

from localmail.config import SearchConfig
from localmail.db import open_pool
from localmail.search.embed_worker import run_embed_worker_once
from localmail.search.page_cache import CacheMissError, PageOutOfPoolError
from localmail.search.searcher import Searcher


class _E:
    name = "s"; model = "s"; dimension = 768
    def embed_documents(self, t): return [[1.0]*768 for _ in t]
    def embed_query(self, t): return [0.5]*768
    def health_check(self): pass


class _R:
    name = "s"; model = "s"
    def rerank(self, q, c): return [1.0 - i*0.001 for i, _ in enumerate(c)]


def _seed_many(conn, n=30):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO accounts (name,email_address,imap_host,auth_method)"
                    " VALUES ('a','a@x','h','password') RETURNING id")
        acct = cur.fetchone()[0]
        for i in range(n):
            cur.execute(
                "INSERT INTO messages (account_id, message_id, raw_sha256, subject,"
                " body_text, headers, raw_bytes, size_bytes)"
                " VALUES (%s, %s, %s, %s, %s, '{}'::jsonb, 'r', 1)",
                (acct, f"<m{i}>", bytes([i+1])*32, f"Subject {i} test",
                 f"Body {i} content with the keyword test."),
            )
    conn.commit()


def test_continue_page_returns_next_page_from_cache(db_dsn, db_conn):
    _seed_many(db_conn, n=30)
    cfg = SearchConfig(page_size_default=5)
    run_embed_worker_once(db_conn, cfg, _E())
    pool = open_pool(db_dsn)
    try:
        s = Searcher(pool=pool, cfg=cfg, embeddings=_E(), reranker=_R(), rewriter=None)
        p1 = s.search("test")
        p2 = s.continue_page(p1.search_token, page=2)
    finally:
        pool.close()
    assert p2.page == 2
    assert p2.results
    ids1 = {r.message_id for r in p1.results}
    ids2 = {r.message_id for r in p2.results}
    assert ids1.isdisjoint(ids2)


def test_continue_page_beyond_pool_raises(db_dsn, db_conn):
    _seed_many(db_conn, n=3)
    cfg = SearchConfig(page_size_default=5)
    run_embed_worker_once(db_conn, cfg, _E())
    pool = open_pool(db_dsn)
    try:
        s = Searcher(pool=pool, cfg=cfg, embeddings=_E(), reranker=_R(), rewriter=None)
        p1 = s.search("test")
        with pytest.raises(PageOutOfPoolError):
            s.continue_page(p1.search_token, page=2)
    finally:
        pool.close()


def test_grow_pool_returns_page_1_with_larger_pool(db_dsn, db_conn):
    _seed_many(db_conn, n=20)
    cfg = SearchConfig(page_size_default=5, candidates_per_arm=3, rerank_pool_size=3)
    run_embed_worker_once(db_conn, cfg, _E())
    pool = open_pool(db_dsn)
    try:
        s = Searcher(pool=pool, cfg=cfg, embeddings=_E(), reranker=_R(), rewriter=None)
        p1 = s.search("test")
        assert p1.pool_size <= 3
        p1_big = s.grow_pool(p1.search_token, candidates_per_arm=20)
    finally:
        pool.close()
    assert p1_big.candidates_per_arm == 20
    assert p1_big.pool_size > p1.pool_size
    assert p1_big.page == 1


def test_continue_page_with_invalid_token_raises(db_dsn, db_conn):
    cfg = SearchConfig()
    pool = open_pool(db_dsn)
    try:
        s = Searcher(pool=pool, cfg=cfg, embeddings=_E(), reranker=_R(), rewriter=None)
        with pytest.raises(CacheMissError):
            s.continue_page("nonexistent", page=2)
    finally:
        pool.close()
```

- [ ] **Step 2: Verify failing**

- [ ] **Step 3: Implement** — add methods to `Searcher` in `src/localmail/search/searcher.py`:

```python
    def continue_page(self, search_token: str, page: int) -> SearchPage:
        """Serve subsequent pages from the cached pool. Raises if past pool's end."""
        import math
        entry = self._cache.get(search_token)  # may raise CacheMissError
        page_size = entry["page_size"]
        pool_size = len(entry["hydrated"])
        max_page = max(1, math.ceil(pool_size / page_size))
        if page < 1 or page > max_page:
            raise PageOutOfPoolError(
                f"page {page} out of pool (pool_size={pool_size}, page_size={page_size}); "
                "call grow_pool to widen the candidate pool"
            )
        results = self._build_results(
            entry["hydrated"], entry["parsed"], entry["scores"], page, page_size,
        )
        return SearchPage(
            results=results, page=page, page_size=page_size, pool_size=pool_size,
            candidates_per_arm=entry["candidates_per_arm"],
            has_more_in_pool=pool_size > page * page_size,
            can_grow_pool=True,
            search_token=search_token, query=entry["parsed"],
            timing_ms={"cache_hit": 0.0},
        )

    def grow_pool(self, search_token: str, candidates_per_arm: int) -> SearchPage:
        """Re-run the pipeline with a larger candidate pool. Returns page 1."""
        entry = self._cache.get(search_token)
        parsed = entry["parsed"]
        self._cache.invalidate(search_token)
        # rerank pool grows proportionally so the larger arm output isn't wasted
        rps = max(candidates_per_arm, entry["rerank_pool_size"])
        page = self._search_with_parsed(parsed, page_size=entry["page_size"],
                                        candidates_per_arm=candidates_per_arm,
                                        rerank_pool_size=rps, use_cache=True)
        return page

    def _search_with_parsed(self, parsed, *, page_size, candidates_per_arm,
                            rerank_pool_size, use_cache):
        """Variant of search() that takes an already-parsed query."""
        import time, uuid
        t0 = time.monotonic()
        timing: dict[str, float] = {"parse": 0.0}
        with self._pool.connection() as conn:
            parsed = self._resolve_account_names(conn, parsed)
            t = time.monotonic()
            fused = self._retrieve_pool(conn, parsed, candidates_per_arm, rerank_pool_size)
            timing["retrieve"] = (time.monotonic() - t) * 1000
            hydrated = self._hydrate(conn, fused)
        t = time.monotonic()
        if self._reranker and hydrated:
            snippets = [item["snippet_source_text"][: self._cfg.snippet_width_chars * 4]
                        for item in hydrated]
            scores = self._reranker.rerank(parsed.rewritten_text or parsed.free_text, snippets)
        else:
            scores = [item["fused"].rrf_score for item in hydrated]
        timing["rerank"] = (time.monotonic() - t) * 1000
        results = self._build_results(hydrated, parsed, scores, page=1, page_size=page_size)
        timing["total"] = (time.monotonic() - t0) * 1000
        token = uuid.uuid4().hex[:16] if use_cache else None
        if token:
            self._cache.put(token, {
                "parsed": parsed, "hydrated": hydrated, "scores": scores,
                "candidates_per_arm": candidates_per_arm,
                "rerank_pool_size": rerank_pool_size, "page_size": page_size,
            })
        return SearchPage(
            results=results, page=1, page_size=page_size, pool_size=len(hydrated),
            candidates_per_arm=candidates_per_arm,
            has_more_in_pool=len(hydrated) > page_size, can_grow_pool=True,
            search_token=token, query=parsed, timing_ms=timing,
        )
```

- [ ] **Step 4: Verify passing**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_searcher_pagination.py -v
```

- [ ] **Step 5: Commit**

```bash
git add src/localmail/search/searcher.py tests/test_searcher_pagination.py
git commit -m "feat(search): Searcher.continue_page + grow_pool with cache-backed pagination"
```

---

## Task 17: `search/__init__.py` — `create_searcher` factory + public exports

**Files:**
- Modify: `src/localmail/search/__init__.py`
- Test: `tests/test_search_public_api.py`

- [ ] **Step 1: Write failing test** — `tests/test_search_public_api.py`:

```python
"""Sanity tests for the public Python API surface."""

from __future__ import annotations


def test_public_exports_available():
    from localmail.search import (
        create_searcher, Searcher, SearchPage, SearchResult,
        ParsedQuery, SearchFilters, QueryParseError,
    )
    assert callable(create_searcher)
    assert Searcher is not None
    assert SearchPage is not None
    assert SearchResult is not None
    assert ParsedQuery is not None
    assert SearchFilters is not None
    assert QueryParseError is not None


def test_create_searcher_returns_searcher(db_dsn):
    from localmail.config import LocalmailConfig
    from localmail.search import Searcher, create_searcher

    class _E:
        name = "s"; model = "s"; dimension = 768
        def embed_documents(self, t): return [[0.0]*768 for _ in t]
        def embed_query(self, t): return [0.0]*768
        def health_check(self): pass

    # NOTE: top-level config requires `database`; construct via model_validate.
    cfg = LocalmailConfig.model_validate({"database": {"dsn": db_dsn}})
    s = create_searcher(cfg=cfg, dsn=db_dsn, embeddings=_E(), reranker=None)
    assert isinstance(s, Searcher)
    s._pool.close()
```

- [ ] **Step 2: Verify failing**

- [ ] **Step 3: Implement** — replace `src/localmail/search/__init__.py`:

```python
"""Public Python API for localmail's hybrid search subsystem.

Stable surface: create_searcher (factory), Searcher (class), SearchPage and
SearchResult (result types), ParsedQuery / SearchFilters / QueryParseError
(query parsing). Everything else under localmail.search.* is reachable but
not part of the contract.
"""

from __future__ import annotations

from typing import Any

from localmail.config import LocalmailConfig, SearchConfig
from localmail.db import open_pool
from localmail.search.embeddings import EmbeddingBackend, FastEmbedBackend
from localmail.search.query import (
    ParsedQuery, QueryParseError, SearchFilters,
)
from localmail.search.reranker import FastEmbedReranker, Reranker
from localmail.search.searcher import SearchPage, SearchResult, Searcher

__all__ = [
    "create_searcher", "Searcher", "SearchPage", "SearchResult",
    "ParsedQuery", "SearchFilters", "QueryParseError",
]


def create_searcher(
    cfg: LocalmailConfig | None = None,
    *,
    dsn: str | None = None,
    embeddings: EmbeddingBackend | None = None,
    reranker: Reranker | None = None,
) -> Searcher:
    """Build a Searcher with config defaults; reuse the returned instance.

    For tests or custom DI, pass `embeddings` and/or `reranker` explicitly;
    otherwise the factory builds `FastEmbedBackend` and `FastEmbedReranker`
    from `cfg.search`. `dsn` defaults to the same value used by the CLI
    (read from the standard config file).
    """
    cfg = cfg or LocalmailConfig.load()  # existing LocalmailConfig.load()
    search_cfg: SearchConfig = cfg.search
    pool = open_pool(dsn or cfg.dsn)
    emb: EmbeddingBackend = embeddings or FastEmbedBackend(search_cfg)
    rr: Reranker | None = reranker
    if rr is None and search_cfg.reranker_enabled:
        rr = FastEmbedReranker(search_cfg)
    return Searcher(pool=pool, cfg=search_cfg, embeddings=emb, reranker=rr, rewriter=None)
```

NOTE: if `LocalmailConfig.load()` and `LocalmailConfig.dsn` don't match this exact shape in the existing codebase, adapt the call to whatever the existing CLI uses (read `cli.py` for the pattern; same code path).

- [ ] **Step 4: Verify passing**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_search_public_api.py -v
```

- [ ] **Step 5: Commit**

```bash
git add src/localmail/search/__init__.py tests/test_search_public_api.py
git commit -m "feat(search): public Python API surface + create_searcher factory"
```

---

## Task 18: CLI verb `localmail search`

**Files:**
- Modify: `src/localmail/cli.py`
- Test: `tests/test_cli_search.py`

- [ ] **Step 1: Read existing `cli.py`** to learn its click conventions and how `LocalmailConfig` is loaded — your new verb must follow the same pattern:

```bash
sed -n '1,80p' src/localmail/cli.py
```

- [ ] **Step 2: Write failing test** — `tests/test_cli_search.py`:

```python
"""CLI tests for `localmail search` using click's CliRunner."""

from __future__ import annotations

import json

from click.testing import CliRunner

from localmail.cli import main


def test_cli_search_help_shows_filter_flags():
    runner = CliRunner()
    result = runner.invoke(main, ["search", "--help"])
    assert result.exit_code == 0
    out = result.output
    for flag in ["--account", "--folder", "--after", "--before", "--from",
                 "--to", "--subject", "--has-attachment", "--label",
                 "--page-size", "--candidates-per-arm", "--rerank-pool",
                 "--no-rerank", "--smart", "--no-cache",
                 "--format", "--verbose"]:
        assert flag in out, f"missing flag {flag} in help"


def test_cli_search_json_output_is_valid_search_page(monkeypatch, db_dsn, db_conn):
    """End-to-end: seed mail, run search, parse JSON output."""
    with db_conn.cursor() as cur:
        cur.execute("INSERT INTO accounts (name,email_address,imap_host,auth_method)"
                    " VALUES ('a','a@x','h','password') RETURNING id")
        acct = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO messages (account_id, message_id, raw_sha256, subject,"
            " body_text, headers, raw_bytes, size_bytes)"
            " VALUES (%s, '<m1>', %s, %s, %s, '{}'::jsonb, 'r', 1)",
            (acct, b'\\x01'*32, "Berlin conference", "We are meeting in Berlin."),
        )
    db_conn.commit()

    # Monkeypatch create_searcher to use a stub embedder + reranker
    from localmail.search import create_searcher as real_create
    from localmail.search.embeddings import EmbeddingBackend

    class _E:
        name = "s"; model = "s"; dimension = 768
        def embed_documents(self, t): return [[0.5]*768 for _ in t]
        def embed_query(self, t): return [0.5]*768
        def health_check(self): pass

    def fake_create(cfg=None, **kw):
        return real_create(cfg=cfg, dsn=db_dsn, embeddings=_E(), reranker=None)

    monkeypatch.setattr("localmail.cli.create_searcher", fake_create)
    # Embed the seed first
    from localmail.search.embed_worker import run_embed_worker_once
    from localmail.config import SearchConfig
    run_embed_worker_once(db_conn, SearchConfig(), _E())

    runner = CliRunner()
    result = runner.invoke(main, ["search", "Berlin", "--format", "json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert "results" in payload
    assert payload["page"] == 1
```

- [ ] **Step 3: Verify failing**

- [ ] **Step 4: Implement** — add to `src/localmail/cli.py`:

```python
import json as _json
import sys
from datetime import date as _date
from dataclasses import asdict

import click

from localmail.search import create_searcher


def _page_to_dict(page) -> dict:
    """Convert a SearchPage into a JSON-serializable dict."""
    out = asdict(page)
    # ParsedQuery contains SearchFilters with date fields; ensure JSON-safe
    return _json.loads(_json.dumps(out, default=str))


def _print_text_page(page) -> None:
    if not page.results:
        click.echo("no results")
        return
    for r in page.results:
        click.echo(f"[{r.rank}] {r.date_sent or '-'}  {r.from_addr or '-':40.40s}  "
                   f"{r.subject or '(no subject)':60.60s}")
        click.echo(f"    score={r.score:.3f} (rrf={r.rrf_score:.4f})  {r.snippet_source}")
        if r.attachment_filename:
            click.echo(f"    [{r.attachment_filename}]")
        click.echo(f"    {r.snippet}")
        click.echo("")
    if page.search_token:
        click.echo(f"token: {page.search_token}   "
                   f"(page {page.page}, pool {page.pool_size})")
        if page.has_more_in_pool:
            click.echo(f"hint: localmail search-page {page.search_token} {page.page + 1}")
        if page.can_grow_pool:
            click.echo(f"hint: localmail search-grow {page.search_token} --candidates 200")


@main.command()
@click.argument("query", nargs=-1, required=True)
@click.option("--account", "accounts", multiple=True, help="restrict to account name(s)")
@click.option("--folder", "folders", multiple=True)
@click.option("--after", type=click.DateTime(formats=["%Y-%m-%d"]))
@click.option("--before", type=click.DateTime(formats=["%Y-%m-%d"]))
@click.option("--from", "from_substr")
@click.option("--to", "to_substr")
@click.option("--subject", "subject_substr")
@click.option("--has-attachment", is_flag=True, default=None)
@click.option("--label")
@click.option("--page-size", type=int)
@click.option("--candidates-per-arm", type=int)
@click.option("--rerank-pool", type=int)
@click.option("--no-rerank", is_flag=True)
@click.option("--smart", is_flag=True)
@click.option("--no-cache", is_flag=True)
@click.option("--format", "fmt", type=click.Choice(["text", "json"]), default="text")
@click.option("--verbose", is_flag=True)
def search(query, accounts, folders, after, before, from_substr, to_substr,
           subject_substr, has_attachment, label, page_size, candidates_per_arm,
           rerank_pool, no_rerank, smart, no_cache, fmt, verbose):
    """Hybrid BM25 + vector search over the local archive."""
    text_q = " ".join(query)
    # Inline operator-style filters
    extra: list[str] = []
    for a in accounts: extra.append(f"account:{a}")
    for f in folders: extra.append(f'folder:"{f}"')
    if from_substr: extra.append(f'from:"{from_substr}"')
    if to_substr: extra.append(f'to:"{to_substr}"')
    if subject_substr: extra.append(f'subject:"{subject_substr}"')
    if after: extra.append(f"after:{after.date().isoformat()}")
    if before: extra.append(f"before:{before.date().isoformat()}")
    if has_attachment: extra.append("has:attachment")
    if label: extra.append(f"label:{label}")
    if extra:
        text_q = f"{text_q} {' '.join(extra)}".strip()

    searcher = create_searcher()
    try:
        if no_rerank:
            # Bypass rerank by temporarily nulling it
            searcher._reranker = None  # documented internal — Phase 5 can promote
        page = searcher.search(
            text_q, page_size=page_size, candidates_per_arm=candidates_per_arm,
            rerank_pool_size=rerank_pool, use_cache=not no_cache, smart=smart,
        )
    except RuntimeError as exc:
        click.echo(f"error: {exc}", err=True); sys.exit(2)

    if verbose:
        click.echo(f"timing(ms): {page.timing_ms}", err=True)
    if fmt == "json":
        click.echo(_json.dumps(_page_to_dict(page), default=str))
    else:
        _print_text_page(page)
```

- [ ] **Step 5: Verify passing**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_cli_search.py -v
```

- [ ] **Step 6: Commit**

```bash
git add src/localmail/cli.py tests/test_cli_search.py
git commit -m "feat(cli): localmail search with operator flags + json/text output"
```

---

## Task 19: CLI verbs `localmail search-page` + `localmail search-grow`

**Files:**
- Modify: `src/localmail/cli.py`
- Test: `tests/test_cli_search.py` (extend)

- [ ] **Step 1: Note the cache scope limitation**

The page cache lives in the `Searcher` instance, which lives in the `create_searcher()` call. CLI invocations are short-lived (new process per call) → the in-process cache is useless for CLI pagination across separate `localmail` invocations. Two ways to handle:

- (A) Print a clear message: `"page cache lives in-process; CLI invocations don't share it. Re-run with --no-cache and grep, or use the Python API / MCP server."`
- (B) Persist the cache to a small SQLite or JSON file under `~/.cache/localmail/search/`, indexed by token, with the same TTL.

For Phase 1, ship (A) as the explicit behaviour — CLI is best for one-shot search; pagination is what the MCP/Python API are for. (B) can be added in Phase 5 if real CLI use surfaces the need.

- [ ] **Step 2: Write failing tests** — append to `tests/test_cli_search.py`:

```python
def test_cli_search_page_explains_in_process_cache_limitation():
    runner = CliRunner()
    result = runner.invoke(main, ["search-page", "deadbeef", "2"])
    assert result.exit_code == 2
    assert "in-process" in result.output.lower() or "cache" in result.output.lower()


def test_cli_search_grow_same_limitation():
    runner = CliRunner()
    result = runner.invoke(main, ["search-grow", "deadbeef", "--candidates", "200"])
    assert result.exit_code == 2
    assert "in-process" in result.output.lower() or "cache" in result.output.lower()
```

- [ ] **Step 3: Implement** — append to `src/localmail/cli.py`:

```python
_CACHE_HINT = (
    "the page cache lives in-process and isn't shared across CLI invocations. "
    "For deep pagination, use the Python API (localmail.search.create_searcher) "
    "or the MCP server (Phase 3). For one-shot follow-up, re-run with "
    "`localmail search ... --candidates-per-arm 200 --rerank-pool 200`."
)


@main.command("search-page")
@click.argument("token")
@click.argument("page", type=int)
def search_page(token, page):
    """Fetch a follow-up page from an earlier `localmail search` token.

    Not supported across separate CLI invocations — see message.
    """
    click.echo(_CACHE_HINT, err=True)
    sys.exit(2)


@main.command("search-grow")
@click.argument("token")
@click.option("--candidates", type=int, required=True)
def search_grow(token, candidates):
    """Re-run with a larger candidate pool — see CLI cache limitation."""
    click.echo(_CACHE_HINT, err=True)
    sys.exit(2)
```

- [ ] **Step 4: Verify passing**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_cli_search.py -v
```

- [ ] **Step 5: Commit**

```bash
git add src/localmail/cli.py tests/test_cli_search.py
git commit -m "feat(cli): search-page / search-grow stubs with in-process cache warning"
```

---

## Task 20: CLI `localmail embed-backfill` + `localmail search-status`

**Files:**
- Modify: `src/localmail/cli.py`
- Test: `tests/test_cli_embed_backfill.py`

- [ ] **Step 1: Write failing tests** — `tests/test_cli_embed_backfill.py`:

```python
"""Tests for embed-backfill and search-status CLI verbs."""

from __future__ import annotations

import json

from click.testing import CliRunner

from localmail.cli import main


def test_cli_embed_backfill_drains_queue(monkeypatch, db_dsn, db_conn):
    with db_conn.cursor() as cur:
        cur.execute("INSERT INTO accounts (name,email_address,imap_host,auth_method)"
                    " VALUES ('a','a@x','h','password') RETURNING id")
        acct = cur.fetchone()[0]
        for i in range(3):
            cur.execute(
                "INSERT INTO messages (account_id, message_id, raw_sha256, subject,"
                " body_text, headers, raw_bytes, size_bytes)"
                " VALUES (%s, %s, %s, 's', 'b', '{}'::jsonb, 'r', 1)",
                (acct, f"<m{i}>", bytes([i+1])*32),
            )
    db_conn.commit()

    class _E:
        name = "s"; model = "s"; dimension = 768
        def embed_documents(self, t): return [[0.5]*768 for _ in t]
        def embed_query(self, t): return [0.5]*768
        def health_check(self): pass

    from localmail.search import embed_worker as ew

    real_factory = ew.run_embed_worker_once

    def patched(*a, **kw): return real_factory(*a, **kw)
    monkeypatch.setattr("localmail.cli._make_backend", lambda cfg: _E())
    monkeypatch.setattr("localmail.cli._dsn", lambda: db_dsn)

    runner = CliRunner()
    result = runner.invoke(main, ["embed-backfill", "--no-progress"])
    assert result.exit_code == 0, result.output
    with db_conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM message_chunks WHERE embedding_v1 IS NOT NULL")
        assert cur.fetchone()[0] >= 3


def test_cli_search_status_reports_counts(monkeypatch, db_dsn, db_conn):
    monkeypatch.setattr("localmail.cli._dsn", lambda: db_dsn)
    runner = CliRunner()
    result = runner.invoke(main, ["search-status", "--format", "json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert "messages_total" in payload
    assert "chunks_total" in payload
    assert "chunks_embedded" in payload
    assert "failed_embeddings" in payload
```

- [ ] **Step 2: Verify failing**

- [ ] **Step 3: Implement** — add to `src/localmail/cli.py`:

```python
from localmail.config import LocalmailConfig
from localmail.db import open_pool
from localmail.search.embed_worker import run_embed_worker_once
from localmail.search.embeddings import FastEmbedBackend


def _dsn() -> str:
    """Resolve DSN from the existing localmail config (used by other CLI verbs too)."""
    return LocalmailConfig.load().dsn


def _make_backend(cfg):
    """Build the configured EmbeddingBackend. Override via monkeypatch in tests."""
    return FastEmbedBackend(cfg.search)


@main.command("embed-backfill")
@click.option("--account", "account_name")
@click.option("--no-progress", is_flag=True)
def embed_backfill(account_name, no_progress):
    """Drain the embedding queue in the foreground; exit when empty."""
    cfg = LocalmailConfig.load()
    backend = _make_backend(cfg)
    pool = open_pool(_dsn())
    try:
        total = 0
        while True:
            with pool.connection() as conn:
                wrote = run_embed_worker_once(conn, cfg.search, backend)
            if wrote == 0:
                break
            total += wrote
            if not no_progress:
                click.echo(f"embedded {wrote} chunks (total {total})", err=True)
    finally:
        pool.close()
    click.echo(f"done: {total} chunks embedded")


@main.command("search-status")
@click.option("--format", "fmt", type=click.Choice(["text", "json"]), default="text")
def search_status(fmt):
    """Show progress: how many chunks remain to be embedded, failures, etc."""
    pool = open_pool(_dsn())
    try:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM messages")
            messages_total = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM message_chunks")
            chunks_total = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM message_chunks WHERE embedding_v1 IS NOT NULL")
            chunks_embedded = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM failed_embeddings")
            failed = cur.fetchone()[0]
    finally:
        pool.close()
    payload = {
        "messages_total": messages_total,
        "chunks_total": chunks_total,
        "chunks_embedded": chunks_embedded,
        "chunks_pending": chunks_total - chunks_embedded,
        "failed_embeddings": failed,
    }
    if fmt == "json":
        click.echo(_json.dumps(payload))
    else:
        for k, v in payload.items():
            click.echo(f"{k:24s} {v}")
```

- [ ] **Step 4: Verify passing**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_cli_embed_backfill.py -v
```

- [ ] **Step 5: Commit**

```bash
git add src/localmail/cli.py tests/test_cli_embed_backfill.py
git commit -m "feat(cli): embed-backfill + search-status verbs"
```

---

## Task 21: CLI `list-failed-embeddings` + `retry-failed-embeddings`

**Files:**
- Modify: `src/localmail/cli.py`
- Test: `tests/test_cli_failed_embeddings.py`

- [ ] **Step 1: Write failing test** — `tests/test_cli_failed_embeddings.py`:

```python
"""Tests for the failed-embeddings inspection / retry verbs."""

from __future__ import annotations

import json

from click.testing import CliRunner

from localmail.cli import main


def _insert_failed(conn, n=3):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO accounts (name,email_address,imap_host,auth_method)"
                    " VALUES ('a','a@x','h','password') RETURNING id")
        acct = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO messages (account_id, raw_sha256, headers, raw_bytes, size_bytes)"
            " VALUES (%s, %s, '{}'::jsonb, 'r', 1) RETURNING id",
            (acct, b'\\x01'*32),
        )
        mid = cur.fetchone()[0]
        for i in range(n):
            cur.execute(
                "INSERT INTO message_chunks (message_id, kind, chunk_idx, text, token_count)"
                " VALUES (%s, 'body', %s, 'x', 1) RETURNING id", (mid, i),
            )
            cid = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO failed_embeddings (chunk_table, chunk_id, error_class,"
                " error_message) VALUES ('message_chunks', %s, 'X', 'msg %s')",
                (cid, i),
            )
    conn.commit()


def test_cli_list_failed_embeddings_json(monkeypatch, db_dsn, db_conn):
    _insert_failed(db_conn, n=2)
    monkeypatch.setattr("localmail.cli._dsn", lambda: db_dsn)
    runner = CliRunner()
    result = runner.invoke(main, ["list-failed-embeddings", "--format", "json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert len(payload) == 2
    assert payload[0]["error_class"] == "X"


def test_cli_retry_failed_embeddings_clears_rows(monkeypatch, db_dsn, db_conn):
    _insert_failed(db_conn, n=2)
    monkeypatch.setattr("localmail.cli._dsn", lambda: db_dsn)
    runner = CliRunner()
    result = runner.invoke(main, ["retry-failed-embeddings"])
    assert result.exit_code == 0
    with db_conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM failed_embeddings")
        assert cur.fetchone()[0] == 0
```

- [ ] **Step 2: Verify failing**

- [ ] **Step 3: Implement** — add to `src/localmail/cli.py`:

```python
@main.command("list-failed-embeddings")
@click.option("--limit", type=int, default=50)
@click.option("--format", "fmt", type=click.Choice(["text", "json"]), default="text")
def list_failed_embeddings(limit, fmt):
    """Show recent failed_embeddings rows."""
    pool = open_pool(_dsn())
    try:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT id, chunk_table, chunk_id, error_class, error_message,"
                " retry_count, failed_at, last_retry_at FROM failed_embeddings"
                " ORDER BY failed_at DESC LIMIT %s", (limit,),
            )
            rows = cur.fetchall()
    finally:
        pool.close()
    cols = ["id", "chunk_table", "chunk_id", "error_class", "error_message",
            "retry_count", "failed_at", "last_retry_at"]
    payload = [dict(zip(cols, r, strict=True)) for r in rows]
    if fmt == "json":
        click.echo(_json.dumps(payload, default=str))
    else:
        for p in payload:
            click.echo(f"#{p['id']:6d}  {p['chunk_table']}:{p['chunk_id']}  "
                       f"{p['error_class']}  retries={p['retry_count']}  "
                       f"{p['failed_at']}")
            click.echo(f"        {p['error_message']}")


@main.command("retry-failed-embeddings")
@click.option("--chunk-table", default=None,
              help="restrict to message_chunks or attachment_chunks")
def retry_failed_embeddings(chunk_table):
    """Clear failed_embeddings rows so the embed worker re-attempts them."""
    pool = open_pool(_dsn())
    try:
        with pool.connection() as conn, conn.cursor() as cur:
            if chunk_table:
                cur.execute("DELETE FROM failed_embeddings WHERE chunk_table = %s",
                            (chunk_table,))
            else:
                cur.execute("DELETE FROM failed_embeddings")
            n = cur.rowcount
        conn.commit()
    finally:
        pool.close()
    click.echo(f"cleared {n} failed_embeddings rows")
```

- [ ] **Step 4: Verify passing**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_cli_failed_embeddings.py -v
```

- [ ] **Step 5: Commit**

```bash
git add src/localmail/cli.py tests/test_cli_failed_embeddings.py
git commit -m "feat(cli): list-failed-embeddings + retry-failed-embeddings"
```

---

## Task 22: Daemon integration — spawn `embed_worker` thread

**Files:**
- Modify: `src/localmail/daemon.py`
- Test: `tests/test_daemon_embed_thread.py`

- [ ] **Step 1: Read existing `daemon.py`** to see how IDLE / poll threads are spawned per account so the new embed-worker thread follows the same lifecycle (single instance, shares `pool`, joins on stop):

```bash
sed -n '1,120p' src/localmail/daemon.py
```

- [ ] **Step 2: Write failing test** — `tests/test_daemon_embed_thread.py`:

```python
"""Test that Daemon spawns + cleanly joins the embed_worker thread."""

from __future__ import annotations

import threading
import time

from localmail.config import LocalmailConfig, SearchConfig
from localmail.daemon import Daemon


class _E:
    name = "s"; model = "s"; dimension = 768
    def embed_documents(self, t): return [[0.5]*768 for _ in t]
    def embed_query(self, t): return [0.5]*768
    def health_check(self): pass


def test_daemon_starts_embed_worker_when_enabled(db_dsn):
    cfg = LocalmailConfig.model_validate({"database": {"dsn": db_dsn}})
    cfg.search.run_embed_worker = True
    d = Daemon(cfg=cfg, dsn=db_dsn, embedding_backend_factory=lambda c: _E())
    d.start()
    time.sleep(0.5)
    names = {t.name for t in threading.enumerate()}
    assert any(n.startswith("embed_worker") for n in names)
    d.stop()
    d.join(timeout=5)
    names_after = {t.name for t in threading.enumerate()}
    assert not any(n.startswith("embed_worker") for n in names_after)


def test_daemon_skips_embed_worker_when_disabled(db_dsn):
    cfg = LocalmailConfig.model_validate({"database": {"dsn": db_dsn}})
    cfg.search.run_embed_worker = False
    d = Daemon(cfg=cfg, dsn=db_dsn, embedding_backend_factory=lambda c: _E())
    d.start()
    time.sleep(0.3)
    names = {t.name for t in threading.enumerate()}
    assert not any(n.startswith("embed_worker") for n in names)
    d.stop(); d.join(timeout=5)
```

- [ ] **Step 3: Implement** — modify `src/localmail/daemon.py`:

In `Daemon.__init__`, add the new field:
```python
def __init__(
    self, cfg: LocalmailConfig, dsn: str | None = None,
    embedding_backend_factory=None,
) -> None:
    self._cfg = cfg
    self._dsn = dsn or cfg.dsn
    self._stop = threading.Event()
    self._threads: list[threading.Thread] = []
    # New: factory so tests can pass a stub; prod default builds FastEmbedBackend
    self._embedding_backend_factory = embedding_backend_factory
```

In `Daemon.start()`, after the per-account thread spawn loop, add:
```python
if self._cfg.search.run_embed_worker:
    from localmail.search.embed_worker import run_embed_worker

    if self._embedding_backend_factory is None:
        from localmail.search.embeddings import FastEmbedBackend
        backend = FastEmbedBackend(self._cfg.search)
    else:
        backend = self._embedding_backend_factory(self._cfg.search)
    pool = open_pool(self._dsn)
    t = threading.Thread(
        target=run_embed_worker,
        args=(self._stop, pool, self._cfg.search, backend),
        name="embed_worker",
        daemon=True,
    )
    t.start()
    self._threads.append(t)
    # Stash pool so we can close it on stop
    self._embed_pool = pool
```

In `Daemon.stop()` / cleanup, add:
```python
if hasattr(self, "_embed_pool"):
    try:
        self._embed_pool.close()
    except Exception:
        pass
```

- [ ] **Step 4: Verify passing**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_daemon_embed_thread.py -v
unset VIRTUAL_ENV && uv run pytest tests/test_daemon.py -v  # no regression
```

- [ ] **Step 5: Commit**

```bash
git add src/localmail/daemon.py tests/test_daemon_embed_thread.py
git commit -m "feat(daemon): spawn embed_worker thread when search.run_embed_worker is true"
```

---

## Task 23: Multilingual test corpus — `tests/_multilingual_corpus.py`

**Files:**
- Create: `tests/_multilingual_corpus.py`
- Create: `tests/fixtures/multilingual_queries.example.json`
- Test: `tests/test_multilingual_corpus.py`

- [ ] **Step 1: Write failing test** — `tests/test_multilingual_corpus.py`:

```python
"""Sanity tests for the multilingual fixture corpus."""

from __future__ import annotations

from tests._multilingual_corpus import build_corpus


def test_build_corpus_returns_messages_for_all_target_languages(db_conn):
    msgs = build_corpus(db_conn)
    langs = {m["lang"] for m in msgs}
    assert {"de", "en", "es", "ja", "no"}.issubset(langs)
    assert len(msgs) >= 50


def test_build_corpus_inserts_into_messages_table(db_conn):
    msgs = build_corpus(db_conn)
    with db_conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM messages")
        assert cur.fetchone()[0] == len(msgs)
```

- [ ] **Step 2: Verify failing**

- [ ] **Step 3: Implement** — `tests/_multilingual_corpus.py`:

```python
"""Synthetic multilingual email corpus for Phase 1 acceptance.

Builds ~50 emails across de / en / es / no / ja using existing `_eml.py`
patterns (no .eml files on disk). Each message has a short subject and a
2-4 sentence body in the target language. The corpus is intentionally
small so that author-supplied ground-truth queries are tractable to
verify by eye; it's not a benchmark suite.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

_BASE = datetime(2025, 1, 1, tzinfo=timezone.utc)

# (lang, subject, body)
_SEED: list[tuple[str, str, str]] = [
    # German (10)
    ("de", "Konferenz Berlin", "Wir treffen uns nächste Woche zur Konferenz in Berlin. Bitte bringe das Programm mit."),
    ("de", "Mittagessen morgen", "Hast du Lust auf Mittagessen morgen um 12:30 im Café am Markt?"),
    ("de", "Reisekostenabrechnung", "Bitte reiche deine Reisekostenabrechnung bis Freitag ein, sonst verzögert sich die Auszahlung."),
    ("de", "Geburtstagsgeschenk Mama", "Was schenken wir Mama zum 70. Geburtstag? Hast du Ideen?"),
    ("de", "Urlaub Toskana", "Wir buchen unseren Urlaub in der Toskana für September. Drei Wochen."),
    ("de", "Arzttermin Verschoben", "Mein Arzttermin wurde auf Dienstag nächster Woche verschoben."),
    ("de", "Re: Rechnungen Q3", "Anbei die offenen Rechnungen für das dritte Quartal. Bitte zur Freigabe."),
    ("de", "Wohnungsbesichtigung", "Die Wohnungsbesichtigung in Hamburg-Eppendorf ist am Samstag um 14 Uhr."),
    ("de", "Heizung defekt", "Die Heizung ist seit gestern Abend ausgefallen. Hast du eine Empfehlung für einen Klempner?"),
    ("de", "Konferenzprogramm anbei", "Anbei das Programm für die Berliner Konferenz. Donnerstag Plenarsitzung im Saal Berlin."),
    # English (10)
    ("en", "Berlin conference next week", "Looking forward to the conference in Berlin next week. Please bring the agenda."),
    ("en", "Lunch tomorrow", "Want to grab lunch tomorrow at 12:30 at the corner café?"),
    ("en", "Q3 expense reports", "Please submit your Q3 expense reports by Friday to avoid payment delays."),
    ("en", "Mom's 70th birthday", "What should we get Mom for her 70th birthday? Any ideas?"),
    ("en", "Tuscany vacation booked", "We've booked the Tuscany vacation for September. Three weeks."),
    ("en", "Doctor appointment moved", "My doctor's appointment got moved to next Tuesday."),
    ("en", "Re: Q3 invoices", "Attached are the outstanding Q3 invoices for your approval."),
    ("en", "Apartment viewing Hamburg", "Apartment viewing in Hamburg-Eppendorf this Saturday at 2pm."),
    ("en", "Heating broken", "Heating's been out since last night. Any plumber recommendations?"),
    ("en", "Conference program attached", "Attached the program for the Berlin conference. Thursday plenary in Hall Berlin."),
    # Spanish (10)
    ("es", "Conferencia en Berlín", "Nos vemos la próxima semana en la conferencia de Berlín. Trae el programa por favor."),
    ("es", "Comida mañana", "¿Quieres comer mañana a las 12:30 en el café de la esquina?"),
    ("es", "Gastos del Q3", "Por favor envía tus gastos del Q3 antes del viernes para evitar retrasos."),
    ("es", "Cumpleaños 70 de mamá", "¿Qué le regalamos a mamá por su 70 cumpleaños? ¿Tienes ideas?"),
    ("es", "Vacaciones Toscana", "Hemos reservado las vacaciones en Toscana para septiembre. Tres semanas."),
    ("es", "Cita médica aplazada", "Mi cita médica fue movida al próximo martes."),
    ("es", "Re: facturas Q3", "Adjunto las facturas pendientes del Q3 para tu aprobación."),
    ("es", "Visita piso Hamburgo", "Visita al piso en Hamburgo-Eppendorf este sábado a las 14h."),
    ("es", "Calefacción rota", "La calefacción no funciona desde anoche. ¿Recomiendas algún fontanero?"),
    ("es", "Programa conferencia adjunto", "Adjunto el programa de la conferencia de Berlín. Jueves plenaria en la Sala Berlín."),
    # Norwegian (10) — short, vocabulary-frugal: BM25 should carry most of the load
    ("no", "Konferanse i Berlin", "Vi møtes neste uke på konferansen i Berlin. Ta med programmet."),
    ("no", "Lunsj i morgen", "Vil du ta lunsj i morgen klokken 12:30 på kafeen på hjørnet?"),
    ("no", "Reiseregning Q3", "Send inn reiseregningen for Q3 innen fredag."),
    ("no", "Mammas 70-årsdag", "Hva gir vi mamma til 70-årsdagen? Har du ideer?"),
    ("no", "Ferie i Toscana", "Vi har booket ferien i Toscana i september. Tre uker."),
    ("no", "Legetime flyttet", "Legetimen min er flyttet til neste tirsdag."),
    ("no", "Re: fakturaer Q3", "Vedlagt åpne fakturaer for Q3 til godkjenning."),
    ("no", "Visning leilighet Hamburg", "Visning av leilighet i Hamburg-Eppendorf på lørdag kl 14."),
    ("no", "Varmen er borte", "Varmen har vært borte siden i natt. Kjenner du en rørlegger?"),
    ("no", "Konferanseprogram vedlagt", "Vedlagt programmet for Berlin-konferansen. Torsdag plenum i sal Berlin."),
    # Japanese (10)
    ("ja", "ベルリン会議", "来週ベルリンで開催される会議でお会いしましょう。アジェンダをお持ちください。"),
    ("ja", "明日のランチ", "明日12時半に角のカフェでランチはどうですか?"),
    ("ja", "第3四半期の経費報告", "金曜日までに第3四半期の経費報告を提出してください。"),
    ("ja", "母の70歳の誕生日", "母の70歳の誕生日に何を贈りましょうか?何かアイデアはありますか?"),
    ("ja", "トスカーナ休暇予約", "9月のトスカーナ休暇を予約しました。3週間です。"),
    ("ja", "通院日変更", "通院日が来週火曜日に変更になりました。"),
    ("ja", "Re: Q3請求書", "Q3の未払い請求書を添付します。承認をお願いします。"),
    ("ja", "ハンブルク内見", "ハンブルク・エッペンドルフのアパート内見は土曜14時です。"),
    ("ja", "暖房故障", "昨夜から暖房が止まっています。配管工をご存知ですか?"),
    ("ja", "会議プログラム添付", "ベルリン会議のプログラムを添付します。木曜午前は本会議場ベルリンにて。"),
]


def build_corpus(conn) -> list[dict[str, Any]]:
    """Insert the synthetic corpus into `accounts` + `messages`; return seed list."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO accounts (name, email_address, imap_host, auth_method)"
            " VALUES ('multilingual', 'test@example', 'host', 'password') RETURNING id"
        )
        acct = cur.fetchone()[0]
        out: list[dict[str, Any]] = []
        for i, (lang, subj, body) in enumerate(_SEED):
            sha = bytes([i + 1]) * 32
            cur.execute(
                "INSERT INTO messages (account_id, message_id, raw_sha256, subject,"
                " from_addr, body_text, date_sent, headers, raw_bytes, size_bytes)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, '{}'::jsonb, %s, %s)"
                " RETURNING id",
                (
                    acct, f"<mc{i}@local>", sha, subj, f"user{i % 5}@example.com",
                    body, _BASE + timedelta(days=i), b"raw", len(body),
                ),
            )
            mid = cur.fetchone()[0]
            out.append({"id": mid, "lang": lang, "subject": subj, "body": body})
    conn.commit()
    return out
```

- [ ] **Step 4: Write the example queries file** — `tests/fixtures/multilingual_queries.example.json`:

```json
{
  "_doc": "Author 20 queries per language (10 for Norwegian) with the relevant message subjects as ground truth. Run `localmail eval` (Task 24) to compute recall@20 + MRR@20.",
  "queries": [
    {"lang": "de", "query": "Berlin", "relevant_subjects": ["Konferenz Berlin", "Konferenzprogramm anbei"]},
    {"lang": "de", "query": "Mama Geburtstag", "relevant_subjects": ["Geburtstagsgeschenk Mama"]},
    {"lang": "de", "query": "Heizung", "relevant_subjects": ["Heizung defekt"]},
    {"lang": "en", "query": "Berlin conference", "relevant_subjects": ["Berlin conference next week", "Conference program attached"]},
    {"lang": "en", "query": "Mom birthday", "relevant_subjects": ["Mom's 70th birthday"]},
    {"lang": "es", "query": "conferencia Berlín", "relevant_subjects": ["Conferencia en Berlín", "Programa conferencia adjunto"]},
    {"lang": "ja", "query": "ベルリン", "relevant_subjects": ["ベルリン会議", "会議プログラム添付"]}
  ]
}
```

The file is `.example.json`, not `.json`, so it's a template. The user fills in 20 queries per language (10 for Norwegian) into `multilingual_queries.json` before running the eval.

- [ ] **Step 5: Verify passing**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_multilingual_corpus.py -v
```

- [ ] **Step 6: Commit**

```bash
git add tests/_multilingual_corpus.py tests/test_multilingual_corpus.py \
        tests/fixtures/multilingual_queries.example.json
git commit -m "test: multilingual fixture corpus (de/en/es/no/ja) + queries template"
```

---

## Task 24: Acceptance eval harness + README "Search" section

**Files:**
- Create: `tests/acceptance/__init__.py` (empty), `tests/acceptance/run_recall_eval.py`
- Modify: `README.md`
- Modify: `CLAUDE.md`

- [ ] **Step 1: Write the eval harness** — `tests/acceptance/run_recall_eval.py`:

```python
"""Recall@K + MRR@K eval harness for the Phase-1 multilingual acceptance suite.

Usage:
    LOCALMAIL_TEST_DSN=postgresql://... \\
      uv run python tests/acceptance/run_recall_eval.py \\
      --queries tests/fixtures/multilingual_queries.json \\
      --k 20

Prints a per-language summary and an overall pass/fail against the
Phase-1 targets (recall@20 ≥ 80% and MRR@20 ≥ 0.5 for de/en/es/ja;
Norwegian reported but not gated).
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

import psycopg

from localmail.config import SearchConfig
from localmail.db import apply_migrations, open_pool
from localmail.search.embed_worker import run_embed_worker_once
from localmail.search.embeddings import FastEmbedBackend
from localmail.search.searcher import Searcher

from tests._multilingual_corpus import build_corpus

TARGETS = {"de": (0.80, 0.50), "en": (0.80, 0.50),
           "es": (0.80, 0.50), "ja": (0.80, 0.50)}


def _reciprocal_rank(ordered_subjects: list[str], relevant: set[str]) -> float:
    for i, s in enumerate(ordered_subjects, start=1):
        if s in relevant:
            return 1.0 / i
    return 0.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--queries", required=True, type=Path)
    ap.add_argument("--k", type=int, default=20)
    ap.add_argument("--dsn", default=None)
    args = ap.parse_args()

    dsn = args.dsn or "postgresql://localmail:local%40%40mail@localhost:5532/localmail_test"
    apply_migrations(dsn)
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE accounts, mailboxes, messages, message_labels,"
                        " attachment_blobs, failed_messages, message_chunks,"
                        " failed_embeddings, embedding_models RESTART IDENTITY CASCADE")
        conn.commit()
        seeded = build_corpus(conn)
        subj_by_id = {m["id"]: m["subject"] for m in seeded}

        cfg = SearchConfig()
        backend = FastEmbedBackend(cfg)
        while run_embed_worker_once(conn, cfg, backend) > 0:
            pass

    pool = open_pool(dsn)
    try:
        searcher = Searcher(pool=pool, cfg=cfg, embeddings=backend,
                            reranker=None, rewriter=None)
        suite = json.loads(args.queries.read_text())
        per_lang_recall = defaultdict(list)
        per_lang_mrr = defaultdict(list)
        for q in suite["queries"]:
            page = searcher.search(q["query"], page_size=args.k,
                                   candidates_per_arm=args.k * 3,
                                   rerank_pool_size=args.k * 3)
            ranked = [r.subject for r in page.results]
            relevant = set(q["relevant_subjects"])
            hits = len([s for s in ranked if s in relevant])
            recall = hits / max(1, len(relevant))
            per_lang_recall[q["lang"]].append(min(1.0, recall))
            per_lang_mrr[q["lang"]].append(_reciprocal_rank(ranked, relevant))
    finally:
        pool.close()

    failures: list[str] = []
    print(f"{'lang':<6} {'#q':>4} {'recall@K':>10} {'MRR@K':>8}  status")
    for lang in sorted({*per_lang_recall, *per_lang_mrr}):
        recalls = per_lang_recall[lang]
        mrrs = per_lang_mrr[lang]
        r = statistics.fmean(recalls); m = statistics.fmean(mrrs)
        target = TARGETS.get(lang)
        status = "—"
        if target:
            ok = r >= target[0] and m >= target[1]
            status = "PASS" if ok else "FAIL"
            if not ok:
                failures.append(f"{lang}: recall={r:.3f} (need {target[0]}), MRR={m:.3f} (need {target[1]})")
        print(f"{lang:<6} {len(recalls):>4} {r:>10.3f} {m:>8.3f}  {status}")
    if failures:
        print("\nFAILURES (Phase 1 acceptance gates not met):", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1
    print("\nAll gated languages PASS Phase 1 acceptance.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

`tests/acceptance/__init__.py` is empty.

- [ ] **Step 2: Run the eval harness against the example queries** (smoke):

```bash
unset VIRTUAL_ENV && uv run python tests/acceptance/run_recall_eval.py \
    --queries tests/fixtures/multilingual_queries.example.json --k 20
```
Expected: harness runs end-to-end (model download takes time first call). The example file only has 7 queries — recall numbers will be small but the harness should print a table without crashing. The user authors the real `multilingual_queries.json` separately and re-runs to gate Phase 1.

- [ ] **Step 3: README update** — append a "Search" section to `README.md`:

```markdown
## Search (Phase 1)

`localmail` ships a hybrid BM25 + vector search subsystem. Once initial
backfill completes, you can search the local archive from the CLI or
from Python.

### Setup

```bash
# Apply migrations (creates message_chunks, indexes, etc.)
uv run localmail init-db

# Backfill embeddings for an existing archive. First run downloads
# ~250 MB of model weights to ~/.cache/fastembed/ (one-time).
uv run localmail embed-backfill
```

### Search from the CLI

```bash
uv run localmail search "Berlin conference"
uv run localmail search "invoice has:attachment after:2025-01-01 from:anna"
uv run localmail search "Heizung" --format json | jq
```

### Search from Python

```python
from localmail.search import create_searcher

searcher = create_searcher()
page = searcher.search("Berlin conference", page_size=20)
for r in page.results:
    print(r.rank, r.score, r.subject, r.snippet)

# Page 2:
page2 = searcher.continue_page(page.search_token, page=2)

# Needle-in-haystack — widen the candidate pool:
deeper = searcher.grow_pool(page.search_token, candidates_per_arm=200)
```

### Embedding model

The default model is **EmbeddingGemma-300M** (`google/embeddinggemma-300m`),
distributed under the [Gemma Terms of Use](https://ai.google.dev/gemma/terms).
The model weights are downloaded at runtime by fastembed; by using the
default you accept those terms.

For a strictly Apache-2.0 alternative:

```toml
[search]
embedding_model = "Snowflake/snowflake-arctic-embed-l-v2.0"
embedding_dim = 1024
```

Re-run `localmail embed-backfill` after switching models (the design
supports coexisting `embedding_v1` / `embedding_v2` columns for in-place
migration — see Phase 5).

### Tuning

All knobs live in `[search]` in `~/.config/localmail/config.toml`. The
defaults are calibrated for hundreds of thousands of messages on a
modern laptop. The most likely knobs to touch:

- `candidates_per_arm` (default 50) — increase for hard queries
- `rerank_pool_size` (default 50) — match `candidates_per_arm`
- `chunk_size_tokens` (default 512) — smaller for short messages
```

- [ ] **Step 4: Update `CLAUDE.md`** — append a "Search subsystem" section after "Sync model":

```markdown
## Search subsystem (Phase 1 shipped)

Hybrid BM25 + vector search over messages. See
[docs/superpowers/specs/2026-05-16-hybrid-search-design.md](docs/superpowers/specs/2026-05-16-hybrid-search-design.md)
for the full design and [docs/superpowers/plans/2026-05-16-hybrid-search-phase1.md](docs/superpowers/plans/2026-05-16-hybrid-search-phase1.md)
for the Phase 1 implementation plan.

- Code lives under `src/localmail/search/` — `chunking.py`, `embeddings.py`,
  `reranker.py`, `query.py`, `searcher.py`, `arms.py`, `page_cache.py`,
  `embed_worker.py`. Public API: `localmail.search.create_searcher`.
- All numeric tunables in `LocalmailConfig.search` (`SearchConfig`).
  **No magic numbers elsewhere in search code.**
- BM25 via `pg_search` (ParadeDB) — AGPL-3, PG18-compatible. Required
  extension; install via the ParadeDB prebuilt binaries.
- Vector via pgvector HNSW + `halfvec(768)`. Default embedder:
  EmbeddingGemma-300M via fastembed (Gemma Terms — runtime download).
- One embed_worker thread per process (account-agnostic; backend-bound).
  Lazily chunks messages it sees without chunks; per-chunk SAVEPOINT
  isolates poison chunks into `failed_embeddings`.
- Phase 2 (attachment search), Phase 3 (MCP), Phase 4 (--smart),
  Phase 5 (polish) — separate design + plans.
```

- [ ] **Step 5: Final full test suite**

```bash
unset VIRTUAL_ENV && uv run pytest -q -m "not slow"
```
Expected: all green. The slow `test_fastembed_backend_real_model_smoke` is opt-in.

- [ ] **Step 6: Commit**

```bash
git add tests/acceptance/__init__.py tests/acceptance/run_recall_eval.py README.md CLAUDE.md
git commit -m "test+docs: Phase 1 acceptance eval harness + README/CLAUDE.md search docs"
```

---

## Phase 1 complete

When all 24 tasks are committed:

1. Confirm the full test suite is green:
   ```bash
   unset VIRTUAL_ENV && uv run pytest -q
   ```
2. Author your real `tests/fixtures/multilingual_queries.json` (20 queries per language for de/en/es/ja, 10 for no) against your own archive sample, and run:
   ```bash
   unset VIRTUAL_ENV && uv run python tests/acceptance/run_recall_eval.py \
       --queries tests/fixtures/multilingual_queries.json --k 20
   ```
3. Verify recall@20 ≥ 80% and MRR@20 ≥ 0.5 for de/en/es/ja. If any language fails, **do not silently accept** — document the gap, propose a remediation (e.g. swap to Snowflake arctic-embed-2), and decide before moving to Phase 2.
4. Spot-check the latency budget on a real ~50k-message corpus: `localmail search "test query" --verbose` should report p50 < 800 ms / p95 < 1.5 s for default settings on M-series.

Phase 2 (attachment extraction + Arm 4) gets its own design checkpoint and plan file once Phase 1 is gated.

