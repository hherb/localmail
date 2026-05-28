"""CSRF tokens for admin forms.

Bound to (user_id, action) so a token minted for one form is useless for
another. No expiry — the cookie session itself expires; once it does, the
session middleware redirects to /admin/login before CSRF is checked.
"""
from __future__ import annotations

import hmac
from hashlib import sha256

from localmail.api.admin.session_tokens import _b64url_encode


class CSRFError(Exception):
    """Tamper, wrong user, wrong action, or malformed token."""


def _bind_string(user_id: int, action: str) -> bytes:
    return f"v=1|u={user_id}|a={action}".encode("utf-8")


def make_csrf_token(*, user_id: int, action: str, key: bytes) -> str:
    if not isinstance(key, (bytes, bytearray)) or len(key) < 16:
        raise ValueError("key must be at least 16 bytes")
    bound = _bind_string(user_id, action)
    mac = hmac.new(key, bound, sha256).digest()
    return _b64url_encode(mac)


def verify_csrf_token(
    token: str,
    *,
    user_id: int,
    action: str,
    key: bytes,
) -> None:
    if not isinstance(token, str) or not token or "." in token:
        raise CSRFError("malformed")
    expected = make_csrf_token(user_id=user_id, action=action, key=key)
    if not hmac.compare_digest(expected, token):
        raise CSRFError("mismatch")
