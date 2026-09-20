# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Compact search hits: the `fields` projection (slice E).

Pure — no IO. The one authority for which hit keys a caller may name. The
rule returns a message or ``None`` (the
``account_names.account_name_error`` shape); the caller decides what an
error *is* (``run_search`` raises ``ValidationFailed``, a 400).

The sibling argument, ``snippet_chars``, is **not** ruled on here: its type
and floor are identical for a library caller, so they live in
``search.snippet_width`` and this boundary supplies only the cap (#390).

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


def project_hit(hit: dict[str, Any], fields: Sequence[str]) -> dict[str, Any]:
    """Return a new hit carrying exactly ``fields``, in ``HIT_FIELDS`` order.

    ``fields`` must already have passed ``fields_error``, and that
    precondition is re-checked here rather than restated: an unvalidated
    ``fields`` raises ``ValueError`` rather than being honoured — a loud bug
    at the one boundary that can still see it. ``hit`` is not mutated.

    The check *delegates* (#392). The hand-written guard it replaced
    re-derived a subset of ``fields_error``'s judgement — names outside
    ``HIT_FIELDS``, and nothing else — so an **empty** list, the one shape
    ``fields_error`` calls a caller bug rather than a request, passed
    through and returned a hit with no keys at all. ``run_search`` gates
    ~250 lines before the call, with nothing between them carrying the fact
    of validation, which is why the precondition is written down; the
    enforcement has to be the rule itself, not a paraphrase of it.

    ``fields`` is materialised **once**, into ``names``, and both the check
    and the projection read that binding. Traversing the parameter twice
    drains a one-shot iterable on the first pass, so the second sees nothing
    and the comprehension returns a hit with no keys — silently, and
    verbatim the outcome the delegation above exists to end. The guard this
    replaced read ``set(fields)`` first and iterated *that*, so it was
    single-traversal by accident; the delegation has to be so on purpose.

    Cost is O(``len(fields)`` × ``len(HIT_FIELDS)``) per hit — the bound is
    the caller's list, not ``HIT_FIELDS``, since ``fields_error`` accepts
    duplicates and the wire field is uncapped (#394). At a dozen names and
    ≤200 hits a page that is a fraction of a millisecond.
    """
    names = list(fields)
    if (error := fields_error(names)) is not None:
        raise ValueError(error)
    source = {**hit, "snippet": hit[_SNIPPET_ALIAS_OF]}
    wanted = set(names)
    return {name: source[name] for name in HIT_FIELDS if name in wanted}
