# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-09-07 (session 45).** `main` was **`75cdd0d`** at the
> start. This session opened **one PR** on `fix/361-nonfinite-rerank-scores`,
> closing **#361** — the review-round follow-up to #359.
>
> **This file was one session stale when the session opened, and that is a new
> failure mode.** `main` carried `75cdd0d` (#359) while the newest handoff
> commit was `c10b3f1` (#355): **session 44 landed its code and its CLAUDE.md
> entry but never updated NEXT_SESSION.md or `docs/handoffs/`**. So the live
> handoff described a tree two merges behind, predicted 21 issues where there
> were 22, and quoted 3372 where `main` collects **3407**. Risk 3 again, from a
> direction it had not come before — not "the operator changed things after the
> handoff was written" but "the handoff was never written". **`git log
> --oneline -5 -- NEXT_SESSION.md` against `git log --oneline -5` is the
> check**: if the newest commit touching this file is not the newest commit,
> a session ended without handing off.
>
> **#361 was filed as a review finding and it reproduced exactly as written.**
> With `A=NaN, B=1.0, C=3.0, D=2.0`, **9 of the 24** input permutations
> mis-order the three *finite* rows. The issue's "pre-existing, not introduced
> by #359" claim was **verified rather than taken**: the pre-#359
> `key=lambda x: x[1]` mis-orders the same 9 of 24.
>
> **Four findings this session, all from running things rather than reading
> them.**
>
> **(1) The HTTP consequence is the opposite of the obvious guess.** I assumed
> a surviving NaN reached the wire as the bare token `NaN` — invalid JSON,
> which `JSON.parse` does reject (checked in node). It does not: Starlette's
> `JSONResponse` renders with `allow_nan=False`, so `/v1/search` **raises**
> `ValueError: Out of range float values are not JSON compliant` from the
> *response renderer*, after the search has already succeeded, with nothing
> naming the reranker. The corruption is silent on the Searcher, CLI and MCP
> paths and a mystery 500 on HTTP. The guess is corrected in place in
> CLAUDE.md rather than quietly dropped.
>
> **(2) A `NamedTuple` return here is a silent failure, a dataclass is a loud
> one.** `FiniteScores` carries `(scores, replaced)`. As a two-field
> `NamedTuple` it is an iterable of length 2, so a caller who forgets
> `.scores` and binds the container is **accepted** by `_build_results`'
> `zip(hydrated, scores, strict=True)` whenever the pool happens to hold two
> rows — a page ordered by a list and an int. A frozen dataclass is not
> iterable, so the same slip is a `TypeError` at the first zip. Found while
> writing the shape test, not after.
>
> **(3) The filtered assertion alone is not enough, and a mutation proved
> it.** The property #361 asks for is "the finite rows keep their relative
> order". Substituting some *other* constant for the NaN (mutation: `99.0`)
> keeps `C, D, B` intact and moves only the NaN row — so the filtered
> assertion passes for it. The permutation test asserts the **full** page
> order as well; that is the assertion that catches it.
>
> **(4) The DGX's `lan=FAIL` is the Mac, not the DGX.** The probe has read
> `lan=FAIL(0/3)@none` continuously since **2026-08-26T20:13:34Z** — 12 days —
> which reads as an outage. It is not one. The Mac is on **192.168.50.86** and
> the DGX on **192.168.68.83**: *different subnets*, so the LAN leg cannot work
> from here at all. The probe's hardcoded `LAN_CANDIDATES` is *also* stale
> (`.83` is not in it), but fixing that alone would not fix this. **This does
> not explain the historical drops** (those were `tunnel=FAIL`, the inverse
> shape) — risk 34 stands. It removes one false signal, nothing more.
>
> **Open issue count is 22, dropping to 21 on merge. Dependabot stays 0.**

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
SPDX headers in `src/localmail/`; **not** in `gui/`).

## What we shipped this session

Three commits plus this handoff, one PR. **`gui/` was not touched**, so the five
frontend gates were correctly *not* run (risk 35 does not apply this session).

### `3846ff2` — a non-finite rerank score never reaches the page's sort key

`relevance_key`'s first slot is a `float` straight from the cross-encoder, and
a NaN makes the comparator **inconsistent** — every comparison against NaN is
`False`, so neither of a pair is "less than" the other and Timsort's output
depends on the order the pool arrived in. The damage is not confined to the NaN
row.

- The rule is the pure `relevance_order.finite_scores`, and it lives **there**
  rather than beside its caller because it is the slot-0 counterpart of what
  `date_key` does for slot 1: keep a value that has no place in a total order
  out of one. A total order is only as total as its slots.
- **Per row, not per batch** — a *raise* says the reranker is unusable and the
  whole batch already degrades; one bad *value* says one score is unusable, and
  discarding the scores the model got right is a worse answer.
- **`math.isfinite`**, so ±inf goes the same way. An infinity orders
  consistently and corrupts nothing, but it pins its row to one end of every
  page, and the remedy is identical. "NaN is bad, inf is tolerable" would be
  two predicates for one question.
- **The guard sits outside `_safe_rerank`'s `try`.** Its only raise is a length
  mismatch, already a hard failure downstream at the same strict `zip`;
  catching it there would quietly convert that into a degrade — a different fix
  for a different problem, which nothing asked for.
- **`_cut_pool` needs no equivalent**: it keys on `fused.rrf_score`, finite by
  construction. Checked, not assumed.

### `tests:` — the positive-control docstrings claimed more than they held

*(The last commit on the branch; it carries this handoff's own edit, so it
cannot name its own SHA — the same reason earlier handoffs write "and the
handoff commit".)*

Both said a rule that substituted *unconditionally* "would satisfy every
assertion above". Measured: it does not — the first assertion in each file
catches that mutation too, because its fallback values differ from its inputs.
The control genuinely guards the **parametrized** test, whose inputs are all
non-finite. The distinction is worth the words: the other test catches it by
luck of the fixture's numbers, this one by construction. A justification that
overstates its own reach is how a later reader deletes a pin that matters.

### `c29b528` — README + CLAUDE.md

README's `reranker_enabled` bullet gains the operator half — the only person
who can reach this is the one who flipped it — with the WARNING to grep for and
what a steady non-zero count means. CLAUDE.md records the entry in place beside
#359's, **adds `relevance_order.py` to the Layout tree** (#359 left it out),
and corrects finding (1) rather than shipping the guess.

### Verification (this Mac, all extras)

- **Both refs measured in this session** (risk 7), clean tree at each:

  | gate | `main` @ `75cdd0d` | branch @ `c29b528` |
  |---|---|---|
  | pytest collected | **3407** | **3422** |
  | pytest run | — | **3422 passed, 2 warnings, 247.8 s** |

  The +15 is 8 (`test_rerank_nonfinite_scores.py`) + 7 (`test_relevance_order.py`),
  counted per file, not derived.
- The 2 warnings are the pre-existing `websockets` deprecations (**#25**), so
  **#321's acceptance signal still holds**.
- `mypy src/localmail` → Success, **154** files. `ruff check src/localmail/`
  → **10**, the unchanged #285 baseline (none from the new files).
- **11 mutations, 11 caught.** One (`FiniteScores` → `NamedTuple`) did **not
  apply** on the first attempt — it needed the `typing` import too, and the
  collection error it produced reads exactly like a caught mutation. Risk 8,
  fired again. Re-applied properly, it is caught by the one test written for
  it.
- **Live end-to-end against the 129,063-message archive**, with a counterfactual
  in the same script: a NaN-injecting reranker through the real
  `create_searcher` → `Searcher.search` path returns 8 finite, non-increasing
  scores and one WARNING (`returned 1 non-finite score(s) of 100`); with
  `finite_scores` monkeypatched to a no-op, the same query returns
  `[1.0, 0.99, nan, 0.97, …]`. The fix is load-bearing on the real path, not
  only in unit tests.
- **There is still no ROADMAP.md** — that `/nextsession` step remains a no-op
  (risk 39, re-confirmed an **eleventh** time).
- Machine was **not** carrying orphaned burners (risk 5 checked first), but
  load ran **9.1 → 11.7** during the session, so 247.8 s is not comparable to a
  quiet-machine figure.

## What's next

### 0. **Merge the PR, then check the issue actually closed**
   **You merge** (project convention). The body uses `Closes #361`. Risk 2 says
   verify with `gh issue list` afterwards — **22 → 21**.
   - **No `uv.lock` change**, so neither host needs a dependency sync. The
     Mac's editable install follows the tree, so `git checkout main` there
     after merging (risk 29).
   - **No migration.** Latest is `0036_api_keys.sql`; next free slot
     `0037_*.sql`.
   - **Neither daemon was restarted** this session. Nothing here changes the
     wire, so there is no reason to restart unless you want the new WARNING
     available — and it can only fire with `reranker_enabled = true`, which
     neither host sets.

### 1. **The DGX is six merges behind, and its probe signal is stale**
   `ssh 10.0.0.3 'cd ~/src/localmail && git log --oneline -1'` reads
   **`815e74b`** against `main`'s `75cdd0d` — it has missed #336, #338, #339,
   #341, #342, #346, #351, #352, #355, #359. All three services are `active`.
   - **Acceptance:** `ssh 10.0.0.3 'cd ~/src/localmail && git pull && ~/.local/bin/uv sync --extra mcp --extra extraction'`
     then restart the two units, and `/v1/version`'s `build_hash` matches.
     **Never a bare `uv sync`** (risk 28).
   - **Separately**, decide what to do about `tunnel-probe.sh`'s
     `LAN_CANDIDATES` — it is your file, so this session did not touch it. As
     it stands the LAN leg reports `FAIL` permanently and carries no
     information. Either add `192.168.68.83` *and* accept it will still fail
     while the Mac sits on `192.168.50.0/24`, or drop the LAN leg and let the
     probe report the tunnel alone.

### 2. **The #355 review-round leftovers** *(new, carried from session 44)*
   **#358** (`run_search -> dict[str, Any]`, so a new wire field is a bare
   string key with a MagicMock trap behind it) · **#357** (the sort rules take
   a bare `str` whose "already `parse_query`'d" precondition lives only in
   prose). Both were filed *by* the #355 round and both are about the same
   thing this session's `FiniteScores` decision is about: making the shape
   carry the contract.

### 3. **The #322/#332 review-round leftovers** *(carried)*
   **#330** (`SortOrder`/`SortMode` restated in three wire layers) · **#327**
   (`CursorPlan` carries two fields its pool-mode consumer must ignore) ·
   **#328** (the page-cache entry is an untyped dict).

### 4. **The #317 API-keys review round** *(carried)*
   **#319** (`ApiKeyNotFound` carries three meanings; the panel reports a
   security refusal as success) · **#318** (the `api_key_name` ⟺ `is_service`
   pairing is unenforced in the schema; **next free migration slot is
   `0037_*.sql`**) · **#320** (admin panel routes do blocking DB IO on the
   event loop).

### 5. **#340 — the harness lock proves a lock was taken, not which database**
   *(carried)*. The AST rule compares call *positions* and never arguments, so
   locking one database while working against another passes. Latent — all five
   harnesses use one `dsn`. A correct check needs parameter-flow analysis.

### 6. **#305 — `--version` dies on a missing third-party dependency** *(carried)*
   `cli.py` imports the daemon at module scope. **Acceptance:** blocking
   `sqlparse` on `sys.meta_path` leaves `localmail --version` printing its line
   and exiting 0. **Do it with the `cli.py` refactor, not before** — `cli.py` is
   **2177 lines**.
   - **#331's point 2 lives here**: widen `cli.py`'s search catch to
     `SearchArgumentRefused`, never to bare `ValueError`.
   - **#350 adds**: the CLI is the third consumer of the wire join, and since
     #350 it is one call — `exc.wire_message()` — not an f-string to restate.
   - **#348 adds**: a `--sort`/`--sort-order` flag makes the Searcher's
     plain-`ValueError` membership check reachable from the CLI, where it would
     traceback. `cli.py` calls `create_searcher(...).search(...)` directly, so
     `run_search`'s gate does **not** cover it. That makes **two** family
     members reachable, not four — the two keyset ones need a *cursor* flag.
   - **#345/#353 add a fourth and fifth**: `cli.py`'s search output shows no
     ordering at all, so it has `page.sort_applied` *and* `page.rankable` to
     print if the refactor wants them.

### 7. **#285 — ruff, repo-wide** *(carried)*
   **10** pre-existing errors in `src/`, plus **1 F841** in
   `tests/acceptance/run_recall_eval.py`. 9 dead `# noqa: S608`, no
   `[tool.ruff]`, no CI step. **The config and the CI step are one decision,
   and it is the operator's.**

### 8. **Admin GUI phase 5 — Users & ACL panel** *(carried)*
   `/v1/admin/users` is already `require_admin()` — **no backend work.**
   Mirror [serve/admin/users_panel_router.py](src/localmail/serve/admin/users_panel_router.py),
   follow the Daemon-panel shape, surface the two lock-out guards as 409s, and
   **stub the new API module in both `AdminView.test.ts` and `MainView.test.ts`**
   (risk 35).

### 9. **Remaining robustness backlog** *(carried)*
   **#218** · **#226** · **#225 / #227** · **#200 / #211 / #208** · **#206** ·
   **#204** · **#25**.

### 10. **Smaller, deliberately not done** *(carried)*
   - **`blobs_gave_up 182`** on the Mac, of which the handoff records ~165 as
     docling failures. Unchanged this session and **not re-measured**; that
     breakdown is a carried hypothesis, not a finding.
   - **Residual implausible language labels dominated by `ja`** (~0.24%); the
     confidence-floor lever was measured useless. **Sample `ja` first.**
   - **A session-22 stash is still on the stack** (`stash@{0}`); its content is
     on `main`, so `git stash drop` it if you want the tree tidy.
   - **A stray SDD workspace** at `.superpowers/sdd/`. Git-ignored scratch.

## Open decisions & risks

1. **One PR is open and yours to merge.** On `fix/361-nonfinite-rerank-scores`,
   based on `main` (`75cdd0d`), closing **#361**. **22 open issues, dropping to
   21. Dependabot stays 0.**
2. **A merge does NOT close issues its subject merely names** *(carried)*. Use
   `Closes #N` in the **PR body** and **check `gh issue list` after the merge**.
3. **THIS FILE IS NOT THE AUTHORITY — `git` and `gh` are** *(carried, and it
   paid in a NEW way)*. This time it was not stale because the operator moved
   afterwards; it was stale because **session 44 never wrote one**. Open every
   session with `git fetch --prune && git log --oneline -1 origin/main`,
   `gh pr list`, `gh issue list`, the Dependabot query — and **re-measure
   `main` yourself**. Add: **`git log --oneline -3 -- NEXT_SESSION.md`** and
   compare its newest commit against `git log --oneline -1`. A gap means a
   session ended without handing off, and everything below is that much older.
4. **Finish the `/nextsession` ritual, including the archive copy.** Session 44
   updated CLAUDE.md and stopped. `docs/handoffs/` is the audit trail and it is
   missing an entry for #359; this session cannot reconstruct it and did not
   try.
5. **Base every branch on `main`; put a session's code and its handoff in ONE
   PR** *(carried)*. Session 25's stacked handoff PR merged into an
   already-merged branch and was lost silently.
   - The stranded-branch check is **NOISY**: `git log --oneline main..origin/<b>`
     is non-empty for every squash-merged branch. Signal = non-empty on a branch
     whose PR merged *recently*, confirmed by a **content** diff
     (`git diff --stat main origin/<branch>` empty ⇒ it landed).
6. **A machine that is inexplicably slow may be carrying someone else's
   garbage** *(carried)*. Check
   `ps -eo pid,ppid,etime,time,command | grep "[w]hile :"` **before**
   concluding a change made things slow. Clean this session — but **load ran
   9.1 → 11.7**, so **do not compare 247.8 s to a quiet-machine figure**.
7. **In zsh an unquoted `$VAR` does not word-split** *(carried)*. `uv run
   pytest -q $T` runs **zero** tests and prints `no tests ran in 0.00s`, which
   reads as benign. Use `${=T}`, and **always run the unmutated baseline
   through the same harness first** — done here, twice.
8. **Measure both refs in the session that reports them** *(carried)*. Held.
   `main`'s number is a real `--collect-only` run on a clean tree, not a
   carried figure — the carried one was wrong by 35.
9. **A MUTATION THAT DOES NOT APPLY LOOKS EXACTLY LIKE ONE THAT IS CAUGHT**
   *(carried, and it fired again)*. The `NamedTuple` mutation needed an import
   the anchor did not carry; it produced a **collection error**, which the
   harness's `tail -1` reports as `3 errors in 0.19s` — not obviously distinct
   from a failure. **Read what failed, not the count.** The helper is in the
   resume commands below.
10. **"It's only a backstop" / "it's unreachable" / "verified" is a claim to
    test** *(carried)*. Two of this session's four findings came from testing a
    claim I had just written down.
11. **Write the justification down, then try to refute it** *(carried, and it
    paid)*. The HTTP-consequence paragraph was written as "a bare `NaN` token
    on the wire", then refuted by rendering one through the real
    `JSONResponse`. It ships as the corrected version.
12. **A dependency floor that a vulnerable version satisfies is not a floor**
    *(carried)*. **Read `vulnerable_version_range` against `uv.lock`, never the
    declared floor.** `uv lock --dry-run` is read-only; `uv sync --dry-run` is
    **not**.
13. **NEVER run two pytest sessions against one test database** *(carried —
    enforced since #336; since #337 the acceptance harnesses are in the same
    guard)*. The second **waits**. To run both, give one its own
    `LOCALMAIL_TEST_DSN`. A harness refused by the lock exits **3**, distinct
    from the **1** an eval returns when it fails its own gates.
14. **When reverting a mutation, restore from a file copy — never `git
    checkout`** *(carried, sessions 23–45)*. Used 11 times this session, with a
    re-snapshot after the mid-session refactor.
15. **A MagicMock attribute reaches the wire as garbage, and the garbage is not
    a constant** *(carried — #345/#353)*. `jsonable_encoder` serialises an
    unset auto-attribute rather than raising: `{}` bare, `[]` through the
    route's `-> dict[str, Any]`. **When you add a wire key, assert it through
    the real transport**, set it explicitly on every mock page, and make the
    shared guard **structural** (type/membership). **No wire key was added this
    session**, so nothing new is exposed here — but #358 is the open issue
    about the shape that keeps producing it.
16. **A shape that unpacks is a shape that can be mis-bound** *(new — #361)*.
    A two-field `NamedTuple` whose first field is a list is a footgun: forget
    the field name and you get an iterable of the right *kind*, which a
    `zip(..., strict=True)` accepts whenever the other side happens to be the
    same length. Prefer a frozen dataclass for a multi-field result unless
    unpacking is the point — the codebase already does this
    (`LangDetectPass`, `SweepOutcome`, `RotateResult`, `ConsumeResult`).
17. **A NaN is not a value that sorts badly; it is a value that breaks the
    comparator** *(new — #361)*. Every comparison against NaN is `False`, so
    Timsort's result is a function of input order and rows with sound scores
    move. **Never assume the blast radius of a bad float is the row it is on** —
    measure it over permutations. And on HTTP the symptom is not a bad page but
    a **500 from the response renderer** (`allow_nan=False`), *after* a
    successful search.
18. **`run_search` treats `allowed_account_ids=None` AND `[]` as
    grant-nothing** *(carried — #345)*. Both short-circuit before the Searcher.
    **A wire test that expects rows must pass a real grant**, and should assert
    `results` is non-empty.
19. **A test module must import `localmail.serve.app` at MODULE scope**
    *(carried — #321)*. An AST scan plus a teardown re-check enforce it.
20. **When a guard is an AST rule, mutate BOTH branches of every predicate**
    *(carried)*. Related: **never assert a substring the message's own remedy
    text contains**, and **never compare a constant against itself**. Applied
    here: the log assertion was sharpened from `"2" in message` to the whole
    rendered fragment before it was trusted.
21. **Distinguish a surviving mutation from an EQUIVALENT one** *(carried —
    #353)*. **Record the proof next to the code**; a reader who conflates the
    two deletes pins that are load-bearing. None this session — all 11 applied
    mutations were caught.
22. **Verify host revisions; do not infer them** *(carried, and it paid)*.
    `ssh 10.0.0.3 'cd ~/src/localmail && git log --oneline -1'` says
    **`815e74b`** — six merges behind. The previous handoff's "both hosts left
    untouched" was true and is not the same as "both hosts are current".
23. **A new Searcher guard over a stated argument must subclass
    `SearchArgumentRefused`, be declared in `argument_errors.py`, and be raised
    before any IO** *(carried — #344, #347, #349)*. **`finite_scores` is not
    one of these** and must not be made one: it refuses nothing the *caller*
    stated — it sanitises a *backend's* output — so it has no wire audience
    and maps to no 400.
24. **Membership outranks every other guard, at both layers** *(carried — #348)*.
25. **A keyset predicate must be a row comparison, in BOTH directions**
    *(carried — #75, #322, #323)*. Written by mistake **twice**.
    `tests/test_searcher_sort_order_plan.py` keeps three negative controls —
    **do not "tidy" them away**.
26. **The undated block is where the two directions genuinely differ**
    *(carried)*. **Do not "restore symmetry"**, and **do not "fix" a short page
    by restoring `OR expr IS NULL`**.
27. **A cursor identifies a position, not a query — with one enforced
    exception** *(carried — #326)*.
28. **Never state a `sort` the server cannot serve** *(carried — #308, #311,
    #324)*, **never render one it did not run** *(carried — #345)*, and **never
    infer availability from the ordering** *(carried — #353)*.
29. **`sort` and `sort_order` resolve from the query, not from a constant**
    *(carried — #324)*. `sort_axes.resolve_sort` is the one authority, and
    since #353 it asks `is_rankable` rather than repeating the test.
30. **The stale NOTIFY queue recurs, and gate 1 lies** *(carried)*. **Check
    both gates before reaching for the runbook.**
31. **`uv sync` without extras silently downgrades a host** *(carried)*. Use
    `uv sync --all-extras` (Mac) / `--extra mcp --extra extraction` (DGX).
    **`uv` is not on the DGX's non-interactive PATH** — use `~/.local/bin/uv`.
    **A branch checkout re-resolves the venv** — re-sync after any checkout you
    intend to measure against.
32. **The Mac's launchd daemon runs an EDITABLE install** *(carried)*. It
    executes whatever it imported at start; a checkout moves the tree under it
    without reloading. **Currently on `fix/361-nonfinite-rerank-scores`** —
    check out `main` after merging.
33. **`search-status` is sub-second** *(carried, re-measured)*. 0.30 s user,
    1.9 s wall including `uv run` startup. If it runs long that is a
    **regression of #280**.
34. **`blobs_claimable 0` alongside `blobs_pending 0` is the SETTLED shape**
    *(carried, confirmed)*. The four buckets sum:
    `9283 + 106 + 182 + 0 = 9571 = blobs_eligible`. A steady non-zero
    `blobs_no_text` is **normal** (#277); **`blobs_gave_up` is the one to act
    on**.
35. **`uv run pytest -q` with NO arguments is the right command** *(carried)*.
    **Do not run the suite while a backfill is draining.**
36. **An empty `daemon_heartbeats` right after a restart is normal for minutes**
    *(carried — #269/#271)*. Grep for **`blob-temp sweep done: walked=`**.
    Seven rows, all under 30 s, at the end of this session.
37. **The DGX drops are STILL UNEXPLAINED — five theories refuted** *(carried)*.
    **Do not propose a sixth without a captured outage in which the host was
    demonstrably up throughout.** **Do not edit `/etc/wireguard/wg0.conf`.**
    The `lan=FAIL` signal is now explained (finding 4) and is **not** one of
    those drops — it is the two hosts being on different subnets.
38. **CI trap** *(carried)*: any GUI admin panel that fetches on mount MUST be
    stubbed in **both** `AdminView.test.ts` and `MainView.test.ts`.
    **Run vitest from `gui/`, not the repo root.** `cargo clippy --all-targets`
    is clean but **ungated** — CI runs clippy without it. **Not exercised this
    session; `gui/` was untouched.**
39. **`type-skipped` is a one-way door for a widened allowlist** *(carried)*.
    **#266's whitespace-heal is a one-way door too.** **`--relabel` is the only
    destructive verb in the lang path**; reach for `--retry-declined` first.
40. **Secrets/ACL invariants unchanged** *(carried)*.
41. **CI reports `1 skipped` on Linux; macOS reports 0** *(carried)*. It is
    pre-existing and still unidentified. Expect CI here to read
    **3421 passed, 1 skipped**.
42. **No ROADMAP.md** *(carried, re-confirmed an eleventh time)* — that
    `/nextsession` step is a no-op. **README and CLAUDE.md were both updated**
    this session; README genuinely needed it (a new operator-visible WARNING).
43. **A green local run is still not evidence** *(carried)*. Push and let CI
    decide.

## Exact commands to resume

```bash
cd /Users/hherb/src/localmail
git fetch --prune origin                 # ALWAYS first
git status && git branch --show-current
git log --oneline -1 origin/main         # THE authority, not this file's header

# RISK 3 — did the LAST session actually hand off? Compare these two.
git log --oneline -1
git log --oneline -1 -- NEXT_SESSION.md  # a gap = a session ended without one
ls -t docs/handoffs/ | head -3

# RISK 2 — after any merge, CHECK THE ISSUE ACTUALLY CLOSED.
gh pr list
gh issue list --limit 40                 # 22 open; the PR should take it to 21
gh issue view 361 --json state --jq .state

# RISK 12 — expect ZERO, still. Confirm, don't assume.
gh api repos/hherb/localmail/dependabot/alerts \
  --jq '[.[] | select(.state=="open")] | length'      # expect 0
# If a new one appears, read its range against THE LOCK, never the floor:
gh api repos/hherb/localmail/dependabot/alerts \
  --jq '.[] | select(.state=="open") | "\(.security_advisory.severity)\t\(.dependency.package.name)\t\(.security_vulnerability.vulnerable_version_range)\tfixed in \(.security_vulnerability.first_patched_version.identifier)"'
grep -A1 '^name = "<pkg>"' uv.lock

# RISK 6 — BEFORE trusting any timing, check for orphaned CPU burners
#          AND read the load. This session ran at 9-12.
ps -eo pid,ppid,etime,time,command | grep "[w]hile :; do :; done"
uptime

# RISK 5 — stranded-branch shortlist (noisy: every squash-merged branch shows).
for b in $(git branch -r --format='%(refname:short)' | grep -v 'origin/main\|HEAD'); do
  n=$(git log --oneline main.."$b" | wc -l); [ "$n" -gt 0 ] && echo "$b: $n commits not on main"
done
git diff --stat main origin/<branch>     # EMPTY = landed, not stranded

# AFTER MERGING — the Mac tree only; uv.lock did NOT change this session.
#   git checkout main && git pull        # editable install follows the tree (risk 32)

# Python suite. NEVER a bare `uv sync` (risk 31).
unset VIRTUAL_ENV && uv sync --all-extras
unset VIRTUAL_ENV && uv run pytest -q
#   macOS: expect 3422 passed, 0 failed, 0 skipped, and **2 warnings**.
#   THOSE 2 ARE #321's ACCEPTANCE SIGNAL: both are the pre-existing websockets
#   DeprecationWarnings (#25). A leaked pool is now a FAILING test whose message
#   reads "cannot join current thread"; the test it names is arbitrary (the GC
#   picks it), the message is the diagnosis.
#   LINUX/CI: expect 3421 passed, 1 SKIPPED; pre-existing (risk 41).
#   MEASURE BOTH REFS IN THIS SESSION (risk 8) — no DB needed:
unset VIRTUAL_ENV && uv run pytest --collect-only -q | tail -2   # 3422 here
git checkout main && unset VIRTUAL_ENV && uv run pytest --collect-only -q | tail -2
#   main @ 75cdd0d = 3407.  Assert `git status --short` is EMPTY at each.
#   DO NOT trust this file's number for main — the last one was stale by 35.
unset VIRTUAL_ENV && uv run mypy src/localmail    # expect Success, 154 files
unset VIRTUAL_ENV && uv run ruff check src/localmail/ | tail -2   # expect 10 (#285)

# #361, verified directly:
unset VIRTUAL_ENV && uv run pytest -q tests/test_rerank_nonfinite_scores.py \
                                     tests/test_relevance_order.py \
                                     tests/test_reranker.py
#   expect 36 passed in well under a second

# #361 END TO END against the LIVE archive, WITH its counterfactual. The second
# block is the point: it is the same query with the guard removed.
unset VIRTUAL_ENV && uv run python - <<'PY' 2>&1 | grep -v "couldn't stop\|hint:"
import math, psycopg
import localmail.search.searcher as S
from localmail.config import load_config
from localmail.search import create_searcher
from localmail.search.relevance_order import FiniteScores
cfg = load_config()
with psycopg.connect(cfg.database.dsn) as c:
    ids = [r[0] for r in c.execute("SELECT id FROM accounts ORDER BY id").fetchall()]
class _NaNReranker:
    name, model = "fastembed", "live-probe/nan-on-row-2"
    def rerank(self, query, candidates):
        out = [1.0 - i * 0.01 for i in range(len(candidates))]
        if len(out) > 2:
            out[2] = float("nan")
        return out
def probe(label):
    s = create_searcher(cfg=cfg); s._reranker = _NaNReranker()
    page = s.search("invoice", allowed_account_ids=ids, page_size=8)
    sc = [round(r.score, 4) for r in page.results]
    print(f"{label:12} scores={sc}")
    print(f"{'':12} all finite={all(math.isfinite(x) for x in sc)}  "
          f"non-increasing={all(a >= b for a, b in zip(sc, sc[1:]))}")
    s._pool.close()
probe("WITH fix")
S.finite_scores = lambda scores, *, fallback: FiniteScores(scores=scores, replaced=0)
probe("WITHOUT fix")
PY
#   expect: WITH fix -> all finite=True, non-increasing=True, plus one WARNING
#           "returned 1 non-finite score(s) of 100".
#           WITHOUT fix -> a `nan` in the middle of the page.

# RISK 7 — the mutation harness MUST word-split. zsh does not do it for you:
T="tests/test_rerank_nonfinite_scores.py"
uv run pytest -q ${=T}       # ${=T}, NOT $T. $T runs ZERO tests and says
                             # "no tests ran in 0.00s", which reads as benign.

# RISK 9 — the mutation helper. It REFUSES a non-unique anchor and prints where
# the edit landed. It CANNOT tell you the mutation needed an import it did not
# add — read WHICH test failed, and treat "N errors" as "did not apply".
cat > /tmp/mutate.py <<'PY'
"""Apply one mutation, report WHERE it landed, never guess."""
import ast, pathlib, sys
path, old, new = sys.argv[1], sys.argv[2], sys.argv[3]
p = pathlib.Path(path); s = p.read_text(); n = s.count(old)
if n != 1:
    sys.exit(f"ANCHOR MATCHED {n} TIMES — mutation NOT applied: {old[:70]!r}")
line = s[:s.index(old)].count("\n") + 1
p.write_text(s.replace(old, new))
if p.suffix == ".py":
    tree = ast.parse(p.read_text())
    o = [x for x in ast.walk(tree) if isinstance(x, (ast.FunctionDef, ast.ClassDef))
         and x.lineno <= line <= (x.end_lineno or 0)]
    where = (max(o, key=lambda x: x.lineno).name + "()") if o else "<module>"
else:
    where = repr(s.split("\n")[line - 1].strip()[:60])
print(f"MUTATED {path}:{line} at {where}")
PY
#   Snapshot to a scratch dir first and restore from THERE (risk 14) —
#   `git checkout <file>` wipes your own uncommitted edits in that file.
#   RE-SNAPSHOT after each GREEN, not once at the start.

# RISK 13 — the test-database lock covers harnesses too (#336 + #337).
# If a run seems to hang at startup, look for this line; it is not a fault:
#   "waiting for another test run to release the test database ..."
#   LOCALMAIL_TEST_DSN=postgresql://localmail:local%40%40mail@localhost:5532/localmail_test2 uv run pytest -q

# If EXACTLY the three LISTEN/NOTIFY tests fail, CHECK BOTH GATES (risk 30):
psql -h localhost -p 5532 -d postgres -U localmail -tAc 'SELECT pg_notification_queue_usage()'
psql -h localhost -p 5532 -d localmail_test -U localmail -c 'LISTEN daemon_commands'

# Host health (Mac):
launchctl list | grep -i localmail
psql -h localhost -p 5532 -U localmail -d localmail -c \
  "SELECT worker_kind, account_id, state, now()-last_heartbeat_at AS age
     FROM daemon_heartbeats ORDER BY worker_kind, account_id"
#   expect 7 rows, ages under ~60 s (EMPTY during the startup sweep — risk 36)

unset VIRTUAL_ENV && uv run localmail search-status    # UNDER A SECOND (risk 33)
#   Check the SHAPE, not the literals: the four buckets must sum to
#   blobs_eligible, and claimable must equal pending.

# The DGX (risk 22 — verify, never infer). IT IS SIX MERGES BEHIND.
ssh 10.0.0.3 'systemctl --user is-active localmail-daemon localmail-serve localmail-wgprobe'
ssh 10.0.0.3 'cd ~/src/localmail && git log --oneline -1'   # was 815e74b
ssh 10.0.0.3 'curl -sk https://10.0.0.3:8443/v1/version'    # binds 10.0.0.3, NOT localhost
tail -3 ~/localmail-probe/tunnel-probe.log
#   lan=FAIL(0/3)@none since 2026-08-26 is NOT a DGX fault (finding 4):
ssh 10.0.0.3 "ip -4 -o addr show scope global | awk '{print \$2, \$4}'"   # 192.168.68.83
ipconfig getifaddr en0                                                    # 192.168.50.86
#   Different subnets. tunnel=ok is the signal that matters.

# Frontend — NOT run this session (gui/ untouched). Required only if you touch
# gui/, and then from gui/, not the repo root (risk 38):
# cd gui && npm run check && npm test && npm run build && cd ..
# cd gui/src-tauri && cargo test && cargo clippy --locked -- -D warnings \
#   && cargo clippy --all-targets -- -D warnings && cd ../..
```

`main` tip at session start was **`75cdd0d`**. This session left **one PR** open
on `fix/361-nonfinite-rerank-scores` — `3846ff2` (fix + tests), `c29b528`
(README + CLAUDE.md) and the handoff commit — closing **#361**. Latest migration
**`0036_api_keys.sql`**; next free slot `0037_*.sql` (this session adds none).
**Open issues: 22**, dropping to **21** on merge. **Dependabot: 0.**
