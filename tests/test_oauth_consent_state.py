# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

import time

import pytest

from localmail.mcp.oauth.consent_state import (
    ConsentPayload,
    ConsentStateExpired,
    ConsentStateInvalid,
    decode_consent_state,
    encode_consent_state,
)

KEY = b"unit-test-signing-key"


def _payload(exp_offset: int = 300) -> ConsentPayload:
    return ConsentPayload(
        client_id="cid-123",
        redirect_uri="https://client.example/cb",
        redirect_uri_provided_explicitly=True,
        code_challenge="abc123",
        scopes=[],
        state="xyz",
        exp=int(time.time()) + exp_offset,
    )


def test_roundtrip():
    tok = encode_consent_state(_payload(), key=KEY)
    got = decode_consent_state(tok, key=KEY)
    assert got.client_id == "cid-123"
    assert got.redirect_uri == "https://client.example/cb"
    assert got.code_challenge == "abc123"


def test_tampered_signature_rejected():
    tok = encode_consent_state(_payload(), key=KEY)
    with pytest.raises(ConsentStateInvalid):
        decode_consent_state(tok, key=b"different-key")


def test_tampered_body_rejected():
    tok = encode_consent_state(_payload(), key=KEY)
    body, sig = tok.split(".", 1)
    with pytest.raises(ConsentStateInvalid):
        decode_consent_state("AAAA" + body + "." + sig, key=KEY)


def test_expired_rejected():
    tok = encode_consent_state(_payload(exp_offset=-1), key=KEY)
    with pytest.raises(ConsentStateExpired):
        decode_consent_state(tok, key=KEY)


def test_missing_separator_rejected():
    with pytest.raises(ConsentStateInvalid):
        decode_consent_state("no-dot-here", key=KEY)


def test_consent_payload_round_trips_resource():
    payload = ConsentPayload(
        client_id="cid",
        redirect_uri="https://c/cb",
        redirect_uri_provided_explicitly=True,
        code_challenge="chal",
        scopes=["s"],
        state=None,
        exp=int(time.time()) + 300,
        resource="https://h/mcp",
    )
    blob = encode_consent_state(payload, key=KEY)
    back = decode_consent_state(blob, key=KEY)
    assert back.resource == "https://h/mcp"
