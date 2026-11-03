# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-11-04 (session 83).** `main` is **`08d329c`** (#447:
> #374, closed).
>
> This session has **one open PR** on `fix/305-cli-search-catch`. It does
> **not** close #305 (only its search-catch half is done); the body says
> `Refs #305`. It carries the code and this handoff.
>
> **Both hosts are deployed at `08d329c`**; nothing from the #305 PR is
> deployed (CLI only, nothing to restart).
>
> **Open issues: 31.**
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

### Deploy of `08d329c` (#447, #374) to both hosts (no commit)

- **Mac:** `uv sync --frozen --extra mcp --extra extraction` (no change),
  kickstarted `com.localmail.serve`.
- **DGX:** pulled → `08d329c`, synced (reinstalled `nvidia-cusparselt-cu13`
  again, lock unchanged), restarted `localmail-serve`. Both units `active`.
- Both `/v1/version`: `api_minor` 6, `build_hash` `08d329c`.
- **Acceptance met:** `localmail search 'x "y' --from z --format json` on
  the Mac exits 0 with `from_substr` `z`.

### PR on `fix/305-cli-search-catch` (based on `main` at `08d329c`)

| SHA | What |
|---|---|
| `66c0ba0` | #305 (search-catch half): a malformed query is a usage error |
| handoff | this file + `docs/handoffs/2026-11-04T0545Z-cli-search-catch.md` |

- **The defect.** `cli.py::search` caught `RuntimeError` only, so a
  `QueryParseError` (`has:attachments`, `after:last-week`, a contradictory
  `has:` pair; since #373 also `has:'attachment`) was a bare traceback, as
  would be any `SearchArgumentRefused` member once a sort/cursor flag exists.
- **The fix.** `except (QueryParseError, SearchArgumentRefused)` →
  `click.UsageError` (exit 2). Bare `ValueError` deliberately not caught.
- **Docstring half (option 1 of #305)** was already done:
  `version_report.py`'s "Scope" paragraph narrows the claim. Nothing to do.
- **Tests:** 5 in `tests/test_cli_search.py` — three malformed queries
  through a real `Searcher` whose pool refuses every connection, a fresh
  family member via a stub, and a bare-`ValueError` control. RED first
  (4 failed with `QueryParseError`/`_NewMember`, the control passed).
  Mutations: widening to `ValueError` fails the control; dropping
  `SearchArgumentRefused` fails the family test.
- README (two "traceback, #305" notes) and CLAUDE.md (three) updated.

## Verification

| check | result |
|---|---|
| RED first | 4 of the 5 new tests fail against `main`'s `cli.py` |
| `uv run pytest -q` | **4566 passed**, 6 warnings |
| mypy | Success, 166 files |
| GUI | untouched (530 at session 81) |
| CI | **check on the PR** (`gh pr checks`) |

## What's next

### 1. **Merge the #305 PR** (yours), once CI is green
- **Acceptance:** `gh pr checks <n>` all pass; #305 stays **OPEN** (the
  import-deferral half remains).

### 2. **Deploy once merged** (CLI only: `git pull` + `uv sync` per host)
- **Acceptance:** `localmail search 'has:attachments'` on the Mac exits 2
  with `Error: has: expected 'attachment' or 'no-attachment'…`, no traceback.

### 3. **Next small slice: pick with the operator**
- Candidates: #305's other half (defer `cli.py`'s module-scope imports so
  `--version` survives a missing dependency; touches many commands, risks
  moving import errors to first use), #378 (empty/unparsable operator value
  becomes free text; likely refuse only id/date operators), #369
  (last-token-wins; needs a refuse-vs-keep call), #383 (drop write-only
  `messages.headers` + GIN in `0039`; one-way door), #365 (product call).
- **Acceptance (any):** a RED test first, then full `uv run pytest -q`
  green and mypy clean.
- **Tell kastellan** (yours; carried): problem+json 400s on `/v1/search`,
  `filters.lang` as a list, #419 syntax, #415 dates, `api_minor` 5 and 6
  features, and #373 (`'` is literal). #305/#374 are CLI-only.

### 4. **Carried**
- **Migrations:** `0038` is taken. #319/#318/#320 need `0039`.
- **#414 watchdog:** waits for a stack dump (SIGUSR1 works on the DGX).
- **Dependabot #71 / #185:** left open on purpose.
- **docling > 2.144:** probe the next release when it lands.
- **emSign G3:** re-check the trust stores after OS updates.
- **kastellan slice C:** ask whether it is still wanted.
- **Mac `failed_messages`:** one `iso2022_jp` decode failure from
  2026-07-25 (`hherb-dmc/[Gmail]/Important` uid 5263). Not investigated.
- **Issues:** #383, #378, #369, #372, #368, #365; #375; #305 (imports);
  #358/#357; #330/#327/#328; #340, #363, #285; robustness #218, #226,
  #225/#227, #200/#211/#208, #206, #204, #25.

## Rulings made this session

1. **The operator asked for `08d329c` on both hosts and picked #305's
   search-catch half** (over #305 in full, #378, #369).
2. **#305:** a malformed query is `click.UsageError` (exit 2, matching the
   quotes-only flag refusal from #374) rather than a plain `ClickException`
   (exit 1); my call.

## Open decisions & risks

0. **Since #438, `localmail import` exits 1 on a failed job** (behaviour
   change for scripts; none known on either host). Since #441, a contended
   poison-pill record also fails the job / aborts the sync pass. Since #442,
   an import whose final status write fails on all 5 attempts (~40 s) also
   exits 1, and the row stays `running` until serve restarts. Since #440,
   a re-imported pill bumps its existing `failed_messages` row; duplicates
   made before #440 are not merged (none exist on either host, checked
   2026-11-03). Since #373, `'` in a search query is literal text:
   an undocumented `from:'Anna Schmidt'` now splits, and `has:'attachment`
   is a 400 (unknown `has:` value). Since #374, in `localmail search` a
   query operator out-votes the matching flag, and `--from '""'` exits 2.
   Since #305's half, a malformed `localmail search` query exits 2 with a
   one-line error instead of 1 with a traceback.
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
10. **Load on the Mac was ~6–8 this session.** Check
    `uptime` before trusting any timing.

## Exact commands to resume

```bash
cd /Users/hherb/src/localmail
git fetch --prune origin                 # ALWAYS first
git switch main && git pull --ff-only
git log --oneline -1 origin/main         # THE authority
git diff main origin/fix/305-cli-search-catch --stat   # empty once merged (squash)
gh pr list --state all --limit 3
gh issue view 305 --json state --jq .state   # stays OPEN
gh issue list --limit 200 --json number --jq length

# Deploy — Mac (the repo is the install; no migration for #305; CLI only)
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
unset VIRTUAL_ENV && uv run pytest -q              # 4566 at session 83
uv run pytest -q tests/test_cli_search.py tests/test_cli_search_query.py
uvx mypy --python-executable .venv/bin/python src/localmail   # Success, 166 files
cd gui && npm test -- --run && npm run check   # 530 at session 81
```

The latest migration is **`0038_messages_attachment_names_strict_idx.sql`**;
the next free slot is `0039_*.sql`.
