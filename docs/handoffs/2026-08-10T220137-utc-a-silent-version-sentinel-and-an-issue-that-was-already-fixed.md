# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-08-10 (session 24).** Session 23's two PRs (#289, #290)
> were **merged by the operator** before this one started, so `main` moved
> `76fef01` → **`8d31045`** and `localmail --version` is live on this host.
>
> This session closed **both** issues session 23's review had filed, by opposite
> means. **#291** — `--version` reporting `0.0.0+unknown` silently — is fixed in
> **PR #293** (`d8f49b4`), **CI green, not merged**. **#292** — the
> `_PRE280_CORRELATED_ALLOWLIST_SQL` "museum piece" note — was **closed with no
> code**: its premise did not survive checking (§2).
>
> **The shape of #291's fix is a redesign of #279, not a tweak.**
> `@click.version_option` is gone from `cli.py` and is now forbidden **in every
> spelling**, where #279 merely required it to carry `__version__`. Two of
> #279's three pins changed shape in the process, and both got stronger. Read §1
> before touching anything version-related — several CLAUDE.md sentences you may
> remember are now wrong on purpose.
>
> **One process lesson worth more than the code:** a source-*text* pin and the
> prose explaining it are in conflict by construction. Writing down *why*
> `@click.version_option` is banned broke the pin that bans it. See risk 3.

## Project context (1-minute version)

`localmail` mirrors IMAP accounts (Gmail OAuth, password) into Postgres,
**strictly read-only w.r.t. IMAP**. The database is canonical for accounts.
Daemon: hot-reload account set, heartbeats, DB command queue, two-plane
supervision. Web admin UI (HTMX): account CRUD, user management, archive
imports, daemon control. Hybrid search (Phases 1+2) + an HTTPS GUI server + a
remote MCP server (optionally a full OAuth 2.1 authorization server) + the
opt-in `--smart` LLM query rewriter are all shipped. A Tauri 2 + Svelte 5 GUI
lives under `gui/` — read-only viewer plus an admin mode (Accounts + Daemon
panels shipped; Users + Imports still placeholders). Version **0.3.0**.
Licensed AGPL-3.0-or-later (per-file SPDX headers in `src/localmail/`; **not**
in `gui/`). See [CLAUDE.md](CLAUDE.md), [README.md](README.md).

## What we shipped this session

### 1. PR #293 → commit `d8f49b4` — an unresolvable version is now *reported* (#291)

Branch `fix/291-version-unknown-diagnostic`. **Open, CI green (3m24s),
unmerged.** No migration, no new dependency, no config change.

`localmail --version` answered a broken install with `0.0.0+unknown`, exit 0,
and nothing on stderr — "the version could not be determined" in a format
indistinguishable from a success, at the one moment an operator is diagnosing a
broken install. The sentinel lived in `__init__.py` and was surfaced **nowhere**:
`grep -rn "0.0.0+unknown"` found only its two definitions, a comment, and test
assertions.

**The fallback itself stays.** Import must not fail, and `/v1/version` emitting
`server_version: null` breaks the GUI's connect probe, which decodes that field
as a non-optional String. Only the operator-facing presentation was wrong.

New pure module [src/localmail/version_report.py](src/localmail/version_report.py)
owns the resolution and the wording: `UNKNOWN_VERSION` (named, not repeated),
`VersionSource`, `resolve_version`, pure `unknown_version_diagnostic`.
`__init__.py` resolves **once** and exports the pair `__version__` /
`__version_source__`.

**The two causes are kept apart because the remedies differ** — the only reason
to read the line at all:

| cause | meaning | remedy |
|---|---|---|
| `NOT_INSTALLED` | no dist-info — an uninstalled tree | `uv sync`, or `uv tool install localmail` |
| `METADATA_INCOMPLETE` | dist-info present, no `Version:` | add `--reinstall` |

`uv sync` does not repair the second. They used to collapse to one string.
`python -m localmail` from a checkout is a first-class entry point (the 2B.4
supervisor launches the daemon that way), so `NOT_INSTALLED` is the reachable
case; `uv tool install` stamps metadata, so the manual's install-verification
path does not normally land here.

**Three deliberate calls a future session would plausibly undo — don't:**

- **stdout stays one machine-readable line; the diagnostic goes to stderr.**
  That is why this is *not* `version_option(message=…)`, whose message is
  echoed to stdout. `--version` is scripted — it is the manual's
  install-verification step — and a warning on stdout breaks every naive parser.
- **Exit stays 0 on the unknown path.** The explicit decision #291 asked for. A
  non-zero status breaks every script using the flag as a liveness check, and
  argues against degrading gracefully rather than raising the way click's own
  lookup does. The stderr line carries the diagnosis.
- **`@click.version_option` is banned outright now**, not merely constrained.
  click's own callback prints and exits without ever consulting *why* the
  version is what it is, so even the compliant `version_option(__version__)`
  printed the sentinel and said nothing.

**Two of #279's three pins changed shape, and both got stronger:**

| pin | #279 | #291 |
|---|---|---|
| derivation | source regex ending `[,)]` | **behavioural**: rebind `localmail.cli.__version__`, assert the flag prints it |
| the `version_option` ban | regex over comment-stripped text, exactly one allowed | **AST walk**, zero allowed |
| config/DB-free | unchanged (`forbid_db` + the `list-accounts` filename control) | unchanged |

The derivation pin is the interesting one. The decorator froze its argument at
**decoration** time, so from the callback's point of view it *was* a literal —
the mutation proves it, failing with `assert '0.3.0' == '9.9.9+sentinel'`. That
is why `_print_version` reads the module attribute at **call** time. The new pin
also catches an f-string-assembled version, which no regex reliably does.

**10 mutations run, each caught** (every file restored from a **copy**, never
`git checkout` — session 23's trap):

| mutation | tests that caught it |
|---|---|
| drop the stderr diagnostic (the defect, restored) | 3 |
| diagnostic to stdout instead of stderr | 3 |
| collapse the two causes to one message | 2 |
| a fourth `VersionSource` with no remedy | 1 |
| reinstate `@click.version_option(__version__)` | 5 |
| hardcode the version literal in the echo | 3 |
| `__version__ + "-dev"` | 5 |
| non-zero exit on the unknown path | 1 |
| empty-string metadata treated as a real version | 1 |
| warn even when the version IS known | 2 |

### 2. #292 closed with no code — the premise did not survive checking

#292 asked for a durable home for the "do not tidy up
`_PRE280_CORRELATED_ALLOWLIST_SQL`" rule, on the grounds that the #290 review
found it "living nowhere else — not in CLAUDE.md, not in the test file, not in a
comment". **All three were present the whole time**, and the review was reading
the handoff rotation rather than the tree:

1. A comment at the constant since `bc5b556` (the #280 commit itself):
   `Do not "fix" it; it is a museum piece.` — including the
   `--predicate-form pre75` parallel the issue asked for.
2. [CLAUDE.md:1353](CLAUDE.md) — "Both keep the pre-#280 predicate verbatim as a
   negative control".
3. **It is not dead code.** `_PRE280_COUNTS_SQL` is consumed by two named tests
   ([test_extract_queue_sql.py:383](tests/test_extract_queue_sql.py#L383),
   [:466](tests/test_extract_queue_sql.py#L466)), so deleting the constant is an
   immediate `NameError` at collection — a stronger guard than any comment.

Closed as already satisfied, with that evidence posted on the issue.

### 3. Verification (this Mac, all extras)

- `unset VIRTUAL_ENV && uv run pytest -q` → **2385 passed, 0 skipped, 0 failed**
  in 180 s. `main` measures **2369** (+16: 8 in `test_version_report.py`, 8 in
  `test_version_single_source.py`).
- `unset VIRTUAL_ENV && uv run mypy src/localmail` → **Success, 141 source
  files** (was 140; +1 for `version_report.py`).
- `uv run ruff check` on every changed file → clean. The 10 repo-wide errors are
  pre-existing #285 and **identical on `main`** (verified by stashing).
- Real invocation: `localmail, version 0.3.0` on stdout, stderr empty. Simulated
  uninstalled tree prints the sentinel on stdout and the two-line warning on
  stderr, exit 0.
- The 4 pytest warnings are pre-existing (websockets #25 + `psycopg_pool`
  teardown `ResourceWarning`s); the count varies run to run with teardown
  timing. None come from this change.

### 4. Host health confirmed, not assumed

Both launchd services `running`/`active`; **7 fresh `daemon_heartbeats` rows**
(idle:1, idle:59, poll:1, poll:59, embed, extract, reconcile), all under 30 s.
`search-status` **828 ms**:

```
messages_total 127584
blobs_eligible 9495 = extracted 9207 + no_text 106 + gave_up 182 + pending 0
blobs_claimable 0
```

Partition sums, `claimable` agrees with `pending`. Two blobs more than session
23's 9493 — the archive grows; check the **shape**, not the literals.
**Dependabot: 0 open alerts.**

### 5. Docs

- **README** — a paragraph under `## CLI` describing the unknown-version
  behaviour, both remedies, and that stdout and the exit status are unchanged.
- **CLAUDE.md** — the #279 section was **factually wrong** after this change
  (it still said the version is "passed to `click.version_option` explicitly")
  and is corrected; a new `version_report` block records the six invariants;
  the layout tree and the Commands block gained entries.
- **No ROADMAP.md** (that `/nextsession` step remains a no-op).

## What's next

### 0. **Merge PR #293** — the only open PR
   Green and unmerged; **the operator merges** (project convention). Closes
   #291, taking open issues **13 → 12**.
   - **Acceptance:** `uv run localmail --version` still prints `localmail,
     version 0.3.0` with **empty stderr** on both hosts. The diagnostic path is
     unreachable on an installed host by design — do not try to provoke it there;
     it is covered by tests and by the simulated run recorded in §3.
   - Then deploy the DGX (it is at `8d31045`, one commit behind after the merge):
     the recipe in the resume block.
   - **The Mac daemon still predates #286** (process start 07:41:48 on 2026-08-09
     vs #286's merge at 07:49:41) and still does not need restarting — #280 left
     `_claim_batch` untouched, so the running worker's behaviour is identical.
     `launchctl kickstart -k gui/$UID/com.localmail.daemon` if you want it tidy;
     costs a startup blob-temp sweep (risk 17).

### 1. **#278 — the version surface's other half** *(carried; needs YOUR decision first)*
   The obvious next slice now that both #279 and #291 have closed. The GUI About
   tab renders a `build_hash` that `/v1/version` has **never** emitted, so the
   "Server build" row always shows `?` — while five test files mock the field and
   make it look covered. **Two options, product call before any code:** emit a
   real build hash (from git, at build time — but the version is stamped at
   *install* time, so this needs a story for `uv tool install` from a tarball),
   or delete the field end-to-end including the five mocks. Deleting is cheaper
   and honest; emitting is more useful on a host where `--version` alone cannot
   distinguish two builds of `0.3.0`.

### 2. **#285 — ruff, repo-wide** *(carried)*
   Every `# noqa: S608` in the tree is a dead directive: `ruff check --select
   S608 --ignore-noqa` reports nothing on those files, so the rule never fired.
   Still **9** across 5 files (`grep -rn "noqa: S608" src/ | wc -l`) — unchanged
   this session. There is no `[tool.ruff]` config and no CI step; repo-wide
   `ruff check src/localmail/` reports **10 errors** (the "131" in earlier
   handoffs was a whole-repo count including `tests/`; the `src/` figure is the
   one this session measured, on both `main` and the branch). Two separable
   decisions — adopt ruff properly, or drop the directives and keep the reasoning
   as plain comments — worth deciding once.

### 3. **Admin GUI phase 5 — Users & ACL panel** *(carried, still the design's next slice)*
   `/v1/admin/users` is already `require_admin()` (bearer-capable) — **no backend
   work needed.** Service layer:
   [src/localmail/api/admin/users.py](src/localmail/api/admin/users.py).
   **Acceptance:** a `UsersPanel.svelte` replacing the placeholder tab: list,
   create, delete, per-account ACL grant/revoke (a checklist over every account),
   `is_admin` toggle, password reset, enable/disable. Surface the **two lock-out
   guards as 409s** — the count-based last-admin rule (`LastAdminError`) and the
   identity-based self-action rule. Mirror
   [serve/admin/users_panel_router.py](src/localmail/serve/admin/users_panel_router.py).
   Follow the Daemon-panel shape, and **stub the new API module in both
   `AdminView.test.ts` and `MainView.test.ts`** (see risk 12).

### 4. **Remaining robustness backlog** *(carried)*
   **#218** (GUI download commands buffer the full body before enforcing the
   size ceiling) · **#226** (self-signed cert misses the reachable IP when
   `--bind 0.0.0.0`) · **#225 / #227** (`/v1/changes` subscription lifecycle
   gaps) · **#200 / #211 / #208** (admin panels silently swallow 4xx) ·
   **#206** (GUI AccountForm: folder filters not editable) · **#204** (admin
   bearer-token scope) · **#25** (websockets DeprecationWarning).

### 5. **Smaller, deliberately not done** *(carried)*
   - **`cli.py` is now 1959 lines** (+48 this session), `daemon.py` 573 — both
     over the 500-line guideline. The refactor session 21 deferred is still owed
     in full. #291 put its logic in a new 125-line pure module rather than in
     `cli.py`, but the callback and its rationale still landed there.
   - **165 `docling: File format not allowed` failures on the Mac** (of 182
     `blobs_gave_up`), and **127** on the DGX.
   - **Residual implausible language labels are dominated by `ja`** (229 of the
     Mac's 350). 0.24% of labels; the confidence-floor lever was measured
     useless. If ever chased, **sample the `ja` rows first**.
   - **The DGX drops remain uninvestigated and unexplained** (risk 4).
   - **#269's suggestion 2** (blob-temp sweep off the critical path) — deferred
     in session 19; reopen only if cold-cache startups grow past tolerable.

## Open decisions & risks

1. **One PR is open and yours to merge.** `main` is `8d31045`; **#293**
   (`fix/291-version-unknown-diagnostic`) closes #291. **13 open issues** → 12
   once it merges (#292 was closed this session). **Dependabot: 0 open alerts.**
2. **`--version`'s contract is now four things, all pinned** *(supersedes session
   23's risk 10)*. It must (a) read no config and touch no database — the moment
   an operator most needs a version is the moment those lookups fail; (b) keep
   **stdout** to the single machine-readable line; (c) put any diagnostic on
   **stderr**; (d) **exit 0** even when the version is unknown. And **do not
   reintroduce `@click.version_option` in any spelling** — the AST pin forbids
   it, for two independent reasons (it bypasses the diagnostic entirely, and the
   bare form adds a second metadata reader that raises `RuntimeError` where every
   other reader degrades).
3. **A source-*text* pin and the prose explaining it are in conflict by
   construction — use the AST** *(new)*. The rationale for banning
   `@click.version_option` necessarily quotes its spelling, in a comment beside
   the replacement *and* in `_print_version`'s docstring. #279's
   regex-over-stripped-comments handled the comment but not the docstring, so
   **writing the reason down broke the pin that enforces it** — it did, once,
   mid-session. `_mentions_version_option` walks `ast.parse` instead: prose is
   not code, and the AST is where that distinction already lives. Applies to any
   future "this construct must not appear" pin.
4. **`CliRunner.result.output` is stdout AND stderr concatenated in click 8.4**
   *(new)*. `_printed_version` anchors on the tail (`rpartition("version ")`), so
   once a diagnostic containing the word "version" went to stderr it started
   reading from whichever stream spoke last. **Pass `result.stdout`.** Any new
   assertion about a CLI's machine-readable output has the same trap.
5. **An issue's premise can be wrong — check the tree, not the handoff** *(new,
   #292)*. #292 was filed on a review finding that a rule lived nowhere; it lived
   in three places, one of them two live tests that make deletion a `NameError`.
   Cost: one `git log -L` and one `grep`. **Do this before writing code for any
   carried issue**, especially one filed from a docs review.
6. **When reverting a mutation, restore from a file copy — never `git
   checkout`** *(carried, session 23)*. An uncommitted fix lives only in the
   working tree, so `git checkout <file>` silently discards it and the next
   "mutation" runs against no fix at all. The tell was `Updated 0 paths from the
   index`. This session used `cp` + an md5 check after every revert; one
   mutation script also mangled its target instead of mutating it and produced
   **no test output at all** — treat an empty result as a failed mutation, not a
   passing one.
7. **`search-status` is sub-second on BOTH hosts — stop budgeting minutes**
   *(carried)*. Mac **828 ms** this session, DGX **0.756 s**. If it ever runs
   long that is a **regression** of #280: check `EXPLAIN (FORMAT JSON)` for a
   `Seq Scan on messages` under a `SubPlan` before looking anywhere else.
8. **The DGX drops are STILL UNEXPLAINED — five theories refuted** *(carried; not
   investigated this session)*. **Do not propose a sixth without a captured
   outage in which the host was demonstrably up throughout.** Triage with
   `journalctl --list-boots` first. **Power is not a candidate** (~5-day UPS).
   **Do not edit `/etc/wireguard/wg0.conf`.** Session 19 established that the LAN
   address answers ping and refuses SSH, so a green `lan=` probe line is *not*
   evidence it is the DGX. **Try `10.0.0.3` first.** A single `tunnel=FAIL` probe
   sample is not an outage — sustained means several consecutive samples.
9. **`blobs_claimable 0` alongside `blobs_pending 0` is the SETTLED archive
   shape, on both hosts** *(carried)*. The non-allowlisted remainder was claimed
   and disposed of with `type-skipped` rows long ago, so it is not claimable. The
   #216 shape (`pending 0` alongside a large `claimable`) is what a *fresh*
   image-heavy archive looks like.
10. **A steady non-zero `blobs_no_text` is NORMAL** *(carried — #277)*. Terminal
    by design; read it like `body_lang_declined`. **`blobs_gave_up` is the one to
    act on** — `list-failed-extractions` says why (poison-pill half only),
    `retry-failed-extractions` re-queues. **`QueueCounts.__post_init__` raises on
    two distinct conditions** (#284): `misfiled` is checked **before** the sum,
    deliberately. A fifth disposition goes in `BUCKET_WHERE_SQL`; everything else
    derives. **`DISTINCT` in `EXTENSION_MATCH_JOIN_SQL` is load-bearing with no
    runtime guard** — its only symptom is `pending` diverging from `claimable`.
11. **`type-skipped` is a one-way door for a widened allowlist** *(carried)*.
    Clear deliberately: `DELETE FROM attachment_text WHERE extractor =
    'type-skipped'`. **#266's whitespace-heal is a one-way door too** — what
    makes it safe is the `is_blank` gate, not the nature of the data.
12. **CI trap** *(carried)*: any GUI admin panel that fetches on mount MUST be
    stubbed in **both** `AdminView.test.ts` and `MainView.test.ts`, or an
    unhandled promise rejection leaks while vitest still reports "passed".
13. **`--relabel` is the only destructive verb in the lang path** *(carried)*.
    Discards **every** label; archive unsearchable by `lang:` until the drain
    completes. Prompts unless `--yes`. Reach for `--retry-declined` first. Budget
    ~45 min for a 100k-row archive, and note that **`reopen_all`'s bulk UPDATE
    shows no progress in `pg_stat_activity` until it commits** — tens of minutes
    of apparent hang is expected; do **not** cancel it. `body_lang_pending` means
    claimable work only; a steady non-zero `declined` is **normal** (12,182 Mac,
    1,973 DGX). **Do not add normalisation steps to `lang_text.py` without a
    measurement** — every candidate beyond URL-stripping measured zero.
14. **Test-count baselines: measure, don't subtract** *(carried)*. `main` →
    **2369**, this branch → **2385**. Cheap to check without a DB:
    `uv run pytest --collect-only -q | tail -2`.
15. **Do not run the test suite while a backfill is draining** *(carried)*.
    Shared-cluster contention produces dozens of false failures. `search-status`
    does not qualify (risk 7).
16. **The macOS socket deselect is GONE — stop using it** *(carried)*. #276 fixed
    it; `uv run pytest -q` with **no arguments** is the right command.
17. **An empty `daemon_heartbeats` right after a daemon restart is normal for
    minutes** *(carried — #269/#271)*. It is the startup blob-temp sweep. Grep
    for **`blob-temp sweep done: walked=`**.
18. **The stale NOTIFY queue read CLEAN all session** *(carried; treat "clear at
    the last handoff" as worth nothing — session 22 saw it recur mid-session)*.
    Fix is the runbook's Option A: `launchctl bootout
    gui/$UID/com.localmail.daemon`, **wait until `launchctl print` says the
    service is gone**, verify both gates, then `bootstrap` back. **Verify both
    gates, not one:** session 22 saw `pg_notification_queue_usage()` read healthy
    while `LISTEN daemon_commands` errored outright; session 19 saw the inverse.
19. **`uv sync` without extras silently downgrades a host** *(carried)*. Use
    `--all-extras` on the Mac and `--extra mcp --extra extraction` on the DGX.
    **`uv` is not on the DGX's default non-interactive PATH** — use
    `~/.local/bin/uv` over SSH. **A non-zero `skipped` count means an extra went
    missing.** CI installs only `--extra mcp`, so its count differs by design.
    **The skip to look for by name** is
    `tests/test_extractor.py::test_pdf_pipeline_options_reflect_the_configured_engine`
    (the #248 OCR-engine pin).
20. **`ExtractorConfigurationError` subclasses `TransientExtractorError`, and
    that subclassing *is* the #248 fix — do not "clean it up"** *(carried)*.
21. **`run_embed_worker_once`'s `failure_log` defaults to the process-wide
    `embed_worker._FAILURE_LOG`, and that default is the fix** *(carried —
    #267)*. **A new looping caller should pass nothing.**
22. **No ROADMAP.md** *(carried)* — the `/nextsession` ROADMAP step is a no-op;
    slice status lives here + `docs/handoffs/` + the specs. **README WAS updated
    this session** (the unknown-version paragraph under `## CLI`).
23. **Secrets/ACL invariants unchanged** *(carried)*: `secrets.configure`'s pin
    kept though #245 is fixed; #246 warns on group-write; `InsecureSecretsFile`
    refuses; #239's manual tombstone retention deliberate; admin bearer tokens
    have no per-token scope (#204).
24. **Run vitest from `gui/`, not the repo root** *(carried)*. **`cargo clippy
    --all-targets` is clean but ungated** — CI runs clippy without
    `--all-targets`, so `#[cfg(test)]` modules are never linted.
25. **Do not "tidy up" `_PRE280_CORRELATED_ALLOWLIST_SQL`** *(carried; #292
    closed this session confirming the note is durable)*.
    [tests/test_extract_queue_sql.py:306](tests/test_extract_queue_sql.py#L306)
    holds the pre-#280 correlated predicate **on purpose**, as the negative
    control that proves the plan assertions can fail — the same role
    `--predicate-form pre75` plays in `run_browse_explain.py`. It is consumed by
    two named tests, so deleting it is a `NameError`, not a silent loss.

## Exact commands to resume

```bash
cd /Users/hherb/src/localmail
git fetch --prune origin                 # ALWAYS first
git status                               # expect clean
git branch --show-current
git log --oneline origin/main..main      # expect 0

# ONE PR is open, awaiting your merge (What's next, 0).
gh pr list
gh pr checks 293
gh issue list --limit 20                 # 13 open; 12 once #293 merges

# Dependabot: 0 open. A non-zero count right after a lockfile merge is scan
# lag — check uv.lock before chasing it.
gh api repos/hherb/localmail/dependabot/alerts \
  --jq '[.[] | select(.state=="open")] | length'

# AFTER MERGING #293:
#   Mac:  git checkout main && git pull && uv sync --all-extras
#   DGX:  ssh 10.0.0.3 'cd ~/src/localmail && git pull && ~/.local/bin/uv sync --extra mcp --extra extraction && ~/.local/bin/uv run localmail init-db && systemctl --user restart localmail-daemon localmail-serve'
#   expect on both: `localmail --version` -> `localmail, version 0.3.0`,
#   exit 0, and NOTHING on stderr:
unset VIRTUAL_ENV && uv run localmail --version 2>/tmp/v.err; echo "exit=$?"; wc -c < /tmp/v.err   # expect 0

# Python test suite. No --deselect (risk 16).
# Do NOT run while a backfill is draining (risk 15).
unset VIRTUAL_ENV && uv sync --all-extras   # NOT a bare `uv sync` — risk 19
unset VIRTUAL_ENV && uv run pytest -q
#   expect: 2385 passed, 0 SKIPPED on fix/291…; 2369 on main.
#   MEASURE, don't subtract (risk 14):
unset VIRTUAL_ENV && uv run pytest --collect-only -q | tail -2   # no DB needed

unset VIRTUAL_ENV && uv run mypy src/localmail
#   expect: Success, 141 source files

# Host health checks:
launchctl print gui/$UID/com.localmail.daemon | grep -E '^\s*(state|pid) '
psql -h localhost -p 5532 -U localmail -d localmail -c \
  "SELECT worker_kind, account_id, state, now()-last_heartbeat_at AS age
     FROM daemon_heartbeats ORDER BY worker_kind, account_id"
#   expect: 7 rows, ages under ~60 s (EMPTY during the startup sweep — risk 17)

# The attachment counters — UNDER A SECOND on both hosts (risk 7):
unset VIRTUAL_ENV && uv run localmail search-status
#   These MOVE as the archive grows (9491 -> 9493 -> 9495 over two days), so
#   check the SHAPE, not the literals: the four buckets must sum to
#   blobs_eligible, and claimable must equal pending.
#   Mac  approx: blobs_eligible ~9.5k = ~9.2k + 106 + 182 + 0, claimable 0
#   DGX  approx: blobs_eligible ~4.4k = ~4.1k +  91 + 127 + 0, claimable 0
# If it takes minutes again that is a REGRESSION of #280:
psql -h localhost -p 5532 -U localmail -d localmail -c \
  "SELECT now()-query_start, left(query,60) FROM pg_stat_activity
    WHERE datname='localmail' AND state='active' AND pid <> pg_backend_pid()"

# NOTIFY gates — CLEAR as of this handoff; check BOTH only if those 3 tests fail:
psql -h localhost -p 5532 -d postgres -U localmail -tAc 'SELECT pg_notification_queue_usage()'  # 0
psql -h localhost -p 5532 -d localmail_test -U localmail -c 'LISTEN daemon_commands'            # LISTEN

# The DGX — at 8d31045, one commit behind once #293 merges. Use the WireGuard
# address; uv is not on its non-interactive PATH (risks 8, 19):
ssh 10.0.0.3 'systemctl --user is-active localmail-daemon localmail-serve localmail-wgprobe'
ssh 10.0.0.3 'cd ~/src/localmail && git log --oneline -1'
tail -5 ~/localmail-probe/tunnel-probe.log   # expect lan=ok(3/3)@<addr>

# Frontend — only if you touch gui/ (MUST run from gui/ — risk 24):
cd gui && npm run check && npm test && npm run build && cd ..
cd gui/src-tauri && cargo test && cargo clippy --locked -- -D warnings \
  && cargo clippy --all-targets -- -D warnings && cd ../..
```

`main` tip is **`8d31045`** (PR #290). This session's work is **`d8f49b4`** on
`fix/291-version-unknown-diagnostic`, **open as PR #293, CI green, not merged**.
Latest migration **`0035_messages_body_lang_attempted_at.sql`**; next free slot
`0036_*.sql` (this session adds none). **Open issues: 13** (12 once #293 closes
#291; #292 was closed here). **Dependabot: 0 open alerts.** Both hosts run
`8d31045`; the Mac daemon process still predates #286 and deliberately was not
restarted (behaviour identical — see What's next 0).
