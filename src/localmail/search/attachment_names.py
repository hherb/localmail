# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Attachment file names in the lexical match (#426).

``messages.fts_v2`` holds subject, from, body and to, but not the names in
``messages.attachments``, so a search for ``2312 Herb Fee Proposal
230712.pdf`` could only find that message through its body words. The names
get their own small tsvector, served by the expression index of migration
0037, rather than joining ``fts_v2``: that column is generated, so changing
it rewrites every row of ``messages`` (raw bytes included), and a second
index over ``fts_v2 || names`` would duplicate its 688 MB GIN index.

Each name is indexed twice. As written, so a quoted full name matches as a
phrase; and split on punctuation, because the ``simple`` parser reads
``230712.pdf`` as one file token, so ``230712`` alone would not match.
Weighted ``A``, like the subject.

The match is ``fts_v2 @@ q OR names @@ q``, not a match on the
concatenation, so that each half keeps its index. The price is that a query
whose words are split between a name and the body (``proposal`` only in the
file name, ``herb`` only in the body) matches neither half.
"""
from __future__ import annotations


def names_tsvector_sql(column: str) -> str:
    """The names tsvector over ``column`` (``m.attachments`` in a query).

    Must stay identical, up to the column reference, to migration 0037's
    index expression, or the planner cannot use the index (pinned by a
    test). Every function in it is immutable, and none raises on a
    malformed array: lax jsonpath skips what has no ``filename``.
    """
    names = f"jsonb_path_query_array({column}, '$[*].filename')::text"
    return (
        f"setweight(to_tsvector('simple', {names})"
        f" || to_tsvector('simple', regexp_replace({names}, '[^[:alnum:]]+', ' ', 'g')),"
        " 'A')"
    )


#: The expression as the hybrid arm and the date walk write it.
NAMES_TSV_SQL = names_tsvector_sql("m.attachments")
