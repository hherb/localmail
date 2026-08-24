# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""CLI surface for API keys. stdout carries only the key, so a provisioning
script can capture it."""
from __future__ import annotations

import psycopg
import pytest
from click.testing import CliRunner

from localmail.cli import main


@pytest.fixture
def runner(db_dsn, monkeypatch):
    monkeypatch.setenv("LOCALMAIL_DSN_OVERRIDE", db_dsn)
    return CliRunner()


def _account(conn: psycopg.Connection, name: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO accounts (name, email_address, auth_method, "
            "imap_host, imap_port, config) "
            "VALUES (%s, %s, 'password', 'imap.example', 993, '{}'::jsonb)",
            (name, f"{name}@b.test"),
        )
    conn.commit()


def test_add_prints_only_the_key_on_stdout(runner, db_conn):
    _account(db_conn, "work")
    result = runner.invoke(
        main, ["add-api-key", "my_mail_bot", "--grant", "work"],
        catch_exceptions=False,
    )
    assert result.exit_code == 0
    assert result.stdout.strip().startswith("lmk_")
    assert len(result.stdout.strip().splitlines()) == 1


def test_add_rejects_an_unknown_account(runner, db_conn):
    result = runner.invoke(main, ["add-api-key", "bot", "--grant", "nope"])
    assert result.exit_code != 0
    assert "no such account" in result.output


def test_add_rejects_a_duplicate(runner, db_conn):
    runner.invoke(main, ["add-api-key", "bot"], catch_exceptions=False)
    result = runner.invoke(main, ["add-api-key", "bot"])
    assert result.exit_code != 0


def test_list_shows_names_and_grants(runner, db_conn):
    _account(db_conn, "work")
    runner.invoke(main, ["add-api-key", "bot", "--grant", "work"],
                  catch_exceptions=False)
    result = runner.invoke(main, ["list-api-keys"], catch_exceptions=False)
    assert "bot" in result.output
    assert "work" in result.output


def test_revoke_keeps_the_principal_then_re_key_works(runner, db_conn):
    _account(db_conn, "work")
    runner.invoke(main, ["add-api-key", "bot", "--grant", "work"],
                  catch_exceptions=False)
    assert runner.invoke(main, ["revoke-api-key", "bot"]).exit_code == 0
    assert "no key" in runner.invoke(main, ["list-api-keys"]).output
    second = runner.invoke(main, ["add-api-key", "bot"], catch_exceptions=False)
    assert second.exit_code == 0
    assert "work" in runner.invoke(main, ["list-api-keys"]).output


def test_remove_deletes_the_principal(runner, db_conn):
    runner.invoke(main, ["add-api-key", "bot"], catch_exceptions=False)
    assert runner.invoke(main, ["remove-api-key", "bot"]).exit_code == 0
    assert "(no API keys)" in runner.invoke(main, ["list-api-keys"]).output


def test_revoke_unknown_is_an_error(runner, db_conn):
    assert runner.invoke(main, ["revoke-api-key", "ghost"]).exit_code != 0


def test_remove_unknown_is_an_error(runner, db_conn):
    assert runner.invoke(main, ["remove-api-key", "ghost"]).exit_code != 0


def test_list_api_users_marks_a_service_principal(runner, db_conn):
    """A bot listed among people, with nothing saying which is which, is the
    same false impression the Users screen carried."""
    runner.invoke(main, ["add-api-key", "bot"], catch_exceptions=False)
    runner.invoke(main, ["add-api-user", "amy", "--password", "pw"],
                  catch_exceptions=False)
    out = runner.invoke(main, ["list-api-users"], catch_exceptions=False).output
    assert "bot [service]" in out
    assert "amy\n" in out


def test_revoke_refuses_a_persons_username(runner, db_conn):
    """"ghost" never reaches the service layer -- resolve_user_id_by_username
    fails first -- so the CLI's ApiKeyNotFound branches were unexercised. A real
    human username is the way in, and makes this a second id-resolving path onto
    a person's row, guarded only at the service layer.
    """
    runner.invoke(main, ["add-api-user", "amy", "--password", "pw"],
                  catch_exceptions=False)
    assert runner.invoke(main, ["revoke-api-key", "amy"]).exit_code != 0
    assert runner.invoke(main, ["remove-api-key", "amy"]).exit_code != 0
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM api_users WHERE username = 'amy'")
        row = cur.fetchone()
    assert row is not None and row[0] == 1


def test_a_key_in_use_reports_when_it_was_last_used(runner, db_conn):
    """last_used_at is the whole signal for "is this bot live or abandoned", and
    it travels through a LEFT JOIN projection plus verify_token's
    LAST_USED_REFRESH_SECONDS dedup -- either could break in silence. It was
    asserted only in its two null states."""
    from localmail.api.auth import verify_token

    # .stdout, never .output: since click 8.2 output interleaves stderr in
    # write order, and this command deliberately puts its note there.
    raw = runner.invoke(
        main, ["add-api-key", "bot"], catch_exceptions=False
    ).stdout.strip()
    assert "never" in runner.invoke(main, ["list-api-keys"]).output

    assert verify_token(db_conn, raw) is not None
    db_conn.commit()
    assert "last-used=never" not in runner.invoke(main, ["list-api-keys"]).output
