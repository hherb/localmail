from localmail.api.errors import (
    APIError,
    AuthenticationFailed,
    InvalidToken,
    NotFound,
    RateLimited,
    ValidationFailed,
)


def test_each_error_carries_a_problem_type() -> None:
    for cls, status in [
        (AuthenticationFailed, 401),
        (InvalidToken, 401),
        (NotFound, 404),
        (RateLimited, 429),
        (ValidationFailed, 400),
    ]:
        err = cls("test message")
        assert isinstance(err, APIError)
        assert err.http_status == status
        assert err.problem_type.startswith("/problems/")
        assert err.detail == "test message"


def test_apierror_to_problem_dict() -> None:
    err = NotFound("no such message")
    problem = err.to_problem()
    assert problem["status"] == 404
    assert problem["type"] == err.problem_type
    assert problem["detail"] == "no such message"
    assert problem["title"]


def test_rate_limited_carries_cap_and_retry_after() -> None:
    exc = RateLimited("too many", cap="ip", retry_after_s=42)
    assert exc.detail == "too many"
    assert exc.cap == "ip"
    assert exc.retry_after_s == 42
    payload = exc.to_problem()
    assert payload["status"] == 429
    assert payload["cap"] == "ip"
    assert payload["retry_after_s"] == 42


def test_rate_limited_backwards_compat_without_cap() -> None:
    """Existing call sites raising RateLimited(detail) still work."""
    exc = RateLimited("legacy")
    assert exc.cap is None
    assert exc.retry_after_s is None
    payload = exc.to_problem()
    # When cap is None we omit it from the body to keep the contract clean.
    assert "cap" not in payload
    assert "retry_after_s" not in payload
