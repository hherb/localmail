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
