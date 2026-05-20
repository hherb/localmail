# Pagination Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the GUI message list and search results scrollable past the current ~200-row ceiling, by adding a keyset-cursor browse endpoint, wiring the existing `Searcher` cursor machinery through HTTP, and switching the GUI to infinite-scroll with a visible "Load more" fallback.

**Architecture:** Backend gets one new HTTP endpoint (`GET /v1/messages`) for keyset-paginated browse and a `cursor`/`next_cursor` field pair on `POST /v1/search` that wraps the existing `Searcher.continue_page` + `grow_pool`. GUI gains an `IntersectionObserver`-driven bottom sentinel in `MessageList.svelte`, with per-store `loadMore*()` methods that append (search) or merge-into-buffer (incoming polled mail).

**Tech Stack:** Python 3.12 + `psycopg` v3 + FastAPI on the backend; SvelteKit + Tauri (Rust) on the GUI. Postgres `messages_recent_idx` expression index already covers the keyset predicate — no migration.

**Spec:** [docs/superpowers/specs/2026-05-20-pagination-design.md](../specs/2026-05-20-pagination-design.md)

---

## Phase 1 — Backend

### Task 1: Add `candidates_per_arm_max` to SearchConfig

**Files:**
- Modify: `src/localmail/config.py:125-130`
- Test: `tests/test_config.py` (add to existing file)

- [ ] **Step 1: Find the existing search config test (or pick a representative spot to assert defaults)**

Run: `grep -rn "SearchConfig\(\)\|page_size_default" tests/ | head -5`

If `tests/test_config.py` doesn't have a defaults test, write one inline below.

- [ ] **Step 2: Write the failing test**

In `tests/test_config.py` (create if absent), add:

```python
from localmail.config import SearchConfig


def test_candidates_per_arm_max_default_is_800() -> None:
    cfg = SearchConfig()
    assert cfg.candidates_per_arm_max == 800
    # Sanity: max must be >= initial; otherwise grow_pool can't ever fire.
    assert cfg.candidates_per_arm_max >= cfg.candidates_per_arm
```

- [ ] **Step 3: Run test to verify it fails**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_config.py::test_candidates_per_arm_max_default_is_800 -v`
Expected: FAIL with `AttributeError: 'SearchConfig' object has no attribute 'candidates_per_arm_max'`

- [ ] **Step 4: Add the field**

In `src/localmail/config.py`, after the line `rerank_pool_size: int = 20` (line 128), add:

```python
    # Cap for transparent grow_pool growth driven by the /v1/search cursor
    # path. When the page cursor would advance past the current cached pool
    # and `can_grow_pool=True`, the route doubles candidates_per_arm up to
    # this ceiling; once the ceiling is hit, next_cursor flips to null.
    candidates_per_arm_max: int = 800
```

- [ ] **Step 5: Run test to verify it passes**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_config.py::test_candidates_per_arm_max_default_is_800 -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/localmail/config.py tests/test_config.py
git commit -m "$(cat <<'EOF'
feat(config): add candidates_per_arm_max for transparent search pool growth

Cap for the /v1/search cursor path's grow_pool growth schedule. Default
800 keeps the historical upper bound on per-arm fanout reasonable while
giving deep-scroll users enough room before next_cursor flips to null.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: Browse cursor codec (pure module)

**Files:**
- Create: `src/localmail/api/browse_cursor.py`
- Create: `tests/test_api_browse_cursor.py`

- [ ] **Step 1: Write the failing tests**

`tests/test_api_browse_cursor.py`:

```python
"""Tests for the opaque browse-list cursor codec."""
from datetime import datetime, timezone

import pytest

from localmail.api.browse_cursor import (
    BrowseCursor,
    decode_browse_cursor,
    encode_browse_cursor,
)
from localmail.api.errors import ValidationFailed


def test_dated_cursor_roundtrip() -> None:
    ts = datetime(2026, 5, 20, 12, 34, 56, tzinfo=timezone.utc)
    cur = BrowseCursor(ts=ts, id=42)
    encoded = encode_browse_cursor(cur)
    # Wire form must be URL-safe (no '+', '/', '=' padding required).
    assert "/" not in encoded and "+" not in encoded
    decoded = decode_browse_cursor(encoded)
    assert decoded == cur


def test_null_date_cursor_roundtrip() -> None:
    cur = BrowseCursor(ts=None, id=99)
    encoded = encode_browse_cursor(cur)
    decoded = decode_browse_cursor(encoded)
    assert decoded == cur
    assert decoded.ts is None


def test_decode_rejects_garbage() -> None:
    with pytest.raises(ValidationFailed, match="cursor"):
        decode_browse_cursor("not-a-cursor")


def test_decode_rejects_empty_string() -> None:
    with pytest.raises(ValidationFailed):
        decode_browse_cursor("")


def test_decode_rejects_negative_id() -> None:
    # encode_browse_cursor never emits these, but a hostile client could.
    import base64
    payload = base64.urlsafe_b64encode(b"n|-1").rstrip(b"=").decode("ascii")
    with pytest.raises(ValidationFailed):
        decode_browse_cursor(payload)


def test_decode_rejects_unknown_kind() -> None:
    import base64
    payload = base64.urlsafe_b64encode(b"x|1").rstrip(b"=").decode("ascii")
    with pytest.raises(ValidationFailed):
        decode_browse_cursor(payload)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_api_browse_cursor.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'localmail.api.browse_cursor'`

- [ ] **Step 3: Implement the codec**

`src/localmail/api/browse_cursor.py`:

```python
"""Opaque cursor codec for GET /v1/messages.

Wire form is URL-safe base64 of one of:
  - "d|<iso-ts>|<id>"   — dated row
  - "n|<id>"            — NULL-date tail row

Clients MUST treat the cursor as opaque; the encoding can change without an
API version bump as long as `decode_browse_cursor` keeps accepting any
encoding `encode_browse_cursor` ever emitted.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime

from localmail.api.errors import ValidationFailed


@dataclass(frozen=True)
class BrowseCursor:
    """Keyset position for the message browse list.

    `ts is None` means "already in the NULLS-LAST tail; paginate by id alone".
    """
    ts: datetime | None
    id: int


def encode_browse_cursor(cur: BrowseCursor) -> str:
    if cur.ts is None:
        payload = f"n|{cur.id}".encode("ascii")
    else:
        payload = f"d|{cur.ts.isoformat()}|{cur.id}".encode("ascii")
    return base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")


def decode_browse_cursor(raw: str) -> BrowseCursor:
    if not raw:
        raise ValidationFailed("cursor: empty")
    try:
        # Restore base64 padding the encoder stripped.
        padded = raw + "=" * (-len(raw) % 4)
        body = base64.urlsafe_b64decode(padded.encode("ascii")).decode("ascii")
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValidationFailed(f"cursor: malformed base64 ({exc})") from exc
    parts = body.split("|")
    if not parts:
        raise ValidationFailed("cursor: empty payload")
    kind = parts[0]
    if kind == "n" and len(parts) == 2:
        return BrowseCursor(ts=None, id=_parse_nonneg_int(parts[1]))
    if kind == "d" and len(parts) == 3:
        try:
            ts = datetime.fromisoformat(parts[1])
        except ValueError as exc:
            raise ValidationFailed(f"cursor: bad timestamp {parts[1]!r}") from exc
        return BrowseCursor(ts=ts, id=_parse_nonneg_int(parts[2]))
    raise ValidationFailed(f"cursor: unknown kind {kind!r}")


def _parse_nonneg_int(s: str) -> int:
    if not s.isascii() or not s.isdigit():
        raise ValidationFailed(f"cursor: bad id {s!r}")
    return int(s)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_api_browse_cursor.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add src/localmail/api/browse_cursor.py tests/test_api_browse_cursor.py
git commit -m "$(cat <<'EOF'
feat(api): opaque browse cursor codec for /v1/messages

URL-safe base64 of "d|<iso-ts>|<id>" or "n|<id>". Pure module — no DB,
no FastAPI — so future transports (MCP) can reuse it. Malformed input
raises ValidationFailed which the HTTP layer maps to 400 problem+json.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: Service-layer `list_messages` — dated rows, initial page + cursor

**Files:**
- Create: `src/localmail/api/browse.py`
- Create: `tests/test_api_browse.py`

- [ ] **Step 1: Write the failing tests**

`tests/test_api_browse.py`:

```python
"""Tests for localmail.api.browse.list_messages."""
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from localmail.api.browse import list_messages
from localmail.api.browse_cursor import decode_browse_cursor
from localmail.api.errors import ValidationFailed


def _ensure_account(conn: psycopg.Connection, name: str = "a") -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO accounts (name, email_address, imap_host, auth_method) "
            "VALUES (%s, %s, 'imap.x', 'password') RETURNING id",
            (name, f"{name}@y.test"),
        )
        row = cur.fetchone(); assert row is not None
        return int(row[0])


def _seed(
    conn: psycopg.Connection, *,
    account_id: int,
    suffix: str,
    internal_date: datetime | None = None,
    date_sent: datetime | None = None,
) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO messages (account_id, message_id, subject, raw_bytes,
                                     raw_sha256, size_bytes, headers, attachments,
                                     date_sent, internal_date, date_received)
               VALUES (%s, %s, 's', 'r', %s, 1, '{}'::jsonb, '[]'::jsonb,
                       %s, %s, now()) RETURNING id""",
            (account_id, f"<{suffix}@x>", bytes.fromhex(suffix * 32),
             date_sent, internal_date),
        )
        row = cur.fetchone(); assert row is not None
        return int(row[0])


def test_initial_page_returns_messages_in_recent_first_order(db_conn) -> None:
    aid = _ensure_account(db_conn)
    now = datetime.now(timezone.utc)
    m_old = _seed(db_conn, account_id=aid, suffix="aa",
                  internal_date=now - timedelta(days=2))
    m_mid = _seed(db_conn, account_id=aid, suffix="bb",
                  internal_date=now - timedelta(days=1))
    m_new = _seed(db_conn, account_id=aid, suffix="cc",
                  internal_date=now)
    db_conn.commit()

    out = list_messages(db_conn, allowed_account_ids=[aid], limit=10)
    ids = [int(m["message_id"]) for m in out["messages"]]
    assert ids == [m_new, m_mid, m_old]
    assert out["next_cursor"] is None  # pool exhausted, only 3 rows


def test_cursor_round_trip_paginates_strictly_older(db_conn) -> None:
    aid = _ensure_account(db_conn)
    now = datetime.now(timezone.utc)
    ids = [
        _seed(db_conn, account_id=aid, suffix=f"{i:02x}" * 1,
              internal_date=now - timedelta(hours=i))
        for i in range(5)
    ]
    # ids[0] is the newest (i=0), ids[4] is the oldest (i=4).
    db_conn.commit()

    page1 = list_messages(db_conn, allowed_account_ids=[aid], limit=2)
    assert [int(m["message_id"]) for m in page1["messages"]] == [ids[0], ids[1]]
    assert page1["next_cursor"] is not None

    page2 = list_messages(db_conn, allowed_account_ids=[aid], limit=2,
                          cursor=page1["next_cursor"])
    assert [int(m["message_id"]) for m in page2["messages"]] == [ids[2], ids[3]]
    assert page2["next_cursor"] is not None

    page3 = list_messages(db_conn, allowed_account_ids=[aid], limit=2,
                          cursor=page2["next_cursor"])
    assert [int(m["message_id"]) for m in page3["messages"]] == [ids[4]]
    assert page3["next_cursor"] is None


def test_tied_internal_date_paginates_by_id_desc(db_conn) -> None:
    aid = _ensure_account(db_conn)
    ts = datetime(2026, 5, 20, 12, 0, 0, tzinfo=timezone.utc)
    a = _seed(db_conn, account_id=aid, suffix="aa", internal_date=ts)
    b = _seed(db_conn, account_id=aid, suffix="bb", internal_date=ts)
    c = _seed(db_conn, account_id=aid, suffix="cc", internal_date=ts)
    db_conn.commit()

    p1 = list_messages(db_conn, allowed_account_ids=[aid], limit=2)
    assert [int(m["message_id"]) for m in p1["messages"]] == [c, b]
    p2 = list_messages(db_conn, allowed_account_ids=[aid], limit=2,
                       cursor=p1["next_cursor"])
    assert [int(m["message_id"]) for m in p2["messages"]] == [a]


def test_empty_allowed_account_ids_returns_empty_page(db_conn) -> None:
    out = list_messages(db_conn, allowed_account_ids=[], limit=10)
    assert out == {"messages": [], "next_cursor": None}


def test_malformed_cursor_raises_validation_failed(db_conn) -> None:
    with pytest.raises(ValidationFailed):
        list_messages(db_conn, allowed_account_ids=[1], limit=10,
                      cursor="not-a-cursor")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_api_browse.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'localmail.api.browse'`

- [ ] **Step 3: Implement the service-layer function (dated branch only)**

`src/localmail/api/browse.py`:

```python
"""Paginated message browse — service layer.

Mirrors the shape of the /v1/changes payload but supports keyset pagination
into the past instead of forward incremental polling. The wire cursor is
opaque (see browse_cursor.py); the ACL filter applies at the SQL boundary.
"""
from __future__ import annotations

from typing import Any

import psycopg

from localmail.api.browse_cursor import (
    BrowseCursor, decode_browse_cursor, encode_browse_cursor,
)


def list_messages(
    conn: psycopg.Connection,
    *,
    allowed_account_ids: list[int],
    account_ids: list[int] | None = None,
    folder_ids: list[int] | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> dict[str, Any]:
    """Return one keyset page of messages and a `next_cursor` for the next one.

    Ordering: ``COALESCE(internal_date, date_sent) DESC NULLS LAST, id DESC``.
    Uses the ``messages_recent_idx`` expression index.

    ACL: ``allowed_account_ids`` is the *authoritative* ACL list. Caller's
    ``account_ids`` filter is intersected against it before the query runs;
    an empty intersection short-circuits to an empty page.

    ``cursor`` is the opaque token returned as ``next_cursor`` on the
    previous page; ``None`` for the initial page. Malformed cursors raise
    ``ValidationFailed`` so the HTTP layer emits a 400.
    """
    if not allowed_account_ids:
        return {"messages": [], "next_cursor": None}

    effective_account_ids = _intersect_account_ids(allowed_account_ids, account_ids)
    if not effective_account_ids:
        return {"messages": [], "next_cursor": None}

    parsed_cursor = decode_browse_cursor(cursor) if cursor is not None else None

    # Fetch one extra row to detect "more pages remain" without a COUNT.
    fetch_limit = limit + 1
    where, params = _build_where(
        account_ids=effective_account_ids,
        folder_ids=folder_ids,
        cursor=parsed_cursor,
    )
    join = "JOIN message_labels ml ON ml.message_id = m.id " if folder_ids else ""

    sql = f"""
        SELECT DISTINCT m.id, m.subject, m.from_addr, m.from_name, m.date_sent,
                        m.internal_date, m.account_id, a.name
          FROM messages m
          JOIN accounts a ON a.id = m.account_id
          {join}
         WHERE {where}
         ORDER BY COALESCE(m.internal_date, m.date_sent) DESC NULLS LAST, m.id DESC
         LIMIT %s
    """
    with conn.cursor() as cur:
        cur.execute(sql, params + [fetch_limit])
        rows = cur.fetchall()

    has_more = len(rows) > limit
    page_rows = rows[:limit]
    messages = [
        {
            "message_id": str(mid),
            "subject": subject,
            "from": {"address": from_addr, "name": from_name},
            "date": date_sent.isoformat() if date_sent else None,
            "account": {"id": str(account_id), "name": account_name},
        }
        for (mid, subject, from_addr, from_name, date_sent, _internal_date,
             account_id, account_name) in page_rows
    ]
    next_cursor: str | None = None
    if has_more and page_rows:
        last = page_rows[-1]
        _, _, _, _, last_date_sent, last_internal_date, _, _ = last
        last_mid = last[0]
        keyset_ts = last_internal_date or last_date_sent
        next_cursor = encode_browse_cursor(
            BrowseCursor(ts=keyset_ts, id=int(last_mid))
        )
    return {"messages": messages, "next_cursor": next_cursor}


def _intersect_account_ids(
    allowed: list[int], requested: list[int] | None,
) -> list[int]:
    if not requested:
        return list(allowed)
    return sorted(set(allowed) & set(requested))


def _build_where(
    *,
    account_ids: list[int],
    folder_ids: list[int] | None,
    cursor: BrowseCursor | None,
) -> tuple[str, list[Any]]:
    clauses = ["m.account_id = ANY(%s)"]
    params: list[Any] = [account_ids]
    if folder_ids:
        clauses.append("ml.mailbox_id = ANY(%s)")
        params.append(folder_ids)
    if cursor is not None:
        if cursor.ts is None:
            # Already in the NULL-date tail.
            clauses.append(
                "COALESCE(m.internal_date, m.date_sent) IS NULL AND m.id < %s"
            )
            params.append(cursor.id)
        else:
            # Still in the dated portion: tuple keyset, plus NULLs are
            # already strictly "later" in NULLS-LAST order.
            clauses.append(
                "(COALESCE(m.internal_date, m.date_sent) < %s "
                " OR (COALESCE(m.internal_date, m.date_sent) = %s AND m.id < %s) "
                " OR COALESCE(m.internal_date, m.date_sent) IS NULL)"
            )
            params.extend([cursor.ts, cursor.ts, cursor.id])
    return " AND ".join(clauses), params
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_api_browse.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add src/localmail/api/browse.py tests/test_api_browse.py
git commit -m "$(cat <<'EOF'
feat(api): list_messages service for keyset-paginated browse

Returns one page in COALESCE(internal_date, date_sent) DESC NULLS LAST
order with an opaque next_cursor. ACL applies at the SQL boundary;
empty grants short-circuit to an empty page. Uses messages_recent_idx —
no migration needed.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: `list_messages` — NULL-date tail + filter coverage

**Files:**
- Modify: `tests/test_api_browse.py` (append)

- [ ] **Step 1: Append failing tests for the NULL-tail + filter paths**

```python
def test_null_date_rows_paginate_after_dated_rows(db_conn) -> None:
    aid = _ensure_account(db_conn)
    now = datetime.now(timezone.utc)
    dated = _seed(db_conn, account_id=aid, suffix="aa",
                  internal_date=now - timedelta(hours=1))
    nul_a = _seed(db_conn, account_id=aid, suffix="bb")  # both dates NULL
    nul_b = _seed(db_conn, account_id=aid, suffix="cc")
    db_conn.commit()

    # Dated row first; NULL rows tail in id DESC (so nul_b before nul_a).
    p1 = list_messages(db_conn, allowed_account_ids=[aid], limit=1)
    assert [int(m["message_id"]) for m in p1["messages"]] == [dated]

    p2 = list_messages(db_conn, allowed_account_ids=[aid], limit=1,
                       cursor=p1["next_cursor"])
    assert [int(m["message_id"]) for m in p2["messages"]] == [nul_b]

    p3 = list_messages(db_conn, allowed_account_ids=[aid], limit=1,
                       cursor=p2["next_cursor"])
    assert [int(m["message_id"]) for m in p3["messages"]] == [nul_a]
    assert p3["next_cursor"] is None


def test_account_ids_filter_is_intersected_with_acl(db_conn) -> None:
    aid1 = _ensure_account(db_conn, name="alpha")
    aid2 = _ensure_account(db_conn, name="beta")
    now = datetime.now(timezone.utc)
    m1 = _seed(db_conn, account_id=aid1, suffix="aa", internal_date=now)
    _seed(db_conn, account_id=aid2, suffix="bb", internal_date=now)
    db_conn.commit()

    # Caller asks for both accounts but is only granted aid1.
    out = list_messages(db_conn, allowed_account_ids=[aid1],
                        account_ids=[aid1, aid2], limit=10)
    ids = [int(m["message_id"]) for m in out["messages"]]
    assert ids == [m1]


def test_account_ids_intersection_empty_short_circuits(db_conn) -> None:
    aid_granted = _ensure_account(db_conn, name="alpha")
    aid_other = _ensure_account(db_conn, name="beta")
    now = datetime.now(timezone.utc)
    _seed(db_conn, account_id=aid_granted, suffix="aa", internal_date=now)
    _seed(db_conn, account_id=aid_other, suffix="bb", internal_date=now)
    db_conn.commit()

    out = list_messages(db_conn, allowed_account_ids=[aid_granted],
                        account_ids=[aid_other], limit=10)
    assert out == {"messages": [], "next_cursor": None}


def test_folder_ids_filter_restricts_to_labelled_messages(db_conn) -> None:
    aid = _ensure_account(db_conn)
    now = datetime.now(timezone.utc)
    m_in = _seed(db_conn, account_id=aid, suffix="aa", internal_date=now)
    m_out = _seed(db_conn, account_id=aid, suffix="bb", internal_date=now)
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO mailboxes (account_id, name, uidvalidity) "
            "VALUES (%s, 'INBOX', 1) RETURNING id", (aid,),
        )
        row = cur.fetchone(); assert row is not None
        mb_id = int(row[0])
        cur.execute(
            "INSERT INTO message_labels (message_id, mailbox_id, uid) "
            "VALUES (%s, %s, 1)", (m_in, mb_id),
        )
    db_conn.commit()

    out = list_messages(db_conn, allowed_account_ids=[aid],
                        folder_ids=[mb_id], limit=10)
    ids = [int(m["message_id"]) for m in out["messages"]]
    assert ids == [m_in]
    assert m_out not in ids
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_api_browse.py -v`
Expected: PASS for the four new tests (the dated path already handles intersection + folder join; NULL-tail predicate works via the existing code in Task 3). If a test fails, fix the implementation accordingly.

Note: if `test_null_date_rows_paginate_after_dated_rows` fails because the dated→NULL boundary cursor is wrong (the cursor encodes `ts=None` only when the *last row of the page* had NULL date), inspect `next_cursor` from page 1 and confirm the encoder is using the last row's actual `internal_date` / `date_sent`. The implementation in Task 3 derives `keyset_ts = last_internal_date or last_date_sent`, which is correctly None when both are None.

- [ ] **Step 3: Commit**

```bash
git add tests/test_api_browse.py
git commit -m "$(cat <<'EOF'
test(api/browse): cover NULL-date tail, ACL intersection, folder filter

Locks in the keyset behaviour at the dated→NULL boundary and the
intersection semantics of caller-supplied account_ids vs the ACL grant
list.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: HTTP route `GET /v1/messages`

**Files:**
- Modify: `src/localmail/serve/routes/messages.py`
- Create: `tests/test_serve_browse_route.py`

- [ ] **Step 1: Write the failing tests**

`tests/test_serve_browse_route.py`:

```python
"""Tests for GET /v1/messages — the keyset-paginated browse route."""
from datetime import datetime, timedelta, timezone

import psycopg
from fastapi.testclient import TestClient

from localmail.serve.app import create_app


def _seed_acct(conn: psycopg.Connection, name: str = "a") -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO accounts (name, email_address, imap_host, auth_method) "
            "VALUES (%s, %s, 'imap.x', 'password') RETURNING id",
            (name, f"{name}@y.test"),
        )
        row = cur.fetchone(); assert row is not None
        return int(row[0])


def _seed_msg(conn: psycopg.Connection, account_id: int, suffix: str,
              when: datetime) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO messages (account_id, message_id, subject, raw_bytes,
                                     raw_sha256, size_bytes, headers, attachments,
                                     date_sent, internal_date, date_received)
               VALUES (%s, %s, 's', 'r', %s, 1, '{}'::jsonb, '[]'::jsonb,
                       %s, %s, now()) RETURNING id""",
            (account_id, f"<{suffix}@x>", bytes.fromhex(suffix * 32),
             when, when),
        )
        row = cur.fetchone(); assert row is not None
        return int(row[0])


def _grant(conn: psycopg.Connection, user_id: int, account_id: int) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO user_accounts (user_id, account_id) VALUES (%s, %s)",
            (user_id, account_id),
        )
    conn.commit()


def test_browse_initial_page_and_cursor_roundtrip(
    db_dsn: str, api_token: str, api_user, db_conn,
) -> None:
    aid = _seed_acct(db_conn)
    now = datetime.now(timezone.utc)
    ids = [_seed_msg(db_conn, aid, f"{i:02d}", now - timedelta(hours=i))
           for i in range(5)]
    db_conn.commit()
    _grant(db_conn, api_user.id, aid)

    c = TestClient(create_app(db_dsn=db_dsn, searcher=None))
    r1 = c.get("/v1/messages?limit=2",
               headers={"Authorization": f"Bearer {api_token}"})
    assert r1.status_code == 200
    body1 = r1.json()
    assert [int(m["message_id"]) for m in body1["messages"]] == [ids[0], ids[1]]
    assert body1["next_cursor"] is not None

    r2 = c.get(f"/v1/messages?limit=2&cursor={body1['next_cursor']}",
               headers={"Authorization": f"Bearer {api_token}"})
    body2 = r2.json()
    assert [int(m["message_id"]) for m in body2["messages"]] == [ids[2], ids[3]]


def test_browse_empty_grants_returns_empty(
    db_dsn: str, api_token: str, db_conn,
) -> None:
    c = TestClient(create_app(db_dsn=db_dsn, searcher=None))
    r = c.get("/v1/messages",
              headers={"Authorization": f"Bearer {api_token}"})
    assert r.status_code == 200
    assert r.json() == {"messages": [], "next_cursor": None}


def test_browse_garbage_cursor_400(db_dsn: str, api_token: str) -> None:
    c = TestClient(create_app(db_dsn=db_dsn, searcher=None))
    r = c.get("/v1/messages?cursor=not-a-cursor",
              headers={"Authorization": f"Bearer {api_token}"})
    assert r.status_code == 400
    assert r.headers["content-type"].startswith("application/problem+json")
    assert r.json()["type"] == "/problems/validation-failed"


def test_browse_account_id_non_digit_400(db_dsn: str, api_token: str) -> None:
    c = TestClient(create_app(db_dsn=db_dsn, searcher=None))
    r = c.get("/v1/messages?account_id=abc",
              headers={"Authorization": f"Bearer {api_token}"})
    assert r.status_code == 400


def test_browse_account_id_repeats_treated_as_list(
    db_dsn: str, api_token: str, api_user, db_conn,
) -> None:
    aid1 = _seed_acct(db_conn, name="one")
    aid2 = _seed_acct(db_conn, name="two")
    now = datetime.now(timezone.utc)
    m1 = _seed_msg(db_conn, aid1, "aa", now)
    m2 = _seed_msg(db_conn, aid2, "bb", now - timedelta(hours=1))
    db_conn.commit()
    _grant(db_conn, api_user.id, aid1)
    _grant(db_conn, api_user.id, aid2)

    c = TestClient(create_app(db_dsn=db_dsn, searcher=None))
    r = c.get(f"/v1/messages?account_id={aid1}&account_id={aid2}",
              headers={"Authorization": f"Bearer {api_token}"})
    assert r.status_code == 200
    ids = [int(m["message_id"]) for m in r.json()["messages"]]
    assert ids == [m1, m2]


def test_browse_requires_auth(db_dsn: str) -> None:
    c = TestClient(create_app(db_dsn=db_dsn, searcher=None))
    r = c.get("/v1/messages")
    assert r.status_code == 401
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_serve_browse_route.py -v`
Expected: FAIL — the route doesn't exist yet (404 on GET / for the `messages` router) or returns the wrong shape.

- [ ] **Step 3: Add the route**

In `src/localmail/serve/routes/messages.py`, add new imports at the top:

```python
from typing import List

from localmail.api.browse import list_messages
```

Then add a new handler **above** the existing `@router.get("/{message_id}")` handler (FastAPI route ordering — the specific `{message_id}` path must come AFTER the empty path or it will swallow this route):

```python
@router.get("")
def browse(
    request: Request,
    account_id: List[str] = Query(default_factory=list),
    folder_id: List[str] = Query(default_factory=list),
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(default=None),
    user=Depends(get_authenticated_user),
) -> dict[str, Any]:
    """Keyset-paginated browse of messages, newest first.

    `account_id` / `folder_id` are repeatable query parameters and intersect
    with the caller's ACL grants at the service-layer SQL boundary.
    """
    parsed_account_ids = [parse_int_id(v, field="account_id") for v in account_id]
    parsed_folder_ids = [parse_int_id(v, field="folder_id") for v in folder_id]
    pool = request.app.state.pool
    with pool.connection() as conn:
        allowed = allowed_account_ids(conn, user.id)
        return list_messages(
            conn,
            allowed_account_ids=allowed,
            account_ids=parsed_account_ids or None,
            folder_ids=parsed_folder_ids or None,
            limit=limit,
            cursor=cursor,
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_serve_browse_route.py -v`
Expected: PASS (6 tests). Also re-run the existing detail-route tests to confirm they didn't regress:

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_api_messages.py tests/test_serve_browse_route.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/localmail/serve/routes/messages.py tests/test_serve_browse_route.py
git commit -m "$(cat <<'EOF'
feat(serve): GET /v1/messages — keyset-paginated browse

Mounted on the existing /v1/messages router above the {message_id}
detail handler so the specific path doesn't swallow it. Repeatable
account_id / folder_id query params intersect with the caller's ACL
at the service-layer SQL boundary.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: `SearchCursorExpired` error type

**Files:**
- Modify: `src/localmail/api/errors.py`
- Create: `tests/test_api_search_cursor_error.py`

- [ ] **Step 1: Write the failing test**

`tests/test_api_search_cursor_error.py`:

```python
from localmail.api.errors import SearchCursorExpired


def test_search_cursor_expired_problem_shape() -> None:
    err = SearchCursorExpired("token abc not found")
    problem = err.to_problem()
    assert err.http_status == 409
    assert problem["type"] == "/problems/search-cursor-expired"
    assert problem["title"] == "Search cursor expired"
    assert problem["status"] == 409
    assert problem["detail"] == "token abc not found"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_api_search_cursor_error.py -v`
Expected: FAIL with `ImportError: cannot import name 'SearchCursorExpired'`

- [ ] **Step 3: Add the class**

In `src/localmail/api/errors.py`, append:

```python
class SearchCursorExpired(APIError):
    """The page cursor pool has been evicted (TTL, LRU, cross-user replay).

    Clients should re-run the original query without a cursor and resume
    scrolling from where they left off — the transparent recovery path is
    documented in the pagination spec.
    """
    http_status = 409
    problem_type = "/problems/search-cursor-expired"
    title = "Search cursor expired"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_api_search_cursor_error.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/localmail/api/errors.py tests/test_api_search_cursor_error.py
git commit -m "$(cat <<'EOF'
feat(api): SearchCursorExpired 409 problem type

Surfaces page-cache miss (TTL/LRU eviction or cross-user replay) as a
distinct error code so the GUI can run the transparent re-search +
resume path instead of bubbling a generic 500.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 7: Search cursor codec (`token:page`)

**Files:**
- Create: `src/localmail/api/search_cursor.py`
- Create: `tests/test_api_search_cursor.py`

- [ ] **Step 1: Write the failing tests**

`tests/test_api_search_cursor.py`:

```python
import pytest

from localmail.api.errors import ValidationFailed
from localmail.api.search_cursor import (
    SearchCursor, decode_search_cursor, encode_search_cursor,
)


def test_roundtrip() -> None:
    c = SearchCursor(token="abc123", page=4)
    s = encode_search_cursor(c)
    assert s == "abc123:4"
    assert decode_search_cursor(s) == c


def test_decode_rejects_missing_colon() -> None:
    with pytest.raises(ValidationFailed):
        decode_search_cursor("abc123")


def test_decode_rejects_non_digit_page() -> None:
    with pytest.raises(ValidationFailed):
        decode_search_cursor("abc123:x")


def test_decode_rejects_page_zero_or_negative() -> None:
    with pytest.raises(ValidationFailed):
        decode_search_cursor("abc123:0")
    with pytest.raises(ValidationFailed):
        decode_search_cursor("abc123:-1")


def test_decode_rejects_empty_token() -> None:
    with pytest.raises(ValidationFailed):
        decode_search_cursor(":3")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_api_search_cursor.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'localmail.api.search_cursor'`

- [ ] **Step 3: Implement the codec**

`src/localmail/api/search_cursor.py`:

```python
"""Opaque cursor codec for POST /v1/search.

The wire form is ``"{search_token}:{page}"``. Both parts are
ASCII-alphanumeric, so no encoding layer is needed; clients MUST still
treat the cursor as opaque.
"""
from __future__ import annotations

from dataclasses import dataclass

from localmail.api.errors import ValidationFailed


@dataclass(frozen=True)
class SearchCursor:
    token: str
    page: int


def encode_search_cursor(cursor: SearchCursor) -> str:
    return f"{cursor.token}:{cursor.page}"


def decode_search_cursor(raw: str) -> SearchCursor:
    if ":" not in raw:
        raise ValidationFailed(f"cursor: missing ':' separator in {raw!r}")
    token, _, page_str = raw.rpartition(":")
    if not token:
        raise ValidationFailed("cursor: empty token")
    if not page_str.isascii() or not page_str.isdigit():
        raise ValidationFailed(f"cursor: page must be a positive integer, got {page_str!r}")
    page = int(page_str)
    if page < 1:
        raise ValidationFailed(f"cursor: page must be >= 1, got {page}")
    return SearchCursor(token=token, page=page)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_api_search_cursor.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add src/localmail/api/search_cursor.py tests/test_api_search_cursor.py
git commit -m "$(cat <<'EOF'
feat(api): search cursor codec ('token:page')

Pure codec module the search route uses to round-trip the page-cache
token + page index through the wire. Page is 1-based; invalid input
surfaces as ValidationFailed → 400 problem+json.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 8: Wire cursor through `run_search` + transparent `grow_pool`

**Files:**
- Modify: `src/localmail/api/search.py`
- Create: `tests/test_api_search_pagination.py`

- [ ] **Step 1: Write the failing tests**

`tests/test_api_search_pagination.py`:

```python
"""Cursor + transparent pool growth tests for localmail.api.search.run_search."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from localmail.api.errors import SearchCursorExpired, ValidationFailed
from localmail.api.search import run_search
from localmail.api.search_cursor import encode_search_cursor, SearchCursor
from localmail.search.page_cache import CacheMissError


def _result(message_id: int = 1) -> MagicMock:
    r = MagicMock()
    r.message_id = message_id
    r.account_id = 1
    r.rank = 1
    r.score = 0.5
    r.rrf_score = 0.4
    r.subject = "s"
    r.from_addr = "a@x"
    r.from_name = "A"
    r.date_sent = None
    r.snippet = ""
    r.snippet_source = "body"
    r.attachment_filename = None
    r.matched_chunk_table = "message_chunks"
    return r


def _page(*, results: list, token: str | None, pool_size: int,
          page_size: int, has_more: bool, can_grow: bool,
          page: int = 1) -> MagicMock:
    p = MagicMock()
    p.results = results
    p.search_token = token
    p.pool_size = pool_size
    p.page_size = page_size
    p.page = page
    p.has_more_in_pool = has_more
    p.can_grow_pool = can_grow
    p.timing_ms = {"total": 1.0}
    return p


def test_initial_search_emits_next_cursor_when_more_in_pool() -> None:
    s = MagicMock()
    s.search.return_value = _page(
        results=[_result()], token="tok-1", pool_size=10,
        page_size=2, has_more=True, can_grow=True,
    )
    out = run_search(searcher=s, free_text="hello", filters={},
                     limit=2, allowed_account_ids=[1], user_id=99)
    assert out["next_cursor"] == "tok-1:2"
    assert len(out["results"]) == 1
    s.search.assert_called_once()


def test_initial_search_emits_null_cursor_when_pool_exhausted() -> None:
    s = MagicMock()
    s.search.return_value = _page(
        results=[_result()], token="tok-1", pool_size=2,
        page_size=2, has_more=False, can_grow=False,
    )
    out = run_search(searcher=s, free_text="hello", filters={},
                     limit=2, allowed_account_ids=[1], user_id=99)
    assert out["next_cursor"] is None


def test_cursor_dispatches_to_continue_page() -> None:
    s = MagicMock()
    s.continue_page.return_value = _page(
        results=[_result(2)], token="tok-1", pool_size=10,
        page_size=2, has_more=True, can_grow=True, page=2,
    )
    cursor = encode_search_cursor(SearchCursor(token="tok-1", page=2))
    out = run_search(searcher=s, free_text="hello", filters={},
                     limit=2, allowed_account_ids=[1], user_id=99,
                     cursor=cursor)
    s.search.assert_not_called()
    s.continue_page.assert_called_once_with("tok-1", 2, user_id=99)
    assert out["next_cursor"] == "tok-1:3"


def test_cache_miss_raises_search_cursor_expired() -> None:
    s = MagicMock()
    s.continue_page.side_effect = CacheMissError("tok-1")
    cursor = encode_search_cursor(SearchCursor(token="tok-1", page=2))
    with pytest.raises(SearchCursorExpired):
        run_search(searcher=s, free_text="hello", filters={},
                   limit=2, allowed_account_ids=[1], user_id=99,
                   cursor=cursor)


def test_pool_exhausted_with_grow_pool_available_triggers_grow_pool() -> None:
    """When the cursor's page would land past the pool but can_grow_pool=True,
    the route calls grow_pool(token, candidates_per_arm*2) and returns the
    resulting page."""
    from localmail.search.page_cache import PageOutOfPoolError
    s = MagicMock()
    # The cached pool currently has cpa=50; advancing the cursor past it.
    s.continue_page.side_effect = PageOutOfPoolError("past pool")
    # grow_pool returns a freshly enlarged pool's page 1.
    grown_page = _page(
        results=[_result(3)], token="tok-2", pool_size=20,
        page_size=2, has_more=True, can_grow=True,
    )
    s.grow_pool.return_value = grown_page
    # The cursor encodes which cpa to grow into; the route reads the cached
    # entry to find the current cpa via the searcher's internal access.
    # For this test, we just assert grow_pool was called with a larger cpa.
    cursor = encode_search_cursor(SearchCursor(token="tok-1", page=5))
    out = run_search(searcher=s, free_text="hello", filters={},
                     limit=2, allowed_account_ids=[1], user_id=99,
                     cursor=cursor)
    s.grow_pool.assert_called_once()
    args, kwargs = s.grow_pool.call_args
    # Token is positional or keyword; assert correctness either way.
    assert (args and args[0] == "tok-1") or kwargs.get("search_token") == "tok-1"
    # Caller asked for a larger cpa than current default (50).
    cpa_arg = (args[1] if len(args) > 1 else kwargs.get("candidates_per_arm"))
    assert cpa_arg > 50
    assert out["next_cursor"] == "tok-2:2"


def test_malformed_cursor_raises_validation_failed() -> None:
    s = MagicMock()
    with pytest.raises(ValidationFailed):
        run_search(searcher=s, free_text="x", filters={}, limit=2,
                   allowed_account_ids=[1], user_id=99,
                   cursor="not-a-cursor")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_api_search_pagination.py -v`
Expected: FAIL — `run_search` signature doesn't accept `cursor`, and all the next_cursor expectations are not met.

- [ ] **Step 3: Modify `run_search`**

In `src/localmail/api/search.py`:

3a. Add imports near the top:

```python
from localmail.api.errors import SearchCursorExpired
from localmail.api.search_cursor import (
    SearchCursor, decode_search_cursor, encode_search_cursor,
)
from localmail.search.page_cache import CacheMissError, PageOutOfPoolError
```

3b. Replace the existing `run_search` body. The new signature adds `cursor: str | None = None` and the body branches on it.

```python
def run_search(
    *,
    searcher: Searcher,
    free_text: str,
    filters: dict[str, Any],
    limit: int,
    allowed_account_ids: list[int],
    user_id: int,
    sort: Literal["rank", "date"] = "rank",
    cursor: str | None = None,
) -> dict[str, Any]:
    """Run a search (or continue an existing one) and return the API-shaped response.

    ``cursor`` is the opaque ``next_cursor`` returned by a previous call.
    When present, ``searcher.continue_page`` serves the next page from the
    cached rerank pool with zero re-retrieval. If the page index advances
    past the cached pool's end (``PageOutOfPoolError``) and the pool can
    still be grown, the route transparently calls
    ``searcher.grow_pool(token, candidates_per_arm * 2)`` and returns its
    page 1. If the cache has been evicted (``CacheMissError``) — TTL, LRU,
    or cross-user replay — the route raises ``SearchCursorExpired`` (HTTP
    409) so the GUI can run its transparent re-search recovery.

    ``next_cursor`` in the response is ``None`` once the rerank pool is
    exhausted *and* further growth would exceed
    ``searcher._cfg.candidates_per_arm_max``.
    """
    scoped_filters = _scope_filters_by_acl(filters, allowed_account_ids)
    if scoped_filters is None:
        return {"results": [], "next_cursor": None, "total_estimate": 0, "took_ms": 0.0}

    cfg = searcher._cfg  # noqa: SLF001  — route needs the grow-pool cap
    if cursor is None:
        query = build_query_string(free_text=free_text, filters=scoped_filters)
        page = searcher.search(query, page_size=limit, user_id=user_id, sort=sort)
    else:
        parsed = decode_search_cursor(cursor)
        page = _continue_or_grow(searcher, parsed, user_id=user_id, cfg=cfg)

    next_cursor = _next_cursor(page, cfg=cfg)
    return {
        "results": [_to_api_result(r) for r in page.results],
        "next_cursor": next_cursor,
        "total_estimate": None,
        "took_ms": page.timing_ms.get("total", 0.0),
    }


def _continue_or_grow(
    searcher: Searcher, parsed: SearchCursor, *, user_id: int, cfg,
):
    try:
        return searcher.continue_page(parsed.token, parsed.page, user_id=user_id)
    except CacheMissError as exc:
        raise SearchCursorExpired(f"cursor {parsed.token!r} not found") from exc
    except PageOutOfPoolError:
        try:
            entry = searcher._cache.get(parsed.token)  # noqa: SLF001
        except CacheMissError as exc:
            raise SearchCursorExpired(f"cursor {parsed.token!r} not found") from exc
        current_cpa = int(entry.get("candidates_per_arm", cfg.candidates_per_arm))
        if current_cpa >= cfg.candidates_per_arm_max:
            # Pool already at the cap — return an empty page so the caller's
            # next_cursor derivation flips to null.
            return _empty_grown_page(parsed.token, page_size=entry.get("page_size", 50))
        new_cpa = min(current_cpa * 2, cfg.candidates_per_arm_max)
        return searcher.grow_pool(parsed.token, new_cpa, user_id=user_id)


def _empty_grown_page(token: str, *, page_size: int):
    """Synthetic 'pool exhausted at cap' page so callers see next_cursor=null."""
    from localmail.search.searcher import SearchPage
    from localmail.search.query import parse_query
    return SearchPage(
        results=[], page=1, page_size=page_size, pool_size=0,
        candidates_per_arm=0, has_more_in_pool=False, can_grow_pool=False,
        search_token=token, query=parse_query(""), timing_ms={"total": 0.0},
    )


def _next_cursor(page, *, cfg) -> str | None:
    """Compute the cursor for the page after `page`, or None if exhausted."""
    if page.search_token is None:
        return None
    # `has_more_in_pool` already encodes the "more rows available in cache" check.
    if page.has_more_in_pool:
        return encode_search_cursor(SearchCursor(token=page.search_token,
                                                 page=page.page + 1))
    # Pool exhausted but can still be grown: hand out a cursor so the next
    # client request triggers grow_pool transparently.
    if page.can_grow_pool and page.candidates_per_arm < cfg.candidates_per_arm_max:
        return encode_search_cursor(SearchCursor(token=page.search_token,
                                                 page=page.page + 1))
    return None
```

Note: the existing `_to_api_result` and `_scope_filters_by_acl` helpers stay as-is.

3c. Update the file's module docstring to mention cursor support (optional, low-priority).

- [ ] **Step 4: Run pagination tests to verify they pass**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_api_search_pagination.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Re-run existing search tests to confirm no regression**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_api_search.py tests/test_serve_search_route.py tests/test_searcher_pagination.py tests/test_searcher_acl_cursor.py -v`
Expected: PASS — these may need adjustment if any test asserts `next_cursor is None`. Update those assertions to either `None` (initial search with small pool) or `"<token>:<page>"` (when there's more to fetch). The existing `test_search_returns_results` in `tests/test_serve_search_route.py` asserts `body["next_cursor"] is None`; since the fake searcher's page has `pool_size=2, has_more_in_pool` unset on the MagicMock, the assertion needs the fake to explicitly set those — update the fake to match the new contract:

```python
# In _fake_searcher_returning_one_hit (test_serve_search_route.py line ~28):
    page.has_more_in_pool = False
    page.can_grow_pool = False
    page.candidates_per_arm = 50
    page.page = 1
```

Re-run the suite; everything should pass.

- [ ] **Step 6: Wire `cursor` into the HTTP layer**

In `src/localmail/serve/routes/search.py`, add `cursor: str | None = None` to `SearchRequest` and forward it:

```python
class SearchRequest(BaseModel):
    query: str
    filters: SearchFiltersModel = Field(default_factory=SearchFiltersModel)
    limit: int = Field(default=50, ge=1, le=SEARCH_LIMIT_MAX)
    sort: Literal["rank", "date"] = "rank"
    cursor: str | None = None
```

And in the handler body, pass `cursor=req.cursor`:

```python
    return run_search(
        searcher=searcher,
        free_text=req.query,
        filters=filters_dict,
        limit=req.limit,
        allowed_account_ids=allowed,
        user_id=user.id,
        sort=req.sort,
        cursor=req.cursor,
    )
```

- [ ] **Step 7: Add HTTP-level pagination test**

Append to `tests/test_serve_search_route.py`:

```python
def test_search_cursor_forwarded_to_run_search(
    db_dsn: str, api_token: str, db_conn, api_user,
) -> None:
    """Wire-level: when the client sends `cursor`, the route must forward it
    to run_search so continue_page fires instead of search()."""
    _seed_acct_and_grant(db_conn, api_user.id)
    fake = _fake_searcher_returning_one_hit()
    # Make the fake's continue_page return the same shape as .search.
    fake.continue_page = fake.search
    app = create_app(db_dsn=db_dsn, searcher=fake)
    c = TestClient(app)
    r = c.post(
        "/v1/search",
        json={"query": "hello", "filters": {}, "limit": 20,
              "cursor": "tok-99:2"},
        headers={"Authorization": f"Bearer {api_token}"},
    )
    assert r.status_code == 200
    fake.continue_page.assert_called_once()
    args = fake.continue_page.call_args
    assert args.args[0] == "tok-99"
    assert args.args[1] == 2
```

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_serve_search_route.py::test_search_cursor_forwarded_to_run_search -v`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add src/localmail/api/search.py src/localmail/serve/routes/search.py \
        tests/test_api_search_pagination.py tests/test_serve_search_route.py
git commit -m "$(cat <<'EOF'
feat(serve/search): wire cursor through /v1/search

run_search now branches on `cursor`: None → search(); otherwise
continue_page() with transparent grow_pool() once the cached pool
exhausts (up to candidates_per_arm_max). PageCache misses surface as
HTTP 409 SearchCursorExpired so the GUI can run its transparent
re-search recovery.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Phase 2 — GUI

### Task 9: Rust `list_messages_cmd`

**Files:**
- Create: `gui/src-tauri/src/commands/browse.rs`
- Modify: `gui/src-tauri/src/commands/mod.rs`
- Modify: `gui/src-tauri/src/lib.rs`

- [ ] **Step 1: Inspect the existing `changes.rs` for the pattern**

Run: `cat gui/src-tauri/src/commands/changes.rs`

Use it as the template — same `MessageSummary` shape, similar `(url, pin, token)` extraction, `build_pinned_client`, `http_get_json` flow.

- [ ] **Step 2: Write the command + its serialisation tests**

`gui/src-tauri/src/commands/browse.rs`:

```rust
//! GET /v1/messages — keyset-paginated browse.
//!
//! Mirrors the wire shape of /v1/changes but supports a `cursor` query
//! parameter for paging into older messages. /v1/changes stays in place
//! for forward incremental polling.

use serde::{Deserialize, Serialize};

use crate::commands::auth::AuthError;
use crate::commands::changes::{MessageAccount, MessageAddress, MessageSummary};
use crate::commands::session::read_authenticated;
use crate::http::client::{build_pinned_client, http_get_json};
use crate::storage::keyring::KeyringStore;

#[derive(Debug, Default, Deserialize, Serialize)]
pub struct ListMessagesRequest {
    #[serde(default)]
    pub account_ids: Vec<String>,
    #[serde(default)]
    pub folder_ids: Vec<String>,
    #[serde(default = "default_limit")]
    pub limit: u32,
    #[serde(default)]
    pub cursor: Option<String>,
}

fn default_limit() -> u32 { 50 }

#[derive(Debug, Deserialize, Serialize)]
pub struct ListMessagesResponse {
    pub messages: Vec<MessageSummary>,
    pub next_cursor: Option<String>,
}

pub async fn list_messages(
    store: &KeyringStore,
    req: ListMessagesRequest,
) -> Result<ListMessagesResponse, AuthError> {
    let (url, pin, token) = read_authenticated(store)?;
    let client = build_pinned_client(&pin)?;
    let mut qs: Vec<(String, String)> = Vec::new();
    for a in &req.account_ids { qs.push(("account_id".into(), a.clone())); }
    for f in &req.folder_ids { qs.push(("folder_id".into(), f.clone())); }
    qs.push(("limit".into(), req.limit.to_string()));
    if let Some(c) = &req.cursor { qs.push(("cursor".into(), c.clone())); }
    let mut endpoint = format!("{url}v1/messages?");
    endpoint.push_str(
        &qs.into_iter()
            .map(|(k, v)| format!("{}={}", k, urlencoding::encode(&v)))
            .collect::<Vec<_>>()
            .join("&"),
    );
    let resp: ListMessagesResponse = http_get_json(&client, &endpoint, Some(&token)).await?;
    Ok(resp)
}

#[tauri::command]
pub async fn list_messages_cmd(
    req: ListMessagesRequest,
) -> Result<ListMessagesResponse, AuthError> {
    let store = KeyringStore::new();
    list_messages(&store, req).await
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::storage::keyring::{MemKeyring, Slot};

    fn fake_store() -> KeyringStore {
        KeyringStore::with_backend(MemKeyring::new())
    }

    fn req() -> ListMessagesRequest {
        ListMessagesRequest {
            account_ids: vec![],
            folder_ids: vec![],
            limit: 50,
            cursor: None,
        }
    }

    #[tokio::test]
    async fn without_connection_returns_not_connected() {
        let s = fake_store();
        let err = list_messages(&s, req()).await.unwrap_err();
        assert!(matches!(err, AuthError::NotConnected));
    }

    #[tokio::test]
    async fn without_token_returns_not_logged_in() {
        let s = fake_store();
        s.put(Slot::ServerUrl, "https://localhost:8443/").unwrap();
        s.put(Slot::CertPin, "deadbeef").unwrap();
        let err = list_messages(&s, req()).await.unwrap_err();
        assert!(matches!(err, AuthError::NotLoggedIn));
    }

    #[test]
    fn response_deserialises_with_null_next_cursor() {
        let json = r#"{"messages": [], "next_cursor": null}"#;
        let resp: ListMessagesResponse = serde_json::from_str(json).unwrap();
        assert!(resp.messages.is_empty());
        assert!(resp.next_cursor.is_none());
    }

    #[test]
    fn response_deserialises_with_present_next_cursor() {
        let json = r#"{"messages": [], "next_cursor": "abcd"}"#;
        let resp: ListMessagesResponse = serde_json::from_str(json).unwrap();
        assert_eq!(resp.next_cursor.as_deref(), Some("abcd"));
    }
}
```

- [ ] **Step 3: Check that `urlencoding` is already a dep, or add it**

Run: `grep urlencoding gui/src-tauri/Cargo.toml`

If absent, add to `[dependencies]`:

```toml
urlencoding = "2"
```

- [ ] **Step 4: Register the module**

In `gui/src-tauri/src/commands/mod.rs`, add the line in alphabetical order:

```rust
pub mod browse;
```

In `gui/src-tauri/src/lib.rs`, find the existing `tauri::generate_handler!` block (around line 69) and add inside the macro list:

```rust
            crate::commands::browse::list_messages_cmd,
```

- [ ] **Step 5: Build + run Rust tests**

Run: `cd gui/src-tauri && cargo test --lib browse`
Expected: PASS (4 tests)

Also run a full build to catch unrelated regressions:

Run: `cd gui/src-tauri && cargo build`
Expected: success.

- [ ] **Step 6: Commit**

```bash
git add gui/src-tauri/src/commands/browse.rs gui/src-tauri/src/commands/mod.rs \
        gui/src-tauri/src/lib.rs gui/src-tauri/Cargo.toml gui/src-tauri/Cargo.lock
git commit -m "$(cat <<'EOF'
feat(gui/tauri): list_messages_cmd for GET /v1/messages

Forwards account_id / folder_id repeats, limit, and cursor as query
params with URL-encoded values. Mirrors the pattern of changes.rs;
returns ListMessagesResponse { messages, next_cursor }.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 10: TS wrapper for `list_messages`

**Files:**
- Create: `gui/src/lib/api/browse.ts`
- Modify: `gui/src/lib/tauri.ts`

- [ ] **Step 1: Create the TS module**

`gui/src/lib/api/browse.ts`:

```ts
/**
 * Wire types + Tauri wrapper for GET /v1/messages.
 *
 * Mirrors the Rust ListMessagesRequest/Response in
 * src-tauri/src/commands/browse.rs.
 */
import { invoke } from "@tauri-apps/api/core";

import type { MessageSummary } from "./types";

export interface ListMessagesRequest {
  account_ids: string[];
  folder_ids: string[];
  limit: number;
  cursor: string | null;
}

export interface ListMessagesResponse {
  messages: MessageSummary[];
  next_cursor: string | null;
}

export async function listMessages(
  req: ListMessagesRequest,
): Promise<ListMessagesResponse> {
  return invoke<ListMessagesResponse>("list_messages_cmd", { req });
}
```

- [ ] **Step 2: Re-export from tauri.ts**

In `gui/src/lib/tauri.ts`, after the existing `getMessage` / `runSearch` re-exports, add:

```ts
export type {
  ListMessagesRequest,
  ListMessagesResponse,
} from "./api/browse";
export { listMessages } from "./api/browse";
```

- [ ] **Step 3: Verify TypeScript still compiles**

Run: `cd gui && npx tsc --noEmit`
Expected: no errors.

- [ ] **Step 4: Commit**

```bash
git add gui/src/lib/api/browse.ts gui/src/lib/tauri.ts
git commit -m "$(cat <<'EOF'
feat(gui): TS wrapper for list_messages_cmd

Wire types + invoke() wrapper for the new browse endpoint. Re-exported
from tauri.ts so stores import from the same surface as the other
HTTP wrappers.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 11: `mail` store — switch initial load to `/v1/messages` + add `loadMoreMessages`

**Files:**
- Modify: `gui/src/lib/stores/mail.svelte.ts`
- Modify: `gui/src/lib/stores/mail.test.ts`

- [ ] **Step 1: Write the failing tests**

In `gui/src/lib/stores/mail.test.ts`, add (preserve existing tests):

```ts
import { describe, expect, it, vi, beforeEach } from "vitest";

import { mail } from "./mail.svelte";

// Pull in the module so we can spy on its tauri wrappers. The store imports
// from ../tauri; tests substitute via vi.mock at the top of the file (see
// existing test setup pattern in this file).

// --- new tests for paginated browse ---

describe("loadInitialMessages", () => {
  beforeEach(() => {
    mail.reset();
    vi.restoreAllMocks();
  });

  it("populates messages from /v1/messages and sets messagesHasMore from next_cursor", async () => {
    const tauri = await import("../tauri");
    vi.spyOn(tauri, "listMessages").mockResolvedValue({
      messages: [
        { message_id: "1", subject: "a", from: { address: null, name: null },
          date: null, account: { id: "1", name: "x" } },
      ],
      next_cursor: "cur-1",
    });
    await mail.loadInitialMessages();
    const snap = mail.snapshot;
    expect(snap.messages.map((m) => m.message_id)).toEqual(["1"]);
    expect(mail.messagesCursor).toBe("cur-1");
    expect(mail.messagesHasMore).toBe(true);
  });

  it("sets messagesHasMore=false when next_cursor is null", async () => {
    const tauri = await import("../tauri");
    vi.spyOn(tauri, "listMessages").mockResolvedValue({
      messages: [], next_cursor: null,
    });
    await mail.loadInitialMessages();
    expect(mail.messagesHasMore).toBe(false);
    expect(mail.messagesCursor).toBeNull();
  });
});

describe("loadMoreMessages", () => {
  beforeEach(() => {
    mail.reset();
    vi.restoreAllMocks();
  });

  it("appends results and advances cursor", async () => {
    const tauri = await import("../tauri");
    const spy = vi.spyOn(tauri, "listMessages")
      .mockResolvedValueOnce({
        messages: [
          { message_id: "1", subject: "a", from: { address: null, name: null },
            date: null, account: { id: "1", name: "x" } },
        ],
        next_cursor: "cur-1",
      })
      .mockResolvedValueOnce({
        messages: [
          { message_id: "2", subject: "b", from: { address: null, name: null },
            date: null, account: { id: "1", name: "x" } },
        ],
        next_cursor: null,
      });
    await mail.loadInitialMessages();
    await mail.loadMoreMessages();
    expect(spy).toHaveBeenCalledTimes(2);
    expect(mail.snapshot.messages.map((m) => m.message_id)).toEqual(["1", "2"]);
    expect(mail.messagesHasMore).toBe(false);
  });

  it("is a no-op when messagesHasMore is false", async () => {
    const tauri = await import("../tauri");
    const spy = vi.spyOn(tauri, "listMessages")
      .mockResolvedValue({ messages: [], next_cursor: null });
    await mail.loadInitialMessages();      // sets hasMore=false
    spy.mockClear();
    await mail.loadMoreMessages();          // should not fire
    expect(spy).not.toHaveBeenCalled();
  });

  it("two concurrent calls fire one network request", async () => {
    const tauri = await import("../tauri");
    const spy = vi.spyOn(tauri, "listMessages")
      .mockResolvedValueOnce({
        messages: [], next_cursor: "cur-1",
      })
      .mockResolvedValue({
        messages: [{ message_id: "9", subject: "z",
                     from: { address: null, name: null }, date: null,
                     account: { id: "1", name: "x" } }],
        next_cursor: null,
      });
    await mail.loadInitialMessages();
    spy.mockClear();
    spy.mockResolvedValue({
      messages: [{ message_id: "9", subject: "z",
                   from: { address: null, name: null }, date: null,
                   account: { id: "1", name: "x" } }],
      next_cursor: null,
    });
    await Promise.all([mail.loadMoreMessages(), mail.loadMoreMessages()]);
    expect(spy).toHaveBeenCalledTimes(1);
  });

  it("re-uses the current filter opts on subsequent loadMore calls", async () => {
    const tauri = await import("../tauri");
    const spy = vi.spyOn(tauri, "listMessages")
      .mockResolvedValueOnce({
        messages: [{ message_id: "1", subject: "a",
          from: { address: null, name: null }, date: null,
          account: { id: "7", name: "scoped" } }],
        next_cursor: "cur-1",
      })
      .mockResolvedValueOnce({
        messages: [], next_cursor: null,
      });
    await mail.loadInitialMessages({ accountIds: ["7"] });
    await mail.loadMoreMessages();
    expect(spy).toHaveBeenLastCalledWith({
      account_ids: ["7"], folder_ids: [], limit: 50, cursor: "cur-1",
    });
  });
});

describe("setSelection refetches from /v1/messages", () => {
  beforeEach(() => {
    mail.reset();
    vi.restoreAllMocks();
  });

  it("calls listMessages with the selected account's id", async () => {
    const tauri = await import("../tauri");
    const spy = vi.spyOn(tauri, "listMessages").mockResolvedValue({
      messages: [], next_cursor: null,
    });
    mail.setSelection({ kind: "account", accountId: "42" });
    // setSelection fires the load without awaiting; flush microtasks.
    await Promise.resolve();
    await Promise.resolve();
    expect(spy).toHaveBeenCalledWith({
      account_ids: ["42"], folder_ids: [], limit: 50, cursor: null,
    });
  });

  it("calls listMessages with both account_id and folder_id when a folder is selected", async () => {
    const tauri = await import("../tauri");
    const spy = vi.spyOn(tauri, "listMessages").mockResolvedValue({
      messages: [], next_cursor: null,
    });
    mail.setSelection({ kind: "folder", accountId: "42", folderId: "9" });
    await Promise.resolve();
    await Promise.resolve();
    expect(spy).toHaveBeenCalledWith({
      account_ids: ["42"], folder_ids: ["9"], limit: 50, cursor: null,
    });
  });

  it("is a no-op when the selection is unchanged", async () => {
    const tauri = await import("../tauri");
    const spy = vi.spyOn(tauri, "listMessages").mockResolvedValue({
      messages: [], next_cursor: null,
    });
    mail.setSelection({ kind: "account", accountId: "42" });
    await Promise.resolve(); await Promise.resolve();
    spy.mockClear();
    mail.setSelection({ kind: "account", accountId: "42" });
    await Promise.resolve(); await Promise.resolve();
    expect(spy).not.toHaveBeenCalled();
  });
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd gui && npx vitest run src/lib/stores/mail.test.ts`
Expected: FAIL — `loadInitialMessages`/`loadMoreMessages` don't exist; `messagesCursor` / `messagesHasMore` are not on the snapshot.

- [ ] **Step 3: Modify the store**

In `gui/src/lib/stores/mail.svelte.ts`:

3a. Add the import `listMessages` and update the existing imports block:

```ts
import { getChanges } from "../api/changes";
import { formatError } from "../format_error";
import { POLL_INTERVAL_MS, dedupNewMessages, parseCursor } from "../change_poller";
import {
  getMessage,
  listAccounts,
  listFolders,
  listMessages,
  type AccountSummary,
  type FolderSummary,
  type MessageDetail,
  type MessageSummary,
  type Selection,
} from "../tauri";
```

3b. Add state fields:

```ts
export interface MailState {
  accounts: AccountSummary[];
  folders: Map<string, FolderSummary[]>;
  messages: MessageSummary[];
  selection: Selection;
  selectedMessage: MessageDetail | null;
  loadingMessages: boolean;
  loadingMore: boolean;
  loadingDetail: boolean;
  errorMessage: string | null;
  bodyMode: "html" | "plain" | "raw";
  externalImagesAllowed: boolean;
}

function initialState(): MailState {
  return {
    accounts: [],
    folders: new Map(),
    messages: [],
    selection: { kind: "all" },
    selectedMessage: null,
    loadingMessages: false,
    loadingMore: false,
    loadingDetail: false,
    errorMessage: null,
    bodyMode: "html",
    externalImagesAllowed: false,
  };
}
```

3c. Add private cursor + hasMore + reentrancy + last-used filter opts:

```ts
class MailStore {
  #state: MailState = $state(initialState());
  #changeCursor: string | null = null;
  #messagesCursor: string | null = null;
  #messagesHasMore: boolean = false;
  #loadMoreInFlight: Promise<void> | null = null;
  // Filter opts the *current* page set was fetched with; loadMoreMessages
  // re-uses them so a paginated browse stays scoped to the same selection.
  #currentFilterOpts: { accountIds: string[]; folderIds: string[] } = {
    accountIds: [], folderIds: [],
  };
  #pollHandle: ReturnType<typeof setInterval> | null = null;
  #pollFailureCount: number = 0;
```

3d. Add getters:

```ts
  get messagesCursor(): string | null { return this.#messagesCursor; }
  get messagesHasMore(): boolean { return this.#messagesHasMore; }
```

3e. Replace `loadRecentMessages` with `loadInitialMessages` (keep the old name as an alias for the screen import-site to keep working — or update the call site; this task takes the latter route in the next step):

```ts
  async loadInitialMessages(opts?: {
    accountIds?: string[]; folderIds?: string[];
  }): Promise<void> {
    this.#state.loadingMessages = true;
    this.#state.errorMessage = null;
    this.#state.messages = [];
    this.#messagesCursor = null;
    this.#messagesHasMore = false;
    this.#currentFilterOpts = {
      accountIds: opts?.accountIds ?? [],
      folderIds: opts?.folderIds ?? [],
    };
    try {
      const resp = await listMessages({
        account_ids: this.#currentFilterOpts.accountIds,
        folder_ids: this.#currentFilterOpts.folderIds,
        limit: 50,
        cursor: null,
      });
      this.#state.messages = resp.messages;
      this.#messagesCursor = resp.next_cursor;
      this.#messagesHasMore = resp.next_cursor !== null;
    } catch (err: unknown) {
      this.#state.errorMessage = formatError(err);
    } finally {
      this.#state.loadingMessages = false;
    }
  }

  async loadMoreMessages(): Promise<void> {
    if (!this.#messagesHasMore || this.#messagesCursor === null) return;
    if (this.#loadMoreInFlight) {
      // Coalesce concurrent calls onto a single in-flight request.
      return this.#loadMoreInFlight;
    }
    const cursor = this.#messagesCursor;
    this.#state.loadingMore = true;
    const promise = (async () => {
      try {
        const resp = await listMessages({
          account_ids: this.#currentFilterOpts.accountIds,
          folder_ids: this.#currentFilterOpts.folderIds,
          limit: 50,
          cursor,
        });
        this.#state.messages = [...this.#state.messages, ...resp.messages];
        this.#messagesCursor = resp.next_cursor;
        this.#messagesHasMore = resp.next_cursor !== null;
      } catch (err: unknown) {
        this.#state.errorMessage = formatError(err);
      } finally {
        this.#state.loadingMore = false;
        this.#loadMoreInFlight = null;
      }
    })();
    this.#loadMoreInFlight = promise;
    return promise;
  }
```

3f1. Update `setSelection` to refetch from the server when the selection
narrows or widens. This is the spec's "pagination traverses the *filtered*
set" requirement — without it, the client-side `visible` filter in
`MessageList` would still hide rows but couldn't reveal more of them
because the cursor would be paging through an unfiltered set.

```ts
  setSelection(sel: Selection): void {
    // No-op for repeated selection of the same target.
    if (selectionsEqual(this.#state.selection, sel)) return;
    this.#state.selection = sel;
    const opts = selectionToFilterOpts(sel);
    void this.loadInitialMessages(opts);
  }
```

Add the two pure helpers near the top of the module (above the class):

```ts
function selectionsEqual(a: Selection, b: Selection): boolean {
  if (a.kind !== b.kind) return false;
  if (a.kind === "all" && b.kind === "all") return true;
  if (a.kind === "account" && b.kind === "account") return a.accountId === b.accountId;
  if (a.kind === "folder" && b.kind === "folder") {
    return a.accountId === b.accountId && a.folderId === b.folderId;
  }
  return false;
}

function selectionToFilterOpts(sel: Selection): {
  accountIds: string[]; folderIds: string[];
} {
  if (sel.kind === "all") return { accountIds: [], folderIds: [] };
  if (sel.kind === "account") return { accountIds: [sel.accountId], folderIds: [] };
  return { accountIds: [sel.accountId], folderIds: [sel.folderId] };
}
```

3f. Keep `loadRecentMessages` as a thin compatibility shim *only if* the old name is still referenced from other components; otherwise rename call sites. Run:

```
grep -rn "loadRecentMessages" gui/src/
```

Update every call site to `loadInitialMessages`. The known site is `gui/src/screens/MainView.svelte:94` — replace `mail.loadRecentMessages()` with `mail.loadInitialMessages()`.

3g. Remove `MAX_RECENT_MESSAGES` enforcement from any append path (the import + the prepend cap inside `mergeNewMessages` stay for the *pending buffer* in the next task, but the appended array is no longer capped).

For now, leave the export and `mergeNewMessages` alone — Task 13 reworks `pollOnce` and that buffer.

- [ ] **Step 4: Run vitest to confirm pass**

Run: `cd gui && npx vitest run src/lib/stores/mail.test.ts`
Expected: PASS for the 5 new tests + existing tests. Fix any existing test that breaks because `loadRecentMessages` is renamed (search the test file and update).

- [ ] **Step 5: Commit**

```bash
git add gui/src/lib/stores/mail.svelte.ts gui/src/lib/stores/mail.test.ts \
        gui/src/screens/MainView.svelte
git commit -m "$(cat <<'EOF'
feat(gui/mail): keyset-paginated browse via /v1/messages

Initial load now fetches /v1/messages (was /v1/changes initial-fetch).
loadMoreMessages appends the next page and advances the cursor; the
in-flight promise is coalesced so concurrent IntersectionObserver
firings don't double-fetch.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 12: `mail` store — pending-new buffer + merge

**Files:**
- Modify: `gui/src/lib/stores/mail.svelte.ts`
- Modify: `gui/src/lib/stores/mail.test.ts`

- [ ] **Step 1: Write the failing tests**

Append to `gui/src/lib/stores/mail.test.ts`:

```ts
describe("pendingNewMessages buffer", () => {
  beforeEach(() => {
    mail.reset();
    vi.restoreAllMocks();
  });

  it("pollOnce pushes into pendingNewMessages, not messages", async () => {
    const tauri = await import("../tauri");
    vi.spyOn(tauri, "listMessages").mockResolvedValue({
      messages: [], next_cursor: null,
    });
    await mail.loadInitialMessages();
    const changes = await import("../api/changes");
    vi.spyOn(changes, "getChanges").mockResolvedValue({
      new_messages: [
        { message_id: "10", subject: "fresh",
          from: { address: null, name: null }, date: null,
          account: { id: "1", name: "x" } },
      ],
      next_cursor: "11",
    });
    await mail.pollOnce();
    expect(mail.snapshot.messages).toHaveLength(0);
    expect(mail.snapshot.pendingNewMessages).toHaveLength(1);
  });

  it("dedups against both messages and pendingNewMessages", async () => {
    const tauri = await import("../tauri");
    vi.spyOn(tauri, "listMessages").mockResolvedValue({
      messages: [
        { message_id: "5", subject: "old",
          from: { address: null, name: null }, date: null,
          account: { id: "1", name: "x" } },
      ],
      next_cursor: null,
    });
    await mail.loadInitialMessages();
    const changes = await import("../api/changes");
    vi.spyOn(changes, "getChanges").mockResolvedValue({
      new_messages: [
        // Same as the one already in messages — must NOT appear in pending.
        { message_id: "5", subject: "old",
          from: { address: null, name: null }, date: null,
          account: { id: "1", name: "x" } },
        { message_id: "10", subject: "fresh",
          from: { address: null, name: null }, date: null,
          account: { id: "1", name: "x" } },
      ],
      next_cursor: "11",
    });
    await mail.pollOnce();
    expect(mail.snapshot.pendingNewMessages.map((m) => m.message_id)).toEqual(["10"]);
  });

  it("mergePendingNewMessages prepends and clears", async () => {
    const tauri = await import("../tauri");
    vi.spyOn(tauri, "listMessages").mockResolvedValue({
      messages: [
        { message_id: "5", subject: "old",
          from: { address: null, name: null }, date: null,
          account: { id: "1", name: "x" } },
      ],
      next_cursor: null,
    });
    await mail.loadInitialMessages();
    const changes = await import("../api/changes");
    vi.spyOn(changes, "getChanges").mockResolvedValue({
      new_messages: [
        { message_id: "10", subject: "fresh",
          from: { address: null, name: null }, date: null,
          account: { id: "1", name: "x" } },
      ],
      next_cursor: "11",
    });
    await mail.pollOnce();
    mail.mergePendingNewMessages();
    expect(mail.snapshot.messages.map((m) => m.message_id)).toEqual(["10", "5"]);
    expect(mail.snapshot.pendingNewMessages).toEqual([]);
  });
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd gui && npx vitest run src/lib/stores/mail.test.ts`
Expected: FAIL — `pendingNewMessages` not on snapshot; `mergePendingNewMessages` doesn't exist.

- [ ] **Step 3: Add `pendingNewMessages` and rework `pollOnce`**

In `gui/src/lib/stores/mail.svelte.ts`:

3a. Extend `MailState` + `initialState`:

```ts
export interface MailState {
  // ... existing fields ...
  pendingNewMessages: MessageSummary[];
}

function initialState(): MailState {
  return {
    // ... existing fields ...
    pendingNewMessages: [],
  };
}
```

3b. Soft cap constant near the top of the file (replace the existing `MAX_RECENT_MESSAGES` docstring; keep the export name for backward compat or rename — I'll rename):

```ts
// Soft cap for the pendingNewMessages buffer. /v1/changes prepends fresh
// items into this buffer (not directly into `messages`); the banner shows
// `pendingNewMessages.length`. Cap prevents an unattended tab from growing
// the buffer unboundedly while idle.
export const MAX_PENDING_NEW_MESSAGES = 500;
```

Remove the `MAX_RECENT_MESSAGES` export and any reference to it (it was only used inside the old `loadRecentMessages` / `mergeNewMessages` paths — search the file for residual uses and delete them).

3c. Replace `mergeNewMessages` and `pollOnce`:

```ts
  /**
   * Add fresh polled messages to the pending buffer (banner-driven).
   * Dedups against both `messages` and `pendingNewMessages`. Returns the
   * number of items appended to the buffer.
   */
  mergePendingNewMessages_internal(incoming: readonly MessageSummary[]): number {
    const seen = new Set<string>();
    for (const m of this.#state.messages) seen.add(m.message_id);
    for (const m of this.#state.pendingNewMessages) seen.add(m.message_id);
    const fresh = incoming.filter((m) => !seen.has(m.message_id));
    if (fresh.length === 0) return 0;
    const merged = [...fresh, ...this.#state.pendingNewMessages];
    this.#state.pendingNewMessages =
      merged.length > MAX_PENDING_NEW_MESSAGES
        ? merged.slice(0, MAX_PENDING_NEW_MESSAGES)
        : merged;
    return fresh.length;
  }

  /**
   * Move the pending buffer into the visible list, clearing the banner.
   */
  mergePendingNewMessages(): void {
    if (this.#state.pendingNewMessages.length === 0) return;
    this.#state.messages = [
      ...this.#state.pendingNewMessages, ...this.#state.messages,
    ];
    this.#state.pendingNewMessages = [];
  }

  async pollOnce(): Promise<void> {
    try {
      const resp = await getChanges(this.#changeCursor);
      this.#changeCursor = parseCursor(resp.next_cursor) ?? this.#changeCursor;
      this.mergePendingNewMessages_internal(resp.new_messages);
      this.#pollFailureCount = 0;
    } catch (err: unknown) {
      this.#state.errorMessage = formatError(err);
      this.#pollFailureCount += 1;
      if (this.#pollFailureCount >= MAX_POLL_FAILURES && this.#pollHandle !== null) {
        this.stopPolling();
        this.#state.errorMessage = `polling stopped after ${MAX_POLL_FAILURES} consecutive failures (last: ${formatError(err)})`;
      }
    }
  }
```

Remove the old public `mergeNewMessages` method.

- [ ] **Step 4: Run vitest**

Run: `cd gui && npx vitest run src/lib/stores/mail.test.ts`
Expected: PASS (new + existing tests). Update any existing test that referenced `mergeNewMessages` to call `mergePendingNewMessages` or assert against `pendingNewMessages` (the legacy `prepend` behaviour is gone).

- [ ] **Step 5: Run the full GUI test suite to catch unrelated breakage**

Run: `cd gui && npx vitest run`
Expected: PASS. If `MessageList.test.ts` asserts on the prior auto-prepend behaviour, defer the fix to Task 14 where MessageList is reworked.

- [ ] **Step 6: Commit**

```bash
git add gui/src/lib/stores/mail.svelte.ts gui/src/lib/stores/mail.test.ts
git commit -m "$(cat <<'EOF'
feat(gui/mail): banner-driven pending-new-messages buffer

pollOnce stops auto-prepending. New polled messages land in
pendingNewMessages (deduped against both messages and pending);
mergePendingNewMessages prepends them on user action and clears the
banner state.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 13: `search` store — cursor + `loadMore` + 409 recovery

**Files:**
- Modify: `gui/src/lib/stores/search.svelte.ts`
- Modify: `gui/src/lib/stores/search.test.ts`

- [ ] **Step 1: Identify how 409 surfaces in the TS client**

Run: `grep -rn "409\|SEARCH_CURSOR\|problem" gui/src/ | head -20`

The Rust `http_post_json` likely turns non-2xx into an `Err(AuthError::HttpError(...))` carrying status + body. Verify with:

Run: `grep -n "HttpError\|status\|problem" gui/src-tauri/src/http/client.rs 2>/dev/null | head -20`

We'll match the existing convention. The 409 problem+json body has `{"type": "/problems/search-cursor-expired", ...}`. The Rust layer surfaces the body string to JS via `AuthError`, and the TS `formatError` already extracts it.

For the TS recovery branch, the simplest predicate is: when the thrown error's serialised body contains `"/problems/search-cursor-expired"`, run the recovery. Wrap it in a helper `isSearchCursorExpired(err: unknown): boolean`.

- [ ] **Step 2: Add a tiny helper for the error predicate**

In `gui/src/lib/format_error.ts` (or a new sibling — pick the existing module by reading it first):

Run: `cat gui/src/lib/format_error.ts`

If the file is small, append the helper there. Otherwise create `gui/src/lib/search_cursor_expired.ts`:

```ts
/**
 * Returns true when `err` carries the /problems/search-cursor-expired
 * problem-type from the server. Used by the search store to drive the
 * transparent re-submit + drop-and-append recovery path.
 *
 * The Rust side surfaces the response body as a string in AuthError /
 * formatError output; substring match against the canonical type URI is
 * sufficient (we control both sides of the wire).
 */
export function isSearchCursorExpired(err: unknown): boolean {
  const text = typeof err === "string" ? err :
               (err && typeof err === "object" && "message" in err
                  ? String((err as { message: unknown }).message)
                  : String(err));
  return text.includes("/problems/search-cursor-expired");
}
```

Add a quick unit test `gui/src/lib/search_cursor_expired.test.ts`:

```ts
import { describe, expect, it } from "vitest";

import { isSearchCursorExpired } from "./search_cursor_expired";

describe("isSearchCursorExpired", () => {
  it("matches when error message contains the problem type", () => {
    expect(isSearchCursorExpired(
      new Error('{"type":"/problems/search-cursor-expired"}'),
    )).toBe(true);
  });
  it("matches plain strings", () => {
    expect(isSearchCursorExpired(
      "server returned 409 /problems/search-cursor-expired",
    )).toBe(true);
  });
  it("returns false for unrelated errors", () => {
    expect(isSearchCursorExpired(new Error("network unreachable"))).toBe(false);
  });
});
```

Run: `cd gui && npx vitest run src/lib/search_cursor_expired.test.ts`
Expected: PASS

- [ ] **Step 3: Write the failing tests for `loadMore` + recovery**

Append to `gui/src/lib/stores/search.test.ts`:

```ts
import { search } from "./search.svelte";

describe("search.loadMore", () => {
  beforeEach(() => {
    search.reset();
    vi.restoreAllMocks();
  });

  it("appends results and advances cursor", async () => {
    const tauri = await import("../tauri");
    const r1 = (id: string) => ({
      message_id: id, account: { id: "1", name: null }, folder: null,
      subject: id, from: { address: null, name: null }, to: [], date: null,
      snippet_html: null, has_attachments: false, score: 1, matched_arms: [],
    });
    vi.spyOn(tauri, "runSearch")
      .mockResolvedValueOnce({
        results: [r1("1"), r1("2")], next_cursor: "tok:2",
        total_estimate: null, took_ms: 1,
      })
      .mockResolvedValueOnce({
        results: [r1("3")], next_cursor: null,
        total_estimate: null, took_ms: 1,
      });
    search.setQuery("hello");
    await search.submit();
    await search.loadMore();
    expect(search.snapshot.results.map((r) => r.message_id))
      .toEqual(["1", "2", "3"]);
  });

  it("is a no-op when hasMore is false", async () => {
    const tauri = await import("../tauri");
    const spy = vi.spyOn(tauri, "runSearch").mockResolvedValue({
      results: [], next_cursor: null, total_estimate: null, took_ms: 1,
    });
    search.setQuery("hello");
    await search.submit();
    spy.mockClear();
    await search.loadMore();
    expect(spy).not.toHaveBeenCalled();
  });

  it("on 409 cursor-expired, re-submits and drops prior count", async () => {
    const tauri = await import("../tauri");
    const r1 = (id: string) => ({
      message_id: id, account: { id: "1", name: null }, folder: null,
      subject: id, from: { address: null, name: null }, to: [], date: null,
      snippet_html: null, has_attachments: false, score: 1, matched_arms: [],
    });
    vi.spyOn(tauri, "runSearch")
      .mockResolvedValueOnce({
        // initial submit: 2 results, cursor present
        results: [r1("1"), r1("2")], next_cursor: "tok:2",
        total_estimate: null, took_ms: 1,
      })
      // loadMore: server says cursor expired
      .mockRejectedValueOnce(new Error(
        '{"type":"/problems/search-cursor-expired","detail":"gone"}',
      ))
      // transparent re-submit: 4 results (pool may differ in size)
      .mockResolvedValueOnce({
        results: [r1("1"), r1("2"), r1("3"), r1("4")], next_cursor: "tok2:2",
        total_estimate: null, took_ms: 1,
      });
    search.setQuery("hello");
    await search.submit();
    await search.loadMore();
    expect(search.snapshot.results.map((r) => r.message_id))
      .toEqual(["1", "2", "3", "4"]);
  });

  it("on 409 when re-submitted pool is smaller, falls back to full reset", async () => {
    const tauri = await import("../tauri");
    const r1 = (id: string) => ({
      message_id: id, account: { id: "1", name: null }, folder: null,
      subject: id, from: { address: null, name: null }, to: [], date: null,
      snippet_html: null, has_attachments: false, score: 1, matched_arms: [],
    });
    vi.spyOn(tauri, "runSearch")
      .mockResolvedValueOnce({
        results: [r1("1"), r1("2"), r1("3")], next_cursor: "tok:2",
        total_estimate: null, took_ms: 1,
      })
      .mockRejectedValueOnce(new Error(
        '{"type":"/problems/search-cursor-expired"}',
      ))
      .mockResolvedValueOnce({
        results: [r1("9")], next_cursor: null,
        total_estimate: null, took_ms: 1,
      });
    search.setQuery("hello");
    await search.submit();
    await search.loadMore();
    // New pool was smaller than prior count → full reset (just the new row).
    expect(search.snapshot.results.map((r) => r.message_id)).toEqual(["9"]);
  });
});
```

- [ ] **Step 4: Run tests to verify they fail**

Run: `cd gui && npx vitest run src/lib/stores/search.test.ts`
Expected: FAIL — `loadMore` doesn't exist; cursor is not tracked.

- [ ] **Step 5: Extend the store**

In `gui/src/lib/stores/search.svelte.ts`:

5a. Imports:

```ts
import { isSearchCursorExpired } from "../search_cursor_expired";
```

5b. State + initialState:

```ts
export interface SearchState {
  query: string;
  filters: SearchFiltersUI;
  sort: SortMode;
  results: SearchResultRow[];
  cursor: string | null;
  hasMore: boolean;
  tookMs: number | null;
  loading: boolean;
  loadingMore: boolean;
  errorMessage: string | null;
}

function initialState(): SearchState {
  return {
    query: "",
    filters: emptyFilters(),
    sort: "rank",
    results: [],
    cursor: null,
    hasMore: false,
    tookMs: null,
    loading: false,
    loadingMore: false,
    errorMessage: null,
  };
}
```

5c. Update `submit` to store `cursor` + `hasMore`:

```ts
  async submit(): Promise<void> {
    const seq = ++this.#submitSeq;
    this.#state.loading = true;
    this.#state.errorMessage = null;
    try {
      const resp = await runSearch({
        query: this.#state.query,
        filters: filtersUiToWire(this.#state.filters),
        limit: DEFAULT_LIMIT,
        cursor: null,
        sort: this.#state.sort,
      });
      if (seq !== this.#submitSeq) return;
      this.#state.results = resp.results;
      this.#state.tookMs = resp.took_ms;
      this.#state.cursor = resp.next_cursor;
      this.#state.hasMore = resp.next_cursor !== null;
    } catch (err: unknown) {
      if (seq !== this.#submitSeq) return;
      this.#state.results = [];
      this.#state.tookMs = null;
      this.#state.cursor = null;
      this.#state.hasMore = false;
      this.#state.errorMessage = formatError(err);
    } finally {
      if (seq === this.#submitSeq) this.#state.loading = false;
    }
  }
```

5d. New `loadMore`:

```ts
  async loadMore(): Promise<void> {
    if (!this.#state.hasMore || this.#state.cursor === null) return;
    if (this.#state.loadingMore) return;
    const seq = this.#submitSeq;
    const cursor = this.#state.cursor;
    const priorCount = this.#state.results.length;
    this.#state.loadingMore = true;
    try {
      let resp;
      try {
        resp = await runSearch({
          query: this.#state.query,
          filters: filtersUiToWire(this.#state.filters),
          limit: DEFAULT_LIMIT,
          cursor,
          sort: this.#state.sort,
        });
      } catch (err: unknown) {
        if (!isSearchCursorExpired(err)) throw err;
        // Transparent recovery: re-submit without cursor, drop the rows
        // the user already has, append the remainder.
        const fresh = await runSearch({
          query: this.#state.query,
          filters: filtersUiToWire(this.#state.filters),
          limit: DEFAULT_LIMIT,
          cursor: null,
          sort: this.#state.sort,
        });
        if (seq !== this.#submitSeq) return;
        if (fresh.results.length <= priorCount) {
          // New pool is smaller — full reset (the filter context changed
          // underneath us; safest to start over).
          this.#state.results = fresh.results;
        } else {
          this.#state.results = [
            ...this.#state.results,
            ...fresh.results.slice(priorCount),
          ];
        }
        this.#state.cursor = fresh.next_cursor;
        this.#state.hasMore = fresh.next_cursor !== null;
        return;
      }
      if (seq !== this.#submitSeq) return;
      this.#state.results = [...this.#state.results, ...resp.results];
      this.#state.cursor = resp.next_cursor;
      this.#state.hasMore = resp.next_cursor !== null;
    } catch (err: unknown) {
      if (seq !== this.#submitSeq) return;
      this.#state.errorMessage = formatError(err);
    } finally {
      this.#state.loadingMore = false;
    }
  }
```

- [ ] **Step 6: Run vitest**

Run: `cd gui && npx vitest run src/lib/stores/search.test.ts`
Expected: PASS for all tests. If `__setSearchResultsForTest` callers in other test files rely on the prior `SearchState` shape, update them to include `cursor: null, hasMore: false, loadingMore: false`.

- [ ] **Step 7: Commit**

```bash
git add gui/src/lib/stores/search.svelte.ts gui/src/lib/stores/search.test.ts \
        gui/src/lib/search_cursor_expired.ts gui/src/lib/search_cursor_expired.test.ts
git commit -m "$(cat <<'EOF'
feat(gui/search): cursor + loadMore with transparent 409 recovery

submit() captures next_cursor; loadMore() appends. On 409 cursor-
expired (server-side cache eviction), the store re-runs the query
without a cursor, drops the rows the user already has, and appends
the rest. Smaller-pool fallback resets cleanly.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 14: `MessageList.svelte` — sentinel, "Load more" button, "N new messages" banner

**Files:**
- Modify: `gui/src/components/MessageList.svelte`
- Modify: `gui/src/components/MessageList.test.ts`

- [ ] **Step 1: Read the current component end-to-end first**

Run: `cat gui/src/components/MessageList.svelte`

Familiarise with the existing `searchActive` derivation; the new code follows the same `$derived` pattern.

- [ ] **Step 2: Write failing component tests**

Append to `gui/src/components/MessageList.test.ts`:

```ts
import { render, screen, fireEvent } from "@testing-library/svelte";

import MessageList from "./MessageList.svelte";
import { mail } from "../lib/stores/mail.svelte";
import { search } from "../lib/stores/search.svelte";

describe("MessageList — pagination affordances", () => {
  beforeEach(() => {
    mail.reset();
    search.reset();
  });

  it("renders a Load more button when mail has more pages", async () => {
    // Seed the store so messagesHasMore=true. The simplest path is to call
    // loadInitialMessages with a mocked listMessages that returns a cursor.
    const tauri = await import("../lib/tauri");
    vi.spyOn(tauri, "listMessages").mockResolvedValue({
      messages: [
        { message_id: "1", subject: "a",
          from: { address: null, name: null }, date: null,
          account: { id: "1", name: "x" } },
      ],
      next_cursor: "tok",
    });
    await mail.loadInitialMessages();
    render(MessageList);
    expect(screen.getByRole("button", { name: /load more/i })).toBeTruthy();
  });

  it("Load more button fires mail.loadMoreMessages when not searching", async () => {
    const tauri = await import("../lib/tauri");
    vi.spyOn(tauri, "listMessages")
      .mockResolvedValueOnce({
        messages: [
          { message_id: "1", subject: "a",
            from: { address: null, name: null }, date: null,
            account: { id: "1", name: "x" } },
        ],
        next_cursor: "tok",
      })
      .mockResolvedValueOnce({
        messages: [
          { message_id: "2", subject: "b",
            from: { address: null, name: null }, date: null,
            account: { id: "1", name: "x" } },
        ],
        next_cursor: null,
      });
    await mail.loadInitialMessages();
    render(MessageList);
    const btn = screen.getByRole("button", { name: /load more/i });
    await fireEvent.click(btn);
    // After load: messages now has 2 entries.
    expect(mail.snapshot.messages).toHaveLength(2);
  });

  it("renders 'N new messages' banner when pendingNewMessages is non-empty", async () => {
    const tauri = await import("../lib/tauri");
    vi.spyOn(tauri, "listMessages").mockResolvedValue({
      messages: [], next_cursor: null,
    });
    await mail.loadInitialMessages();
    const changes = await import("../lib/api/changes");
    vi.spyOn(changes, "getChanges").mockResolvedValue({
      new_messages: [
        { message_id: "10", subject: "fresh",
          from: { address: null, name: null }, date: null,
          account: { id: "1", name: "x" } },
        { message_id: "11", subject: "fresh2",
          from: { address: null, name: null }, date: null,
          account: { id: "1", name: "x" } },
      ],
      next_cursor: "12",
    });
    await mail.pollOnce();
    render(MessageList);
    expect(screen.getByText(/2 new messages/i)).toBeTruthy();
  });

  it("clicking the banner merges pendingNewMessages and dismisses", async () => {
    const tauri = await import("../lib/tauri");
    vi.spyOn(tauri, "listMessages").mockResolvedValue({
      messages: [], next_cursor: null,
    });
    await mail.loadInitialMessages();
    const changes = await import("../lib/api/changes");
    vi.spyOn(changes, "getChanges").mockResolvedValue({
      new_messages: [
        { message_id: "10", subject: "fresh",
          from: { address: null, name: null }, date: null,
          account: { id: "1", name: "x" } },
      ],
      next_cursor: "11",
    });
    await mail.pollOnce();
    render(MessageList);
    await fireEvent.click(screen.getByText(/1 new message/i));
    expect(mail.snapshot.pendingNewMessages).toEqual([]);
    expect(mail.snapshot.messages).toHaveLength(1);
  });
});
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd gui && npx vitest run src/components/MessageList.test.ts`
Expected: FAIL — none of the new affordances exist in the component yet.

- [ ] **Step 4: Update `MessageList.svelte`**

Replace the existing `<section class="list">` template with the pagination-aware version. Keep the existing `searchActive` / `rows` / `visible` derivations as-is; add a sentinel + button + banner block.

```svelte
<script lang="ts">
  import { onMount, onDestroy } from "svelte";
  import MessageListRow from "./MessageListRow.svelte";
  import { mail } from "../lib/stores/mail.svelte";
  import { search } from "../lib/stores/search.svelte";

  let searchActive = $derived(search.snapshot.tookMs !== null);

  interface ListRow {
    id: string;
    subject: string | null;
    from: { address: string | null; name: string | null };
    date: string | null;
    account: { id: string; name: string | null };
    snippet: string | null;
  }

  let rows: ListRow[] = $derived(
    searchActive
      ? search.snapshot.results.map((r) => ({
          id: r.message_id,
          subject: r.subject,
          from: r.from,
          date: r.date,
          account: r.account,
          snippet: r.snippet_html,
        }))
      : mail.snapshot.messages.map((m) => ({
          id: m.message_id,
          subject: m.subject,
          from: m.from,
          date: m.date,
          account: m.account,
          snippet: null,
        })),
  );

  let visible = $derived(
    searchActive
      ? rows
      : rows.filter((r) => {
          const sel = mail.snapshot.selection;
          if (sel.kind === "all") return true;
          return r.account.id === sel.accountId;
        }),
  );

  let hasMore = $derived(
    searchActive ? search.snapshot.hasMore : mail.messagesHasMore,
  );

  let loadingMore = $derived(
    searchActive ? search.snapshot.loadingMore : mail.snapshot.loadingMore,
  );

  let pendingCount = $derived(mail.snapshot.pendingNewMessages.length);

  async function loadMore(): Promise<void> {
    if (searchActive) {
      await search.loadMore();
    } else {
      await mail.loadMoreMessages();
    }
  }

  function mergePending(): void {
    mail.mergePendingNewMessages();
  }

  async function openMessage(id: string): Promise<void> {
    await mail.openMessage(id);
  }

  // IntersectionObserver-driven auto-load on near-bottom scroll.
  let sentinel: HTMLDivElement | undefined = $state();
  let observer: IntersectionObserver | undefined;

  onMount(() => {
    if (sentinel && typeof IntersectionObserver !== "undefined") {
      observer = new IntersectionObserver(
        (entries) => {
          for (const e of entries) {
            if (e.isIntersecting && hasMore && !loadingMore) {
              void loadMore();
            }
          }
        },
        { rootMargin: "200px 0px" },
      );
      observer.observe(sentinel);
    }
  });

  onDestroy(() => {
    observer?.disconnect();
  });
</script>

<section class="list">
  {#if pendingCount > 0 && !searchActive}
    <button class="banner" onclick={mergePending}>
      {pendingCount === 1 ? "1 new message" : `${pendingCount} new messages`} — click to show
    </button>
  {/if}
  {#if searchActive}
    <div class="caption">
      Search took {Math.round(search.snapshot.tookMs ?? 0)} ms — {search.snapshot.results.length} result(s)
    </div>
  {/if}
  {#if search.snapshot.errorMessage}
    <div class="error">{search.snapshot.errorMessage}</div>
  {:else if mail.snapshot.errorMessage}
    <div class="error">{mail.snapshot.errorMessage}</div>
  {/if}
  {#if mail.snapshot.loadingMessages && !searchActive}
    <div class="hint">Loading…</div>
  {:else if visible.length === 0}
    {#if searchActive}
      <div class="hint">No matches.</div>
    {:else}
      <div class="hint">No messages.</div>
    {/if}
  {:else}
    {#each visible as r (r.id)}
      <MessageListRow
        subject={r.subject}
        from={r.from}
        date={r.date}
        account={r.account}
        snippet={r.snippet}
        selected={mail.snapshot.selectedMessage?.id === r.id}
        onSelect={() => openMessage(r.id)}
      />
    {/each}
    <div bind:this={sentinel}></div>
    <div class="more">
      {#if loadingMore}
        <span class="hint">Loading more…</span>
      {:else if hasMore}
        <button onclick={loadMore}>Load more</button>
      {:else}
        <span class="hint">End of list</span>
      {/if}
    </div>
  {/if}
</section>

<style>
  .list {
    height: 100%;
    overflow-y: auto;
    background: #fff;
    border-right: 1px solid #e5e5e5;
  }
  .hint {
    padding: 24px;
    text-align: center;
    color: #888;
    font-size: 13px;
  }
  .caption {
    padding: 4px 12px;
    color: #666;
    font-size: 12px;
  }
  .error {
    padding: 12px;
    color: #b00;
    font-size: 13px;
  }
  .banner {
    display: block;
    width: 100%;
    border: none;
    background: #e8f0fe;
    color: #1a73e8;
    padding: 8px 12px;
    font-size: 13px;
    cursor: pointer;
    text-align: center;
  }
  .more {
    padding: 12px;
    text-align: center;
  }
  .more button {
    padding: 6px 14px;
    font-size: 13px;
    cursor: pointer;
  }
</style>
```

- [ ] **Step 5: Run vitest**

Run: `cd gui && npx vitest run src/components/MessageList.test.ts`
Expected: PASS for the 4 new tests + existing tests.

- [ ] **Step 6: Run the full GUI test suite**

Run: `cd gui && npx vitest run`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add gui/src/components/MessageList.svelte gui/src/components/MessageList.test.ts
git commit -m "$(cat <<'EOF'
feat(gui): infinite-scroll + 'N new messages' banner in MessageList

Bottom sentinel (IntersectionObserver, rootMargin 200px) triggers
loadMore on the active store (search or mail). A visible 'Load more'
button shares the handler so the button still works when the observer
is unavailable. Banner shows pending poll arrivals; clicking merges.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 15: Manual smoke + final commit

**Files:**
- None (verification only)

- [ ] **Step 1: Run the full backend test suite**

Run: `unset VIRTUAL_ENV && uv run pytest -x`
Expected: PASS (or SKIPS for DB-less runs).

- [ ] **Step 2: Run the full GUI test suite**

Run: `cd gui && npx vitest run`
Expected: PASS

- [ ] **Step 3: Type-check the GUI**

Run: `cd gui && npx tsc --noEmit`
Expected: no errors.

- [ ] **Step 4: Manual UI smoke test — golden path**

Start the daemon + serve in one shell:

```bash
unset VIRTUAL_ENV && uv run localmail serve --bind 127.0.0.1 --port 8443
```

In another shell, start the Tauri dev:

```bash
cd gui && npm run tauri dev
```

Walk through:

1. Log in.
2. Confirm the initial list loads via `/v1/messages` (check the server logs for the request).
3. Scroll to the bottom — confirm the next page auto-loads (you should see another `/v1/messages?cursor=...` request hit the server).
4. Click "Load more" explicitly — confirm it fetches even when the observer would have fired.
5. Run a search that returns >50 results — scroll the result pane, confirm `/v1/search` is called with `cursor` set.
6. Manually invalidate the search cache (open a Python shell against the running server: `searcher._cache.invalidate(token)` — pick a token from the logs) and `loadMore` again; confirm the GUI transparently re-fetches and resumes without an error banner.
7. Trigger a poll by sending yourself a new email (or just wait `POLL_INTERVAL_MS`); confirm the banner appears at the top of the list instead of the row silently prepending.
8. Click the banner — confirm the new rows appear at the top.

If anything misbehaves, fix it before declaring the work done. Type-check and tests are *necessary* but not *sufficient* — the manual smoke is the only thing that verifies the UX actually feels right.

- [ ] **Step 5: If anything was fixed in step 4, commit**

```bash
git add -A
git commit -m "$(cat <<'EOF'
fix(pagination): manual-smoke fixups

<describe what you fixed>

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

If nothing was fixed, skip this step.

---

## Self-Review Notes

- **Spec coverage**: every spec section maps to at least one task.
  - Backend `/v1/messages` → Tasks 2, 3, 4, 5.
  - Backend `/v1/search` cursor + grow_pool + 409 → Tasks 6, 7, 8.
  - Config `candidates_per_arm_max` → Task 1.
  - GUI mail store cursor + buffer → Tasks 11, 12.
  - GUI search store cursor + 409 recovery → Task 13.
  - GUI MessageList sentinel + banner + button → Task 14.
  - Tauri layer (Rust cmd + TS wrapper) → Tasks 9, 10.
  - Manual smoke for UX validation → Task 15.

- **Out of scope (per spec, not in plan)**: virtualised render list, numbered pagination, `multipart/byteranges`. Fixing the pre-existing `list_recent_messages_cmd` polling cursor pass-through (Rust ignores the `since` arg) is also out of scope — the polling code path keeps working (slightly redundant re-fetches dedup'd client-side) and is a separate concern from "make pagination work".

- **Type-consistency**: `BrowseCursor.ts` / `BrowseCursor.id` and `SearchCursor.token` / `SearchCursor.page` are the only new dataclasses on the Python side; GUI types are `ListMessagesRequest`/`ListMessagesResponse` + the existing `MessageSummary`. No naming drift between tasks.
