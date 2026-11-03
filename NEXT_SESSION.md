# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-11-03 (session 82).** `main` is **`c282709`** (#446:
> #373, closed).
>
> This session has **one open PR** on `fix/374-cli-composer-filters-first`.
> Its body says **`Closes #374`**. It carries the #374 code and this handoff.
>
> **Both hosts are deployed at `c282709`**; nothing from the #374 PR is
> deployed (the CLI is not a long-running process: nothing to restart).
>
> **Open issues: 32** (31 once the #374 PR merges).
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

### Deploy of `c282709` (#446, #373) to both hosts (no commit)

- **Mac:** `uv sync --frozen --extra mcp --extra extraction` (no change),
  kickstarted `com.localmail.serve` only.
- **DGX:** pulled → `c282709`, synced (reinstalled `nvidia-cusparselt-cu13`
  again, lock unchanged), restarted `localmail-serve`. Both units `active`.
- Both `/v1/version`: `api_minor` 6, `build_hash` `c282709`.

### PR on `fix/374-cli-composer-filters-first` (based on `main` at `c282709`)

| SHA | What |
|---|---|
| `bf7da9f` | #374: CLI search composes the flags ahead of the free text |
| handoff | this file + `docs/handoffs/2026-11-03T1307Z-cli-composer-filters-first.md` |

- **The defect.** `cli.py::search` built `"<free text> from:\"x\" ..."`, so
  an unclosed `"` in the query swallowed every flag and the command exited 0
  unfiltered. Flag values were unsanitized: `--label` unquoted, `"` not
  stripped from `--from`/`--to`/`--subject`/`--folder`.
- **The fix.** New pure `src/localmail/cli_search_query.py::compose_cli_query`
  (flags first, free text last). Every string flag value goes through the new
  `search/query.py::quoted_filter_value`, which `api.search._quote_value` now
  delegates to: one stripping rule for both composers. A flag value of only
  quotes is a `click.UsageError` (exit 2).
- **Cost:** same precedence flip as #367: `from:bob` in the query out-votes
  `--from alice` (#369). Documented in README.
- **Not done (#305):** `cli.py`'s search still catches `RuntimeError` only,
  so a `QueryParseError` from the query text (e.g. `after:last-week`) still
  tracebacks.
- **Tests:** `tests/test_cli_search_query.py` (29: #367's two properties over
  hostile free text, flag injection, empty-value refusal) and two
  `CliRunner` tests in `test_cli_search.py`, both RED against `main`'s
  `cli.py` (checked by swapping the file in). README and CLAUDE.md updated.
  No migration, no wire change.

## Verification

| check | result |
|---|---|
| RED first | the 2 `CliRunner` tests fail against `main`'s `cli.py` |
| `uv run pytest -q` | **4561 passed**, 6 warnings (after the review fixes) |
| mypy | Success, 166 files |
| GUI | untouched (530 at session 81) |
| CI | **check on the PR** (`gh pr checks`) |

## What's next

### 1. **Merge the #374 PR** (yours), once CI is green
- **Acceptance:** `gh pr checks <n>` all pass; after the merge
  `gh issue view 374` is `CLOSED`.

### 2. **Deploy #374 once merged**
- Pure Python, CLI only, no migration. A `git pull` + `uv sync` on each host
  is the whole deploy; restarting serve only moves `build_hash`.
- **Acceptance:** `localmail search 'x "y' --from z --format json` on the Mac
  exits 0 and its `query.filters` show `from_substr` `z`.

### 3. **Next small slice: pick with the operator**
- Candidates: #305 (widen `cli.py` search's catch to `QueryParseError` /
  `SearchArgumentRefused`; defer the module-scope imports so `--version`
  survives a missing dependency), #378 (an operator with an empty value
  becomes free text), #369 (silent last-token-wins on conflicting scalar
  operators; needs a refuse-vs-keep call), #383 (drop write-only
  `messages.headers` + GIN in `0039`; one-way door), #365 (product call).
- **Acceptance (any):** a RED test first, then full `uv run pytest -q`
  green and mypy clean.
- **Tell kastellan** (yours; carried): problem+json 400s on `/v1/search`,
  `filters.lang` as a list, #419 syntax, #415 dates, `api_minor` 5 and 6
  features, and #373 (`'` is literal). #374 is CLI-only; nothing to tell.

### 4. **Carried**
- **Migrations:** `0038` is taken. #319/#318/#320 need `0039`.
- **#414 watchdog:** waits for a stack dump (SIGUSR1 works on the DGX).
- **Dependabot #71 / #185:** left open on purpose.
- **docling > 2.144:** probe the next release when it lands.
- **emSign G3:** re-check the trust stores after OS updates.
- **kastellan slice C:** ask whether it is still wanted.
- **Mac `failed_messages`:** one `iso2022_jp` decode failure from
  2026-07-25 (`hherb-dmc/[Gmail]/Important` uid 5263). Not investigated.
- **Issues:** #383, #378, #369, #372, #368, #365; #375; #305;
  #358/#357; #330/#327/#328; #340, #363, #285; robustness #218, #226,
  #225/#227, #200/#211/#208, #206, #204, #25.

## Rulings made this session

1. **The operator asked for `c282709` on both hosts and picked #374** (over
   #378/#369/#383).
2. **#374:** the quote-stripping rule moved to `search/query.py` and is
   shared rather than restated in the CLI (my call); a quotes-only flag
   value is a usage error rather than an absent filter, matching the API.

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
10. **Load on the Mac was ~5 this session.** Check
    `uptime` before trusting any timing.

## Exact commands to resume

```bash
cd /Users/hherb/src/localmail
git fetch --prune origin                 # ALWAYS first
git switch main && git pull --ff-only
git log --oneline -1 origin/main         # THE authority
git diff main origin/fix/374-cli-composer-filters-first --stat   # empty once merged (squash)
gh pr list --state all --limit 3
gh issue view 374 --json state --jq .state
gh issue list --limit 200 --json number --jq length

# Deploy — Mac (the repo is the install; no migration for #374; CLI only)
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
unset VIRTUAL_ENV && uv run pytest -q              # 4559 at session 82
uv run pytest -q tests/test_cli_search_query.py tests/test_cli_search.py tests/test_query_parser.py
uvx mypy --python-executable .venv/bin/python src/localmail   # Success, 166 files
cd gui && npm test -- --run && npm run check   # 530 at session 81
```

The latest migration is **`0038_messages_attachment_names_strict_idx.sql`**;
the next free slot is `0039_*.sql`.
