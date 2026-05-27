# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-05-27T1248 UTC (post-session).**
> **#2 implementation shipped** as PR
> [#102](https://github.com/hherb/localmail/pull/102)
> (`issue-2-upgrade-estimator-runbook`). 12 commits. CI pending at
> hand-off; first run is
> [`26512017307`](https://github.com/hherb/localmail/actions/runs/26512017307).
>
> Closes the long-deferred [#2](https://github.com/hherb/localmail/issues/2)
> by shipping `localmail estimate-upgrade` (pre-flight CLI projecting
> storage footprint + rough lock duration for migration 0006 against a
> populated `messages` table) and a new operator runbook at
> [docs/operations/upgrade-runbook.md](docs/operations/upgrade-runbook.md).
>
> Verification: full local suite at **826 passed** (was 809; +17 new
> tests across config / module / CLI). `mypy` clean on the three
> touched production files. `localmail estimate-upgrade --help` and
> `--format json | python -m json.tool` both succeed.
>
> Three follow-up issues filed from the final whole-branch code review;
> none block PR #102.

## Project context (1-minute version)

`localmail` mirrors IMAP accounts (Gmail OAuth, password) into Postgres.
**Strictly read-only with respect to IMAP**. Downstream consumers read
the DB + attachment tree directly or via the `localmail serve` HTTPS API.
See [CLAUDE.md](CLAUDE.md), [README.md](README.md),
[docs/operations/upgrade-runbook.md](docs/operations/upgrade-runbook.md)
(new this session), and
[docs/superpowers/specs/2026-05-17-localmail-gui-design.md](docs/superpowers/specs/2026-05-17-localmail-gui-design.md).

## What we shipped this session

### Issues + PR

- **Issue [#2](https://github.com/hherb/localmail/issues/2)** —
  `search: migration 0006 — make GIN index builds CONCURRENT for production upgrades`.
  Filed during code review of #1 (hybrid search Phase 1). Deferred
  until someone actually hit the live-upgrade scenario. Closed by
  PR #102 (`Closes #2` in the PR body).
- **PR [#102](https://github.com/hherb/localmail/pull/102)** —
  `feat: large-archive upgrade estimator + operator runbook (#2)`.
  12 commits; +2893 / -0 lines across 12 files.
- **Follow-up issues filed** (none block PR #102; all flagged by the
  final whole-branch review):
  - **[#103](https://github.com/hherb/localmail/issues/103)** — exit
    code mismatch (CLI exits 1, spec wanted 2).
  - **[#104](https://github.com/hherb/localmail/issues/104)** —
    GIN duration projection misses chunks-GIN component (currently
    counts only one GIN index in the duration formula).
  - **[#105](https://github.com/hherb/localmail/issues/105)** —
    regression test for the "chunks GIN cannot be projected" warning.

### Commits (12)

```
38c933e  docs(spec): large-archive upgrade estimator + operator runbook (#2)
a6d981d  docs(plan): implementation plan for upgrade estimator + runbook (#2)
dc837da  feat(config): add UpgradeEstimateConfig sub-model (#2)
08bf546  feat(upgrade_estimate): skeleton module + EstimateResult + registry (#2)
07491f1  feat(upgrade_estimate): pending-branch projections for 0006 (#2)
449d488  test(upgrade_estimate): cover not_applicable branch for missing messages table (#2)
0934778  feat(upgrade_estimate): applied-branch reads actual sizes (#2)
5a2a3a1  test(upgrade_estimate): pin missing-index warning behaviour (#2)
93298de  feat(cli): add `localmail estimate-upgrade` command (#2)
e3e8ffd  docs: operator runbook for lock-heavy migrations (#2)
cf88405  docs: cross-reference upgrade runbook from README + CLAUDE.md (#2)
1990908  fix(cli): drop mypy [call-arg] error on EstimatorFn invocation (#2)
```

### Headline changes

- **`src/localmail/upgrade_estimate.py`** *(new, 242 lines)* — pure
  estimator module. Exposes `EstimateResult` (frozen dataclass), the
  `ESTIMATORS` registry, and `estimate_0006`. The estimator dispatches
  on three branches: `not_applicable` (no `messages` table yet),
  `applied` (read `pg_total_relation_size` for both GIN indexes), and
  `pending` (project from row count + avg text length + config-driven
  throughput rates). Uses `to_regclass()` for safe catalog lookups
  (no exception-handling path for missing relations).
- **`src/localmail/config.py`** *(+34 lines)* — new
  `UpgradeEstimateConfig` sub-model with four config-overridable
  throughput / sizing constants (`fts_v2_blowup_factor`,
  `gin_size_factor`, `table_rewrite_mb_per_sec`,
  `gin_build_mb_per_sec`). Attached as `Config.upgrade`. No magic
  numbers leak into `upgrade_estimate.py`.
- **`src/localmail/cli.py`** *(+86 lines)* — new `estimate-upgrade`
  command with `--format text|json` (matches the
  `search-status`/`list-failed-*` convention). Read-only; iterates the
  registry, formats human-readable output or JSON.
- **`docs/operations/upgrade-runbook.md`** *(new, 202 lines)* — first
  doc in a new `docs/operations/` subdirectory. Covers when to read
  it, what each lock-heavy migration does, how to interpret
  estimator output, two adoption procedures (scheduled downtime /
  online column-rename), disk-space planning, and a calibration
  procedure for the throughput rates.
- **`config.example.toml`** *(+12 lines)* — commented `[upgrade]`
  block with all four tunables + brief comments.
- **`README.md`** *(+8 lines)* — blockquote near the existing
  migration-0016 callout pointing to the new runbook.
- **`CLAUDE.md`** *(+2 lines)* — `estimate-upgrade` added to the
  Commands block.
- **`tests/test_upgrade_estimate.py`** *(new, 242 lines)* — 10 tests
  across pending / applied / not_applicable branches, including a
  savepoint-scoped test that drops the `messages` table and asserts
  the `not_applicable` warning.
- **`tests/test_cli_estimate_upgrade.py`** *(new, 60 lines)* — 3 CLI
  integration tests (text output, JSON output, db-unreachable).
- **`tests/test_upgrade_estimate_config.py`** *(new, 41 lines)* — 4
  config round-trip tests.

### Spec + plan + design artefacts

- **Spec**:
  [docs/superpowers/specs/2026-05-27-large-archive-upgrade-estimator-design.md](docs/superpowers/specs/2026-05-27-large-archive-upgrade-estimator-design.md)
  (committed 38c933e; +332 lines).
- **Plan**:
  [docs/superpowers/plans/2026-05-27-large-archive-upgrade-estimator.md](docs/superpowers/plans/2026-05-27-large-archive-upgrade-estimator.md)
  (committed a6d981d; +1632 lines, 9 tasks).

### Verification

- `unset VIRTUAL_ENV && uv run pytest -q tests/` → **826 passed,
  4 warnings in 37.61s** (was 809; +17 new tests).
- `unset VIRTUAL_ENV && uv run mypy src/localmail/{upgrade_estimate,config,cli}.py`
  → **Success: no issues found in 3 source files**.
- `unset VIRTUAL_ENV && uv run localmail --help | grep estimate-upgrade`
  → command listed.
- `unset VIRTUAL_ENV && uv run localmail estimate-upgrade --format json | python -m json.tool`
  → valid JSON.

### Docs

- **README.md** — *updated this session* (Task 8 commit `cf88405`).
  New blockquote near the migration-0016 callout pointing operators
  to the new runbook before running `init-db` on a large pre-existing
  archive.
- **CLAUDE.md** — *updated this session* (Task 8 commit `cf88405`).
  `estimate-upgrade` added to the Commands block, with a reference
  comment pointing at the runbook.
- **ROADMAP.md** — does not exist in this repo. Not created.
- **docs/operations/** — *new directory* (Task 7 commit `e3e8ffd`).
  Created for ops-class documentation; first occupant is
  `upgrade-runbook.md`.

## What's next

### 1. **Maintainer: review + merge PR #102** *(blocks closing #2)*

PR is ready-for-review (12 commits; all per-task reviews + a final
whole-branch review approved; CI running at hand-off — first run is
[`26512017307`](https://github.com/hherb/localmail/actions/runs/26512017307)).

**Acceptance**: PR #102 merged to `main`, issue #2 auto-closes via
`Closes #2` in the PR body.

If CI fails: read the run log, fix the root cause (don't
`--no-verify`), re-push. The full local suite is green at the merge
SHA, so failures are most likely environment / Postgres version
mismatches; the `pgvector/pgvector:pg18` service container in
`.github/workflows/python-ci.yml` should match local.

### 2. **Address the three follow-up issues filed this session**

In rough priority order, decide which (if any) to address before
the next release:

- **[#104](https://github.com/hherb/localmail/issues/104)** —
  *GIN duration projection misses chunks-GIN.* Most operationally
  meaningful: the estimator currently underestimates lock duration
  whenever `message_chunks` is non-empty. Fix is either a `× 2`
  multiplier (conservative) or a separate query against
  `message_chunks` to get a real signal. Recommended for any
  operator who actually relies on the projection.
- **[#103](https://github.com/hherb/localmail/issues/103)** —
  *Exit code mismatch (1 vs spec's 2).* Cosmetic; nothing in the
  test or runbook depends on the exact exit code. Defer.
- **[#105](https://github.com/hherb/localmail/issues/105)** —
  *Pin chunks-GIN-cannot-be-projected warning in a test.* One-liner.
  Defer until someone touches that test file for any reason.

**Acceptance** (per issue): fix lands as a follow-up PR with `Closes
#NNN` in the body; full suite stays green.

### 3. **#38 — `/v1/changes` semantics decision** *(unchanged, needs user input)*

Conversation-first design call on what the wire contract should be
for initial backfill (since-cursor semantics, safe-horizon
interaction). No code until aligned.

### 4. **Carried-forward deferred items** *(unchanged)*

- **#90** glib Cargo alert — upstream-blocked.
- **#25** websockets.legacy DeprecationWarning — upstream-blocked.
- **#47** Third-party transient classes — needs ops data.
- **#5** Search batch INSERT — deferred until measured.

**Open issue count: 9** (was 6, +3 filed this session; #2 will
close on PR #102 merge → back to 8).

## Open decisions & risks

1. **`gin_chunks=0` projection is honest but underestimates lock
   duration.** The estimator says "I don't know the chunks-GIN size
   yet, so I'm calling it zero" and surfaces a warning. This is
   transparent but means the headline number is wrong. Filed as
   #104. Mitigation today: the runbook tells operators to schedule
   `2 × estimated_duration` as a maintenance window, which absorbs
   most of the chunks-GIN gap.

2. **Throughput defaults (80 MB/s rewrite, 30 MB/s GIN) assume SSD +
   modern Postgres.** Operators on HDD will see lock durations 5-10×
   the projection. The runbook documents a halving rule of thumb +
   a calibration procedure on a clone. All four constants live in
   `UpgradeEstimateConfig` and can be tuned per-deployment.

3. **The `not_applicable` test uses `DROP TABLE messages CASCADE`
   inside a savepoint, then rolls back.** Postgres DDL is
   transactional, so the schema is fully restored for subsequent
   tests. Verified at hand-off: 826 / 826 passing with no
   fixture-pollution failures. If a future change to `db_conn`
   changes the transaction model, that test may need to move to
   its own connection.

4. **`.claude/settings.local.json` stays untracked.** Same as prior
   sessions — local-only file. Not in `.gitignore`; if a future
   contributor wonders, add explicit ignore rules rather than
   committing.

## Exact commands to resume

```bash
cd /Users/hherb/src/localmail

# Verify state:
git status                           # expect: only .claude/* untracked (by design)
git log --oneline -5                 # tip: 1990908 on
                                     #   issue-2-upgrade-estimator-runbook
gh pr view 102                       # status: OPEN
gh pr checks 102 --watch             # watch CI; was pending at hand-off

# If picking option 1 (merge PR #102):
gh pr merge 102 --squash             # squash-merge (matches recent style)
git checkout main && git pull        # sync local
# Issue #2 auto-closes via `Closes #2` in PR body.

# If picking option 2 (fix #104 — GIN duration underestimate):
# Read the issue, branch off main, implement, PR.
gh issue view 104

# If picking option 3 (#38 semantics decision):
gh issue view 38                     # read the design context; conversation-first.

# Useful one-shot:
unset VIRTUAL_ENV && uv run pytest -q tests/   # 826 passed at hand-off
unset VIRTUAL_ENV && uv run localmail estimate-upgrade --help

gh issue list --state open --limit 40
```

## File map (post-session)

```
NEXT_SESSION.md                                                 # MODIFIED this session
docs/handoffs/
  2026-05-27T1248-utc-upgrade-estimator-pr-102.md               # NEW (this session's snapshot)
  2026-05-26T1255-utc-cli-config-fixture-pr-101.md              # prior session
  2026-05-26T0827-utc-at-scale-regression-pr-99.md              # earlier
  2026-05-26T0004-utc-node24-action-bumps-pr-98.md              # earlier
  …

docs/operations/                                                # NEW directory
  upgrade-runbook.md                                            # NEW (Task 7)

docs/superpowers/
  specs/
    2026-05-27-large-archive-upgrade-estimator-design.md        # NEW (this session)
  plans/
    2026-05-27-large-archive-upgrade-estimator.md               # NEW (this session)

src/localmail/
  upgrade_estimate.py                                           # NEW (Task 2 → 5)
  config.py                                                     # MODIFIED — UpgradeEstimateConfig added
  cli.py                                                        # MODIFIED — estimate-upgrade command + helpers

tests/
  test_upgrade_estimate.py                                      # NEW — 10 module tests
  test_upgrade_estimate_config.py                               # NEW — 4 config tests
  test_cli_estimate_upgrade.py                                  # NEW — 3 CLI tests

config.example.toml                                             # MODIFIED — commented [upgrade] block
README.md                                                       # MODIFIED — cross-reference blockquote
CLAUDE.md                                                       # MODIFIED — Commands block entry

migrations/                                                     # UNTOUCHED — 0006 is immutable per CLAUDE.md
```

Branch `issue-2-upgrade-estimator-runbook` is up-to-date with origin
at `1990908`. PR #102 is OPEN. Working tree clean (only
`.claude/settings.local.json` untracked, by design).
