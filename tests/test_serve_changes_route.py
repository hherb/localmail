from datetime import datetime, timedelta, timezone

import psycopg
from fastapi.testclient import TestClient

from localmail.serve.app import create_app


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


def test_changes_no_cursor_returns_recent(db_dsn: str, api_token: str, db_conn) -> None:
    now = datetime.now(timezone.utc)
    _seed_msg(db_conn, now - timedelta(hours=2), "aa")
    _seed_msg(db_conn, now - timedelta(hours=1), "bb")
    db_conn.commit()
    c = TestClient(create_app(db_dsn=db_dsn, searcher=None))
    r = c.get("/v1/changes", headers={"Authorization": f"Bearer {api_token}"})
    assert r.status_code == 200
    body = r.json()
    assert len(body["new_messages"]) == 2
    assert "next_cursor" in body


def test_changes_with_cursor_filters(db_dsn: str, api_token: str, db_conn) -> None:
    now = datetime.now(timezone.utc)
    mid_old = _seed_msg(db_conn, now - timedelta(hours=2), "aa")
    db_conn.commit()
    c = TestClient(create_app(db_dsn=db_dsn, searcher=None))
    r1 = c.get("/v1/changes", headers={"Authorization": f"Bearer {api_token}"})
    cursor = r1.json()["next_cursor"]
    _seed_msg(db_conn, now, "bb")
    db_conn.commit()
    r2 = c.get(f"/v1/changes?since={cursor}", headers={"Authorization": f"Bearer {api_token}"})
    body = r2.json()
    assert all(m["message_id"] != str(mid_old) for m in body["new_messages"])
    assert len(body["new_messages"]) == 1
