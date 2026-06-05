"""MCP tool bodies — thin api/ wrappers, ACL-scoped."""
from localmail.api.acl import allowed_account_ids, grant_account
from localmail.api.auth import create_user
from localmail.config import SearchConfig
from localmail.db import open_pool
from localmail.mcp import tools
from localmail.search.searcher import Searcher


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
    assert page == {"results": [], "next_cursor": None,
                    "total_estimate": 0, "took_ms": 0.0}
