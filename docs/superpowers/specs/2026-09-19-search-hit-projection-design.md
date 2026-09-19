# Compact search hits: `fields` projection and `snippet_chars` (kastellan slice E)

**Status:** approved 2026-09-19. Slice E of kastellan's request list (order
0 → A → B → D → **E** → C). No issue number; the request came from the
kastellan project's session of 2026-09-14.

## Problem

kastellan's `mail.search` tool hands `/v1/search` hits to its planner, which
reads them as pruned JSON under a byte budget (16 KiB per step since
kastellan #702, 64 KiB accumulated). A `limit: 50` search overruns it. Every
hit carries eleven keys, several of which the planner never uses (`score`,
`matched_arms`, `folder` and `to`, which are always `null`/`[]` on this path),
and a ~200-character snippet the caller cannot size.

The snippet field is also misnamed: `snippet_html` holds **plain text**
(`make_snippet` slices chunk text; body chunks are built from `body_text` alone, and header and attachment chunks are plain text too).
kastellan's fixtures had to learn the name from `api/search.py::_to_api_result`.

What kastellan asked for, as agreed on 2026-09-14:

- a `fields` projection that **rejects unknown field names**;
- `snippet_chars`, with a server-side cap;
- `snippet` as the honest name for the snippet text;
- the **default response unchanged**, for the GUI's sake.

## Decisions (operator, 2026-09-19)

1. **Surface: `POST /v1/search` only.** kastellan is REST-only. Browse rows
   (`/v1/messages`) already carry five keys. The MCP `search` tool keeps its
   schema; it could adopt this later cheaply, because the work lives in
   `run_search` and a pure module.
2. **`snippet` is selectable through `fields` only.** The default response is
   byte-identical to today, so it never gains a `snippet` key.
   `snippet_html` stays a valid field name.
3. **`snippet_chars` both shrinks and widens**, capped by a new config value.
   A value outside the range is a 400 and is never clamped silently.
4. **The projection returns exactly the named keys.** `message_id` is not
   implied.

## Wire contract

Two new optional request fields on `POST /v1/search`. Both are nullable, and
`null` or omitted means today's behaviour.

### `fields: list[str] | null`

- It projects **each element of `results`** to exactly the named keys. The
  envelope (`next_cursor`, `total_estimate`, `took_ms`, `sort_applied`,
  `rankable`, `rewrite_*`) is never projected.
- The permitted names are the current hit keys plus `snippet`:
  `message_id`, `account`, `folder`, `subject`, `from`, `to`, `date`,
  `snippet_html`, `has_attachments`, `score`, `matched_arms`, `snippet`.
- `snippet` is the same string as `snippet_html`. Naming both returns both.
- Keys come back in the canonical order above, whatever order the request
  used. JSON consumers must not depend on order anyway, but a deterministic
  order makes the golden tests exact.
- A duplicate name collapses. That is harmless: the output is a mapping, and
  no information is lost.
- **400 problem+json** in three cases:
  - an unknown name (the message names it and lists the supported set);
  - an empty list (asking for hits with no keys is a caller bug, not a
    request);
  - a non-string element.
- It applies uniformly on every branch: fresh, keyset continuation and pool
  continuation, and also the empty-ACL page, trivially, since that has no
  results.

### `snippet_chars: int | null`

- It is the width of the snippet **window**, in characters. It is passed as
  `make_snippet(..., width=snippet_chars)`. `make_snippet` may add a leading
  and a trailing `…` when the window is cut from inside the text, so the
  returned string is at most `snippet_chars + 2` characters. That is
  documented, not changed: changing `make_snippet` would change the default
  response, which decision 2 forbids.
- **Range: `1 ≤ snippet_chars ≤ [search] snippet_max_chars`.** The config
  value is new, with a default of 1000. Anything outside the range is a
  **400** that names the range. `true`/`false` count as outside it: `bool` is
  an `int` subclass in Python and would otherwise read as 1/0. This is decided
  in the pure rule, not left to pydantic's lax mode.
- Omitted means `[search] snippet_width_chars` (200), exactly as today.
- **Widening is cheap and is honoured on every page.** `_build_results`
  builds each page's snippets from the full `snippet_source_text` held in
  the cached pool, so a continuation page takes its own `snippet_chars` and
  no re-retrieval happens. Page 2 may therefore ask for a different width
  from page 1, and gets it.
- **The date walk** (`sort=date`, or any query with no free text) emits
  `snippet: ""` today (`Searcher._date_keyset_search`). There, `snippet_chars`
  is accepted and bounds a string that is already empty. Giving that branch
  snippets is a separate change and is out of scope. Say so in the README
  rather than let a caller infer that a filter-only search has snippets.

### Validation order

Both arguments are validated in `run_search` **before the empty-ACL
short-circuit**, alongside the other gates over stated arguments (#348,
#364). Otherwise a grant-nothing caller sending `fields: ["bogus"]` would be
answered 200 with an empty page, byte-identical to "no results".

### `api_minor` 2 → 3

A server after #366 already refuses an unknown top-level field with a 400,
so a client could discover the feature by failing. The bump lets kastellan
gate on a version instead, the same arrangement as slices B (`api_minor` 1)
and D (2). Nothing in-tree pins the value. An external consumer testing
`== 2` breaks, as in D.

## Code

### New pure module: `src/localmail/api/search_projection.py`

No IO. It is the one authority for the name set and both rules, in the
same shape as `account_names.account_name_error`: a function returns a
message or `None`, and the caller decides what an error *is*.

- `HIT_FIELDS: tuple[str, ...]`: the canonical, ordered name set.
- `fields_error(fields: object) -> str | None`: covers unknown names, an
  empty list and non-string elements.
- `snippet_chars_error(value: object, *, max_chars: int) -> str | None`:
  covers `bool`, non-`int` values and the range.
- `project_hit(hit: dict[str, Any], fields: Sequence[str]) -> dict[str, Any]`:
  derives `snippet` from `snippet_html`, then selects keys in `HIT_FIELDS`
  order. It assumes the fields were already validated, and raises `KeyError`
  otherwise. That is a loud bug at the boundary that can see it, not a
  silent drop.
- A test pins that the keys `_to_api_result` emits, plus `snippet`, **equal**
  `HIT_FIELDS`. A hit key added later then cannot go missing from the
  projection, and a name cannot be permitted that the hit never carries.
  This is the `QueueCounts.status_field_names()` lesson.

### `SearchConfig.snippet_max_chars: int = 1000`

- `Field(ge=1)`.
- A model validator requires `snippet_max_chars >= snippet_width_chars`.
  Otherwise a caller could not state the default width explicitly: the
  default would be a value the API refuses.

### `Searcher`

- `search`, `continue_page` and `grow_pool` each gain a keyword-only
  `snippet_chars: int | None = None`.
- `_build_results` takes it and computes
  `width = snippet_chars if snippet_chars is not None else cfg.snippet_width_chars`.
- A default of `None` is right here, not #234's no-default shape: there is a
  safe value (the config width), and CLI and library callers must keep
  working unchanged.
- The Searcher checks **positivity only**, not the upper cap. The cap is an
  operator's resource bound for *network* callers, so the api boundary owns
  it; `make_snippet` is correct for any positive width.
- `snippet_chars < 1` (a `bool` included) raises a plain `ValueError` before
  any IO. `0` would otherwise yield a meaningless empty window, so a library
  caller's bug is loud.
  - It is not a `SearchArgumentRefused` member, for the reason the sort-axis
    membership checks are not (#348): no wire caller can reach it past the
    api gate.

### `run_search`

It gains `fields: list[str] | None = None` and `snippet_chars: int | None = None`.

- It validates both right after `sort_membership_error` and before
  `_gate_query`, raising `ValidationFailed`.
- It forwards `snippet_chars` to all three Searcher entry points (including
  `_continue_or_grow`).
- If `fields` is set, it applies `project_hit` to each `_to_api_result(r)`.

### Route: `serve/routes/search.py`

- `SearchRequest` gains `fields: list[str] | None = None` and
  `snippet_chars: int | None = None`. They are deliberately untyped beyond
  that, so that `true`, `0` and an out-of-range value reach the pure rule and
  come back as problem+json 400. A pydantic `ge`/`le` would answer 422 with
  an array `detail` (#370), which kastellan cannot act on.
- **`list[str]`** stays, though: a non-list `fields` is a type error, and the
  422 for a type error on a known field is today's contract (#370 tracks it).
  The non-string-element check in `fields_error` is for library callers.
- `_unknown_field_error` needs no change. The new fields are model fields,
  so they drop out of `model_extra`.

### MCP

`mcp/tools.py` calls `run_search` with keywords, so the defaults leave it
unchanged. No schema change.

## Tests

**Pure (`tests/test_search_projection.py`):**
- every name accepted;
- unknown name refused, with the supported set listed;
- empty list, non-string element and duplicates;
- canonical order regardless of request order;
- `snippet` equals `snippet_html`;
- `snippet_chars` at 0, 1, max, max+1, `True`, `False`, `1.5` and `"10"`;
- the `HIT_FIELDS` ⇔ `_to_api_result` equality pin.

**Config:**
- the default is 1000;
- `snippet_max_chars < snippet_width_chars` is rejected;
- `snippet_max_chars = 0` is rejected.

**Searcher (seeded DB):**
- on a body longer than 1000 characters, `snippet_chars=500` yields a window
  longer than 200, and `=50` yields a string of at most 52 characters;
- omitted yields today's snippet, byte-identical (asserted against the
  default-width result);
- a continuation page honours its own `snippet_chars`, different from
  page 1's;
- `snippet_chars=0` raises from the Searcher before any IO.

**`run_search` / route (through the real transport, never a MagicMock hit;
see the `{}` trap under #345):**
- `fields=["message_id","snippet"]` returns hits with exactly those two
  keys;
- the envelope keys are all still present;
- an unknown name, `[]`, `snippet_chars` of 0, `snippet_chars` of
  `max_chars + 1` and `snippet_chars` of `true` each give a 400
  problem+json;
- each of those is also a 400 **from a grant-nothing caller**, which pins
  the ordering;
- `fields` applies on a keyset continuation and on a pool continuation.

**Default unchanged:**
- a request without either field returns hits whose key set is exactly
  today's eleven;
- the snippet is exactly the default-width snippet.

**Existing:**
- `test_serve_search_request_keys.py` stays green. The GUI sends neither key,
  and the test only requires GUI fields to be a subset of supported keys.

**Version:**
- `api_minor == 3`, in the existing version route test.

## Out of scope

- The MCP `search` tool's schema.
- `/v1/messages`.
- `get_message`.
- Snippets on the date walk.
- Deprecating or renaming `snippet_html`.
- Splitting `api/search.py`, which is 726 lines and grows by about 15 here;
  the new logic lives in the pure module.
