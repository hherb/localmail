# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-08-24 (session 34).** `main` was **`b612f00`** at the
> start; this session opened **one PR** on `fix/keyset-index-cond-and-walk-kind`
> (5 commits) closing **#323** and **#326** and clearing **all four Dependabot
> alerts**.
>
> **This file was FOUR SESSIONS STALE when the session opened.** Its header
> claimed `main` was `eec8e09` with PR #315 awaiting merge; in fact #315, #316,
> #317, #322 and #332 had all landed, open issues had gone 15 → 25, and
> Dependabot 0 → 4. CLAUDE.md *was* current — sessions 30–33 updated it and not
> this file. **Risk 3 is now three-times earned: read `git` and `gh` first,
> always.**
>
> **The DGX is now SEVEN merges behind** (`fb48f23`). Not deployed this session
> — see "What's next", item 0.

## Project context (1-minute version)

`localmail` mirrors IMAP accounts (Gmail OAuth, password) into Postgres,
**strictly read-only w.r.t. IMAP**. The database is canonical for accounts.
Daemon: hot-reload account set, heartbeats, DB command queue, two-plane
supervision. Web admin UI (HTMX): account CRUD, user management, archive
imports, daemon control, API keys. Hybrid search (Phases 1+2) + an HTTPS GUI
server + a remote MCP server (optionally a full OAuth 2.1 authorization server)
+ the opt-in `--smart` LLM query rewriter are all shipped. A Tauri 2 + Svelte 5
GUI lives under `gui/` — read-only viewer plus an admin mode (Accounts + Daemon
panels shipped; Users + Imports still placeholders). Version **0.3.0**, Python
**3.13** pinned (CI matrixes 3.12 + 3.13). Licensed AGPL-3.0-or-later (per-file
SPDX headers in `src/localmail/`; **not** in `gui/`).

## What we shipped this session

One PR, branch `fix/keyset-index-cond-and-walk-kind`, based on `main`.

### `a3325f6` — sqlparse 0.5.5 → 0.6.0 (4 Dependabot alerts, 3 HIGH)

Dollar-quote ReDoS, `TokenList.__init__` CPU DoS, quadratic `group_comments`,
plus a medium string-breakout — all `<= 0.5.5`, all fixed in 0.6.0.

**Nothing attacker-controlled reaches the parser**: `db._split_statements` is
its only consumer and it reads our own numbered migration files. So this is
hygiene, not incident response — which is why it was **verified rather than
assumed**. Splitting all 34 migrations on disk produces byte-identical
statement lists under both versions (probe script diffed before/after);
`tests/test_db_migrations.py` passes unchanged. Floor moved to `>=0.6` with the
reason recorded beside it, the way the `cryptography` floor already is.

### `e8bc08d` — #323: the descending keyset predicate is an Index Cond

#322 fixed the *ascending* half with a row comparison and left descending on
the OR-form — verbatim what this repo's own #75 entry forbids for the browse
path, reintroduced on the search path in newly written code.

**Re-measured this session on the live 128,324-message archive**, mid-walk at
offset 64,000, LIMIT 51:

| predicate | plan | time | buffers | rows filtered |
|---|---|---|---|---|
| OR-form (pre-fix) | `Filter` | **70.383 ms** | 54,230 | 64,001 |
| `ROW(…) < ROW(…)` | `Index Cond` | **0.040 ms** | 48 | 0 |

Linear in scroll depth — invisible on page 1, which is the only page #322's
"no new index" measurements covered.

**Why it could not be a one-line copy of the ascending fix.** Under
`DESC NULLS LAST` the undated block sits *ahead* of the cursor and must be
admitted; `ROW(NULL, id) < ROW(…)` is NULL, so a row comparison drops it. Those
rows now come from a **second top-up statement in the same response** — the
shape `api.browse.list_messages` has used for #75 since before this walk
existed. Ascending needs none of it: its undated block is at the head of the
walk, where its own predicate already reaches it.

The walk therefore issues two queries, so the SQL moved out of the f-string it
was inlined in:

- **`src/localmail/search/date_keyset.py`** (new, pure) — ordering per
  direction, both keyset predicates, `UNDATED_TAIL_ONLY_SQL`,
  `needs_undated_top_up`, and the one `compose_date_keyset_sql` emitter both
  phases go through. The #77 convention one module over.
- **`src/localmail/search/sort_axes.py`** (new, pure) — `SortMode`/`SortOrder`
  and both defaults, because `date_keyset` needs `SortOrder` at runtime for its
  ORDER BY completeness check and defining it twice is #312's drift one level
  down. `searcher.py` imports both, so every existing import path resolves.
- `searcher.py` lost **146 lines** (1360 → 1277).

### `63719ea` + `50f0bb2` — #326: a text-walk cursor needs its query back

#322 taught the blank-query branch to paginate and, its premise gone, dropped
the guard refusing a keyset cursor presented without a query. Removing it was
right as far as it went. What it uncovered is **the single most likely client
mistake there is**: `docs/mcp-usage.md` tells agents to "re-send the same
`query` and filters", and an agent that skipped that line was served the next
`limit` messages of the **entire archive** as a continuation of its text
search. No error, no log line.

- `KeysetCursor.walk` (`"text"` | `"archive"`, **no default**) records which
  walk minted a position. The rule is the pure
  [src/localmail/search/keyset_walk.py](src/localmail/search/keyset_walk.py),
  shaped like `account_names.py::account_name_error`.
- **`_date_keyset_search` derives the branch and the stamp from one
  `walk_for_text` call**, so a cursor cannot claim a walk its query did not
  take. That derivation was the *unpinned* part — a constant `walk=` left every
  mocked test green — which is why end-to-end tests against a seeded archive
  exist now.
- **Only the text-cursor-plus-blank-query pair is refused.** An archive cursor
  continues under any query, so #322's blank-query pagination is untouched —
  pinned by its own positive control, not left to argument.
- Wire: `K|`, `KA|`, `KT|`, `KAT|` (`K` + `A` ascending + `T` text). `K|`/`KA|`
  keep their meanings and read as `archive` (the lenient half — a legacy cursor
  could have come from either walk, and the strict reading would 400 a caller
  correctly paging a blank-query walk). Disjointness is now **asserted**, not
  argued.
- `encode_keyset_cursor` **raises** on an unmapped `(order, walk)` pair instead
  of defaulting — which surfaced **two long-standing test fakes** whose
  auto-`MagicMock` `next_keyset` was being minted into a garbage cursor.
- The follow-up commit is a self-review pass: `resolve_cursor_plan` now decodes
  the whole cursor, which also moves a malformed **payload** ahead of the
  empty-ACL short-circuit. Pinned with a positive control and mutation-proven.

### `8a0bd55` — docs

README (client-facing cursor + paging contract), CLAUDE.md (#323 resolved with
the new measurement; #326 as a sub-entry under #322's relaxation note, which
stays as written; the three new modules in the Layout map; the sqlparse floor's
rationale). **Two addresses corrected in place** — `DEFAULT_SORT` /
`DEFAULT_SORT_ORDER` moved to `sort_axes.py`. **No ROADMAP.md exists**, so that
handoff step remains a no-op.

### Verification (this Mac, all extras)

- `uv run pytest -q` → **2850 passed, 0 skipped** (177 s).
- **Both refs measured in this session** (risk 5): `main` **2806** → branch
  **2850** collected (+44). Main measured in a throwaway worktree, since
  removed.
- `mypy src/localmail` → Success, **152** source files.
- `ruff check src/localmail/` → **10**, unchanged from the documented
  pre-existing baseline (#285). New files clean.
- **Mutation-proven, every new pin**: OR-form restored → 6 fail; top-up deleted
  → 5 (incl. a pre-existing test); `needs_undated_top_up` forced True → 10;
  #326 rule inert → 5; `walk_for_text` stuck → 5; rule broadened to all keyset
  cursors → 8 (incl. #322's own); constant `walk=` stamp → 2 / 1 by flavour;
  ACL ordering regression → 3.
- GUI untouched, so no `npm`/`cargo` run was needed (`git diff --name-only
  main...HEAD | grep ^gui/` is empty).

### The stale NOTIFY queue recurred, and was cleared

The first full run failed **exactly the three** LISTEN/NOTIFY tests with
`could not access status of transaction 959732539 / pg_xact/0393`. **Gate 1 read
healthy (2.86e-06) while gate 2 errored** — the asymmetry session 26 found, so
check both, always. Runbook Option A (`bootout`, verify while down, `bootstrap`)
cleared it: both gates clean with the daemon down, and the re-run is green.
Daemon restarted, pid 88824.

### Host health

**Mac** — daemon running (pid 88824), 7 heartbeats, max age 26 s.
`search-status` **0.92 s**; partition holds:
`blobs_eligible 9530 = 9242 + 106 + 182 + 0`, `claimable 0`. 128,324 messages,
all chunks embedded, `body_lang_pending 0`.
**DGX** — all three units `active`, still at **`fb48f23`** — **seven merges
behind**.

**Dependabot: 0 open alerts** once this merges (was 4). **Open issues: 25**,
dropping to **23**.

## What's next

### 0. **Merge the PR, then deploy — the DGX is seven merges behind**
   **You merge** (project convention). Closes #323 and #326.
   - **Acceptance after merge:** on the live archive, a mid-walk descending
     keyset page EXPLAINs as `Index Cond`, not `Filter` (recipe in the resume
     block); and `search`ing over MCP with a cursor but no `query` returns a
     400 naming `query` rather than unrelated mail.
   - **DGX deploy is the bigger half.** It has missed build provenance (#315),
     the GUI modernisation (#316), API keys (#317, **migration 0036**),
     sort_order (#322/#332) and this PR. It therefore **needs `init-db`**.
     Recipe in the resume block. **Never a bare `uv sync`** — risk 13.
   - The Mac tree is on `fix/keyset-index-cond-and-walk-kind` and its launchd
     daemon runs an editable install (risk 12), so `git checkout main` after
     merging.

### 1. **#324 — the blank-query/`sort="rank"` cursor wart** *(carried, and now the closest neighbour)*
   A blank query is served by the date branch whatever the `sort`, so
   `sort="rank"` is accepted on page 1 and its own cursor rejected on page 2.
   Its surface is wider than "blank query" — the branch predicate runs *after*
   `parse_query`, so `subject:invoice` takes the same path. **Acceptance:** the
   stated `rank` is refused on **page 1**, wire-visibly, with README updated.
   **Do not** "fix" it by having the cursor record the sort the caller *stated*
   rather than the one that ran — a cursor claiming an ordering it did not walk
   is #308 itself.

### 2. **#329 — the intermittent test failures** *(carried; consider it next)*
   `test_serve_search_route.py` fails ~1-in-10 alongside other search/MCP files
   on unmutated source, at base `7f3ae2a` too. Smells like `api_users` fixture
   isolation, related to **#321** (TestClient apps built inline leak their
   pools). **Acceptance:** 20 consecutive runs of the combination, green.
   **Why it outranks its severity:** this repo proves every pin by mutation,
   and intermittent unrelated failures make a real mutation failure
   indistinguishable from noise. It did **not** appear in this session's runs —
   which is consistent with flakiness, not evidence it is gone.

### 3. **The #317 API-keys review round** *(new, carried from session 33)*
   **#319** (`ApiKeyNotFound` carries three meanings; the panel reports a
   security refusal as success) · **#318** (the `api_key_name` ⟺ `is_service`
   pairing is unenforced in the schema — sketch with a generated column + FK is
   in the issue) · **#320** (admin panel routes do blocking DB IO on the event
   loop) · **#321** (TestClient pool leakage).

### 4. **The #322/#332 review round leftovers** *(new)*
   **#327** (`CursorPlan` carries two fields its pool-mode consumer must ignore
   — this session added a comment explaining why a third was *not* added) ·
   **#328** (the page-cache entry is an untyped dict) · **#330**
   (`SortOrder`/`SortMode` restated in three wire layers — **partly touched
   this session**: `sort_axes.py` is now the single definition site, but the MCP
   and HTTP schemas still restate the literals) · **#331**
   (`SortOrderNotApplicable`'s stated audience is wrong).

### 5. **#305 — `--version` dies on a missing third-party dependency** *(carried)*
   `cli.py` imports the daemon at module scope. **Acceptance:** blocking
   `sqlparse` on `sys.meta_path` leaves `localmail --version` printing its line
   and exiting 0. **Do it with the `cli.py` refactor, not before** — `cli.py` is
   still ~2075 lines.

### 6. **#285 — ruff, repo-wide** *(carried)*
   **10** pre-existing errors (two are the `math` import/redefinition pair in
   `searcher.py`), 9 dead `# noqa: S608`, no `[tool.ruff]`, no CI step.
   **Decide the config and the CI step together.**

### 7. **Admin GUI phase 5 — Users & ACL panel** *(carried)*
   `/v1/admin/users` is already `require_admin()` — **no backend work.**
   Mirror [serve/admin/users_panel_router.py](src/localmail/serve/admin/users_panel_router.py),
   follow the Daemon-panel shape, surface the two lock-out guards as 409s, and
   **stub the new API module in both `AdminView.test.ts` and `MainView.test.ts`**
   (risk 11).

### 8. **Remaining robustness backlog** *(carried)*
   **#218** · **#226** · **#225 / #227** · **#200 / #211 / #208** · **#206** ·
   **#204** · **#25**.

### 9. **Smaller, deliberately not done** *(carried)*
   - **165 docling failures on the Mac**, of 182 `blobs_gave_up`.
   - **Residual implausible language labels dominated by `ja`** (~0.24%); the
     confidence-floor lever was measured useless. **Sample `ja` first.**
   - **The DGX drops remain uninvestigated** (risk 7).
   - **`resetpwd.py` is untracked in the working tree** — a one-off admin
     password reset script. Not committed, not in `.gitignore`. Decide.
   - **`git stash drop` the session-22 leftover** if you want it tidy; its
     content is on `main`.
   - **A stray SDD workspace** at `.superpowers/sdd/`. Git-ignored scratch; left
     alone deliberately.

## Open decisions & risks

1. **One PR is open and yours to merge.** `fix/keyset-index-cond-and-walk-kind`,
   5 commits, based on `main` (`b612f00`), closing **#323** and **#326** and
   clearing **all 4 Dependabot alerts**. **25 open issues**, dropping to **23**.
2. **THIS FILE IS NOT THE AUTHORITY — `git` and `gh` are** *(carried, now
   three-times earned)*. Sessions 30–33 shipped five PRs and updated CLAUDE.md
   without touching this file, so it described a world four sessions old.
   Open every session with `git fetch --prune && git log --oneline -1
   origin/main`, `gh pr list`, `gh issue list`, `gh api …/dependabot/alerts`,
   and reconcile **before acting**.
3. **Base every branch on `main`; put a session's code and its handoff in ONE
   PR** *(carried)*. Session 25's stacked handoff PR merged into an
   already-merged branch 13 seconds after it landed and was lost silently.
   - The stranded-branch check is **NOISY**: `git log --oneline main..origin/<b>`
     is non-empty for **every** squash-merged branch (18 of them right now).
     Signal = non-empty on a branch whose PR merged *recently*, confirmed by a
     **content** diff (`git diff --stat main origin/<b>` empty ⇒ it landed).
4. **Verify host revisions; do not infer them** *(carried)*. The DGX is at
   `fb48f23`, seven merges back, and one of those adds **migration 0036** — so
   its deploy needs `init-db`, not just a restart.
5. **Test-count baselines: measure both refs IN THE SAME SESSION** *(carried)*.
   `main` **2806**, branch **2850**. A number quoted from a previous handoff is
   not a baseline. A fresh worktree needs `uv sync --all-extras` first.
6. **A keyset predicate must be a row comparison, in BOTH directions** *(new —
   #75, #322, #323)*. The OR-form is semantically identical, keeps the index
   scan, adds no Sort, and plans as a per-tuple `Filter` that rescans from the
   index head on every page. It has now been written by mistake **twice**.
   `tests/test_searcher_sort_order_plan.py` keeps three negative controls
   (`NULLS LAST`, the ascending OR-form, the descending OR-form) — **do not
   "tidy" them away**; without them the positive assertions are near-tautologies.
7. **The undated block is where the two directions genuinely differ** *(new)*.
   Descending must *admit* it (hence the top-up query); ascending meets it at
   the head of its own walk. The `ts is None` branches keep their shapes in both
   directions — their residual is bounded by the undated row count, not archive
   size. **Do not "restore symmetry"** in either direction, and **do not "fix" a
   short page by restoring `OR expr IS NULL`** — that is the one edit both
   directions' tests exist to catch.
8. **A cursor identifies a position, not a query — with exactly one enforced
   exception** *(new — #326)*. Changing the query or filters between pages
   stays undefined. Dropping the query a **text** cursor was minted from is a
   400. Archive-walk cursors page with or without a query, and that asymmetry
   is what keeps the refusal from reopening #322.
9. **Never state a `sort` on a request that carries a cursor** *(carried —
   #308, #311)*. And a paging client must treat **409 as recoverable** and
   **400 as permanent for that cursor** — the GUI's
   [gui/src/lib/search_paging.ts](gui/src/lib/search_paging.ts) is the rule.
10. **When reverting a mutation, restore from a file copy — never `git
    checkout`** *(carried, sessions 23–34)*. Used ~8 times this session.
    Treat **empty** pytest output as a failed mutation, not a pass.
11. **A no-default field is a sweep, and that is the point** *(new)*. Adding
    `KeysetCursor.walk` with no default broke 22 construction sites, each of
    which had to *decide*. Two of them turned out to be latent fake defects
    (auto-`MagicMock` `next_keyset` minted into garbage cursors). A default
    would have hidden both.
12. **The stale NOTIFY queue recurs, and gate 1 lies** *(carried, recurred this
    session)*. Exactly three LISTEN/NOTIFY failures with a `pg_xact/…` signature
    = stale queue, **not** a code regression and **not** clog corruption.
    **Verify BOTH gates** — gate 1 read 2.86e-06 (healthy) while gate 2 errored.
    Runbook Option A; gate the pytest re-run on the probes, not on a fixed wait.
13. **`uv sync` without extras silently downgrades a host** *(carried)*. A bare
    `uv run` on a venv predating the 3.13 pin rebuilds it **without extras**,
    stripping docling/mcp/extraction. Use `uv sync --all-extras` (Mac) /
    `--extra mcp --extra extraction` (DGX). **`uv` is not on the DGX's
    non-interactive PATH** — use `~/.local/bin/uv`. A non-zero `skipped` count
    means an extra went missing; look for
    `test_extractor.py::test_pdf_pipeline_options_reflect_the_configured_engine`.
    `rapidocr` missing on darwin is **correct** (`ocrmac` is the macOS engine).
14. **The Mac's launchd daemon runs an EDITABLE install** *(carried)*. It
    executes whatever the tree is checked out to. **Currently on
    `fix/keyset-index-cond-and-walk-kind`** — check out `main` after merging.
15. **`search-status` is sub-second** *(carried)*. Mac 0.92 s. If it runs long
    that is a **regression of #280** — check `EXPLAIN (FORMAT JSON)` for a
    `Seq Scan on messages` under a `SubPlan` first.
16. **`blobs_claimable 0` alongside `blobs_pending 0` is the SETTLED shape**
    *(carried)*. A steady non-zero `blobs_no_text` is **normal** (#277);
    **`blobs_gave_up` is the one to act on**. `DISTINCT` in
    `EXTENSION_MATCH_JOIN_SQL` is load-bearing with no runtime guard.
17. **`uv run pytest -q` with NO arguments is the right command** *(carried)* —
    the macOS socket deselect is gone. **Do not run the suite while a backfill
    is draining**, and never two suites in parallel against the same Postgres.
18. **An empty `daemon_heartbeats` right after a restart is normal for minutes**
    *(carried — #269/#271)*: the startup blob-temp sweep. Grep for
    **`blob-temp sweep done: walked=`**.
19. **`--version`'s contract is six things, all pinned** *(carried)*. **Never
    reintroduce `@click.version_option` in any spelling** — an AST pin forbids
    it, covering `daemon_cli.py` too. stderr non-empty ⟺ unresolvable.
20. **`/v1/version` is unauthenticated, so identifiers only** *(carried)*. The
    diagnostic **text** carries errno values and paths and must stay off the
    wire. The exact-key-set assertion in `test_serve_version_route.py` is what
    stops a *future* key leaking — do not relax it to a subset check.
21. **A negative assertion needs the module's own constant and a positive
    control** *(carried)*. Used again this session: the empty-ACL ordering pin
    would pass against a `run_search` that refused *every* cursor without its
    control.
22. **The DGX drops are STILL UNEXPLAINED — five theories refuted** *(carried)*.
    **Do not propose a sixth without a captured outage in which the host was
    demonstrably up throughout.** Triage with `journalctl --list-boots` first.
    Power is not a candidate (~5-day UPS). **Do not edit
    `/etc/wireguard/wg0.conf`.**
23. **CI trap** *(carried)*: any GUI admin panel that fetches on mount MUST be
    stubbed in **both** `AdminView.test.ts` and `MainView.test.ts`, or an
    unhandled promise rejection leaks while vitest still reports "passed".
    **Run vitest from `gui/`, not the repo root.** `cargo clippy --all-targets`
    is clean but **ungated** — CI runs clippy without it.
24. **`type-skipped` is a one-way door for a widened allowlist** *(carried)*.
    **#266's whitespace-heal is a one-way door too** — what makes it safe is the
    `is_blank` gate, not the nature of the data. **`--relabel` is the only
    destructive verb in the lang path**; reach for `--retry-declined` first, and
    do **not** cancel `reopen_all` (it shows no progress until it commits).
25. **`ExtractorConfigurationError` subclasses `TransientExtractorError`, and
    that subclassing *is* the #248 fix — do not "clean it up"** *(carried)*.
26. **`run_embed_worker_once`'s `failure_log` defaults to the process-wide
    `embed_worker._FAILURE_LOG`, and that default is the fix** *(carried —
    #267)*.
27. **Secrets/ACL invariants unchanged** *(carried)*: `secrets.configure`'s pin
    kept though #245 is fixed; #246 warns on group-write; `InsecureSecretsFile`
    refuses; #239's manual tombstone retention deliberate; admin bearer tokens
    have no per-token scope (#204).
28. **No ROADMAP.md** *(carried, re-confirmed)* — that `/nextsession` step is a
    no-op. **README and CLAUDE.md were both updated** this session.

## Exact commands to resume

```bash
cd /Users/hherb/src/localmail
git fetch --prune origin                 # ALWAYS first
git status && git branch --show-current
git log --oneline -1 origin/main         # THE authority, not this file's header
git log --oneline main..origin/main      # non-empty = a session landed since

# RISK 3 — stranded-branch SHORTLIST (noisy: ~18 squash-merged branches appear).
# Only act on a branch whose PR merged recently; confirm with a CONTENT diff:
for b in $(git branch -r --format='%(refname:short)' | grep -v 'origin/main\|HEAD'); do
  n=$(git log --oneline main.."$b" | wc -l); [ "$n" -gt 0 ] && echo "$b: $n commits not on main"
done
git diff --stat main origin/<branch>     # EMPTY = landed, not stranded

# ONE PR is open, awaiting your merge (What's next, 0).
gh pr list
gh pr view --json baseRefName --jq .baseRefName       # MUST be "main"
gh issue list --limit 40                              # 25 open; merge closes #323, #326
gh api repos/hherb/localmail/dependabot/alerts \
  --jq '[.[] | select(.state=="open")] | length'      # 4 now, 0 after merge

# AFTER MERGING — the tree is on fix/keyset-index-cond-and-walk-kind (risk 14):
#   Mac:  git checkout main && git pull && unset VIRTUAL_ENV && uv sync --all-extras
#   DGX (SEVEN merges behind; migration 0036 landed, so init-db is REQUIRED):
#     ssh 10.0.0.3 'cd ~/src/localmail && git pull \
#       && ~/.local/bin/uv sync --extra mcp --extra extraction \
#       && ~/.local/bin/uv run localmail init-db \
#       && systemctl --user restart localmail-daemon localmail-serve'

# ACCEPTANCE for #323 — a deep descending page must be an Index Cond, not a
# Filter. Substitute a real (ts, id) from the OFFSET probe into the second query.
psql -h localhost -p 5532 -U localmail -d localmail -X -c \
 "SELECT COALESCE(internal_date,date_sent) AS ts, id FROM messages
   ORDER BY COALESCE(internal_date,date_sent) DESC NULLS LAST, id DESC
   OFFSET 64000 LIMIT 1"
psql -h localhost -p 5532 -U localmail -d localmail -X -c \
 "EXPLAIN (ANALYZE, BUFFERS) SELECT m.id FROM messages m
   WHERE ROW(COALESCE(m.internal_date,m.date_sent), m.id) < ROW('<ts>', <id>)
   ORDER BY COALESCE(m.internal_date,m.date_sent) DESC NULLS LAST, m.id DESC
   LIMIT 51" | grep -E 'Index Cond|Filter|Execution Time'
#   expect Index Cond + ~0.04 ms; a Filter line naming the date expr is the regression

# Python suite. No --deselect (risk 17). NEVER a bare `uv sync` (risk 13).
unset VIRTUAL_ENV && uv sync --all-extras
unset VIRTUAL_ENV && uv run pytest -q
#   expect 2850 passed, 0 skipped on this branch; 2806 collected on main.
#   MEASURE BOTH REFS IN THIS SESSION (risk 5) — no DB needed:
unset VIRTUAL_ENV && uv run pytest --collect-only -q | tail -2
unset VIRTUAL_ENV && uv run mypy src/localmail    # expect Success, 152 files
unset VIRTUAL_ENV && uv run ruff check src/localmail/ | tail -2   # expect 10 (#285)

# If EXACTLY the three LISTEN/NOTIFY tests fail, it is the stale queue (risk 12).
# CHECK BOTH GATES — this session's recurrence had gate 1 reading healthy:
psql -h localhost -p 5532 -d postgres -U localmail -tAc 'SELECT pg_notification_queue_usage()'
psql -h localhost -p 5532 -d localmail_test -U localmail -c 'LISTEN daemon_commands'
# Remedy (runbook Option A) — verify the gates WHILE the daemon is down:
#   launchctl bootout gui/$UID/com.localmail.daemon
#   until ! launchctl print gui/$UID/com.localmail.daemon >/dev/null 2>&1; do sleep 2; done
#   <re-run both gates; both must be clean>
#   launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.localmail.daemon.plist

# Host health (Mac):
launchctl print gui/$UID/com.localmail.daemon | grep -E '^\s*(state|pid) '
psql -h localhost -p 5532 -U localmail -d localmail -c \
  "SELECT worker_kind, account_id, state, now()-last_heartbeat_at AS age
     FROM daemon_heartbeats ORDER BY worker_kind, account_id"
#   expect 7 rows, ages under ~60 s (EMPTY during the startup sweep — risk 18)

unset VIRTUAL_ENV && uv run localmail search-status    # UNDER A SECOND (risk 15)
#   Check the SHAPE, not the literals: the four buckets must sum to
#   blobs_eligible, and claimable must equal pending.
#   Mac 9530 = 9242 + 106 + 182 + 0, claimable 0

# The DGX — seven merges behind as of this session (risk 4):
ssh 10.0.0.3 'systemctl --user is-active localmail-daemon localmail-serve localmail-wgprobe'
ssh 10.0.0.3 'cd ~/src/localmail && git log --oneline -1'
tail -5 ~/localmail-probe/tunnel-probe.log   # expect lan=ok(3/3)@<addr>

# Frontend — REQUIRED only if you touch gui/ (MUST run from gui/ — risk 23):
cd gui && npm run check && npm test && npm run build && cd ..
cd gui/src-tauri && cargo test && cargo clippy --locked -- -D warnings \
  && cargo clippy --all-targets -- -D warnings && cd ../..
#   expect: 0 svelte-check errors, 408 vitest, 104 cargo
```

`main` tip at session start was **`b612f00`**. This session left **one PR** open
on `fix/keyset-index-cond-and-walk-kind` (5 commits, head `8a0bd55`), closing
**#323** and **#326**. Latest migration **`0036_api_keys.sql`**; next free slot
`0037_*.sql` (this session adds none). **Open issues: 25**, dropping to **23**
on merge. **Dependabot: 4 open alerts, all cleared by this PR.**
