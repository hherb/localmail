import time

import psycopg
import pytest

from localmail.api.auth import LOGIN_LOCKOUT_SECONDS, LOGIN_MAX_FAILURES, create_user, login, reset_login_rate_limiter
from localmail.api.errors import AuthenticationFailed, RateLimited


@pytest.fixture(autouse=True)
def _reset_limiter():
    reset_login_rate_limiter()
    yield
    reset_login_rate_limiter()


def test_login_rate_limited_after_max_failures(db_conn: psycopg.Connection) -> None:
    create_user(db_conn, "alice", "hunter2")
    db_conn.commit()
    for _ in range(LOGIN_MAX_FAILURES):
        with pytest.raises(AuthenticationFailed):
            login(db_conn, "alice", "wrong")
    with pytest.raises(RateLimited):
        login(db_conn, "alice", "wrong")


def test_rate_limit_does_not_leak_across_usernames(db_conn: psycopg.Connection) -> None:
    create_user(db_conn, "alice", "hunter2")
    create_user(db_conn, "bob", "correct horse")
    db_conn.commit()
    for _ in range(LOGIN_MAX_FAILURES):
        with pytest.raises(AuthenticationFailed):
            login(db_conn, "alice", "wrong")
    token, _ = login(db_conn, "bob", "correct horse")
    assert token


def test_successful_login_resets_failure_count(db_conn: psycopg.Connection) -> None:
    create_user(db_conn, "alice", "hunter2")
    db_conn.commit()
    for _ in range(LOGIN_MAX_FAILURES - 1):
        with pytest.raises(AuthenticationFailed):
            login(db_conn, "alice", "wrong")
    token, _ = login(db_conn, "alice", "hunter2")
    db_conn.commit()
    assert token
    with pytest.raises(AuthenticationFailed):
        login(db_conn, "alice", "wrong")  # one failure tolerated again
