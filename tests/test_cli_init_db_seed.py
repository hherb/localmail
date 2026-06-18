# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""init-db seeds config.toml [[accounts]] into the DB (Sub-plan 2A.2)."""
from __future__ import annotations

from pathlib import Path

import psycopg
from click.testing import CliRunner

from localmail.api.admin.accounts import list_accounts_full
from localmail.cli import main


def _config_with_accounts(tmp_path: Path, dsn: str) -> Path:
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f'[database]\ndsn = "{dsn}"\n\n'
        f'[attachments]\nroot = "{tmp_path / "att"}"\n\n'
        '[[accounts]]\n'
        'name = "alice"\n'
        'email = "alice@example.com"\n'
        'imap_host = "imap.example.com"\n'
        'imap_port = 993\n'
        'auth_method = "password"\n\n'
        '[[accounts]]\n'
        'name = "bob"\n'
        'email = "bob@example.com"\n'
        'imap_host = "imap.example.com"\n'
        'imap_port = 993\n'
        'auth_method = "password"\n'
    )
    return cfg


def test_init_db_seeds_accounts(db_conn, db_dsn: str, tmp_path: Path) -> None:
    cfg = _config_with_accounts(tmp_path, db_dsn)
    runner = CliRunner()

    r = runner.invoke(main, ["--config", str(cfg), "init-db"])

    assert r.exit_code == 0, r.output
    assert "seeded accounts: inserted=2 skipped=0 drifted=0" in r.output
    # Re-read on a fresh connection so we see the CLI's committed rows.
    with psycopg.connect(db_dsn) as conn:
        names = {row.name for row in list_accounts_full(conn)}
    assert names == {"alice", "bob"}


def test_init_db_seed_is_idempotent(db_conn, db_dsn: str, tmp_path: Path) -> None:
    cfg = _config_with_accounts(tmp_path, db_dsn)
    runner = CliRunner()

    runner.invoke(main, ["--config", str(cfg), "init-db"])
    r = runner.invoke(main, ["--config", str(cfg), "init-db"])

    assert r.exit_code == 0, r.output
    assert "seeded accounts: inserted=0 skipped=2 drifted=0" in r.output


def test_init_db_seed_aborts_non_zero_on_invalid_account(
    db_conn, db_dsn: str, tmp_path: Path
) -> None:
    # oauth2 without an oauth_provider passes pydantic but fails
    # create_account's validation -> AccountFieldError -> ClickException.
    cfg = tmp_path / "config.toml"
    # A valid account ("good") is ordered BEFORE the broken one so the test
    # proves the whole batch is atomic: the good insert must be rolled back
    # when the later block aborts, not merely that the broken block is skipped.
    cfg.write_text(
        f'[database]\ndsn = "{db_dsn}"\n\n'
        f'[attachments]\nroot = "{tmp_path / "att"}"\n\n'
        '[[accounts]]\n'
        'name = "good"\n'
        'email = "good@example.com"\n'
        'imap_host = "imap.example.com"\n'
        'imap_port = 993\n'
        'auth_method = "password"\n\n'
        '[[accounts]]\n'
        'name = "broken"\n'
        'email = "broken@example.com"\n'
        'imap_host = "imap.example.com"\n'
        'imap_port = 993\n'
        'auth_method = "oauth2"\n'
    )
    runner = CliRunner()

    r = runner.invoke(main, ["--config", str(cfg), "init-db"])

    assert r.exit_code != 0
    assert "account seed failed" in r.output
    # The seed runs in one uncommitted transaction — the earlier valid
    # "good" insert must be rolled back too, leaving no partial rows.
    with psycopg.connect(db_dsn) as conn:
        assert list_accounts_full(conn) == []
