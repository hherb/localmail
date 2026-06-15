"""End-to-end: real MCP client over Streamable HTTP against the mounted /mcp."""
import asyncio
import base64
import hashlib
import secrets
import socket
import threading
import time
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import uvicorn

from localmail.api.acl import grant_account
from localmail.api.auth import create_user, issue_token
from localmail.config import McpConfig, SearchConfig, ServeConfig
from localmail.db import open_pool
from localmail.search.searcher import Searcher
from localmail.serve.app import create_app

# The mcp *client* (imported inside the async helpers below) is only present
# when the [mcp] extra is installed; skip the whole module otherwise. None of
# the module-level imports above need it (create_app imports build_mcp_server
# lazily), so this gate sits after them.
pytest.importorskip("mcp")
pytestmark = pytest.mark.integration

# The non-deprecated streamable_http_client takes a caller-built httpx client
# instead of a headers= kwarg. Mirror the MCP SDK's own client defaults so
# behaviour is unchanged: connect 30s / SSE read 300s (a default httpx 5s read
# timeout could prematurely close a server->client stream) and, crucially,
# follow_redirects=True — the /mcp sub-app redirects to /mcp/ with a 307 that an
# unconfigured httpx client (follow_redirects=False) would surface as an error.
_MCP_CONNECT_TIMEOUT_S = 30.0
_MCP_SSE_READ_TIMEOUT_S = 300.0


def _mcp_http_client(headers: dict[str, str] | None = None) -> httpx.AsyncClient:
    """httpx client configured like the MCP SDK's own streamable-HTTP defaults."""
    return httpx.AsyncClient(
        headers=headers,
        timeout=httpx.Timeout(_MCP_CONNECT_TIMEOUT_S, read=_MCP_SSE_READ_TIMEOUT_S),
        follow_redirects=True,
    )


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
    from mcp.client.streamable_http import streamable_http_client
    url = f"http://127.0.0.1:{port}/mcp"
    headers = {"Authorization": f"Bearer {token}"}
    async with _mcp_http_client(headers) as client:
        async with streamable_http_client(url, http_client=client) as (read, write, _):
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
        from mcp.client.streamable_http import streamable_http_client
        url = f"http://127.0.0.1:{port}/mcp"
        async with _mcp_http_client() as client:
            async with streamable_http_client(url, http_client=client) as (r, w, _):
                async with ClientSession(r, w) as session:
                    await session.initialize()

    try:
        with pytest.raises(Exception):
            asyncio.run(_no_auth(port))
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        searcher._pool.close()


# ---------------------------------------------------------------------------
# Full cold-connect OAuth authorization-server dance (Task 12).
#
# Path-placement resolution (the key deliverable): FastMCP is sub-mounted at
# /mcp, so the SDK's AS routes (/authorize, /token, /register) and the AS
# metadata document all land UNDER /mcp. The SDK derives the metadata's
# endpoint URLs from AuthSettings.issuer_url. With a bare-origin issuer
# (http://host:port) those would point at /authorize at the ROOT, which 404s.
#
# The fix (Task 12b) auto-DERIVES the AS issuer from the resource origin: the
# operator sets ONLY `resource_server_url = http://127.0.0.1:<port>` (bare
# origin, NO /mcp); `_try_build_mcp` derives `issuer_url =
# http://127.0.0.1:<port>/mcp` so the SDK advertises endpoints under /mcp
# (matching the sub-mount) AND serves the AS metadata at
# `<issuer>/.well-known/oauth-authorization-server`
# (= /mcp/.well-known/oauth-authorization-server) consistently, and the PRM's
# authorization_servers[0] (the issuer) is a URL whose AS metadata is
# fetchable. This fixture sets no issuer_url — proving the zero-config path
# end-to-end. The test FOLLOWS the metadata documents (reads the endpoint URLs
# from the fetched JSON) rather than hardcoding paths — proving a real client
# could discover and use the AS cold.
#
# Residual RFC 8414 nuance (documented, not blocking): a path-bearing issuer's
# strict RFC 8414 §3.1 metadata location inserts the well-known segment between
# authority and path (/.well-known/oauth-authorization-server/mcp). The SDK
# serves the OIDC-style path-suffix form (/mcp/.well-known/oauth-authorization-
# server) instead. The MCP spec directs clients to try the path-suffix form, so
# the real `mcp` client works; a hypothetical RFC-8414-only client that probes
# *only* the insertion form would miss it. PRM → issuer → AS-metadata still
# resolves because we follow the document.


def _start_fixed_port(app, port):
    """Start uvicorn bound to an already-chosen port (so the issuer can carry it)."""
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(500):
        if server.started:
            break
        time.sleep(0.02)
    assert server.started, "uvicorn did not start"
    return server, thread


def _free_port():
    """Reserve a free port, then release it so uvicorn can bind it next."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _seed_oauth_user(db_conn):
    """Seed a password user (username 'agent'/'pw'), grant 1 account + 1 message."""
    uid = create_user(db_conn, "agent", "pw")
    with db_conn.cursor() as cur:
        cur.execute("INSERT INTO accounts (name,email_address,imap_host,auth_method)"
                    " VALUES ('granted','g@x','imap.example.com','password') RETURNING id")
        granted = int(cur.fetchone()[0])
    grant_account(db_conn, uid, granted)
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO messages (account_id,message_id,raw_sha256,subject,"
            "body_text,headers,raw_bytes,size_bytes)"
            " VALUES (%s,'<oauth-int@x>',%s,'invoice','the invoice','{}'::jsonb,'r',1)",
            (granted, b"\x07" * 32))
    db_conn.commit()
    return granted


def _pkce_pair():
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    return verifier, challenge


async def _drive_with_token(port, token):
    """Authenticated MCP call using a bearer obtained via the full OAuth dance."""
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    url = f"http://127.0.0.1:{port}/mcp"
    headers = {"Authorization": f"Bearer {token}"}
    async with _mcp_http_client(headers) as client:
        async with streamable_http_client(url, http_client=client) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await session.call_tool("list_accounts", {})


def test_mcp_oauth_cold_connect_dance(db_dsn, db_conn):
    granted = _seed_oauth_user(db_conn)
    searcher = Searcher(pool=open_pool(db_dsn), cfg=SearchConfig(),
                        embeddings=None, reranker=None, rewriter=None)
    port = _free_port()
    issuer = f"http://127.0.0.1:{port}/mcp"
    app = create_app(
        db_dsn=db_dsn, searcher=searcher, enable_mcp=True,
        mcp_config=McpConfig(enabled=True, authorization_server_enabled=True,
                             resource_server_url=f"http://127.0.0.1:{port}"),
        serve_config=ServeConfig(state_signing_key="x" * 32))
    server, thread = _start_fixed_port(app, port)
    base = f"http://127.0.0.1:{port}"
    redirect_uri = "http://127.0.0.1:9/cb"  # need not resolve; we parse the code out
    try:
        # follow_redirects=False so we can read the 302/303 Location headers.
        with httpx.Client(timeout=10, follow_redirects=False) as c:
            # a. RFC 9728 protected-resource metadata -> issuer.
            prm = c.get(f"{base}/.well-known/oauth-protected-resource/mcp")
            assert prm.status_code == 200, prm.text
            issuer_url = prm.json()["authorization_servers"][0]
            assert issuer_url == issuer

            # b. AS metadata at <issuer>/.well-known/oauth-authorization-server.
            asmeta = c.get(
                issuer_url.rstrip("/") + "/.well-known/oauth-authorization-server")
            assert asmeta.status_code == 200, asmeta.text
            meta = asmeta.json()
            reg_ep = meta["registration_endpoint"]
            authz_ep = meta["authorization_endpoint"]
            token_ep = meta["token_endpoint"]
            # The metadata's endpoints must resolve under /mcp (the sub-mount),
            # i.e. discovery is self-consistent with the mounted routes.
            assert reg_ep == f"{base}/mcp/register"
            assert authz_ep == f"{base}/mcp/authorize"
            assert token_ep == f"{base}/mcp/token"

            # c. Dynamic client registration (public client, PKCE).
            reg = c.post(reg_ep, json={
                "redirect_uris": [redirect_uri],
                "token_endpoint_auth_method": "none",
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "client_name": "it",
            })
            assert reg.status_code in (200, 201), reg.text
            client_id = reg.json()["client_id"]

            # d. PKCE S256.
            verifier, challenge = _pkce_pair()

            # e. Authorize -> 302/303 to /oauth/consent?req=...
            authz = c.get(authz_ep, params={
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": "xyz",
            })
            assert authz.status_code in (302, 303), authz.text
            loc = authz.headers["location"]
            # The consent path is relative to the origin (top-level mount).
            consent_url = base + loc if loc.startswith("/") else loc
            assert "/oauth/consent" in consent_url
            req_blob = parse_qs(urlparse(consent_url).query)["req"][0]

            # f. Interactive consent: login + allow -> 303 to redirect_uri?code=...
            consent = c.post(f"{base}/oauth/consent", data={
                "req": req_blob,
                "username": "agent",
                "password": "pw",
                "decision": "allow",
            })
            assert consent.status_code == 303, consent.text
            cb = consent.headers["location"]
            cb_q = parse_qs(urlparse(cb).query)
            assert cb_q.get("state") == ["xyz"]
            code = cb_q["code"][0]

            # g. Token exchange (authorization_code + PKCE verifier).
            tok = c.post(token_ep, data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": client_id,
                "code_verifier": verifier,
            })
            assert tok.status_code == 200, tok.text
            tj = tok.json()
            access_token = tj["access_token"]
            refresh_token = tj["refresh_token"]
            assert access_token and refresh_token

            # i. Refresh grant -> a fresh access token.
            refreshed = c.post(token_ep, data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": client_id,
            })
            assert refreshed.status_code == 200, refreshed.text
            assert refreshed.json()["access_token"]

        # h. The KEY assertion: the dance-obtained access token authenticates a
        # real MCP tool call over Streamable HTTP, ACL-scoped to the grant.
        result = asyncio.run(_drive_with_token(port, access_token))
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        searcher._pool.close()

    payload = _payload(result)
    acct_list = payload if isinstance(payload, list) else payload.get("result", payload)
    ids = {a["id"] for a in acct_list}
    assert ids == {str(granted)}
