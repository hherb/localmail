# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-09-15 (session 47).** `main` was **`964ff13`** at the
> start. This session **deployed `main` to both hosts** and opened **one PR**
> **#376** on `fix/367-filters-before-free-text`, which closes **#367** and
> six Dependabot alerts (#72–#77).
>
> **The handoff was one session stale again (risk 3).** `main` carried
> `964ff13` (#366, #364) while `NEXT_SESSION.md` described session 45
> (#361). **Session 46 shipped #366 and wrote no handoff**, so
> `docs/handoffs/` has no entry for it. Other drift against the old file:
> Dependabot read **7** where it predicted 0, and open issues read **29**
> where it predicted 21. Seven of those issues (#365, #367–#372) had been
> filed by #366's review.
>
> **Four findings, all from running things.**
>
> **(1) #367's fix flips a precedence the issue never mentioned.**
> `parse_query` keeps the *last* value of a scalar operator. With the filters
> composed first, `query="from:bob"` + `filters.from="alice"` now searches
> for **bob**; it used to search for alice. The operator chose "flip +
> document": both orders are the silent last-token-wins that #369 exists to
> end. The flip is pinned by a test and documented in README, and #369 has a
> comment with the before/after table.
>
> **(2) The fix retired more than the issue listed, and three things it
> does NOT close are filed.**
> - **Retired:** `run_search` now parses **one** string, where #366 left
>   three parses and a refusal naming the open quote, and
>   `query.unclosed_quote` is gone. The gate-vs-Searcher divergence is
>   closed for the `from:"` / `"` class, and so is CLAUDE.md's
>   "known residual" (rank+asc on that class) without the restructuring it
>   predicted.
> - **#373:** the tokenizer itself mangles `O'Brien` → `OBrien`. Measured
>   against the live cluster: the tsvector holds `'o' 'brien'`, so an
>   apostrophe name **never matches lexically**, filters or not.
> - **#374:** `localmail search` (CLI) has its own composer with the pre-#367
>   order *and* unsanitized flag values, so moving its tokens first alone
>   would be wrong.
> - **#375:** see (3).
>
> **(3) The Mac's `/v1/version` said `build_source: git_failed` after the
> deploy, and it is not a bad deploy.** `serve` under launchd hit the 2 s git
> timeout, on `rev-parse` the first time and `diff --quiet HEAD` after a
> second restart. The same commands take 10–70 ms from a shell, even with
> `env -i PATH=/usr/bin:/bin`, and the timeout is **cached for the life of the
> process**. Load ran 8–50 all session. Filed as **#375**. Verify the Mac's
> revision by process start time against HEAD.
>
> **(4) The first full run had the three LISTEN/NOTIFY failures (risk 8)**
> with the `could not access status of transaction` signature. Both gates read
> clean minutes later and the three tests passed in isolation, so the second
> full run is the result quoted below.
>
> **Open issues: 32** (29 + #373/#374/#375), dropping to **31** when the PR
> merges. **Dependabot: 7 → 1** on merge (#71 `accelerate` stays open on
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

### Deploy — both hosts on `964ff13` (kastellan slice 0)

- **Mac:** `uv sync --frozen --extra mcp --extra extraction`, `pending_migrations` empty,
  `launchctl kickstart -k` both agents. Both processes started after the tree
  was at `964ff13`. `/v1/version` reads `git_failed` (#375, finding 3).
- **DGX:** `git pull --ff-only` (`815e74b` → `964ff13`, 12 commits),
  `~/.local/bin/uv sync --frozen --extra extraction --extra mcp`. That moved
  icalendar 7.3.0, pypdf 6.16.2, transformers 5.16.1, tokenizers 0.23.1 and
  safetensors 0.8.0 onto the DGX's security floors. `pending_migrations` was
  empty; restarted both units. `build_hash` reads `964ff13`, `NRestarts=0`,
  and all three services are active. The first SSH attempt timed out during
  the banner exchange; the second, minutes later, worked.
- **Not redeployed after the PR.** The PR changes wire behaviour (the
  precedence flip), so deploy it after merging; see What's next, item 1.

### PR #376 — `fix/367-filters-before-free-text` (3 commits + this handoff)

**`9882cb0` — compose filter tokens ahead of the free text (#367).**
- `build_query_string` now emits `_filter_tokens(...)` and then the free
  text. Every filter token is self-contained, so
  `parse(composed).free_text == parse(free_text).free_text` holds for every
  input, exactly, with no `.strip()`.
- `run_search` parses one composed string (`_gate_query`). The rowed
  branches' re-parse, the open-quote message, `_parses` and
  `query.unclosed_quote` are gone.
- Both `except SearchArgumentRefused` catches are backstops again, and their
  comments say so.
- `test_api_search_unclosed_quote.py` became
  `test_api_search_filters_before_free_text.py` (53 tests). The neutrality
  test gained 4 open-quote free texts (+44 cases). The two
  `rank_without_text` tests that pinned the divergence as live now pin the
  agreement, with a positive control.
- Prose in `keyset_walk`, `search_cursor` and `argument_errors` is corrected.

**`e3a1819` — dev-only security floors (Dependabot #72–#77).**
- `httpx2>=2.12.0` (2.4.0 → 2.13.0). It pins `httpcore2` exactly, which
  carries `httpcore2` to 2.13.0. `httpx2-jsfetch` enters the lock
  emscripten-only.
- `vitest ^4.1.11` (4.1.9 → 4.1.11, with `@vitest/*` and `tinyrainbow`).
- `nanoid` 3.3.16 → 3.3.18 via `npm audit fix`: flagged by `npm audit`, not
  yet by Dependabot.
- `accelerate` (#71) is left open: no fix exists; it arrives via
  `docling-ibm-models` and `docling-slim`, and the attack needs a malicious
  checkpoint index.

**`ab73610` — README + CLAUDE.md.**
- README replaces the "#367 known gap" sentence and states the precedence
  flip.
- CLAUDE.md annotates the #324, #326, #331, #345 and #353 divergence notes as
  closed, in place. The #364 "three strings" block becomes the #367 entry.
  Also added: the dependency floors, and a #375 note under build provenance.
- `test_serve_search_request_keys.py`: one docstring referencing
  `_gate_free_text`.

### Verification (this Mac, all extras)

| gate | `main` @ `964ff13` | branch @ `ab73610` |
|---|---|---|
| pytest collected | **3543** | **3622** |
| pytest run | — | **3622 passed, 0 failed, 2 warnings, 276 s** (load ~50) |
| mypy | — | Success, **155** files |
| ruff `src/` | — | **10** (#285 baseline) |
| svelte-check / vitest / build | — | 0 errors / **483 passed** / ok |
| CI (#376) | — | green; Linux pytest **3621 passed, 1 skipped** on 3.12 and 3.13 |

- **+79 collected is counted per file**, not derived: +53 new file, −20
  retired file, +44 neutrality cases, +2 `rank_without_text`.
- **3 mutations, 3 caught:**
  - free-text-first (82 failures, including the served-open-quote and
    agreement tests);
  - gate parses the raw field (6: the `has:` contradiction pins);
  - gate stops mapping `QueryParseError` (18).
  The unmutated baseline went through the same harness first (266 passed).
- `gui/` gates ran because the npm lock changed. Cargo was **not** run:
  no Rust changed.
- **There is still no ROADMAP.md** (twelfth confirmation).

## What's next

### 0. **Merge the PR, then check what actually closed**
**You merge** #376. The PR body carries `Closes #367`. **CI was green on
`ab73610`**: pytest 3.12 and 3.13 each 3621 passed / 1 skipped (the
pre-existing Linux skip), cargo macOS + Ubuntu, svelte-check + vitest.
Cloudflare Pages was still pending at handoff (it passed on #366). The
handoff commit touches no CI-filtered path. Afterwards:
- `gh issue view 367 --json state` should be `CLOSED`, and **open issues
  32 → 31**.
- Dependabot should read **1** (#71) once GitHub rescans the lock files. If
  #72–#77 stay open, they need the manifests on `main`, not the branch.

### 1. **Deploy the merge to both hosts** *(the precedence flip is wire behaviour)*
- **Mac:** `git checkout main && git pull`, then
  `uv sync --frozen --extra mcp --extra extraction`, then kickstart both
  agents.
- **DGX:** `git pull --ff-only && ~/.local/bin/uv sync --frozen --extra extraction --extra mcp`,
  then restart both units.
- **Acceptance:** DGX `build_hash` equals the merge SHA. On the Mac,
  `serve`'s start time is after the pull (#375). Then a live `/v1/search`
  with `query="O'Brien"` + a `folder_ids` filter returns only rows from that
  folder.
- `uv.lock` changed (dev-group only), so the sync is needed but moves
  nothing at runtime.

### 2. **Kastellan slice B — `?headers=list`** *(next in the approved order)*
- One `{name, value}` entry per occurrence, in wire order, re-parsed from
  the `raw_bytes` header block.
- The `headers` param becomes a `Literal`, and `api_minor` is bumped.
- `?headers=full`'s name-keyed object must stay byte-identical (kastellan's
  DMARC check reads it).
- Flow: brainstorm → spec → plan, as for #364
  (`docs/superpowers/specs/2026-09-14-search-request-honesty-design.md`).
- **Acceptance:** a message with two `Received` headers and a case-variant
  duplicate returns every occurrence in order; `full` is unchanged against a
  golden response.

### 3. **The #366 review-round issues**
- **#369**: refuse scalar conflicts. Its first two rows now read the other
  way; see the comment there. When it lands,
  `test_a_query_operator_now_outvotes_a_conflicting_structured_filter`
  becomes the refusal test.
- **#373**: apostrophe mangling. Decide `'` quote semantics; the GUI already
  treats it as literal.
- **#372**: ghost hits on deletion races.
- **#370**: a 422 array `detail` instead of problem+json.
- **#371**: the Tauri hop drops `lang` and the date filters.
- **#368**: MCP ignores unknown arguments.
- **#365**: embedded images count as attachments.

### 4. **#375 — the Mac build-hash probe** *(new)*
**Acceptance:** a `TimeoutExpired` is not cached, and a later `/v1/version`
re-probes (rate-limited). Measure *why* launchd children are slow before
touching `_GIT_TIMEOUT_S`.

### 5. **#374 + #305 — the `cli.py` refactor** *(carried, grown)*
**Acceptance for #305:** blocking `sqlparse` on `sys.meta_path` leaves
`localmail --version` exiting 0. With it:
- **#374:** extract a pure CLI query composer that sanitizes flag values and
  emits them first.
- **#331 point 2:** widen the search catch to `SearchArgumentRefused`.
- Print `sort_applied`/`rankable`. `cli.py` is **2177 lines**.

### 6. **Carried backlogs**
- **#358 / #357** (the shape should carry the contract).
- **#330 / #327 / #328** (search typing).
- **#319 / #318 / #320** (API keys; next free migration slot `0037_*.sql`).
- **#340** (harness lock vs database).
- **#363** (degraded rerank invisible).
- **#285** (ruff + CI: the operator's call).
- Admin GUI phase 5, the Users panel (no backend work; stub the new API
  module in **both** `AdminView.test.ts` and `MainView.test.ts`).
- Robustness: **#218 · #226 · #225/#227 · #200/#211/#208 · #206 · #204 ·
  #25**.

## Open decisions & risks

1. **One PR is open and yours to merge** (**#376**, `fix/367-filters-before-free-text`,
   based on `main` `964ff13`). **Open issues 32 → 31; Dependabot 7 → 1.**
2. **A merge does NOT close issues its subject merely names.** Use `Closes #N`
   in the PR body and check `gh issue list` afterwards.
3. **THIS FILE IS NOT THE AUTHORITY — `git` and `gh` are.** Session 46 wrote no
   handoff, so the header was a session stale again. Open with
   `git fetch --prune`, `git log --oneline -1 origin/main`,
   **`git log --oneline -1 -- NEXT_SESSION.md`** (a gap = no handoff),
   `gh pr list`, `gh issue list`, and the Dependabot query.
4. **The precedence flip is a wire change nobody asked for by name.** If a
   kastellan or MCP caller sends the same scalar in both `query` and
   `filters`, its results change on deploy. #369 is the fix; until then, the
   README says don't send both.
5. **`npm audit` and Dependabot disagree in time.** `nanoid` showed in
   `npm audit` before any alert existed. When touching `gui/`, run both.
6. **A green `/v1/version` is not the Mac's deploy check (#375).** Use the
   process start time and the log line instead.
7. **The test DB is on 5532, not 5432.** The 5432 cluster has no `localmail`
   role; the memory saying otherwise is corrected. The NOTIFY runbook checks
   must target 5532.
8. **The stale NOTIFY queue recurs (risk 30, carried).** Exactly three
   LISTEN/NOTIFY failures with `could not access status of transaction` means
   cluster state. Check both gates; re-run the three in isolation before
   suspecting the change.
9. **The machine ran at load 8–50 this session** (WindowServer, fseventsd,
   Chrome; no orphaned `while :` burners). Do not compare this session's
   suite time to a quiet-machine figure.
10. **Base every branch on `main`; one PR per session with its handoff**
    (carried). The stranded-branch check is noisy for squash merges; confirm
    with a content diff.
11. **In zsh an unquoted `$VAR` does not word-split** — use `${=T}`. Also, in
    zsh a bare `echo =====` is an `=`-expansion error that aborts the rest of
    a `;` chain (hit this session).
12. **A mutation that does not apply looks caught** (carried). Use the helper
    below, which refuses a non-unique anchor, and read *which* test failed.
13. **Restore a mutation from a file copy, never `git checkout`** (carried).
14. **Dependency floors: read the range against the lock, not the declared
    floor** (carried). `uv lock --dry-run` is read-only; `uv sync --dry-run`
    is **not**. Never a bare `uv sync`: `--extra mcp --extra extraction` on
    both hosts, and `~/.local/bin/uv` on the DGX.
15. **NEVER run two pytest sessions against one test database** (carried;
    enforced by #336/#337).
16. **A MagicMock attribute reaches the wire as `{}`/`[]`** (carried). Assert
    new wire keys through the real transport. No wire key was added this
    session.
17. **Membership outranks every guard; keyset predicates are row comparisons
    in both directions; the undated block is where directions differ; never
    state a sort the server cannot serve** (carried, #75/#322–#326/#345/#348/#353).
18. **CI trap** (carried): a GUI admin panel that fetches on mount must be
    stubbed in both `AdminView.test.ts` and `MainView.test.ts`. Run vitest
    from `gui/`. `cargo clippy --all-targets` is clean but ungated.
19. **CI reports `1 skipped` on Linux; macOS reports 0** (carried,
    pre-existing).
20. **The DGX drops are STILL UNEXPLAINED** (carried). The banner-exchange
    timeout this session was brief and self-healed. Don't theorise without a
    captured outage. `lan=FAIL` is the two hosts being on different subnets,
    not a drop.
21. **No ROADMAP.md** (carried) — that `/nextsession` step is a no-op.

## Exact commands to resume

```bash
cd /Users/hherb/src/localmail
git fetch --prune origin                 # ALWAYS first
git status && git branch --show-current
git log --oneline -1 origin/main         # THE authority, not this file's header

# RISK 3 — did the LAST session hand off?
git log --oneline -1
git log --oneline -1 -- NEXT_SESSION.md  # a gap = a session ended without one
ls -t docs/handoffs/ | head -3

# RISK 2 — after the merge, CHECK WHAT CLOSED.
gh pr list
gh issue list --limit 50                 # 32 open; 31 after the merge
gh issue view 367 --json state --jq .state

# Dependabot — expect 1 (#71 accelerate) once main carries the new locks.
gh api repos/hherb/localmail/dependabot/alerts \
  --jq '.[] | select(.state=="open") | "\(.number)\t\(.security_advisory.severity)\t\(.dependency.package.name)\t\(.security_vulnerability.vulnerable_version_range)"'
(cd gui && npm audit | tail -3)          # RISK 5 — can lead Dependabot

# RISK 9 — load and orphans before trusting timings.
uptime; ps -eo pid,ppid,etime,time,command | grep "[w]hile :; do :; done"

# AFTER MERGING — Mac (the tree IS the install):
#   git checkout main && git pull
#   unset VIRTUAL_ENV && uv sync --frozen --extra mcp --extra extraction
#   launchctl kickstart -k gui/$(id -u)/com.localmail.daemon
#   launchctl kickstart -k gui/$(id -u)/com.localmail.serve
#   ps -o pid,lstart,command -p $(pgrep -f "localmail serve" | head -1)   # after the pull? (#375)
# AFTER MERGING — DGX:
#   ssh 10.0.0.3 'cd ~/src/localmail && git pull --ff-only && ~/.local/bin/uv sync --frozen --extra extraction --extra mcp'
#   ssh 10.0.0.3 'systemctl --user restart localmail-daemon localmail-serve'
#   ssh 10.0.0.3 'curl -sk https://10.0.0.3:8443/v1/version'   # build_hash == merge SHA

# Python suite.
unset VIRTUAL_ENV && uv run pytest -q
#   macOS: expect 3622 passed, 0 skipped, 2 warnings (the #25 websockets pair).
#   Exactly 3 LISTEN/NOTIFY failures = RISK 8, not your change:
psql -h localhost -p 5532 -d postgres -U localmail -tAc 'SELECT pg_notification_queue_usage()'
psql -h localhost -p 5532 -d localmail_test -U localmail -c 'LISTEN daemon_commands'
unset VIRTUAL_ENV && uv run pytest -q tests/test_daemon_command_listen.py tests/test_daemon_commands_service.py
unset VIRTUAL_ENV && uv run pytest --collect-only -q | tail -1   # 3622 on the branch, 3543 on main@964ff13
unset VIRTUAL_ENV && uv run mypy src/localmail                   # Success, 155 files
unset VIRTUAL_ENV && uv run ruff check src/localmail/ | tail -1  # 10 (#285)

# #367 directly:
T="tests/test_api_search.py tests/test_api_search_filters_before_free_text.py tests/test_api_search_rank_without_text.py tests/test_api_search_malformed_query.py tests/test_api_search_filter_keys.py"
unset VIRTUAL_ENV && uv run pytest -q ${=T}   # 266 passed on the branch

# Frontend (only if you touch gui/; run from gui/):
# cd gui && npm run check && npm test && npm run build && cd ..
# cd gui/src-tauri && cargo test && cargo clippy --locked -- -D warnings && cd ../..

# Mutation helper (RISK 12). Snapshot first; restore from the snapshot (RISK 13).
cat > /tmp/mutate.py <<'PY'
"""Apply one mutation, report WHERE it landed, never guess."""
import ast, pathlib, sys
path, old, new = sys.argv[1], sys.argv[2], sys.argv[3]
p = pathlib.Path(path); s = p.read_text(); n = s.count(old)
if n != 1:
    sys.exit(f"ANCHOR MATCHED {n} TIMES — mutation NOT applied: {old[:70]!r}")
line = s[:s.index(old)].count("\n") + 1
p.write_text(s.replace(old, new))
tree = ast.parse(p.read_text())
o = [x for x in ast.walk(tree) if isinstance(x, (ast.FunctionDef, ast.ClassDef))
     and x.lineno <= line <= (x.end_lineno or 0)]
print(f"MUTATED {path}:{line} at {(max(o, key=lambda x: x.lineno).name + '()') if o else '<module>'}")
PY
T="tests/test_api_search_filters_before_free_text.py"
uv run python -B -m pytest -q -p no:cacheprovider ${=T}   # ${=T}, NOT $T

# Host health (Mac):
launchctl list | grep -i localmail
psql -h localhost -p 5532 -U localmail -d localmail -c \
  "SELECT worker_kind, account_id, state, now()-last_heartbeat_at AS age FROM daemon_heartbeats ORDER BY 1,2"
unset VIRTUAL_ENV && uv run localmail search-status    # under a second (#280)
```

`main` tip at session start was **`964ff13`**. This session left **one PR**
open (**#376**) on `fix/367-filters-before-free-text` — `9882cb0` (#367), `e3a1819`
(dependency floors), `ab73610` (README + CLAUDE.md) and the handoff commit.
Latest migration **`0036_api_keys.sql`**; next free slot `0037_*.sql` (none
added). **Open issues: 32 → 31 on merge. Dependabot: 7 → 1 on merge.**
