# Pagination — Design

**Date**: 2026-05-20
**Status**: Approved, ready for implementation plan
**Related code**: `src/localmail/api/search.py`, `src/localmail/search/searcher.py`,
`src/localmail/serve/routes/{search,changes,messages}.py`,
`gui/src/lib/stores/{mail,search}.svelte.ts`,
`gui/src/components/MessageList.svelte`

## Problem

The GUI's message list — both the recent-mail view and search results — scrolls
to a fixed ceiling and then nothing more is reachable. Concretely:

- **Recent mail**: the GUI bootstraps from `/v1/changes` (which is built for
  forward incremental polling, not browsing). Only the most-recent 200 rows
  are returned, capped at 1000 in-memory; everything older is unreachable.
- **Search**: `/v1/search` returns up to 200 results and hard-codes
  `next_cursor: None`. The underlying `Searcher` already has
  `continue_page(token, page)` + `grow_pool(token, cpa)` + a `PageCache`
  namespaced by `user_id`, but the wire never carries a cursor.

The pagination machinery exists in the backend; it's one wire-up away from
working. The browse path needs a new endpoint because `/v1/changes` is
incremental-poll-shaped.

## Goals

1. The browse list scrolls indefinitely (subject only to total message count).
2. Search results scroll past the first page, reusing the cached rerank pool
   without re-running retrieval.
3. Live updates do not surprise the user mid-scroll.
4. Cursor expiry is recoverable without the user noticing.

## Non-goals

- No virtualised render list (the existing flat `<section>` is fine; the cost
  of one extra `MessageSummary` per row in memory is negligible).
- No numbered pagination UI.
- No `multipart/byteranges` or anything attachment-related.
- No schema changes.

## UX

- **Trigger**: bottom sentinel in `MessageList.svelte`, registered with
  `IntersectionObserver` at `rootMargin: "200px 0px"`. A visible
  "Load more" / "Loading…" / "End of list" button sits at the same point as
  a manual fallback (errors, no observer, accessibility).
- **Live updates while scrolled deep**: a `pendingNewMessages` buffer
  collects poll results; a *"N new messages — click to show"* banner renders
  at the top of the list. Click merges the buffer into the visible list.
  Scroll position never jumps from a background poll.
- **Cursor expiry**: invisible. The GUI transparently re-runs the same query
  with `cursor: null`, drops the first `results.length` rows of the new
  pool, and appends the remainder.

## Backend

### New endpoint: `GET /v1/messages`

Browse-paginated message listing, separate from `/v1/changes` (which stays
incremental-poll-only).

```
GET /v1/messages?account_id=…&account_id=…&folder_id=…&limit=50&cursor=<opaque>
```

- `account_id` and `folder_id` are repeatable query params. `account_ids`
  intersect with the caller's ACL at the service-layer SQL boundary.
- `limit`: 1..200, default 50 (matches `SEARCH_LIMIT_MAX` / `DEFAULT_LIMIT`
  conventions).
- `cursor`: opaque URL-safe base64 of `f"{ts_iso}|{id}"` from the last row
  on the previous page. `null` / absent = first page.

**Response shape** (same `MessageSummary` as `/v1/changes`):

```json
{
  "messages": [
    {"message_id": "...", "subject": "...", "from": {...}, "date": "...",
     "account": {"id": "...", "name": "..."}}
  ],
  "next_cursor": "<opaque>" | null
}
```

**Ordering & cursor predicate**:

```sql
ORDER BY COALESCE(internal_date, date_sent) DESC NULLS LAST, id DESC
```

The wire cursor is URL-safe base64 of one of two payload shapes:
- `"d|<iso-ts>|<id>"` — last row had a non-NULL date.
- `"n|<id>"` — last row had NULL date (we're already in the NULLS-LAST tail).

Keyset predicate, branched by cursor kind:

```sql
-- cursor kind "d" (ts, id) — still in the dated portion:
WHERE (COALESCE(internal_date, date_sent) <  %s::timestamptz)
   OR (COALESCE(internal_date, date_sent) =  %s::timestamptz AND id < %s)
   OR  COALESCE(internal_date, date_sent) IS NULL

-- cursor kind "n" (id) — already in the NULL-date tail:
WHERE COALESCE(internal_date, date_sent) IS NULL AND id < %s
```

Initial page (no cursor) has no predicate beyond filters. Uses the existing
`messages_recent_idx` expression index — no new migration.

**Folder filter**: joins `message_labels` on `message_id`. When both
`account_id` and `folder_id` are present, both apply.

**Service layer**: new file `src/localmail/api/browse.py`:

```python
def list_messages(
    conn: psycopg.Connection,
    *,
    allowed_account_ids: list[int],
    account_ids: list[int] | None = None,
    folder_ids: list[int] | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> dict[str, Any]: ...
```

**Errors**:
- Empty `allowed_account_ids` → `{"messages": [], "next_cursor": null}` (matches `/v1/changes` behaviour).
- Malformed `cursor` → `ValidationFailed` (400 `/problems/validation-failed`).
- `account_id` outside ACL → silently dropped from the intersection.

### Modified endpoint: `POST /v1/search`

- Add `cursor: str | None = None` to `SearchRequest`.
- `run_search` branches on `cursor`:
  - `cursor is None`: call `searcher.search(query, page_size=limit, user_id=user_id, sort=sort)` as today.
  - `cursor is not None`: decode `f"{token}:{page}"`, call
    `searcher.continue_page(token, page, user_id=user.id)`.
- **Transparent pool growth**: when `continue_page` would return a page past
  the pool but `can_grow_pool=True`, the route calls
  `searcher.grow_pool(token, new_cpa)` (doubling until
  `cfg.search.candidates_per_arm_max`, default 800). When the cap is hit,
  the response returns `next_cursor: null`.
- **Cursor expiry**: `CacheMissError` (TTL eviction, LRU eviction, or
  cross-user replay) → HTTP 409
  `{"type": "/problems/search-cursor-expired", "title": "search cursor expired"}`.
- **Wire cursor format**: `f"{token}:{page}"`. Server sets
  `next_cursor = f"{token}:{page+1}"` when `(page * page_size) < pool_size`;
  otherwise `null`.

### Config additions

`SearchConfig` (in `config.py`) gains:

- `candidates_per_arm_max: int = 800` — cap for transparent `grow_pool` growth.

No other config changes.

### What does NOT change

- Schema (no migration).
- `/v1/changes` semantics (still forward-only poll with the safe horizon).
- `Searcher.continue_page` / `grow_pool` internals.
- `PageCache` namespacing by `user_id`.
- The `messages_recent_idx` index.

## GUI

### `gui/src/lib/stores/mail.svelte.ts`

State additions:

```ts
messagesCursor: string | null    // null = either initial OR end-of-list
messagesHasMore: boolean
loadingMore: boolean             // distinct from loadingMessages
pendingNewMessages: MessageSummary[]
```

Method changes:

- Rename `loadRecentMessages()` → `loadInitialMessages(opts?)`. Calls
  `GET /v1/messages` (not `/v1/changes`). Sets `messagesCursor` and
  `messagesHasMore` from the response.
- New `loadMoreMessages()`: fetches with current cursor, appends to
  `messages`, advances cursor. Guarded by `loadingMore` reentrancy flag.
- `pollOnce()` no longer prepends. Dedups against both `messages` and
  `pendingNewMessages`, pushes new items into `pendingNewMessages`.
- New `mergePendingNewMessages()`: prepends the buffer into `messages`,
  clears the buffer.
- **`MAX_RECENT_MESSAGES` dropped on the appended array.** The pending
  buffer keeps a soft cap (500) so an unattended tab can't grow
  unboundedly.

Selection changes (account / folder picked in the tree) reset `messages` and
`cursor`, then call `loadInitialMessages({accountId, folderId})` so the
server applies the filter and pagination traverses the filtered set.

### `gui/src/lib/stores/search.svelte.ts`

State additions:

```ts
cursor: string | null
hasMore: boolean
loadingMore: boolean
```

Method changes:

- `submit()` resets `cursor`/`results`/`hasMore`, fires the request, stores
  `next_cursor`. `hasMore = next_cursor !== null`.
- New `loadMore()`: appends results, advances cursor. The existing
  monotonic `#submitSeq` guard extends to `loadMore` so a slow stale
  response can't pollute a fresh result list.
- **Cursor expiry recovery (transparent)**: on `SEARCH_CURSOR_EXPIRED`,
  `loadMore()` re-submits the same query with `cursor: null`, then
  discards the first `results.length` rows of the new pool and appends
  the remainder. If the new pool is smaller than prior count
  (e.g. filter changed underneath), fall back to a full reset.

### `gui/src/components/MessageList.svelte`

- Bottom sentinel: `<div bind:this={sentinel}>` registered with
  `IntersectionObserver` (`rootMargin: "200px 0px"`). The handler picks
  `search.loadMore()` when `searchActive`, otherwise
  `mail.loadMoreMessages()`.
- Visible "Load more" / "Loading…" / "End of list" button below the
  sentinel. Same handler as the observer; visible fallback.
- Top banner when `mail.snapshot.pendingNewMessages.length > 0`:
  *"N new messages — click to show"*. Click → `mail.mergePendingNewMessages()`,
  banner disappears.

### Tauri layer (`src-tauri/src/commands/`)

- New `list_messages_cmd` → `GET /v1/messages` with
  `{account_ids?, folder_ids?, limit, cursor}`.
- Extend `search_cmd`: forward `cursor` in the request body; parse
  `next_cursor` from the response (TS side already declares `cursor` on
  `SearchRequest`).
- 409 from `/v1/search` surfaces as `SEARCH_CURSOR_EXPIRED` — typed
  error string the TS store recognises, distinct from generic errors.

### What does NOT change

- `MessageListRow.svelte`, `ReadingPane.svelte`, auth/version stores,
  routing layer.
- `MAX_POLL_FAILURES` and the polling backoff.
- Client-side selection filter for already-loaded rows (still applies as
  a render-time filter; the server-side filter on the next fetch makes
  it consistent for new pages).

## Testing

### Backend (pytest, new files)

`tests/test_api_browse.py` — service-layer:
- Initial page returns N rows in `COALESCE(internal_date, date_sent) DESC, id DESC` order.
- Cursor round-trip: feed `next_cursor` into a second call → strictly older, no overlap, no gap.
- Tie-breaking: two rows sharing the same `internal_date` paginate deterministically via the `id` tiebreaker.
- Exhausted cursor → `next_cursor: null`.
- ACL: rows outside `allowed_account_ids` are absent; empty grant list → empty page.
- `account_ids` and `folder_ids` filters scope the result; intersect with ACL.
- Malformed cursor → `ValidationFailed`.
- NULL `internal_date` + `date_sent` rows land at the end and paginate cleanly.

`tests/test_serve_browse.py` — HTTP route:
- 200 with cursor round-trip end-to-end.
- Repeated `account_id` query params parse as a list.
- Caller without a grant → empty messages + `next_cursor: null`.
- 400 on garbage cursor or non-digit `account_id`.

`tests/test_api_search_pagination.py`:
- `cursor=null` → first page + `next_cursor` set when pool > page_size.
- `cursor=<token:2>` returns page 2 from cache; zero retrieval round-trips beyond hydrate.
- Cross-user replay: cursor minted by user A, replayed by user B → 409.
- Pool exhaustion triggers transparent `grow_pool` once; exhausting at `candidates_per_arm_max` → `next_cursor: null`.
- Forced cache eviction (`searcher._cache.invalidate(token)`) → 409 with `/problems/search-cursor-expired`.
- Sort mode preserved across pages (mint with `sort=date`, page 2 stays date-ordered).

### GUI (vitest, extending existing test files)

`gui/src/lib/stores/mail.test.ts`:
- `loadMoreMessages()` appends and advances cursor; `hasMore=false` on null cursor.
- Reentrancy: two concurrent `loadMoreMessages()` only fire one network request.
- Poll fills `pendingNewMessages` instead of prepending; `mergePendingNewMessages()` prepends and clears.
- Switching selection resets cursor + messages and refetches.
- Dedup spans `messages` and `pendingNewMessages`.

`gui/src/lib/stores/search.test.ts`:
- `loadMore()` appends; `#submitSeq` discards stale `loadMore` responses.
- 409 cursor-expired → transparent re-submit + drop-and-append; `results.length` monotonic.
- 409 fallback when re-submitted pool is smaller than prior count → full reset.

`gui/src/components/MessageList.test.ts`:
- Bottom sentinel intersection triggers the right store's `loadMore*()`.
- "Load more" button shares the handler.
- "N new messages" banner appears/disappears correctly.

### Out of scope

- No browser end-to-end test for IntersectionObserver (JSDOM stubs it).
- No load test for deep pagination — keyset is constant-time per page,
  the existing index covers it.

## Risks / open questions

- **Cursor wire format stability**: `f"{token}:{page}"` is opaque to clients
  by contract. If a future change wants richer state (e.g. anchoring at a
  specific row id rather than page-number arithmetic), the format can
  change without API version bump as long as clients treat it as opaque.
- **Cache eviction under load**: with `page_cache_size` (default 100) and
  high concurrent search activity, an LRU eviction mid-scroll triggers
  the 409 recovery path. The transparent re-submit costs the user one
  extra search round-trip; under sustained pressure that compounds. If
  this shows up in practice, raise `page_cache_size` or shorten
  `page_cache_ttl_s` to amortise pool memory differently.
- **Selection filter divergence**: client-side selection filter on already-
  loaded rows could *show* fewer items than the server would return for
  the same filter, if the in-memory list contains rows the server-side
  filter would exclude (e.g. folder membership changed). Acceptable for
  Phase 1; revisit if it surfaces.
