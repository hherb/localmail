"""Authentication primitives: password hashing, token issuance, verification.

Higher-level service functions (login, refresh, whoami) are added in
subsequent tasks. This module is transport-free; HTTP concerns live in
localmail.serve.
"""
from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, InvalidHashError, VerificationError

_HASHER = PasswordHasher()


def hash_password(password: str) -> str:
    """Hash a password with argon2id. Raises ValueError on empty input."""
    if not password:
        raise ValueError("password must be non-empty")
    return _HASHER.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """Constant-time verify; returns False on any mismatch or malformed hash."""
    try:
        return _HASHER.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHashError, VerificationError):
        return False
