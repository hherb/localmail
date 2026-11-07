# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-11-07 (session 98).** `main` is **`dd9f54c`** (#467,
> closes #208).
>
> **One open PR** on `fix/464-465-download-errors`, based on `main` at
> `dd9f54c`, carrying the #464/#465 code **and this handoff**.
>
> **Both hosts are deployed at `eef2972`** (carried, not re-checked). #467
> and this PR are GUI-only: no server deploy needed for either.
>
> **Open issues: 23** (21 once the PR closes #464 and #465). **Open
> Dependabot alerts: 2**: #71 and #185, both open on purpose (carried).
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

- **Confirmed #467 merged** (`dd9f54c`), which closed #208; 23 issues open.
- **Operator picked #464 + #465.**

### PR on `fix/464-465-download-errors`

| SHA | What |
|---|---|
| `da12c93` | **#464:** `BodyError::Timeout` (from `is_timeout()`); `body::error_chain` appends each `source()` not already in the text; `AttachmentError`/`RawMessageError` gain `Timeout { seconds }` and `send_failed` (timeout, else `Network(error_chain)`). **#465:** new `gui/src-tauri/src/save_file.rs::write_replacing` (hidden `create_new` temp beside `dest`, `write_all` + `sync_all`, `rename`, temp removed on failure), called on `spawn_blocking` by `download_attachment`. Attachment tests split to `attachments_tests.rs` (the module would have been ~600 lines with the new tests). README (GUI "Downloads" paragraph) and CLAUDE.md (#218 bullet's follow-ups) updated |
| `a298e35` | doc fix from review: 100 MiB in 15 s needs ~56 Mbit/s |
| `2b49a6f` | this handoff |
| next | **`/finalcheck` review fixes:** the three download/preview/raw views rendered every command error as `[object Object]` (`String(e)` on Tauri's `{kind, detail}` rejection), so none of #464 reached the screen; they use `formatError` now, which also renders a JS `Error` by its message. The temp name is `.localmail-<pid>-<nanos>.part`, no longer embedding the user's name (a ~225-byte name failed with "File name too long"). A temp that cannot be created says so; the file is closed before the rename; a failed save is pinned to fail the command. CLAUDE.md and comment corrections |

- **Verification:** `cargo test --locked` **144 passing** (123 → 134 → 141 →
  144); `cargo clippy --all-targets -- -D warnings` clean; `cargo fmt`
  applied; `npm test -- --run` **540 passing**, `npm run check` clean. No
  Python touched, so pytest and mypy were not re-run.
- **Mutations, each failing its own test(s):** body read back to
  `Read(e.to_string())` (2 fail); `AttachmentError::send_failed` back to
  `to_string` with no timeout branch (4; `RawMessageError`'s fails 3); `error_chain` without its dedup (1); each call site
  back to `Network(e.to_string())` (3, one per command); temp clean-up
  removed (2); truncate-then-write inside `replace_with` (1). Review round: the temp
  name embedding `dest`'s (2); no temp context in the error (1); dropping the
  `?` on the save's `Write` mapping (1); each view back to `String(e)` (1
  each, 3).
- **Known unpinned:** `download_attachment` calling `write_replacing` vs
  `fs::write` (they agree on success; said so in the test and CLAUDE.md),
  and a timeout driven *through* a command (15 s; `send_failed` is pinned
  directly instead).
- **Review (one agent):** nothing significant; one doc number fixed.
- **`/finalcheck` review (four agents):** found the `[object Object]`
  rendering (pre-existing, but it hid the whole of #464), the long-name
  regression, the untested failed-save path and four false doc claims; all
  fixed above. Left as noted, not filed: `send_failed` duplicates
  `HttpError::from_reqwest` and so reports a pin mismatch as `Network`;
  a failed temp removal is swallowed; no directory fsync after the rename;
  the refused-port tests reuse a freed port (a tiny race).

## What's next

### 1. **Merge the #464/#465 PR** (yours), once CI is green
- **Acceptance:** `gh pr checks` all pass; #464 and #465 close (`Closes`
  lines in the PR body); `gh issue list` shows 21. No deploy (GUI-only).

### 2. **Re-authorise `hherb-dmc`** (yours, browser; carried)
- **Acceptance:** no new `crashed for hherb-dmc` in `daemon.err.log` after
  the kickstart; its `max(date_received)` moves past 2026-11-03.

### 3. **Next small slice: pick with the operator**
- Candidates: **download timeout budget** (decision below), **#368** (MCP drops unknown args; design call),
  **#285** (ruff/mypy in CI; ruff-pin call), **#369** (DSL contradictions;
  design calls), #383 (`0039`), #365, #363.
- **Acceptance (any):** a RED test first, then the touched suite green
  (`uv run pytest -q` + mypy for Python; `cargo test` + `clippy --all-targets`
  for Rust; `npm test -- --run && npm run check` for TS).
- **Tell kastellan** (yours; carried): problem+json 400s on `/v1/search`,
  `filters.lang` as a list, #419 syntax, #415 dates, `api_minor` 5 and 6
  features, #373 (`'` is literal), #378 (an empty
  `after:`/`before:`/`has:` or a non-int id in `query` is a 400).

### 4. **Carried**
- **Migrations:** `0038` is taken. #318 and #320 need `0039`.
- **#414 watchdog:** waits for a stack dump (SIGUSR1 works on the DGX).
- **#459:** harness-lock DSN residual (helpers, rebinding).
- **`ApiKeyNotFound` still derives from bare `Exception`** (latent; every
  route catches it explicitly).
- **GUI JSON helpers** in `http/client.rs` still read unbounded bodies (noted
  in CLAUDE.md, not filed).
- **Dependabot #71 / #185:** left open on purpose.
- **docling > 2.144:** probe the next release when it lands.
- **emSign G3:** re-check the trust stores after OS updates.
- **kastellan slice C:** ask whether it is still wanted.
- **Mac `failed_messages`:** one `iso2022_jp` decode failure from
  2026-07-25 (`hherb-dmc/[Gmail]/Important` uid 5263). Not investigated.
- **Issues:** #383, #369, #368, #365; #375; #357; #363, #285;
  robustness #226, #225/#227, #200/#211, #206, #204, #25.

## Rulings made this session

1. **Operator picked #464 + #465** over #285, #368 and #383.
2. **The 15 s budget is unchanged** (mine). #464 asked only to "consider"
   it; a longer per-request download timeout is a behaviour change worth
   its own call (open decision 0a below).
3. **The `Timeout` wire shape matches `HttpError::Timeout`**
   (`{kind:"Timeout", detail:{seconds}}`, rendered `Timeout: {"seconds":15}`)
   (mine). A nicer `formatError` rendering would cover both; not done.
   (Until the review round the views did not call `formatError` at all.)
4. **No `tempfile` dependency** (mine): `write_replacing` is std-only.

## Open decisions & risks

0a. **Download timeout budget.** Every GUI request, body included, has 15 s
    (`REQUEST_TIMEOUT_SECS`). A 100 MiB attachment needs ~56 Mbit/s to fit;
    on WireGuard it now fails *legibly* (`Timeout`) but still fails. Option:
    `RequestBuilder::timeout(DOWNLOAD_TIMEOUT_SECS)` on the three download
    requests (and teach the error its own number). Your call.
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
   daemon tab's "Request accepted." clears after 5 s. **New:** #464 GUI
   download errors read `Timeout: {"seconds":15}` or carry their cause;
   #465 a failed save leaves the chosen file as it was.
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
10. **Load on the Mac was ~25 in session 97 (not re-checked in 98)** (another project's rust
    build and a consensus python job, not localmail). Check `uptime` before
    trusting any timing.

## Exact commands to resume

```bash
cd /Users/hherb/src/localmail
git fetch --prune origin                 # ALWAYS first
git switch main && git pull --ff-only
git log --oneline -1 origin/main         # THE authority
gh pr list --state all --limit 4
gh pr checks fix/464-465-download-errors
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
unset VIRTUAL_ENV && uv run pytest -q              # 4765, session 96 (5:37 under load ~12)
uvx mypy --python-executable .venv/bin/python src/localmail   # Success, 169 files
cd gui && npm test -- --run && npm run check   # 535, session 97
cd gui/src-tauri && cargo test --lib && cargo clippy --all-targets -- -D warnings   # 141, session 98
```

The latest migration is **`0038_messages_attachment_names_strict_idx.sql`**;
the next free slot is `0039_*.sql`.
