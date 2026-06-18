# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Typed exceptions for the localmail.api layer.

Each subclass declares its HTTP status + RFC 7807 problem type.
serve/middleware.py turns these into application/problem+json responses.
"""
from __future__ import annotations


class APIError(Exception):
    """Base for all api-layer errors."""

    http_status: int = 500
    problem_type: str = "/problems/internal-error"
    title: str = "Internal error"

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail

    def to_problem(self) -> dict[str, object]:
        return {
            "type": self.problem_type,
            "title": self.title,
            "status": self.http_status,
            "detail": self.detail,
        }


class AuthenticationFailed(APIError):
    http_status = 401
    problem_type = "/problems/authentication-failed"
    title = "Authentication failed"


class InvalidToken(APIError):
    http_status = 401
    problem_type = "/problems/invalid-token"
    title = "Invalid or expired token"


class NotFound(APIError):
    http_status = 404
    problem_type = "/problems/not-found"
    title = "Not found"


class RateLimited(APIError):
    http_status = 429
    problem_type = "/problems/rate-limited"
    title = "Too many requests"

    def __init__(
        self,
        detail: str,
        *,
        cap: str | None = None,
        retry_after_s: int | None = None,
    ) -> None:
        super().__init__(detail)
        self.cap = cap
        self.retry_after_s = retry_after_s

    def to_problem(self) -> dict[str, object]:
        payload = super().to_problem()
        if self.cap is not None:
            payload["cap"] = self.cap
        if self.retry_after_s is not None:
            payload["retry_after_s"] = self.retry_after_s
        return payload


class ValidationFailed(APIError):
    http_status = 400
    problem_type = "/problems/validation-failed"
    title = "Validation failed"


class FeatureUnavailable(APIError):
    http_status = 503
    problem_type = "/problems/feature-unavailable"
    title = "Feature unavailable"


class SearchCursorExpired(APIError):
    """The page cursor pool has been evicted (TTL, LRU, cross-user replay).

    Clients should re-run the original query without a cursor and resume
    scrolling from where they left off — the transparent recovery path is
    documented in the pagination spec.
    """
    http_status = 409
    problem_type = "/problems/search-cursor-expired"
    title = "Search cursor expired"
