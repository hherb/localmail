# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Tests for embed-backfill and search-status CLI verbs."""

from __future__ import annotations

import json

from click.testing import CliRunner

from localmail.cli import main


def test_cli_embed_backfill_drains_queue(monkeypatch, db_dsn, db_conn, cli_config):
    with db_conn.cursor() as cur:
        cur.execute("INSERT INTO accounts (name,email_address,imap_host,auth_method)"
                    " VALUES ('a','a@x','h','password') RETURNING id")
        acct = cur.fetchone()[0]
        for i in range(3):
            cur.execute(
                "INSERT INTO messages (account_id, message_id, raw_sha256, subject,"
                " body_text, headers, raw_bytes, size_bytes)"
                " VALUES (%s, %s, %s, 's', 'b', '{}'::jsonb, 'r', 1)",
                (acct, f"<m{i}>", bytes([i + 1]) * 32),
            )
    db_conn.commit()

    class _E:
        name = "s"; model = "s"; dimension = 768
        def embed_documents(self, t): return [[0.5] * 768 for _ in t]
        def embed_query(self, t): return [0.5] * 768
        def health_check(self): pass

    monkeypatch.setattr("localmail.cli._make_backend", lambda cfg: _E())
    monkeypatch.setattr("localmail.cli._dsn", lambda ctx: db_dsn)

    runner = CliRunner()
    result = runner.invoke(main, ["embed-backfill", "--no-progress"])
    assert result.exit_code == 0, result.output
    with db_conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM message_chunks WHERE embedding_v1 IS NOT NULL")
        assert cur.fetchone()[0] >= 3


def test_cli_search_status_reports_counts(monkeypatch, db_dsn, db_conn, cli_config):
    monkeypatch.setattr("localmail.cli._dsn", lambda ctx: db_dsn)
    runner = CliRunner()
    result = runner.invoke(main, ["search-status", "--format", "json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert "messages_total" in payload
    assert "chunks_total" in payload
    assert "chunks_embedded" in payload
    assert "failed_embeddings" in payload
