# MCP OAuth 2.1 Authorization Server Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn localmail's MCP server into a real OAuth 2.1 authorization server so spec-strict MCP clients self-onboard via a browser login + consent (open DCR + PKCE), with no hand-pasted bearer token.

**Architecture:** Implement the MCP SDK's `OAuthAuthorizationServerProvider` (9 methods) backed by three new hashed-token SQL stores + a bridge that reuses `api_tokens` for access tokens (so the existing verifier + per-user ACL are untouched). The one piece the SDK can't supply — interactive resource-owner login + consent — is a small HTML router; the authorization parameters ride through it as a stateless HMAC-signed blob, exactly like the Gmail admin OAuth flow. Opt-in, default off.

**Tech Stack:** Python ≥3.12, `psycopg` v3 + raw SQL migrations, `pydantic` v2 config, `mcp` 1.27.2 (`create_auth_routes` / `OAuthAuthorizationServerProvider`), FastAPI/Starlette for the consent router, stdlib `hmac`/`hashlib` for the signed blob.

**Spec:** [docs/superpowers/specs/2026-06-15-mcp-oauth-authorization-server-design.md](../specs/2026-06-15-mcp-oauth-authorization-server-design.md)

**Key SDK facts the implementation relies on (verified against `mcp` 1.27.2):**
- `provider.authorize(client, params) -> str` returns a redirect URL string. `params` (`AuthorizationParams`) has `state`, `scopes`, `code_challenge`, `redirect_uri`, `redirect_uri_provided_explicitly`, `resource`. **There is no `code_challenge_method`** — the SDK is S256-only.
- The SDK's `TokenHandler` performs **PKCE S256 verification and `redirect_uri` matching itself** using the `AuthorizationCode` object returned by `provider.load_authorization_code`. Our provider stores `code_challenge` verbatim and never touches `code_verifier`.
- `AuthorizationCode` fields: `code, scopes, expires_at, client_id, code_challenge, redirect_uri, redirect_uri_provided_explicitly, resource, subject`. We put the localmail user id (string) in `subject`.
- `RefreshToken` fields: `token, client_id, scopes, expires_at, subject`. `AccessToken` fields: `token, client_id, scopes, expires_at, resource, subject, claims`.
- `OAuthToken` (returned by exchanges): `access_token, token_type, expires_in, scope, refresh_token`.
- `OAuthClientInformationFull` (passed to `register_client` / returned by `get_client`): `redirect_uris, token_endpoint_auth_method, grant_types, response_types, scope, client_name, client_id, client_secret, client_id_issued_at, client_secret_expires_at, …`.
- Passing `auth_server_provider=` to `FastMCP` makes the SDK use `provider.load_access_token` for the `/mcp` resource check (replacing `token_verifier`) and mounts `/authorize`, `/token`, `/register`, `/revoke`, `/.well-known/oauth-authorization-server` via `create_auth_routes`, gated by `ClientRegistrationOptions(enabled=True)` / `RevocationOptions(enabled=True)`.

**Reused primitives:**
- `localmail.api.auth.generate_token()` (32-byte url-safe) and `hash_token(str) -> bytes` (sha256) — used for codes, refresh tokens, and access tokens.
- `localmail.api.auth.verify_token(conn, token)` — reused unchanged by the access-token bridge's `load`.
- `localmail.api.auth.check_login_rate_limits` / `record_login_attempt` / `verify_password` — reused by the consent login so it isn't a brute-force bypass.
- The HMAC pattern in `localmail.api.admin.oauth_state` (`_b64url_encode/_decode`, sign/verify) — mirrored by `mcp/oauth/consent_state.py`.

**Run all tests with the mcp extra:** `unset VIRTUAL_ENV && uv run --extra mcp pytest …`.

---

## File Structure

**Create:**
- `migrations/0028_oauth_server.sql` — three tables + nullable `api_tokens.oauth_client_id`.
- `src/localmail/mcp/oauth/__init__.py` — package marker + public exports.
- `src/localmail/mcp/oauth/consent_state.py` — PURE signed-blob encode/decode.
- `src/localmail/mcp/oauth/consent_forms.py` — PURE consent POST validation.
- `src/localmail/mcp/oauth/clients.py` — DCR store.
- `src/localmail/mcp/oauth/codes.py` — authorization-code store.
- `src/localmail/mcp/oauth/refresh.py` — refresh-token store.
- `src/localmail/mcp/oauth/access.py` — access-token bridge over `api_tokens`.
- `src/localmail/mcp/oauth/provider.py` — `LocalmailASProvider`.
- `src/localmail/serve/oauth/__init__.py`
- `src/localmail/serve/oauth/consent_router.py` — `/oauth/consent` HTML router.
- `src/localmail/serve/oauth/templates/consent.html`
- Tests: `tests/test_oauth_consent_state.py`, `tests/test_oauth_consent_forms.py`, `tests/test_oauth_clients_store.py`, `tests/test_oauth_codes_store.py`, `tests/test_oauth_refresh_store.py`, `tests/test_oauth_access_bridge.py`, `tests/test_oauth_provider.py`, `tests/test_serve_oauth_consent.py`, `tests/test_serve_oauth_gating.py`.

**Modify:**
- `src/localmail/config.py` — `McpConfig` new fields.
- `src/localmail/serve/app.py` — build provider + mount consent router when enabled; fallback when off; fail loud without `state_signing_key`.
- `src/localmail/mcp/__init__.py` — export the provider builder behind the guarded import.
- `tests/test_mcp_integration.py` — full cold-connect dance.
- `docs/mcp-usage.md`, `README.md`, `CLAUDE.md` — document the AS.

---

## Task 1: Migration `0028_oauth_server.sql`

**Files:**
- Create: `migrations/0028_oauth_server.sql`
- Test: `tests/test_oauth_migration.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_oauth_migration.py
"""The 0028 migration creates the OAuth AS tables + api_tokens.oauth_client_id."""
from __future__ import annotations


def _columns(conn, table: str) -> set[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = %s",
            (table,),
        )
        return {r[0] for r in cur.fetchall()}


def test_oauth_tables_exist(db_conn):
    for table in ("oauth_clients", "oauth_authorization_codes", "oauth_refresh_tokens"):
        cols = _columns(db_conn, table)
        assert cols, f"{table} missing"


def test_oauth_clients_shape(db_conn):
    cols = _columns(db_conn, "oauth_clients")
    assert {"client_id", "client_secret_sha256", "redirect_uris", "client_name",
            "created_at", "last_used_at"} <= cols


def test_authorization_codes_shape(db_conn):
    cols = _columns(db_conn, "oauth_authorization_codes")
    assert {"code_sha256", "client_id", "user_id", "redirect_uri", "code_challenge",
            "redirect_uri_provided_explicitly", "scopes", "expires_at"} <= cols


def test_refresh_tokens_shape(db_conn):
    cols = _columns(db_conn, "oauth_refresh_tokens")
    assert {"token_sha256", "client_id", "user_id", "scopes", "expires_at"} <= cols


def test_api_tokens_gains_oauth_client_id(db_conn):
    assert "oauth_client_id" in _columns(db_conn, "api_tokens")


def test_registration_attempts_table(db_conn):
    cols = _columns(db_conn, "oauth_registration_attempts")
    assert {"ip", "ts"} <= cols
```

- [ ] **Step 2: Run test to verify it fails**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_oauth_migration.py -q`
Expected: FAIL — tables/column don't exist yet (the `db_conn` fixture applies all migrations; this one is missing).

- [ ] **Step 3: Write the migration**

```sql
-- migrations/0028_oauth_server.sql
-- OAuth 2.1 authorization-server storage (MCP "Approach B"). Tokens/codes are
-- stored SHA-256-hashed; the raw value is returned to the client exactly once.
-- Access tokens reuse api_tokens; the new oauth_client_id column attributes an
-- OAuth-minted access token to its client and cascade-revokes with it. NULL on
-- every login-issued token, so existing rows + /v1/auth/login are unaffected.

CREATE TABLE oauth_clients (
    client_id                  TEXT PRIMARY KEY,
    client_secret_sha256       BYTEA,
    redirect_uris              TEXT[] NOT NULL,
    client_name                TEXT,
    grant_types                TEXT[],
    response_types             TEXT[],
    token_endpoint_auth_method TEXT,
    scope                      TEXT,
    created_at                 TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used_at               TIMESTAMPTZ
);

CREATE TABLE oauth_authorization_codes (
    code_sha256                      BYTEA PRIMARY KEY,
    client_id                        TEXT NOT NULL REFERENCES oauth_clients ON DELETE CASCADE,
    user_id                          BIGINT NOT NULL REFERENCES api_users ON DELETE CASCADE,
    redirect_uri                     TEXT NOT NULL,
    redirect_uri_provided_explicitly BOOLEAN NOT NULL,
    code_challenge                   TEXT NOT NULL,
    scopes                           TEXT[] NOT NULL DEFAULT '{}',
    expires_at                       TIMESTAMPTZ NOT NULL,
    created_at                       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE oauth_refresh_tokens (
    token_sha256 BYTEA PRIMARY KEY,
    client_id    TEXT NOT NULL REFERENCES oauth_clients ON DELETE CASCADE,
    user_id      BIGINT NOT NULL REFERENCES api_users ON DELETE CASCADE,
    scopes       TEXT[] NOT NULL DEFAULT '{}',
    expires_at   TIMESTAMPTZ NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE api_tokens
    ADD COLUMN oauth_client_id TEXT REFERENCES oauth_clients ON DELETE CASCADE;

-- Per-IP rate-limit audit for open Dynamic Client Registration. Append-only,
-- read by a sliding-window COUNT, swept on retention — same shape and
-- multi-worker-safety rationale as api_login_attempts (an in-memory limiter
-- loses the promise under `uvicorn --workers N`).
CREATE TABLE oauth_registration_attempts (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ip TEXT,
    ts TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX oauth_registration_attempts_ts_idx ON oauth_registration_attempts (ts);
```

> **Implementer note:** confirm `BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY`
> matches the `api_login_attempts` primary-key style in
> `migrations/0019_api_login_attempts.sql`; if that file uses a different idiom,
> mirror it for consistency.

- [ ] **Step 4: Run test to verify it passes**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_oauth_migration.py -q`
Expected: PASS (the `db_conn` fixture re-applies migrations including 0028).

- [ ] **Step 5: Commit**

```bash
git add migrations/0028_oauth_server.sql tests/test_oauth_migration.py
git commit -m "feat(oauth): migration 0028 — AS tables + api_tokens.oauth_client_id"
```

---

## Task 2: `McpConfig` fields

**Files:**
- Modify: `src/localmail/config.py` (the `McpConfig` class)
- Test: `tests/test_config_mcp_oauth.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_config_mcp_oauth.py
from localmail.config import McpConfig


def test_defaults():
    c = McpConfig()
    assert c.authorization_server_enabled is False
    assert c.oauth_access_token_ttl_s == 3600
    assert c.oauth_refresh_token_ttl_s == 2592000
    assert c.oauth_authorization_code_ttl_s == 60
    assert c.oauth_consent_state_ttl_s == 300
    assert c.oauth_registration_window_s == 3600
    assert c.oauth_registration_max == 20
    assert c.oauth_client_unused_retention_s == 86400


def test_override_roundtrip():
    c = McpConfig(authorization_server_enabled=True, oauth_refresh_token_ttl_s=7776000)
    assert c.authorization_server_enabled is True
    assert c.oauth_refresh_token_ttl_s == 7776000
```

- [ ] **Step 2: Run test to verify it fails**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_config_mcp_oauth.py -q`
Expected: FAIL — `AttributeError`/`ValidationError` for unknown fields.

- [ ] **Step 3: Add the fields**

In `src/localmail/config.py`, inside `class McpConfig`, after the existing
`authorization_servers` field, add:

```python
    # OAuth 2.1 authorization server (MCP "Approach B"). Opt-in; when off the
    # MCP server stays opaque-bearer + discovery only (today's behaviour). All
    # tunables defaulted so the provider carries no magic numbers.
    authorization_server_enabled: bool = False
    oauth_access_token_ttl_s: int = 3600
    oauth_refresh_token_ttl_s: int = 2592000
    oauth_authorization_code_ttl_s: int = 60
    oauth_consent_state_ttl_s: int = 300
    oauth_registration_window_s: int = 3600
    oauth_registration_max: int = 20
    oauth_client_unused_retention_s: int = 86400
```

Extend the `McpConfig` docstring with one sentence: that
`authorization_server_enabled` turns localmail into an OAuth AS for MCP and that
it requires `[serve].state_signing_key`.

- [ ] **Step 4: Run test to verify it passes**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_config_mcp_oauth.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/localmail/config.py tests/test_config_mcp_oauth.py
git commit -m "feat(oauth): McpConfig fields for the authorization server"
```

---

## Task 3: `consent_state.py` (pure signed blob)

**Files:**
- Create: `src/localmail/mcp/oauth/__init__.py` (empty marker for now)
- Create: `src/localmail/mcp/oauth/consent_state.py`
- Test: `tests/test_oauth_consent_state.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_oauth_consent_state.py
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_oauth_consent_state.py -q`
Expected: FAIL — `ModuleNotFoundError: localmail.mcp.oauth.consent_state`.

- [ ] **Step 3: Implement**

Create `src/localmail/mcp/oauth/__init__.py` empty. Then:

```python
# src/localmail/mcp/oauth/consent_state.py
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
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode((s + pad).encode("ascii"))


def encode_consent_state(payload: ConsentPayload, *, key: bytes) -> str:
    body_bytes = json.dumps(
        asdict(payload), sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    body_b64 = _b64url_encode(body_bytes)
    sig = hmac.new(key, body_b64.encode("ascii"), sha256).digest()
    return body_b64 + "." + _b64url_encode(sig)


def decode_consent_state(token: str, *, key: bytes) -> ConsentPayload:
    if "." not in token:
        raise ConsentStateInvalid("missing separator")
    body_b64, sig_b64 = token.split(".", 1)
    expected_sig = hmac.new(key, body_b64.encode("ascii"), sha256).digest()
    try:
        actual_sig = _b64url_decode(sig_b64)
    except Exception as e:
        raise ConsentStateInvalid("malformed signature") from e
    if not hmac.compare_digest(expected_sig, actual_sig):
        raise ConsentStateInvalid("signature mismatch")
    try:
        body = json.loads(_b64url_decode(body_b64))
        payload = ConsentPayload(**body)
    except Exception as e:
        raise ConsentStateInvalid("malformed payload") from e
    if payload.exp < int(time.time()):
        raise ConsentStateExpired(f"consent state expired at {payload.exp}")
    return payload
```

- [ ] **Step 4: Run test to verify it passes**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_oauth_consent_state.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/localmail/mcp/oauth/__init__.py src/localmail/mcp/oauth/consent_state.py tests/test_oauth_consent_state.py
git commit -m "feat(oauth): pure signed consent-state blob"
```

---

## Task 4: `consent_forms.py` (pure validation)

**Files:**
- Create: `src/localmail/mcp/oauth/consent_forms.py`
- Test: `tests/test_oauth_consent_forms.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_oauth_consent_forms.py
import pytest

from localmail.mcp.oauth.consent_forms import (
    ConsentDecision,
    ConsentFormError,
    parse_consent_form,
)


def test_allow_with_credentials():
    d = parse_consent_form({"req": "blob", "username": "alice",
                            "password": "pw", "decision": "allow"})
    assert d == ConsentDecision(req="blob", username="alice", password="pw", allow=True)


def test_deny_needs_no_credentials():
    d = parse_consent_form({"req": "blob", "decision": "deny"})
    assert d.allow is False
    assert d.req == "blob"


def test_missing_req_rejected():
    with pytest.raises(ConsentFormError):
        parse_consent_form({"decision": "allow", "username": "a", "password": "b"})


def test_allow_missing_password_rejected():
    with pytest.raises(ConsentFormError):
        parse_consent_form({"req": "blob", "username": "alice", "decision": "allow"})


def test_unknown_decision_rejected():
    with pytest.raises(ConsentFormError):
        parse_consent_form({"req": "blob", "decision": "maybe"})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_oauth_consent_forms.py -q`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

```python
# src/localmail/mcp/oauth/consent_forms.py
"""Pure validation/parsing of the /oauth/consent POST body.

No IO. The router calls this, then (on allow) verifies credentials and mints a
code; (on deny) redirects with error=access_denied.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


class ConsentFormError(ValueError):
    """The submitted form was structurally invalid."""


@dataclass(frozen=True)
class ConsentDecision:
    req: str
    username: str | None
    password: str | None
    allow: bool


def parse_consent_form(form: Mapping[str, str]) -> ConsentDecision:
    req = form.get("req")
    if not req:
        raise ConsentFormError("missing authorization request")
    decision = form.get("decision")
    if decision not in ("allow", "deny"):
        raise ConsentFormError("decision must be 'allow' or 'deny'")
    if decision == "deny":
        return ConsentDecision(req=req, username=None, password=None, allow=False)
    username = form.get("username")
    password = form.get("password")
    if not username or not password:
        raise ConsentFormError("username and password are required to allow")
    return ConsentDecision(req=req, username=username, password=password, allow=True)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_oauth_consent_forms.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/localmail/mcp/oauth/consent_forms.py tests/test_oauth_consent_forms.py
git commit -m "feat(oauth): pure consent-form parsing"
```

---

## Task 5: `clients.py` (DCR store)

**Files:**
- Create: `src/localmail/mcp/oauth/clients.py`
- Test: `tests/test_oauth_clients_store.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_oauth_clients_store.py
from localmail.mcp.oauth import clients


def _register(conn, **over):
    kwargs = dict(
        client_id="cid-abc",
        client_secret_sha256=None,
        redirect_uris=["https://c.example/cb"],
        client_name="Test Client",
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        token_endpoint_auth_method="none",
        scope=None,
    )
    kwargs.update(over)
    clients.register_client(conn, **kwargs)
    conn.commit()
    return kwargs["client_id"]


def test_register_then_get(db_conn):
    cid = _register(db_conn)
    row = clients.get_client(db_conn, cid)
    assert row is not None
    assert row.client_id == cid
    assert row.redirect_uris == ["https://c.example/cb"]
    assert row.client_secret_sha256 is None


def test_get_unknown_returns_none(db_conn):
    assert clients.get_client(db_conn, "nope") is None


def test_touch_last_used(db_conn):
    cid = _register(db_conn)
    assert clients.get_client(db_conn, cid).last_used_at is None
    clients.touch_last_used(db_conn, cid)
    db_conn.commit()
    assert clients.get_client(db_conn, cid).last_used_at is not None


def test_cleanup_unused_deletes_only_stale_unused(db_conn):
    used = _register(db_conn, client_id="used")
    clients.touch_last_used(db_conn, used)
    _register(db_conn, client_id="fresh-unused")
    db_conn.commit()
    # retention 0 → every unused client is stale; used one is kept.
    deleted = clients.cleanup_unused(db_conn, retention_s=0)
    db_conn.commit()
    assert deleted == 1
    assert clients.get_client(db_conn, "fresh-unused") is None
    assert clients.get_client(db_conn, "used") is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_oauth_clients_store.py -q`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

```python
# src/localmail/mcp/oauth/clients.py
"""Dynamic-client-registration store (RFC 7591). Open registration is inert
until a user logs in + consents; spam is bounded by the route rate limit and
`cleanup_unused`.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import psycopg


@dataclass(frozen=True)
class ClientRow:
    client_id: str
    client_secret_sha256: bytes | None
    redirect_uris: list[str]
    client_name: str | None
    grant_types: list[str] | None
    response_types: list[str] | None
    token_endpoint_auth_method: str | None
    scope: str | None
    created_at: datetime
    last_used_at: datetime | None


def register_client(
    conn: psycopg.Connection,
    *,
    client_id: str,
    client_secret_sha256: bytes | None,
    redirect_uris: list[str],
    client_name: str | None,
    grant_types: list[str] | None,
    response_types: list[str] | None,
    token_endpoint_auth_method: str | None,
    scope: str | None,
) -> None:
    """Insert a registered client. Caller commits."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO oauth_clients (client_id, client_secret_sha256, "
            "redirect_uris, client_name, grant_types, response_types, "
            "token_endpoint_auth_method, scope) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (client_id, client_secret_sha256, redirect_uris, client_name,
             grant_types, response_types, token_endpoint_auth_method, scope),
        )


def get_client(conn: psycopg.Connection, client_id: str) -> ClientRow | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT client_id, client_secret_sha256, redirect_uris, client_name, "
            "grant_types, response_types, token_endpoint_auth_method, scope, "
            "created_at, last_used_at FROM oauth_clients WHERE client_id = %s",
            (client_id,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return ClientRow(*row)


def touch_last_used(conn: psycopg.Connection, client_id: str) -> None:
    """Mark a successful token exchange. Caller commits."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE oauth_clients SET last_used_at = now() WHERE client_id = %s",
            (client_id,),
        )


def cleanup_unused(conn: psycopg.Connection, *, retention_s: int) -> int:
    """Delete clients that never completed a token exchange and were created
    more than ``retention_s`` ago. Returns the deleted count. Caller commits.
    """
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM oauth_clients WHERE last_used_at IS NULL "
            "AND created_at < now() - make_interval(secs => %s)",
            (retention_s,),
        )
        return cur.rowcount
```

- [ ] **Step 4: Run test to verify it passes**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_oauth_clients_store.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/localmail/mcp/oauth/clients.py tests/test_oauth_clients_store.py
git commit -m "feat(oauth): DCR client store"
```

---

## Task 6: `codes.py` (authorization-code store)

**Files:**
- Create: `src/localmail/mcp/oauth/codes.py`
- Test: `tests/test_oauth_codes_store.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_oauth_codes_store.py
import pytest

from localmail.api import auth as api_auth
from localmail.mcp.oauth import clients, codes


def _seed_client_and_user(conn):
    clients.register_client(
        conn, client_id="cid", client_secret_sha256=None,
        redirect_uris=["https://c/cb"], client_name=None,
        grant_types=["authorization_code"], response_types=["code"],
        token_endpoint_auth_method="none", scope=None,
    )
    uid = api_auth.create_user(conn, "code-user", "pw")
    conn.commit()
    return uid


def test_mint_then_load(db_conn):
    uid = _seed_client_and_user(db_conn)
    raw = codes.mint_code(
        db_conn, client_id="cid", user_id=uid, redirect_uri="https://c/cb",
        redirect_uri_provided_explicitly=True, code_challenge="chal",
        scopes=[], ttl_s=60,
    )
    db_conn.commit()
    loaded = codes.load_code(db_conn, raw)
    assert loaded is not None
    assert loaded.client_id == "cid"
    assert loaded.user_id == uid
    assert loaded.code_challenge == "chal"
    assert loaded.redirect_uri == "https://c/cb"
    assert loaded.redirect_uri_provided_explicitly is True


def test_consume_is_single_use(db_conn):
    uid = _seed_client_and_user(db_conn)
    raw = codes.mint_code(
        db_conn, client_id="cid", user_id=uid, redirect_uri="https://c/cb",
        redirect_uri_provided_explicitly=True, code_challenge="chal",
        scopes=[], ttl_s=60,
    )
    db_conn.commit()
    assert codes.consume_code(db_conn, raw) is True
    db_conn.commit()
    assert codes.load_code(db_conn, raw) is None
    assert codes.consume_code(db_conn, raw) is False


def test_expired_code_does_not_load(db_conn):
    uid = _seed_client_and_user(db_conn)
    raw = codes.mint_code(
        db_conn, client_id="cid", user_id=uid, redirect_uri="https://c/cb",
        redirect_uri_provided_explicitly=True, code_challenge="chal",
        scopes=[], ttl_s=-1,
    )
    db_conn.commit()
    assert codes.load_code(db_conn, raw) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_oauth_codes_store.py -q`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

```python
# src/localmail/mcp/oauth/codes.py
"""Single-use authorization-code store. Codes are SHA-256-hashed; the raw code
is returned to the client once (via the redirect) and never stored.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import psycopg

from localmail.api.auth import generate_token, hash_token


@dataclass(frozen=True)
class CodeRow:
    client_id: str
    user_id: int
    redirect_uri: str
    redirect_uri_provided_explicitly: bool
    code_challenge: str
    scopes: list[str]
    expires_at: datetime


def mint_code(
    conn: psycopg.Connection,
    *,
    client_id: str,
    user_id: int,
    redirect_uri: str,
    redirect_uri_provided_explicitly: bool,
    code_challenge: str,
    scopes: list[str],
    ttl_s: int,
) -> str:
    """Mint + persist a single-use code; return the raw code. Caller commits."""
    raw = generate_token()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO oauth_authorization_codes (code_sha256, client_id, "
            "user_id, redirect_uri, redirect_uri_provided_explicitly, "
            "code_challenge, scopes, expires_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, now() + make_interval(secs => %s))",
            (hash_token(raw), client_id, user_id, redirect_uri,
             redirect_uri_provided_explicitly, code_challenge, scopes, ttl_s),
        )
    return raw


def load_code(conn: psycopg.Connection, raw_code: str) -> CodeRow | None:
    """Return the unexpired code row, or None. Does not consume it."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT client_id, user_id, redirect_uri, "
            "redirect_uri_provided_explicitly, code_challenge, scopes, expires_at "
            "FROM oauth_authorization_codes "
            "WHERE code_sha256 = %s AND expires_at > now()",
            (hash_token(raw_code),),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return CodeRow(*row)


def consume_code(conn: psycopg.Connection, raw_code: str) -> bool:
    """Delete the code; return True if a row was removed. Caller commits."""
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM oauth_authorization_codes WHERE code_sha256 = %s",
            (hash_token(raw_code),),
        )
        return cur.rowcount > 0
```

- [ ] **Step 4: Run test to verify it passes**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_oauth_codes_store.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/localmail/mcp/oauth/codes.py tests/test_oauth_codes_store.py
git commit -m "feat(oauth): single-use authorization-code store"
```

---

## Task 7: `refresh.py` (refresh-token store)

**Files:**
- Create: `src/localmail/mcp/oauth/refresh.py`
- Test: `tests/test_oauth_refresh_store.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_oauth_refresh_store.py
from localmail.api import auth as api_auth
from localmail.mcp.oauth import clients, refresh


def _seed(conn):
    clients.register_client(
        conn, client_id="cid", client_secret_sha256=None,
        redirect_uris=["https://c/cb"], client_name=None,
        grant_types=["refresh_token"], response_types=["code"],
        token_endpoint_auth_method="none", scope=None,
    )
    uid = api_auth.create_user(conn, "refresh-user", "pw")
    conn.commit()
    return uid


def test_mint_then_load(db_conn):
    uid = _seed(db_conn)
    raw = refresh.mint_refresh(db_conn, client_id="cid", user_id=uid, scopes=[], ttl_s=100)
    db_conn.commit()
    row = refresh.load_refresh(db_conn, raw)
    assert row is not None and row.user_id == uid and row.client_id == "cid"


def test_revoke(db_conn):
    uid = _seed(db_conn)
    raw = refresh.mint_refresh(db_conn, client_id="cid", user_id=uid, scopes=[], ttl_s=100)
    db_conn.commit()
    assert refresh.revoke_refresh(db_conn, raw) is True
    db_conn.commit()
    assert refresh.load_refresh(db_conn, raw) is None


def test_rotate_revokes_old_returns_new(db_conn):
    uid = _seed(db_conn)
    old = refresh.mint_refresh(db_conn, client_id="cid", user_id=uid, scopes=[], ttl_s=100)
    db_conn.commit()
    new = refresh.rotate_refresh(db_conn, old, ttl_s=100)
    db_conn.commit()
    assert new is not None and new != old
    assert refresh.load_refresh(db_conn, old) is None
    assert refresh.load_refresh(db_conn, new) is not None


def test_rotate_unknown_returns_none(db_conn):
    assert refresh.rotate_refresh(db_conn, "bogus", ttl_s=100) is None


def test_expired_refresh_does_not_load(db_conn):
    uid = _seed(db_conn)
    raw = refresh.mint_refresh(db_conn, client_id="cid", user_id=uid, scopes=[], ttl_s=-1)
    db_conn.commit()
    assert refresh.load_refresh(db_conn, raw) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_oauth_refresh_store.py -q`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

```python
# src/localmail/mcp/oauth/refresh.py
"""Rotating refresh-token store. Tokens are SHA-256-hashed. Rotation deletes the
presented token and mints a fresh one with a new sliding expiry, so an active
client never needs re-authentication.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import psycopg

from localmail.api.auth import generate_token, hash_token


@dataclass(frozen=True)
class RefreshRow:
    client_id: str
    user_id: int
    scopes: list[str]
    expires_at: datetime


def mint_refresh(
    conn: psycopg.Connection,
    *,
    client_id: str,
    user_id: int,
    scopes: list[str],
    ttl_s: int,
) -> str:
    """Mint + persist a refresh token; return the raw token. Caller commits."""
    raw = generate_token()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO oauth_refresh_tokens (token_sha256, client_id, user_id, "
            "scopes, expires_at) "
            "VALUES (%s, %s, %s, %s, now() + make_interval(secs => %s))",
            (hash_token(raw), client_id, user_id, scopes, ttl_s),
        )
    return raw


def load_refresh(conn: psycopg.Connection, raw_token: str) -> RefreshRow | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT client_id, user_id, scopes, expires_at "
            "FROM oauth_refresh_tokens "
            "WHERE token_sha256 = %s AND expires_at > now()",
            (hash_token(raw_token),),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return RefreshRow(*row)


def revoke_refresh(conn: psycopg.Connection, raw_token: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM oauth_refresh_tokens WHERE token_sha256 = %s",
            (hash_token(raw_token),),
        )
        return cur.rowcount > 0


def rotate_refresh(
    conn: psycopg.Connection, raw_token: str, *, ttl_s: int
) -> str | None:
    """Revoke ``raw_token`` and mint a fresh one with the same (client, user,
    scopes) and a new sliding expiry. Returns the new raw token, or None if the
    presented token was unknown/expired. Caller commits.
    """
    row = load_refresh(conn, raw_token)
    if row is None:
        return None
    revoke_refresh(conn, raw_token)
    return mint_refresh(
        conn, client_id=row.client_id, user_id=row.user_id,
        scopes=row.scopes, ttl_s=ttl_s,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_oauth_refresh_store.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/localmail/mcp/oauth/refresh.py tests/test_oauth_refresh_store.py
git commit -m "feat(oauth): rotating refresh-token store"
```

---

## Task 8: `access.py` (access-token bridge over `api_tokens`)

**Files:**
- Create: `src/localmail/mcp/oauth/access.py`
- Test: `tests/test_oauth_access_bridge.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_oauth_access_bridge.py
from localmail.api import auth as api_auth
from localmail.mcp.oauth import access, clients


def _seed(conn):
    clients.register_client(
        conn, client_id="cid", client_secret_sha256=None,
        redirect_uris=["https://c/cb"], client_name=None,
        grant_types=["authorization_code"], response_types=["code"],
        token_endpoint_auth_method="none", scope=None,
    )
    uid = api_auth.create_user(conn, "access-user", "pw")
    conn.commit()
    return uid


def test_minted_access_token_verifies_via_existing_verifier(db_conn):
    uid = _seed(db_conn)
    raw = access.mint_access(db_conn, user_id=uid, client_id="cid", ttl_s=3600)
    db_conn.commit()
    # The existing resource-server verifier accepts it unchanged.
    user = api_auth.verify_token(db_conn, raw)
    assert user is not None and user.id == uid


def test_minted_access_token_records_client_id(db_conn):
    uid = _seed(db_conn)
    raw = access.mint_access(db_conn, user_id=uid, client_id="cid", ttl_s=3600)
    db_conn.commit()
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT oauth_client_id FROM api_tokens WHERE token_sha256 = %s",
            (api_auth.hash_token(raw),),
        )
        assert cur.fetchone()[0] == "cid"


def test_load_access_returns_subject(db_conn):
    uid = _seed(db_conn)
    raw = access.mint_access(db_conn, user_id=uid, client_id="cid", ttl_s=3600)
    db_conn.commit()
    at = access.load_access(db_conn, raw)
    assert at is not None and at.subject == str(uid) and at.client_id == "cid"


def test_load_unknown_returns_none(db_conn):
    assert access.load_access(db_conn, "bogus") is None


def test_revoke_access(db_conn):
    uid = _seed(db_conn)
    raw = access.mint_access(db_conn, user_id=uid, client_id="cid", ttl_s=3600)
    db_conn.commit()
    assert access.revoke_access(db_conn, raw) is True
    db_conn.commit()
    assert access.load_access(db_conn, raw) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `unset VIRTUAL_ENV && uv run --extra mcp pytest tests/test_oauth_access_bridge.py -q`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

```python
# src/localmail/mcp/oauth/access.py
"""Access-token bridge: OAuth access tokens live in `api_tokens` so the existing
resource-server verifier (`api.auth.verify_token`) and per-user ACL apply
unchanged. `oauth_client_id` attributes the token to its client.

`load_access` returns the SDK's `AccessToken` (subject = user id) for
`provider.load_access_token`. The SDK import is function-local so this module
stays import-safe without the `mcp` extra.
"""
from __future__ import annotations

import psycopg

from localmail.api.auth import generate_token, hash_token, verify_token


def mint_access(
    conn: psycopg.Connection, *, user_id: int, client_id: str, ttl_s: int
) -> str:
    """Mint an access token into api_tokens; return the raw token. Caller commits."""
    raw = generate_token()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO api_tokens (token_sha256, user_id, expires_at, oauth_client_id) "
            "VALUES (%s, %s, now() + make_interval(secs => %s), %s)",
            (hash_token(raw), user_id, ttl_s, client_id),
        )
    return raw


def load_access(conn: psycopg.Connection, raw_token: str):  # -> AccessToken | None
    """Verify an access token and return the SDK AccessToken, or None."""
    from mcp.server.auth.provider import AccessToken

    user = verify_token(conn, raw_token)
    if user is None:
        return None
    with conn.cursor() as cur:
        cur.execute(
            "SELECT oauth_client_id FROM api_tokens WHERE token_sha256 = %s",
            (hash_token(raw_token),),
        )
        row = cur.fetchone()
    client_id = row[0] if row and row[0] is not None else "localmail"
    return AccessToken(
        token=raw_token, client_id=client_id, scopes=[], subject=str(user.id)
    )


def revoke_access(conn: psycopg.Connection, raw_token: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM api_tokens WHERE token_sha256 = %s",
            (hash_token(raw_token),),
        )
        return cur.rowcount > 0
```

- [ ] **Step 4: Run test to verify it passes**

Run: `unset VIRTUAL_ENV && uv run --extra mcp pytest tests/test_oauth_access_bridge.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/localmail/mcp/oauth/access.py tests/test_oauth_access_bridge.py
git commit -m "feat(oauth): access-token bridge reusing api_tokens"
```

---

## Task 9: `provider.py` (`LocalmailASProvider`)

**Files:**
- Create: `src/localmail/mcp/oauth/provider.py`
- Test: `tests/test_oauth_provider.py`

The provider implements the 9 SDK methods over the Task 5-8 stores. It takes a
`ConnectionPool` + `McpConfig`. `authorize` builds the consent redirect; the
actual code minting happens in the consent router (Task 10) — `authorize` only
packs the signed blob and points at `/oauth/consent`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_oauth_provider.py
import anyio
import pytest
from mcp.server.auth.provider import AuthorizationParams
from mcp.shared.auth import OAuthClientInformationFull

from localmail.api import auth as api_auth
from localmail.config import McpConfig
from localmail.mcp.oauth import codes
from localmail.mcp.oauth.consent_state import decode_consent_state
from localmail.mcp.oauth.provider import LocalmailASProvider

SIGNING_KEY = b"provider-test-key"


def _provider(pool):
    return LocalmailASProvider(
        pool, config=McpConfig(authorization_server_enabled=True),
        signing_key=SIGNING_KEY, consent_path="/oauth/consent",
    )


def _client(cid="cid", uris=("https://c/cb",)):
    return OAuthClientInformationFull(
        client_id=cid, redirect_uris=list(uris),
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"], token_endpoint_auth_method="none",
    )


def test_register_and_get_client(db_pool):
    p = _provider(db_pool)
    anyio.run(p.register_client, _client())
    got = anyio.run(p.get_client, "cid")
    assert got is not None and got.client_id == "cid"


def test_authorize_returns_consent_redirect_with_signed_blob(db_pool):
    p = _provider(db_pool)
    anyio.run(p.register_client, _client())
    params = AuthorizationParams(
        state="st", scopes=[], code_challenge="chal",
        redirect_uri="https://c/cb", redirect_uri_provided_explicitly=True,
        resource=None,
    )
    url = anyio.run(p.authorize, _client(), params)
    assert url.startswith("/oauth/consent?req=")
    blob = url.split("req=", 1)[1]
    payload = decode_consent_state(blob, key=SIGNING_KEY)
    assert payload.client_id == "cid"
    assert payload.code_challenge == "chal"
    assert payload.redirect_uri == "https://c/cb"


def test_exchange_authorization_code_mints_tokens_and_consumes_code(db_pool):
    p = _provider(db_pool)
    anyio.run(p.register_client, _client())
    with db_pool.connection() as conn:
        uid = api_auth.create_user(conn, "prov-user", "pw")
        raw_code = codes.mint_code(
            conn, client_id="cid", user_id=uid, redirect_uri="https://c/cb",
            redirect_uri_provided_explicitly=True, code_challenge="chal",
            scopes=[], ttl_s=60,
        )
        conn.commit()
    loaded = anyio.run(p.load_authorization_code, _client(), raw_code)
    assert loaded is not None and loaded.subject == str(uid)
    token = anyio.run(p.exchange_authorization_code, _client(), loaded)
    assert token.access_token and token.refresh_token
    # code is single-use now
    assert anyio.run(p.load_authorization_code, _client(), raw_code) is None
    # the minted access token authenticates via the resource path
    at = anyio.run(p.load_access_token, token.access_token)
    assert at is not None and at.subject == str(uid)


def test_exchange_refresh_rotates(db_pool):
    p = _provider(db_pool)
    anyio.run(p.register_client, _client())
    with db_pool.connection() as conn:
        uid = api_auth.create_user(conn, "prov-refresh", "pw")
        raw_code = codes.mint_code(
            conn, client_id="cid", user_id=uid, redirect_uri="https://c/cb",
            redirect_uri_provided_explicitly=True, code_challenge="chal",
            scopes=[], ttl_s=60,
        )
        conn.commit()
    loaded = anyio.run(p.load_authorization_code, _client(), raw_code)
    token = anyio.run(p.exchange_authorization_code, _client(), loaded)
    old_refresh = anyio.run(p.load_refresh_token, _client(), token.refresh_token)
    assert old_refresh is not None
    new = anyio.run(p.exchange_refresh_token, _client(), old_refresh, [])
    assert new.refresh_token and new.refresh_token != token.refresh_token
    # old refresh no longer loads
    assert anyio.run(p.load_refresh_token, _client(), token.refresh_token) is None
```

Add a `db_pool` fixture if not already present — check `tests/conftest.py`; the
codebase already exposes a pool fixture (search for `def pool` / `def db_pool`).
If the existing fixture is named `pool`, use that name in the test instead.

- [ ] **Step 2: Run test to verify it fails**

Run: `unset VIRTUAL_ENV && uv run --extra mcp pytest tests/test_oauth_provider.py -q`
Expected: FAIL — `ModuleNotFoundError: localmail.mcp.oauth.provider`.

- [ ] **Step 3: Implement**

```python
# src/localmail/mcp/oauth/provider.py
"""LocalmailASProvider — the MCP SDK OAuthAuthorizationServerProvider backed by
the localmail OAuth stores.

`authorize` does NOT mint a code: it packs the authorization params into a
signed consent blob and redirects to the interactive consent router, which mints
the code after a verified login. PKCE S256 + redirect_uri matching are done by
the SDK's TokenHandler using the AuthorizationCode we return from
`load_authorization_code`; this provider never sees the code_verifier.
"""
from __future__ import annotations

import time
from urllib.parse import urlencode

import anyio.to_thread
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    RefreshToken,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from psycopg_pool import ConnectionPool

from localmail.config import McpConfig
from localmail.mcp.oauth import access, clients, codes, refresh
from localmail.mcp.oauth.consent_state import ConsentPayload, encode_consent_state


class LocalmailASProvider(
    OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]
):
    def __init__(
        self,
        pool: ConnectionPool,
        *,
        config: McpConfig,
        signing_key: bytes,
        consent_path: str,
    ) -> None:
        self._pool = pool
        self._cfg = config
        self._key = signing_key
        self._consent_path = consent_path

    # --- client registration -------------------------------------------------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        row = await anyio.to_thread.run_sync(self._get_client_sync, client_id)
        return row

    def _get_client_sync(self, client_id: str) -> OAuthClientInformationFull | None:
        with self._pool.connection() as conn:
            row = clients.get_client(conn, client_id)
        if row is None:
            return None
        return OAuthClientInformationFull(
            client_id=row.client_id,
            redirect_uris=row.redirect_uris,
            client_name=row.client_name,
            grant_types=row.grant_types or [],
            response_types=row.response_types or [],
            token_endpoint_auth_method=row.token_endpoint_auth_method or "none",
            scope=row.scope,
        )

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        await anyio.to_thread.run_sync(self._register_client_sync, client_info)

    def _register_client_sync(self, ci: OAuthClientInformationFull) -> None:
        with self._pool.connection() as conn:
            clients.register_client(
                conn,
                client_id=ci.client_id,
                client_secret_sha256=None,
                redirect_uris=[str(u) for u in ci.redirect_uris],
                client_name=ci.client_name,
                grant_types=list(ci.grant_types or []),
                response_types=list(ci.response_types or []),
                token_endpoint_auth_method=ci.token_endpoint_auth_method,
                scope=ci.scope,
            )
            conn.commit()

    # --- authorize -> consent redirect ---------------------------------------

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        payload = ConsentPayload(
            client_id=client.client_id,
            redirect_uri=str(params.redirect_uri),
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            code_challenge=params.code_challenge,
            scopes=list(params.scopes or []),
            state=params.state,
            exp=int(time.time()) + self._cfg.oauth_consent_state_ttl_s,
        )
        blob = encode_consent_state(payload, key=self._key)
        return f"{self._consent_path}?{urlencode({'req': blob})}"

    # --- authorization code --------------------------------------------------

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        return await anyio.to_thread.run_sync(
            self._load_code_sync, client.client_id, authorization_code
        )

    def _load_code_sync(self, client_id: str, raw_code: str) -> AuthorizationCode | None:
        with self._pool.connection() as conn:
            row = codes.load_code(conn, raw_code)
        if row is None or row.client_id != client_id:
            return None
        return AuthorizationCode(
            code=raw_code,
            scopes=row.scopes,
            expires_at=row.expires_at.timestamp(),
            client_id=row.client_id,
            code_challenge=row.code_challenge,
            redirect_uri=row.redirect_uri,
            redirect_uri_provided_explicitly=row.redirect_uri_provided_explicitly,
            subject=str(row.user_id),
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        return await anyio.to_thread.run_sync(
            self._exchange_code_sync, client.client_id, authorization_code
        )

    def _exchange_code_sync(
        self, client_id: str, auth_code: AuthorizationCode
    ) -> OAuthToken:
        user_id = int(auth_code.subject)
        with self._pool.connection() as conn:
            codes.consume_code(conn, auth_code.code)
            access_raw = access.mint_access(
                conn, user_id=user_id, client_id=client_id,
                ttl_s=self._cfg.oauth_access_token_ttl_s,
            )
            refresh_raw = refresh.mint_refresh(
                conn, client_id=client_id, user_id=user_id,
                scopes=auth_code.scopes, ttl_s=self._cfg.oauth_refresh_token_ttl_s,
            )
            clients.touch_last_used(conn, client_id)
            conn.commit()
        return OAuthToken(
            access_token=access_raw,
            token_type="Bearer",
            expires_in=self._cfg.oauth_access_token_ttl_s,
            refresh_token=refresh_raw,
        )

    # --- refresh -------------------------------------------------------------

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        return await anyio.to_thread.run_sync(
            self._load_refresh_sync, client.client_id, refresh_token
        )

    def _load_refresh_sync(self, client_id: str, raw: str) -> RefreshToken | None:
        with self._pool.connection() as conn:
            row = refresh.load_refresh(conn, raw)
        if row is None or row.client_id != client_id:
            return None
        return RefreshToken(
            token=raw, client_id=row.client_id, scopes=row.scopes,
            expires_at=int(row.expires_at.timestamp()),
        )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        return await anyio.to_thread.run_sync(
            self._exchange_refresh_sync, client.client_id, refresh_token
        )

    def _exchange_refresh_sync(
        self, client_id: str, rt: RefreshToken
    ) -> OAuthToken:
        with self._pool.connection() as conn:
            new_refresh = refresh.rotate_refresh(
                conn, rt.token, ttl_s=self._cfg.oauth_refresh_token_ttl_s
            )
            assert new_refresh is not None  # caller already loaded it
            row = refresh.load_refresh(conn, new_refresh)
            assert row is not None
            access_raw = access.mint_access(
                conn, user_id=row.user_id, client_id=client_id,
                ttl_s=self._cfg.oauth_access_token_ttl_s,
            )
            conn.commit()
        return OAuthToken(
            access_token=access_raw,
            token_type="Bearer",
            expires_in=self._cfg.oauth_access_token_ttl_s,
            refresh_token=new_refresh,
        )

    # --- resource-server verify + revoke ------------------------------------

    async def load_access_token(self, token: str) -> AccessToken | None:
        return await anyio.to_thread.run_sync(self._load_access_sync, token)

    def _load_access_sync(self, token: str) -> AccessToken | None:
        with self._pool.connection() as conn:
            at = access.load_access(conn, token)
            conn.commit()
        return at

    async def revoke_token(self, token) -> None:  # AccessToken | RefreshToken
        await anyio.to_thread.run_sync(self._revoke_sync, token.token)

    def _revoke_sync(self, raw: str) -> None:
        with self._pool.connection() as conn:
            if not access.revoke_access(conn, raw):
                refresh.revoke_refresh(conn, raw)
            conn.commit()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `unset VIRTUAL_ENV && uv run --extra mcp pytest tests/test_oauth_provider.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/localmail/mcp/oauth/provider.py tests/test_oauth_provider.py
git commit -m "feat(oauth): LocalmailASProvider over the OAuth stores"
```

---

## Task 10: `/oauth/consent` router + template

**Files:**
- Create: `src/localmail/serve/oauth/__init__.py` (empty)
- Create: `src/localmail/serve/oauth/consent_router.py`
- Create: `src/localmail/serve/oauth/templates/consent.html`
- Test: `tests/test_serve_oauth_consent.py`

The router is built by a factory taking the pool, signing key, `McpConfig`, and
`AuthConfig` (for login rate limits). It renders the login+consent page on GET
and processes the decision on POST. It reuses
`api.auth.check_login_rate_limits` / `record_login_attempt` /
`verify_password` so the consent login obeys the same brute-force caps as
`/v1/auth/login`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_serve_oauth_consent.py
import time

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from localmail.api import auth as api_auth
from localmail.config import AuthConfig, McpConfig
from localmail.mcp.oauth import clients
from localmail.mcp.oauth.consent_state import ConsentPayload, encode_consent_state

KEY = b"consent-router-key"


@pytest.fixture
def consent_client(db_pool):
    from localmail.serve.oauth.consent_router import build_consent_router

    with db_pool.connection() as conn:
        clients.register_client(
            conn, client_id="cid", client_secret_sha256=None,
            redirect_uris=["https://c/cb"], client_name="C",
            grant_types=["authorization_code"], response_types=["code"],
            token_endpoint_auth_method="none", scope=None,
        )
        api_auth.create_user(conn, "consent-user", "secret-pw")
        api_auth.reset_login_rate_limiter(conn)
        conn.commit()
    router = build_consent_router(
        pool=db_pool, signing_key=KEY,
        mcp_config=McpConfig(authorization_server_enabled=True),
        auth_config=AuthConfig(),
    )
    app = Starlette(routes=router)
    return TestClient(app, follow_redirects=False)


def _blob():
    return encode_consent_state(
        ConsentPayload(
            client_id="cid", redirect_uri="https://c/cb",
            redirect_uri_provided_explicitly=True, code_challenge="chal",
            scopes=[], state="st", exp=int(time.time()) + 300,
        ),
        key=KEY,
    )


def test_get_renders_form(consent_client):
    r = consent_client.get("/oauth/consent", params={"req": _blob()})
    assert r.status_code == 200
    assert "consent-user" not in r.text  # username not pre-filled
    assert "password" in r.text.lower()


def test_post_allow_with_valid_credentials_redirects_with_code(consent_client):
    r = consent_client.post("/oauth/consent", data={
        "req": _blob(), "username": "consent-user",
        "password": "secret-pw", "decision": "allow",
    })
    assert r.status_code == 303
    loc = r.headers["location"]
    assert loc.startswith("https://c/cb?")
    assert "code=" in loc and "state=st" in loc


def test_post_deny_redirects_with_error(consent_client):
    r = consent_client.post("/oauth/consent", data={"req": _blob(), "decision": "deny"})
    assert r.status_code == 303
    assert "error=access_denied" in r.headers["location"]
    assert "state=st" in r.headers["location"]


def test_post_allow_bad_password_rerenders_with_error(consent_client):
    r = consent_client.post("/oauth/consent", data={
        "req": _blob(), "username": "consent-user",
        "password": "wrong", "decision": "allow",
    })
    assert r.status_code == 401
    assert "incorrect" in r.text.lower() or "invalid" in r.text.lower()


def test_post_tampered_blob_rejected(consent_client):
    r = consent_client.post("/oauth/consent", data={
        "req": "tampered.blob", "username": "consent-user",
        "password": "secret-pw", "decision": "allow",
    })
    assert r.status_code == 400
```

- [ ] **Step 2: Run test to verify it fails**

Run: `unset VIRTUAL_ENV && uv run --extra mcp pytest tests/test_serve_oauth_consent.py -q`
Expected: FAIL — `ModuleNotFoundError: localmail.serve.oauth.consent_router`.

- [ ] **Step 3: Implement the template**

```html
{# src/localmail/serve/oauth/templates/consent.html #}
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Authorize application — localmail</title>
</head>
<body>
  <h1>Authorize {{ client_name }}</h1>
  <p>
    <strong>{{ client_name }}</strong> is requesting access to read mail in the
    accounts you have been granted. Sign in to allow it.
  </p>
  {% if error %}<p role="alert">{{ error }}</p>{% endif %}
  <form method="post" action="/oauth/consent">
    <input type="hidden" name="req" value="{{ req }}">
    <label>Username <input name="username" autocomplete="username"></label>
    <label>Password
      <input type="password" name="password" autocomplete="current-password">
    </label>
    <button type="submit" name="decision" value="allow">Allow</button>
    <button type="submit" name="decision" value="deny">Deny</button>
  </form>
</body>
</html>
```

- [ ] **Step 4: Implement the router**

```python
# src/localmail/serve/oauth/consent_router.py
"""Interactive login + consent interstitial for the OAuth authorization flow.

GET  /oauth/consent?req=<blob>  → render the login + Allow/Deny form.
POST /oauth/consent             → verify the signed blob; on Allow, rate-limited
                                  credential check + mint a single-use code and
                                  302 to the client redirect_uri; on Deny, 302
                                  with error=access_denied.

Credential checks reuse the /v1/auth/login rate-limit path so this surface is
not a brute-force bypass.
"""
from __future__ import annotations

from pathlib import Path
from urllib.parse import urlencode

from psycopg_pool import ConnectionPool
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Route
from starlette.templating import Jinja2Templates

from localmail.api import auth as api_auth
from localmail.api.errors import RateLimited
from localmail.config import AuthConfig, McpConfig
from localmail.mcp.oauth import clients, codes
from localmail.mcp.oauth.consent_forms import ConsentFormError, parse_consent_form
from localmail.mcp.oauth.consent_state import (
    ConsentStateExpired,
    ConsentStateInvalid,
    decode_consent_state,
)

_TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _redirect_with(redirect_uri: str, **params: str) -> RedirectResponse:
    sep = "&" if "?" in redirect_uri else "?"
    return RedirectResponse(redirect_uri + sep + urlencode(params), status_code=303)


def build_consent_router(
    *,
    pool: ConnectionPool,
    signing_key: bytes,
    mcp_config: McpConfig,
    auth_config: AuthConfig,
) -> list[Route]:
    def _client_name(client_id: str) -> str:
        with pool.connection() as conn:
            row = clients.get_client(conn, client_id)
        return (row.client_name if row and row.client_name else client_id)

    async def get_consent(request: Request) -> Response:
        blob = request.query_params.get("req", "")
        try:
            payload = decode_consent_state(blob, key=signing_key)
        except (ConsentStateInvalid, ConsentStateExpired):
            return HTMLResponse("invalid or expired authorization request", status_code=400)
        return _TEMPLATES.TemplateResponse(
            request, "consent.html",
            {"req": blob, "client_name": _client_name(payload.client_id), "error": None},
        )

    async def post_consent(request: Request) -> Response:
        form = await request.form()
        try:
            decision = parse_consent_form({k: str(v) for k, v in form.items()})
        except ConsentFormError as exc:
            return HTMLResponse(str(exc), status_code=400)
        try:
            payload = decode_consent_state(decision.req, key=signing_key)
        except (ConsentStateInvalid, ConsentStateExpired):
            return HTMLResponse("invalid or expired authorization request", status_code=400)

        if not decision.allow:
            return _redirect_with(
                payload.redirect_uri, error="access_denied",
                **({"state": payload.state} if payload.state else {}),
            )

        client_ip = request.client.host if request.client else None
        with pool.connection() as conn:
            try:
                api_auth.check_login_rate_limits(
                    conn, decision.username, client_ip, cfg=auth_config
                )
            except RateLimited as exc:
                return HTMLResponse(str(exc), status_code=429)
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, password_hash FROM api_users "
                    "WHERE username = %s AND disabled_at IS NULL",
                    (decision.username,),
                )
                row = cur.fetchone()
            ok = row is not None and api_auth.verify_password(decision.password, row[1])
            api_auth.record_login_attempt(
                conn, decision.username, client_ip,
                "success" if ok else "failure",
            )
            if not ok:
                return _TEMPLATES.TemplateResponse(
                    request, "consent.html",
                    {"req": decision.req, "client_name": _client_name(payload.client_id),
                     "error": "invalid username or password"},
                    status_code=401,
                )
            raw_code = codes.mint_code(
                conn, client_id=payload.client_id, user_id=row[0],
                redirect_uri=payload.redirect_uri,
                redirect_uri_provided_explicitly=payload.redirect_uri_provided_explicitly,
                code_challenge=payload.code_challenge, scopes=payload.scopes,
                ttl_s=mcp_config.oauth_authorization_code_ttl_s,
            )
            conn.commit()
        return _redirect_with(
            payload.redirect_uri, code=raw_code,
            **({"state": payload.state} if payload.state else {}),
        )

    return [
        Route("/oauth/consent", get_consent, methods=["GET"]),
        Route("/oauth/consent", post_consent, methods=["POST"]),
    ]
```

Add `src/localmail/serve/oauth/__init__.py` (empty). If `Jinja2Templates`'
`TemplateResponse(request, name, ctx)` signature differs in the pinned Starlette,
match the call style already used in `serve/admin/*_panel_router.py`.

- [ ] **Step 5: Run test to verify it passes**

Run: `unset VIRTUAL_ENV && uv run --extra mcp pytest tests/test_serve_oauth_consent.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/localmail/serve/oauth/ tests/test_serve_oauth_consent.py
git commit -m "feat(oauth): interactive /oauth/consent login + consent router"
```

---

## Task 11: Wire the AS into `create_app` (gating + fail-loud)

**Files:**
- Modify: `src/localmail/serve/app.py` (`_try_build_mcp` + `create_app`)
- Modify: `src/localmail/mcp/__init__.py` (export `build_as_provider` behind the guarded import)
- Test: `tests/test_serve_oauth_gating.py`

When `mcp.authorization_server_enabled` is true: require
`[serve].state_signing_key` (fail loud at build if empty); build the provider;
pass it to `build_mcp_server` as `auth_server_provider` (extend
`build_mcp_server` to accept an optional provider and, when present, pass
`auth_server_provider=` + `ClientRegistrationOptions(enabled=True)` +
`RevocationOptions(enabled=True)` instead of `token_verifier=`); and extend the
app routes with the consent router. When false: behaviour is exactly as today
(`token_verifier=LocalmailTokenVerifier`).

- [ ] **Step 1: Write the failing test**

`create_app`'s VERIFIED signature is keyword-only and takes `db_dsn` (it builds
the pool internally) — NOT a `pool=`. Relevant kwargs:
`create_app(*, db_dsn: str, searcher=None, serve_config: ServeConfig | None,
auth_config: AuthConfig | None, enable_mcp: bool, mcp_config: McpConfig | None)`.
The MCP build happens in `_try_build_mcp(pool, searcher, mcp_config)`, which must
be extended to also receive `serve_config` + `auth_config` (for the signing key +
login rate limits). Discovery routes are already appended via
`app.router.routes.extend(mcp_discovery_routes)` — append the consent routes the
same way.

```python
# tests/test_serve_oauth_gating.py
import pytest

from localmail.config import McpConfig, ServeConfig
from localmail.serve.app import create_app


def _has_route(app, path: str) -> bool:
    return any(getattr(r, "path", None) == path for r in app.router.routes)


def test_consent_route_absent_when_as_disabled(db_dsn, db_conn):
    app = create_app(
        db_dsn=db_dsn, searcher=None, serve_config=ServeConfig(),
        enable_mcp=True, mcp_config=McpConfig(enabled=True),
    )
    assert not _has_route(app, "/oauth/consent")


def test_consent_route_present_when_as_enabled(db_dsn, db_conn):
    app = create_app(
        db_dsn=db_dsn, searcher=None,
        serve_config=ServeConfig(state_signing_key="x" * 32),
        enable_mcp=True,
        mcp_config=McpConfig(enabled=True, authorization_server_enabled=True),
    )
    assert _has_route(app, "/oauth/consent")


def test_as_enabled_without_signing_key_fails_loud(db_dsn, db_conn):
    with pytest.raises(ValueError, match="state_signing_key"):
        create_app(
            db_dsn=db_dsn, searcher=None, serve_config=ServeConfig(),
            enable_mcp=True,
            mcp_config=McpConfig(enabled=True, authorization_server_enabled=True),
        )
```

Note: `ServeConfig` has a `field_validator` on `state_signing_key` — check its
constraints (e.g. a minimum length) and use a value that satisfies it in the
"enabled" test (`"x" * 32` is a safe placeholder; adjust if the validator
requires a specific shape).

- [ ] **Step 2: Run test to verify it fails**

Run: `unset VIRTUAL_ENV && uv run --extra mcp pytest tests/test_serve_oauth_gating.py -q`
Expected: FAIL — no `/oauth/consent` route / no fail-loud guard.

- [ ] **Step 3: Implement**

In `src/localmail/serve/app.py`:

- Extend `_try_build_mcp` (and `build_mcp_server`) so that when
  `mcp_config.authorization_server_enabled`:
  - assert `serve_config.state_signing_key` is non-empty, else
    `raise ValueError("authorization_server_enabled requires [serve].state_signing_key")`.
  - build `provider = build_as_provider(pool, config=mcp_config, signing_key=serve_config.state_signing_key.encode(), consent_path="/oauth/consent")`.
  - pass `auth_server_provider=provider` to `FastMCP` (and
    `client_registration_options=ClientRegistrationOptions(enabled=True)`,
    `revocation_options=RevocationOptions(enabled=True)` on `AuthSettings`)
    **instead of** `token_verifier=`.
  - build the consent routes via `build_consent_router(pool=pool,
    signing_key=serve_config.state_signing_key.encode(), mcp_config=mcp_config,
    auth_config=auth_config)` and return them alongside the discovery routes.
- In `create_app`, extend `app.router.routes` with the returned consent routes
  (next to `mcp_discovery_routes`).
- In `src/localmail/mcp/__init__.py`, export `build_as_provider` (a thin factory
  returning `LocalmailASProvider`) behind the same guarded import as
  `build_mcp_server`.

`build_mcp_server` gains an optional `auth_server_provider=None` param; when
provided it takes precedence over `token_verifier`. Keep the existing
`token_verifier` path unchanged for the disabled case.

- [ ] **Step 4: Run test to verify it passes**

Run: `unset VIRTUAL_ENV && uv run --extra mcp pytest tests/test_serve_oauth_gating.py -q`
Expected: PASS.

- [ ] **Step 5: Run the full serve + mcp suites for regressions**

Run: `unset VIRTUAL_ENV && uv run --extra mcp pytest tests/test_serve_*.py tests/test_mcp_*.py -q`
Expected: PASS (no regression in the disabled/default path).

- [ ] **Step 6: Commit**

```bash
git add src/localmail/serve/app.py src/localmail/mcp/__init__.py tests/test_serve_oauth_gating.py
git commit -m "feat(oauth): wire AS provider + consent router into create_app (gated, fail-loud)"
```

---

## Task 11b: Registration rate-limiting + unused-client cleanup

**Files:**
- Create: `src/localmail/mcp/oauth/registration.py` — DB-backed sliding-window store + sweep.
- Create: `src/localmail/serve/oauth/registration_guard.py` — Starlette middleware gating the `/register` path.
- Modify: `src/localmail/serve/app.py` — install the middleware + call `clients.cleanup_unused` from the same sweep.
- Test: `tests/test_oauth_registration_guard.py`

The SDK owns the `/register` route inside the FastMCP sub-mount, so the per-IP
cap is enforced by a top-level HTTP middleware that matches the registration
path and 429s before the request reaches the sub-app. Counting is DB-backed
(multi-worker-safe, mirroring `api_login_attempts`). `cleanup_unused` is invoked
opportunistically from the same code path (advisory-lock-gated, like the login
sweep) so unused client rows don't accumulate.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_oauth_registration_guard.py
from localmail.mcp.oauth import clients, registration


def test_count_and_over_limit(db_conn):
    registration.reset(db_conn)
    db_conn.commit()
    for _ in range(3):
        registration.record(db_conn, "1.2.3.4")
    db_conn.commit()
    assert registration.count_recent(db_conn, "1.2.3.4", window_s=3600) == 3
    assert registration.over_limit(db_conn, "1.2.3.4", window_s=3600, max_n=3) is True
    assert registration.over_limit(db_conn, "1.2.3.4", window_s=3600, max_n=4) is False
    # a different IP is independent
    assert registration.count_recent(db_conn, "9.9.9.9", window_s=3600) == 0


def test_sweep_deletes_old(db_conn):
    registration.reset(db_conn)
    registration.record(db_conn, "1.2.3.4")
    db_conn.commit()
    # retention 0 → everything is stale
    deleted = registration.sweep(db_conn, retention_s=0)
    db_conn.commit()
    assert deleted >= 1


def test_cleanup_unused_runs_from_sweep(db_conn):
    # cleanup_unused is exercised here too so the wiring is covered end-to-end.
    clients.register_client(
        db_conn, client_id="stale", client_secret_sha256=None,
        redirect_uris=["https://c/cb"], client_name=None,
        grant_types=["authorization_code"], response_types=["code"],
        token_endpoint_auth_method="none", scope=None,
    )
    db_conn.commit()
    assert clients.cleanup_unused(db_conn, retention_s=0) == 1
    db_conn.commit()
    assert clients.get_client(db_conn, "stale") is None
```

For the middleware itself, add a route-level test against a tiny Starlette app
that mounts the guard plus a stub `/register` returning 200, and assert the
`oauth_registration_max + 1`-th POST from the same client returns 429:

```python
# append to tests/test_oauth_registration_guard.py
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from localmail.config import McpConfig


def test_middleware_caps_registration(db_pool):
    from localmail.serve.oauth.registration_guard import RegistrationRateLimit

    with db_pool.connection() as conn:
        from localmail.mcp.oauth import registration
        registration.reset(conn)
        conn.commit()

    async def stub_register(request):
        return JSONResponse({"client_id": "x"})

    app = Starlette(routes=[Route("/register", stub_register, methods=["POST"])])
    app.add_middleware(
        RegistrationRateLimit, pool=db_pool,
        config=McpConfig(oauth_registration_max=2, oauth_registration_window_s=3600),
        register_path_suffix="/register",
    )
    client = TestClient(app)
    assert client.post("/register", json={}).status_code == 200
    assert client.post("/register", json={}).status_code == 200
    assert client.post("/register", json={}).status_code == 429
```

- [ ] **Step 2: Run test to verify it fails**

Run: `unset VIRTUAL_ENV && uv run --extra mcp pytest tests/test_oauth_registration_guard.py -q`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Implement the store**

```python
# src/localmail/mcp/oauth/registration.py
"""DB-backed per-IP sliding-window rate limit for open Dynamic Client
Registration, mirroring api_login_attempts (multi-worker-safe).
"""
from __future__ import annotations

import psycopg

# Stable advisory-lock key for the registration sweep (distinct from the login
# sweep's key). Arbitrary fixed int64.
_SWEEP_LOCK_KEY = 0x6F_61_75_74_68_72_65_67  # "oauthreg" in ASCII


def reset(conn: psycopg.Connection) -> None:
    """Test-only: truncate the audit table. Caller commits."""
    with conn.cursor() as cur:
        cur.execute("TRUNCATE oauth_registration_attempts RESTART IDENTITY")


def record(conn: psycopg.Connection, ip: str | None) -> None:
    """Append one registration attempt. Caller commits."""
    with conn.cursor() as cur:
        cur.execute("INSERT INTO oauth_registration_attempts (ip) VALUES (%s)", (ip,))


def count_recent(conn: psycopg.Connection, ip: str | None, *, window_s: int) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM oauth_registration_attempts "
            "WHERE ip = %s AND ts > now() - make_interval(secs => %s)",
            (ip, window_s),
        )
        row = cur.fetchone()
    assert row is not None
    return int(row[0])


def over_limit(
    conn: psycopg.Connection, ip: str | None, *, window_s: int, max_n: int
) -> bool:
    return count_recent(conn, ip, window_s=window_s) >= max_n


def sweep(conn: psycopg.Connection, *, retention_s: int) -> int:
    """Best-effort DELETE of expired rows, advisory-lock-gated. Caller commits."""
    with conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(%s)", (_SWEEP_LOCK_KEY,))
        row = cur.fetchone()
        assert row is not None
        if not row[0]:
            return 0
        try:
            cur.execute(
                "DELETE FROM oauth_registration_attempts "
                "WHERE ts < now() - make_interval(secs => %s)",
                (retention_s,),
            )
            return cur.rowcount
        finally:
            cur.execute("SELECT pg_advisory_unlock(%s)", (_SWEEP_LOCK_KEY,))
```

- [ ] **Step 4: Implement the middleware**

```python
# src/localmail/serve/oauth/registration_guard.py
"""Top-level HTTP middleware enforcing the per-IP DCR registration cap before a
request reaches the SDK-owned /register route inside the /mcp sub-mount. On each
admitted POST it records the attempt and opportunistically sweeps both the
attempt audit and unused client rows.
"""
from __future__ import annotations

from psycopg_pool import ConnectionPool
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from localmail.config import McpConfig
from localmail.mcp.oauth import clients, registration


class RegistrationRateLimit:
    def __init__(
        self,
        app: ASGIApp,
        *,
        pool: ConnectionPool,
        config: McpConfig,
        register_path_suffix: str = "/register",
    ) -> None:
        self._app = app
        self._pool = pool
        self._cfg = config
        self._suffix = register_path_suffix

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method") != "POST" \
                or not scope.get("path", "").endswith(self._suffix):
            await self._app(scope, receive, send)
            return
        client = scope.get("client")
        ip = client[0] if client else None
        if self._over_limit(ip):
            resp = JSONResponse(
                {"error": "rate_limited",
                 "error_description": "too many registration attempts"},
                status_code=429,
            )
            await resp(scope, receive, send)
            return
        self._record_and_sweep(ip)
        await self._app(scope, receive, send)

    def _over_limit(self, ip: str | None) -> bool:
        with self._pool.connection() as conn:
            over = registration.over_limit(
                conn, ip, window_s=self._cfg.oauth_registration_window_s,
                max_n=self._cfg.oauth_registration_max,
            )
            conn.commit()
        return over

    def _record_and_sweep(self, ip: str | None) -> None:
        with self._pool.connection() as conn:
            registration.record(conn, ip)
            registration.sweep(conn, retention_s=self._cfg.oauth_registration_window_s)
            clients.cleanup_unused(
                conn, retention_s=self._cfg.oauth_client_unused_retention_s
            )
            conn.commit()
```

- [ ] **Step 5: Wire it in `create_app`**

In `src/localmail/serve/app.py`, when `mcp_config.authorization_server_enabled`,
add the middleware to the top-level app:

```python
from localmail.serve.oauth.registration_guard import RegistrationRateLimit
# ...
app.add_middleware(
    RegistrationRateLimit, pool=pool, config=mcp_config,
    register_path_suffix="/register",
)
```

(Place it among the other `app.add_middleware(...)` calls. The `/register` route
lives at `/mcp/register` under the sub-mount, so the `/register` suffix match is
correct; verify the actual mounted path in the Task 12 integration run and
tighten the suffix if needed.)

- [ ] **Step 6: Run tests to verify they pass**

Run: `unset VIRTUAL_ENV && uv run --extra mcp pytest tests/test_oauth_registration_guard.py -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/localmail/mcp/oauth/registration.py src/localmail/serve/oauth/registration_guard.py src/localmail/serve/app.py tests/test_oauth_registration_guard.py
git commit -m "feat(oauth): DCR per-IP rate limit + unused-client cleanup"
```

---

## Task 12: End-to-end integration (full cold-connect dance)

**Files:**
- Modify: `tests/test_mcp_integration.py`

Drive a real `mcp` client through discover → register → authorize → consent POST
→ token → authenticated tool call, against uvicorn in a thread (the existing
integration test already sets this scaffolding up — extend it). The consent POST
is scripted (the test plays the browser): GET `/authorize`, follow the redirect
to `/oauth/consent`, POST credentials, capture the `code` from the redirect, then
exchange at `/token`.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_mcp_integration.py
import re
import httpx
import pytest

# Reuse the module's existing uvicorn-in-thread fixture (server_url) + DB seeding
# helpers. If the existing fixture is named differently, use that name.


@pytest.mark.integration
def test_full_oauth_dance(as_server):
    """discover → register → authorize → consent → token → call a tool."""
    base = as_server.base_url  # e.g. http://127.0.0.1:PORT
    username, password = as_server.username, as_server.password

    # 1. PRM discovery → AS metadata
    prm = httpx.get(f"{base}/.well-known/oauth-protected-resource/mcp").json()
    issuer = prm["authorization_servers"][0]
    meta = httpx.get(f"{issuer.rstrip('/')}/.well-known/oauth-authorization-server").json()

    # 2. Dynamic client registration
    reg = httpx.post(meta["registration_endpoint"], json={
        "redirect_uris": [f"{base}/callback"],
        "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "client_name": "integration-client",
    }).json()
    client_id = reg["client_id"]

    # 3. PKCE + /authorize → consent redirect
    import base64, hashlib, secrets
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).decode().rstrip("=")
    auth = httpx.get(meta["authorization_endpoint"], params={
        "response_type": "code", "client_id": client_id,
        "redirect_uri": f"{base}/callback", "code_challenge": challenge,
        "code_challenge_method": "S256", "state": "xyz",
    }, follow_redirects=False)
    assert auth.status_code in (302, 303)
    consent_url = auth.headers["location"]
    if consent_url.startswith("/"):
        consent_url = base + consent_url
    req_blob = re.search(r"req=([^&]+)", consent_url).group(1)

    # 4. Play the browser: POST consent
    consent = httpx.post(f"{base}/oauth/consent", data={
        "req": httpx.URL(consent_url).params["req"],
        "username": username, "password": password, "decision": "allow",
    }, follow_redirects=False)
    assert consent.status_code == 303
    code = httpx.URL(consent.headers["location"]).params["code"]

    # 5. Exchange code → tokens
    tok = httpx.post(meta["token_endpoint"], data={
        "grant_type": "authorization_code", "code": code,
        "redirect_uri": f"{base}/callback", "client_id": client_id,
        "code_verifier": verifier,
    }).json()
    assert tok["access_token"] and tok["refresh_token"]

    # 6. Authenticated MCP tool call with the access token
    r = httpx.post(f"{base}/mcp", headers={
        "Authorization": f"Bearer {tok['access_token']}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code == 200
    assert "search" in r.text
```

Build the `as_server` fixture by cloning the module's existing integration
fixture but with `authorization_server_enabled=True`,
`state_signing_key` set, and a seeded `api_user` (username/password) granted at
least one account. Reuse the existing uvicorn-thread launcher. **This task is the
arbiter for the spec's "Metadata & endpoint path placement" risk** — if the
real client can't resolve `authorization_endpoint`/`token_endpoint` from the
discovered metadata, fix the wiring (issuer URL / route placement) in
`server.py`/`app.py` until this test passes.

- [ ] **Step 2: Run test to verify it fails**

Run: `unset VIRTUAL_ENV && uv run --extra mcp pytest tests/test_mcp_integration.py -k oauth -q`
Expected: FAIL initially (fixture/wiring incomplete).

- [ ] **Step 3: Make it pass**

Resolve the metadata path placement per the spec's risk section: ensure the PRM
`authorization_servers[0]` issuer is the same issuer the SDK builds AS metadata
for, and that `authorization_endpoint` / `token_endpoint` / `registration_endpoint`
in that metadata resolve to the mounted routes. Iterate on the issuer URL /
route registration until the test is green.

- [ ] **Step 4: Run test to verify it passes**

Run: `unset VIRTUAL_ENV && uv run --extra mcp pytest tests/test_mcp_integration.py -k oauth -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/test_mcp_integration.py src/localmail/mcp/server.py src/localmail/serve/app.py
git commit -m "test(oauth): end-to-end cold-connect OAuth dance integration"
```

---

## Task 13: Documentation

**Files:**
- Modify: `docs/mcp-usage.md`
- Modify: `README.md`
- Modify: `CLAUDE.md`

- [ ] **Step 1: `docs/mcp-usage.md`** — add an "OAuth onboarding (zero-config)"
  section: enabling `authorization_server_enabled` + `state_signing_key`, the
  client flow (discover → register → browser login + consent → tokens), the
  three TTL knobs, and that the daemon/IMAP side is unaffected. Keep the existing
  opaque-bearer (`/v1/auth/login`) section — it remains valid when the AS is off.

- [ ] **Step 2: `README.md`** — in the MCP section, add one paragraph: localmail
  can act as an OAuth 2.1 authorization server for zero-config MCP client
  onboarding (opt-in via `[mcp].authorization_server_enabled`, requires
  `[serve].state_signing_key`); access tokens reuse the existing token store, so
  the per-user ACL is unchanged.

- [ ] **Step 3: `CLAUDE.md`** — under "MCP server (search Phase 3)", add a bullet
  documenting the AS: the new `mcp/oauth/` sub-package + `serve/oauth/` consent
  router, migration `0028_oauth_server.sql`, that access tokens reuse `api_tokens`
  (so `load_access_token` wraps `verify_token`), open DCR + safeguards, the
  consent-state signed blob reusing `[serve].state_signing_key`, and that it is
  opt-in/default-off. Update the migrations line: latest is now
  `0028_oauth_server.sql`; next free slot `0029_*.sql`.

- [ ] **Step 4: Commit**

```bash
git add docs/mcp-usage.md README.md CLAUDE.md
git commit -m "docs(oauth): document the MCP OAuth authorization server"
```

---

## Final verification

- [ ] Full suite + types:

```bash
unset VIRTUAL_ENV && uv run --extra mcp pytest -q tests/ \
  --deselect tests/test_daemon_control_socket.py
unset VIRTUAL_ENV && uv run mypy src/localmail
```

Expected: all green (the integration test is `integration`-marked; run it
explicitly with `-k oauth` if it's deselected by default config), `mypy` clean.

- [ ] Open the PR:

```bash
git push -u origin feat/mcp-oauth-authorization-server
gh pr create --fill
```

---

## Notes for the implementer

- **Fixture names (VERIFIED against this repo):** `conftest.py` provides
  `db_conn` (a committed-then-truncated `psycopg.Connection`) and `db_dsn` (a
  session-scoped DSN string). **There is NO `db_pool`/`pool` fixture.** Wherever a
  task's test text says `db_pool`, build a pool inline from `db_dsn` and close it,
  exactly like `tests/test_mcp_auth.py`:

  ```python
  import pytest
  from psycopg_pool import ConnectionPool

  @pytest.fixture
  def db_pool(db_dsn):
      pool = ConnectionPool(db_dsn, min_size=1, max_size=2, open=True)
      try:
          yield pool
      finally:
          pool.close()
  ```

  Define this local fixture in each test module that needs it (or add it to
  `conftest.py` once in Task 5 and reuse). Use `db_conn` for the store unit tests
  (Tasks 5-8, 11b) and the inline `db_pool` for provider/router/app tests
  (Tasks 9-12).
- **`anyio.run` in provider tests:** the provider methods are `async`; the unit
  tests drive them with `anyio.run(...)`. The `mcp` SDK already pulls in `anyio`.
- **No PKCE in the provider:** never hash `code_verifier` here — the SDK does it.
  The provider only persists/returns `code_challenge`.
- **Commit discipline:** each store function documents "Caller commits"; the
  provider's sync helpers own the `conn.commit()`. Keep that split — it mirrors
  `api/auth.py`.
- **CSP:** the consent page ships no inline JS, matching the admin panel's
  `script-src 'self'`. If you add any JS, make it a served static file.
