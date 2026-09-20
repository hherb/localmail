# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Compact search hits: the `fields` projection and `snippet_chars` (slice E).

Pure — no IO. The one authority for which hit keys a caller may name and for
the range a caller may ask a snippet to span. Each rule returns a message or
``None`` (the ``account_names.account_name_error`` shape); the caller decides
what an error *is* (``run_search`` raises ``ValidationFailed``, a 400).

Spec: docs/superpowers/specs/2026-09-19-search-hit-projection-design.md
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any

#: Every name a `fields` projection may ask for, in the order the projected
#: hit's keys come back. The first eleven are exactly the keys
#: ``api.search._to_api_result`` emits (pinned by a test, so a hit key added
#: later cannot go missing here); ``snippet`` is the honest, plain-text name
#: for ``snippet_html``, which has never held HTML. It is emitted only when
#: named, so the default response stays byte-identical.
HIT_FIELDS: tuple[str, ...] = (
    "message_id", "account", "folder", "subject", "from", "to", "date",
    "snippet_html", "has_attachments", "score", "matched_arms", "snippet",
)

_SNIPPET_ALIAS_OF = "snippet_html"


def fields_error(fields: object) -> str | None:
    """Why ``fields`` cannot be honoured, or ``None`` when it can.

    Refused: a non-list, an empty list (hits with no keys is a caller bug,
    not a request), a non-string element, and any name outside
    ``HIT_FIELDS`` — all of them named, so the caller can fix the call.
    Duplicates are accepted: the projection is a mapping, so they collapse
    without losing anything.
    """
    if not isinstance(fields, list):
        return "fields must be a list of hit field names"
    if not fields:
        return ("fields must not be empty; omit it for the full hit, or name "
                f"at least one of: {', '.join(HIT_FIELDS)}")
    if any(not isinstance(name, str) for name in fields):
        return "fields must contain only strings"
    unknown = sorted({name for name in fields if name not in HIT_FIELDS})
    if not unknown:
        return None
    noun = "field" if len(unknown) == 1 else "fields"
    return (f"unknown hit {noun} {', '.join(repr(n) for n in unknown)}; "
            f"supported: {', '.join(HIT_FIELDS)}")


def snippet_chars_error(value: object, *, max_chars: int) -> str | None:
    """Why ``value`` cannot size a snippet window, or ``None`` when it can.

    ``bool`` is refused explicitly: it is an ``int`` subclass, so ``True``
    would otherwise read as a one-character window. Out of range is refused
    rather than clamped — a silently clamped value is an answer to a
    question the caller did not ask.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return f"snippet_chars must be an integer (got {value!r})"
    if not 1 <= value <= max_chars:
        return f"snippet_chars must be between 1 and {max_chars} (got {value})"
    return None


def project_hit(hit: dict[str, Any], fields: Sequence[str]) -> dict[str, Any]:
    """Return a new hit carrying exactly ``fields``, in ``HIT_FIELDS`` order.

    ``fields`` must already have passed ``fields_error``. An unvalidated name
    raises ``KeyError`` here rather than being dropped — a loud bug at the
    one boundary that can still see it. ``hit`` is not mutated.
    """
    source = {**hit, "snippet": hit[_SNIPPET_ALIAS_OF]}
    wanted = set(fields)
    for name in wanted:
        if name not in HIT_FIELDS:
            raise KeyError(name)
    return {name: source[name] for name in HIT_FIELDS if name in wanted}
