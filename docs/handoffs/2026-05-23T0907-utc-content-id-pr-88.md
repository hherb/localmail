# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-05-23 (post-session).** PR **#88**
> (`test+docs(messages): cover content_id e2e + refresh stale
> docstring (closes #10 #12)`) **open against `main`** on branch
> `chore/close-10-12-content-id-already-shipped` (head
> `1c5521e`). One commit, +64 / −6, two files. Full pytest suite
> **805 passed** (was 802; +3 new tests in `test_api_messages.py`
> covering `_build_cid_map` and an end-to-end cid-rewrite chain).
> mypy clean on touched files; 4 pre-existing `parser.py` errors
> carry forward unchanged. Awaiting review + merge.
>
> Prior session's PR **#86** (`perf(browse): swap folder-filter
> to EXISTS semi-join (closes #85)`) **merged** to `main` on
> 2026-05-23 as `fa30edc`. Its handoff
> (`docs/handoffs/2026-05-23T0755-utc-exists-semi-join-pr-86.md`)
> was left untracked at the close of that session; it remains
> untracked on `main` and was not brought forward in this PR
> (PR #88 is scoped to the messages/docstring fix). Bring it
> forward in the next housekeeping commit if you want the audit
> trail on `main`.

## Project context (1-minute version)

`localmail` mirrors IMAP accounts (Gmail OAuth, password) into Postgres.
**Strictly read-only with respect to IMAP**. Downstream consumers read
DB + attachment tree directly or via the `localmail serve` HTTPS API.
See [CLAUDE.md](CLAUDE.md), [README.md](README.md), and
[docs/superpowers/specs/2026-05-17-localmail-gui-design.md](docs/superpowers/specs/2026-05-17-localmail-gui-design.md).

This session picked up the carried-forward `#10 / #12`
("persist Content-Id on attachments so inline cid: images
render") items from prior session's "What's next" list.
Discovery: the persistence already shipped in commit
`8e1e829` (2026-05-18, "fix: address open GitHub issues that
are unblocked on main"). The unit tests at each layer (parser,
write_attachments, sanitizer) are green, the JSONB rows
already carry `content_id` for inline parts, and `get_message`
already invokes `_build_cid_map` → `sanitize_html(cid_to_sha=…)`.
What was missing: (a) end-to-end coverage proving the chain
works, (b) a direct unit test for `_build_cid_map`, and (c) a
docstring on `_build_cid_map` that didn't lie. This PR closes
all three gaps and the two stale issues.

## What we shipped this session

### PR #88 — content_id e2e coverage + docstring refresh

Branch: `chore/close-10-12-content-id-already-shipped` (head
`1c5521e`). Single commit. No production behaviour change.

#### Implementation

| SHA | What |
|---|---|
| `1c5521e` | [src/localmail/api/messages.py](src/localmail/api/messages.py): rewrote the docstring on `_build_cid_map` — the prior text claimed `content_id` "is empty in practice today" and pointed at a never-closed issue; new text describes current behaviour (cid → sha map reading `content_id` JSONB field) and notes the unused `headers` kwarg is reserved for future Content-Location fallback. [tests/test_api_messages.py](tests/test_api_messages.py): added `test_build_cid_map_emits_cid_to_sha_for_inline_attachments` (regular-vs-inline filter), `test_build_cid_map_strips_residual_angle_brackets` (defense-in-depth for legacy rows), and `test_get_message_rewrites_cid_img_src_to_attachment_url` (full chain: `parse_message` → `write_attachments` → `get_message` → assert `body_html` contains `/v1/attachments/<sha>` and neither `cid:` nor `src=""`). Reuses the existing `_eml.html_with_inline_image()` fixture and the existing `_seed_msg(db_conn, ...)` helper. |

#### Acceptance — #10 / #12 issue criteria

| # | criterion | status |
|---|---|---|
| #10 (1) | add `content_id: str \| None = None` to `Attachment` | ✅ shipped 8e1e829 |
| #10 (2) | populate from `part.get("Content-Id")` in `_attachments` | ✅ shipped 8e1e829 (with angle-bracket strip) |
| #10 (3) | include `content_id` in `messages.attachments` JSONB | ✅ shipped 8e1e829 |
| #10 (4) | update tests pinning the JSONB shape | ✅ shipped 8e1e829 (unit) + PR #88 (e2e) |
| #12 (1) | persist `content_id` via parser/write_attachments | ✅ shipped 8e1e829 |
| #12 (2) | no migration needed (JSONB additive) | ✅ |
| #12 (3) | backfill optional | ⏸️ deferred (see open-decision #1) |
| #12 (4) | test multipart message with inline image | ✅ shipped 8e1e829 (parser+writer unit tests) + PR #88 (e2e get_message) |

### Test deltas

```
backend pytest:    802 → 805  (+3 in tests/test_api_messages.py:
                               - test_build_cid_map_emits_cid_to_sha_for_inline_attachments
                               - test_build_cid_map_strips_residual_angle_brackets
                               - test_get_message_rewrites_cid_img_src_to_attachment_url)
mypy:              4 pre-existing parser.py errors (unchanged);
                   clean on src/localmail/api/messages.py
```

### Docs updates this session

- **README.md** — unchanged (no user-visible change; the production
  feature was already shipped in 8e1e829).
- **ROADMAP.md** — file does not exist in this repo; no update needed.
- **CLAUDE.md** — unchanged (the content_id chain is already correct;
  no new invariant introduced).
- **`_build_cid_map` docstring** in `src/localmail/api/messages.py` —
  rewrote (was stale by ~5 days).

## What's next

### 1. Merge PR #88

Documentation + test fix, low-risk:

```bash
git checkout main
git pull
git branch -d chore/close-10-12-content-id-already-shipped
# origin branch auto-deleted on merge; issues #10 + #12 auto-closed.
```

### 2. Pick the next piece

In rough order of recommendation:

- **Bring forward the dangling PR-86 handoff to `main`.** The file
  `docs/handoffs/2026-05-23T0755-utc-exists-semi-join-pr-86.md` was
  written at the end of the prior session but never committed before
  the session ended; it remains untracked. Single-commit housekeeping:
  `git add docs/handoffs/2026-05-23T0755-utc-exists-semi-join-pr-86.md
  && git commit -m "docs: archive PR #86 handoff snapshot"`. Either
  bundle into PR #88 (re-open) or do as a quick follow-up PR.
- **Dependabot — 12 vulnerabilities** (1 high / 9 mod / 2 low) on
  `main`. All in `gui/` (10 vite, 1 esbuild, 1 glib). The high-sev
  is vite dev-server (Arbitrary File Read via WebSocket), affects
  developers running `npm run dev` not end-users of the Tauri build.
  Worth a session against the `gui/` subproject — bump vite to the
  Dependabot-recommended version + verify the Tauri build still
  compiles. Blocked on no GUI CI (#18); manual build verification
  needed.
- **#38** `/v1/changes` semantics decision — now that the GUI's
  initial load goes through `/v1/messages` (PR #70), the cursor
  pagination is range-bounded (PR #80), and folder-filter is
  EXISTS-shaped (PR #86), `/v1/changes` is only the delta-fetch
  path. Worth resolving while the change is fresh.
- **#87** CI-gated at-scale regression coverage for the folder-filter
  plan family. Infra-heavy; needs a strategy decision on where the
  harness runs (GitHub Actions PG service? self-hosted runner?).
- **`grow_pool` deep-pagination duplicates on `sort=rank`** —
  carried from PR #70 handoff. When the cache exhausts past pool
  size 100, `grow_pool` returns page 1 of the enlarged pool,
  surfacing already-seen top hits. `sort=date` covers "show me
  everything" so this is lower priority; revisit if rank-paginated
  duplicates become user-visible.
- **#28 / #27 / #24 / #22 / #18 / #17** GUI client polish & CI.
- **#4 / #2 / #5** Search-perf follow-ups (model paths, CONCURRENT
  GIN build, batch INSERT for chunking).
- **#25** `websockets.legacy` DeprecationWarning — still blocked on
  upstream uvicorn release.
- **#47** Third-party transient classes (deferred follow-up to #36).
  Still gated on real ops data.

## Open decisions & risks

1. **Historical-archive backfill for inline images — DEFERRED.**
   Both #10 and #12 noted "backfill is optional" since the feature
   is forward-only-additive: messages synced AFTER 8e1e829 have
   `content_id` on their JSONB rows; messages synced BEFORE that
   date will continue to render with `src=""` for inline images.
   A re-parse sweep is doable but adds substantial scope (parser
   pass over every multipart message in the archive, careful
   JSONB merge to avoid clobbering filenames/sha256). Not pursued
   here; revisit if a user surfaces broken inline images on an
   archive predating 2026-05-18.

2. **`_build_cid_map(attachments, headers)` `headers` kwarg is
   unused but kept.** Considered dropping it as dead surface.
   Kept because the design notes in the sanitizer suggest a future
   Content-Location header fallback (some senders reference inline
   parts by URL rather than `cid:`), and the docstring now
   documents this intent. The unused-arg lint is a non-issue —
   the function signature is internal to `api/messages.py` and
   has exactly one caller.

3. **End-to-end test seeds messages via `_seed_msg` rather than
   the production sync upsert.** The chain under test is `parse →
   write_attachments → get_message → sanitize`; the sync layer
   (`upsert_message`) is exercised separately in `test_sync.py`
   and isn't on the cid-rewrite critical path. Using `_seed_msg`
   keeps the test focused and fast. If a future refactor changes
   how `messages.attachments` is materialized in the row, this
   test could miss a regression — pair it with a small sync-layer
   integration test if that risk materializes.

4. **Carried forward from prior sessions (still load-bearing):**
   - **PR #86** (folder-filter EXISTS semi-join, #85) — merged
     this session start. `build_where(folder_ids=…)` emits
     `WHERE EXISTS (SELECT 1 FROM message_labels …)` in
     [src/localmail/api/browse.py](src/localmail/api/browse.py).
     Do NOT re-introduce `SELECT DISTINCT` + `JOIN message_labels`.
   - **PR #84** (`PR-73 follow-up`) — `TrustedProxies` canonical in
     `src/localmail/api/client_ip.py`; `AuthConfig.model_post_init`
     uses direct PrivateAttr assignment.
   - **PR #83** (`#79` — harness perf + parser fix) —
     `_mid_cursor_from_seed(cfg)` is pure (no `psycopg.Connection`);
     `_scan_actual_rows` parses PG≤17 / PG≥18 output formats.
   - **PR #82** (`#78` — folder-filter plan coverage) —
     eligibility tests in `tests/test_api_browse_plan.py` cover the
     semi-join-shaped browse SQL.
   - **PR #81** (`#77` — canonical browse SQL emitter) —
     `BROWSE_ROW_SQL_TEMPLATE` + `compose_browse_sql` +
     `build_where` are the only authoritative SQL emitter.
   - **PR #80** (`#75` — row-comparison keyset + NULL-tail
     top-up) — mid-keyset browse pagination is range-bounded; do
     NOT rewrite the dated cursor predicate to the equivalent OR
     form.
   - **PR #76** (`messages_recent_idx` planner choice verified).
   - **PR #74** (`Searcher.get_pool_metadata` + `Searcher.config`)
     — use the public accessors, not `_cache` / `_cfg`.
   - `auth.trusted_proxies` (#73) — opt-in CIDR list.
   - Postgres-backed login rate limiter (#7, PR #69).
   - PR #70 (`sort=date` keyset, reranker off-by-default).
   - MIME clamp list (#32), `parse_int_id` (#33), `rrf_k=60` (#35),
     `websockets.legacy` DeprecationWarning (#25, uvicorn blocker).

## Exact commands to resume

```bash
cd /Users/hherb/src/localmail
git fetch origin

# If PR #88 is still open:
git checkout chore/close-10-12-content-id-already-shipped
gh pr view 88                              # check CI + review state

# After PR #88 is merged:
git checkout main
git pull
git branch -d chore/close-10-12-content-id-already-shipped

# Quick sanity:
unset VIRTUAL_ENV && uv sync
unset VIRTUAL_ENV && LOCALMAIL_TEST_DSN='postgresql://localmail:local%40%40mail@localhost:5532/localmail_test' \
  uv run pytest -q                         # expect 805 passed
unset VIRTUAL_ENV && uv run mypy src/localmail    # 4 pre-existing parser.py errors

# Verify the cid-rewrite chain works end-to-end:
unset VIRTUAL_ENV && LOCALMAIL_TEST_DSN='postgresql://localmail:local%40%40mail@localhost:5532/localmail_test' \
  uv run pytest -q tests/test_api_messages.py -k "cid or build_cid"
# Expect: 3 passed.

# Bring forward the dangling PR-86 handoff (housekeeping):
ls docs/handoffs/2026-05-23T0755-utc-exists-semi-join-pr-86.md  # should exist locally
git add docs/handoffs/2026-05-23T0755-utc-exists-semi-join-pr-86.md
git commit -m "docs: archive PR #86 handoff snapshot"

# Pick next piece:
gh issue list --state open --limit 40
# Top candidates: Dependabot triage (12 GUI vulns, 1 high),
# #38 /v1/changes semantics, #87 at-scale CI for folder-filter plan.
```

## Known gotchas (still load-bearing)

- **`unset VIRTUAL_ENV && uv run …`** — shells frequently have a stale
  `VIRTUAL_ENV` pointing at some other pyenv venv.
- **`LOCALMAIL_TEST_DSN` defaults to `postgresql:///localmail_test`** —
  tests must not touch the live `localmail` DB.
- **Migrations 0011–0019 are additive.** Next migration would be
  `0020_*.sql`.
- **Wire `date` = `COALESCE(internal_date, date_sent)`** on every
  paginated list endpoint (PR #70).
- **Two cursor flavours on `/v1/search`** — `"<token>:<page>"` (pool)
  and `"K|<base64>"` (keyset, `sort=date` + non-empty query).
- **Page-cache miss → HTTP 409 `/problems/search-cursor-expired`**,
  never 500.
- **`reranker_enabled` default = False.** CPU-bound cross-encoder
  rerank fanout overruns timeouts when `grow_pool` doubles the pool.
- **`auth.trusted_proxies`** must contain the proxy's CIDR for the
  per-IP login cap. Do NOT also set `uvicorn --forwarded-allow-ips`.
- **`TrustedProxies` alias is canonical in
  `src/localmail/api/client_ip.py`** (PR #84). Do NOT re-introduce
  a local alias definition in `config.py`.
- **Probe-then-condition boundary** (#62) — for any new
  conditional-GET endpoint, the order is
  **ACL+probe → precondition → expensive IO**.
- **Streaming WARNING contract** (#58) — short-read detection via
  `_log_truncation()`.
- **ID-typing boundary** (#33) — routes accept `str`, cast via
  `localmail.api.ids.parse_int_id(...)`.
- **`Searcher` public boundaries** (PR #74) — use
  `searcher.get_pool_metadata(token, *, user_id)` and
  `searcher.config`, never `_cache` / `_cfg`.
- **`messages_recent_idx` planner choice** (#72, PR #76) — the
  planner uses this index for ACL-filtered browse queries at
  production scale across all distribution shapes.
- **Dated-cursor predicate MUST use ROW comparison** (#75, PR #80) —
  `ROW(COALESCE(internal_date, date_sent), m.id) < ROW(%s, %s)`.
- **NULL-tail top-up is conditional** (#75, PR #80) — only runs
  when `cursor is not None AND cursor.ts is not None AND
  len(rows) < fetch_limit`.
- **Canonical browse SQL emitter** (#77, simplified by #85) —
  `BROWSE_ROW_SQL_TEMPLATE` + `compose_browse_sql(where=…)` +
  `build_where` in [src/localmail/api/browse.py](src/localmail/api/browse.py).
- **Folder-filter uses EXISTS semi-join** (#85, PR #86). `build_where
  (folder_ids=…)` emits `EXISTS (SELECT 1 FROM message_labels …)`
  inside the WHERE clause. Do NOT re-introduce `SELECT DISTINCT`
  + `JOIN message_labels`.
- **Folder-filter eligibility tests at fixture scale tolerate Sort
  nodes** (#85, PR #86) — at fixture scale the planner inverts the
  semi-join (starts from `message_labels`, looks up by PK via
  `messages_recent_idx`, then Sorts to restore ORDER BY). This is
  correct; the DISTINCT-regression signature only surfaces at
  scales the acceptance harness covers.
- **NEW from this session: content_id chain is end-to-end covered.**
  `tests/test_api_messages.py::test_get_message_rewrites_cid_img_src_to_attachment_url`
  asserts the full chain (parser → write_attachments → JSONB →
  `_build_cid_map` → `sanitize_html` → sanitized HTML carries
  `/v1/attachments/<sha>`). If any link in the chain regresses,
  that test fails. Do NOT delete the `Attachment.content_id` field,
  the `_content_id` parser helper, the `content_id` JSONB key on
  `write_attachments` rows, the `_build_cid_map` function, or the
  `cid_to_sha=` argument to `sanitize_html` — they are mutually
  load-bearing.
- **NEW from this session: `_build_cid_map` accepts an unused
  `headers` kwarg** reserved for future Content-Location fallback.
  Documented in its docstring. The signature is internal-to-
  `api/messages.py`; if a future change introduces the fallback,
  update the docstring + tests in lockstep.

## File map (as of branch HEAD `1c5521e`)

```
src/localmail/
  api/messages.py                          # MODIFIED (PR #88):
                                            # - docstring rewrite of
                                            #   _build_cid_map; no behaviour
                                            #   change
  api/browse.py                            # unchanged (post-PR #86)
  config.py                                # unchanged (post-PR #84)
  api/client_ip.py                         # unchanged (canonical TrustedProxies)
  parser.py                                # unchanged (Attachment.content_id +
                                            # _content_id() helper present since
                                            # 8e1e829)
  attachments.py                           # unchanged (write_attachments emits
                                            # content_id JSONB since 8e1e829)
  api/sanitize.py                          # unchanged (cid_to_sha rewrite
                                            # parser-aware via nh3
                                            # attribute_filter)
  search/                                  # unchanged
  serve/                                   # unchanged
  cli.py daemon.py worker.py ...           # unchanged
migrations/                                # 0001 … 0019_api_login_attempts.sql
tests/                                     # 805 passing
  test_api_messages.py                     # MODIFIED (PR #88):
                                            # - +3 new tests:
                                            #   test_build_cid_map_emits_cid_to_sha_for_inline_attachments
                                            #   test_build_cid_map_strips_residual_angle_brackets
                                            #   test_get_message_rewrites_cid_img_src_to_attachment_url
                                            # - imports parse_message,
                                            #   write_attachments, _build_cid_map,
                                            #   _eml
  _eml.py                                  # unchanged (html_with_inline_image()
                                            # already present)
  test_parser.py                           # unchanged
  test_attachments.py                      # unchanged
  test_api_sanitize.py                     # unchanged
  acceptance/                              # unchanged
docs/handoffs/
  2026-05-23T0907-utc-content-id-pr-88.md  # this session's snapshot
  2026-05-23T0755-utc-exists-semi-join-pr-86.md  # dangling; untracked on main
  2026-05-23T0308-utc-pr73-followup-pr-84.md     # prior
  2026-05-22T0942-utc-harness-cleanup-pr-83.md   # prior
  2026-05-22T0721-utc-folder-filter-pr-82.md     # prior
  2026-05-22T0406-utc-canonical-browse-sql-pr-81.md   # prior
  2026-05-22T0334-utc-browse-cursor-split-pr-80.md    # prior
NEXT_SESSION.md                            # this file (post-session)
gui/                                       # unchanged
  src-tauri/
  src/
```

End of `chore/close-10-12-content-id-already-shipped` session. PR #88
open against `main` (`1c5521e`), closes #10 + #12. Branch alive on
local + remote until merge. Next: merge #88, optionally bring forward
the dangling PR-86 handoff, then either Dependabot triage (#18-blocked),
#38 (`/v1/changes` semantics), or #87 (at-scale CI for folder-filter
plan).
