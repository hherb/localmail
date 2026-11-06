# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-11-06 (session 93).** `main` is **`2234775`** (#457:
> oauthlib 4.0.1, rustls floor).
>
> This session has **one open PR** on `fix/340-harness-lock-dsn`. It carries
> the code and this handoff.
>
> **Both hosts are deployed at `2234775`** (verified via `/v1/version`; the
> DGX now has oauthlib 4.0.1, the ssh deploy was not blocked this time).
> This PR is test-only, so it needs no deploy.
>
> **Open issues: 26** (25 once this PR closes #340; #459, the residual, was filed by the review). **Open Dependabot
> alerts: 2** — #71 and #185, both open on purpose. #115 and #197–#200 closed.
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

- **Deployed `2234775` to both hosts** (Mac: launchd kickstart; DGX: the
  ssh line from the resume commands). Both report `build_hash: 2234775`.

### PR on `fix/340-harness-lock-dsn` (based on `main` at `2234775`)

| SHA | What |
|---|---|
| `e84b705` | `tests/acceptance/_harness_lock.py::_mismatched_dsn_calls`: inside a `with harness_db_lock(X)` body, every `DB_ENTRY_CALLS` member's DSN spelling (first positional, else `conninfo=`/`dsn=`) must equal `X`'s; an unreadable one (`**params`) is reported as `<unreadable>`. #340 option 1. New `tests/test_acceptance_harness_lock_dsn.py` (8 tests); CLAUDE.md #337 entry updated |
| next | this handoff |
| review | `/finalcheck` fixes: two fail-open cases closed — two `<unreadable>` DSNs no longer match each other, and a call carrying any other keyword or `**` (`dbname=`, `host=`, `connect_kwargs=`) is `<unreadable>`; 10 more tests (`test_acceptance_harness_lock_dsn.py` now 18); residual (helper mismatch, rebinding) filed as **#459** |

- **Verification:** RED first (5 fail, 3 controls pass); three mutations
  (drop the keyword read, drop the report, drop the comparison) each fail
  their test; all five real harnesses still pass the rule (no harness
  changed). Full suite **4752 passed, 6 warnings** after the review fixes (4742 before) (warnings not
  inspected; earlier sessions recorded 2); mypy clean, 169 files.
- **Known limit (in a `shortcut:` comment, tracked as #459):** a mismatch
  inside a helper reached under the lock, and a name rebound inside the body
  (`dsn = live`), are still missed; a rebound copy (`d = args.dsn`) is
  reported (fails closed).

## What's next

### 1. **Merge this PR** (yours), once CI is green
- **Acceptance:** `gh pr checks <PR>` all pass; #340 closes (`Closes #340`
  in the body).

### 2. **Check the 6 test warnings**
- **Acceptance:** `uv run pytest -q -rw` lists them; either they match a
  baseline on `main` or the new ones are filed/fixed.

### 3. **Re-authorise `hherb-dmc`** (yours, browser; carried)
- **Acceptance:** no new `crashed for hherb-dmc` in `daemon.err.log` after
  the kickstart; its `max(date_received)` moves past 2026-11-03.

### 4. **Next small slice: pick with the operator**
- Candidates: **#285** (does mypy/ruff gate CI — needs a ruff-pin call),
  **#357** (only worth it with #285), #369 (refuse-vs-keep call), #383
  (one-way door, `0039`), #365 (product call), #363 (rerank degradation on
  the wire — needs a renderer).
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
- **Issues:** #383, #369, #372, #368, #365; #375; #357; #363, #285;
  robustness #218, #226, #225/#227, #200/#211/#208, #206, #204, #25.

## Rulings made this session

1. **Operator picked #340** and asked for both deploys.
2. **#340 option 1, not 2 or 3** (mine): the cheap in-frame comparison
   covers the realistic mistake (two DSN expressions side by side in
   `main`) and false-positives on no current harness; option 3 (runtime)
   would need every harness connection routed through the lock.

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
10. **Load on the Mac was 15–40 this session.** Check `uptime` before
    trusting any timing.
11. **Deploy was allowed this session** (the DGX ssh line ran). If the
    classifier refuses it again, run it yourself.

## Exact commands to resume

```bash
cd /Users/hherb/src/localmail
git fetch --prune origin                 # ALWAYS first
git switch main && git pull --ff-only
git log --oneline -1 origin/main         # THE authority
git diff main origin/fix/340-harness-lock-dsn --stat   # empty once merged (squash)
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
unset VIRTUAL_ENV && uv run pytest -q              # 4752 at session 93 (after review)
uvx mypy --python-executable .venv/bin/python src/localmail   # Success, 169 files
cd gui && npm test -- --run && npm run check   # 530 at session 81
```

The latest migration is **`0038_messages_attachment_names_strict_idx.sql`**;
the next free slot is `0039_*.sql`.
