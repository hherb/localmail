# Search `sort_order` (asc/desc) with Pagination — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an orthogonal `sort_order: "asc"|"desc"` to `POST /v1/search` and the MCP `search` tool, defaulting to `desc`, and make every date-ordered branch paginate in both directions.

**Architecture:** `sort_order` is a second axis beside the existing `sort`, resolved to `DEFAULT_SORT_ORDER` exactly once at the top of `Searcher.search`. Ascending is spelled `ASC NULLS FIRST, id ASC` — the exact reverse of `messages_recent_idx` — so it is served by a backward index scan with no migration. The keyset cursor gains a second prefix (`KA|`) so a cursor carries the direction it continues; `resolve_cursor_plan` makes the cursor the authority on both axes and rejects a stated value that contradicts either. The lexical-date walk and the blank-query recent-mail list collapse into one keyset helper, which is what gives the blank-query branch pagination it has never had.

**Tech Stack:** Python 3.12+, `uv`, psycopg 3 + raw SQL, pydantic v2, FastAPI, the `mcp` SDK, pytest.

**Spec:** [docs/superpowers/specs/2026-08-24-search-sort-order-design.md](../specs/2026-08-24-search-sort-order-design.md)

## Global Constraints

- **No migration, no new index, no new dependency.** Ascending must be spelled `ASC NULLS FIRST, id ASC`. The `NULLS LAST` spelling full-sorts the table (33,372 buffers vs 44 on the live 128k archive) and an `IS NOT NULL` restriction does **not** rescue it — both variants were measured.
- **Undated rows sort first in ascending order.** Ascending is the exact reverse of descending; `asc == reversed(desc)` is a testable invariant.
- **`sort="rank"` + `sort_order="asc"` is refused**, at the HTTP boundary *and* inside `Searcher.search`. `sort_order="desc"` on `rank` is accepted.
- **Never a bare `assert` for a guard** — asserts vanish under `python -O`. Named exception classes only.
- **Never a bare `ValueError` catch** at the api boundary — catch the named subclass, so a psycopg or embedding-backend failure is not relabelled as a caller error.
- **No default for `PoolMetadata.sort_order`**, and read it as `entry["sort_order"]`, never `entry.get("sort_order", "desc")`.
- **`DEFAULT_SORT_ORDER` has exactly one definition**, in `search/searcher.py`. `api/` imports it; it may not restate `"desc"`.
- Run tests with `unset VIRTUAL_ENV && uv run pytest …` — a stray `VIRTUAL_ENV` makes `uv` pick the wrong interpreter.
- Commit after every task. Branch is `feat/search-sort-order`, already created off `main`.

---

### Task 1: Pin that the hybrid pool never sees `sort="date"`

The spec's branch analysis says branch 3 (the hybrid pool) is reached only when `sort="rank"`, which makes `_build_results`' `sort="date"` path and `_date_sort_key` unreachable. Every later task depends on that being true. Pin it first, and document it — per the design decision, nothing is deleted.

**Files:**
- Create: `tests/test_searcher_pool_sort_unreachable.py`
- Modify: `src/localmail/search/searcher.py` (comment on `_date_sort_key`, around line 56)

**Interfaces:**
- Consumes: nothing.
- Produces: nothing. Test + comment only.

- [ ] **Step 1: Write the failing test**

Create `tests/test_searcher_pool_sort_unreachable.py`:

```python
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""The hybrid pool is only ever built for ``sort="rank"``.

``Searcher.search`` has three retrieval branches. The date-keyset branch
takes ``sort="date"`` with non-blank free text; the blank-query branch
takes everything with blank free text; so the hybrid pool branch — the
only one that caches a pool and the only reader of ``_build_results``'
``sort`` parameter — is reachable only as ``rank`` + non-blank text.

That makes ``_date_sort_key`` dead code. It is kept and documented rather
than deleted, so this test is what stops a later change adding
``sort_order`` handling "for symmetry" to a branch that never runs.
"""
from __future__ import annotations

from localmail.config import SearchConfig
from localmail.db import open_pool
from localmail.search.embed_worker import run_embed_worker_once
from localmail.search.searcher import Searcher


class _E:
    name = "s"; model = "s"; dimension = 768
    def embed_documents(self, t): return [[1.0] * 768 for _ in t]
    def embed_query(self, t): return [0.5] * 768
    def health_check(self): pass


def _seed(conn, n=6):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO accounts (name,email_address,imap_host,auth_method)"
                    " VALUES ('a','a@x','h','password') RETURNING id")
        acct = cur.fetchone()[0]
        for i in range(n):
            cur.execute(
                "INSERT INTO messages (account_id, message_id, raw_sha256, subject,"
                " body_text, headers, raw_bytes, size_bytes)"
                " VALUES (%s, %s, %s, %s, %s, '{}'::jsonb, 'r', 1)",
                (acct, f"<m{i}>", bytes([i + 1]) * 32, f"Subject {i} test",
                 f"Body {i} content with the keyword test."),
            )
    conn.commit()


def test_a_cached_pool_always_records_sort_rank(db_dsn, db_conn):
    """Every pool the Searcher caches was built as a rank pool.

    Asserted through the public ``get_pool_metadata`` rather than the cache
    dict, so it survives a cache refactor.
    """
    _seed(db_conn)
    cfg = SearchConfig()
    run_embed_worker_once(db_conn, cfg, _E())
    pool = open_pool(db_dsn)
    try:
        s = Searcher(pool=pool, cfg=cfg, embeddings=_E(), reranker=None)
        for sort in ("rank", "date", None):
            page = s.search("test", allowed_account_ids=None, page_size=2,
                            user_id=1, sort=sort)
            if page.search_token is None:
                continue
            meta = s.get_pool_metadata(page.search_token, user_id=1)
            assert meta is not None
            assert meta.sort == "rank", (
                f"search(sort={sort!r}) cached a pool recording "
                f"sort={meta.sort!r}; _build_results' date branch is "
                "reachable after all and _date_sort_key is not dead code"
            )
    finally:
        pool.close()
```

- [ ] **Step 2: Run it and confirm it passes today**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_searcher_pool_sort_unreachable.py -v
```

Expected: PASS. This one is a characterisation test, not a red-green test — it records an existing property the rest of the plan leans on. If it FAILS, stop and re-derive the branch analysis before continuing; the spec is wrong.

- [ ] **Step 3: Document the unreachability at `_date_sort_key`**

In `src/localmail/search/searcher.py`, replace the docstring of `_date_sort_key` (currently at line 56) with one that names the situation. Keep the function and `_DATE_SORT_NULL_SENTINEL` exactly as they are:

```python
def _date_sort_key(item: dict) -> tuple[int, datetime]:
    """Key for ``COALESCE(internal_date, date_sent) DESC NULLS LAST``.

    **Unreachable.** ``Searcher.search``'s date-keyset branch takes
    ``sort="date"`` with non-blank free text and its blank-query branch
    takes every blank query, so the hybrid pool branch — the sole caller
    of ``_build_results`` with a ``sort`` other than the default, and the
    sole writer of the cached pool's ``sort`` — is reached only as
    ``rank`` + non-blank text. Pinned by
    ``tests/test_searcher_pool_sort_unreachable.py``.

    Kept rather than deleted because deleting is not what the sort_order
    change is for. Do **not** add ``sort_order`` handling here "for
    symmetry": it would be tested against a branch that never runs.

    Returned tuple uses (1, dt) for rows with a usable date and (0,
    sentinel) for NULLs, so Python's default ascending sort puts NULLs
    first; ``sorted(..., reverse=True)`` then reverses to (newest, ...,
    older, NULLs-last).
    """
```

- [ ] **Step 4: Re-run the test and the searcher suite**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_searcher_pool_sort_unreachable.py tests/test_searcher.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/test_searcher_pool_sort_unreachable.py src/localmail/search/searcher.py
git commit -m "test(search): pin that the hybrid pool is only ever built for sort=rank

_build_results' sort='date' path and _date_sort_key are unreachable: the
date-keyset branch takes date+text and the blank-query branch takes every
blank query, leaving the pool branch reachable only as rank+text. Every
later step of the sort_order change leans on that, and nothing asserted it.

Documented rather than deleted, so the pin is what stops a later change
adding sort_order handling to a branch that never runs.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: `SortOrder`, `DEFAULT_SORT_ORDER`, and the rank+asc guard

Introduce the type, the single default, and the refusal — all inside `Searcher.search`, before any IO. No ordering behaviour changes yet and nothing is exposed on the wire, so an `asc` request is unreachable from outside until Task 7.

**Files:**
- Modify: `src/localmail/search/searcher.py` (near `SortMode`/`DEFAULT_SORT` at lines 42-48; `PoolMetadata` at ~328; `search()` at ~934; the two `_cache.put` calls at ~1132 and ~918; `get_pool_metadata` at ~410)
- Create: `tests/test_searcher_sort_order_guard.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `localmail.search.searcher.SortOrder = Literal["asc", "desc"]`
  - `localmail.search.searcher.DEFAULT_SORT_ORDER: SortOrder = "desc"`
  - `localmail.search.searcher.SortOrderNotApplicable(ValueError)`
  - `Searcher.search(..., sort_order: SortOrder | None = None)`
  - `PoolMetadata.sort_order: SortOrder` (no default)

- [ ] **Step 1: Write the failing test**

Create `tests/test_searcher_sort_order_guard.py`:

```python
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""``sort_order="asc"`` is refused for ``sort="rank"``, before any IO.

The rank path serves a bounded candidate pool, so reversing it returns the
least relevant of the *top hits* rather than of the archive — an answer
that looks meaningful and is an artifact of where the pool stopped. We
cannot serve the question honestly, so we decline it rather than ignore it
(#308/#312: a stated parameter the server will not honour is reported).

The guard lives in the Searcher as well as in api/ because the CLI and
library callers reach it without passing through the HTTP layer — the same
reason ``KeysetCursorUnusable`` is guarded twice.
"""
from __future__ import annotations

import pytest

from localmail.config import SearchConfig
from localmail.search.searcher import (
    DEFAULT_SORT_ORDER,
    Searcher,
    SortOrderNotApplicable,
)


class _Embeddings:
    name = "s"; model = "s"; dimension = 768

    def embed_documents(self, texts):  # pragma: no cover - never reached
        raise AssertionError("retrieval must not start")

    def embed_query(self, text):  # pragma: no cover - never reached
        raise AssertionError("retrieval must not start")

    def health_check(self) -> None:
        pass


def _searcher():
    """A Searcher whose pool raises if touched — the guard precedes all IO."""
    from unittest.mock import MagicMock
    pool = MagicMock()
    pool.connection.side_effect = AssertionError("no connection may be opened")
    return Searcher(pool=pool, cfg=SearchConfig(), embeddings=_Embeddings(),
                    reranker=None, rewriter=None), pool


def test_the_default_order_is_descending() -> None:
    assert DEFAULT_SORT_ORDER == "desc"


def test_rank_with_ascending_order_is_refused_before_any_io() -> None:
    searcher, pool = _searcher()
    with pytest.raises(SortOrderNotApplicable) as exc:
        searcher.search("invoice", allowed_account_ids=None, sort="rank",
                        sort_order="asc")
    pool.connection.assert_not_called()
    assert "sort='date'" in str(exc.value), (
        "the message must name the remedy: a caller who sent sort_order "
        "alone needs to be told which sort serves it"
    )


def test_an_unstated_sort_is_rank_and_is_refused_the_same_way() -> None:
    """`sort_order="asc"` alone resolves `sort` to rank, so it is refused.

    This is the shape a caller reaches for first, so the refusal has to
    cover it — a guard reading only an explicitly stated `sort` misses it.
    """
    searcher, pool = _searcher()
    with pytest.raises(SortOrderNotApplicable):
        searcher.search("invoice", allowed_account_ids=None, sort_order="asc")
    pool.connection.assert_not_called()


def test_rank_with_descending_order_is_accepted() -> None:
    """Only `asc` is refused. "Descending relevance" is what rank serves."""
    searcher, pool = _searcher()
    with pytest.raises(AssertionError, match="no connection"):
        searcher.search("invoice", allowed_account_ids=None, sort="rank",
                        sort_order="desc")
    pool.connection.assert_called_once()


def test_date_with_ascending_order_reaches_retrieval() -> None:
    searcher, pool = _searcher()
    with pytest.raises(AssertionError, match="no connection"):
        searcher.search("invoice", allowed_account_ids=None, sort="date",
                        sort_order="asc")
    pool.connection.assert_called_once()


def test_a_raise_not_an_assert() -> None:
    """Guards must survive `python -O`, where `assert` is compiled out."""
    import inspect
    src = inspect.getsource(Searcher.search)
    assert "SortOrderNotApplicable(" in src
    assert "assert effective_sort" not in src
```

- [ ] **Step 2: Run it to verify it fails**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_searcher_sort_order_guard.py -v
```

Expected: FAIL — `ImportError: cannot import name 'DEFAULT_SORT_ORDER'`.

- [ ] **Step 3: Add the type, the default, and the exception**

In `src/localmail/search/searcher.py`, immediately after the `DEFAULT_SORT` block (line 48):

```python
SortOrder = Literal["asc", "desc"]

#: The direction a caller gets when it states none. It lives beside
#: ``DEFAULT_SORT`` for the same reason (#312): ``Searcher.search`` and
#: ``api.search_cursor`` both resolve an unstated value, and two layers
#: resolving "unstated" from two literals is the drift itself.
DEFAULT_SORT_ORDER: SortOrder = "desc"


class SortOrderNotApplicable(ValueError):
    """``sort_order="asc"`` was asked for on a sort that cannot serve it.

    A named subclass rather than a bare ``ValueError`` so the api/ layer
    can map exactly this to a 400 without also catching what psycopg,
    ``datetime`` and the embedding backends raise — which would relabel a
    real outage as a caller error and send them to fix a blameless query.
    """
```

- [ ] **Step 4: Resolve once and guard, in `search()`**

In `Searcher.search`, the body currently begins:

```python
        t0 = time.monotonic()
        effective_sort: SortMode = DEFAULT_SORT if sort is None else sort
        cfg = self._cfg
```

Add the `sort_order` parameter to the signature immediately after `sort: SortMode | None = None`:

```python
        sort_order: SortOrder | None = None,
```

and extend the body:

```python
        t0 = time.monotonic()
        effective_sort: SortMode = DEFAULT_SORT if sort is None else sort
        effective_order: SortOrder = (
            DEFAULT_SORT_ORDER if sort_order is None else sort_order
        )
        # Refused rather than honoured: the rank path serves a bounded
        # candidate pool, so reversing it returns the least relevant of the
        # top hits rather than of the archive — an artifact of where the
        # pool stopped, wearing the shape of an answer. Refused rather than
        # ignored because a stated parameter the server will not honour is
        # reported, never dropped (#308, #312). Before any IO, so a caller
        # error costs no connection.
        if effective_sort == "rank" and effective_order == "asc":
            raise SortOrderNotApplicable(
                "sort_order='asc' is not applicable to sort='rank' (the "
                "default); pass sort='date' for oldest-first. The rank path "
                "serves a bounded candidate pool, so reversing it returns "
                "the least relevant of the top hits, not of the archive."
            )
        cfg = self._cfg
```

Every read below must use `effective_order`. A surviving read of the raw `sort_order` is the #312 defect.

- [ ] **Step 5: Add `PoolMetadata.sort_order` and write it into the cache**

In `PoolMetadata` (line ~346), after the `sort: SortMode` field:

```python
    # The direction this pool was built with, recorded beside ``sort`` and
    # for the same reason. Pool cursors are only minted on the rank branch,
    # where "asc" is refused — so a pool carrying "asc" is unreachable
    # today. Recorded anyway rather than assumed: encoding the invariant in
    # the reader is what makes a future dispatch change silently wrong.
    # No default, for the reason ``sort`` has none.
    sort_order: SortOrder
```

In `get_pool_metadata`'s `PoolMetadata(...)` construction, add:

```python
            sort_order=entry["sort_order"],
```

**Read it as `entry["sort_order"]`, never `entry.get(...)`** — a missing key is a bug in whichever `_cache.put` forgot it and belongs as a loud `KeyError` at the boundary that can still see it.

In **both** `_cache.put` call sites — `_search_with_parsed` (~line 918) and `search` (~line 1132) — add `"sort_order"` beside the existing `"sort"` key:

```python
                "sort": effective_sort,
                "sort_order": effective_order,
```

In `_search_with_parsed` the local is named `sort`; give that function a matching `sort_order: SortOrder = DEFAULT_SORT_ORDER` keyword parameter, write `"sort_order": sort_order`, and have `grow_pool` pass `sort_order=entry["sort_order"]` alongside its existing `sort=sort`.

- [ ] **Step 6: Run the tests to verify they pass**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_searcher_sort_order_guard.py tests/test_searcher_pool_metadata.py tests/test_searcher_pagination.py tests/test_api_search_pagination.py tests/test_api_search_cursor_mode.py -v
```

Expected: PASS *after* you fix the constructions below. `PoolMetadata` is built by hand in **three** test files, and the new field is intentionally defaultless, so all three raise `TypeError: missing 1 required positional argument: 'sort_order'` until updated. Add `sort_order="desc"` to each:

- `tests/test_searcher_pool_metadata.py:258`
- `tests/test_api_search_pagination.py:114` and `:151`
- `tests/test_api_search_cursor_mode.py:166` and `:181`

Do **not** give the field a default to avoid this — the breakage is the point. A defaulted `sort_order` makes a pool built one way report itself as built the other, which is the value `reject_pool_sort_mismatch` then makes a 400/200 call on.

- [ ] **Step 7: Commit**

```bash
git add src/localmail/search/searcher.py tests/test_searcher_sort_order_guard.py tests/test_searcher_pool_metadata.py tests/test_api_search_pagination.py tests/test_api_search_cursor_mode.py
git commit -m "feat(search): SortOrder type, one default, and the rank+asc refusal

Adds SortOrder/DEFAULT_SORT_ORDER beside SortMode/DEFAULT_SORT so the two
layers that resolve an unstated value cannot answer differently (#312),
and resolves it once at the top of Searcher.search.

sort='rank' with sort_order='asc' raises SortOrderNotApplicable before any
connection is opened. The rank path serves a bounded candidate pool, so
reversing it returns the least relevant of the top hits rather than of the
archive -- an artifact of where the pool stopped, shaped like an answer.
Refused rather than ignored, per #308/#312.

PoolMetadata records sort_order beside sort, defaultless and read as
entry['sort_order'], for the reason sort is.

No wire surface yet, so 'asc' is unreachable from outside until the
transport task.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Direction in the date-keyset walk

Make `_lexical_date_search` honour `effective_order`: the ORDER BY and the keyset predicate both flip. This is where ascending starts actually working.

**Files:**
- Modify: `src/localmail/search/searcher.py` (`_lexical_date_search` at ~531-620; its call site at ~1032)
- Create: `tests/test_searcher_sort_order_walk.py`

**Interfaces:**
- Consumes: `SortOrder`, `DEFAULT_SORT_ORDER`, `effective_order` (Task 2).
- Produces:
  - `Searcher._ORDER_BY_SQL: dict[SortOrder, str]`
  - `Searcher._keyset_clause(keyset: KeysetCursor, order: SortOrder) -> tuple[str, list[Any]]` (module-level function, not a method)
  - `_lexical_date_search(conn, parsed, page_size, keyset, order)` — the `order` parameter is positional-or-keyword, appended last.

- [ ] **Step 1: Write the failing test**

Create `tests/test_searcher_sort_order_walk.py`:

```python
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Ascending date order is the exact reverse of descending, and pages.

Ascending is spelled ``ASC NULLS FIRST, id ASC`` because that is the exact
reverse of ``messages_recent_idx`` and is therefore served by a backward
index scan. The ``NULLS LAST`` spelling full-sorts the table; an
``IS NOT NULL`` restriction does not rescue it. Both were measured on the
live 128k archive before this was written.

Undated rows therefore sort *first* ascending, which is what makes
``asc == reversed(desc)`` hold as an invariant.
"""
from __future__ import annotations

from datetime import datetime, timezone

from localmail.config import SearchConfig
from localmail.db import open_pool
from localmail.search.searcher import Searcher


class _E:
    name = "s"; model = "s"; dimension = 768
    def embed_documents(self, t): return [[1.0] * 768 for _ in t]
    def embed_query(self, t): return [0.5] * 768
    def health_check(self): pass


def _seed(conn, *, n=7, undated=2):
    """n dated messages plus `undated` with no usable date at all."""
    with conn.cursor() as cur:
        cur.execute("INSERT INTO accounts (name,email_address,imap_host,auth_method)"
                    " VALUES ('a','a@x','h','password') RETURNING id")
        acct = cur.fetchone()[0]
        for i in range(n):
            cur.execute(
                "INSERT INTO messages (account_id, message_id, raw_sha256, subject,"
                " body_text, headers, raw_bytes, size_bytes, internal_date)"
                " VALUES (%s,%s,%s,%s,%s,'{}'::jsonb,'r',1,%s)",
                (acct, f"<d{i}>", bytes([i + 1]) * 32, f"dated {i} needle",
                 "body needle", datetime(2026, 1, i + 1, tzinfo=timezone.utc)),
            )
        for j in range(undated):
            cur.execute(
                "INSERT INTO messages (account_id, message_id, raw_sha256, subject,"
                " body_text, headers, raw_bytes, size_bytes)"
                " VALUES (%s,%s,%s,%s,%s,'{}'::jsonb,'r',1)",
                (acct, f"<u{j}>", bytes([200 + j]) * 32, f"undated {j} needle",
                 "body needle"),
            )
    conn.commit()


def _all_pages(searcher, *, order, query="needle", page_size=3):
    """Walk every page, returning the flat list of message ids."""
    ids: list[int] = []
    cursor = None
    for _ in range(50):  # generous bound; the walk must terminate
        page = searcher.search(query, allowed_account_ids=None,
                               page_size=page_size, user_id=1, sort="date",
                               sort_order=order, keyset_cursor=cursor)
        ids.extend(r.message_id for r in page.results)
        if page.next_keyset is None:
            return ids
        cursor = page.next_keyset
    raise AssertionError("walk did not terminate")


def test_ascending_is_exactly_reversed_descending(db_dsn, db_conn):
    """The whole ordering, undated rows included — not just the dated head."""
    _seed(db_conn)
    pool = open_pool(db_dsn)
    try:
        s = Searcher(pool=pool, cfg=SearchConfig(), embeddings=_E(), reranker=None)
        desc = _all_pages(s, order="desc")
        asc = _all_pages(s, order="asc")
    finally:
        pool.close()
    assert len(desc) == 9
    assert asc == list(reversed(desc))


def test_undated_rows_sort_first_ascending(db_dsn, db_conn):
    """NULLS FIRST is not incidental — it is what the backward scan requires."""
    _seed(db_conn, n=7, undated=2)
    pool = open_pool(db_dsn)
    try:
        s = Searcher(pool=pool, cfg=SearchConfig(), embeddings=_E(), reranker=None)
        page = s.search("needle", allowed_account_ids=None, page_size=2,
                        user_id=1, sort="date", sort_order="asc")
    finally:
        pool.close()
    subjects = [r.subject for r in page.results]
    assert all(s_.startswith("undated") for s_ in subjects), subjects


def test_ascending_pages_do_not_overlap_or_skip(db_dsn, db_conn):
    _seed(db_conn)
    pool = open_pool(db_dsn)
    try:
        s = Searcher(pool=pool, cfg=SearchConfig(), embeddings=_E(), reranker=None)
        ids = _all_pages(s, order="asc", page_size=2)
    finally:
        pool.close()
    assert len(ids) == len(set(ids)) == 9


def test_descending_is_unchanged_when_order_is_unstated(db_dsn, db_conn):
    """The default path must be byte-identical to today's behaviour."""
    _seed(db_conn)
    pool = open_pool(db_dsn)
    try:
        s = Searcher(pool=pool, cfg=SearchConfig(), embeddings=_E(), reranker=None)
        stated = _all_pages(s, order="desc")
        unstated_page = s.search("needle", allowed_account_ids=None, page_size=3,
                                 user_id=1, sort="date")
    finally:
        pool.close()
    assert [r.message_id for r in unstated_page.results] == stated[:3]
```

- [ ] **Step 2: Run it to verify it fails**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_searcher_sort_order_walk.py -v
```

Expected: FAIL — `search()` does not yet accept the ordering through to the SQL, so `asc` returns descending and `test_ascending_is_exactly_reversed_descending` fails on the list comparison.

- [ ] **Step 3: Add the ORDER BY table and the keyset-clause builder**

In `src/localmail/search/searcher.py`, at module level near `_date_sort_key`:

```python
#: The one place either direction's ORDER BY is written. Ascending is
#: ``ASC NULLS FIRST`` because that is the exact reverse of
#: ``messages_recent_idx`` (``… DESC NULLS LAST, id DESC``) and is served
#: by a backward index scan. Measured on the live 128k archive: 44 buffers
#: against 33,372 for the ``ASC NULLS LAST`` spelling, which full-sorts —
#: and an ``IS NOT NULL`` restriction does *not* rescue it. Do not
#: "normalise" these to NULLS LAST.
_DATE_ORDER_BY_SQL: dict[str, str] = {
    "desc": ("ORDER BY COALESCE(m.internal_date, m.date_sent) DESC NULLS LAST, "
             "m.id DESC"),
    "asc": ("ORDER BY COALESCE(m.internal_date, m.date_sent) ASC NULLS FIRST, "
            "m.id ASC"),
}

_DATE_EXPR_SQL = "COALESCE(m.internal_date, m.date_sent)"


def _keyset_clause(keyset: KeysetCursor, order: str) -> tuple[str, list[Any]]:
    """The ``AND …`` fragment placing the walk strictly after ``keyset``.

    The two directions are not mirror images in shape, only in effect.
    Descending needs ``OR <expr> IS NULL`` so a dated cursor still admits
    the undated tail that follows it. Ascending needs no such disjunct —
    under ``NULLS FIRST`` the undated block is already behind the cursor,
    and ``NULL > ts`` is not true, so those rows drop out on their own.
    That makes the ascending dated predicate the more index-friendly of
    the two; the descending disjunct is the shape #75 identified as
    preventing an index range bound, and is pre-existing here.
    """
    expr = _DATE_EXPR_SQL
    if order == "desc":
        if keyset.ts is None:
            return f" AND {expr} IS NULL AND m.id < %s ", [keyset.id]
        return (
            f" AND ({expr} < %s OR ({expr} = %s AND m.id < %s) "
            f" OR {expr} IS NULL) ",
            [keyset.ts, keyset.ts, keyset.id],
        )
    if keyset.ts is None:
        # Still in the undated head: the rest of it, then every dated row.
        return (
            f" AND (({expr} IS NULL AND m.id > %s) OR {expr} IS NOT NULL) ",
            [keyset.id],
        )
    # SUPERSEDED (review of #322) — the OR-form below plans as a per-tuple
    # Filter, so every continuation page restarts at the head of
    # messages_recent_idx. The shipped form is a row comparison:
    #     f" AND ROW({expr}, m.id) > ROW(%s, %s) ", [keyset.ts, keyset.id]
    # See searcher._keyset_clause and _PRE_FIX_OR_FORM in
    # tests/test_searcher_sort_order_plan.py.
    return (
        f" AND ({expr} > %s OR ({expr} = %s AND m.id > %s)) ",
        [keyset.ts, keyset.ts, keyset.id],
    )
```

- [ ] **Step 4: Thread `order` through `_lexical_date_search`**

Change the signature to append `order: str`, replace the inline `keyset_clause` construction with a call to `_keyset_clause`, and replace the hardcoded ORDER BY with the table lookup:

```python
    def _lexical_date_search(
        self,
        conn: psycopg.Connection,
        parsed: ParsedQuery,
        page_size: int,
        keyset: KeysetCursor | None,
        order: str,
    ) -> tuple[list[SearchResult], KeysetCursor | None]:
```

Inside, replace the whole `keyset_clause = ""` / `if keyset is not None:` block with:

```python
        keyset_clause = ""
        if keyset is not None:
            keyset_clause, keyset_params = _keyset_clause(keyset, order)
            params.extend(keyset_params)
```

and the ORDER BY line in the SQL f-string with:

```python
             {_DATE_ORDER_BY_SQL[order]}
```

At the call site (~line 1032) pass `effective_order`:

```python
                results, next_keyset = self._lexical_date_search(
                    conn, parsed, effective_page_size, keyset_cursor,
                    effective_order,
                )
```

The `next_keyset` computation at the end of the function needs no change — it reads the last row of the page whichever way the page was ordered.

- [ ] **Step 5: Run the tests to verify they pass**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_searcher_sort_order_walk.py tests/test_searcher.py -v
```

Expected: PASS.

- [ ] **Step 6: Confirm the ascending plan is a backward index scan**

```bash
psql "postgresql://localhost:5532/localmail" -c "EXPLAIN (COSTS OFF) SELECT m.id FROM messages m WHERE m.fts_v2 @@ plainto_tsquery('simple','invoice') ORDER BY COALESCE(m.internal_date, m.date_sent) ASC NULLS FIRST, m.id ASC LIMIT 51"
```

Expected: the plan contains `Index Scan Backward using messages_recent_idx`. If it shows a `Sort` or `Gather Merge`, the ORDER BY was written with `NULLS LAST` — fix it before committing.

- [ ] **Step 7: Commit**

```bash
git add src/localmail/search/searcher.py tests/test_searcher_sort_order_walk.py
git commit -m "feat(search): ascending date order in the lexical keyset walk

ORDER BY and the keyset predicate both flip on the resolved order.
Ascending is ASC NULLS FIRST, id ASC -- the exact reverse of
messages_recent_idx, so it is served by a backward index scan with no
migration. Measured: 44 buffers against 33,372 for the NULLS LAST
spelling, which full-sorts, and an IS NOT NULL restriction does not
rescue that.

The two directions are not mirror images in shape. Descending needs
'OR expr IS NULL' so a dated cursor still admits the undated tail behind
it; ascending needs no disjunct, because NULLS FIRST puts the undated
block ahead of the cursor and NULL > ts is not true. The ascending dated
predicate is consequently the more index-friendly of the two.

Undated rows therefore sort first ascending, which is what makes
asc == reversed(desc) hold over the whole ordering.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Blank-query pagination — collapse the two walks into one

`_list_recent_messages` is `_lexical_date_search` minus the FTS predicate. Merge them, so the blank-query branch mints and honours cursors. This is the task that relaxes two existing guards; the spec's *"Consequence"* section is the authority for why.

**Files:**
- Modify: `src/localmail/search/searcher.py` (delete `_list_recent_messages` at ~470-529; rename and generalise `_lexical_date_search`; merge branches 1 and 2 in `search()` at ~1027-1090; move the `KeysetCursorUnusable` guard)
- Modify: `tests/test_searcher_keyset_guard.py` (one test's expectation inverts)
- Create: `tests/test_searcher_blank_query_paging.py`

**Interfaces:**
- Consumes: `_keyset_clause`, `_DATE_ORDER_BY_SQL` (Task 3).
- Produces: `Searcher._date_keyset_search(conn, parsed, page_size, keyset, order) -> tuple[list[SearchResult], KeysetCursor | None]` — replaces both `_lexical_date_search` and `_list_recent_messages`, which are deleted.

- [ ] **Step 1: Write the failing test**

Create `tests/test_searcher_blank_query_paging.py`:

```python
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""A blank query paginates, in both directions.

The blank-query branch used to return ``search_token=None``,
``has_more_in_pool=False`` and ``next_keyset=None``, so its next_cursor was
always null: one page, then nothing. That is exactly the branch "show me my
oldest mail" lands on, which made ascending order close to useless.

``_list_recent_messages`` was ``_lexical_date_search`` minus the FTS
predicate — same SELECT list, same ORDER BY, same filter composition — so
the two are one helper now and the blank branch inherits the keyset walk.
"""
from __future__ import annotations

from datetime import datetime, timezone

from localmail.config import SearchConfig
from localmail.db import open_pool
from localmail.search.searcher import Searcher


class _E:
    name = "s"; model = "s"; dimension = 768
    def embed_documents(self, t): return [[1.0] * 768 for _ in t]
    def embed_query(self, t): return [0.5] * 768
    def health_check(self): pass


def _seed(conn, n=7):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO accounts (name,email_address,imap_host,auth_method)"
                    " VALUES ('a','a@x','h','password') RETURNING id")
        acct = cur.fetchone()[0]
        for i in range(n):
            cur.execute(
                "INSERT INTO messages (account_id, message_id, raw_sha256, subject,"
                " body_text, headers, raw_bytes, size_bytes, internal_date)"
                " VALUES (%s,%s,%s,%s,%s,'{}'::jsonb,'r',1,%s)",
                (acct, f"<m{i}>", bytes([i + 1]) * 32, f"Subject {i}", "body",
                 datetime(2026, 3, i + 1, tzinfo=timezone.utc)),
            )
    conn.commit()


def _walk(searcher, *, order, page_size=3):
    ids: list[int] = []
    cursor = None
    for _ in range(50):
        page = searcher.search("", allowed_account_ids=None, page_size=page_size,
                               user_id=1, sort="date", sort_order=order,
                               keyset_cursor=cursor)
        ids.extend(r.message_id for r in page.results)
        if page.next_keyset is None:
            return ids
        cursor = page.next_keyset
    raise AssertionError("walk did not terminate")


def test_a_blank_query_emits_a_cursor_when_more_remain(db_dsn, db_conn):
    _seed(db_conn, n=7)
    pool = open_pool(db_dsn)
    try:
        s = Searcher(pool=pool, cfg=SearchConfig(), embeddings=_E(), reranker=None)
        page = s.search("", allowed_account_ids=None, page_size=3, user_id=1,
                        sort="date")
    finally:
        pool.close()
    assert len(page.results) == 3
    assert page.next_keyset is not None, (
        "the blank-query branch minted no cursor: 'show me my oldest mail' "
        "returns one page and stops"
    )


def test_a_blank_query_walk_covers_every_row_once_descending(db_dsn, db_conn):
    _seed(db_conn, n=7)
    pool = open_pool(db_dsn)
    try:
        s = Searcher(pool=pool, cfg=SearchConfig(), embeddings=_E(), reranker=None)
        ids = _walk(s, order="desc")
    finally:
        pool.close()
    assert len(ids) == len(set(ids)) == 7


def test_a_blank_query_walk_ascending_is_the_reverse(db_dsn, db_conn):
    _seed(db_conn, n=7)
    pool = open_pool(db_dsn)
    try:
        s = Searcher(pool=pool, cfg=SearchConfig(), embeddings=_E(), reranker=None)
        desc = _walk(s, order="desc")
        asc = _walk(s, order="asc")
    finally:
        pool.close()
    assert asc == list(reversed(desc))


def test_the_last_page_reports_no_cursor(db_dsn, db_conn):
    """A walk that never ends is worse than one that never starts."""
    _seed(db_conn, n=4)
    pool = open_pool(db_dsn)
    try:
        s = Searcher(pool=pool, cfg=SearchConfig(), embeddings=_E(), reranker=None)
        page = s.search("", allowed_account_ids=None, page_size=10, user_id=1,
                        sort="date")
    finally:
        pool.close()
    assert len(page.results) == 4
    assert page.next_keyset is None
```

- [ ] **Step 2: Run it to verify it fails**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_searcher_blank_query_paging.py -v
```

Expected: FAIL — `test_a_blank_query_emits_a_cursor_when_more_remain` fails on `page.next_keyset is not None`, and the walks raise `KeysetCursorUnusable` as soon as they pass a cursor back.

- [ ] **Step 3: Generalise the walk to an optional FTS predicate**

Rename `_lexical_date_search` to `_date_keyset_search` and make the FTS match conditional. Replace the opening of the body:

```python
        from localmail.search.arms import _filter_sql, build_lexical_tsquery

        where_extra, where_params = _filter_sql(parsed.filters)
        params: list[Any] = []
        if parsed.free_text.strip():
            tsq_sql, tsq_params = build_lexical_tsquery(
                parsed.free_text, parsed.expansion_terms
            )
            match_clause = f"m.fts_v2 @@ {tsq_sql}"
            params.extend(tsq_params)
        else:
            # No free text: the walk is over the whole (filtered) archive.
            # This is the branch that used to be _list_recent_messages, which
            # was this query minus the FTS predicate and minus the cursor.
            match_clause = "TRUE"
```

and the SQL's `WHERE` line:

```python
             WHERE {match_clause}
```

Update the docstring to say it serves both the lexical walk and the recent-mail walk, and that the FTS predicate is the only difference between them.

- [ ] **Step 4: Delete `_list_recent_messages` and merge the two branches**

Delete the whole `_list_recent_messages` method (~lines 470-529). It has no callers outside `searcher.py` — verified by grep across `src/`, `tests/` and `docs/`.

In `search()`, replace the two separate branch bodies (the `if effective_sort == "date" and parsed.free_text.strip():` block and the later `if not parsed.free_text.strip():` block) with one:

```python
        # The date-ordered keyset walk serves two intents that are one query.
        #
        # sort=date with free text: the hybrid path caps at
        # ``rerank_pool_size`` candidates fused by RRF, so a user searching
        # "e-ticket" sees only the top-K even though they asked for
        # chronological order. ``messages.fts_v2`` gives identical lexical
        # recall and the keyset cursor scrolls back arbitrarily far.
        #
        # A blank query, whatever the sort: the hybrid pipeline degenerates
        # for it (the BM25 arms early-return with no terms, the vector arms
        # rank by distance to the embedding of the empty string), so a blank
        # query has always been answered as a date-ordered list. It now
        # paginates too — before, it returned one page and no cursor, which
        # is the branch "show me my oldest mail" lands on.
        if effective_sort == "date" or not parsed.free_text.strip():
            t = time.monotonic()
            with self._pool.connection() as conn:
                parsed = self._resolve_account_names(conn, parsed)
                self._maybe_warn_unpopulated_body_lang(conn, parsed)
                results, next_keyset = self._date_keyset_search(
                    conn, parsed, effective_page_size, keyset_cursor,
                    effective_order,
                )
            timing["retrieve"] = (time.monotonic() - t) * 1000
            timing["rerank"] = 0.0
            timing["total"] = (time.monotonic() - t0) * 1000
            return SearchPage(
                results=results, page=1, page_size=effective_page_size,
                pool_size=len(results), candidates_per_arm=cpa,
                has_more_in_pool=next_keyset is not None,
                can_grow_pool=False,
                search_token=None, query=parsed, timing_ms=timing,
                next_keyset=next_keyset,
                rewrite_status=rewrite_status,
                rewrite_note=rewrite_note,
                rewrite_note_code=rewrite_note_code,
            )
```

- [ ] **Step 5: Narrow the `KeysetCursorUnusable` guard**

The guard currently sits after the old branch 1 and fires for both `rank` and blank-query shapes. It must now fire only for the hybrid pool branch — the one branch left that does not read the cursor. Keep it immediately after the merged branch above and replace its body:

```python
        # The branch above is the only reader of ``keyset_cursor`` (#308).
        # Reaching here means the caller's (sort, query) selected the hybrid
        # pool, whose page 1 would go back as if it continued the walk — a
        # restart wearing a continuation's clothes. Raise rather than answer
        # the wrong question quietly. A named error, not an assert: asserts
        # vanish under ``python -O``.
        #
        # The blank-query shape used to land here too. It no longer does:
        # that branch honours the cursor now, so rejecting it would forbid
        # exactly the paging this reachability change adds.
        if keyset_cursor is not None:
            raise KeysetCursorUnusable(
                "keyset_cursor is not readable by the hybrid pool branch; it "
                f"requires sort='date' or a blank query, got "
                f"sort={effective_sort!r} with a non-empty query"
            )
```

- [ ] **Step 6: Update the guard test whose expectation inverted**

In `tests/test_searcher_keyset_guard.py`, replace `test_an_empty_query_rejects_a_keyset_cursor_instead_of_dropping_it` with:

```python
def test_an_empty_query_now_reads_the_keyset_cursor() -> None:
    """The blank-query branch honours the cursor, so it must not be refused.

    It used to be refused because that branch dropped the cursor and
    answered with its own page 1. Now it continues the walk at the right
    position, and refusing would forbid the paging that change adds. The
    Searcher's pool raises on touch, so reaching retrieval is the assertion.
    """
    searcher, pool = _searcher()
    with pytest.raises(AssertionError, match="no connection"):
        searcher.search("", allowed_account_ids=None, sort="date",
                        keyset_cursor=_CURSOR)
    pool.connection.assert_called_once()
```

Update the module docstring's "The other two used to ignore it" to say the hybrid pool branch is now the only one that does not read it.

- [ ] **Step 7: Run the tests to verify they pass**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_searcher_blank_query_paging.py tests/test_searcher_keyset_guard.py tests/test_searcher_sort_order_walk.py tests/test_searcher.py tests/test_search_acl_clamp.py -v
```

Expected: PASS. `test_searcher.py` exercises the old `_list_recent_messages` name — if it references it directly, update the reference to `_date_keyset_search`; if it asserts `next_keyset is None` for a blank query, that assertion is now wrong and must be inverted with a comment naming this task.

- [ ] **Step 8: Commit**

```bash
git add src/localmail/search/searcher.py tests/test_searcher_blank_query_paging.py tests/test_searcher_keyset_guard.py tests/test_searcher.py
git commit -m "feat(search): a blank query paginates, in both directions

_list_recent_messages was _lexical_date_search minus the FTS predicate --
same SELECT list, same ORDER BY, same filter composition -- so the two are
one helper now and the blank-query branch inherits the keyset walk. It
used to return search_token=None, has_more_in_pool=False and
next_keyset=None, so its next_cursor was always null: one page, then
nothing. That is the branch 'show me my oldest mail' lands on, which made
ascending order close to useless.

Narrows the KeysetCursorUnusable guard to the hybrid pool branch, now the
only one that does not read the cursor. The blank-query shape used to land
there; refusing it would forbid exactly the paging this adds. That does
not weaken #308 -- the keyset cursor has never identified a query, only a
position, and re-sending different filters alongside it is equally
unvalidated. What #308 forbids is silently answering a differently
*ordered* question, and ordering is what the cursor carries.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: The cursor codec — `KA|` and `CursorPlan`

The keyset cursor carries no direction, so paging an ascending search the documented way (send `next_cursor`, state nothing else) would resolve the unstated order to `desc` and silently continue backwards. This is #308 on a new axis, and this task is the fix.

**Files:**
- Modify: `src/localmail/api/search_cursor.py` (whole module)
- Create: `tests/test_api_search_cursor_direction.py`

**Interfaces:**
- Consumes: `SortOrder`, `DEFAULT_SORT_ORDER` (Task 2).
- Produces:
  - `CursorPlan(mode: CursorMode, sort: SortMode, sort_order: SortOrder)` — frozen dataclass
  - `resolve_cursor_plan(*, cursor, requested_sort, requested_sort_order, free_text) -> CursorPlan` — replaces `resolve_cursor_mode`
  - `encode_keyset_cursor(ks, order) -> str` — gains a required second parameter
  - `keyset_order(raw: str) -> SortOrder`
  - `reject_pool_sort_mismatch(*, requested_sort, requested_sort_order, pool_sort, pool_sort_order) -> None`

- [ ] **Step 1: Write the failing test**

Create `tests/test_api_search_cursor_direction.py`:

```python
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""A keyset cursor carries the direction it continues (#308, new axis).

The cursor used to carry only ``(ts, id)``. Paging an ascending search the
documented way — send ``next_cursor`` back and state nothing else — would
resolve the unstated ``sort_order`` to ``desc`` and silently continue
backwards: page 1 of a differently ordered search wearing a continuation's
clothes, which looks right until the results repeat.

``K|`` keeps its meaning (descending), so no cursor in flight breaks and
``api.browse_cursor``'s shared payload encoding is untouched.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from localmail.api.errors import ValidationFailed
from localmail.api.search_cursor import (
    CursorPlan,
    decode_keyset_cursor,
    encode_keyset_cursor,
    is_keyset_cursor,
    keyset_order,
    resolve_cursor_plan,
)
from localmail.search.searcher import KeysetCursor

_KS = KeysetCursor(ts=datetime(2026, 5, 21, tzinfo=timezone.utc), id=100)


def test_the_two_directions_mint_different_prefixes() -> None:
    assert encode_keyset_cursor(_KS, "desc").startswith("K|")
    assert encode_keyset_cursor(_KS, "asc").startswith("KA|")


def test_both_prefixes_are_keyset_cursors_and_round_trip() -> None:
    for order in ("asc", "desc"):
        raw = encode_keyset_cursor(_KS, order)
        assert is_keyset_cursor(raw)
        assert keyset_order(raw) == order
        assert decode_keyset_cursor(raw) == _KS


def test_a_legacy_cursor_still_means_descending() -> None:
    """Cursors minted before this change carry no marker and must not flip."""
    legacy = encode_keyset_cursor(_KS, "desc")
    assert keyset_order(legacy) == "desc"
    plan = resolve_cursor_plan(cursor=legacy, requested_sort=None,
                               requested_sort_order=None, free_text="invoice")
    assert plan == CursorPlan(mode="keyset", sort="date", sort_order="desc")


def test_an_ascending_cursor_alone_continues_ascending() -> None:
    """The documented way to page: send the cursor, state nothing else."""
    raw = encode_keyset_cursor(_KS, "asc")
    plan = resolve_cursor_plan(cursor=raw, requested_sort=None,
                               requested_sort_order=None, free_text="invoice")
    assert plan == CursorPlan(mode="keyset", sort="date", sort_order="asc")


def test_a_stated_order_contradicting_the_cursor_is_rejected() -> None:
    raw = encode_keyset_cursor(_KS, "asc")
    with pytest.raises(ValidationFailed, match="sort_order"):
        resolve_cursor_plan(cursor=raw, requested_sort=None,
                            requested_sort_order="desc", free_text="invoice")


def test_a_stated_order_agreeing_with_the_cursor_is_accepted() -> None:
    raw = encode_keyset_cursor(_KS, "asc")
    plan = resolve_cursor_plan(cursor=raw, requested_sort="date",
                               requested_sort_order="asc", free_text="invoice")
    assert plan.sort_order == "asc"


def test_a_blank_query_with_a_keyset_cursor_is_now_allowed() -> None:
    """The blank-query branch honours the cursor since it gained pagination.

    Refusing would forbid exactly the paging that change adds. The cursor
    has never identified a query — it carries a position — so this is the
    same "send the same query and filters" contract that already governs
    every filter.
    """
    raw = encode_keyset_cursor(_KS, "asc")
    plan = resolve_cursor_plan(cursor=raw, requested_sort=None,
                               requested_sort_order=None, free_text="")
    assert plan == CursorPlan(mode="keyset", sort="date", sort_order="asc")


def test_a_fresh_request_resolves_both_defaults() -> None:
    plan = resolve_cursor_plan(cursor=None, requested_sort=None,
                               requested_sort_order=None, free_text="invoice")
    assert plan == CursorPlan(mode="fresh", sort="rank", sort_order="desc")


def test_a_fresh_request_keeps_what_the_caller_stated() -> None:
    plan = resolve_cursor_plan(cursor=None, requested_sort="date",
                               requested_sort_order="asc", free_text="invoice")
    assert plan == CursorPlan(mode="fresh", sort="date", sort_order="asc")


def test_a_pool_cursor_reports_the_pool_mode() -> None:
    plan = resolve_cursor_plan(cursor="tok-1:2", requested_sort=None,
                               requested_sort_order=None, free_text="invoice")
    assert plan.mode == "pool"
```

- [ ] **Step 2: Run it to verify it fails**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_api_search_cursor_direction.py -v
```

Expected: FAIL — `ImportError: cannot import name 'CursorPlan'`.

- [ ] **Step 3: Rewrite the codec**

In `src/localmail/api/search_cursor.py`, replace the prefix constant, `encode_keyset_cursor`, `resolve_cursor_mode` and `reject_pool_sort_mismatch`:

```python
_KEYSET_PREFIX_DESC = "K|"
_KEYSET_PREFIX_ASC = "KA|"

#: NOTE (review of #322): the reasoning on the next two lines is WRONG and
#: was NOT shipped. "KA|…" does not start with "K|" — the "|" terminator is
#: what keeps the two prefixes disjoint — so no scan order can misclassify a
#: cursor. See the shipped comment above _KEYSET_PREFIXES in
#: src/localmail/api/search_cursor.py for the corrected version.
#: Longest first: "KA|" also starts with "K", so a shortest-first scan
#: would classify every ascending cursor as descending.
_KEYSET_PREFIXES: tuple[tuple[str, SortOrder], ...] = (
    (_KEYSET_PREFIX_ASC, "asc"),
    (_KEYSET_PREFIX_DESC, "desc"),
)

#: The only sort a keyset cursor can continue — the date-keyset branch is
#: the sole minter and the sole reader of that cursor kind.
KEYSET_SORT: SortMode = "date"

CursorMode = Literal["fresh", "pool", "keyset"]


@dataclass(frozen=True)
class CursorPlan:
    """Which retrieval mode a request continues, and in what order.

    Returned by :func:`resolve_cursor_plan`. ``sort`` and ``sort_order``
    are the **resolved** values the request will actually run with: the
    cursor's own when it has one, the caller's when there is no cursor,
    the module defaults when neither states anything.

    One object rather than one function per axis. Two predicates for one
    rule is what produced the #308 follow-up defect, where the api gate
    and the retrieval branch disagreed about what counted as a blank
    query — so the axes are decided together or not at all.
    """
    mode: CursorMode
    sort: SortMode
    sort_order: SortOrder


def encode_keyset_cursor(ks: KeysetCursor, order: SortOrder) -> str:
    """Mint a keyset cursor that carries the direction it continues.

    ``order`` is required, not defaulted: a forgotten argument would mint
    a descending cursor for an ascending walk, which is the exact silent
    reversal this parameter exists to make impossible.
    """
    payload = encode_browse_cursor(BrowseCursor(ts=ks.ts, id=ks.id))
    prefix = _KEYSET_PREFIX_ASC if order == "asc" else _KEYSET_PREFIX_DESC
    return f"{prefix}{payload}"


def is_keyset_cursor(raw: str) -> bool:
    return any(raw.startswith(p) for p, _ in _KEYSET_PREFIXES)


def keyset_order(raw: str) -> SortOrder:
    """The direction a keyset cursor continues.

    A cursor minted before the ascending prefix existed carries ``K|`` and
    is descending, which is what it has always meant — so no cursor in
    flight changes meaning.
    """
    for prefix, order in _KEYSET_PREFIXES:
        if raw.startswith(prefix):
            return order
    raise ValidationFailed(f"cursor: not a keyset cursor: {raw!r}")


def decode_keyset_cursor(raw: str) -> KeysetCursor:
    for prefix, _ in _KEYSET_PREFIXES:
        if raw.startswith(prefix):
            bc = decode_browse_cursor(raw[len(prefix):])
            return KeysetCursor(ts=bc.ts, id=bc.id)
    raise ValidationFailed(f"cursor: not a keyset cursor: {raw!r}")


def resolve_cursor_plan(
    *,
    cursor: str | None,
    requested_sort: SortMode | None,
    requested_sort_order: SortOrder | None,
    free_text: str,
) -> CursorPlan:
    """Decide the retrieval mode and both ordering axes — cursor first.

    ``free_text`` must be ``parse_query(...).free_text``, **not** the raw
    request field: filter operators parse out of the free text, so
    ``"subject:invoice"`` is non-blank as a request field and blank by the
    time ``Searcher.search`` tests it, and this function's job is to ask
    the question the Searcher will ask.

    A ``None`` on either axis means the caller stated nothing. That is the
    documented way to page, so an unstated value never out-votes the
    cursor: the cursor is the only statement about ordering in such a
    request, and it was minted by us.

    A *stated* value the cursor cannot serve raises ``ValidationFailed``.
    Coercing it would ignore the caller silently; honouring it means
    dropping the cursor, which answers a paging request with page 1 of a
    differently ordered search.
    """
    if cursor is None:
        return CursorPlan(
            mode="fresh",
            sort=DEFAULT_SORT if requested_sort is None else requested_sort,
            sort_order=(DEFAULT_SORT_ORDER if requested_sort_order is None
                        else requested_sort_order),
        )
    if is_keyset_cursor(cursor):
        order = keyset_order(cursor)
        _reject_sort_mismatch(requested=requested_sort, cursor_sort=KEYSET_SORT)
        _reject_order_mismatch(requested=requested_sort_order, cursor_order=order)
        # A blank query is allowed: the blank-query branch reads the cursor
        # too since it gained pagination, so refusing would forbid exactly
        # that paging. The cursor carries a position, never a query — the
        # "send the same query and filters" contract is unchanged, and it
        # already governs every filter equally.
        return CursorPlan(mode="keyset", sort=KEYSET_SORT, sort_order=order)
    return CursorPlan(
        mode="pool",
        sort=DEFAULT_SORT if requested_sort is None else requested_sort,
        sort_order=(DEFAULT_SORT_ORDER if requested_sort_order is None
                    else requested_sort_order),
    )


def reject_pool_sort_mismatch(
    *,
    requested_sort: SortMode | None,
    requested_sort_order: SortOrder | None,
    pool_sort: SortMode,
    pool_sort_order: SortOrder,
) -> None:
    """Guard both axes of the pool kind, whose ordering lives in the pool.

    ``Searcher.continue_page`` serves whatever the pool was minted with, so
    a contradicting stated value is not applied — and was not reported
    either, until #308.
    """
    _reject_sort_mismatch(requested=requested_sort, cursor_sort=pool_sort)
    _reject_order_mismatch(requested=requested_sort_order,
                           cursor_order=pool_sort_order)


def _reject_sort_mismatch(*, requested: SortMode | None, cursor_sort: SortMode) -> None:
    if requested is not None and requested != cursor_sort:
        raise ValidationFailed(
            f"cursor: this cursor continues a {cursor_sort}-sorted search; "
            f"pass sort={cursor_sort!r} or omit sort (got {requested!r})"
        )


def _reject_order_mismatch(
    *, requested: SortOrder | None, cursor_order: SortOrder,
) -> None:
    if requested is not None and requested != cursor_order:
        raise ValidationFailed(
            f"cursor: this cursor continues a {cursor_order}ending search; "
            f"pass sort_order={cursor_order!r} or omit sort_order "
            f"(got {requested!r})"
        )
```

Add `DEFAULT_SORT`, `DEFAULT_SORT_ORDER` and `SortOrder` to the existing `from localmail.search.searcher import …` line, and delete `resolve_cursor_mode`. Update the module docstring to describe both prefixes.

- [ ] **Step 4: Run the tests to verify they pass**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_api_search_cursor_direction.py -v
```

Expected: PASS. `tests/test_api_search_cursor_mode.py` will still fail — it drives the old names and is fixed in Task 6, which is where its call sites move.

- [ ] **Step 5: Commit**

```bash
git add src/localmail/api/search_cursor.py tests/test_api_search_cursor_direction.py
git commit -m "feat(search): the keyset cursor carries its direction (KA| prefix)

The cursor carried only (ts, id), so paging an ascending search the
documented way -- send next_cursor, state nothing else -- would resolve the
unstated sort_order to desc and silently continue backwards. #308 on a new
axis.

K| keeps meaning descending, so no cursor in flight changes meaning and
api.browse_cursor's shared payload encoding is untouched -- which is why a
prefix rather than a payload field. (NOTE, review of #322: the longest-first
rationale that followed here was wrong — "KA|" does not start with "K|", so the
prefixes are disjoint whatever the scan order.) Prefixes are matched longest-first:
'KA|' also starts with 'K'.

resolve_cursor_mode becomes resolve_cursor_plan, returning the resolved
mode and both axes together. One object rather than one function per axis:
two predicates for one rule is what produced the #308 follow-up defect
where the api gate and the retrieval branch disagreed about what counted
as a blank query.

encode_keyset_cursor's order argument is required, not defaulted -- a
forgotten one would mint a descending cursor for an ascending walk, the
exact reversal the parameter exists to prevent.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: Thread `sort_order` through the api layer

`run_search` learns the new axis: it resolves the plan, refuses rank+asc with a 400, passes both axes to the Searcher, and mints direction-carrying cursors.

**Files:**
- Modify: `src/localmail/api/search.py` (imports; `run_search` signature and body; `_check_pool_sort`; `_next_cursor`)
- Modify: `tests/test_api_search_cursor_mode.py` (two tests invert; call sites gain the new kwarg name)
- Create: `tests/test_api_search_sort_order.py`

**Interfaces:**
- Consumes: `CursorPlan`, `resolve_cursor_plan`, `encode_keyset_cursor(ks, order)`, `keyset_order`, `reject_pool_sort_mismatch` (Task 5); `SortOrderNotApplicable`, `DEFAULT_SORT_ORDER` (Task 2).
- Produces: `run_search(..., sort_order: SortOrder | None = None)`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_api_search_sort_order.py`:

```python
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""``sort_order`` at the api boundary: threading, refusal, and cursor minting."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from localmail.api.errors import ValidationFailed
from localmail.api.search import run_search
from localmail.api.search_cursor import encode_keyset_cursor, keyset_order
from localmail.search.searcher import KeysetCursor, PoolMetadata

_KS = KeysetCursor(ts=datetime(2026, 5, 21, tzinfo=timezone.utc), id=100)


def _page(*, token=None, next_keyset=None):
    p = MagicMock()
    p.results = []
    p.search_token = token
    p.pool_size = 0
    p.page_size = 2
    p.page = 1
    p.has_more_in_pool = False
    p.can_grow_pool = False
    p.candidates_per_arm = 50
    p.timing_ms = {"total": 1.0}
    p.next_keyset = next_keyset
    p.rewrite_status = "not_requested"
    p.rewrite_note = None
    p.rewrite_note_code = None
    return p


def _searcher(page=None):
    s = MagicMock()
    s.config.candidates_per_arm = 50
    s.config.candidates_per_arm_max = 800
    s.smart_available = False
    s.search.return_value = page or _page()
    return s


def test_a_stated_order_reaches_the_searcher() -> None:
    s = _searcher()
    run_search(searcher=s, free_text="invoice", filters={}, limit=2,
               allowed_account_ids=[1], user_id=99, sort="date",
               sort_order="asc")
    _, kwargs = s.search.call_args
    assert kwargs.get("sort_order") == "asc"


def test_an_unstated_order_reaches_the_searcher_as_desc() -> None:
    """Resolved at this boundary, from the one shared default."""
    s = _searcher()
    run_search(searcher=s, free_text="invoice", filters={}, limit=2,
               allowed_account_ids=[1], user_id=99, sort="date")
    _, kwargs = s.search.call_args
    assert kwargs.get("sort_order") == "desc"


def test_rank_with_ascending_is_a_validation_error_not_a_search() -> None:
    s = _searcher()
    with pytest.raises(ValidationFailed, match="sort_order"):
        run_search(searcher=s, free_text="invoice", filters={}, limit=2,
                   allowed_account_ids=[1], user_id=99, sort="rank",
                   sort_order="asc")
    s.search.assert_not_called()


def test_rank_with_ascending_is_refused_even_with_an_empty_acl() -> None:
    """Validation precedes the empty-ACL short-circuit.

    That branch answers with an empty page, byte-identical to "you have
    reached the end" — so a grant-nothing caller would be told a
    contradictory request had succeeded and was complete.
    """
    s = _searcher()
    with pytest.raises(ValidationFailed):
        run_search(searcher=s, free_text="invoice", filters={}, limit=2,
                   allowed_account_ids=[], user_id=99, sort="rank",
                   sort_order="asc")


def test_an_ascending_page_mints_an_ascending_cursor() -> None:
    s = _searcher(_page(next_keyset=_KS))
    out = run_search(searcher=s, free_text="invoice", filters={}, limit=2,
                     allowed_account_ids=[1], user_id=99, sort="date",
                     sort_order="asc")
    assert keyset_order(out["next_cursor"]) == "asc", (
        "an ascending walk minted a descending cursor: the next page would "
        "silently reverse"
    )


def test_a_descending_page_mints_a_descending_cursor() -> None:
    s = _searcher(_page(next_keyset=_KS))
    out = run_search(searcher=s, free_text="invoice", filters={}, limit=2,
                     allowed_account_ids=[1], user_id=99, sort="date")
    assert keyset_order(out["next_cursor"]) == "desc"


def test_an_ascending_cursor_alone_continues_ascending() -> None:
    """The documented round trip, end to end through run_search."""
    s = _searcher()
    raw = encode_keyset_cursor(_KS, "asc")
    run_search(searcher=s, free_text="invoice", filters={}, limit=2,
               allowed_account_ids=[1], user_id=99, cursor=raw)
    _, kwargs = s.search.call_args
    assert kwargs.get("sort") == "date"
    assert kwargs.get("sort_order") == "asc"
    assert kwargs.get("keyset_cursor") == _KS


def test_a_stated_order_contradicting_the_cursor_is_a_400() -> None:
    s = _searcher()
    raw = encode_keyset_cursor(_KS, "asc")
    with pytest.raises(ValidationFailed, match="sort_order"):
        run_search(searcher=s, free_text="invoice", filters={}, limit=2,
                   allowed_account_ids=[1], user_id=99, sort_order="desc",
                   cursor=raw)
    s.search.assert_not_called()


def test_a_pool_cursor_rejects_a_contradicting_order() -> None:
    s = _searcher()
    s.get_pool_metadata.return_value = PoolMetadata(
        candidates_per_arm=50, page_size=2, rerank_pool_size=100, pool_size=10,
        sort="rank", sort_order="desc",
    )
    with pytest.raises(ValidationFailed, match="sort_order"):
        run_search(searcher=s, free_text="invoice", filters={}, limit=2,
                   allowed_account_ids=[1], user_id=99, sort_order="asc",
                   cursor="tok-1:2")
```

- [ ] **Step 2: Run it to verify it fails**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_api_search_sort_order.py -v
```

Expected: FAIL — `run_search() got an unexpected keyword argument 'sort_order'`.

- [ ] **Step 3: Update the imports and the signature**

In `src/localmail/api/search.py`, change the `search_cursor` import block to:

```python
from localmail.api.search_cursor import (
    SearchCursor,
    decode_keyset_cursor,
    decode_search_cursor,
    encode_keyset_cursor,
    encode_search_cursor,
    keyset_order,
    reject_pool_sort_mismatch,
    resolve_cursor_plan,
)
```

and the `searcher` import block to add `DEFAULT_SORT_ORDER`, `SortOrder` and `SortOrderNotApplicable`. `KEYSET_SORT` is no longer needed — the plan carries the sort.

Add to `run_search`'s signature, after `sort`:

```python
    sort_order: SortOrder | None = None,
```

- [ ] **Step 4: Resolve the plan and refuse rank+asc**

Replace the `mode = resolve_cursor_mode(...)` call with:

```python
    plan = resolve_cursor_plan(cursor=cursor, requested_sort=sort,
                               requested_sort_order=sort_order,
                               free_text=parse_query(free_text).free_text)
    # Refused here as well as in the Searcher so the caller gets a clean 400
    # before any work; the Searcher's own guard is what covers CLI and
    # library callers, who never reach this function. Ahead of the empty-ACL
    # short-circuit below, which answers with an empty page indistinguishable
    # from "you have reached the end" — a contradictory request must not be
    # reported as a completed one.
    if plan.mode != "keyset" and plan.sort == "rank" and plan.sort_order == "asc":
        raise ValidationFailed(
            "sort_order='asc' is not applicable to sort='rank' (the default); "
            "pass sort='date' for oldest-first"
        )
```

- [ ] **Step 5: Pass both axes to the Searcher on all three branches**

In the `cursor is None` branch, replace the `searcher.search(...)` call's `sort=` argument:

```python
        page = searcher.search(query, page_size=limit, user_id=user_id,
                               sort=plan.sort, sort_order=plan.sort_order,
                               smart=effective_smart,
                               allowed_account_ids=allowed_account_ids)
```

In the `elif mode == "keyset":` branch — which becomes `elif plan.mode == "keyset":` —

```python
        page = searcher.search(query, page_size=limit, user_id=user_id,
                               sort=plan.sort, sort_order=plan.sort_order,
                               keyset_cursor=keyset,
                               allowed_account_ids=allowed_account_ids)
```

Extend that branch's `except KeysetCursorUnusable` to also catch `SortOrderNotApplicable`, mapping it to `ValidationFailed` the same way. Catch the named subclasses, never bare `ValueError`.

Replace every other `mode ==` comparison in the function with `plan.mode ==`.

- [ ] **Step 6: Update `_check_pool_sort` and `_next_cursor`**

`_check_pool_sort` gains the second axis:

```python
def _check_pool_sort(
    searcher: Searcher, parsed: SearchCursor, *,
    requested_sort: SortMode | None, requested_sort_order: SortOrder | None,
    user_id: int,
) -> None:
    """Reject a stated ordering the cached pool cannot serve.

    Only reached when the caller stated one — with nothing to contradict,
    the pool stays the authority and no cache probe is spent.
    """
    if requested_sort is None and requested_sort_order is None:
        return
    meta = searcher.get_pool_metadata(parsed.token, user_id=user_id)
    if meta is None:
        raise SearchCursorExpired(f"cursor {parsed.token!r} not found")
    reject_pool_sort_mismatch(requested_sort=requested_sort,
                              requested_sort_order=requested_sort_order,
                              pool_sort=meta.sort,
                              pool_sort_order=meta.sort_order)
```

Its call site passes the **raw** arguments, not the plan's resolved ones — a resolved default would read as a contradiction against a pool built the other way:

```python
        _check_pool_sort(searcher, parsed, requested_sort=sort,
                         requested_sort_order=sort_order, user_id=user_id)
```

`_next_cursor` must mint the direction the page was walked in. Give it the order and use it:

```python
def _next_cursor(page: Any, *, cfg: SearchConfig, order: SortOrder) -> str | None:
    if page.next_keyset is not None:
        return encode_keyset_cursor(page.next_keyset, order)
    ...
```

and its call site:

```python
    next_cursor = _next_cursor(page, cfg=cfg, order=plan.sort_order)
```

- [ ] **Step 7: Update the existing cursor-mode tests**

In `tests/test_api_search_cursor_mode.py`:

- Every `encode_keyset_cursor(ks)` call becomes `encode_keyset_cursor(ks, "desc")`.
- `test_keyset_cursor_without_the_original_query_is_rejected` and `test_keyset_cursor_without_query_or_sort_is_rejected` now assert the opposite. Replace both with one test:

```python
def test_a_keyset_cursor_with_a_blank_query_continues_the_recent_mail_walk() -> None:
    """Both branches read the cursor now, so neither shape is refused.

    These two used to be rejections, because the blank-query branch dropped
    the cursor and answered with its own page 1. That branch paginates now,
    so refusing would forbid exactly the paging it gained. The cursor
    carries a position, never a query — the "send the same query and
    filters" contract is unchanged and already governs every filter.
    """
    s = _searcher()
    incoming, cursor = _keyset_cursor()
    run_search(searcher=s, free_text="", filters={}, limit=2,
               allowed_account_ids=[1], user_id=99, cursor=cursor)
    _, kwargs = s.search.call_args
    assert kwargs.get("sort") == "date"
    assert kwargs.get("keyset_cursor") == incoming
```

- The `PoolMetadata(...)` constructions in that file already gained `sort_order="desc"` in Task 2; leave them.

- [ ] **Step 8: Run the api suite**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_api_search_sort_order.py tests/test_api_search_cursor_mode.py tests/test_api_search_pagination.py tests/test_api_search_cursor.py tests/test_api_search_cursor_error.py tests/test_api_search.py -v
```

Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add src/localmail/api/search.py tests/test_api_search_sort_order.py tests/test_api_search_cursor_mode.py
git commit -m "feat(search): thread sort_order through run_search

run_search resolves a CursorPlan, refuses rank+asc as a 400 ahead of the
empty-ACL short-circuit (which answers with an empty page indistinguishable
from 'you have reached the end', so a contradictory request must not be
reported as a completed one), passes both axes to the Searcher, and mints
cursors carrying the direction the page was actually walked in.

_check_pool_sort takes the raw arguments rather than the plan's resolved
ones: a resolved default would read as a contradiction against a pool built
the other way, which is #312's defect exactly.

Two cursor-mode tests invert. Rejecting a keyset cursor presented with a
blank query existed because that branch dropped it; it paginates now, so
the rejection would forbid the paging it gained.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 7: The wire — HTTP route and MCP tool

Expose `sort_order` on both transports. Until this task an `asc` request is unreachable from outside the process.

**Files:**
- Modify: `src/localmail/serve/routes/search.py` (`SearchRequest`, the `run_search` call)
- Modify: `src/localmail/mcp/tools.py` (`tool_search`)
- Modify: `src/localmail/mcp/server.py` (the `search` tool parameter, its docstring, the `tool_search` call)
- Create: `tests/test_serve_search_sort_order.py`
- Modify: `tests/test_mcp_server_build.py` (add the schema-default assertion)

**Interfaces:**
- Consumes: `run_search(..., sort_order=...)` (Task 6).
- Produces: the `sort_order` wire field on both transports.

- [ ] **Step 1: Write the failing test**

Create `tests/test_serve_search_sort_order.py`:

```python
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""``sort_order`` on the HTTP wire: accepted, defaulted, and null-by-default."""
from __future__ import annotations

from localmail.serve.routes.search import SearchRequest


def test_sort_order_is_null_by_default_not_desc() -> None:
    """"Omitted" must stay distinguishable from "asked for" (#308).

    Alongside a cursor the cursor decides the ordering; a model default of
    "desc" would be a statement the caller never made, and would contradict
    every ascending cursor.
    """
    assert SearchRequest(query="x").sort_order is None


def test_sort_order_accepts_both_directions() -> None:
    assert SearchRequest(query="x", sort_order="asc").sort_order == "asc"
    assert SearchRequest(query="x", sort_order="desc").sort_order == "desc"


def test_sort_order_rejects_anything_else() -> None:
    import pytest
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        SearchRequest(query="x", sort_order="ascending")


def test_sort_is_still_null_by_default() -> None:
    assert SearchRequest(query="x").sort is None
```

Add to `tests/test_mcp_server_build.py`, beside the existing sort test:

```python
def test_search_declares_no_sort_order_default_of_its_own(db_dsn):
    """The MCP tool must not fill in a direction the agent did not ask for.

    server.py restates every parameter for the agent-facing schema, so a
    default written here is sent on the agent's behalf — and a "desc" sent
    that way contradicts every ascending cursor, turning the documented
    paging call into a 400. Fixing run_search alone would not catch this.
    """
    pool = ConnectionPool(db_dsn, min_size=1, max_size=2, open=True)
    try:
        server = build_mcp_server(pool, searcher=None, config=McpConfig(enabled=True))
        tools = {t.name: t for t in asyncio.run(server.list_tools())}
    finally:
        pool.close()
    props = (tools["search"].inputSchema or {})["properties"]
    assert "sort_order" in props, "the search tool does not expose sort_order"
    assert props["sort_order"].get("default") is None, (
        f"search states sort_order={props['sort_order'].get('default')!r} "
        "for the agent: an ascending cursor can never win against it"
    )
```

- [ ] **Step 2: Run them to verify they fail**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_serve_search_sort_order.py tests/test_mcp_server_build.py -v
```

Expected: FAIL — `SearchRequest` has no `sort_order`, and the MCP schema lacks the property.

- [ ] **Step 3: Add the HTTP field**

In `src/localmail/serve/routes/search.py`, add to `SearchRequest` immediately after `sort`:

```python
    # Direction for the sort criterion above. Orthogonal to `sort` so a
    # future criterion inherits it without doubling the `sort` enum.
    # "asc" is rejected for sort="rank": the rank path serves a bounded
    # candidate pool, so reversing it returns the least relevant of the top
    # hits rather than of the archive.
    #
    # Null rather than "desc" for the reason `sort` is null: alongside a
    # `cursor` the cursor decides the direction, and a model default would
    # be a statement the caller never made — contradicting every ascending
    # cursor.
    sort_order: Literal["asc", "desc"] | None = None
```

and pass it through in `search_endpoint`:

```python
        sort_order=req.sort_order,
```

- [ ] **Step 4: Add the MCP parameter**

In `src/localmail/mcp/tools.py`, add to `tool_search`'s signature after `sort` and forward it to `run_search`:

```python
    sort_order: Literal["asc", "desc"] | None = None,
```

```python
        sort_order=sort_order,
```

In `src/localmail/mcp/server.py`, add the parameter to the `search` tool immediately after `sort`:

```python
        sort_order: Annotated[Literal["asc", "desc"] | None, Field(description=(
            'Direction for `sort`: "desc" (the default when omitted) or '
            '"asc". Only applicable to sort="date" — oldest first; '
            'sort="rank" with sort_order="asc" is rejected, because the '
            "rank path searches a bounded candidate pool and reversing it "
            "returns the least relevant of the top hits rather than of the "
            "archive. Leave it unset when paging — a `cursor` already "
            "carries the direction it continues, and a direction that "
            "contradicts it is rejected."))] = None,
```

and forward it in the `tools.tool_search(...)` call:

```python
                sort_order=sort_order,
```

Update the tool's docstring: after *"pass `sort="date"` for strictly newest-first"* add *"and `sort_order="asc"` alongside it for oldest-first"*, and change *"leave `sort` unset"* to *"leave `sort` and `sort_order` unset"*.

- [ ] **Step 5: Run the tests to verify they pass**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_serve_search_sort_order.py tests/test_mcp_server_build.py tests/test_mcp_tools.py tests/test_serve_search_route.py -v
```

Expected: PASS.

- [ ] **Step 6: Run the whole suite**

```bash
unset VIRTUAL_ENV && uv run pytest -q
```

Expected: PASS. Investigate every failure — do not proceed with a red suite.

- [ ] **Step 7: Commit**

```bash
git add src/localmail/serve/routes/search.py src/localmail/mcp/tools.py src/localmail/mcp/server.py tests/test_serve_search_sort_order.py tests/test_mcp_server_build.py
git commit -m "feat(search): expose sort_order on the HTTP and MCP surfaces

Null-by-default on both, for the reason sort is: alongside a cursor the
cursor decides, and a model default would be a statement the caller never
made -- contradicting every ascending cursor.

server.py restates every parameter for the agent-facing schema, so its
default is the half that is easy to miss: a 'desc' sent on an agent's
behalf turns the documented paging call into a 400. Pinned by reading the
default off the published inputSchema, beside the same pin for sort.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 8: Documentation

**Files:**
- Modify: `README.md` (~872-908)
- Modify: `docs/mcp-usage.md` (line 268 tool table, ~284-291 paging guidance)
- Modify: `CLAUDE.md` (the "Browse & search pagination" section ~line 900 and ~3012)

**Interfaces:**
- Consumes: everything above. No code.

- [ ] **Step 1: Update `README.md`**

In the cursor-flavour list, extend the keyset bullet and correct the paragraph that follows:

```markdown
- **Keyset cursor** (`"K|<base64>"` descending, `"KA|<base64>"` ascending)
  — used for `sort=date` and for any blank-query search, backed by a scan
  over `COALESCE(internal_date, date_sent)` (lexical when there is free
  text). Unbounded scroll; no pool cap. Same recall as the lexical
  retrieval arm. The prefix carries the direction so a cursor paged back
  on its own continues the order it was minted in.

When paging, send the cursor back with the same `query` and filters and
**leave `sort` and `sort_order` unset**. The cursor already carries the
ordering it continues, so a stated value that contradicts it on either
axis is a 400 rather than a silent restart at page 1 of a differently
ordered search. The cursor carries a position, not a query: re-sending a
different `query` or different filters alongside it is undefined, the same
way it always has been.
```

Wherever the README documents `sort`, add: `sort_order` is `"asc"` or `"desc"` (default `"desc"`), applies to `sort=date`, and `sort=rank` with `sort_order=asc` is a 400.

- [ ] **Step 2: Update `docs/mcp-usage.md`**

In the tool table (line 268) insert `sort_order="asc"\|"desc"` after `sort`. In the paging guidance (~291) change **"Leave `sort` unset."** to **"Leave `sort` and `sort_order` unset."**, and extend the sentence at ~284 to note that a blank-query search now pages too.

- [ ] **Step 3: Update `CLAUDE.md`**

In the "Browse & search pagination" section, under the keyset-cursor bullet, record: both prefixes; that `sort_order` is orthogonal and defaults to `desc`; that rank+asc is a 400 with the bounded-pool reasoning; that ascending is `ASC NULLS FIRST` served by a backward scan with the measured buffer counts; that undated rows sort first ascending; that the blank-query branch now paginates; and that the two "keyset needs a query" guards were relaxed with the reasoning from the spec.

Correct the stale claim at ~line 3012 that the pool cursor serves *"`sort=date` with an empty query"* — an empty query takes the keyset branch and mints no pool cursor. Note beside it that `_date_sort_key` is unreachable and pinned by `tests/test_searcher_pool_sort_unreachable.py`.

Update the layout line at ~902-904 naming `_list_recent_messages` and `_lexical_date_search` to name `_date_keyset_search`.

- [ ] **Step 4: Verify no stale references remain**

```bash
grep -rn "_list_recent_messages\|_lexical_date_search\|resolve_cursor_mode" src/ tests/ README.md CLAUDE.md docs/mcp-usage.md
```

Expected: no hits outside `docs/handoffs/` and `docs/superpowers/` (historical records, correctly left alone).

- [ ] **Step 5: Commit**

```bash
git add README.md docs/mcp-usage.md CLAUDE.md
git commit -m "docs(search): sort_order, the KA| prefix, and two corrections

Documents the new axis on all three surfaces, plus the measured index
facts (ASC NULLS FIRST is served by a backward scan at 44 buffers; the
NULLS LAST spelling full-sorts at 33,372, and an IS NOT NULL restriction
does not rescue it) so the spelling is not 'normalised' later.

Two corrections to claims that were already stale: the pool cursor does
not serve 'sort=date with an empty query' -- an empty query takes the
keyset branch and mints no pool cursor -- and the keyset cursor is no
longer rejected without a query, since the blank-query branch reads it.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage:**

| Spec section | Task |
| --- | --- |
| Orthogonal `sort_order`, not new `sort` members | 2, 7 |
| The cross-product table / rank+asc 400 | 2 (Searcher), 6 (api), 7 (wire) |
| One authority for the resolved value | 2 |
| The guard is enforced twice | 2, 6 |
| `KA|` prefix | 5 |
| `resolve_cursor_plan` / `CursorPlan` | 5 |
| `PoolMetadata.sort_order`, no default | 2 (field + write), 6 (read) |
| SQL: `ASC NULLS FIRST`, no migration | 3 |
| Undated rows first ascending | 3 |
| Keyset predicates, both directions | 3 |
| Blank-query pagination | 4 |
| Consequence: two guards relax | 4 (Searcher), 5 + 6 (api) |
| `_date_sort_key` pinned and documented | 1 |
| Testing: round trip, MCP schema default, invariant, refusal, contradiction, blank paging, plan assertion, pool unreachability | 6, 7, 3, 2+6, 5+6, 4, 3 (step 6), 1 |
| Documentation | 8 |

**Placeholder scan:** none — every step carries the literal code or command.

**Type consistency checked:** `SortOrder` and `DEFAULT_SORT_ORDER` are defined in Task 2 and imported by Tasks 5-7. `encode_keyset_cursor(ks, order)` gains its second argument in Task 5 and every call site is updated in Tasks 6-7. `_date_keyset_search` is named identically in Tasks 4 and 8. `resolve_cursor_plan`'s four keyword arguments match between Tasks 5 and 6. `reject_pool_sort_mismatch`'s four keyword arguments match between Tasks 5 and 6. `PoolMetadata`'s defaultless `sort_order` breaks five hand-written constructions across three test files, all enumerated in Task 2 step 6 — verified by `grep -n "PoolMetadata(" tests/*.py`.

**One risk to watch:** Task 3's `_keyset_clause` and Task 4's `_date_keyset_search` are the only places the ordering is expressed. If an executor writes the ORDER BY inline instead of using `_DATE_ORDER_BY_SQL`, the plan assertion in Task 3 step 6 is the check that catches a `NULLS LAST` slip.
