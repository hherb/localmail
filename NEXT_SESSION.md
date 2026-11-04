# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-11-05 (session 86).** `main` is **`5d361fe`** (#450:
> #305 `--version` survives a missing dependency).
>
> This session has **one open PR** on `fix/358-typed-search-response`
> (closes #358). It carries the code and this handoff.
>
> **Both hosts are deployed at `5d361fe`** (`uv sync` regenerated
> `.venv/bin/localmail`, which now imports `localmail.cli_entry`).
>
> **Open issues: 29** (28 once #358 closes).
>
> **`hherb-dmc`'s Gmail refresh token is expired on the Mac** (identified
> this session). Every IDLE/poll pass for it has failed with
> `RefreshError: invalid_grant: Token has been expired or revoked` since
> **2026-11-03 20:24**; its last message landed 2026-11-03 18:09. Fix (yours,
> needs a browser): `localmail oauth-login hherb-dmc`, then
> `launchctl kickstart -k gui/$(id -u)/com.localmail.daemon`. The DGX has
> its own token store; not checked.
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

### Deploy of `5d361fe` (#450) — both hosts (no commit)

- Mac: `uv sync --frozen --extra mcp --extra extraction`, kickstarted
  `com.localmail.serve`; `/v1/version` `build_hash` `5d361fe`.
- DGX: `git pull`, same sync, restarted `localmail-serve`; both services
  `active`; `build_hash` `5d361fe`.
- On both, `.venv/bin/localmail` contains `from localmail.cli_entry import
  main` and `localmail --version` prints `localmail, version 0.3.0`.

### PR on `fix/358-typed-search-response` (based on `main` at `5d361fe`)

| SHA | What |
|---|---|
| `016c034` | #358: the search response has one typed constructor that refuses a fake |
| handoff | this file + `docs/handoffs/2026-11-04T2205Z-typed-search-response.md` |

- **The defect.** `run_search -> dict[str, Any]`, built from two
  hand-written literals (rowed path + empty-ACL branch). A MagicMock page
  with `sort_applied`/`rankable` unset serialised as `{}`/`[]` instead of
  raising (the trap behind #345 and #353).
- **The fix.** New `src/localmail/api/search_response.py`: the
  `SearchResponse` TypedDict plus `search_response(...)`, now the only way
  the response is built. It derives `total_estimate` (`None`) and
  `rewrite_skipped` once. It raises `TypeError` on a `sort_applied` outside
  `SortMode`, a non-`bool` `rankable`, or a non-numeric `took_ms`, and
  `ValueError` on `rank` beside `rankable=False` (#356's wire half).
  - HTTP route annotated `SearchResponse` with `response_model=None`
    (serialization unchanged).
  - MCP `server.search` annotated `SearchResponse`, so the tool now
    **publishes an `outputSchema`** with all ten keys required, and the SDK
    validates results against it.
- **It caught 29 live fakes** (3 files) that set `sort_applied` and forgot
  `rankable`, plus 6 MCP fakes returning a partial dict.
- **Tests:** new `tests/test_search_response.py` (21), plus a published-schema
  pin in `test_mcp_server_build.py`. Six mutations (each check off, the
  server annotation reverted, the empty-ACL return back to `dict(...)`) each
  fail their own test.
- CLAUDE.md (new entry + layout line), `docs/mcp-usage.md` (outputSchema
  note) updated. README unchanged: no wire change on HTTP.

## Verification

| check | result |
|---|---|
| RED first | `test_search_response.py` failed collection (no module) |
| `uv run pytest -q` | **4653 passed**, 6 warnings (pre-existing) |
| mypy | Success, 168 files |
| GUI | untouched |
| CI | **check on the PR** (`gh pr checks`) |

## What's next

### 1. **Merge the #358 PR** (yours), once CI is green
- **Acceptance:** `gh pr checks <n>` all pass; `gh issue view 358` CLOSED.

### 2. **Deploy** on both hosts
- No migration and no entry-point change; restart `serve` (the MCP schema
  and `build_hash` move). The daemon need not restart.
- **Acceptance:** both `/v1/version` show the merge SHA; an MCP `tools/list`
  shows `search` with an `outputSchema`.

### 3. **Re-authorise `hherb-dmc`** (yours, browser)
- **Acceptance:** `daemon.err.log` shows no new `crashed for hherb-dmc`
  after the kickstart, and
  `psql -p 5532 -d localmail -Atc "select max(date_received) from messages m join accounts a on a.id=m.account_id where a.name='hherb-dmc'"`
  moves past 2026-11-03.

### 4. **Next small slice: pick with the operator**
- Candidates: **#356** (now smaller: only `SearchPage`/`PoolMetadata`
  `__post_init__` left, the wire half is done), **#357** (parsed-free-text
  type for the four sort rules), **#328** (typed page-cache entry; same class
  as #358, one layer in), #369 (needs a refuse-vs-keep call), #383 (one-way
  door, `0039`), #365 (product call).
- **Acceptance (any):** a RED test first, then full `uv run pytest -q`
  green and mypy clean.
- **Tell kastellan** (yours; carried): problem+json 400s on `/v1/search`,
  `filters.lang` as a list, #419 syntax, #415 dates, `api_minor` 5 and 6
  features, #373 (`'` is literal), #378 (an empty
  `after:`/`before:`/`has:` or a non-int id in `query` is a 400).

### 5. **Carried**
- **Migrations:** `0038` is taken. #319/#318/#320 need `0039`.
- **#414 watchdog:** waits for a stack dump (SIGUSR1 works on the DGX).
- **Dependabot #71 / #185:** left open on purpose.
- **docling > 2.144:** probe the next release when it lands.
- **emSign G3:** re-check the trust stores after OS updates.
- **kastellan slice C:** ask whether it is still wanted.
- **Mac `failed_messages`:** one `iso2022_jp` decode failure from
  2026-07-25 (`hherb-dmc/[Gmail]/Important` uid 5263). Not investigated.
- **Issues:** #383, #369, #372, #368, #365; #375; #357/#356;
  #330/#327/#328; #340, #363, #285; robustness #218, #226, #225/#227,
  #200/#211/#208, #206, #204, #25.

## Rulings made this session

1. **The operator picked #358** and approved the DGX deploy.
2. **#358 (mine):** a TypedDict plus a checking constructor rather than a
   pydantic `response_model` (transport-neutral, as the issue suggested), and
   the `rank`+unrankable pair check folded in since every response now passes
   one place. `SearchPage`'s own `__post_init__` (#356) left for its own slice.
3. **The MCP output schema** came free with the annotation and was kept: it
   documents the contract to agents and adds SDK-side validation.

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
   one-line error instead of 1 with a traceback. Since #305, a missing
   third-party dependency makes `localmail --version` print its line, exit 0
   and write to stderr (the stderr contract widened); other commands exit 1
   with one line instead of a traceback. Since #378, an empty
   `after:`/`before:`/`has:`/`account_id:`/`folder_id:`, or a non-int id,
   in a query is a 400 / exit 2 (it was served as free text). The GUI
   shows it as an error banner.
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
10. **Load on the Mac was ~7–19 this session** (full suite 4½ min). Check
    `uptime` before trusting any timing.

## Exact commands to resume

```bash
cd /Users/hherb/src/localmail
git fetch --prune origin                 # ALWAYS first
git switch main && git pull --ff-only
git log --oneline -1 origin/main         # THE authority
git diff main origin/fix/358-typed-search-response --stat   # empty once merged (squash)
gh pr list --state all --limit 3
gh issue view 358 --json state --jq .state   # CLOSED once merged
gh issue list --limit 200 --json number --jq length

# Deploy — Mac (the repo is the install)
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
unset VIRTUAL_ENV && uv run pytest -q              # 4653 at session 86
uv run pytest -q tests/test_search_response.py tests/test_mcp_server_build.py
uvx mypy --python-executable .venv/bin/python src/localmail   # Success, 168 files
cd gui && npm test -- --run && npm run check   # 530 at session 81
```

The latest migration is **`0038_messages_attachment_names_strict_idx.sql`**;
the next free slot is `0039_*.sql`.
