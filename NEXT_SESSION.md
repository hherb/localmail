# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-11-07 (session 95).** `main` is **`b6b9ab7`** (#461,
> closes #319; #460 closed #372 just before it).
>
> **One open PR: #463** on `fix/218-bounded-gui-download`, based on `main` at
> `b6b9ab7`, carrying the #218 code **and this handoff**.
>
> **Both hosts are deployed at `b6b9ab7`** (this session; `/v1/version`
> verified on each). #218 is GUI-only (Tauri), so merging it needs **no server
> deploy**; the desktop GUI needs a rebuild to pick it up.
>
> **Open issues: 26** (25 once #463 closes #218; #464 and #465 filed in review). **Open Dependabot alerts: 2**
> — #71 and #185, both open on purpose.
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

- **Confirmed #460 and #461 merged** (`9ed57c6`, `b6b9ab7`); #372 and #319
  closed.
- **Deployed both hosts to `b6b9ab7`** (Mac: `uv sync` + both launchd
  kickstarts; DGX: pull + sync + `systemctl --user restart`). Both report
  `build_hash: b6b9ab7`.
- **Operator picked #218** as the next slice.

### PR #463 on `fix/218-bounded-gui-download`

| SHA | What |
|---|---|
| `b56a2e6` | New `gui/src-tauri/src/http/body.rs::read_capped(resp, max)`: refuses an over-cap `Content-Length` up front, then reads `resp.chunk()` and returns `BodyError::TooLarge` once the running total would pass `max`. `download_attachment`, `fetch_attachment_bytes` and `get_message_raw` use it (via `From<BodyError>` per command error enum), replacing three copies of buffer-then-check; `check_content_length` deleted. 5 helper tests + 1 mapping test per module. CLAUDE.md bullet in the GUI section |
| `27e8861` | this handoff |
| next | `/finalcheck` review fixes: a server-driven wiring test per caller (reverting any caller to `resp.bytes()` passed every test before), three helper tests closing surviving mutations (fast-fail removed, `>=` there, the `Read` branch), `Vec` pre-sized from a declared length, doc corrections. Six mutations each fail their own test. #464 and #465 filed |

- **Verification:** RED first. The helper landed holding today's logic, and
  the endless-chunked-body test timed out (`Elapsed`) while the other four
  passed. The loop made it green. After the review fixes `cargo test --lib`:
  **123 passed**; `cargo clippy` with and without `--all-targets` and
  `cargo fmt --check` are clean. No Python or TS changed, so neither suite
  was re-run.
- **The commit also carries rustfmt reflows** in ~15 untouched Rust files
  (behaviour-neutral, checked line by line in review). gui-ci has no
  `cargo fmt --check`, so nothing gated them.
- **Wire nuance:** without a `Content-Length`, `TooLarge.size` is the bytes
  received when the read stopped. That is a lower bound, not the real size.
- **Not done (scoped out):** the JSON helpers in `http/client.rs` still read
  unbounded bodies, both `.json()` and the error-path `.text()` in all six.
  Noted in CLAUDE.md, not filed.
- **Filed from review:** #464 (a body timeout reads as a generic "read body"
  error), #465 (a failed disk write leaves a truncated file).

## What's next

### 1. **Merge #463** (yours), once CI is green
- **Acceptance:** `gh pr checks 463` all pass (gui-ci runs the Rust tests);
  #218 closes; `gh issue list` shows 25. No server deploy needed.

### 2. **Re-authorise `hherb-dmc`** (yours, browser; carried)
- **Acceptance:** no new `crashed for hherb-dmc` in `daemon.err.log` after
  the kickstart; its `max(date_received)` moves past 2026-11-03.

### 3. **Next small slice: pick with the operator**
- Candidates: **#462** (tiny: the CLI names the typed username, plus one
  WARNING on the panel's refusal branch), **#208** (DaemonPanel message
  never dismisses), **#368** (MCP drops unknown args; needs a design call),
  **#285** (ruff/mypy in CI; needs a ruff-pin call), **#369** (DSL
  contradictions; needs design calls), #383 (`0039`), #365, #363.
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
- **#462:** API-key leftovers (see slice candidates).
- **`ApiKeyNotFound` still derives from bare `Exception`** (latent; every
  route catches it explicitly).
- **Dependabot #71 / #185:** left open on purpose.
- **docling > 2.144:** probe the next release when it lands.
- **emSign G3:** re-check the trust stores after OS updates.
- **kastellan slice C:** ask whether it is still wanted.
- **Mac `failed_messages`:** one `iso2022_jp` decode failure from
  2026-07-25 (`hherb-dmc/[Gmail]/Important` uid 5263). Not investigated.
- **Issues:** #464, #465 (GUI downloads); #383, #369, #368, #365; #375; #357; #363, #285;
  robustness #226, #225/#227, #200/#211/#208, #206, #204, #25.

## Rulings made this session

1. **Operator picked #218** over #462, #208 and #369.
2. **One shared helper in `http/`, not a fix per command** (mine): the three
   copies are why the bug existed three times.
3. **Still buffered in memory, just bounded** (mine): `download_attachment`
   could stream to disk, but the 100 MiB ceiling is the documented contract
   and a bounded `Vec` keeps one code path for all three commands.

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
   in the 404 detail of all three JSON key routes (revoke, delete, grants)
   and in `revoke-api-key` / `remove-api-key`'s CLI error. **New:** #218 a GUI
   download over 100 MiB without `Content-Length` now fails with a
   lower-bound `size` instead of buffering first.
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
10. **Load on the Mac was 22–37 this session** (another project's rust
    build and a consensus python job, not localmail). Check `uptime` before
    trusting any timing.

## Exact commands to resume

```bash
cd /Users/hherb/src/localmail
git fetch --prune origin                 # ALWAYS first
git switch main && git pull --ff-only
git log --oneline -1 origin/main         # THE authority
gh pr list --state all --limit 4
gh pr checks 463
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
unset VIRTUAL_ENV && uv run pytest -q              # 4761 at session 94 (not re-run in 95)
uvx mypy --python-executable .venv/bin/python src/localmail   # Success, 169 files
cd gui && npm test -- --run && npm run check   # 530 at session 81
cd gui/src-tauri && cargo test --lib && cargo clippy --all-targets -- -D warnings   # 123, session 95
```

The latest migration is **`0038_messages_attachment_names_strict_idx.sql`**;
the next free slot is `0039_*.sql`.
