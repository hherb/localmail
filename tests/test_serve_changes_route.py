# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

from datetime import datetime, timedelta, timezone

import psycopg
from fastapi.testclient import TestClient

from localmail.config import ServeConfig
from localmail.serve.app import create_app
from localmail.serve.routes.changes import _DEFAULT_LIMIT

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


def _seed_msg_full(
    conn: psycopg.Connection,
    *,
    date_sent: datetime | None,
    internal_date: datetime | None,
    suffix: str,
    date_received: datetime | None = None,
) -> int:
    """Seed a message with explicit ordering-relevant dates.

    ``date_received`` defaults to ``now()`` so the `/v1/changes` safe-horizon
    filter doesn't mask the row when the test runs with horizon_s=0.
    """
    aid = _ensure_account(conn)
    when_received = date_received if date_received is not None else datetime.now(timezone.utc)
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO messages (account_id, message_id, subject, raw_bytes, raw_sha256,
                                     size_bytes, headers, attachments,
                                     date_sent, internal_date, date_received)
               VALUES (%s, %s, 'x', 'r', %s, 1, '{}'::jsonb, '[]'::jsonb, %s, %s, %s) RETURNING id""",
            (aid, f"<{suffix}@x>", bytes.fromhex(suffix * 32),
             date_sent, internal_date, when_received),
        )
        row = cur.fetchone(); assert row is not None
        return row[0]


def test_changes_no_cursor_orders_by_coalesce_internal_date_date_sent_desc(
    db_dsn: str, api_token: str, db_conn, grant_alice_all_accounts,
) -> None:
    """Initial-load `/v1/changes` orders by
    ``COALESCE(internal_date, date_sent) DESC NULLS LAST, id DESC``.

    The user's "received date" intent maps to the IMAP server INTERNALDATE
    (``messages.internal_date`` after migration 0018). Rows that have been
    backfilled to a real INTERNALDATE sort by that; legacy rows where the
    backfill hasn't run yet fall through to ``date_sent`` (the header
    ``Date:``) so the ordering stays meaningful even mid-rollout.

    The seed exercises every coalesce branch:
      * mid_a — internal_date populated, sorts by it (recent)
      * mid_b — internal_date NULL, falls through to date_sent (recent)
      * mid_c — both populated, sorts by internal_date (oldest of the three)
    """
    now = datetime.now(timezone.utc)
    # mid_a: internal_date = most recent → at top.
    mid_a = _seed_msg_full(
        db_conn, date_sent=now - timedelta(days=365),
        internal_date=now - timedelta(hours=1), suffix="aa",
    )
    # mid_b: no internal_date; date_sent is the second-newest signal.
    mid_b = _seed_msg_full(
        db_conn, date_sent=now - timedelta(days=1),
        internal_date=None, suffix="bb",
    )
    # mid_c: internal_date oldest among the three.
    mid_c = _seed_msg_full(
        db_conn, date_sent=now - timedelta(hours=1),
        internal_date=now - timedelta(days=30), suffix="cc",
    )
    db_conn.commit()
    grant_alice_all_accounts()
    c = TestClient(create_app(db_dsn=db_dsn, searcher=None, serve_config=_TEST_SERVE_CFG))
    r = c.get("/v1/changes", headers={"Authorization": f"Bearer {api_token}"})
    assert r.status_code == 200
    ids = [m["message_id"] for m in r.json()["new_messages"]]
    assert ids == [str(mid_a), str(mid_b), str(mid_c)]


def test_changes_wire_date_reflects_internal_date_when_set(
    db_dsn: str, api_token: str, db_conn, grant_alice_all_accounts,
) -> None:
    """The wire `date` field must match the sort key — i.e.
    ``COALESCE(internal_date, date_sent)``. Returning only the header
    ``Date:`` while sorting by INTERNALDATE makes the displayed dates
    look out of order whenever the two differ (forwarded mail, mailing
    lists, sender clock skew, mid-rollout backfill).
    """
    header_date = datetime(2022, 1, 1, tzinfo=timezone.utc)
    arrived = datetime(2026, 5, 20, 12, 0, 0, tzinfo=timezone.utc)
    _seed_msg_full(db_conn, date_sent=header_date,
                   internal_date=arrived, suffix="aa")
    db_conn.commit()
    grant_alice_all_accounts()
    c = TestClient(create_app(db_dsn=db_dsn, searcher=None, serve_config=_TEST_SERVE_CFG))
    r = c.get("/v1/changes", headers={"Authorization": f"Bearer {api_token}"})
    body = r.json()
    assert datetime.fromisoformat(body["new_messages"][0]["date"]) == arrived


def test_changes_wire_date_falls_back_to_date_sent_when_internal_date_null(
    db_dsn: str, api_token: str, db_conn, grant_alice_all_accounts,
) -> None:
    """Legacy / un-backfilled rows must still surface a non-null date."""
    header_date = datetime(2022, 1, 1, tzinfo=timezone.utc)
    _seed_msg_full(db_conn, date_sent=header_date,
                   internal_date=None, suffix="bb")
    db_conn.commit()
    grant_alice_all_accounts()
    c = TestClient(create_app(db_dsn=db_dsn, searcher=None, serve_config=_TEST_SERVE_CFG))
    r = c.get("/v1/changes", headers={"Authorization": f"Bearer {api_token}"})
    body = r.json()
    assert datetime.fromisoformat(body["new_messages"][0]["date"]) == header_date


def test_changes_no_cursor_caps_at_default_limit_in_desc_order(
    db_dsn: str, api_token: str, db_conn, grant_alice_all_accounts,
) -> None:
    """`/v1/changes` without a cursor is a **tail subscription**, not a
    full archive scan — it returns at most ``_DEFAULT_LIMIT`` rows in
    ``COALESCE(internal_date, date_sent) DESC NULLS LAST, id DESC``
    order. This pins the wire contract resolved in #38: backfill goes
    through ``/v1/messages``; ``/v1/changes`` stays tail-only. Removing
    the cap (e.g. to support initial archive scroll) would make a
    first-time client against a 1M-message archive try to load the whole
    table in one response — exactly the surprise #38 was filed against.

    Seeds ``_DEFAULT_LIMIT + 5`` rows so the cap is observable and not
    coincidentally equal to the seeded count.
    """
    base = datetime.now(timezone.utc) - timedelta(days=1)
    seeded = _DEFAULT_LIMIT + 5
    seeded_ids: list[int] = []
    for i in range(seeded):
        when = base + timedelta(seconds=i)
        # 4-char hex suffix scales to 65535 rows; _seed_msg expands it to
        # a valid BYTEA via ``bytes.fromhex(suffix * 32)``, unique per i.
        suffix = f"{i:04x}"
        seeded_ids.append(_seed_msg(db_conn, when, suffix))
    db_conn.commit()
    grant_alice_all_accounts()
    c = TestClient(create_app(db_dsn=db_dsn, searcher=None, serve_config=_TEST_SERVE_CFG))
    r = c.get("/v1/changes", headers={"Authorization": f"Bearer {api_token}"})
    assert r.status_code == 200
    body = r.json()
    new_messages = body["new_messages"]
    assert len(new_messages) == _DEFAULT_LIMIT

    # DESC order: each seeded row had a strictly increasing date, so the
    # first row in the response is the highest-seeded id, and every
    # subsequent id is strictly smaller.
    returned_ids = [int(m["message_id"]) for m in new_messages]
    assert returned_ids == sorted(returned_ids, reverse=True)
    assert returned_ids[0] == seeded_ids[-1]


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
