# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Integration tests for the embed worker against a real Postgres."""

from __future__ import annotations

import contextlib
import logging

import pytest

from localmail.config import SearchConfig
from localmail.search.embed_worker import (
    record_failed_embedding,
    run_embed_worker_once,
)
from localmail.search.sweep_pacing import SweepOutcome


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
    sweep = run_embed_worker_once(db_conn, cfg, _StaticEmbedder())
    assert sweep.embedded >= 1
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
    assert first.embedded >= 1 and second.embedded == 0


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
        record_failed_embedding(db_conn, "message_chunks", cid, exc)
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
            record_failed_embedding(db_conn, "message_chunks", cid, exc)
            db_conn.commit()
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT retry_count FROM failed_embeddings WHERE chunk_id=%s", (cid,))
        assert cur.fetchone()[0] == 2  # 0 on insert, +1 each subsequent


def test_claim_filter_skips_chunks_past_max_retries(db_conn):
    """Chunks with retry_count >= max are excluded from claim selection."""
    mid = _seed_message(db_conn, body="x")
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO message_chunks (message_id, kind, chunk_idx, text, token_count)"
            " VALUES (%s, 'body', 0, 'x', 1) RETURNING id", (mid,))
        cid = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO failed_embeddings (chunk_table, chunk_id, error_class,"
            " error_message, retry_count) VALUES ('message_chunks', %s, 'X', 'X', 5)",
            (cid,),
        )
    db_conn.commit()

    cfg = SearchConfig(embed_worker_max_chunk_retries=3)
    sweep = run_embed_worker_once(db_conn, cfg, _StaticEmbedder())
    assert sweep.embedded == 0


def test_batch_failure_does_not_poison_queue(db_conn):
    """A transient backend error rolls back without marking chunks failed."""
    _seed_message(db_conn, body="hello world")

    class _Boom:
        name = "boom"; model = "boom"; dimension = 768
        def embed_documents(self, texts): raise RuntimeError("transient blip")
        def embed_query(self, t): return [0.0] * 768
        def health_check(self): pass

    cfg = SearchConfig(embed_worker_max_chunk_retries=3)
    # First sweep chunks the message but the batch embedding raises.
    sweep = run_embed_worker_once(db_conn, cfg, _Boom())
    assert sweep.embedded == 0

    with db_conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM failed_embeddings")
        assert cur.fetchone()[0] == 0

    # A subsequent sweep with a working backend embeds the same chunks.
    sweep = run_embed_worker_once(db_conn, cfg, _StaticEmbedder())
    assert sweep.embedded >= 1


def test_chunking_failure_records_failed_chunking_and_skips_message(db_conn, monkeypatch):
    """A poison message lands in failed_chunkings and is excluded next sweep."""
    from localmail.search import embed_worker as ew

    mid = _seed_message(db_conn, body="poisonous content")

    real_chunk_message = ew.chunk_message
    calls = {"n": 0}

    def boom_chunk_message(msg, cfg):
        calls["n"] += 1
        if msg.id == mid:
            raise RuntimeError("cannot chunk this message")
        return real_chunk_message(msg, cfg)

    monkeypatch.setattr(ew, "chunk_message", boom_chunk_message)

    cfg = SearchConfig(embed_worker_max_chunk_retries=1)
    # Sweep 1: failure recorded with retry_count = 0.
    run_embed_worker_once(db_conn, cfg, _StaticEmbedder())
    # Sweep 2: row still selectable (0 < 1); retry bumps to 1.
    run_embed_worker_once(db_conn, cfg, _StaticEmbedder())

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT error_class, retry_count FROM failed_chunkings WHERE message_id=%s",
            (mid,),
        )
        row = cur.fetchone()
    assert row is not None
    assert row[0] == "RuntimeError"
    assert row[1] == 1

    calls_before = calls["n"]
    # Sweep 3: 1 >= 1 → message excluded, chunk_message not invoked again.
    run_embed_worker_once(db_conn, cfg, _StaticEmbedder())
    assert calls["n"] == calls_before


def test_insert_failure_isolates_poison_message_per_savepoint(db_conn, monkeypatch):
    """A DB-level INSERT failure in one message rolls back only that message;
    a sibling message in the same sweep still gets chunks.

    Existing tests cover chunk_message() *raising*; this covers the chunk
    INSERT itself failing inside the per-message SAVEPOINT. A NUL byte in chunk
    text makes Postgres reject the INSERT (TEXT can't hold NUL), exercising the
    failure path at the INSERT layer. (Added while investigating #5 — guards the
    per-message poison isolation that any future INSERT-batching change must
    preserve.)
    """
    from localmail.search import embed_worker as ew
    from localmail.search.chunking import ChunkSpec

    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO accounts (name, email_address, imap_host, auth_method)"
            " VALUES ('iso', 'iso@x', 'h', 'password') RETURNING id"
        )
        acct = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO messages (account_id, message_id, raw_sha256, subject,"
            " from_addr, body_text, headers, raw_bytes, size_bytes)"
            " VALUES (%s, '<good@x>', %s, 'g', 'x@y', 'good body', '{}'::jsonb, 'r', 3)"
            " RETURNING id",
            (acct, b"\x02" * 32),
        )
        good_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO messages (account_id, message_id, raw_sha256, subject,"
            " from_addr, body_text, headers, raw_bytes, size_bytes)"
            " VALUES (%s, '<bad@x>', %s, 'b', 'x@y', 'bad body', '{}'::jsonb, 'r', 3)"
            " RETURNING id",
            (acct, b"\x03" * 32),
        )
        bad_id = cur.fetchone()[0]
    db_conn.commit()

    def chunk_message_with_poison(msg, cfg):
        text = "\x00" if msg.id == bad_id else "ok"
        return [ChunkSpec(kind="body", chunk_idx=0, text=text, token_count=1)]

    monkeypatch.setattr(ew, "chunk_message", chunk_message_with_poison)

    cfg = SearchConfig(embed_worker_max_chunk_retries=3)
    run_embed_worker_once(db_conn, cfg, _StaticEmbedder())

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM message_chunks WHERE message_id = %s", (good_id,))
        assert cur.fetchone()[0] == 1
        cur.execute(
            "SELECT count(*) FROM message_chunks WHERE message_id = %s", (bad_id,))
        assert cur.fetchone()[0] == 0
        cur.execute(
            "SELECT count(*) FROM failed_chunkings WHERE message_id = %s", (bad_id,))
        assert cur.fetchone()[0] == 1


# ---------------------------------------------------------------------------
# Phase 2: attachment_text chunking + embedding
# ---------------------------------------------------------------------------


def _seed_blob(conn, payload: bytes, text: str, extractor: str = "lightweight@1.0") -> bytes:
    """Insert attachment_blobs + attachment_text rows; return sha256 bytes."""
    import hashlib

    sha = hashlib.sha256(payload).digest()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO attachment_blobs (sha256, path, mime_type, size_bytes)"
            " VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
            (sha, f"/nonexistent/{sha.hex()[:8]}", "text/plain", len(payload)),
        )
        cur.execute(
            "INSERT INTO attachment_text (sha256, extractor, extracted_text)"
            " VALUES (%s, %s, %s)",
            (sha, extractor, text),
        )
    conn.commit()
    return sha


class _FakeBackend:
    """Minimal EmbeddingBackend stub that returns zero vectors."""

    name = "fake"
    model = "fake"
    dimension = 768

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Return a zero vector for each input text."""
        return [[0.0] * 768 for _ in texts]

    def embed_query(self, text: str) -> list[float]:
        """Return a zero query vector."""
        return [0.0] * 768

    def health_check(self) -> None:
        """No-op health check."""


def test_embed_worker_chunks_attachment_text(db_conn) -> None:
    """attachment_text rows with extracted_text != '' produce
    attachment_chunks rows on the next embed_worker pass."""
    sha = _seed_blob(db_conn, b"unique blob bytes", "this is the extracted body of an attachment")
    cfg = SearchConfig()
    run_embed_worker_once(db_conn, cfg, _FakeBackend())

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM attachment_chunks WHERE sha256 = %s",
            (sha,),
        )
        row = cur.fetchone()
        assert row is not None
        assert row[0] >= 1


def test_embed_worker_skips_sentinel_attachment_text(db_conn) -> None:
    """attachment_text rows with extracted_text='' produce zero chunks."""
    sha = _seed_blob(db_conn, b"empty marker", "", extractor="lightweight-empty")
    cfg = SearchConfig()
    run_embed_worker_once(db_conn, cfg, _FakeBackend())

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM attachment_chunks WHERE sha256 = %s",
            (sha,),
        )
        row = cur.fetchone()
        assert row is not None
        assert row[0] == 0


def test_whitespace_only_attachment_text_is_healed_and_leaves_the_claim(db_conn) -> None:
    """#266: a legacy whitespace-only row passes the `<> ''` claim filter but
    chunks to nothing, so it used to be re-claimed on every sweep forever —
    enough of them sorting low in the sha256 order fills the batch and stops
    attachment ingestion archive-wide (the #216 shape). The worker now stamps
    such a row to the '' sentinel in place, so it leaves the claim for good."""
    from localmail.search.embed_worker import _chunk_attachments_lazily

    sha = _seed_blob(db_conn, b"legacy whitespace blob", " \n\t\n  ")
    cfg = SearchConfig()

    first = _chunk_attachments_lazily(db_conn, cfg, batch=50)
    assert first == 1  # claimed once — and healed

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT extracted_text FROM attachment_text WHERE sha256 = %s", (sha,))
        row = cur.fetchone()
        assert row is not None
        assert row[0] == ""  # now the sentinel the claim filter skips
        cur.execute(
            "SELECT count(*) FROM attachment_chunks WHERE sha256 = %s", (sha,))
        row = cur.fetchone()
        assert row is not None
        assert row[0] == 0

    second = _chunk_attachments_lazily(db_conn, cfg, batch=50)
    assert second == 0  # never re-claimed


def test_attachment_text_that_chunks_is_never_overwritten(db_conn) -> None:
    """The heal is an in-place UPDATE on a column nothing can rebuild — the
    blob is not re-extractable once an attachment_text row exists — so pin
    that a row which does produce chunks keeps its text verbatim."""
    from localmail.search.embed_worker import _chunk_attachments_lazily

    text = "  Real extracted text, with leading and trailing space.  \n"
    sha = _seed_blob(db_conn, b"substantive blob", text)
    cfg = SearchConfig()

    assert _chunk_attachments_lazily(db_conn, cfg, batch=50) == 1

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT extracted_text FROM attachment_text WHERE sha256 = %s", (sha,))
        row = cur.fetchone()
        assert row is not None
        assert row[0] == text
        cur.execute(
            "SELECT count(*) FROM attachment_chunks WHERE sha256 = %s", (sha,))
        row = cur.fetchone()
        assert row is not None
        assert row[0] >= 1


def test_zero_chunks_from_substantive_text_warns_instead_of_deleting(
    db_conn, monkeypatch, caplog
) -> None:
    """If the chunker ever returned [] for text with substance, healing on that
    verdict alone would silently delete real extracted text archive-wide, with
    no way back (the blob never re-extracts). The heal is gated on `is_blank`
    instead, so this case logs and stays claimable — a loud wedge beats a quiet
    one-way door."""
    import localmail.search.embed_worker as ew_mod
    from localmail.search.embed_worker import _chunk_attachments_lazily

    text = "text the chunker unexpectedly declines to chunk"
    sha = _seed_blob(db_conn, b"drifted chunker blob", text)
    cfg = SearchConfig()
    monkeypatch.setattr(ew_mod, "chunk_attachment_text", lambda *a, **k: [])

    with caplog.at_level(logging.WARNING, logger="localmail.search.embed_worker"):
        assert _chunk_attachments_lazily(db_conn, cfg, batch=50) == 1

    assert any(
        "chunked to nothing" in r.getMessage() for r in caplog.records
    ), caplog.text

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT extracted_text FROM attachment_text WHERE sha256 = %s", (sha,))
        row = cur.fetchone()
        assert row is not None
        assert row[0] == text  # untouched

    # Still claimable: the operator can fix the chunker and re-run.
    assert _chunk_attachments_lazily(db_conn, cfg, batch=50) == 1


def test_embed_worker_embeds_attachment_chunks(db_conn) -> None:
    """After chunking, the next pass embeds attachment_chunks where
    embedding_v1 IS NULL."""

    class _IndexedBackend:
        """Returns distinct non-zero vectors so we can assert embedding was set."""

        name = "indexed"
        model = "indexed"
        dimension = 768

        def embed_documents(self, texts: list[str]) -> list[list[float]]:
            """Return a unique vector per text position."""
            return [[0.1 * (i + 1)] * 768 for i in range(len(texts))]

        def embed_query(self, text: str) -> list[float]:
            """Return a zero query vector."""
            return [0.0] * 768

        def health_check(self) -> None:
            """No-op health check."""

    sha = _seed_blob(db_conn, b"another blob", "embedded text here")
    cfg = SearchConfig()

    # The contract is that after enough passes the embeddings get filled.
    # One pass typically both chunks and embeds (embed happens on same sweep);
    # three passes guarantees it regardless of interleaving order.
    for _ in range(3):
        run_embed_worker_once(db_conn, cfg, _IndexedBackend())

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM attachment_chunks"
            " WHERE sha256 = %s AND embedding_v1 IS NOT NULL",
            (sha,),
        )
        row = cur.fetchone()
        assert row is not None
        assert row[0] >= 1


def test_run_embed_worker_populates_body_lang_when_detector_provided(db_conn):
    """End-to-end: detector kwarg flows through and sets messages.body_lang."""
    from localmail.search.lang_detect import FixedDetector

    body = "anything"
    mid = _seed_message(db_conn, body=body)
    cfg = SearchConfig()
    detector = FixedDetector({body: "de"})

    run_embed_worker_once(db_conn, cfg, _StaticEmbedder(), lang_detector=detector)

    with db_conn.cursor() as cur:
        cur.execute("SELECT body_lang FROM messages WHERE id = %s", (mid,))
        row = cur.fetchone()
    assert row is not None
    assert row[0] == "de"


def test_run_embed_worker_leaves_body_lang_null_when_no_detector(db_conn):
    """No detector → body_lang is never written (existing callers unaffected)."""
    mid = _seed_message(db_conn, body="anything")
    cfg = SearchConfig()

    run_embed_worker_once(db_conn, cfg, _StaticEmbedder())

    with db_conn.cursor() as cur:
        cur.execute("SELECT body_lang FROM messages WHERE id = %s", (mid,))
        row = cur.fetchone()
    assert row is not None
    assert row[0] is None


def test_run_embed_worker_skips_lang_detect_when_disabled_in_cfg(db_conn):
    """`body_lang_enabled=False` short-circuits the pass even if a detector is given."""
    from localmail.search.lang_detect import FixedDetector

    mid = _seed_message(db_conn, body="anything")
    cfg = SearchConfig(body_lang_enabled=False)
    detector = FixedDetector({"anything": "de"})

    run_embed_worker_once(db_conn, cfg, _StaticEmbedder(), lang_detector=detector)

    with db_conn.cursor() as cur:
        cur.execute("SELECT body_lang FROM messages WHERE id = %s", (mid,))
        row = cur.fetchone()
    assert row is not None
    assert row[0] is None


# --- #259: the loop's backoff must see language-detection progress -----------


class _FakeStop:
    """Stop event that records each wait() timeout and halts after N waits."""

    def __init__(self, stop_after: int) -> None:
        self.timeouts: list[float] = []
        self._stop_after = stop_after
        self._set = False

    def is_set(self) -> bool:
        return self._set

    def wait(self, timeout=None) -> bool:
        self.timeouts.append(timeout)
        if len(self.timeouts) >= self._stop_after:
            self._set = True
        return self._set


class _FakePool:
    """Minimal stand-in: `with pool.connection() as conn` yields a dummy."""

    @contextlib.contextmanager
    def connection(self):
        yield object()


def _loop_sleeps(monkeypatch, outcome, *, sweeps, cfg) -> list[float]:
    """Run `run_embed_worker` for `sweeps` iterations; return the sleeps taken."""
    from localmail.search import embed_worker as ew

    monkeypatch.setattr(ew, "safe_heartbeat", lambda *a, **k: None)
    monkeypatch.setattr(ew, "run_embed_worker_once", lambda *a, **k: outcome)
    stop = _FakeStop(stop_after=sweeps)
    ew.run_embed_worker(stop, _FakePool(), cfg, _StaticEmbedder())
    return stop.timeouts


def test_worker_does_not_back_off_while_language_detection_progresses(monkeypatch):
    """#259: a sweep that labelled 200 rows embedded nothing but did work."""
    cfg = SearchConfig(embed_worker_poll_interval_s=5.0)
    sleeps = _loop_sleeps(
        monkeypatch, SweepOutcome(embedded=0, lang_visited=200), sweeps=3, cfg=cfg,
    )
    assert sleeps == [5.0, 5.0, 5.0]


def test_worker_backs_off_when_a_sweep_did_nothing_at_all(monkeypatch):
    cfg = SearchConfig(embed_worker_poll_interval_s=5.0)
    sleeps = _loop_sleeps(
        monkeypatch, SweepOutcome(embedded=0, lang_visited=0), sweeps=3, cfg=cfg,
    )
    assert sleeps == [10.0, 15.0, 20.0]


def test_worker_backoff_saturates_at_the_configured_ceiling(monkeypatch):
    cfg = SearchConfig(
        embed_worker_poll_interval_s=5.0, embed_worker_idle_backoff_max_steps=2,
    )
    sleeps = _loop_sleeps(
        monkeypatch, SweepOutcome(embedded=0, lang_visited=0), sweeps=4, cfg=cfg,
    )
    assert sleeps == [10.0, 15.0, 15.0, 15.0]


def test_a_sweep_that_only_detected_language_reports_it(db_conn):
    """#259 end-to-end: the sweep result carries the lang rows it visited.

    Sweep 1 runs without a detector, so it drains the embedding queue and
    leaves body_lang NULL. Sweep 2 therefore embeds nothing while the
    language queue still has work — the exact shape that used to read as an
    empty sweep and trigger the backoff.
    """
    from localmail.search.lang_detect import FixedDetector

    body = "The Berlin conference is next week."
    _seed_message(db_conn, body=body)
    cfg = SearchConfig()

    first = run_embed_worker_once(db_conn, cfg, _StaticEmbedder())
    assert first.embedded >= 1
    assert first.lang_visited == 0

    second = run_embed_worker_once(
        db_conn, cfg, _StaticEmbedder(), lang_detector=FixedDetector({body: "de"}),
    )
    assert second.embedded == 0
    assert second.lang_visited == 1
    assert second.made_progress is True


# --- #267: a persistently broken backend must not log a traceback per sweep --


class _Clock:
    """Monotonic stand-in the test advances explicitly."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class _ScriptedBackend:
    """Backend following a script: each step is an exception to raise, or None
    to succeed.

    Running past the end raises rather than repeating the last step, because
    `_embed_table`'s broad `except Exception` would swallow either into a
    "batch failed" record — a script overrun would otherwise manufacture the
    very log lines these tests count. Every test asserts `calls` for the same
    reason: the raise alone is invisible from inside that handler.
    """

    name = "scripted"
    model = "scripted-768"
    dimension = 768

    def __init__(self, script: list[BaseException | None]) -> None:
        self._script = list(script)
        self.calls = 0

    def embed_documents(self, texts):
        if self.calls >= len(self._script):
            raise AssertionError(
                f"_ScriptedBackend ran past its script: call {self.calls + 1}"
                f" of {len(self._script)} steps"
            )
        step = self._script[self.calls]
        self.calls += 1
        if step is not None:
            raise step
        return [[1.0] * 768 for _ in texts]

    def embed_query(self, text):
        return [0.5] * 768

    def health_check(self):
        pass


def _batch_failure_records(caplog) -> list:
    return [r for r in caplog.records if "embed_worker batch failed" in r.getMessage()]


def _seed_second_message(conn, body: str) -> int:
    """Another message on the account `_seed_message` created."""
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM accounts WHERE name = 'a'")
        row = cur.fetchone()
        assert row is not None
        cur.execute(
            "INSERT INTO messages (account_id, message_id, raw_sha256, subject,"
            " from_addr, body_text, headers, raw_bytes, size_bytes)"
            " VALUES (%s, %s, %s, %s, %s, %s, '{}'::jsonb, %s, %s) RETURNING id",
            (row[0], "<b@x>", b"\x02" * 32, "Hi again", "x@y", body, b"raw", 3),
        )
        mid_row = cur.fetchone()
        assert mid_row is not None
    conn.commit()
    return mid_row[0]


def test_a_broken_backend_reports_once_and_then_stays_quiet(db_conn, caplog) -> None:
    """The defect: while a language backlog drains, a broken backend is retried
    once per base poll interval, so an unthrottled report is ~24 tracebacks a
    minute for as long as the backlog lasts."""
    _seed_message(db_conn, body="Text that will be chunked but never embedded.")
    cfg = SearchConfig()
    backend = _ScriptedBackend([ConnectionError("backend down")] * 4)
    failure_log: dict = {}
    clock = _Clock()

    with caplog.at_level("WARNING", logger="localmail.search.embed_worker"):
        for _ in range(4):
            clock.now += 5.0
            run_embed_worker_once(
                db_conn, cfg, backend, failure_log=failure_log, clock=clock,
            )

    assert backend.calls == 4
    records = _batch_failure_records(caplog)
    assert len(records) == 1
    assert records[0].exc_info  # and it is the diagnosable one
    assert records[0].levelname == "WARNING"


def test_the_report_resumes_once_the_interval_elapses(db_conn, caplog) -> None:
    """The traceback re-arms, so a week-old incident is still diagnosable from
    a rotated log without restarting the daemon — and the resumed line accounts
    for what was swallowed in between."""
    _seed_message(db_conn, body="Text that will be chunked but never embedded.")
    cfg = SearchConfig(embed_worker_failure_report_interval_s=60.0)
    backend = _ScriptedBackend([ConnectionError("backend down")] * 3)
    failure_log: dict = {}
    clock = _Clock()

    with caplog.at_level("WARNING", logger="localmail.search.embed_worker"):
        for tick in (0.0, 5.0, 60.0):
            clock.now = tick
            run_embed_worker_once(
                db_conn, cfg, backend, failure_log=failure_log, clock=clock,
            )

    assert backend.calls == 3
    records = _batch_failure_records(caplog)
    assert len(records) == 2
    assert records[1].exc_info
    assert "1 further failures" in records[1].getMessage()


def test_a_different_failure_mode_reports_immediately(db_conn, caplog) -> None:
    """A second failure mode arriving mid-incident must not be swallowed as a
    continuation of the first — the one traceback on record would then name a
    problem that is no longer the one happening."""
    _seed_message(db_conn, body="Text that will be chunked but never embedded.")
    cfg = SearchConfig()
    backend = _ScriptedBackend(
        [ConnectionError("db blip"), ConnectionError("db blip"), ImportError("no easyocr")]
    )
    failure_log: dict = {}
    clock = _Clock()

    with caplog.at_level("WARNING", logger="localmail.search.embed_worker"):
        for _ in range(3):
            clock.now += 5.0
            run_embed_worker_once(
                db_conn, cfg, backend, failure_log=failure_log, clock=clock,
            )

    assert backend.calls == 3
    records = _batch_failure_records(caplog)
    assert len(records) == 2
    assert "ConnectionError" in records[0].getMessage()
    assert "ImportError" in records[1].getMessage()


def test_a_flapping_backend_is_reported_only_once(db_conn, caplog) -> None:
    """A backend alternating success and failure — the "network blip" the
    batch-level handler exists for — would defeat a reset-on-success rule
    entirely: every failure would be the first of a fresh streak."""
    _seed_message(db_conn, body="Text that will be chunked but never embedded.")
    cfg = SearchConfig()
    down = ConnectionError("backend down")
    backend = _ScriptedBackend([down, None, down])
    failure_log: dict = {}
    clock = _Clock()

    with caplog.at_level("WARNING", logger="localmail.search.embed_worker"):
        run_embed_worker_once(db_conn, cfg, backend, failure_log=failure_log, clock=clock)
        clock.now += 5.0
        run_embed_worker_once(db_conn, cfg, backend, failure_log=failure_log, clock=clock)
        _seed_second_message(db_conn, body="More text for the recovered backend.")
        clock.now += 5.0
        run_embed_worker_once(db_conn, cfg, backend, failure_log=failure_log, clock=clock)

    assert backend.calls == 3
    assert len(_batch_failure_records(caplog)) == 1


def test_the_report_names_the_exception_class_even_with_no_message(
    db_conn, caplog
) -> None:
    """`str(exc)` is empty for ConnectionError(), MemoryError() and much of
    what a backend raises, and this line is what a log grep shows."""
    _seed_message(db_conn, body="Text that will be chunked but never embedded.")
    cfg = SearchConfig()
    backend = _ScriptedBackend([ConnectionError()])

    with caplog.at_level("WARNING", logger="localmail.search.embed_worker"):
        run_embed_worker_once(db_conn, cfg, backend, failure_log={}, clock=_Clock())

    assert backend.calls == 1
    assert "ConnectionError" in _batch_failure_records(caplog)[0].getMessage()


def test_the_two_chunk_tables_are_throttled_independently(db_conn, caplog) -> None:
    """Keyed on one shared bucket, a failure on either table would suppress the
    other's *first* report, and each table's incident would mask the other's."""
    _seed_message(db_conn, body="Text that will be chunked but never embedded.")
    _seed_blob(db_conn, b"attachment payload", "extracted attachment body text")
    cfg = SearchConfig()
    backend = _ScriptedBackend([ConnectionError("backend down")] * 2)

    with caplog.at_level("WARNING", logger="localmail.search.embed_worker"):
        run_embed_worker_once(db_conn, cfg, backend, failure_log={}, clock=_Clock())

    assert backend.calls == 2
    tables = [
        table
        for table in ("message_chunks", "attachment_chunks")
        if any(table in r.getMessage() for r in _batch_failure_records(caplog))
    ]
    assert tables == ["message_chunks", "attachment_chunks"]


def test_a_successful_or_empty_sweep_leaves_the_failure_log_alone(db_conn) -> None:
    """The log records what has been *said*, not what the backend is doing, so
    only the failure branch touches it. Clearing it on success is the flapping
    hole above; clearing it on an empty claim would restore the per-sweep
    report on any alternating claim/empty pattern."""
    _seed_message(db_conn, body="Text that will be chunked but never embedded.")
    cfg = SearchConfig()
    backend = _ScriptedBackend([ConnectionError("backend down"), None])
    failure_log: dict = {}
    clock = _Clock()

    run_embed_worker_once(db_conn, cfg, backend, failure_log=failure_log, clock=clock)
    after_failure = dict(failure_log)
    assert set(after_failure) == {"message_chunks"}

    clock.now += 5.0  # sweep 2 embeds the same chunks with the recovered backend
    assert run_embed_worker_once(
        db_conn, cfg, backend, failure_log=failure_log, clock=clock,
    ).embedded >= 1
    assert failure_log == after_failure

    clock.now += 5.0  # sweep 3 has nothing left to claim
    assert run_embed_worker_once(
        db_conn, cfg, backend, failure_log=failure_log, clock=clock,
    ).embedded == 0
    assert backend.calls == 2
    assert failure_log == after_failure


def test_sweeps_share_the_process_failure_log_by_default(db_conn, caplog) -> None:
    """No caller has to know the parameter exists: the four looping callers in
    the repo (the daemon loop, `embed-backfill`, three acceptance harnesses)
    pass nothing and are throttled anyway."""
    _seed_message(db_conn, body="Text that will be chunked but never embedded.")
    cfg = SearchConfig()
    backend = _ScriptedBackend([ConnectionError("backend down")] * 2)

    with caplog.at_level("WARNING", logger="localmail.search.embed_worker"):
        run_embed_worker_once(db_conn, cfg, backend)
        run_embed_worker_once(db_conn, cfg, backend)

    assert backend.calls == 2
    assert len(_batch_failure_records(caplog)) == 1
