# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-11-01 (session 75).** `main` is **`a2815e2`**: #434
> (#431) merged before this session started, review fixes included
> (`git diff origin/main a7146ba` is empty). #431 is closed.
>
> This session opened **one PR** on `fix/429-strict-attachment-names`:
> `db08ec0` (the code) and this handoff. Its body says **`Closes #429`**.
>
> **Open issues: 35** (34 once #429 closes).
>
> **Leftover worktree `.claude/worktrees/bottie-bearer-token-dbc203`**
> (detached at `5b85180`). Untouched; ask before removing it.
>
> **Correction to session 74's handoff:** the DGX **does** have migration
> `0037` applied (`schema_migrations` reads `0037_…`). The Mac live DB is at
> `0036`. Which code the DGX *runs* was not re-checked.

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

### PR on `fix/429-strict-attachment-names` (based on `main` at `a2815e2`)

| SHA | What |
|---|---|
| `db08ec0` | #429: both attachment-name rules read names with a strict jsonpath; migration `0038` |
| handoff | this file + `docs/handoffs/2026-11-01T0829Z-strict-attachment-names.md` |

- **The defect.** `NAMES_TSV_SQL` (lexical match, index expression) and
  `NAME_CONTAINS_SQL` (`attachment_name:` filter) used lax
  `$[*].filename`. Lax mode wraps a top-level object and unwraps a nested
  array or an array-valued name, so those malformed `attachments` columns
  matched. The object case contradicted `HAS_ATTACHMENT_SQL`:
  `has:no-attachment attachment_name:fee` returned a row flagged
  `has_attachments: false`. Latent (no writer produces these shapes).
- **The fix.** `attachment_names.NAMES_JSONPATH` =
  `strict $[*] ? (@.type() == "object" && @.filename.type() == "string").filename`,
  called with `silent => true` (positional `'{}', true`) so a non-array
  yields nothing. Shared by both rules through `names_query_sql`. Both
  functions are immutable, so it is a valid index expression.
- **Migration `0038_messages_attachment_names_strict_idx.sql`**: drops
  `messages_attachment_names_idx`, creates
  `messages_attachment_names_strict_idx` (new name: `IF NOT EXISTS` matches
  on name only). Computing the expression over the Mac's 132k messages took
  0.2 s, so the rebuild is cheap. On the Mac, `init-db` will build 0037 and
  then replace it with 0038 in one run.
- New `tests/test_attachment_names_malformed.py` (7 tests). CLAUDE.md: new
  strict-jsonpath bullet under #426, layout and migration-slot lines.
  README unchanged (well-formed behaviour is the same).

## Verification

| check | result |
|---|---|
| RED first | all 7 new tests failed on the lax rules (filter, lexical rank+date, has:no-attachment, 3 names-vector shapes) |
| `uv run pytest -q` | **4447 passed**, 6 warnings (4440 at session 74) |
| mypy | Success, 165 files |
| test DB | `messages_attachment_names_strict_idx` present, old index gone, latest `0038` |
| GUI | not run (no GUI change) |
| CI | **check on the PR** (`gh pr checks`) |

## What's next

### 1. **Merge this PR** (yours)
- **Acceptance:** `gh issue view 429 --json state` is `CLOSED`.

### 2. **Deploy both hosts** (yours; carried)
- Undeployed: #424 (pool bounds, SIGUSR1), #427 (paging, migration `0037`),
  #428 (`api_minor` 6), #432 (GUI), #433, #434, and this PR (migration `0038`) once merged.
- Mac: `uv sync --frozen --extra mcp --extra extraction`, then
  `localmail init-db` (builds `0037`, then `0038` replaces it), then restart daemon and serve.
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

### 4. **Next small slice: pick with the operator**
- Candidates: #425 (embed worker / lang detection misclassify a DB
  contention error), #383 (`messages.headers` + its GIN index are
  write-only), #365 (images embedded in HTML counted as attachments — an
  edit to `HAS_ATTACHMENT_SQL`), #384 (comment or close: partly answered).
- **Acceptance (any):** a RED test first, then full `uv run pytest -q`
  green and mypy clean.

### 5. **Carried**
- **Migrations:** `0038` is taken. #319/#318/#320 need `0039`.
- **#414 watchdog:** waits for a stack dump.
- **Dependabot #71 / #185:** left open on purpose.
- **docling > 2.144:** probe the next release when it lands.
- **emSign G3:** re-check the trust stores after OS updates.
- **kastellan slice C:** ask whether it is still wanted.
- **#384** is partly answered (2.7 ms warm for 50 prefixes). Comment or close.
- **Issues:** #425, #383, #378, #369, #373, #372, #368, #365; #375;
  #374 + #305; #358/#357; #330/#327/#328; #340, #363, #285; robustness #218,
  #226, #225/#227, #200/#211/#208, #206, #204, #25.

## Rulings made this session

1. **#429: fix both rules plus a migration, not a query-side guard** (my
   call; session 74 had said "decide with the operator"). The issue itself
   prescribes it, 0037 is applied on the DGX so it cannot be edited, and
   the rebuild costs ~0.2 s of expression work. A query-time
   `jsonb_typeof` guard would have fixed only the object shape.

## Open decisions & risks

1. **Repair the pre-#431 `-0000` rows?** Mac: 2,865 of 132,320 rows carry a
   `-0000` Date, stored 8 h off; **none** has a NULL `internal_date`, so the
   list/search/changes wire `date` (COALESCE) is right. Raw `date_sent`
   readers see the skew: the message-detail `date` (`api/messages.py`, also
   MCP `get_message`), `after:`/`before:`, arm 4's fan-out cap
   (`arms.py:360`), `localmail search`'s printed date, and the `Date:` line
   in embedded chunk text. DGX: session is UTC, so its rows are correct. A
   repair should re-parse `raw_bytes` (`date_sent_instant` over the Date
   header), not shift by a fixed offset. Measuring query (read-only; it reads
   only the exact `Date` key, so `DATE`/`date` spellings are missed and the
   count may be slightly low):
   `select count(*) from messages where exists (select 1 from jsonb_array_elements_text(case when jsonb_typeof(headers->'Date')='array' then headers->'Date' else '[]' end) d where d ~ '-0000\s*(\(.*\))?\s*$')`
2. **`attachment_name` is unindexed** (~0.2 s warm at 132k for a filter-only
   miss). A trigram index would be `0039`.
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
git diff main origin/fix/429-strict-attachment-names --stat   # empty once merged (squash)
gh pr list --state all --limit 3
gh issue view 429 --json state --jq .state
gh issue list --limit 200 --json number --jq length

# Deploy — Mac (the repo is the install)
unset VIRTUAL_ENV && uv sync --frozen --extra mcp --extra extraction
uv run localmail init-db                # builds 0037 then 0038; messages writes pause meanwhile
launchctl kickstart -k gui/$(id -u)/com.localmail.daemon
launchctl kickstart -k gui/$(id -u)/com.localmail.serve
curl -sk https://127.0.0.1:8443/v1/version     # api_minor 6

# Deploy — DGX
ssh 10.0.0.3 'cd ~/src/localmail && git pull --ff-only && ~/.local/bin/uv sync --frozen --extra mcp --extra extraction && systemctl --user stop localmail-daemon && .venv/bin/localmail init-db && systemctl --user start localmail-daemon && systemctl --user restart localmail-serve'
ssh 10.0.0.3 'docker exec localmail-pg psql -U localmail -d localmail -Atc "select revision from schema_migrations order by 1 desc limit 1"'   # 0038_…
ssh 10.0.0.3 'kill -USR1 $(systemctl --user show -p MainPID --value localmail-daemon); sleep 2; journalctl --user -u localmail-daemon --since "-1min" | grep -c "Thread 0x"'

# Load before trusting any timing; no other localmail pytest may be running.
uptime; pgrep -fl pytest

# Suites.
unset VIRTUAL_ENV && uv run pytest -q              # 4447 at session 75
uv run pytest -q tests/test_attachment_names_malformed.py tests/test_attachment_names.py tests/test_search_attachment_name_filter.py
uvx mypy --python-executable .venv/bin/python src/localmail   # Success, 165 files
cd gui && npm test -- --run && npm run check   # 530 at session 72
```

The latest migration is **`0038_messages_attachment_names_strict_idx.sql`**;
the next free slot is `0039_*.sql`.
