# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""#426: attachment file names are searchable, and hits can name them."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import psycopg
import pytest

from localmail.api.search import run_search
from localmail.config import SearchConfig
from localmail.db import open_pool
from localmail.search.arms import arm_bm25_messages
from localmail.search.attachment_names import (
    NAMES_MAX_CHARS, names_match_sql, names_tsvector_sql,
)
from localmail.search.embed_worker import run_embed_worker_once
from localmail.search.query import parse_query
from localmail.search.searcher import Searcher

_MIGRATION = (Path(__file__).parent.parent / "migrations"
              / "0038_messages_attachment_names_strict_idx.sql")
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
        cur.execute("UPDATE messages SET attachments = %s::jsonb WHERE id = %s",
                    (json.dumps([{"filename": 5, "sha256": 7}]), ids["wanted"]))
    db_conn.commit()
    got = searcher.message_attachments([ids["filler"], ids["sibling"], ids["wanted"]])
    none = {"filename": None, "sha256": None, "content_type": None, "size": None}
    assert got[ids["filler"]] == []
    # The non-object keeps its slot, so entry i is still attachment index i.
    assert got[ids["sibling"]] == [{"filename": "y", "sha256": "zz",
                                    "content_type": None, "size": None}, none]
    # A non-string name or hash is no name, not its JSON text.
    assert got[ids["wanted"]] == [none]


def test_attachments_are_listed_in_stored_order(archive, db_conn) -> None:
    searcher, _, ids = archive
    names = ["b-second.pdf", "a-first.pdf", "c-third.pdf"]
    with db_conn.cursor() as cur:
        cur.execute("UPDATE messages SET attachments = %s::jsonb WHERE id = %s",
                    (json.dumps([{"filename": n, "sha256": _SHA} for n in names]),
                     ids["wanted"]))
    db_conn.commit()
    got = searcher.message_attachments([ids["wanted"]])[ids["wanted"]]
    assert [a["filename"] for a in got] == names


def test_a_name_only_match_scores_above_zero_in_arm_1(archive, db_conn) -> None:
    """Arm 1 ranks the names too; a name-only match scoring 0 would sit below
    every body match, which is the report's symptom."""
    _, _, ids = archive
    hits = arm_bm25_messages(db_conn, parse_query("230712"), SearchConfig(), limit=10)
    assert {h.message_id: h.arm_score for h in hits}[ids["wanted"]] > 0


@pytest.mark.parametrize("sort", ["rank", "date"])
def test_a_file_name_excludes_like_a_body_word(archive, sort) -> None:
    """A name matches a positive word, so it excludes a ``-word`` too."""
    searcher, acl, ids = archive
    found = _ids(searcher, acl, "attached -230712", sort=sort)
    assert ids["sibling"] in found
    assert ids["wanted"] not in found


def test_an_empty_name_list_does_not_satisfy_a_negation_only_query(archive) -> None:
    """``-herb OR -fee`` is satisfied by an empty document, and an
    attachment-less message's names vector is empty. The names half is off
    for such a query, so the body alone decides (#426 review)."""
    searcher, acl, ids = archive
    found = _ids(searcher, acl, "-herb OR -fee")
    assert ids["wanted"] in found
    assert ids["filler"] not in found


def _insert(conn, body: str, names: list[str]) -> int:
    with conn.cursor() as cur:
        cur.execute("INSERT INTO accounts (name,email_address,imap_host,auth_method)"
                    " VALUES ('big','b@x','h','password') RETURNING id")
        acct = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO messages (account_id, message_id, raw_sha256, subject,"
            " body_text, headers, raw_bytes, size_bytes, attachments)"
            " VALUES (%s, '<big>', %s, 's', %s, '{}'::jsonb, 'r', 1, %s::jsonb)"
            " RETURNING id",
            (acct, b"\x09" * 32, body, json.dumps([{"filename": n} for n in names])),
        )
        mid = cur.fetchone()[0]
    conn.commit()
    return mid


def _md5s(prefix: str, n: int) -> list[str]:
    return [hashlib.md5(f"{prefix}{i}".encode()).hexdigest() for i in range(n)]


def test_a_huge_name_list_still_inserts(db_conn) -> None:
    """The index expression runs on every INSERT; uncut, ~1.2 MB of names
    pass the 1 MiB tsvector cap and the message fails on every sync."""
    names = [h + ".pdf" for h in _md5s("n", 30000)]
    assert len(json.dumps(names)) > 20 * NAMES_MAX_CHARS
    _insert(db_conn, "body", names)


def test_arm_1_ranks_a_body_and_names_whose_sum_passes_the_cap(db_conn) -> None:
    """Each half fits the 1 MiB tsvector cap, together they do not: a rank
    over ``fts_v2 || names`` raised on every search matching the message."""
    mid = _insert(db_conn, "invoice " + " ".join(_md5s("b", 27000)),
                  [h + ".pdf" for h in _md5s("n", 30000)])
    # The fixture still overflows `||`. `length()` forces the evaluation a
    # bare `SELECT 1 FROM (SELECT expr)` would plan away.
    with pytest.raises(psycopg.errors.ProgramLimitExceeded), db_conn.cursor() as cur:
        cur.execute(f"SELECT length(m.fts_v2 || {names_tsvector_sql('m.attachments')})"
                    f" FROM messages m WHERE id = %s", (mid,))
    db_conn.rollback()
    hits = arm_bm25_messages(db_conn, parse_query("invoice"), SearchConfig(), limit=10)
    assert [h.message_id for h in hits] == [mid]


def test_the_name_match_can_use_the_index(db_conn) -> None:
    tsq = "websearch_to_tsquery('simple', '230712')"
    sql = f"EXPLAIN SELECT id FROM messages m WHERE {names_match_sql(tsq)}"
    with db_conn.cursor() as cur:
        cur.execute("SET LOCAL enable_seqscan = off")
        cur.execute(sql)
        plan = "\n".join(r[0] for r in cur.fetchall())
    db_conn.rollback()
    assert "messages_attachment_names_strict_idx" in plan
