# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-09-20 (session 51).** `main` was **`f9fa3b5`** in the
> last handoff; the operator merged **both** of that session's PRs between
> sessions, so `main` started this one at **`046ad0a`**.
>
> This session:
> - **deployed `046ad0a` to both hosts.** Both report **`api_minor: 3`**,
>   `build_hash: 046ad0a`, anyio **4.15.1**, `NRestarts=0`. Dependabot is
>   down to **#71** alone.
> - met **slice E's live acceptance** against the Mac archive;
> - closed the **three issues the review of #388 filed** — #390, #391, #392 —
>   in **PR #393**.
>
> **Open issues: 40** (37 + #390/#391/#392, which #393 closes → **37** after
> merge). **Dependabot: 1** (#71 `accelerate`, left open on purpose).
>
> **Three things to know before you touch anything.**
>
> **(1) Nothing is left of the kastellan slice order but C.** E is deployed.
> Slice C (`/problems/token-expired`) is the last one, and the previous
> handoff already called it the lowest-value slice — API keys never expire,
> so nothing kastellan holds can hit it. **Ask kastellan whether it still
> wants it** before building it.
>
> **(2) Telling kastellan is still not done, and is still yours.** All three
> gates are live on both hosts now. The message should say:
> - `?headers=list` (gate on `api_minor >= 1`);
> - attachment index addressing and paging (`>= 2`), and **never** compute
>   the next offset from `text.length`;
> - `fields` and `snippet_chars` (`>= 3`).
>
> **(3) The one mutation worth remembering from #393.** Making the Searcher
> restate the snippet-width rule *correctly* — a duplicate that agrees with
> the shared rule on every input — leaves **all 80 verdict tests green**. Only
> the structural source pin catches it. When a fix's whole point is "one
> authority", a differential over verdicts is not enough; something has to
> read the source.

## Project context (1-minute version)

`localmail` mirrors IMAP accounts (Gmail OAuth or password) into Postgres, and
is **strictly read-only w.r.t. IMAP**. The database is canonical for accounts.

- **Daemon:** hot-reloads the account set, writes heartbeats, runs a DB
  command queue, and has two-plane supervision.
- **Web admin UI (HTMX):** account CRUD, user management, archive imports,
  daemon control and API keys.
- **Also shipped:** hybrid search (Phases 1+2), an HTTPS GUI server, a remote
  MCP server (optionally a full OAuth 2.1 authorization server) and the opt-in
  `--smart` LLM query rewriter.
- **Tauri 2 + Svelte 5 GUI** under `gui/`: a read-only viewer plus an admin
  mode.
- **Versions and licence:** version **0.3.0**. Python **3.13** is pinned, and
  CI runs a 3.12 + 3.13 matrix. Licensed AGPL-3.0-or-later.
- **kastellan** consumes `/v1` over REST with an API key. Slice order
  **0 → A → B → D → E → C**. **E is deployed; only C remains.**

## What we shipped this session

### Deploy: both hosts on `046ad0a` (#387 anyio floor + #388 slice E)

- **Mac:** `uv sync --frozen --extra mcp --extra extraction` moved
  **anyio 4.13.0 → 4.15.1** and typing-extensions 4.15.0 → 4.16.0; `init-db`
  reported the schema up to date; both agents kickstarted.
  `/v1/version` → `api_minor: 3`, `build_hash: 046ad0a`,
  `build_source: git_checkout`. **#375 did not recur.**
- **DGX:** same, plus the usual `nvidia-cusparselt-cu13` re-resolve. Both
  units `NRestarts=0`; `/v1/version` → `api_minor: 3`, `build_hash: 046ad0a`;
  `uv pip show anyio` → 4.15.1.
- **Dependabot #79 and #80 are closed.** Note they still read *open* for a
  few minutes after the merge — the first query this session returned all
  three. Re-query before concluding anything.

#### Slice E acceptance, on the live Mac archive

There are **no API keys** on this archive, so acceptance ran through the api
layer (the shape slice D used) plus an unauthenticated route probe.

- `fields: ["message_id","snippet"]`, `snippet_chars: 80` → hits carrying
  **exactly** those two keys, snippet 81 chars (80 + one ellipsis).
- `snippet_chars` of `0`, `"5"`, `5.0`, `True` and `10**9` each refused with
  the documented message.
- An unknown `fields` name refused, naming the supported set.
- Both omitted → today's eleven hit keys and the ten-key envelope.
- `POST /v1/search` unauthenticated → **401**.

### PR #393: `fix/slice-e-review-followups` (3 commits, opened on `main`)

| SHA | What |
|---|---|
| `f61ce28` | **#390 + #392.** New pure `search/snippet_width.py::snippet_width_error`; `api/search_projection.py` keeps `fields` only; `project_hit` delegates to `fields_error` |
| `f537e06` | **#391.** `MessageListRow` renders `{snippet}`; `snippet_sanitize.{ts,test.ts}` deleted; `MessageList.test.ts`' fixture made a real snippet |
| `27f6b4b` | CLAUDE.md: the slice E bullets corrected in place, two Layout lines, the GUI bullet |

**#390 — one authority for the snippet width.** The type check and the floor
were hand-written twice, in two wordings. The **policy** split stays (the cap
is an operator's bound on *network* callers; `make_snippet` is correct for any
positive width), so the cap became an **argument**: `max_chars` is
keyword-only with **no default**, `None` meaning uncapped — the permissive
reading, so it must be written (#234's shape). The module lives under
`search/` because `search/` must not import `api/`; that placement is what
makes the nullable-cap variant work, which the issue had rejected on the
assumption it could not. **Wire byte-identical**; only the Searcher's wording
moved.

**#392 — `project_hit` re-checks by calling `fields_error`.** Its hand-written
guard enforced a *subset* — unknown names only — so an **empty** `fields`
returned hits with no keys at all, and several unknown names produced a
`KeyError` naming an arbitrary one. Unreachable from the wire; a totality fix.

**#391 — the snippet renders as text.** Verified independently that the server
has never emitted `<mark>` (`grep -rn "mark>" src/` empty; `git log -S "<mark>"
-- src/` empty; `make_snippet` only slices). Option 1 per the operator: delete
the allowlist and the `{@html}` sink. **`MessageList.test.ts` asserted the
`<mark>` element** — a test of a behaviour that does not exist, and the only
other occurrence in the tree.

## Verification (this Mac, all extras, at `27f6b4b`)

| gate | result |
|---|---|
| pytest | **4028 passed, 0 failed**, 3 warnings, 318 s |
| baseline | `main` at `046ad0a` collects **3954**; **+74**, reconciled exactly: +81 (`test_snippet_width.py`), −7 (`test_search_projection.py`, 37 → 30) |
| `gui/` vitest | **512 passed**, 49 files (`main` is 516: −9 deleted, +5 added) |
| `svelte-check` | 326 files, **0 errors, 0 warnings** |
| mypy | Success, **160** files (was 159) |
| ruff `src/` | **10**, the #285 baseline, none new; both new/touched files clean |
| CI | see `gh pr checks 393` — I stopped at the push |

- The 3 warnings are the #25 websockets pair **plus** anyio 4.15's
  `BlockingPortal` deprecation inside starlette's testclient, which arrived
  with #387 and was predicted in the last handoff.
- **Note the last handoff's 3947 is not `main`'s number.** `main` collects
  **3954**: PR #388 gained the review commit `f437a6c` *after* its handoff was
  written. Measure both refs yourself — see Risks 8.
- **Largest touched files:** `searcher.py` 1771 (was 1769, and it *lost* the
  inline rule), `api/search.py` 768. The new module is **61 lines**;
  `api/search_projection.py` shrank to **82**.
- **There is still no ROADMAP.md** (sixteenth confirmation).
- **README needed no change**: the wire is byte-identical and it already says
  `snippet_html` is raw message text a consumer must escape.

### Mutations, each caught by the pin written for it

| mutation | result |
|---|---|
| Searcher restates the rule **correctly** instead of delegating | **only** `test_neither_layer_restates_the_rule_in_its_own_source` fails; the other 80 pass |
| `project_hit` reverts to the subset guard | 4 tests fail, including the empty-`fields` one |
| `max_chars` gains `= None` | `test_max_chars_is_keyword_only_with_no_default` fails |
| `{@html}` sink restored | `MessageListRow.test.ts` fails on the literal-`<mark>` case (this was the RED step) |

Every mutation was restored from a scratchpad copy, never `git checkout`;
`git status --short` was empty afterwards.

## Review round on #393 (same PR, four reviewers)

A comprehensive review of #393 ran before merge. **One regression, three
pins weaker than they read, and four documentation claims that were false.**
All fixed in this PR; two findings were out of scope and are filed.

| finding | outcome |
|---|---|
| `project_hit` traversed `fields` **twice** — a one-shot iterable drained by `list(fields)` left `set(fields)` empty, returning a **keyless hit**, silently | **fixed**; this is #392's own defect reintroduced by #392's fix for a different input shape. `main` was single-traversal *by accident*. Pinned. |
| The structural anti-duplication pin scanned `api/search_projection.py` — the module #390 moved the rule **out of** — and not `api/search.py`, which holds the capped gate | **fixed**: replaced by an AST rule over all four consumers |
| …and its forbidden set held the two *type* wordings, **neither floor** wording | **fixed** by the same rule, which reads shape not text |
| The wire sentence was **unpinned** — rewriting it left 154 tests passing, the route asserting only loose needles | **fixed**: asserted literally |
| `test_the_capped_refusal_names_the_range` asserted `"1" in msg and "1000" in msg`; `"1000"` **contains** `"1"`, so the first conjunct was vacuous | **fixed**: each end asserted separately |
| `gui/README.md`'s manual-QA step told a tester to confirm `<mark>` highlighting that #391 makes impossible | **fixed** |
| `.snippet :global(mark)` CSS survived the deleted sink; `:global()` exempts it from svelte-check, so nothing warned | **deleted** |
| "an identical mistake cannot earn two wordings" — false for the **floor** (`snippet_chars=0` earns two sentences, and a test *requires* it) | **fixed** in 3 places |
| `project_hit`'s `O(len(HIT_FIELDS))` cost claim — the bound is `len(fields)`, which is uncapped on the wire | **corrected**; the cap itself is **#394** |
| `MIN_WIDTH`'s "empties every snippet" — true of the no-match branch only; the match branch returns `'……'` | **fixed**, re-measured |
| `snippet_chars` description promised a 400 for "anything else", but `null` is *unstated* | **fixed** |
| CLAUDE.md's "only other occurrence in the tree" | **narrowed** to `gui/src`, which is what was true |

**Filed, not fixed** — each is a decision rather than a defect in this change:

- **#394** — `fields` is unbounded on the wire and revalidated per hit. The
  exposure is **pre-existing** (`main` also traversed once per hit) and ~8×
  amplified; 20,000 names × 200 hits = 212 ms against `main`'s 27.7 ms. A
  `max_length` is a **wire contract change**, and #393 claims none.
- **#395** — nothing forbids a *new* `{@html}` sink elsewhere in `gui/src`.
  There are now zero, so the property is tree-wide, but only one component
  test holds it. That is new coverage and a new rule module.

### The new rule module

`tests/_snippet_width_rules.py::snippet_width_duplication_error` reads the
**AST** of all four consumers (`api/search.py`, `api/search_projection.py`,
`search/searcher.py`, `serve/routes/search.py`) and reports the *shape* a
hand-written copy must take: an `isinstance` test or an **ordering**
comparison touching a snippet-named value. Notes for whoever touches it:

- **Ordering only.** `is not None` is how both gates spell "unstated".
- **Either side counts.** The first cut required a literal opposite and so
  missed `n > cfg.snippet_max_chars` — a cap read off config, which is how a
  copy at the route would most naturally read. Caught by its own test.
- **A consumer not handed to it is reported**, not skipped. Silently scanning
  less than it claims is exactly how the predecessor came to read the one
  module that could no longer hold a copy.
- **AST, not text**, for `_mentions_version_option`'s reason: every one of
  those modules explains #390 in its own prose.

### Verification after the review round

| gate | result |
|---|---|
| pytest | **4035 passed, 0 skipped**, 3 warnings, 243 s (4028 + 7 net new) |
| `gui/` vitest | **512 passed**, 49 files |
| `svelte-check` | 326 files, **0 errors, 0 warnings** |
| mypy | clean on all four changed modules |

Mutations re-run, each caught: the double traversal (fails the new one-shot
pin), an identically-worded copy in `api/search.py` **and** a verbatim floor
copy in `searcher.py` (both fail the AST rule — **both passed the
predecessor**), and a consumer dropped from the scanned set. Every mutation
was restored from a scratchpad copy, never `git checkout`; `git status
--short` was clean afterwards.

## What's next

### 0. **Merge #393** *(acceptance: open issues 40 → 37)*
- **You merge**, once `gh pr checks 393` is green.
- **No deploy is needed for the Python half** — the wire is byte-identical, so
  the running servers are already correct. Deploy only if you want the GUI
  build to carry #391.
- **Acceptance:** `gh issue list` reports **37**; #390, #391, #392 closed by
  the merge (the body carries three `Closes` lines — verify, per Risks 5).

### 1. **Ask kastellan about slice C, then build it or close it out**
- **What was agreed on 2026-09-14:**
  - 401 with `/problems/token-expired` and `WWW-Authenticate: Bearer
    error="invalid_token"`, **only** when the presented token's `expires_at`
    has passed.
  - A revoked, disabled or unknown token keeps the generic answer. The expiry
    check must not consult the account, so nothing leaks about whether it was
    disabled.
  - `whoami` gains `credential: {kind, expires_at}`.
- **Acceptance:** an expired login token gets the new problem type; a revoked
  one does not; an API key (`expires_at NULL`) never does; `whoami` reports
  `kind` as `session` or `api_key` and `expires_at` as a timestamp or null;
  `api_minor` goes to **4**.
- **Ask first.** kastellan authenticates with an API key, and API keys never
  expire — so nothing it holds can reach the new branch.

### 2. **Candidate issue (still not filed): `snippet_source` mislabel**
- A whole-message lexical win (no chunk) builds its snippet from the full
  `body_text` yet reports `snippet_source="header"` (`searcher._hydrate` →
  `_build_results`). Predates slice E, not on the wire. It also shapes test
  fixtures — slice E had to seed "zebras" so the message-level arm would not
  win the #360 tie.

### 3. **Issues filed by the recent review rounds**
- **#386:** blob-streaming file handle and mid-stream failure (from #385).
- **#389:** the `fields` / `snippet_chars` 422-vs-400 asymmetry (named in a
  `serve/routes/search.py` comment; `fields_error`'s first two branches are
  library-only because pydantic's `list[str]` answers first).
- **#380:** a lone surrogate in a header value.
- **#383:** `messages.headers` and its GIN index are now write-only.
- **#384:** the TOAST prefix read.
- **#377:** a NUL in `query` is a 500.
- **#378:** an empty operator value becomes free text.
- **#369:** last-token-wins on scalar operators.
- **#373:** `O'Brien` becomes `OBrien`. Seed `OBrien` in tests.
- **#372:** ghost hits. **#370:** a type error is a 422, not problem+json.
- **#371:** the Tauri hop drops `lang` and the dates.
- **#368:** MCP ignores unknown arguments.
- **#365:** embedded images count as attachments.

### 4. **Carried**
- **#375 (Mac build-hash probe):** acceptance is that a `TimeoutExpired` is
  not cached. **It did not recur on this session's deploy.**
- **#374 + #305, the `cli.py` refactor (2177 lines):** acceptance is that
  `localmail --version` survives a blocked `sqlparse`. #331's point 2 is
  folded in — widen `cli.py`'s search catch to `SearchArgumentRefused`,
  never to bare `ValueError`.
- **Other issues:** #358/#357, #330/#327/#328, #319/#318/#320 (the next
  migration slot is `0037_*.sql`), #340, #363, #285 (ruff in CI: your call).
- **Admin GUI phase 5:** the Users panel.
- **Robustness:** #218, #226, #225/#227, #200/#211/#208, #206, #204, #25.

## Rulings made this session

1. **Deployed before writing any code.** The last handoff's step 0 was the
   deploy, and both PRs were already merged. *Cost if wrong:* a broken
   deploy would have blocked the session; it was clean.
2. **Live acceptance ran through the api layer, not over HTTP with a minted
   key.** There are no API keys on the Mac archive, and minting one creates a
   service-user row in the live database. The route was probed
   unauthenticated instead (401). *Cost if wrong:* the wire serialisation of
   a projected hit is proven by `test_serve_search_projection.py` against a
   seeded archive, not by the live probe.
3. **#390 took the nullable-cap variant the issue rejected.** The issue
   rejected it because it believed the shared rule would have to live in
   `api/` and so `search/` would import `api/`. Putting the module in
   `search/` — where the issue's *own* primary sketch puts it — removes the
   obstacle, and the result is one implementation with the policy difference
   as an argument, which the issue itself calls the better shape. *Cost if
   wrong:* the api layer's 0-message is produced by a shared branch rather
   than a local one; it is byte-identical, and pinned.
4. **#390 and #392 landed in one commit.** They touch the same module and the
   same principle ("delegate, don't restate"); splitting them needed
   hunk-level staging of `search_projection.py` for no review gain, since PRs
   squash-merge. *Cost if wrong:* one commit names two issues.
5. **#391 took option 1 (delete), per the operator's answer.** *Cost if
   wrong:* a future server-side highlighter has to re-add escaping
   server-side — which the issue argues it should do anyway, and which would
   finally make `snippet_html`'s name true.
6. **`MessageList.test.ts`' `<mark>` fixture was rewritten, not deleted.**
   It was asserting a server behaviour that does not exist; the test's real
   subject (search results render) is kept. *Cost if wrong:* none — the
   literal-`<mark>` case is now pinned properly in `MessageListRow.test.ts`.
7. **Plans and handoffs under `docs/` were left alone** though they describe
   `snippet_sanitize.ts` as required. They are frozen records; CLAUDE.md is
   the live document and carries the correction. *Cost if wrong:* a reader
   of the 2026-05-17 GUI plan finds a rule that no longer holds.
8. **The baseline was measured with `--collect-only` on both refs**, after
   committing. A fresh `git worktree` has no synced venv and gives 57
   collection errors. *Cost if wrong:* none — `collected == passed` on macOS
   (0 skipped), and the +74 reconciled exactly.

**Parked** (none blocks merge):
- `project_hit` still reads `snippet_html` even when `snippet` is not
  requested. The hit always carries it; documented.
- `MIN_WIDTH` is a named constant in the new module, but `make_snippet`'s own
  positivity is still implicit in its slicing. Not worth a guard there.
- The structural pin reads `inspect.getsource` of two modules for two
  literal sentences. It cannot see a duplicate written in *different* words —
  the verdict differentials cover that case, and between them nothing gets
  through.

## Open decisions & risks

1. **#393 is open and yours to merge.** After it, open issues **37**,
   Dependabot **1**.
2. **`api_minor` stays 3.** Nothing on the wire changed.
3. **A library caller's snippet-width error message changed** (#390's
   sanctioned behaviour change): `"must be a positive integer"` → `"must be
   an integer"` / `"must be at least 1"`. No network caller sees this.
4. **anyio 4.15's deprecation warning** comes from starlette's test client.
   If `filterwarnings` ever escalates `DeprecationWarning`, it fails. A
   starlette release will fix it.
5. **A merge does NOT close issues its subject merely names.** #393's body
   carries three `Closes` lines; verify with `gh issue list` afterwards.
6. **THIS FILE IS NOT THE AUTHORITY; `git` and `gh` are.**
7. **Scoped test runs cannot see mock callers of a changed signature.**
   Always finish with one full run. (No signature changed this session, but
   the rule stands.)
8. **Never quote a published test count.** The last handoff's 3947 was not
   `main`'s 3954 — the PR gained a review commit after the handoff was
   written. Measure both refs yourself.
9. **A "one authority" fix needs a source-reading pin.** Verdict
   differentials pass against a correct duplicate — proven this session.
10. **The stale NOTIFY queue** (three LISTEN/NOTIFY failures with `could not
    access status of transaction`) did not appear. Still cluster state.
11. **Never run two pytest sessions against one test database.**
12. **Restore a mutation from a file copy, never with `git checkout`.**
13. **The test DB is on port 5532**, the same cluster as the live archive.
14. **Always sync with `--extra mcp --extra extraction` on both hosts**, and
    use `~/.local/bin/uv` on the DGX.
15. **A MagicMock attribute reaches the wire as `{}`/`[]`.**
16. **No ROADMAP.md.** That `/nextsession` step is a no-op.
17. **This Mac was under load all session** (four Chrome renderers at ~150%,
    load average 24). **No timing in this handoff is comparable.** No stray
    spin loops — that was checked.

## Exact commands to resume

```bash
cd /Users/hherb/src/localmail
git fetch --prune origin                 # ALWAYS first
git status && git branch --show-current
git log --oneline -1 origin/main         # THE authority

# Did the last session hand off, and is #393 merged?
git log --oneline -1 -- NEXT_SESSION.md
ls -t docs/handoffs/ | head -3
gh pr view 393 --json state,mergeCommit --jq '"\(.state) \(.mergeCommit.oid // "-")"'
gh pr checks 393
gh issue list --limit 100 --json number --jq length      # 40 open, 37 after #393
gh api repos/hherb/localmail/dependabot/alerts \
  --jq '.[] | select(.state=="open") | "\(.number)\t\(.dependency.package.name)"'   # only 71

# Tunnel + load before trusting any timing.
tail -3 ~/localmail-probe/tunnel-probe.log      # utun_rx=NA => THIS Mac's wg0 is down
uptime; ps -A -o pid,pcpu,etime,comm -r | head -6
ps -eo pid,ppid,etime,time,command | grep "[w]hile :; do :; done"

# Both hosts are ALREADY on 046ad0a (api_minor 3). Verify, don't redeploy:
curl -sk https://127.0.0.1:8443/v1/version
ssh 10.0.0.3 'curl -sk https://10.0.0.3:8443/v1/version'
# If #393 is merged and you want the GUI to carry #391 — Mac:
#   git checkout main && git pull
#   unset VIRTUAL_ENV && uv sync --frozen --extra mcp --extra extraction
#   unset VIRTUAL_ENV && uv run localmail init-db      # expect "schema already up to date"
#   launchctl kickstart -k gui/$(id -u)/com.localmail.daemon
#   launchctl kickstart -k gui/$(id -u)/com.localmail.serve
# DGX:
#   ssh 10.0.0.3 'cd ~/src/localmail && git pull --ff-only && ~/.local/bin/uv sync --frozen --extra extraction --extra mcp'
#   ssh 10.0.0.3 'systemctl --user restart localmail-daemon localmail-serve'

# Python suite.
unset VIRTUAL_ENV && uv run pytest -q
#   macOS at 27f6b4b: 4028 passed, 0 skipped, 3 warnings.
unset VIRTUAL_ENV && uv run mypy src/localmail                   # Success, 160 files
unset VIRTUAL_ENV && uv run ruff check src/localmail/ | tail -1  # 10 (#285)

# Baselines — commit first, then collect-only on BOTH refs (a fresh worktree
# has no synced venv and gives 57 collection errors):
unset VIRTUAL_ENV && uv run pytest --collect-only -q | tail -1
git checkout -q main && unset VIRTUAL_ENV && uv run pytest --collect-only -q | tail -1
git checkout -q -                     # back

# gui/
cd gui && npm test -- --run            # 512 passed, 49 files
cd gui && npm run check                # 326 files, 0 errors

# This slice directly:
T="tests/test_snippet_width.py tests/test_search_projection.py tests/test_serve_search_projection.py tests/test_searcher_snippet_width.py"
unset VIRTUAL_ENV && uv run pytest -q ${=T}   # ${=T}, NOT $T — zsh does not word-split
```

`main` was **`046ad0a`** at session start and is unchanged. This session left
**one PR**: **#393** on `fix/slice-e-review-followups`, commits
`f61ce28..27f6b4b`, plus this handoff.

The latest migration is still **`0036_api_keys.sql`**; the next free slot is
`0037_*.sql`. **Open issues: 40 (37 after #393). Dependabot: 1.**
