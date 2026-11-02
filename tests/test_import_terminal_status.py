# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""#442: a failing terminal ``import_jobs`` write is retried, then reported.

Before the fix ``_mark_terminal`` tried once. A failure escaped
``run_import``: in serve the in-thread dump on stderr was the only record,
and the row stayed ``running``, holding the single-active busy-guard until a
serve restart. A failed *completed* write was also retried as ``failed``,
mislabelling an import that had finished.
"""
from __future__ import annotations

import logging
import mailbox as _mailbox
from collections.abc import Callable
from pathlib import Path

import psycopg
import pytest
from click.testing import CliRunner

from localmail.api.admin import imports as imports_svc
from localmail.cli import main
from localmail.config import ImportsConfig
from localmail.importer import runner
from tests import _eml
from tests.conftest import TEST_DSN

_ATTEMPTS = 3
_BACKOFF_S = 0.25


def _real() -> psycopg.Connection:
    return psycopg.connect(TEST_DSN, autocommit=False)


def _flaky(failures: int) -> tuple[Callable[[], psycopg.Connection], list[int]]:
    """A factory whose 2nd..(failures+1)th calls raise: the worker connection
    opens, then the terminal write's first `failures` connections fail."""
    calls = [0]

    def factory() -> psycopg.Connection:
        calls[0] += 1
        if 2 <= calls[0] <= failures + 1:
            raise psycopg.OperationalError("connection refused (test)")
        return _real()
    return factory, calls


def _archive(conn: psycopg.Connection) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO accounts (name, email_address, auth_method, imap_host, "
            "imap_port, config) "
            "VALUES ('arch', 'a@b.test', 'archive', NULL, NULL, '{}') RETURNING id")
        row = cur.fetchone()
    conn.commit()
    assert row is not None
    return int(row[0])


def _mbox(tmp_path: Path) -> str:
    p = tmp_path / "a.mbox"
    box = _mailbox.mbox(str(p))
    box.lock()
    box.add(_mailbox.mboxMessage(_eml.plain()))
    box.flush()
    box.unlock()
    return str(p)


def _job(conn: psycopg.Connection, tmp_path: Path) -> int:
    aid = _archive(conn)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO import_jobs (account_id, source_kind, source_path, status) "
            "VALUES (%s, 'mbox', %s, 'pending') RETURNING id", (aid, _mbox(tmp_path)))
        row = cur.fetchone()
    conn.commit()
    assert row is not None
    return int(row[0])


def _status(conn: psycopg.Connection, jid: int) -> str:
    conn.rollback()
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM import_jobs WHERE id=%s", (jid,))
        row = cur.fetchone()
    assert row is not None
    return str(row[0])


def _run(factory: Callable[[], psycopg.Connection], jid: int, tmp_path: Path,
         sleeps: list[float]) -> None:
    runner.run_import(
        factory, jid, attachments_root=tmp_path / "blobs", checkpoint_every=50,
        checkpoint_seconds=3600, terminal_write_attempts=_ATTEMPTS,
        terminal_write_backoff_s=_BACKOFF_S, sleep=sleeps.append)


def test_a_transient_failure_of_the_terminal_write_is_retried(
    db_conn: psycopg.Connection, tmp_path: Path,
) -> None:
    jid = _job(db_conn, tmp_path)
    factory, _calls = _flaky(failures=_ATTEMPTS - 1)
    sleeps: list[float] = []
    _run(factory, jid, tmp_path, sleeps)
    assert _status(db_conn, jid) == "completed"
    assert sleeps == [_BACKOFF_S] * (_ATTEMPTS - 1)


def test_an_exhausted_terminal_write_is_logged_and_raised_not_relabelled(
    db_conn: psycopg.Connection, tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    jid = _job(db_conn, tmp_path)
    # One more failure than attempts: a retry of the write as "failed" (the
    # old outer handler) would then succeed and mislabel a finished import.
    factory, calls = _flaky(failures=_ATTEMPTS + 1)
    sleeps: list[float] = []
    with caplog.at_level(logging.ERROR, logger=runner.__name__):
        with pytest.raises(runner.TerminalStatusUnwritten):
            _run(factory, jid, tmp_path, sleeps)
    assert calls[0] == 1 + _ATTEMPTS
    assert _status(db_conn, jid) == "running"
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    msg = errors[0].getMessage()
    assert f"import job {jid}" in msg and "'completed'" in msg
    assert "stays 'running'" in msg and "restart serve" in msg


def test_an_unopenable_worker_connection_still_writes_failed_once_the_db_returns(
    db_conn: psycopg.Connection, tmp_path: Path,
) -> None:
    jid = _job(db_conn, tmp_path)
    calls = [0]

    def factory() -> psycopg.Connection:
        calls[0] += 1
        if calls[0] <= _ATTEMPTS - 1:  # the worker conn + the first retries
            raise psycopg.OperationalError("connection refused (test)")
        return _real()
    sleeps: list[float] = []
    _run(factory, jid, tmp_path, sleeps)
    assert _status(db_conn, jid) == "failed"


def test_the_serve_thread_reports_an_exhausted_write_without_an_unhandled_exception(
    db_conn: psycopg.Connection, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An unhandled thread exception fails the suite on its own
    # (filterwarnings = error::PytestUnhandledThreadExceptionWarning).
    jid = _job(db_conn, tmp_path)

    def refuse(*_a: object, **_k: object) -> None:
        raise psycopg.OperationalError("connection refused (test)")
    monkeypatch.setattr(runner, "_write_terminal", refuse)
    with caplog.at_level(logging.ERROR, logger=runner.__name__):
        t = imports_svc.start_job(
            _real, jid, attachments_root=tmp_path / "blobs", checkpoint_every=50,
            checkpoint_seconds=3600, terminal_write_attempts=1,
            terminal_write_backoff_s=0)
        t.join(timeout=30)
    assert not t.is_alive()
    assert _status(db_conn, jid) == "running"
    assert any("stays 'running'" in r.getMessage() for r in caplog.records)


def test_cli_import_reports_an_exhausted_write_in_one_line(
    db_conn: psycopg.Connection, tmp_path: Path, cli_config: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _archive(db_conn)
    cli_config.write_text(
        cli_config.read_text()
        + "[imports]\nterminal_write_attempts = 1\nterminal_write_backoff_s = 0\n")

    def refuse(*_a: object, **_k: object) -> None:
        raise psycopg.OperationalError("connection refused (test)")
    monkeypatch.setattr(runner, "_write_terminal", refuse)
    result = CliRunner().invoke(
        main, ["import", _mbox(tmp_path), "--account", "arch", "--kind", "mbox"])
    assert result.exit_code == 1, result.output
    assert isinstance(result.exception, SystemExit)
    assert "could not write terminal status" in result.stderr


def test_the_retry_tunables_are_floored() -> None:
    with pytest.raises(ValueError):
        ImportsConfig(terminal_write_attempts=0)
    with pytest.raises(ValueError):
        ImportsConfig(terminal_write_backoff_s=-1)
