# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-11-02 (session 77, continued).** `main` is
> **`d5f1277`**: #436 (#425) merged before this session, and #425 is closed.
>
> This session has **one open PR, #438**, on `fix/437-import-retry-contention`.
> Its body says **`Closes #437`** and **`Closes #439`**. It carries the #437
> code, a four-reviewer review round, the #439 fix, and this handoff.
>
> **Both hosts are deployed at `d5f1277`**; nothing from #438 is deployed.
>
> **Open issues: 35** (33 once #438 merges: #437 and #439 close).
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
- **kastellan** consumes `/v1` over REST with an API key.

## What we shipped this session

### Deploy of `d5f1277` (#425) to both hosts (no commit)

- **Mac:** `uv sync --frozen --extra mcp --extra extraction` (no change),
  kickstarted `com.localmail.daemon` and `com.localmail.serve`. `/v1/version`
  reports `api_minor` 6, `build_hash` `d5f1277`.
- **DGX:** pulled `3326a75` → `d5f1277`, synced (it reinstalled
  `nvidia-cusparselt-cu13==0.8.1`, lock unchanged), restarted both units.
  `api_minor` 6, `build_hash` `d5f1277`, `NRestarts` 0, daemon `active`.

### PR #438 on `fix/437-import-retry-contention` (based on `main` at `d5f1277`)

| SHA | What |
|---|---|
| `4083794` | #437: importer and `retry-failed` re-raise `db.DB_CONTENTION_ERRORS` |
| `fec3a55` | first handoff (`docs/handoffs/2026-11-01T1243Z-import-retry-contention.md`, frozen) |
| `0c2860e` | review fixes: contention WARNINGs, `import` exits 1 on a failed job, `retry-failed` `ClickException`, three-row + CLI tests, corrected trigger wording |
| `c7fd77b` | #439: a failed import job reports the counters as of the last commit |
| handoff | this file + `docs/handoffs/2026-11-02T0800Z-import-retry-contention-and-counters.md` |

- **The defect.** Two per-message SAVEPOINT handlers recorded
  `QueryCanceled` / `DeadlockDetected` / `LockNotAvailable` in
  `failed_messages` as if the message were malformed:
  `importer/runner.py::run_import` and `sync.py::retry_failed_messages`.
- **The fix**, the sync idiom: after `ROLLBACK TO SAVEPOINT`,
  `if isinstance(exc, DB_CONTENTION_ERRORS): raise`.
  - `run_import` lands in its outer handler, which writes `status='failed'`
    with `error_msg` (`"QueryCanceled: …"`) on a fresh connection. The
    check picks the WARNING (contention or "skipping poison message"), so
    a contended message is no longer reported as poison.
  - `retry_failed_messages` raises out of the command; rows retried earlier
    were committed one by one, so they keep their outcome.
- **Review round** (four reviewers): the first cut's trigger story was
  wrong. These connections carry no timeout, so a wait on the daemon just
  blocks, and an import cannot meet the daemon on a Message-Id (per-account
  dedup; the daemon never syncs archive accounts). Fixed in the comments,
  test docstring, CLAUDE.md and README. Also: a contention WARNING naming
  the message at both sites, `localmail import` exits 1 on a failed job,
  `retry-failed` turns contention into a `ClickException`, and a three-row
  test pins "rows recovered before it stay committed" (moving the commit
  to the end of the loop survived every test before it). Each new pin was
  mutation-checked. The counters defect was filed as #439.
- **#439 (the operator picked it next; same PR, not a stacked branch).** A
  failed job wrote its in-memory counters, but closing the worker
  connection rolls back everything since its last commit, poison pills'
  `failed_messages` rows included. `run_import` now keeps a `committed`
  snapshot, taken after `_flush` and after a new mailbox's
  `upsert_mailbox` commit (which also commits the messages before it), and
  the outer handler writes that. Two tests in
  `tests/test_importer_runner.py`; RED first (`('failed', 2, 1, 0, 1)` vs
  `('failed', 1, 1, 0, 0)`).
- New `tests/test_import_retry_db_contention.py` (11 tests), reusing
  `_raising_trigger` / `_CASES` from `tests/test_worker_db_contention.py`.
- CLAUDE.md gained a #437 bullet beside #425's; README's failed-messages
  section says a lock timeout/deadlock is not counted against the message.
  No migration.

## Verification

| check | result |
|---|---|
| RED first | #437: 6 contention cases failed, 2 controls passed. #439: the counters test failed as above |
| mutation | each re-raise; the per-row retry commit; the CLI catch; `ctx.exit(1)`; each #439 snapshot point and the handler's use of it: every one fails exactly its own test |
| `uv run pytest -q` | **4484 passed**, 6 warnings (load ~15) |
| mypy | Success, 165 files |
| GUI | not run (no GUI change) |
| CI | **check on the PR** (`gh pr checks`) |

## What's next

### 1. **Merge PR #438** (yours), once CI is green
- **Acceptance:** `gh pr checks 438` all pass; after the merge, `gh issue
  view 437` and `gh issue view 439` are both `CLOSED`.

### 2. **Deploy #438 once merged** (low urgency)
- Pure Python, no migration. It only changes `localmail import`,
  admin-panel imports (in-serve) and `retry-failed`, so **restart serve**;
  the daemon runs none of it and needs no restart.
- **Acceptance:** `build_hash` on `/v1/version` equals the merge SHA on both hosts.

### 3. **Tell kastellan** (yours; carried)
- Problem+json 400s on `/v1/search`, `filters.lang` as a list, #419 syntax,
  #415 dates; `api_minor` 5 (no repeats, `attachment_filename` /
  `attachments`); `api_minor` 6 (`filters.attachment_name`, `date_local`).
  Both hosts serve `api_minor` 6.

### 4. **Next small slice: pick with the operator**
- Candidates:
  - #383: `messages.headers` and its GIN index are write-only. Dropping them
    is a one-way door and needs `0039`.
  - #365: images embedded in the HTML are counted as attachments. One edit
    to `HAS_ATTACHMENT_SQL`, but which images count is a product decision.
  - #384: comment or close (2.7 ms warm for 50 prefixes).
  - #369: last-token-wins on scalar operators.
  - #373: an apostrophe inside a word is dropped.
- **Acceptance (any):** a RED test first, then full `uv run pytest -q`
  green and mypy clean.

### 5. **Carried**
- **Migrations:** `0038` is taken. #319/#318/#320 need `0039`.
- **#414 watchdog:** waits for a stack dump (SIGUSR1 works on the DGX).
- **Dependabot #71 / #185:** left open on purpose.
- **docling > 2.144:** probe the next release when it lands.
- **emSign G3:** re-check the trust stores after OS updates.
- **kastellan slice C:** ask whether it is still wanted.
- **Mac `failed_messages`:** one `iso2022_jp` decode failure from
  2026-07-25 (`hherb-dmc/[Gmail]/Important` uid 5263). Not investigated.
- **Issues:** #383, #378, #369, #373, #372, #368, #365; #375; #374 + #305;
  #358/#357; #330/#327/#328; #340, #363, #285; robustness #218, #226,
  #225/#227, #200/#211/#208, #206, #204, #25.

## Rulings made this session

1. **The operator picked #437** and asked for both hosts to be deployed at
   `d5f1277`.
2. **The counters defect was first filed (#439), then fixed here** at the
   operator's pick. It went into #438 rather than a second PR because both
   touch `runner.py`, and the convention is one PR per session.
3. **`localmail import` exits 1 on a failed job** (my call, review round).
   A script that ignored a failed import now sees it. A failed job was
   exit 0 before.
4. **`retry-failed` catches only `DB_CONTENTION_ERRORS`**, not every
   exception; the wider `cli.py` error handling stays with #305.

## Open decisions & risks

0. **The `import` exit-code change is a behaviour change** for any cron or
   script running `localmail import`; none is known on either host.
1. **Repair the pre-#431 `-0000` rows?** Mac: 2,865 of 132,320 rows carry a
   `-0000` Date and are stored 8 h off. None has a NULL `internal_date`, so
   the list/search/changes wire `date` is right; raw `date_sent` readers
   (message-detail `date`, `after:`/`before:`, arm 4's cap, CLI search,
   chunk `Date:` lines) see the skew. The DGX is correct (UTC session). A
   repair should re-parse `raw_bytes`. Read-only count:
   `select count(*) from messages where exists (select 1 from jsonb_array_elements_text(case when jsonb_typeof(headers->'Date')='array' then headers->'Date' else '[]' end) d where d ~ '-0000\s*(\(.*\))?\s*$')`
2. **`attachment_name` is unindexed** (~0.2 s warm at 132k for a filter-only
   miss). A trigram index would be `0039`.
3. **Should a wire widening bump `api_minor`?** Still open.
4. **3,194 rows keep a skewed `internal_date`** (Mac 2,720, DGX 474). Not
   decided: leave them, NULL them, or apply a per-host SQL shift.
5. **THIS FILE IS NOT THE AUTHORITY; `git` and `gh` are.** After a squash
   merge `main..origin/<branch>` is non-empty by construction; compare with
   `git diff main origin/<branch> --stat` (empty = landed).
6. **Never run two localmail pytest sessions against one test DB** (port
   5532, the same cluster as the live archive).
7. **Restore a mutation from a file copy, never with `git checkout`.**
8. **Always sync with `--extra mcp --extra extraction`.** On the DGX use
   `~/.local/bin/uv`. `uv sync --dry-run` is not read-only.
9. **No ROADMAP.md.** That `/nextsession` step is a no-op.
10. **Load on the Mac was 8–15 this session** (other projects). Check
    `uptime` before trusting any timing.

## Exact commands to resume

```bash
cd /Users/hherb/src/localmail
git fetch --prune origin                 # ALWAYS first
git switch main && git pull --ff-only
git log --oneline -1 origin/main         # THE authority
git diff main origin/fix/437-import-retry-contention --stat   # empty once merged (squash)
gh pr list --state all --limit 3
gh issue view 437 --json state --jq .state; gh issue view 439 --json state --jq .state
gh issue list --limit 200 --json number --jq length

# Deploy — Mac (the repo is the install; no migration for #437)
unset VIRTUAL_ENV && uv sync --frozen --extra mcp --extra extraction
launchctl kickstart -k gui/$(id -u)/com.localmail.daemon
launchctl kickstart -k gui/$(id -u)/com.localmail.serve
curl -sk https://127.0.0.1:8443/v1/version     # api_minor 6, build_hash = merge SHA

# Deploy — DGX
ssh 10.0.0.3 'cd ~/src/localmail && git pull --ff-only && ~/.local/bin/uv sync --frozen --extra mcp --extra extraction && systemctl --user restart localmail-daemon localmail-serve'
ssh 10.0.0.3 'curl -sk https://10.0.0.3:8443/v1/version'

# Load before trusting any timing; no other localmail pytest may be running.
uptime; pgrep -fl pytest

# Suites.
unset VIRTUAL_ENV && uv run pytest -q              # 4484 at session 77
uv run pytest -q tests/test_import_retry_db_contention.py tests/test_worker_db_contention.py tests/test_importer_runner.py tests/test_import_uid_collision.py
uvx mypy --python-executable .venv/bin/python src/localmail   # Success, 165 files
cd gui && npm test -- --run && npm run check   # 530 at session 72
```

The latest migration is **`0038_messages_attachment_names_strict_idx.sql`**;
the next free slot is `0039_*.sql`.
