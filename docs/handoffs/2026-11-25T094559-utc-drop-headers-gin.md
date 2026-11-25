# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-11-25 (session 101).** `main` is **`c206ad7`** (#470
> merged: mail-parser/soupsieve floors).
>
> **One open PR, #471**, on `fix/383-drop-headers-gin`, based on `main` at
> `c206ad7`, carrying migration `0039` **and this handoff**.
>
> **Both hosts are deployed at `eef2972`** (carried, not re-checked).
> #467–#470 need no deploy. **#471 does**: `localmail init-db` before the
> restart, since `serve` refuses to start with a pending migration.
>
> **Open issues: 21** before #471 (it closes #383). **Open Dependabot alerts:
> 1** (#71, accelerate, no fixed release). #201–#209 closed this session
> once GitHub's dependency graph re-read `uv.lock`, ~10 h after #470 merged.
>
> **`hherb-dmc`'s Gmail refresh token is expired on the Mac** (carried, not
> re-checked). Fix (yours, needs a browser): `localmail oauth-login
> hherb-dmc`, then `launchctl kickstart -k gui/$(id -u)/com.localmail.daemon`.
>
> **Leftover worktree:** `.claude/worktrees/bottie-bearer-token-dbc203`
> (carried; ask before removing). `../localmail-depbump` was removed.

## Project context (1-minute version)

`localmail` mirrors IMAP accounts (Gmail OAuth or password) into Postgres, and
is **strictly read-only w.r.t. IMAP**. The database is canonical for accounts.

- **Daemon** (IDLE + poll per account), **web admin UI** (HTMX).
- **Hybrid search**, the HTTPS `/v1` server, a remote **MCP server**, and the
  opt-in `--smart` rewriter.
- **Tauri 2 + Svelte 5 GUI** under `gui/`.
- **kastellan** consumes `/v1` over REST with an API key.

## What we shipped this session

- **Confirmed #470 merged** (`c206ad7`); removed `../localmail-depbump` and
  its local branch. Dependabot #201–#209 closed during the session (the
  SBOM still listed the old versions at session start: graph lag, not a
  bad lock).
- **Operator picked #383** over the GUI JSON caps, #285 and #368.

### PR #471 on `fix/383-drop-headers-gin`

| SHA | What |
|---|---|
| `a451374` | `migrations/0039_drop_messages_headers_gin.sql` drops `messages_headers_gin` (355 MB on the Mac, more than the 260 MB heap; nothing reads the column since #379). Column kept as the sync-time snapshot (#383 option 1). New `tests/test_messages_headers_snapshot.py` (RED first); `test_api_browse_plan.py` loses the index from its three competing-index lists. CLAUDE.md: the #379 "write-only" note becomes the snapshot rule; latest migration `0039`, next `0040` |
| next | this handoff |

- **Verification:** full suite `4767 passed` (7:24, load ~42); the new
  test failed before the migration and passes after.
- **Not run:** mypy (no `src/` change); the migration against the live
  archive (that is the deploy step).

## What's next

### 1. **Merge #471** (yours), once CI is green, then deploy
- **Acceptance:** `gh pr checks 471` all pass; merged; #383 closed (verify
  with `gh issue view 383`).
- **Deploy (both hosts):** `git pull`, `uv run localmail init-db` (applies
  `0039`; a catalog-only drop, but it waits behind the running daemon's open
  transactions on `messages` and every query queues behind it meanwhile, so
  expect a short stall and a few `QueryCanceled` retries, or stop the daemon
  first), restart both services. Then
  `select 1 from pg_indexes where indexname='messages_headers_gin'` returns
  no row on each host.

### 2. **Re-authorise `hherb-dmc`** (yours, browser; carried)
- **Acceptance:** no new `crashed for hherb-dmc` in `daemon.err.log` after
  the kickstart; its `max(date_received)` moves past 2026-11-03.

### 3. **Next small slice: pick with the operator**
- Candidates: the unbounded **GUI JSON helpers** in `http/client.rs` (not
  filed; reuse `body::read_capped`), **#285** (ruff/mypy in CI; ruff-pin
  call), **#368** (MCP drops unknown args; design call), **#369** (DSL
  contradictions; design calls), #365, #363.
- **Acceptance (any):** a RED test first, then the touched suite green
  (`uv run pytest -q` + mypy for Python; `cargo test` + `clippy --all-targets`
  for Rust; `npm test -- --run && npm run check` for TS).
- **Tell kastellan** (yours; carried): problem+json 400s on `/v1/search`,
  `filters.lang` as a list, #419 syntax, #415 dates, `api_minor` 5 and 6
  features, #373 (`'` is literal), #378 (an empty
  `after:`/`before:`/`has:` or a non-int id in `query` is a 400).

### 4. **Carried**
- **Migrations:** `0039` is taken (#471). #318 and #320 need `0040`.
- **#414 watchdog:** waits for a stack dump (SIGUSR1 works on the DGX).
- **#459:** harness-lock DSN residual (helpers, rebinding).
- **`ApiKeyNotFound` still derives from bare `Exception`** (latent).
- **GUI JSON helpers** in `http/client.rs` still read unbounded bodies.
- **Dependabot #71:** left open on purpose (no fixed release).
- **docling > 2.144:** probe the next release when it lands.
- **emSign G3:** re-check the trust stores after OS updates.
- **kastellan slice C:** ask whether it is still wanted.
- **Mac `failed_messages`:** one `iso2022_jp` decode failure from
  2026-07-25 (`hherb-dmc/[Gmail]/Important` uid 5263). Not investigated.
- **docling deprecations in the suite:** `force_full_page_ocr` is
  deprecated (pydantic warning, docling 2.142); check whether our
  `_ocr_the_whole_page` or docling itself sets it before the next bump.
- **Issues:** #369, #368, #365; #375; #357; #363, #285;
  robustness #226, #225/#227, #200/#211, #206, #204, #25.

## Rulings made this session

1. **Operator picked #383**, option 1 (keep the column, drop the index).

## Open decisions & risks

0a. **A stalled GUI download waits up to 300 s** (carried, #469)
    before it says `Timeout: {"seconds":300}` (was 15 s), and a dead host
    takes the OS connect timeout (~75 s on macOS) where it took 15 s. The
    GUI has no cancel button for a download in flight. A client `connect_timeout` was
    not added: it would be reported as the 300 s budget.
0b. **`write_replacing` limits**: two marked `shortcut:` (an overwritten
    file's permissions are not carried over, a symlink at the destination
    is replaced rather than followed), plus a SIGKILL or panic mid-write
    strands a hidden `.localmail-<pid>-<nanos>.part` beside it, and a
    writable file in a read-only folder can no longer be overwritten (the
    error now names the temp).
0. **Behaviour changes still worth knowing** (carried): since #438
   `localmail import` exits 1 on a failed job; #441 a contended poison-pill
   record fails the job / aborts the sync pass; #442 a final status write that
   fails 5 times (~40 s) exits 1 and leaves the row `running` until serve
   restarts; #440 a re-imported pill bumps its existing `failed_messages` row;
   #373 `'` in a search query is literal; #374 a query operator out-votes its
   `localmail search` flag; #305 a malformed `localmail search` query exits 2,
   and a missing dependency makes `--version` exit 0 with a stderr line;
   #378 an empty fixed-shape operator or non-int id in a query is a 400 /
   exit 2; #372 a page can hold fewer hits than `pool_size`
   suggested only if a message was deleted mid-search; #319 a person's id now reads "… is a person, not an API-key principal"
   in the 404 detail of all three JSON key routes (revoke, delete, grants);
   #218 a GUI download over 100 MiB without `Content-Length` fails with a
   lower-bound `size` instead of buffering first. #462
   `revoke-api-key`/`remove-api-key` on a person's username print
   `'<name>' is a person's account, not an API key`, and the panel's refusal
   branch logs one WARNING on `localmail.serve.admin`. #208 the GUI
   daemon tab's "Request accepted." clears after 5 s. #464 GUI
   download errors carry their cause; #465 a failed save leaves the chosen
   file as it was; #469 a GUI download times out after 300 s, not 15 s.
1. **Repair the pre-#431 `-0000` rows?** Mac: 2,865 of 132,320 rows are
   stored 8 h off in `date_sent` (wire `date` is right). A repair should
   re-parse `raw_bytes`. Read-only count:
   `select count(*) from messages where exists (select 1 from jsonb_array_elements_text(case when jsonb_typeof(headers->'Date')='array' then headers->'Date' else '[]' end) d where d ~ '-0000\s*(\(.*\))?\s*$')`
2. **`attachment_name` is unindexed** (~0.2 s warm at 132k). A trigram index
   would be `0040`.
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
10. **Load on the Mac was ~42 in sessions 100–101** (drugref and consensus
    pytest runs, not localmail). Check `uptime` before trusting any timing.

## Exact commands to resume

```bash
cd /Users/hherb/src/localmail
git fetch --prune origin                 # ALWAYS first
git switch main && git pull --ff-only
git log --oneline -1 origin/main         # THE authority
gh pr list --state all --limit 4
gh pr checks 471
gh api 'repos/{owner}/{repo}/dependabot/alerts?state=open&per_page=100' --jq '.[].number'
gh issue list --limit 200 --json number --jq length

# Deploy — Mac (the repo is the install); migrate, then restart BOTH
unset VIRTUAL_ENV && uv sync --frozen --extra mcp --extra extraction
uv run localmail init-db
launchctl kickstart -k gui/$(id -u)/com.localmail.serve
launchctl kickstart -k gui/$(id -u)/com.localmail.daemon
curl -sk https://127.0.0.1:8443/v1/version     # build_hash = merge SHA

# Deploy — DGX
ssh 10.0.0.3 'cd ~/src/localmail && git pull --ff-only && ~/.local/bin/uv sync --frozen --extra mcp --extra extraction && ~/.local/bin/uv run localmail init-db && systemctl --user restart localmail-daemon localmail-serve && systemctl --user is-active localmail-daemon localmail-serve'
ssh 10.0.0.3 'curl -sk https://10.0.0.3:8443/v1/version'

# Load before trusting any timing; no other LOCALMAIL pytest may be running.
uptime; pgrep -fl pytest

# Suites.
unset VIRTUAL_ENV && uv run pytest -q              # 4767, session 101 (7:24 under load ~42)
uvx mypy --python-executable .venv/bin/python src/localmail   # Success, 169 files
cd gui && npm test -- --run && npm run check   # 540, session 99
cd gui/src-tauri && cargo test --lib && cargo clippy --all-targets -- -D warnings   # 147, session 99
```

The latest migration is **`0039_drop_messages_headers_gin.sql`** (#471);
the next free slot is `0040_*.sql`.
