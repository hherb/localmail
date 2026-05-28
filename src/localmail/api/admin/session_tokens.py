"""HMAC-signed JSON payloads for admin cookie sessions.

Wire format:
    base64url(json(payload)) + "." + base64url(hmac_sha256(key, body))

The payload is a fixed-schema dict; unknown versions are rejected so we
can rotate format atomically.
"""
from __future__ import annotations

import base64
import hmac
import json
import time
from dataclasses import dataclass
from hashlib import sha256
from typing import Any


_CURRENT_VERSION = 1


class SessionTokenError(Exception):
    """Any verify-side failure: tamper, expiry, malformed, wrong version."""


@dataclass(frozen=True)
class SessionPayload:
    user_id: int
    issued_at: int
    exp: int


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def _encode_unsigned(d: dict[str, Any]) -> str:
    raw = json.dumps(d, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return _b64url_encode(raw)


def _sign(body_b64: str, key: bytes) -> str:
    mac = hmac.new(key, body_b64.encode("ascii"), sha256).digest()
    return _b64url_encode(mac)


def encode_session_token(payload: SessionPayload, *, key: bytes) -> str:
    if not isinstance(key, (bytes, bytearray)) or len(key) < 16:
        raise ValueError("key must be at least 16 bytes")
    body = _encode_unsigned({
        "v": _CURRENT_VERSION,
        "user_id": payload.user_id,
        "issued_at": payload.issued_at,
        "exp": payload.exp,
    })
    sig = _sign(body, key)
    return f"{body}.{sig}"


def decode_session_token(
    token: str,
    *,
    key: bytes,
    now: int | None = None,
) -> SessionPayload:
    if not isinstance(token, str) or token.count(".") != 1:
        raise SessionTokenError("malformed token")
    body, sig = token.split(".")
    if not body or not sig:
        raise SessionTokenError("malformed token")
    expected = _sign(body, key)
    if not hmac.compare_digest(expected, sig):
        raise SessionTokenError("signature mismatch")
    try:
        d = json.loads(_b64url_decode(body))
    except Exception as exc:
        raise SessionTokenError("malformed payload") from exc
    if not isinstance(d, dict):
        raise SessionTokenError("malformed payload")
    if d.get("v") != _CURRENT_VERSION:
        raise SessionTokenError(f"unsupported token version: {d.get('v')!r}")
    try:
        payload = SessionPayload(
            user_id=int(d["user_id"]),
            issued_at=int(d["issued_at"]),
            exp=int(d["exp"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise SessionTokenError("malformed payload") from exc
    current = now if now is not None else int(time.time())
    if payload.exp <= current:
        raise SessionTokenError("expired")
    return payload
