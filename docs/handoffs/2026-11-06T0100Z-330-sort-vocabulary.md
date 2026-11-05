# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-11-06 (session 90).** `main` is **`753133c`** (#454:
> #328, the page-cache entry is a checked `PoolEntry`).
>
> This session has **one open PR** on `fix/330-sort-vocabulary` (#330). It
> carries the code and this handoff.
>
> **Both hosts are deployed at `753133c`** (deployed this session, verified
> via `/v1/version`). #330 changes no wire behaviour; deploy it with the
> next PR or after merge.
>
> **Open issues: 27** (26 once this PR merges and closes #330). **Open
> Dependabot alerts: 2** (#71, #185, open on purpose; carried, not
> re-checked).
>
> **`hherb-dmc`'s Gmail refresh token is expired on the Mac** (carried, not
> re-checked). Fix (yours, needs a browser): `localmail oauth-login
> hherb-dmc`, then `launchctl kickstart -k gui/$(id -u)/com.localmail.daemon`.
>
> **Leftover worktree:** `.claude/worktrees/bottie-bearer-token-dbc203`
> (carried; ask before removing).

## Project context (1-minute version)

`localmail` mirrors IMAP accounts (Gmail OAuth or password) into Postgres, and
is **strictly read-only w.r.t. IMAP**. The database is canonical for accounts.

- **Daemon** (IDLE + poll per account), **web admin UI** (HTMX).
- **Hybrid search**, the HTTPS `/v1` server, a remote **MCP server**, and the
  opt-in `--smart` rewriter.
- **Tauri 2 + Svelte 5 GUI** under `gui/`.
- **kastellan** consumes `/v1` over REST with an API key.

## What we shipped this session

- **Deployed `753133c`** (#356 + #328) to the Mac and the DGX; both report
  `build_hash: 753133c`.

### PR on `fix/330-sort-vocabulary` (based on `main` at `753133c`)

| SHA | What |
|---|---|
| `fa44f7c` | #330: `serve/routes/search.py`, `mcp/server.py`, `mcp/tools.py` annotate `sort`/`sort_order` with `sort_axes.SortMode`/`SortOrder` instead of restating the `Literal`s; CLAUDE.md note |
| handoff | this file + `docs/handoffs/2026-11-06T0100Z-330-sort-vocabulary.md` |

- Tests: `tests/test_sort_vocabulary.py` (8). An AST rule refuses any
  `Literal` in `src/` outside `sort_axes.py` sharing two or more members
  with either vocabulary (aliases like `Literal as L` resolved; negative
  control built from `get_args`; a check the authority still matches), and
  the published HTTP (`SearchRequest.model_json_schema()`) and MCP
  (`inputSchema`, unopened pool, no DB) enums are compared with `get_args`,
  reading `const` too.
- Review round (same PR): the first cut matched exact sets only, so a copy
  in `mcp/tools.py` left stale by a grown authority was caught by nothing,
  and the hard-coded negative control failed on a legitimate growth. Both
  fixed; GUI TS copies and `mcp/server.py` description prose are documented
  as uncovered.
- Mutations (copied tree, import path verified): a legitimate `SortMode`
  growth stays green; a grown authority with stale `tools.py`, an aliased
  `L["rank", "date"]` in the route, and reordering fail the AST rule; a
  split `Literal["rank"] | Literal["date", "x"]` or stale `Literal["rank"]`
  fails only the schema pin.
- **Not done:** the issue's larger idea (`SortOrder` as a `str` Enum
  carrying its ORDER BY). The wire is unchanged because the aliases are the
  same `Literal`s in the same order.

## Verification

| check | result |
|---|---|
| `uv run pytest -q` | **4716 passed**, 6 warnings (pre-existing), 8:18 |
| mypy | Success, 169 files |
| GUI | untouched |
| CI | not yet run when this was written; check `gh pr checks` |

## What's next

### 1. **Merge the #330 PR** (yours), once CI is green
- **Acceptance:** `gh pr checks <PR>` all pass; `gh issue view 330` closed.

### 2. **Re-authorise `hherb-dmc`** (yours, browser; carried)
- **Acceptance:** no new `crashed for hherb-dmc` in `daemon.err.log` after
  the kickstart; its `max(date_received)` moves past 2026-11-03.

### 3. **Next small slice: pick with the operator**
- Candidates: **#327** (pool-mode `CursorPlan` carries `sort`/`sort_order`
  it must ignore; the last of the #322 type-design follow-ups), **#285**
  (does mypy/ruff gate CI — needs a ruff-pin call), **#357** (only worth it
  with #285), #369 (refuse-vs-keep call), #383 (one-way door, `0039`),
  #365 (product call).
- **Acceptance (any):** a RED test first, then full `uv run pytest -q`
  green and mypy clean.
- **Tell kastellan** (yours; carried): problem+json 400s on `/v1/search`,
  `filters.lang` as a list, #419 syntax, #415 dates, `api_minor` 5 and 6
  features, #373 (`'` is literal), #378 (an empty
  `after:`/`before:`/`has:` or a non-int id in `query` is a 400).

### 4. **Carried**
- **Migrations:** `0038` is taken. #319/#318/#320 need `0039`.
- **#414 watchdog:** waits for a stack dump (SIGUSR1 works on the DGX).
- **Dependabot #71 / #185:** left open on purpose.
- **docling > 2.144:** probe the next release when it lands.
- **emSign G3:** re-check the trust stores after OS updates.
- **kastellan slice C:** ask whether it is still wanted.
- **Mac `failed_messages`:** one `iso2022_jp` decode failure from
  2026-07-25 (`hherb-dmc/[Gmail]/Important` uid 5263). Not investigated.
- **Issues:** #383, #369, #372, #368, #365; #375; #357; #327; #340, #363,
  #285; robustness #218, #226, #225/#227, #200/#211/#208, #206, #204, #25.

## Rulings made this session

1. **The operator picked #330** and asked for both hosts to be deployed at
   `753133c` first.
2. **Import the aliases, not the Enum** (mine): the smallest change that
   closes the drift; the Enum would rewrite `date_keyset` for no wire gain.

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
10. **Load on the Mac was ~7–30 this session** (full suite 5 min; a
    bmlibrarian pytest was running beside it). Check
    `uptime` before trusting any timing.

## Exact commands to resume

```bash
cd /Users/hherb/src/localmail
git fetch --prune origin                 # ALWAYS first
git switch main && git pull --ff-only
git log --oneline -1 origin/main         # THE authority
git diff main origin/fix/330-sort-vocabulary --stat   # empty once merged (squash)
gh pr list --state all --limit 3
gh api 'repos/{owner}/{repo}/dependabot/alerts?state=open&per_page=100' --jq length   # 2 once Dependabot rescans
gh issue list --limit 200 --json number --jq length

# Deploy — Mac (the repo is the install); restart BOTH
unset VIRTUAL_ENV && uv sync --frozen --extra mcp --extra extraction
launchctl kickstart -k gui/$(id -u)/com.localmail.serve
launchctl kickstart -k gui/$(id -u)/com.localmail.daemon
curl -sk https://127.0.0.1:8443/v1/version     # build_hash = merge SHA

# Deploy — DGX
ssh 10.0.0.3 'cd ~/src/localmail && git pull --ff-only && ~/.local/bin/uv sync --frozen --extra mcp --extra extraction && systemctl --user restart localmail-daemon localmail-serve && systemctl --user is-active localmail-daemon localmail-serve'
ssh 10.0.0.3 'curl -sk https://10.0.0.3:8443/v1/version'

# Load before trusting any timing; no other LOCALMAIL pytest may be running.
uptime; pgrep -fl pytest

# Suites.
unset VIRTUAL_ENV && uv run pytest -q              # 4716 at session 90
uvx mypy --python-executable .venv/bin/python src/localmail   # Success, 169 files
cd gui && npm test -- --run && npm run check   # 530 at session 81
```

The latest migration is **`0038_messages_attachment_names_strict_idx.sql`**;
the next free slot is `0039_*.sql`.
