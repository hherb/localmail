# Attachments by Message Index — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Serve attachment N of message M at `GET /v1/messages/{id}/attachments/{index}` (bytes) and `…/{index}/text` (paged extracted text), give `/v1/attachments/{sha256}/text` the same character paging, and announce it with `api_minor = 2`.

**Architecture:** A pure `text_window.py` owns character paging. `api/attachments.py` gains a message-ACL'd resolver (index → sha + that entry's filename) and a paged text accessor. The streaming machinery moves out of `serve/routes/attachments.py` into `serve/routes/blob_response.py` so both routers share it; each route keeps its own 304 check and file open.

**Tech Stack:** Python 3.13, FastAPI, psycopg 3, Postgres (jsonb, TOAST'd TEXT), pytest.

**Spec:** [docs/superpowers/specs/2026-09-17-message-attachment-index-design.md](../specs/2026-09-17-message-attachment-index-design.md) — read it first; this plan argues from it.

## Global Constraints

- Every new `.py` file starts with the two SPDX lines:
  `# SPDX-License-Identifier: AGPL-3.0-or-later` / `# Copyright (C) 2026 Horst Herb`.
- Run commands with `unset VIRTUAL_ENV && uv run …` (a stray `VIRTUAL_ENV` picks the wrong interpreter).
- **Run pytest in the foreground only.** One pytest session per test database is enforced; a backgrounded run blocks the next one for 600 s.
- The test DB is `localmail_test` on port 5532. Never point tests at `localmail`.
- **Never undo an experiment with `git checkout <file>`** — it discards uncommitted work in that file. Copy the file aside and restore the copy.
- `offset`/`limit` count **characters**. Omitting both returns the whole text, exactly as today.
- Bad `offset`/`limit`/`index` are **400 problem+json** (`/problems/validation-failed`), never FastAPI's 422.
- Every 404 from the resolver uses the one message `attachment {index} of message {message_id} not found`.
- `MAX_JSONB_INDEX = 2**31 - 1`; `MAX_TEXT_CHARS = 2**30`. Both carry the reason comment given in the spec.
- The logger in `blob_response.py` is `logging.getLogger("localmail.serve")` — **not** `__name__`.
- `api_minor` becomes `2`.
- Report failing tests **by name**. Never call a failure "pre-existing" without re-running it on `main`. Never run with `--tb=no` when judging failures.
- Commit messages end with `Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>`.

## File map

| File | Responsibility |
|---|---|
| Create `src/localmail/text_window.py` | Pure: `MAX_TEXT_CHARS`, `text_window_error`, `TextWindow`, `TextPage` |
| Modify `src/localmail/api/attachments.py` | `MAX_JSONB_INDEX`, `MessageAttachment`, `resolve_message_attachment`, `text_window_from_query`, `get_attachment_text_page`; `get_attachment_text` delegates |
| Create `src/localmail/serve/routes/blob_response.py` | The #32/#54/#58/#59 response rules: `not_modified`, `blob_response`, and the moved helpers |
| Modify `src/localmail/serve/routes/attachments.py` | Thin: `stream_blob` + paged `attachment_text` |
| Modify `src/localmail/serve/routes/messages.py` | Two new routes |
| Modify `src/localmail/serve/routes/version.py` | `API_MINOR = 2` |
| Tests | `test_text_window.py`, `test_api_attachment_text_page.py`, `test_api_message_attachment.py`, `test_serve_attachments_golden.py`, `test_serve_message_attachment_routes.py`, `test_attachment_index_acceptance.py`; edits to `test_serve_attachments_routes.py`, `test_serve_acl_routes.py`, `test_serve_version_route.py` |
| Docs | `README.md`, `CLAUDE.md` |

---

### Task 1: The pure text window

**Files:**
- Create: `src/localmail/text_window.py`
- Test: `tests/test_text_window.py`

**Interfaces:**
- Produces:
  - `MAX_TEXT_CHARS: int = 2**30`
  - `text_window_error(offset: int, limit: int | None) -> str | None`
  - `TextWindow(offset: int = 0, limit: int | None = None)` — frozen; `.sql_from -> int`, `.sql_for -> int | None`, `.page(text: str, total: int) -> TextPage`
  - `TextPage(text: str, offset: int, limit: int | None, total: int, next_offset: int | None)` — frozen; `.to_wire() -> dict[str, object]` with exactly the keys `text, offset, limit, total, next_offset`

- [ ] **Step 1: Write the failing tests**

`tests/test_text_window.py`:

```python
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Character paging of a stored text — the one rule both text routes use."""
from __future__ import annotations

import pytest

from localmail.text_window import (
    MAX_TEXT_CHARS,
    TextPage,
    TextWindow,
    text_window_error,
)


def test_a_default_window_is_the_whole_text() -> None:
    assert text_window_error(0, None) is None
    w = TextWindow()
    assert (w.offset, w.limit, w.sql_from, w.sql_for) == (0, None, 1, None)


@pytest.mark.parametrize(("offset", "limit", "expected"), [
    (-1, None, "offset must be >= 0, got -1"),
    (0, 0, "limit must be >= 1, got 0"),
    (0, -5, "limit must be >= 1, got -5"),
])
def test_refusals_are_worded(offset: int, limit: int | None, expected: str) -> None:
    assert text_window_error(offset, limit) == expected


def test_a_zero_limit_is_refused_because_a_client_would_never_advance() -> None:
    # A zero-length window answers next_offset == offset for any unfinished
    # text, so a client looping on next_offset would spin forever.
    assert text_window_error(3, 0) is not None


@pytest.mark.parametrize(("offset", "limit"), [(-1, None), (0, 0)])
def test_construction_refuses_what_the_rule_refuses(
    offset: int, limit: int | None,
) -> None:
    with pytest.raises(ValueError, match="must be >= "):
        TextWindow(offset=offset, limit=limit)


def test_sql_from_is_one_based() -> None:
    assert TextWindow(offset=0).sql_from == 1
    assert TextWindow(offset=7).sql_from == 8


def test_positions_are_clamped_below_int4() -> None:
    # substring() takes int4 positions; 2**31 is a 500 from Postgres. Every
    # offset at or past MAX_TEXT_CHARS is past the end of every text anyway.
    w = TextWindow(offset=2**40, limit=2**40)
    assert w.sql_from == MAX_TEXT_CHARS + 1
    assert w.sql_for == MAX_TEXT_CHARS
    assert w.sql_from <= 2**31 - 1


def test_the_clamp_leaves_ordinary_values_alone() -> None:
    w = TextWindow(offset=5, limit=20_000)
    assert (w.sql_from, w.sql_for) == (6, 20_000)


@pytest.mark.parametrize(("offset", "limit", "text", "total", "next_offset"), [
    (0, 2, "ab", 5, 2),          # first page
    (2, 2, "cd", 5, 4),          # middle page
    (4, 2, "e", 5, None),        # last, short page
    (0, 5, "abcde", 5, None),    # exact fit
    (10, 2, "", 5, None),        # past the end
    (0, None, "", 0, None),      # empty text
    (3, None, "de", 5, None),    # un-paged remainder
])
def test_page_arithmetic(
    offset: int, limit: int | None, text: str, total: int,
    next_offset: int | None,
) -> None:
    page = TextWindow(offset=offset, limit=limit).page(text, total)
    assert page == TextPage(
        text=text, offset=offset, limit=limit, total=total,
        next_offset=next_offset,
    )


def test_next_offset_counts_code_points_not_utf16_units() -> None:
    # U+1D11E is one code point (what Postgres and Python count) but two
    # UTF-16 units (what JavaScript's .length counts). A client computing
    # offset + text.length would skip a character on every such text.
    text = "a\U0001D11E"
    page = TextWindow(offset=0, limit=2).page(text, total=4)
    assert page.next_offset == 2
    assert len(text.encode("utf-16-le")) // 2 == 3


def test_to_wire_carries_exactly_the_five_keys() -> None:
    wire = TextWindow(offset=0, limit=2).page("ab", 5).to_wire()
    assert wire == {
        "text": "ab", "offset": 0, "limit": 2, "total": 5, "next_offset": 2,
    }
```

- [ ] **Step 2: Run to verify they fail**

Run: `unset VIRTUAL_ENV && uv run pytest -q tests/test_text_window.py`
Expected: collection error, `ModuleNotFoundError: No module named 'localmail.text_window'`.

- [ ] **Step 3: Implement**

`src/localmail/text_window.py`:

```python
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Paging a stored text by characters.

Pure: no IO. Both text routes — ``/v1/attachments/{sha256}/text`` and
``/v1/messages/{id}/attachments/{index}/text`` — page through this module, so
the validation, the conversion to Postgres' 1-based ``substring()`` positions,
and the ``next_offset`` arithmetic are decided once.

Characters are code points: what Postgres' ``length()``/``substring()`` count
on a UTF-8 database and what Python's ``len(str)`` counts. They are not
JavaScript's UTF-16 units, which is why ``next_offset`` is computed here rather
than left to the client.
"""
from __future__ import annotations

from dataclasses import dataclass

#: Postgres caps a single field value at 1 GB, and a character is at least one
#: byte, so no TEXT value is longer than this. `substring()` takes int4
#: positions — a larger one is `function substring(…, bigint, …) does not
#: exist`, a 500 — so offsets and limits are clamped here, which cannot change
#: an answer: every offset at or past this is past the end of every text.
MAX_TEXT_CHARS = 2**30


def text_window_error(offset: int, limit: int | None) -> str | None:
    """Why ``(offset, limit)`` is not a window, or ``None`` when it is.

    A zero ``limit`` is refused, not served: it would answer
    ``next_offset == offset`` for any text not yet exhausted, and a client
    looping on ``next_offset`` would never advance.
    """
    if offset < 0:
        return f"offset must be >= 0, got {offset}"
    if limit is not None and limit < 1:
        return f"limit must be >= 1, got {limit}"
    return None


@dataclass(frozen=True)
class TextPage:
    """One window of a text, with what a client needs to fetch the next."""

    text: str
    offset: int
    limit: int | None
    total: int
    next_offset: int | None

    def to_wire(self) -> dict[str, object]:
        return {
            "text": self.text,
            "offset": self.offset,
            "limit": self.limit,
            "total": self.total,
            "next_offset": self.next_offset,
        }


@dataclass(frozen=True)
class TextWindow:
    """A validated ``(offset, limit)``; ``limit=None`` means to the end.

    The 1-based conversion lives on the object that validated ``offset``,
    because ``offset + 1`` is only a correct ``substring()`` position when
    ``offset >= 0`` holds.
    """

    offset: int = 0
    limit: int | None = None

    def __post_init__(self) -> None:
        # The by-construction backstop: the HTTP boundary words refusals
        # through `text_window_error` first, so no wire input reaches this.
        problem = text_window_error(self.offset, self.limit)
        if problem is not None:
            raise ValueError(problem)

    @property
    def sql_from(self) -> int:
        return min(self.offset, MAX_TEXT_CHARS) + 1

    @property
    def sql_for(self) -> int | None:
        return None if self.limit is None else min(self.limit, MAX_TEXT_CHARS)

    def page(self, text: str, total: int) -> TextPage:
        end = self.offset + len(text)
        return TextPage(
            text=text, offset=self.offset, limit=self.limit, total=total,
            next_offset=end if end < total else None,
        )
```

- [ ] **Step 4: Run to verify they pass**

Run: `unset VIRTUAL_ENV && uv run pytest -q tests/test_text_window.py`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/localmail/text_window.py tests/test_text_window.py
git commit -m "text_window: page a stored text by characters, one rule for both routes"
```

---

### Task 2: The paged text accessor

**Files:**
- Modify: `src/localmail/api/attachments.py` (imports; `get_attachment_text` at the end of the file)
- Test: `tests/test_api_attachment_text_page.py`

**Interfaces:**
- Consumes: `TextWindow`, `TextPage`, `text_window_error` from Task 1.
- Produces:
  - `text_window_from_query(offset: str | None, limit: str | None) -> TextWindow` — raises `ValidationFailed`.
  - `get_attachment_text_page(conn: psycopg.Connection, sha256_hex: str, *, allowed_account_ids: list[int], window: TextWindow) -> TextPage` — `window` keyword-only, **no default**.
  - `get_attachment_text(...)` keeps its exact signature and `-> str` return.

- [ ] **Step 1: Write the failing tests**

`tests/test_api_attachment_text_page.py`:

```python
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""The paged extracted-text read, against real rows."""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone

import psycopg
import pytest

from localmail.api.attachments import (
    get_attachment_text,
    get_attachment_text_page,
    text_window_from_query,
)
from localmail.api.errors import NotFound, ValidationFailed
from localmail.text_window import TextWindow

_ASTRAL = "a\U0001D11Eb\U0001D11Ec"  # five code points, seven UTF-16 units


def _seed_text(conn: psycopg.Connection, text: str) -> tuple[str, int]:
    """Blob + carrier message + account + extracted text. Returns (sha, account)."""
    sha = hashlib.sha256(text.encode()).hexdigest()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO attachment_blobs (sha256, mime_type, size_bytes, path) "
            "VALUES (%s, 'application/pdf', 1, '/nonexistent')",
            (bytes.fromhex(sha),),
        )
        cur.execute(
            "INSERT INTO attachment_text (sha256, extractor, extracted_text) "
            "VALUES (%s, 'pypdf', %s)",
            (bytes.fromhex(sha), text),
        )
        cur.execute(
            "INSERT INTO accounts (name, email_address, imap_host, auth_method) "
            "VALUES (%s, 'x@y.test', 'imap.x', 'password') RETURNING id",
            (f"acct-{sha[:8]}",),
        )
        row = cur.fetchone(); assert row is not None
        aid = int(row[0])
        raw = b"x"
        cur.execute(
            "INSERT INTO messages (account_id, message_id, raw_bytes, raw_sha256, "
            "size_bytes, headers, attachments, date_sent) "
            "VALUES (%s, %s, %s, %s, 1, '{}'::jsonb, %s, %s)",
            (aid, f"<{sha}@x>", raw, hashlib.sha256(raw).digest(),
             psycopg.types.json.Jsonb([{"filename": "t.pdf", "sha256": sha}]),
             datetime.now(timezone.utc)),
        )
    conn.commit()
    return sha, aid


def test_pages_concatenate_to_the_whole_text_even_across_astral_characters(
    db_conn: psycopg.Connection,
) -> None:
    sha, aid = _seed_text(db_conn, _ASTRAL)
    pieces: list[str] = []
    offset: int | None = 0
    while offset is not None:
        page = get_attachment_text_page(
            db_conn, sha, allowed_account_ids=[aid],
            window=TextWindow(offset=offset, limit=2),
        )
        assert page.total == 5
        pieces.append(page.text)
        offset = page.next_offset
    assert pieces == ["a\U0001D11E", "b\U0001D11E", "c"]
    assert "".join(pieces) == _ASTRAL


def test_an_offset_past_int4_is_clamped_not_a_500(db_conn: psycopg.Connection) -> None:
    sha, aid = _seed_text(db_conn, "hello")
    page = get_attachment_text_page(
        db_conn, sha, allowed_account_ids=[aid],
        window=TextWindow(offset=2**31, limit=2**40),
    )
    assert (page.text, page.total, page.next_offset) == ("", 5, None)


def test_a_limit_past_int4_reads_to_the_end(db_conn: psycopg.Connection) -> None:
    sha, aid = _seed_text(db_conn, "hello")
    page = get_attachment_text_page(
        db_conn, sha, allowed_account_ids=[aid],
        window=TextWindow(offset=1, limit=2**40),
    )
    assert (page.text, page.next_offset) == ("ello", None)


def test_get_attachment_text_still_returns_the_whole_string(
    db_conn: psycopg.Connection,
) -> None:
    sha, aid = _seed_text(db_conn, _ASTRAL)
    assert get_attachment_text(db_conn, sha, allowed_account_ids=[aid]) == _ASTRAL


def test_an_ungranted_caller_gets_the_shared_404(db_conn: psycopg.Connection) -> None:
    sha, _aid = _seed_text(db_conn, "hello")
    with pytest.raises(NotFound):
        get_attachment_text_page(
            db_conn, sha, allowed_account_ids=[], window=TextWindow(),
        )


def test_window_is_keyword_only_with_no_default(db_conn: psycopg.Connection) -> None:
    sha, aid = _seed_text(db_conn, "hello")
    with pytest.raises(TypeError):
        get_attachment_text_page(db_conn, sha, allowed_account_ids=[aid])  # type: ignore[call-arg]


@pytest.mark.parametrize(("offset", "limit", "expected"), [
    (None, None, TextWindow()),
    ("3", None, TextWindow(offset=3)),
    ("0", "20000", TextWindow(offset=0, limit=20_000)),
])
def test_the_query_becomes_a_window(
    offset: str | None, limit: str | None, expected: TextWindow,
) -> None:
    assert text_window_from_query(offset, limit) == expected


@pytest.mark.parametrize(("offset", "limit", "fragment"), [
    ("-1", None, "offset must be a base-10 integer"),
    ("x", None, "offset must be a base-10 integer"),
    (None, "abc", "limit must be a base-10 integer"),
    (None, "0", "limit must be >= 1, got 0"),
])
def test_a_bad_query_is_a_validation_failure(
    offset: str | None, limit: str | None, fragment: str,
) -> None:
    with pytest.raises(ValidationFailed, match=fragment):
        text_window_from_query(offset, limit)
```

- [ ] **Step 2: Run to verify they fail**

Run: `unset VIRTUAL_ENV && uv run pytest -q tests/test_api_attachment_text_page.py`
Expected: collection error, `ImportError: cannot import name 'get_attachment_text_page'`.

- [ ] **Step 3: Implement**

In `src/localmail/api/attachments.py`, extend the imports:

```python
from localmail.api.errors import NotFound, ValidationFailed
from localmail.api.ids import parse_int_id
from localmail.text_window import TextPage, TextWindow, text_window_error
```

Replace the whole of `get_attachment_text` (the last function in the file) with:

```python
def text_window_from_query(offset: str | None, limit: str | None) -> TextWindow:
    """The wire's ``offset``/``limit`` as a window; anything malformed is a 400.

    The one place a query string becomes a :class:`TextWindow`, shared by both
    text routes so they cannot word a refusal two ways. Parsed with
    ``parse_int_id`` so a malformed value is problem+json, never FastAPI's 422
    array (#370). A consequence: ``offset=-1`` is refused as "not a base-10
    integer" before ``text_window_error`` could word it as "must be >= 0" —
    the wording every other integer parameter on /v1 already has.
    """
    off = 0 if offset is None else parse_int_id(offset, field="offset")
    lim = None if limit is None else parse_int_id(limit, field="limit")
    problem = text_window_error(off, lim)
    if problem is not None:
        raise ValidationFailed(problem)
    return TextWindow(offset=off, limit=lim)


def get_attachment_text_page(
    conn: psycopg.Connection,
    sha256_hex: str,
    *,
    allowed_account_ids: list[int],
    window: TextWindow,
) -> TextPage:
    """One character window of a blob's extracted text, with its total length.

    Raises ``NotFound`` if the text is not yet extracted or the caller cannot
    read any carrying message. ``window`` has no default: ``TextWindow()`` is
    the whole text, and a caller that meant a page must say so (#234's shape).

    Not cheaper in the database than the whole text: the value is
    TOAST-compressed, so every window decompresses all of it. What a window
    buys is the size of the response.
    """
    sha_bytes = _parse_sha256_hex(sha256_hex)
    if not _caller_can_read_blob(
        conn, sha256_hex, allowed_account_ids=allowed_account_ids,
    ):
        raise NotFound(f"no extracted text for attachment {sha256_hex}")
    with conn.cursor() as cur:
        if window.sql_for is None:
            cur.execute(
                "SELECT substring(extracted_text from %s::int), "
                "       length(extracted_text) "
                "FROM attachment_text WHERE sha256 = %s",
                (window.sql_from, sha_bytes),
            )
        else:
            cur.execute(
                "SELECT substring(extracted_text from %s::int for %s::int), "
                "       length(extracted_text) "
                "FROM attachment_text WHERE sha256 = %s",
                (window.sql_from, window.sql_for, sha_bytes),
            )
        row = cur.fetchone()
    if row is None:
        raise NotFound(f"no extracted text for attachment {sha256_hex}")
    return window.page(row[0], int(row[1]))


def get_attachment_text(
    conn: psycopg.Connection, sha256_hex: str, *, allowed_account_ids: list[int],
) -> str:
    """Return extracted text for a blob. Raises NotFound if not yet extracted
    or if the caller cannot read any carrying message.
    """
    return get_attachment_text_page(
        conn, sha256_hex,
        allowed_account_ids=allowed_account_ids, window=TextWindow(),
    ).text
```

- [ ] **Step 4: Run to verify they pass, plus the existing callers**

Run: `unset VIRTUAL_ENV && uv run pytest -q tests/test_api_attachment_text_page.py tests/test_mcp_tools.py tests/test_serve_attachments_routes.py`
Expected: all pass (`get_attachment_text`'s callers are unchanged).

- [ ] **Step 5: Commit**

```bash
git add src/localmail/api/attachments.py tests/test_api_attachment_text_page.py
git commit -m "api: page extracted text by characters; get_attachment_text delegates"
```

---

### Task 3: The index resolver

**Files:**
- Modify: `src/localmail/api/attachments.py` (add after `_parse_sha256_hex`)
- Test: `tests/test_api_message_attachment.py`

**Interfaces:**
- Produces:
  - `MAX_JSONB_INDEX: int = 2**31 - 1`
  - `MessageAttachment(sha256: str, filename: str | None)` — frozen dataclass
  - `resolve_message_attachment(conn: psycopg.Connection, message_id: int, index: int, *, allowed_account_ids: list[int]) -> MessageAttachment`

- [ ] **Step 1: Write the failing tests**

`tests/test_api_message_attachment.py`:

```python
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""An attachment is addressed by its position in the message's array."""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any

import psycopg
import pytest

from localmail.api.attachments import (
    MAX_JSONB_INDEX,
    MessageAttachment,
    resolve_message_attachment,
)
from localmail.api.errors import NotFound, ValidationFailed

_A = "a1" * 32
_B = "b2" * 32

_ENTRIES: list[dict[str, Any]] = [
    {"filename": "note.txt", "sha256": _A},
    {"filename": "note.txt", "sha256": _B},
    {"sha256": _A},  # same bytes as entry 0, no name of its own
]


def _seed(conn: psycopg.Connection, entries: list[dict[str, Any]]) -> tuple[int, int]:
    """One account carrying one message with this `attachments` array."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO accounts (name, email_address, imap_host, auth_method) "
            "VALUES ('acct', 'x@y.test', 'imap.x', 'password') RETURNING id",
        )
        row = cur.fetchone(); assert row is not None
        aid = int(row[0])
        raw = b"x"
        cur.execute(
            "INSERT INTO messages (account_id, message_id, raw_bytes, raw_sha256, "
            "size_bytes, headers, attachments, date_sent) "
            "VALUES (%s, '<m@x>', %s, %s, 1, '{}'::jsonb, %s, %s) RETURNING id",
            (aid, raw, hashlib.sha256(raw).digest(),
             psycopg.types.json.Jsonb(entries), datetime.now(timezone.utc)),
        )
        row = cur.fetchone(); assert row is not None
        mid = int(row[0])
    conn.commit()
    return aid, mid


def test_each_index_resolves_to_its_own_entry(db_conn: psycopg.Connection) -> None:
    aid, mid = _seed(db_conn, _ENTRIES)
    got = [
        resolve_message_attachment(db_conn, mid, i, allowed_account_ids=[aid])
        for i in range(3)
    ]
    assert got == [
        MessageAttachment(sha256=_A, filename="note.txt"),
        MessageAttachment(sha256=_B, filename="note.txt"),
        MessageAttachment(sha256=_A, filename=None),
    ]


def test_an_index_past_the_end_is_the_shared_404(db_conn: psycopg.Connection) -> None:
    aid, mid = _seed(db_conn, _ENTRIES)
    with pytest.raises(NotFound, match=f"attachment 3 of message {mid} not found"):
        resolve_message_attachment(db_conn, mid, 3, allowed_account_ids=[aid])


def test_ungranted_is_indistinguishable_from_missing(db_conn: psycopg.Connection) -> None:
    aid, mid = _seed(db_conn, _ENTRIES)
    with pytest.raises(NotFound) as ungranted:
        resolve_message_attachment(db_conn, mid, 0, allowed_account_ids=[aid + 1])
    with pytest.raises(NotFound) as missing:
        resolve_message_attachment(db_conn, mid + 1, 0, allowed_account_ids=[aid])
    assert str(ungranted.value) == f"attachment 0 of message {mid} not found"
    assert str(missing.value) == f"attachment 0 of message {mid + 1} not found"


def test_a_negative_index_is_refused_not_answered_with_the_last_entry(
    db_conn: psycopg.Connection,
) -> None:
    # Postgres `->` indexes from the end: `attachments -> -1` IS the last
    # entry. The accessor is public API, so it must refuse -1 itself rather
    # than rely on parse_int_id having refused it on the wire.
    aid, mid = _seed(db_conn, _ENTRIES)
    with pytest.raises(ValidationFailed, match="attachment index must be >= 0, got -1"):
        resolve_message_attachment(db_conn, mid, -1, allowed_account_ids=[aid])


def test_a_negative_index_is_refused_before_the_empty_acl_short_circuit(
    db_conn: psycopg.Connection,
) -> None:
    aid, mid = _seed(db_conn, _ENTRIES)
    with pytest.raises(ValidationFailed):
        resolve_message_attachment(db_conn, mid, -1, allowed_account_ids=[])


class _Untouchable:
    """A connection that fails the test if anything asks it for a cursor."""

    def cursor(self, *args: object, **kwargs: object) -> None:
        raise AssertionError("the resolver queried when it should not have")


def test_an_index_past_int4_is_a_404_without_a_query() -> None:
    with pytest.raises(NotFound):
        resolve_message_attachment(
            _Untouchable(), 1, MAX_JSONB_INDEX + 1,  # type: ignore[arg-type]
            allowed_account_ids=[1],
        )


def test_the_largest_int4_index_is_answered_by_the_query_not_a_500(
    db_conn: psycopg.Connection,
) -> None:
    aid, mid = _seed(db_conn, _ENTRIES)
    with pytest.raises(NotFound):
        resolve_message_attachment(
            db_conn, mid, MAX_JSONB_INDEX, allowed_account_ids=[aid],
        )


def test_the_ceiling_is_int4_max() -> None:
    assert MAX_JSONB_INDEX == 2**31 - 1


def test_an_entry_without_a_hash_is_a_404(db_conn: psycopg.Connection) -> None:
    aid, mid = _seed(db_conn, [{"filename": "orphan.bin"}])
    with pytest.raises(NotFound):
        resolve_message_attachment(db_conn, mid, 0, allowed_account_ids=[aid])


def test_an_empty_acl_is_a_404(db_conn: psycopg.Connection) -> None:
    _aid, mid = _seed(db_conn, _ENTRIES)
    with pytest.raises(NotFound):
        resolve_message_attachment(db_conn, mid, 0, allowed_account_ids=[])
```

- [ ] **Step 2: Run to verify they fail**

Run: `unset VIRTUAL_ENV && uv run pytest -q tests/test_api_message_attachment.py`
Expected: collection error, `ImportError: cannot import name 'MAX_JSONB_INDEX'`.

- [ ] **Step 3: Implement**

In `src/localmail/api/attachments.py`, add `from dataclasses import dataclass` to the imports, then insert after `_parse_sha256_hex`:

```python
#: The largest operand `jsonb -> integer` accepts. A larger index is
#: `operator does not exist: jsonb -> bigint` — a 500 — and cannot address an
#: entry anyway, since no array has that many.
MAX_JSONB_INDEX = 2**31 - 1


@dataclass(frozen=True)
class MessageAttachment:
    """One entry of a message's ``attachments`` array, resolved by position.

    ``filename`` is *this entry's* name. A blob is content-addressable and may
    be carried under several names, even within one message, so this is the
    only way to know which one the caller meant.
    """

    sha256: str
    filename: str | None


def _attachment_absent(message_id: int, index: int) -> NotFound:
    # One wording for every resolver 404, so no branch can tell a caller
    # whether the message exists, is ungranted, or is merely short.
    return NotFound(f"attachment {index} of message {message_id} not found")


def resolve_message_attachment(
    conn: psycopg.Connection,
    message_id: int,
    index: int,
    *,
    allowed_account_ids: list[int],
) -> MessageAttachment:
    """Resolve entry ``index`` of message ``message_id`` under the message ACL.

    A negative index is refused (``ValidationFailed``) before anything else:
    Postgres ``->`` indexes from the end, so ``attachments -> -1`` would
    silently answer with the last entry. It is refused ahead of the empty-ACL
    short-circuit so a malformed request is never disguised as a 404.

    Every other failure is the one ``NotFound``: the message is missing or
    ungranted, the index is past the end, the index exceeds
    ``MAX_JSONB_INDEX`` (decided without a query), or the entry carries no
    hash.
    """
    if index < 0:
        raise ValidationFailed(f"attachment index must be >= 0, got {index}")
    if index > MAX_JSONB_INDEX or not allowed_account_ids:
        raise _attachment_absent(message_id, index)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT m.attachments -> %s::int FROM messages m "
            "WHERE m.id = %s AND m.account_id = ANY(%s)",
            (index, message_id, allowed_account_ids),
        )
        row = cur.fetchone()
    entry = None if row is None else row[0]
    if not isinstance(entry, dict) or not entry.get("sha256"):
        raise _attachment_absent(message_id, index)
    filename = entry.get("filename")
    return MessageAttachment(
        sha256=str(entry["sha256"]),
        filename=None if filename is None else str(filename),
    )
```

- [ ] **Step 4: Run to verify they pass**

Run: `unset VIRTUAL_ENV && uv run pytest -q tests/test_api_message_attachment.py`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/localmail/api/attachments.py tests/test_api_message_attachment.py
git commit -m "api: resolve an attachment by its position in the message"
```

---

### Task 4: Move the streaming rules to `blob_response.py`

A behaviour-preserving refactor. It has no red state of its own, so it is guarded by a **golden written first and green on the unchanged tree**, plus a positive control proving the existing 304 spy still intercepts.

**Files:**
- Create: `src/localmail/serve/routes/blob_response.py`
- Modify: `src/localmail/serve/routes/attachments.py`
- Test: `tests/test_serve_attachments_golden.py`

**Interfaces:**
- Produces:
  - `not_modified(request: Request, sha256: str) -> Response | None`
  - `blob_response(request: Request, *, sha256: str, mime: str, size: int, fp: BinaryIO, filename: str | None) -> Response`

- [ ] **Step 1: Write the golden (must PASS on the current tree)**

`tests/test_serve_attachments_golden.py`:

```python
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""What `/v1/attachments/{sha256}` answers, pinned before the streaming rules
moved to `blob_response.py` — so the move can be shown to change nothing.

Written green on the unchanged tree. If a literal here disagrees with the
tree *before* the refactor, the literal is what is wrong: fix it to the
tree's answer, then refactor.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from fastapi.testclient import TestClient

from localmail.serve.app import create_app

_PAYLOAD = b"0123456789"
_SHA = "84d89877f0d4041efb6bf91a16f0248f2fd573e6af05c19f96bedb9f882f7882"
_ETAG = f'"{_SHA}"'
_DISPOSITION = "attachment; filename=\"report.pdf\"; filename*=UTF-8''report.pdf"


def _seed(conn: psycopg.Connection, tmp_path: Path) -> None:
    assert hashlib.sha256(_PAYLOAD).hexdigest() == _SHA
    blob = tmp_path / "blobs" / _SHA[:2] / _SHA[2:4] / _SHA
    blob.parent.mkdir(parents=True)
    blob.write_bytes(_PAYLOAD)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO attachment_blobs (sha256, mime_type, size_bytes, path) "
            "VALUES (%s, 'application/pdf', %s, %s)",
            (bytes.fromhex(_SHA), len(_PAYLOAD), str(blob)),
        )
        cur.execute(
            "INSERT INTO accounts (name, email_address, imap_host, auth_method) "
            "VALUES ('golden', 'x@y.test', 'imap.x', 'password') RETURNING id",
        )
        row = cur.fetchone(); assert row is not None
        raw = b"x"
        cur.execute(
            "INSERT INTO messages (account_id, message_id, raw_bytes, raw_sha256, "
            "size_bytes, headers, attachments, date_sent) "
            "VALUES (%s, '<g@x>', %s, %s, 1, '{}'::jsonb, %s, %s)",
            (row[0], raw, hashlib.sha256(raw).digest(),
             psycopg.types.json.Jsonb([{"filename": "report.pdf", "sha256": _SHA}]),
             datetime.now(timezone.utc)),
        )
    conn.commit()


def _get(db_dsn: str, token: str, **headers: str):
    c = TestClient(create_app(db_dsn=db_dsn, searcher=None))
    return c.get(
        f"/v1/attachments/{_SHA}",
        headers={"Authorization": f"Bearer {token}", **headers},
    )


def test_full_200(db_dsn, api_token, db_conn, tmp_path, grant_alice_all_accounts) -> None:
    _seed(db_conn, tmp_path); grant_alice_all_accounts()
    r = _get(db_dsn, api_token)
    assert r.status_code == 200
    assert r.content == _PAYLOAD
    assert r.headers["content-type"] == "application/pdf"
    assert r.headers["content-length"] == "10"
    assert r.headers["content-disposition"] == _DISPOSITION
    assert r.headers["accept-ranges"] == "bytes"
    assert r.headers["etag"] == _ETAG
    assert "content-range" not in r.headers


def test_partial_206(db_dsn, api_token, db_conn, tmp_path, grant_alice_all_accounts) -> None:
    _seed(db_conn, tmp_path); grant_alice_all_accounts()
    r = _get(db_dsn, api_token, Range="bytes=2-5")
    assert r.status_code == 206
    assert r.content == b"2345"
    assert r.headers["content-length"] == "4"
    assert r.headers["content-range"] == "bytes 2-5/10"
    assert r.headers["content-disposition"] == _DISPOSITION
    assert r.headers["etag"] == _ETAG


def test_unsatisfiable_416(db_dsn, api_token, db_conn, tmp_path, grant_alice_all_accounts) -> None:
    _seed(db_conn, tmp_path); grant_alice_all_accounts()
    r = _get(db_dsn, api_token, Range="bytes=50-")
    assert r.status_code == 416
    assert r.content == b""
    assert r.headers["content-range"] == "bytes */10"
    assert r.headers["content-disposition"] == _DISPOSITION
    assert r.headers["accept-ranges"] == "bytes"
    assert r.headers["etag"] == _ETAG


def test_not_modified_304(db_dsn, api_token, db_conn, tmp_path, grant_alice_all_accounts) -> None:
    _seed(db_conn, tmp_path); grant_alice_all_accounts()
    r = _get(db_dsn, api_token, **{"If-None-Match": _ETAG})
    assert r.status_code == 304
    assert r.content == b""
    assert r.headers["etag"] == _ETAG
    assert "content-disposition" not in r.headers


def test_the_304_spy_intercepts_when_the_body_is_served(
    db_dsn, api_token, db_conn, tmp_path, grant_alice_all_accounts, monkeypatch,
) -> None:
    """Positive control for `test_304_does_not_call_open_attachment_bytes_or_filename`.

    That test asserts the route module's `_open_blob_file_at` is NOT called on
    a 304. Were the open ever moved out of the route module, its spy would
    stop intercepting and `== []` would pass for nothing. Here the same spy
    must see exactly one call on a 200.
    """
    _seed(db_conn, tmp_path); grant_alice_all_accounts()
    import localmail.serve.routes.attachments as routes
    calls: list[object] = []
    real = routes._open_blob_file_at

    def spy(*args, **kwargs):
        calls.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(routes, "_open_blob_file_at", spy)
    assert _get(db_dsn, api_token).status_code == 200
    assert len(calls) == 1
```

- [ ] **Step 2: Run it on the UNCHANGED tree — it must pass**

Run: `unset VIRTUAL_ENV && uv run pytest -q tests/test_serve_attachments_golden.py`
Expected: 5 passed. If a literal fails, correct the literal to the current answer (see the module docstring) and re-run until green. Do not touch `src/` in this step.

- [ ] **Step 3: Commit the golden alone**

```bash
git add tests/test_serve_attachments_golden.py
git commit -m "test: pin /v1/attachments/{sha256} before the streaming rules move"
```

- [ ] **Step 4: Create `blob_response.py`**

`src/localmail/serve/routes/blob_response.py` — the constants and helpers are **moved verbatim** from `serve/routes/attachments.py` (lines 37–152 of the pre-change file: `_CHUNK` through the end of `_stream_range`; the `logger` on line 35 is re-declared below with its reason); the two public functions are new:

```python
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Serving an attachment blob: the #32/#54/#58/#59 response rules.

Shared by every route that addresses a blob — by content hash
(`/v1/attachments/{sha256}`) and by position in a message
(`/v1/messages/{id}/attachments/{index}`). Callers run the ACL probe, then
:func:`not_modified`, then open the file themselves, then hand it to
:func:`blob_response`. The open stays at the call site on purpose: the #62
tests spy on each route module's ``_open_blob_file_at``, and an open moved in
here would make those spies pass whether or not a file was opened.
"""
from __future__ import annotations

import logging
from typing import BinaryIO, Iterator
from urllib.parse import quote

from fastapi import Request
from fastapi.responses import Response, StreamingResponse

from localmail.api.conditional import (
    etag_for_sha256,
    if_none_match_satisfies,
    if_range_allows_partial,
)
from localmail.api.range_requests import (
    ByteRange,
    UnsatisfiableRange,
    content_range_header,
    parse_byte_range,
    unsatisfiable_content_range,
)

# Spelled out rather than __name__: the #58 truncation tests listen on it.
logger = logging.getLogger("localmail.serve")

# ---- moved verbatim from serve/routes/attachments.py -------------------
# _CHUNK, _HTTP_NOT_MODIFIED, _HTTP_PARTIAL_CONTENT,
# _HTTP_RANGE_NOT_SATISFIABLE, _INLINE_RISKY_MIMES, _SAFE_FALLBACK_MIME,
# _SHA_PREFIX_LEN_FOR_FALLBACK_NAME, _QUOTED_STRING_UNSAFE,
# _PRINTABLE_ASCII_MIN, _PRINTABLE_ASCII_MAX, _ascii_fallback_name,
# _content_disposition, _safe_response_mime, _fallback_filename,
# _log_truncation, _stream_full, _stream_range — with their comments and
# docstrings, unchanged. (Paste them here; delete this comment block.)
# ------------------------------------------------------------------------


def not_modified(request: Request, sha256: str) -> Response | None:
    """A 304 when the client's copy is current, else ``None``.

    Run after the ACL probe — so a caller without a grant still sees 404,
    never 304 — and before the file open and any filename lookup (#62).
    The 304 carries only the ``ETag`` (RFC 9110 §15.4.5).
    """
    etag = etag_for_sha256(sha256)
    if if_none_match_satisfies(request.headers.get("if-none-match"), etag):
        return Response(status_code=_HTTP_NOT_MODIFIED, headers={"ETag": etag})
    return None


def blob_response(
    request: Request,
    *,
    sha256: str,
    mime: str,
    size: int,
    fp: BinaryIO,
    filename: str | None,
) -> Response:
    """200 / 206 / 416 for an already-opened, ACL-cleared blob.

    Takes ownership of ``fp``: the streamer closes it, or this function does
    on the 416 branch. ``filename`` falls back to a sha-prefix name when it
    is ``None`` or blank. Every response carries the #32 force-download
    headers and the strong ``ETag``.
    """
    etag = etag_for_sha256(sha256)
    disposition = _content_disposition(
        (filename or "").strip() or _fallback_filename(sha256)
    )
    response_mime = _safe_response_mime(mime)

    range_header = request.headers.get("range")
    if range_header is not None and not if_range_allows_partial(
        request.headers.get("if-range"), etag,
    ):
        range_header = None

    try:
        byte_range = parse_byte_range(range_header, size)
    except UnsatisfiableRange:
        fp.close()
        return Response(
            status_code=_HTTP_RANGE_NOT_SATISFIABLE,
            media_type=response_mime,
            headers={
                "Content-Range": unsatisfiable_content_range(size),
                "Content-Disposition": disposition,
                "Accept-Ranges": "bytes",
                "ETag": etag,
            },
        )

    if byte_range is None:
        return StreamingResponse(
            _stream_full(fp, sha256, size),
            media_type=response_mime,
            headers={
                "Content-Length": str(size),
                "Content-Disposition": disposition,
                "Accept-Ranges": "bytes",
                "ETag": etag,
            },
        )

    return StreamingResponse(
        _stream_range(fp, byte_range, sha256),
        status_code=_HTTP_PARTIAL_CONTENT,
        media_type=response_mime,
        headers={
            "Content-Length": str(byte_range.length),
            "Content-Range": content_range_header(byte_range, size),
            "Content-Disposition": disposition,
            "Accept-Ranges": "bytes",
            "ETag": etag,
        },
    )
```

- [ ] **Step 5: Thin `serve/routes/attachments.py`**

Delete lines 35–152 (the `logger` line through the end of `_stream_range`) and the now-unused imports. The module becomes:

```python
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Attachment streaming + extracted-text routes, addressed by content hash."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

from localmail.api.acl import allowed_account_ids
from localmail.api.attachments import (
    _open_blob_file_at,
    get_attachment_blob_info,
    get_attachment_filename,
    get_attachment_text,
)
from localmail.serve.middleware import get_authenticated_user
from localmail.serve.routes.blob_response import blob_response, not_modified

router = APIRouter()


@router.get("/{sha256}")
def stream_blob(
    sha256: str,
    request: Request,
    user=Depends(get_authenticated_user),
) -> Response:
    """<keep the existing docstring verbatim>"""
    pool = request.app.state.pool
    with pool.connection() as conn:
        allowed = allowed_account_ids(conn, user.id)
        mime, size, path = get_attachment_blob_info(
            conn, sha256, allowed_account_ids=allowed,
        )
        cached = not_modified(request, sha256)
        if cached is not None:
            return cached
        fp = _open_blob_file_at(path, sha256)
        original = get_attachment_filename(
            conn, sha256, allowed_account_ids=allowed,
        )
    return blob_response(
        request, sha256=sha256, mime=mime, size=size, fp=fp, filename=original,
    )


@router.get("/{sha256}/text")
def attachment_text(
    sha256: str,
    request: Request,
    user=Depends(get_authenticated_user),
) -> dict[str, str]:
    pool = request.app.state.pool
    with pool.connection() as conn:
        allowed = allowed_account_ids(conn, user.id)
        text = get_attachment_text(conn, sha256, allowed_account_ids=allowed)
    return {"text": text}
```

(`attachment_text` is unchanged here; Task 5 pages it.)

- [ ] **Step 6: Run the golden and every attachment-route test**

Run: `unset VIRTUAL_ENV && uv run pytest -q tests/test_serve_attachments_golden.py tests/test_serve_attachments_routes.py tests/test_serve_attachments_conditional.py tests/test_serve_acl_routes.py`
Expected: all pass, **no test file edited**. A failure means the move changed behaviour — fix `blob_response.py`, never the golden.

- [ ] **Step 7: mypy and commit**

Run: `unset VIRTUAL_ENV && uv run mypy src/localmail`
Expected: `Success`.

```bash
git add src/localmail/serve/routes/blob_response.py src/localmail/serve/routes/attachments.py
git commit -m "serve: move the blob response rules out of the sha route so two routes share them"
```

---

### Task 5: Page `/v1/attachments/{sha256}/text`

**Files:**
- Modify: `src/localmail/serve/routes/attachments.py` (`attachment_text`)
- Modify: `tests/test_serve_attachments_routes.py:106`, `tests/test_serve_acl_routes.py:163`
- Test: `tests/test_serve_attachments_routes.py` (new tests appended)

**Interfaces:**
- Consumes: `text_window_from_query`, `get_attachment_text_page` (Task 2).

- [ ] **Step 1: Update the two exact-body assertions and add the paging tests**

In `tests/test_serve_attachments_routes.py`, line 106 becomes:

```python
    assert r.json() == {
        "text": "Hello world", "offset": 0, "limit": None,
        "total": 11, "next_offset": None,
    }
```

In `tests/test_serve_acl_routes.py`, line 163 becomes:

```python
    assert r.json() == {
        "text": "secret-a-content", "offset": 0, "limit": None,
        "total": 16, "next_offset": None,
    }
```

Append to `tests/test_serve_attachments_routes.py`:

```python
def _seed_text_blob(conn, tmp_path: Path, sha: str, text: str) -> None:
    _seed_blob_with_carrier(conn, tmp_path, sha, b"%PDF")
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO attachment_text (sha256, extractor, extracted_text) "
            "VALUES (%s, 'pypdf', %s)",
            (bytes.fromhex(sha), text),
        )
    conn.commit()


def test_attachment_text_pages_by_character(
    db_dsn: str, api_token: str, db_conn, tmp_path: Path, grant_alice_all_accounts,
) -> None:
    sha = "e1" * 32
    _seed_text_blob(db_conn, tmp_path, sha, "Hello world")
    grant_alice_all_accounts()
    c = TestClient(create_app(db_dsn=db_dsn, searcher=None))
    r = c.get(
        f"/v1/attachments/{sha}/text?offset=6&limit=3",
        headers={"Authorization": f"Bearer {api_token}"},
    )
    assert r.status_code == 200
    assert r.json() == {
        "text": "wor", "offset": 6, "limit": 3, "total": 11, "next_offset": 9,
    }


@pytest.mark.parametrize("query", ["offset=-1", "offset=x", "limit=0", "limit=1.5"])
def test_a_bad_window_is_problem_json_not_422(
    db_dsn: str, api_token: str, db_conn, tmp_path: Path,
    grant_alice_all_accounts, query: str,
) -> None:
    sha = "e2" * 32
    _seed_text_blob(db_conn, tmp_path, sha, "Hello world")
    grant_alice_all_accounts()
    c = TestClient(create_app(db_dsn=db_dsn, searcher=None))
    r = c.get(
        f"/v1/attachments/{sha}/text?{query}",
        headers={"Authorization": f"Bearer {api_token}"},
    )
    assert r.status_code == 400
    assert r.headers["content-type"].startswith("application/problem+json")
    assert r.json()["type"] == "/problems/validation-failed"


def test_a_bad_window_is_a_400_even_for_a_caller_granted_nothing(
    db_dsn: str, api_token: str, db_conn, tmp_path: Path,
) -> None:
    sha = "e3" * 32
    _seed_text_blob(db_conn, tmp_path, sha, "Hello world")
    c = TestClient(create_app(db_dsn=db_dsn, searcher=None))
    r = c.get(
        f"/v1/attachments/{sha}/text?limit=0",
        headers={"Authorization": f"Bearer {api_token}"},
    )
    assert r.status_code == 400
```

Add `import pytest` to the file's imports if absent.

- [ ] **Step 2: Run to verify they fail**

Run: `unset VIRTUAL_ENV && uv run pytest -q tests/test_serve_attachments_routes.py tests/test_serve_acl_routes.py`
Expected: FAIL — the two updated assertions (body lacks the new keys), `test_attachment_text_pages_by_character` (whole text returned), and the 400 tests (200 returned).

- [ ] **Step 3: Implement**

In `serve/routes/attachments.py`, change the imports and replace `attachment_text`:

```python
from fastapi import APIRouter, Depends, Query, Request

from localmail.api.attachments import (
    _open_blob_file_at,
    get_attachment_blob_info,
    get_attachment_filename,
    get_attachment_text_page,
    text_window_from_query,
)
```

```python
@router.get("/{sha256}/text")
def attachment_text(
    sha256: str,
    request: Request,
    offset: str | None = Query(None),
    limit: str | None = Query(None),
    user=Depends(get_authenticated_user),
) -> dict[str, object]:
    """Extracted text, paged by character. Omit both for the whole text.

    ``offset``/``limit`` arrive as strings so a malformed one is a 400
    problem+json rather than FastAPI's 422 (#370). The window is judged
    before the connection opens, so it is a 400 even for a caller granted
    nothing.
    """
    window = text_window_from_query(offset, limit)
    pool = request.app.state.pool
    with pool.connection() as conn:
        allowed = allowed_account_ids(conn, user.id)
        page = get_attachment_text_page(
            conn, sha256, allowed_account_ids=allowed, window=window,
        )
    return page.to_wire()
```

- [ ] **Step 4: Run to verify they pass**

Run: `unset VIRTUAL_ENV && uv run pytest -q tests/test_serve_attachments_routes.py tests/test_serve_acl_routes.py tests/test_serve_attachments_golden.py tests/test_mcp_tools.py`
Expected: all pass (the MCP pin is unchanged because MCP still calls `get_attachment_text`).

- [ ] **Step 5: Commit**

```bash
git add src/localmail/serve/routes/attachments.py tests/test_serve_attachments_routes.py tests/test_serve_acl_routes.py
git commit -m "serve: page /v1/attachments/{sha256}/text by character"
```

---

### Task 6: The two index routes

**Files:**
- Modify: `src/localmail/serve/routes/messages.py`
- Test: `tests/test_serve_message_attachment_routes.py`

**Interfaces:**
- Consumes: `resolve_message_attachment`, `get_attachment_blob_info`, `_open_blob_file_at`, `text_window_from_query`, `get_attachment_text_page` (api); `not_modified`, `blob_response` (Task 4).

- [ ] **Step 1: Write the failing tests**

`tests/test_serve_message_attachment_routes.py`:

```python
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""GET /v1/messages/{id}/attachments/{index}[/text]."""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient

from localmail.serve.app import create_app

_Part = tuple[str | None, bytes, str]  # (filename, payload, mime)


def _seed(
    conn: psycopg.Connection, tmp_path: Path, parts: list[_Part],
) -> tuple[int, list[str]]:
    """One message whose attachments are `parts`, in order. Returns (id, shas)."""
    shas: list[str] = []
    entries: list[dict[str, str]] = []
    with conn.cursor() as cur:
        for filename, payload, mime in parts:
            sha = hashlib.sha256(payload).hexdigest()
            blob = tmp_path / "blobs" / sha[:2] / sha[2:4] / sha
            blob.parent.mkdir(parents=True, exist_ok=True)
            blob.write_bytes(payload)
            cur.execute(
                "INSERT INTO attachment_blobs (sha256, mime_type, size_bytes, path) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
                (bytes.fromhex(sha), mime, len(payload), str(blob)),
            )
            shas.append(sha)
            entries.append(
                {"sha256": sha} if filename is None
                else {"filename": filename, "sha256": sha}
            )
        cur.execute(
            "INSERT INTO accounts (name, email_address, imap_host, auth_method) "
            "VALUES ('acct', 'x@y.test', 'imap.x', 'password') RETURNING id",
        )
        row = cur.fetchone(); assert row is not None
        raw = b"x"
        cur.execute(
            "INSERT INTO messages (account_id, message_id, raw_bytes, raw_sha256, "
            "size_bytes, headers, attachments, date_sent) "
            "VALUES (%s, '<m@x>', %s, %s, 1, '{}'::jsonb, %s, %s) RETURNING id",
            (row[0], raw, hashlib.sha256(raw).digest(),
             psycopg.types.json.Jsonb(entries), datetime.now(timezone.utc)),
        )
        row = cur.fetchone(); assert row is not None
        mid = int(row[0])
    conn.commit()
    return mid, shas


def _client(db_dsn: str) -> TestClient:
    return TestClient(create_app(db_dsn=db_dsn, searcher=None))


def _auth(token: str, **extra: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", **extra}


_SAME_NAME: list[_Part] = [
    ("note.txt", b"file-one", "text/plain"),
    ("note.txt", b"file-two", "text/plain"),
]


def test_each_index_serves_its_own_bytes(
    db_dsn, api_token, db_conn, tmp_path, grant_alice_all_accounts,
) -> None:
    mid, _ = _seed(db_conn, tmp_path, _SAME_NAME)
    grant_alice_all_accounts()
    c = _client(db_dsn)
    assert c.get(f"/v1/messages/{mid}/attachments/0", headers=_auth(api_token)).content == b"file-one"
    assert c.get(f"/v1/messages/{mid}/attachments/1", headers=_auth(api_token)).content == b"file-two"


def test_disposition_carries_the_entrys_own_name(
    db_dsn, api_token, db_conn, tmp_path, grant_alice_all_accounts,
) -> None:
    # Same bytes under two names in one message: the sha route can serve only
    # one of them; the index route serves each entry's own.
    payload = b"\x89PNG-same-bytes"
    mid, shas = _seed(db_conn, tmp_path, [
        ("logo.png", payload, "image/png"),
        ("image001.png", payload, "image/png"),
    ])
    grant_alice_all_accounts()
    c = _client(db_dsn)
    r0 = c.get(f"/v1/messages/{mid}/attachments/0", headers=_auth(api_token))
    r1 = c.get(f"/v1/messages/{mid}/attachments/1", headers=_auth(api_token))
    assert 'filename="logo.png"' in r0.headers["content-disposition"]
    assert 'filename="image001.png"' in r1.headers["content-disposition"]
    # One representation, one validator: the ETag is the blob's.
    assert r0.headers["etag"] == r1.headers["etag"] == f'"{shas[0]}"'


def test_an_unnamed_entry_falls_back_to_the_sha_prefix_name(
    db_dsn, api_token, db_conn, tmp_path, grant_alice_all_accounts,
) -> None:
    mid, shas = _seed(db_conn, tmp_path, [(None, b"anon", "application/pdf")])
    grant_alice_all_accounts()
    r = _client(db_dsn).get(f"/v1/messages/{mid}/attachments/0", headers=_auth(api_token))
    assert f'filename="attachment-{shas[0][:16]}.bin"' in r.headers["content-disposition"]


def test_range_and_416_and_304(
    db_dsn, api_token, db_conn, tmp_path, grant_alice_all_accounts,
) -> None:
    mid, shas = _seed(db_conn, tmp_path, [("d.pdf", b"0123456789", "application/pdf")])
    grant_alice_all_accounts()
    c = _client(db_dsn)
    url = f"/v1/messages/{mid}/attachments/0"
    r = c.get(url, headers=_auth(api_token, Range="bytes=2-5"))
    assert (r.status_code, r.content, r.headers["content-range"]) == (206, b"2345", "bytes 2-5/10")
    r = c.get(url, headers=_auth(api_token, Range="bytes=50-"))
    assert (r.status_code, r.headers["content-range"]) == (416, "bytes */10")
    r = c.get(url, headers=_auth(api_token, **{"If-None-Match": f'"{shas[0]}"'}))
    assert (r.status_code, r.content) == (304, b"")


def test_risky_mime_is_clamped(
    db_dsn, api_token, db_conn, tmp_path, grant_alice_all_accounts,
) -> None:
    mid, _ = _seed(db_conn, tmp_path, [("x.html", b"<script>", "text/html")])
    grant_alice_all_accounts()
    r = _client(db_dsn).get(f"/v1/messages/{mid}/attachments/0", headers=_auth(api_token))
    assert r.headers["content-type"] == "application/octet-stream"


@pytest.mark.parametrize("path", ["attachments/2", "attachments/2147483648"])
def test_absent_index_is_404(
    db_dsn, api_token, db_conn, tmp_path, grant_alice_all_accounts, path: str,
) -> None:
    mid, _ = _seed(db_conn, tmp_path, _SAME_NAME)
    grant_alice_all_accounts()
    r = _client(db_dsn).get(f"/v1/messages/{mid}/{path}", headers=_auth(api_token))
    assert r.status_code == 404


def test_ungranted_is_404(db_dsn, api_token, db_conn, tmp_path) -> None:
    mid, _ = _seed(db_conn, tmp_path, _SAME_NAME)
    r = _client(db_dsn).get(f"/v1/messages/{mid}/attachments/0", headers=_auth(api_token))
    assert r.status_code == 404


@pytest.mark.parametrize("index", ["-1", "x", "1.0"])
def test_malformed_index_is_problem_json(
    db_dsn, api_token, db_conn, tmp_path, grant_alice_all_accounts, index: str,
) -> None:
    mid, _ = _seed(db_conn, tmp_path, _SAME_NAME)
    grant_alice_all_accounts()
    r = _client(db_dsn).get(f"/v1/messages/{mid}/attachments/{index}", headers=_auth(api_token))
    assert r.status_code == 400
    assert r.json()["type"] == "/problems/validation-failed"


def _spy_open(monkeypatch) -> list[object]:
    import localmail.serve.routes.messages as routes
    calls: list[object] = []
    real = routes._open_blob_file_at

    def spy(*args, **kwargs):
        calls.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(routes, "_open_blob_file_at", spy)
    return calls


def test_a_304_opens_no_file(
    db_dsn, api_token, db_conn, tmp_path, grant_alice_all_accounts, monkeypatch,
) -> None:
    mid, shas = _seed(db_conn, tmp_path, [("d.pdf", b"payload", "application/pdf")])
    grant_alice_all_accounts()
    calls = _spy_open(monkeypatch)
    r = _client(db_dsn).get(
        f"/v1/messages/{mid}/attachments/0",
        headers=_auth(api_token, **{"If-None-Match": f'"{shas[0]}"'}),
    )
    assert r.status_code == 304
    assert calls == []


def test_the_spy_does_intercept_a_served_body(
    db_dsn, api_token, db_conn, tmp_path, grant_alice_all_accounts, monkeypatch,
) -> None:
    """Positive control: without it, `calls == []` above could pass for a spy
    that intercepts nothing."""
    mid, _ = _seed(db_conn, tmp_path, [("d.pdf", b"payload", "application/pdf")])
    grant_alice_all_accounts()
    calls = _spy_open(monkeypatch)
    r = _client(db_dsn).get(f"/v1/messages/{mid}/attachments/0", headers=_auth(api_token))
    assert r.status_code == 200
    assert len(calls) == 1


def _seed_text(conn: psycopg.Connection, sha: str, text: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO attachment_text (sha256, extractor, extracted_text) "
            "VALUES (%s, 'pypdf', %s)",
            (bytes.fromhex(sha), text),
        )
    conn.commit()


def test_text_pages_by_index(
    db_dsn, api_token, db_conn, tmp_path, grant_alice_all_accounts,
) -> None:
    mid, shas = _seed(db_conn, tmp_path, _SAME_NAME)
    _seed_text(db_conn, shas[1], "second document")
    grant_alice_all_accounts()
    r = _client(db_dsn).get(
        f"/v1/messages/{mid}/attachments/1/text?offset=7&limit=4",
        headers=_auth(api_token),
    )
    assert r.status_code == 200
    assert r.json() == {
        "text": "docu", "offset": 7, "limit": 4, "total": 15, "next_offset": 11,
    }


def test_text_without_extraction_is_404(
    db_dsn, api_token, db_conn, tmp_path, grant_alice_all_accounts,
) -> None:
    mid, _ = _seed(db_conn, tmp_path, _SAME_NAME)
    grant_alice_all_accounts()
    r = _client(db_dsn).get(f"/v1/messages/{mid}/attachments/0/text", headers=_auth(api_token))
    assert r.status_code == 404


def test_a_bad_text_window_is_a_400_even_ungranted(
    db_dsn, api_token, db_conn, tmp_path,
) -> None:
    mid, _ = _seed(db_conn, tmp_path, _SAME_NAME)
    r = _client(db_dsn).get(
        f"/v1/messages/{mid}/attachments/0/text?limit=0", headers=_auth(api_token),
    )
    assert r.status_code == 400
    assert r.json()["type"] == "/problems/validation-failed"
```

- [ ] **Step 2: Run to verify they fail**

Run: `unset VIRTUAL_ENV && uv run pytest -q tests/test_serve_message_attachment_routes.py`
Expected: FAIL — the routes do not exist, so 404s where 200/206/400 are expected, and `AttributeError: … has no attribute '_open_blob_file_at'` in the spy tests.

- [ ] **Step 3: Implement**

In `src/localmail/serve/routes/messages.py`, extend the imports:

```python
from localmail.api.attachments import (
    _open_blob_file_at,
    get_attachment_blob_info,
    get_attachment_text_page,
    resolve_message_attachment,
    text_window_from_query,
)
from localmail.serve.routes.blob_response import blob_response, not_modified
```

Append:

```python
@router.get("/{message_id}/attachments/{index}")
def message_attachment(
    message_id: str,
    index: str,
    request: Request,
    user=Depends(get_authenticated_user),
) -> Response:
    """Attachment ``index`` (0-based, in ``get_message``'s order) of a message.

    The same bytes, Range, ETag and force-download rules as
    ``/v1/attachments/{sha256}``. The difference is the name: the
    ``Content-Disposition`` carries this entry's own filename, where the sha
    route can only pick one of the names a blob is carried under.
    """
    mid = parse_int_id(message_id, field="message_id")
    idx = parse_int_id(index, field="index")
    pool = request.app.state.pool
    with pool.connection() as conn:
        allowed = allowed_account_ids(conn, user.id)
        entry = resolve_message_attachment(
            conn, mid, idx, allowed_account_ids=allowed,
        )
        mime, size, path = get_attachment_blob_info(
            conn, entry.sha256, allowed_account_ids=allowed,
        )
        cached = not_modified(request, entry.sha256)
        if cached is not None:
            return cached
        fp = _open_blob_file_at(path, entry.sha256)
    return blob_response(
        request, sha256=entry.sha256, mime=mime, size=size, fp=fp,
        filename=entry.filename,
    )


@router.get("/{message_id}/attachments/{index}/text")
def message_attachment_text(
    message_id: str,
    index: str,
    request: Request,
    offset: str | None = Query(None),
    limit: str | None = Query(None),
    user=Depends(get_authenticated_user),
) -> dict[str, object]:
    """Extracted text of attachment ``index``, paged by character.

    Every parameter is judged before the connection opens, so a malformed
    request is a 400 even for a caller granted nothing.
    """
    mid = parse_int_id(message_id, field="message_id")
    idx = parse_int_id(index, field="index")
    window = text_window_from_query(offset, limit)
    pool = request.app.state.pool
    with pool.connection() as conn:
        allowed = allowed_account_ids(conn, user.id)
        entry = resolve_message_attachment(
            conn, mid, idx, allowed_account_ids=allowed,
        )
        page = get_attachment_text_page(
            conn, entry.sha256, allowed_account_ids=allowed, window=window,
        )
    return page.to_wire()
```

- [ ] **Step 4: Run to verify they pass**

Run: `unset VIRTUAL_ENV && uv run pytest -q tests/test_serve_message_attachment_routes.py tests/test_serve_messages_routes.py`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/localmail/serve/routes/messages.py tests/test_serve_message_attachment_routes.py
git commit -m "serve: address an attachment by its position in the message"
```

---

### Task 7: Announce it, prove it end to end, document it

**Files:**
- Modify: `src/localmail/serve/routes/version.py:17-20`
- Modify: `tests/test_serve_version_route.py`
- Create: `tests/test_attachment_index_acceptance.py`
- Modify: `README.md` (after the `headers=list` paragraph, ~line 850), `CLAUDE.md` (GUI server section, after the #379 bullet)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_serve_version_route.py`:

```python
def test_api_minor_announces_attachment_index_addressing(db_dsn: str) -> None:
    """An old server answers /v1/messages/{id}/attachments/0 with 404 — the
    same status a missing message or an index past the end gets. Only the
    version can tell the two apart."""
    body = _client(db_dsn).get("/v1/version").json()
    assert body["api_minor"] >= 2
```

`tests/test_attachment_index_acceptance.py`:

```python
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Slice D's acceptance, through the real ingestion path: two attachments of
the same filename are addressable by index, the text pages, and the sha route
is untouched (its golden is `test_serve_attachments_golden.py`)."""
from __future__ import annotations

from pathlib import Path

import psycopg
from fastapi.testclient import TestClient

from localmail.serve.app import create_app
from localmail.sync import process_one_message
from . import _eml


def _ingest(conn: psycopg.Connection, tmp_path: Path) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO accounts (name, email_address, imap_host, auth_method) "
            "VALUES ('acc', 'x@y.test', 'imap.x', 'password') RETURNING id",
        )
        row = cur.fetchone(); assert row is not None
        aid = int(row[0])
        cur.execute(
            "INSERT INTO mailboxes (account_id, name) VALUES (%s, 'INBOX') RETURNING id",
            (aid,),
        )
        row = cur.fetchone(); assert row is not None
        mb = int(row[0])
    mid, inserted = process_one_message(
        conn, account_id=aid, mailbox_id=mb, uid=1,
        raw=_eml.two_attachments_same_name(), flags=[],
        attachments_root=tmp_path,
    )
    assert inserted
    conn.commit()
    return mid


def test_two_attachments_of_one_name_are_addressable_by_index(
    db_dsn, api_token, db_conn, tmp_path, grant_alice_all_accounts,
) -> None:
    mid = _ingest(db_conn, tmp_path)
    grant_alice_all_accounts()
    c = TestClient(create_app(db_dsn=db_dsn, searcher=None))
    auth = {"Authorization": f"Bearer {api_token}"}

    listed = c.get(f"/v1/messages/{mid}", headers=auth).json()["attachments"]
    assert [a["filename"] for a in listed] == ["note.txt", "note.txt"]

    first = c.get(f"/v1/messages/{mid}/attachments/0", headers=auth)
    second = c.get(f"/v1/messages/{mid}/attachments/1", headers=auth)
    assert (first.content, second.content) == (b"file-one", b"file-two")
    # The index route agrees with the array get_message returns.
    assert first.headers["etag"] == f'"{listed[0]["sha256"]}"'
    assert second.headers["etag"] == f'"{listed[1]["sha256"]}"'
    assert 'filename="note.txt"' in second.headers["content-disposition"]


def test_the_text_of_an_indexed_attachment_pages(
    db_dsn, api_token, db_conn, tmp_path, grant_alice_all_accounts,
) -> None:
    mid = _ingest(db_conn, tmp_path)
    row = db_conn.execute(
        "SELECT attachments -> 1 ->> 'sha256' FROM messages WHERE id = %s", (mid,),
    ).fetchone()
    assert row is not None
    sha = row[0]
    db_conn.execute(
        "INSERT INTO attachment_text (sha256, extractor, extracted_text) "
        "VALUES (%s, 'lightweight', 'file-two')",
        (bytes.fromhex(sha),),
    )
    db_conn.commit()
    grant_alice_all_accounts()
    c = TestClient(create_app(db_dsn=db_dsn, searcher=None))
    auth = {"Authorization": f"Bearer {api_token}"}
    pages: list[str] = []
    offset: int | None = 0
    while offset is not None:
        body = c.get(
            f"/v1/messages/{mid}/attachments/1/text?offset={offset}&limit=3",
            headers=auth,
        ).json()
        pages.append(body["text"])
        offset = body["next_offset"]
    assert pages == ["fil", "e-t", "wo"]
```

(`from . import _eml` is the form `tests/test_attachments.py` uses — `tests/` is a package.)

- [ ] **Step 2: Run to verify they fail**

Run: `unset VIRTUAL_ENV && uv run pytest -q tests/test_serve_version_route.py tests/test_attachment_index_acceptance.py`
Expected: `test_api_minor_announces_attachment_index_addressing` FAILS (`1 >= 2`); the acceptance tests pass already (Tasks 3–6 built them) — that is fine, they are the end-to-end proof, not a red step.

- [ ] **Step 3: Bump the version**

`src/localmail/serve/routes/version.py`:

```python
API_MAJOR = 1
# 1: GET /v1/messages/{id}?headers=list (#379). An older server answers an
# unknown mode with 200 and no headers key, so this is the only feature signal.
# 2: GET /v1/messages/{id}/attachments/{index}[/text]. An older server answers
# 404, which a client cannot tell from a missing message or index.
API_MINOR = 2
```

- [ ] **Step 4: Document it**

README.md, directly after the paragraph ending "…answers the unknown mode with 200 and no `headers` key.":

```markdown
`GET /v1/messages/{id}/attachments/{index}` serves attachment `index` — its
0-based position in the `attachments` array `GET /v1/messages/{id}` returns —
with the same bytes, `Range`, `ETag` and download headers as
`GET /v1/attachments/{sha256}`. Use it when a message carries two attachments
of one filename, or when the download should keep *this* entry's name: a blob
is content-addressed, so the hash route can only offer one of the names it is
carried under. The index counts inline parts too, exactly as the array does.
An index past the end is a 404.

Extracted text is paged by **character** on both
`GET /v1/attachments/{sha256}/text` and
`GET /v1/messages/{id}/attachments/{index}/text`: pass `offset` and `limit`,
and read the next page from `next_offset` (`null` at the end). Omitting both
returns the whole text, as before. Do not compute the next offset from
`text.length` in JavaScript — it counts UTF-16 units, not characters, and
skips text on any page containing a character above U+FFFF.

```json
{"text": "…", "offset": 0, "limit": 20000, "total": 128875, "next_offset": 20000}
```

Both are announced by `api_minor >= 2`: an older server answers the index
route with a 404 indistinguishable from a missing message.
```

CLAUDE.md, in the GUI server section directly after the bullet beginning
"**Message headers are served per occurrence, in wire order (#379).**", add:

```markdown
- **Attachments are addressable by position (kastellan slice D).** Design:
  [docs/superpowers/specs/2026-09-17-message-attachment-index-design.md](docs/superpowers/specs/2026-09-17-message-attachment-index-design.md).
  `GET /v1/messages/{id}/attachments/{index}[/text]`; `api_minor` is **2**.
  The resolver (`api.attachments.resolve_message_attachment`) reads
  `m.attachments -> %s::int` under the **message** ACL. Two index values never
  reach it, both measured: a **negative** index is refused (Postgres `->`
  indexes from the end, so `-> -1` is the *last* entry — `parse_int_id` refuses
  it on the wire, and the accessor refuses it again for library callers), and
  one past **`MAX_JSONB_INDEX`** (int4) is a 404 decided without a query
  (`jsonb -> bigint` does not exist, so it was a 500).
  - **The only behavioural difference from the sha route is the name.** The
    `Content-Disposition` carries the resolved entry's own filename. Measured:
    888 in-message `(message, sha)` groups carry one blob under two names, and
    1,109 blobs carry several names across messages; `get_attachment_filename`
    picks the earliest carrier, so the sha route is wrong for the rest.
  - **The streaming rules live in `serve/routes/blob_response.py`**, shared by
    both routes. **The file open stays at each call site**, after
    `not_modified`: the #62 tests spy on each route module's
    `_open_blob_file_at`, and an open moved into the helper would make every
    `calls == []` pass whether or not a file was opened. Each spy has a
    positive control proving it intercepts.
  - **Text is paged by character** through the pure
    [src/localmail/text_window.py](src/localmail/text_window.py), on both text
    routes. `next_offset` is server-computed because JS `.length` counts UTF-16
    units — 8 of 9,303 live extractions contain astral characters. Offsets and
    limits are clamped at `MAX_TEXT_CHARS` (2³⁰) because `substring()` takes
    int4, which changes no answer. **A window is not cheaper in the database**:
    the text is TOAST-compressed (the 2.1 MB maximum is stored as 388 KB), so
    every window decompresses all of it — 3 ms first page, 8 ms last, against
    2 ms for the whole. Paging buys response size, not DB time.
  - **MCP is untouched**: `get_attachment_text` keeps its signature and
    delegates to `get_attachment_text_page(…, window=TextWindow())`.
```

- [ ] **Step 5: Run the whole suite, mypy, ruff**

Run, each in the foreground:
```bash
unset VIRTUAL_ENV && uv run pytest -q
unset VIRTUAL_ENV && uv run mypy src/localmail
unset VIRTUAL_ENV && uv run ruff check src/localmail/ | tail -1
```
Expected: pytest all passed, 2 warnings (the #25 websockets pair) — report the pass count; mypy `Success`; ruff `10` errors (the #285 baseline, unchanged). **Exactly three LISTEN/NOTIFY failures with `could not access status of transaction` are the stale NOTIFY queue (CLAUDE.md), not this change — report them by name and stop; do not "fix" them.**

- [ ] **Step 6: Commit**

```bash
git add src/localmail/serve/routes/version.py tests/test_serve_version_route.py tests/test_attachment_index_acceptance.py README.md CLAUDE.md
git commit -m "Announce attachment index addressing as api_minor 2; acceptance and docs"
```
