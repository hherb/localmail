# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""The embed worker and language detection do not blame a row for contention (#425).

Since #424 every pool connection carries `statement_timeout`, so a statement
that waited on another session can fail with `QueryCanceled` (or deadlock, or
`LockNotAvailable`). Sync re-raises those; these three per-item failure paths
used to record them instead: a `failed_chunkings` row, a `failed_embeddings`
row, and a language "decline" stamp. Each of those takes the item out of its
queue for something that was not its fault.

The error is raised by the server, through a trigger, rather than by a patched
Python function: the embedding UPDATE has no Python seam inside its savepoint,
and a server-side failure is also what aborts the transaction for real, so the
`ROLLBACK TO SAVEPOINT` that precedes the re-raise is exercised too. A
`DataException` through the same trigger is the positive control at every site: a
re-raise of everything would pass the contention cases alone.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator

import psycopg
import pytest

from localmail.config import SearchConfig
from localmail.db import DB_CONTENTION_ERRORS
from localmail.search.embed_worker import run_embed_worker_once
from localmail.search.lang_detect import FixedDetector, run_lang_detect_pass

from .test_embed_worker import _seed_message, _StaticEmbedder

_TRIGGER_FN = "localmail_test_raise_425"
# Not `DataError.sqlstate`: that is the DB-API base class, whose sqlstate is
# None, so the trigger raised `UndefinedObject` for the code 'None' instead.
_POISON_SQLSTATE = psycopg.errors.DataException.sqlstate
assert _POISON_SQLSTATE and all(exc.sqlstate for exc in DB_CONTENTION_ERRORS)

#: (sqlstate, expected exception or None when the item is recorded instead).
_CASES = [
    pytest.param(exc.sqlstate, exc, id=exc.__name__) for exc in DB_CONTENTION_ERRORS
] + [pytest.param(_POISON_SQLSTATE, None, id="DataException-is-still-recorded")]


@contextlib.contextmanager
def _raising_trigger(
    conn: psycopg.Connection, *, table: str, event: str, sqlstate: str,
    when: str = "TRUE",
) -> Iterator[None]:
    """A trigger raising `sqlstate` on `event` of `table`, dropped on exit.

    The workers commit, so the trigger must be committed to be seen by them,
    and must therefore be removed explicitly rather than by a rollback. The
    setup replaces whatever a killed run left behind, since `TRUNCATE` does not.
    """
    conn.execute(f"DROP TRIGGER IF EXISTS raise_425 ON {table}")
    conn.execute(
        f"CREATE OR REPLACE FUNCTION {_TRIGGER_FN}() RETURNS trigger LANGUAGE plpgsql AS"
        f" $$BEGIN RAISE EXCEPTION 'simulated' USING ERRCODE = '{sqlstate}'; END$$"
    )
    conn.execute(
        f"CREATE TRIGGER raise_425 BEFORE {event} ON {table}"
        f" FOR EACH ROW WHEN ({when}) EXECUTE FUNCTION {_TRIGGER_FN}()"
    )
    conn.commit()
    try:
        yield
    finally:
        conn.rollback()
        conn.execute(f"DROP TRIGGER IF EXISTS raise_425 ON {table}")
        conn.execute(f"DROP FUNCTION IF EXISTS {_TRIGGER_FN}()")
        conn.commit()


def _count(conn: psycopg.Connection, sql: str) -> int:
    row = conn.execute(sql).fetchone()
    assert row is not None
    return row[0]


@pytest.mark.parametrize(("sqlstate", "raised"), _CASES)
def test_message_chunking_records_only_a_genuine_failure(
    db_conn: psycopg.Connection, sqlstate: str, raised,
) -> None:
    _seed_message(db_conn, body="a body that chunks")
    with _raising_trigger(
        db_conn, table="message_chunks", event="INSERT", sqlstate=sqlstate,
    ):
        if raised is None:
            run_embed_worker_once(db_conn, SearchConfig(), _StaticEmbedder())
        else:
            with pytest.raises(raised):
                run_embed_worker_once(db_conn, SearchConfig(), _StaticEmbedder())
    expected = 1 if raised is None else 0
    assert _count(db_conn, "SELECT count(*) FROM failed_chunkings") == expected


@pytest.mark.parametrize(("sqlstate", "raised"), _CASES)
def test_chunk_embedding_records_only_a_genuine_failure(
    db_conn: psycopg.Connection, sqlstate: str, raised,
) -> None:
    """A contention error here is caught one level up, by `_embed_table`'s
    batch-level handler: the sweep embeds nothing and the chunk stays
    claimable, which is the transient-backend path, not the poison one."""
    _seed_message(db_conn, body="a body that chunks")
    with _raising_trigger(
        db_conn, table="message_chunks", event="UPDATE", sqlstate=sqlstate,
    ):
        sweep = run_embed_worker_once(db_conn, SearchConfig(), _StaticEmbedder())
    assert sweep.embedded == 0
    expected = 1 if raised is None else 0
    assert _count(db_conn, "SELECT count(*) FROM failed_embeddings") == expected
    assert _count(
        db_conn, "SELECT count(*) FROM message_chunks WHERE embedding_v1 IS NULL",
    ) >= 1


@pytest.mark.parametrize(("sqlstate", "raised"), _CASES)
def test_language_detection_does_not_decline_a_contended_row(
    db_conn: psycopg.Connection, sqlstate: str, raised,
) -> None:
    """The trigger fires only on the labelling write, so the fallback stamp in
    `_mark_attempted_safely` succeeds — which is exactly how a contended row
    used to leave the queue looking like a detector decline."""
    mid = _seed_message(db_conn, body="ein deutscher Text")
    detector = FixedDetector({"ein deutscher Text": "de"})
    with _raising_trigger(
        db_conn, table="messages", event="UPDATE", sqlstate=sqlstate,
        when="NEW.body_lang IS NOT NULL",
    ):
        if raised is None:
            run_lang_detect_pass(db_conn, SearchConfig(), detector)
        else:
            with pytest.raises(raised):
                run_lang_detect_pass(db_conn, SearchConfig(), detector)
    row = db_conn.execute(
        "SELECT body_lang, body_lang_attempted_at FROM messages WHERE id = %s",
        (mid,),
    ).fetchone()
    assert row is not None
    assert row[0] is None
    if raised is None:
        assert row[1] is not None  # poison: stamped, leaves the queue
    else:
        assert row[1] is None  # contention: still claimable next sweep


def test_a_contended_language_pass_does_not_cost_the_sweep_its_embeddings(
    db_conn: psycopg.Connection,
) -> None:
    """Language detection runs after both embedding steps, so a lang write
    that keeps timing out stalls only itself: the chunks embedded earlier in
    the same sweep were committed before it raised."""
    _seed_message(db_conn, body="ein deutscher Text")
    detector = FixedDetector({"ein deutscher Text": "de"})
    with _raising_trigger(
        db_conn, table="messages", event="UPDATE",
        sqlstate=psycopg.errors.QueryCanceled.sqlstate,
        when="NEW.body_lang IS NOT NULL",
    ), pytest.raises(psycopg.errors.QueryCanceled):
        run_embed_worker_once(
            db_conn, SearchConfig(), _StaticEmbedder(), lang_detector=detector,
        )
    assert _count(
        db_conn, "SELECT count(*) FROM message_chunks WHERE embedding_v1 IS NOT NULL",
    ) >= 1
