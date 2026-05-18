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


def test_login_failures_dict_is_bounded(monkeypatch) -> None:
    """Memory cannot grow unboundedly under a username-rotating attacker.

    The per-username failure dict has a hard size cap; once exceeded, the
    least-recently-touched usernames are evicted (LRU). This is what stops
    `dict[str, ...]` blowing up to RAM-pressure size on adversarial traffic
    that rotates usernames faster than entries expire on their own.
    """
    from localmail.api import auth

    monkeypatch.setattr(auth, "LOGIN_FAILURES_MAX_USERS", 4)
    auth.reset_login_rate_limiter()
    for i in range(50):
        auth._record_login_failure(f"user-{i}")
    assert len(auth._LOGIN_FAILURES) <= 4


def test_global_login_rate_limit_caps_all_usernames(db_conn: psycopg.Connection, monkeypatch) -> None:
    """Global limiter bounds argon2 CPU work no matter which username is tried.

    Without this, an attacker can rotate usernames to bypass the per-username
    limit and induce unbounded argon2 verifies on the server.
    """
    from localmail.api import auth

    monkeypatch.setattr(auth, "LOGIN_GLOBAL_MAX_PER_WINDOW", 3)
    auth.reset_login_rate_limiter()
    create_user(db_conn, "alice", "hunter2")
    db_conn.commit()

    for u in ("alice", "bob", "charlie"):
        with pytest.raises(AuthenticationFailed):
            auth.login(db_conn, u, "wrong")
    with pytest.raises(RateLimited):
        auth.login(db_conn, "dave", "wrong")
