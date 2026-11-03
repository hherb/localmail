# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-11-03 (session 80).** `main` is **`598c401`** (#444:
> #442, closed).
>
> This session has **one open PR** on `fix/440-import-duplicate-failed-rows`.
> Its body says **`Closes #440`**. It carries the #440 code and this handoff.
>
> **Both hosts are deployed at `598c401`**; nothing from the #440 PR is
> deployed.
>
> **Open issues: 34** (33 once the #440 PR merges).
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

### Deploy of `598c401` (#444, #442) to both hosts (no commit)

- **Mac:** `uv sync --frozen --extra mcp --extra extraction` (no change),
  kickstarted `com.localmail.serve` only (imports run in serve).
- **DGX:** pulled → `598c401`, synced (reinstalled `nvidia-cusparselt-cu13`
  again, lock unchanged), restarted `localmail-serve`. Daemon and serve both
  `active`.
- Both `/v1/version`: `api_minor` 6, `build_hash` `598c401`.

### PR on `fix/440-import-duplicate-failed-rows` (based on `main` at `598c401`)

| SHA | What |
|---|---|
| `1544e4d` | #440: a re-run keeps one `failed_messages` row per poison pill |
| handoff | this file + `docs/handoffs/2026-11-03T0245Z-import-duplicate-failed-rows.md` |

- **The defect, plus a worse neighbour found while reading.** The importer's
  uid counter started at `max_label_uid`, and a pill gets no label, so its uid
  was never reserved. (1) A re-run recorded the same bytes under a fresh uid,
  giving a second row (the issue). (2) **A different pill could land on an old
  pill's uid**, and `record_failed_message`'s upsert **overwrote the old pill's
  stored bytes**, losing that record. Reproduced: a run ending on a pill, then
  a second source in the same mailbox opening with a pill.
- **The fix.** `uids.max_reserved_uid(conn, account_id, mailbox_id)` (labels
  **and** failed rows) is the importer's counter base. `runner._failed_uid`
  looks up an existing row for `(account_id, mailbox_id, raw_sha256)` and
  records the pill under that uid, so the upsert bumps `retry_count`.
  `retry_failed_messages` is unchanged: it re-records under the stored uid.
- **No cleanup needed:** 0 duplicate groups on the Mac (1 failed row) and the
  DGX (0 rows). Pre-fix duplicates elsewhere would not be merged.
- New `tests/test_import_failed_rows.py` (2 tests). README (imports
  paragraph) and CLAUDE.md (#439's note, the uid-numbering section) updated.
  No migration.

## Verification

| check | result |
|---|---|
| RED first | both new tests failed (2 rows; old pill's row overwritten) |
| mutation | sha lookup ignored → test 1 fails; counter back to `max_label_uid` → test 2 fails; each only its own |
| `uv run pytest -q` | **4517 passed**, 6 warnings (load ~6–10; another project's pytest running, not localmail's) |
| mypy | Success, 165 files |
| GUI | not run (no GUI change) |
| CI | **check on the PR** (`gh pr checks`) |

## What's next

### 1. **Merge the #440 PR** (yours), once CI is green
- **Acceptance:** `gh pr checks <n>` all pass; after the merge
  `gh issue view 440` is `CLOSED`.

### 2. **Deploy #440 once merged**
- Pure Python, no migration. Imports run in **serve** (and the CLI), so
  restarting serve is enough.
- **Acceptance:** `build_hash` on `/v1/version` equals the merge SHA on both
  hosts; units `active`.

### 3. **Next small slice: pick with the operator**
- Candidates: #373 (the query tokenizer drops an in-word apostrophe:
  `O'Brien` never matches), #369 (silent last-token-wins on conflicting
  scalar operators), #383 (drop the write-only `messages.headers` + GIN in
  `0039`, a one-way door), #365 (inline images counted as attachments;
  product call), #384 (comment or close).
- **Acceptance (any):** a RED test first, then full `uv run pytest -q`
  green and mypy clean.
- **Tell kastellan** (yours; carried): problem+json 400s on `/v1/search`,
  `filters.lang` as a list, #419 syntax, #415 dates; `api_minor` 5 and 6
  features. Both hosts serve `api_minor` 6.

### 4. **Carried**
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

1. **The operator asked for `598c401` on both hosts and picked #440** (over
   #373/#383/#369).

## Open decisions & risks

0. **Since #438, `localmail import` exits 1 on a failed job** (behaviour
   change for scripts; none known on either host). Since #441, a contended
   poison-pill record also fails the job / aborts the sync pass. Since #442,
   an import whose final status write fails on all 5 attempts (~40 s) also
   exits 1, and the row stays `running` until serve restarts. Since #440,
   a re-imported pill bumps its existing `failed_messages` row; duplicates
   made before #440 are not merged (none exist on either host, checked
   2026-11-03).
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
git diff main origin/fix/440-import-duplicate-failed-rows --stat   # empty once merged (squash)
gh pr list --state all --limit 3
gh issue view 440 --json state --jq .state
gh issue list --limit 200 --json number --jq length

# Deploy — Mac (the repo is the install; no migration for #440; serve only)
unset VIRTUAL_ENV && uv sync --frozen --extra mcp --extra extraction
launchctl kickstart -k gui/$(id -u)/com.localmail.serve
curl -sk https://127.0.0.1:8443/v1/version     # api_minor 6, build_hash = merge SHA

# Deploy — DGX
ssh 10.0.0.3 'cd ~/src/localmail && git pull --ff-only && ~/.local/bin/uv sync --frozen --extra mcp --extra extraction && systemctl --user restart localmail-serve && systemctl --user is-active localmail-daemon localmail-serve'
ssh 10.0.0.3 'curl -sk https://10.0.0.3:8443/v1/version'

# Load before trusting any timing; no other LOCALMAIL pytest may be running
# (check the cwd: other projects' pytest runs are harmless).
uptime; pgrep -fl pytest

# Suites.
unset VIRTUAL_ENV && uv run pytest -q              # 4517 at session 80
uv run pytest -q tests/test_import_failed_rows.py tests/test_import_uid_collision.py tests/test_importer_runner.py tests/test_import_terminal_status.py
uvx mypy --python-executable .venv/bin/python src/localmail   # Success, 165 files
cd gui && npm test -- --run && npm run check   # 530 at session 72
```

The latest migration is **`0038_messages_attachment_names_strict_idx.sql`**;
the next free slot is `0039_*.sql`.
