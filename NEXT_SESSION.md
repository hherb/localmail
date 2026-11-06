# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-11-06 (session 92).** `main` is **`db31db5`** (#456:
> #327, one plan type per cursor mode).
>
> This session has **one open PR** on `deps/oauthlib-rustls-floors`. It
> carries the code and this handoff.
>
> **Both hosts are still at `753133c`.** This session's deploy of `db31db5`
> was **refused by the permission classifier** ("Remote Shell Writes"), so
> nothing was deployed. #330, #327 and this PR change no wire behaviour on any
> normal path. Deploy is yours (commands below), ideally after this PR merges.
>
> **Open issues: 25.** **Open Dependabot alerts: 7** before this PR merges:
> #71, #185 (open on purpose), #115 (stale), #197 (fixed by this PR), #198,
> #199, #200 (rustls, stale like #115: the lock already holds 0.23.46). After
> the merge, dismiss #115 and #198–#200 as stale if Dependabot does not close
> them itself.
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

- **#327 merged** as `db31db5` (PR #456) before the session started.

### PR on `deps/oauthlib-rustls-floors` (based on `main` at `db31db5`)

| SHA | What |
|---|---|
| `7ac9b69` | `oauthlib>=4.0.1` (Dependabot #197), re-locked with `uv lock --upgrade-package oauthlib` (4.0.0 → 4.0.1, nothing else moved); `rustls` floor 0.23.45 → 0.23.46 in both `gui/src-tauri/Cargo.toml` entries (#198–#200; `Cargo.lock` unchanged, already 0.23.46) |
| next | CLAUDE.md note (second November round) + this handoff |
| next | `.claude/skills/finalcheck/SKILL.md`: the pre-merge review → fix → CI-green loop, adapted from `~/src/secretary` (localmail's workflows, no required checks, the 3.12 leg reproduced only in a worktree, no deploy from the skill) |

- **Reach, checked:** #197 is in oauthlib's *server* device-flow endpoints;
  localmail is only an OAuth client. #198 needs rustls' `brotli` feature,
  #200 post-quantum key exchange (aws-lc-rs); `cargo tree -e features -i
  rustls` shows only `ring`, `std`, `tls12`. #199 needs a Debug-formatted TLS
  session; `gui/src-tauri/src` has none.
- **Verification (in a throwaway worktree + venv, the Mac venv untouched):**
  the 196 OAuth/Gmail tests pass on 4.0.1; the `InstalledAppFlow` consent URL
  (fixed state + verifier) is byte-identical under 4.0.0 and 4.0.1;
  `cargo metadata --locked` accepts the new floor.
- **Not run:** the full Python suite (load ~22 on the Mac); CI runs it on both
  legs, and `gui-ci` covers the Rust build. Check `gh pr checks`.

## What's next

### 1. **Merge this PR** (yours), once CI is green
- **Acceptance:** `gh pr checks <PR>` all pass; Dependabot #197 closes.

### 2. **Deploy `main` to both hosts** (yours this time; the classifier blocked me)
- **Acceptance:** `/v1/version` on both reports the merge SHA. The Mac venv
  needs `uv sync` for oauthlib 4.0.1.

### 3. **Dismiss stale rustls alerts** #115, #198, #199, #200 if they stay open
- **Acceptance:** `gh api …/dependabot/alerts?state=open` lists only #71 and
  #185.

### 4. **Re-authorise `hherb-dmc`** (yours, browser; carried)
- **Acceptance:** no new `crashed for hherb-dmc` in `daemon.err.log` after
  the kickstart; its `max(date_received)` moves past 2026-11-03.

### 5. **Next small slice: pick with the operator**
- Candidates: **#285** (does mypy/ruff gate CI — needs a ruff-pin call),
  **#357** (only worth it with #285), #369 (refuse-vs-keep call), #383
  (one-way door, `0039`), #365 (product call), #340 (harness lock DSN
  arguments), #363 (rerank degradation on the wire — needs a renderer).
- **Acceptance (any):** a RED test first, then full `uv run pytest -q`
  green and mypy clean.
- **Tell kastellan** (yours; carried): problem+json 400s on `/v1/search`,
  `filters.lang` as a list, #419 syntax, #415 dates, `api_minor` 5 and 6
  features, #373 (`'` is literal), #378 (an empty
  `after:`/`before:`/`has:` or a non-int id in `query` is a 400).

### 6. **Carried**
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

1. **No slice picked with the operator**; I took the outstanding Dependabot
   work (#197 was flagged last session as "its own small PR"), adding the
   rustls floor because three new alerts arrived for it.
2. **Raise the rustls floor even though the lock is already fixed** (mine):
   the lock is the state, the floor is the constraint (the icalendar lesson).

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
   exit 2.
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
   `git diff main origin/<branch> --stat` (empty = landed).
6. **Never run two localmail pytest sessions against one test DB** (port
   5532, the same cluster as the live archive).
7. **Restore a mutation from a file copy, never with `git checkout`.**
8. **Always sync with `--extra mcp --extra extraction`.** On the DGX use
   `~/.local/bin/uv`. `uv sync --dry-run` is not read-only.
9. **No ROADMAP.md.** That `/nextsession` step is a no-op.
10. **Load on the Mac was ~22 this session.** Check `uptime` before
    trusting any timing.
11. **Deploy needs your permission.** The auto-mode classifier refuses the
    `ssh 10.0.0.3 '… git pull … systemctl restart'` line as a remote shell
    write. Either run it yourself or add an allow rule.

## Exact commands to resume

```bash
cd /Users/hherb/src/localmail
git fetch --prune origin                 # ALWAYS first
git switch main && git pull --ff-only
git log --oneline -1 origin/main         # THE authority
git diff main origin/deps/oauthlib-rustls-floors --stat   # empty once merged (squash)
gh pr list --state all --limit 3
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
unset VIRTUAL_ENV && uv run pytest -q              # 4723 at session 91
uvx mypy --python-executable .venv/bin/python src/localmail   # Success, 169 files
cd gui && npm test -- --run && npm run check   # 530 at session 81
```

The latest migration is **`0038_messages_attachment_names_strict_idx.sql`**;
the next free slot is `0039_*.sql`.
