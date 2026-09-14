# Search requests are honoured or refused, never dropped

Date: 2026-09-14
Status: design approved in outline, awaiting spec review
Issue: #364 (slice A of the kastellan request triage; #365 follows it)

## Problem

A planner in hherb/kastellan#677 asked `POST /v1/search` for messages with attachments. It then could not find the message carrying a PDF. #364 reports that the `has_attachment` filter is "silently ignored". Reading the code shows the filter works; four other things are wrong, and each fails without a signal:

1. **Every hit's `has_attachments` flag is wrong.** `api/search.py::_to_api_result` emits `r.attachment_filename is not None`. That reports whether the *matched chunk* came from an attachment, not whether the message has one. A message with two PDFs matched through its subject reports `false`, which is exactly the evidence in #364's thread ("true only on hits whose matched_arms was attachment_chunks"). The filter itself (`search/arms.py::_filter_sql`) is applied on all four retrieval arms and on the date walk.
2. **`has_attachment: false` is discarded.** `_filter_tokens` emits `has:attachment` for `True` and nothing for `False`. The DSL has no spelling for "without". The MCP `search` tool's own description promises "False for only those without". `_filter_sql` already honours `False`; it is reachable today only through the smart rewriter.
3. **Unknown filter keys are discarded.** `SearchFiltersModel` sets `extra: "ignore"`, and `build_query_string`'s docstring calls that forward compatibility. It is the opposite: a planner that copies the hit field's name back as a filter (`has_attachments`, plural) gets unfiltered results with HTTP 200. So does a newer client talking to an older server. The DSL has the same hole: `has:` with any value but `attachment` is removed from the query entirely, not even kept as free text.
4. **`query` is required.** A filter-only search works when sent as `"query": ""`, since a textless query takes the date walk. But the field is mandatory, so a client that models it as optional gets a 422 (hherb/kastellan#698).

Unknown **top-level** request fields are discarded the same way. That matters beyond typos: slices B–E add request fields, and an older server ignoring `fields` or `snippet_chars` returns an uncompacted answer that looks like a compacted one.

A fifth defect sits beside these: filter *values* are validated after the empty-ACL short-circuit. A malformed `date_from` from a caller granted nothing is answered 200 with an empty page, byte-identical to "no results". Every other gate over a stated argument already precedes that branch (#348); these checks were missed.

## Non-goals

- **Changing what "has attachments" means.** Images embedded in the HTML body still count, as they do today. That is #365, which moves the filter and the flag together; this slice makes them agree first, and puts the rule in one place so #365 changes one constant.
- A GUI control for "without attachments". The GUI never sends `false` (unchecked is `null`), and its DSL parser keeps an unrecognised `has:` token as free text, so it is unaffected.
- A CLI `--no-attachment` flag.
- Renaming `has_attachments` (hit) or `has_attachment` (filter). The GUI's types and Kastellan's fixtures carry both; the did-you-mean below covers the confusion.
- Any API version bump. `API_MINOR` has never moved, through several additive changes. Every behaviour here is visible in the response itself: a 400 or a corrected flag. Feature detection is slice B's problem, because an old server answers `?headers=list` silently.

## Design

### 1. One rule for "this message has attachments"

A new pure module `search/attachment_presence.py` owns the SQL expression:

```python
#: True iff the message's attachments array is non-empty. Guarded, because
#: `jsonb_array_length` raises 22023 on a non-array and `messages.attachments`
#: is `JSONB NOT NULL DEFAULT '[]'` with no CHECK (see #280's note): one
#: malformed row would otherwise fail every search page that surfaces it.
#: CASE, not AND: Postgres does not promise AND's evaluation order.
HAS_ATTACHMENT_SQL = (
    "CASE WHEN jsonb_typeof(m.attachments) = 'array' "
    "THEN jsonb_array_length(m.attachments) > 0 ELSE FALSE END"
)
```

Measured on the test cluster: `'[]'` gives false, `'[{...}]'` gives true, and `'{}'`, `'"x"'` and `'null'` give false. The bare form raises `InvalidParameterValue` on all three of those.

It has three consumers, and none of them may restate it:

- **`_filter_sql`:** `has_attachment is True` gives `HAS_ATTACHMENT_SQL`; `is False` gives `NOT (HAS_ATTACHMENT_SQL)`, an exact complement because the expression is never NULL.
- **`Searcher._hydrate`'s messages SELECT:** one more column. The table gains the alias `m`, since the expression names it.
- **`date_keyset.ROW_SQL_TEMPLATE`:** one more column, composed in rather than written out, because that template is also what the plan tests EXPLAIN.

It lives in its own module rather than `arms.py` because `arms.py` imports `searcher.py` (for `ArmHit`), and `searcher.py` imports `date_keyset.py` at module level. `date_keyset.py` taking the constant from `arms.py` would close that cycle, which is why `searcher.py` already imports `arms` only inside functions. It is also the one place #365 edits.

`SearchResult` gains `has_attachments: bool`, **defaultless**: a result that could claim `false` by omission is #364. Both construction sites set it from the row: the date walk and `_build_results`. `_to_api_result` emits `r.has_attachments`. `attachment_filename` keeps its real job, naming the file a snippet came from.

The pool cache stores hydrated message dicts, so continuation pages carry the field with no further change. A pool cached before the deploy dies with the process that held it.

### 2. `has_attachment: false` is honoured

The DSL gains one value: `has:no-attachment`.

- `query.parse_query` sets `has_attachment = False` for it.
- `has:` with any other value raises `QueryParseError`, naming both valid values. `_gate_free_text` already translates that to a 400 at the top of `run_search`, so no new catch is needed.
- `has:attachment` together with `has:no-attachment` in one query also raises, instead of the last one winning.
- `_filter_tokens` emits `has:no-attachment` for `False`.

**Spelling.** `-has:attachment` (Gmail's) was rejected: negation would then exist for one operator only, and `-from:x` would silently stay free text, the defect this slice removes. A second value on the existing operator needs no tokenizer change.

**The GUI** (`gui/src/lib/filter_parse.ts`) keeps an unrecognised `has:` token as free text and sends it on, so a user who types `has:no-attachment` gets the filter without a chip. It never *produces* the token. No GUI change.

### 3. Unknown keys are refused, by name

**Filter keys.** A pure `filter_key_error(filters: Mapping[str, object]) -> str | None` sits in `api/search.py` beside `_SUPPORTED_FILTER_KEYS`, shaped like `account_names.account_name_error`: a message, or `None`.

- It reports every key outside the supported set, **whatever its value**, `null` included. A key that does not exist is a mistake even when null.
- It adds a did-you-mean from `difflib.get_close_matches(key, supported, n=1, cutoff=0.8)`, so `has_attachments` suggests `has_attachment`.
- `run_search` calls it right after the sort-membership check and raises `ValidationFailed`. It therefore applies to every transport and to library callers.

The HTTP route has to see extra keys to report them:

- `SearchFiltersModel` switches to `extra: "allow"`.
- The route forwards `model_dump(by_alias=True, exclude_none=True)` merged with `model_extra`, so an unknown key reaches `run_search` even when its value is `null`.
- The MCP tool declares typed parameters and cannot produce one.

`_KNOWN_UNSUPPORTED_FILTER_KEYS` and its check are deleted: always empty, and superseded. A test pins that the model's wire names equal `_SUPPORTED_FILTER_KEYS`, which is the drift that machinery existed for: a field accepted by the model and silently unsupported downstream.

**Top-level keys.** `SearchRequest` switches to `extra: "allow"`, and the route refuses a non-empty `model_extra` before calling `run_search`. That is a transport concern: `run_search` takes keyword arguments and cannot receive one. The supported names are derived from `SearchRequest.model_fields`, so a field added later is in scope without editing a list.

**Why 400 problem+json rather than Pydantic's `extra: "forbid"`.** Forbid produces FastAPI's 422, whose `detail` is an array, not problem+json. Kastellan's worker renders only a problem+json `detail`, and falls back to a 512-byte raw body otherwise. Registering a global `RequestValidationError` handler would change every route's 422, which is out of scope.

**Wording** (one line each; the route test pins the exact strings):

```
filters: unknown key 'has_attachments' (did you mean 'has_attachment'?); supported: account_ids, after, before, date_from, date_to, folder_ids, from, has_attachment, lang, subject, to
unknown field 'order'; supported: cursor, filters, limit, query, smart, sort, sort_order
has: expected 'attachment' or 'no-attachment', got 'attachments'
has: 'attachment' and 'no-attachment' contradict each other
```

### 4. Filter validation precedes the empty-ACL short-circuit

Filter values are checked by `_filter_tokens` (dates, `lang`, digit-string ids). Today that runs inside `build_query_string`, after `_scope_filters_by_acl` has returned `None` for a grant-nothing caller. Validation moves ahead of that branch, right after `filter_key_error`.

This slice does **not** restructure `_filter_tokens` into separate validate and emit halves. The early call validates and discards its tokens, and the later `build_query_string` call emits them for the ACL-scoped filters. The duplicated work is a handful of string operations.

### 5. `query` is optional

- `SearchRequest.query: str = ""`.
- The MCP `search` tool's `query` defaults to `""`, and its description loses "prefer `list_messages` for that intent". A filter-only query is a search, not a browse.

A textless query already resolves to the date walk and reports `sort_applied: "date"`, `rankable: false`.

## Compatibility

- **GUI:** sends `query`, `filters` (known keys only), `limit`, `cursor` and `sort`. `has_attachment` is `true` or `null`. It never renders the hit's `has_attachments`. Nothing changes.
- **Kastellan's mail worker:** sends known top-level fields, and forwards planner filters after moving the id lists in. A planner-produced unknown key becomes a 400 whose `detail` names the key and the likely fix; that is the intended outcome. The hit flag becomes trustworthy.
- **Kastellan, separately, from the deploy rather than this slice:** `main` (#324) refuses `sort: "rank"` for a query with no free text, and Kastellan's `plan_sort` sends `"rank"` by default. Filter-only searches need `sort` omitted. The #364 comment should say so.
- **The MCP `search` tool:** gains a default for `query`. A `has_attachment: false` that used to be dropped is now applied, which is what its description already said.

## Testing

The house rules apply. Fixtures are built with `tests/_eml.py` against the seeded test DB, each new refusal gets a positive control beside it, and every wire assertion goes through the real route, since a MagicMock attribute serialises instead of failing.

**The flag:**
- A message with attachments matched through its body or subject reports `true`. A message without reports `false`. Both are checked on the hybrid path and on the date walk, blank query included.
- A differential test over one seeded archive: `has_attachment=true` results equal exactly the unfiltered hits whose flag is `true`, and `false` results equal the rest. That is what "one rule" means, checked by behaviour rather than by grepping for the constant.
- A row with `attachments = '{}'::jsonb` is returned by a blank-query search with `has_attachments: false`, not an error. The test must fail against the bare expression, and that failure is to be observed, not assumed.
- `test_serve_search_route.py`'s wire guard gains `isinstance(hit["has_attachments"], bool)` on every 200 body, catching a fake that leaves the attribute unset.

**`false` and the DSL:**
- `parse_query` for `has:no-attachment`, an unknown `has:` value, and the contradictory pair.
- `_filter_tokens` emits the token.
- End to end over HTTP and over the MCP tool: `false` excludes messages that have attachments.

**Unknown keys:**
- `filter_key_error`: unit cases, including a `null`-valued unknown key and the did-you-mean.
- Route: an unknown filter key gives a 400 with the exact wording; an unknown top-level field gives a 400; the GUI's and Kastellan's real request shapes still give 200.
- A grant-nothing caller with an unknown key, or with a malformed `date_from`, gets a 400, not an empty 200. That test must fail on `main`.
- The model's wire names equal `_SUPPORTED_FILTER_KEYS`.

**Optional `query`:** omitted over HTTP gives a 200 date walk. The MCP tool is called without `query`.

**Plan tests:** `test_searcher_sort_order_plan.py`, `test_api_browse_plan.py` and `test_date_keyset.py` must stay green with the added column, as must the acceptance library `tests/acceptance/browse_explain_lib.py`, which also composes the template. A projected expression cannot change index eligibility, but the template is shared, so they are run rather than argued.

## Documentation

- **README:** the `has:` operator list (`has:no-attachment`), the search request section (unknown keys refused, `query` optional), and the hit field's meaning.
- **`docs/mcp-usage.md`:** `query` is optional.
- **CLAUDE.md:** a short entry under Browse & search pagination covering one presence rule for filter and flag, refusal of unknown keys, the DSL value, and the validation order. The incorrect "content_id only on inline parts" claims are #365's to correct, not this slice's.
- **The `search/query.py` module docstring** lists the new value.

## Files

- **New:** `src/localmail/search/attachment_presence.py`, `tests/test_search_attachment_presence.py`, `tests/test_api_search_filter_keys.py`.
- **Changed:**
  - `src/localmail/search/`: `arms.py`, `date_keyset.py`, `searcher.py`, `query.py`.
  - `src/localmail/api/search.py`, `src/localmail/serve/routes/search.py`, `src/localmail/mcp/server.py`.
  - Tests constructing `SearchResult` directly, which the defaultless field forces.
  - `README.md`, `docs/mcp-usage.md`, `CLAUDE.md`.
