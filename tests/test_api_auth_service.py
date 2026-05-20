from datetime import timedelta

import psycopg
import pytest

from localmail.api.auth import (
    AuthenticatedUser,
    create_user,
    hash_password,
    login,
    logout,
    refresh_token,
    whoami,
)
from localmail.api.errors import AuthenticationFailed, InvalidToken


def _seed_user(conn: psycopg.Connection, username: str = "alice", password: str = "hunter2") -> int:
    return create_user(conn, username, password)


def test_create_user_returns_id(db_conn: psycopg.Connection) -> None:
    uid = _seed_user(db_conn)
    db_conn.commit()
    assert uid > 0


def test_login_with_correct_password_returns_token(db_conn: psycopg.Connection) -> None:
    _seed_user(db_conn)
    db_conn.commit()
    token, expires_at = login(db_conn, "alice", "hunter2")
    db_conn.commit()
    assert isinstance(token, str)
    assert expires_at is not None


def test_login_with_wrong_password_raises(db_conn: psycopg.Connection) -> None:
    _seed_user(db_conn)
    db_conn.commit()
    with pytest.raises(AuthenticationFailed):
        login(db_conn, "alice", "wrong")


def test_login_for_unknown_user_raises(db_conn: psycopg.Connection) -> None:
    with pytest.raises(AuthenticationFailed):
        login(db_conn, "nobody", "anything")


def test_login_for_disabled_user_raises(db_conn: psycopg.Connection) -> None:
    uid = _seed_user(db_conn)
    with db_conn.cursor() as cur:
        cur.execute("UPDATE api_users SET disabled_at = now() WHERE id = %s", (uid,))
    db_conn.commit()
    with pytest.raises(AuthenticationFailed):
        login(db_conn, "alice", "hunter2")


def test_whoami_returns_user(db_conn: psycopg.Connection) -> None:
    _seed_user(db_conn)
    db_conn.commit()
    token, _ = login(db_conn, "alice", "hunter2")
    db_conn.commit()
    user = whoami(db_conn, token)
    assert isinstance(user, AuthenticatedUser)
    assert user.username == "alice"


def test_whoami_raises_for_bogus_token(db_conn: psycopg.Connection) -> None:
    with pytest.raises(InvalidToken):
        whoami(db_conn, "bogus")


def test_logout_revokes_token(db_conn: psycopg.Connection) -> None:
    _seed_user(db_conn)
    db_conn.commit()
    token, _ = login(db_conn, "alice", "hunter2")
    db_conn.commit()
    logout(db_conn, token)
    db_conn.commit()
    with pytest.raises(InvalidToken):
        whoami(db_conn, token)


def test_refresh_token_issues_new_and_revokes_old(db_conn: psycopg.Connection) -> None:
    _seed_user(db_conn)
    db_conn.commit()
    old_token, _ = login(db_conn, "alice", "hunter2")
    db_conn.commit()
    new_token, new_expires_at = refresh_token(db_conn, old_token)
    db_conn.commit()
    assert new_token != old_token
    with pytest.raises(InvalidToken):
        whoami(db_conn, old_token)
    user = whoami(db_conn, new_token)
    assert user.username == "alice"


def test_login_timing_unknown_user_vs_wrong_password(db_conn: psycopg.Connection) -> None:
    """Login latency for an unknown username must be comparable to login
    latency for a known user with a wrong password.

    Without the dummy-hash branch in api/auth.login, the unknown-user path
    skips argon2 verify and returns in microseconds while wrong-password
    spends ~50-200 ms hashing. The ratio reveals which usernames exist.
    """
    import time

    from localmail.api.auth import reset_login_rate_limiter

    _seed_user(db_conn, "alice", "hunter2")
    db_conn.commit()

    # 7 samples + a discarded warmup keeps the median robust to a single GC
    # pause or DB latency spike on a loaded CI host. With 3 samples, one
    # outlier swings the median directly and the ratio test flaked.
    samples_unknown: list[float] = []
    samples_wrong_pw: list[float] = []
    n_samples = 7
    for i in range(n_samples + 1):
        reset_login_rate_limiter(db_conn)
        db_conn.commit()
        t0 = time.perf_counter()
        try:
            login(db_conn, f"ghost_{i}", "any-password")
        except AuthenticationFailed:
            pass
        elapsed_unknown = time.perf_counter() - t0

        reset_login_rate_limiter(db_conn)
        db_conn.commit()
        t0 = time.perf_counter()
        try:
            login(db_conn, "alice", "wrong-password")
        except AuthenticationFailed:
            pass
        elapsed_wrong_pw = time.perf_counter() - t0

        if i == 0:
            continue  # discard warmup (first-call argon2 / JIT / page cache)
        samples_unknown.append(elapsed_unknown)
        samples_wrong_pw.append(elapsed_wrong_pw)
    reset_login_rate_limiter(db_conn)
    db_conn.commit()

    med_unknown = sorted(samples_unknown)[len(samples_unknown) // 2]
    med_wrong_pw = sorted(samples_wrong_pw)[len(samples_wrong_pw) // 2]
    ratio = max(med_unknown, med_wrong_pw) / max(min(med_unknown, med_wrong_pw), 1e-9)
    assert ratio < 5.0, (
        f"login timing diverges: unknown-user median={med_unknown*1000:.1f}ms "
        f"vs wrong-password median={med_wrong_pw*1000:.1f}ms (ratio={ratio:.2f})"
    )
