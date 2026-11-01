# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""A statement that failed because of another session is not a broken row (#414).

Bounding the daemon pool with `statement_timeout` made one such failure
routine: the IDLE and poll threads insert the same Message-Id (and the same
global attachment blob) concurrently, and the loser waits until the winner's
whole 50-message batch commits. Past the timeout that wait raises
`QueryCanceled`, which sync's per-message SAVEPOINT used to record in
`failed_messages` as a poison pill and then advance `uidnext` past: healthy
mail in `list-failed`, the #231 pollution. It now aborts the pass instead, and
the next pass resumes from the committed checkpoint.
"""

from __future__ import annotations

from pathlib import Path

import psycopg
import pytest

from localmail import sync as sync_mod
from localmail.db import DB_CONTENTION_ERRORS, bounded_connect_kwargs
from localmail.parser import parse_message
from localmail.search.extract_worker import _is_transient

from . import _eml
from ._fake_imap import FakeIMAPClient
from .test_sync import _ensure_account, _sync, make_account


def _count(conn: psycopg.Connection, sql: str) -> int:
    row = conn.execute(sql).fetchone()
    assert row is not None
    return row[0]


def test_a_lock_wait_past_the_timeout_aborts_the_pass_not_the_message(
    db_conn: psycopg.Connection, db_dsn: str, tmp_path: Path,
) -> None:
    """The real shape: a second session holds an uncommitted insert of the
    same message, as the sibling sync thread does for the rest of its batch."""
    account = make_account()
    account_id = _ensure_account(db_conn, account)
    db_conn.commit()
    raw = _eml.plain()
    imap = FakeIMAPClient()
    imap.add_folder("INBOX")
    imap.append("INBOX", raw)

    with psycopg.connect(db_dsn, autocommit=False) as winner:
        sync_mod.upsert_message(
            winner, account_id=account_id, parsed=parse_message(raw),
            internal_date=None,
        )
        db_conn.execute("SET statement_timeout = '300ms'")
        with pytest.raises(psycopg.errors.QueryCanceled):
            _sync(db_conn, imap, account=account, attachments_root=tmp_path)
        db_conn.rollback()
        winner.rollback()

    assert _count(db_conn, "SELECT count(*) FROM failed_messages") == 0
    assert _count(db_conn, "SELECT count(*) FROM messages") == 0
    # Not checkpointed past the message, so the next pass fetches it again.
    assert _count(
        db_conn, "SELECT count(*) FROM mailboxes WHERE uidnext > 1"
    ) == 0


@pytest.mark.parametrize("exc_type", DB_CONTENTION_ERRORS)
def test_every_contention_error_aborts_the_pass(
    db_conn: psycopg.Connection, tmp_path: Path, monkeypatch, exc_type,
) -> None:
    imap = FakeIMAPClient()
    imap.add_folder("INBOX")
    imap.append("INBOX", _eml.plain())

    def contended(*a, **kw):
        raise exc_type("simulated")

    monkeypatch.setattr(sync_mod, "upsert_message", contended)
    with pytest.raises(exc_type):
        _sync(db_conn, imap, account=make_account(), attachments_root=tmp_path)
    db_conn.rollback()
    assert _count(db_conn, "SELECT count(*) FROM failed_messages") == 0


def test_a_genuine_poison_pill_is_still_recorded(
    db_conn: psycopg.Connection, tmp_path: Path, monkeypatch,
) -> None:
    """Positive control: the re-raise is for contention only. A rule that
    re-raised everything would pass every test above."""
    imap = FakeIMAPClient()
    imap.add_folder("INBOX")
    imap.append("INBOX", _eml.plain())

    def broken(*a, **kw):
        raise psycopg.errors.DataError("bad row")

    monkeypatch.setattr(sync_mod, "upsert_message", broken)
    _sync(db_conn, imap, account=make_account(), attachments_root=tmp_path)
    assert _count(db_conn, "SELECT count(*) FROM failed_messages") == 1


@pytest.mark.parametrize("exc_type", DB_CONTENTION_ERRORS)
def test_extraction_treats_contention_as_transient(exc_type) -> None:
    """Not the blob's fault, so the #153 transient budget, not retry_count."""
    assert _is_transient(exc_type("simulated"))
    assert not _is_transient(psycopg.errors.DataError("bad row"))


def _kwargs(dsn: str) -> dict:
    return bounded_connect_kwargs(
        dsn, connect_timeout_s=10, tcp_user_timeout_ms=30000, statement_timeout_s=30,
    )


@pytest.mark.parametrize(
    "dsn",
    [
        "postgresql://u@h/db?options=-c%20search_path%3Dx",
        "host=h dbname=db options='-c search_path=x'",
    ],
)
def test_an_operator_dsn_options_survives_the_bounds(dsn: str, monkeypatch) -> None:
    monkeypatch.delenv("PGOPTIONS", raising=False)
    assert _kwargs(dsn)["options"] == "-c search_path=x -c statement_timeout=30s"


def test_pgoptions_is_kept_when_the_dsn_has_none(monkeypatch) -> None:
    monkeypatch.setenv("PGOPTIONS", "-c search_path=y")
    assert _kwargs("postgresql://u@h/db")["options"] == (
        "-c search_path=y -c statement_timeout=30s"
    )


def test_the_dsn_options_win_over_pgoptions_as_in_libpq(monkeypatch) -> None:
    monkeypatch.setenv("PGOPTIONS", "-c search_path=y")
    dsn = "postgresql://u@h/db?options=-c%20search_path%3Dx"
    assert _kwargs(dsn)["options"] == "-c search_path=x -c statement_timeout=30s"


def test_no_operator_options_means_only_ours(monkeypatch) -> None:
    monkeypatch.delenv("PGOPTIONS", raising=False)
    assert _kwargs("postgresql://u@h/db")["options"] == "-c statement_timeout=30s"


def test_the_server_sees_both_the_operator_option_and_the_bound(
    db_dsn: str, monkeypatch,
) -> None:
    monkeypatch.delenv("PGOPTIONS", raising=False)
    dsn = psycopg.conninfo.make_conninfo(db_dsn, options="-c work_mem=7MB")
    with psycopg.connect(dsn, **_kwargs(dsn)) as conn:
        work_mem = conn.execute("SHOW work_mem").fetchone()
        timeout = conn.execute("SHOW statement_timeout").fetchone()
    assert work_mem == ("7MB",)
    assert timeout == ("30s",)
