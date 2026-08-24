# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""MCP tool bodies — thin api/ wrappers, ACL-scoped."""
import hashlib
from datetime import datetime, timezone

import psycopg.types.json
import pytest

from localmail.api.acl import allowed_account_ids, grant_account
from localmail.api.auth import create_user
from localmail.api.errors import NotFound
from localmail.config import SearchConfig
from localmail.db import open_pool
from localmail.search.searcher import Searcher

pytest.importorskip("mcp")  # the [mcp] extra (mcp SDK) gates this module

from localmail.mcp import tools  # noqa: E402


def _insert_account(conn, name: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO accounts (name, email_address, imap_host, auth_method)"
            " VALUES (%s, %s, 'imap.example.com', 'password') RETURNING id",
            (name, f"{name}@example.com"),
        )
        row = cur.fetchone()
        assert row is not None
        return int(row[0])


_msg_counter = 0


def _insert_message(conn, account_id: int, subject: str, body_text: str) -> int:
    global _msg_counter
    _msg_counter += 1
    sha = bytes([_msg_counter % 256]) * 32
    msg_id = f"<msg-{_msg_counter}@example.com>"
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO messages"
            "  (account_id, message_id, raw_sha256, subject, body_text,"
            "   headers, raw_bytes, size_bytes)"
            " VALUES (%s, %s, %s, %s, %s, '{}'::jsonb, 'r', 1) RETURNING id",
            (account_id, msg_id, sha, subject, body_text),
        )
        row = cur.fetchone()
        assert row is not None
        return int(row[0])


def _lexical_searcher(db_dsn):
    return Searcher(pool=open_pool(db_dsn), cfg=SearchConfig(),
                    embeddings=None, reranker=None, rewriter=None)


class _FakeRewriter:
    name = "fake"; model = "fake"

    def __init__(self, result):
        self._result = result

    def rewrite(self, free_text):
        return self._result


def test_tool_search_states_no_sort_of_its_own():
    """The published schema is pinned elsewhere; this is the function itself.

    `mcp/server.py` always forwards `sort` explicitly, so restoring
    `= "rank"` here leaves every other MCP test green while silently
    re-arming the defect for a direct library caller — the layer the schema
    pin cannot see.
    """
    import inspect

    assert inspect.signature(tools.tool_search).parameters["sort"].default is None


def test_tool_search_scopes_to_allowed_accounts(db_dsn, db_conn):
    uid = create_user(db_conn, "alice", "hunter2")
    granted = _insert_account(db_conn, "granted")
    other = _insert_account(db_conn, "other")
    grant_account(db_conn, uid, granted)
    gmid = _insert_message(db_conn, granted, "invoice Q1", "the invoice for Q1")
    _insert_message(db_conn, other, "invoice Q2", "the invoice for Q2")
    db_conn.commit()

    acl = allowed_account_ids(db_conn, uid)
    assert acl == [granted]
    searcher = _lexical_searcher(db_dsn)
    try:
        page = tools.tool_search(
            searcher=searcher, user_id=uid, allowed_account_ids=acl,
            query="invoice", sort="date", limit=20, cursor=None, filters={},
        )
    finally:
        searcher._pool.close()

    assert page["results"], "expected at least one hit"
    returned = {int(r["message_id"]) for r in page["results"]}
    assert gmid in returned
    for r in page["results"]:
        assert r["account"]["id"] == str(granted)


def test_tool_search_empty_grants_returns_empty(db_dsn, db_conn):
    uid = create_user(db_conn, "bob", "hunter2")
    db_conn.commit()
    searcher = _lexical_searcher(db_dsn)
    try:
        page = tools.tool_search(
            searcher=searcher, user_id=uid, allowed_account_ids=[],
            query="invoice", sort="date", limit=20, cursor=None, filters={},
        )
    finally:
        searcher._pool.close()
    assert page == {"results": [], "next_cursor": None, "total_estimate": None,
                    "took_ms": 0.0, "rewrite_skipped": False,
                    "rewrite_status": "not_requested", "rewrite_note": None,
                    "rewrite_note_code": None}


def test_tool_get_message_granted(db_conn):
    uid = create_user(db_conn, "carol", "hunter2")
    acct = _insert_account(db_conn, "carol-acct")
    grant_account(db_conn, uid, acct)
    mid = _insert_message(db_conn, acct, "hello", "world")
    db_conn.commit()
    msg = tools.tool_get_message(
        db_conn, message_id=mid,
        allowed_account_ids=allowed_account_ids(db_conn, uid),
    )
    assert msg["id"] == str(mid)
    assert msg["account"]["id"] == str(acct)


def test_tool_get_message_attachment_carries_content_type_and_size(db_conn):
    """#196: the MCP get_message body exposes content_type + size per
    attachment, matching the REST route (shared api.messages path)."""
    uid = create_user(db_conn, "carol-att", "hunter2")
    acct = _insert_account(db_conn, "carol-att-acct")
    grant_account(db_conn, uid, acct)
    sha_hex = "ab" * 32
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO attachment_blobs (sha256, path, mime_type, size_bytes)"
            " VALUES (%s, %s, 'application/pdf', 84213)",
            (bytes.fromhex(sha_hex), f"/nonexistent/{sha_hex}"),
        )
        cur.execute(
            "INSERT INTO messages"
            "  (account_id, message_id, raw_sha256, subject, attachments,"
            "   headers, raw_bytes, size_bytes)"
            " VALUES (%s, '<att-mcp@x>', %s, 'hi', %s::jsonb, '{}'::jsonb, 'r', 1)"
            " RETURNING id",
            (acct, b"\x7f" * 32,
             psycopg.types.json.Jsonb([{"filename": "booking.pdf", "sha256": sha_hex}])),
        )
        row = cur.fetchone(); assert row is not None
        mid = int(row[0])
    db_conn.commit()
    msg = tools.tool_get_message(
        db_conn, message_id=mid,
        allowed_account_ids=allowed_account_ids(db_conn, uid),
    )
    assert msg["attachments"] == [
        {"filename": "booking.pdf", "sha256": sha_hex,
         "content_type": "application/pdf", "size": 84213}
    ]


def test_tool_get_message_denied_raises_notfound(db_conn):
    uid = create_user(db_conn, "dave", "hunter2")
    granted = _insert_account(db_conn, "dave-granted")
    other = _insert_account(db_conn, "dave-other")
    grant_account(db_conn, uid, granted)
    mid_other = _insert_message(db_conn, other, "secret", "not yours")
    db_conn.commit()
    with pytest.raises(NotFound):
        tools.tool_get_message(
            db_conn, message_id=mid_other,
            allowed_account_ids=allowed_account_ids(db_conn, uid),
        )


def test_tool_list_messages_scopes(db_conn):
    uid = create_user(db_conn, "erin", "hunter2")
    granted = _insert_account(db_conn, "erin-granted")
    other = _insert_account(db_conn, "erin-other")
    grant_account(db_conn, uid, granted)
    _insert_message(db_conn, granted, "g1", "body")
    _insert_message(db_conn, other, "o1", "body")
    db_conn.commit()
    page = tools.tool_list_messages(
        db_conn, allowed_account_ids=allowed_account_ids(db_conn, uid),
    )
    assert "messages" in page and "next_cursor" in page
    assert page["messages"], "expected the granted account's message"
    for m in page["messages"]:
        assert m["account"]["id"] == str(granted)


def test_tool_list_accounts_returns_only_granted(db_conn):
    uid = create_user(db_conn, "frank", "hunter2")
    granted = _insert_account(db_conn, "frank-granted")
    _insert_account(db_conn, "frank-other")
    grant_account(db_conn, uid, granted)
    db_conn.commit()
    accounts = tools.tool_list_accounts(
        db_conn, allowed_account_ids=allowed_account_ids(db_conn, uid),
    )
    assert {a["id"] for a in accounts} == {str(granted)}


# ---------------------------------------------------------------------------
# tool_get_attachment tests
# ---------------------------------------------------------------------------

_ATT_SHA_HEX = "ab" * 32
_ATT_TEXT = "Hello world extracted text"


def _seed_attachment(db_conn, account_name: str):
    """Seed account + message referencing a blob + blob + attachment_text.

    Returns (account_id, sha_hex, expected_text). The on-disk blob file is
    not needed for text/metadata reads, so we don't write one.
    """
    sha_hex = _ATT_SHA_HEX
    sha_bytes = bytes.fromhex(sha_hex)
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO accounts (name, email_address, imap_host, auth_method)"
            " VALUES (%s, %s, 'imap.example.com', 'password') RETURNING id",
            (account_name, f"{account_name}@example.com"),
        )
        row = cur.fetchone()
        assert row is not None
        acct = int(row[0])
        raw = b"From: x@example.com\r\nSubject: t\r\n\r\nbody"
        cur.execute(
            "INSERT INTO messages (account_id, message_id, raw_bytes, raw_sha256,"
            " size_bytes, headers, attachments, date_sent)"
            " VALUES (%s, %s, %s, %s, %s, '{}'::jsonb, %s::jsonb, %s)",
            (acct, f"<{account_name}-{sha_hex}@x>", raw, hashlib.sha256(raw).digest(),
             len(raw),
             psycopg.types.json.Jsonb([{"filename": "r.pdf", "sha256": sha_hex}]),
             datetime.now(timezone.utc)),
        )
        cur.execute(
            "INSERT INTO attachment_blobs (sha256, mime_type, size_bytes, path)"
            " VALUES (%s, 'application/pdf', 12, %s)",
            (sha_bytes, f"/nonexistent/{sha_hex}"),
        )
        cur.execute(
            "INSERT INTO attachment_text (sha256, extractor, extracted_text)"
            " VALUES (%s, 'stub', %s)",
            (sha_bytes, _ATT_TEXT),
        )
    db_conn.commit()
    return acct, sha_hex, _ATT_TEXT


def test_tool_get_attachment_text(db_conn):
    acct, sha, text = _seed_attachment(db_conn, "att-owner-text")
    out = tools.tool_get_attachment(
        db_conn, sha256=sha, mode="text", allowed_account_ids=[acct])
    assert out == {"mode": "text", "sha256": sha, "text": text}


def test_tool_get_attachment_metadata(db_conn):
    acct, sha, _ = _seed_attachment(db_conn, "att-owner-meta")
    out = tools.tool_get_attachment(
        db_conn, sha256=sha, mode="metadata", allowed_account_ids=[acct])
    assert out["mode"] == "metadata"
    assert out["sha256"] == sha
    assert out["metadata"]["mime_type"] == "application/pdf"
    assert out["metadata"]["size_bytes"] == 12


def test_tool_get_attachment_denied_raises_notfound(db_conn):
    acct, sha, _ = _seed_attachment(db_conn, "att-owner-denied")
    # An account with no referencing message cannot read the blob.
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO accounts (name, email_address, imap_host, auth_method)"
            " VALUES ('att-other', 'o@example.com', 'imap.example.com', 'password')"
            " RETURNING id")
        row = cur.fetchone()
        assert row is not None
        other = int(row[0])
    db_conn.commit()
    with pytest.raises(NotFound):
        tools.tool_get_attachment(
            db_conn, sha256=sha, mode="text", allowed_account_ids=[other])


def test_tool_get_attachment_bad_mode_raises(db_conn):
    acct, sha, _ = _seed_attachment(db_conn, "att-owner-badmode")
    with pytest.raises(ValueError):
        tools.tool_get_attachment(
            db_conn, sha256=sha, mode="bytes", allowed_account_ids=[acct])


def test_tool_search_smart_without_rewriter_degrades(db_dsn, db_conn):
    uid = create_user(db_conn, "smartless", "hunter2")
    acct = _insert_account(db_conn, "smartless-acct")
    grant_account(db_conn, uid, acct)
    _insert_message(db_conn, acct, "invoice", "the invoice body")
    db_conn.commit()
    acl = allowed_account_ids(db_conn, uid)
    searcher = _lexical_searcher(db_dsn)
    try:
        page = tools.tool_search(
            searcher=searcher, user_id=uid, allowed_account_ids=acl,
            query="invoice", sort="date", limit=20, cursor=None, filters={},
            smart=True,
        )
    finally:
        searcher._pool.close()
    assert page["rewrite_skipped"] is True
    assert page["results"]  # search still ran on the un-rewritten query


def test_tool_search_smart_with_rewriter_applies_over_the_wire(db_dsn, db_conn):
    """smart=True with a configured rewriter actually rewrites over the
    tool_search wire layer: expansion ORs in a synonym the plain query
    misses, surfacing a message that the un-rewritten query does not, and
    rewrite_skipped is False (the rewrite happened)."""
    from localmail.search.query import SearchFilters
    from localmail.search.rewriter import RewriteResult

    uid = create_user(db_conn, "smartful", "hunter2")
    acct = _insert_account(db_conn, "smartful-acct")
    grant_account(db_conn, uid, acct)
    # subject carries the synonym only; the literal query word never appears.
    _insert_message(db_conn, acct, "receipt for lunch", "body")
    db_conn.commit()
    acl = allowed_account_ids(db_conn, uid)
    expand = RewriteResult(rewritten_text="invoice", expansion_terms=["receipt"],
                           extracted_filters=SearchFilters())
    searcher = Searcher(pool=open_pool(db_dsn), cfg=SearchConfig(),
                        embeddings=None, reranker=None,
                        rewriter=_FakeRewriter(expand))
    try:
        plain = tools.tool_search(
            searcher=searcher, user_id=uid, allowed_account_ids=acl,
            query="invoice", sort="date", limit=20, cursor=None, filters={},
        )
        smart = tools.tool_search(
            searcher=searcher, user_id=uid, allowed_account_ids=acl,
            query="invoice", sort="date", limit=20, cursor=None, filters={},
            smart=True,
        )
    finally:
        searcher._pool.close()
    assert plain["results"] == []  # "invoice" matches nothing un-rewritten
    assert smart["results"]  # expansion "receipt" surfaces the message
    assert smart["rewrite_skipped"] is False


def test_tool_search_forwards_sort_order_to_run_search(monkeypatch):
    """The `tool_search` → `run_search` hop, which nothing pinned.

    Every other MCP test here drives a real searcher and asserts on rows,
    so a dropped `sort_order` is invisible to them: the call still 200s and
    still returns date-ordered results, just in the direction the agent did
    not ask for. Deleting the forwarding line left this whole file green.
    """
    seen: dict = {}

    def _recording_run_search(**kwargs):
        seen.update(kwargs)
        return {"results": [], "next_cursor": None}

    monkeypatch.setattr(tools, "run_search", _recording_run_search)
    tools.tool_search(searcher=object(), user_id=1, allowed_account_ids=[1],
                      query="invoice", sort="date", sort_order="asc")
    assert seen.get("sort_order") == "asc"


def test_tool_search_states_no_sort_order_of_its_own():
    """`mcp/server.py` always forwards `sort_order` explicitly, so a
    `= "desc"` restored here leaves every schema pin green while silently
    re-arming the defect for a direct library caller — the layer
    `test_mcp_server_build.py`'s schema assertion cannot see.
    """
    import inspect

    param = inspect.signature(tools.tool_search).parameters["sort_order"]
    assert param.default is None


def test_tool_search_pages_ascending_end_to_end_over_a_real_archive(db_dsn, db_conn):
    """Full stack, no mocks: real Searcher, real DB, real cursor round trip.

    Every other ``sort_order`` test at this layer stubs ``run_search`` and
    asserts the inbound kwarg, and the route-level ones assert a cursor
    minted from a canned ``next_keyset``. Nothing drove the whole chain —
    ORDER BY, the ``_keyset_clause`` predicate, the ``KA|`` mint, and the
    decode on the way back in — against actual rows.

    That chain is where a direction gets lost silently: each link can be
    individually correct while the walk still doubles back, and the symptom
    is duplicate results rather than an error. So this asserts the property
    a client actually depends on — page 2 continues where page 1 stopped,
    strictly oldest-first, with nothing repeated and nothing skipped.
    """
    uid = create_user(db_conn, "asc-pager", "hunter2")
    acct = _insert_account(db_conn, "asc-archive")
    grant_account(db_conn, uid, acct)
    # Distinct dates, inserted out of order so a walk that merely returns
    # insertion order cannot pass.
    ids_by_day = {}
    for day in (3, 1, 5, 2, 4, 6):
        mid = _insert_message(db_conn, acct, f"invoice day {day}", "the invoice")
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE messages SET internal_date = %s WHERE id = %s",
                (datetime(2026, 3, day, tzinfo=timezone.utc), mid),
            )
        ids_by_day[day] = mid
    db_conn.commit()
    oldest_first = [ids_by_day[d] for d in sorted(ids_by_day)]

    acl = allowed_account_ids(db_conn, uid)
    searcher = _lexical_searcher(db_dsn)
    try:
        from localmail.api.search_cursor import keyset_order

        walked: list[int] = []
        cursor = None
        seen_ascending_prefix = False
        for _ in range(len(oldest_first) + 2):  # bounded: a loop here is the bug
            kwargs = {"sort": "date", "sort_order": "asc"} if cursor is None else {}
            page = tools.tool_search(
                searcher=searcher, user_id=uid, allowed_account_ids=acl,
                query="invoice", limit=2, cursor=cursor, filters={}, **kwargs,
            )
            walked.extend(int(r["message_id"]) for r in page["results"])
            cursor = page["next_cursor"]
            if cursor is None:
                break
            # Paging states nothing but the cursor — the documented call, and
            # the one that used to resolve back to "desc" and reverse.
            # Asked of `keyset_order` rather than of the prefix spelling:
            # since #326 the prefix carries the walk too ("KAT|" here, the
            # query being a text one), and the property under test is the
            # direction, not the encoding.
            assert keyset_order(cursor) == "asc", cursor
            seen_ascending_prefix = True
    finally:
        searcher._pool.close()

    assert seen_ascending_prefix, "the walk never paged; the test proved nothing"
    assert walked == oldest_first, (
        f"ascending walk did not cover the archive oldest-first: {walked} "
        f"!= {oldest_first}"
    )
