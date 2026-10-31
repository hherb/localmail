# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""INTERNALDATE is stored as the instant the server reported (#415).

imapclient's default ``normalise_times=True`` turns INTERNALDATE into a
*naive* datetime in the host's local time. psycopg sends a naive value as
``timestamp`` and Postgres reads it in the *session* time zone, so the stored
instant was off by (host offset - session offset): +10/+11 h on the DGX
(Sydney host, UTC session), +2 h on the Mac (Brisbane host, Perth session).

``FakeIMAPClient`` returns aware datetimes, i.e. it behaves like
``normalise_times=False``, which is why no sync test ever saw this.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
import pytest
from click.testing import CliRunner
from imapclient.response_parser import parse_fetch_response

from localmail import imap_client
from localmail.cli import main
from localmail.imap_dates import aware_internal_date
from localmail.sync import backfill_internal_date

from . import _eml
from ._fake_imap import FakeIMAPClient
from .test_cli_accounts_db import _make_db_account, _write_config
from .test_sync import _sync, make_account

_UTC_WHEN = datetime(2026, 10, 29, 15, 24, 42, tzinfo=timezone.utc)
_WRONG_BY = timedelta(hours=11)


# --- the pure guard ----------------------------------------------------------


def test_an_aware_datetime_is_kept() -> None:
    assert aware_internal_date(_UTC_WHEN) == _UTC_WHEN


def test_a_naive_datetime_is_refused() -> None:
    # A naive value has no zone; storing it lets the session zone pick one.
    assert aware_internal_date(_UTC_WHEN.replace(tzinfo=None)) is None


@pytest.mark.parametrize("value", [None, b"29-Oct-2026 15:24:42 +0000", 0])
def test_a_non_datetime_is_absent(value: object) -> None:
    assert aware_internal_date(value) is None


def test_imapclient_without_normalising_yields_the_reported_instant() -> None:
    """The library half: what the guard is fed once normalising is off."""
    parsed = parse_fetch_response(
        [b'1 (UID 7 INTERNALDATE "30-Oct-2026 02:24:42 +1100")'],
        normalise_times=False,
    )
    value = aware_internal_date(parsed[7][b"INTERNALDATE"])
    assert value == _UTC_WHEN


# --- the connection ----------------------------------------------------------


class _RecordingClient:
    def __init__(self, **kw: object) -> None:
        self.normalise_times = True

    def login(self, *a: object) -> None:
        pass

    def logout(self) -> None:
        pass


def test_open_connection_turns_off_time_normalising(monkeypatch, memory_keyring) -> None:
    from localmail import secrets

    secrets.set_password("acct", "pw")
    monkeypatch.setattr(imap_client, "IMAPClient", _RecordingClient)
    with imap_client.open_connection(make_account()) as client:
        assert client.normalise_times is False


# --- sync --------------------------------------------------------------------


def _internal_dates(conn: psycopg.Connection) -> list[datetime | None]:
    with conn.cursor() as cur:
        cur.execute("SELECT internal_date FROM messages ORDER BY id")
        return [r[0] for r in cur.fetchall()]


def test_sync_stores_a_naive_internaldate_as_null(db_conn, tmp_path: Path) -> None:
    imap = FakeIMAPClient()
    imap.add_folder("INBOX")
    imap.append("INBOX", _eml.plain(), internal_date=_UTC_WHEN.replace(tzinfo=None))
    _sync(db_conn, imap, account=make_account(), attachments_root=tmp_path)
    assert _internal_dates(db_conn) == [None]


# --- backfill --refetch ------------------------------------------------------


def _account_id(conn: psycopg.Connection) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM accounts WHERE name = 'acct'")
        row = cur.fetchone()
    assert row is not None
    return int(row[0])


def _corrupt_all(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        cur.execute("UPDATE messages SET internal_date = internal_date + %s", (_WRONG_BY,))
    conn.commit()


def test_refetch_overwrites_a_wrong_stored_value(db_conn, tmp_path: Path) -> None:
    imap = FakeIMAPClient()
    imap.add_folder("INBOX")
    imap.append("INBOX", _eml.plain(), internal_date=_UTC_WHEN)
    _sync(db_conn, imap, account=make_account(), attachments_root=tmp_path)
    _corrupt_all(db_conn)

    result = backfill_internal_date(db_conn, imap, account_id=_account_id(db_conn), refetch=True)

    assert (result.scanned, result.updated, result.missing) == (1, 1, 0)
    assert _internal_dates(db_conn) == [_UTC_WHEN]


def test_without_refetch_a_populated_value_is_left_alone(db_conn, tmp_path: Path) -> None:
    imap = FakeIMAPClient()
    imap.add_folder("INBOX")
    imap.append("INBOX", _eml.plain(), internal_date=_UTC_WHEN)
    _sync(db_conn, imap, account=make_account(), attachments_root=tmp_path)
    _corrupt_all(db_conn)

    result = backfill_internal_date(db_conn, imap, account_id=_account_id(db_conn))

    assert (result.scanned, result.updated) == (0, 0)
    assert _internal_dates(db_conn) == [_UTC_WHEN + _WRONG_BY]


def test_refetch_of_a_correct_value_counts_no_update(db_conn, tmp_path: Path) -> None:
    imap = FakeIMAPClient()
    imap.add_folder("INBOX")
    imap.append("INBOX", _eml.plain(), internal_date=_UTC_WHEN)
    _sync(db_conn, imap, account=make_account(), attachments_root=tmp_path)

    result = backfill_internal_date(db_conn, imap, account_id=_account_id(db_conn), refetch=True)

    assert (result.scanned, result.updated, result.missing) == (1, 0, 0)


def test_refetch_reports_a_message_the_server_no_longer_has(db_conn, tmp_path: Path) -> None:
    imap = FakeIMAPClient()
    imap.add_folder("INBOX")
    imap.append("INBOX", _eml.plain(), internal_date=_UTC_WHEN)
    _sync(db_conn, imap, account=make_account(), attachments_root=tmp_path)
    _corrupt_all(db_conn)
    imap.folders["INBOX"].messages.clear()  # expunged since sync

    result = backfill_internal_date(db_conn, imap, account_id=_account_id(db_conn), refetch=True)

    assert (result.scanned, result.updated, result.missing) == (1, 0, 1)
    assert _internal_dates(db_conn) == [_UTC_WHEN + _WRONG_BY]


def test_refetch_takes_the_earliest_date_across_mailboxes(db_conn, tmp_path: Path) -> None:
    """One message row, two labels: the earliest arrival is the one kept."""
    raw = _eml.plain()
    later = _UTC_WHEN + timedelta(days=3)
    imap = FakeIMAPClient()
    imap.add_folder("INBOX")
    imap.add_folder("Work")
    imap.append("INBOX", raw, internal_date=later)
    imap.append("Work", raw, internal_date=_UTC_WHEN)
    _sync(db_conn, imap, account=make_account(), attachments_root=tmp_path)
    _corrupt_all(db_conn)

    result = backfill_internal_date(db_conn, imap, account_id=_account_id(db_conn), refetch=True)

    assert (result.scanned, result.updated, result.missing) == (1, 1, 0)
    assert _internal_dates(db_conn) == [_UTC_WHEN]


# --- the CLI -----------------------------------------------------------------


def _fake_open(imap: FakeIMAPClient):
    @contextmanager
    def _open(account, **kw):  # noqa: ARG001
        yield imap
    return _open


def test_cli_backfill_reads_db_accounts_and_refetches(
    db_conn, db_dsn: str, tmp_path: Path, monkeypatch, memory_keyring,
) -> None:
    """Both deployments carry an account in the DB that config.toml lacks."""
    from localmail import cli as cli_mod

    account_id = _make_db_account(db_dsn, "work")
    cfg = _write_config(tmp_path, db_dsn)  # no [[accounts]] in TOML
    imap = FakeIMAPClient()
    imap.add_folder("INBOX")
    imap.append("INBOX", _eml.plain(), internal_date=_UTC_WHEN)
    monkeypatch.setattr(cli_mod, "open_connection", _fake_open(imap))
    runner = CliRunner()
    assert runner.invoke(main, ["--config", str(cfg), "sync", "--no-ssl"]).exit_code == 0
    _corrupt_all(db_conn)

    result = runner.invoke(
        main, ["--config", str(cfg), "backfill-internal-date", "--refetch", "--no-ssl"],
    )

    assert result.exit_code == 0, result.output
    assert "work: 1/1 updated, 0 not on the server" in result.stdout
    with db_conn.cursor() as cur:
        cur.execute("SELECT internal_date FROM messages WHERE account_id = %s", (account_id,))
        assert [r[0] for r in cur.fetchall()] == [_UTC_WHEN]


def test_cli_backfill_rejects_an_archive_account(db_conn, db_dsn: str, tmp_path: Path) -> None:
    _make_db_account(db_dsn, "legacy", auth="archive")
    cfg = _write_config(tmp_path, db_dsn)
    result = CliRunner().invoke(
        main, ["--config", str(cfg), "backfill-internal-date", "--account", "legacy"],
    )
    assert result.exit_code != 0
    assert "archive" in result.output.lower()

