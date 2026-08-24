# Ascending and descending date order for search, with pagination

Date: 2026-08-24
Status: design approved, not yet implemented

## Problem

`POST /v1/search` and the MCP `search` tool accept `sort="rank"|"date"`, and
`"date"` means **newest first** with no way to ask for the opposite. An agent or
a user who wants "my oldest mail about X, in order" cannot express it.

Two consequences follow that are worth stating separately, because the second
is not obvious from the request:

1. There is no ascending order on any read surface.
2. The branch that would serve *"my oldest mail"* — a search with no free text —
   cannot paginate at all. It returns one page and `next_cursor: null`, always.
   Ascending order without that fix is close to useless: it would hand back the
   50 oldest messages and offer no way forward.

## Non-goals

- `GET /v1/messages` (browse) and the MCP `list_messages` tool keep their single
  fixed ordering. They have their own cursor codec, their own two-phase
  NULL-tail top-up, and their own EXPLAIN harness; extending them roughly
  doubles the review surface for no gain to the stated request. Follow-up issue.
- `GET /v1/changes` is forward incremental polling, not a sort. Untouched.
- The desktop GUI gains no control. It never sends the new field, so the server
  default reproduces its current behaviour byte for byte.
- No new migration and no new index. See **SQL**.

## The shape: an orthogonal `sort_order`, not new `sort` members

`sort` keeps its two members. A separate optional `sort_order: "asc"|"desc"`
modifies whichever sort criterion is in force.

Rejected alternative: adding `date_asc`/`date_descending`-style members to
`sort`. That conflates *what we order by* with *which direction*, so a third
criterion (relevance-then-date, sender, size) would double the enum again, and
either the existing `"date"` becomes an alias to carry forever or every current
client breaks. The orthogonal parameter costs one field and generalises.

### The cross-product

An orthogonal parameter makes `sort="rank"` + `sort_order="asc"` expressible.
It is **refused with a 400**.

| `sort` | `sort_order` | outcome |
| --- | --- | --- |
| `rank` (stated or defaulted) | unstated / `desc` | most relevant first — unchanged |
| `rank` (stated or defaulted) | `asc` | **400** |
| `date` | unstated / `desc` | newest first — unchanged |
| `date` | `asc` | oldest first — new |

Refusing rather than honouring is not fastidiousness. The rank path serves a
**bounded candidate pool** — the top-K fused across four arms — so reversing it
yields the least relevant *of the top hits*, not the least relevant mail in the
archive. That is a result which looks meaningful and is an artifact of where the
pool happened to stop. We cannot serve the question honestly, so we decline it.

Refusing rather than *ignoring* it follows the rule this cluster has now been
bitten by twice (#308, #312): a stated parameter the server will not honour is
reported, never silently dropped.

Note the asymmetry is deliberate. `sort_order="desc"` on `rank` is accepted,
because "descending relevance" is exactly what the rank path already serves.
Only `asc` is refused.

A caller who sends `sort_order="asc"` and states no `sort` therefore gets a 400,
since the unstated `sort` resolves to `rank`. The message names the remedy:

> `sort_order='asc' is not applicable to sort='rank' (the default); pass
> sort='date' for oldest-first`

The alternative — letting `sort_order="asc"` *imply* `sort="date"` — was
rejected. It would make `sort_order` change which criterion is used rather than
its direction, which is the orthogonality this design exists to buy.

## One authority for the resolved value

```python
SortOrder = Literal["asc", "desc"]
DEFAULT_SORT_ORDER: SortOrder = "desc"
```

Both live in `search/searcher.py` beside `SortMode` and `DEFAULT_SORT`, and
`api/search.py` imports `DEFAULT_SORT_ORDER` rather than restating `"desc"`.
This is #312's rule applied to the new axis: `Searcher.search` and
`api.search_cursor` both resolve an unstated value, and two layers resolving
"unstated" from two literals **is** the drift.

`Searcher.search` resolves once, at the top of the function:

```python
effective_order: SortOrder = DEFAULT_SORT_ORDER if sort_order is None else sort_order
```

Every read below goes through `effective_order`. A surviving read of the raw
argument is the defect — #312 was exactly that: the raw `None` was recorded as
the pool's sort and then read back as a contradiction, so the next paging
request stating the sort it would actually be served was rejected.

The signature is `sort_order: SortOrder | None = None`, **not** a removed
default. `allowed_account_ids` is keyword-only-with-no-default because no safe
value exists for it (#234); a sort order has one, so the fix is to make
"unstated" spellable, not unspellable.

## The guard is enforced twice

`api/search.py` raises `ValidationFailed` (→ 400) before any work is done, so
the caller gets a clean problem+json.

`Searcher.search` raises a **named `ValueError` subclass** for the same
condition. The CLI and library callers reach the Searcher without passing
through `api/`, so a guard only at the HTTP boundary is not the invariant it
appears to be. A subclass rather than a bare `ValueError` so the api layer can
map exactly this to a 400 without also catching what psycopg, `datetime` and the
embedding backends raise — relabelling a real outage as a caller error. A
`raise`, never an `assert`: asserts vanish under `python -O`.

This is the arrangement `KeysetCursorUnusable` already has, and for the same
reasons.

## The cursor

This is the crux, and the place this feature can silently reintroduce #308.

The keyset cursor is `K|<base64 of ts|id>` and **carries no direction**. Paging
an ascending search the documented way — re-send `next_cursor`, state nothing
else, which is what `docs/mcp-usage.md` tells every client — would resolve the
unstated `sort_order` to `desc` and silently continue in the wrong direction.
The caller sees page 1 of a differently-ordered search wearing a continuation's
clothes: it looks like it worked until the results repeat.

### A second prefix

`KA|` is the ascending keyset cursor. `K|` keeps its current meaning,
descending.

Chosen over encoding the direction inside the base64 payload because that
payload is `api.browse_cursor`'s encoding, which `/v1/messages` shares — a
format change there would reach an endpoint this design explicitly does not
touch. The prefix is also what `is_keyset_cursor` already dispatches on, and
every cursor currently in flight keeps its meaning.

The prefix stays an internal discriminator. Clients treat the cursor as opaque;
it is not API.

### `resolve_cursor_mode` becomes `resolve_cursor_plan`

```python
@dataclass(frozen=True)
class CursorPlan:
    mode: CursorMode          # "fresh" | "pool" | "keyset"
    sort: SortMode
    sort_order: SortOrder
```

The cursor is the authority on **both** axes. A *stated* value contradicting
either raises `ValidationFailed`; an *unstated* one is inherited from the cursor
and never out-votes it.

One function returning both rather than two functions each answering one,
because two predicates for one rule is what produced the #308 follow-up defect
where the api gate and the retrieval branch disagreed about what counted as a
blank query. Minting, matching, and interpreting stay together in
`api/search_cursor.py`, which is the call that module's own docstring already
makes.

The rename lands on `tests/test_api_search_cursor_mode.py`, which drives the
current name.

### The pool cursor

`PoolMetadata` gains `sort_order`, **with no default** — for the reason
`get_pool_metadata` reads `entry["sort"]` and not `entry.get("sort", "rank")`.
A defaulted read makes a pool built one way report itself as built the other,
which is precisely the value `reject_pool_sort_mismatch` then makes a 400/200
call on. A missing key is a bug in whichever `_cache.put` forgot it and belongs
as a loud `KeyError` at the boundary that can still see it.

`reject_pool_sort_mismatch` checks both axes.

Pool cursors are only minted on the rank branch, so a pool carrying
`sort_order="asc"` is unreachable today. It is recorded anyway rather than
assumed, for the reason `PoolMetadata.sort` records a sort that is likewise
always `"rank"`: encoding the invariant in the reader makes a future dispatch
change silently wrong.

## SQL

**No migration and no new index.** Ascending is spelled `ASC NULLS FIRST, id
ASC`, which is the exact reverse of `messages_recent_idx`
(`COALESCE(internal_date, date_sent) DESC NULLS LAST, id DESC`) and is therefore
served by a backward scan.

Measured on the live 128,289-message archive:

| ordering | plan | buffers | time |
| --- | --- | --- | --- |
| `ASC NULLS FIRST, id ASC` | Index Scan Backward | 44 | 0.83 ms |
| `ASC NULLS LAST, id ASC` | Gather Merge, full sort | 33,372 | 42 ms |
| `IS NOT NULL` + `ASC NULLS LAST` | Gather Merge, full sort | 33,372 | 30 ms |
| `IS NOT NULL` + `ASC NULLS FIRST` | Index Scan Backward, `IS NOT NULL` as Index Cond | 44 | 0.09 ms |

The third row refutes the tempting shortcut: restricting to `IS NOT NULL` does
**not** let Postgres treat `NULLS LAST` as equivalent to the index's ordering.
Only the `NULLS FIRST` spelling matches. Write it that way.

The FTS-restricted form behaves the same — the search path already walks
`messages_recent_idx` with the FTS match as a per-tuple filter (the same shape
#72 documents for the ACL filter on browse), and the backward scan serves
ascending identically.

### Where undated messages go

**First**, in ascending order. Ascending is the exact reverse of descending: the
undated tail becomes the undated head, same rows, reversed.

This keeps one ordering, one direction flag and one query, and makes
`asc == reversed(desc)` a testable invariant. The alternative — undated last in
both directions — is not a reversal, and would need the two-phase dated-then-
top-up query `browse.py` carries for #75, plus its own cursor flavour.

The live archive has **0 undated rows of 128,289**, so the cosmetic cost is
theoretical while the code cost is not. Correctness still matters: both date
columns are nullable and archive imports can produce such rows.

### Keyset predicates

> **Correction (review of #322) — the ascending dated predicate below is
> WRONG, and its stated rationale is wrong twice over. Do not implement it.**
> The shipped form is a SQL row comparison, `ROW(expr, m.id) > ROW(%s, %s)`;
> see `searcher._keyset_clause` and the table under "Review round" in #322.
> The OR-form written here is **not** "the more index-friendly of the two" —
> it is the one Postgres refuses to decompose into an index range bound, so
> it plans as a per-tuple `Filter` and every continuation page restarts at
> the head of `messages_recent_idx`. Measured mid-walk on the live archive
> at page ~1250: 62.1 ms / 53,789 buffers / 64,001 rows removed by filter,
> against 0.57 ms / 46 buffers for the row comparison. And #75's cause is
> **not** the `IS NULL` disjunct alone — the plain two-column OR-form has
> the same defect, which is exactly why `tests/test_searcher_sort_order_plan.py`
> keeps it as the `_PRE_FIX_OR_FORM` negative control. What the missing
> disjunct actually buys ascending is that a row comparison becomes
> *available*; it is not itself the optimisation.

```
desc, dated cursor:  expr < ts OR (expr = ts AND id < %s) OR expr IS NULL
desc, NULL cursor:   expr IS NULL AND id < %s

asc,  dated cursor:  expr > ts OR (expr = ts AND id > %s)     <-- SUPERSEDED
asc,  NULL cursor:   (expr IS NULL AND id > %s) OR expr IS NOT NULL
```

The ascending dated predicate needs **no** `OR expr IS NULL` disjunct: under
`NULLS FIRST` the undated block is already behind the cursor, and `NULL > ts` is
not true, so those rows drop out on their own. That absence is what makes the
row comparison `ROW(expr, m.id) > ROW(%s, %s)` *expressible* on this side —
descending must admit the undated tail, and `ROW(NULL, id) < ROW(...)` is NULL,
so it would drop those rows. Descending's disjunct therefore keeps the OR-form
and the `Filter` plan that comes with it: pre-existing, out of scope here, and
filed as **#323**.

## Blank-query pagination

`Searcher.search` has three retrieval branches:

1. `sort="date"` **and** non-blank free text → lexical keyset walk, unbounded.
2. **blank free text** (any sort) → `_list_recent_messages`.
3. the remainder, which is necessarily `sort="rank"` and non-blank text → hybrid
   pool.

Branch 2 returns `search_token=None`, `has_more_in_pool=False` and
`next_keyset=None`, so its `next_cursor` is **always** null. That is the branch
"show me my oldest mail" lands on.

`_list_recent_messages` is `_lexical_date_search` minus the FTS predicate: same
SELECT list, same ORDER BY, same filter composition. They collapse into one
keyset helper taking an optional FTS clause, after which branch 2 mints and
honours cursors in both directions like branch 1.

Blank-query results are date-ordered regardless of `sort` — that is why branch 2
exists, since the hybrid pipeline degenerates for an empty query. The rank+asc
400 still applies uniformly, so a caller wanting oldest-first blank browse
writes `sort="date"`, `sort_order="asc"`, which is honest about what it gets.
Special-casing the blank query out of that rule was rejected: a uniform rule is
predictable, and the exception would be invisible from the wire.

### Consequence: two existing "keyset needs a query" guards must relax

Both branches now read `keyset_cursor`, which contradicts two guards added in
the #308 follow-up:

- `resolve_cursor_mode` rejects a keyset cursor presented with a blank query.
- `Searcher.search` raises `KeysetCursorUnusable` for the same shape.

Both existed because the blank-query branch would have *dropped* the cursor and
answered with its own page 1 — a restart wearing a continuation's clothes. Once
that branch honours the cursor, the premise is gone: the cursor is continued, at
the right position, and the guards would forbid exactly the paging this change
adds. They relax to fire only for the hybrid pool branch (`sort="rank"` with
non-blank text), which is still the one branch that does not read the cursor.

**This does not weaken the #308 property**, because the keyset cursor has never
identified a query — it carries `(ts, id)` and nothing else. Changing
`folder_ids` or the free text between pages is already undefined and already
unvalidated; the contract is, and remains, *"send the cursor back with the same
query and filters"*. Blanking the query is one instance of that, not a new
class. What #308 forbids is the server silently answering a **differently
ordered** question, and ordering is exactly what the cursor does carry — now on
both axes.

The alternative, giving the recent-mail walk its own cursor prefixes so a
lexical cursor replayed blank is a 400, was rejected: it takes the prefix set
from two to four to validate one component of a predicate whose other
components (filters, folder scope) are equally unvalidated and equally capable
of changing the result set. The keyset cursor is a **position in the date
ordering**; the predicate comes from the request.

Three tests assert the old behaviour and are rewritten to assert the new:
`test_api_search_cursor_mode.py::test_keyset_cursor_without_the_original_query_is_rejected`,
`::test_keyset_cursor_without_query_or_sort_is_rejected`, and
`test_searcher_keyset_guard.py::test_an_empty_query_rejects_a_keyset_cursor_instead_of_dropping_it`.
`README.md:888-889` states the old rule in prose and is corrected with them.

## `_date_sort_key` is unreachable

From the branch analysis above, branch 3 is reached only when `sort` is
`"rank"`, so `entry["sort"]` is always `"rank"` and `_build_results`'
`sort="date"` path — and the `_date_sort_key` / `_DATE_SORT_NULL_SENTINEL` pair
it uses — never runs. No test references `_date_sort_key`.

CLAUDE.md's claim that the pool cursor serves *"`sort=date` with an empty
query"* is stale for the same reason: an empty query takes branch 2, which mints
no pool cursor.

**Decision: pin and document, do not delete.** A test asserts that a cached pool
entry always carries `sort="rank"`; a comment on `_date_sort_key` names it
unreachable and points at the branch analysis; the stale CLAUDE.md sentence is
corrected. Nothing is removed. Deleting is the tidier end state but it is not
what this change is for, and the pin is what stops the next reader adding
`sort_order` handling "for symmetry" to code that never runs — which is the
concrete risk (#278 is this codebase's precedent for a declared-but-unserved
surface that four test files made look covered).

No `sort_order` flag is added to `_date_sort_key`.

## Testing

- **The round trip.** An ascending search paged by cursor alone advances rather
  than restarting — the shape of
  `test_api_search_cursor_mode.py::test_paging_a_date_sorted_search_with_the_cursor_alone_advances`,
  which must fail against pre-fix source. This is the #308 regression on the new
  axis and is the single most important test here.
- **The MCP schema's own default.** `mcp/server.py` restates every parameter for
  the agent-facing schema, so a `sort_order="desc"` default written there would
  make agents send it on their own behalf and turn the documented paging call
  into a 400. Assert the published `inputSchema` declares **no** default for
  `sort_order`, reading it off the built server — the shape of
  `test_mcp_server_build.py::test_search_declares_no_sort_default_of_its_own`.
- **The invariant.** Over a seeded corpus, `asc` results equal `reversed(desc)`
  results, undated rows included.
- **The refusal.** `sort="rank"` + `sort_order="asc"` is a 400 at the HTTP
  boundary *and* a named exception from `Searcher.search` reached directly. The
  Searcher test must assert the guard fires before any connection is opened, by
  handing it a pool that raises when touched.
- **Cursor contradiction, both axes.** A `KA|` cursor with a stated
  `sort_order="desc"` is a 400; with `sort_order` unstated it continues
  ascending; a legacy `K|` cursor still continues descending.
- **Blank-query paging.** A blank query pages to a second page in both
  directions, and the pages do not overlap.
- **Plan assertions.** Ascending keeps `Index Scan Backward using
  messages_recent_idx`, with the pre-fix ordering kept as a negative control —
  the role `--predicate-form pre75` plays in `run_browse_explain.py`.
- **Pool unreachability.** A cached pool entry always carries `sort="rank"`.

## Documentation

- `docs/mcp-usage.md`: the tool table gains `sort_order`; the "**Leave `sort`
  unset**" paging instruction becomes "leave `sort` *and* `sort_order` unset",
  since that sentence is what makes the contradiction 400 unreachable for
  well-behaved clients.
- `CLAUDE.md`: the sort/pagination section gains `sort_order`, the `KA|` prefix,
  the measured index table, and the correction to the stale "pool cursor for
  `sort=date` with an empty query" claim.
- `README.md` (lines ~872-908): the cursor-flavour list gains the `KA|`
  ascending keyset cursor and the note that blank-query searches now paginate
  too; the "**leave `sort` unset**" paging instruction becomes "leave `sort` and
  `sort_order` unset".

## Files

| file | change |
| --- | --- |
| `src/localmail/search/searcher.py` | `SortOrder`, `DEFAULT_SORT_ORDER`, resolution, the named guard, direction through the keyset helper, `_list_recent_messages` merged into it, `PoolMetadata.sort_order` |
| `src/localmail/api/search_cursor.py` | `KA\|` prefix, `CursorPlan`, `resolve_cursor_plan`, both-axis mismatch checks |
| `src/localmail/api/search.py` | thread `sort_order`, the 400, `DEFAULT_SORT_ORDER` import |
| `src/localmail/serve/routes/search.py` | request model field |
| `src/localmail/mcp/server.py` | tool param + docstring, no default |
| `tests/…` | as above |
| `docs/mcp-usage.md`, `CLAUDE.md` | as above |

No migration. No new dependency.
