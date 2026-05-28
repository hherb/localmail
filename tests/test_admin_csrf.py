"""CSRF tokens bound to (session_user_id, action_key)."""
from __future__ import annotations

import pytest

from localmail.api.admin.csrf import CSRFError, make_csrf_token, verify_csrf_token

KEY = b"a" * 32


def test_round_trip() -> None:
    tok = make_csrf_token(user_id=7, action="/admin/accounts/new", key=KEY)
    verify_csrf_token(tok, user_id=7, action="/admin/accounts/new", key=KEY)  # no raise


def test_wrong_user_rejected() -> None:
    tok = make_csrf_token(user_id=7, action="/admin/accounts/new", key=KEY)
    with pytest.raises(CSRFError):
        verify_csrf_token(tok, user_id=8, action="/admin/accounts/new", key=KEY)


def test_wrong_action_rejected() -> None:
    tok = make_csrf_token(user_id=7, action="/admin/accounts/new", key=KEY)
    with pytest.raises(CSRFError):
        verify_csrf_token(tok, user_id=7, action="/admin/daemon/start", key=KEY)


def test_wrong_key_rejected() -> None:
    tok = make_csrf_token(user_id=7, action="/admin/x", key=KEY)
    with pytest.raises(CSRFError):
        verify_csrf_token(tok, user_id=7, action="/admin/x", key=b"b" * 32)


def test_malformed_rejected() -> None:
    for bad in ["", "no-dot", "a.b.c"]:
        with pytest.raises(CSRFError):
            verify_csrf_token(bad, user_id=7, action="/admin/x", key=KEY)


def test_short_key_rejected() -> None:
    """Matches encode_session_token's >= 16 byte requirement so the two
    primitives can't be misused with the same too-short key."""
    with pytest.raises(ValueError, match="at least 16 bytes"):
        make_csrf_token(user_id=7, action="/admin/x", key=b"a" * 8)
