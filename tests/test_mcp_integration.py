"""End-to-end: real MCP client over Streamable HTTP against the mounted /mcp."""
import asyncio
import threading
import time

import pytest
import uvicorn

from localmail.api.acl import grant_account
from localmail.api.auth import create_user, issue_token
from localmail.config import McpConfig, SearchConfig
from localmail.db import open_pool
from localmail.search.searcher import Searcher
from localmail.serve.app import create_app

# The mcp *client* (imported inside the async helpers below) is only present
# when the [mcp] extra is installed; skip the whole module otherwise. None of
# the module-level imports above need it (create_app imports build_mcp_server
# lazily), so this gate sits after them.
pytest.importorskip("mcp")
pytestmark = pytest.mark.integration


def _seed(db_conn):
    """Seed user+token, two accounts (one granted), one 'invoice' message each."""
    uid = create_user(db_conn, "agent", "pw")
    raw_token, _ = issue_token(db_conn, uid)
    with db_conn.cursor() as cur:
        cur.execute("INSERT INTO accounts (name,email_address,imap_host,auth_method)"
                    " VALUES ('granted','g@x','imap.example.com','password') RETURNING id")
        granted = int(cur.fetchone()[0])
        cur.execute("INSERT INTO accounts (name,email_address,imap_host,auth_method)"
                    " VALUES ('other','o@x','imap.example.com','password') RETURNING id")
        other = int(cur.fetchone()[0])
    grant_account(db_conn, uid, granted)
    for i, acct in enumerate((granted, other)):
        with db_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO messages (account_id,message_id,raw_sha256,subject,"
                "body_text,headers,raw_bytes,size_bytes)"
                " VALUES (%s,%s,%s,%s,%s,'{}'::jsonb,'r',1)",
                (acct, f"<int-{i}@x>", bytes([i + 1]) * 32, "invoice", "the invoice"))
    db_conn.commit()
    return raw_token, granted, other


def _start(app):
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(500):
        if server.started:
            break
        time.sleep(0.02)
    assert server.started, "uvicorn did not start"
    port = server.servers[0].sockets[0].getsockname()[1]
    return server, thread, port


async def _drive(port, token, granted):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client
    url = f"http://127.0.0.1:{port}/mcp"
    headers = {"Authorization": f"Bearer {token}"}
    async with streamablehttp_client(url, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = {t.name for t in tools.tools}
            assert names == {"search", "get_message", "get_attachment",
                             "list_messages", "list_accounts"}, names
            accounts_res = await session.call_tool("list_accounts", {})
            search_res = await session.call_tool(
                "search", {"query": "invoice", "sort": "date"})
            smart_res = await session.call_tool(
                "search", {"query": "invoice", "sort": "date", "smart": True})
            return accounts_res, search_res, smart_res


def _payload(call_result):
    """Extract the JSON payload from a CallToolResult (structuredContent or text)."""
    sc = getattr(call_result, "structuredContent", None)
    if sc is not None:
        return sc
    import json
    return json.loads(call_result.content[0].text)


def test_mcp_end_to_end_acl_scoped(db_dsn, db_conn):
    token, granted, other = _seed(db_conn)
    searcher = Searcher(pool=open_pool(db_dsn), cfg=SearchConfig(),
                        embeddings=None, reranker=None, rewriter=None)
    app = create_app(db_dsn=db_dsn, searcher=searcher, enable_mcp=True,
                     mcp_config=McpConfig(enabled=True))
    server, thread, port = _start(app)
    try:
        accounts_res, search_res, smart_res = asyncio.run(_drive(port, token, granted))
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        searcher._pool.close()

    accounts = _payload(accounts_res)
    # accounts payload may be a list or wrapped; normalize
    acct_list = accounts if isinstance(accounts, list) else accounts.get("result", accounts)
    ids = {a["id"] for a in acct_list}
    assert ids == {str(granted)}

    page = _payload(search_res)
    results = page["results"] if isinstance(page, dict) else page
    assert results, "search returned no results; the granted 'invoice' message must match"
    for r in results:
        assert r["account"]["id"] == str(granted)

    # smart=True over the wire: this searcher has rewriter=None, so the rewrite
    # is unavailable — the search must still run (graceful) and the response
    # must carry rewrite_skipped=True (the cited MCP acceptance criterion).
    smart_page = _payload(smart_res)
    assert smart_page["rewrite_skipped"] is True
    assert smart_page["results"], "smart search must still return results un-rewritten"


def test_mcp_rejects_missing_bearer(db_dsn, db_conn):
    _seed(db_conn)
    searcher = Searcher(pool=open_pool(db_dsn), cfg=SearchConfig(),
                        embeddings=None, reranker=None, rewriter=None)
    app = create_app(db_dsn=db_dsn, searcher=searcher, enable_mcp=True,
                     mcp_config=McpConfig(enabled=True))
    server, thread, port = _start(app)

    async def _no_auth(port):
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client
        async with streamablehttp_client(f"http://127.0.0.1:{port}/mcp") as (r, w, _):
            async with ClientSession(r, w) as session:
                await session.initialize()

    try:
        with pytest.raises(Exception):
            asyncio.run(_no_auth(port))
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        searcher._pool.close()
