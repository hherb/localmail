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
import pytest

from localmail.api import auth as auth_mod
from localmail.api.auth import (
    DUMMY_PASSWORD_HASH,
    check_login_rate_limits,
    record_login_attempt,
    reset_login_rate_limiter,
    verify_password,
)
from localmail.api.errors import RateLimited
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
    underscored names still resolve via ``__getattr__`` and are the *same
    object* as the public name so existing callers don't double-evaluate
    or drift. Each lookup also emits ``DeprecationWarning`` so a caller
    still importing the old name gets a runtime signal before the alias
    is removed.
    """
    for old, new in (
        ("_DUMMY_PASSWORD_HASH", "DUMMY_PASSWORD_HASH"),
        ("_check_login_rate_limits", "check_login_rate_limits"),
        ("_record_login_attempt", "record_login_attempt"),
    ):
        with pytest.warns(DeprecationWarning, match=old):
            resolved = getattr(auth_mod, old)
        assert resolved is getattr(auth_mod, new)


def test_unknown_attribute_still_raises_attribute_error() -> None:
    """``__getattr__`` only handles the three deprecated aliases — other
    misses must surface as ``AttributeError`` so typos aren't silently
    swallowed.
    """
    with pytest.raises(AttributeError, match="does_not_exist"):
        auth_mod.does_not_exist  # type: ignore[attr-defined]


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
    with pytest.raises(RateLimited) as excinfo:
        check_login_rate_limits(db_conn, "alice", "10.0.0.1", cfg=cfg)
    assert excinfo.value.cap == "user"
