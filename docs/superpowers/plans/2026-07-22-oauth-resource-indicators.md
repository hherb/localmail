# RFC 8707 Resource Indicators Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Validate the OAuth `resource` parameter at `/authorize`, bind the issued token to that audience through the whole grant chain, and enforce audience membership at `/mcp`.

**Architecture:** A new pure module canonicalizes + decides the resource; the decision is packed into the consent blob, carried onto the authorization code, and bound onto the minted access + refresh tokens (three new nullable DB columns). `load_access` rejects a token whose non-NULL bound resource is not in the accepted set. NULL resource = unrestricted (login/legacy tokens). Enforcement is `/mcp`-only.

**Tech Stack:** Python 3.12, psycopg v3 (raw SQL), pydantic v2 (`McpConfig`), the `mcp` SDK OAuth provider, pytest against `localmail_test`.

## Global Constraints

- Python ≥ 3.12, managed by `uv`. Run tests with `unset VIRTUAL_ENV && uv run --extra mcp pytest …` (the `--extra mcp` is mandatory — the OAuth/MCP code imports the `mcp` SDK).
- No ORM: raw SQL in numbered `migrations/*.sql`, tracked in `schema_migrations`. Next free slot is `0031_*.sql`. **Never edit an applied migration.**
- No magic numbers in code — all tunables live on `McpConfig`.
- Every new `.py` file starts with the two-line SPDX header:
  `# SPDX-License-Identifier: AGPL-3.0-or-later` / `# Copyright (C) 2026 Horst Herb`.
- mypy is enabled: no `cur.fetchone()[0]` without an `assert row is not None` first.
- No comments unless the WHY is non-obvious. Keep every file < 500 lines.
- DB tests use the `db_conn` fixture (TRUNCATEs between tests); commit within a test when a later read needs to see the write.
- **Accepted SDK limitations (do not try to fix):** the SDK's error enums lack `invalid_target` (use `AuthorizeError("invalid_request", …)`); the SDK swallows the token-endpoint `resource` (validate/bind at authorize time only).

Spec: [docs/superpowers/specs/2026-07-22-oauth-resource-indicators-design.md](../specs/2026-07-22-oauth-resource-indicators-design.md).

## File Structure

- **Create** `src/localmail/mcp/oauth/resource_indicator.py` — pure canonicalization + decision (no IO, no SDK).
- **Create** `migrations/0031_oauth_resource_indicator.sql` — three nullable columns.
- **Modify** `src/localmail/config.py` — two `McpConfig` fields.
- **Modify** `src/localmail/mcp/oauth/codes.py` — carry `resource` on mint/load.
- **Modify** `src/localmail/mcp/oauth/access.py` — bind `resource` on mint; enforce on load.
- **Modify** `src/localmail/mcp/oauth/refresh.py` — carry `resource` on mint/load/rotate.
- **Modify** `src/localmail/mcp/oauth/consent_state.py` — `ConsentPayload.resource`.
- **Modify** `src/localmail/mcp/oauth/provider.py` — accepted-set at construction; validate/bind in `authorize`; map onto code; bind onto exchanged tokens; pass accepted set into `load_access`.
- **Modify** `src/localmail/serve/oauth/consent_router.py` — pass `payload.resource` into `mint_code`.
- **Modify** `CLAUDE.md`, `README.md` — document the shipped feature; remove the "not carried through" limitation note.

---

### Task 1: Pure `resource_indicator` module

**Files:**
- Create: `src/localmail/mcp/oauth/resource_indicator.py`
- Test: `tests/test_oauth_resource_indicator.py`

**Interfaces:**
- Consumes: nothing (pure stdlib).
- Produces:
  - `canonicalize_resource(raw: str) -> str | None`
  - `resolve_accepted_resources(configured: list[str] | None, derived: str) -> list[str]` (raises `ValueError` if a non-None `configured` canonicalizes to empty)
  - `@dataclass(frozen=True) ResourceDecision(ok: bool, bound: str | None, error: str | None)`
  - `decide_resource(requested: str | None, accepted: list[str], *, require: bool) -> ResourceDecision`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_oauth_resource_indicator.py
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

import pytest

from localmail.mcp.oauth.resource_indicator import (
    ResourceDecision,
    canonicalize_resource,
    decide_resource,
    resolve_accepted_resources,
)

CANON = "https://mail.example.com/mcp"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("https://mail.example.com/mcp", CANON),
        ("https://mail.example.com/mcp/", CANON),          # trailing slash stripped
        ("https://MAIL.Example.COM/mcp", CANON),           # host lowercased
        ("HTTPS://mail.example.com/mcp", CANON),           # scheme lowercased
        ("https://mail.example.com:443/mcp", CANON),       # default port dropped
        ("http://h/mcp", "http://h/mcp"),
        ("http://h:80/mcp", "http://h/mcp"),               # default http port dropped
        ("https://h:8443/mcp", "https://h:8443/mcp"),      # non-default port kept
        ("https://h/", "https://h"),                       # bare root slash stripped
        ("https://mail.example.com/mcp#frag", None),       # fragment rejected
        ("ftp://h/mcp", None),                             # non-http scheme
        ("/mcp", None),                                     # relative
        ("not a url", None),
        ("", None),
    ],
)
def test_canonicalize(raw, expected):
    assert canonicalize_resource(raw) == expected


def test_resolve_defaults_to_derived_when_configured_none():
    assert resolve_accepted_resources(None, "https://h/mcp/") == ["https://h/mcp"]


def test_resolve_uses_configured_and_canonicalizes():
    assert resolve_accepted_resources(
        ["https://A.com/mcp/", "https://b.com:443/mcp"], "https://h/mcp"
    ) == ["https://a.com/mcp", "https://b.com/mcp"]


def test_resolve_drops_malformed_but_keeps_valid():
    assert resolve_accepted_resources(
        ["https://a.com/mcp", "bogus"], "https://h/mcp"
    ) == ["https://a.com/mcp"]


def test_resolve_all_malformed_configured_raises():
    with pytest.raises(ValueError):
        resolve_accepted_resources(["bogus", "also bad"], "https://h/mcp")


def test_decide_absent_not_required_binds_first_accepted():
    d = decide_resource(None, [CANON], require=False)
    assert d == ResourceDecision(ok=True, bound=CANON, error=None)


def test_decide_absent_required_errors():
    d = decide_resource(None, [CANON], require=True)
    assert d.ok is False and d.bound is None and d.error


def test_decide_match_binds_canonical():
    d = decide_resource("https://mail.example.com/mcp/", [CANON], require=False)
    assert d.ok is True and d.bound == CANON


def test_decide_mismatch_errors():
    d = decide_resource("https://evil.com/mcp", [CANON], require=False)
    assert d.ok is False and d.bound is None and d.error


def test_decide_malformed_errors():
    d = decide_resource("https://h/mcp#x", [CANON], require=False)
    assert d.ok is False and d.error
```

- [ ] **Step 2: Run test to verify it fails**

Run: `unset VIRTUAL_ENV && uv run --extra mcp pytest tests/test_oauth_resource_indicator.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'localmail.mcp.oauth.resource_indicator'`.

- [ ] **Step 3: Write minimal implementation**

```python
# src/localmail/mcp/oauth/resource_indicator.py
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Pure RFC 8707 resource-indicator canonicalization + accept/reject decision.

No IO, no SDK import. `canonicalize_resource` implements the RFC 8707 §2 rules
(absolute http(s) URI, no fragment, lowercase scheme/host, drop default port,
strip a trailing slash). `decide_resource` is the accept/bind/reject table the
provider applies at /authorize.
"""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

_DEFAULT_PORTS = {"http": 80, "https": 443}


def canonicalize_resource(raw: str) -> str | None:
    """Return the canonical resource identifier, or None if `raw` is invalid."""
    try:
        parts = urlsplit(raw.strip())
    except ValueError:
        return None
    if parts.scheme not in _DEFAULT_PORTS or not parts.hostname or parts.fragment:
        return None
    host = parts.hostname  # already lowercased by urlsplit
    port = parts.port
    netloc = host
    if port is not None and port != _DEFAULT_PORTS[parts.scheme]:
        netloc = f"{host}:{port}"
    path = parts.path[:-1] if parts.path.endswith("/") else parts.path
    return urlunsplit((parts.scheme, netloc, path, parts.query, ""))


def resolve_accepted_resources(
    configured: list[str] | None, derived: str
) -> list[str]:
    """The accepted resource set: `configured or [derived]`, each canonicalized.

    Malformed entries are dropped. A non-None `configured` that canonicalizes to
    an empty list is a hard operator misconfiguration -> raise ValueError (the
    caller resolves this once at construction, so it surfaces at startup).
    """
    if configured:
        out = [c for c in (canonicalize_resource(x) for x in configured) if c]
        if not out:
            raise ValueError("resource_indicators has no valid entries")
        return out
    canon = canonicalize_resource(derived)
    assert canon is not None  # derived comes from mcp_resource_url — always valid
    return [canon]


@dataclass(frozen=True)
class ResourceDecision:
    ok: bool
    bound: str | None
    error: str | None


def decide_resource(
    requested: str | None, accepted: list[str], *, require: bool
) -> ResourceDecision:
    """Accept/bind/reject a requested resource against the accepted set."""
    if requested is None:
        if require:
            return ResourceDecision(False, None, "resource indicator is required")
        return ResourceDecision(True, accepted[0], None)
    canon = canonicalize_resource(requested)
    if canon is not None and canon in accepted:
        return ResourceDecision(True, canon, None)
    return ResourceDecision(False, None, "invalid or unknown resource indicator")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `unset VIRTUAL_ENV && uv run --extra mcp pytest tests/test_oauth_resource_indicator.py -q`
Expected: PASS (all parametrized cases + decision cases).

- [ ] **Step 5: Commit**

```bash
git add src/localmail/mcp/oauth/resource_indicator.py tests/test_oauth_resource_indicator.py
git commit -m "feat(mcp): pure RFC 8707 resource-indicator canonicalize + decide"
```

---

### Task 2: Migration `0031` — three nullable columns

**Files:**
- Create: `migrations/0031_oauth_resource_indicator.sql`
- Test: `tests/test_oauth_migration.py` (append a test)

**Interfaces:**
- Produces: columns `oauth_authorization_codes.resource TEXT`, `oauth_refresh_tokens.resource TEXT`, `api_tokens.oauth_resource TEXT` (all nullable).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_oauth_migration.py  (append)
def test_migration_0031_adds_resource_columns(db_conn):
    checks = [
        ("oauth_authorization_codes", "resource"),
        ("oauth_refresh_tokens", "resource"),
        ("api_tokens", "oauth_resource"),
    ]
    with db_conn.cursor() as cur:
        for table, col in checks:
            cur.execute(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name = %s AND column_name = %s",
                (table, col),
            )
            assert cur.fetchone() is not None, f"{table}.{col} missing"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `unset VIRTUAL_ENV && uv run --extra mcp pytest tests/test_oauth_migration.py::test_migration_0031_adds_resource_columns -q`
Expected: FAIL — `api_tokens.oauth_resource missing` (migration not yet applied to `localmail_test`).

Note: if the DB was created before this migration existed, run `unset VIRTUAL_ENV && LOCALMAIL_TEST_DSN=... uv run localmail init-db` or let the `db_conn` fixture apply pending migrations (it runs the migration runner on setup).

- [ ] **Step 3: Write the migration**

```sql
-- migrations/0031_oauth_resource_indicator.sql
-- RFC 8707 resource indicators. The audience a token is bound to is carried
-- from /authorize onto the authorization code, then onto the minted access +
-- refresh tokens, and enforced at /mcp (load_access). NULL = unrestricted, so
-- /v1/auth/login tokens and pre-migration rows are structurally immune.

ALTER TABLE oauth_authorization_codes ADD COLUMN resource TEXT;
ALTER TABLE oauth_refresh_tokens      ADD COLUMN resource TEXT;
ALTER TABLE api_tokens                ADD COLUMN oauth_resource TEXT;
```

- [ ] **Step 4: Run test to verify it passes**

Run: `unset VIRTUAL_ENV && uv run --extra mcp pytest tests/test_oauth_migration.py::test_migration_0031_adds_resource_columns -q`
Expected: PASS (the `db_conn` fixture applies the new migration).

- [ ] **Step 5: Commit**

```bash
git add migrations/0031_oauth_resource_indicator.sql tests/test_oauth_migration.py
git commit -m "feat(mcp): migration 0031 — resource columns on codes/refresh/api_tokens"
```

---

### Task 3: `McpConfig` fields

**Files:**
- Modify: `src/localmail/config.py:558` (after `authorization_servers`) and `:563` region (`oauth_*` block)
- Test: `tests/test_mcp_config.py` (append)

**Interfaces:**
- Produces: `McpConfig.resource_indicators: list[AnyHttpUrl] | None = None`, `McpConfig.oauth_require_resource_indicator: bool = False`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_mcp_config.py  (append)
from localmail.config import McpConfig


def test_resource_indicator_defaults():
    cfg = McpConfig()
    assert cfg.resource_indicators is None
    assert cfg.oauth_require_resource_indicator is False


def test_resource_indicators_parse_list():
    cfg = McpConfig(resource_indicators=["https://a.com/mcp"])
    assert [str(u) for u in cfg.resource_indicators] == ["https://a.com/mcp"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `unset VIRTUAL_ENV && uv run --extra mcp pytest tests/test_mcp_config.py -q -k resource_indicator`
Expected: FAIL — `AttributeError`/`ValidationError` (fields don't exist).

- [ ] **Step 3: Add the fields**

In `src/localmail/config.py`, in `class McpConfig`, immediately after the
`authorization_servers: list[AnyHttpUrl] | None = None` line:

```python
    resource_indicators: list[AnyHttpUrl] | None = None
```

and in the `oauth_*` block (after `authorization_server_enabled: bool = False`):

```python
    oauth_require_resource_indicator: bool = False
```

Extend the class docstring with one sentence:

```
    `resource_indicators` is the RFC 8707 accepted-resource set; `None` falls
    back to `[mcp_resource_url(resource_server_url)]`. `oauth_require_resource_indicator`
    rejects an /authorize request that omits `resource`.
```

- [ ] **Step 4: Run test to verify it passes**

Run: `unset VIRTUAL_ENV && uv run --extra mcp pytest tests/test_mcp_config.py -q -k resource_indicator`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/localmail/config.py tests/test_mcp_config.py
git commit -m "feat(mcp): McpConfig resource_indicators + oauth_require_resource_indicator"
```

---

### Task 4: Carry `resource` through the authorization-code store

**Files:**
- Modify: `src/localmail/mcp/oauth/codes.py` (`CodeRow`, `mint_code`, `load_code`)
- Test: `tests/test_oauth_codes_store.py` (append)

**Interfaces:**
- Consumes: migration 0031 (`oauth_authorization_codes.resource`).
- Produces: `mint_code(..., resource: str | None = None)`; `CodeRow.resource: str | None`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_oauth_codes_store.py  (append — reuse the file's existing seed helper)
def test_mint_and_load_code_round_trips_resource(db_conn):
    _seed_client(db_conn)  # existing helper in this file; registers "cid" + a user
    raw = codes.mint_code(
        db_conn, client_id="cid", user_id=_seed_user(db_conn),
        redirect_uri="https://c/cb", redirect_uri_provided_explicitly=True,
        code_challenge="chal", scopes=["s"], ttl_s=60,
        resource="https://h/mcp",
    )
    db_conn.commit()
    row = codes.load_code(db_conn, raw)
    assert row is not None and row.resource == "https://h/mcp"


def test_mint_code_defaults_resource_none(db_conn):
    _seed_client(db_conn)
    raw = codes.mint_code(
        db_conn, client_id="cid", user_id=_seed_user(db_conn),
        redirect_uri="https://c/cb", redirect_uri_provided_explicitly=True,
        code_challenge="chal", scopes=["s"], ttl_s=60,
    )
    db_conn.commit()
    row = codes.load_code(db_conn, raw)
    assert row is not None and row.resource is None
```

> If `tests/test_oauth_codes_store.py` has no `_seed_client`/`_seed_user` helpers, mirror the seed used by its existing tests (register a client via `clients.register_client` + create a user via `api_auth.create_user`) inline.

- [ ] **Step 2: Run test to verify it fails**

Run: `unset VIRTUAL_ENV && uv run --extra mcp pytest tests/test_oauth_codes_store.py -q -k resource`
Expected: FAIL — `TypeError: mint_code() got an unexpected keyword argument 'resource'`.

- [ ] **Step 3: Implement**

In `src/localmail/mcp/oauth/codes.py`:

Add to `CodeRow`:
```python
    resource: str | None
```
(place it last so positional construction in `load_code` stays clear).

`mint_code` — add the parameter and the column:
```python
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
    resource: str | None = None,
) -> str:
    """Mint + persist a single-use code; return the raw code. Caller commits."""
    raw = generate_token()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO oauth_authorization_codes (code_sha256, client_id, "
            "user_id, redirect_uri, redirect_uri_provided_explicitly, "
            "code_challenge, scopes, expires_at, resource) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, "
            "now() + make_interval(secs => %s), %s)",
            (hash_token(raw), client_id, user_id, redirect_uri,
             redirect_uri_provided_explicitly, code_challenge, scopes, ttl_s,
             resource),
        )
    return raw
```

`load_code` — select the column and map it:
```python
        cur.execute(
            "SELECT client_id, user_id, redirect_uri, "
            "redirect_uri_provided_explicitly, code_challenge, scopes, "
            "expires_at, resource "
            "FROM oauth_authorization_codes "
            "WHERE code_sha256 = %s AND expires_at > now()",
            (hash_token(raw_code),),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return CodeRow(
        client_id=row[0], user_id=row[1], redirect_uri=row[2],
        redirect_uri_provided_explicitly=row[3], code_challenge=row[4],
        scopes=row[5], expires_at=row[6], resource=row[7],
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `unset VIRTUAL_ENV && uv run --extra mcp pytest tests/test_oauth_codes_store.py -q`
Expected: PASS (new + existing tests — existing callers pass no `resource`, defaulting to None).

- [ ] **Step 5: Commit**

```bash
git add src/localmail/mcp/oauth/codes.py tests/test_oauth_codes_store.py
git commit -m "feat(mcp): carry resource on the authorization-code store"
```

---

### Task 5: Bind `resource` on access mint; enforce on load

**Files:**
- Modify: `src/localmail/mcp/oauth/access.py` (`mint_access`, `load_access`)
- Test: `tests/test_oauth_access_bridge.py` (append)

**Interfaces:**
- Consumes: `resource_indicator.canonicalize_resource` (Task 1); migration 0031.
- Produces: `mint_access(..., resource: str | None = None)`; `load_access(conn, raw_token, *, accepted_resources: list[str] | None = None)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_oauth_access_bridge.py  (append; reuses the module's _seed helper)
def test_mint_access_binds_resource(db_conn):
    uid = _seed(db_conn)
    raw = access.mint_access(
        db_conn, user_id=uid, client_id="cid", ttl_s=3600,
        resource="https://h/mcp",
    )
    db_conn.commit()
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT oauth_resource FROM api_tokens WHERE token_sha256 = %s",
            (api_auth.hash_token(raw),),
        )
        row = cur.fetchone()
        assert row is not None and row[0] == "https://h/mcp"


def test_load_access_accepts_matching_resource(db_conn):
    uid = _seed(db_conn)
    raw = access.mint_access(
        db_conn, user_id=uid, client_id="cid", ttl_s=3600,
        resource="https://h/mcp",
    )
    db_conn.commit()
    at = access.load_access(db_conn, raw, accepted_resources=["https://h/mcp"])
    assert at is not None and at.subject == str(uid)


def test_load_access_rejects_unlisted_resource(db_conn):
    uid = _seed(db_conn)
    raw = access.mint_access(
        db_conn, user_id=uid, client_id="cid", ttl_s=3600,
        resource="https://other/mcp",
    )
    db_conn.commit()
    assert access.load_access(
        db_conn, raw, accepted_resources=["https://h/mcp"]
    ) is None


def test_load_access_null_resource_unrestricted(db_conn):
    uid = _seed(db_conn)
    raw = access.mint_access(db_conn, user_id=uid, client_id="cid", ttl_s=3600)
    db_conn.commit()
    assert access.load_access(
        db_conn, raw, accepted_resources=["https://h/mcp"]
    ) is not None


def test_load_access_no_accepted_set_skips_enforcement(db_conn):
    uid = _seed(db_conn)
    raw = access.mint_access(
        db_conn, user_id=uid, client_id="cid", ttl_s=3600,
        resource="https://other/mcp",
    )
    db_conn.commit()
    assert access.load_access(db_conn, raw) is not None  # accepted_resources=None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `unset VIRTUAL_ENV && uv run --extra mcp pytest tests/test_oauth_access_bridge.py -q -k resource`
Expected: FAIL — `TypeError: mint_access() got an unexpected keyword argument 'resource'`.

- [ ] **Step 3: Implement**

In `src/localmail/mcp/oauth/access.py`:

Add the import near the top:
```python
from localmail.mcp.oauth.resource_indicator import canonicalize_resource
```

`mint_access` — add the parameter and the column:
```python
def mint_access(
    conn: psycopg.Connection,
    *,
    user_id: int,
    client_id: str,
    ttl_s: int,
    family_id: uuid.UUID | None = None,
    resource: str | None = None,
) -> str:
    """Mint an access token into api_tokens; return the raw token. Caller commits.

    ``family_id`` ties the token to a refresh family so reuse detection can purge
    it. ``resource`` binds the RFC 8707 audience (``None`` = unrestricted).
    """
    raw = generate_token()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO api_tokens "
            "(token_sha256, user_id, expires_at, oauth_client_id, "
            " oauth_refresh_family_id, oauth_resource) "
            "VALUES (%s, %s, now() + make_interval(secs => %s), %s, %s, %s)",
            (hash_token(raw), user_id, ttl_s, client_id, family_id, resource),
        )
    return raw
```

`load_access` — add the accepted-set parameter, select `oauth_resource`, enforce:
```python
def load_access(
    conn: psycopg.Connection,
    raw_token: str,
    *,
    accepted_resources: list[str] | None = None,
) -> "AccessToken | None":
    """Verify an access token and return the SDK AccessToken, or None.

    When ``accepted_resources`` is given, a token bound to a resource
    (``oauth_resource IS NOT NULL``) is rejected unless its canonical resource is
    in the set (RFC 8707 audience enforcement at /mcp). A NULL resource is always
    unrestricted; ``accepted_resources=None`` skips enforcement entirely.
    """
    from mcp.server.auth.provider import AccessToken

    user = verify_token(conn, raw_token)
    if user is None:
        return None
    with conn.cursor() as cur:
        cur.execute(
            "SELECT oauth_client_id, oauth_resource FROM api_tokens "
            "WHERE token_sha256 = %s",
            (hash_token(raw_token),),
        )
        row = cur.fetchone()
    bound_resource = row[1] if row else None
    if accepted_resources is not None and bound_resource is not None:
        if canonicalize_resource(bound_resource) not in accepted_resources:
            return None
    client_id = row[0] if row and row[0] is not None else _NO_OAUTH_CLIENT_ID
    return AccessToken(
        token=raw_token, client_id=client_id, scopes=[], subject=str(user.id)
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `unset VIRTUAL_ENV && uv run --extra mcp pytest tests/test_oauth_access_bridge.py -q`
Expected: PASS (new + existing; existing `load_access(db_conn, raw)` calls pass `accepted_resources=None` → enforcement skipped).

- [ ] **Step 5: Commit**

```bash
git add src/localmail/mcp/oauth/access.py tests/test_oauth_access_bridge.py
git commit -m "feat(mcp): bind resource on access mint + enforce audience on load"
```

---

### Task 6: Carry `resource` through refresh mint / load / rotate

**Files:**
- Modify: `src/localmail/mcp/oauth/refresh.py` (`RefreshRow`, `mint_refresh`, `load_refresh`, `rotate_refresh`)
- Test: `tests/test_oauth_refresh_store.py` (append)

**Interfaces:**
- Consumes: migration 0031 (`oauth_refresh_tokens.resource`).
- Produces: `mint_refresh(..., resource: str | None = None)`; `RefreshRow.resource: str | None`; `rotate_refresh` copies `resource` from the consumed row to the successor.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_oauth_refresh_store.py  (append; reuse the file's existing seed helper)
def test_mint_and_load_refresh_round_trips_resource(db_conn):
    _seed(db_conn)  # existing helper: registers client + user
    raw = refresh.mint_refresh(
        db_conn, client_id="cid", user_id=_uid(db_conn), scopes=["s"],
        ttl_s=3600, resource="https://h/mcp",
    )
    db_conn.commit()
    row = refresh.load_refresh(db_conn, raw)
    assert row is not None and row.resource == "https://h/mcp"


def test_rotate_carries_resource_to_successor(db_conn):
    _seed(db_conn)
    raw = refresh.mint_refresh(
        db_conn, client_id="cid", user_id=_uid(db_conn), scopes=["s"],
        ttl_s=3600, resource="https://h/mcp",
    )
    db_conn.commit()
    result = refresh.rotate_refresh(db_conn, raw, ttl_s=3600)
    db_conn.commit()
    assert result.outcome == "rotated" and result.new_token is not None
    succ = refresh.load_refresh(db_conn, result.new_token)
    assert succ is not None and succ.resource == "https://h/mcp"
```

> Adapt `_seed` / `_uid` to whatever the existing tests in this file use to register the `cid` client and create a user.

- [ ] **Step 2: Run test to verify it fails**

Run: `unset VIRTUAL_ENV && uv run --extra mcp pytest tests/test_oauth_refresh_store.py -q -k resource`
Expected: FAIL — `TypeError: mint_refresh() got an unexpected keyword argument 'resource'`.

- [ ] **Step 3: Implement**

In `src/localmail/mcp/oauth/refresh.py`:

Add to `RefreshRow`:
```python
    resource: str | None
```
(place it last).

`mint_refresh` — add the parameter and include the column in both branches:
```python
def mint_refresh(
    conn: psycopg.Connection,
    *,
    client_id: str,
    user_id: int,
    scopes: list[str],
    ttl_s: int,
    family_id: _uuid.UUID | None = None,
    resource: str | None = None,
) -> str:
    """Mint + persist a refresh token; return the raw token. Caller commits."""
    raw = generate_token()
    with conn.cursor() as cur:
        if family_id is None:
            cur.execute(
                "INSERT INTO oauth_refresh_tokens (token_sha256, client_id, "
                "user_id, scopes, expires_at, resource) "
                "VALUES (%s, %s, %s, %s, now() + make_interval(secs => %s), %s)",
                (hash_token(raw), client_id, user_id, scopes, ttl_s, resource),
            )
        else:
            cur.execute(
                "INSERT INTO oauth_refresh_tokens (token_sha256, client_id, "
                "user_id, scopes, expires_at, family_id, resource) "
                "VALUES (%s, %s, %s, %s, now() + make_interval(secs => %s), "
                "%s, %s)",
                (hash_token(raw), client_id, user_id, scopes, ttl_s, family_id,
                 resource),
            )
    return raw
```

`load_refresh` — select the column and map it:
```python
        cur.execute(
            "SELECT r.client_id, r.user_id, r.scopes, r.expires_at, "
            "r.family_id, r.resource "
            "FROM oauth_refresh_tokens r "
            "JOIN api_users u ON u.id = r.user_id "
            "WHERE r.token_sha256 = %s AND r.expires_at > now() "
            "  AND r.consumed_at IS NULL AND u.disabled_at IS NULL",
            (hash_token(raw_token),),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return RefreshRow(
        client_id=row[0], user_id=row[1], scopes=row[2],
        expires_at=row[3], family_id=row[4], resource=row[5],
    )
```

`rotate_refresh` — pass the loaded row's resource to the successor mint:
```python
    new = mint_refresh(
        conn, client_id=row.client_id, user_id=row.user_id,
        scopes=row.scopes, ttl_s=ttl_s, family_id=row.family_id,
        resource=row.resource,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `unset VIRTUAL_ENV && uv run --extra mcp pytest tests/test_oauth_refresh_store.py -q`
Expected: PASS (new + existing reuse/rotation tests still green — resource defaults to None where not supplied).

- [ ] **Step 5: Commit**

```bash
git add src/localmail/mcp/oauth/refresh.py tests/test_oauth_refresh_store.py
git commit -m "feat(mcp): carry resource through refresh mint/load/rotate"
```

---

### Task 7: `ConsentPayload.resource` round-trip

**Files:**
- Modify: `src/localmail/mcp/oauth/consent_state.py` (`ConsentPayload`)
- Test: `tests/test_oauth_consent_state.py` (append)

**Interfaces:**
- Produces: `ConsentPayload.resource: str | None` surviving `encode_consent_state` → `decode_consent_state`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_oauth_consent_state.py  (append)
def test_consent_payload_round_trips_resource():
    key = b"k" * 32
    payload = ConsentPayload(
        client_id="cid", redirect_uri="https://c/cb",
        redirect_uri_provided_explicitly=True, code_challenge="chal",
        scopes=["s"], state=None, exp=_future_exp(), resource="https://h/mcp",
    )
    blob = encode_consent_state(payload, key=key)
    back = decode_consent_state(blob, key=key)
    assert back.resource == "https://h/mcp"
```

> Use the file's existing helper for a valid future `exp` (or inline `int(time.time()) + 300`). Import `ConsentPayload`, `encode_consent_state`, `decode_consent_state` if not already imported.

- [ ] **Step 2: Run test to verify it fails**

Run: `unset VIRTUAL_ENV && uv run --extra mcp pytest tests/test_oauth_consent_state.py -q -k resource`
Expected: FAIL — `TypeError: ConsentPayload.__init__() got an unexpected keyword argument 'resource'`.

- [ ] **Step 3: Implement**

In `src/localmail/mcp/oauth/consent_state.py`, add the field to `ConsentPayload`
(as the last field, keeping a default so older encoders/tests without it still
construct — but note all producers will set it after Task 8):

```python
@dataclass(frozen=True)
class ConsentPayload:
    client_id: str
    redirect_uri: str
    redirect_uri_provided_explicitly: bool
    code_challenge: str
    scopes: list[str]
    state: str | None
    exp: int
    resource: str | None = None
```

`encode_consent_state` / `decode_consent_state` need no change — they already
operate generically over `asdict(payload)` and `ConsentPayload(**body)`.

> Because `decode_consent_state` does `ConsentPayload(**body)`, the `resource`
> default keeps *old* blobs (encoded before this field existed, e.g. in-flight
> during a rolling deploy) decodable — they simply get `resource=None`.

- [ ] **Step 4: Run test to verify it passes**

Run: `unset VIRTUAL_ENV && uv run --extra mcp pytest tests/test_oauth_consent_state.py -q`
Expected: PASS (new + existing).

- [ ] **Step 5: Commit**

```bash
git add src/localmail/mcp/oauth/consent_state.py tests/test_oauth_consent_state.py
git commit -m "feat(mcp): carry resource on the consent-state blob"
```

---

### Task 8: Provider — accepted set at construction + validate/bind in `authorize`

**Files:**
- Modify: `src/localmail/mcp/oauth/provider.py` (`__init__`, `authorize`)
- Test: `tests/test_oauth_provider.py` (append)

**Interfaces:**
- Consumes: `resource_indicator.{resolve_accepted_resources,decide_resource}` (Task 1); `discovery.mcp_resource_url`; `ConsentPayload.resource` (Task 7); `McpConfig.{resource_indicators,resource_server_url,oauth_require_resource_indicator}` (Task 3).
- Produces: `self._accepted: list[str]` on the provider; `authorize` raises `AuthorizeError("invalid_request", …)` on a bad resource and packs `ConsentPayload.resource = decision.bound` otherwise.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_oauth_provider.py  (append)
import pytest
from mcp.server.auth.provider import AuthorizationParams, AuthorizeError
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl

from localmail.config import McpConfig
from localmail.mcp.oauth.consent_state import decode_consent_state
from localmail.mcp.oauth.provider import LocalmailASProvider

_KEY = b"k" * 32


def _provider(pool, **cfg_over):
    cfg = McpConfig(resource_server_url="https://h", **cfg_over)
    return LocalmailASProvider(
        pool, config=cfg, signing_key=_KEY, consent_path="/oauth/consent"
    )


def _client():
    return OAuthClientInformationFull(
        client_id="cid", redirect_uris=[AnyUrl("https://c/cb")],
        grant_types=["authorization_code"], response_types=["code"],
        token_endpoint_auth_method="none",
    )


def _params(resource):
    return AuthorizationParams(
        state=None, scopes=[], code_challenge="chal",
        redirect_uri=AnyUrl("https://c/cb"),
        redirect_uri_provided_explicitly=True, resource=resource,
    )


async def _authorize(provider, resource):
    return await provider.authorize(_client(), _params(resource))


@pytest.mark.anyio
async def test_authorize_binds_matching_resource(db_pool):
    provider = _provider(db_pool)
    url = await _authorize(provider, "https://h/mcp")
    blob = url.split("req=", 1)[1]
    payload = decode_consent_state(blob, key=_KEY)
    assert payload.resource == "https://h/mcp"


@pytest.mark.anyio
async def test_authorize_binds_canonical_when_absent(db_pool):
    provider = _provider(db_pool)
    url = await _authorize(provider, None)
    payload = decode_consent_state(url.split("req=", 1)[1], key=_KEY)
    assert payload.resource == "https://h/mcp"


@pytest.mark.anyio
async def test_authorize_rejects_unlisted_resource(db_pool):
    provider = _provider(db_pool)
    with pytest.raises(AuthorizeError):
        await _authorize(provider, "https://evil/mcp")


@pytest.mark.anyio
async def test_authorize_rejects_absent_when_required(db_pool):
    provider = _provider(db_pool, oauth_require_resource_indicator=True)
    with pytest.raises(AuthorizeError):
        await _authorize(provider, None)
```

> Check `tests/test_oauth_provider.py` for the existing pool fixture name and the `anyio` marker convention (the SDK provider methods are async). If the file uses a different fixture than `db_pool` or a different async-test style (e.g. `asyncio.run(...)` in a sync test), match it. `AuthorizationParams` field names must match the installed SDK — confirm with the `handlers/authorize.py` you already read (`redirect_uri_provided_explicitly`, `code_challenge`, `resource`).

- [ ] **Step 2: Run test to verify it fails**

Run: `unset VIRTUAL_ENV && uv run --extra mcp pytest tests/test_oauth_provider.py -q -k resource`
Expected: FAIL — `payload.resource` is missing / no `AuthorizeError` raised (authorize ignores `params.resource`).

- [ ] **Step 3: Implement**

In `src/localmail/mcp/oauth/provider.py`, add imports:
```python
from mcp.server.auth.provider import AuthorizeError
from localmail.mcp.discovery import mcp_resource_url
from localmail.mcp.oauth.resource_indicator import (
    decide_resource, resolve_accepted_resources,
)
```
(`AuthorizeError` joins the existing `from mcp.server.auth.provider import (...)` block.)

In `__init__`, after storing `self._consent_path`, resolve the accepted set once
(so a malformed `resource_indicators` fails loud at construction / app startup):
```python
        self._accepted = resolve_accepted_resources(
            [str(u) for u in config.resource_indicators]
            if config.resource_indicators else None,
            mcp_resource_url(str(config.resource_server_url)),
        )
```

In `authorize`, before building the payload:
```python
        decision = decide_resource(
            params.resource, self._accepted,
            require=self._cfg.oauth_require_resource_indicator,
        )
        if not decision.ok:
            raise AuthorizeError("invalid_request", decision.error)
        payload = ConsentPayload(
            client_id=client.client_id,
            redirect_uri=str(params.redirect_uri),
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            code_challenge=params.code_challenge,
            scopes=list(params.scopes or []),
            state=params.state,
            exp=int(time.time()) + self._cfg.oauth_consent_state_ttl_s,
            resource=decision.bound,
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `unset VIRTUAL_ENV && uv run --extra mcp pytest tests/test_oauth_provider.py -q`
Expected: PASS (new + existing provider tests).

- [ ] **Step 5: Commit**

```bash
git add src/localmail/mcp/oauth/provider.py tests/test_oauth_provider.py
git commit -m "feat(mcp): validate + bind resource at /authorize"
```

---

### Task 9: Provider — bind resource onto exchanged tokens + enforce at load

**Files:**
- Modify: `src/localmail/mcp/oauth/provider.py` (`_load_code_sync`, `_exchange_code_sync`, `_exchange_refresh_sync`, `_load_access_sync`)
- Test: `tests/test_oauth_provider.py` (append)

**Interfaces:**
- Consumes: `codes.CodeRow.resource` (Task 4); `access.mint_access(resource=…)` + `access.load_access(accepted_resources=…)` (Task 5); `refresh.mint_refresh(resource=…)` + `RefreshRow.resource` (Task 6); `self._accepted` (Task 8).
- Produces: access + refresh tokens minted through the provider carry the code's resource; `load_access` is called with `self._accepted`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_oauth_provider.py  (append)
# End-to-end: seed a code carrying a resource, exchange it, assert both tokens
# carry the resource, then assert enforcement at load_access.
from localmail.api import auth as api_auth
from localmail.mcp.oauth import access, clients, codes


def _seed_client_user(conn):
    clients.register_client(
        conn, client_id="cid", client_secret_sha256=None,
        redirect_uris=["https://c/cb"], client_name=None,
        grant_types=["authorization_code"], response_types=["code"],
        token_endpoint_auth_method="none", scope=None,
    )
    uid = api_auth.create_user(conn, "prov-res-user", "pw")
    conn.commit()
    return uid


@pytest.mark.anyio
async def test_code_exchange_binds_resource_and_enforces(db_pool, db_conn):
    uid = _seed_client_user(db_conn)
    raw_code = codes.mint_code(
        db_conn, client_id="cid", user_id=uid, redirect_uri="https://c/cb",
        redirect_uri_provided_explicitly=True, code_challenge="chal",
        scopes=[], ttl_s=60, resource="https://h/mcp",
    )
    db_conn.commit()

    provider = _provider(db_pool)
    client = _client()
    auth_code = await provider.load_authorization_code(client, raw_code)
    assert auth_code is not None and auth_code.resource == "https://h/mcp"
    tokens = await provider.exchange_authorization_code(client, auth_code)

    # access token bound + enforced through the provider's accepted set
    at_ok = access.load_access(
        db_conn, tokens.access_token, accepted_resources=["https://h/mcp"]
    )
    assert at_ok is not None
    at_bad = access.load_access(
        db_conn, tokens.access_token, accepted_resources=["https://other/mcp"]
    )
    assert at_bad is None
    # refresh token carries the resource too
    rrow = refresh.load_refresh(db_conn, tokens.refresh_token)
    assert rrow is not None and rrow.resource == "https://h/mcp"


@pytest.mark.anyio
async def test_load_access_token_enforces_via_provider(db_pool, db_conn):
    uid = _seed_client_user(db_conn)
    # A token bound to an unlisted resource must not verify through the provider.
    raw = access.mint_access(
        db_conn, user_id=uid, client_id="cid", ttl_s=3600,
        resource="https://other/mcp",
    )
    db_conn.commit()
    provider = _provider(db_pool)  # accepted = ["https://h/mcp"]
    assert await provider.load_access_token(raw) is None
```

> `db_pool` and `db_conn` must point at the same `localmail_test` DB. If the provider tests so far used only `db_pool`, confirm `db_conn` is available (it is a standard fixture in `tests/conftest.py`).

- [ ] **Step 2: Run test to verify it fails**

Run: `unset VIRTUAL_ENV && uv run --extra mcp pytest tests/test_oauth_provider.py -q -k enforce`
Expected: FAIL — `auth_code.resource` is None (not mapped) / access token has no bound resource / provider `load_access_token` doesn't enforce.

- [ ] **Step 3: Implement**

In `src/localmail/mcp/oauth/provider.py`:

`_load_code_sync` — map the code row's resource onto the SDK `AuthorizationCode`:
```python
        return AuthorizationCode(
            code=raw_code,
            scopes=row.scopes,
            expires_at=row.expires_at.timestamp(),
            client_id=row.client_id,
            code_challenge=row.code_challenge,
            redirect_uri=AnyUrl(row.redirect_uri),
            redirect_uri_provided_explicitly=row.redirect_uri_provided_explicitly,
            subject=str(row.user_id),
            resource=row.resource,
        )
```

`_exchange_code_sync` — bind the resource onto both minted tokens:
```python
                refresh_raw = refresh.mint_refresh(
                    conn, client_id=client_id, user_id=user_id,
                    scopes=auth_code.scopes,
                    ttl_s=self._cfg.oauth_refresh_token_ttl_s,
                    resource=auth_code.resource,
                )
                new_row = refresh.load_refresh(conn, refresh_raw)
                if new_row is None:
                    conn.rollback()
                    user_vanished = True
                else:
                    access_raw = access.mint_access(
                        conn, user_id=user_id, client_id=client_id,
                        ttl_s=self._cfg.oauth_access_token_ttl_s,
                        family_id=new_row.family_id,
                        resource=auth_code.resource,
                    )
```

`_exchange_refresh_sync` — bind the rotated access token to the row's resource
(the `row` is already loaded for `family_id`; just add `resource=row.resource`):
```python
                access_raw = access.mint_access(
                    conn, user_id=row.user_id, client_id=client_id,
                    ttl_s=self._cfg.oauth_access_token_ttl_s,
                    family_id=row.family_id,
                    resource=row.resource,
                )
```

`_load_access_sync` — pass the accepted set so `/mcp` enforces audience:
```python
    def _load_access_sync(self, token: str) -> AccessToken | None:
        with self._pool.connection() as conn:
            at = access.load_access(
                conn, token, accepted_resources=self._accepted
            )
            conn.commit()
        return at
```

- [ ] **Step 4: Run test to verify it passes**

Run: `unset VIRTUAL_ENV && uv run --extra mcp pytest tests/test_oauth_provider.py -q`
Expected: PASS. Then run the refresh-reuse regression to confirm the family-purge path is untouched:
`unset VIRTUAL_ENV && uv run --extra mcp pytest tests/test_oauth_provider.py tests/test_oauth_refresh_store.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/localmail/mcp/oauth/provider.py tests/test_oauth_provider.py
git commit -m "feat(mcp): bind resource onto exchanged tokens; enforce at /mcp load"
```

---

### Task 10: Consent router passes `resource` into `mint_code`

**Files:**
- Modify: `src/localmail/serve/oauth/consent_router.py:138-147` (the `codes.mint_code(...)` call)
- Test: `tests/test_serve_oauth_consent.py` (append)

**Interfaces:**
- Consumes: `ConsentPayload.resource` (Task 7); `codes.mint_code(resource=…)` (Task 4).
- Produces: the code minted by a successful consent POST carries `payload.resource`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_serve_oauth_consent.py  (append)
# After a successful consent login, the minted authorization code must carry the
# resource that was packed into the consent blob. Mirror this file's existing
# "successful consent mints a code" test; add a resource to the ConsentPayload
# and assert the persisted oauth_authorization_codes row has it.
def test_consent_mints_code_with_resource(<existing fixtures>):
    # 1. build a ConsentPayload(..., resource="https://h/mcp") and encode it
    # 2. POST valid credentials to the consent endpoint
    # 3. follow/capture the issued code (redirect `code=` param)
    # 4. SELECT resource FROM oauth_authorization_codes WHERE code_sha256 = hash(code)
    #    assert it == "https://h/mcp"
    ...
```

> Fill this in by copying the file's existing successful-consent test verbatim, adding `resource="https://h/mcp"` to the `ConsentPayload` it builds, and asserting the persisted code row's `resource` column. Use `api_auth.hash_token(code)` to locate the row. Do not invent new fixtures — reuse whatever that test already uses (test client, signing key, seeded user).

- [ ] **Step 2: Run test to verify it fails**

Run: `unset VIRTUAL_ENV && uv run --extra mcp pytest tests/test_serve_oauth_consent.py -q -k resource`
Expected: FAIL — persisted `resource` is NULL (router doesn't forward it).

- [ ] **Step 3: Implement**

In `src/localmail/serve/oauth/consent_router.py`, add `resource=payload.resource`
to the `codes.mint_code(...)` call (around line 145):
```python
            raw_code = codes.mint_code(
                conn,
                client_id=payload.client_id,
                user_id=row[0],
                redirect_uri=payload.redirect_uri,
                redirect_uri_provided_explicitly=payload.redirect_uri_provided_explicitly,
                code_challenge=payload.code_challenge,
                scopes=payload.scopes,
                ttl_s=mcp_config.oauth_authorization_code_ttl_s,
                resource=payload.resource,
            )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `unset VIRTUAL_ENV && uv run --extra mcp pytest tests/test_serve_oauth_consent.py -q`
Expected: PASS (new + existing).

- [ ] **Step 5: Commit**

```bash
git add src/localmail/serve/oauth/consent_router.py tests/test_serve_oauth_consent.py
git commit -m "feat(mcp): forward resource from consent to the authorization code"
```

---

### Task 11: Full-suite gate + docs

**Files:**
- Modify: `CLAUDE.md` (MCP OAuth AS section), `README.md` (OAuth section)

- [ ] **Step 1: Run the full suite + types**

Run:
```bash
unset VIRTUAL_ENV && uv run --extra mcp pytest -q tests/ --deselect tests/test_daemon_control_socket.py
unset VIRTUAL_ENV && uv run mypy src/localmail
```
Expected: all pass (baseline was 1670 passed, 14 deselected + mypy clean, 121 files; this feature adds tests and one new source file). If any pre-existing test broke, fix it before proceeding — a broken existing test is a real regression, not noise.

- [ ] **Step 2: Update CLAUDE.md**

In the MCP server section, in the OAuth 2.1 authorization server "Known
limitations" bullet, **remove** the "(2) RFC 8707 resource indicators are not
carried through the flow or bound onto tokens…" clause and add a new shipped
bullet, e.g.:

> **RFC 8707 resource indicators (shipped):** `/authorize` validates the
> client's `resource` against a configurable accepted set
> (`McpConfig.resource_indicators`, default `[mcp_resource_url(resource_server_url)]`)
> via the pure `mcp/oauth/resource_indicator.py`; the bound resource is carried
> through the consent blob → `oauth_authorization_codes.resource` → onto the
> minted access (`api_tokens.oauth_resource`) + refresh
> (`oauth_refresh_tokens.resource`) tokens, and enforced at `/mcp` in
> `access.load_access` (NULL = unrestricted; `/v1` unchanged). A missing
> `resource` is accepted unless `oauth_require_resource_indicator = true`.
> Migration `0031_oauth_resource_indicator.sql`. **Accepted:** the SDK swallows
> the token-endpoint `resource` (validated at authorize only) and lacks an
> `invalid_target` code (a bad resource → `invalid_request`).

Also bump the "Latest is `0030…`; next free slot `0031_*.sql`" line in the
Conventions section to "Latest is `0031_oauth_resource_indicator.sql`; next free
slot `0032_*.sql`."

- [ ] **Step 3: Update README.md**

In the OAuth/MCP section, add a sentence that localmail validates the RFC 8707
`resource` indicator and binds the issued token's audience to `<origin>/mcp`,
with `oauth_require_resource_indicator` to require it. Match the README's
existing tone and formatting.

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md README.md
git commit -m "docs: RFC 8707 resource indicators shipped (CLAUDE.md + README)"
```

---

## Self-Review

**Spec coverage:**
- Config (`resource_indicators`, `oauth_require_resource_indicator`) → Task 3. ✓
- Pure `resource_indicator.py` (canonicalize, resolve, decide) → Task 1. ✓
- Migration 0031 (three columns) → Task 2. ✓
- authorize validate + bind → Task 8. ✓
- consent → code carry → Tasks 7 (payload) + 10 (router) + 4 (code store). ✓
- code exchange binds onto access + refresh → Task 9 (+ stores in 5, 6). ✓
- refresh rotation carries forward → Task 6 (store) + Task 9 (provider uses it). ✓
- enforcement at load_access → Task 5 (store) + Task 9 (provider passes accepted set). ✓
- error handling (`invalid_request`, NULL unrestricted, mismatch → None) → Tasks 5, 8. ✓
- accepted-set non-empty guarantee / ValueError → Task 1 (+ resolved at construction in Task 8). ✓
- docs → Task 11. ✓

**Placeholder scan:** the only intentional fill-ins are Task 10's test body and its `<existing fixtures>` marker — the serve-consent test must be cloned from that file's existing successful-consent test, whose exact fixtures aren't reproduced here; the step spells out precisely what to copy and assert. Every code step shows real code.

**Type consistency:** `resource: str | None` everywhere; `mint_access(resource=…)` / `mint_refresh(resource=…)` / `mint_code(resource=…)` keyword-only with `= None` default; `load_access(..., accepted_resources: list[str] | None = None)`; `RefreshRow.resource` / `CodeRow.resource` / `ConsentPayload.resource`; `self._accepted: list[str]`; `decide_resource(...) -> ResourceDecision(ok, bound, error)`. Consistent across tasks.

**Ordering:** 1 (pure) → 2 (migration) → 3 (config) → 4,5,6 (stores) → 7 (consent state) → 8 (authorize) → 9 (exchange/load wiring) → 10 (consent router) → 11 (gate + docs). Each task's `Consumes` is produced by an earlier task.
