# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Which of the two date-ordered walks a keyset cursor continues (#326).

``Searcher._date_keyset_search`` serves two intents through one query. With
free text it is the lexical walk, whose FTS predicate is **rebuilt from the
query the caller re-sends on every page**; with none it is the archive walk,
which has no such predicate and needs nothing from the query at all.

Before #322 those were told apart by refusing a keyset cursor presented with
a blank query. #322 taught the archive walk to paginate and removed the
guard with the premise it rested on — correctly, since that refusal would
have forbidden the new pagination outright. What it also removed was the
one client mistake that had been caught by construction: paging a *text*
search without re-sending ``query`` is now served as the next ``limit``
messages of the whole archive, presented as a continuation of the search.
``docs/mcp-usage.md`` tells agents to re-send it, and agents are the
reported audience for this cluster, so that is the likeliest mistake there
is — and it fails silently.

The distinction therefore rides on the cursor itself, which is what lets the
check come back for that one pair without touching the blank-query paging.
This module owns both halves of it: the rule that decides a walk's kind, and
the rule that judges a cursor against the query it arrived with. Written
apart, those are two predicates for one question — which is exactly the
shape of the #308 follow-up defect, where the api gate and the retrieval
branch disagreed about what counted as a blank query.

Shaped like ``account_names.account_name_error`` and
``rewriter_url.base_url_error``: a message, or ``None``. The caller decides
what an error *is* — ``ValidationFailed`` at the api boundary, a named
``ValueError`` inside the Searcher — so the wording cannot drift between
them.
"""
from __future__ import annotations

from typing import Literal

#: Which walk minted a position. ``text`` carries an FTS predicate the next
#: page must rebuild; ``archive`` carries none.
KeysetWalk = Literal["text", "archive"]


def walk_for_text(free_text: str) -> KeysetWalk:
    """The walk a query of ``free_text`` takes.

    **The one authority**, called both where the branch is chosen and where
    the cursor is stamped, so a cursor cannot record a walk its query did
    not take. ``free_text`` must already be
    ``parse_query(...).free_text`` — the filter operators are lifted out by
    then, and ``subject:invoice`` is a non-empty *request field* that leaves
    no free text behind for an FTS predicate to be built from.
    """
    return "text" if free_text.strip() else "archive"


def keyset_walk_error(*, cursor_walk: KeysetWalk, free_text: str) -> str | None:
    """Why this cursor cannot continue under ``free_text``, or ``None``.

    Only one pair is refused: a **text** cursor with no free text. The walk
    that minted it rebuilds its FTS predicate from the re-sent query, so
    without one there is nothing to continue and the caller would silently
    be handed the archive walk instead.

    An **archive** cursor is accepted either way, deliberately. It has no
    predicate to rebuild, so the query does not bear on continuing it —
    refusing a blank query there would forbid precisely the pagination #322
    added. This is why the rule is keyed on the cursor's own walk rather
    than on "a cursor plus a blank query", which is the shape the pre-#322
    guard had and the reason it had to go.

    Nothing here re-litigates a *changed* query. A keyset cursor has only
    ever identified a position, and varying the free text or ``folder_ids``
    between pages was undefined before #322 and is undefined now; what this
    restores is the one case that used to be impossible to get wrong by
    accident.
    """
    if cursor_walk == "text" and walk_for_text(free_text) == "archive":
        return (
            "this cursor continues a text search; re-send the original "
            "'query' alongside it"
        )
    return None
