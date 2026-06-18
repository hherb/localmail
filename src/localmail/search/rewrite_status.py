# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Pure helpers describing the per-page outcome of a smart query rewrite.

No IO, no FastAPI — reusable by the api/ layer and any future transport.
The wire carries a ``rewrite_status`` (one of the status constants), a
machine-readable ``rewrite_note_code`` (one of the code constants, or ``None``),
and the optional curated human ``rewrite_note``. The **code is canonical**: each
note is rendered *from* its code by ``note_for_code`` so the two never drift.
Raw exception text never leaves the ``Searcher`` (only these curated strings
travel).
"""
from __future__ import annotations

from http import HTTPStatus
from typing import Literal

import httpx

from localmail.search.rewriter import RewriteParseError

RewriteStatus = Literal[
    "applied", "unavailable", "failed", "not_attempted", "not_requested"
]

APPLIED: RewriteStatus = "applied"
UNAVAILABLE: RewriteStatus = "unavailable"
FAILED: RewriteStatus = "failed"
NOT_ATTEMPTED: RewriteStatus = "not_attempted"
NOT_REQUESTED: RewriteStatus = "not_requested"

RewriteNoteCode = Literal[
    "missing_model", "unreachable", "unparseable",
    "not_configured", "continuation_page",
]

MISSING_MODEL: RewriteNoteCode = "missing_model"
UNREACHABLE: RewriteNoteCode = "unreachable"
UNPARSEABLE: RewriteNoteCode = "unparseable"
NOT_CONFIGURED: RewriteNoteCode = "not_configured"
CONTINUATION_PAGE: RewriteNoteCode = "continuation_page"

NOTE_UNAVAILABLE = "smart search is not configured on this server"
NOTE_NOT_ATTEMPTED = (
    "smart query rewriting applies to the first page only; "
    "this is a continuation page"
)
NOTE_UNREACHABLE = "could not reach the rewriter service"
NOTE_UNPARSEABLE = "the rewriter returned an unparseable response"

_SKIPPED_STATUSES: frozenset[str] = frozenset({UNAVAILABLE, FAILED})

# Static (model-independent) notes keyed by code. ``missing_model`` is absent
# because its note interpolates the configured model name (see note_for_code).
_STATIC_NOTES: dict[RewriteNoteCode, str] = {
    UNREACHABLE: NOTE_UNREACHABLE,
    UNPARSEABLE: NOTE_UNPARSEABLE,
    NOT_CONFIGURED: NOTE_UNAVAILABLE,
    CONTINUATION_PAGE: NOTE_NOT_ATTEMPTED,
}


def note_model_unavailable(model: str) -> str:
    """Actionable note for an Ollama 'model not pulled' (404) failure."""
    return (
        f"rewriter model {model!r} is not available; "
        f"pull it with: ollama pull {model}"
    )


def classify_rewrite_failure(exc: Exception) -> RewriteNoteCode:
    """Map a typed rewriter failure to a stable machine-readable code.

    The code is model-independent; the human note is rendered later via
    ``note_for_code`` (which is where the configured model name is needed).
    """
    if (
        isinstance(exc, httpx.HTTPStatusError)
        and exc.response.status_code == HTTPStatus.NOT_FOUND
    ):
        return MISSING_MODEL
    if isinstance(exc, RewriteParseError):
        return UNPARSEABLE
    return UNREACHABLE


def note_for_code(code: RewriteNoteCode, *, model: str | None = None) -> str:
    """Render the curated human note for a code.

    The code is the single source of truth; the note is derived from it (and,
    for ``missing_model``, the configured model name). Total over the
    ``RewriteNoteCode`` Literal.
    """
    if code == MISSING_MODEL:
        if model is None:
            raise ValueError("missing_model note requires a model name")
        return note_model_unavailable(model)
    return _STATIC_NOTES[code]


def rewrite_skipped_for_status(status: str) -> bool:
    """Back-compat bool: a rewrite was *skipped* only when unavailable/failed."""
    return status in _SKIPPED_STATUSES
