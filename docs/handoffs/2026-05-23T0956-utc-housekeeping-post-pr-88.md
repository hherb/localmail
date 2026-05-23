# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-05-23T0956 UTC (post-session).** `main`
> clean, no open PRs. Prior session's PR **#88**
> (`test+docs(messages): cover content_id e2e + refresh stale
> docstring (closes #10 #12)`) **merged** to `main` on
> 2026-05-23 as `4445fc2`. The PR-86 and PR-88 handoff snapshots
> that prior sessions left untracked have been brought forward
> in a single housekeeping commit this session — see `git log -1`
> on `main` (one commit ahead of `origin/main`, **not yet pushed**
> to origin). pytest **805 passed**; mypy unchanged (4 pre-existing
> `parser.py` errors).
>
> No code touched this session — the deliverable is the audit
> trail: `docs/handoffs/2026-05-23T0755-utc-exists-semi-join-pr-86.md`,
> `docs/handoffs/2026-05-23T0907-utc-content-id-pr-88.md`, and
> the post-merge NEXT_SESSION.md update are now on `main`.

## Project context (1-minute version)

`localmail` mirrors IMAP accounts (Gmail OAuth, password) into Postgres.
**Strictly read-only with respect to IMAP**. Downstream consumers read
DB + attachment tree directly or via the `localmail serve` HTTPS API.
See [CLAUDE.md](CLAUDE.md), [README.md](README.md), and
[docs/superpowers/specs/2026-05-17-localmail-gui-design.md](docs/superpowers/specs/2026-05-17-localmail-gui-design.md).

This session picked up two leftover items from prior sessions:
PR #88 was already merged on GitHub but the post-merge
NEXT_SESSION.md update + the dangling PR-86 handoff (untracked
from the session that shipped #86) were never committed. Bringing
them forward keeps the audit trail on `main` so future
investigators don't have to grep working copies for the
operational history.

## What we shipped this session

### Housekeeping commit — post-PR-88 audit trail forward to `main`

Single docs commit on `main` (local-only — push at your
convenience; see `git log -1`). No production code, no migration,
no test changes.

| File | Change |
|---|---|
| [NEXT_SESSION.md](NEXT_SESSION.md) | Rewritten to reflect post-merge state (PR #88 merged; this session = housekeeping). |
| [docs/handoffs/2026-05-23T0755-utc-exists-semi-join-pr-86.md](docs/handoffs/2026-05-23T0755-utc-exists-semi-join-pr-86.md) | Brought forward (was untracked locally since the PR #86 session). |
| [docs/handoffs/2026-05-23T0907-utc-content-id-pr-88.md](docs/handoffs/2026-05-23T0907-utc-content-id-pr-88.md) | Brought forward (was untracked locally since the PR #88 session). |
| [docs/handoffs/2026-05-23T0956-utc-housekeeping-post-pr-88.md](docs/handoffs/2026-05-23T0956-utc-housekeeping-post-pr-88.md) | This session's snapshot (timestamped copy of NEXT_SESSION.md). |

### Verification

```
pytest:  805 passed  (unchanged from end of PR #88 session)
mypy:    4 pre-existing errors in src/localmail/parser.py  (unchanged)
```

No source under `src/localmail/` modified; no behaviour change.

### Docs updates this session

- **README.md** — unchanged (no user-visible change).
- **ROADMAP.md** — file does not exist in this repo; no update needed.
- **CLAUDE.md** — unchanged (no new invariant introduced).

## What's next

### Pick the next piece

In rough order of recommendation, drawn from the menu carried
forward across the last several sessions:

- **Dependabot — 12 vulnerabilities** (1 high / 9 mod / 2 low) on
  `main`. All in `gui/` (10 vite, 1 esbuild, 1 glib). The high-sev
  is vite dev-server (Arbitrary File Read via WebSocket), which
  affects developers running `npm run dev`, not end-users of the
  Tauri build. Worth a session against the `gui/` subproject —
  bump vite to the Dependabot-recommended version + verify the
  Tauri build still compiles. Blocked on no GUI CI (#18); manual
  build verification needed.
- **#38** `/v1/changes` semantics decision — now that the GUI's
  initial load goes through `/v1/messages` (PR #70), the cursor
  pagination is range-bounded (PR #80), and folder-filter is
  EXISTS-shaped (PR #86), `/v1/changes` is only the delta-fetch
  path. The issue body suggests waiting for the GUI to surface
  what the client actually does before deciding; if you have GUI
  telemetry now, this is the time to call it.
- **#87** CI-gated at-scale regression coverage for the
  folder-filter plan family. Infra-heavy; needs a strategy
  decision on where the harness runs (GitHub Actions PG service?
  self-hosted runner?).
- **`grow_pool` deep-pagination duplicates on `sort=rank`** —
  carried from PR #70 handoff. When the cache exhausts past pool
  size 100, `grow_pool` returns page 1 of the enlarged pool,
  surfacing already-seen top hits. `sort=date` covers "show me
  everything" so this is lower priority; revisit if rank-paginated
  duplicates become user-visible.
- **#28 / #27 / #24 / #22 / #18 / #17** GUI client polish & CI.
- **#5 / #4 / #2** Search-perf follow-ups (batch INSERT for
  chunking, model paths, CONCURRENT GIN build).
- **#25** `websockets.legacy` DeprecationWarning — still blocked
  on upstream uvicorn release.
- **#47** Third-party transient classes (deferred follow-up to
  #36). Still gated on real ops data.

## Open decisions & risks

1. **Historical-archive backfill for inline images — STILL
   DEFERRED.** Carried from the PR #88 session: messages synced
   AFTER 8e1e829 (2026-05-18) have `content_id` on their JSONB
   rows; older messages render with `src=""` for inline images.
   A re-parse sweep is doable but adds substantial scope. Not
   pursued; revisit if a user surfaces broken inline images on
   an archive predating 2026-05-18.

2. **`_build_cid_map(attachments, headers)` `headers` kwarg is
   unused but kept.** Reserved for future Content-Location
   header fallback (some senders reference inline parts by URL
   rather than `cid:`). Documented in its docstring as of PR
   #88. The signature is internal to `api/messages.py`; if a
   future change introduces the fallback, update the docstring
   + tests in lockstep.

3. **End-to-end test seeds messages via `_seed_msg` rather than
   the production sync upsert** (PR #88). The chain under test
   is `parse → write_attachments → get_message → sanitize`; the
   sync layer is exercised separately in `test_sync.py`. If a
   future refactor changes how `messages.attachments` is
   materialized in the row, the e2e test could miss a regression
   — pair it with a small sync-layer integration test if that
   risk materializes.

4. **Carried forward from prior sessions (still load-bearing):**
   - **PR #88** (#10/#12 — `content_id` e2e coverage + docstring
     refresh) — `tests/test_api_messages.py` carries
     `test_build_cid_map_emits_cid_to_sha_for_inline_attachments`,
     `test_build_cid_map_strips_residual_angle_brackets`,
     `test_get_message_rewrites_cid_img_src_to_attachment_url`.
     The full cid-rewrite chain (`Attachment.content_id` →
     `_content_id` parser helper → `content_id` JSONB key on
     `write_attachments` rows → `_build_cid_map` → `cid_to_sha=`
     argument to `sanitize_html`) is mutually load-bearing.
   - **PR #86** (folder-filter EXISTS semi-join, #85) —
     `build_where(folder_ids=…)` emits `WHERE EXISTS (SELECT 1
     FROM message_labels …)` in
     [src/localmail/api/browse.py](src/localmail/api/browse.py).
     Do NOT re-introduce `SELECT DISTINCT` + `JOIN
     message_labels`.
   - **PR #84** (`PR-73 follow-up`) — `TrustedProxies` canonical
     in `src/localmail/api/client_ip.py`;
     `AuthConfig.model_post_init` uses direct `PrivateAttr`
     assignment.
   - **PR #83** (`#79` — harness perf + parser fix) —
     `_mid_cursor_from_seed(cfg)` is pure (no
     `psycopg.Connection`); `_scan_actual_rows` parses PG≤17 /
     PG≥18 output formats.
   - **PR #82** (`#78` — folder-filter plan coverage) —
     eligibility tests in `tests/test_api_browse_plan.py` cover
     the semi-join-shaped browse SQL.
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
   - MIME clamp list (#32), `parse_int_id` (#33), `rrf_k=60`
     (#35), `websockets.legacy` DeprecationWarning (#25, uvicorn
     blocker).

## Exact commands to resume

```bash
cd /Users/hherb/src/localmail
git fetch origin
git checkout main
git pull

# Quick sanity:
unset VIRTUAL_ENV && uv sync
unset VIRTUAL_ENV && LOCALMAIL_TEST_DSN='postgresql://localmail:local%40%40mail@localhost:5532/localmail_test' \
  uv run pytest -q                         # expect 805 passed
unset VIRTUAL_ENV && uv run mypy src/localmail    # 4 pre-existing parser.py errors

# Pick next piece:
gh issue list --state open --limit 40
# Top candidates: Dependabot triage (12 GUI vulns, 1 high),
# #38 /v1/changes semantics, #87 at-scale CI for folder-filter plan.

# If picking Dependabot:
cd gui && npm audit
# Bump vite per Dependabot, verify Tauri build still compiles.

# If picking #38:
# Read GUI usage telemetry (if any), then choose one of:
#   (1) keep tail-subscription, point clients at /v1/messages for backfill
#   (2) add min_id query parameter for backward sweep
#   (3) deprecate /v1/changes initial-load branch entirely
```

## Known gotchas (still load-bearing)

- **`unset VIRTUAL_ENV && uv run …`** — shells frequently have a
  stale `VIRTUAL_ENV` pointing at some other pyenv venv.
- **`LOCALMAIL_TEST_DSN` defaults to `postgresql:///localmail_test`** —
  tests must not touch the live `localmail` DB.
- **Migrations 0011–0019 are additive.** Next migration would be
  `0020_*.sql`.
- **Wire `date` = `COALESCE(internal_date, date_sent)`** on every
  paginated list endpoint (PR #70).
- **Two cursor flavours on `/v1/search`** — `"<token>:<page>"`
  (pool) and `"K|<base64>"` (keyset, `sort=date` + non-empty
  query).
- **Page-cache miss → HTTP 409 `/problems/search-cursor-expired`**,
  never 500.
- **`reranker_enabled` default = False.** CPU-bound cross-encoder
  rerank fanout overruns timeouts when `grow_pool` doubles the
  pool.
- **`auth.trusted_proxies`** must contain the proxy's CIDR for
  the per-IP login cap. Do NOT also set `uvicorn
  --forwarded-allow-ips`.
- **`TrustedProxies` alias is canonical in
  `src/localmail/api/client_ip.py`** (PR #84). Do NOT
  re-introduce a local alias definition in `config.py`.
- **Probe-then-condition boundary** (#62) — for any new
  conditional-GET endpoint, the order is **ACL+probe →
  precondition → expensive IO**.
- **Streaming WARNING contract** (#58) — short-read detection
  via `_log_truncation()`.
- **ID-typing boundary** (#33) — routes accept `str`, cast via
  `localmail.api.ids.parse_int_id(...)`.
- **`Searcher` public boundaries** (PR #74) — use
  `searcher.get_pool_metadata(token, *, user_id)` and
  `searcher.config`, never `_cache` / `_cfg`.
- **`messages_recent_idx` planner choice** (#72, PR #76) — the
  planner uses this index for ACL-filtered browse queries at
  production scale across all distribution shapes.
- **Dated-cursor predicate MUST use ROW comparison** (#75, PR
  #80) — `ROW(COALESCE(internal_date, date_sent), m.id) < ROW(%s,
  %s)`.
- **NULL-tail top-up is conditional** (#75, PR #80) — only runs
  when `cursor is not None AND cursor.ts is not None AND
  len(rows) < fetch_limit`.
- **Canonical browse SQL emitter** (#77, simplified by #85) —
  `BROWSE_ROW_SQL_TEMPLATE` + `compose_browse_sql(where=…)` +
  `build_where` in
  [src/localmail/api/browse.py](src/localmail/api/browse.py).
- **Folder-filter uses EXISTS semi-join** (#85, PR #86).
  `build_where(folder_ids=…)` emits `EXISTS (SELECT 1 FROM
  message_labels …)` inside the WHERE clause. Do NOT
  re-introduce `SELECT DISTINCT` + `JOIN message_labels`.
- **Folder-filter eligibility tests at fixture scale tolerate
  Sort nodes** (#85, PR #86) — at fixture scale the planner
  inverts the semi-join (starts from `message_labels`, looks up
  by PK via `messages_recent_idx`, then Sorts to restore ORDER
  BY). This is correct; the DISTINCT-regression signature only
  surfaces at scales the acceptance harness covers.
- **content_id chain is end-to-end covered** (PR #88) —
  `tests/test_api_messages.py::test_get_message_rewrites_cid_img_src_to_attachment_url`
  asserts the full chain. Do NOT delete the
  `Attachment.content_id` field, the `_content_id` parser
  helper, the `content_id` JSONB key on `write_attachments` rows,
  the `_build_cid_map` function, or the `cid_to_sha=` argument
  to `sanitize_html`.

## File map (as of `main` post-housekeeping)

```
src/localmail/                              # unchanged this session
  api/messages.py                          # unchanged (post-PR #88)
  api/browse.py                            # unchanged (post-PR #86)
  config.py                                # unchanged (post-PR #84)
  api/client_ip.py                         # unchanged (canonical TrustedProxies)
  parser.py                                # unchanged
  attachments.py                           # unchanged
  api/sanitize.py                          # unchanged
  search/                                  # unchanged
  serve/                                   # unchanged
  cli.py daemon.py worker.py ...           # unchanged
migrations/                                # 0001 … 0019_api_login_attempts.sql
tests/                                     # 805 passing (unchanged)
docs/handoffs/
  2026-05-23T0956-utc-housekeeping-post-pr-88.md  # THIS session's snapshot
  2026-05-23T0907-utc-content-id-pr-88.md         # NEW on main (brought forward)
  2026-05-23T0755-utc-exists-semi-join-pr-86.md   # NEW on main (brought forward)
  2026-05-23T0308-utc-pr73-followup-pr-84.md      # prior
  2026-05-22T0942-utc-harness-cleanup-pr-83.md    # prior
  2026-05-22T0721-utc-folder-filter-pr-82.md      # prior
  2026-05-22T0406-utc-canonical-browse-sql-pr-81.md   # prior
  2026-05-22T0334-utc-browse-cursor-split-pr-80.md    # prior
NEXT_SESSION.md                            # this file (post-session)
gui/                                       # unchanged
```

End of post-PR-88 housekeeping session. `main` clean, no open
PRs. Next: pick from the menu — Dependabot triage (#18-blocked),
#38 (`/v1/changes` semantics), or #87 (at-scale CI for
folder-filter plan).
