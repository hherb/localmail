# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""#426: attachment file names are searchable, and hits can name them."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from localmail.api.search import run_search
from localmail.config import SearchConfig
from localmail.db import open_pool
from localmail.search.attachment_names import names_tsvector_sql
from localmail.search.embed_worker import run_embed_worker_once
from localmail.search.searcher import Searcher

_MIGRATION = (Path(__file__).parent.parent / "migrations"
              / "0037_messages_attachment_names_idx.sql")
_WANTED = "2312 Herb Fee Proposal 230712.pdf"
_SHA = "ab" * 32


class _E:
    name = "s"; model = "s"; dimension = 768
    def embed_documents(self, t): return [[1.0] * 768 for _ in t]
    def embed_query(self, t): return [0.5] * 768
    def health_check(self): pass


def _squash(sql: str) -> str:
    return re.sub(r"\s+", " ", sql)


def test_the_index_expression_is_the_query_expression() -> None:
    # The planner matches an expression index only on an identical tree.
    assert _squash(names_tsvector_sql("attachments")) in _squash(_MIGRATION.read_text())


def _seed(conn) -> tuple[list[int], dict[str, int]]:
    """The report's shape: the wanted file's message matches only by name;
    its sibling and a filler both match the words in their bodies."""
    rows = {
        "filler": ("Herb fee proposal", "the herb fee proposal 2312", []),
        "sibling": ("Fee Proposal", "2312 Herb fee proposal attached",
                    ["2312 Herb Fee Proposal 230710.pdf"]),
        # Seeded last: the vector arms tie on these fixtures and break the
        # tie toward the lowest id, which would rank it first without #426.
        "wanted": ("Documents", "see attached", [_WANTED]),
    }
    ids: dict[str, int] = {}
    with conn.cursor() as cur:
        cur.execute("INSERT INTO accounts (name,email_address,imap_host,auth_method)"
                    " VALUES ('a','a@x','h','password') RETURNING id")
        acct = cur.fetchone()[0]
        cur.execute("INSERT INTO attachment_blobs (sha256, path, mime_type, size_bytes)"
                    " VALUES (%s, 'p', 'application/pdf', 1234)", (bytes.fromhex(_SHA),))
        for i, (key, (subject, body, names)) in enumerate(rows.items()):
            atts = [{"filename": n, "sha256": _SHA} for n in names]
            cur.execute(
                "INSERT INTO messages (account_id, message_id, raw_sha256, subject,"
                " body_text, headers, raw_bytes, size_bytes, attachments)"
                " VALUES (%s, %s, %s, %s, %s, '{}'::jsonb, 'r', 1, %s::jsonb)"
                " RETURNING id",
                (acct, f"<m{i}>", bytes([i + 1]) * 32, subject, body, json.dumps(atts)),
            )
            ids[key] = cur.fetchone()[0]
    conn.commit()
    return [acct], ids


@pytest.fixture
def archive(db_dsn, db_conn):
    acl, ids = _seed(db_conn)
    cfg = SearchConfig()
    run_embed_worker_once(db_conn, cfg, _E())
    pool = open_pool(db_dsn)
    try:
        yield Searcher(pool=pool, cfg=cfg, embeddings=_E(), reranker=None,
                       rewriter=None), acl, ids
    finally:
        pool.close()


def _ids(searcher, acl, query, **kw) -> list[int]:
    out = run_search(searcher=searcher, free_text=query, filters={}, limit=10,
                     allowed_account_ids=acl, user_id=1, **kw)
    return [int(r["message_id"]) for r in out["results"]]


@pytest.mark.parametrize("query", [f'"{_WANTED}"', _WANTED, "230712"])
@pytest.mark.parametrize("sort", ["rank", "date"])
def test_a_file_name_finds_its_message(archive, query, sort) -> None:
    searcher, acl, ids = archive
    found = _ids(searcher, acl, query, sort=sort)
    assert ids["wanted"] in found
    if sort == "rank":
        assert found[0] == ids["wanted"]
    if query == "230712" and sort == "date":
        # The date fragment is in no body and in no other name. (The rank
        # path's vector arms add every message as a neighbour, by design.)
        assert found == [ids["wanted"]]


def test_attachments_are_opt_in_on_a_hit(archive) -> None:
    searcher, acl, ids = archive
    default = run_search(searcher=searcher, free_text="230712", filters={},
                         limit=10, allowed_account_ids=acl, user_id=1)
    assert "attachments" not in default["results"][0]
    assert "attachment_filename" not in default["results"][0]

    out = run_search(searcher=searcher, free_text="230712", filters={}, limit=10,
                     allowed_account_ids=acl, user_id=1,
                     fields=["message_id", "attachment_filename", "attachments"])
    hit = out["results"][0]
    assert hit["message_id"] == str(ids["wanted"])
    assert hit["attachments"] == [{"filename": _WANTED, "sha256": _SHA,
                                   "content_type": "application/pdf", "size": 1234}]
    # Matched by name, not by an attachment's extracted text.
    assert hit["attachment_filename"] is None


def test_a_message_without_attachments_lists_none(archive) -> None:
    searcher, _, ids = archive
    assert searcher.message_attachments([ids["filler"]]) == {ids["filler"]: []}


def test_a_malformed_attachments_column_does_not_fail_the_listing(archive, db_conn) -> None:
    searcher, _, ids = archive
    with db_conn.cursor() as cur:
        cur.execute("UPDATE messages SET attachments = %s::jsonb WHERE id = %s",
                    (json.dumps({"filename": "x"}), ids["filler"]))
        cur.execute("UPDATE messages SET attachments = %s::jsonb WHERE id = %s",
                    (json.dumps([{"filename": "y", "sha256": "zz"}, 3]), ids["sibling"]))
    db_conn.commit()
    got = searcher.message_attachments([ids["filler"], ids["sibling"]])
    assert got[ids["filler"]] == []
    assert got[ids["sibling"]] == [{"filename": "y", "sha256": "zz",
                                    "content_type": None, "size": None}]


def test_the_name_match_can_use_the_index(db_conn) -> None:
    sql = (f"EXPLAIN SELECT id FROM messages m WHERE "
           f"{names_tsvector_sql('m.attachments')} @@ websearch_to_tsquery('simple', '230712')")
    with db_conn.cursor() as cur:
        cur.execute("SET LOCAL enable_seqscan = off")
        cur.execute(sql)
        plan = "\n".join(r[0] for r in cur.fetchall())
    db_conn.rollback()
    assert "messages_attachment_names_idx" in plan
