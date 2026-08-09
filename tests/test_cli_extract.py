# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Tests for Phase 2 extraction CLI commands."""

from __future__ import annotations

import hashlib
import json

from click.testing import CliRunner

from localmail.cli import main
from localmail.config import SearchConfig
from localmail.search.extract_queue import QueueCounts, QueueCountsInconsistent


def test_cli_extract_backfill_drains_queue(
    monkeypatch, db_dsn, db_conn, tmp_path, cli_config
) -> None:
    """extract-backfill should drain attachment_text for eligible blobs."""
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

    monkeypatch.setattr("localmail.cli._dsn", lambda ctx: db_dsn)

    runner = CliRunner()
    result = runner.invoke(main, ["extract-backfill", "--no-progress"])
    assert result.exit_code == 0, result.output

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM attachment_text WHERE sha256 = %s", (sha,)
        )
        row = cur.fetchone()
        assert row is not None
        assert row[0] == 1


def test_cli_search_status_reports_attachment_counts(
    monkeypatch, db_dsn, db_conn, cli_config
) -> None:
    """search-status --format json must include Phase 2 attachment fields."""
    monkeypatch.setattr("localmail.cli._dsn", lambda ctx: db_dsn)
    runner = CliRunner()
    result = runner.invoke(main, ["search-status", "--format", "json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    # Every field the counts type carries reaches the wire, derived from the
    # type so a bucket added there cannot go missing from the report.
    assert set(QueueCounts.status_field_names()) <= set(payload)
    assert "attachment_chunks_total" in payload
    assert "attachment_chunks_embedded" in payload
    assert "failed_extractions" in payload


def _seed_blob(conn, label: str, mime: str = "text/plain") -> bytes:
    """Insert one attachment_blobs row; return its sha256 digest."""
    sha = hashlib.sha256(label.encode()).digest()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO attachment_blobs (sha256, path, mime_type, size_bytes) "
            "VALUES (%s, %s, %s, %s)",
            (sha, f"/blobs/{label}", mime, 10),
        )
    return sha


def _seed_text(conn, sha: bytes, extractor: str, text: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO attachment_text (sha256, extractor, extracted_text) "
            "VALUES (%s, %s, %s)",
            (sha, extractor, text),
        )


def _search_status(monkeypatch, db_dsn) -> dict:
    monkeypatch.setattr("localmail.cli._dsn", lambda ctx: db_dsn)
    result = CliRunner().invoke(main, ["search-status", "--format", "json"])
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def test_search_status_does_not_report_sentinel_blobs_as_pending(
    monkeypatch, db_dsn, db_conn, cli_config
) -> None:
    """#277: a blob the worker disposed of with an empty-text sentinel row is
    finished work, not outstanding work.

    `_claim_batch` skips any blob that already has an `attachment_text` row, so
    counting these as pending reports a queue that can never drain.
    """
    for label, extractor in [
        ("type_skipped", "type-skipped"),
        ("lightweight_empty", "lightweight-empty"),
        ("size_skipped", "size-skipped"),
        ("healed", "lightweight@1.0"),  # #266 whitespace heal
    ]:
        _seed_text(db_conn, _seed_blob(db_conn, label), extractor, "")
    db_conn.commit()

    payload = _search_status(monkeypatch, db_dsn)
    assert payload["blobs_pending"] == 0
    assert payload["blobs_no_text"] == 4


def test_search_status_does_not_report_capped_out_blobs_as_pending(
    monkeypatch, db_dsn, db_conn, cli_config
) -> None:
    """A blob parked at a retry cap (#153) is excluded by the claim too, so it
    would never drain either — reported separately as `blobs_gave_up`."""
    poisoned = _seed_blob(db_conn, "poisoned")
    stalled = _seed_blob(db_conn, "stalled")
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO failed_extractions "
            "(sha256, extractor, error_class, error_message, retry_count) "
            "VALUES (%s, 'lightweight', 'Boom', 'broken', %s)",
            (poisoned, SearchConfig().extract_worker_max_retries),
        )
        cur.execute(
            "INSERT INTO transient_extractions (sha256, transient_count) "
            "VALUES (%s, %s)",
            (stalled, SearchConfig().extract_worker_max_transient_retries),
        )
    db_conn.commit()

    payload = _search_status(monkeypatch, db_dsn)
    assert payload["blobs_pending"] == 0
    assert payload["blobs_gave_up"] == 2


def test_search_status_still_reports_genuinely_outstanding_blobs(
    monkeypatch, db_dsn, db_conn, cli_config
) -> None:
    """The counter must stay useful: an untouched eligible blob is pending."""
    _seed_blob(db_conn, "fresh")
    _seed_text(db_conn, _seed_blob(db_conn, "done"), "lightweight@1.0", "text")
    db_conn.commit()

    payload = _search_status(monkeypatch, db_dsn)
    assert payload["blobs_pending"] == 1
    assert payload["blobs_extracted"] == 1
    assert payload["blobs_claimable"] == 1


def test_search_status_reports_claimable_work_no_bucket_counts(
    monkeypatch, db_dsn, db_conn, cli_config
) -> None:
    """`blobs_pending` is allowlist-scoped and the worker's claim is not, so on
    an archive of images it reads zero while the worker still has claims to
    burn (#216). `blobs_claimable` is what makes that visible."""
    _seed_blob(db_conn, "image_1", "image/png")
    _seed_blob(db_conn, "image_2", "image/png")
    db_conn.commit()

    payload = _search_status(monkeypatch, db_dsn)
    assert payload["blobs_eligible"] == 0
    assert payload["blobs_pending"] == 0
    assert payload["blobs_claimable"] == 2


def test_search_status_survives_an_inconsistent_partition(
    monkeypatch, db_dsn, db_conn, cli_config
) -> None:
    """An attachment-side predicate bug must not take the rest of the report
    with it, and must not reach the operator as a traceback.

    The blob read is deliberately last for this reason: the embedding and
    body_lang counters — the ones #251 exists to make trustworthy — are
    already gathered by the time it can refuse to answer.
    """
    def _boom(conn, cfg):
        raise QueueCountsInconsistent("buckets do not sum: contrived")

    monkeypatch.setattr(
        "localmail.search.extract_queue.fetch_queue_counts", _boom
    )
    monkeypatch.setattr("localmail.cli._dsn", lambda ctx: db_dsn)
    result = CliRunner().invoke(main, ["search-status", "--format", "json"])

    assert result.exit_code != 0
    payload = json.loads(result.output.splitlines()[0])
    assert payload["messages_total"] == 0
    assert payload["body_lang_pending"] == 0
    assert all(payload[k] is None for k in QueueCounts.status_field_names())
    assert result.exception is None or not isinstance(result.exception, TypeError)
    assert "Traceback" not in result.output


def test_cli_list_failed_extractions(monkeypatch, db_dsn, db_conn) -> None:
    """list-failed-extractions --format json returns rows from failed_extractions."""
    sha = hashlib.sha256(b"x_failed").digest()
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

    monkeypatch.setattr("localmail.cli._dsn", lambda ctx: db_dsn)

    runner = CliRunner()
    result = runner.invoke(
        main, ["list-failed-extractions", "--format", "json"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert len(payload) == 1
    assert payload[0]["error_class"] == "BadFile"
    assert payload[0]["extractor"] == "lightweight"


def test_cli_retry_failed_extractions_clears_rows(monkeypatch, db_dsn, db_conn) -> None:
    """retry-failed-extractions deletes all failed_extractions rows."""
    sha = hashlib.sha256(b"y_retry").digest()
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

    monkeypatch.setattr("localmail.cli._dsn", lambda ctx: db_dsn)

    runner = CliRunner()
    result = runner.invoke(main, ["retry-failed-extractions"])
    assert result.exit_code == 0, result.output

    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM failed_extractions")
        row = cur.fetchone()
        assert row is not None
        assert row[0] == 0


def test_cli_retry_failed_extractions_single_sha(monkeypatch, db_dsn, db_conn) -> None:
    """--sha256 HEX restricts retry to one blob."""
    sha_a = hashlib.sha256(b"alpha").digest()
    sha_b = hashlib.sha256(b"beta").digest()
    with db_conn.cursor() as cur:
        for sha in (sha_a, sha_b):
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

    monkeypatch.setattr("localmail.cli._dsn", lambda ctx: db_dsn)

    runner = CliRunner()
    result = runner.invoke(
        main, ["retry-failed-extractions", "--sha256", sha_a.hex()]
    )
    assert result.exit_code == 0, result.output

    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM failed_extractions")
        row = cur.fetchone()
        assert row is not None
        assert row[0] == 1  # only sha_b's row remains


def test_cli_retry_failed_extractions_clears_transient(
    monkeypatch, db_dsn, db_conn
) -> None:
    """retry-failed-extractions also clears transient_extractions rows so a
    stuck-transient blob (#153) becomes eligible again after the operator
    fixes the underlying issue (e.g. a bad HF token)."""
    sha = hashlib.sha256(b"stuck_transient").digest()
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO attachment_blobs (sha256, path, mime_type, size_bytes) "
            "VALUES (%s, %s, %s, %s)",
            (sha, "/p", "application/pdf", 10),
        )
        cur.execute(
            "INSERT INTO transient_extractions "
            "(sha256, transient_count, error_class, error_message) "
            "VALUES (%s, 5, 'HfHubHTTPError', '401') ",
            (sha,),
        )
    db_conn.commit()

    monkeypatch.setattr("localmail.cli._dsn", lambda ctx: db_dsn)

    runner = CliRunner()
    result = runner.invoke(main, ["retry-failed-extractions"])
    assert result.exit_code == 0, result.output

    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM transient_extractions WHERE sha256 = %s",
                    (sha,))
        row = cur.fetchone()
        assert row is not None and row[0] == 0


def test_cli_retry_failed_extractions_single_sha_clears_transient(
    monkeypatch, db_dsn, db_conn
) -> None:
    """--sha256 HEX also restricts the transient_extractions clear to that blob."""
    sha_a = hashlib.sha256(b"alpha_tr").digest()
    sha_b = hashlib.sha256(b"beta_tr").digest()
    with db_conn.cursor() as cur:
        for sha in (sha_a, sha_b):
            cur.execute(
                "INSERT INTO attachment_blobs (sha256, path, mime_type, size_bytes) "
                "VALUES (%s, %s, %s, %s)",
                (sha, "/p", "application/pdf", 10),
            )
            cur.execute(
                "INSERT INTO transient_extractions "
                "(sha256, transient_count) VALUES (%s, 5)",
                (sha,),
            )
    db_conn.commit()

    monkeypatch.setattr("localmail.cli._dsn", lambda ctx: db_dsn)

    runner = CliRunner()
    result = runner.invoke(
        main, ["retry-failed-extractions", "--sha256", sha_a.hex()]
    )
    assert result.exit_code == 0, result.output

    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM transient_extractions")
        row = cur.fetchone()
        assert row is not None and row[0] == 1  # only sha_b's transient row remains
