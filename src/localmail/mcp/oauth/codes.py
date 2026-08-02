# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Single-use authorization-code store. Codes are SHA-256-hashed; the raw code
is returned to the client once (via the redirect) and never stored.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import psycopg

from localmail.api.auth import generate_token, hash_token
from localmail.api.revocation_sql import credential_valid_sql


@dataclass(frozen=True)
class CodeRow:
    client_id: str
    user_id: int
    redirect_uri: str
    redirect_uri_provided_explicitly: bool
    code_challenge: str
    scopes: list[str]
    expires_at: datetime
    resource: str | None


@dataclass(frozen=True)
class ConsumeResult:
    """Outcome of burning an authorization code.

    - ``burned``: a row was actually deleted (False = already used).
    - ``still_valid``: at the instant of the burn, the code was unexpired and
      its owning user existed, was enabled, and had not revoked its sessions
      since the code was minted. Meaningless when ``burned`` is False, and
      reported as False there.

    One field rather than one per reason, deliberately: the caller's question is
    "may I honour this?", and splitting the answer invites honouring a burn that
    satisfied two conditions out of three. Same safe-by-default reasoning as the
    ``allowed_account_ids`` kwarg (#234) and ``open_attachment_bytes`` (#67).
    """
    burned: bool
    still_valid: bool


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


def load_code(conn: psycopg.Connection, raw_code: str) -> CodeRow | None:
    """Return the unexpired code row of an enabled, non-revoked user, or None.
    Does not consume it.

    The ``api_users`` JOIN mirrors ``refresh.load_refresh`` (and through it
    ``api.auth.verify_token``), so revocation is terminal for all three
    credential kinds rather than two: exchanging a code mints an access +
    refresh pair stamped ``created_at = now()`` — past the cutoff, hence valid
    — so honouring the code would hand back exactly the credentials the
    operator just cut off. ``disabled_at`` is the same argument (RFC 9700
    §4.13). The window is only ``oauth_authorization_code_ttl_s`` (default 60 s)
    wide, but a user disabled *during* the consent round trip should fail
    closed, not complete.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT c.client_id, c.user_id, c.redirect_uri, "
            "c.redirect_uri_provided_explicitly, c.code_challenge, c.scopes, "
            "c.expires_at, c.resource "
            "FROM oauth_authorization_codes c "
            "JOIN api_users u ON u.id = c.user_id "
            "WHERE c.code_sha256 = %s AND c.expires_at > now() "
            "  AND " + credential_valid_sql(user="u", credential="c"),
            (hash_token(raw_code),),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return CodeRow(
        client_id=row[0],
        user_id=row[1],
        redirect_uri=row[2],
        redirect_uri_provided_explicitly=row[3],
        code_challenge=row[4],
        scopes=row[5],
        expires_at=row[6],
        resource=row[7],
    )


def consume_code(conn: psycopg.Connection, raw_code: str) -> ConsumeResult:
    """Burn the code unconditionally and report, in the same statement, whether
    it was still honourable at that instant. Caller commits.

    **The two halves are deliberately separate concerns (#241).** Making the
    DELETE itself conditional on validity — the shape the issue first suggested
    — would leave a rejected code *unburned*, i.e. replayable for the rest of
    its TTL by anyone holding a copy, which is precisely the single-use
    invariant #219 established. So the code always dies; validity is reported
    beside it.

    Reporting has to happen *here* rather than at ``load_code`` because the SDK
    drives load and exchange as two separate calls: a revocation landing in that
    gap left the load's check stale, and the tokens the exchange then minted
    carried ``created_at = now()`` — past the cutoff, hence valid — handing back
    exactly the credentials the operator had just cut off.

    The CTE keeps every conjunct under one snapshot, so nothing can slip between
    the burn and the check.

    **Expiry is re-decided here for the same reason**, not merely inherited from
    the SDK's load: that verdict is equally stale by the time the exchange runs.
    The window is much narrower than the revocation one — a code can only cross
    its own deadline, never be revoked mid-round-trip — so this is defence in
    depth, but it costs one conjunct and it is what lets the burn stand alone
    instead of assuming its caller checked, which is the assumption #241 punished.

    ``u.id IS NOT NULL`` is the fail-closed guard for a user row that has
    vanished outright, and it has to be written explicitly: against the LEFT
    JOIN's all-NULL row every ``IS NULL`` test inside ``credential_valid_sql``
    is TRUE, so the predicate reads a deleted user as *valid*. It returns TRUE
    rather than NULL, so wrapping it in ``COALESCE(..., FALSE)`` — the obvious
    guard, and the one this first shipped with — catches nothing at all.
    Today the branch is unreachable, because ``oauth_authorization_codes.user_id``
    is ``ON DELETE CASCADE`` and a deleted user takes its codes with it, leaving
    nothing to burn; the guard is what keeps that an implementation detail of the
    schema rather than the only thing standing between a deleted user and a token
    pair. Pinned by ``test_consume_of_an_orphaned_code_reports_it_invalid``.
    """
    with conn.cursor() as cur:
        cur.execute(
            "WITH burned AS ("
            "  DELETE FROM oauth_authorization_codes WHERE code_sha256 = %s"
            "  RETURNING user_id, created_at, expires_at"
            ") "
            "SELECT u.id IS NOT NULL AND b.expires_at > now() AND "
            + credential_valid_sql(user="u", credential="b")
            + " FROM burned b LEFT JOIN api_users u ON u.id = b.user_id",
            (hash_token(raw_code),),
        )
        row = cur.fetchone()
    if row is None:
        return ConsumeResult(burned=False, still_valid=False)
    return ConsumeResult(burned=True, still_valid=row[0])
