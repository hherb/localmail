# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Tests for `search.extract_queue` — the one authority on what the extract
worker will still claim, and on how the eligible blob population divides.

The defect these pin is #277: `search-status` derived `blobs_pending` as
`eligible - extracted`, so every blob the worker had *already* disposed of by
writing an empty-text sentinel row (`type-skipped`, `lightweight-empty`,
`size-skipped`, a #266-healed row) counted as outstanding work forever. Same
drift #251 found on the language half of the same command.
"""

from __future__ import annotations

import hashlib
import re

import pytest

from localmail.config import SearchConfig
from localmail.search import extract_queue
from localmail.search.extract_worker import _claim_batch

# Distinct from the production defaults so a test that silently fell back to
# them would fail rather than pass by coincidence.
MAX_RETRIES = 3
MAX_TRANSIENT_RETRIES = 5

ALLOWLISTED_MIME = "text/plain"
NOT_ALLOWLISTED_MIME = "image/png"


def _cfg(**overrides) -> SearchConfig:
    return SearchConfig(
        extract_worker_max_retries=MAX_RETRIES,
        extract_worker_max_transient_retries=MAX_TRANSIENT_RETRIES,
        **overrides,
    )


# --------------------------------------------------------------------------
# Pure: the SQL fragments and the parameter helpers cannot drift apart
# --------------------------------------------------------------------------


def test_every_placeholder_in_the_sql_has_a_parameter_helper_key() -> None:
    """A renamed placeholder must break here, not at runtime.

    The fragments are strings, so a rename in one and not the other is
    invisible until psycopg raises `ProgrammingError` on a real archive.
    """
    referenced = set(re.findall(r"%\((\w+)\)s", extract_queue.QUEUE_COUNTS_SQL))
    supplied = set(
        extract_queue.cap_params(max_retries=1, max_transient_retries=2)
    ) | set(
        extract_queue.allowlist_params(
            mime_allowlist=["text/plain"], extension_allowlist=[".txt"]
        )
    )
    assert referenced == supplied


def test_claim_fragments_reference_only_the_cap_parameters() -> None:
    """The claim has no allowlist half — it is applied in Python (#216)."""
    referenced = set(
        re.findall(
            r"%\((\w+)\)s",
            extract_queue.QUEUE_FROM_SQL + extract_queue.CLAIMABLE_WHERE_SQL,
        )
    )
    assert referenced == set(
        extract_queue.cap_params(max_retries=1, max_transient_retries=2)
    )


def test_queue_counts_rejects_buckets_that_do_not_account_for_every_blob() -> None:
    """The four buckets partition the eligible set; a gap is a predicate bug.

    Reported rather than silently absorbed, because the number an operator
    reads is the whole point of the command.
    """
    with pytest.raises(ValueError, match="do not sum"):
        extract_queue.QueueCounts(
            eligible=10, extracted=1, no_text=1, gave_up=1, pending=1
        )


def test_queue_counts_accepts_a_partition() -> None:
    counts = extract_queue.QueueCounts(
        eligible=10, extracted=4, no_text=3, gave_up=2, pending=1
    )
    assert counts.eligible == 10


# --------------------------------------------------------------------------
# DB: the buckets over a seeded archive
# --------------------------------------------------------------------------


def _blob(conn, label: str, mime: str = ALLOWLISTED_MIME) -> bytes:
    """Insert one attachment_blobs row; return its sha256 digest."""
    sha = hashlib.sha256(label.encode()).digest()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO attachment_blobs (sha256, path, mime_type, size_bytes) "
            "VALUES (%s, %s, %s, %s)",
            (sha, f"/blobs/{label}", mime, 10),
        )
    return sha


def _text(conn, sha: bytes, extractor: str, text: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO attachment_text (sha256, extractor, extracted_text) "
            "VALUES (%s, %s, %s)",
            (sha, extractor, text),
        )


def _failed(conn, sha: bytes, retry_count: int) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO failed_extractions "
            "(sha256, extractor, error_class, error_message, retry_count) "
            "VALUES (%s, 'lightweight', 'Boom', 'broken', %s)",
            (sha, retry_count),
        )


def _transient(conn, sha: bytes, transient_count: int) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO transient_extractions (sha256, transient_count) "
            "VALUES (%s, %s)",
            (sha, transient_count),
        )


def _seed_every_state(conn) -> dict[str, bytes]:
    """One blob per disposition the extract queue can put a blob in."""
    shas = {
        "fresh": _blob(conn, "fresh"),
        "extracted": _blob(conn, "extracted"),
        "type_skipped": _blob(conn, "type_skipped"),
        "lightweight_empty": _blob(conn, "lightweight_empty"),
        "size_skipped": _blob(conn, "size_skipped"),
        "healed": _blob(conn, "healed"),
        "failed_at_cap": _blob(conn, "failed_at_cap"),
        "failed_under_cap": _blob(conn, "failed_under_cap"),
        "transient_at_cap": _blob(conn, "transient_at_cap"),
        "not_allowlisted": _blob(conn, "not_allowlisted", NOT_ALLOWLISTED_MIME),
    }
    _text(conn, shas["extracted"], "lightweight@1.0", "real text")
    _text(conn, shas["type_skipped"], "type-skipped", "")
    _text(conn, shas["lightweight_empty"], "lightweight-empty", "")
    _text(conn, shas["size_skipped"], "size-skipped", "")
    _text(conn, shas["healed"], "lightweight@1.0", "")
    _failed(conn, shas["failed_at_cap"], MAX_RETRIES)
    _failed(conn, shas["failed_under_cap"], MAX_RETRIES - 1)
    _transient(conn, shas["transient_at_cap"], MAX_TRANSIENT_RETRIES)
    conn.commit()
    return shas


def _counts(conn, cfg: SearchConfig) -> extract_queue.QueueCounts:
    return extract_queue.fetch_queue_counts(conn, cfg)


def test_sentinel_rows_are_not_counted_as_pending(db_conn) -> None:
    """The #277 defect: four sentinel flavours, none of them outstanding work."""
    _seed_every_state(db_conn)
    counts = _counts(db_conn, _cfg())
    assert counts.no_text == 4
    assert counts.pending == 2  # fresh + failed-under-cap


def test_a_blob_at_a_retry_cap_is_not_counted_as_pending(db_conn) -> None:
    """The worker's claim excludes it, so reporting it as pending is the same
    defect wearing a different row — it would never drain either."""
    _seed_every_state(db_conn)
    counts = _counts(db_conn, _cfg())
    assert counts.gave_up == 2  # poison-pill cap + transient cap


def test_non_allowlisted_blobs_are_outside_every_bucket(db_conn) -> None:
    _seed_every_state(db_conn)
    counts = _counts(db_conn, _cfg())
    assert counts.eligible == 9


def test_pending_reaches_zero_once_every_eligible_blob_is_disposed_of(
    db_conn,
) -> None:
    """The issue's headline acceptance criterion."""
    shas = _seed_every_state(db_conn)
    _text(db_conn, shas["fresh"], "lightweight@1.0", "text")
    _text(db_conn, shas["failed_under_cap"], "lightweight-empty", "")
    db_conn.commit()
    assert _counts(db_conn, _cfg()).pending == 0


def test_the_four_buckets_partition_the_eligible_population(db_conn) -> None:
    """Jointly exhaustive and disjoint — the property #251 pinned for the
    language half, here for the attachment half."""
    _seed_every_state(db_conn)
    counts = _counts(db_conn, _cfg())
    assert (
        counts.extracted + counts.no_text + counts.gave_up + counts.pending
        == counts.eligible
    )


def test_pending_matches_exactly_what_the_worker_would_claim(db_conn) -> None:
    """The anti-drift guarantee: the number reported is the number of blobs
    `_claim_batch` would hand the worker, not an estimate beside it."""
    _seed_every_state(db_conn)
    cfg = _cfg(extract_worker_batch_size=100)
    claimed = {row[0] for row in _claim_batch(db_conn, cfg)}
    db_conn.rollback()

    # `_claim_batch` has no allowlist half, so the non-allowlisted blob is
    # claimable to the worker (it disposes of it with a `type-skipped` row).
    counts = _counts(db_conn, cfg)
    assert len(claimed) == counts.pending + 1
