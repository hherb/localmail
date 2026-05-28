"""Public surface of ``localmail.api.auth`` (#115).

The dummy hash, rate-limit check, and audit insert are legitimately needed
by every login path (the public ``/v1/auth/login`` route AND the admin login
router in ``localmail.serve.admin.auth_router``). Pre-#115 they were exposed
only via leading-underscore names, forcing the admin code to import private
symbols. These tests pin the post-#115 contract: the public names exist, the
underscored aliases still resolve to the same objects (one-release
deprecation window per the issue body), and the public surface is fully
callable end-to-end against the real DB.
"""
from __future__ import annotations

import psycopg

from localmail.api import auth as auth_mod
from localmail.api.auth import (
    DUMMY_PASSWORD_HASH,
    check_login_rate_limits,
    record_login_attempt,
    reset_login_rate_limiter,
    verify_password,
)
from localmail.config import AuthConfig


def test_public_names_exposed_on_module() -> None:
    """``DUMMY_PASSWORD_HASH``, ``check_login_rate_limits`` and
    ``record_login_attempt`` are importable without a leading underscore.
    """
    assert isinstance(auth_mod.DUMMY_PASSWORD_HASH, str)
    assert auth_mod.DUMMY_PASSWORD_HASH.startswith("$argon2")
    assert callable(auth_mod.check_login_rate_limits)
    assert callable(auth_mod.record_login_attempt)


def test_underscored_aliases_resolve_to_public_objects() -> None:
    """Issue #115 commits to a one-release deprecation window — the
    underscored names still import and are the *same object* as the public
    name so existing callers don't double-evaluate or drift.
    """
    assert auth_mod._DUMMY_PASSWORD_HASH is auth_mod.DUMMY_PASSWORD_HASH
    assert auth_mod._check_login_rate_limits is auth_mod.check_login_rate_limits
    assert auth_mod._record_login_attempt is auth_mod.record_login_attempt


def test_public_dummy_hash_verifies_as_mismatch() -> None:
    """The dummy hash is purely a timing-parity sink — any candidate password
    must verify False against it. This pins the *behavior* of the public
    name, not just its existence.
    """
    assert verify_password("any-password-at-all", DUMMY_PASSWORD_HASH) is False
    assert verify_password("dummy-password-for-timing-parity", DUMMY_PASSWORD_HASH) is True


def test_public_record_then_check_round_trip(db_conn: psycopg.Connection) -> None:
    """Calling the public helpers in sequence behaves the same as the
    pre-#115 underscored helpers: a recorded failure increments the counter
    seen by the next rate-limit check.
    """
    reset_login_rate_limiter(db_conn)
    db_conn.commit()
    cfg = AuthConfig(login_per_user_max=2, login_per_user_window_s=60)
    record_login_attempt(db_conn, "alice", "10.0.0.1", "failure")
    record_login_attempt(db_conn, "alice", "10.0.0.1", "failure")
    db_conn.commit()
    import pytest
    from localmail.api.errors import RateLimited
    with pytest.raises(RateLimited) as excinfo:
        check_login_rate_limits(db_conn, "alice", "10.0.0.1", cfg=cfg)
    assert excinfo.value.cap == "user"
