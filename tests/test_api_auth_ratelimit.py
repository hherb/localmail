"""Port of the original per-username + global rate-limit tests to the
Postgres-backed limiter. Multi-worker / per-IP semantics live in
test_api_auth_rate_limiter.py."""
import psycopg
import pytest

from localmail.api import auth as auth_mod
from localmail.api.auth import create_user, login, reset_login_rate_limiter
from localmail.api.errors import AuthenticationFailed, RateLimited
from localmail.config import AuthConfig


@pytest.fixture(autouse=True)
def _reset(db_conn: psycopg.Connection):
    reset_login_rate_limiter(db_conn)
    db_conn.commit()
    yield
    reset_login_rate_limiter(db_conn)
    db_conn.commit()


def test_login_rate_limited_after_max_failures(db_conn: psycopg.Connection) -> None:
    cfg = AuthConfig(login_per_user_max=5)
    create_user(db_conn, "alice", "hunter2")
    db_conn.commit()
    for _ in range(cfg.login_per_user_max):
        with pytest.raises(AuthenticationFailed):
            login(db_conn, "alice", "wrong", cfg=cfg)
        db_conn.commit()
    with pytest.raises(RateLimited):
        login(db_conn, "alice", "wrong", cfg=cfg)


def test_rate_limit_does_not_leak_across_usernames(db_conn: psycopg.Connection) -> None:
    cfg = AuthConfig(login_per_user_max=5, login_per_ip_max=100, login_global_max=100)
    create_user(db_conn, "alice", "hunter2")
    create_user(db_conn, "bob", "correct horse")
    db_conn.commit()
    for _ in range(cfg.login_per_user_max):
        with pytest.raises(AuthenticationFailed):
            login(db_conn, "alice", "wrong", cfg=cfg)
        db_conn.commit()
    token, _ = login(db_conn, "bob", "correct horse", cfg=cfg)
    db_conn.commit()
    assert token


def test_successful_login_resets_user_failure_count(db_conn: psycopg.Connection) -> None:
    cfg = AuthConfig(login_per_user_max=5)
    create_user(db_conn, "alice", "hunter2")
    db_conn.commit()
    for _ in range(cfg.login_per_user_max - 1):
        with pytest.raises(AuthenticationFailed):
            login(db_conn, "alice", "wrong", cfg=cfg)
        db_conn.commit()
    token, _ = login(db_conn, "alice", "hunter2", cfg=cfg)
    db_conn.commit()
    assert token
    with pytest.raises(AuthenticationFailed):
        login(db_conn, "alice", "wrong", cfg=cfg)


def test_global_login_rate_limit_caps_all_usernames(db_conn: psycopg.Connection) -> None:
    """Global limiter bounds argon2 CPU work no matter which username is tried."""
    cfg = AuthConfig(login_global_max=3, login_per_ip_max=100)
    create_user(db_conn, "alice", "hunter2")
    db_conn.commit()
    for u in ("alice", "bob", "charlie"):
        with pytest.raises(AuthenticationFailed):
            login(db_conn, u, "wrong", cfg=cfg)
        db_conn.commit()
    with pytest.raises(RateLimited) as ei:
        login(db_conn, "dave", "wrong", cfg=cfg)
    assert ei.value.cap == "global"
