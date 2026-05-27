"""Unit tests for localmail.upgrade_estimate (issue #2)."""

from __future__ import annotations

import dataclasses

import pytest
from psycopg.types.json import Jsonb

from localmail.config import UpgradeEstimateConfig
from localmail.upgrade_estimate import (
    ESTIMATORS,
    EstimateResult,
    estimate_0006,
)


def _seed_account(conn) -> int:
    """Insert one account row, return its id. Required so message rows can FK."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO accounts (name, email_address, imap_host, auth_method)
            VALUES ('test', 'test@example.com', 'localhost', 'password')
            RETURNING id
            """,
        )
        row = cur.fetchone()
        assert row is not None
        return int(row[0])


def _seed_messages_with_known_text(
    conn, *, account_id: int, count: int, body_len: int
) -> None:
    """Insert ``count`` rows into ``messages`` with each body_text of length
    ``body_len`` and subject of length ``body_len // 10``. Knowing the
    text length lets the projection-math tests assert linearity.
    """
    subject_len = max(1, body_len // 10)
    subject = "s" * subject_len
    body = "b" * body_len
    with conn.cursor() as cur:
        for i in range(count):
            cur.execute(
                """
                INSERT INTO messages (
                    account_id, message_id, raw_sha256, headers,
                    subject, body_text, body_html, raw_bytes, size_bytes
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    account_id,
                    f"<m{i}@test>",
                    f"sha-{i}".encode(),
                    Jsonb({}),
                    subject,
                    body,
                    None,
                    b"",
                    0,
                ),
            )
    conn.commit()


def _seed_message_chunks(
    conn, *, message_id: int, count: int, text_len: int
) -> None:
    """Insert ``count`` rows into ``message_chunks`` against ``message_id``.

    Each chunk has ``text`` of length ``text_len`` (ASCII, so
    octet_length == len), kind 'body', and unique chunk_idx within the
    message. ``token_count`` is a rough estimate and not load-bearing
    for the estimator (which reads ``text`` only).
    """
    text = "c" * text_len
    token_count = max(1, text_len // 4)
    with conn.cursor() as cur:
        for i in range(count):
            cur.execute(
                """
                INSERT INTO message_chunks (
                    message_id, kind, chunk_idx, text, token_count
                )
                VALUES (%s, %s, %s, %s, %s)
                """,
                (message_id, "body", i, text, token_count),
            )
    conn.commit()


def _first_message_id(conn) -> int:
    """Return any message id; the chunks-GIN projection doesn't care which."""
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM messages LIMIT 1")
        row = cur.fetchone()
        assert row is not None
        return int(row[0])


def test_estimate_result_is_immutable_dataclass():
    """EstimateResult is frozen — accidental mutation must fail."""
    r = EstimateResult(
        revision="0006_search_indexes",
        status="pending",
        current_bytes={},
        projected_bytes={"fts_v2": 100, "gin_messages": 40, "gin_chunks": 20},
        projected_duration_s=1.5,
        warnings=[],
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        r.revision = "other"  # type: ignore[misc]


def test_estimate_result_fields_present():
    """All wire-stable fields exist on EstimateResult."""
    r = EstimateResult(
        revision="0006_search_indexes",
        status="applied",
        current_bytes={"fts_v2_idx": 1000, "chunks_fts_idx": 500},
        projected_bytes={},
        projected_duration_s=0.0,
        warnings=[],
    )
    assert r.revision == "0006_search_indexes"
    assert r.status == "applied"
    assert r.current_bytes == {"fts_v2_idx": 1000, "chunks_fts_idx": 500}
    assert r.projected_bytes == {}
    assert r.projected_duration_s == 0.0
    assert r.warnings == []


def test_estimators_registry_has_0006():
    """ESTIMATORS is a dict[str, Callable]; 0006 is registered."""
    assert "0006_search_indexes" in ESTIMATORS
    assert callable(ESTIMATORS["0006_search_indexes"])


def test_unknown_revision_raises_keyerror():
    """Documented contract: missing key raises KeyError (not silent miss)."""
    with pytest.raises(KeyError):
        ESTIMATORS["0099_nonsense"]


def test_estimate_0006_pending_empty_messages(db_conn):
    """No rows -> all projections are zero. No divide-by-zero anywhere."""
    cfg = UpgradeEstimateConfig()
    result = estimate_0006(db_conn, cfg, applied=False)
    assert result.revision == "0006_search_indexes"
    assert result.status == "pending"
    assert result.projected_bytes == {
        "fts_v2": 0,
        "gin_messages": 0,
        "gin_chunks": 0,
    }
    assert result.projected_duration_s == 0.0
    assert result.current_bytes == {}


def test_estimate_0006_pending_with_seeded_rows(db_conn):
    """Projection scales with rows × text length × blowup factor."""
    account_id = _seed_account(db_conn)
    rows = 100
    body_len = 200
    _seed_messages_with_known_text(
        db_conn, account_id=account_id, count=rows, body_len=body_len
    )
    cfg = UpgradeEstimateConfig()  # defaults

    result = estimate_0006(db_conn, cfg, applied=False)

    assert result.status == "pending"
    # avg(octet_length(subject) + octet_length(body_text) + octet_length(body_html))
    # body_html is NULL -> coalesce('') -> 0
    # subject_len = body_len // 10 = 20 (per helper); ASCII so length == octet_length
    avg_text_bytes_expected = body_len + (body_len // 10)
    projected_fts_v2_expected = rows * avg_text_bytes_expected * cfg.fts_v2_blowup_factor
    # ±10% absorbs avg() returning a Decimal with rounding.
    assert result.projected_bytes["fts_v2"] == pytest.approx(
        projected_fts_v2_expected, rel=0.1
    )
    projected_gin_expected = projected_fts_v2_expected * cfg.gin_size_factor
    assert result.projected_bytes["gin_messages"] == pytest.approx(
        projected_gin_expected, rel=0.1
    )
    # Duration is non-zero and positive.
    assert result.projected_duration_s > 0.0
    # Chunks-GIN cannot be sized before chunks exist; pin the warning
    # string so a future rewrite doesn't silently break the operator
    # contract documented in the runbook.
    assert any(
        "chunks GIN size cannot be projected" in w for w in result.warnings
    ), f"expected chunks-GIN warning in {result.warnings!r}"


def test_estimate_0006_pending_duration_uses_config_rates(db_conn):
    """Slower throughput rate -> proportionally longer projected duration."""
    account_id = _seed_account(db_conn)
    _seed_messages_with_known_text(
        db_conn, account_id=account_id, count=100, body_len=500
    )
    cfg_fast = UpgradeEstimateConfig(table_rewrite_mb_per_sec=1000.0)
    cfg_slow = UpgradeEstimateConfig(table_rewrite_mb_per_sec=10.0)

    r_fast = estimate_0006(db_conn, cfg_fast, applied=False)
    r_slow = estimate_0006(db_conn, cfg_slow, applied=False)

    # The GIN-build component uses gin_build_mb_per_sec (untouched here)
    # so the ratio isn't exactly 100x; assert directional + monotonic.
    assert r_slow.projected_duration_s > r_fast.projected_duration_s
    # The table-rewrite component scales linearly with 1/rate, so the
    # delta is bounded below by (rewrite_fast_term - rewrite_slow_term).
    # Sanity check: slow duration is at least 10x fast.
    assert r_slow.projected_duration_s >= 10 * r_fast.projected_duration_s


def test_estimate_0006_not_applicable_when_messages_missing(db_conn):
    """If messages table doesn't exist, return not_applicable with a
    friendly warning. Uses a savepoint + rollback so we don't break
    the schema for subsequent tests in the same session."""
    cfg = UpgradeEstimateConfig()
    with db_conn.cursor() as cur:
        cur.execute("SAVEPOINT before_drop_messages")
        # CASCADE drops dependent FKs (message_labels.message_id, etc).
        # All restored by the ROLLBACK TO SAVEPOINT below — DDL in
        # PostgreSQL is transactional.
        cur.execute("DROP TABLE messages CASCADE")
        try:
            result = estimate_0006(db_conn, cfg, applied=False)
        finally:
            cur.execute("ROLLBACK TO SAVEPOINT before_drop_messages")

    assert result.revision == "0006_search_indexes"
    assert result.status == "not_applicable"
    assert result.current_bytes == {}
    assert result.projected_bytes == {}
    assert result.projected_duration_s == 0.0
    assert any("messages table not present" in w for w in result.warnings), (
        f"expected friendly missing-table warning in {result.warnings!r}"
    )


def test_estimate_0006_applied_reports_actual_sizes(db_conn):
    """With 0006 in the fixture state, applied=True reads pg_total_relation_size."""
    account_id = _seed_account(db_conn)
    _seed_messages_with_known_text(
        db_conn, account_id=account_id, count=20, body_len=500
    )
    cfg = UpgradeEstimateConfig()

    result = estimate_0006(db_conn, cfg, applied=True)

    assert result.status == "applied"
    assert result.projected_bytes == {}
    assert result.projected_duration_s == 0.0
    # Both GIN indexes exist post-fixture and report a non-zero size.
    assert result.current_bytes["messages_fts_v2_idx"] > 0
    assert result.current_bytes["message_chunks_fts_idx"] >= 0
    # message_chunks_fts_idx is on an empty table here, so its
    # pg_total_relation_size will be small but should still be returned.
    assert "message_chunks_fts_idx" in result.current_bytes


def test_estimate_0006_applied_with_index_missing_emits_warning(db_conn):
    """Drop the messages GIN inside a savepoint; estimator must report it."""
    cfg = UpgradeEstimateConfig()

    with db_conn.cursor() as cur:
        cur.execute("SAVEPOINT before_drop_idx")
        cur.execute("DROP INDEX messages_fts_v2_idx")

        try:
            result = estimate_0006(db_conn, cfg, applied=True)
        finally:
            cur.execute("ROLLBACK TO SAVEPOINT before_drop_idx")

    assert result.status == "applied"
    assert "messages_fts_v2_idx" not in result.current_bytes
    assert any(
        "messages_fts_v2_idx missing" in w for w in result.warnings
    ), f"expected missing-index warning in {result.warnings!r}"
    # The chunks GIN still exists, so it should still report a size.
    assert "message_chunks_fts_idx" in result.current_bytes


# ---- issue #106: chunks-GIN projection when message_chunks is populated ---


def test_estimate_0006_pending_with_populated_chunks_projects_chunks_gin(db_conn):
    """When message_chunks is non-empty, gin_chunks projection is positive
    and the 'cannot be projected' warning is absent.

    Acceptance criterion from issue #106.
    """
    account_id = _seed_account(db_conn)
    _seed_messages_with_known_text(
        db_conn, account_id=account_id, count=5, body_len=200
    )
    chunks_count = 20
    chunk_text_len = 500
    _seed_message_chunks(
        db_conn,
        message_id=_first_message_id(db_conn),
        count=chunks_count,
        text_len=chunk_text_len,
    )

    cfg = UpgradeEstimateConfig()
    result = estimate_0006(db_conn, cfg, applied=False)

    assert result.status == "pending"
    assert result.projected_bytes["gin_chunks"] > 0
    # text is ASCII so octet_length == len(text). Same blowup + gin_size
    # factors as the messages GIN.
    expected_gin_chunks = (
        chunks_count
        * chunk_text_len
        * cfg.fts_v2_blowup_factor
        * cfg.gin_size_factor
    )
    assert result.projected_bytes["gin_chunks"] == pytest.approx(
        expected_gin_chunks, rel=0.1
    )
    # The "cannot be projected" warning must NOT appear when chunks exist.
    assert not any(
        "chunks GIN size cannot be projected" in w for w in result.warnings
    ), (
        "unexpected chunks-projection warning when chunks populated: "
        f"{result.warnings!r}"
    )


def test_estimate_0006_pending_chunks_gin_contributes_to_duration(db_conn):
    """The chunks-GIN component must contribute to projected_duration_s.

    Compare the same message corpus with and without chunks: populating
    chunks must push the projected duration strictly higher, because the
    duration formula sums both GIN sizes / build throughput.
    """
    account_id = _seed_account(db_conn)
    _seed_messages_with_known_text(
        db_conn, account_id=account_id, count=5, body_len=200
    )
    cfg = UpgradeEstimateConfig()
    baseline = estimate_0006(db_conn, cfg, applied=False)

    _seed_message_chunks(
        db_conn,
        message_id=_first_message_id(db_conn),
        count=20,
        text_len=500,
    )
    with_chunks = estimate_0006(db_conn, cfg, applied=False)

    assert with_chunks.projected_duration_s > baseline.projected_duration_s


def test_estimate_0006_pending_empty_chunks_still_warns(db_conn):
    """Regression pin: when ``message_chunks`` is empty the
    'cannot be projected' warning must still appear and gin_chunks
    must be zero.

    Tightens the contract documented in the runbook and addresses
    issue #105 (was a one-line follow-up of PR #102).
    """
    account_id = _seed_account(db_conn)
    _seed_messages_with_known_text(
        db_conn, account_id=account_id, count=5, body_len=200
    )
    cfg = UpgradeEstimateConfig()

    result = estimate_0006(db_conn, cfg, applied=False)

    assert result.projected_bytes["gin_chunks"] == 0
    assert any(
        "chunks GIN size cannot be projected" in w for w in result.warnings
    ), (
        "expected chunks-GIN cannot-be-projected warning when chunks empty: "
        f"{result.warnings!r}"
    )
