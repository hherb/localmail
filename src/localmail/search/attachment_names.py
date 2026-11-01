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
``230712.pdf`` as one token (of type ``host``), so ``230712`` alone would
not match. Weighted ``A``, like the subject.

The match is ``fts_v2 @@ q OR names @@ q``, not a match on the
concatenation, so that each half keeps its index. Two prices. Lexically, a
query's *required* words must all sit in the name or all in the message:
split between them (``proposal`` only in the file name, ``herb`` only in
the body) it matches neither half. And a query an empty document satisfies
(``-acme OR -beta``) would match every message through an empty names
vector, so ``names_match_sql`` turns the names half off for such a query;
``invoice OR -beta`` then finds ``invoice.pdf`` only through the body.
"""
from __future__ import annotations


def names_tsvector_sql(column: str) -> str:
    """The names tsvector over ``column`` (``m.attachments`` in a query).

    Must stay identical, up to the column reference, to migration 0037's
    index expression, or the planner cannot use the index (pinned by a
    test). Every function in it is immutable, and none raises on a
    malformed array: lax jsonpath skips what has no ``filename``. The text
    is cut at ``NAMES_MAX_CHARS`` because ``to_tsvector`` raises past the
    1 MiB tsvector cap, and this expression runs on every INSERT into
    ``messages``: an uncut name list that large would fail its message on
    every sync and retry, and stop migration 0037 on an archive holding one.
    """
    names = (f"left(jsonb_path_query_array({column}, '$[*].filename')::text,"
             f" {NAMES_MAX_CHARS})")
    return (
        f"setweight(to_tsvector('simple', {names})"
        f" || to_tsvector('simple', regexp_replace({names}, '[^[:alnum:]]+', ' ', 'g')),"
        " 'A')"
    )


#: About 1000 file names; past that the tail is not searchable. Both halves
#: together stay a few hundred KB below the 1 MiB cap even for short
#: multi-byte words (50000 characters of md5-shaped names make 84 KB of
#: lexemes).
NAMES_MAX_CHARS = 50000

#: The expression as the hybrid arm and the date walk write it.
NAMES_TSV_SQL = names_tsvector_sql("m.attachments")


def names_match_sql(tsq_sql: str) -> str:
    """The names half of the lexical match, for the tsquery ``tsq_sql``.

    Off for a query the empty document satisfies; see the module docstring.
    ``tsq_sql`` appears twice, so its parameters must be passed twice.
    """
    return f"({NAMES_TSV_SQL} @@ {tsq_sql} AND NOT ''::tsvector @@ {tsq_sql})"


#: The ``attachment_name:`` filter: some attachment's file name contains the
#: one parameter, case-insensitively. ``strpos`` rather than ``ILIKE`` so a
#: ``_`` or ``%`` in the value is literal, as it is in a file name. Lax
#: jsonpath skips an entry with no ``filename`` and the type test a non-string
#: one, so a malformed column neither raises nor matches. Unindexed: it runs
#: per row the rest of the query admits.
NAME_CONTAINS_SQL = (
    "EXISTS (SELECT 1 FROM jsonb_path_query(m.attachments,"
    " '$[*].filename ? (@.type() == \"string\")') AS n(name)"
    " WHERE strpos(lower(n.name #>> '{}'), lower(%s)) > 0)"
)
