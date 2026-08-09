# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-08-09 (session 22).** A **single-issue session** again:
> the operator picked **#280** (the 13-minute `search-status`) and #284 came
> with it, because #284's own issue says to do it alongside #280 — its added
> aggregate lands on the query being profiled. One commit, **`0f0b1aa`**, open
> as **PR #286**, **not merged**. `main` is still **`57ce228`**.
>
> **The headline number: `search-status` went from `13:28.45` to `0.97 s` on
> the 127k-message Mac archive, with every counter byte-identical.** Risk 2 of
> the last handoff ("budget ~14 minutes and do not take it for a hang") is
> **gone** — delete that habit.
>
> **The DGX was deployed this session** and is no longer three commits behind.
> Both hosts now run `57ce228`; neither runs `0f0b1aa` yet.
>
> **A follow-up, PR #288, clears the two `pypdf` Dependabot alerts** that were
> filed after the last handoff was frozen. Lock-only, branched off `main`, CI
> green — mergeable in any order relative to the other two.
>
> **One prediction the last handoff made was wrong, and it matters for how you
> read the counters:** it expected `blobs_claimable` to come in "well above 0"
> on the Mac. It is **0**. See risk 5.

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

### 0. The DGX is deployed — `80c1138` → `57ce228`

It had been three commits behind since session 19, so it was missing the 0.3.0
release, the version/socket fix (#276), **and** the #277 counter fix. Done:
`git pull`, `~/.local/bin/uv sync --extra mcp --extra extraction` (localmail
0.1.0 → 0.3.0), `localmail init-db` (**"schema already up to date"** — #277
added no migration), `systemctl --user restart localmail-daemon
localmail-serve`.

Verified: both units `active`, five heartbeat rows (embed / extract / idle:1 /
poll:1 / reconcile) all under 30 s old. The Mac's seven rows are equally fresh.

### 1. PR #286 → commit `0f0b1aa` — decorrelate blob eligibility (#280), guard the partition (#284)

Branch `fix/280-decorrelate-blob-eligibility`. **Open, unmerged.** No
migration, no new dependency, no config change.

#### #280 — 13:28.45 → 0.97 s

The eligibility predicate's extension half reads original filenames out of
`messages.attachments` (#216). Written as a correlated `EXISTS` it was a
`SubPlan` re-executed once per blob, and correlating the operand on `b.sha256`
is exactly what makes the planner abandon `messages_attachments_gin`:

| operand | plan | cost |
|---|---|---|
| constant | `Bitmap Index Scan on messages_attachments_gin` | ~42 |
| `encode(b.sha256,'hex')` | `Seq Scan on messages`, once per blob | ~36,203 |

`EXTENSION_MATCH_JOIN_SQL` resolves it once for the whole archive. Note it does
**not** restore the ~42 index plan — it carries no containment predicate, so it
is one `Seq Scan on messages` + `HashAggregate` for the whole archive, i.e. the
scan paid once instead of once per blob. (`messages_attachments_gin`'s remaining
user is `extract_worker._blob_filenames`, which does pass a constant.) Measured
on the live Mac archive (127,494 messages, 16,644 blobs), the **same command**
before and after:

```
13:28.45  ->  0.97 s
blobs_eligible 9491 = extracted 9203 + no_text 106 + gave_up 182 + pending 0
blobs_claimable 0
```

Every counter byte-identical, including the eleven non-blob ones.

**Three judgement calls a future session would plausibly undo — don't:**

- **It hangs off the new `QUEUE_COUNTS_FROM_SQL`, not `QUEUE_FROM_SQL`.** The
  latter is shared with `_claim_batch`, which runs every sweep under
  `FOR UPDATE … SKIP LOCKED`; putting a scan of every message there would put
  it on the worker's hot path. Pinned by
  `test_the_claim_join_shape_never_touches_messages`.
- **A `LEFT JOIN`, not an uncorrelated `IN (SELECT …)`.** The subquery form
  reads better and plans identically until the planner *estimates* the hashed
  subplan will not fit `work_mem`, at which point it plans the per-row form —
  undoing the fix on precisely the large archives it was written for. The
  estimate is made at plan time from statistics; Postgres does not detect
  overflow at runtime and switch, so bad statistics can choose that form on an
  archive that would have fit. A hash join spills to disk instead. This one is
  invisible at fixture scale and will look like gratuitous complexity to the
  next reader.
- **`jsonb_typeof(m.attachments) = 'array'` guards the expansion.** The
  correlated form's `@>` was a single-relation qual the planner pushed below
  the lateral, so `jsonb_array_elements` only ever saw arrays. Decorrelated
  there is no restriction on `messages`, and `jsonb_array_elements` raises
  `22023` on an object or scalar — while the column is `JSONB NOT NULL DEFAULT
  '[]'` with no `CHECK`. One malformed row would abort the statement and escape
  `search_status`'s narrow catch, taking the eleven healthy counters with it.
  Pinned by `test_a_malformed_attachments_row_does_not_abort_the_report`.
- **`DISTINCT` is load-bearing, and no runtime guard covers it.** A blob is
  content-addressable and global, so every message carrying those bytes names
  it independently. Without it a blob several messages named admissibly fans
  out into one row per message, inflating every counter. The partition check
  does **not** catch this: the fan-out multiplies `eligible` and the buckets
  equally, each duplicate still matches exactly one bucket, so the sum holds
  and `misfiled` stays `0`. The only symptom an operator sees is `pending`
  diverging from `claimable` — #277's failure mode returning. Pinned solely by
  `test_a_blob_two_messages_both_named_admissibly_is_counted_once`, which is
  therefore load-bearing rather than redundant.

**The regression pin is a plan assertion, because nothing about the answers
changes — and it takes two, because either alone has a hole.**
`tests/test_extract_queue_sql.py` walks `EXPLAIN (FORMAT JSON)` and requires
that no scan of `messages` sit under a `SubPlan` — a property of the plan tree,
so it holds at fixture scale where a wall-clock assertion would mean nothing.
But it recognises re-execution only in the shape the pre-fix predicate had: a
`LEFT JOIN LATERAL` re-correlation keeps the join, merely makes it per-blob,
and slips straight past (measured: 30 loops on 30 blobs, zero `SubPlan` hits).
So it also walks `EXPLAIN (ANALYZE)` and requires `Actual Loops == 1`, over a
**seeded** fixture — on the empty tables `db_conn` yields, every form reports
`0` loops and that assertion is vacuous. Both keep the pre-#280 predicate
**verbatim** as a negative control, the role `--predicate-form pre75` plays in
`run_browse_explain.py`. Do not "tidy up"
`_PRE280_CORRELATED_ALLOWLIST_SQL`; it is a museum piece.

#### #284 — check the partition, not just its sum

`__post_init__`'s sum check is *implied by* a partition but does not *imply*
one: a blob counted twice plus a blob counted not at all adds up correctly.

- **`BUCKET_WHERE_SQL` is now the one authority for what the buckets are.** The
  SELECT's aggregates, `__post_init__`'s sum, and the new guard all derive from
  it, so a fifth disposition cannot reach one and miss another.
- **`misfiled_count_sql` casts each predicate to `int` and demands exactly
  `1`.** `IS DISTINCT FROM` rather than `<>` because the total goes SQL `NULL`
  as soon as any predicate does — which is exactly what relaxing one of the
  `NOT NULL` columns they pivot on would produce, and a `NULL` filter condition
  counts nothing and reports the archive healthy.
- **It takes its buckets as a parameter.** The production four are structurally
  incapable of overlapping, so the detector cannot be exercised through them —
  which is why nothing tested this guard. Contrived predicates over a `VALUES`
  row do it with no fixtures at all.
- **Its scope is one blob against the four buckets.** It does not — cannot —
  see a blob duplicated by a join fan-out, because each duplicate still lands
  in exactly one bucket. That failure mode belongs to `DISTINCT` above, and the
  only thing guarding it is a test.
- **`misfiled` is the one field `status_field_names()` excludes.** Its only
  non-zero value raises, so reporting it would put a permanently-`0` line in
  front of an operator and invite the wrong question.

#### Test layout

`tests/test_extract_queue.py` was already at 506 lines. It is now DB behaviour
only (494); the SQL fragments, the plan pins, and the pure `QueueCounts`
invariants moved to the new `tests/test_extract_queue_sql.py` (468).
`extract_queue.py` is 443. All three under the 500-line guideline, though the
two test files have little headroom left after the review follow-ups.

A fourth near-copy of the blob-seeding helper was **deliberately not created** —
#283 named exactly that smell and was closed — so the two new DB-backed tests
live beside the existing seeder rather than in the new file.

### 2. Verification (this Mac, all extras)

- `unset VIRTUAL_ENV && uv run pytest -q` → **2358 passed, 0 skipped, 0
  failed**, no `--deselect` (2343 before; 15 new). Read section 4 before
  comparing that to any earlier handoff — the *environment* changed too.
- `unset VIRTUAL_ENV && uv run mypy src/localmail` → **Success, 140 source
  files**.
- `uv run ruff check` on every changed file → clean.
- **Every new test watched fail first**, with the fix stashed: 10 failed,
  including the plan pin as **`assert ['Seq Scan'] == []`** — the #280 defect
  itself, not a missing key.
- **Three mutations, each caught by exactly one test**: dropped `DISTINCT`,
  `IS DISTINCT FROM 1` → `<> 1`, and the misfiled guard removed. This is the
  technique that caught session 21's vacuous tests; keep using it.
- Live archive before → after captured with `time` on the identical command
  (see the table above), not reasoned about.
- Both NOTIFY gates pass: `pg_notification_queue_usage()` → `0` **and**
  `LISTEN daemon_commands` on `localmail_test` succeeds.

### 2b. Review follow-ups, folded into PR #286

A five-agent review of the stack found two real defects and four inaccurate
claims. All are fixed on the branch; the counters are unchanged.

- **`jsonb_array_elements` aborted on a non-array `attachments`.** The
  correlated form never met one — its `@>` was pushed below the lateral — so
  decorrelating removed a guard nobody had written down. `messages.attachments`
  is `JSONB NOT NULL DEFAULT '[]'` with no `CHECK`, and the raw `psycopg.Error`
  escapes `search_status`'s narrow catch, discarding the eleven healthy
  counters that the "read blobs last" ordering exists to preserve. No writer
  produces one today, so this was defence-in-depth. Fixed with
  `jsonb_typeof(m.attachments) = 'array'`; pinned by
  `test_a_malformed_attachments_row_does_not_abort_the_report`, which fails on
  all four malformed shapes without it.
- **The `SubPlan` walk missed a `LEFT JOIN LATERAL` re-correlation** — the
  likeliest accidental regression, since it keeps the join and only makes it
  per-blob. Measured: 30 loops over 30 blobs, and zero `SubPlan` hits, so the
  pin for the headline fix passed it. Added an `Actual Loops == 1` assertion
  over a seeded fixture, plus its pre-#280 negative control.
- **The `DISTINCT` rationale was wrong in five places.** It claimed a missing
  `DISTINCT` would break the partition. It does not: the fan-out multiplies
  `eligible` and the buckets equally, so the sum holds and `misfiled` stays
  `0`. Reworded everywhere, and the test is now labelled load-bearing — the
  old wording invited deleting it as redundant.
- **CLAUDE.md still justified `CLAIMABLE_TOTAL_SQL` by "that correlated
  `EXISTS`"**, which #280 deleted — so CLAUDE.md and the module gave mutually
  exclusive reasons for the same decision.
- **`~70 ms` was unsourced and self-contradicting**; it appeared nowhere else
  in the tree and sat 150 lines from a claim that the same work was "still the
  dominant cost" of a 0.97 s command. Removed. `13:04` is kept but now
  attributed to session 21's measurement of the eligibility counter *alone*,
  against the `13:28.45` whole-command figure.
- **Two mechanisms were misstated**: `work_mem` reversion is a plan-time
  estimate, not a runtime switch; and the fix does not restore the GIN index
  plan — it is one `Seq Scan on messages` paid once rather than per blob.
- Smaller: `_UNREPORTED_FIELDS` gained a field-name pin, and
  `QueueCountsInconsistent`'s docstring now covers both raise conditions.
- **The `AS misfiled` pin took two attempts, and the first was the same bug
  the review was about.** `assert "AS misfiled" in QUEUE_COUNTS_SQL` is
  satisfied by `0 AS misfiled`, i.e. the disabled form. Replacing it with
  `assert misfiled_count_sql(BUCKET_WHERE_SQL) in QUEUE_COUNTS_SQL` looked
  stronger and is a **tautology** — both sides derive from the same function,
  so they mutate together, and it passed the `0 AS misfiled` mutation cleanly.
  The shipped pin never calls the function: each bucket predicate must appear
  exactly twice in the statement, once as its own `FILTER` and once in the
  misfiled sum. Caught only by running the mutation; keep doing that.

One review claim did **not** survive checking: that dropping a bucket from the
report would leave the suite green. `test_cli_extract.py` asserts exact values
for all six `blobs_*` keys, so any drop raises `KeyError`. Left alone.

### 3. PR #288 → commit `d025f2e` — pypdf 6.14.2 → 6.15.0

Branch `chore/pypdf-6.15`, off **`main`** rather than the #286 stack, so merge
order does not matter. **Open, CI green, unmerged.** Two medium Dependabot
alerts, both resource-exhaustion in `pypdf < 6.15.0`:

| severity | advisory | patched |
|---|---|---|
| medium | large memory usage for large `/ToUnicode` streams | 6.15.0 |
| medium | long runtimes / large memory for large CID font width ranges | 6.15.0 |

Both filed **2026-08-08T22:04Z**, after session 21's handoff was frozen — which
is why every earlier handoff says "0 open alerts".

**Not purely theoretical here.** `LightweightExtractor` runs pypdf over PDF
attachments that arrive from strangers, so a crafted PDF is a plausible way to
make the extract worker chew memory or time. Bounded by
`extractor_max_blob_bytes` (50 MB) and the transient-retry cap (#153), so it
degrades to a stuck blob rather than a dead daemon — medium, not urgent.

`pyproject.toml` already declared `pypdf>=6.12.0`, so **no constraint changed**;
only the lock pinned the vulnerable version. `uv lock --upgrade-package pypdf`
resolved 193 packages and updated exactly one — three lines of `uv.lock`, no
transitive churn. `gui/`'s lockfiles are untouched (pypdf is Python-side).

Verified on **real** pypdf parsing, not stubs: `tests/test_extractor.py` builds
its PDF fixtures with reportlab and parses them back. 87 passed across the three
extractor suites; full suite 2343; mypy Success on 140 files.

### 4. Two environment findings that move the test count

Neither is a code change, and both explain numbers a future session would
otherwise mistrust.

- **The `[extraction]` extra was not actually installed on this Mac.**
  `tests/test_extractor.py::test_pdf_pipeline_options_reflect_the_configured_engine`
  — the #248 regression test that runs against **real docling** — was the
  session's one `skipped`, silently, for as long as that state lasted. `uv sync
  --all-extras` installed it; the test now runs and passes. Every count in this
  document is post-fix: **`main` is 2343/0, `fix/280…` is 2358/0**, where the
  last handoff recorded 2342/1. Nothing was wrong with the suite; the
  environment was under-provisioned, exactly as risk 17 warns and nobody had
  checked.
- **The stale-NOTIFY fault recurred mid-session**, having read clean at the
  start. Same three `LISTEN`/`NOTIFY` tests, same
  `could not access status of transaction … Could not open file "pg_xact/…"`.
  Cleared with the runbook's Option A — `launchctl bootout` the daemon, wait for
  it to actually be gone, verify **both** gates (`pg_notification_queue_usage()`
  → `0` *and* `LISTEN daemon_commands` → `LISTEN`), `bootstrap` back. Note the
  queue reading alone was **not** the tell: it read `9.5e-07`, not an obvious
  failure, while `LISTEN` errored outright.

## What's next

### 0. **Merge PRs #286, #287 and #288, then deploy both hosts**
   All three are green and unmerged; **the operator merges** (project
   convention). **#287 is stacked on #286** and retargets to `main` once #286
   lands; **#288 is independent** (off `main`, lock-only).
   - **Mac**: `git pull`, `uv sync --all-extras` — **`--all-extras`, not a bare
     `uv sync`**, see What we shipped 4 — then `launchctl kickstart -k
     gui/$UID/com.localmail.daemon` (and the serve agent).
   - **DGX**: `git pull`, `~/.local/bin/uv sync --extra mcp --extra
     extraction`, `systemctl --user restart localmail-daemon localmail-serve`.
   - **Acceptance:** `localmail search-status` returns in **about a second** on
     the Mac, reporting `blobs_eligible 9491 = 9203 + 106 + 182 + 0` and
     `blobs_claimable 0`. A daemon restart is not required for the counter (it
     is a CLI read) and `_claim_batch` is untouched by this change, so there is
     no urgency on the restart either.
   - **On the DGX the numbers are unmeasured** — its archive is smaller and the
     command was never timed there. Record what it says; that is the second
     data point #280 never had.

### 1. **#279 and #278 — the version surface** *(carried from session 20's review)*
   - **#279** is close to a one-liner: `@click.version_option(__version__,
     package_name="localmail")`. The manual's *install-verification* step tells
     users to run `localmail --version`, which currently prints a usage error —
     the worst possible place for it. Also closes a real gap: on a daemon-only
     host the version is unobtainable without starting `serve`.
   - **#278** needs a product decision first: the GUI About tab renders a
     `build_hash` that `/v1/version` has **never** emitted, so the "Server
     build" row always shows `?` — while five test files mock the field and make
     it look covered. Either emit a build hash or delete the field end-to-end.

### 2. **#285 — ruff, repo-wide** *(carried)*
   Every `# noqa: S608` in the tree is a dead directive: `ruff check --select
   S608 --ignore-noqa` reports nothing on those files, so the rule never fired.
   There is no `[tool.ruff]` config and no CI step; repo-wide `ruff check`
   reports 131 pre-existing errors. Two separable decisions (adopt ruff
   properly, or drop the directives and keep the reasoning as plain comments) —
   worth deciding once. **This PR added two more `# noqa: S608` comments** for
   consistency with their neighbours; they are equally dead.

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
   - **`cli.py` is 1906 lines**, `daemon.py` 573 — both over the 500-line
     guideline. This session did **not** touch `cli.py` (the whole change fits
     inside `extract_queue.py`), so the refactor session 21 deferred is still
     owed in full.
   - **165 `docling: File format not allowed` failures on the Mac** (of 182).
     Visible as `blobs_gave_up 182`, and now visible in **one second** rather
     than fourteen minutes — this is the cheapest it has ever been to act on.
   - **Residual implausible language labels are dominated by `ja`** (229 of the
     Mac's 350). 0.24% of labels; the confidence-floor lever was measured
     useless. If ever chased, **sample the `ja` rows first**.
   - **The DGX drops remain uninvestigated and unexplained** (risk 3).
   - **#269's suggestion 2** (blob-temp sweep off the critical path) — deferred
     in session 19; reopen only if cold-cache startups grow past tolerable.

## Open decisions & risks

1. **Three PRs are open, all green, all yours to merge.** `main` is `57ce228`.
   **#286** (`fix/280-decorrelate-blob-eligibility` @ `0f0b1aa`) is the fix;
   **#287** (`docs/session-22-handoff`) is stacked on it and carries this
   document; **#288** (`chore/pypdf-6.15` @ `d025f2e`) is independent, off
   `main`. **15 open issues**; #280 and #284 both close with #286, taking it to
   13. **2 open Dependabot alerts, both cleared by #288** — every earlier
   handoff's "0 alerts" was written before they were filed.
2. **`search-status` is fast now — stop budgeting fourteen minutes for it**
   *(changed; this was risk 2 of the last two handoffs)*. `13:28.45 → 0.97 s`
   on the 127k-message Mac archive. If it ever runs long again, that is a
   **regression**, not the known cost: check `EXPLAIN (FORMAT JSON)` for a
   `Seq Scan on messages` under a `SubPlan` before looking anywhere else.
   Risk 13's extension ("`search-status` counts as heavy DB work") is
   correspondingly withdrawn.
3. **The DGX drops are STILL UNEXPLAINED — five theories refuted, four of them
   mine** *(carried; not investigated this session)*. **Do not propose a sixth
   without a captured outage in which the host was demonstrably up throughout.**
   Triage with `journalctl --list-boots` first. **Power is not a candidate**
   (~5-day UPS). **Do not edit `/etc/wireguard/wg0.conf`.**
   **Addressing, as verified this session:** `10.0.0.3` (WireGuard) worked
   first try, repeatedly, including a multi-minute `uv sync` over SSH. Session
   19 established that the LAN address answers ping and refuses SSH, so a green
   `lan=` probe line is *not* evidence it is the DGX. **Try `10.0.0.3` first.**
4. **A single `tunnel=FAIL` probe sample is not an outage** *(carried)* —
   Starlink losing three packets on a ~900 ms path. Sustained = several
   consecutive samples.
5. **`blobs_claimable 0` on the Mac is correct, and the last handoff predicted
   otherwise** *(new)*. It expected "well above 0" because ~16.6k blobs exist
   against 9,491 eligible. But the non-allowlisted remainder was claimed and
   disposed of with `type-skipped` rows long ago, so it is not claimable — the
   worker's queue really is empty on both counts. The #216 archive shape
   (`blobs_pending 0` alongside a large `blobs_claimable`) is what a *fresh*
   image-heavy archive looks like, not a settled one.
6. **A steady non-zero `blobs_no_text` is NORMAL** *(carried — #277)*. Those
   blobs are finished, just with nothing to index; the bucket is terminal by
   design. Read it like `body_lang_declined`. **`blobs_gave_up` is the one to
   act on** — `list-failed-extractions` says why (poison-pill half only),
   `retry-failed-extractions` re-queues.
7. **`QueueCounts.__post_init__` now raises on two distinct conditions**
   *(extended — #284)*. `misfiled` is checked **before** the sum, and that
   ordering is deliberate: a misfiled row usually disturbs the sum too, and
   "do not sum" would send the reader hunting for a missing bucket instead of
   an overlapping predicate. If you add a fifth disposition, add it to
   `BUCKET_WHERE_SQL` — everything else derives. Do not relax either check.
8. **`type-skipped` is a one-way door for a widened allowlist** *(carried)*.
   Clear deliberately: `DELETE FROM attachment_text WHERE extractor =
   'type-skipped'`. **#266's whitespace-heal is a one-way door too** — what
   makes it safe is the `is_blank` gate, not the nature of the data.
9. **`--relabel` is the only destructive verb in the lang path** *(carried)*.
   Discards **every** label; archive unsearchable by `lang:` until the drain
   completes. Prompts unless `--yes`. Reach for `--retry-declined` first.
   Budget ~45 min for a 100k-row archive, and note that **`reopen_all`'s bulk
   UPDATE shows no progress in `pg_stat_activity` until it commits** — tens of
   minutes of apparent hang is expected; do **not** cancel it.
10. **`body_lang_pending` means claimable work only** *(carried)*; the
    turned-away remainder is `body_lang_declined`. A steady non-zero `declined`
    is **normal** (currently 12,182 on the Mac).
11. **CI trap** *(carried)*: any GUI admin panel that fetches on mount MUST be
    stubbed in **both** `AdminView.test.ts` and `MainView.test.ts`, or an
    unhandled promise rejection leaks while vitest still reports "passed".
12. **Do not add normalisation steps to `lang_text.py` without a measurement**
    *(carried)*. Every candidate step beyond URL-stripping measured zero.
13. **Do not run the test suite while a backfill is draining** *(carried,
    narrowed)*. Shared-cluster contention produces dozens of false failures.
    `search-status` no longer qualifies (risk 2).
14. **The macOS socket deselect is GONE — stop using it** *(carried)*. #276
    fixed it; `uv run pytest -q` with **no arguments** is the right command.
15. **The stale NOTIFY queue RECURRED mid-session and was cleared**
    *(changed)*. It read clean at the start and had taken the usual three
    `LISTEN`/`NOTIFY` tests down by the afternoon — so treat "clear at the last
    handoff" as worth nothing. Fixed with the runbook's Option A: `launchctl
    bootout gui/$UID/com.localmail.daemon`, **wait until `launchctl print` says
    the service is gone**, verify both gates, then `bootstrap` back.
    **Verify both gates, not one:** this time
    `pg_notification_queue_usage()` read `9.5e-07` — small enough to look
    healthy at a glance — while `LISTEN daemon_commands` errored outright.
    Session 19 saw the inverse (usage `0`, `LISTEN` still erroring). Neither
    reading alone is the gate.
16. **An empty `daemon_heartbeats` right after a daemon restart is normal for
    minutes** *(carried — #269/#271)*. It is the startup blob-temp sweep. Grep
    for **`blob-temp sweep done: walked=`**.
17. **`uv sync` without extras silently downgrades a host — and this Mac WAS
    downgraded** *(changed; this stopped being hypothetical)*. The
    `[extraction]` extra was missing here, so
    `test_pdf_pipeline_options_reflect_the_configured_engine` — the #248
    regression test that exercises **real docling** — had been skipping. That
    is what the "1 skipped" in every recent handoff was, and nobody looked.
    Use `--all-extras` on the Mac and `--extra mcp --extra extraction` on the
    DGX. **`uv` is not on the DGX's default non-interactive PATH** — use
    `~/.local/bin/uv` over SSH. Counts, all post-fix and all with **0 skipped**:
    `main` **2343**, `fix/280…` **2358**. A non-zero `skipped` on this machine
    now means an extra has gone missing again — check it rather than ignoring
    it. CI installs only `--extra mcp`, so its count differs by design.
18. **`ExtractorConfigurationError` subclasses `TransientExtractorError`, and
    that subclassing *is* the #248 fix — do not "clean it up"** *(carried)*.
19. **`run_embed_worker_once`'s `failure_log` defaults to the process-wide
    `embed_worker._FAILURE_LOG`, and that default is the fix** *(carried —
    #267)*. **A new looping caller should pass nothing.**
20. **No ROADMAP.md** *(carried)* — the `/nextsession` ROADMAP step is a no-op;
    slice status lives here + `docs/handoffs/` + the specs. **README was NOT
    updated this session** and did not need to be: #280 is pure performance and
    the "Reading the attachment counters" section it would touch describes
    counters that are byte-identical before and after.
21. **Secrets/ACL invariants unchanged** *(carried)*: `secrets.configure`'s pin
    kept though #245 is fixed; #246 warns on group-write; `InsecureSecretsFile`
    refuses; #239's manual tombstone retention deliberate; admin bearer tokens
    have no per-token scope (#204).
22. **Run vitest from `gui/`, not the repo root** *(carried)*.
23. **`cargo clippy --all-targets` is clean but ungated** *(carried)* — CI runs
    clippy without `--all-targets`, so `#[cfg(test)]` modules are never linted.

## Exact commands to resume

```bash
cd /Users/hherb/src/localmail
git fetch --prune origin                 # ALWAYS first
git status                               # expect clean
git branch --show-current
git log --oneline origin/main..main      # expect 0

# THREE PRs are OPEN and GREEN, awaiting your merge (What's next, 0).
# #287 is stacked on #286; #288 is independent. 15 open issues.
gh pr list
for n in 286 287 288; do gh pr checks $n; done
gh issue list --limit 20

# Dependabot: 2 open alerts, both cleared by #288. Expect 0 once it merges:
gh api repos/hherb/localmail/dependabot/alerts \
  --jq '[.[] | select(.state=="open")] | length'

# AFTER MERGING #286, deploy both hosts:
#   Mac:  git pull && uv sync --all-extras && launchctl kickstart -k gui/$UID/com.localmail.daemon
#   DGX:  ssh 10.0.0.3 'cd ~/src/localmail && git pull && ~/.local/bin/uv sync --extra mcp --extra extraction && systemctl --user restart localmail-daemon localmail-serve'

# Python test suite. No --deselect (risk 14).
# Do NOT run while a backfill is draining (risk 13).
unset VIRTUAL_ENV && uv sync --all-extras   # NOT a bare `uv sync` — risk 17
unset VIRTUAL_ENV && uv run pytest -q
#   expect: 2358 passed, 0 SKIPPED on fix/280…; 2343 on main.
#   A non-zero skip count means an extra went missing again (risk 17).

unset VIRTUAL_ENV && uv run mypy src/localmail
#   expect: Success, 140 source files

# Host health checks:
launchctl print gui/$UID/com.localmail.daemon | grep -E '^\s*(state|pid) '
psql -h localhost -p 5532 -U localmail -d localmail -c \
  "SELECT worker_kind, account_id, state, now()-last_heartbeat_at AS age
     FROM daemon_heartbeats ORDER BY worker_kind, account_id"
#   expect: 7 rows, ages under ~60 s (EMPTY during the startup sweep — risk 16)

# The attachment counters — ABOUT ONE SECOND now (risk 2):
unset VIRTUAL_ENV && uv run localmail search-status
#   expect: blobs_eligible 9491 = extracted 9203 + no_text 106
#                                 + gave_up 182 + pending 0, claimable 0
# If it takes minutes again that is a REGRESSION of #280, not the known cost:
psql -h localhost -p 5532 -U localmail -d localmail -c \
  "SELECT now()-query_start, left(query,60) FROM pg_stat_activity
    WHERE datname='localmail' AND state='active' AND pid <> pg_backend_pid()"

# NOTIFY gates — CLEAR as of this handoff; check both only if those 3 tests fail:
psql -h localhost -p 5532 -d postgres -U localmail -tAc 'SELECT pg_notification_queue_usage()'  # 0
psql -h localhost -p 5532 -d localmail_test -U localmail -c 'LISTEN daemon_commands'            # LISTEN

# The DGX — deployed this session, now at 57ce228. Use the WireGuard address;
# uv is not on its non-interactive PATH (risks 3, 17):
ssh 10.0.0.3 'systemctl --user is-active localmail-daemon localmail-serve localmail-wgprobe'
ssh 10.0.0.3 'cd ~/src/localmail && git log --oneline -1'   # 57ce228 until you deploy #286
tail -5 ~/localmail-probe/tunnel-probe.log   # expect lan=ok(3/3)@<addr>

# Frontend — only if you touch gui/ (MUST run from gui/ — risk 22):
cd gui && npm run check && npm test && npm run build && cd ..
cd gui/src-tauri && cargo test && cargo clippy --locked -- -D warnings \
  && cargo clippy --all-targets -- -D warnings && cd ../..
```

`main` tip is **`57ce228`** (PR #282). This session's work is **`0f0b1aa`** on
`fix/280-decorrelate-blob-eligibility`, **open as PR #286, CI green, not
merged**. Latest migration **`0035_messages_body_lang_attempted_at.sql`**; next
free slot `0036_*.sql` (this session adds none). **Open issues: 15** (13 after
#286 merges closes #280 and #284). Dependabot: **2** open alerts, both medium,
both `pypdf < 6.15.0`, **both cleared by PR #288** (`d025f2e`, off `main`, CI
green).
