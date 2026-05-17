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


class ValidationFailed(APIError):
    http_status = 400
    problem_type = "/problems/validation-failed"
    title = "Validation failed"
