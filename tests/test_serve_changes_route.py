from datetime import datetime, timedelta, timezone

import psycopg
from fastapi.testclient import TestClient

from localmail.config import ServeConfig
from localmail.serve.app import create_app

# Tests insert + read messages within the same millisecond; the production
# safe-horizon would mask every just-seeded row. Drop it to 0 so the test
# assertions cover the cursor logic itself, not the horizon.
_TEST_SERVE_CFG = ServeConfig(changes_safe_horizon_s=0)


def _ensure_account(conn: psycopg.Connection) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO accounts (name, email_address, imap_host, auth_method) "
            "VALUES ('a','x@y.test','imap.x','password') "
            "ON CONFLICT (name) DO UPDATE SET name = EXCLUDED.name RETURNING id"
        )
        row = cur.fetchone(); assert row is not None
        return row[0]


def _seed_msg(conn: psycopg.Connection, when: datetime, suffix: str) -> int:
    aid = _ensure_account(conn)
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO messages (account_id, message_id, subject, raw_bytes, raw_sha256,
                                     size_bytes, headers, attachments, date_sent, date_received)
               VALUES (%s, %s, 'x', 'r', %s, 1, '{}'::jsonb, '[]'::jsonb, %s, %s) RETURNING id""",
            (aid, f"<{suffix}@x>", bytes.fromhex(suffix * 32), when, when),
        )
        row = cur.fetchone(); assert row is not None
        return row[0]


def test_changes_no_cursor_returns_recent(
    db_dsn: str, api_token: str, db_conn, grant_alice_all_accounts,
) -> None:
    now = datetime.now(timezone.utc)
    _seed_msg(db_conn, now - timedelta(hours=2), "aa")
    _seed_msg(db_conn, now - timedelta(hours=1), "bb")
    db_conn.commit()
    grant_alice_all_accounts()
    c = TestClient(create_app(db_dsn=db_dsn, searcher=None, serve_config=_TEST_SERVE_CFG))
    r = c.get("/v1/changes", headers={"Authorization": f"Bearer {api_token}"})
    assert r.status_code == 200
    body = r.json()
    assert len(body["new_messages"]) == 2
    assert "next_cursor" in body


def test_changes_with_cursor_filters(
    db_dsn: str, api_token: str, db_conn, grant_alice_all_accounts,
) -> None:
    now = datetime.now(timezone.utc)
    mid_old = _seed_msg(db_conn, now - timedelta(hours=2), "aa")
    db_conn.commit()
    grant_alice_all_accounts()
    c = TestClient(create_app(db_dsn=db_dsn, searcher=None, serve_config=_TEST_SERVE_CFG))
    r1 = c.get("/v1/changes", headers={"Authorization": f"Bearer {api_token}"})
    cursor = r1.json()["next_cursor"]
    _seed_msg(db_conn, now, "bb")
    db_conn.commit()
    r2 = c.get(f"/v1/changes?since={cursor}", headers={"Authorization": f"Bearer {api_token}"})
    body = r2.json()
    assert all(m["message_id"] != str(mid_old) for m in body["new_messages"])
    assert len(body["new_messages"]) == 1


def test_changes_with_bogus_cursor_400(db_dsn: str, api_token: str) -> None:
    """A non-integer ?since= should surface as problem+json 400, not silently
    return the entire archive."""
    c = TestClient(create_app(db_dsn=db_dsn, searcher=None, serve_config=_TEST_SERVE_CFG))
    r = c.get("/v1/changes?since=not-a-number", headers={"Authorization": f"Bearer {api_token}"})
    assert r.status_code == 400
    assert r.headers["content-type"].startswith("application/problem+json")
    body = r.json()
    assert body["type"] == "/problems/validation-failed"


def _seed_msg_with_dates(
    conn: psycopg.Connection,
    *,
    date_sent: datetime,
    date_received: datetime,
    suffix: str,
) -> int:
    aid = _ensure_account(conn)
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO messages (account_id, message_id, subject, raw_bytes, raw_sha256,
                                     size_bytes, headers, attachments, date_sent, date_received)
               VALUES (%s, %s, 'x', 'r', %s, 1, '{}'::jsonb, '[]'::jsonb, %s, %s) RETURNING id""",
            (aid, f"<{suffix}@x>", bytes.fromhex(suffix * 32), date_sent, date_received),
        )
        row = cur.fetchone(); assert row is not None
        return row[0]


def test_changes_no_cursor_orders_by_date_received_desc(
    db_dsn: str, api_token: str, db_conn, grant_alice_all_accounts,
) -> None:
    """Initial-load `/v1/changes` must return messages by `date_received DESC`.

    `m.id` reflects local insertion order — for a sync that processes archive
    folders after the inbox, ordering by `id DESC` surfaces decade-old
    messages at the top. The canonical default order for "show me my mail" is
    `date_received DESC`, matching the empty-query search fallback.

    The seed deliberately diverges `date_sent` from `date_received` so the
    test fails if anyone "helpfully" switches the ORDER BY to `date_sent`.
    """
    now = datetime.now(timezone.utc)
    # mid_a: received recently, but the email itself dates from years ago.
    mid_a = _seed_msg_with_dates(
        db_conn, date_sent=now - timedelta(days=365), date_received=now - timedelta(hours=1),
        suffix="aa",
    )
    # mid_b: received older, but the email's date_sent is the most recent.
    mid_b = _seed_msg_with_dates(
        db_conn, date_sent=now - timedelta(hours=1), date_received=now - timedelta(days=2),
        suffix="bb",
    )
    # mid_c: middle by date_received, oldest by date_sent.
    mid_c = _seed_msg_with_dates(
        db_conn, date_sent=now - timedelta(days=30), date_received=now - timedelta(days=1),
        suffix="cc",
    )
    db_conn.commit()
    grant_alice_all_accounts()
    c = TestClient(create_app(db_dsn=db_dsn, searcher=None, serve_config=_TEST_SERVE_CFG))
    r = c.get("/v1/changes", headers={"Authorization": f"Bearer {api_token}"})
    assert r.status_code == 200
    ids = [m["message_id"] for m in r.json()["new_messages"]]
    assert ids == [str(mid_a), str(mid_c), str(mid_b)]


def test_changes_idempotent_when_no_new_messages(db_dsn: str, api_token: str, db_conn) -> None:
    """Polling clients should see an empty `new_messages` and the same
    `next_cursor` they sent in, when no rows have been inserted between calls.
    A regression here means clients re-render the same batch forever."""
    now = datetime.now(timezone.utc)
    _seed_msg(db_conn, now - timedelta(hours=1), "aa")
    db_conn.commit()
    c = TestClient(create_app(db_dsn=db_dsn, searcher=None, serve_config=_TEST_SERVE_CFG))
    r1 = c.get("/v1/changes", headers={"Authorization": f"Bearer {api_token}"})
    cursor = r1.json()["next_cursor"]
    r2 = c.get(f"/v1/changes?since={cursor}", headers={"Authorization": f"Bearer {api_token}"})
    body = r2.json()
    assert body["new_messages"] == []
    assert body["next_cursor"] == cursor
