"""Round-trip + tamper + expiry tests for the OAuth state token."""

from __future__ import annotations

import time

import pytest

from localmail.api.admin.oauth_state import (
    encode_state, decode_state,
    StatePayload, StateExpired, StateInvalid,
    _b64url_decode, _b64url_encode,
)


KEY = b"k" * 32


def test_encode_decode_roundtrip() -> None:
    payload = StatePayload(user_id=42, account_id=7,
                           nonce='abc', exp=int(time.time()) + 60)
    token = encode_state(payload, key=KEY)
    decoded = decode_state(token, key=KEY)
    assert decoded == payload


def test_decode_rejects_tampered_payload() -> None:
    payload = StatePayload(user_id=42, account_id=7,
                           nonce='abc', exp=int(time.time()) + 60)
    token = encode_state(payload, key=KEY)
    head, sig = token.split('.', 1)
    # Flip the last char of the payload half.
    bad_token = head[:-1] + ('A' if head[-1] != 'A' else 'B') + '.' + sig
    with pytest.raises(StateInvalid):
        decode_state(bad_token, key=KEY)


def test_decode_rejects_tampered_signature() -> None:
    payload = StatePayload(user_id=1, account_id=1,
                           nonce='a', exp=int(time.time()) + 60)
    token = encode_state(payload, key=KEY)
    head, sig = token.split('.', 1)
    # Tamper at the byte level. Flipping the *last* base64url char is unreliable:
    # a 32-byte HMAC digest's final char carries 2 dropped padding bits, so for
    # ~1/16 of digests 'A'->'B' re-encodes to the identical signature bytes and
    # decode_state (which compares decoded bytes) correctly does NOT raise.
    raw = _b64url_decode(sig)
    bad_token = head + '.' + _b64url_encode(bytes([raw[0] ^ 0x01]) + raw[1:])
    with pytest.raises(StateInvalid):
        decode_state(bad_token, key=KEY)


def test_decode_rejects_wrong_key() -> None:
    payload = StatePayload(user_id=1, account_id=1,
                           nonce='a', exp=int(time.time()) + 60)
    token = encode_state(payload, key=KEY)
    with pytest.raises(StateInvalid):
        decode_state(token, key=b"x" * 32)


def test_decode_raises_state_expired_when_past_exp() -> None:
    payload = StatePayload(user_id=1, account_id=1,
                           nonce='a', exp=int(time.time()) - 1)
    token = encode_state(payload, key=KEY)
    with pytest.raises(StateExpired):
        decode_state(token, key=KEY)


def test_decode_rejects_malformed_token() -> None:
    with pytest.raises(StateInvalid):
        decode_state("no-dot-here", key=KEY)
