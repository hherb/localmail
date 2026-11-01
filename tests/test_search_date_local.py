# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""#426 item 5: the opt-in `date_local` hit field, the sender's own clock."""

from __future__ import annotations

import logging

import pytest

from localmail.api.search import run_search
from localmail.config import SearchConfig
from localmail.db import open_pool
from localmail.header_block import HEADER_BLOCK_READ_BYTES
from localmail.search.searcher import Searcher


class _E:
    name = "s"; model = "s"; dimension = 768
    def embed_documents(self, t): return [[1.0] * 768 for _ in t]
    def embed_query(self, t): return [0.5] * 768
    def health_check(self): pass


_RAW = {
    "brisbane": b"Date: Wed, 12 Jul 2023 09:58:24 +1000\r\nSubject: s\r\n\r\nbody",
    "nodate": b"Subject: s\r\n\r\nbody",
    # Only the first occurrence counts, as for `date_sent`.
    "twice": (b"Date: Mon, 1 Jan 2024 08:00:00 -0500\r\n"
              b"Date: Tue, 2 Jan 2024 08:00:00 +0000\r\n\r\nbody"),
    "lower": b"date: Mon, 1 Jan 2024 08:00:00 -0500\r\n\r\nbody",
    # Small headers, big body (every message with attachments): the prefix
    # holds the separator, so it must be served from the prefix.
    "bigbody": b"Date: Wed, 12 Jul 2023 09:58:24 +1000\r\n\r\n"
               + b"x" * (HEADER_BLOCK_READ_BYTES + 10_000),
    # A block past the read ceiling: no separator in the prefix.
    "huge": b"X-Pad: " + b"a" * HEADER_BLOCK_READ_BYTES
            + b"\r\nDate: Wed, 12 Jul 2023 09:58:24 +1000\r\n\r\nbody",
}


@pytest.fixture
def archive(db_dsn, db_conn):
    ids: dict[str, int] = {}
    with db_conn.cursor() as cur:
        cur.execute("INSERT INTO accounts (name,email_address,imap_host,auth_method)"
                    " VALUES ('a','a@x','h','password') RETURNING id")
        acct = cur.fetchone()[0]
        for i, (key, raw) in enumerate(_RAW.items()):
            cur.execute(
                "INSERT INTO messages (account_id, message_id, raw_sha256, subject,"
                " headers, raw_bytes, size_bytes)"
                " VALUES (%s, %s, %s, 's', '{}'::jsonb, %s, %s) RETURNING id",
                (acct, f"<m{i}>", bytes([i + 1]) * 32, raw, len(raw)),
            )
            ids[key] = cur.fetchone()[0]
    db_conn.commit()
    pool = open_pool(db_dsn)
    try:
        yield Searcher(pool=pool, cfg=SearchConfig(), embeddings=_E(),
                       reranker=None, rewriter=None), acct, ids
    finally:
        pool.close()


def _hits(archive, fields=None) -> dict[int, dict]:
    searcher, acct, _ = archive
    out = run_search(searcher=searcher, free_text="", filters={}, limit=50,
                     allowed_account_ids=[acct], user_id=1, fields=fields)
    assert out["results"]
    return {int(r["message_id"]): r for r in out["results"]}


def test_date_local_is_opt_in(archive) -> None:
    assert all("date_local" not in h for h in _hits(archive).values())


def test_date_local_is_the_header_s_own_clock(archive, caplog) -> None:
    _, _, ids = archive
    with caplog.at_level(logging.WARNING):
        hits = _hits(archive, fields=["message_id", "date_local"])
    assert hits[ids["brisbane"]] == {"message_id": str(ids["brisbane"]),
                                     "date_local": "2023-07-12T09:58:24+10:00"}
    assert hits[ids["nodate"]]["date_local"] is None
    assert hits[ids["twice"]]["date_local"] == "2024-01-01T08:00:00-05:00"
    assert hits[ids["lower"]]["date_local"] == "2024-01-01T08:00:00-05:00"
    assert hits[ids["bigbody"]]["date_local"] == "2023-07-12T09:58:24+10:00"
    assert f"message {ids['bigbody']}" not in caplog.text
    # Past the ceiling: re-read in full, with a WARNING, as `?headers=` does.
    assert hits[ids["huge"]]["date_local"] == "2023-07-12T09:58:24+10:00"
    assert f"message {ids['huge']}" in caplog.text


@pytest.mark.parametrize("fields", [None, ["message_id"]])
def test_the_read_is_skipped_unless_named(archive, monkeypatch, fields) -> None:
    searcher, _, _ = archive
    monkeypatch.setattr(searcher, "message_local_dates",
                        lambda ids: pytest.fail("read without being named"))
    _hits(archive, fields=fields)
