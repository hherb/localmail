# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""A header the stdlib cannot parse, with 8-bit bytes, still serves (#380).

The read path parses `raw_bytes` per request, so the fallback's value reaches
both transports. Kept surrogate-escaped it made Starlette's renderer raise
after the handler succeeded (a 500), and the MCP SDK's serializer likewise.
The policy seam is `test_header_block._XAParseRaises`, for #314's reason.
"""
from __future__ import annotations

import email.policy

import pydantic_core
import pytest
from fastapi.testclient import TestClient

from localmail.serve.app import create_app

from .test_header_block import _XAParseRaises
from .test_serve_messages_routes import _seed_msg

_RAW = b"From: a@x\r\nX-A: caf\xc3\xa9 \xff\r\nSubject: ok\r\n\r\nhi"
_VALUE = "café �"


@pytest.mark.parametrize("mode", ["list", "full"])
def test_the_http_route_serves_the_decoded_value(
    db_dsn, api_token, db_conn, grant_alice_all_accounts, monkeypatch, mode,
):
    mid = _seed_msg(db_conn, raw=_RAW)
    grant_alice_all_accounts()
    monkeypatch.setattr(email.policy, "default", _XAParseRaises())
    c = TestClient(create_app(db_dsn=db_dsn, searcher=None))

    r = c.get(f"/v1/messages/{mid}?headers={mode}",
              headers={"Authorization": f"Bearer {api_token}"})

    assert r.status_code == 200, r.text
    headers = r.json()["headers"]
    if mode == "list":
        assert {"name": "X-A", "value": _VALUE} in headers
    else:
        assert headers["X-A"] == [_VALUE]


def test_the_mcp_tool_result_serializes(db_conn, monkeypatch):
    pytest.importorskip("mcp")
    from localmail.mcp import tools

    mid = _seed_msg(db_conn, raw=_RAW)
    with db_conn.cursor() as cur:
        cur.execute("SELECT account_id FROM messages WHERE id = %s", (mid,))
        row = cur.fetchone()
    assert row is not None
    monkeypatch.setattr(email.policy, "default", _XAParseRaises())

    msg = tools.tool_get_message(
        db_conn, message_id=mid, allowed_account_ids=[row[0]], headers="list",
    )

    assert {"name": "X-A", "value": _VALUE} in msg["headers"]
    # What FastMCP's `func_metadata.convert_result` calls on a tool's result.
    pydantic_core.to_json(msg, fallback=str, indent=2)
