# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""`localmail migrate-secrets` — the one-shot keyring → file copy.

Without it, switching a headless host to the file backend means re-driving the
Gmail consent flow on a machine with no browser. With it, the operator unlocks
the keyring one final time and never again.
"""
from __future__ import annotations

from pathlib import Path

import psycopg
from click.testing import CliRunner

from localmail import secrets
from localmail.cli import main
from localmail.secrets_file import FileSecretStore
from localmail.secrets_migrate import plan_secret_migration


# ---------------------------------------------------------------- pure planner


def test_plan_pairs_each_account_with_both_key_kinds() -> None:
    plan = plan_secret_migration(["gmail"], {"gmail": "pw", "gmail:refresh": "tok"})
    assert [(i.account_name, i.kind, i.value) for i in plan.to_copy] == [
        ("gmail", "password", "pw"),
        ("gmail", "refresh", "tok"),
    ]
    assert plan.absent == []


def test_plan_skips_keys_the_source_does_not_hold() -> None:
    """A password account has no refresh token and vice versa; neither is an
    error, and reporting them as absent is what makes the summary honest."""
    plan = plan_secret_migration(["gmail"], {"gmail:refresh": "tok"})
    assert [(i.account_name, i.kind) for i in plan.to_copy] == [("gmail", "refresh")]
    assert [(i.account_name, i.kind) for i in plan.absent] == [("gmail", "password")]


def test_plan_treats_a_none_value_as_absent() -> None:
    """`keyring.get_password` returns None rather than raising for a missing
    entry, so None must not be copied through as a literal secret."""
    plan = plan_secret_migration(["gmail"], {"gmail": None, "gmail:refresh": "tok"})
    assert [i.kind for i in plan.to_copy] == ["refresh"]


def test_plan_covers_every_account() -> None:
    plan = plan_secret_migration(["a", "b"], {"a": "1", "b:refresh": "2"})
    assert {i.account_name for i in plan.to_copy} == {"a", "b"}
    assert len(plan.to_copy) == 2
    assert len(plan.absent) == 2


def test_plan_for_no_accounts_is_empty() -> None:
    plan = plan_secret_migration([], {})
    assert plan.to_copy == [] and plan.absent == []


# ------------------------------------------------------------------- CLI wiring


def _make_cfg(tmp_path: Path, dsn: str, store: Path) -> Path:
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f'[database]\ndsn = "{dsn}"\n\n[attachments]\nroot = "{tmp_path / "att"}"\n'
        f'\n[secrets]\nfile_path = "{store}"\n'
    )
    return cfg


def _seed_account(conn: psycopg.Connection, name: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO accounts (name, email_address, imap_host, imap_port, "
            "auth_method) VALUES (%s, %s, 'imap.example.com', 993, 'password')",
            (name, f"{name}@example.com"),
        )
    conn.commit()


def test_migrate_copies_both_key_kinds_into_the_file(
    db_conn: psycopg.Connection, db_dsn: str, tmp_path: Path, memory_keyring
) -> None:
    _seed_account(db_conn, "gmail")
    store = tmp_path / "secrets.json"
    secrets.set_password("gmail", "hunter2")
    secrets.set_refresh_token("gmail", "1//0aBc")

    res = CliRunner().invoke(
        main, ["--config", str(_make_cfg(tmp_path, db_dsn, store)), "migrate-secrets"]
    )
    assert res.exit_code == 0, res.output

    on_disk = FileSecretStore(store)
    assert on_disk.get("gmail") == "hunter2"
    assert on_disk.get("gmail:refresh") == "1//0aBc"


def test_migrate_leaves_the_keyring_intact(
    db_conn: psycopg.Connection, db_dsn: str, tmp_path: Path, memory_keyring
) -> None:
    """A failed migration must be re-runnable, so the source is never emptied."""
    _seed_account(db_conn, "gmail")
    secrets.set_password("gmail", "hunter2")

    res = CliRunner().invoke(
        main,
        [
            "--config",
            str(_make_cfg(tmp_path, db_dsn, tmp_path / "secrets.json")),
            "migrate-secrets",
        ],
    )
    assert res.exit_code == 0, res.output
    assert memory_keyring.get_password(secrets.SERVICE, "gmail") == "hunter2"


def test_dry_run_writes_nothing(
    db_conn: psycopg.Connection, db_dsn: str, tmp_path: Path, memory_keyring
) -> None:
    _seed_account(db_conn, "gmail")
    store = tmp_path / "secrets.json"
    secrets.set_password("gmail", "hunter2")

    res = CliRunner().invoke(
        main,
        [
            "--config",
            str(_make_cfg(tmp_path, db_dsn, store)),
            "migrate-secrets",
            "--dry-run",
        ],
    )
    assert res.exit_code == 0, res.output
    assert not store.exists()
    assert "gmail" in res.output


def test_migrate_reports_accounts_with_no_stored_secret(
    db_conn: psycopg.Connection, db_dsn: str, tmp_path: Path, memory_keyring
) -> None:
    _seed_account(db_conn, "never-configured")
    res = CliRunner().invoke(
        main,
        [
            "--config",
            str(_make_cfg(tmp_path, db_dsn, tmp_path / "secrets.json")),
            "migrate-secrets",
        ],
    )
    assert res.exit_code == 0, res.output
    assert "copied 0" in res.output


def test_migrate_reads_the_keyring_even_when_the_file_backend_is_active(
    db_conn: psycopg.Connection, db_dsn: str, tmp_path: Path, memory_keyring
) -> None:
    """The realistic order of operations is: flip the config, restart, watch it
    fail, then migrate. So the command must not read through whichever backend
    the config happens to name — it always reads the keyring."""
    _seed_account(db_conn, "gmail")
    store = tmp_path / "secrets.json"
    secrets.set_password("gmail", "hunter2")
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f'[database]\ndsn = "{db_dsn}"\n\n[attachments]\nroot = "{tmp_path / "att"}"\n'
        f'\n[secrets]\nbackend = "file"\nfile_path = "{store}"\n'
    )

    res = CliRunner().invoke(main, ["--config", str(cfg), "migrate-secrets"])
    assert res.exit_code == 0, res.output
    assert FileSecretStore(store).get("gmail") == "hunter2"
