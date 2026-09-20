# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""The one rule for a caller-supplied snippet window width (#390).

Pure — no IO. Returns a message, or ``None`` when the width can be honoured
(the ``account_names.account_name_error`` shape); the caller decides what an
error *is* — ``run_search`` raises ``ValidationFailed`` (a 400),
``Searcher._snippet_width`` raises ``ValueError`` before any IO.

Slice E wrote the type check and the floor twice, once per layer, in two
wordings — so ``"10"`` earned a different sentence for an identical mistake,
and a decision to accept (say) an integral ``float`` could land at one layer
and silently not at the other. That is this repo's recorded failure mode for
a rule stated twice, and the reason this module exists.

**The cap is an argument, not a second rule.** An operator's
``search.snippet_max_chars`` bounds what a *network* caller may ask for; a
library caller asking for a 5,000-character window is not doing anything
wrong, and ``make_snippet`` is correct for any positive width. Passing that
difference in is what keeps one implementation honest about serving two
policies — the alternative, a separate uncapped rule for the Searcher, is
the duplication again with an extra step.

It lives under ``search/`` rather than beside its api consumer because
``search/`` must not import ``api/``: that would be the first ``search →
api`` edge in the tree, and the Searcher is the layer with no cap.
"""
from __future__ import annotations

#: The smallest useful window. `make_snippet` slices `chunk_text[:width]`
#: on its no-match branch, so a non-positive width empties every snippet
#: (and a negative one returns nearly the whole chunk).
MIN_WIDTH = 1


def snippet_width_error(value: object, *, max_chars: int | None) -> str | None:
    """Why ``value`` cannot size a snippet window, or ``None`` when it can.

    ``max_chars`` is keyword-only with **no default**: ``None`` means
    uncapped, which is the *permissive* reading, so it has to be written at
    the call site rather than arrived at by forgetting the argument
    (``allowed_account_ids``' shape, #234). Forgetting it is a
    ``TypeError``, never a silently uncapped network boundary.

    ``bool`` is refused explicitly: it is an ``int`` subclass, so ``True``
    would otherwise read as a one-character window. Out of range is refused
    rather than clamped — a silently clamped value answers a question the
    caller did not ask.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return f"snippet_chars must be an integer (got {value!r})"
    if max_chars is None:
        # No ceiling to name, so naming one would send a library caller to a
        # limit that does not apply to them.
        if value < MIN_WIDTH:
            return f"snippet_chars must be at least {MIN_WIDTH} (got {value})"
        return None
    if not MIN_WIDTH <= value <= max_chars:
        return f"snippet_chars must be between {MIN_WIDTH} and {max_chars} (got {value})"
    return None
