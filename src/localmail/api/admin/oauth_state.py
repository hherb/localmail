"""Stateless HMAC-signed OAuth state tokens for the admin web flow.

Format: base64url(json(payload)) + "." + base64url(hmac_sha256(key, payload_b64)).
"""

from __future__ import annotations

import base64
import hmac
import json
import time
from dataclasses import asdict, dataclass
from hashlib import sha256


@dataclass(frozen=True)
class StatePayload:
    user_id: int
    account_id: int
    nonce: str
    exp: int


class StateExpired(ValueError):
    """Token signed correctly but its exp is in the past."""


class StateInvalid(ValueError):
    """Token shape, signature, or payload could not be verified."""


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b'=').decode('ascii')


def _b64url_decode(s: str) -> bytes:
    pad = '=' * (-len(s) % 4)
    return base64.urlsafe_b64decode((s + pad).encode('ascii'))


def encode_state(payload: StatePayload, *, key: bytes) -> str:
    body_bytes = json.dumps(asdict(payload), sort_keys=True,
                            separators=(',', ':')).encode('utf-8')
    body_b64 = _b64url_encode(body_bytes)
    sig = hmac.new(key, body_b64.encode('ascii'), sha256).digest()
    return body_b64 + '.' + _b64url_encode(sig)


def decode_state(token: str, *, key: bytes) -> StatePayload:
    if '.' not in token:
        raise StateInvalid("missing separator")
    body_b64, sig_b64 = token.split('.', 1)
    expected_sig = hmac.new(key, body_b64.encode('ascii'), sha256).digest()
    try:
        actual_sig = _b64url_decode(sig_b64)
    except Exception as e:
        raise StateInvalid("malformed signature") from e
    if not hmac.compare_digest(expected_sig, actual_sig):
        raise StateInvalid("signature mismatch")
    try:
        body = json.loads(_b64url_decode(body_b64))
        payload = StatePayload(**body)
    except Exception as e:
        raise StateInvalid("malformed payload") from e
    if payload.exp < int(time.time()):
        raise StateExpired(f"state expired at {payload.exp}")
    return payload
