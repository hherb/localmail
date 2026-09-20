# Compact Search Hits (kastellan slice E) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `POST /v1/search` gains two request fields. `fields` projects each hit to exactly the named keys, including a new plain-text `snippet`. `snippet_chars` sizes the snippet window, capped by config. The default response stays byte-identical.

**Architecture:**
- A new pure module, `api/search_projection.py`, owns the permitted name set and both validation rules. Each rule returns a message or `None`.
- `run_search` validates both arguments ahead of the empty-ACL short-circuit, then projects hits last.
- The Searcher threads a per-call snippet width into `_build_results`. That is where `make_snippet` runs, per page, from the full cached source text.

**Tech Stack:** Python 3.13, FastAPI + pydantic v2, psycopg 3, pytest against the real `localmail_test` Postgres (port 5532).

**Spec:** `docs/superpowers/specs/2026-09-19-search-hit-projection-design.md` (read it first; this plan argues from it).

## Global Constraints

- Default response of `POST /v1/search` (no `fields`, no `snippet_chars`) must stay **byte-identical**: eleven hit keys, default-width snippet.
- `HIT_FIELDS` order, verbatim: `message_id, account, folder, subject, from, to, date, snippet_html, has_attachments, score, matched_arms, snippet`.
- `snippet_chars` range: `1 ≤ n ≤ [search] snippet_max_chars`; `snippet_max_chars` default **1000**; `bool` is refused; out of range is a **400 problem+json**, never clamped.
- `snippet_max_chars` must be `>= snippet_width_chars` (default 200) — config validation error otherwise.
- Both new arguments are validated **before** the empty-ACL short-circuit in `run_search`.
- `API_MINOR` goes **2 → 3**.
- Surface is `POST /v1/search` only. No MCP schema change, and no `/v1/messages` change.
- The pydantic model must **not** carry `ge`/`le` on `snippet_chars`. That would answer 422 with an array `detail` (#370). The pure rule answers 400.
- Keep every touched source file's growth minimal. `api/search.py` is already 726 lines, so new logic goes in the pure module.
- Tests: TDD. Never run two pytest sessions at once against the test DB. Run pytest in the **foreground**. Use `unset VIRTUAL_ENV && uv run pytest …`.
- Every new file starts with the two SPDX lines used across `src/` and `tests/`:
  `# SPDX-License-Identifier: AGPL-3.0-or-later` / `# Copyright (C) 2026 Horst Herb`.
- Commit messages end with `Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>`. Stage specific files, never `git add -A`.
- Assert new wire keys **through the real transport** (TestClient + a real `Searcher`), never through a `MagicMock` hit. An unset MagicMock attribute serialises to `{}`/`[]` instead of failing.

---

### Task 1: Pure projection module

**Files:**
- Create: `src/localmail/api/search_projection.py`
- Test: `tests/test_search_projection.py`

**Interfaces:**
- Consumes: `localmail.api.search._to_api_result` (test only, for the equality pin), `localmail.search.searcher.SearchResult` (test only).
- Produces:
  - `HIT_FIELDS: tuple[str, ...]`
  - `fields_error(fields: object) -> str | None`
  - `snippet_chars_error(value: object, *, max_chars: int) -> str | None`
  - `project_hit(hit: dict[str, Any], fields: Sequence[str]) -> dict[str, Any]`

- [ ] **Step 1: Write the failing tests**

```python
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""The pure rules behind `fields` and `snippet_chars` (kastellan slice E)."""
from __future__ import annotations

import pytest

from localmail.api.search import _to_api_result
from localmail.api.search_projection import (
    HIT_FIELDS,
    fields_error,
    project_hit,
    snippet_chars_error,
)
from localmail.search.searcher import SearchResult


def _result() -> SearchResult:
    return SearchResult(
        message_id=7, account_id=1, rank=1, score=0.5, rrf_score=0.5,
        subject="s", from_addr="a@b", from_name="A",
        date_sent=None, internal_date=None,
        snippet="the snippet", snippet_source="body",
        attachment_filename=None, has_attachments=False,
        matched_chunk_id=None, matched_chunk_table="message_chunks",
    )


def test_hit_fields_are_exactly_the_hit_keys_plus_snippet() -> None:
    # One authority: a key added to the hit later cannot go missing from the
    # projection, and no name is permitted that the hit never carries.
    assert set(HIT_FIELDS) == set(_to_api_result(_result())) | {"snippet"}
    assert len(HIT_FIELDS) == len(set(HIT_FIELDS))


def test_hit_fields_order_is_the_documented_one() -> None:
    assert HIT_FIELDS == (
        "message_id", "account", "folder", "subject", "from", "to", "date",
        "snippet_html", "has_attachments", "score", "matched_arms", "snippet",
    )


@pytest.mark.parametrize("name", HIT_FIELDS)
def test_every_documented_name_is_accepted(name: str) -> None:
    assert fields_error([name]) is None


def test_an_unknown_name_is_refused_by_name_with_the_supported_set() -> None:
    msg = fields_error(["message_id", "snippet_text"])
    assert msg is not None
    assert "'snippet_text'" in msg
    assert "message_id" in msg and "matched_arms" in msg  # the supported set


def test_several_unknown_names_are_all_named() -> None:
    msg = fields_error(["bogus", "nope"])
    assert msg is not None
    assert "'bogus'" in msg and "'nope'" in msg


def test_an_empty_list_is_refused() -> None:
    msg = fields_error([])
    assert msg is not None
    assert "empty" in msg


@pytest.mark.parametrize("bad", [[1], [None], ["message_id", 3.0]])
def test_a_non_string_element_is_refused(bad: list) -> None:
    msg = fields_error(bad)
    assert msg is not None
    assert "string" in msg


def test_a_non_list_is_refused() -> None:
    assert fields_error("message_id") is not None


def test_duplicates_are_accepted_and_collapse() -> None:
    assert fields_error(["score", "score"]) is None
    hit = _to_api_result(_result())
    assert project_hit(hit, ["score", "score"]) == {"score": 0.5}


def test_projection_returns_exactly_the_named_keys_in_canonical_order() -> None:
    hit = _to_api_result(_result())
    out = project_hit(hit, ["snippet", "message_id"])
    assert list(out) == ["message_id", "snippet"]
    assert out == {"message_id": "7", "snippet": "the snippet"}


def test_snippet_is_the_same_text_as_snippet_html() -> None:
    hit = _to_api_result(_result())
    out = project_hit(hit, ["snippet", "snippet_html"])
    assert out["snippet"] == out["snippet_html"] == "the snippet"


def test_projection_does_not_mutate_the_hit() -> None:
    hit = _to_api_result(_result())
    before = dict(hit)
    project_hit(hit, ["snippet"])
    assert hit == before
    assert "snippet" not in hit


def test_an_unvalidated_name_raises_rather_than_dropping() -> None:
    with pytest.raises(KeyError):
        project_hit(_to_api_result(_result()), ["bogus"])


@pytest.mark.parametrize("value", [1, 500, 1000])
def test_snippet_chars_in_range_is_accepted(value: int) -> None:
    assert snippet_chars_error(value, max_chars=1000) is None


@pytest.mark.parametrize("value", [0, -1, 1001])
def test_snippet_chars_out_of_range_names_the_range(value: int) -> None:
    msg = snippet_chars_error(value, max_chars=1000)
    assert msg is not None
    assert "1" in msg and "1000" in msg


@pytest.mark.parametrize("value", [True, False, 1.5, "10", None])
def test_snippet_chars_that_is_not_an_int_is_refused(value: object) -> None:
    # bool is an int subclass; True would otherwise read as 1.
    assert snippet_chars_error(value, max_chars=1000) is not None
```

- [ ] **Step 2: Run to verify failure**

Run: `unset VIRTUAL_ENV && uv run pytest -q tests/test_search_projection.py`
Expected: collection error, `ModuleNotFoundError: localmail.api.search_projection`.

- [ ] **Step 3: Implement the module**

```python
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Compact search hits: the `fields` projection and `snippet_chars` (slice E).

Pure — no IO. The one authority for which hit keys a caller may name and for
the range a caller may ask a snippet to span. Each rule returns a message or
``None`` (the ``account_names.account_name_error`` shape); the caller decides
what an error *is* (``run_search`` raises ``ValidationFailed``, a 400).

Spec: docs/superpowers/specs/2026-09-19-search-hit-projection-design.md
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any

#: Every name a `fields` projection may ask for, in the order the projected
#: hit's keys come back. The first eleven are exactly the keys
#: ``api.search._to_api_result`` emits (pinned by a test, so a hit key added
#: later cannot go missing here); ``snippet`` is the honest, plain-text name
#: for ``snippet_html``, which has never held HTML. It is emitted only when
#: named, so the default response stays byte-identical.
HIT_FIELDS: tuple[str, ...] = (
    "message_id", "account", "folder", "subject", "from", "to", "date",
    "snippet_html", "has_attachments", "score", "matched_arms", "snippet",
)

_SNIPPET_ALIAS_OF = "snippet_html"


def fields_error(fields: object) -> str | None:
    """Why ``fields`` cannot be honoured, or ``None`` when it can.

    Refused: a non-list, an empty list (hits with no keys is a caller bug,
    not a request), a non-string element, and any name outside
    ``HIT_FIELDS`` — all of them named, so the caller can fix the call.
    Duplicates are accepted: the projection is a mapping, so they collapse
    without losing anything.
    """
    if not isinstance(fields, list):
        return "fields must be a list of hit field names"
    if not fields:
        return ("fields must not be empty; omit it for the full hit, or name "
                f"at least one of: {', '.join(HIT_FIELDS)}")
    if any(not isinstance(name, str) for name in fields):
        return "fields must contain only strings"
    unknown = sorted({name for name in fields if name not in HIT_FIELDS})
    if not unknown:
        return None
    noun = "field" if len(unknown) == 1 else "fields"
    return (f"unknown hit {noun} {', '.join(repr(n) for n in unknown)}; "
            f"supported: {', '.join(HIT_FIELDS)}")


def snippet_chars_error(value: object, *, max_chars: int) -> str | None:
    """Why ``value`` cannot size a snippet window, or ``None`` when it can.

    ``bool`` is refused explicitly: it is an ``int`` subclass, so ``True``
    would otherwise read as a one-character window. Out of range is refused
    rather than clamped — a silently clamped value is an answer to a
    question the caller did not ask.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return f"snippet_chars must be an integer (got {value!r})"
    if not 1 <= value <= max_chars:
        return f"snippet_chars must be between 1 and {max_chars} (got {value})"
    return None


def project_hit(hit: dict[str, Any], fields: Sequence[str]) -> dict[str, Any]:
    """Return a new hit carrying exactly ``fields``, in ``HIT_FIELDS`` order.

    ``fields`` must already have passed ``fields_error``. An unvalidated name
    raises ``KeyError`` here rather than being dropped — a loud bug at the
    one boundary that can still see it. ``hit`` is not mutated.
    """
    source = {**hit, "snippet": hit[_SNIPPET_ALIAS_OF]}
    wanted = set(fields)
    for name in wanted:
        if name not in HIT_FIELDS:
            raise KeyError(name)
    return {name: source[name] for name in HIT_FIELDS if name in wanted}
```

- [ ] **Step 4: Run to verify pass**

Run: `unset VIRTUAL_ENV && uv run pytest -q tests/test_search_projection.py`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/localmail/api/search_projection.py tests/test_search_projection.py
git commit -m "Pure rules for the search-hit projection and snippet width (slice E)

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: `SearchConfig.snippet_max_chars`

**Files:**
- Modify: `src/localmail/config.py`. Place it immediately after `snippet_width_chars: int = 200` (currently line 427) in `class SearchConfig`, and add a `model_validator` to `SearchConfig`.
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `SearchConfig.snippet_max_chars: int` (default 1000, `>= 1`, `>= snippet_width_chars`).

- [ ] **Step 1: Write the failing tests** (append to `tests/test_config.py`; `SearchConfig` is already imported there, and `pydantic.ValidationError` must be imported if it is not):

```python
def test_snippet_max_chars_defaults_to_1000() -> None:
    assert SearchConfig().snippet_max_chars == 1000


def test_snippet_max_chars_below_the_default_width_is_rejected() -> None:
    # Otherwise the default width would be a value the API refuses, and a
    # caller could not state it explicitly.
    with pytest.raises(ValidationError, match="snippet_max_chars"):
        SearchConfig(snippet_width_chars=200, snippet_max_chars=199)


def test_snippet_max_chars_equal_to_the_width_is_accepted() -> None:
    assert SearchConfig(snippet_width_chars=300, snippet_max_chars=300).snippet_max_chars == 300


def test_snippet_max_chars_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        SearchConfig(snippet_width_chars=1, snippet_max_chars=0)
```

- [ ] **Step 2: Run to verify failure**

Run: `unset VIRTUAL_ENV && uv run pytest -q tests/test_config.py -k snippet_max`
Expected: FAIL (`AttributeError`/no validation error).

- [ ] **Step 3: Implement.** After `snippet_width_chars: int = 200`:

```python
    # Upper bound for a caller's `snippet_chars` on POST /v1/search (slice E).
    # An operator's resource bound for network callers, not a correctness
    # limit — `make_snippet` handles any positive width. Must be >= the
    # default width, or the default is a value the API refuses.
    snippet_max_chars: int = Field(default=1000, ge=1)
```

And add to `SearchConfig` (after its fields, before the next class):

```python
    @model_validator(mode="after")
    def _snippet_max_covers_the_default_width(self) -> SearchConfig:
        if self.snippet_max_chars < self.snippet_width_chars:
            raise ValueError(
                f"snippet_max_chars ({self.snippet_max_chars}) must be >= "
                f"snippet_width_chars ({self.snippet_width_chars})")
        return self
```

(`Field` and `model_validator` are already imported in `config.py`. Check that `SearchConfig` has no existing `model_validator`: `grep -n "model_validator" src/localmail/config.py`. The two existing ones belong to `Config`.)

- [ ] **Step 4: Run to verify pass**

Run: `unset VIRTUAL_ENV && uv run pytest -q tests/test_config.py`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/localmail/config.py tests/test_config.py
git commit -m "search.snippet_max_chars caps a caller's snippet width (slice E)

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Thread a per-call snippet width through the Searcher

**Files:**
- Modify: `src/localmail/search/searcher.py`:
  - `_build_results` (≈ line 983), and its `make_snippet(..., width=self._cfg.snippet_width_chars)` call (≈ line 1045);
  - `continue_page` (≈ line 1095);
  - `grow_pool` (≈ line 1149);
  - `_search_with_parsed` (≈ line 1172);
  - `search` (≈ line 1248, and its two `_build_results` calls ≈ line 1686).
- Modify (callers of `_build_results` that pass positionally):
  - `tests/test_rerank_nonfinite_scores.py:232`
  - `tests/test_search_has_attachments_flag.py:210`
  - `tests/test_search_relevance_ties.py` (lines 123, 132, 145, 154, 163, 166)
- Test: `tests/test_searcher_snippet_width.py` (new)

**Interfaces:**
- Consumes: `SearchConfig.snippet_width_chars` (existing).
- Produces:
  - `Searcher.search(..., snippet_chars: int | None = None)`
  - `Searcher.continue_page(token, page, *, user_id=None, snippet_chars: int | None = None)`
  - `Searcher.grow_pool(token, cpa, *, user_id=None, snippet_chars: int | None = None)`
  - `None` means `cfg.snippet_width_chars`. `< 1` or a `bool` raises `ValueError` before any IO.
  - `_build_results(..., *, snippet_width: int)` is required and keyword-only.

Design note: `_build_results` takes `snippet_width` as a **required** keyword. Defaulting it would let a call site forget it and silently serve the default width to a caller who asked for another, with nothing failing. That is the #234 footgun. The public entry points default to `None` because a safe value exists and CLI/library callers must keep working.

- [ ] **Step 1: Write the failing tests** (`tests/test_searcher_snippet_width.py`):

```python
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""A caller-sized snippet window, honoured per page (kastellan slice E).

Snippets are built per page from the full cached source text, so a width can
widen as well as shrink and a continuation page takes its own.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import psycopg
import pytest

from localmail.config import SearchConfig
from localmail.db import open_pool
from localmail.search.embed_worker import run_embed_worker_once
from localmail.search.searcher import Searcher

# Filler words carry no query term, so the window is centred on the one
# "zebra" and every width up to the chunk length is fully used.
_FILLER = " ".join(f"word{i}" for i in range(400))  # ~3 KB


class _E:
    """Chunks containing "zebra" point at the query; every other chunk points
    away. A constant embedder ties every chunk in the vector arm, and the
    tie-break (#360) could then pick a header or a zebra-less body chunk as
    the snippet source."""
    name = "s"; model = "s"; dimension = 768
    def embed_documents(self, t):
        return [[1.0 if "zebra" in x else -1.0] * 768 for x in t]
    def embed_query(self, t): return [0.5] * 768
    def health_check(self): pass


def _seed(conn: psycopg.Connection, n: int = 2) -> None:
    """`n` messages whose ONLY "zebra" is mid-body, so the body chunk (long)
    is the snippet source rather than the header chunk (short)."""
    with conn.cursor() as cur:
        cur.execute("INSERT INTO accounts (name,email_address,imap_host,auth_method)"
                    " VALUES ('a','a@x','h','password') RETURNING id")
        row = cur.fetchone()
        assert row is not None
        acct = row[0]
        for i in range(n):
            cur.execute(
                "INSERT INTO messages (account_id, message_id, raw_sha256, subject,"
                " body_text, headers, raw_bytes, size_bytes)"
                " VALUES (%s, %s, %s, %s, %s, '{}'::jsonb, 'r', 1)",
                (acct, f"<m{i}>", bytes([i + 1]) * 32, f"Note {i}",
                 f"{_FILLER} zebra {_FILLER}"),
            )
    conn.commit()


@pytest.fixture
def searcher(db_dsn, db_conn):
    _seed(db_conn)
    cfg = SearchConfig(page_size_default=1)
    run_embed_worker_once(db_conn, cfg, _E())
    pool = open_pool(db_dsn)
    try:
        yield Searcher(pool=pool, cfg=cfg, embeddings=_E(), reranker=None,
                       rewriter=None)
    finally:
        pool.close()


def _snippet(page) -> str:
    [hit] = page.results
    assert hit.snippet_source == "body", "fixture must select the long body chunk"
    return hit.snippet


def test_omitted_is_byte_identical_to_the_configured_width(searcher) -> None:
    default = _snippet(searcher.search("zebra", allowed_account_ids=None))
    explicit = _snippet(searcher.search("zebra", allowed_account_ids=None,
                                        snippet_chars=200))
    assert default == explicit
    assert 150 < len(default) <= 202


def test_a_wider_window_is_honoured(searcher) -> None:
    snip = _snippet(searcher.search("zebra", allowed_account_ids=None,
                                    snippet_chars=500))
    assert len(snip) > 400
    assert "zebra" in snip


def test_a_narrower_window_is_honoured(searcher) -> None:
    snip = _snippet(searcher.search("zebra", allowed_account_ids=None,
                                    snippet_chars=50))
    assert len(snip) <= 52  # window + at most two ellipsis marks
    assert "zebra" in snip


def test_a_continuation_page_takes_its_own_width(searcher) -> None:
    p1 = searcher.search("zebra", allowed_account_ids=None, snippet_chars=500)
    p2 = searcher.continue_page(p1.search_token, page=2, snippet_chars=60)
    assert len(_snippet(p1)) > 400
    assert len(_snippet(p2)) <= 62


def test_continue_page_without_a_width_uses_the_default(searcher) -> None:
    p1 = searcher.search("zebra", allowed_account_ids=None, snippet_chars=500)
    p2 = searcher.continue_page(p1.search_token, page=2)
    assert 150 < len(_snippet(p2)) <= 202


def test_grow_pool_takes_its_own_width(searcher) -> None:
    p1 = searcher.search("zebra", allowed_account_ids=None)
    grown = searcher.grow_pool(p1.search_token, candidates_per_arm=100,
                               snippet_chars=40)
    assert len(_snippet(grown)) <= 42


@pytest.mark.parametrize("bad", [0, -5, True, False])
def test_a_non_positive_width_is_refused_before_any_io(bad) -> None:
    pool = MagicMock()
    pool.connection.side_effect = AssertionError("touched the pool")
    s = Searcher(pool=pool, cfg=SearchConfig(), embeddings=None, reranker=None,
                 rewriter=None)
    with pytest.raises(ValueError, match="snippet_chars"):
        s.search("zebra", allowed_account_ids=None, snippet_chars=bad)
    pool.connection.assert_not_called()


@pytest.mark.parametrize("method", ["continue_page", "grow_pool"])
def test_continuations_refuse_a_non_positive_width_too(method) -> None:
    s = Searcher(pool=MagicMock(), cfg=SearchConfig(), embeddings=None,
                 reranker=None, rewriter=None)
    with pytest.raises(ValueError, match="snippet_chars"):
        getattr(s, method)("tok", 2, snippet_chars=0)
```

- [ ] **Step 2: Run to verify failure**

Run: `unset VIRTUAL_ENV && uv run pytest -q tests/test_searcher_snippet_width.py`
Expected: FAIL (`TypeError: unexpected keyword argument 'snippet_chars'`).

- [ ] **Step 3: Implement.** In `searcher.py`:

  1. Add a method on `Searcher` (next to `_build_results`):

```python
    def _snippet_width(self, snippet_chars: int | None) -> int:
        """Resolve a caller's snippet width; ``None`` is the configured default.

        Positivity only — the upper cap is an operator's bound for *network*
        callers and lives at the api boundary (``search_projection``). A
        ``bool`` is refused because it is an ``int`` subclass. Raised before
        any IO so a library caller's bug is loud and costs nothing.
        """
        if snippet_chars is None:
            return self._cfg.snippet_width_chars
        if isinstance(snippet_chars, bool) or snippet_chars < 1:
            raise ValueError(
                f"snippet_chars must be a positive integer (got {snippet_chars!r})")
        return snippet_chars
```

  2. `_build_results`: add a required keyword-only parameter `snippet_width: int` after `sort`. The signature becomes `(..., conn=None, sort: SortMode = DEFAULT_SORT, *, snippet_width: int)`. Replace `width=self._cfg.snippet_width_chars` with `width=snippet_width`.
  3. `continue_page`: add `snippet_chars: int | None = None` to the keyword-only parameters. As the **first** statement, before the cache read, add `width = self._snippet_width(snippet_chars)`. Pass `snippet_width=width` to both `_build_results` calls.
  4. `grow_pool`: add `snippet_chars: int | None = None` to the keyword-only parameters. The first statement is `width = self._snippet_width(snippet_chars)`. Pass `snippet_width=width` into `_search_with_parsed`.
  5. `_search_with_parsed`: add a required keyword-only `snippet_width: int` (the same no-default rationale as its `sort`/`sort_order`, which are already written that way). Pass `snippet_width=snippet_width` to its two `_build_results` calls.
  6. `search`: add `snippet_chars: int | None = None` after `keyset_cursor`. The first statement after `t0 = time.monotonic()` is `snippet_width = self._snippet_width(snippet_chars)`. Pass `snippet_width=snippet_width` to its two `_build_results` calls (≈ line 1686). The date-keyset branch builds no snippets (`snippet=""`), so it needs nothing. Add one sentence to the `search` docstring: "`snippet_chars` sizes the snippet window for this page (``None`` = `snippet_width_chars`); the date walk emits no snippet, so it has no effect there."
  7. Update every existing test caller of `_build_results` listed under **Files** to pass `snippet_width=200` as a keyword (append it to each call's arguments).

  Verify no call site was missed: `grep -n "_build_results(" src/localmail/search/searcher.py tests/*.py`. Each call must now carry `snippet_width=`.

- [ ] **Step 4: Run to verify pass**

Run: `unset VIRTUAL_ENV && uv run pytest -q tests/test_searcher_snippet_width.py tests/test_rerank_nonfinite_scores.py tests/test_search_has_attachments_flag.py tests/test_search_relevance_ties.py tests/test_searcher_pagination.py tests/test_searcher.py`
Expected: all pass.

If `test_omitted_is_byte_identical…` shows `snippet_source != "body"`, the fixture is selecting the header chunk. Do not weaken the assertion; fix the fixture so the body is the only place the term appears.

- [ ] **Step 5: Commit**

```bash
git add src/localmail/search/searcher.py tests/test_searcher_snippet_width.py tests/test_rerank_nonfinite_scores.py tests/test_search_has_attachments_flag.py tests/test_search_relevance_ties.py
git commit -m "Searcher sizes the snippet window per call and per page (slice E)

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: `run_search`, the route, and `api_minor` 3

**Files:**
- Modify: `src/localmail/api/search.py`: the `run_search` signature (≈ line 251), the gate placement right after `sort_membership_error` (≈ line 331), the fresh / keyset / pool calls, `_continue_or_grow`, and the final `results` list.
- Modify: `src/localmail/serve/routes/search.py`: `SearchRequest` and the `run_search(...)` call.
- Modify: `src/localmail/serve/routes/version.py`: `API_MINOR = 3`, plus a `# 3:` comment line in the history block above it.
- Test: `tests/test_serve_search_projection.py` (new), `tests/test_serve_version_route.py` (append).

**Interfaces:**
- Consumes: Task 1's `HIT_FIELDS`, `fields_error`, `snippet_chars_error`, `project_hit`; Task 2's `SearchConfig.snippet_max_chars`; Task 3's `snippet_chars=` on `search` / `continue_page` / `grow_pool`.
- Produces: `run_search(..., fields: list[str] | None = None, snippet_chars: int | None = None)`, plus the wire fields `fields` and `snippet_chars` on `POST /v1/search`.

- [ ] **Step 1: Write the failing tests** (`tests/test_serve_search_projection.py`):

```python
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""`fields` and `snippet_chars` on POST /v1/search, through the real transport.

A real `Searcher` over a seeded archive, never a MagicMock hit: an unset
MagicMock attribute serialises to `{}` rather than failing, so a mocked hit
proves nothing about which keys reach the wire.
"""
from __future__ import annotations

import psycopg
import pytest
from fastapi.testclient import TestClient

from localmail.api.search import run_search
from localmail.api.errors import ValidationFailed
from localmail.config import SearchConfig
from localmail.db import open_pool
from localmail.search.embed_worker import run_embed_worker_once
from localmail.search.searcher import Searcher
from localmail.serve.app import create_app

_FILLER = " ".join(f"word{i}" for i in range(400))
_DEFAULT_HIT_KEYS = {
    "message_id", "account", "folder", "subject", "from", "to", "date",
    "snippet_html", "has_attachments", "score", "matched_arms",
}
_ENVELOPE = {"results", "next_cursor", "total_estimate", "took_ms",
             "sort_applied", "rankable", "rewrite_skipped", "rewrite_status",
             "rewrite_note", "rewrite_note_code"}


class _E:
    """Chunks containing "zebra" point at the query; every other chunk points
    away. A constant embedder ties every chunk in the vector arm, and the
    tie-break (#360) could then pick a header or a zebra-less body chunk as
    the snippet source."""
    name = "s"; model = "s"; dimension = 768
    def embed_documents(self, t):
        return [[1.0 if "zebra" in x else -1.0] * 768 for x in t]
    def embed_query(self, t): return [0.5] * 768
    def health_check(self): pass


def _seed(conn: psycopg.Connection, n: int = 3) -> int:
    with conn.cursor() as cur:
        cur.execute("INSERT INTO accounts (name,email_address,imap_host,auth_method)"
                    " VALUES ('a','a@x','h','password') RETURNING id")
        row = cur.fetchone()
        assert row is not None
        acct = int(row[0])
        for i in range(n):
            cur.execute(
                "INSERT INTO messages (account_id, message_id, raw_sha256, subject,"
                " body_text, headers, raw_bytes, size_bytes)"
                " VALUES (%s, %s, %s, %s, %s, '{}'::jsonb, 'r', 1)",
                (acct, f"<m{i}>", bytes([i + 1]) * 32, f"Note {i}",
                 f"{_FILLER} zebra {_FILLER}"),
            )
    conn.commit()
    return acct


@pytest.fixture
def archive(db_dsn, db_conn):
    acct = _seed(db_conn)
    cfg = SearchConfig(snippet_max_chars=1000)
    run_embed_worker_once(db_conn, cfg, _E())
    pool = open_pool(db_dsn)
    try:
        yield acct, Searcher(pool=pool, cfg=cfg, embeddings=_E(), reranker=None,
                             rewriter=None)
    finally:
        pool.close()


def _post(db_dsn, searcher, token, body):
    client = TestClient(create_app(db_dsn=db_dsn, searcher=searcher))
    return client.post("/v1/search", json=body,
                       headers={"Authorization": f"Bearer {token}"})


def _grant(db_conn, user_id: int, acct: int) -> None:
    with db_conn.cursor() as cur:
        cur.execute("INSERT INTO user_accounts (user_id, account_id) VALUES (%s, %s)",
                    (user_id, acct))
    db_conn.commit()


def test_the_default_response_is_unchanged(archive, db_dsn, db_conn, api_user, api_token):
    acct, searcher = archive
    _grant(db_conn, api_user.id, acct)
    r = _post(db_dsn, searcher, api_token, {"query": "zebra"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == _ENVELOPE
    assert body["results"], "the seeded archive must match, or this proves nothing"
    for hit in body["results"]:
        assert set(hit) == _DEFAULT_HIT_KEYS
        assert 150 < len(hit["snippet_html"]) <= 202


def test_fields_returns_exactly_the_named_keys(archive, db_dsn, db_conn, api_user, api_token):
    acct, searcher = archive
    _grant(db_conn, api_user.id, acct)
    r = _post(db_dsn, searcher, api_token,
              {"query": "zebra", "fields": ["snippet", "message_id"]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == _ENVELOPE  # the envelope is never projected
    assert body["results"]
    for hit in body["results"]:
        assert list(hit) == ["message_id", "snippet"]
        assert "zebra" in hit["snippet"]


def test_snippet_chars_widens_on_the_wire(archive, db_dsn, db_conn, api_user, api_token):
    acct, searcher = archive
    _grant(db_conn, api_user.id, acct)
    r = _post(db_dsn, searcher, api_token,
              {"query": "zebra", "fields": ["snippet"], "snippet_chars": 800})
    assert r.status_code == 200, r.text
    assert all(len(h["snippet"]) > 600 for h in r.json()["results"])


def test_a_pool_continuation_is_projected_and_sized(archive, db_dsn, db_conn, api_user, api_token):
    acct, searcher = archive
    _grant(db_conn, api_user.id, acct)
    first = _post(db_dsn, searcher, api_token,
                  {"query": "zebra", "limit": 1, "fields": ["message_id"]}).json()
    assert first["next_cursor"], "need a second page"
    r = _post(db_dsn, searcher, api_token,
              {"query": "zebra", "limit": 1, "cursor": first["next_cursor"],
               "fields": ["snippet"], "snippet_chars": 30})
    assert r.status_code == 200, r.text
    [hit] = r.json()["results"]
    assert list(hit) == ["snippet"] and len(hit["snippet"]) <= 32


def test_a_keyset_continuation_is_projected(archive, db_dsn, db_conn, api_user, api_token):
    acct, searcher = archive
    _grant(db_conn, api_user.id, acct)
    first = _post(db_dsn, searcher, api_token,
                  {"query": "", "limit": 1, "fields": ["message_id"]}).json()
    assert first["next_cursor"], "need a second page"
    r = _post(db_dsn, searcher, api_token,
              {"query": "", "limit": 1, "cursor": first["next_cursor"],
               "fields": ["message_id", "date"]})
    assert r.status_code == 200, r.text
    [hit] = r.json()["results"]
    assert list(hit) == ["message_id", "date"]


_REFUSALS = [
    ({"fields": ["bogus"]}, "'bogus'"),
    ({"fields": []}, "empty"),
    ({"snippet_chars": 0}, "snippet_chars"),
    ({"snippet_chars": 1001}, "1000"),
    ({"snippet_chars": True}, "snippet_chars"),
]


@pytest.mark.parametrize(("extra", "needle"), _REFUSALS)
def test_a_bad_argument_is_a_problem_json_400(
    archive, db_dsn, db_conn, api_user, api_token, extra, needle,
):
    acct, searcher = archive
    _grant(db_conn, api_user.id, acct)
    r = _post(db_dsn, searcher, api_token, {"query": "zebra", **extra})
    assert r.status_code == 400, r.text
    assert r.headers["content-type"].startswith("application/problem+json")
    assert needle in r.json()["detail"]


@pytest.mark.parametrize(("extra", "needle"), _REFUSALS)
def test_a_bad_argument_is_refused_even_for_a_caller_granted_nothing(
    archive, db_dsn, api_token, extra, needle,
):
    # No grant: the empty-ACL short-circuit would answer 200 with an empty
    # page, byte-identical to "no results", if the gate sat after it.
    _, searcher = archive
    r = _post(db_dsn, searcher, api_token, {"query": "zebra", **extra})
    assert r.status_code == 400, r.text
    assert needle in r.json()["detail"]


def test_run_search_refuses_before_touching_the_searcher() -> None:
    from unittest.mock import MagicMock

    searcher = MagicMock()
    searcher.config = SearchConfig()
    with pytest.raises(ValidationFailed):
        run_search(searcher=searcher, free_text="x", filters={}, limit=5,
                   allowed_account_ids=[1], user_id=1, fields=["bogus"])
    searcher.search.assert_not_called()
```

Append to `tests/test_serve_version_route.py`:

```python
def test_api_minor_announces_the_search_hit_projection(db_dsn: str) -> None:
    """An old server refuses `fields`/`snippet_chars` with a 400 (#364), so a
    client could discover the feature by failing; the version lets it ask
    first."""
    body = _client(db_dsn).get("/v1/version").json()
    assert body["api_minor"] >= 3
```

- [ ] **Step 2: Run to verify failure**

Run: `unset VIRTUAL_ENV && uv run pytest -q tests/test_serve_search_projection.py tests/test_serve_version_route.py`
Expected: FAIL (`unknown field 'fields'` 400s where 200 is expected; `run_search` has no `fields` keyword; `api_minor` is 2).

- [ ] **Step 3a: Implement `run_search`** (`api/search.py`):
  - Import: `from localmail.api.search_projection import fields_error, project_hit, snippet_chars_error`.
  - Signature: add `fields: list[str] | None = None, snippet_chars: int | None = None` after `smart: bool = False`.
  - Docstring: add a paragraph.

    ```
    ``fields`` projects each hit to exactly the named keys (the envelope is
    never projected); ``snippet_chars`` sizes the snippet window, between 1
    and ``search.snippet_max_chars``. Both are refused (400) before the
    empty-ACL short-circuit, for the reason every stated-argument gate is.
    See ``search_projection`` and
    docs/superpowers/specs/2026-09-19-search-hit-projection-design.md.
    ```

  - Immediately after the `membership_error` block (`if membership_error is not None: raise ValidationFailed(membership_error)`), insert:

    ```python
    # Slice E's two arguments, ahead of the empty-ACL short-circuit for the
    # reason the membership gate above is: that branch's empty page reads as
    # "no results", so a malformed request from a grant-nothing caller would
    # be reported as a completed one.
    if fields is not None and (error := fields_error(fields)) is not None:
        raise ValidationFailed(error)
    if snippet_chars is not None and (error := snippet_chars_error(
            snippet_chars, max_chars=searcher.config.snippet_max_chars)) is not None:
        raise ValidationFailed(error)
    ```

  - Fresh branch `searcher.search(...)`: add `snippet_chars=snippet_chars`.
  - Keyset branch `searcher.search(...)`: add `snippet_chars=snippet_chars`. The date walk builds no snippet, but forwarding keeps the Searcher's positivity rule in force on every branch.
  - Pool branch: `page = _continue_or_grow(searcher, parsed, user_id=user_id, cfg=cfg, snippet_chars=snippet_chars)`. Give `_continue_or_grow` a keyword-only `snippet_chars: int | None` (no default: its single caller must state it), and pass it to both `searcher.continue_page(...)` and `searcher.grow_pool(...)`.
  - Final return: replace `"results": [_to_api_result(r) for r in page.results],` with:

    ```python
        "results": [
            _to_api_result(r) if fields is None
            else project_hit(_to_api_result(r), fields)
            for r in page.results
        ],
    ```

  - `grep -n "searcher.config" src/localmail/api/search.py` shows `cfg = searcher.config` is bound **after** the empty-ACL branch. That is why the gate reads `searcher.config.snippet_max_chars` directly.
- [ ] **Step 3b: Implement the route** (`serve/routes/search.py`), adding to `SearchRequest` after `smart`:

    ```python
    # Slice E: project each hit to exactly these keys (`snippet` is the
    # plain-text name for `snippet_html`), and size the snippet window.
    # Deliberately no `min_length`/`ge`/`le`: pydantic would answer 422 with
    # an array `detail` (#370), and `run_search`'s pure rules answer a
    # problem+json 400 that names the problem. A non-list `fields` is a type
    # error on a known field and stays the 422 that #370 tracks.
    fields: list[str] | None = None
    snippet_chars: int | None = None
    ```

    In the `run_search(...)` call add `fields=req.fields, snippet_chars=req.snippet_chars,`.

    **Check pydantic's lax coercion of `true` to an `int` field.** Pydantic v2 lax mode rejects `bool` for `int`, answering 422. The `_REFUSALS` case `{"snippet_chars": True}` expects **400**. If the route answers 422, declare the field `snippet_chars: int | bool | None = None` so the bool reaches `snippet_chars_error`. Add a comment saying why: the pure rule is the one authority, and it names the problem. Then re-run.

- [ ] **Step 3c: Bump the version** (`serve/routes/version.py`). Add to the history comment above `API_MINOR`:

    ```python
    # 3: `fields` and `snippet_chars` on POST /v1/search (kastellan slice E).
    # An older server refuses both with a 400 naming the unknown field (#364),
    # so this is the ask-first half rather than a silent one.
    ```

    Set `API_MINOR = 3`.

- [ ] **Step 4: Run to verify pass**

Run: `unset VIRTUAL_ENV && uv run pytest -q tests/test_serve_search_projection.py tests/test_serve_version_route.py tests/test_serve_search_route.py tests/test_serve_search_request_keys.py tests/test_mcp_tools.py tests/test_api_search.py`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/localmail/api/search.py src/localmail/serve/routes/search.py src/localmail/serve/routes/version.py tests/test_serve_search_projection.py tests/test_serve_version_route.py
git commit -m "POST /v1/search takes fields and snippet_chars; api_minor 3 (slice E)

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: Docs, and the full gate

**Files:**
- Modify: `README.md`, in the `POST /v1/search` section (`grep -n "POST /v1/search" README.md`; the cursor-flavours paragraph is ≈ line 910). Add a short subsection after it.
- Modify: `CLAUDE.md`, in two places:
  - **Layout**: add a line under `src/localmail/` for `api/search_projection.py`. If there is no `api/` sub-listing, put it next to the `search/` entries as `api/search_projection.py # pure: HIT_FIELDS / fields_error / snippet_chars_error / project_hit (slice E)`.
  - **GUI server**: add a bullet after the "Attachments are addressable by position (kastellan slice D)" bullet.

- [ ] **Step 1: README subsection** (after the cursor-flavours paragraph):

```markdown
#### Compact hits: `fields` and `snippet_chars` (`api_minor` ≥ 3)

`POST /v1/search` accepts two optional fields:

- `fields`: a list of hit keys to return. Each hit then carries **exactly**
  those keys, in a fixed order. The permitted names are the default hit keys
  plus `snippet`, the plain-text snippet. (`snippet_html` is the same text;
  it has never held HTML.) The envelope (`next_cursor`, `sort_applied`,
  `rankable`, `rewrite_*`, …) is never projected.
- `snippet_chars`: the width of the snippet window, between 1 and
  `[search] snippet_max_chars` (default 1000). A cut window may gain a `…`
  at either end, so the text is at most `snippet_chars + 2` characters.
  Each page, continuations included, takes the width stated on its own
  request.

An unknown or empty `fields`, or an out-of-range or non-integer
`snippet_chars`, is a 400 problem+json. That holds for a caller granted no
accounts too. Omit both for today's response, unchanged.

A search with no free text (filters only, or `sort=date`) takes the date walk,
which returns an empty snippet. There `snippet_chars` has nothing to size.

Example, the smallest useful hit for an agent:
`{"query": "invoice", "fields": ["message_id", "subject", "date", "snippet"], "snippet_chars": 120}`.
```

- [ ] **Step 2: CLAUDE.md bullet** (GUI server section, after the slice D bullet):

```markdown
- **Search hits can be projected and their snippet sized (kastellan slice E).**
  Design:
  [docs/superpowers/specs/2026-09-19-search-hit-projection-design.md](docs/superpowers/specs/2026-09-19-search-hit-projection-design.md).
  `POST /v1/search` takes `fields` (each hit gets exactly the named keys) and
  `snippet_chars` (window width, `1..search.snippet_max_chars`, default cap
  1000). `api_minor` is **3**. The rules are the pure
  [src/localmail/api/search_projection.py](src/localmail/api/search_projection.py).
  - **`HIT_FIELDS` is pinned equal to `_to_api_result`'s keys plus `snippet`.**
    A hit key added later cannot go missing from the projection, and no name
    can be permitted that the hit never carries.
  - **`snippet` is selectable only.** The default response stays
    byte-identical, which is why `snippet_html` keeps its misleading name.
  - **The width is per page, not per pool.** `_build_results` builds snippets
    from the full cached source text, so a continuation takes its own width
    with no re-retrieval. `_build_results`' `snippet_width` is keyword-only
    **with no default**: a call site that forgot it would silently serve the
    configured width to a caller who asked for another.
  - **The cap lives at the api boundary; the Searcher checks positivity
    only.** The cap is an operator's bound for network callers, and
    `make_snippet` is correct for any positive width.
  - **No `ge`/`le` on the pydantic model.** That would answer 422 with an
    array `detail` (#370); the pure rule answers a problem+json 400 naming
    the range.
  - **Both are gated ahead of the empty-ACL short-circuit** (#348's rule), and
    pinned from a grant-nothing caller.
  - **The date walk emits no snippet** (`_date_keyset_search`), so
    `snippet_chars` sizes nothing there. Giving it snippets is out of scope.
```

- [ ] **Step 3: Full gate**

Run each of these in the foreground, one at a time:

```bash
unset VIRTUAL_ENV && uv run pytest -q          # expect main's count + this slice's new tests, 0 failed
unset VIRTUAL_ENV && uv run mypy src/localmail # Success
unset VIRTUAL_ENV && uv run ruff check src/localmail/ | tail -1   # 10 (the #285 baseline), none in touched files
```

Reconcile the pytest count by hand: the previous full run's total plus the tests added in Tasks 1–4 must equal the new total. Exactly three LISTEN/NOTIFY failures with `could not access status of transaction` are cluster state (see CLAUDE.md Testing notes), not this change. Report them by name if seen.

- [ ] **Step 4: Commit**

```bash
git add README.md CLAUDE.md
git commit -m "Docs: fields projection and snippet_chars on /v1/search (slice E)

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```
