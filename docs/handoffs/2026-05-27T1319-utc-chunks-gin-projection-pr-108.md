# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-05-27T1319 UTC (post-session).**
> **#106 fix shipped** as PR
> [#108](https://github.com/hherb/localmail/pull/108)
> (`issue-106-chunks-gin-projection`). 1 commit. CI pending at
> hand-off; run is
> [`26513577449`](https://github.com/hherb/localmail/actions/runs/26513577449).
>
> Fixes the chunks-GIN underestimate from the previous session's
> upgrade estimator (#102): `_estimate_0006_pending` now projects
> `message_chunks` size into `projected_bytes["gin_chunks"]` and
> sums both GIN builds in `gin_duration` when `message_chunks` is
> populated. Suppresses the cannot-be-projected warning in that
> case; keeps it when the table is empty or missing.
>
> Verification: full local suite at **829 passed** (was 826; +3 new
> tests). `mypy` clean. `estimate-upgrade --format json` smoke test
> green.
>
> One housekeeping action: **closed #104 as a duplicate of #106**
> (same root cause, #106 had the better acceptance criteria).
> **PR #108 closes #105 and #106** on merge (the new
> `test_estimate_0006_pending_empty_chunks_still_warns` is exactly
> the one-line pin #105 asked for).

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

- **Issue [#106](https://github.com/hherb/localmail/issues/106)** —
  `estimate-upgrade: project chunks-GIN build duration from message_chunks when populated`.
  Filed at the tail end of the previous session as a follow-up of
  PR #102. Closed by PR #108 (`Closes #106` in body).
- **Issue [#105](https://github.com/hherb/localmail/issues/105)** —
  `test(upgrade_estimate): pin chunks-GIN-cannot-be-projected warning string`.
  Subsumed: the new empty-chunks regression test
  (`test_estimate_0006_pending_empty_chunks_still_warns`) lands the
  exact assertion #105 asked for. PR #108 closes it on merge.
- **Issue [#104](https://github.com/hherb/localmail/issues/104)** —
  closed as **duplicate of #106** (same problem, #106 had cleaner
  framing + acceptance criteria).
- **PR [#108](https://github.com/hherb/localmail/pull/108)** —
  `fix(upgrade_estimate): project chunks-GIN from message_chunks when populated (#106)`.
  1 commit; +208 / -17 lines across 3 files.

### Commits (1)

```
b06bd22  fix(upgrade_estimate): project chunks-GIN from message_chunks when populated (#106)
```

### Headline changes

- **`src/localmail/upgrade_estimate.py`** *(+57 lines net)* — new
  `_project_chunks_gin_bytes(conn, cfg, warnings) -> int` helper.
  When `message_chunks` exists and is non-empty, projects
  `count × avg(octet_length(text)) × fts_v2_blowup × gin_size`
  (mirrors the messages-side formula). Lifts the
  `_CHUNKS_GIN_EMPTY_WARNING` constant out of the inline string
  so empty/missing branches share one canonical message.
  `_estimate_0006_pending` now calls the helper and sums both
  GIN sizes when computing `gin_duration`.
- **`tests/test_upgrade_estimate.py`** *(+132 lines)* — three new
  tests + two new helpers (`_seed_message_chunks`,
  `_first_message_id`). Tests added:
  - `test_estimate_0006_pending_with_populated_chunks_projects_chunks_gin`
    — populated chunks yield `gin_chunks > 0` matching the formula;
    warning absent (acceptance criterion from #106).
  - `test_estimate_0006_pending_chunks_gin_contributes_to_duration`
    — same corpus, with vs. without chunks: populating chunks pushes
    `projected_duration_s` strictly higher.
  - `test_estimate_0006_pending_empty_chunks_still_warns` — pins
    the cannot-be-projected warning when chunks are empty (#105's
    requested regression pin).
- **`docs/operations/upgrade-runbook.md`** *(+11 / -4 lines)* —
  `gin_chunks` bullet rewritten to explain both empty and populated
  cases; `2 × estimated_duration` rationale updated to clarify the
  safety margin is consumed when the chunks warning is present
  (chunks-GIN gap is no longer the dominant uncertainty).

### Verification

- `unset VIRTUAL_ENV && uv run pytest -q tests/` → **829 passed,
  3 warnings in 46.70s** (was 826; +3 new tests).
- `unset VIRTUAL_ENV && uv run pytest -q tests/test_upgrade_estimate.py`
  → **13 passed in 0.42s** (was 10).
- `unset VIRTUAL_ENV && uv run mypy src/localmail/upgrade_estimate.py`
  → **Success: no issues found in 1 source file**.
- `unset VIRTUAL_ENV && uv run localmail estimate-upgrade --format json | python -m json.tool`
  → valid JSON (local DB has 0006 applied, so output is
  `status: applied` — the new pending-branch behavior is covered by
  unit tests against `db_conn`).

### Docs

- **`docs/operations/upgrade-runbook.md`** — *updated this session*.
- **README.md** — *unchanged*. Its callout points operators at the
  runbook, which now has the correct explanation.
- **CLAUDE.md** — *unchanged*. The `estimate-upgrade` command line
  is still accurate.
- **ROADMAP.md** — does not exist in this repo. Not created (same
  decision as prior sessions).

## What's next

### 1. **Maintainer: review + merge PR #108** *(closes #105 + #106)*

PR is single-commit, focused, ~200 lines. All tests green locally
(829 passed). CI running at hand-off — run is
[`26513577449`](https://github.com/hherb/localmail/actions/runs/26513577449).

**Acceptance**: PR #108 merged to `main`; issues #105 and #106
auto-close via the `Closes` lines in the PR body. #104 already
closed manually as a duplicate.

If CI fails: read the run log, fix the root cause (don't
`--no-verify`), re-push. Local suite is green at the merge SHA;
failures are most likely environment differences.

### 2. **#107 — CLI exit-code alignment (low priority)**

The remaining follow-up from PR #102's review. Spec said exit 2
on DB-unreachable; implementation exits 1 via
`click.ClickException`. Two paths (issue body has both):
- **(a)** Edit the spec to match implementation (one-line; matches
  Click idiom; **recommended in the issue body**).
- **(b)** `sys.exit(2)` explicitly + tighten the test.

Cosmetic. Defer until someone scripts against the exit code.

**Acceptance**: either spec one-liner merged, or CLI handler
returns exit 2 with test pinned.

### 3. **#38 — `/v1/changes` semantics decision** *(unchanged, needs user input)*

Conversation-first design call on what the wire contract should be
for initial backfill (since-cursor semantics, safe-horizon
interaction). No code until aligned.

### 4. **Carried-forward deferred items** *(unchanged)*

- **#90** glib Cargo alert — upstream-blocked.
- **#25** websockets.legacy DeprecationWarning — upstream-blocked.
- **#47** Third-party transient classes — needs ops data.
- **#5** Search batch INSERT — deferred until measured.

**Open issue count: 8** (was 10 at start of session: #2 closed by
prior PR's merge before this session began; #104 closed as
duplicate; #105 + #106 will auto-close on PR #108 merge → 6;
#107 then becomes the smallest remaining estimator follow-up).

## Open decisions & risks

1. **The local archive cannot pre-flight the new pending-branch
   behavior end-to-end** because 0006 is already `applied`. The
   unit tests against `db_conn` cover both branches (pending +
   populated, pending + empty), so the lack of a live JSON
   dry-run is not a blocker. If operating against a real
   pre-0006 archive, run `localmail estimate-upgrade` and check
   that (a) `gin_chunks (projected)` is non-zero if the embed
   worker has run and (b) the cannot-be-projected warning is
   absent in that case.

2. **The chunks-GIN projection assumes the messages-GIN formula
   shape applies unchanged** (`text bytes × fts_v2_blowup ×
   gin_size`). This is correct for `tsvector` GIN indexes built
   on text data; both indexes have the same flavor. If a future
   migration adds a different GIN over `message_chunks` (e.g.
   trigram), the helper would need a separate factor — but that's
   speculative and not in scope.

3. **Throughput defaults (80 MB/s rewrite, 30 MB/s GIN) still
   assume SSD + modern Postgres.** Unchanged from PR #102. The
   runbook documents a halving rule of thumb + a calibration
   procedure on a clone. All four constants live in
   `UpgradeEstimateConfig`.

4. **`.claude/settings.local.json` stays untracked.** Same as
   prior sessions — local-only file. Not in `.gitignore`; if a
   future contributor wonders, add explicit ignore rules rather
   than committing.

## Exact commands to resume

```bash
cd /Users/hherb/src/localmail

# Verify state:
git status                           # expect: only .claude/* untracked
git log --oneline -5                 # tip on issue-106-chunks-gin-projection:
                                     #   b06bd22 fix(upgrade_estimate): project chunks-GIN ... (#106)
gh pr view 108                       # status: OPEN
gh pr checks 108 --watch             # watch CI; pending at hand-off

# If picking option 1 (merge PR #108):
gh pr merge 108 --squash             # squash-merge (matches recent style)
git checkout main && git pull        # sync local
# Issues #105 + #106 auto-close via `Closes` lines in PR body.

# If picking option 2 (fix #107 — exit code mismatch):
gh issue view 107

# If picking option 3 (#38 semantics decision):
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
  2026-05-27T1319-utc-chunks-gin-projection-pr-108.md           # NEW (this session's snapshot)
  2026-05-27T1248-utc-upgrade-estimator-pr-102.md               # prior session
  2026-05-26T1255-utc-cli-config-fixture-pr-101.md              # earlier
  …

docs/operations/
  upgrade-runbook.md                                            # MODIFIED — chunks-GIN explanation

src/localmail/
  upgrade_estimate.py                                           # MODIFIED — _project_chunks_gin_bytes helper

tests/
  test_upgrade_estimate.py                                      # MODIFIED — 3 new tests + 2 new helpers
```

Branch `issue-106-chunks-gin-projection` is up-to-date with origin
at `b06bd22`. PR #108 is OPEN. Working tree clean (only
`.claude/settings.local.json` untracked, by design).
