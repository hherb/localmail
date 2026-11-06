# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-11-06 (session 91).** `main` is **`1e3b3a8`** (#455:
> #330, the transports import `SortMode`/`SortOrder`).
>
> This session has **one open PR** on `fix/327-cursor-plan-types` (#327). It
> carries the code and this handoff.
>
> **Both hosts are deployed at `753133c`** (checked this session via
> `/v1/version`). #330 and #327 change no wire behaviour on any normal path;
> deploy both after #327 merges.
>
> **Open issues: 26** (25 once this PR merges and closes #327). **Open
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

- **#330 merged** as `1e3b3a8` (PR #455) before the session started.

### PR on `fix/327-cursor-plan-types` (based on `main` at `1e3b3a8`)

| SHA | What |
|---|---|
| `8bb13fe` | #327: `resolve_cursor_plan` returns `FreshPlan(sort, sort_order)` / `KeysetPlan(cursor)` / `PoolPlan(cursor, stated_sort, stated_sort_order)`; `run_search` is a `match` over them; CLAUDE.md note |
| handoff | this file + `docs/handoffs/2026-11-06T0800Z-327-cursor-plan-types.md` |

- **The defect it fixes:** a pool cursor paged with `sort_order="asc"` was
  refused by the fresh-request rank+asc rule, reasoning from a `sort`
  resolved to its default, before the pool was consulted. Now that rule is
  `FreshPlan`-only, and the request reaches `_check_pool_sort`, which names
  the pool's real order (`this cursor continues a descending search`). The
  "cannot be carried over" remedy string is gone.
- **Edge-path wire changes:** a malformed **pool** cursor is now a 400
  ahead of the empty-ACL short-circuit (it was a 200 empty page for a
  grant-nothing caller); `reject_pool_sort_mismatch`'s order half is now
  reachable end to end. `run_search` decodes each cursor once.
- Tests: `tests/test_api_search_cursor_plan_types.py` (8). RED first: the
  two `run_search` tests failed on `main` with the old default-`rank`
  message. 12 existing tests updated (type equality, two pool tests), one
  deleted (the "cannot be carried over" pin).
- Mutations (file snapshot, restored from copy): rank+asc re-applied to
  pools, the pool arm dropping the stated order or sort, the empty-ACL
  branch using a default for a pool, and removing rank+asc — each fails a
  test. The empty-ACL one survived at first; the last test in the new file
  pins it.

## Verification

| check | result |
|---|---|
| `uv run pytest -q` | **4723 passed**, 6 warnings (pre-existing), 5:28 at load ~23 |
| mypy | Success, 169 files |
| GUI | untouched |
| CI | not yet run when this was written; check `gh pr checks` |

## What's next

### 1. **Merge the #327 PR** (yours), once CI is green
- **Acceptance:** `gh pr checks <PR>` all pass; `gh issue view 327` closed.

### 2. **Deploy `main` to both hosts** after the merge (#330 + #327)
- **Acceptance:** `/v1/version` on both reports the merge SHA.

### 3. **Re-authorise `hherb-dmc`** (yours, browser; carried)
- **Acceptance:** no new `crashed for hherb-dmc` in `daemon.err.log` after
  the kickstart; its `max(date_received)` moves past 2026-11-03.

### 4. **Next small slice: pick with the operator**
- The #322 type-design follow-ups are done with #327. Candidates:
  **#285** (does mypy/ruff gate CI — needs a ruff-pin call), **#357**
  (only worth it with #285), #369 (refuse-vs-keep call), #383 (one-way
  door, `0039`), #365 (product call), #340 (harness lock DSN arguments),
  #363 (rerank degradation on the wire — needs a renderer).
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
- **Issues:** #383, #369, #372, #368, #365; #375; #357; #340, #363,
  #285; robustness #218, #226, #225/#227, #200/#211/#208, #206, #204, #25.

## Rulings made this session

1. **The operator picked #327.**
2. **The issue's three-type shape, not its cheaper `__post_init__`
   alternative** (mine): the alternative leaves the pool plan answering an
   ordering question it cannot know, which is the defect.
3. **The decoded cursor rides on the plan** (mine): it makes the `match`
   total without narrowing `cursor`, and retires the double decode.

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
10. **Load on the Mac was ~23 this session** (full suite 5½ min; a
    bmlibrarian pytest was running beside it). Check
    `uptime` before trusting any timing.

## Exact commands to resume

```bash
cd /Users/hherb/src/localmail
git fetch --prune origin                 # ALWAYS first
git switch main && git pull --ff-only
git log --oneline -1 origin/main         # THE authority
git diff main origin/fix/327-cursor-plan-types --stat   # empty once merged (squash)
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
unset VIRTUAL_ENV && uv run pytest -q              # 4723 at session 91
uvx mypy --python-executable .venv/bin/python src/localmail   # Success, 169 files
cd gui && npm test -- --run && npm run check   # 530 at session 81
```

The latest migration is **`0038_messages_attachment_names_strict_idx.sql`**;
the next free slot is `0039_*.sql`.
