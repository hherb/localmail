"""Pure helpers describing the per-page outcome of a smart query rewrite.

No IO, no FastAPI — reusable by the api/ layer and any future transport.
The wire carries a ``rewrite_status`` (one of the constants below) plus an
optional curated ``rewrite_note``; raw exception text never leaves the
``Searcher`` (only these curated strings travel).
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

NOTE_UNAVAILABLE = "smart search is not configured on this server"
NOTE_NOT_ATTEMPTED = (
    "smart query rewriting applies to the first page only; "
    "this is a continuation page"
)
NOTE_UNREACHABLE = "could not reach the rewriter service"
NOTE_UNPARSEABLE = "the rewriter returned an unparseable response"

_SKIPPED_STATUSES: frozenset[str] = frozenset({UNAVAILABLE, FAILED})


def note_model_unavailable(model: str) -> str:
    """Actionable note for an Ollama 'model not pulled' (404) failure."""
    return (
        f"rewriter model {model!r} is not available; "
        f"pull it with: ollama pull {model}"
    )


def classify_rewrite_failure(exc: Exception, *, model: str) -> str:
    """Map a typed rewriter failure to a curated, actionable note."""
    if (
        isinstance(exc, httpx.HTTPStatusError)
        and exc.response.status_code == HTTPStatus.NOT_FOUND
    ):
        return note_model_unavailable(model)
    if isinstance(exc, RewriteParseError):
        return NOTE_UNPARSEABLE
    return NOTE_UNREACHABLE


def rewrite_skipped_for_status(status: str) -> bool:
    """Back-compat bool: a rewrite was *skipped* only when unavailable/failed."""
    return status in _SKIPPED_STATUSES
