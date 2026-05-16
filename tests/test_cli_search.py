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
