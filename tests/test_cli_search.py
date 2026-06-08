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


def test_cli_search_json_output_is_valid_search_page(monkeypatch, db_dsn, db_conn, cli_config):
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


def test_search_prints_notice_when_rewrite_skipped(monkeypatch, cli_config):
    # `search` now resolves config eagerly (create_searcher(load_config(...))),
    # so this test needs `cli_config` to make load_config() resolvable on a
    # clean CI runner (issue #100) even though create_searcher is stubbed.
    from click.testing import CliRunner
    from localmail.cli import main
    from localmail.search.searcher import SearchPage
    from localmail.search.query import ParsedQuery

    page = SearchPage(
        results=[], page=1, page_size=10, pool_size=0, candidates_per_arm=50,
        has_more_in_pool=False, can_grow_pool=False, search_token=None,
        query=ParsedQuery(free_text="x"), timing_ms={},
        rewrite_status="failed",
        rewrite_note="could not reach the rewriter service",
    )

    class _Stub:
        def search(self, *a, **k):
            return page

    monkeypatch.setattr("localmail.cli.create_searcher", lambda *a, **k: _Stub())
    res = CliRunner(capture="fd").invoke(main, ["search", "x", "--smart"])
    assert res.exit_code == 0
    assert "could not reach the rewriter service" in res.stderr


def test_cli_search_honours_config_flag(monkeypatch, tmp_path, db_dsn):
    """`localmail --config PATH search` must build the searcher from PATH.

    Regression: the `search` command used to call `create_searcher()` with no
    args, so it ignored the global `--config` flag and always re-read the
    default config — the only override was the `LOCALMAIL_CONFIG` env var.
    """
    cfg_path = tmp_path / "custom.toml"
    cfg_path.write_text(
        f'[database]\ndsn = "{db_dsn}"\n\n'
        "[search]\nrewriter_max_expansion_terms = 3\n"
    )

    captured: dict[str, object] = {}

    class _Stub:
        def search(self, *a, **k):
            raise RuntimeError("stop after capturing cfg")

    def fake_create(cfg=None, **kw):
        captured["cfg"] = cfg
        return _Stub()

    monkeypatch.setattr("localmail.cli.create_searcher", fake_create)
    runner = CliRunner()
    result = runner.invoke(main, ["--config", str(cfg_path), "search", "hello"])

    assert "cfg" in captured, "create_searcher was never called"
    assert captured["cfg"] is not None, "search ignored --config (cfg was None)"
    assert captured["cfg"].search.rewriter_max_expansion_terms == 3
    # the RuntimeError from the stub maps to the CLI's clean exit(2)
    assert result.exit_code == 2


def test_cli_search_page_and_grow_are_not_registered():
    """search-page / search-grow were removed — they were process-local stubs.

    Verify that invoking them is now a "no such command" error so anyone
    still calling them from a script sees a clear failure rather than a
    silently misleading exit.
    """
    runner = CliRunner()
    for argv in (["search-page", "deadbeef", "2"],
                 ["search-grow", "deadbeef", "--candidates", "200"]):
        result = runner.invoke(main, argv)
        assert result.exit_code != 0
