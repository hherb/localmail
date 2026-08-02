# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""The extraction allowlist gate: what it admits, and what it does with the
blobs it turns away (#216).

Two defects, one root cause. The gate read the extension off
`attachment_blobs.path` — extensionless by construction — so
`extractor_extension_allowlist` never matched anything and a mis-typed
attachment could not be indexed. And a turned-away blob was `continue`d with no
record, so it never gained an `attachment_text` row, stayed eligible forever,
and was re-claimed on every sweep. Since `_claim_batch` is `ORDER BY
first_seen_at LIMIT extract_worker_batch_size`, a batch that happened to be all
images returned `touched=0` — which both the CLI backfill loop and the daemon
worker read as "queue drained". Everything behind those blobs was then never
extracted at all.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from localmail.config import SearchConfig

SKIPPED_EXTRACTOR = "type-skipped"


def _seed_blob(
    db_conn,
    content: bytes,
    mime_type: str,
    attachments_root: Path,
    filename: str | None = None,
) -> bytes:
    """Insert a blob, write its bytes, and — when `filename` is given — a
    message referencing it under that original name, which is the only place
    an attachment's real filename is recorded."""
    sha = hashlib.sha256(content).digest()
    hex_ = sha.hex()
    blob_path = attachments_root / "blobs" / hex_[:2] / hex_[2:4] / hex_
    blob_path.parent.mkdir(parents=True, exist_ok=True)
    blob_path.write_bytes(content)

    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO attachment_blobs (sha256, path, mime_type, size_bytes) "
            "VALUES (%s, %s, %s, %s)",
            (sha, str(blob_path), mime_type, len(content)),
        )
        if filename is not None:
            cur.execute(
                "INSERT INTO accounts (name, email_address, imap_host, auth_method) "
                "VALUES (%s, 'a@x', 'h', 'password') "
                "ON CONFLICT (name) DO UPDATE SET email_address = EXCLUDED.email_address "
                "RETURNING id",
                ("acct",),
            )
            row = cur.fetchone()
            assert row is not None
            cur.execute(
                "INSERT INTO messages (account_id, message_id, raw_sha256, subject,"
                " headers, raw_bytes, size_bytes, attachments)"
                " VALUES (%s, %s, %s, 's', '{}'::jsonb, %s, %s, %s::jsonb)",
                (
                    row[0],
                    f"<{hex_}>",
                    sha,
                    b"raw",
                    1,
                    json.dumps([{"filename": filename, "sha256": hex_}]),
                ),
            )
    db_conn.commit()
    return sha


def _text_row(db_conn, sha: bytes) -> tuple[str, str] | None:
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT extractor, extracted_text FROM attachment_text WHERE sha256 = %s",
            (sha,),
        )
        return cur.fetchone()


def _drain(db_conn, cfg: SearchConfig, max_sweeps: int = 20) -> int:
    """Run sweeps until one reports nothing done, exactly as the CLI backfill
    loop and the daemon worker do."""
    from localmail.search.extract_worker import run_extract_worker_once

    total = 0
    for _ in range(max_sweeps):
        touched = run_extract_worker_once(db_conn, cfg)
        if touched == 0:
            return total
        total += touched
    raise AssertionError("did not settle")


def test_a_mistyped_attachment_is_extracted_via_its_filename(
    db_conn, tmp_path
) -> None:
    """The filed bug: mobile and webmail clients routinely send a real document
    as `application/octet-stream`. The original filename is what identifies it.
    """
    sha = _seed_blob(
        db_conn,
        b"the quick brown fox",
        "application/octet-stream",
        tmp_path,
        filename="notes.txt",
    )

    _drain(db_conn, SearchConfig())

    row = _text_row(db_conn, sha)
    assert row is not None, "blob was turned away despite a .txt filename"
    assert "the quick brown fox" in row[1]


def test_a_blob_outside_both_allowlists_is_recorded_as_skipped(
    db_conn, tmp_path
) -> None:
    """Turning a blob away is a decision worth recording. Without a row there is
    no operator-visible signal at all — the point of #216 — and nothing stops
    the blob being re-claimed forever."""
    sha = _seed_blob(db_conn, b"\x89PNG\x00", "image/png", tmp_path, filename="logo.png")

    _drain(db_conn, SearchConfig())

    row = _text_row(db_conn, sha)
    assert row is not None
    assert row[0] == SKIPPED_EXTRACTOR
    assert row[1] == ""


def test_a_skipped_blob_is_not_reclaimed_on_the_next_sweep(
    db_conn, tmp_path
) -> None:
    from localmail.search.extract_worker import run_extract_worker_once

    _seed_blob(db_conn, b"\x89PNG\x00", "image/png", tmp_path, filename="logo.png")
    cfg = SearchConfig()

    assert run_extract_worker_once(db_conn, cfg) == 1
    assert run_extract_worker_once(db_conn, cfg) == 0


def test_a_batch_of_unsupported_blobs_does_not_starve_the_queue(
    db_conn, tmp_path
) -> None:
    """The severe half. `_claim_batch` is FIFO by `first_seen_at`, so one full
    batch of images ahead of a document used to end extraction for the entire
    archive: the sweep reported `touched=0` and every caller read that as
    "drained".
    """
    cfg = SearchConfig(extract_worker_batch_size=3)
    for i in range(cfg.extract_worker_batch_size):
        _seed_blob(
            db_conn, f"png-{i}".encode(), "image/png", tmp_path, filename=f"{i}.png"
        )
    wanted = _seed_blob(
        db_conn, b"findable document text", "text/plain", tmp_path, filename="doc.txt"
    )

    _drain(db_conn, cfg)

    row = _text_row(db_conn, wanted)
    assert row is not None, "the queue starved behind a batch of unsupported blobs"
    assert "findable document text" in row[1]


def test_a_blob_with_no_recorded_filename_still_goes_by_its_mime_type(
    db_conn, tmp_path
) -> None:
    """No message references it yet (a partially-imported archive), so there is
    no filename to consult — the MIME branch must still work on its own."""
    sha = _seed_blob(db_conn, b"plain words here", "text/plain", tmp_path)

    _drain(db_conn, SearchConfig())

    row = _text_row(db_conn, sha)
    assert row is not None
    assert "plain words here" in row[1]
