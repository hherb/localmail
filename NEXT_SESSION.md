# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-11-07 (session 97).** `main` is **`eef2972`** (#466,
> closes #462).
>
> **One open PR** on `fix/208-daemon-message-dismiss`, based on `main` at
> `eef2972`, carrying the #208 code **and this handoff**.
>
> **Both hosts are deployed at `eef2972`** (`/v1/version` confirmed on each
> this session). The #208 PR is GUI-only: no server deploy after it merges.
>
> **Open issues: 24** (23 once the PR closes #208). **Open Dependabot alerts:
> 2**: #71 and #185, both open on purpose.
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

- **Confirmed #466 merged** (`eef2972`), which closed #462; 24 issues open.
- **Deployed `eef2972` to both hosts** (operator approved); `/v1/version`
  reports `build_hash: eef2972` on the Mac and the DGX.
- **Operator picked #208.**

### PR on `fix/208-daemon-message-dismiss`

| SHA | What |
|---|---|
| `b6001f8` | `DaemonPanel.svelte`: "Request accepted." clears after `ACCEPTED_DISMISS_MS` (5 s). A rejection stays until the next action (#148). `dismissTimer` is cleared by `clearDismiss()` at the start of each action and in `onDestroy`, and is not scheduled once `destroyed` is set. 6 fake-timer tests (the sixth from review); README (GUI daemon tab) and CLAUDE.md (Daemon panel bullet) updated |
| next | this handoff |

- **Verification:** RED first (2 of the new tests failed; 3 passed at RED:
  the stale-timer and post-unmount tests vacuously, since no timer existed,
  and the rejection test because it pins existing behaviour). Then the panel
  file has 17 passing tests; the **GUI suite has 535 passing**; `npm run
  check` reports 0 errors. Three mutations, each failing only its own test:
  drop the clear in `runAction`, drop the clear in `onDestroy`, drop the
  `destroyed` guard.
- **`/finalcheck` review round:** moving the clear from the top of
  `runAction` into the success branch passed all 17, yet lets a pending
  success timer wipe a later rejection (#148). A sixth test pins it
  (success, then a 409 within 5 s, still shown 5 s later); that mutation now
  fails only it. The "until the next action" test, which never ran one, is
  renamed "past the dismiss window". Panel file 18 passing.
- **No Python or Rust touched**, so pytest, mypy and cargo were not re-run.

## What's next

### 1. **Merge the #208 PR** (yours), once CI is green
- **Acceptance:** `gh pr checks` all pass; #208 closes; `gh issue list` shows
  23. No deploy needed (GUI-only).

### 2. **Re-authorise `hherb-dmc`** (yours, browser; carried)
- **Acceptance:** no new `crashed for hherb-dmc` in `daemon.err.log` after
  the kickstart; its `max(date_received)` moves past 2026-11-03.

### 3. **Next small slice: pick with the operator**
- Candidates: **#464 + #465** (GUI
  download follow-ups: distinct body-timeout error; no truncated file on a
  failed disk write), **#368** (MCP drops unknown args; design call),
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
- **Issues:** #464, #465 (GUI downloads); #383, #369, #368, #365; #375; #357; #363, #285;
  robustness #226, #225/#227, #200/#211, #206, #204, #25.

## Rulings made this session

1. **Operator approved deploying `eef2972`** to both hosts, and picked #208
   over #464/#465, #285 and #383.
2. **Only the success message auto-dismisses** (mine). The issue said "the
   action-message"; dismissing a rejection would hide a refused control,
   which is #148's defect. Errors stay until the next action.
3. **5 s, not "on the next successful poll"** (mine). The poll fires every
   2 s, so the message would flash for up to 2 s and could be missed.

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
   exit 2; #372 a page can hold fewer hits than `pool_size`
   suggested only if a message was deleted mid-search; #319 a person's id now reads "… is a person, not an API-key principal"
   in the 404 detail of all three JSON key routes (revoke, delete, grants);
   #218 a GUI download over 100 MiB without `Content-Length` fails with a
   lower-bound `size` instead of buffering first. #462
   `revoke-api-key`/`remove-api-key` on a person's username print
   `'<name>' is a person's account, not an API key`, and the panel's refusal
   branch logs one WARNING on `localmail.serve.admin`. **New:** #208 the GUI
   daemon tab's "Request accepted." clears after 5 s.
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
10. **Load on the Mac was ~25 this session (12–15 in 96)** (another project's rust
    build and a consensus python job, not localmail). Check `uptime` before
    trusting any timing.

## Exact commands to resume

```bash
cd /Users/hherb/src/localmail
git fetch --prune origin                 # ALWAYS first
git switch main && git pull --ff-only
git log --oneline -1 origin/main         # THE authority
gh pr list --state all --limit 4
gh pr checks fix/208-daemon-message-dismiss
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
cd gui/src-tauri && cargo test --lib && cargo clippy --all-targets -- -D warnings   # 123, session 95
```

The latest migration is **`0038_messages_attachment_names_strict_idx.sql`**;
the next free slot is `0039_*.sql`.
