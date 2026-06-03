# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-06-03T (issue #47).**
> Resolved open issue **#47** (`extract_worker: opt third-party transient
> classes into transient classification`). docling pulls OCR models over the
> network and raises third-party exception classes
> (`requests`/`httpx`/`urllib3`/`huggingface_hub`) that are **not** in the
> builtin `ConnectionError`/`TimeoutError` hierarchy, so `_is_transient`
> couldn't recognise them — a model-download blip was recorded as a poison-pill
> in `failed_extractions` even though the next sweep would likely succeed.
>
> **Fix (issue option 1, preferred — keep wrapper knowledge in the wrapper):**
> `DoclingExtractor.extract` now re-raises a `convert()` failure as
> `TransientExtractorError` when its cause/context chain contains a package in
> the new `extractor._TRANSIENT_THIRD_PARTY_MODULES` frozenset. `_is_transient`
> already treats `TransientExtractorError` as transient → ROLLBACK, WARNING, no
> `failed_extractions` row, blob retried next sweep with `retry_count` intact.
> `_TRANSIENT_EXC_TYPES` stays deliberately narrow (the builtin guarantee).
>
> **Refactor:** the duplicated cause/context chain walk (with
> `__suppress_context__` handling + cycle guard) is now a single pure generator
> `extractor.iter_exc_chain`, reused by both `_is_transient` and the new
> `_exc_chain_has_transient_module`.
>
> Work is on branch **`fix-47-extract-transient-thirdparty`** (branched from
> `main` at `1d06d93`, merged #151 / PR #151), **pushed**, opened as **PR #152**
> (<https://github.com/hherb/localmail/pull/152>, **open, not yet merged**)
> which **Closes #47** on merge.
>
> Full suite **1233 passed** (+10 vs merged main's 1223), mypy clean (84 files).

## Project context (1-minute version)

`localmail` mirrors IMAP accounts (Gmail OAuth, password) into Postgres,
**strictly read-only w.r.t. IMAP**. The **database is canonical for accounts**
end-to-end. The daemon hot-reloads its account set (2B.1), records per-thread
heartbeats (2B.2), consumes a DB command queue with LISTEN/NOTIFY wake (2B.3),
is supervised + controllable via two planes (2B.4), and has non-blocking
lifecycle control + an admin panel (2B.5). **The 2B arc is complete.** Downstream
consumers read the DB + attachment tree directly or via the `localmail serve`
HTTPS API. See [CLAUDE.md](CLAUDE.md), [README.md](README.md).

## What we did this session (#47)

### TDD — tests written first, watched fail, then implemented

- **New pure generator** `extractor.iter_exc_chain(exc)` — yields `exc` then
  each exception in its cause/context chain, following `__cause__` first and
  `__context__` only when not suppressed, with a `seen`-set cycle guard.
  Replaces two copies of the same walk.
- **New module-name helper** `extractor._exc_chain_has_transient_module(exc,
  modules=_TRANSIENT_THIRD_PARTY_MODULES)` — True iff any exception in the
  chain belongs to a top-level package in the frozenset (`requests.exceptions`
  → `requests`).
- **`DoclingExtractor.extract`** — the `converter.convert()` `except` now raises
  `TransientExtractorError` when `_exc_chain_has_transient_module(exc)`, else
  the existing `ExtractorError`. (Only `convert()` — the network/model-fetch
  site; `export_to_markdown` is local serialisation, stays permanent.)
- **`extract_worker._is_transient`** — refactored to reuse `iter_exc_chain`;
  behaviour unchanged (still recognises `TransientExtractorError` +
  `_TRANSIENT_EXC_TYPES`). `_TRANSIENT_EXC_TYPES` **deliberately left narrow**.

### Tests added (10, all TDD-first)

- `iter_exc_chain`: self-then-cause ordering; `__context__` fallback; `from
  None` stop.
- `_exc_chain_has_transient_module`: top-level-package match; cause-chain walk;
  unknown-module reject; suppress-context.
- `DoclingExtractor.extract`: direct `requests.exceptions.ConnectionError` →
  `TransientExtractorError`; `huggingface_hub` error in the cause chain →
  transient; `ValueError` stays a permanent `ExtractorError` (regression guard,
  asserts `not isinstance(..., TransientExtractorError)`).

### Docs

- `extract_worker.py` module docstring (transient-classification section now
  cites #36 **+ #47**, explains the wrapper opt-in).
- README transient-policy bullet.
- CLAUDE.md Phase 2 notes bullet (#47).

### Commit on `fix-47-extract-transient-thirdparty`

```
2647c1e  fix(extract): classify docling third-party network errors as transient (#47)
```

### Verification (this session)

- `unset VIRTUAL_ENV && uv run pytest -q tests/` — **1233 passed** (merged
  main's 1223 + 10 new). The 6 pytest warnings are the known-harmless psycopg
  pool `__del__` ResourceWarnings at interpreter teardown — *not* failures.
- `unset VIRTUAL_ENV && uv run mypy src/localmail` — **clean, 84 files**.

## What's next

### 0. **Review & merge PR #152** *(immediate)*

PR #152 (<https://github.com/hherb/localmail/pull/152>) is **open and green**
(1233 passed, mypy clean). It `Closes #47` on merge. After merge:

```bash
gh pr merge 152 --squash --delete-branch
git checkout main && git fetch --prune origin && git merge --ff-only origin/main
git branch -D fix-47-extract-transient-thirdparty
```

### 1. **Pick the next issue or the next feature**

**Open issues after #152 merges (#47 closes): 3** — #25, #90, #125.

- **#90** (glib/Tauri Dependabot, medium) — Rust/Cargo Tauri-stack dependency
  walk (`cargo tree -i glib`, bump `tauri`/`tauri-plugin-*` or
  `[patch.crates-io]`). May be blocked on upstream tauri versions. Externally
  flagged on push. *(Note: GitHub also reports 2 critical Dependabot alerts on
  the default branch — surfaced during this session's `git push`; worth a
  triage pass at `/security/dependabot`.)*
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

1. **#47 changes only the docling wrapper's classification, not the worker's
   policy.** `_is_transient` and `_TRANSIENT_EXC_TYPES` are unchanged in spirit
   (the latter is still narrow on purpose). To add a newly-observed transient
   package, **extend `extractor._TRANSIENT_THIRD_PARTY_MODULES`** — never widen
   the builtin `_TRANSIENT_EXC_TYPES` (that risks mis-classifying permanent
   `ENOENT`/`EACCES`).
2. **`export_to_markdown` failures stay permanent.** Only `convert()` (the
   network/model-fetch site) gets the transient opt-in. If a future docling
   version moves network IO into the export step, revisit.
3. **No new failure-recording table.** #47 reuses the existing transient path
   (ROLLBACK + WARNING, no `failed_extractions` row). There is still no
   `failed_attachment_chunkings` table (intentional Phase 2 scope).
4. **Migration numbering** — latest applied is **0024** (daemon_commands). This
   session added **no** migration. Next free slot: `0025_*.sql`. Re-check
   `ls migrations/` at plan-time.
5. **Dependabot** — `git push` reported **2 critical** vulnerabilities on the
   default branch (plus the standing medium #90 glib alert). Triage at
   <https://github.com/hherb/localmail/security/dependabot> before the next
   feature; the 2 critical ones are new since the #5 session.
6. **No ROADMAP.md** in this repo *(carried)* — slice status lives in
   NEXT_SESSION/handoffs + the specs. The `/nextsession` ROADMAP step is a
   no-op here by design.
7. **Heartbeat vocabulary still load-bearing** *(carried)* — any new heartbeat
   call site must use a `worker_kind`/`state` present in both the SQL CHECK
   lists (0023) and the `WorkerKind`/`WorkerState` Literals; all loop
   heartbeats go through `safe_heartbeat`.
8. **Tooling note** *(carried)* — the full-suite run emits harmless psycopg
   pool `__del__` ResourceWarnings at interpreter teardown — *not* a failure
   (`1233 passed`).
9. **`.claude/` local files** stay untracked, by design.

## Exact commands to resume

```bash
cd /Users/hherb/src/localmail
git fetch --prune origin                 # ALWAYS first

git status                               # clean apart from .claude/ local files
git branch -vv                           # main + fix-47-extract-transient-thirdparty (pushed, PR #152)
git --no-pager log --oneline -8
gh issue list --state open --limit 40    # 4 open now; 3 after #47 closes on merge

# Verify state (expect 1233 passed, mypy clean):
unset VIRTUAL_ENV && uv run pytest -q tests/
unset VIRTUAL_ENV && uv run mypy src/localmail
# This session's artifacts specifically:
unset VIRTUAL_ENV && uv run pytest -q tests/test_extractor.py tests/test_extract_worker.py
```

After PR #152 merges, pick the next issue (triage the 2 critical Dependabot
alerts + #90) or start **2A.3 (account CRUD admin screens)**:

```bash
git checkout main && git pull
git checkout -b admin-ui-2a3-account-screens   # or  deps-dependabot-triage
ls migrations/    # no new migration expected; latest is 0024
# for 2A.3: brainstorm → spec → plan first (routes exist; screen design does not)
```

## File map (this session)

```
NEXT_SESSION.md                                          # REPLACED this session
src/localmail/search/extractor.py                         # +iter_exc_chain, +_TRANSIENT_THIRD_PARTY_MODULES, +_exc_chain_has_transient_module; DoclingExtractor.extract transient opt-in
src/localmail/search/extract_worker.py                    # _is_transient reuses iter_exc_chain; docstring +#47
tests/test_extractor.py                                   # +10 tests (iter_exc_chain, module helper, docling transient)
README.md                                                 # +docling third-party transient policy bullet
CLAUDE.md                                                 # +Phase 2 note (#47)
docs/handoffs/2026-06-03T0929-utc-issue-47-extract-transient.md   # frozen snapshot of this file
```

`main` at `1d06d93` (== `origin/main`, merged #151). Branch
`fix-47-extract-transient-thirdparty` **pushed** (== its `origin/` ref),
**PR #152 open** (Closes #47). Working tree clean (only `.claude/` local files).
2 local branches (`main`, `fix-47-extract-transient-thirdparty`); 1 open PR
(#152). **No migration changed this session.**
