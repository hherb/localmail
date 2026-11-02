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
import re
import threading
from collections.abc import Callable
from pathlib import Path

import psycopg
import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from localmail.api.admin import imports as imports_svc
from localmail.api.admin.csrf import make_csrf_token
from localmail.api.auth import hash_password
from localmail.cli import main
from localmail.config import ImportsConfig, ServeConfig
from localmail.serve.admin.csrf import csrf_action
from localmail.serve.app import create_app
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
    with caplog.at_level(logging.WARNING, logger=runner.__name__):
        with pytest.raises(runner.TerminalStatusUnwritten):
            _run(factory, jid, tmp_path, sleeps)
    assert calls[0] == 1 + _ATTEMPTS
    assert sleeps == [_BACKOFF_S] * (_ATTEMPTS - 1)
    assert _status(db_conn, jid) == "running"
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING
                and "retrying in" in r.getMessage()]
    assert len(warnings) == _ATTEMPTS - 1
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert errors[0].exc_info is not None
    msg = errors[0].getMessage()
    assert f"import job {jid}" in msg and "stays active" in msg
    assert "restart serve" in msg
    # The remedy records the outcome it could not write, not 'failed'.
    assert ("UPDATE import_jobs SET status='completed', processed=1, inserted=1, "
            "skipped_dup=0, failed=0, error_msg=NULL, finished_at=now() "
            f"WHERE id={jid}") in msg


def test_an_unopenable_worker_connection_still_writes_failed_once_the_db_returns(
    db_conn: psycopg.Connection, tmp_path: Path,
) -> None:
    jid = _job(db_conn, tmp_path)
    calls = [0]

    def factory() -> psycopg.Connection:
        calls[0] += 1
        if calls[0] <= _ATTEMPTS - 1:  # the worker conn + the first terminal-write try
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
    assert any("stays active" in r.getMessage() for r in caplog.records)


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


def _missing_source_job(conn: psycopg.Connection, tmp_path: Path) -> int:
    aid = _archive(conn)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO import_jobs (account_id, source_kind, source_path, status) "
            "VALUES (%s, 'mbox', %s, 'pending') RETURNING id",
            (aid, str(tmp_path / "does-not-exist.mbox")))
        row = cur.fetchone()
    conn.commit()
    assert row is not None
    return int(row[0])


def test_a_failed_import_retries_its_failed_write(
    db_conn: psycopg.Connection, tmp_path: Path,
) -> None:
    jid = _missing_source_job(db_conn, tmp_path)
    factory, _calls = _flaky(failures=1)
    sleeps: list[float] = []
    _run(factory, jid, tmp_path, sleeps)
    assert _status(db_conn, jid) == "failed"
    with db_conn.cursor() as cur:
        cur.execute("SELECT error_msg FROM import_jobs WHERE id=%s", (jid,))
        row = cur.fetchone()
    assert row is not None and row[0]
    assert sleeps == [_BACKOFF_S]


def test_an_exhausted_failed_write_carries_the_error_in_its_remedy(
    db_conn: psycopg.Connection, tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    jid = _missing_source_job(db_conn, tmp_path)
    factory, _calls = _flaky(failures=_ATTEMPTS)
    with caplog.at_level(logging.ERROR, logger=runner.__name__):
        with pytest.raises(runner.TerminalStatusUnwritten):
            _run(factory, jid, tmp_path, [])
    assert _status(db_conn, jid) == "running"
    msg = next(r.getMessage() for r in caplog.records
               if "could not write terminal status" in r.getMessage())
    assert "SET status='failed'" in msg
    assert "error_msg='" in msg and "does-not-exist.mbox'" in msg


def test_the_remedy_sql_quotes_the_error_message() -> None:
    sql = runner._terminal_sql(7, "failed", runner._Counters(), "it's broken")
    assert "error_msg='it''s broken'" in sql


def test_zero_attempts_is_refused_rather_than_writing_nothing(tmp_path: Path) -> None:
    def never() -> psycopg.Connection:
        raise AssertionError("must refuse before connecting")
    with pytest.raises(ValueError, match="terminal_write_attempts"):
        runner.run_import(
            never, 1, attachments_root=tmp_path, checkpoint_every=50,
            checkpoint_seconds=3600, terminal_write_attempts=0,
            terminal_write_backoff_s=0)


def test_the_serve_thread_still_surfaces_any_other_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[type[BaseException]] = []
    monkeypatch.setattr(threading, "excepthook", lambda a: seen.append(a.exc_type))

    def boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("not a terminal-write failure")
    monkeypatch.setattr(runner, "run_import", boom)
    t = imports_svc.start_job(
        _real, 1, attachments_root=Path("/nonexistent"), checkpoint_every=50,
        checkpoint_seconds=3600, terminal_write_attempts=1,
        terminal_write_backoff_s=0)
    t.join(timeout=30)
    assert seen == [RuntimeError]


# --- The configured tunables reach run_import / start_job (#442 review). ---
# Values differ from the defaults and from 1, so a hard-coded value fails.
_CFG_ATTEMPTS = 4
_CFG_BACKOFF_S = 0.5


def test_cli_import_passes_the_configured_tunables(
    db_conn: psycopg.Connection, tmp_path: Path, cli_config: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _archive(db_conn)
    cli_config.write_text(
        cli_config.read_text() + f"[imports]\nterminal_write_attempts = {_CFG_ATTEMPTS}\n"
        f"terminal_write_backoff_s = {_CFG_BACKOFF_S}\n")
    got: dict[str, object] = {}
    monkeypatch.setattr(runner, "run_import", lambda *_a, **k: got.update(k))
    result = CliRunner().invoke(
        main, ["import", _mbox(tmp_path), "--account", "arch", "--kind", "mbox"])
    assert result.exit_code == 0, result.output
    assert got["terminal_write_attempts"] == _CFG_ATTEMPTS
    assert got["terminal_write_backoff_s"] == _CFG_BACKOFF_S


_KEY = "x" * 43


@pytest.fixture
def admin_client(
    db_dsn: str, db_conn: psycopg.Connection, tmp_path: Path,
) -> tuple[TestClient, int, Path]:
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO api_users (username, password_hash, is_admin) "
            "VALUES ('horst', %s, TRUE) RETURNING id", (hash_password("pw"),))
        row = cur.fetchone()
    db_conn.commit()
    assert row is not None
    root = tmp_path / "imports"
    root.mkdir()
    app = create_app(
        db_dsn=db_dsn,
        serve_config=ServeConfig(session_signing_key=_KEY, state_signing_key="y" * 43,
                                 cookie_secure=False),
        imports_config=ImportsConfig(
            roots=[root], terminal_write_attempts=_CFG_ATTEMPTS,
            terminal_write_backoff_s=_CFG_BACKOFF_S),
        attachments_root=tmp_path / "blobs")
    c = TestClient(app, follow_redirects=False)
    m = re.search(r'name="csrf_token"\s+value="([^"]+)"', c.get("/admin/login").text)
    assert m is not None
    c.post("/admin/login", data={"username": "horst", "password": "pw",
                                 "csrf_token": m.group(1)})
    return c, int(row[0]), root


def _csrf(user_id: int, url: str) -> str:
    return make_csrf_token(
        user_id=user_id, action=csrf_action("POST", url), key=_KEY.encode())


def _spy_start_job(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    got: dict[str, object] = {}
    monkeypatch.setattr(imports_svc, "start_job", lambda *_a, **k: got.update(k))
    return got


def _mbox_under(root: Path) -> str:
    box_path = root / "a.mbox"
    box = _mailbox.mbox(str(box_path))
    box.lock()
    box.add(_mailbox.mboxMessage(_eml.plain()))
    box.flush()
    box.unlock()
    return str(box_path)


def test_the_json_route_passes_the_configured_tunables(
    admin_client: tuple[TestClient, int, Path], db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    c, uid, root = admin_client
    aid = _archive(db_conn)
    got = _spy_start_job(monkeypatch)
    r = c.post("/v1/admin/imports", json={
        "account_id": str(aid), "source_kind": "mbox", "source_path": _mbox_under(root)},
        headers={"X-CSRF-Token": _csrf(uid, "/v1/admin/imports")})
    assert r.status_code == 201, r.text
    assert got["terminal_write_attempts"] == _CFG_ATTEMPTS
    assert got["terminal_write_backoff_s"] == _CFG_BACKOFF_S


def test_the_panel_route_passes_the_configured_tunables(
    admin_client: tuple[TestClient, int, Path], db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    c, uid, root = admin_client
    aid = _archive(db_conn)
    got = _spy_start_job(monkeypatch)
    r = c.post("/admin/imports", data={
        "account_id": str(aid), "source_kind": "mbox", "source_path": _mbox_under(root)},
        headers={"X-CSRF-Token": _csrf(uid, "/admin/imports")})
    assert r.status_code == 200, r.text
    assert r.headers["HX-Redirect"].startswith("/admin/imports/")
    assert got["terminal_write_attempts"] == _CFG_ATTEMPTS
    assert got["terminal_write_backoff_s"] == _CFG_BACKOFF_S
