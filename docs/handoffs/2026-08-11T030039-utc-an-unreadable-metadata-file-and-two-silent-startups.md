# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-08-11 (session 25).** Session 24's PR #293 was **merged by
> the operator** before this one started, so `main` moved `8d31045` →
> **`29f5fae`** and #291 closed.
>
> This session closed **both** issues session 24's review filed — **#295** and
> **#296** — in one PR. **PR #297** (`42920f6`), **CI green (3m10s), not
> merged**.
>
> **#296 was a real, reproducible outage of every entry point**, not a
> theoretical hole. A `METADATA` byte that is not valid UTF-8 killed `import
> localmail` outright — CLI, `serve`, daemon, MCP, and `--version` itself, the
> one command whose purpose is diagnosing a broken install. Reproduced
> end-to-end before the fix and re-run after it; both transcripts are in §1.
>
> **The last handoff asserted a host revision it had not checked, and it was
> wrong.** It said "Both hosts run `8d31045`"; the DGX was at **`76fef01`**,
> four commits behind. Deployed to `29f5fae` this session. See risk 2 — this is
> the second consecutive handoff to carry a number nobody measured.
>
> **One new trap, and it is a good one:** writing the words `# noqa` *in a
> comment* creates a `# noqa` directive. Ruff scans comment text, so explaining
> why a suppression was **not** added emitted `warning: Invalid # noqa
> directive`. That is session 24's risk 3 — prose and a source-text rule in
> conflict — in a place nobody had looked. See risk 4.

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

### 1. PR #297 → commit `42920f6` — #296 and #295, one diff

Branch `fix/295-296-version-diagnostic-reach`. **Open, CI green (3m10s),
unmerged.** No migration, no new dependency, no config change.

Landed together because they are coupled, not out of convenience: #296 adds a
*cause* to the diagnostic, #295 gives that diagnostic *two more readers*, and the
thing that joins them — rendering the line once, in `__init__.py` — is required
by both. A split would have been cosmetic.

**#296 — the reproduction, before the fix.** A `localmail-9.9.9.dist-info/METADATA`
containing one latin-1 byte, placed ahead on `sys.path`:

```
$ PYTHONPATH=repro uv run python -c "import localmail"
UnicodeDecodeError: 'utf-8' codec can't decode byte 0xe9 in position 65
$ PYTHONPATH=repro uv run localmail --version
UnicodeDecodeError: 'utf-8' codec can't decode byte 0xe9 in position 65
```

`resolve_version` guarded only `PackageNotFoundError`, but
`importlib.metadata.version` reads METADATA as UTF-8 through a `suppress(...)`
list covering neither `UnicodeDecodeError` nor a generic `OSError`. **After**, on
the same tree — stdout unchanged, stderr carrying cause *and* remedy, exit 0:

```
localmail, version 0.0.0+unknown
warning: the localmail version could not be determined — reading its
distribution metadata failed outright, so the file is corrupt or the filesystem
holding it is faulty.
  remedy: check the filesystem under site-packages first — a reinstall cannot
  fix a failing mount — then run `uv sync --reinstall-package localmail` …
  cause: UnicodeDecodeError
```

**#295 — `serve` and `run` were silent.** `__version_source__` had exactly one
reader (`cli.py`), so on a headless host `/v1/version` answered `0.0.0+unknown`
as if it were a version, the GUI rendered it, and the server log said nothing.
Verified end-to-end on the corrupt tree: `create_app` now emits
`WARNING localmail.serve: warning: the localmail version could not be
determined … cause: UnicodeDecodeError` before it does anything else.

**Six deliberate calls a future session would plausibly undo — don't:**

- **The broad `except Exception` reports what it caught.** `type(exc).__name__`
  travels on `ResolvedVersion.detail` and renders as a `cause:` line. A discarded
  exception here would be a silent catch — #291 wearing a third hat. Type name,
  not `str(exc)`, per `failure_pacing.py`'s precedent.
- **`PackageNotFoundError` stays *ahead* of the broad catch.** It is a
  `ModuleNotFoundError` subclass, so reordering silently reclassifies every
  uninstalled tree as a corrupt one and sends the operator to `fsck` instead of
  `uv sync`.
- **`Exception`, never `BaseException`** — a Ctrl-C during a slow read on a hung
  mount must interrupt the process, not be reported as a damaged install.
- **`METADATA_UNREADABLE` is a third cause, not a second spelling of
  `METADATA_INCOMPLETE`.** No reinstall repairs a failing mount, so the remedy
  sends the operator to the filesystem *first*. That word is asserted.
- **Both startup calls run before the gate they precede** — the daemon before
  `retry_with_backoff` waits on Postgres (unbounded, and a host broken enough to
  lose its version may well have a DB down too), `create_app` before the
  `state_signing_key` check raises. **Pinned by two tests, not just commented**
  (see risk 3).
- **`/v1/version` gained no field.** Your call this session. The GUI's connect
  probe decodes `server_version` as a non-optional String — which is *why* the
  sentinel exists rather than a null — and a new key nothing renders is #278 from
  the other end. Reversible; removing a shipped wire key is not.

**Shape:** `__init__.py` renders `__version_diagnostic__` **once** and exports
it, rather than each reader composing it. The exception type is known only at
resolution time, so a reader handed just `__version_source__` drops it silently —
and there are three readers now. `unknown_version_diagnostic`'s `detail` is
keyword-only with **no default** (#234's shape); with exactly one production call
site that is free rather than noisy.

**14 mutations run, 14 caught.** Every target restored from a **file copy** and
md5-verified (never `git checkout` — session 23's trap); empty pytest output
treated as a failed mutation, not a pass (session 24's). Harnesses kept at
`scratchpad/mutate.py` + `mutate_order.py`.

| mutation | caught by |
|---|---|
| drop the broad catch (the #296 defect) | 3 |
| catch `Exception` *before* `PackageNotFoundError` | 1 |
| widen to `BaseException` | 2 |
| discard the swallowed exception's type | 3 |
| render the remedy but ignore the detail | 2 |
| give the unreadable cause the damaged-install remedy | 1 |
| log at INFO instead of WARNING | 3 |
| log unconditionally (drop the healthy guard) | 3 |
| daemon stops reporting (#295, daemon half) | 1 |
| serve stops reporting (#295, serve half) | 1 |
| package renders without the detail | 1 |
| CLI stops printing the diagnostic | 3 |
| **daemon reports AFTER the Postgres wait** | 1 |
| **serve reports AFTER the config check** | 1 |

The last two exist because of session 24's lesson 4b: I had written the ordering
claim into a comment, and a comment is not a pin. Both mutations were **written
before** the tests that catch them.

**One assertion I wrote was vacuous, found on self-review.** `assert "cause:" not
in rendered` would have survived renaming the prefix — an absence assertion
against a literal cannot fail once the literal stops appearing anywhere. It
asserts against the module's own `_CAUSE_PREFIX` now, with a positive control
beside it proving the check can fire. Same family as session 24's `rpartition`
finding.

### 2. Verification (this Mac, all extras)

- `unset VIRTUAL_ENV && uv run pytest -q` → **2405 passed, 0 skipped, 0 failed**
  in 165 s. `main` measures **2388**. +17 is **exactly** the number of tests
  added (7 + 2 + 8), checked against `--collect-only`, not inferred.
- `unset VIRTUAL_ENV && uv run mypy src/localmail` → **Success, 141 source
  files**.
- `ruff check` clean on all 8 changed files. Repo-wide `src/localmail/` still
  **10** pre-existing errors (#285) — no new ones, and **no new `noqa`
  directive** was added (see risk 4).
- The 4 pytest warnings are pre-existing (websockets #25 + `psycopg_pool`
  teardown `ResourceWarning`s); the count varies with teardown timing.

### 3. The DGX is deployed — `76fef01` → `29f5fae` (four commits, not one)

`git pull`, `~/.local/bin/uv sync --extra mcp --extra extraction` (moved
`nvidia-cusparselt-cu13` only), `localmail init-db` → **"schema already up to
date"**, `systemctl --user restart localmail-daemon localmail-serve`. Both units
`active`; **5 fresh heartbeat rows** (idle:1 / poll:1 / embed / extract /
reconcile), all under 30 s. `localmail --version` → `localmail, version 0.3.0`,
exit 0, **empty stderr**.

```
messages_total 37531
blobs_eligible 4365 = extracted 4147 + no_text 91 + gave_up 127 + pending 0
blobs_claimable 0        body_lang_pending 0, declined 1974
```

### 4. Host health confirmed, not assumed (Mac)

launchd daemon `running` (pid 5669); **7 fresh heartbeats**. `search-status`
**1.337 s**:

```
messages_total 127604
blobs_eligible 9495 = extracted 9207 + no_text 106 + gave_up 182 + pending 0
blobs_claimable 0        body_lang_pending 0, declined 12185
```

Partition sums on both hosts, `claimable` agrees with `pending` on both.
**Dependabot: 0 open alerts.**

### 5. Docs

- **README** — the unknown-version paragraph now describes **three** causes and
  their differing remedies, plus a new paragraph saying `serve` and `run` log the
  same warning at startup and that `/v1/version` is unchanged.
- **CLAUDE.md** — the `version_report` section gained the #296 and #295 blocks
  (including the `PackageNotFoundError`-ordering hazard, the no-`BLE001`-directive
  decision, and the "rendered once" rationale); the Commands block and layout
  tree updated. The "two known gaps, filed not fixed" bullet is **gone** — it was
  describing this session's work.
- **No ROADMAP.md** (confirmed absent again; that `/nextsession` step stays a
  no-op).

## What's next

### 0. **Merge PR #297** — the only open PR
   Green and unmerged; **the operator merges** (project convention). Closes
   **#295** and **#296**, taking open issues **14 → 12**.
   - **Acceptance:** on both hosts, `uv run localmail --version` prints
     `localmail, version 0.3.0`, exit 0, **empty stderr**; and a `serve`/daemon
     restart logs **nothing** about the version. The diagnostic path is
     unreachable on a healthy host by design — **do not try to provoke it there**;
     it is covered by 17 tests and by the two real transcripts in §1.
   - The DGX is already at `29f5fae`; after the merge it is one commit behind
     again (recipe in the resume block). The Mac needs only `git pull && uv sync
     --all-extras`.

### 1. **#278 — the version surface's other half** *(carried; needs YOUR decision first)*
   Now the last unclosed member of the version cluster (#279, #291, #295, #296 all
   done). The GUI About tab renders a `build_hash` that `/v1/version` has **never**
   emitted, so "Server build" always shows `?`, while five test files mock the
   field and make it look covered. **Two options, product call before any code:**
   emit a real build hash (from git, at build time — but the version is stamped at
   *install* time, so this needs a story for `uv tool install` from a tarball), or
   delete the field end-to-end including the five mocks. Deleting is cheaper and
   honest; emitting is more useful where `--version` alone cannot distinguish two
   builds of `0.3.0`. **Note this session's precedent:** #295's wire question was
   decided *against* adding a field, citing #278 as the cautionary case.

### 2. **#285 — ruff, repo-wide** *(carried, and this session touched its edges)*
   Every `# noqa: S608` in the tree is a dead directive — still **9** across 5
   files, unchanged. `ruff check src/localmail/` reports **10** pre-existing
   errors; there is no `[tool.ruff]` config and no CI step. This session
   **deliberately declined to add a tenth directive** (a `BLE001` suppression on
   the new broad catch) on exactly this reasoning, and discovered risk 4 doing so.
   Two separable decisions — adopt ruff properly with a real rule set, or drop the
   directives and keep the reasoning as plain comments — worth deciding once.

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
   `AdminView.test.ts` and `MainView.test.ts`** (see risk 13).

### 4. **Remaining robustness backlog** *(carried)*
   **#218** (GUI download commands buffer the full body before enforcing the size
   ceiling) · **#226** (self-signed cert misses the reachable IP when `--bind
   0.0.0.0`) · **#225 / #227** (`/v1/changes` subscription lifecycle gaps) ·
   **#200 / #211 / #208** (admin panels silently swallow 4xx) · **#206** (GUI
   AccountForm: folder filters not editable) · **#204** (admin bearer-token
   scope) · **#25** (websockets DeprecationWarning).

### 5. **Smaller, deliberately not done** *(carried)*
   - **`cli.py` is 1972 lines** (+5 this session), **`daemon.py` 580** (+7) —
     measured against `main`'s 1967 / 573, not inferred. Both well over the
     500-line guideline; the refactor session 21 deferred is still owed in full.
     I first wrote "−4" here on the assumption that replacing a call with an
     attribute read shortened the file; it did not, because the docstring gained
     a paragraph. Risk 2, in miniature, inside the handoff about risk 2.
   - **165 docling failures on the Mac**, of which 31 are `File format not
     allowed` and 134 are `Input document ... is not valid` (of 182
     `blobs_gave_up`), and **127** on the DGX.
   - **Residual implausible language labels are dominated by `ja`** (229 of the
     Mac's 350). 0.24% of labels; the confidence-floor lever was measured useless.
     If ever chased, **sample the `ja` rows first**.
   - **The DGX drops remain uninvestigated and unexplained** (risk 5).
   - **#269's suggestion 2** (blob-temp sweep off the critical path) — deferred in
     session 19; reopen only if cold-cache startups grow past tolerable.

## Open decisions & risks

1. **One PR is open and yours to merge.** `main` is `29f5fae`; **#297**
   (`fix/295-296-version-diagnostic-reach`, one commit `42920f6`) closes #295 and
   #296. **14 open issues** → 12 once it merges. **Dependabot: 0 open alerts.**
2. **Verify host revisions; do not infer them** *(new)*. Session 24's handoff
   stated "Both hosts run `8d31045`" — the DGX was at `76fef01`, four commits
   behind, because that session planned the deploy and the plan became the claim.
   One `ssh 10.0.0.3 'cd ~/src/localmail && git log --oneline -1'` settles it, and
   it is in the resume block for that reason. Same family as risk 6 (measure test
   counts, don't subtract): **the cheap check is cheaper than the wrong number.**
3. **A claim written in a comment is not pinned** *(new, and the direct successor
   to session 24's 4b)*. Both startup reports must run *before* a blocking gate,
   and I wrote that into two comments before noticing nothing tested it — the
   diagnostic could have moved after the Postgres wait or after the config raise
   with the suite fully green. Two mutations and two tests now cover it. **When a
   comment says "before X, deliberately", that sentence is a test that has not
   been written yet.**
4. **Writing `# noqa` in a comment CREATES a `# noqa` directive** *(new)*. Ruff
   scans comment text for the token, so a comment explaining why a `BLE001`
   suppression was deliberately *not* added produced `warning: Invalid # noqa
   directive on version_report.py:194`. Reworded to name the rule without the
   token, and the comment now says why it cannot be spelled out. This is session
   24's risk 3 — prose in conflict with a source-text rule — in a new place;
   assume it generalises to any tool that parses comments (`type: ignore`,
   `pragma: no cover`, `fmt: off`).
5. **The DGX drops are STILL UNEXPLAINED — five theories refuted** *(carried; not
   investigated this session)*. **Do not propose a sixth without a captured
   outage in which the host was demonstrably up throughout.** Triage with
   `journalctl --list-boots` first. **Power is not a candidate** (~5-day UPS).
   **Do not edit `/etc/wireguard/wg0.conf`.** `10.0.0.3` worked first try again
   this session, including a multi-minute `uv sync` over SSH. Session 19
   established that the LAN address answers ping and refuses SSH, so a green
   `lan=` probe line is *not* evidence it is the DGX. **Try `10.0.0.3` first.** A
   single `tunnel=FAIL` probe sample is not an outage.
6. **Test-count baselines: measure, don't subtract** *(carried)*. `main` →
   **2388**, this branch → **2405**. Cheap to check without a DB:
   `uv run pytest --collect-only -q | tail -2`.
7. **`--version`'s contract is now five things, all pinned** *(supersedes session
   24's risk 2)*. It must (a) read no config and touch no database; (b) keep
   **stdout** to the single machine-readable line; (c) put any diagnostic on
   **stderr**; (d) **exit 0** even when the version is unknown; and (e) **still
   work on a tree whose METADATA cannot be read at all** — that is #296, and it is
   the case where the flag matters most. **Do not reintroduce
   `@click.version_option` in any spelling** — the AST pin forbids it.
8. **`search-status` is fast on BOTH hosts — stop budgeting minutes** *(carried)*.
   Mac **1.337 s** this session, DGX **sub-second**. If it ever runs long that is
   a **regression** of #280: check `EXPLAIN (FORMAT JSON)` for a `Seq Scan on
   messages` under a `SubPlan` before looking anywhere else.
9. **When reverting a mutation, restore from a file copy — never `git checkout`**
   *(carried, sessions 23–24)*. An uncommitted fix lives only in the working tree.
   This session's harnesses `cp` + md5-verify after every revert and treat empty
   pytest output as a failed mutation, not a pass.
10. **An absence assertion needs the constant, not a literal** *(new)*.
    `assert "cause:" not in rendered` cannot fail once the prefix is renamed —
    it asserts the absence of a string that appears nowhere. Assert against the
    module's own constant and put a positive control beside it. Generalises to
    every `not in` / `assertNotIn` over a spelling the code owns.
11. **`blobs_claimable 0` alongside `blobs_pending 0` is the SETTLED archive
    shape, on both hosts** *(carried)*. **A steady non-zero `blobs_no_text` is
    NORMAL** (#277) — terminal by design, read it like `body_lang_declined`.
    **`blobs_gave_up` is the one to act on** — `list-failed-extractions` says why
    (poison-pill half only), `retry-failed-extractions` re-queues.
    **`QueueCounts.__post_init__` raises on two distinct conditions** (#284):
    `misfiled` is checked **before** the sum, deliberately. A fifth disposition
    goes in `BUCKET_WHERE_SQL`; everything else derives. **`DISTINCT` in
    `EXTENSION_MATCH_JOIN_SQL` is load-bearing with no runtime guard** — its only
    symptom is `pending` diverging from `claimable`.
12. **`type-skipped` is a one-way door for a widened allowlist** *(carried)*.
    Clear deliberately: `DELETE FROM attachment_text WHERE extractor =
    'type-skipped'`. **#266's whitespace-heal is a one-way door too** — what makes
    it safe is the `is_blank` gate, not the nature of the data.
13. **CI trap** *(carried)*: any GUI admin panel that fetches on mount MUST be
    stubbed in **both** `AdminView.test.ts` and `MainView.test.ts`, or an
    unhandled promise rejection leaks while vitest still reports "passed".
14. **`--relabel` is the only destructive verb in the lang path** *(carried)*.
    Discards **every** label; archive unsearchable by `lang:` until the drain
    completes. Prompts unless `--yes`. Reach for `--retry-declined` first. Budget
    ~45 min for a 100k-row archive, and note that **`reopen_all`'s bulk UPDATE
    shows no progress in `pg_stat_activity` until it commits** — tens of minutes
    of apparent hang is expected; do **not** cancel it. `body_lang_pending` means
    claimable work only; a steady non-zero `declined` is **normal** (12,185 Mac,
    1,974 DGX). **Do not add normalisation steps to `lang_text.py` without a
    measurement** — every candidate beyond URL-stripping measured zero.
15. **Do not run the test suite while a backfill is draining** *(carried)*.
    Shared-cluster contention produces dozens of false failures. `search-status`
    does not qualify (risk 8).
16. **The macOS socket deselect is GONE — stop using it** *(carried)*. #276 fixed
    it; `uv run pytest -q` with **no arguments** is the right command.
17. **An empty `daemon_heartbeats` right after a daemon restart is normal for
    minutes** *(carried — #269/#271)*. It is the startup blob-temp sweep. Grep for
    **`blob-temp sweep done: walked=`**.
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
    (the #248 OCR-engine pin). Both hosts ran **0 skipped** this session.
20. **`ExtractorConfigurationError` subclasses `TransientExtractorError`, and that
    subclassing *is* the #248 fix — do not "clean it up"** *(carried)*.
21. **`run_embed_worker_once`'s `failure_log` defaults to the process-wide
    `embed_worker._FAILURE_LOG`, and that default is the fix** *(carried — #267)*.
    **A new looping caller should pass nothing.**
22. **No ROADMAP.md** *(carried, re-confirmed)* — the `/nextsession` ROADMAP step
    is a no-op; slice status lives here + `docs/handoffs/` + the specs. **README
    WAS updated this session.**
23. **Secrets/ACL invariants unchanged** *(carried)*: `secrets.configure`'s pin
    kept though #245 is fixed; #246 warns on group-write; `InsecureSecretsFile`
    refuses; #239's manual tombstone retention deliberate; admin bearer tokens
    have no per-token scope (#204).
24. **Run vitest from `gui/`, not the repo root** *(carried)*. **`cargo clippy
    --all-targets` is clean but ungated** — CI runs clippy without
    `--all-targets`, so `#[cfg(test)]` modules are never linted.
25. **Do not "tidy up" `_PRE280_CORRELATED_ALLOWLIST_SQL`** *(carried; #292 closed
    in session 24 confirming the note is durable)*.
    [tests/test_extract_queue_sql.py:306](tests/test_extract_queue_sql.py#L306)
    holds the pre-#280 correlated predicate **on purpose**, as the negative
    control that proves the plan assertions can fail. It is consumed by two named
    tests, so deleting it is a `NameError`, not a silent loss.

## Exact commands to resume

```bash
cd /Users/hherb/src/localmail
git fetch --prune origin                 # ALWAYS first
git status                               # expect clean
git branch --show-current
git log --oneline origin/main..main      # expect 0
git log --oneline main..origin/main      # expect 0 — NON-empty means a session
                                         #   landed after this handoff was written

# ONE PR is open, awaiting your merge (What's next, 0).
gh pr list
gh pr checks 297
gh issue list --limit 20                 # 14 open; 12 once #297 merges

gh api repos/hherb/localmail/dependabot/alerts \
  --jq '[.[] | select(.state=="open")] | length'          # expect 0

# AFTER MERGING #297:
#   Mac:  git checkout main && git pull && unset VIRTUAL_ENV && uv sync --all-extras
#   DGX:  ssh 10.0.0.3 'cd ~/src/localmail && git pull && ~/.local/bin/uv sync --extra mcp --extra extraction && ~/.local/bin/uv run localmail init-db && systemctl --user restart localmail-daemon localmail-serve'
# Acceptance on BOTH hosts — version line, exit 0, and NOTHING on stderr:
unset VIRTUAL_ENV && uv run localmail --version 2>/tmp/v.err; echo "exit=$?"; wc -c < /tmp/v.err   # expect 0

# Python test suite. No --deselect (risk 16).
# Do NOT run while a backfill is draining (risk 15).
unset VIRTUAL_ENV && uv sync --all-extras   # NOT a bare `uv sync` — risk 19
unset VIRTUAL_ENV && uv run pytest -q
#   expect: 2405 passed, 0 SKIPPED on fix/295-296…; 2388 on main.
#   MEASURE, don't subtract (risk 6):
unset VIRTUAL_ENV && uv run pytest --collect-only -q | tail -2   # no DB needed

unset VIRTUAL_ENV && uv run mypy src/localmail
#   expect: Success, 141 source files

# Host health checks (Mac):
launchctl print gui/$UID/com.localmail.daemon | grep -E '^\s*(state|pid) '
psql -h localhost -p 5532 -U localmail -d localmail -c \
  "SELECT worker_kind, account_id, state, now()-last_heartbeat_at AS age
     FROM daemon_heartbeats ORDER BY worker_kind, account_id"
#   expect: 7 rows, ages under ~60 s (EMPTY during the startup sweep — risk 17)

# The attachment counters — UNDER ~1.5 s on both hosts (risk 8):
unset VIRTUAL_ENV && uv run localmail search-status
#   These MOVE as the archive grows, so check the SHAPE, not the literals: the
#   four buckets must sum to blobs_eligible, and claimable must equal pending.
#   Mac  approx: blobs_eligible ~9.5k = ~9.2k + 106 + 182 + 0, claimable 0
#   DGX  approx: blobs_eligible ~4.4k = ~4.1k +  91 + 127 + 0, claimable 0
# If it takes minutes again that is a REGRESSION of #280:
psql -h localhost -p 5532 -U localmail -d localmail -c \
  "SELECT now()-query_start, left(query,60) FROM pg_stat_activity
    WHERE datname='localmail' AND state='active' AND pid <> pg_backend_pid()"

# NOTIFY gates — CLEAR as of this handoff; check BOTH only if those 3 tests fail:
psql -h localhost -p 5532 -d postgres -U localmail -tAc 'SELECT pg_notification_queue_usage()'  # 0
psql -h localhost -p 5532 -d localmail_test -U localmail -c 'LISTEN daemon_commands'            # LISTEN

# The DGX — deployed this session, now at 29f5fae. VERIFY, don't assume (risk 2).
# Use the WireGuard address; uv is not on its non-interactive PATH (risks 5, 19):
ssh 10.0.0.3 'systemctl --user is-active localmail-daemon localmail-serve localmail-wgprobe'
ssh 10.0.0.3 'cd ~/src/localmail && git log --oneline -1'   # expect 29f5fae
# Its Postgres is on 5532 too, but needs PGPASSWORD (it is not in ~/.pgpass):
ssh 10.0.0.3 'PGPASSWORD="local@@mail" psql -h 127.0.0.1 -p 5532 -U localmail -d localmail \
  -tAc "SELECT worker_kind, state, round(extract(epoch from now()-last_heartbeat_at)) FROM daemon_heartbeats"'
#   expect 5 rows (one account), ages under ~60 s
tail -5 ~/localmail-probe/tunnel-probe.log   # expect lan=ok(3/3)@<addr>

# Frontend — only if you touch gui/ (MUST run from gui/ — risk 24):
cd gui && npm run check && npm test && npm run build && cd ..
cd gui/src-tauri && cargo test && cargo clippy --locked -- -D warnings \
  && cargo clippy --all-targets -- -D warnings && cd ../..
```

`main` tip is **`29f5fae`** (PR #294). This session's work is **`42920f6`** on
`fix/295-296-version-diagnostic-reach`, **open as PR #297, CI green (3m10s), not
merged**. Latest migration **`0035_messages_body_lang_attempted_at.sql`**; next
free slot `0036_*.sql` (this session adds none). **Open issues: 14** (12 once
#297 closes #295 and #296). **Dependabot: 0 open alerts.** The DGX runs
`29f5fae` — **verified, not assumed**; the Mac's checkout is on the branch and
its daemon process still predates #286, which remains harmless (#280 left
`_claim_batch` untouched).
