"""CLI account commands operate on the DB (Sub-plan 2A.2d).

Scaffolding for Tasks 4-8.  Task 4 covers `list-accounts`.
Tasks 5-8 will append `add-account`, `oauth-login`, `remove-account`,
and `sync` tests to this same file.
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import psycopg
import pytest
from click.testing import CliRunner

from localmail.api.admin.accounts import create_account, get_account_by_name
from localmail.cli import main

pytestmark = pytest.mark.usefixtures("memory_keyring")


# ---------------------------------------------------------------------------
# Shared helpers (used by every task in 2A.2d)
# ---------------------------------------------------------------------------

def _write_config(tmp_path: Path, dsn: str, body: str = "") -> Path:
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        textwrap.dedent(
            f"""
            [database]
            dsn = "{dsn}"

            [attachments]
            root = "{tmp_path / 'attachments'}"

            {body}
            """
        ).strip()
    )
    return cfg


def _toml_block(
    name: str,
    email: str,
    auth: str = "password",
    oauth_provider: str | None = None,
) -> str:
    extra = f'\noauth_provider = "{oauth_provider}"' if oauth_provider else ""
    return textwrap.dedent(
        f"""
        [[accounts]]
        name = "{name}"
        email = "{email}"
        imap_host = "imap.example.com"
        imap_port = 993
        auth_method = "{auth}"{extra}
        """
    ).strip()


def _run(args: list[str], config_path: Path, **kw):
    runner = CliRunner()
    return runner.invoke(main, ["--config", str(config_path), *args], **kw)


def _make_db_account(
    dsn: str,
    name: str,
    *,
    auth: str = "password",
    oauth_provider: str | None = None,
    sync_enabled: bool = True,
) -> int:
    """Insert an account directly into the DB and return its id."""
    with psycopg.connect(dsn) as conn:
        acct = create_account(
            conn,
            name=name,
            email_address=f"{name}@example.com",
            auth_method=auth,
            imap_host=("imap.example.com" if auth != "archive" else None),
            imap_port=(993 if auth != "archive" else None),
            oauth_provider=oauth_provider,
            folder_allow=None,
            folder_deny=None,
            folder_deny_flags=None,
        )
        if not sync_enabled:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE accounts SET sync_enabled = FALSE WHERE id = %s",
                    (acct.id,),
                )
        conn.commit()
        return acct.id


# ---------------------------------------------------------------------------
# Task 4: list-accounts
# ---------------------------------------------------------------------------

def test_list_accounts_reads_db(db_conn, db_dsn: str, tmp_path: Path) -> None:
    """list-accounts must show accounts that exist in the DB, not just TOML."""
    _make_db_account(db_dsn, "work")
    cfg = _write_config(tmp_path, db_dsn)  # no [[accounts]] in TOML at all
    result = _run(["list-accounts"], cfg)
    assert result.exit_code == 0, result.output
    assert "work" in result.output


def test_list_accounts_empty_db(db_conn, db_dsn: str, tmp_path: Path) -> None:
    """list-accounts on an empty DB must report 'no accounts' clearly."""
    # db_conn fixture truncates accounts — so the table is clean here.
    cfg = _write_config(tmp_path, db_dsn)
    result = _run(["list-accounts"], cfg)
    assert result.exit_code == 0, result.output
    assert "no accounts" in result.output


# ---------------------------------------------------------------------------
# Task 5: add-account
# ---------------------------------------------------------------------------

def test_add_account_stores_password_for_existing_db_row(
    db_conn, db_dsn: str, tmp_path: Path
) -> None:
    _make_db_account(db_dsn, "work")
    cfg = _write_config(tmp_path, db_dsn)
    result = _run(["add-account", "work", "--password", "s3cret"], cfg)
    assert result.exit_code == 0, result.output
    from localmail import secrets as s
    assert s.get_password("work") == "s3cret"


def test_add_account_seeds_from_toml_when_absent(
    db_conn, db_dsn: str, tmp_path: Path
) -> None:
    cfg = _write_config(tmp_path, db_dsn, _toml_block("work", "work@example.com"))
    result = _run(["add-account", "work", "--password", "s3cret"], cfg)
    assert result.exit_code == 0, result.output
    with psycopg.connect(db_dsn) as conn:
        assert get_account_by_name(conn, "work") is not None  # row created
    from localmail import secrets as s
    assert s.get_password("work") == "s3cret"


def test_add_account_unknown_name_fails(
    db_conn, db_dsn: str, tmp_path: Path
) -> None:
    cfg = _write_config(tmp_path, db_dsn)
    result = _run(["add-account", "ghost", "--password", "x"], cfg)
    assert result.exit_code != 0
    assert "ghost" in result.output


def test_add_account_rejects_oauth_row(
    db_conn, db_dsn: str, tmp_path: Path
) -> None:
    _make_db_account(db_dsn, "gmail", auth="oauth2", oauth_provider="gmail")
    cfg = _write_config(tmp_path, db_dsn)
    result = _run(["add-account", "gmail", "--password", "x"], cfg)
    assert result.exit_code != 0
    assert "oauth-login" in result.output


def test_add_account_rejects_archive_row(
    db_conn, db_dsn: str, tmp_path: Path
) -> None:
    _make_db_account(db_dsn, "legacy", auth="archive")
    cfg = _write_config(tmp_path, db_dsn)
    result = _run(["add-account", "legacy", "--password", "x"], cfg)
    assert result.exit_code != 0
    assert "archive" in result.output.lower()


# ---------------------------------------------------------------------------
# Task 6: oauth-login
# ---------------------------------------------------------------------------

def _gmail_config(tmp_path: Path, dsn: str, body: str = "") -> Path:
    secrets_json = tmp_path / "client_secret.json"
    secrets_json.write_text("{}")
    return _write_config(
        tmp_path, dsn,
        f'[gmail_oauth]\nclient_secrets_file = "{secrets_json}"\n\n{body}',
    )


def test_oauth_login_stores_refresh_token(
    db_conn, db_dsn: str, tmp_path: Path, monkeypatch
) -> None:
    _make_db_account(db_dsn, "gmail", auth="oauth2", oauth_provider="gmail")
    cfg = _gmail_config(tmp_path, db_dsn)

    class _Creds:
        refresh_token = "refresh-123"

    monkeypatch.setattr("localmail.cli.run_consent_flow", lambda _f: _Creds())
    result = _run(["oauth-login", "gmail"], cfg)
    assert result.exit_code == 0, result.output
    from localmail import secrets as s
    assert s.get_refresh_token("gmail") == "refresh-123"


def test_oauth_login_rejects_password_row(
    db_conn, db_dsn: str, tmp_path: Path
) -> None:
    _make_db_account(db_dsn, "work")  # password account
    cfg = _gmail_config(tmp_path, db_dsn)
    result = _run(["oauth-login", "work"], cfg)
    assert result.exit_code != 0
    assert "oauth" in result.output.lower()


def test_oauth_login_seeds_from_toml_when_absent(
    db_conn, db_dsn: str, tmp_path: Path, monkeypatch
) -> None:
    """oauth-login on a name only in config.toml seeds the DB row, then stores."""
    cfg = _gmail_config(
        tmp_path, db_dsn,
        _toml_block("gmail", "gmail@example.com",
                    auth="oauth2", oauth_provider="gmail"),
    )

    class _Creds:
        refresh_token = "refresh-seed"

    monkeypatch.setattr("localmail.cli.run_consent_flow", lambda _f: _Creds())
    result = _run(["oauth-login", "gmail"], cfg)
    assert result.exit_code == 0, result.output
    with psycopg.connect(db_dsn) as conn:
        assert get_account_by_name(conn, "gmail") is not None  # row seeded
    from localmail import secrets as s
    assert s.get_refresh_token("gmail") == "refresh-seed"


# ---------------------------------------------------------------------------
# Task 7: remove-account
# ---------------------------------------------------------------------------

def test_remove_account_default_clears_secrets_keeps_row(
    db_conn, db_dsn: str, tmp_path: Path
) -> None:
    _make_db_account(db_dsn, "work")
    from localmail import secrets as s
    s.set_password("work", "pw")
    cfg = _write_config(tmp_path, db_dsn)
    result = _run(["remove-account", "work"], cfg)
    assert result.exit_code == 0, result.output
    assert s.get_password("work") is None              # secret cleared
    with psycopg.connect(db_dsn) as conn:
        assert get_account_by_name(conn, "work") is not None  # row survives


def test_remove_account_delete_row_removes_row(
    db_conn, db_dsn: str, tmp_path: Path
) -> None:
    _make_db_account(db_dsn, "work")
    cfg = _write_config(tmp_path, db_dsn)
    result = _run(["remove-account", "work", "--delete-row"], cfg)
    assert result.exit_code == 0, result.output
    with psycopg.connect(db_dsn) as conn:
        assert get_account_by_name(conn, "work") is None


def test_remove_account_delete_row_refuses_when_messages_without_force(
    db_conn, db_dsn: str, tmp_path: Path
) -> None:
    acct_id = _make_db_account(db_dsn, "work")
    with psycopg.connect(db_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO messages (account_id, raw_sha256, headers, "
            "  raw_bytes, size_bytes) "
            "VALUES (%s, %s, '{}'::jsonb, %s, 3)",
            (acct_id, b"\x00" * 32, b"abc"),
        )
        conn.commit()
    cfg = _write_config(tmp_path, db_dsn)
    result = _run(["remove-account", "work", "--delete-row"], cfg)
    assert result.exit_code != 0
    assert "force" in result.output.lower()
    # --force succeeds and cascades
    ok = _run(["remove-account", "work", "--delete-row", "--force"], cfg)
    assert ok.exit_code == 0, ok.output
    with psycopg.connect(db_dsn) as conn:
        assert get_account_by_name(conn, "work") is None


def test_remove_account_delete_row_missing_clears_keyring_only(
    db_conn, db_dsn: str, tmp_path: Path
) -> None:
    from localmail import secrets as s
    s.set_password("ghost", "pw")
    cfg = _write_config(tmp_path, db_dsn)
    result = _run(["remove-account", "ghost", "--delete-row"], cfg)
    assert result.exit_code == 0, result.output
    assert s.get_password("ghost") is None


def test_remove_account_force_without_delete_row_errors(
    db_conn, db_dsn: str, tmp_path: Path
) -> None:
    cfg = _write_config(tmp_path, db_dsn)
    result = _run(["remove-account", "work", "--force"], cfg)
    assert result.exit_code != 0


# ---------------------------------------------------------------------------
# Task 8: sync reads DB accounts
# ---------------------------------------------------------------------------

from contextlib import contextmanager

from . import _eml
from ._fake_imap import FakeIMAPClient


def _fake_open_connection_factory(imap):
    """Return a context-manager factory that always yields `imap`."""
    @contextmanager
    def _fake_open(account, **kw):  # noqa: ARG001
        yield imap
    return _fake_open


def test_sync_reads_db_accounts(
    db_conn, db_dsn: str, tmp_path: Path, monkeypatch
) -> None:
    """sync command must sync every syncable DB account (no TOML needed)."""
    from localmail import cli as cli_mod

    _make_db_account(db_dsn, "work")
    cfg = _write_config(tmp_path, db_dsn)  # no [[accounts]] in TOML

    imap = FakeIMAPClient()
    imap.add_folder("INBOX")
    imap.append("INBOX", _eml.plain())

    monkeypatch.setattr(cli_mod, "open_connection", _fake_open_connection_factory(imap))
    result = _run(["sync", "--no-ssl"], cfg)
    assert result.exit_code == 0, result.output
    assert "work" in result.output
    with psycopg.connect(db_dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM messages")
        assert cur.fetchone()[0] == 1


def test_sync_single_account_by_name(
    db_conn, db_dsn: str, tmp_path: Path, monkeypatch
) -> None:
    """--account NAME restricts sync to that one DB account."""
    from localmail import cli as cli_mod

    _make_db_account(db_dsn, "work")
    _make_db_account(db_dsn, "personal")
    cfg = _write_config(tmp_path, db_dsn)

    imap = FakeIMAPClient()
    imap.add_folder("INBOX")
    imap.append("INBOX", _eml.plain())

    monkeypatch.setattr(cli_mod, "open_connection", _fake_open_connection_factory(imap))
    result = _run(["sync", "--no-ssl", "--account", "work"], cfg)
    assert result.exit_code == 0, result.output
    assert "work" in result.output
    with psycopg.connect(db_dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM messages")
        assert cur.fetchone()[0] == 1


def test_sync_unknown_account_fails(
    db_conn, db_dsn: str, tmp_path: Path
) -> None:
    """--account NAME with an unknown name must exit non-zero."""
    cfg = _write_config(tmp_path, db_dsn)
    result = _run(["sync", "--account", "ghost"], cfg)
    assert result.exit_code != 0
    assert "ghost" in result.output


def test_sync_no_syncable_accounts_fails(
    db_conn, db_dsn: str, tmp_path: Path
) -> None:
    """sync with no syncable accounts (empty DB) must exit non-zero."""
    cfg = _write_config(tmp_path, db_dsn)
    result = _run(["sync"], cfg)
    assert result.exit_code != 0


def test_sync_account_override_syncs_paused_account(
    db_conn, db_dsn: str, tmp_path: Path, monkeypatch
) -> None:
    """--account NAME syncs even a paused (sync_enabled=FALSE) account."""
    from localmail import cli as cli_mod

    _make_db_account(db_dsn, "paused", sync_enabled=False)
    cfg = _write_config(tmp_path, db_dsn)

    imap = FakeIMAPClient()
    imap.add_folder("INBOX")
    imap.append("INBOX", _eml.plain())

    monkeypatch.setattr(cli_mod, "open_connection", _fake_open_connection_factory(imap))
    result = _run(["sync", "--no-ssl", "--account", "paused"], cfg)
    assert result.exit_code == 0, result.output
    with psycopg.connect(db_dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM messages")
        assert cur.fetchone()[0] == 1


def test_sync_rejects_archive_account(
    db_conn, db_dsn: str, tmp_path: Path
) -> None:
    """--account NAME pointing at an archive account must exit non-zero."""
    _make_db_account(db_dsn, "legacy", auth="archive")
    cfg = _write_config(tmp_path, db_dsn)
    result = _run(["sync", "--account", "legacy"], cfg)
    assert result.exit_code != 0
    assert "archive" in result.output.lower()
