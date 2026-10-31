# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-10-31 (session 67).** `main` was **`cf002e8`** at
> session start. #418 (closes #371) and #420 (#419, web-search syntax in the
> query) had both merged since the last handoff. #420 left no handoff, and it
> filed #421.
>
> This session opened **one PR** on `fix/415-internaldate-utc`. It has one
> code commit (`f8c9d21`), closes **#415**, and also carries this handoff.
>
> **Open issues: 36** (35 once the PR merges).
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
- Version **0.3.0**, Python **3.13** pinned (CI tests 3.12 + 3.13),
  AGPL-3.0-or-later.
- **kastellan** consumes `/v1` over REST with an API key. Only slice C
  remains.

## What we shipped this session

### PR on `fix/415-internaldate-utc` (based on `main` at `cf002e8`)

| SHA | What |
|---|---|
| `f8c9d21` | INTERNALDATE stored as the server-reported instant; `backfill-internal-date --refetch` repairs old rows; the command now reads DB accounts |
| handoff | this file + `docs/handoffs/2026-10-31T0744Z-internaldate-utc.md` |

**The defect, measured on both hosts (not only the DGX, as the issue says):**

- imapclient's default `normalise_times=True` returns INTERNALDATE as a
  **naive host-local** datetime.
- psycopg sends a naive value as `timestamp`, and Postgres reads it in the
  **session** zone. The error is the host offset minus the session offset.
- **DGX:** the host is in Sydney and the `localmail-pg` container runs UTC,
  so the error is +10/+11 h.
- **Mac:** the host is in Brisbane (+10) and Postgres.app's default zone is
  Perth (+8).
  - Of the live-account rows, 87,414 are +2 h, 32,070 are at 0 and 4,707
    are +1.5 h.
  - The host's zone has changed over time, so **no SQL offset can repair
    it.**
  - That is why the repair re-fetches from IMAP (operator ruling).

**The fix:**

- `imap_client.open_connection` sets `client.normalise_times = False`.
- The pure `src/localmail/imap_dates.py::aware_internal_date` is used by
  sync and backfill. It passes an aware datetime through, and turns a naive
  value or a non-datetime into `None`. Sync stores that as NULL (search
  falls back to `date_sent`) and logs one WARNING per pass; backfill leaves
  the row as it was and counts it as `unusable`.
- `backfill_internal_date(..., refetch=False)` now returns a
  `BackfillResult(scanned, updated, missing, unusable, skipped_mailboxes)`
  dataclass (the last two from the review round, below).
  - **`refetch=True`** asks about every row and overwrites it
    `WHERE internal_date IS DISTINCT FROM`, so `updated` counts only real
    changes.
  - A message in several mailboxes gets the **earliest** INTERNALDATE.
  - A message the server no longer has keeps its old value and is counted
    as `missing`; one it returned without a usable date, as `unusable`.
  - A mailbox it cannot read is reported and the CLI exits 1. Its messages
    are in no count.
- **The CLI** `backfill-internal-date [--account N] [--refetch]` now
  resolves DB accounts through `cli._live_account_rows`, shared with
  `sync`.
  - It used to be TOML-only, and the Mac's `hherb-dmc` is not in
    `config.toml`.
  - Archive accounts are refused. `_account_or_die` was deleted (no callers
    left).
- **Why no test caught this:** `FakeIMAPClient` returns aware datetimes, so
  it already behaved like the fixed client.

## Verification

| check | result |
|---|---|
| RED first | `ModuleNotFoundError: localmail.imap_dates` (collection) |
| new file `tests/test_internal_date_utc.py` | 25 passed (15 before the review round) |
| 5 mutations (normalise line, sync guard, earliest `<`→`>`, `IS DISTINCT FROM` dropped, `--refetch` ignored) | each caught by its own test |
| review round: 10 more (per-mailbox commit, `updated` by rowcount, broad SELECT catch, UIDVALIDITY skip, naive warning per message, `missing` conflated, exit 0 on skip, paused notice, backfill bypassing the guard, default-mode earliest) | each caught |
| full `pytest -q` | **4317 passed**, 6 warnings, 251 s (4307 before the review round) |
| touched files under `-W error` | 75 passed, no warnings |
| `mypy src/localmail` | Success, 162 files |
| CI | see `gh pr checks` |

- **Not checked:** what the 6 warnings are. The recorded baseline was 2
  (the websockets pair, #25). The 4 extra are outside the touched files. I
  did not compare against `main`, so it is unconfirmed whether they predate
  #420.
- **Not run against a real server:** the fix itself, i.e. `normalise_times`
  on a live IMAPClient. The library half is pinned by
  `parse_fetch_response(..., normalise_times=False)`.

## Review round (same PR)

Four reviewers (code, tests, error handling, docs). Fixed:

- **Backfill committed nothing until every mailbox was fetched**, and its
  first SELECT left a transaction (with read locks) open across all IMAP
  traffic. It now ends the read transaction before any IMAP call and commits
  each mailbox as it finishes. `updated` is computed against the value read,
  not summed from `rowcount`, because a row can be written twice and end
  where it began.
- **Skipped mailboxes were in no count**, and the warning said "next regular
  sync will refresh", which is false: `upsert_message` returns early for an
  existing row. They are reported now, the CLI exits 1, and the warning names
  the real remedy.
- **`missing` conflated "not on the server" with "returned an unusable
  date"**: split into `missing` and `unusable`.
- **The SELECT catch was `except Exception`**, so a dead connection made every
  later mailbox fail and the run "complete". Narrowed to `IMAPClientError`.
- **The bare command skipped paused accounts silently** (the TOML-driven
  version covered them). They are named now; not included, since a paused
  account may have broken credentials and would abort the run.
- **`sync_mailbox` stored NULL for a naive date silently**: one WARNING per
  pass.
- **Test gaps:** the UIDVALIDITY skip and backfill's own use of the guard had
  no test, and the earliest-date test passed for "last mailbox wins". All
  pinned. Nine mutations, each caught by its own test.

## What's next

### 1. **Merge the PR, redeploy both hosts, then run the repair** (yours)
- After the merge, check that `gh issue view 415` reports closed.
- Redeploy (commands below). New mail is then stored correctly.
- **Then repair each host once:**
  `uv run localmail backfill-internal-date --refetch`.
  - Do it **after** the redeploy. Before it, the daemon keeps writing
    wrong values.
  - This writes to the live archive.
  - The Mac's label count is ~216k, so about 4,300 body-free FETCHes:
    minutes.
- **Acceptance:**
  - On the DGX, message 39045's search-hit `date` equals its
    `GET /v1/messages/{id}` `date` to within seconds (it is +11 h today).
  - On the Mac, the skew histogram has its peak at 0, not +2 h:
    `psql -p 5532 -d localmail -c "select round(extract(epoch from internal_date - date_sent)/1800)*0.5 h, count(*) from messages m join accounts a on a.id=m.account_id where internal_date is not null and date_sent is not null and a.auth_method<>'archive' group by 1 order by 2 desc limit 8"`
  - The command's "not on the server" and "without a usable date" counts
    are reported. Expect the first to be non-zero; those rows stay wrong.
  - If it exits 1 naming skipped mailboxes, run `localmail sync` and then
    the command again.
  - Paused accounts are named and skipped. Repair one with `--account NAME`
    if its dates matter.
- **Open after the repair:** the `missing` rows keep wrong dates. If the
  count is large, decide whether to NULL them, which makes search fall back
  to `date_sent`.

### 2. **Tell kastellan** (yours; carried)
- Problem+json 400s on `/v1/search`, `filters.lang` as a list, web-search
  syntax in `query` (#419).
- After the repair, dates are no longer in the future.

### 3. **Next small slice**
- **#416:** Gmail's 29-minute IDLE drop logged as a crash.
- **#414:** stalled workers on unbounded pool connections.
- **#421:** the GUI drops quotes on a single quoted word (`"OR"`, `"-acme"`).
- **Acceptance (any of them):** RED test first, the fix, and the full suite
  green.

### 4. **Carried**
- **Dependabot #71 / #185:** left open on purpose.
- **docling > 2.144:** watch for a release and probe it.
- **emSign G3:** re-check the trust stores after OS updates.
- **kastellan slice C:** ask whether it is still wanted.
- **Issues:** #383, #384, #378, #369, #373, #372, #368, #365; #375;
  #374 + #305; #358/#357; #330/#327/#328; #319/#318/#320 (migration
  `0037_*.sql`); #340, #363, #285; robustness #218, #226, #225/#227,
  #200/#211/#208, #206, #204, #25.

## Rulings made this session

1. **#415 first.** It is the data-correctness bug, ahead of #416 and #414.
2. **Repair by re-fetching from IMAP** (operator ruling). No SQL offset
   correction, and no `date_sent` heuristic.
3. **Earliest INTERNALDATE across a message's mailboxes** wins on refetch.
4. **A naive INTERNALDATE is never stored as a guess**: sync writes NULL,
   backfill leaves the row alone.

## Open decisions & risks

1. **The repair has not been run on either host.** Until it is, every
   pre-fix row stays skewed.
2. **Rows no longer on the server** cannot be repaired by refetch (see
   next-step 1).
3. **Should a wire widening bump `api_minor`?** Still open.
4. **#375 under load:** `build_hash` is null on the Mac until a quiet
   restart.
5. **THIS FILE IS NOT THE AUTHORITY; `git` and `gh` are.**
6. **Never run two localmail pytest sessions against one test DB.** The
   test DB is on port 5532, the same cluster as the live archive.
7. **Restore a mutation from a file copy, never with `git checkout`.**
8. **Always sync with `--extra mcp --extra extraction`**, and use
   `~/.local/bin/uv` on the DGX. `uv sync --dry-run` is not read-only.
9. **No ROADMAP.md.** That `/nextsession` step is a no-op.

## Exact commands to resume

```bash
cd /Users/hherb/src/localmail
git fetch --prune origin                 # ALWAYS first
git switch main && git pull --ff-only
git log --oneline -1 origin/main         # THE authority
git log --oneline main..origin/fix/415-internaldate-utc   # empty once merged
gh issue view 415 --json state
gh issue list --limit 200 --json number --jq length

# Redeploy — Mac (the repo is the install)
unset VIRTUAL_ENV && uv sync --frozen --extra mcp --extra extraction
launchctl kickstart -k gui/$(id -u)/com.localmail.daemon
launchctl kickstart -k gui/$(id -u)/com.localmail.serve
curl -sk https://127.0.0.1:8443/v1/version
uv run localmail backfill-internal-date --refetch          # #415 repair (live DB)

# Redeploy — DGX
ssh 10.0.0.3 'cd ~/src/localmail && git pull --ff-only && ~/.local/bin/uv sync --frozen --extra mcp --extra extraction && systemctl --user restart localmail-daemon localmail-serve'
ssh 10.0.0.3 'cd ~/src/localmail && ~/.local/bin/uv run localmail backfill-internal-date --refetch'
ssh 10.0.0.3 'curl -sk https://10.0.0.3:8443/v1/version; systemctl --user show -p NRestarts localmail-daemon localmail-serve'

# Load before trusting any timing; no other localmail pytest may be running.
uptime; pgrep -fl pytest

# Suites.
unset VIRTUAL_ENV && uv run pytest -q              # 4307 at session 67
uv run pytest -q tests/test_internal_date_utc.py tests/test_sync.py
uvx mypy --python-executable .venv/bin/python src/localmail   # Success, 162 files
cd gui && npm test -- --run && npm run check
cd gui/src-tauri && cargo test --locked && cargo clippy --locked --all-targets -- -D warnings
```

The latest migration is still **`0036_api_keys.sql`**; the next free slot is
`0037_*.sql`.
