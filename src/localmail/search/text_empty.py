# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""The one rule for "this text carries nothing to index" (#266).

``chunk_attachment_text`` yields no chunks for text whose
``normalize_whitespace`` result is empty. Two places outside the chunker have
to answer that same question and **must** answer it identically:

- ``search/extractor.py`` — ``ExtractedText`` collapses such text to the ``''``
  sentinel on construction, so it is never stored in the first place.
- ``search/embed_worker.py`` — ``_chunk_attachments_lazily`` heals a legacy row
  that was stored before that boundary existed.

Writing the predicate twice is the drift this module exists to prevent: the
backstop's job is precisely "this row is one the boundary would have
collapsed", so if the two ever disagreed the backstop would start acting on
rows it was never meant to touch — and its action is a destructive, one-way
``UPDATE``.

``is_blank`` agrees with ``normalize_whitespace(text) == ""`` for every
possible ``str``, which ``tests/test_text_empty.py`` pins over the whole set of
characters Python considers whitespace. Pure: no IO, no imports.
"""

from __future__ import annotations


def is_blank(text: str) -> bool:
    """True iff ``text`` is empty or consists entirely of whitespace.

    ``str.isspace()`` rather than ``not text.strip()``: it short-circuits on the
    first character with substance and never allocates, where ``strip()`` copies
    the whole string whenever there is leading or trailing whitespace — which an
    extracted document nearly always has, and which can be megabytes.

    The empty string is handled explicitly because ``"".isspace()`` is ``False``.
    """
    return not text or text.isspace()
