# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""The one rule for "this message has attachments" (#364).

Three consumers compose it: the ``has_attachment`` filter in
``arms._filter_sql``, the hybrid path's hydration SELECT in
``Searcher._hydrate``, and the date walk's ``date_keyset.ROW_SQL_TEMPLATE``.
Before #364 the hit flag had its own rule ("the matched chunk was an
attachment's"), so a filtered search returned hits flagged ``false``. #365
narrows what counts (images embedded in the HTML body) by editing this
constant, and the filter and the flag move together.

It has its own module because ``arms`` imports ``searcher`` and ``searcher``
imports ``date_keyset`` at module level, so ``date_keyset`` taking this from
``arms`` would close an import cycle.
"""
from __future__ import annotations

#: True iff ``m.attachments`` is a non-empty array. Never NULL, so ``NOT``
#: of it is an exact complement.
#:
#: Guarded because ``jsonb_array_length`` raises 22023 on a non-array, and
#: ``messages.attachments`` is ``JSONB NOT NULL DEFAULT '[]'`` with no CHECK.
#: Unguarded, one such row fails every search page that surfaces it. A
#: ``CASE`` rather than ``AND``, because Postgres does not promise the order
#: in which ``AND``'s operands are evaluated. Contains no ``{}`` or ``%``, so
#: it composes safely into ``str.format`` templates and psycopg statements.
HAS_ATTACHMENT_SQL = (
    "(CASE WHEN jsonb_typeof(m.attachments) = 'array'"
    " THEN jsonb_array_length(m.attachments) > 0 ELSE FALSE END)"
)
