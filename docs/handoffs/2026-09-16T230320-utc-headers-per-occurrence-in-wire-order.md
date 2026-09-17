# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-09-17 (session 48).** `main` was **`2ccfb1b`** at the
> start — #376 had been merged between sessions, so the previous handoff
> described its own state accurately and risk 3 was clean. (`git log -- NEXT_SESSION.md`
> still shows the gap at `964ff13`: session 46 shipped #366 and wrote none.)
>
> This session **deployed `main` to both hosts**, diagnosed and cleared a
> Postgres fault that made three unrelated tests fail, and opened **one PR**
> on `fix/kastellan-b-headers-list` — **kastellan slice B**, `?headers=list`,
> closing **#379**.
>
> **Five things worth knowing before you touch anything.**
>
> **(1) The DGX was not down; this Mac's WireGuard was.** `ssh 10.0.0.3`
> timed out and `tunnel-probe.log` read `tunnel=FAIL` for hours — which looks
> exactly like the recurring DGX drop (risk 20). It was not. The Mac rebooted
> at 07:07, `wg0` has no launch daemon, and nothing brought it back: no route
> to `10.0.0.0/24`, no `wg` process, and every probe sample since the reboot
> reads **`utun_rx=NA`** — the probe cannot even find the interface. **That
> `NA` is the tell**: a real DGX-side drop still shows `utun_rx=<number>`.
> The operator brought the tunnel up and the DGX answered immediately.
>
> **(2) Three test failures were a Postgres fault, not the branch.** The
> first full run under this session showed exactly the LISTEN/NOTIFY trio
> failing with `UndefinedFile: could not access status of transaction
> 2217881398` — CLAUDE.md risk 8, and my own baseline 40 minutes earlier had
> been 0 failed. Cleared with the runbook's Option A (`launchctl bootout` the
> daemon → probe `pg_notification_queue_usage()` = 0 and a live `LISTEN` →
> re-run → `bootstrap` back). 11 passed after. **The subagent that hit it had
> run `--tb=no`, which cannot show which tests failed, and called them
> "pre-existing" anyway.** Verify before you believe that word.
>
> **(3) `headers=full` was serving values frozen at sync time, and nobody
> knew.** Measured on the live archive while designing: **11.6%** of messages
> disagree with a fresh parse of their own `raw_bytes`. Every one of the 309
> differing values is whitespace-only — same keys, same occurrence counts,
> identical after `.strip()` — and the cause is #314 (`eec8e09`) changing
> `_headers_dict`. The oldest 300 rows differ in 16; the newest 300 in 0.
> The branch makes both modes re-parse, so this is now closed rather than
> latent.
>
> **(4) My own plan had four defects, all caught by implementers or review.**
> A separator offset that contradicted the plan's own tests; a monkeypatch of
> an immutable `EmailPolicy` that could never pass; a fixture date whose
> weekday does not match the calendar; and `r.message % r.args`, which
> double-formats under caplog. Each is corrected in the plan file, in place.
> **The lesson is that the briefs were specific enough for the defects to
> surface** — a vaguer plan would have had them absorbed silently.
>
> **(5) One implementer committed a failing test and reported DONE.** Named
> in its fix dispatch; it acknowledged the process error. Read
> `.superpowers/sdd/…/progress.md` — no, do not: that workspace is deleted.
> The ledger's rulings are reproduced under "Rulings made this session" below.
>
> **Open issues: 35** (33 + **#379** + **#380**), dropping to **34** when the
> PR merges. **Dependabot: 1** (#71 `accelerate`, no fix exists, left open on
> purpose).

## Project context (1-minute version)

`localmail` mirrors IMAP accounts (Gmail OAuth, password) into Postgres,
**strictly read-only w.r.t. IMAP**. The database is canonical for accounts.
Daemon: hot-reload account set, heartbeats, DB command queue, two-plane
supervision. Web admin UI (HTMX): account CRUD, user management, archive
imports, daemon control, API keys. Hybrid search (Phases 1+2) + an HTTPS GUI
server + a remote MCP server (optionally a full OAuth 2.1 authorization server)
+ the opt-in `--smart` LLM query rewriter are all shipped. A Tauri 2 + Svelte 5
GUI lives under `gui/` — read-only viewer plus an admin mode (Accounts + Daemon
panels shipped; Users + Imports still placeholders). Version **0.3.0**, Python
**3.13** pinned (CI matrixes 3.12 + 3.13). Licensed AGPL-3.0-or-later (per-file
SPDX headers in `src/localmail/`; **not** in `gui/`). **kastellan** consumes
`/v1` over REST with an API key; its slice order is tracked in memory
(`kastellan-request-triage`).

## What we shipped this session

### Deploy — both hosts on `2ccfb1b` (the #367 precedence flip)

- **Mac:** `uv sync --frozen --extra mcp --extra extraction`,
  `pending_migrations` empty, `launchctl kickstart -k` both agents.
  `/v1/version` read `build_hash: 2ccfb1b`, `build_source: git_checkout` —
  the #375 timeout did **not** recur this time.
- **DGX:** `git pull --ff-only` (`964ff13` → `2ccfb1b`),
  `~/.local/bin/uv sync --frozen --extra extraction --extra mcp` (added
  `httpx2 2.13.0` from the dev group), `pending_migrations` empty, both units
  restarted, `NRestarts=0`, `build_hash: 2ccfb1b`.
- **Acceptance met:** a live `/v1/search` for `query="O'Brien"` with a
  `folder_ids` filter returns only rows carrying that folder's label, and two
  different folders return disjoint sets. (Run through `run_search` against
  the live archive, since I hold no bearer token for the Mac.)

### PR — `fix/kastellan-b-headers-list`, 15 commits, `Closes #379`

**Design + plan** (`a431025`, `55ca5a0`, `01bc2ad`, plus `2e228d3` and
`76782cc` correcting the plan in place):
[spec](docs/superpowers/specs/2026-09-16-message-headers-list-design.md),
[plan](docs/superpowers/plans/2026-09-16-message-headers-list.md).

**`381596a` + `41bdc7c` — the pure module.**
[src/localmail/header_block.py](src/localmail/header_block.py): `HeaderEntry`,
`entries_from_message`, `parse_header_block`, `group_entries`,
`entries_to_wire`, `header_block(data, *, truncated)`, `header_mode_error`,
`HEADER_BLOCK_READ_BYTES` (64 KiB). `_SEPARATORS` carries each separator's
terminator length, so there is no slice arithmetic to explain.

**`0cee650` + `f0095cc` — the parser reimplemented on it.**
`parser._headers_dict` **is** `group_entries(entries_from_message(msg))`, so
the stored column and the wire come from one rule. Pinned by an oracle test
holding the pre-change body inline, over every `_eml` fixture.

**`286fd4d` — the accessor.** `get_message` takes `headers: str`
(`compact`/`full`/`list`), validates it **before** the empty-ACL short-circuit
so a refusal is never disguised as a 404, and reads
`substring(raw_bytes from 1 for HEADER_BLOCK_READ_BYTES + 1)` — the `+1` is
how truncation is detected without a second query. A block that does not end
inside the ceiling is re-read in full with a WARNING.

**`6408cc8` — the route.** `?headers=list`; an unknown mode is **400
problem+json**, not FastAPI's 422 array `detail` (#370's complaint).

**`3fa6c52` — MCP.** `get_message(message_id, headers=…)`; `full_headers` is
gone from the published schema, and its description no longer claims that
`false` returns a subset (it returned no `headers` key at all).

**`47bd633` — `api_minor` 0 → 1**, its first ever move, because an old server
cannot refuse the new mode. README, `docs/mcp-usage.md` and CLAUDE.md
document it.

**`bcff1f1` — the acceptance test.** Two `Received` headers plus a
case-variant duplicate come back in wire order, and `full` is unchanged
against a golden response.

**`98ea36a` — the final review's fixes.** CLAUDE.md's MCP tool list no longer
documents the removed `full_headers` (and the superseded reconciliation bullet
is annotated in place); `HEADER_MODES` derives from `get_args(HeaderMode)`;
the pre-IO guard gained the pin the spec named; the truncation WARNING no
longer asserts an oversized block when the real cause is a missing separator.

### Measurements taken on the live archive (129,590 messages)

They are in the spec and CLAUDE.md, and none of them existed in the code before:

| | |
|---|---|
| messages with a repeated header name | **89.1%** (2,448 of 2,746 sampled) |
| messages with one name under two spellings | **5.1%** (140) |
| rows disagreeing with a fresh parse | **11.6%** (104 of 900) — whitespace only |
| header block size | p50 4.7 KB, **p95 8.7 KB**, max 24 KB |
| `raw_bytes` size | p50 44 KB, p95 286 KB, **p99 2.6 MB, max 35 MB** |
| blocks needing more than the 64 KiB ceiling | **0 of 129,590** |
| prefix read cost | **0.33 ms** median (vs 0.09 ms for the JSONB passthrough) |

### Issues filed

- **#379** — the slice B defect (what the PR closes).
- **#380** — a lone surrogate in a header value would 500 the response
  renderer. **Unreachable today** (the DB would have rejected the row), but
  the guarantee moved from the database to `header_block`, so it is recorded
  rather than fixed. Deliberately out of scope.

## Verification (this Mac, all extras, controller-run)

| gate | `main` @ `2ccfb1b` | branch @ `98ea36a` |
|---|---|---|
| pytest | **3682 passed**, 2 warnings, 193 s | **3731 passed, 0 failed**, 2 warnings, 186 s |
| mypy | — | Success, **156** files |
| ruff `src/` | — | **10** (#285 baseline, unchanged) |
| `gui/` touched | — | **no** (so no vitest/cargo gate needed) |
| CI (PR #381) | — | **green** on `30b1dff`: pytest **3730 passed, 1 skipped** on 3.12 *and* 3.13, plus Cloudflare Pages. `gui-ci` correctly did not run. |

- **+49 tests.** The two pre-existing warnings are the #25 websockets pair.
  CI's `1 skipped` against macOS's `0` is the pre-existing platform difference
  CLAUDE.md records — same 3731 collected on both.
- **Mutation-checked, both restored from file copies (never `git checkout`):**
  `list` emitting the `full` shape fails the acceptance test; a truncated
  block served as complete fails two tests.
- **There is still no ROADMAP.md** (thirteenth confirmation) — that
  `/nextsession` step is a no-op. README **was** updated (the endpoint had
  been undocumented entirely).

## What's next

### 0. **Merge the PR, then check what actually closed**
**You merge.** The body carries `Closes #379`. Afterwards:
- `gh issue view 379 --json state` should be `CLOSED`, open issues **35 → 34**.
- CI: `python-ci` runs on the PR (3.12 + 3.13). No `gui/` file changed, so
  `gui-ci` should not run — if it does, read why rather than assuming.

### 1. **Deploy the merge to both hosts** *(`api_minor` is the acceptance)*
- **Mac:** `git checkout main && git pull`, `uv sync --frozen --extra mcp
  --extra extraction`, kickstart both agents.
- **DGX:** `git pull --ff-only && ~/.local/bin/uv sync --frozen --extra
  extraction --extra mcp`, restart both units.
- **Acceptance:** `/v1/version` reports **`api_minor: 1`** on both hosts —
  that is the one field that proves the new code is live, and it is
  independent of #375's flaky `build_hash`. Then
  `GET /v1/messages/<id>?headers=list` on a real message returns its
  `Received` chain in order.
- **Tell kastellan.** Its `email-in` worker reads `?headers=full` for DMARC
  ordering; `list` is what it actually wants, gated on `api_minor >= 1`.

### 2. **Kastellan slice D — attachments by message index** *(next in the approved order 0→A→B→D→E→C)*
- `/v1/messages/{id}/attachments/{index}[/text]`, plus offset/limit paging on
  the text. Filename matching stays client-side.
- **Acceptance:** a message with two attachments of the same filename is
  addressable by index; the text endpoint pages; the existing
  `/v1/attachments/{sha256}` route is untouched.
- Flow: brainstorm → spec → plan, as for slices A and B.

### 3. **The issues this and the last two review rounds filed**
- **#380** (new): the surrogate path. `pgtext` is where a sanitiser belongs.
- **#377**: a NUL in `query` or a text filter is a psycopg 500.
- **#378**: operators with an empty/unparsable value silently become free text.
- **#369**: scalar conflicts resolve last-token-wins; #367 flipped which side
  wins, and the comment there has the before/after table.
- **#373**: the tokenizer mangles `O'Brien` → `OBrien`, so it never matches
  lexically. **Note this bit me while writing a test this session** — seed
  `OBrien`, not `O'Brien`, until it is fixed.
- **#372** ghost hits · **#370** 422 vs problem+json · **#371** the Tauri hop
  drops `lang`/dates · **#368** MCP ignores unknown arguments · **#365**
  embedded images count as attachments.

### 4. **#375 — the Mac build-hash probe** *(carried, and it did NOT recur)*
`/v1/version` read `git_checkout` on both restarts this session. The defect is
real but intermittent. **Acceptance:** a `TimeoutExpired` is not cached, and a
later `/v1/version` re-probes (rate-limited). Measure why launchd children are
slow before touching `_GIT_TIMEOUT_S`.

### 5. **#374 + #305 — the `cli.py` refactor** *(carried, grown)*
**Acceptance for #305:** blocking `sqlparse` on `sys.meta_path` leaves
`localmail --version` exiting 0. With it: **#374** (a pure CLI query composer
that sanitizes flag values and emits them first), **#331 point 2** (widen the
search catch to `SearchArgumentRefused`), and printing
`sort_applied`/`rankable`. `cli.py` is **2177 lines**.

### 6. **Carried backlogs**
- **#358 / #357** (the shape should carry the contract).
- **#330 / #327 / #328** (search typing).
- **#319 / #318 / #320** (API keys; next free migration slot `0037_*.sql`).
- **#340** (harness lock vs database) · **#363** (degraded rerank invisible)
  · **#285** (ruff + CI: the operator's call).
- Admin GUI phase 5, the Users panel (no backend work; stub the new API
  module in **both** `AdminView.test.ts` and `MainView.test.ts`).
- Robustness: **#218 · #226 · #225/#227 · #200/#211/#208 · #206 · #204 · #25**.

## Rulings made this session

The SDD workspace is deleted; these are the decisions it recorded, so you can
undo any you disagree with.

1. **Work on the branch in the main tree, not a git worktree** — the repo's
   convention is branch + PR, this Mac's `.venv` IS the production install,
   and implementers ran strictly sequentially. *Cost if wrong:* a subagent's
   edit reaches the tree the launchd agents restart from.
2. **Task 2's test was written green-first** under an inline oracle holding
   the pre-change body. A behaviour-preserving refactor has no failing state
   to produce. *Cost if wrong:* a tautological test — mitigated because the
   oracle is a copy of the old body, not a call to the new one.
3. **The oracle does not strip NULs** while the new code does, one layer down.
   No `_eml` fixture carries one, and `parse_message`'s `strip_nuls_all` is
   idempotent. *Cost if wrong:* a future NUL fixture fails spuriously.
4. **`header_block` keeps the final header's line terminator** — the
   implementer's deviation was right and my plan was wrong; the plan's own
   tests required it.
5. **The #314 guard is pinned on a policy subclass**, not a monkeypatch:
   `EmailPolicy` is immutable on 3.13. It drives `entries_from_message`
   because the read path never calls `parse_message`.
6. **`raw_bytes` exactly `ceiling+1` is flagged truncated** though fully
   captured — one identical re-read, never a wrong answer. Deferred.
7. **#380 is filed, not fixed.** The path is unreachable today; expanding a
   feature onto an unreachable path is how it acquires its second defect.

## Open decisions & risks

1. **One PR is open and yours to merge** (`fix/kastellan-b-headers-list`,
   based on `main` `2ccfb1b`). **Open issues 35 → 34. Dependabot stays 1.**
2. **`api_minor` moved for the first time.** Anything that pinned it to `0`
   breaks — in-tree nothing does (`test_serve_app_baseline.py` asserts
   `>= 0`), but an external consumer might.
3. **`headers=full` values change for ~12% of live rows** (leading whitespace
   an older parser kept). Whitespace-only, same keys, same counts. The GUI's
   unfold panel and kastellan's DMARC check both parse the value, so neither
   should notice — but it is a wire change nobody asked for by name.
4. **A merge does NOT close issues its subject merely names.** Use `Closes #N`
   in the body and verify with `gh issue list` after merging.
5. **THIS FILE IS NOT THE AUTHORITY — `git` and `gh` are.** Open with
   `git fetch --prune`, `git log --oneline -1 origin/main`,
   `git log --oneline -1 -- NEXT_SESSION.md`, `gh pr list`, `gh issue list`.
6. **`utun_rx=NA` in the tunnel probe means the MAC's tunnel is down**, not
   the DGX's (risk 20 is about the DGX). `wg0` has no launch daemon here, so
   it does not survive a reboot; `sudo wg-quick up wg0` is the fix and needs
   the operator.
7. **The stale NOTIFY queue recurs** (risk 8, hit again this session). Exactly
   three LISTEN/NOTIFY failures with `could not access status of transaction`
   means cluster state, not your change. Runbook Option A; gate the re-run on
   the probes, not on a fixed wait.
8. **Never trust "pre-existing" from a subagent that ran `--tb=no`.** It
   cannot know. Require failures by name, always.
9. **Never run two pytest sessions against one test database** (enforced by
   #336/#337, and the reason every subagent this session was told to run
   pytest in the foreground).
10. **Restore a mutation from a file copy, never `git checkout`** — it would
    discard uncommitted work in the same file.
11. **The test DB is on 5532**, same cluster as the live archive.
12. **Dependency floors: read the range against `uv.lock`**, not the declared
    floor. `uv lock --dry-run` is read-only; `uv sync --dry-run` is **not**.
    Never a bare `uv sync`: `--extra mcp --extra extraction` on both hosts,
    and `~/.local/bin/uv` on the DGX.
13. **A MagicMock attribute reaches the wire as `{}`/`[]`** (carried). No wire
    key was added by a mocked path this session — the new `headers` shapes are
    asserted through the real transport.
14. **CI trap** (carried): a GUI admin panel that fetches on mount must be
    stubbed in both `AdminView.test.ts` and `MainView.test.ts`.
15. **The machine ran at load 15–50 all session** (Codex, Chrome, CoreGraphics
    PDF services). Do not compare this session's timings to a quiet machine.
16. **No ROADMAP.md** (carried) — that `/nextsession` step is a no-op.

## Exact commands to resume

```bash
cd /Users/hherb/src/localmail
git fetch --prune origin                 # ALWAYS first
git status && git branch --show-current
git log --oneline -1 origin/main         # THE authority, not this file's header

# RISK 5 — did the LAST session hand off?
git log --oneline -1 -- NEXT_SESSION.md
ls -t docs/handoffs/ | head -3

# RISK 4 — after the merge, CHECK WHAT CLOSED.
gh pr list
gh issue list --limit 60                 # 35 open; 34 after the merge
gh issue view 379 --json state --jq .state
gh api repos/hherb/localmail/dependabot/alerts \
  --jq '.[] | select(.state=="open") | "\(.number)\t\(.dependency.package.name)"'   # expect only 71

# RISK 6 — is the tunnel up, or is it this Mac again?
tail -3 ~/localmail-probe/tunnel-probe.log      # utun_rx=NA => THIS Mac's wg0 is down
ping -c 3 -t 5 10.0.0.3                          # if NA: ask the operator for `sudo wg-quick up wg0`

# RISK 15 — load and orphans before trusting any timing.
uptime; ps -eo pid,ppid,etime,time,command | grep "[w]hile :; do :; done"

# AFTER MERGING — Mac (the tree IS the install):
#   git checkout main && git pull
#   unset VIRTUAL_ENV && uv sync --frozen --extra mcp --extra extraction
#   launchctl kickstart -k gui/$(id -u)/com.localmail.daemon
#   launchctl kickstart -k gui/$(id -u)/com.localmail.serve
#   curl -sk https://127.0.0.1:8443/v1/version    # api_minor MUST read 1
# AFTER MERGING — DGX:
#   ssh 10.0.0.3 'cd ~/src/localmail && git pull --ff-only && ~/.local/bin/uv sync --frozen --extra extraction --extra mcp'
#   ssh 10.0.0.3 'systemctl --user restart localmail-daemon localmail-serve'
#   ssh 10.0.0.3 'curl -sk https://10.0.0.3:8443/v1/version'   # api_minor == 1

# Python suite.
unset VIRTUAL_ENV && uv run pytest -q
#   macOS: expect 3731 passed, 0 skipped, 2 warnings (the #25 websockets pair).
#   Exactly 3 LISTEN/NOTIFY failures = RISK 7, not your change:
psql -h localhost -p 5532 -U localmail -d postgres -tAc 'SELECT pg_notification_queue_usage()'
psql -h localhost -p 5532 -U localmail -d localmail_test -c 'LISTEN daemon_commands'
#   the fix (runbook Option A), gated on the probes above reading clean:
#     launchctl bootout gui/$(id -u)/com.localmail.daemon
#     launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.localmail.daemon.plist
unset VIRTUAL_ENV && uv run mypy src/localmail                   # Success, 156 files
unset VIRTUAL_ENV && uv run ruff check src/localmail/ | tail -1  # 10 (#285)

# This slice directly:
T="tests/test_header_block.py tests/test_parser_headers_one_rule.py tests/test_headers_list_acceptance.py tests/test_api_messages.py"
unset VIRTUAL_ENV && uv run pytest -q ${=T}   # ${=T}, NOT $T — zsh does not word-split

# Host health (Mac):
launchctl list | grep -i localmail
psql -h localhost -p 5532 -U localmail -d localmail -c \
  "SELECT worker_kind, account_id, state, now()-last_heartbeat_at AS age FROM daemon_heartbeats ORDER BY 1,2"
unset VIRTUAL_ENV && uv run localmail search-status    # under a second (#280)
```

`main` tip at session start was **`2ccfb1b`**. This session left **one PR**
open on `fix/kastellan-b-headers-list` — 15 commits, `a431025..98ea36a`.
Latest migration **`0036_api_keys.sql`**; next free slot `0037_*.sql` (none
added — this slice needed no migration). **Open issues: 35 → 34 on merge.
Dependabot: 1.**
