# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""#429: the file-name rules agree with ``HAS_ATTACHMENT_SQL`` on a malformed
``attachments`` column.

The column is ``JSONB NOT NULL DEFAULT '[]'`` with no CHECK, and no writer
produces these shapes (a restore or a hand ``UPDATE`` can). A lax jsonpath
wraps a top-level object and unwraps a nested array or an array-valued name,
so all three used to match the name rules, and the first one contradicted
``HAS_ATTACHMENT_SQL``: ``has:no-attachment attachment_name:fee`` returned a
row whose own ``has_attachments`` flag said false.
"""

from __future__ import annotations

import json

import pytest

from localmail.api.search import run_search
from localmail.config import SearchConfig
from localmail.db import open_pool
from localmail.search.attachment_names import NAMES_TSV_SQL
from localmail.search.searcher import Searcher

_MALFORMED = {
    "object": {"filename": "fee.pdf"},
    "nested": [[{"filename": "fee.pdf"}]],
    "array_name": [{"filename": ["fee.pdf"]}],
}
#: A well-formed entry among malformed ones still counts: one bad entry must
#: not hide its siblings.
_MIXED = [1, None, {"filename": 3}, {"x": "fee"}, {"filename": "fee.pdf"}]


class _E:
    name = "s"; model = "s"; dimension = 768
    def embed_documents(self, t): return [[1.0] * 768 for _ in t]
    def embed_query(self, t): return [0.5] * 768
    def health_check(self): pass


@pytest.fixture
def archive(db_dsn, db_conn):
    rows = {**_MALFORMED, "mixed": _MIXED}
    ids: dict[str, int] = {}
    with db_conn.cursor() as cur:
        cur.execute("INSERT INTO accounts (name,email_address,imap_host,auth_method)"
                    " VALUES ('a','a@x','h','password') RETURNING id")
        acct = cur.fetchone()[0]
        for i, (key, atts) in enumerate(rows.items()):
            # Subject and body carry no "fee", so only a name can match.
            cur.execute(
                "INSERT INTO messages (account_id, message_id, raw_sha256, subject,"
                " body_text, headers, raw_bytes, size_bytes, attachments)"
                " VALUES (%s, %s, %s, 's', 'b', '{}'::jsonb, 'r', 1, %s::jsonb)"
                " RETURNING id",
                (acct, f"<m{i}>", bytes([i + 1]) * 32, json.dumps(atts)),
            )
            ids[key] = cur.fetchone()[0]
    db_conn.commit()
    pool = open_pool(db_dsn)
    try:
        yield Searcher(pool=pool, cfg=SearchConfig(), embeddings=_E(),
                       reranker=None, rewriter=None), acct, ids
    finally:
        pool.close()


def _found(archive, query: str, filters: dict, sort: str | None = None) -> set[int]:
    searcher, acct, _ = archive
    out = run_search(searcher=searcher, free_text=query, filters=filters, limit=50,
                     allowed_account_ids=[acct], user_id=1, sort=sort)
    return {int(r["message_id"]) for r in out["results"]}


def test_the_filter_reads_only_well_formed_names(archive) -> None:
    _, _, ids = archive
    assert _found(archive, "", {"attachment_name": "fee"}) == {ids["mixed"]}


@pytest.mark.parametrize("sort", ["rank", "date"])
def test_the_lexical_match_reads_only_well_formed_names(archive, sort) -> None:
    _, _, ids = archive
    assert _found(archive, "fee", {}, sort=sort) == {ids["mixed"]}


def test_a_non_array_is_not_named_by_a_message_without_attachments(archive) -> None:
    """The headline contradiction: the filter and the flag disagreed."""
    assert _found(archive, "", {"attachment_name": "fee",
                                "has_attachment": False}) == set()


@pytest.mark.parametrize("shape", sorted(_MALFORMED))
def test_a_malformed_column_has_an_empty_names_vector(db_conn, shape) -> None:
    with db_conn.cursor() as cur:
        cur.execute(f"SELECT ({NAMES_TSV_SQL})::text FROM (SELECT %s::jsonb AS"
                    f" attachments) m", (json.dumps(_MALFORMED[shape]),))
        assert cur.fetchone()[0] == ""
