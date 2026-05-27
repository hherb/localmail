# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-05-27T1352 UTC (post-session).**
> **#38 resolution shipped** as PR
> [#110](https://github.com/hherb/localmail/pull/110)
> (`issue-38-changes-tail-only-doc`). 1 commit. CI pending at
> hand-off (run `gh pr checks 110 --watch` to follow).
>
> Doc-heavy change: codifies the role split between `/v1/changes`
> (tail-only, 200-row cap) and `/v1/messages` (canonical backfill,
> shipped in PR #70). Both endpoints' wire behaviour was already
> correct — what was missing was an explicit contract in the spec,
> route docstrings, README, and one regression-pin test. Adopts
> **option (3)** from #38: keep `/v1/changes` strictly tail,
> point clients at `/v1/messages` for backfill. Option 2 (add
> `min_id` / `before` to `/v1/changes`) is explicitly rejected in
> the route docstring to prevent re-litigation.
>
> Verification: full local suite at **830 passed** (was 829;
> +1 from the new tail-cap regression pin). `mypy` clean on
> touched files. 3 warnings (pre-existing baseline shift; no new
> warnings introduced).

## Project context (1-minute version)

`localmail` mirrors IMAP accounts (Gmail OAuth, password) into Postgres.
**Strictly read-only with respect to IMAP**. Downstream consumers read
the DB + attachment tree directly or via the `localmail serve` HTTPS API.
See [CLAUDE.md](CLAUDE.md), [README.md](README.md),
[docs/operations/upgrade-runbook.md](docs/operations/upgrade-runbook.md),
and
[docs/superpowers/specs/2026-05-17-localmail-gui-design.md](docs/superpowers/specs/2026-05-17-localmail-gui-design.md).

## What we shipped this session

### Issue + PR

- **Issue [#38](https://github.com/hherb/localmail/issues/38)** —
  `api: /v1/changes — semantics decision on initial backfill window`.
  Filed as a PR #30 review follow-up; held open pending a design
  call on whether to extend `/v1/changes` or split the tail-vs-
  backfill responsibilities across two endpoints. The decision
  matured naturally once PR #70's `/v1/messages` keyset browse
  endpoint shipped — this PR documents the split. Closed by PR #110
  (`Closes #38` in body).
- **PR [#110](https://github.com/hherb/localmail/pull/110)** —
  `docs(api): /v1/changes is tail-only; /v1/messages is the canonical backfill endpoint (#38)`.
  1 commit; +84 / -2 lines across 5 files.

### Commits (1)

```
2ef78d5  docs(api): /v1/changes is tail-only; /v1/messages is the canonical backfill endpoint (#38)
```

### Headline changes

- **`docs/superpowers/specs/2026-05-17-localmail-gui-design.md`**
  *(+19 / -2 lines across 2 spots)* — Replaces the never-shipped
  `GET /v1/folders/{id}/messages` table row in *Accounts & folders*
  with the actual `GET /v1/messages?account_id&folder_id&cursor&limit`
  shape (matching PR #70). Adds a *"tail-only / use `/v1/messages`
  for backfill"* paragraph below the *Changes (polling)* table,
  with an explicit note rejecting `min_id` / `before` extensions
  on `/v1/changes`.
- **`src/localmail/serve/routes/changes.py`** *(+10 / -0 lines)* —
  Docstring now leads with *"Tail-subscription endpoint, not a
  backfill walk (#38)"*, points at `/v1/messages` for backfill,
  and carries the do-not-add `min_id` note.
- **`src/localmail/serve/routes/messages.py`** *(+7 / -0 lines)* —
  Docstring leads with *"Canonical browse / backfill endpoint
  (#38)"*, names the shared sort key, and identifies the
  unbounded-scroll vs row-capped split.
- **`README.md`** *(+6 / -0 lines)* — Short operator-facing role-
  split note in the *Browse & search pagination* section, mirroring
  the spec language.
- **`tests/test_serve_changes_route.py`** *(+42 / -0 lines)* — New
  `test_changes_no_cursor_caps_at_default_limit_in_desc_order`
  seeds `_DEFAULT_LIMIT + 5` rows and asserts the no-cursor response
  is exactly `_DEFAULT_LIMIT` long, in strictly DESC id order.
  Imports `_DEFAULT_LIMIT` to avoid a magic number; 4-char hex
  suffix scales to 65535 rows so the test stays robust if the cap
  is later raised.

### Verification

- `unset VIRTUAL_ENV && uv run pytest -q tests/test_serve_changes_route.py tests/test_serve_browse_route.py`
  → **14 passed in 1.94s** (8 changes tests + 6 browse tests; 1 new).
- `unset VIRTUAL_ENV && uv run pytest -q tests/` → **830 passed,
  3 warnings in 39.05s** (+1 row vs. the 829 baseline from the
  prior session, exactly the new pin test).
- `unset VIRTUAL_ENV && uv run mypy src/localmail/serve/routes/changes.py
  src/localmail/serve/routes/messages.py tests/test_serve_changes_route.py`
  → **Success: no issues found in 3 source files**.

### Docs

- **`docs/superpowers/specs/2026-05-17-localmail-gui-design.md`** —
  *updated this session* (Accounts & folders table + Changes section).
- **README.md** — *updated this session* (one-paragraph role-split
  note in *Browse & search pagination*).
- **CLAUDE.md** — *unchanged*. CLAUDE.md already documents the
  GUI server's browse/changes endpoints; the role split is
  implicit in the existing content and didn't need to be restated.
- **ROADMAP.md** — does not exist in this repo. Not created (same
  decision as prior sessions).
- **`docs/operations/upgrade-runbook.md`** — *unchanged this session*.

## What's next

### 1. **Maintainer: review + merge PR #110** *(closes #38)*

PR is single-commit, docs + one new regression-pin test, ~84 lines.
All tests green locally (830 passed). CI running at hand-off.

**Acceptance**: PR #110 merged to `main`; issue #38 auto-closes
via the `Closes #38` line in the PR body.

If CI fails: read the run log, fix the root cause (don't
`--no-verify`), re-push. Local suite is green at the branch tip;
failures are most likely environment differences.

### 2. **Carried-forward deferred items** *(unchanged from prior session)*

- **#47** `extract_worker` transient-class opt-in for third
  parties — follow-up to #36; needs production telemetry on which
  third-party extractor exceptions are recoverable before broadening
  the transient-classification list. Open until that data is
  available.
- **#90** glib Cargo alert — upstream-blocked (Tauri stack bump).
- **#25** websockets.legacy DeprecationWarning — upstream-blocked.
- **#5** Search batch INSERT — deferred until measured.

**Open issue count: 4** (was 5 at start of session; PR #110 will
auto-close #38 on merge, taking the count to **4**: #90, #47, #25,
#5 — all blocked on external input or telemetry).

### 3. **Possible next sessions** *(no urgent driver)*

With #38 resolved, all open issues are blocked on inputs from
outside this repo (upstream stacks, ops telemetry, or measured
need). Productive next-session options:

- **Wait** for input on #47, #5 — no work to do here without it.
- **GUI client follow-up**: the spec is now correct, but the GUI
  client plan in `docs/superpowers/plans/2026-05-17-localmail-gui-client-3-mainview.md`
  still describes the *prior* "use `/v1/changes` for the recent-
  200 because there's no list endpoint" approach as "acknowledged
  tech debt". Now that `/v1/messages` exists and is documented,
  that plan / the client itself could be updated to use the
  canonical backfill endpoint. Out of scope for this PR (no
  localmail-side change needed); raise an issue if the GUI side
  hasn't already migrated.
- **Net-new work**: a small operator quality-of-life PR
  (e.g. `localmail search-status` augmentations, or extra
  `estimate-upgrade` outputs) would fit a short session.

## Open decisions & risks

1. **The route-docstring `min_id` rejection is load-bearing.**
   `serve/routes/changes.py`'s docstring explicitly says *"do NOT
   add a `min_id` / `before` parameter here"* with the rationale
   inline. A future contributor who wants to widen `/v1/changes`
   for backfill should be redirected to `/v1/messages`; if a
   genuinely new requirement emerges (e.g. a scripted consumer
   that needs both forward and backward sweeps in a single
   endpoint), revisit at a design level — don't quietly extend
   the polling endpoint.

2. **The new tail-cap pin (`len == _DEFAULT_LIMIT`) makes a
   future cap bump a deliberate two-file change.** That's the
   point: option (2) from #38 (extend `/v1/changes`) would now
   require editing both the test pin *and* the docstring rejection
   note, instead of silently widening the contract. The 4-char hex
   suffix in the test scales to 65535 rows so the test itself
   stays valid if the cap is raised; only the assertion needs to
   move.

3. **`.claude/settings.local.json` stays untracked.** Same as
   prior sessions — local-only file. Not in `.gitignore`; if a
   future contributor wonders, add explicit ignore rules rather
   than committing.

4. **GUI client may still call `/v1/changes` for initial load.**
   The plan in
   `docs/superpowers/plans/2026-05-17-localmail-gui-client-3-mainview.md`
   was implemented before `/v1/messages` shipped. The localmail
   server side is now self-consistent (spec + routes + README +
   regression test), but the Tauri client may still be doing the
   prior "recent-200 via /v1/changes" walk. That's a client-side
   issue, not a server-side one — file separately if it surfaces.

## Exact commands to resume

```bash
cd /Users/hherb/src/localmail

# Verify state:
git status                           # expect: only .claude/* untracked
git log --oneline -5                 # tip on issue-38-changes-tail-only-doc:
                                     #   2ef78d5 docs(api): /v1/changes is tail-only ... (#38)
gh pr view 110                       # status: OPEN
gh pr checks 110 --watch             # watch CI; pending at hand-off

# If picking option 1 (merge PR #110):
gh pr merge 110 --squash             # squash-merge (matches recent style)
git checkout main && git pull        # sync local
# Issue #38 auto-closes via `Closes #38` in PR body.

# Useful one-shots:
unset VIRTUAL_ENV && uv run pytest -q tests/   # 830 passed at hand-off
unset VIRTUAL_ENV && uv run pytest -q tests/test_serve_changes_route.py  # 8 passed, 1.x s
gh issue list --state open --limit 40
```

## File map (post-session)

```
NEXT_SESSION.md                                                 # MODIFIED this session
docs/handoffs/
  2026-05-27T1352-utc-changes-tail-only-pr-110.md               # NEW (this session's snapshot)
  2026-05-27T1336-utc-exit-code-spec-alignment-pr-109.md        # prior session
  2026-05-27T1319-utc-chunks-gin-projection-pr-108.md           # earlier
  2026-05-27T1248-utc-upgrade-estimator-pr-102.md               # earlier
  …

docs/superpowers/specs/
  2026-05-17-localmail-gui-design.md                            # MODIFIED — Accounts & folders + Changes sections

src/localmail/serve/routes/
  changes.py                                                    # MODIFIED — tail-only docstring (#38)
  messages.py                                                   # MODIFIED — backfill docstring (#38)

README.md                                                       # MODIFIED — role-split note in Browse & search pagination

tests/
  test_serve_changes_route.py                                   # MODIFIED — new tail-cap regression pin
```

Branch `issue-38-changes-tail-only-doc` is up-to-date with origin
at `2ef78d5`. PR #110 is OPEN. Working tree clean (only
`.claude/settings.local.json` untracked, by design).
