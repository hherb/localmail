# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Tests for `search.extract_queue` — the one authority on what the extract
worker will still claim, and on how the eligible blob population divides.

The defect these pin is #277: `search-status` derived `blobs_pending` as
`eligible - extracted`, so every blob the worker had *already* disposed of by
writing an empty-text sentinel row (`type-skipped`, `lightweight-empty`,
`size-skipped`, a #266-healed row) counted as outstanding work forever. Same
drift #251 found on the language half of the same command.

The review of that fix found the mirror image and it is pinned here too: the
partition counts only *allowlisted* blobs while the claim ignores the
allowlist, so `pending` alone under-reports the worker's queue (`claimable` is
the honest depth), and the SQL allowlist had drifted from the Python one it
claims to mirror on case-folding and on dotfiles.
"""

from __future__ import annotations

import hashlib
import json
import re

import pytest

from localmail.config import SearchConfig
from localmail.search import attachment_kind, extract_queue
from localmail.search.extract_worker import _claim_batch

# Distinct from the production defaults (3 and 5) so a test that silently fell
# back to them would fail rather than pass by coincidence.
MAX_RETRIES = 2
MAX_TRANSIENT_RETRIES = 7

# Narrower than the production allowlists, for the same reason.
ALLOWLISTED_MIME = "text/plain"
NOT_ALLOWLISTED_MIME = "image/png"
ALLOWLISTED_EXTENSION = ".txt"


def _cfg(**overrides) -> SearchConfig:
    return SearchConfig(
        **{
            "extract_worker_max_retries": MAX_RETRIES,
            "extract_worker_max_transient_retries": MAX_TRANSIENT_RETRIES,
            "extractor_mime_allowlist": [ALLOWLISTED_MIME],
            "extractor_extension_allowlist": [ALLOWLISTED_EXTENSION],
            **overrides,
        }
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


def test_the_claimable_total_query_has_no_allowlist_half() -> None:
    """`claimable` is the worker's true queue depth, so it must not inherit the
    report's allowlist scoping — that gap is the whole reason it exists."""
    referenced = set(
        re.findall(r"%\((\w+)\)s", extract_queue.CLAIMABLE_TOTAL_SQL)
    )
    assert referenced == set(
        extract_queue.cap_params(max_retries=1, max_transient_retries=2)
    )
    assert "jsonb_array_elements" not in extract_queue.CLAIMABLE_TOTAL_SQL


def test_allowlist_params_lowercases_both_allowlists() -> None:
    """`attachment_kind.is_allowlisted` lowers the configured values as well as
    the stored ones; the SQL assumes this half has already been done."""
    params = extract_queue.allowlist_params(
        mime_allowlist=["Application/PDF"], extension_allowlist=[".PDF"]
    )
    assert params == {
        "mime_allowlist": ["application/pdf"],
        "extension_allowlist": [".pdf"],
    }


def test_queue_counts_rejects_buckets_that_do_not_account_for_every_blob() -> None:
    """The four buckets partition the eligible set; a gap is a predicate bug.

    Reported rather than silently absorbed, because the number an operator
    reads is the whole point of the command.
    """
    with pytest.raises(extract_queue.QueueCountsInconsistent, match="do not sum"):
        extract_queue.QueueCounts(
            eligible=10, extracted=1, no_text=1, gave_up=1, pending=1
        )


def test_queue_counts_accepts_a_partition() -> None:
    counts = extract_queue.QueueCounts(
        eligible=10, extracted=4, no_text=3, gave_up=2, pending=1
    )
    assert counts.eligible == 10


def test_claimable_is_outside_the_partition_check() -> None:
    """It comes from a second statement, so a worker committing between the two
    can briefly put it below `pending`. Crashing over that race would be worse
    than reporting it."""
    counts = extract_queue.QueueCounts(
        eligible=1, extracted=0, no_text=0, gave_up=0, pending=1, claimable=0
    )
    assert counts.claimable == 0


def test_queue_counts_has_no_truth_value() -> None:
    """`if counts:` is the implicit read that caused #251 and #259."""
    counts = extract_queue.QueueCounts(
        eligible=0, extracted=0, no_text=0, gave_up=0, pending=0
    )
    with pytest.raises(TypeError, match="no truth value"):
        bool(counts)


def test_status_field_names_covers_every_field() -> None:
    """The CLI projects these onto its payload, so a bucket added to the type
    must not be able to go missing from the command that reports it."""
    counts = extract_queue.QueueCounts(
        eligible=3, extracted=1, no_text=1, gave_up=1, pending=0, claimable=9
    )
    assert set(counts.status_fields()) == set(
        extract_queue.QueueCounts.status_field_names()
    )
    assert counts.status_fields()["blobs_claimable"] == 9
    assert counts.status_fields()["blobs_eligible"] == 3


# --------------------------------------------------------------------------
# DB helpers
# --------------------------------------------------------------------------


def _blob(
    conn, label: str, mime: str = ALLOWLISTED_MIME, filename: str | None = None
) -> bytes:
    """Insert one `attachment_blobs` row; return its sha256 digest.

    With `filename`, also insert the message that references it under that
    name — `messages.attachments` is the only place an attachment's original
    filename is recorded, and therefore the only thing the extension half of
    the allowlist can read (#216).
    """
    sha = hashlib.sha256(label.encode()).digest()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO attachment_blobs (sha256, path, mime_type, size_bytes) "
            "VALUES (%s, %s, %s, %s)",
            (sha, f"/blobs/{label}", mime, 10),
        )
        if filename is not None:
            cur.execute(
                "INSERT INTO accounts (name, email_address, imap_host, auth_method)"
                " VALUES ('acct', 'a@x', 'h', 'password')"
                " ON CONFLICT (name) DO UPDATE SET email_address = EXCLUDED.email_address"
                " RETURNING id"
            )
            row = cur.fetchone()
            assert row is not None
            cur.execute(
                "INSERT INTO messages (account_id, message_id, raw_sha256, subject,"
                " headers, raw_bytes, size_bytes, attachments)"
                " VALUES (%s, %s, %s, 's', '{}'::jsonb, %s, %s, %s::jsonb)",
                (
                    row[0],
                    f"<{sha.hex()}>",
                    sha,
                    b"raw",
                    1,
                    json.dumps([{"filename": filename, "sha256": sha.hex()}]),
                ),
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


# Every disposition the queue can put a blob in, and the bucket it belongs to.
# `None` means "outside the eligible population entirely". Bucket sizes are
# deliberately pairwise distinct (2/4/6/5) so swapping any two `FILTER`
# expressions in `QUEUE_COUNTS_SQL` changes an assertion.
EXPECTED_BUCKET = {
    "extracted": "extracted",
    "extracted_2": "extracted",
    "type_skipped": "no_text",
    "lightweight_empty": "no_text",
    "size_skipped": "no_text",
    "healed": "no_text",
    "failed_at_cap": "gave_up",
    "failed_over_cap": "gave_up",
    "transient_at_cap": "gave_up",
    "transient_over_cap": "gave_up",
    "failed_under_transient_at": "gave_up",
    "transient_under_failed_at": "gave_up",
    "fresh": "pending",
    "fresh_2": "pending",
    "failed_under_cap": "pending",
    "transient_under_cap": "pending",
    "both_under_caps": "pending",
    "not_allowlisted": None,
}


def _labels_in(bucket: str | None) -> set[str]:
    return {lbl for lbl, b in EXPECTED_BUCKET.items() if b == bucket}


def _seed_every_state(conn) -> dict[str, bytes]:
    """One blob per disposition named in `EXPECTED_BUCKET`."""
    shas = {
        lbl: _blob(
            conn,
            lbl,
            NOT_ALLOWLISTED_MIME if lbl == "not_allowlisted" else ALLOWLISTED_MIME,
        )
        for lbl in EXPECTED_BUCKET
    }
    _text(conn, shas["extracted"], "lightweight@1.0", "real text")
    _text(conn, shas["extracted_2"], "docling@2.0", "more text")
    _text(conn, shas["type_skipped"], "type-skipped", "")
    _text(conn, shas["lightweight_empty"], "lightweight-empty", "")
    _text(conn, shas["size_skipped"], "size-skipped", "")
    _text(conn, shas["healed"], "lightweight@1.0", "")

    _failed(conn, shas["failed_at_cap"], MAX_RETRIES)
    _failed(conn, shas["failed_over_cap"], MAX_RETRIES + 1)
    _transient(conn, shas["transient_at_cap"], MAX_TRANSIENT_RETRIES)
    _transient(conn, shas["transient_over_cap"], MAX_TRANSIENT_RETRIES + 1)
    # Only one counter needs to be exhausted: the caps are AND-ed, so a blob
    # comfortably under one budget is still parked by the other.
    _failed(conn, shas["failed_under_transient_at"], MAX_RETRIES - 1)
    _transient(conn, shas["failed_under_transient_at"], MAX_TRANSIENT_RETRIES)
    _failed(conn, shas["transient_under_failed_at"], MAX_RETRIES)
    _transient(conn, shas["transient_under_failed_at"], MAX_TRANSIENT_RETRIES - 1)

    _failed(conn, shas["failed_under_cap"], MAX_RETRIES - 1)
    _transient(conn, shas["transient_under_cap"], MAX_TRANSIENT_RETRIES - 1)
    _failed(conn, shas["both_under_caps"], MAX_RETRIES - 1)
    _transient(conn, shas["both_under_caps"], MAX_TRANSIENT_RETRIES - 1)
    conn.commit()
    return shas


def _counts(conn, cfg: SearchConfig) -> extract_queue.QueueCounts:
    return extract_queue.fetch_queue_counts(conn, cfg)


# --------------------------------------------------------------------------
# DB: the buckets over a seeded archive
# --------------------------------------------------------------------------


def test_sentinel_rows_are_not_counted_as_pending(db_conn) -> None:
    """The #277 defect: four sentinel flavours, none of them outstanding work."""
    _seed_every_state(db_conn)
    counts = _counts(db_conn, _cfg())
    assert counts.no_text == len(_labels_in("no_text"))
    assert counts.pending == len(_labels_in("pending"))


def test_a_blob_at_a_retry_cap_is_not_counted_as_pending(db_conn) -> None:
    """The worker's claim excludes it, so reporting it as pending is the same
    defect wearing a different row — it would never drain either.

    Covers both counters at and past their cap, and the two mixed cases that
    pin the budgets as AND-ed rather than OR-ed.
    """
    _seed_every_state(db_conn)
    counts = _counts(db_conn, _cfg())
    assert counts.gave_up == len(_labels_in("gave_up"))


def test_a_blob_under_both_retry_caps_is_still_pending(db_conn) -> None:
    """The bound is `<`, so a blob one short of either cap is still claimable —
    collapsing the transient budget would silently park it forever."""
    _seed_every_state(db_conn)
    counts = _counts(db_conn, _cfg())
    assert counts.extracted == len(_labels_in("extracted"))
    assert counts.pending == len(_labels_in("pending")) == 5


def test_non_allowlisted_blobs_are_outside_every_bucket(db_conn) -> None:
    _seed_every_state(db_conn)
    counts = _counts(db_conn, _cfg())
    assert counts.eligible == len(EXPECTED_BUCKET) - len(_labels_in(None))


def test_pending_reaches_zero_once_every_eligible_blob_is_disposed_of(
    db_conn,
) -> None:
    """The issue's headline acceptance criterion."""
    shas = _seed_every_state(db_conn)
    for label in _labels_in("pending"):
        _text(db_conn, shas[label], "lightweight-empty", "")
    db_conn.commit()
    assert _counts(db_conn, _cfg()).pending == 0


def test_the_four_buckets_partition_the_eligible_population(db_conn) -> None:
    """Jointly exhaustive; disjointness is structural — the four predicates
    pivot on `t.sha256 IS NULL` and then on a `NOT NULL` column each."""
    _seed_every_state(db_conn)
    counts = _counts(db_conn, _cfg())
    assert (
        counts.extracted + counts.no_text + counts.gave_up + counts.pending
        == counts.eligible
    )


def test_pending_is_the_allowlisted_half_of_what_the_worker_would_claim(
    db_conn,
) -> None:
    """`_claim_batch` has no allowlist half (#216), so it also hands the worker
    blobs no bucket counts — which it disposes of with a `type-skipped` row.

    `claimable` is therefore the honest queue depth and `pending` its
    allowlisted subset. Asserted as sets, not lengths: two compensating errors
    (the report calling blob X pending while the worker claims blob Y) would
    survive a cardinality check.
    """
    shas = _seed_every_state(db_conn)
    cfg = _cfg(extract_worker_batch_size=100)
    claimed = {row[0] for row in _claim_batch(db_conn, cfg)}
    db_conn.rollback()

    expected_claimed = {shas[lbl] for lbl in _labels_in("pending") | _labels_in(None)}
    assert claimed == expected_claimed

    counts = _counts(db_conn, cfg)
    assert counts.claimable == len(claimed)
    assert counts.pending == len(claimed) - len(_labels_in(None))


def test_claimable_counts_work_that_no_bucket_reports(db_conn) -> None:
    """The #216 archive in miniature: an operator watching `blobs_pending`
    alone would read an empty queue while the worker still has claims to burn.
    """
    _blob(db_conn, "image_1", NOT_ALLOWLISTED_MIME)
    _blob(db_conn, "image_2", NOT_ALLOWLISTED_MIME)
    db_conn.commit()

    counts = _counts(db_conn, _cfg())
    assert counts.eligible == 0
    assert counts.pending == 0
    assert counts.claimable == 2


# --------------------------------------------------------------------------
# DB: the SQL allowlist mirrors the Python one
# --------------------------------------------------------------------------


def test_a_blob_allowlisted_only_by_its_original_filename_is_eligible(
    db_conn,
) -> None:
    """#216's other half. A real `.txt` arriving as `application/octet-stream`
    from a mobile client is admitted by the extension, which lives only in
    `messages.attachments` — the blob path is content-addressable and carries
    no extension at all.
    """
    _blob(db_conn, "mistyped", "application/octet-stream", filename="notes.txt")
    db_conn.commit()
    counts = _counts(db_conn, _cfg())
    assert counts.eligible == 1
    assert counts.pending == 1


def test_a_blob_neither_allowlist_admits_is_not_eligible(db_conn) -> None:
    _blob(db_conn, "photo", NOT_ALLOWLISTED_MIME, filename="photo.png")
    db_conn.commit()
    assert _counts(db_conn, _cfg()).eligible == 0


# Cases chosen to cover both halves of the allowlist, the case-folding of each
# operand, and the extension edges where `Path.suffix` and a naive regex part
# company. `is_allowlisted` is the authority; the SQL must agree.
_MIRROR_CASES = [
    ("text/plain", None, True),
    ("image/png", None, False),
    ("application/octet-stream", "notes.txt", True),
    ("application/octet-stream", "notes.png", False),
    ("TEXT/PLAIN", None, True),
    ("application/octet-stream", "NOTES.TXT", True),
    ("application/octet-stream", ".txt", False),
    ("application/octet-stream", "..txt", True),
    ("application/octet-stream", "archive.tar.gz", False),
    ("application/octet-stream", "archive.tar.txt", True),
    ("application/octet-stream", "no_dot", False),
    ("application/octet-stream", "report.", False),
]


@pytest.mark.parametrize("mime,filename,expected", _MIRROR_CASES)
def test_the_sql_allowlist_agrees_with_the_python_one(
    db_conn, mime, filename, expected
) -> None:
    """`ALLOWLISTED_WHERE_SQL` calls itself a mirror of
    `attachment_kind.is_allowlisted`; nothing checked that until #277's review
    found it had drifted on case-folding (SQL compared MIME case-sensitively
    and never lowered the configured values) and on dotfiles (`Path('.txt')`
    has no suffix, but `'\\.[^.]+$'` matched the whole name).

    The case-folding half mattered most: it made SQL *under*-count, so a
    mixed-case `config.toml` entry dropped a class of blob out of every bucket
    while the partition still summed — #277's failure mode exactly.
    """
    cfg = _cfg()
    _blob(db_conn, f"{mime}|{filename}", mime, filename=filename)
    db_conn.commit()

    assert (_counts(db_conn, cfg).eligible == 1) is expected
    assert (
        attachment_kind.is_allowlisted(
            mime,
            [filename] if filename else [],
            mime_allowlist=cfg.extractor_mime_allowlist,
            extension_allowlist=cfg.extractor_extension_allowlist,
        )
        is expected
    )


@pytest.mark.parametrize("mime,filename", [("Text/Plain", None), ("x/y", "a.TXT")])
def test_a_mixed_case_config_allowlist_admits_the_same_blobs(
    db_conn, mime, filename
) -> None:
    """The operator's `config.toml` is the other operand, and it was never
    lowered on the SQL side — so `"Text/Plain"` or `".TXT"` matched in the
    worker and in no counter."""
    cfg = _cfg(
        extractor_mime_allowlist=["Text/Plain"],
        extractor_extension_allowlist=[".TXT"],
    )
    _blob(db_conn, "mixed", mime, filename=filename)
    db_conn.commit()

    assert _counts(db_conn, cfg).eligible == 1
    assert attachment_kind.is_allowlisted(
        mime,
        [filename] if filename else [],
        mime_allowlist=cfg.extractor_mime_allowlist,
        extension_allowlist=cfg.extractor_extension_allowlist,
    )
