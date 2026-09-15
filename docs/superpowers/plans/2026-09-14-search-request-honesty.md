# Search Request Honesty Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `POST /v1/search` and the MCP `search` tool honour or refuse every filter a caller states — never silently drop one — and make every hit's `has_attachments` describe the message.

**Architecture:** One SQL constant decides whether a message has attachments, and the filter and both hit-building SELECTs compose it. The DSL gains `has:no-attachment` so `has_attachment: false` can travel through the composed query string, and unknown `has:` values raise. `build_query_string` refuses unknown filter keys by name, and `run_search` calls it before the empty-ACL short-circuit. The HTTP route refuses unknown top-level fields and makes `query` optional; the MCP tool makes `query` optional too.

**Tech Stack:** Python 3.12+/3.13, FastAPI + Pydantic v2, psycopg 3, PostgreSQL (pgvector), pytest.

**Spec:** [docs/superpowers/specs/2026-09-14-search-request-honesty-design.md](../specs/2026-09-14-search-request-honesty-design.md)

## Global Constraints

- **Work on branch `fix/364-search-honesty`** (already created from `origin/main`, with no upstream). Never push to `main`. The work lands through one PR, and the operator merges it.
- **Every Python command runs as `unset VIRTUAL_ENV && uv run --no-sync …`.** On this Mac the repo's `.venv` is the production venv, and a bare `uv run` or `uv sync` can prune the `mcp`/`extraction` extras from the running services.
- **Run only one pytest at a time.** The suite takes a Postgres advisory lock on `localmail_test`, and a second session waits, then exits.
- **Undo a temporary mutation by restoring a snapshot, never with `git checkout <file>`.** Before mutating, run `cp FILE "${TMPDIR:-/tmp}/FILE.orig"`, then copy it back.
- **Commit messages end with** `Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>`.
- **No migration**, and no new dependency. `difflib` is stdlib.
- **Wire strings must be exactly:**
  - `filters: unknown key 'has_attachments' (did you mean 'has_attachment'?); supported: account_ids, after, before, date_from, date_to, folder_ids, from, has_attachment, lang, subject, to`
  - `unknown field 'order'; supported: cursor, filters, limit, query, smart, sort, sort_order`
  - `has: expected 'attachment' or 'no-attachment', got 'attachments'`
  - `has: 'attachment' and 'no-attachment' contradict each other`
  - `has_attachment: expected true, false or null, got 'yes'`
- **Semantics unchanged:** "has attachments" still means "`messages.attachments` is a non-empty array". Narrowing it is #365, not this plan.
- **Comments explain *why*, never *what*.** Match the density of the surrounding code.

---

### Task 1: One "has attachments" rule, and the filter composes it

**Files:**
- Create: `src/localmail/search/attachment_presence.py`
- Modify: `src/localmail/search/arms.py` (imports near line 20; `_filter_sql` lines 60-63)
- Test: `tests/test_search_attachment_presence.py`

**Interfaces:**
- Consumes: `localmail.search.arms._filter_sql(filters: SearchFilters) -> tuple[str, list[Any]]` (returns `" AND …"`, or `""`)
- Produces: `localmail.search.attachment_presence.HAS_ATTACHMENT_SQL: str`, a parenthesised boolean SQL expression over alias `m` that is never NULL

- [ ] **Step 0: Record a baseline on the clean branch**

Run: `cd /Users/hherb/src/localmail && git status --short && unset VIRTUAL_ENV && uv run --no-sync pytest -q 2>&1 | tail -3`

Expected: `git status` prints nothing; pytest ends with `N passed` (and possibly a couple of pre-existing `websockets` deprecation warnings). Write N down; Task 8 compares against it.

- [ ] **Step 1: Write the failing test**

Create `tests/test_search_attachment_presence.py`:

```python
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""The one "has attachments" rule, and its guard against malformed rows (#364)."""
from __future__ import annotations

from datetime import datetime, timezone

import psycopg
import pytest

from localmail.search.arms import _filter_sql
from localmail.search.attachment_presence import HAS_ATTACHMENT_SQL
from localmail.search.query import SearchFilters

_PDF = '[{"filename": "ticket.pdf", "sha256": "' + "ab" * 32 + '"}]'


def _seed(conn: psycopg.Connection) -> dict[str, int]:
    """One message per shape the rule must decide: with, without, malformed.

    ``malformed`` is an object where an array belongs. No writer produces it,
    but a restore or a hand UPDATE can, and the column has no CHECK.
    """
    ids: dict[str, int] = {}
    with conn.cursor() as cur:
        cur.execute("INSERT INTO accounts (name, email_address, imap_host, auth_method)"
                    " VALUES ('a', 'a@x', 'h', 'password') RETURNING id")
        row = cur.fetchone()
        assert row is not None
        acct = row[0]
        for i, (key, attachments) in enumerate(
            [("with", _PDF), ("without", "[]"), ("malformed", "{}")]
        ):
            cur.execute(
                "INSERT INTO messages (account_id, message_id, raw_sha256, subject,"
                " body_text, headers, raw_bytes, size_bytes, attachments, internal_date)"
                " VALUES (%s, %s, %s, %s, 'body', '{}'::jsonb, 'r', 1, %s::jsonb, %s)"
                " RETURNING id",
                (acct, f"<{key}>", bytes([i + 1]) * 32, f"Subject {key}", attachments,
                 datetime(2026, 3, i + 1, tzinfo=timezone.utc)),
            )
            row = cur.fetchone()
            assert row is not None
            ids[key] = row[0]
    conn.commit()
    return ids


def test_the_rule_decides_each_shape(db_conn) -> None:
    ids = _seed(db_conn)
    with db_conn.cursor() as cur:
        cur.execute(f"SELECT m.id, {HAS_ATTACHMENT_SQL} FROM messages m")
        got = dict(cur.fetchall())
    assert got == {ids["with"]: True, ids["without"]: False, ids["malformed"]: False}


@pytest.mark.parametrize("wanted, expected_keys", [
    (True, {"with"}),
    (False, {"without", "malformed"}),
])
def test_the_filter_is_the_rule_and_its_exact_complement(
    db_conn, wanted, expected_keys,
) -> None:
    ids = _seed(db_conn)
    fragment, params = _filter_sql(SearchFilters(has_attachment=wanted))
    with db_conn.cursor() as cur:
        cur.execute(f"SELECT m.id FROM messages m WHERE TRUE{fragment}", params)
        got = {row[0] for row in cur.fetchall()}
    assert got == {ids[k] for k in expected_keys}


def test_the_unguarded_expression_fails_on_the_malformed_row(db_conn) -> None:
    """Negative control: the fixture really reaches the failure the guard exists for.

    Without it, a guard that did nothing would pass both tests above on a
    fixture that never put a non-array in front of ``jsonb_array_length``.
    """
    _seed(db_conn)
    with db_conn.cursor() as cur, pytest.raises(psycopg.errors.InvalidParameterValue):
        cur.execute("SELECT jsonb_array_length(m.attachments) > 0 FROM messages m")
    db_conn.rollback()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `unset VIRTUAL_ENV && uv run --no-sync pytest tests/test_search_attachment_presence.py -q`

Expected: a collection error, `ModuleNotFoundError: No module named 'localmail.search.attachment_presence'`.

- [ ] **Step 3: Create the module**

Create `src/localmail/search/attachment_presence.py`:

```python
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""The one rule for "this message has attachments" (#364).

Three consumers compose it: the ``has_attachment`` filter in
``arms._filter_sql``, the hybrid path's hydration SELECT in
``Searcher._hydrate``, and the date walk's ``date_keyset.ROW_SQL_TEMPLATE``.
Before #364 the hit flag had its own rule ("the matched chunk was an
attachment's"), so a filtered search returned hits flagged ``false``. #365
narrows what counts (images embedded in the HTML body) by editing this
constant, and the filter and the flag move together.

It has its own module because ``arms`` imports ``searcher`` and ``searcher``
imports ``date_keyset`` at module level, so ``date_keyset`` taking this from
``arms`` would close an import cycle.
"""
from __future__ import annotations

#: True iff ``m.attachments`` is a non-empty array. Never NULL, so ``NOT``
#: of it is an exact complement.
#:
#: Guarded because ``jsonb_array_length`` raises 22023 on a non-array, and
#: ``messages.attachments`` is ``JSONB NOT NULL DEFAULT '[]'`` with no CHECK.
#: Unguarded, one such row fails every search page that surfaces it. A
#: ``CASE`` rather than ``AND``, because Postgres does not promise the order
#: in which ``AND``'s operands are evaluated. Contains no ``{}`` or ``%``, so
#: it composes safely into ``str.format`` templates and psycopg statements.
HAS_ATTACHMENT_SQL = (
    "(CASE WHEN jsonb_typeof(m.attachments) = 'array'"
    " THEN jsonb_array_length(m.attachments) > 0 ELSE FALSE END)"
)
```

- [ ] **Step 4: Compose it in `_filter_sql`**

In `src/localmail/search/arms.py`, add below `from localmail.config import SearchConfig`:

```python
from localmail.search.attachment_presence import HAS_ATTACHMENT_SQL
```

Replace:

```python
    if filters.has_attachment is True:
        parts.append("jsonb_array_length(m.attachments) > 0")
    if filters.has_attachment is False:
        parts.append("jsonb_array_length(m.attachments) = 0")
```

with:

```python
    if filters.has_attachment is True:
        parts.append(HAS_ATTACHMENT_SQL)
    if filters.has_attachment is False:
        parts.append(f"NOT {HAS_ATTACHMENT_SQL}")
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `unset VIRTUAL_ENV && uv run --no-sync pytest tests/test_search_attachment_presence.py tests/test_mcp_filter_semantics.py -q`

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add src/localmail/search/attachment_presence.py src/localmail/search/arms.py tests/test_search_attachment_presence.py
git commit -m "search: one guarded rule for 'this message has attachments' (#364)

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: A hit's `has_attachments` describes the message

**Files:**
- Modify: `src/localmail/search/searcher.py`:
  - `SearchResult` near line 418;
  - `_hydrate` messages SELECT near lines 913-919;
  - `_build_results`' `SearchResult(...)` near line 1064;
  - the date walk's result loop and `next_keyset` unpack near lines 780-795;
  - imports near line 68.
- Modify: `src/localmail/search/date_keyset.py` (`ROW_SQL_TEMPLATE` near line 110; imports near line 25)
- Modify: `src/localmail/api/search.py` (`_to_api_result`, the `"has_attachments"` line)
- Modify (test fakes the new field breaks):
  - `tests/test_serve_search_route.py` (`_fake_searcher_returning_one_hit`, `test_search_returns_results`);
  - `tests/test_serve_acl_routes.py` (`_account_scoped_fake_searcher`, plus a new helper beside `_assert_wire_ordering_fields`);
  - `tests/test_api_search_pagination.py` (`_result`);
  - `tests/test_api_search.py` (two `SearchResult(...)` constructions, one MagicMock fake).
- Test: `tests/test_search_has_attachments_flag.py`

**Interfaces:**
- Consumes: `HAS_ATTACHMENT_SQL` (Task 1)
- Produces: `SearchResult.has_attachments: bool` (defaultless); the wire key `"has_attachments"` reads it

- [ ] **Step 1: Write the failing tests**

Create `tests/test_search_has_attachments_flag.py`:

```python
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""A hit's ``has_attachments`` describes the message, not the matched chunk (#364).

It was ``attachment_filename is not None``, i.e. "the text that matched came
from an attachment", so a message carrying two PDFs matched through its
subject reported ``false``. That is the evidence in #364's thread: the flag
was true only on hits whose ``matched_arms`` was ``attachment_chunks``.
"""
from __future__ import annotations

from datetime import datetime, timezone

import psycopg
from fastapi.testclient import TestClient

from localmail.api.search import _to_api_result
from localmail.config import SearchConfig
from localmail.db import open_pool
from localmail.search.embed_worker import run_embed_worker_once
from localmail.search.searcher import Searcher, SearchResult
from localmail.serve.app import create_app

_PDF = '[{"filename": "e-ticket.pdf", "sha256": "' + "cd" * 32 + '"}]'


class _Embedder:
    name = "stub"
    model = "stub"
    dimension = 768

    def embed_documents(self, texts):
        return [[1.0 / (i + 1)] * 768 for i, _ in enumerate(texts)]

    def embed_query(self, t):
        return [0.5] * 768

    def health_check(self):
        pass


def _seed(conn: psycopg.Connection, *, malformed: bool) -> tuple[int, dict[str, int]]:
    """Messages that match "Berlin" through their subject and body only.

    No ``attachment_text`` rows are seeded, so no hit can come from an
    attachment chunk. A flag that is true here can only have come from the
    message itself, which is the distinction #364 is about. The malformed
    row is optional because the embed worker's chunking has no business
    being asked about it; the date walk does.
    """
    shapes = [("ticket", _PDF), ("lunch", "[]")]
    if malformed:
        shapes.append(("malformed", "{}"))
    ids: dict[str, int] = {}
    with conn.cursor() as cur:
        cur.execute("INSERT INTO accounts (name, email_address, imap_host, auth_method)"
                    " VALUES ('a', 'a@x', 'h', 'password') RETURNING id")
        row = cur.fetchone()
        assert row is not None
        acct = int(row[0])
        for i, (key, attachments) in enumerate(shapes):
            cur.execute(
                "INSERT INTO messages (account_id, message_id, raw_sha256, subject,"
                " body_text, headers, raw_bytes, size_bytes, attachments, internal_date)"
                " VALUES (%s, %s, %s, %s, %s, '{}'::jsonb, 'r', 1, %s::jsonb, %s)"
                " RETURNING id",
                (acct, f"<{key}>", bytes([i + 1]) * 32, f"Berlin {key}",
                 f"Berlin {key} details", attachments,
                 datetime(2026, 3, i + 1, tzinfo=timezone.utc)),
            )
            row = cur.fetchone()
            assert row is not None
            ids[key] = int(row[0])
    conn.commit()
    return acct, ids


def _result(*, has_attachments: bool, attachment_filename: str | None) -> SearchResult:
    return SearchResult(
        message_id=1, account_id=1, rank=1, score=0.5, rrf_score=0.5,
        subject="s", from_addr="a@b", from_name="A",
        date_sent=None, internal_date=None,
        snippet="", snippet_source="body",
        attachment_filename=attachment_filename, has_attachments=has_attachments,
        matched_chunk_id=None, matched_chunk_table="message_chunks",
    )


def test_the_wire_field_is_the_result_field_not_the_snippet_source() -> None:
    assert _to_api_result(
        _result(has_attachments=True, attachment_filename=None))["has_attachments"] is True
    assert _to_api_result(
        _result(has_attachments=False, attachment_filename="x.pdf"))["has_attachments"] is False


def test_the_date_walk_flags_the_message(db_dsn, db_conn) -> None:
    _, ids = _seed(db_conn, malformed=True)
    pool = open_pool(db_dsn)
    try:
        searcher = Searcher(pool=pool, cfg=SearchConfig(), embeddings=None,
                            reranker=None, rewriter=None)
        page = searcher.search("", allowed_account_ids=None)
    finally:
        pool.close()
    assert {r.message_id: r.has_attachments for r in page.results} == {
        ids["ticket"]: True, ids["lunch"]: False, ids["malformed"]: False,
    }


def test_the_hybrid_path_flags_the_message_not_the_matched_chunk(db_dsn, db_conn) -> None:
    _, ids = _seed(db_conn, malformed=False)
    cfg = SearchConfig()
    run_embed_worker_once(db_conn, cfg, _Embedder())
    pool = open_pool(db_dsn)
    try:
        searcher = Searcher(pool=pool, cfg=cfg, embeddings=_Embedder(),
                            reranker=None, rewriter=None)
        page = searcher.search("Berlin", allowed_account_ids=None)
    finally:
        pool.close()
    assert page.sort_applied == "rank"
    by_id = {r.message_id: r for r in page.results}
    assert set(by_id) == {ids["ticket"], ids["lunch"]}
    # The old rule read the matched chunk, and no chunk here is an attachment's.
    assert by_id[ids["ticket"]].matched_chunk_table != "attachment_chunks"
    assert by_id[ids["ticket"]].has_attachments is True
    assert by_id[ids["lunch"]].has_attachments is False


def test_the_flag_reaches_the_wire_through_the_real_route(
    db_dsn, db_conn, api_user, api_token,
) -> None:
    acct, ids = _seed(db_conn, malformed=False)
    with db_conn.cursor() as cur:
        cur.execute("INSERT INTO user_accounts (user_id, account_id) VALUES (%s, %s)",
                    (api_user.id, acct))
    db_conn.commit()
    pool = open_pool(db_dsn)
    try:
        searcher = Searcher(pool=pool, cfg=SearchConfig(), embeddings=None,
                            reranker=None, rewriter=None)
        client = TestClient(create_app(db_dsn=db_dsn, searcher=searcher))
        r = client.post("/v1/search", json={"query": "", "filters": {}, "limit": 20},
                        headers={"Authorization": f"Bearer {api_token}"})
    finally:
        pool.close()
    assert r.status_code == 200, r.text
    got = {int(h["message_id"]): h["has_attachments"] for h in r.json()["results"]}
    assert got == {ids["ticket"]: True, ids["lunch"]: False}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `unset VIRTUAL_ENV && uv run --no-sync pytest tests/test_search_has_attachments_flag.py -q`

Expected: FAIL. `_result(...)` raises `TypeError: SearchResult.__init__() got an unexpected keyword argument 'has_attachments'`; the seeded tests fail with `AttributeError: 'SearchResult' object has no attribute 'has_attachments'`, or with the wire dict comparison.

- [ ] **Step 3: Add the field and populate it on both paths**

In `src/localmail/search/searcher.py`, in `class SearchResult`, replace:

```python
    attachment_filename: str | None
```

with:

```python
    attachment_filename: str | None
    #: Whether the *message* carries attachments (``HAS_ATTACHMENT_SQL``),
    #: whatever matched. Defaultless: a result that could claim ``False`` by
    #: omission is #364, where the wire flag was derived from
    #: ``attachment_filename`` — the matched chunk's source.
    has_attachments: bool
```

Add below the `from localmail.search.date_keyset import (...)` block:

```python
from localmail.search.attachment_presence import HAS_ATTACHMENT_SQL
```

In `_hydrate`, replace:

```python
            cur.execute(
                "SELECT id, account_id, subject, from_addr, from_name, date_sent,"
                " internal_date, body_text FROM messages WHERE id = ANY(%s)", (msg_ids,))
            for mid, acct, subj, fa, fn, ds, intd, body in cur.fetchall():
                msgs[mid] = {"account_id": acct, "subject": subj, "from_addr": fa,
                             "from_name": fn, "date_sent": ds, "internal_date": intd,
                             "body_text": body}
```

with:

```python
            cur.execute(
                "SELECT m.id, m.account_id, m.subject, m.from_addr, m.from_name,"
                " m.date_sent, m.internal_date, m.body_text,"
                f" {HAS_ATTACHMENT_SQL}"
                " FROM messages m WHERE m.id = ANY(%s)", (msg_ids,))
            for mid, acct, subj, fa, fn, ds, intd, body, has_att in cur.fetchall():
                msgs[mid] = {"account_id": acct, "subject": subj, "from_addr": fa,
                             "from_name": fn, "date_sent": ds, "internal_date": intd,
                             "body_text": body, "has_attachments": has_att}
```

In `_build_results`, replace:

```python
                attachment_filename=attachment_filename,
                matched_chunk_id=h.best_chunk_id,
```

with:

```python
                attachment_filename=attachment_filename,
                # `m` is `{}` for a message deleted between retrieval and
                # hydration, the same case `account_id`'s default above covers;
                # a message that no longer exists carries no attachments.
                has_attachments=bool(m.get("has_attachments", False)),
                matched_chunk_id=h.best_chunk_id,
```

In `_date_keyset_search`, replace:

```python
        for rank, (mid, account_id, subject, from_addr, from_name,
                   date_sent, internal_date) in enumerate(page_rows, start=1):
```

with:

```python
        for rank, (mid, account_id, subject, from_addr, from_name,
                   date_sent, internal_date, has_attachments) in enumerate(
                       page_rows, start=1):
```

In the `SearchResult(...)` just below it, replace:

```python
                attachment_filename=None, matched_chunk_id=None,
```

with:

```python
                attachment_filename=None, has_attachments=has_attachments,
                matched_chunk_id=None,
```

Replace:

```python
            last_id, _, _, _, _, last_date_sent, last_internal_date = page_rows[-1]
```

with:

```python
            last_id, _, _, _, _, last_date_sent, last_internal_date, _ = page_rows[-1]
```

- [ ] **Step 4: Add the column to the date walk's template**

In `src/localmail/search/date_keyset.py`, add below `from localmail.search.sort_axes import SortOrder`:

```python
from localmail.search.attachment_presence import HAS_ATTACHMENT_SQL
```

Replace:

```python
ROW_SQL_TEMPLATE = """
            SELECT m.id, m.account_id, m.subject, m.from_addr, m.from_name,
                   m.date_sent, m.internal_date
              FROM messages m
             WHERE {where}
             {order_by}
             LIMIT %s
"""
```

with:

```python
ROW_SQL_TEMPLATE = (
    """
            SELECT m.id, m.account_id, m.subject, m.from_addr, m.from_name,
                   m.date_sent, m.internal_date, """
    # Composed, never restated: the filter reads the same constant (#364).
    # It holds no braces, so the `.format` below leaves it alone.
    + HAS_ATTACHMENT_SQL
    + """
              FROM messages m
             WHERE {where}
             {order_by}
             LIMIT %s
"""
)
```

- [ ] **Step 5: Emit the field on the wire**

In `src/localmail/api/search.py::_to_api_result`, replace:

```python
        "has_attachments": r.attachment_filename is not None,
```

with:

```python
        "has_attachments": r.has_attachments,
```

- [ ] **Step 6: Update the test fakes and add the wire check**

In `tests/test_serve_search_route.py::_fake_searcher_returning_one_hit`, after `result.attachment_filename = None` add:

```python
    result.has_attachments = False
```

In `test_search_returns_results`, after `assert body["results"][0]["message_id"] == "7"` add:

```python
    assert body["results"][0]["has_attachments"] is False
```

In `tests/test_serve_acl_routes.py::_account_scoped_fake_searcher`, after `r.attachment_filename = None` add:

```python
                r.has_attachments = False
```

In the same file, directly below the `_assert_wire_ordering_fields` function, add:

```python
def _assert_wire_hit_fields(body: dict) -> None:
    """Every hit carries a real ``has_attachments`` (#364).

    Type, not a value, for the reason ``_assert_wire_ordering_fields``
    gives: a fake that never set the attribute serialises it as ``{}`` or
    ``[]`` instead of failing.
    """
    for hit in body["results"]:
        assert isinstance(hit["has_attachments"], bool), hit["has_attachments"]
```

Then, in `test_search_isolates_alice_from_bob_messages`, after each of the two `_assert_wire_ordering_fields(r.json())` lines add:

```python
    _assert_wire_hit_fields(r.json())
```

In `tests/test_api_search_pagination.py::_result`, after `r.attachment_filename = None` add:

```python
    r.has_attachments = False
```

In `tests/test_api_search.py`, in both `SearchResult(...)` constructions, replace:

```python
        attachment_filename=None, matched_chunk_id=None,
```

with:

```python
        attachment_filename=None, has_attachments=False, matched_chunk_id=None,
```

In `test_run_search_calls_searcher_and_maps_results`, after `fake_result.attachment_filename = None` add:

```python
    fake_result.has_attachments = False
```

- [ ] **Step 7: Run the tests to verify they pass**

Run: `unset VIRTUAL_ENV && uv run --no-sync pytest tests/test_search_has_attachments_flag.py tests/test_serve_search_route.py tests/test_serve_acl_routes.py tests/test_api_search_pagination.py tests/test_api_search.py tests/test_searcher_sort_order_plan.py tests/test_date_keyset.py tests/test_search_public_api.py tests/test_cli_search.py -q`

Expected: all pass. The two plan-test files are included because the template they EXPLAIN gained a column.

- [ ] **Step 8: Prove the malformed-row case is load-bearing**

A test is only a pin if the unguarded form fails it. Run:

```bash
cp src/localmail/search/attachment_presence.py "${TMPDIR:-/tmp}/attachment_presence.py.orig"
python3 - <<'EOF'
import pathlib
p = pathlib.Path("src/localmail/search/attachment_presence.py")
s = p.read_text()
old = ('    "(CASE WHEN jsonb_typeof(m.attachments) = \'array\'"\n'
       '    " THEN jsonb_array_length(m.attachments) > 0 ELSE FALSE END)"\n')
assert old in s, "mutation did not apply"
p.write_text(s.replace(old, '    "(jsonb_array_length(m.attachments) > 0)"\n'))
EOF
unset VIRTUAL_ENV && uv run --no-sync pytest tests/test_search_has_attachments_flag.py::test_the_date_walk_flags_the_message -q 2>&1 | tail -3
cp "${TMPDIR:-/tmp}/attachment_presence.py.orig" src/localmail/search/attachment_presence.py
git diff --stat src/localmail/search/attachment_presence.py
```

Expected: the mutated run FAILS with `InvalidParameterValue` (`cannot get array length of a non-array`). The final `git diff --stat` prints nothing for that file, because it was restored and already committed in Task 1.

- [ ] **Step 9: Commit**

```bash
git add src/localmail/search/searcher.py src/localmail/search/date_keyset.py src/localmail/api/search.py tests/test_search_has_attachments_flag.py tests/test_serve_search_route.py tests/test_serve_acl_routes.py tests/test_api_search_pagination.py tests/test_api_search.py
git commit -m "search: a hit's has_attachments describes the message, not the matched chunk (#364)

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: `has_attachment: false` is honoured; an unknown `has:` value is refused

**Files:**
- Modify: `src/localmail/search/query.py` (module docstring; add `_HAS_VALUES` and `_parse_has` above `parse_query`; the `elif op_l == "has":` branch)
- Modify: `src/localmail/api/search.py` (`_filter_tokens`, the final `has_attachment` lines)
- Modify: `tests/test_query_parser.py`, `tests/test_api_search.py`, `tests/test_mcp_tools.py`
- Test: `tests/test_search_has_attachments_flag.py` (append the differential test)

**Interfaces:**
- Consumes: `SearchResult.has_attachments` / wire `"has_attachments"` (Task 2); `run_search(...)`
- Produces: DSL value `has:no-attachment` → `SearchFilters.has_attachment = False`. `_filter_tokens` raises `ValidationFailed` for a non-bool, non-None `has_attachment`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_query_parser.py`:

```python
def test_has_no_attachment_flag():
    q = parse_query("invoice has:no-attachment")
    assert q.free_text == "invoice"
    assert q.filters.has_attachment is False


def test_has_values_are_case_insensitive():
    assert parse_query("has:No-Attachment").filters.has_attachment is False
    assert parse_query("has:ATTACHMENT").filters.has_attachment is True


def test_an_unknown_has_value_is_refused_not_dropped():
    """It used to vanish from the query — not a filter, and not free text."""
    with pytest.raises(
        QueryParseError,
        match="has: expected 'attachment' or 'no-attachment', got 'attachments'",
    ):
        parse_query("invoice has:attachments")


def test_contradictory_has_values_are_refused():
    with pytest.raises(
        QueryParseError,
        match="has: 'attachment' and 'no-attachment' contradict each other",
    ):
        parse_query("has:attachment has:no-attachment")


def test_a_repeated_has_value_is_accepted():
    assert parse_query("has:attachment has:attachment").filters.has_attachment is True
```

Append to `tests/test_api_search.py`:

```python
def test_build_query_string_emits_no_attachment_for_false() -> None:
    assert build_query_string(
        free_text="x", filters={"has_attachment": False},
    ) == "x has:no-attachment"


@pytest.mark.parametrize("value", ["yes", "true", 1, 0])
def test_a_non_boolean_has_attachment_is_refused(value) -> None:
    with pytest.raises(ValidationFailed,
                       match="has_attachment: expected true, false or null"):
        build_query_string(free_text="x", filters={"has_attachment": value})
```

In the same file, in the `filters` parametrize list of `test_build_query_string_is_free_text_neutral`, add a line after `{"has_attachment": True},`:

```python
    {"has_attachment": False},
```

Append to `tests/test_search_has_attachments_flag.py` (and add `import pytest` and `from localmail.api.search import run_search` to its imports):

```python
@pytest.mark.parametrize("free_text", ["", "Berlin"])
def test_the_filter_selects_exactly_the_flagged_hits(db_dsn, db_conn, free_text) -> None:
    """One rule, checked by behaviour: the filter and the flag cannot disagree.

    ``""`` drives the date walk and ``"Berlin"`` the hybrid pool, so both
    places a hit is built are covered.
    """
    acct, _ = _seed(db_conn, malformed=False)
    cfg = SearchConfig()
    run_embed_worker_once(db_conn, cfg, _Embedder())
    pool = open_pool(db_dsn)
    try:
        searcher = Searcher(pool=pool, cfg=cfg, embeddings=_Embedder(),
                            reranker=None, rewriter=None)

        def flags(filters: dict) -> dict[str, bool]:
            page = run_search(searcher=searcher, free_text=free_text, filters=filters,
                              limit=50, allowed_account_ids=[acct], user_id=1)
            return {h["message_id"]: h["has_attachments"] for h in page["results"]}

        everything = flags({})
        with_attachments = flags({"has_attachment": True})
        without_attachments = flags({"has_attachment": False})
    finally:
        pool.close()
    assert with_attachments and without_attachments, (
        "both halves must be non-empty, or this proves nothing", everything)
    assert set(with_attachments) == {m for m, flag in everything.items() if flag}
    assert set(without_attachments) == {m for m, flag in everything.items() if not flag}
```

Append to `tests/test_mcp_tools.py`:

```python
def test_tool_search_honours_has_attachment_false(db_dsn, db_conn):
    """The tool's description has promised this since it shipped; it was dropped."""
    uid = create_user(db_conn, "carol", "hunter2")
    acct = _insert_account(db_conn, "carol-acct")
    grant_account(db_conn, uid, acct)
    plain = _insert_message(db_conn, acct, "invoice plain", "the invoice")
    attached = _insert_message(db_conn, acct, "invoice attached", "the invoice, attached")
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE messages SET attachments = %s::jsonb WHERE id = %s",
            ('[{"filename": "invoice.pdf", "sha256": "' + "ef" * 32 + '"}]', attached),
        )
    db_conn.commit()
    searcher = _lexical_searcher(db_dsn)
    try:
        page = tools.tool_search(
            searcher=searcher, user_id=uid, allowed_account_ids=[acct],
            query="", limit=20, cursor=None, filters={"has_attachment": False},
        )
    finally:
        searcher._pool.close()
    assert {int(r["message_id"]) for r in page["results"]} == {plain}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `unset VIRTUAL_ENV && uv run --no-sync pytest tests/test_query_parser.py tests/test_api_search.py tests/test_search_has_attachments_flag.py tests/test_mcp_tools.py -q`

Expected failures:
- `test_has_no_attachment_flag`: `has_attachment` is `None`.
- The two refusal tests and the non-boolean tests: DID NOT RAISE.
- `test_build_query_string_emits_no_attachment_for_false`: `'x' != 'x has:no-attachment'`.
- The differential test: `without_attachments` equals `everything`.
- `test_tool_search_honours_has_attachment_false`: both ids returned.

- [ ] **Step 3: Implement the DSL value**

In `src/localmail/search/query.py`, in the module docstring, replace the line

```
    has:attachment
```

with:

```
    has:attachment / has:no-attachment
```

Add, directly above `def parse_query`:

```python
#: ``has:`` values and what each sets ``SearchFilters.has_attachment`` to.
_HAS_VALUES: dict[str, bool] = {"attachment": True, "no-attachment": False}


def _parse_has(value: str, current: bool | None) -> bool:
    """Resolve one ``has:`` token against any earlier one in the same query.

    An unrecognised value used to vanish from the query entirely — not a
    filter, and not free text either — so ``has:attachments`` searched the
    whole archive with a 200 (#364). A contradiction is refused rather than
    letting the last token win, which would be the same silence one step
    removed.
    """
    key = value.lower()
    if key not in _HAS_VALUES:
        expected = " or ".join(repr(v) for v in _HAS_VALUES)
        raise QueryParseError(f"has: expected {expected}, got {value!r}")
    wanted = _HAS_VALUES[key]
    if current is not None and current != wanted:
        raise QueryParseError(
            "has: 'attachment' and 'no-attachment' contradict each other"
        )
    return wanted
```

In `parse_query`, replace:

```python
                elif op_l == "has":
                    if value.lower() == "attachment":
                        f_has_attachment = True
```

with:

```python
                elif op_l == "has":
                    f_has_attachment = _parse_has(value, f_has_attachment)
```

- [ ] **Step 4: Emit the token and refuse a non-boolean**

In `src/localmail/api/search.py::_filter_tokens`, replace:

```python
    if filters.get("has_attachment") is True:
        out.append("has:attachment")
    return out
```

with:

```python
    has_attachment = filters.get("has_attachment")
    if has_attachment is True:
        out.append("has:attachment")
    elif has_attachment is False:
        # Dropped until #364, though the MCP tool's description promised it
        # and `_filter_sql` already honoured it.
        out.append("has:no-attachment")
    elif has_attachment is not None:
        raise ValidationFailed(
            f"has_attachment: expected true, false or null, got {has_attachment!r}"
        )
    return out
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `unset VIRTUAL_ENV && uv run --no-sync pytest tests/test_query_parser.py tests/test_api_search.py tests/test_search_has_attachments_flag.py tests/test_mcp_tools.py tests/test_api_search_malformed_query.py tests/test_mcp_filter_semantics.py -q`

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add src/localmail/search/query.py src/localmail/api/search.py tests/test_query_parser.py tests/test_api_search.py tests/test_search_has_attachments_flag.py tests/test_mcp_tools.py
git commit -m "search: honour has_attachment=false, refuse an unknown has: value (#364)

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Unknown filter keys are refused, and filters are validated before the empty-ACL page

**Files:**
- Modify: `src/localmail/api/search.py`:
  - imports;
  - `_SUPPORTED_FILTER_KEYS` area: delete `_KNOWN_UNSUPPORTED_FILTER_KEYS`, add `filter_key_error`;
  - `build_query_string`;
  - `run_search`, a new gate right after the `sort_membership_error` block.
- Modify: `tests/test_api_search.py` (replace `test_known_unsupported_filter_keys_is_empty`)
- Test: `tests/test_api_search_filter_keys.py`

**Interfaces:**
- Consumes: `_filter_tokens` (raises `ValidationFailed` on bad values, Task 3)
- Produces: `localmail.api.search.filter_key_error(filters: Mapping[str, object]) -> str | None`. `build_query_string` raises `ValidationFailed` on an unknown key.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_api_search_filter_keys.py`:

```python
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Unknown filter keys are refused by name, before the empty-ACL page (#364).

``extra: "ignore"`` used to drop them, and the docstring called that forward
compatibility. It was the opposite: a planner copying the hit field's
plural (``has_attachments``) back as a filter got unfiltered results and a
200, and so did a newer client talking to an older server.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from localmail.api.errors import ValidationFailed
from localmail.api.search import _SUPPORTED_FILTER_KEYS, filter_key_error, run_search

_SUPPORTED = ("account_ids, after, before, date_from, date_to, folder_ids, from, "
              "has_attachment, lang, subject, to")


def _searcher() -> MagicMock:
    """A searcher that fails the test if the refusal did not precede it."""
    searcher = MagicMock()
    searcher.smart_available = False
    searcher.search.side_effect = AssertionError("retrieval must not start")
    return searcher


def test_every_supported_key_passes() -> None:
    assert filter_key_error({k: None for k in _SUPPORTED_FILTER_KEYS}) is None


def test_the_supported_list_in_the_message_is_the_real_set() -> None:
    assert ", ".join(sorted(_SUPPORTED_FILTER_KEYS)) == _SUPPORTED


def test_an_unknown_key_is_named_with_a_suggestion() -> None:
    assert filter_key_error({"has_attachments": True}) == (
        "filters: unknown key 'has_attachments' (did you mean 'has_attachment'?); "
        f"supported: {_SUPPORTED}"
    )


def test_an_unknown_key_with_no_close_match_gets_no_suggestion() -> None:
    assert filter_key_error({"colour": "red"}) == (
        f"filters: unknown key 'colour'; supported: {_SUPPORTED}"
    )


def test_a_null_valued_unknown_key_is_still_refused() -> None:
    """A key that does not exist is a mistake whatever its value."""
    assert filter_key_error({"has_attachments": None}) is not None


def test_several_unknown_keys_are_all_named() -> None:
    assert filter_key_error({"zeta": 1, "has_attachments": True}) == (
        "filters: unknown keys 'has_attachments' (did you mean 'has_attachment'?), "
        f"'zeta'; supported: {_SUPPORTED}"
    )


@pytest.mark.parametrize("allowed", [[1], []])
def test_run_search_refuses_an_unknown_key_before_any_work(allowed) -> None:
    """``[]`` is the case that matters: that branch answers an empty page,
    which reads as "no results" rather than "your request was wrong"."""
    with pytest.raises(ValidationFailed, match="unknown key 'has_attachments'"):
        run_search(searcher=_searcher(), free_text="flight",
                   filters={"has_attachments": True}, limit=10,
                   allowed_account_ids=allowed, user_id=1)


@pytest.mark.parametrize("filters", [
    {"date_from": "last-week"},
    {"lang": ""},
    {"account_ids": ["x1"]},
    {"has_attachment": "yes"},
])
def test_a_malformed_filter_value_is_a_400_even_with_an_empty_acl(filters) -> None:
    """These fail before this task: the value checks ran inside
    ``build_query_string``, below the branch that had already answered."""
    with pytest.raises(ValidationFailed):
        run_search(searcher=_searcher(), free_text="flight", filters=filters,
                   limit=10, allowed_account_ids=[], user_id=1)


def test_a_well_formed_request_with_an_empty_acl_still_answers_an_empty_page() -> None:
    """Positive control: the gate must not refuse what it should pass."""
    page = run_search(searcher=_searcher(), free_text="flight",
                      filters={"has_attachment": False, "date_from": "2026-01-01"},
                      limit=10, allowed_account_ids=[], user_id=1)
    assert page["results"] == []
    assert page["next_cursor"] is None
```

In `tests/test_api_search.py`, replace the whole `test_known_unsupported_filter_keys_is_empty` function with:

```python
def test_build_query_string_refuses_an_unknown_filter_key() -> None:
    """Every v1 filter key is wired through; any other key is refused by
    name rather than ignored (#364)."""
    with pytest.raises(ValidationFailed, match="unknown key 'label'"):
        build_query_string(free_text="x", filters={"label": "work"})
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `unset VIRTUAL_ENV && uv run --no-sync pytest tests/test_api_search_filter_keys.py tests/test_api_search.py -q`

Expected: a collection error for `tests/test_api_search_filter_keys.py` (`ImportError: cannot import name 'filter_key_error'`), and `test_build_query_string_refuses_an_unknown_filter_key` FAILS with DID NOT RAISE.

- [ ] **Step 3: Implement `filter_key_error` and use it in `build_query_string`**

In `src/localmail/api/search.py`, add these imports at the top of the import block, with the stdlib group:

```python
import difflib
from collections.abc import Mapping
```

Delete the block:

```python
# Empty: every v1 spec filter key now wires through to the Searcher.
# Kept as a frozenset so the existing "unsupported key" check keeps working
# without special-casing an empty case at call sites.
_KNOWN_UNSUPPORTED_FILTER_KEYS: frozenset[str] = frozenset()
```

and put in its place:

```python
def filter_key_error(filters: Mapping[str, object]) -> str | None:
    """Name every key that is not a filter, or ``None`` when all of them are.

    Refused by name rather than dropped (#364): an ignored key answers a
    filtered question with unfiltered results and a 200. A key is judged
    whatever its value, ``null`` included. The suggestion is there because
    the commonest slip is the hit field's own name, ``has_attachments``,
    sent back as the filter ``has_attachment``.
    """
    unknown = sorted(str(k) for k in filters if k not in _SUPPORTED_FILTER_KEYS)
    if not unknown:
        return None
    supported = sorted(_SUPPORTED_FILTER_KEYS)
    described = []
    for key in unknown:
        close = difflib.get_close_matches(key, supported, n=1, cutoff=0.8)
        described.append(f"{key!r} (did you mean {close[0]!r}?)" if close else repr(key))
    noun = "key" if len(unknown) == 1 else "keys"
    return (f"filters: unknown {noun} {', '.join(described)}; "
            f"supported: {', '.join(supported)}")
```

Replace the whole `build_query_string` function with:

```python
def build_query_string(*, free_text: str, filters: dict[str, Any]) -> str:
    """Compose `free_text` + filter DSL tokens into a single query string.

    Raises `ValidationFailed` for a key that is not a filter
    (`filter_key_error`) and for a malformed value: dates must be YYYY-MM-DD,
    `lang` non-empty, ids strict digit strings, `has_attachment` a bool.
    Nothing is silently dropped (#364).
    """
    key_error = filter_key_error(filters)
    if key_error is not None:
        raise ValidationFailed(key_error)
    parts: list[str] = []
    if free_text:
        parts.append(free_text)
    parts.extend(_filter_tokens(filters))
    return " ".join(parts)
```

- [ ] **Step 4: Validate before the empty-ACL short-circuit**

In `run_search`, directly after:

```python
    membership_error = sort_membership_error(sort=sort, sort_order=sort_order)
    if membership_error is not None:
        raise ValidationFailed(membership_error)
```

insert:

```python
    # Filters next, keys and then values, for the reason the gate above
    # gives: ahead of the empty-ACL short-circuit, whose empty page reads as
    # "no results". The value checks used to run only in the
    # `build_query_string` calls below that branch, so a malformed
    # `date_from` from a caller granted nothing was answered 200 (#364).
    # Composed and discarded here; the branches compose it again from the
    # ACL-scoped filters.
    build_query_string(free_text=free_text, filters=filters)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `unset VIRTUAL_ENV && uv run --no-sync pytest tests/test_api_search_filter_keys.py tests/test_api_search.py tests/test_api_search_lang_dates.py tests/test_api_search_malformed_query.py tests/test_api_search_sort_membership.py tests/test_api_search_cursor_mode.py tests/test_api_search_rank_without_text.py tests/test_mcp_tools.py tests/test_search_acl_clamp.py -q`

Expected: all pass. If a pre-existing test fails because it passed an unknown filter key and relied on it being ignored, read it first. Change the test's input only if the key was incidental to what it asserts, and say so in the commit message. Never re-admit the key.

- [ ] **Step 6: Commit**

```bash
git add src/localmail/api/search.py tests/test_api_search_filter_keys.py tests/test_api_search.py
git commit -m "search: refuse unknown filter keys by name, validated before the empty-ACL page (#364)

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: The HTTP route refuses unknown fields and makes `query` optional

**Files:**
- Modify: `src/localmail/serve/routes/search.py` (both models, the handler)
- Test: `tests/test_serve_search_request_keys.py`

**Interfaces:**
- Consumes: `filter_key_error` via `build_query_string` (Task 4); `ValidationFailed`; `tests.test_serve_search_route._fake_searcher_returning_one_hit` and `_seed_acct_and_grant` (both updated in Task 2; `tests/` is a package)
- Produces: `SearchRequest.query` defaults to `""`. Both models are `extra: "allow"`. The route answers a 400 problem+json for an unknown top-level field.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_serve_search_request_keys.py`:

```python
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""The HTTP search route refuses what it cannot honour, by name (#364)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from localmail.api.search import _SUPPORTED_FILTER_KEYS
from localmail.search.query import parse_query
from localmail.serve.app import create_app
from localmail.serve.routes.search import SearchFiltersModel, SearchRequest
from tests.test_serve_search_route import (
    _fake_searcher_returning_one_hit,
    _seed_acct_and_grant,
)


def _post(db_dsn: str, api_token: str, body: dict):
    searcher = _fake_searcher_returning_one_hit()
    client = TestClient(create_app(db_dsn=db_dsn, searcher=searcher))
    response = client.post("/v1/search", json=body,
                           headers={"Authorization": f"Bearer {api_token}"})
    return response, searcher


def test_the_filter_model_accepts_exactly_the_supported_keys() -> None:
    """A field the model accepts but the composer does not know would be
    dropped downstream — the gap `_KNOWN_UNSUPPORTED_FILTER_KEYS` stood for."""
    wire = {f.alias or name for name, f in SearchFiltersModel.model_fields.items()}
    assert wire == _SUPPORTED_FILTER_KEYS


def test_the_supported_field_list_is_the_request_model() -> None:
    assert sorted(SearchRequest.model_fields) == [
        "cursor", "filters", "limit", "query", "smart", "sort", "sort_order",
    ]


def test_an_unknown_filter_key_is_a_400_naming_it(
    db_dsn, api_token, db_conn, api_user,
) -> None:
    _seed_acct_and_grant(db_conn, api_user.id)
    r, searcher = _post(db_dsn, api_token,
                        {"query": "flight", "filters": {"has_attachments": True}})
    assert r.status_code == 400, r.text
    assert r.headers["content-type"].startswith("application/problem+json")
    assert r.json()["detail"] == (
        "filters: unknown key 'has_attachments' (did you mean 'has_attachment'?); "
        "supported: account_ids, after, before, date_from, date_to, folder_ids, "
        "from, has_attachment, lang, subject, to"
    )
    searcher.search.assert_not_called()


def test_a_null_valued_unknown_filter_key_is_still_a_400(
    db_dsn, api_token, db_conn, api_user,
) -> None:
    """``exclude_none`` would drop it before ``run_search`` saw it; the route
    must forward unknown keys with their nulls."""
    _seed_acct_and_grant(db_conn, api_user.id)
    r, searcher = _post(db_dsn, api_token,
                        {"query": "flight", "filters": {"has_attachments": None}})
    assert r.status_code == 400, r.text
    searcher.search.assert_not_called()


def test_an_unknown_top_level_field_is_a_400_naming_it(
    db_dsn, api_token, db_conn, api_user,
) -> None:
    _seed_acct_and_grant(db_conn, api_user.id)
    r, searcher = _post(db_dsn, api_token, {"query": "flight", "order": "asc"})
    assert r.status_code == 400, r.text
    assert r.headers["content-type"].startswith("application/problem+json")
    assert r.json()["detail"] == (
        "unknown field 'order'; supported: cursor, filters, limit, query, smart, "
        "sort, sort_order"
    )
    searcher.search.assert_not_called()


@pytest.mark.parametrize("body", [
    # The desktop GUI's shape (gui/src-tauri/src/commands/search.rs).
    {"query": "hello", "filters": {"account_ids": ["1"], "has_attachment": True},
     "limit": 20, "sort": "date"},
    # Kastellan's mail worker, as captured in #364.
    {"query": "flight", "filters": {"has_attachment": True, "account_ids": ["1"]},
     "limit": 10},
])
def test_real_client_shapes_still_succeed(
    db_dsn, api_token, db_conn, api_user, body,
) -> None:
    _seed_acct_and_grant(db_conn, api_user.id)
    r, _ = _post(db_dsn, api_token, body)
    assert r.status_code == 200, r.text


def test_query_may_be_omitted_for_a_filter_only_search(
    db_dsn, api_token, db_conn, api_user,
) -> None:
    _seed_acct_and_grant(db_conn, api_user.id)
    r, searcher = _post(db_dsn, api_token, {"filters": {"has_attachment": True}})
    assert r.status_code == 200, r.text
    composed = parse_query(searcher.search.call_args.args[0])
    assert composed.free_text == ""
    assert composed.filters.has_attachment is True


def test_a_caller_granted_nothing_gets_the_400_not_an_empty_page(
    db_dsn, api_token, api_user,
) -> None:
    for filters in ({"has_attachments": True}, {"date_from": "last-week"}):
        r, _ = _post(db_dsn, api_token, {"query": "flight", "filters": filters})
        assert r.status_code == 400, (filters, r.text)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `unset VIRTUAL_ENV && uv run --no-sync pytest tests/test_serve_search_request_keys.py -q`

Expected failures:
- The two unknown-filter-key tests: the key is ignored, so a 200.
- The unknown top-level field test: a 200.
- `test_query_may_be_omitted_for_a_filter_only_search`: a 422.

The two model tests and the real-client-shape tests pass already; they are controls. `test_a_caller_granted_nothing_gets_the_400_not_an_empty_page` fails only on its first iteration; the date case already passes after Task 4.

- [ ] **Step 3: Implement**

In `src/localmail/serve/routes/search.py`, change the import line:

```python
from localmail.api.errors import FeatureUnavailable
```

to:

```python
from localmail.api.errors import FeatureUnavailable, ValidationFailed
```

In `SearchFiltersModel`, replace:

```python
    model_config = {"populate_by_name": True, "extra": "ignore"}
```

with:

```python
    # "allow", never "ignore": an unknown key has to reach `run_search`,
    # which refuses it by name. Ignored, a planner's `has_attachments` (the
    # hit field's plural) got unfiltered results and a 200 (#364).
    model_config = {"populate_by_name": True, "extra": "allow"}
```

In `SearchRequest`, replace:

```python
class SearchRequest(BaseModel):
    query: str
```

with:

```python
class SearchRequest(BaseModel):
    # "allow" so the route can refuse an unknown field by name rather than
    # drop it (#364). Pydantic's "forbid" answers 422 with an array `detail`,
    # which is not problem+json, so a client that renders `detail` (the
    # kastellan mail worker does) gets nothing it can act on.
    model_config = {"extra": "allow"}

    # Optional: a filter-only search is a search. A query with no free text
    # takes the date walk and reports `rankable: false` (#324).
    query: str = ""
```

Add, directly above `@router.post("")`:

```python
def _unknown_field_error(req: SearchRequest) -> str | None:
    """Name the request fields this route does not define, or ``None``.

    Checked here rather than in `run_search`, which takes keyword arguments
    and so cannot be handed one. It matters beyond typos: an older server
    ignoring a field a newer client relies on returns an answer that looks
    like the one asked for.
    """
    unknown = sorted(req.model_extra or {})
    if not unknown:
        return None
    noun = "field" if len(unknown) == 1 else "fields"
    supported = ", ".join(sorted(SearchRequest.model_fields))
    return (f"unknown {noun} {', '.join(repr(k) for k in unknown)}; "
            f"supported: {supported}")
```

In `search_endpoint`, replace:

```python
    searcher = request.app.state.searcher
```

with:

```python
    field_error = _unknown_field_error(req)
    if field_error is not None:
        raise ValidationFailed(field_error)
    searcher = request.app.state.searcher
```

and replace:

```python
    filters_dict = req.filters.model_dump(by_alias=True, exclude_none=True)
```

with:

```python
    filters_dict = {
        **req.filters.model_dump(by_alias=True, exclude_none=True),
        # Unknown keys again, `null` ones included: `exclude_none` drops those,
        # and a key that is not a filter is a mistake whatever its value.
        **(req.filters.model_extra or {}),
    }
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `unset VIRTUAL_ENV && uv run --no-sync pytest tests/test_serve_search_request_keys.py tests/test_serve_search_route.py tests/test_serve_search_sort_order.py tests/test_serve_acl_routes.py -q`

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/localmail/serve/routes/search.py tests/test_serve_search_request_keys.py
git commit -m "serve: refuse unknown search fields by name; query is optional (#364)

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: The MCP `search` tool's `query` is optional

**Files:**
- Modify: `src/localmail/mcp/server.py` (the `search` tool: the `query` parameter near lines 143-147, the docstring sentence about `list_messages`)
- Modify: `src/localmail/mcp/tools.py` (`tool_search`'s `query` parameter)
- Test: `tests/test_mcp_server_build.py` (append two tests)

**Interfaces:**
- Consumes: `build_mcp_server`, `McpConfig`, `ConnectionPool`, `_search_tool_fn` (all already imported or defined in `tests/test_mcp_server_build.py`)
- Produces: `search(query="")` and `tools.tool_search(..., query: str = "")`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_mcp_server_build.py`:

```python
def test_search_query_is_optional_for_a_filter_only_search(db_dsn):
    """An agent asking for "the last messages with attachments" has no free
    text to send; a required `query` made it invent some (kastellan#698)."""
    pool = ConnectionPool(db_dsn, min_size=1, max_size=2, open=True)
    try:
        server = build_mcp_server(pool, searcher=None, config=McpConfig(enabled=True))
        tools = {t.name: t for t in asyncio.run(server.list_tools())}
    finally:
        pool.close()
    schema = tools["search"].inputSchema or {}
    assert "query" not in schema.get("required", [])
    assert schema["properties"]["query"].get("default") == ""


def test_search_forwards_an_omitted_query_as_empty(db_dsn, monkeypatch):
    import localmail.mcp.server as server_mod

    seen: dict = {}

    def _recording_tool_search(**kwargs):
        seen.update(kwargs)
        return {"results": [], "next_cursor": None}

    monkeypatch.setattr(server_mod.tools, "tool_search", _recording_tool_search)
    monkeypatch.setattr(server_mod, "_current_user_id", lambda: 1)

    pool = ConnectionPool(db_dsn, min_size=1, max_size=2, open=True)
    try:
        server = build_mcp_server(pool, searcher=object(),
                                  config=McpConfig(enabled=True))
        _search_tool_fn(server)(has_attachment=True)
    finally:
        pool.close()
    assert seen["query"] == ""
    assert seen["filters"] == {"has_attachment": True}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `unset VIRTUAL_ENV && uv run --no-sync pytest tests/test_mcp_server_build.py -q`

Expected: `test_search_query_is_optional_for_a_filter_only_search` FAILS (`query` is required). `test_search_forwards_an_omitted_query_as_empty` FAILS with `TypeError: … missing 1 required positional argument: 'query'`.

- [ ] **Step 3: Implement**

In `src/localmail/mcp/server.py`, replace:

```python
        query: Annotated[str, Field(description=(
            "Free-text query matched against message subjects/bodies and "
            "extracted attachment text. An empty string lists recent mail "
            "(date-ordered) — prefer `list_messages` for that intent."))],
```

with:

```python
        query: Annotated[str, Field(description=(
            "Free-text query matched against message subjects/bodies and "
            "extracted attachment text. Optional: omit it to search by "
            "filters alone, newest first — for example `has_attachment=true` "
            "for recent mail with attachments."))] = "",
```

In the same tool's docstring, replace:

```
        Use `list_messages` when you have no query and just want recent mail;
```

with:

```
        Use `list_messages` when you want recent mail with no filters at all;
```

In `src/localmail/mcp/tools.py::tool_search`, replace:

```python
    query: str,
```

with:

```python
    query: str = "",
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `unset VIRTUAL_ENV && uv run --no-sync pytest tests/test_mcp_server_build.py tests/test_mcp_tools.py tests/test_mcp_filter_semantics.py -q`

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/localmail/mcp/server.py src/localmail/mcp/tools.py tests/test_mcp_server_build.py
git commit -m "mcp: the search tool's query is optional (#364)

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 7: Documentation

**Files:**
- Modify: `README.md` (the DSL operator list; the `/v1/search` section)
- Modify: `docs/mcp-usage.md` (the `search` table row)
- Modify: `CLAUDE.md` (Layout; a new bullet under Browse & search pagination)

**Interfaces:**
- Consumes: the behaviour shipped in Tasks 1–6
- Produces: docs only

- [ ] **Step 1: README — the DSL operator list**

In `README.md`, replace:

```
per-message by `lingua-language-detector`), and `has:attachment`. Each operator
```

with:

```
per-message by `lingua-language-detector`), `has:attachment` and
`has:no-attachment` (any other `has:` value is refused). Each operator
```

- [ ] **Step 2: README — the `/v1/search` section**

In `README.md`, directly before the paragraph that begins `When paging, send the cursor back with the same`, insert:

```markdown
**Filters are honoured or refused, never dropped.** `query` is optional: a
request with only `filters` is a search, answered newest first. An unknown
filter key is a 400 naming it, with a suggestion when one is close
(`has_attachments` is not `has_attachment`). An unknown top-level field is a
400 too. Both used to be ignored, which answered a filtered question with
unfiltered results and a 200. `has_attachment: false` now means "only
messages without attachments" (DSL `has:no-attachment`) where it used to be
dropped, and a malformed filter value is a 400 even for a caller with no
account grants. Every hit's `has_attachments` describes the message; it
used to report whether the *matched text* came from an attachment. An
attachment is any non-body MIME part, which still includes images embedded
in an HTML body — [#365](https://github.com/hherb/localmail/issues/365)
tracks narrowing that. Closes
[#364](https://github.com/hherb/localmail/issues/364).

```

- [ ] **Step 3: docs/mcp-usage.md**

In `docs/mcp-usage.md`, in the `search` row, replace the opening:

```
| `search` | `query`, `sort="rank"\|"date"`,
```

with:

```
| `search` | `query` (optional — omit it for a filter-only search), `sort="rank"\|"date"`,
```

- [ ] **Step 4: CLAUDE.md — Layout**

In `CLAUDE.md`, directly after the line

```
    text_empty.py   # pure: is_blank — the one "nothing to index" rule (#266)
```

insert:

```
    attachment_presence.py # pure: HAS_ATTACHMENT_SQL — the one "has
                    #   attachments" rule, filter and hit flag alike (#364)
```

- [ ] **Step 5: CLAUDE.md — the behaviour entry**

In `CLAUDE.md`, directly before the line that begins `- **Hard ACL clamp inside the Searcher**`, insert:

```markdown
- **Search filters are honoured or refused, never dropped (#364).** Design:
  [docs/superpowers/specs/2026-09-14-search-request-honesty-design.md](docs/superpowers/specs/2026-09-14-search-request-honesty-design.md);
  plan: [docs/superpowers/plans/2026-09-14-search-request-honesty.md](docs/superpowers/plans/2026-09-14-search-request-honesty.md).
  #364 reported `has_attachment` as ignored. **The filter worked; the hit flag
  did not**: `has_attachments` was `attachment_filename is not None`, i.e.
  "the matched chunk was an attachment's".
  - **The rule:** `search/attachment_presence.py::HAS_ATTACHMENT_SQL` is the
    one rule. It is composed by `_filter_sql` (`NOT` of it for `false`), by
    `_hydrate`'s SELECT and by `date_keyset.ROW_SQL_TEMPLATE`, so the filter
    and the flag cannot disagree. A `CASE` guards `jsonb_array_length`, which
    raises on a non-array, and the column has no CHECK. #365 narrows what
    counts by editing this one constant.
  - **`false`:** `has_attachment: false` compiles to the DSL
    `has:no-attachment`. Any other `has:` value, and the contradictory pair,
    raise `QueryParseError`, which `_gate_free_text` already maps to a 400
    (`_gate_query` since #367).
    An unknown value used to vanish from the query, not even kept as free
    text.
  - **Unknown keys:** unknown filter keys (`filter_key_error`, inside
    `build_query_string`) and unknown top-level fields (the route, via
    `extra: "allow"` + `model_extra`) are a 400 problem+json naming the key.
    Pydantic's `forbid` was not used: its 422 carries an array `detail`,
    which is not problem+json.
  - **Validation order:** filter values are validated **before** the empty-ACL
    short-circuit, since `run_search` calls `build_query_string` once early
    and discards the result. A malformed `date_from` from a grant-nothing
    caller used to be a 200 empty page.
  - **`query`:** optional on HTTP and MCP.
```

- [ ] **Step 6: Commit**

```bash
git add README.md docs/mcp-usage.md CLAUDE.md
git commit -m "docs: search filters are honoured or refused, never dropped (#364)

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 8: Verify, push, open the PR

**Files:** none changed; this is verification and hand-off.

**Interfaces:**
- Consumes: every commit above
- Produces: an open PR against `main`, with CI green

- [ ] **Step 1: The full suite**

Run: `unset VIRTUAL_ENV && uv run --no-sync pytest -q 2>&1 | tail -5`

Expected: `M passed`, where M = N (from Task 1 Step 0) + the number of tests this plan added, 0 failed, and no new warnings beyond the pre-existing ones. If a failure appears, read it. Do not retry it away.

- [ ] **Step 2: Confirm the branch holds only this work**

Run: `git log --oneline origin/main..HEAD && git status --short`

Expected: the design-doc commit, the plan commit, and the seven task commits; `git status` prints nothing.

- [ ] **Step 3: Push the branch**

Run: `git push -u origin fix/364-search-honesty`

- [ ] **Step 4: Open the PR**

```bash
gh pr create --base main --head fix/364-search-honesty \
  --title "Search requests are honoured or refused, never dropped (#364)" \
  --body "$(cat <<'EOF'
Closes #364.

#364 reported the `has_attachment` filter as ignored. The filter worked; four
other things failed silently, and this fixes all of them:

- A hit's `has_attachments` described the **matched chunk**, not the message.
  It now reads one shared rule (`search/attachment_presence.py`), the same one
  the filter uses.
- `has_attachment: false` was dropped. It now compiles to the DSL
  `has:no-attachment`; any other `has:` value is a 400.
- Unknown filter keys and unknown top-level fields were ignored. Both are now
  a 400 problem+json naming the key, with a did-you-mean.
- Filter values were validated after the empty-ACL short-circuit. A malformed
  filter from a caller with no grants is now a 400, not an empty 200.

`query` is now optional on HTTP and MCP.

Unchanged: what counts as an attachment. Images embedded in an HTML body still
count; #365 narrows that by editing the one shared rule.

Design: docs/superpowers/specs/2026-09-14-search-request-honesty-design.md
Plan: docs/superpowers/plans/2026-09-14-search-request-honesty.md

**For kastellan:** a planner-produced unknown filter key now gets a 400 whose
`detail` names the fix, instead of unfiltered results. Separately, from
deploying `main` rather than from this PR: #324 refuses `sort: "rank"` for a
query with no free text, so the mail worker should omit `sort` for
filter-only searches.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 5: Wait for CI, then stop**

Run: `gh pr checks --watch`

Expected: every check passes on both Python legs. Report the PR URL. **Do not merge**: the operator merges.
