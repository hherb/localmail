# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-11-07 (session 94).** `main` is **`84ed6ee`** (#458:
> harness lock DSN rule, closes #340).
>
> This session has **two open PRs**, both based on `main` at `84ed6ee`:
> **#460** (`fix/372-ghost-hits`, code only, CI green when written) and the
> **#461** (`fix/319-not-a-key-principal`), which carries its code **and this
> handoff**. They touch different files and can merge in either order.
>
> **Both hosts are deployed at `2234775`** (carried from session 93; #458 was
> test-only). #460 and #319 change runtime code, so **deploy after merging**.
>
> **Open issues: 26** (24 once both PRs close #372 and #319; #462, filed by
> #461's pre-merge review, holds its two optional low-severity leftovers). **Open
> Dependabot alerts: 2** — #71 and #185, both open on purpose.
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

- **Confirmed #458 merged** (`84ed6ee`) and #340 closed.
- **The 6 test warnings are all third-party deprecations** (starlette's
  `anyio.abc.BlockingPortal`, three from docling, the two websockets ones of
  #25). The `force_full_page_ocr` one fires inside docling's own serializer
  even though we set `mode=FULL_PAGE`. Nothing to fix. Earlier sessions
  recorded 2 because their runs had no `[extraction]` docling tests.

### PR #460 on `fix/372-ghost-hits`

| SHA | What |
|---|---|
| `108d648` | `Searcher._hydrate` skips a fused hit whose message is gone (DEBUG line) instead of serving `msgs.get(id, {})` as a ghost row; keeps it out of the rerank input and the cached pool too. The `has_attachments … if m else False` guard (ghost-only) removed; its pin replaced by a DB test of `_hydrate` (`test_search_has_attachments_flag.py`). CLAUDE.md: one line under `_fetch_sort_dates` |

### PR #461 on `fix/319-not-a-key-principal`

| SHA | What |
|---|---|
| `471d303` | `api/admin/api_keys.py`: `NotAnApiKeyPrincipal(ApiKeyNotFound)` raised by `revoke_key`, `delete_key_principal` and `set_grant` when the id is a person's (via `_not_found`). Panel's revoke/delete catch it and render a `role="alert"` "Refused: …" notice above the table on a 200. JSON routes (404) and CLI unchanged (subclass), though the JSON 404 detail and the CLI error message now name the person case. 5 new tests + 3 tightened; the finalcheck review added 4 more (person's id on all three JSON routes, unknown id plain at delete/grant, repeated revoke/delete shows no refusal). CLAUDE.md API-keys bullet |
| next | this handoff |

- **Verification:** both RED first. Mutations caught: #319 — dropping the
  panel's notice (2 tests fail), making `_not_found` never see a person (6
  fail since the finalcheck round; it was 5, not the 3 first written), a
  notice on the panel's plain not-found branch (1 fail, added by finalcheck). #372's fix was RED-then-green; no separate mutation run. Full suite
  **4761 passed, 6 warnings** on the #319 branch (4752 on #460's); mypy clean,
  169 files.
- **Not done (#319's "related" half):** `ApiKeyNotFound` still derives from
  bare `Exception`, not `api.errors.NotFound`. Every route catches it
  explicitly, so it is latent; noted in CLAUDE.md.

## What's next

### 1. **Merge both PRs** (yours), once CI is green
- **Acceptance:** `gh pr checks 460` and `gh pr checks 461` all pass; #372 and #319
  close (`Closes #N` in each body); `gh issue list` shows 23.

### 2. **Deploy both hosts** after merging (runtime code changed)
- **Acceptance:** `/v1/version` on both hosts reports the newer merge SHA.

### 3. **Re-authorise `hherb-dmc`** (yours, browser; carried)
- **Acceptance:** no new `crashed for hherb-dmc` in `daemon.err.log` after
  the kickstart; its `max(date_received)` moves past 2026-11-03.

### 4. **Next small slice: pick with the operator**
- Candidates: **#368** (MCP ignores unknown args — no supported FastMCP
  hook, needs a design call), **#208** (DaemonPanel message never
  dismisses; small GUI), **#218** (GUI download buffers before the size
  cap), **#285** (ruff/mypy in CI — needs a ruff-pin call), #369, #383
  (`0039`), #365, #363.
- **Acceptance (any):** a RED test first, then full `uv run pytest -q`
  green and mypy clean.
- **Tell kastellan** (yours; carried): problem+json 400s on `/v1/search`,
  `filters.lang` as a list, #419 syntax, #415 dates, `api_minor` 5 and 6
  features, #373 (`'` is literal), #378 (an empty
  `after:`/`before:`/`has:` or a non-int id in `query` is a 400).

### 5. **Carried**
- **Migrations:** `0038` is taken. #319's sibling #318 and #320 need `0039`.
- **#414 watchdog:** waits for a stack dump (SIGUSR1 works on the DGX).
- **#459:** harness-lock DSN residual (helpers, rebinding).
- **Dependabot #71 / #185:** left open on purpose.
- **docling > 2.144:** probe the next release when it lands.
- **emSign G3:** re-check the trust stores after OS updates.
- **kastellan slice C:** ask whether it is still wanted.
- **Mac `failed_messages`:** one `iso2022_jp` decode failure from
  2026-07-25 (`hherb-dmc/[Gmail]/Important` uid 5263). Not investigated.
- **Issues:** #383, #369, #368, #365; #375; #357; #363, #285;
  robustness #218, #226, #225/#227, #200/#211/#208, #206, #204, #25.

## Rulings made this session

1. **Operator picked "both, one PR each"** for #372 and #319.
2. **#372 fixed in `_hydrate`, not `_build_results`** (mine): the issue
   proposed the page builder, but the hydrated pool is what gets reranked and
   cached, so dropping there removes the ghost from every downstream stage.
3. **#319 as a subclass, not a `RevokeOutcome` enum** (mine): one new class,
   zero changes to the JSON route and CLI.

## Open decisions & risks

0. **Behaviour changes still worth knowing** (carried): since #438
   `localmail import` exits 1 on a failed job; #441 a contended poison-pill
   record fails the job / aborts the sync pass; #442 a final status write that
   fails 5 times (~40 s) exits 1 and leaves the row `running` until serve
   restarts; #440 a re-imported pill bumps its existing `failed_messages` row;
   #373 `'` in a search query is literal; #374 a query operator out-votes its
   `localmail search` flag; #305 a malformed `localmail search` query exits 2,
   and a missing dependency makes `--version` exit 0 with a stderr line;
   #378 an empty fixed-shape operator or non-int id in a query is a 400 /
   exit 2. **New:** #372 a page can hold fewer hits than `pool_size`
   suggested only if a message was deleted mid-search; #319 a person's id now reads "… is a person, not an API-key principal"
   in the 404 detail of all three JSON key routes (revoke, delete, grants)
   and in `revoke-api-key` / `remove-api-key`'s CLI error.
1. **Repair the pre-#431 `-0000` rows?** Mac: 2,865 of 132,320 rows are
   stored 8 h off in `date_sent` (wire `date` is right). A repair should
   re-parse `raw_bytes`. Read-only count:
   `select count(*) from messages where exists (select 1 from jsonb_array_elements_text(case when jsonb_typeof(headers->'Date')='array' then headers->'Date' else '[]' end) d where d ~ '-0000\s*(\(.*\))?\s*$')`
2. **`attachment_name` is unindexed** (~0.2 s warm at 132k). A trigram index
   would be `0039`.
3. **Should a wire widening bump `api_minor`?** Still open.
4. **3,194 rows keep a skewed `internal_date`** (Mac 2,720, DGX 474). Not
   decided.
5. **THIS FILE IS NOT THE AUTHORITY; `git` and `gh` are.** After a squash
   merge `main..origin/<branch>` is non-empty by construction; compare with
   `git diff main origin/<branch> --stat` (empty = landed; errors once the
   branch is deleted).
6. **Never run two localmail pytest sessions against one test DB** (port
   5532, the same cluster as the live archive).
7. **Restore a mutation from a file copy, never with `git checkout`.**
8. **Always sync with `--extra mcp --extra extraction`.** On the DGX use
   `~/.local/bin/uv`. `uv sync --dry-run` is not read-only.
9. **No ROADMAP.md.** That `/nextsession` step is a no-op.
10. **Load on the Mac was 7–18 this session.** Check `uptime` before
    trusting any timing.

## Exact commands to resume

```bash
cd /Users/hherb/src/localmail
git fetch --prune origin                 # ALWAYS first
git switch main && git pull --ff-only
git log --oneline -1 origin/main         # THE authority
gh pr list --state all --limit 4
gh pr checks 460; gh pr checks 461
gh api 'repos/{owner}/{repo}/dependabot/alerts?state=open&per_page=100' --jq '.[].number'
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
unset VIRTUAL_ENV && uv run pytest -q              # 4761 on the #319 branch, session 94
uvx mypy --python-executable .venv/bin/python src/localmail   # Success, 169 files
cd gui && npm test -- --run && npm run check   # 530 at session 81
```

The latest migration is **`0038_messages_attachment_names_strict_idx.sql`**;
the next free slot is `0039_*.sql`.
