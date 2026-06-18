# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Tests for Phase 2 extraction CLI commands."""

from __future__ import annotations

import hashlib
import json

from click.testing import CliRunner

from localmail.cli import main


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

    monkeypatch.setattr("localmail.cli._dsn", lambda: db_dsn)

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
    monkeypatch.setattr("localmail.cli._dsn", lambda: db_dsn)
    runner = CliRunner()
    result = runner.invoke(main, ["search-status", "--format", "json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert "blobs_eligible" in payload
    assert "blobs_extracted" in payload
    assert "blobs_pending" in payload
    assert "attachment_chunks_total" in payload
    assert "attachment_chunks_embedded" in payload
    assert "failed_extractions" in payload


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

    monkeypatch.setattr("localmail.cli._dsn", lambda: db_dsn)

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

    monkeypatch.setattr("localmail.cli._dsn", lambda: db_dsn)

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

    monkeypatch.setattr("localmail.cli._dsn", lambda: db_dsn)

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

    monkeypatch.setattr("localmail.cli._dsn", lambda: db_dsn)

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

    monkeypatch.setattr("localmail.cli._dsn", lambda: db_dsn)

    runner = CliRunner()
    result = runner.invoke(
        main, ["retry-failed-extractions", "--sha256", sha_a.hex()]
    )
    assert result.exit_code == 0, result.output

    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM transient_extractions")
        row = cur.fetchone()
        assert row is not None and row[0] == 1  # only sha_b's transient row remains
