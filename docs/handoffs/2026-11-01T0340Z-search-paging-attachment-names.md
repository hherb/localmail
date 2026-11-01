# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-11-01.** `main` was **`16bc19d`**
> (#424, part of #414, had merged).
>
> **One PR, #427**, on `fix/426-search-paging-attachment-names`:
> `d5a4391` (the #426 work, which landed without a handoff), `e21cf18` (the
> review round) and this handoff. It **does not close #426**: items 4 and 5
> remain, see *What's next*. The PR body must not say `Closes #426`.
>
> **Open issues: 36.**
>
> **Leftover worktree `.claude/worktrees/bottie-bearer-token-dbc203`**
> (detached at `5b85180`). Untouched; ask before removing it.

## Project context (1-minute version)

`localmail` mirrors IMAP accounts (Gmail OAuth or password) into Postgres, and
is **strictly read-only w.r.t. IMAP**. The database is canonical for accounts.

- **Daemon** (IDLE + poll per account), **web admin UI** (HTMX).
- **Hybrid search**, the HTTPS `/v1` server, a remote **MCP server**, and the
  opt-in `--smart` rewriter.
- **Tauri 2 + Svelte 5 GUI** under `gui/`.
- **kastellan** consumes `/v1` over REST with an API key. #426 is its
  re-measured list of search asks (kastellan#947, #948).

## What we shipped

### PR #427 (based on `main` at `16bc19d`), `api_minor` 5

| SHA | What |
|---|---|
| `d5a4391` | #426 items 1–3: paging past a grow, file names searchable (migration `0037`), `fields` gains `attachment_filename` / `attachments` |
| `e21cf18` | review fixes (below) |
| handoff | this file |

- **Item 1, paging.** `grow_pool(page=)` leaves every row already served
  out of the regrown pool and keeps the token; `continue_page` pages from
  the pool's `first_page`. A page below it is a 409.
- **Item 2, file names.** `search/attachment_names.py` + expression GIN
  index `0037`. Arm 1 and the date walk match `fts_v2 @@ q OR names @@ q`.
- **Item 3, hits.** Opt-in `fields`: `attachment_filename` (the file whose
  *text* matched) and `attachments` (every attachment, detail-entry shape).

### The review round (`e21cf18`), each pinned and mutation-checked (11/11)

- **Arm 1 ranked `fts_v2 || names`.** `||` re-checks the 1 MiB tsvector cap
  (lexemes + positions), so one big message made every rank search matching
  it a 500. Ranked apart and summed now.
- **0037 could raise on INSERT** past ~1.2 MB of names (poison pill on every
  sync; migration abort). Name text cut at `NAMES_MAX_CHARS` (50000), in the
  helper **and in 0037**, which was edited rather than superseded.
- **Deep paging under the default config was a 500**: arm 4 asked for
  `hnsw.ef_search` 1200 (pgvector max 1000). Both vector arms clamp.
- **`-acme OR -beta` matched every attachment-less message** through the
  empty names vector; `names_match_sql` turns the names half off for a query
  the empty document satisfies. **File names now exclude a `-word`.**
- **Empty grow → empty page with a cursor**: now grows again up to the cap
  (loop guarded against `candidates_per_arm = 0`). Eviction during the grow
  is a 409.
- `first_page`/`excluded_ids` subscripted (written on the first put);
  `grow_pool`'s `page` has no default. `attachments` keeps a malformed
  entry's slot.

## Verification

| check | result |
|---|---|
| full `pytest -q` | **4366 passed**, 6 warnings (unchanged), 285 s |
| 11 mutations of the review fixes | each caught by its own test |
| CI #427 | both pytest legs pass (3.12, 3.13) |
| plan | date walk keeps `messages_recent_idx` with the keyset `Index Cond` (review agent, EXPLAIN) |

## What's next

### 1. **Before merging #427: check the DGX's `schema_migrations`** (yours)
- 0037 was edited in place. That is only safe if no host applied the old
  text. Mac live DB: stops at `0036` (checked). **DGX: not checked** (the
  classifier refused the read).
- If the DGX has `0037`: `DROP INDEX messages_attachment_names_idx;`
  `DELETE FROM schema_migrations WHERE revision LIKE '0037%';` then
  `init-db` re-applies it.
- Any other test DB built from the first commit needs the same, or
  `test_the_name_match_can_use_the_index` fails.

### 2. **Merge #427, deploy both hosts** (yours)
- `uv sync --extra mcp --extra extraction`, `localmail init-db` (builds the
  0037 index; blocks `messages` writes for its duration — not measured, so
  stop the daemon first or accept the pause), restart `serve`.
- **Acceptance (kastellan's probe, on the DGX):** page `Fee Proposal`,
  `2312 Herb Fee Proposal`, `Qantas booking` for 6 pages each: 0 repeats,
  one token. `"2312 Herb Fee Proposal 230712.pdf"` puts message 18106 at or
  near rank 1, and `230712` finds it. `/v1/version` reports `api_minor` 5.
- Comment on #426 with the results and that 1–3 shipped.

### 3. **Finish #426: items 4 and 5, then close it** (next session)
- **Ask the operator first** whether either is still wanted; the issue
  marks both nice-to-have, and with 2 + 3 shipped item 4 is optional.
- **Item 5, the sender's local time on a hit.** `date_sent` is
  `TIMESTAMPTZ`, so the `Date:` header's offset is gone; the hit says
  `2023-07-11T23:58:24+00:00` for "Wed, 12 Jul 2023, 09:58". Smallest shape:
  an opt-in `fields` name (e.g. `date_local`) rendering the original
  `Date:` header's instant in its own offset, read from the header block
  (`header_block.entries_from_message`, as `?headers=list` does), opt-in
  so the default hit stays byte-identical. A time-zone-aware date *filter*
  is the bigger alternative. Design call for the operator.
- **Item 4, `filters.attachment_name`** (case-insensitive substring of any
  name). If wanted: a structured filter → a new DSL operator → `_filter_sql`
  clause over `jsonb_path_exists(... like_regex ... flag "i")`. Unindexed;
  measure on the 130k Mac archive. Refused-not-dropped rules (#364) apply.
- **Pin kastellan's reliance**: `websearch_to_tsquery('simple','a b OR c d')`
  is `'a' & 'b' | 'c' & 'd'` (checked) but **no test pins it**. One unit
  test in `test_search_websearch_syntax.py`.
- Then a PR with `Closes #426`, and verify with `gh issue view 426`.

### 4. **Carried**
- **Migration slot**: `0037` is now taken. **#319/#318/#320 had planned
  `0037_*.sql`; they need `0038`.**
- **#414 watchdog:** waits for a stack dump.
- **#421** (GUI tokenizer drops quotes on a single quoted word).
- **Tell kastellan** (carried from 69): problem+json 400s, `filters.lang`
  list, web-search syntax (#419), #415 dates; now also `api_minor` 5 and
  the two `fields` names.
- **Dependabot #71 / #185:** left open on purpose.
- The rest of 69's carried list stands.

## Rulings made this session

1. **#427 review findings: all fixed in the PR, none filed** (operator,
   via `/fixall`).
2. **Exclusions vs file names** (operator asked; my recommendation, now
   shipped): guard the names half for empty-satisfiable queries rather than
   parse negations; names exclude `-word`; extracted attachment *text* still
   does not exclude.

## Open decisions & risks

1. **0037 edited in place** — see *What's next* 1. If the DGX applied the old
   text and nobody resets it, the planner cannot use the index there (every
   name match becomes a per-row expression) and nothing errors.
2. **Re-grow loop cost**: a page past the last unseen row now runs up to
   ~log2(max / cpa) retrievals in one request before answering. Bounded, but
   slow on a big archive at the cap.
3. **Pure-negation date-walk latency** (1 ms, measured pre-#426) now also
   evaluates the unindexed names expression per walked row; not re-measured.
4. **`a -b OR c`** still applies no message-level exclusion (#419,
   deliberate); `--smart` synonyms and vector arms can return `b`. Its own
   issue if it matters.
5. **THIS FILE IS NOT THE AUTHORITY; `git` and `gh` are.**
6. **Never run two localmail pytest sessions against one test DB** (port
   5532, same cluster as the live archive).
7. **Restore a mutation from a file copy, never with `git checkout`.**
8. **`SELECT 1 FROM (SELECT expr) x` does not evaluate `expr`** — measure
   with `length(expr)` or similar (cost a wrong calibration this session).
