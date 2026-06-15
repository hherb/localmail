"""Stateless HMAC-signed blob carrying authorization params through the
interactive consent round-trip.

Format mirrors `localmail.api.admin.oauth_state`:
base64url(json(payload)) + "." + base64url(hmac_sha256(key, payload_b64)).
No DB row, no cleanup; the `exp` field bounds replay.
"""
from __future__ import annotations

import base64
import hmac
import json
import time
from dataclasses import asdict, dataclass
from hashlib import sha256


@dataclass(frozen=True)
class ConsentPayload:
    client_id: str
    redirect_uri: str
    redirect_uri_provided_explicitly: bool
    code_challenge: str
    scopes: list[str]
    state: str | None
    exp: int


class ConsentStateExpired(ValueError):
    """Signed correctly but its exp is in the past."""


class ConsentStateInvalid(ValueError):
    """Shape, signature, or payload could not be verified."""


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b'=').decode('ascii')


def _b64url_decode(s: str) -> bytes:
    pad = '=' * (-len(s) % 4)
    return base64.urlsafe_b64decode((s + pad).encode('ascii'))


def encode_consent_state(payload: ConsentPayload, *, key: bytes) -> str:
    body_bytes = json.dumps(
        asdict(payload), sort_keys=True, separators=(',', ':')
    ).encode('utf-8')
    body_b64 = _b64url_encode(body_bytes)
    sig = hmac.new(key, body_b64.encode('ascii'), sha256).digest()
    return body_b64 + '.' + _b64url_encode(sig)


def decode_consent_state(token: str, *, key: bytes) -> ConsentPayload:
    if '.' not in token:
        raise ConsentStateInvalid('missing separator')
    body_b64, sig_b64 = token.split('.', 1)
    expected_sig = hmac.new(key, body_b64.encode('ascii'), sha256).digest()
    try:
        actual_sig = _b64url_decode(sig_b64)
    except Exception as e:
        raise ConsentStateInvalid('malformed signature') from e
    if not hmac.compare_digest(expected_sig, actual_sig):
        raise ConsentStateInvalid('signature mismatch')
    try:
        body = json.loads(_b64url_decode(body_b64))
        payload = ConsentPayload(**body)
    except Exception as e:
        raise ConsentStateInvalid('malformed payload') from e
    if payload.exp < int(time.time()):
        raise ConsentStateExpired(f'consent state expired at {payload.exp}')
    return payload
