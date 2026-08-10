# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-08-10 (session 23).** A small session by design. Session
> 22's three PRs (#286, #287, #288) were **merged by the operator** before this
> one started, so `main` moved `57ce228` → **`76fef01`** and Dependabot went
> **2 open alerts → 0**.
>
> This session did two things: **deployed the DGX** (it was three commits
> behind) and shipped **#279** — `localmail --version`, the flag the manual has
> been telling users to run all along. One commit, **`8b752ef`**, open as
> **PR #289**, **CI green, not merged**.
>
> **Both #280 measurements are now confirmed on live archives, on two hosts:**
> Mac **1.30 s**, DGX **0.756 s**. The 13½-minute era is over on both.
>
> **One number in the last handoff was wrong and you should not trust it:** it
> recorded `fix/280…` at **2358** tests. The branch as *merged* is **2366** —
> its own review follow-ups added 8 more tests after that count was taken. See
> risk 2, because a stale baseline is exactly what makes risk 17 misfire.

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

### 0. The DGX is deployed — `57ce228` → `76fef01`

`git pull`, `~/.local/bin/uv sync --extra mcp --extra extraction`,
`systemctl --user restart localmail-daemon localmail-serve`. The sync moved
**pypdf 6.14.2 → 6.15.0** (the two Dependabot advisories) and nothing else of
substance. Both units `active`; five heartbeat rows (idle:1 / poll:1 / embed /
extract / reconcile) all fresh.

**The DGX's own `search-status` numbers — the second data point #280 never
had:**

```
real 0m0.756s
blobs_eligible 4363 = extracted 4145 + no_text 91 + gave_up 127 + pending 0
blobs_claimable 0
messages_total 37493
```

The partition sums, `claimable` agrees with `pending`, and the shape matches
the Mac's exactly (a settled archive: everything non-allowlisted was disposed
of with `type-skipped` rows long ago). It was never timed pre-fix on this host,
so there is no before/after — only the confirmation that the fix generalises.

### 1. `search-status` re-measured on the Mac — 13:28.45 → **1.30 s**

Independent re-run of #280's headline claim on the live archive, unchanged
command:

```
blobs_eligible 9493 = extracted 9205 + no_text 106 + gave_up 182 + pending 0
blobs_claimable 0
```

Two blobs more than the handoff's 9491/9203 — the archive grew between
measurements. Everything else identical.

### 2. PR #289 → commit `8b752ef` — `localmail --version` (#279)

Branch `fix/279-cli-version-option`. **Open, CI green (3m12s), unmerged.** No
migration, no new dependency, no config change.

[`docs/manual/users/setup/install.html:99`](docs/manual/users/setup/install.html#L99)
tells users to run `localmail --version` as the **install-verification** step.
There was no `version_option` in the CLI, so it printed
`Error: No such option '--version'` — failing at the one point where a user
cannot tell a broken install from a missing flag. It also closes a real
diagnostic gap: `/v1/version` was the only reader of `localmail.__version__`,
so on a host running just the sync daemon the version was unobtainable without
starting `serve`.

**The one judgement call, and the reason the decorator carries a comment:**
the version is **passed explicitly, never detected**. A bare
`@click.version_option()` looks equivalent — click reads the distribution
metadata itself — but adds a *second, independent* lookup that disagrees with
`__version__` in exactly the case `__init__.py`'s `or`/`except` guards exist
for: on a tree that was never installed click raises `RuntimeError` where every
other reader degrades to `0.0.0+unknown`.

Three tests in `tests/test_version_single_source.py` (which is where the
"one version literal per ecosystem" narrative already lives), all watched fail
first — the first with `No such option '--version'`, i.e. the defect itself:

| test | pins |
|---|---|
| `…_reports_the_package_version` | exits 0, prints `__version__` |
| `…_needs_no_config_or_database` | reads no config, touches no DB |
| `…_is_derived_not_a_literal` | source-level: neither a literal nor the bare form |

**Two details a future session would plausibly undo — don't:**

- **The derivation pin is source-level, not a value comparison.** Same reason
  `test_gui_client_version_is_injected_not_a_literal` is: comparing values
  cannot tell a derivation from a literal that happens to match the installed
  distribution, which is the normal state right after a release. It rejects
  **two** spellings, and the second is the subtle one — a bare
  `version_option()` passes both value tests, so the source pin is the only
  thing standing between the codebase and a silent second metadata reader.
- **`list-accounts` is a negative control inside the config-free test**, not
  incidental. It proves the pointed-nowhere `LOCALMAIL_CONFIG` actually bites
  (`FileNotFoundError`, exit 1). Without it that test is the value test with
  extra steps — it would pass against an env var nothing reads. Same role
  `_PRE280_CORRELATED_ALLOWLIST_SQL` and `--predicate-form pre75` play
  elsewhere.

**Mutations run, each caught by exactly one test:**

| mutation | caught by |
|---|---|
| `@click.version_option("0.3.0")` | source pin only |
| `@click.version_option()` | source pin only — value tests pass, which is the point |
| decorator removed | all three |
| `version=__version__` | *green* — no false positive |
| `__version__, package_name="localmail"` | *green* — no false positive |

**A process note worth keeping.** The first mutation run was invalid and I
nearly recorded it as evidence: reverting mutation 1 with `git checkout
src/localmail/cli.py` discarded the *uncommitted fix* too, so "mutation 2" ran
against a tree with no fix at all and produced a plausible-looking 3-failure
result. Back up the file, don't `git checkout` an uncommitted change. Caught by
noticing `Updated 0 paths from the index` on the second revert.

### 3. Verification (this Mac, all extras)

- `unset VIRTUAL_ENV && uv run pytest -q` → **2369 passed, 0 skipped, 0
  failed** in 144 s. `main` measured at **2366** (see risk 2 — this is a
  *measured* baseline, not the handoff's arithmetic).
- `unset VIRTUAL_ENV && uv run mypy src/localmail` → **Success, 140 source
  files**.
- `uv run ruff check` on both changed files → clean.
- Real invocation: `localmail, version 0.3.0`. Click auto-detects the prog name
  from `sys.argv[0]`, so it matches the manual verbatim. (Under `CliRunner` the
  prog name is `main`, which is why the tests assert on the version substring
  and not the whole line.)
- The 3 pytest warnings are pre-existing `psycopg_pool` teardown
  `ResourceWarning`s ("cannot join current thread"), unrelated to this change.

### 4. Housekeeping confirmed, not assumed

- **Dependabot: 2 → 0 open alerts.** They still read `2` at session start —
  GitHub had not yet rescanned `main` after #288 merged. `uv.lock` already
  carried pypdf 6.15.0 at that moment. **A stale alert count right after a
  lockfile merge is scan lag, not a missed fix** — check the lock before
  chasing it.
- **README updated** (see below), **CLAUDE.md updated**, **no ROADMAP.md**
  (that `/nextsession` step remains a no-op).

### 5. Docs

- **README** gained a short global-options paragraph under `## CLI`. It covers
  `--config` as well as `--version`: `--config` was equally undocumented, and a
  "global options" note with a single entry reads oddly. Mild scope stretch,
  flagged deliberately.
- **CLAUDE.md** gained the `--version` line in the Commands block and a
  paragraph beside the `--config`/#245 one, recording the explicit-version
  rationale and both pins.

## What's next

### 0. **Merge PR #289** — then optionally restart the Mac daemon
   Green and unmerged; **the operator merges** (project convention). Closes
   #279, taking open issues 13 → 12.
   - **Acceptance:** `uv run localmail --version` prints `localmail, version
     0.3.0` on both hosts.
   - **The Mac daemon was deliberately NOT restarted this session.** Its
     process (started 07:41:48) predates the merge of #286 (07:49:41), so it
     holds the pre-#280 `extract_queue` module — but #280 deliberately left
     `_claim_batch` untouched (its join shape is pinned by
     `test_the_claim_join_shape_never_touches_messages`), so the running
     worker's behaviour is **identical**. One command if you want it tidy:
     `launchctl kickstart -k gui/$UID/com.localmail.daemon`. Not urgent, and
     costs a startup blob-temp sweep (risk 16).
   - The DGX is already restarted and current.

### 1. **#278 — the version surface's other half** *(carried; needs YOUR decision first)*
   Now the obvious next slice, since #279 just closed its sibling. The GUI
   About tab renders a `build_hash` that `/v1/version` has **never** emitted,
   so the "Server build" row always shows `?` — while five test files mock the
   field and make it look covered. **Two options, and it needs a product call
   before any code:** emit a real build hash (from git, at build time — but the
   version is stamped at *install* time, so this needs a story for
   `uv tool install` from a tarball), or delete the field end-to-end including
   the five mocks. Deleting is cheaper and honest; emitting is more useful on a
   host where `--version` alone can't distinguish two builds of `0.3.0`.

### 2. **#285 — ruff, repo-wide** *(carried)*
   Every `# noqa: S608` in the tree is a dead directive: `ruff check --select
   S608 --ignore-noqa` reports nothing on those files, so the rule never fired.
   There is no `[tool.ruff]` config and no CI step; repo-wide `ruff check`
   reports 131 pre-existing errors. Two separable decisions (adopt ruff
   properly, or drop the directives and keep the reasoning as plain comments) —
   worth deciding once.

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
   `AdminView.test.ts` and `MainView.test.ts`** (see risk 11).

### 4. **Remaining robustness backlog** *(carried)*
   **#218** (GUI download commands buffer the full body before enforcing the
   size ceiling) · **#226** (self-signed cert misses the reachable IP when
   `--bind 0.0.0.0`) · **#225 / #227** (`/v1/changes` subscription lifecycle
   gaps) · **#200 / #211 / #208** (admin panels silently swallow 4xx) ·
   **#206** (GUI AccountForm: folder filters not editable) · **#204** (admin
   bearer-token scope) · **#25** (websockets DeprecationWarning).

### 5. **Smaller, deliberately not done** *(carried)*
   - **`cli.py` is now 1911 lines** (+5 this session), `daemon.py` 573 — both
     over the 500-line guideline. The refactor session 21 deferred is still
     owed in full; #279 was five lines and did not make it worse in kind.
   - **165 `docling: File format not allowed` failures on the Mac** (of 182),
     and **127** `blobs_gave_up` on the DGX. Visible in about a second now.
   - **Residual implausible language labels are dominated by `ja`** (229 of the
     Mac's 350). 0.24% of labels; the confidence-floor lever was measured
     useless. If ever chased, **sample the `ja` rows first**.
   - **The DGX drops remain uninvestigated and unexplained** (risk 3).
   - **#269's suggestion 2** (blob-temp sweep off the critical path) — deferred
     in session 19; reopen only if cold-cache startups grow past tolerable.

## Open decisions & risks

1. **One PR is open, green, yours to merge.** `main` is `76fef01`. **#289**
   (`fix/279-cli-version-option` @ `8b752ef`) closes #279. **13 open issues**,
   → 12 once it merges. **Dependabot: 0 open alerts** (session 22's two cleared
   when #288 merged).
2. **Test-count baselines: measure, don't subtract** *(new)*. The last handoff
   recorded `fix/280…` at **2358** and that number never matched `main`: #286's
   own review follow-ups added 8 more tests after the count was taken, so the
   merged branch is **2366**. Measured across the merge run:
   `57ce228` → **2343**, `bc5b556` (#286) → **2366**, `7842603` → 2366,
   `76fef01` → 2366, this branch → **2369**. Cheap to check without a DB:
   `uv run pytest --collect-only -q | tail -2`. This matters because risk 17
   uses the count as a tripwire — a stale expected value makes a real
   regression look like the known number.
3. **`search-status` is fast on BOTH hosts — stop budgeting minutes for it**
   *(carried, now two-host)*. Mac **1.30 s**, DGX **0.756 s**. If it ever runs
   long again that is a **regression** of #280, not the known cost: check
   `EXPLAIN (FORMAT JSON)` for a `Seq Scan on messages` under a `SubPlan`
   before looking anywhere else.
4. **The DGX drops are STILL UNEXPLAINED — five theories refuted, four of them
   mine** *(carried; not investigated this session)*. **Do not propose a sixth
   without a captured outage in which the host was demonstrably up throughout.**
   Triage with `journalctl --list-boots` first. **Power is not a candidate**
   (~5-day UPS). **Do not edit `/etc/wireguard/wg0.conf`.** `10.0.0.3`
   (WireGuard) worked first try again this session, including a multi-minute
   `uv sync` over SSH. Session 19 established that the LAN address answers ping
   and refuses SSH, so a green `lan=` probe line is *not* evidence it is the
   DGX. **Try `10.0.0.3` first.**
5. **A single `tunnel=FAIL` probe sample is not an outage** *(carried)* —
   Starlink losing three packets on a ~900 ms path. Sustained = several
   consecutive samples.
6. **When reverting a mutation, restore from a file copy — never `git
   checkout`** *(new)*. An uncommitted fix lives only in the working tree, so
   `git checkout <file>` silently discards it and the next "mutation" runs
   against no fix at all, producing a convincing but meaningless failure set.
   The tell was `Updated 0 paths from the index`. Mutation testing is only
   evidence if you verify what you restored.
7. **`blobs_claimable 0` alongside `blobs_pending 0` is the SETTLED archive
   shape, on both hosts** *(carried, extended)*. Session 21 predicted "well
   above 0" and was wrong: the non-allowlisted remainder was claimed and
   disposed of with `type-skipped` rows long ago, so it is not claimable. The
   #216 shape (`pending 0` alongside a large `claimable`) is what a *fresh*
   image-heavy archive looks like, not a settled one.
8. **A steady non-zero `blobs_no_text` is NORMAL** *(carried — #277)*. Those
   blobs are finished, just with nothing to index; the bucket is terminal by
   design. Read it like `body_lang_declined`. **`blobs_gave_up` is the one to
   act on** — `list-failed-extractions` says why (poison-pill half only),
   `retry-failed-extractions` re-queues.
9. **`QueueCounts.__post_init__` raises on two distinct conditions** *(carried
   — #284)*. `misfiled` is checked **before** the sum, deliberately. If you add
   a fifth disposition, add it to `BUCKET_WHERE_SQL` — everything else derives.
   Do not relax either check. And **`DISTINCT` in `EXTENSION_MATCH_JOIN_SQL` is
   load-bearing with no runtime guard** — its only symptom is `pending`
   diverging from `claimable`.
10. **`--version` must stay config- and DB-free** *(new — #279)*. The moment an
    operator most needs a version is the moment those lookups fail, which is
    why nothing config- or DB-derived (a DSN, the applied migration revision)
    belongs in that output. Pinned, with `list-accounts` as the negative
    control. And **do not "simplify" to a bare `@click.version_option()`** — it
    reintroduces a second, independent metadata reader that diverges from
    `__version__` on an uninstalled tree. Both value tests pass under that
    change; only the source pin catches it.
11. **`type-skipped` is a one-way door for a widened allowlist** *(carried)*.
    Clear deliberately: `DELETE FROM attachment_text WHERE extractor =
    'type-skipped'`. **#266's whitespace-heal is a one-way door too** — what
    makes it safe is the `is_blank` gate, not the nature of the data.
12. **`--relabel` is the only destructive verb in the lang path** *(carried)*.
    Discards **every** label; archive unsearchable by `lang:` until the drain
    completes. Prompts unless `--yes`. Reach for `--retry-declined` first.
    Budget ~45 min for a 100k-row archive, and note that **`reopen_all`'s bulk
    UPDATE shows no progress in `pg_stat_activity` until it commits** — tens of
    minutes of apparent hang is expected; do **not** cancel it.
13. **`body_lang_pending` means claimable work only** *(carried)*; the
    turned-away remainder is `body_lang_declined`. A steady non-zero `declined`
    is **normal** (12,182 on the Mac, 1,973 on the DGX).
14. **CI trap** *(carried)*: any GUI admin panel that fetches on mount MUST be
    stubbed in **both** `AdminView.test.ts` and `MainView.test.ts`, or an
    unhandled promise rejection leaks while vitest still reports "passed".
15. **Do not add normalisation steps to `lang_text.py` without a measurement**
    *(carried)*. Every candidate step beyond URL-stripping measured zero.
16. **Do not run the test suite while a backfill is draining** *(carried)*.
    Shared-cluster contention produces dozens of false failures.
    `search-status` does not qualify (risk 3).
17. **The macOS socket deselect is GONE — stop using it** *(carried)*. #276
    fixed it; `uv run pytest -q` with **no arguments** is the right command.
18. **The stale NOTIFY queue read CLEAN all session** *(carried; unchanged this
    session, but session 22 saw it recur mid-session having been clean at
    start — treat "clear at the last handoff" as worth nothing)*. Fix is the
    runbook's Option A: `launchctl bootout gui/$UID/com.localmail.daemon`,
    **wait until `launchctl print` says the service is gone**, verify both
    gates, then `bootstrap` back. **Verify both gates, not one:** session 22
    saw `pg_notification_queue_usage()` read `9.5e-07` — healthy at a glance —
    while `LISTEN daemon_commands` errored outright; session 19 saw the
    inverse. Neither reading alone is the gate.
19. **An empty `daemon_heartbeats` right after a daemon restart is normal for
    minutes** *(carried — #269/#271)*. It is the startup blob-temp sweep. Grep
    for **`blob-temp sweep done: walked=`**.
20. **`uv sync` without extras silently downgrades a host** *(carried; it bit
    this Mac in session 22)*. Use `--all-extras` on the Mac and `--extra mcp
    --extra extraction` on the DGX. **`uv` is not on the DGX's default
    non-interactive PATH** — use `~/.local/bin/uv` over SSH. **A non-zero
    `skipped` count means an extra went missing** — check it rather than
    ignoring it. Verified present this session: `import docling` → OK, and the
    suite ran **0 skipped**. CI installs only `--extra mcp`, so its count
    differs by design.
21. **`ExtractorConfigurationError` subclasses `TransientExtractorError`, and
    that subclassing *is* the #248 fix — do not "clean it up"** *(carried)*.
22. **`run_embed_worker_once`'s `failure_log` defaults to the process-wide
    `embed_worker._FAILURE_LOG`, and that default is the fix** *(carried —
    #267)*. **A new looping caller should pass nothing.**
23. **No ROADMAP.md** *(carried)* — the `/nextsession` ROADMAP step is a no-op;
    slice status lives here + `docs/handoffs/` + the specs. **README WAS
    updated this session** (a global-options note under `## CLI`), unlike the
    last two sessions.
24. **Secrets/ACL invariants unchanged** *(carried)*: `secrets.configure`'s pin
    kept though #245 is fixed; #246 warns on group-write; `InsecureSecretsFile`
    refuses; #239's manual tombstone retention deliberate; admin bearer tokens
    have no per-token scope (#204).
25. **Run vitest from `gui/`, not the repo root** *(carried)*.
26. **`cargo clippy --all-targets` is clean but ungated** *(carried)* — CI runs
    clippy without `--all-targets`, so `#[cfg(test)]` modules are never linted.

## Exact commands to resume

```bash
cd /Users/hherb/src/localmail
git fetch --prune origin                 # ALWAYS first
git status                               # expect clean
git branch --show-current
git log --oneline origin/main..main      # expect 0

# ONE PR is OPEN and GREEN, awaiting your merge (What's next, 0).
gh pr list
gh pr checks 289
gh issue list --limit 20                 # 13 open; 12 once #289 merges

# Dependabot: 0 open. A non-zero count right after a lockfile merge is scan
# lag — check uv.lock before chasing it (risk 1).
gh api repos/hherb/localmail/dependabot/alerts \
  --jq '[.[] | select(.state=="open")] | length'

# AFTER MERGING #289:
#   Mac:  git pull && uv sync --all-extras && uv run localmail --version
#   DGX:  ssh 10.0.0.3 'cd ~/src/localmail && git pull && ~/.local/bin/uv sync --extra mcp --extra extraction && ~/.local/bin/uv run localmail --version'
#   expect on both: localmail, version 0.3.0
# Optional, NOT urgent (see What's next 0):
#   launchctl kickstart -k gui/$UID/com.localmail.daemon

# Python test suite. No --deselect (risk 17).
# Do NOT run while a backfill is draining (risk 16).
unset VIRTUAL_ENV && uv sync --all-extras   # NOT a bare `uv sync` — risk 20
unset VIRTUAL_ENV && uv run pytest -q
#   expect: 2369 passed, 0 SKIPPED on fix/279…; 2366 on main.
#   MEASURE, don't subtract (risk 2):
unset VIRTUAL_ENV && uv run pytest --collect-only -q | tail -2   # no DB needed

unset VIRTUAL_ENV && uv run mypy src/localmail
#   expect: Success, 140 source files

# Host health checks:
launchctl print gui/$UID/com.localmail.daemon | grep -E '^\s*(state|pid) '
psql -h localhost -p 5532 -U localmail -d localmail -c \
  "SELECT worker_kind, account_id, state, now()-last_heartbeat_at AS age
     FROM daemon_heartbeats ORDER BY worker_kind, account_id"
#   expect: 7 rows, ages under ~60 s (EMPTY during the startup sweep — risk 19)

# The attachment counters — ABOUT ONE SECOND on both hosts (risk 3):
unset VIRTUAL_ENV && uv run localmail search-status
#   Mac  expect: blobs_eligible 9493 = 9205 + 106 + 182 + 0, claimable 0
#   DGX  expect: blobs_eligible 4363 = 4145 +  91 + 127 + 0, claimable 0
# If it takes minutes again that is a REGRESSION of #280:
psql -h localhost -p 5532 -U localmail -d localmail -c \
  "SELECT now()-query_start, left(query,60) FROM pg_stat_activity
    WHERE datname='localmail' AND state='active' AND pid <> pg_backend_pid()"

# NOTIFY gates — CLEAR as of this handoff; check BOTH only if those 3 tests fail:
psql -h localhost -p 5532 -d postgres -U localmail -tAc 'SELECT pg_notification_queue_usage()'  # 0
psql -h localhost -p 5532 -d localmail_test -U localmail -c 'LISTEN daemon_commands'            # LISTEN

# The DGX — deployed this session, now at 76fef01. Use the WireGuard address;
# uv is not on its non-interactive PATH (risks 4, 20):
ssh 10.0.0.3 'systemctl --user is-active localmail-daemon localmail-serve localmail-wgprobe'
ssh 10.0.0.3 'cd ~/src/localmail && git log --oneline -1'   # 76fef01
tail -5 ~/localmail-probe/tunnel-probe.log   # expect lan=ok(3/3)@<addr>

# Frontend — only if you touch gui/ (MUST run from gui/ — risk 25):
cd gui && npm run check && npm test && npm run build && cd ..
cd gui/src-tauri && cargo test && cargo clippy --locked -- -D warnings \
  && cargo clippy --all-targets -- -D warnings && cd ../..
```

`main` tip is **`76fef01`** (PR #288). This session's work is **`8b752ef`** on
`fix/279-cli-version-option`, **open as PR #289, CI green, not merged**. Latest
migration **`0035_messages_body_lang_attempted_at.sql`**; next free slot
`0036_*.sql` (this session adds none). **Open issues: 13** (12 once #289
closes #279). **Dependabot: 0 open alerts.** Both hosts run `76fef01`; the DGX
was restarted onto it, the Mac daemon process still predates it and
deliberately was not restarted (behaviour identical — see What's next 0).
