# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-06-03T0910 UTC.**
> A short **measurement-driven** session. Investigated open issue **#5**
> (`search: batch INSERT for chunking loop`) — which itself said *"defer until
> someone actually measures backfill time on a large archive."* We measured,
> the issue's premise didn't hold, and we **resolved it without a production
> code change**. Work is on branch **`search-5-batch-chunk-insert`** (branched
> from `main` at `20ab53f`, the merged 2B.5 follow-ups / PR #150), **pushed**,
> opened as **PR #151** (<https://github.com/hherb/localmail/pull/151>, **open,
> not yet merged**) which **Closes #5** on merge.
>
> **Finding:** the chunking loop is **tokenization-bound** (tiktoken `encode` in
> `chunk_message`), *not* INSERT-round-trip-bound. Batched `executemany` gives
> **no speedup — ~4% slower** on a localhost Postgres. Since localmail is
> **single-host** (Postgres always local), the remote-DB scenario where
> `executemany` would win never applies. So the production chunking loop
> **stays row-by-row**; #5 closes as measured/not-fixed with the evidence
> attached.
>
> **Also cleaned up at session start:** PR #150 (#148 panel toast + #149 close
> orphan-guard) was already **merged** before this session (`20ab53f` on main);
> its stale local + remote branch `daemon-followups-148-149` was deleted, and
> #148/#149 confirmed closed.
>
> Full suite **1223 passed** (+1 net vs merged main), mypy clean (84 files).

## Project context (1-minute version)

`localmail` mirrors IMAP accounts (Gmail OAuth, password) into Postgres,
**strictly read-only w.r.t. IMAP**. The **database is canonical for accounts**
end-to-end. The daemon hot-reloads its account set (2B.1), records per-thread
heartbeats (2B.2), consumes a DB command queue with LISTEN/NOTIFY wake (2B.3),
is supervised + controllable via two planes (2B.4), and has non-blocking
lifecycle control + an admin panel (2B.5). **The 2B arc is complete.** Downstream
consumers read the DB + attachment tree directly or via the `localmail serve`
HTTPS API. See [CLAUDE.md](CLAUDE.md), [README.md](README.md).

## What we did this session (#5)

### Measured before changing anything

- New operator-facing harness
  [tests/acceptance/run_chunk_insert_bench.py](tests/acceptance/run_chunk_insert_bench.py):
  seeds N multi-chunk messages and times the production per-chunk `cur.execute`
  against a batched `cur.executemany` candidate — **both inside the same
  per-message SAVEPOINT** so poison isolation is identical. Both strategies are
  defined **locally in the harness** (the production code carries neither an
  `executemany` form nor a param-builder helper), same pattern as
  `run_browse_explain.py`'s `pre75` variant — a self-contained, reproducible
  measurement.
- **Measurement** (localhost Postgres, 1500 msgs × ~12 chunks ≈ 18k inserts,
  3 isolated runs each):

  | strategy | avg elapsed | throughput |
  |---|---|---|
  | row-by-row (current) | ~20.2s | ~891 chunks/s |
  | executemany (candidate) | ~21.1s | ~853 chunks/s |

  Throughput is ~constant regardless of INSERT strategy → loop is
  **tokenization-bound**, not INSERT-round-trip-bound. `executemany` is ~4%
  *slower* on localhost (per-call batching overhead, no round-trip latency to
  amortise). COPY was never viable (no `ON CONFLICT DO NOTHING`; cross-message
  batching breaks per-message SAVEPOINT isolation).

### Decision: keep row-by-row

- **No production code change. No new migration.** The shipped
  `embed_worker._chunk_messages_lazily` / `_chunk_attachments_lazily` loops are
  unchanged.
- New regression test
  `test_embed_worker.py::test_insert_failure_isolates_poison_message_per_savepoint`
  — existing tests cover `chunk_message()` *raising*; this covers the chunk
  **INSERT itself** failing inside the per-message SAVEPOINT (a NUL-byte chunk
  text → Postgres rejects the INSERT → only that message rolls back; a sibling
  in the same sweep still gets chunks). A guard any future INSERT-batching
  change must keep green.
- CLAUDE.md + README.md record the finding next to the other acceptance
  harnesses / resolved search-perf notes.
- Posted the full measurement as a comment on #5; the PR `Closes #5`.

### Commits on `search-5-batch-chunk-insert` (oldest→newest)

```
4f1e390  perf(search): measure chunk-insert batching; keep row-by-row (#5)
f168f48  docs(readme): list run_chunk_insert_bench.py acceptance harness (#5)
```

### Verification (this session)

- `unset VIRTUAL_ENV && uv run pytest -q tests/` — **1223 passed** (merged
  main's 1222 + 1 new INSERT-isolation regression test; an intermediate
  `executemany` implementation + its unit tests were written TDD-first then
  reverted once the measurement showed no benefit — see git history of the
  branch is squash-clean, only the two commits above remain).
- `unset VIRTUAL_ENV && uv run mypy src/localmail` — **clean, 84 files**.
- The 8 pytest warnings are the known-harmless psycopg pool `__del__`
  ResourceWarnings at interpreter teardown — *not* failures.

## What's next

### 0. **Review & merge PR #151** *(immediate)*

PR #151 (<https://github.com/hherb/localmail/pull/151>) is **open and green**
(1223 passed, mypy clean). It `Closes #5` on merge. After merge:

```bash
gh pr merge 151 --squash --delete-branch
git checkout main && git fetch --prune origin && git merge --ff-only origin/main
git branch -D search-5-batch-chunk-insert
```

### 1. **Pick the next issue or the next feature**

**Open issues after #151 merges (#5 closes): 4** — #25, #47, #90, #125.

- **#90** (glib/Tauri Dependabot, medium) — Rust/Cargo Tauri-stack dependency
  walk (`cargo tree -i glib`, bump `tauri`/`tauri-plugin-*` or
  `[patch.crates-io]`). May be blocked on upstream tauri versions. Externally
  flagged on push.
- **#47** (extract_worker transient classification) — opt docling's third-party
  exception classes (`requests`/`httpx`/`huggingface_hub`/`urllib3`) into
  transient classification so transient extraction failures retry instead of
  being recorded as poison pills. Actionable now, TDD-friendly, self-contained.
- **#25** (websockets depwarn) — *not actionable* until uvicorn ships an
  upstream release on the new `websockets.asyncio` API; only a `filterwarnings`
  band-aid is possible now.
- **#125** (accounts HTML must mint method-bound CSRF) — **stays open until
  2A.3** adopts `csrf_token_for_method` for the account screens (its actual
  subject).

### 2. **Sub-plan 2A.3 — account CRUD admin screens** *(next real feature)*

The 2B arc is done; the remaining open admin-UI work is account-management
screens. Service layer already exists
([src/localmail/api/admin/accounts.py](src/localmail/api/admin/accounts.py):
`list_accounts`, `get_account`, `create_account`, `update_account`,
`delete_account`, `store_password`, `clear_secret`, `probe_connection`) and the
web OAuth flow ([api/admin/oauth.py](src/localmail/api/admin/oauth.py)). 2A.3 is
the HTML UI on top.
- **Reuse, don't reinvent:** mint method-bound CSRF via
  `csrf_token_context().csrf_token_for_method` (closes #125's intent); follow
  the daemon panel's HTMX self-poll / per-button `hx-headers` pattern.
- **CSP gotcha (proven by #148):** any panel JS must be a **served static
  file** (`script-src 'self'`), not inline and not an htmx `hx-on::` handler.
- **Acceptance:** list/create/edit/delete account screens; password + OAuth
  flows wired to the existing service layer + JSON routes; per-control
  method-bound CSRF; TDD; no magic numbers. **No spec/plan yet — brainstorm →
  spec → plan first.**

## Open decisions & risks

1. **#5 is closed by measurement, not by a fix.** The production chunking loop
   is unchanged. If a **remote-DB** deployment ever becomes a thing (contra the
   current single-host design), re-run `run_chunk_insert_bench.py` against that
   DSN — `executemany`'s round-trip savings only materialise under real network
   latency. Do **not** re-open #5 to "implement batching" without first
   re-measuring on the actual target DB.
2. **The benchmark's two strategies live in the harness, not production.** This
   is deliberate (the production code never carried the `executemany` form).
   Keep them there if iterating — don't push an unused `executemany` path into
   `embed_worker.py`.
3. **Per-message poison isolation is load-bearing.** Any future INSERT-batching
   change MUST stay inside the per-message SAVEPOINT (all-or-nothing per
   message) and keep
   `test_insert_failure_isolates_poison_message_per_savepoint` green.
4. **Migration numbering** — latest applied is **0024** (daemon_commands). This
   session added **no** migration. Next free slot: `0025_*.sql`. Re-check
   `ls migrations/` at plan-time.
5. **No ROADMAP.md** in this repo *(carried)* — slice status lives in
   NEXT_SESSION/handoffs + the specs. The `/nextsession` ROADMAP step is a
   no-op here by design.
6. **Heartbeat vocabulary still load-bearing** *(carried)* — any new heartbeat
   call site must use a `worker_kind`/`state` present in both the SQL CHECK
   lists (0023) and the `WorkerKind`/`WorkerState` Literals; all loop
   heartbeats go through `safe_heartbeat`.
7. **Tooling note** *(carried)* — the full-suite run emits harmless psycopg
   pool `__del__` ResourceWarnings at interpreter teardown — *not* a failure
   (`1223 passed`).
8. **`.claude/` local files** stay untracked, by design.

## Exact commands to resume

```bash
cd /Users/hherb/src/localmail
git fetch --prune origin                 # ALWAYS first

git status                               # clean apart from .claude/ local files
git branch -vv                           # main + search-5-batch-chunk-insert (pushed, PR #151)
git --no-pager log --oneline -8
gh issue list --state open --limit 40    # 5 open now; 4 after #5 closes on merge

# Verify state (expect 1223 passed, mypy clean):
unset VIRTUAL_ENV && uv run pytest -q tests/
unset VIRTUAL_ENV && uv run mypy src/localmail
# This session's artifacts specifically:
unset VIRTUAL_ENV && uv run pytest -q tests/test_embed_worker.py
unset VIRTUAL_ENV && PYTHONPATH=src:. uv run python \
    tests/acceptance/run_chunk_insert_bench.py --messages 1500 --mode both
```

After PR #151 merges, pick the next issue (#47 is the most actionable) or start
**2A.3 (account CRUD admin screens)**:

```bash
git checkout main && git pull
git checkout -b admin-ui-2a3-account-screens   # or  fix-47-extract-transient
ls migrations/    # no new migration expected; latest is 0024
# for 2A.3: brainstorm → spec → plan first (routes exist; screen design does not)
```

## File map (this session)

```
NEXT_SESSION.md                                          # REPLACED this session
tests/acceptance/run_chunk_insert_bench.py                # NEW — #5 measurement harness
tests/test_embed_worker.py                                # +1 INSERT-isolation regression test
CLAUDE.md                                                 # +chunk-insert benchmark finding (#5) + harness in layout list
README.md                                                 # +run_chunk_insert_bench.py in acceptance section
docs/handoffs/2026-06-03T0910-utc-issue-5-chunk-insert-measured.md   # frozen snapshot of this file
```

`main` at `20ab53f` (== `origin/main`, merged #150). Branch
`search-5-batch-chunk-insert` **pushed** (== its `origin/` ref), **PR #151
open** (Closes #5). Working tree clean (only `.claude/` local files). 2 local
branches (`main`, `search-5-batch-chunk-insert`); 1 open PR (#151).
**No production code changed this session.**
