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
            (acct, b'\\x01' * 32),
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
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert len(payload) == 2
    assert payload[0]["error_class"] == "X"


def test_cli_retry_failed_embeddings_clears_rows(monkeypatch, db_dsn, db_conn):
    _insert_failed(db_conn, n=2)
    monkeypatch.setattr("localmail.cli._dsn", lambda: db_dsn)
    runner = CliRunner()
    result = runner.invoke(main, ["retry-failed-embeddings"])
    assert result.exit_code == 0, result.output
    with db_conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM failed_embeddings")
        assert cur.fetchone()[0] == 0
