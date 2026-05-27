# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-05-27T1336 UTC (post-session).**
> **#107 fix shipped** as PR
> [#109](https://github.com/hherb/localmail/pull/109)
> (`issue-107-spec-exit-code-alignment`). 1 commit. CI pending at
> hand-off (run `gh pr checks 109 --watch` to follow).
>
> Spec-only change: aligns the design spec's claimed exit code (2)
> with what the implementation has shipped from day one (1, via
> `click.ClickException` — the Click idiom that every other
> `localmail` subcommand follows). Tightens the existing CLI test
> from `assert exit_code != 0` to `assert exit_code == 1` so the
> wire contract can't drift again. **One housekeeping action**:
> **closed #103 as a duplicate of #107** (same root cause, #107 had
> the cleaner framing). PR #109 closes both on merge.
>
> Verification: full local suite at **829 passed** (unchanged baseline
> — this session adds no new tests, only tightens one assertion).
> `mypy` clean on touched files.

## Project context (1-minute version)

`localmail` mirrors IMAP accounts (Gmail OAuth, password) into Postgres.
**Strictly read-only with respect to IMAP**. Downstream consumers read
the DB + attachment tree directly or via the `localmail serve` HTTPS API.
See [CLAUDE.md](CLAUDE.md), [README.md](README.md),
[docs/operations/upgrade-runbook.md](docs/operations/upgrade-runbook.md),
and
[docs/superpowers/specs/2026-05-17-localmail-gui-design.md](docs/superpowers/specs/2026-05-17-localmail-gui-design.md).

## What we shipped this session

### Issues + PR

- **Issue [#107](https://github.com/hherb/localmail/issues/107)** —
  `estimate-upgrade: align DB-unreachable exit code with spec`.
  Filed as a PR #102 review follow-up. Closed by PR #109 (`Closes
  #107` in body).
- **Issue [#103](https://github.com/hherb/localmail/issues/103)** —
  closed as **duplicate of #107** (same problem, #107 had cleaner
  framing + the option-(a) recommendation in the body). PR #109 also
  carries `Closes #103` so the issue stays linked to the PR on merge.
- **PR [#109](https://github.com/hherb/localmail/pull/109)** —
  `docs(spec): align estimate-upgrade DB-unreachable exit code with implementation (#107)`.
  1 commit; +14 / -4 lines across 2 files.

### Commits (1)

```
0bd2670  docs(spec): align estimate-upgrade DB-unreachable exit code with implementation (#107)
```

### Headline changes

- **`docs/superpowers/specs/2026-05-27-large-archive-upgrade-estimator-design.md`**
  *(+2 / -2 lines across 2 spots)* — Failure modes table and
  testing-strategy entry both now say exit **1** via
  `click.ClickException` (Click idiom; matches every other
  `localmail` subcommand). Adds an explicit note that the JSON
  channel is the structured one for scripts; the exit code is not
  differentiated from other CLI errors.
- **`tests/test_cli_estimate_upgrade.py`** *(+12 / -2 lines)* — the
  existing `test_cli_estimate_upgrade_db_unreachable` is tightened
  from `assert result.exit_code != 0` to
  `assert result.exit_code == 1` with a docstring explaining the
  wire contract. Pin makes future spec/impl drift loud at CI time.

### Verification

- `unset VIRTUAL_ENV && uv run pytest -q tests/test_cli_estimate_upgrade.py`
  → **3 passed in 0.41s** (no new tests; one assertion tightened).
- `unset VIRTUAL_ENV && uv run pytest -q tests/` → **829 passed,
  4 warnings in 48.17s** (unchanged baseline).
- `unset VIRTUAL_ENV && uv run mypy src/localmail/cli.py
  tests/test_cli_estimate_upgrade.py` → **Success: no issues found
  in 2 source files**.

### Docs

- **`docs/superpowers/specs/2026-05-27-large-archive-upgrade-estimator-design.md`** —
  *updated this session* (the only doc surface that referred to
  exit 2).
- **README.md** — *unchanged*. README never documented an exit code
  for `estimate-upgrade`, so no operator-facing surface needs to
  follow.
- **CLAUDE.md** — *unchanged*. No mention of the exit code in
  CLAUDE.md either.
- **ROADMAP.md** — does not exist in this repo. Not created (same
  decision as prior sessions).
- **`docs/operations/upgrade-runbook.md`** — *unchanged this session*
  (was updated last session for the chunks-GIN projection text).

## What's next

### 1. **Maintainer: review + merge PR #109** *(closes #107 + #103)*

PR is single-commit, docs + one-line test tightening, ~18 lines.
All tests green locally (829 passed). CI running at hand-off.

**Acceptance**: PR #109 merged to `main`; issues #107 and #103
auto-close via the `Closes` lines in the PR body.

If CI fails: read the run log, fix the root cause (don't
`--no-verify`), re-push. Local suite is green at the branch tip;
failures are most likely environment differences.

### 2. **#38 — `/v1/changes` semantics decision** *(needs user input)*

Conversation-first design call on what the wire contract should be
for initial backfill (since-cursor semantics, safe-horizon
interaction). No code until aligned. Smallest user-facing remaining
issue once #107 lands.

### 3. **#47 — `extract_worker` transient-class opt-in for third parties** *(needs ops data)*

Follow-up to #36; needs production telemetry on which third-party
extractor exceptions are recoverable before broadening the
transient-classification list. Open until that data is available.

### 4. **Carried-forward deferred items** *(unchanged)*

- **#90** glib Cargo alert — upstream-blocked.
- **#25** websockets.legacy DeprecationWarning — upstream-blocked.
- **#5** Search batch INSERT — deferred until measured.

**Open issue count: 5** (was 7 at start of session; PR #109 will
auto-close #107 + #103 on merge, taking the count to **4**: #90,
#47, #38, #25, #5).

## Open decisions & risks

1. **The new exit-code pin (`== 1`) makes a future re-spec to exit
   2 a deliberate two-file change.** That's the point: option (b)
   from #107 would now require editing both the spec back to exit 2
   *and* the test pin, instead of silently re-introducing the same
   drift. If someone genuinely needs exit 2 (e.g. a scripted
   orchestration layer that distinguishes DB-unreachable from other
   CLI failures), that's the time to add a structured channel —
   probably a non-zero `error` field in the JSON output — rather
   than redoing the exit-code change.

2. **`.claude/settings.local.json` stays untracked.** Same as
   prior sessions — local-only file. Not in `.gitignore`; if a
   future contributor wonders, add explicit ignore rules rather
   than committing.

3. **Throughput defaults (80 MB/s rewrite, 30 MB/s GIN) still
   assume SSD + modern Postgres.** Unchanged from PR #102. The
   runbook documents a halving rule of thumb + a calibration
   procedure on a clone. All four constants live in
   `UpgradeEstimateConfig`.

## Exact commands to resume

```bash
cd /Users/hherb/src/localmail

# Verify state:
git status                           # expect: only .claude/* untracked
git log --oneline -5                 # tip on issue-107-spec-exit-code-alignment:
                                     #   0bd2670 docs(spec): align estimate-upgrade ... (#107)
gh pr view 109                       # status: OPEN
gh pr checks 109 --watch             # watch CI; pending at hand-off

# If picking option 1 (merge PR #109):
gh pr merge 109 --squash             # squash-merge (matches recent style)
git checkout main && git pull        # sync local
# Issues #107 + #103 auto-close via `Closes` lines in PR body.

# If picking option 2 (#38 semantics decision):
gh issue view 38                     # read the design context; conversation-first.

# Useful one-shots:
unset VIRTUAL_ENV && uv run pytest -q tests/   # 829 passed at hand-off
unset VIRTUAL_ENV && uv run localmail estimate-upgrade --format json | python -m json.tool

gh issue list --state open --limit 40
```

## File map (post-session)

```
NEXT_SESSION.md                                                 # MODIFIED this session
docs/handoffs/
  2026-05-27T1336-utc-exit-code-spec-alignment-pr-109.md        # NEW (this session's snapshot)
  2026-05-27T1319-utc-chunks-gin-projection-pr-108.md           # prior session
  2026-05-27T1248-utc-upgrade-estimator-pr-102.md               # earlier
  2026-05-26T1255-utc-cli-config-fixture-pr-101.md              # earlier
  …

docs/superpowers/specs/
  2026-05-27-large-archive-upgrade-estimator-design.md          # MODIFIED — Failure modes + testing strategy

tests/
  test_cli_estimate_upgrade.py                                  # MODIFIED — exit_code == 1 pin + docstring
```

Branch `issue-107-spec-exit-code-alignment` is up-to-date with
origin at `0bd2670`. PR #109 is OPEN. Working tree clean (only
`.claude/settings.local.json` untracked, by design).
