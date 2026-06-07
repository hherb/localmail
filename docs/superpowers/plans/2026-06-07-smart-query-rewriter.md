# Smart Query Rewriter (`--smart`) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the opt-in `--smart` LLM query rewriter (Search Phase 4): rewrite free text into a richer vector query, OR-in lexical expansion terms, and fill empty filter slots from natural language — with graceful, surfaced fall-through when the local LLM is unavailable.

**Architecture:** A new pure-plus-one-IO module `search/rewriter.py` holds the `QueryRewriter` protocol, a frozen `RewriteResult`, pure helpers (`build_rewrite_prompt`, `parse_rewrite_response`, `apply_rewrite`), and the only IO class `OllamaLLMRewriter` (httpx → Ollama `/api/generate`). The `Searcher` calls the rewriter once after `parse_query`, behind a timeout, falling through on any failure and surfacing `rewrite_skipped` on `SearchPage`. The lexical arms gain a shared `build_lexical_tsquery` helper that OR-s expansion terms and degrades to the exact current SQL when there are none.

**Tech Stack:** Python 3.12, psycopg v3, pydantic v2, httpx (already a dep), Ollama (external HTTP service), pytest with `httpx.MockTransport`.

**Spec:** [docs/superpowers/specs/2026-06-07-smart-query-rewriter-design.md](../specs/2026-06-07-smart-query-rewriter-design.md)

---

## File Structure

- **Create** `src/localmail/search/rewriter.py` — protocol, `RewriteResult`, `RewriteParseError`, pure helpers, `OllamaLLMRewriter`.
- **Create** `tests/test_rewriter.py` — unit tests for all pure helpers + `OllamaLLMRewriter` via `httpx.MockTransport`.
- **Modify** `src/localmail/config.py` — add `rewriter_max_expansion_terms`.
- **Modify** `src/localmail/search/arms.py` — add `build_lexical_tsquery`; wire it into `arm_bm25_messages` and `arm_bm25_chunks`.
- **Modify** `src/localmail/search/searcher.py` — call rewriter in `search()`; add `rewrite_skipped` to `SearchPage`.
- **Modify** `src/localmail/search/__init__.py` — build rewriter in `create_searcher`; export new names.
- **Modify** `src/localmail/cli.py` — print a notice when `page.rewrite_skipped`.
- **Modify** `tests/test_arms.py` (or wherever arm tests live) — expansion / identity tests.
- **Modify** `README.md` and `CLAUDE.md` — document `--smart`.

Pre-flight (run once before starting): `unset VIRTUAL_ENV && uv sync`.

---

## Task 1: Config field `rewriter_max_expansion_terms`

**Files:**
- Modify: `src/localmail/config.py:350-354`
- Test: `tests/test_config.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_config.py`:

```python
def test_rewriter_max_expansion_terms_default():
    from localmail.config import SearchConfig
    cfg = SearchConfig()
    assert cfg.rewriter_max_expansion_terms == 8
```

- [ ] **Step 2: Run test to verify it fails**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_config.py::test_rewriter_max_expansion_terms_default -v`
Expected: FAIL — `AttributeError`/no such field.

- [ ] **Step 3: Add the field**

In `src/localmail/config.py`, in the `# --- query rewriter (Phase 4) ---` block (after `rewriter_timeout_s: float = 10.0`):

```python
    rewriter_max_expansion_terms: int = 8
```

- [ ] **Step 4: Run test to verify it passes**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_config.py::test_rewriter_max_expansion_terms_default -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/localmail/config.py tests/test_config.py
git commit -m "feat(search): add rewriter_max_expansion_terms config field"
```

---

## Task 2: `RewriteResult`, protocol, `RewriteParseError`, `build_rewrite_prompt`

**Files:**
- Create: `src/localmail/search/rewriter.py`
- Create: `tests/test_rewriter.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_rewriter.py`:

```python
from datetime import date

from localmail.search.rewriter import build_rewrite_prompt


def test_prompt_includes_injected_today_and_free_text():
    prompt = build_rewrite_prompt(
        "tax return last summer", today=date(2026, 6, 7), max_expansion_terms=8
    )
    assert "2026-06-07" in prompt          # date grounding is deterministic
    assert "tax return last summer" in prompt
    assert "8" in prompt                    # expansion-term cap surfaced to the model


def test_prompt_is_deterministic():
    a = build_rewrite_prompt("x", today=date(2026, 1, 1), max_expansion_terms=5)
    b = build_rewrite_prompt("x", today=date(2026, 1, 1), max_expansion_terms=5)
    assert a == b
```

- [ ] **Step 2: Run test to verify it fails**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_rewriter.py -v`
Expected: FAIL — module `localmail.search.rewriter` does not exist.

- [ ] **Step 3: Create the module skeleton + prompt builder**

Create `src/localmail/search/rewriter.py`:

```python
"""LLM query rewriter for the opt-in ``--smart`` search path (Phase 4).

Pure helpers plus a single IO class (:class:`OllamaLLMRewriter`). The IO
class raises typed exceptions; the :class:`~localmail.search.searcher.Searcher`
owns the graceful-degradation policy so the failure behaviour is testable in
one place.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from typing import Any, Callable, Protocol

import httpx
from pydantic import BaseModel, Field, ValidationError

from localmail.config import SearchConfig
from localmail.search.query import ParsedQuery, SearchFilters


class RewriteParseError(ValueError):
    """The LLM response was not valid JSON / did not match the schema."""


@dataclass(frozen=True)
class RewriteResult:
    rewritten_text: str
    expansion_terms: list[str]
    extracted_filters: SearchFilters


class QueryRewriter(Protocol):
    name: str
    model: str

    def rewrite(self, free_text: str) -> RewriteResult: ...


_PROMPT_TEMPLATE = """\
You rewrite an email-search query into structured JSON. Today is {today}.

Return ONLY JSON with these keys:
- "rewritten_text": a cleaner, semantically richer restatement of the query
  for semantic (vector) search.
- "expansion_terms": up to {max_terms} synonyms or closely related terms that
  broaden a keyword search. Omit if none apply.
- "filters": object with optional keys "after" (YYYY-MM-DD inclusive lower
  bound), "before" (YYYY-MM-DD exclusive upper bound), "from", "to",
  "subject" (case-insensitive substrings), "has_attachment" (true/false).
  Resolve relative dates like "last summer" using today's date above. Use null
  for any filter you cannot infer. Never invent account, folder, or language
  filters.

Query: {query}
"""


def build_rewrite_prompt(
    free_text: str, *, today: date, max_expansion_terms: int
) -> str:
    """Render the deterministic rewrite prompt.

    ``today`` is injected (not read from the clock) so relative-date grounding
    is reproducible in tests.
    """
    return _PROMPT_TEMPLATE.format(
        today=today.isoformat(),
        max_terms=max_expansion_terms,
        query=free_text,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_rewriter.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add src/localmail/search/rewriter.py tests/test_rewriter.py
git commit -m "feat(search): rewriter module skeleton + deterministic prompt builder"
```

---

## Task 3: `parse_rewrite_response`

**Files:**
- Modify: `src/localmail/search/rewriter.py`
- Modify: `tests/test_rewriter.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_rewriter.py`:

```python
import pytest

from datetime import date as _date

from localmail.search.rewriter import RewriteParseError, parse_rewrite_response


def test_parse_full_response():
    raw = (
        '{"rewritten_text": "quarterly revenue report",'
        ' "expansion_terms": ["earnings", "Q3"],'
        ' "filters": {"after": "2025-06-01", "before": "2025-09-01",'
        ' "from": "bob", "to": null, "subject": null,'
        ' "has_attachment": true}}'
    )
    r = parse_rewrite_response(raw)
    assert r.rewritten_text == "quarterly revenue report"
    assert r.expansion_terms == ["earnings", "Q3"]
    assert r.extracted_filters.after == _date(2025, 6, 1)
    assert r.extracted_filters.before == _date(2025, 9, 1)
    assert r.extracted_filters.from_substr == "bob"
    assert r.extracted_filters.to_substr is None
    assert r.extracted_filters.has_attachment is True


def test_parse_minimal_response_defaults_empty():
    r = parse_rewrite_response('{"rewritten_text": "hello"}')
    assert r.rewritten_text == "hello"
    assert r.expansion_terms == []
    assert r.extracted_filters.after is None
    assert r.extracted_filters.has_attachment is None


def test_parse_invalid_json_raises():
    with pytest.raises(RewriteParseError):
        parse_rewrite_response("not json at all")


def test_parse_missing_required_field_raises():
    with pytest.raises(RewriteParseError):
        parse_rewrite_response('{"expansion_terms": []}')  # no rewritten_text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_rewriter.py -k parse -v`
Expected: FAIL — `parse_rewrite_response` undefined.

- [ ] **Step 3: Implement the parser + pydantic schema**

Append to `src/localmail/search/rewriter.py`:

```python
class _FiltersSchema(BaseModel):
    after: date | None = None
    before: date | None = None
    from_: str | None = Field(default=None, alias="from")
    to: str | None = None
    subject: str | None = None
    has_attachment: bool | None = None


class _RewriteSchema(BaseModel):
    rewritten_text: str
    expansion_terms: list[str] = Field(default_factory=list)
    filters: _FiltersSchema = Field(default_factory=_FiltersSchema)


def parse_rewrite_response(raw: str) -> RewriteResult:
    """Validate the LLM's JSON output into a :class:`RewriteResult`.

    Raises :class:`RewriteParseError` on invalid / malformed JSON so the
    caller can fall through to the un-rewritten query.
    """
    try:
        model = _RewriteSchema.model_validate_json(raw)
    except ValidationError as exc:
        raise RewriteParseError(str(exc)) from exc
    f = model.filters
    return RewriteResult(
        rewritten_text=model.rewritten_text,
        expansion_terms=list(model.expansion_terms),
        extracted_filters=SearchFilters(
            after=f.after,
            before=f.before,
            from_substr=f.from_,
            to_substr=f.to,
            subject_substr=f.subject,
            has_attachment=f.has_attachment,
        ),
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_rewriter.py -k parse -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add src/localmail/search/rewriter.py tests/test_rewriter.py
git commit -m "feat(search): parse_rewrite_response with pydantic schema validation"
```

---

## Task 4: `apply_rewrite` (pure precedence merge)

**Files:**
- Modify: `src/localmail/search/rewriter.py`
- Modify: `tests/test_rewriter.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_rewriter.py`:

```python
from localmail.search.query import ParsedQuery, SearchFilters
from localmail.search.rewriter import RewriteResult, apply_rewrite


def _result(**filter_kw):
    return RewriteResult(
        rewritten_text="rich query",
        expansion_terms=["a", "b", "c"],
        extracted_filters=SearchFilters(**filter_kw),
    )


def test_apply_sets_rewritten_text_and_expansion():
    parsed = ParsedQuery(free_text="orig")
    out = apply_rewrite(parsed, _result(), max_expansion_terms=8)
    assert out.free_text == "orig"                 # never mutated
    assert out.rewritten_text == "rich query"
    assert out.expansion_terms == ["a", "b", "c"]


def test_apply_caps_expansion_terms():
    parsed = ParsedQuery(free_text="orig")
    out = apply_rewrite(parsed, _result(), max_expansion_terms=2)
    assert out.expansion_terms == ["a", "b"]


def test_apply_fills_empty_filter_slot():
    from datetime import date
    parsed = ParsedQuery(free_text="orig")        # no after typed
    out = apply_rewrite(
        parsed, _result(after=date(2023, 6, 1)), max_expansion_terms=8
    )
    assert out.filters.after == date(2023, 6, 1)


def test_apply_preserves_explicit_operator():
    from datetime import date
    parsed = ParsedQuery(
        free_text="orig", filters=SearchFilters(after=date(2024, 1, 1))
    )
    out = apply_rewrite(
        parsed, _result(after=date(2023, 6, 1)), max_expansion_terms=8
    )
    assert out.filters.after == date(2024, 1, 1)   # explicit wins


def test_apply_llm_empty_filters_leave_user_filters_untouched():
    parsed = ParsedQuery(
        free_text="orig", filters=SearchFilters(subject_substr="invoice")
    )
    out = apply_rewrite(parsed, _result(), max_expansion_terms=8)
    assert out.filters.subject_substr == "invoice"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_rewriter.py -k apply -v`
Expected: FAIL — `apply_rewrite` undefined.

- [ ] **Step 3: Implement `apply_rewrite`**

Append to `src/localmail/search/rewriter.py`:

```python
def _fill(user_value: Any, llm_value: Any) -> Any:
    """Explicit operators win: keep the user's value unless it is unset."""
    return user_value if user_value is not None else llm_value


def apply_rewrite(
    parsed: ParsedQuery, result: RewriteResult, *, max_expansion_terms: int
) -> ParsedQuery:
    """Merge an LLM :class:`RewriteResult` into the parsed query.

    - ``rewritten_text`` and ``expansion_terms`` are added (``free_text`` is
      left untouched so lexical exact-recall is preserved).
    - Scalar filter slots are filled only when the user left them empty
      (explicit operators win). List slots (accounts/folders/languages) are
      never touched by the LLM.
    """
    uf = parsed.filters
    lf = result.extracted_filters
    merged = replace(
        uf,
        after=_fill(uf.after, lf.after),
        before=_fill(uf.before, lf.before),
        from_substr=_fill(uf.from_substr, lf.from_substr),
        to_substr=_fill(uf.to_substr, lf.to_substr),
        subject_substr=_fill(uf.subject_substr, lf.subject_substr),
        has_attachment=_fill(uf.has_attachment, lf.has_attachment),
        label=_fill(uf.label, lf.label),
    )
    return replace(
        parsed,
        rewritten_text=result.rewritten_text or None,
        expansion_terms=list(result.expansion_terms[:max_expansion_terms]),
        filters=merged,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_rewriter.py -k apply -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add src/localmail/search/rewriter.py tests/test_rewriter.py
git commit -m "feat(search): apply_rewrite precedence merge (explicit operators win)"
```

---

## Task 5: `OllamaLLMRewriter` (the only IO)

**Files:**
- Modify: `src/localmail/search/rewriter.py`
- Modify: `tests/test_rewriter.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_rewriter.py`:

```python
import httpx

from localmail.config import SearchConfig
from localmail.search.rewriter import OllamaLLMRewriter, RewriteParseError


def _rewriter_with_handler(handler, **cfg_over):
    cfg = SearchConfig(**cfg_over)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return OllamaLLMRewriter(cfg, client=client, today_provider=lambda: _date(2026, 6, 7))


def test_ollama_happy_path():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/generate"
        return httpx.Response(
            200,
            json={"response": '{"rewritten_text": "x", "expansion_terms": ["y"]}'},
        )

    r = _rewriter_with_handler(handler).rewrite("orig")
    assert r.rewritten_text == "x"
    assert r.expansion_terms == ["y"]


def test_ollama_4xx_raises_http_error():
    def handler(request):
        return httpx.Response(404, json={"error": "model not found"})

    with pytest.raises(httpx.HTTPStatusError):
        _rewriter_with_handler(handler).rewrite("orig")


def test_ollama_connect_error_propagates():
    def handler(request):
        raise httpx.ConnectError("refused")

    with pytest.raises(httpx.ConnectError):
        _rewriter_with_handler(handler).rewrite("orig")


def test_ollama_bad_inner_json_raises_parse_error():
    def handler(request):
        return httpx.Response(200, json={"response": "not json"})

    with pytest.raises(RewriteParseError):
        _rewriter_with_handler(handler).rewrite("orig")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_rewriter.py -k ollama -v`
Expected: FAIL — `OllamaLLMRewriter` undefined.

- [ ] **Step 3: Implement `OllamaLLMRewriter`**

Append to `src/localmail/search/rewriter.py`:

```python
_OLLAMA_FORMAT_SCHEMA = _RewriteSchema.model_json_schema()


class OllamaLLMRewriter:
    """Query rewriter backed by a local Ollama ``/api/generate`` call.

    Raises ``httpx.HTTPError`` subclasses (timeout, connect, status) and
    :class:`RewriteParseError` — it does not swallow failures. The Searcher
    decides whether to fall through.
    """

    name = "ollama"

    def __init__(
        self,
        cfg: SearchConfig,
        *,
        client: httpx.Client | None = None,
        today_provider: Callable[[], date] = date.today,
    ) -> None:
        self._cfg = cfg
        self.model = cfg.rewriter_model
        self._client = client or httpx.Client(timeout=cfg.rewriter_timeout_s)
        self._today = today_provider

    def rewrite(self, free_text: str) -> RewriteResult:
        prompt = build_rewrite_prompt(
            free_text,
            today=self._today(),
            max_expansion_terms=self._cfg.rewriter_max_expansion_terms,
        )
        resp = self._client.post(
            f"{self._cfg.ollama_host}/api/generate",
            json={
                "model": self.model,
                "prompt": prompt,
                "stream": False,
                "format": _OLLAMA_FORMAT_SCHEMA,
                "options": {"temperature": 0},
            },
        )
        resp.raise_for_status()
        return parse_rewrite_response(resp.json()["response"])
```

- [ ] **Step 4: Run test to verify it passes**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_rewriter.py -v`
Expected: PASS (all rewriter tests)

- [ ] **Step 5: Commit**

```bash
git add src/localmail/search/rewriter.py tests/test_rewriter.py
git commit -m "feat(search): OllamaLLMRewriter HTTP backend (httpx, format-constrained JSON)"
```

---

## Task 6: `build_lexical_tsquery` + wire into both BM25 arms

**Files:**
- Modify: `src/localmail/search/arms.py:93-169`
- Test: `tests/test_arms.py` (create if absent)

- [ ] **Step 1: Write the failing unit test (identity + expansion shape)**

Add to `tests/test_arms.py`:

```python
from localmail.search.arms import build_lexical_tsquery


def test_lexical_tsquery_identity_with_no_expansion():
    sql, params = build_lexical_tsquery("hello world", [])
    assert sql == "plainto_tsquery('simple', %s)"
    assert params == ["hello world"]


def test_lexical_tsquery_ors_expansion_terms():
    sql, params = build_lexical_tsquery("invoice", ["bill", "receipt"])
    assert sql == (
        "plainto_tsquery('simple', %s) || plainto_tsquery('simple', %s)"
        " || plainto_tsquery('simple', %s)"
    )
    assert params == ["invoice", "bill", "receipt"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_arms.py -k lexical_tsquery -v`
Expected: FAIL — `build_lexical_tsquery` undefined.

- [ ] **Step 3: Implement the helper and wire both arms**

In `src/localmail/search/arms.py`, add near the top-level helpers (after `_filter_sql`):

```python
def build_lexical_tsquery(
    free_text: str, expansion_terms: list[str]
) -> tuple[str, list[str]]:
    """Build an OR-combined tsquery fragment + its params.

    With no expansion terms this is byte-identical to the single
    ``plainto_tsquery('simple', %s)`` form, so the non-smart path is
    unchanged. Each expansion term adds one OR-ed ``plainto_tsquery`` so a
    message matching only a synonym is still retrieved.
    """
    terms = [free_text, *expansion_terms]
    fragment = " || ".join(["plainto_tsquery('simple', %s)"] * len(terms))
    return fragment, terms
```

Then in `arm_bm25_messages`, replace the `sql`/`params` construction (currently lines ~119-132) with:

```python
    where_extra, where_params = _filter_sql(parsed.filters)
    tsq_sql, tsq_params = build_lexical_tsquery(
        parsed.free_text, parsed.expansion_terms
    )
    sql = f"""
        WITH ranked AS (
            SELECT m.id,
                   ts_rank_cd(%s::float4[], m.fts_v2, {tsq_sql}) AS score
            FROM messages m
            WHERE m.fts_v2 @@ {tsq_sql}
            {where_extra}
            ORDER BY score DESC
            LIMIT %s
        )
        SELECT id, score, ROW_NUMBER() OVER (ORDER BY score DESC) FROM ranked
    """
    params: list[Any] = [weights, *tsq_params, *tsq_params, *where_params, limit]
```

And in `arm_bm25_chunks`, replace its `sql`/`params` (currently lines ~152-166) with:

```python
    where_extra, where_params = _filter_sql(parsed.filters)
    tsq_sql, tsq_params = build_lexical_tsquery(
        parsed.free_text, parsed.expansion_terms
    )
    sql = f"""
        WITH ranked AS (
            SELECT mc.message_id, mc.id AS chunk_id,
                   ts_rank_cd(mc.fts, {tsq_sql}) AS score
            FROM message_chunks mc JOIN messages m ON m.id = mc.message_id
            WHERE mc.fts @@ {tsq_sql}
            {where_extra}
            ORDER BY score DESC
            LIMIT %s
        )
        SELECT message_id, chunk_id, score,
               ROW_NUMBER() OVER (ORDER BY score DESC) FROM ranked
    """
    params: list[Any] = [*tsq_params, *tsq_params, *where_params, limit]
```

- [ ] **Step 4: Run unit + full arm tests**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_arms.py -v`
Expected: PASS (new unit tests + any existing arm tests still green — the empty-expansion path emits the same SQL/params as before)

- [ ] **Step 5: Commit**

```bash
git add src/localmail/search/arms.py tests/test_arms.py
git commit -m "feat(search): OR-in expansion terms via build_lexical_tsquery (identity when empty)"
```

---

## Task 7: DB test — expansion broadens recall

**Files:**
- Test: `tests/test_arms.py` (DB-backed; uses `db_conn` fixture)

- [ ] **Step 1: Write the failing/again-green DB test**

Add to `tests/test_arms.py`. Reuse the file's existing `db_conn` fixture and
direct-INSERT style (see `_seed_corpus`/the inserts at `tests/test_arms.py:52`).
`fts_v2` is a generated STORED column, so a plain INSERT populates it — no
manual refresh needed:

```python
from localmail.config import SearchConfig
from localmail.search.arms import arm_bm25_messages
from localmail.search.query import ParsedQuery


def _seed_one(conn, subject, body):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO accounts (name,email_address,imap_host,auth_method)"
                    " VALUES ('a','a@x','h','password') RETURNING id")
        acct = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO messages (account_id, message_id, raw_sha256, subject,"
            " body_text, headers, raw_bytes, size_bytes)"
            " VALUES (%s, %s, %s, %s, %s, '{}'::jsonb, 'r', 1)",
            (acct, "<m1>", b"\x01" * 32, subject, body),
        )
    conn.commit()


def test_expansion_term_retrieves_synonym_only_message(db_conn):
    _seed_one(db_conn, subject="receipt for lunch", body="thanks")
    cfg = SearchConfig()

    base = ParsedQuery(free_text="invoice")
    assert arm_bm25_messages(db_conn, base, cfg, limit=10) == []   # no match

    expanded = ParsedQuery(free_text="invoice", expansion_terms=["receipt"])
    hits = arm_bm25_messages(db_conn, expanded, cfg, limit=10)
    assert len(hits) == 1                                          # synonym hit
```

> The contract — empty without the expansion term, one hit with it — is what
> matters; if the live `messages` insert needs extra NOT NULL columns, copy the
> exact column list from `tests/test_arms.py:52`.

- [ ] **Step 2: Run test to verify behaviour**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_arms.py -k synonym -v`
Expected: PASS (skips automatically if Postgres is unreachable, per the suite's DB-skip convention).

- [ ] **Step 3: Commit**

```bash
git add tests/test_arms.py
git commit -m "test(search): expansion term retrieves synonym-only message"
```

---

## Task 8: Wire rewriter into `Searcher.search` + `rewrite_skipped` on `SearchPage`

**Files:**
- Modify: `src/localmail/search/searcher.py:238-248` (SearchPage), `:834-924` (search)
- Test: `tests/test_searcher_smart.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/test_searcher_smart.py`. It mirrors the inline construction in
`tests/test_searcher_pagination.py:14-48` (`_E`/`_R` fakes, `_seed_many`,
`open_pool`), injecting a `rewriter=`:

```python
"""Smart-path wiring tests: assert the rewrite call path + surfaced fall-through,
not retrieval quality."""

from __future__ import annotations

import logging

import httpx

from localmail.config import SearchConfig
from localmail.db import open_pool
from localmail.search.embed_worker import run_embed_worker_once
from localmail.search.query import SearchFilters
from localmail.search.rewriter import RewriteResult
from localmail.search.searcher import Searcher


class _E:
    name = "s"; model = "s"; dimension = 768
    def embed_documents(self, t): return [[1.0] * 768 for _ in t]
    def embed_query(self, t): return [0.5] * 768
    def health_check(self): pass


class _R:
    name = "s"; model = "s"
    def rerank(self, q, c): return [1.0 - i * 0.001 for i, _ in enumerate(c)]


class FakeRewriter:
    name = "fake"; model = "fake"
    def __init__(self, result): self._result = result
    def rewrite(self, free_text): return self._result


class RaisingRewriter:
    name = "raise"; model = "raise"
    def rewrite(self, free_text): raise httpx.ConnectError("down")


def _seed_one(conn):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO accounts (name,email_address,imap_host,auth_method)"
                    " VALUES ('a','a@x','h','password') RETURNING id")
        acct = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO messages (account_id, message_id, raw_sha256, subject,"
            " body_text, headers, raw_bytes, size_bytes)"
            " VALUES (%s, '<m1>', %s, 'Subject test', 'body test content',"
            " '{}'::jsonb, 'r', 1)",
            (acct, b"\x01" * 32),
        )
    conn.commit()


def _smart_result():
    return RewriteResult(rewritten_text="rich", expansion_terms=["syn"],
                         extracted_filters=SearchFilters())


def test_smart_enriches_parsed_and_times_rewrite(db_dsn, db_conn):
    _seed_one(db_conn)
    cfg = SearchConfig()
    run_embed_worker_once(db_conn, cfg, _E())
    pool = open_pool(db_dsn)
    try:
        s = Searcher(pool=pool, cfg=cfg, embeddings=_E(), reranker=_R(),
                     rewriter=FakeRewriter(_smart_result()))
        page = s.search("test", smart=True, use_cache=False)
    finally:
        pool.close()
    assert page.query.rewritten_text == "rich"
    assert page.query.expansion_terms == ["syn"]
    assert "rewrite" in page.timing_ms
    assert page.rewrite_skipped is False


def test_smart_falls_through_on_rewriter_failure(db_dsn, db_conn, caplog):
    _seed_one(db_conn)
    cfg = SearchConfig()
    run_embed_worker_once(db_conn, cfg, _E())
    pool = open_pool(db_dsn)
    try:
        s = Searcher(pool=pool, cfg=cfg, embeddings=_E(), reranker=_R(),
                     rewriter=RaisingRewriter())
        with caplog.at_level(logging.WARNING, logger="localmail.search"):
            page = s.search("test", smart=True, use_cache=False)
    finally:
        pool.close()
    assert page.rewrite_skipped is True
    assert page.query.rewritten_text is None          # un-rewritten
    assert any("rewrite skipped" in r.message for r in caplog.records)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_searcher_smart.py -v`
Expected: FAIL — `SearchPage` has no `rewrite_skipped`; smart path does not call the rewriter.

- [ ] **Step 3: Add `rewrite_skipped` to `SearchPage`**

In `src/localmail/search/searcher.py`, in the `SearchPage` dataclass (after `next_keyset: KeysetCursor | None = None`):

```python
    rewrite_skipped: bool = False
```

- [ ] **Step 4: Add the rewrite call + imports in `search()`**

At the top of `searcher.py`, add to the imports:

```python
import httpx

from localmail.search.rewriter import RewriteParseError, apply_rewrite
```

In `search()`, immediately after the `parse_query` timing block (currently lines 871-872, before the `if sort == "date" ...` branch at 882), insert:

```python
        rewrite_skipped = False
        if smart and parsed.free_text.strip():
            t = time.monotonic()
            try:
                result = self._rewriter.rewrite(parsed.free_text)
                parsed = apply_rewrite(
                    parsed, result,
                    max_expansion_terms=cfg.rewriter_max_expansion_terms,
                )
            except (httpx.HTTPError, RewriteParseError) as exc:
                rewrite_skipped = True
                logging.getLogger("localmail.search").warning(
                    "smart rewrite skipped: %s", exc
                )
            timing["rewrite"] = (time.monotonic() - t) * 1000
```

Then thread `rewrite_skipped=rewrite_skipped` into the three `return SearchPage(...)` constructions inside `search()`:
- the `sort == "date"` branch (currently ~893),
- the empty-query branch (currently ~919),
- the hybrid path's final return (currently ~827).

Add the kwarg to each, e.g. `..., next_keyset=next_keyset, rewrite_skipped=rewrite_skipped)`.

> `continue_page`/`grow_pool` reuse the cached, already-enriched `parsed`; they
> do not re-rewrite and leave `rewrite_skipped=False` (page-1 concern only).
> This is intentional — do not thread it through the cache.

- [ ] **Step 5: Run tests to verify they pass + full searcher suite**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_searcher_smart.py tests/test_searcher*.py -v`
Expected: PASS (new smart tests; existing searcher tests unaffected — defaulted `rewrite_skipped`)

- [ ] **Step 6: Commit**

```bash
git add src/localmail/search/searcher.py tests/test_searcher_smart.py
git commit -m "feat(search): call rewriter on --smart with surfaced graceful fall-through"
```

---

## Task 9: `create_searcher` builds the rewriter + exports

**Files:**
- Modify: `src/localmail/search/__init__.py`
- Test: `tests/test_search_public_api.py` (extend — this is where `create_searcher` is tested, with `db_dsn` + `_StubEmbedder`)

- [ ] **Step 1: Write the failing test**

Add to `tests/test_search_public_api.py` (reuse its `_StubEmbedder` and the
`db_dsn` fixture — `create_searcher` opens a live pool). Build the config via
the same loader the other tests use, flip `rewriter_enabled_by_default`, and
pass `reranker=None` so no reranker model is needed:

```python
def test_create_searcher_builds_rewriter_when_enabled(db_dsn):
    from localmail.config import LocalmailConfig
    cfg = LocalmailConfig()                     # match the construction used by
    cfg.database.dsn = db_dsn                    # the other tests in this file
    cfg.search.rewriter_enabled_by_default = True
    searcher = create_searcher(cfg=cfg, embeddings=_StubEmbedder(), reranker=None)
    try:
        assert searcher._rewriter is not None
        assert searcher._rewriter.name == "ollama"
    finally:
        searcher._pool.close()


def test_create_searcher_no_rewriter_when_disabled(db_dsn):
    from localmail.config import LocalmailConfig
    cfg = LocalmailConfig()
    cfg.database.dsn = db_dsn
    cfg.search.rewriter_enabled_by_default = False
    searcher = create_searcher(cfg=cfg, embeddings=_StubEmbedder(), reranker=None)
    try:
        assert searcher._rewriter is None
    finally:
        searcher._pool.close()
```

> Match the exact config construction the neighbouring tests in this file use
> (look at `test_create_searcher_returns_searcher` at line ~69). The contract
> under test is purely the rewriter-construction branch.

- [ ] **Step 2: Run test to verify it fails**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_create_searcher.py -k rewriter -v`
Expected: FAIL — `create_searcher` has no `rewriter` wiring; `_rewriter` is `None` even when enabled.

- [ ] **Step 3: Wire `create_searcher` + exports**

In `src/localmail/search/__init__.py`:

Add to `__all__`: `"QueryRewriter", "RewriteResult", "OllamaLLMRewriter", "RewriteParseError"`.

Add a module-level import (after the existing search imports):

```python
from localmail.search.rewriter import (
    OllamaLLMRewriter,
    QueryRewriter,
    RewriteParseError,
    RewriteResult,
)
```

Add `rewriter=_UNSET` to the `create_searcher` signature (after `reranker=_UNSET`). In the body, after the reranker block and before `return Searcher(...)`:

```python
    if rewriter is _UNSET:
        if cfg.search.rewriter_enabled_by_default:
            try:
                rewriter = OllamaLLMRewriter(cfg.search)
            except Exception as exc:
                logging.getLogger("localmail.search").warning(
                    "rewriter init failed (%s=%r): %s — continuing without --smart",
                    "rewriter_model", cfg.search.rewriter_model, exc,
                )
                rewriter = None
        else:
            rewriter = None
```

Add `rewriter=rewriter` to the `Searcher(...)` call.

- [ ] **Step 4: Run tests to verify they pass**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_create_searcher.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/localmail/search/__init__.py tests/test_create_searcher.py
git commit -m "feat(search): create_searcher builds OllamaLLMRewriter when enabled; export rewriter API"
```

---

## Task 10: CLI surfaces `rewrite_skipped`

**Files:**
- Modify: `src/localmail/cli.py:664-680`
- Test: `tests/test_cli_search.py` (extend, or assert via CliRunner where the suite already tests `search`)

- [ ] **Step 1: Write the failing test**

Add to `tests/test_cli_search.py`, mirroring its existing pattern (`main` +
`monkeypatch.setattr("localmail.cli.create_searcher", ...)`):

```python
def test_search_prints_notice_when_rewrite_skipped(monkeypatch):
    from click.testing import CliRunner
    from localmail.cli import main
    from localmail.search.searcher import SearchPage
    from localmail.search.query import ParsedQuery

    page = SearchPage(
        results=[], page=1, page_size=10, pool_size=0, candidates_per_arm=50,
        has_more_in_pool=False, can_grow_pool=False, search_token=None,
        query=ParsedQuery(free_text="x"), timing_ms={}, rewrite_skipped=True,
    )

    class _Stub:
        def search(self, *a, **k):
            return page

    monkeypatch.setattr("localmail.cli.create_searcher", lambda: _Stub())
    res = CliRunner(mix_stderr=False).invoke(main, ["search", "x", "--smart"])
    assert res.exit_code == 0
    assert "rewrite skipped" in res.stderr.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_cli_search.py -k rewrite_skipped -v`
Expected: FAIL — no notice printed.

- [ ] **Step 3: Add the notice**

In `src/localmail/cli.py`, in the `search` command after the `searcher.search(...)` call returns `page` (right before the `if verbose:` block at ~675):

```python
    if page.rewrite_skipped:
        click.echo("note: --smart rewrite skipped (rewriter unavailable); "
                   "ran the original query", err=True)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_cli_search.py -k rewrite_skipped -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/localmail/cli.py tests/test_cli_search.py
git commit -m "feat(cli): notice when --smart rewrite is skipped"
```

---

## Task 11: Full gate + type check + docs

**Files:**
- Modify: `README.md`, `CLAUDE.md`

- [ ] **Step 1: Run the full suite**

Run: `unset VIRTUAL_ENV && uv run pytest -q tests/ --deselect tests/test_daemon_control_socket.py`
Expected: all pass (the macOS-only AF_UNIX socket-path failures are deselected per the repo convention).

- [ ] **Step 2: Type check**

Run: `unset VIRTUAL_ENV && uv run mypy src/localmail`
Expected: clean. Fix any `Any`/Optional issues in `rewriter.py`/`searcher.py` surfaced here (e.g. annotate the `self._rewriter` call site if mypy complains it is `Any | None`).

- [ ] **Step 3: Document `--smart` in README**

Add a short subsection under the search docs in `README.md` describing: `--smart` runs a local Ollama LLM (`ollama pull qwen2.5:3b`) to rewrite the query, add synonym recall, and infer date/sender/subject filters; explicit operators always win; if Ollama is unavailable the original query runs and a notice is printed. Mention `[search] rewriter_enabled_by_default`, `rewriter_model`, `rewriter_timeout_s`, `rewriter_max_expansion_terms`, and `ollama_host`.

- [ ] **Step 4: Document the rewriter in CLAUDE.md**

In the "Search subsystem" section of `CLAUDE.md`, add `rewriter.py` to the module list and a short note: Phase 4 `--smart` rewriter is shipped; pure helpers (`build_rewrite_prompt`/`parse_rewrite_response`/`apply_rewrite`) + `OllamaLLMRewriter`; explicit-operators-win merge; graceful fall-through surfaced via `SearchPage.rewrite_skipped`; expansion terms OR-ed through `arms.build_lexical_tsquery` (identity when empty); no new migration, no new uv extra.

- [ ] **Step 5: Commit**

```bash
git add README.md CLAUDE.md
git commit -m "docs(search): document --smart query rewriter (Phase 4)"
```

---

## Self-review notes (for the executor)

- **Spec coverage:** Task 1 → config; Tasks 2-5 → rewriter module (prompt/parse/merge/IO); Tasks 6-7 → expansion arms; Task 8 → searcher wiring + `rewrite_skipped`; Task 9 → factory + exports; Task 10 → CLI surface; Task 11 → gate + docs. All spec sections are covered.
- **Identity guarantee:** Task 6's empty-expansion path emits the same SQL/params as the pre-change arms — verify the existing arm tests stay green in Task 6 Step 4 before moving on.
- **Type consistency:** `apply_rewrite(parsed, result, *, max_expansion_terms)`, `build_lexical_tsquery(free_text, expansion_terms) -> (sql, params)`, `RewriteResult(rewritten_text, expansion_terms, extracted_filters)`, `SearchPage.rewrite_skipped` are used identically across tasks.
- **Fixtures:** Tasks 7-10 reuse the suite's existing DB/searcher/CLI fixtures; where a named fixture is assumed (`seed_message`, `make_searcher`), the task says to adapt to the real fixture found via `grep`. Confirm fixture names before writing those tests.
