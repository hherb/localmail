# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-11-01 (session 74).** `main` was **`fa1184b`** after
> #433 merged at the start of this session (#430 is closed).
>
> This session opened **one PR** on `fix/431-date-sent-minus-0000`:
> `3b9d7f8` (the code) and this handoff. Its body says **`Closes #431`**.
>
> **Open issues: 36** (35 once #431 closes).
>
> **Leftover worktree `.claude/worktrees/bottie-bearer-token-dbc203`**
> (detached at `5b85180`). Untouched; ask before removing it.
>
> **Neither host has #424, #427, #428, #432 or #433 deployed.** The DGX is
> at `b07b78e`, the Mac DB at migration `0036`.

## Project context (1-minute version)

`localmail` mirrors IMAP accounts (Gmail OAuth or password) into Postgres, and
is **strictly read-only w.r.t. IMAP**. The database is canonical for accounts.

- **Daemon** (IDLE + poll per account), **web admin UI** (HTMX).
- **Hybrid search**, the HTTPS `/v1` server, a remote **MCP server**, and the
  opt-in `--smart` rewriter.
- **Tauri 2 + Svelte 5 GUI** under `gui/`.
- **kastellan** consumes `/v1` over REST with an API key. #426 was its list of
  search asks, finished by #428.

## What we shipped this session

- **#433 merged** (operator) as `fa1184b`; #430 closed.

### PR on `fix/431-date-sent-minus-0000` (based on `main` at `fa1184b`)

| SHA | What |
|---|---|
| `3b9d7f8` | #431: a `-0000` `Date:` is stored as UTC, not in the Postgres session zone |
| handoff | this file + `docs/handoffs/2026-11-01T0712Z-date-sent-minus-0000.md` |

- **The defect.** `parsedate_to_datetime` returns `-0000` naive (the default
  policy rewrites an unknown zone like `XYZ` to `-0000` too). psycopg sends
  naive as `timestamp`, which Postgres reads in the session zone: #415's
  mechanism. On the Mac (session `Australia/Perth`) such rows are 8 h off.
- **The fix.** New pure `date_header.date_sent_instant` attaches UTC to a
  naive result; `parser._date_sent` calls it. `sender_local_time` is
  unchanged, so `date_local` stays `None` for `-0000`.
- **Existing rows not repaired** (see open decision 1).
- New test file `tests/test_date_sent_minus_zero.py` (12 tests: pure rule,
  parser, and `process_one_message` under `SET TimeZone = 'Australia/Perth'`).
  CLAUDE.md's `date_sent` bullet and the #426 bullet updated. README unchanged.

## Verification

| check | result |
|---|---|
| RED first | the DB test failed storing `09:58:24+08:00` for `09:58:24 -0000`, the parser tests failed on a naive value |
| `uv run pytest -q` | **4436 passed**, 6 warnings (4424 at session 73) |
| mypy | Success, 165 files |
| GUI | not run (no GUI change) |
| CI | **check on the PR** (`gh pr checks`) |

## What's next

### 1. **Merge this PR** (yours)
- **Acceptance:** `gh issue view 431 --json state` is `CLOSED`.

### 2. **Deploy both hosts** (yours; carried)
- Undeployed: #424 (pool bounds, SIGUSR1), #427 (paging, migration `0037`),
  #428 (`api_minor` 6), #432 (GUI), #433, and this PR once merged.
- Mac: `uv sync --frozen --extra mcp --extra extraction`, then
  `localmail init-db` (builds the `0037` index), then restart daemon and serve.
- DGX: the same through ssh (commands below).
- **Acceptance:**
  - `/v1/version` reports `api_minor` 6 on both hosts.
  - kastellan's #426 probe on the DGX passes (`Fee Proposal`,
    `2312 Herb Fee Proposal`, `Qantas booking`: 6 pages each, 0 repeats;
    `230712` finds 18106; `filters.attachment_name` `230712` → 18106;
    `fields: ["message_id","date_local"]` gives an offset-bearing time).
  - `kill -USR1 <daemon pid>` on the DGX prints stacks and the daemon keeps
    running (#424).
  - `localmail retry-failed` on each host; any `OverflowError` rows in
    `failed_messages` succeed (#430).
- Then comment on #426 with the probe results.

### 3. **Tell kastellan** (yours; carried)
- Problem+json 400s on `/v1/search`, `filters.lang` as a list, #419 syntax,
  #415 dates; `api_minor` 5 (no repeats, `attachment_filename` /
  `attachments`); `api_minor` 6 (`filters.attachment_name`, `date_local`).

### 4. **Next small slice: #429** (a malformed `attachments` column can match `attachment_name`)
- Read the issue first; the rule lives in `search/attachment_names.py`
  (`NAME_CONTAINS_SQL`, lax jsonpath, like `NAMES_TSV_SQL`).
- **Acceptance:** a RED test seeding a non-array / malformed `attachments`
  row shows the filter admitting it; the fix makes the name rules agree with
  `HAS_ATTACHMENT_SQL` (which reads a malformed row as "no attachments") on
  what a malformed row is. `NAMES_TSV_SQL` is 0037's index expression, so
  changing it needs `0038` — decide that with the operator.
  Full `uv run pytest -q` green.

### 5. **Carried**
- **Migrations:** `0037` is taken. #319/#318/#320 need `0038`.
- **#414 watchdog:** waits for a stack dump.
- **Dependabot #71 / #185:** left open on purpose.
- **docling > 2.144:** probe the next release when it lands.
- **emSign G3:** re-check the trust stores after OS updates.
- **kastellan slice C:** ask whether it is still wanted.
- **#384** is partly answered (2.7 ms warm for 50 prefixes). Comment or close.
- **Issues:** #429, #425, #383, #378, #369, #373, #372, #368, #365; #375;
  #374 + #305; #358/#357; #330/#327/#328; #340, #363, #285; robustness #218,
  #226, #225/#227, #200/#211/#208, #206, #204, #25.

## Rulings made this session

1. **#431: attach UTC in a new `date_sent_instant`, not in
   `parse_date_header`** (my call). `sender_local_time` needs the naive
   reading to say "offset unknown"; changing the shared parser would have
   broken `date_local`.
2. **#431: no backfill built** (my call, reversible). Left as open decision 1.

## Open decisions & risks

1. **Repair the pre-#431 `-0000` rows?** Mac: 2,865 of 132,320 rows carry a
   `-0000` Date, stored 8 h off; **none** has a NULL `internal_date`, so the
   wire `date` is right and only `after:`/`before:` (which read `date_sent`)
   and two `ORDER BY m.date_sent` sites in `arms.py` see it. DGX: session is
   UTC, so its rows are correct. A repair should re-parse `raw_bytes`
   (`date_sent_instant` over the Date header), not shift by a fixed offset.
   Measuring query (read-only):
   `select count(*) from messages where exists (select 1 from jsonb_array_elements_text(case when jsonb_typeof(headers->'Date')='array' then headers->'Date' else '[]' end) d where d ~ '-0000\s*(\(.*\))?\s*$')`
2. **`attachment_name` is unindexed** (~0.2 s warm at 132k for a filter-only
   miss). A trigram index would be `0038`.
3. **`date_local` vs `date`:** different instants for most mail (send vs
   arrival). Documented in README.
4. **Should a wire widening bump `api_minor`?** Still open.
5. **3,194 rows keep a skewed `internal_date`** (Mac 2,720, DGX 474). Not
   decided: leave them, NULL them, or apply a per-host SQL shift.
6. **THIS FILE IS NOT THE AUTHORITY; `git` and `gh` are.** After a squash
   merge `main..origin/<branch>` is non-empty by construction; compare with
   `git diff main origin/<branch> --stat` (empty = landed).
7. **Never run two localmail pytest sessions against one test DB** (port
   5532, the same cluster as the live archive).
8. **Restore a mutation from a file copy, never with `git checkout`.**
9. **Always sync with `--extra mcp --extra extraction`**. On the DGX use
   `~/.local/bin/uv`. `uv sync --dry-run` is not read-only.
10. **No ROADMAP.md.** That `/nextsession` step is a no-op.

## Exact commands to resume

```bash
cd /Users/hherb/src/localmail
git fetch --prune origin                 # ALWAYS first
git switch main && git pull --ff-only
git log --oneline -1 origin/main         # THE authority
git diff main origin/fix/431-date-sent-minus-0000 --stat   # empty once merged (squash)
gh pr list --state all --limit 3
gh issue view 431 --json state --jq .state
gh issue list --limit 200 --json number --jq length

# Deploy — Mac (the repo is the install)
unset VIRTUAL_ENV && uv sync --frozen --extra mcp --extra extraction
uv run localmail init-db                # builds the 0037 index; messages writes pause meanwhile
launchctl kickstart -k gui/$(id -u)/com.localmail.daemon
launchctl kickstart -k gui/$(id -u)/com.localmail.serve
curl -sk https://127.0.0.1:8443/v1/version     # api_minor 6

# Deploy — DGX
ssh 10.0.0.3 'cd ~/src/localmail && git pull --ff-only && ~/.local/bin/uv sync --frozen --extra mcp --extra extraction && systemctl --user stop localmail-daemon && .venv/bin/localmail init-db && systemctl --user start localmail-daemon && systemctl --user restart localmail-serve'
ssh 10.0.0.3 'docker exec localmail-pg psql -U localmail -d localmail -Atc "select revision from schema_migrations order by 1 desc limit 1"'   # 0037_…
ssh 10.0.0.3 'kill -USR1 $(systemctl --user show -p MainPID --value localmail-daemon); sleep 2; journalctl --user -u localmail-daemon --since "-1min" | grep -c "Thread 0x"'

# Load before trusting any timing; no other localmail pytest may be running.
uptime; pgrep -fl pytest

# Suites.
unset VIRTUAL_ENV && uv run pytest -q              # 4436 at session 74
uv run pytest -q tests/test_date_sent_minus_zero.py tests/test_date_header.py tests/test_date_header_overflow_sync.py
uvx mypy --python-executable .venv/bin/python src/localmail   # Success, 165 files
cd gui && npm test -- --run && npm run check   # 530 at session 72
```

The latest migration is **`0037_messages_attachment_names_idx.sql`**; the next
free slot is `0038_*.sql`.
