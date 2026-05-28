"""HMAC sign/verify for admin cookie-session tokens."""
from __future__ import annotations

import time

import pytest

from localmail.api.admin.session_tokens import (
    SessionPayload,
    SessionTokenError,
    decode_session_token,
    encode_session_token,
)

KEY = b"a" * 32


def test_round_trip() -> None:
    issued = int(time.time())
    payload = SessionPayload(user_id=42, issued_at=issued, exp=issued + 3600)
    token = encode_session_token(payload, key=KEY)
    decoded = decode_session_token(token, key=KEY)
    assert decoded == payload


def test_tamper_in_payload_rejected() -> None:
    issued = int(time.time())
    payload = SessionPayload(user_id=42, issued_at=issued, exp=issued + 3600)
    token = encode_session_token(payload, key=KEY)
    # Flip one character in the payload portion.
    body, sig = token.split(".")
    tampered = body[:-1] + ("A" if body[-1] != "A" else "B") + "." + sig
    with pytest.raises(SessionTokenError):
        decode_session_token(tampered, key=KEY)


def test_tamper_in_signature_rejected() -> None:
    issued = int(time.time())
    payload = SessionPayload(user_id=42, issued_at=issued, exp=issued + 3600)
    token = encode_session_token(payload, key=KEY)
    body, sig = token.split(".")
    tampered = body + "." + sig[:-1] + ("A" if sig[-1] != "A" else "B")
    with pytest.raises(SessionTokenError):
        decode_session_token(tampered, key=KEY)


def test_wrong_key_rejected() -> None:
    issued = int(time.time())
    payload = SessionPayload(user_id=42, issued_at=issued, exp=issued + 3600)
    token = encode_session_token(payload, key=KEY)
    with pytest.raises(SessionTokenError):
        decode_session_token(token, key=b"b" * 32)


def test_expired_token_rejected() -> None:
    now = int(time.time())
    payload = SessionPayload(user_id=42, issued_at=now - 10, exp=now - 1)
    token = encode_session_token(payload, key=KEY)
    with pytest.raises(SessionTokenError, match="expired"):
        decode_session_token(token, key=KEY, now=now)


def test_malformed_input_rejected() -> None:
    for bad in ["", "no-dot", "a.b.c", "...", "!!.!!"]:
        with pytest.raises(SessionTokenError):
            decode_session_token(bad, key=KEY)


def test_unknown_version_rejected() -> None:
    """A future-version token (v=2) must be rejected, not silently parsed."""
    from localmail.api.admin.session_tokens import _encode_unsigned, _sign

    body = _encode_unsigned({"v": 2, "user_id": 1, "issued_at": 0, "exp": 99999999999})
    sig = _sign(body, KEY)
    with pytest.raises(SessionTokenError, match="version"):
        decode_session_token(f"{body}.{sig}", key=KEY)
