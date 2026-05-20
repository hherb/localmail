"""Tests for GET /v1/messages — the keyset-paginated browse route."""
from datetime import datetime, timedelta, timezone

import psycopg
from fastapi.testclient import TestClient

from localmail.serve.app import create_app


def _seed_acct(conn: psycopg.Connection, name: str = "a") -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO accounts (name, email_address, imap_host, auth_method) "
            "VALUES (%s, %s, 'imap.x', 'password') RETURNING id",
            (name, f"{name}@y.test"),
        )
        row = cur.fetchone(); assert row is not None
        return int(row[0])


def _seed_msg(conn: psycopg.Connection, account_id: int, suffix: str,
              when: datetime) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO messages (account_id, message_id, subject, raw_bytes,
                                     raw_sha256, size_bytes, headers, attachments,
                                     date_sent, internal_date, date_received)
               VALUES (%s, %s, 's', 'r', %s, 1, '{}'::jsonb, '[]'::jsonb,
                       %s, %s, now()) RETURNING id""",
            (account_id, f"<{suffix}@x>", bytes.fromhex(suffix * 32),
             when, when),
        )
        row = cur.fetchone(); assert row is not None
        return int(row[0])


def _grant(conn: psycopg.Connection, user_id: int, account_id: int) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO user_accounts (user_id, account_id) VALUES (%s, %s)",
            (user_id, account_id),
        )
    conn.commit()


def test_browse_initial_page_and_cursor_roundtrip(
    db_dsn: str, api_token: str, api_user, db_conn,
) -> None:
    aid = _seed_acct(db_conn)
    now = datetime.now(timezone.utc)
    ids = [_seed_msg(db_conn, aid, f"{i:02d}", now - timedelta(hours=i))
           for i in range(5)]
    db_conn.commit()
    _grant(db_conn, api_user.id, aid)

    c = TestClient(create_app(db_dsn=db_dsn, searcher=None))
    r1 = c.get("/v1/messages?limit=2",
               headers={"Authorization": f"Bearer {api_token}"})
    assert r1.status_code == 200
    body1 = r1.json()
    assert [int(m["message_id"]) for m in body1["messages"]] == [ids[0], ids[1]]
    assert body1["next_cursor"] is not None

    r2 = c.get(f"/v1/messages?limit=2&cursor={body1['next_cursor']}",
               headers={"Authorization": f"Bearer {api_token}"})
    body2 = r2.json()
    assert [int(m["message_id"]) for m in body2["messages"]] == [ids[2], ids[3]]


def test_browse_empty_grants_returns_empty(
    db_dsn: str, api_token: str, db_conn,
) -> None:
    c = TestClient(create_app(db_dsn=db_dsn, searcher=None))
    r = c.get("/v1/messages",
              headers={"Authorization": f"Bearer {api_token}"})
    assert r.status_code == 200
    assert r.json() == {"messages": [], "next_cursor": None}


def test_browse_garbage_cursor_400(db_dsn: str, api_token: str) -> None:
    c = TestClient(create_app(db_dsn=db_dsn, searcher=None))
    r = c.get("/v1/messages?cursor=not-a-cursor",
              headers={"Authorization": f"Bearer {api_token}"})
    assert r.status_code == 400
    assert r.headers["content-type"].startswith("application/problem+json")
    assert r.json()["type"] == "/problems/validation-failed"


def test_browse_account_id_non_digit_400(db_dsn: str, api_token: str) -> None:
    c = TestClient(create_app(db_dsn=db_dsn, searcher=None))
    r = c.get("/v1/messages?account_id=abc",
              headers={"Authorization": f"Bearer {api_token}"})
    assert r.status_code == 400


def test_browse_account_id_repeats_treated_as_list(
    db_dsn: str, api_token: str, api_user, db_conn,
) -> None:
    aid1 = _seed_acct(db_conn, name="one")
    aid2 = _seed_acct(db_conn, name="two")
    now = datetime.now(timezone.utc)
    m1 = _seed_msg(db_conn, aid1, "aa", now)
    m2 = _seed_msg(db_conn, aid2, "bb", now - timedelta(hours=1))
    db_conn.commit()
    _grant(db_conn, api_user.id, aid1)
    _grant(db_conn, api_user.id, aid2)

    c = TestClient(create_app(db_dsn=db_dsn, searcher=None))
    r = c.get(f"/v1/messages?account_id={aid1}&account_id={aid2}",
              headers={"Authorization": f"Bearer {api_token}"})
    assert r.status_code == 200
    ids = [int(m["message_id"]) for m in r.json()["messages"]]
    assert ids == [m1, m2]


def test_browse_requires_auth(db_dsn: str) -> None:
    c = TestClient(create_app(db_dsn=db_dsn, searcher=None))
    r = c.get("/v1/messages")
    assert r.status_code == 401
