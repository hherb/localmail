# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-11-26 (session 102).** `main` is **`e98125a`** (#471
> merged: migration `0039`).
>
> **One open PR, #472**, on `fix/search-uid-windows`, based on `main` at
> `e98125a`, carrying the SEARCH-window fix **and this handoff**.
>
> **Both hosts are deployed at `e98125a`** this session, `0039` applied on
> each, `messages_headers_gin` gone on each (checked). **#472 needs a Mac
> deploy** (restart the daemon; no migration). The DGX lacks the affected
> account, but deploy it too to keep the hosts level.
>
> **Open issues: 20.** **Open Dependabot alerts: 1** (#71, accelerate, no
> fixed release).
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

- **Deployed #471 to both hosts**: daemon stopped, `init-db` applied `0039`
  (under a second on each), both services restarted, `build_hash` `e98125a`
  on each, `messages_headers_gin` absent on each. The sync also pulled #470's
  soupsieve 4.1.3 onto both venvs (they had not been re-synced since).
- **Found a live crash-loop while reading the Mac logs**: the
  `List-subscriptions` account (added 2026-11-07) crashed its poll pass ~1,000
  times a day from 2026-11-19 with `imaplib.IMAP4.error: SEARCH => got more
  than 1000000 bytes`. Its `[Gmail]/All Mail` had never synced (`uidnext`
  NULL), so every pass was a full `SEARCH ALL`, whose one-line reply is over
  imaplib's `_MAXLINE`. The operator chose to fix it ahead of the GUI JSON
  caps.

### PR #472 on `fix/search-uid-windows`

| SHA | What |
|---|---|
| `6c34f6f` | `uids.UID_SEARCH_WINDOW` (50,000) + pure `uids.uid_search_specs`: closed windows over `first .. UIDNEXT-1`, last one open (`lo:*`). `sync_mailbox` runs one `UID` SEARCH per window (full sync asks `UID 1:*`, not `ALL`). `FakeIMAPClient.max_search_results` models the cap. New `tests/test_sync_uid_windows.py` (12 tests, RED first). CLAUDE.md note under the sync model |
| next | this handoff |

- **Verification:** full suite `4779 passed` (10:09, load ~97 from other
  projects); mypy clean (169 files). Mutation: forcing a 10⁹ window fails
  both sync tests.
- **Not run:** the fix against the real `List-subscriptions` All Mail (that
  is the deploy step).

## What's next

### 1. **Merge #472** (yours), once CI is green, then deploy the Mac
- **Acceptance:** `gh pr checks 472` all pass; merged.
- **Deploy (Mac):** `git pull`, `uv sync …`, kickstart the daemon. Then no
  new `got more than 1000000 bytes` in `daemon.err.log`, and
  `select uidnext from mailboxes where account_id=61 and name='[Gmail]/All Mail'`
  turns non-NULL once the first full pass checkpoints (All Mail is large, so
  the first pass will take a while; the embed/lang workers will have a
  backlog after it).
- DGX: pull and restart too, to keep the hosts level.

### 2. **Re-authorise `hherb-dmc`** (yours, browser; carried)
- **Acceptance:** no new `crashed for hherb-dmc` in `daemon.err.log` after
  the kickstart; its `max(date_received)` moves past 2026-11-03.
- Note: `horst-gmail` also logs ~30 poll crashes a day since 2026-11-22.
  Not looked at; check the traceback before assuming it is the same cause.

### 3. **GUI JSON body caps** (the slice the operator picked this session)
- The JSON helpers in `gui/src-tauri/src/http/client.rs` (`.json()` and the
  error-path `resp.text()` in all six) read unbounded bodies. Reuse
  `body::read_capped`, then `serde_json::from_slice`.
- **Acceptance:** a RED mockito test per helper with a finite chunked body
  past the cap (`body::test_support::write_past`), reporting a size near the
  cap; `cargo test --lib` + `cargo clippy --all-targets -- -D warnings` clean.
- Other candidates: **#285**, **#368**, **#369**, #365, #363.
- **Tell kastellan** (yours; carried): problem+json 400s on `/v1/search`,
  `filters.lang` as a list, #419 syntax, #415 dates, `api_minor` 5 and 6
  features, #373 (`'` is literal), #378.

### 4. **Carried**
- **Migrations:** `0039` is taken. #318 and #320 need `0040`.
- **#414 watchdog:** waits for a stack dump (SIGUSR1 works on the DGX).
- **#459:** harness-lock DSN residual (helpers, rebinding).
- **`ApiKeyNotFound` still derives from bare `Exception`** (latent).
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

1. **Deploy #471 to both hosts** (done).
2. **Fix the SEARCH crash before the GUI JSON caps**, by windowing the
   SEARCH rather than raising `imaplib._MAXLINE`.

## Open decisions & risks

00. **A full sync now costs one SEARCH per 50,000 UIDs** (#472) where it
    cost one. On All Mail that is a handful of round trips, each bounded by
    `imap_timeout_s`. A server whose UIDNEXT is far below its real UIDs would
    still put every UID past it in the one open window; not seen on Gmail.
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
10. **Load on the Mac was ~42 in sessions 100–101 and ~97 in 102** (drugref and consensus
    pytest runs, not localmail). Check `uptime` before trusting any timing.

## Exact commands to resume

```bash
cd /Users/hherb/src/localmail
git fetch --prune origin                 # ALWAYS first
git switch main && git pull --ff-only
git log --oneline -1 origin/main         # THE authority
gh pr list --state all --limit 4
gh pr checks 472
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
unset VIRTUAL_ENV && uv run pytest -q              # 4779, session 102 (10:09 under load ~97)
uvx mypy --python-executable .venv/bin/python src/localmail   # Success, 169 files
cd gui && npm test -- --run && npm run check   # 540, session 99
cd gui/src-tauri && cargo test --lib && cargo clippy --all-targets -- -D warnings   # 147, session 99
```

The latest migration is **`0039_drop_messages_headers_gin.sql`** (#471);
the next free slot is `0040_*.sql`.
