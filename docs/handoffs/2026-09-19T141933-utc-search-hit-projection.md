# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-09-19 (session 50).** `main` was **`f9fa3b5`** at the
> start. The operator had merged #385 (kastellan slice D) between sessions,
> and the previous handoff described its own state accurately.
>
> This session:
> - **deployed `f9fa3b5` to both hosts**, and both report `api_minor: 2`;
> - opened **PR #387**, which declares `anyio` with a security floor for the two
>   new Dependabot alerts;
> - built **kastellan slice E**: `fields` projection and `snippet_chars` on
>   `POST /v1/search`, with `api_minor` going to 3.
>
> That makes **two PRs, both based on `main` and independent of each other.**
> #387 is dependency-only. **#388** is on `fix/kastellan-e-search-projection`
> and holds the slice E code, spec, plan and this handoff. **CI green is the
> stopping point; you merge.**
>
> **Three things to know before you touch anything.**
>
> **(1) Two new Dependabot alerts appeared between sessions:** anyio **#80
> (critical)**, a TLSStream IDNA-2003 certificate-spoofing bug, and **#79
> (medium)**, process-pool workers that block on undrained stderr. Both cover
> `< 4.14.2`, and the lock held 4.13.0.
> - localmail imports `anyio` directly but never declared it, the same "no
>   floor to read against" shape as `transformers`.
> - Neither surface is on our path. There is no `TLSStream`, `connect_tcp` or
>   process pool in `src/`, and `serve`'s TLS is uvicorn over stdlib `ssl`.
> - So this is hygiene, but a critical alert should not wait on a feature
>   review. That is why it has its own small PR (#387, **CI green**) rather
>   than riding in #388.
>
> **(2) The plan missed two test files, and only the controller's full run
> caught it.**
> - After Task 5 the full suite had **3 failures**. They were mock tests
>   asserting `continue_page("tok-1", 2, user_id=99)` exactly, while
>   `run_search` now also forwards `snippet_chars=None`.
> - No task's file list included `test_api_search_cursor_mode.py` or
>   `test_api_search_pagination.py`, so every scoped run was green.
> - Fixed in the final wave. **The lesson stands:** scoped runs cannot see a
>   call-signature change's mock callers. Grep `assert_called.*with` for the
>   changed callee before trusting a scoped green.
>
> **(3) pydantic's lax `int` coerces JSON `true` to `1`.** The plan assumed a
> 422 there. The field went through three shapes:
> - `int | bool` fixed `true`, but still coerced `"5"` and `5.0` silently, and
>   answered `1.5` with a 422 carrying an array `detail`.
> - The final review caught the README overclaim this produced.
> - **`snippet_chars` is now typed `Any`**, so every value reaches the pure
>   rule and comes back as a problem+json 400.
>
> **Open issues: 37. Dependabot: 3** (#71 `accelerate`, left open on purpose;
> #79 and #80, which PR #387 closes).

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
- **kastellan** consumes `/v1` over REST with an API key. Its slice order is
  **0 → A → B → D → E → C**. **E is now in review; only C remains.**

## What we shipped this session

### Deploy: both hosts on `f9fa3b5` (#385, slice D)

- **Mac:** `git pull --ff-only`, `uv sync --frozen --extra mcp --extra
  extraction` (no changes), `init-db` reported the schema up to date, and both
  agents were kickstarted. `/v1/version` returns **`api_minor: 2`,
  `build_hash: f9fa3b5`, `build_source: git_checkout`**. (#375, the build-hash
  probe timeout, did not recur.)
- **DGX:** pull, then `uv sync`, which re-resolved only
  `nvidia-cusparselt-cu13` 0.8.1, as last time. Schema up to date; both units
  restarted with `NRestarts=0`; `/v1/version` returns **`api_minor: 2`,
  `build_hash: f9fa3b5`**.
- **Acceptance met on the live Mac archive.**
  - Message 255 has 2+ attachments; its index 0 resolves to its own sha
    `4fd361a0…` and its own filename.
  - `get_attachment_text_page(offset=0, limit=100)` returns 100 characters of
    710 with `next_offset` 100.
  - The live route answers **401** unauthenticated, not 404.
- **Not done: telling kastellan.** That message has to come from you. It
  should say:
  - use `?headers=list` (gate on `api_minor >= 1`);
  - use index addressing and paging (gate on `api_minor >= 2`), and **never
    compute the next offset from `text.length`**;
  - once #388 is deployed, use `fields` and `snippet_chars` (gate on
    `api_minor >= 3`).

### PR #387: `fix/anyio-security-floor` (1 commit, CI green)

| SHA | What |
|---|---|
| `56ba558` | `anyio>=4.14.2` declared in `[project.dependencies]` with the security-floor comment. The lock moves anyio 4.13.0 → 4.15.1 and typing-extensions 4.15.0 → 4.16.0. Full suite: 3864 passed. |

It adds one new warning: anyio 4.15 deprecates `anyio.abc.BlockingPortal`, and
`starlette/testclient.py` still uses it. That is test-client code only.

### PR #388: `fix/kastellan-e-search-projection` (8 commits + this handoff)

| SHA | What |
|---|---|
| `428a97a` | Spec: [2026-09-19-search-hit-projection-design.md](docs/superpowers/specs/2026-09-19-search-hit-projection-design.md) |
| `6c73e3f` | Plan, 5 tasks |
| `ce4abe4` | `api/search_projection.py`: `HIT_FIELDS`, `fields_error`, `snippet_chars_error`, `project_hit` (37 tests) |
| `19649f9` | `SearchConfig.snippet_max_chars` = 1000, validated `>= snippet_width_chars` |
| `2fbbc35` | Searcher: per-call `snippet_chars` on `search`/`continue_page`/`grow_pool`; `_build_results(snippet_width=)` is keyword-only with no default |
| `33bf2b4` | `run_search` and the route take `fields`/`snippet_chars`, gated before the empty-ACL short-circuit; `API_MINOR` = 3 |
| `980f2a9` | README subsection and CLAUDE.md bullet and Layout line |
| `40b43e1` | Final-review fix wave (see Rulings 4 and 5) |

**What the wire gains** (`POST /v1/search` only):
- **`fields: [...]`** returns each hit with exactly those keys, in the fixed
  `HIT_FIELDS` order.
  - It includes the new **`snippet`**, the honest plain-text name for
    `snippet_html`.
  - The envelope is never projected.
  - An unknown or empty `fields` is a 400.
- **`snippet_chars`** is the snippet window width, `1..snippet_max_chars`. The
  text may gain a `…` at each end. Each page, continuations included, takes
  its own width.
  - Any non-integer, bool or out-of-range value is a 400 problem+json, and
    that includes the string `"5"` and `5.0`.
- **Both omitted means today's response, unchanged.** Both are refused from a
  caller granted nothing too.
- **The date walk** (filter-only or `sort=date`) emits no snippet, so there
  `snippet_chars` sizes nothing. That is documented, not changed.
- **`api_minor` is 3.**

## Verification (this Mac, all extras, controller-run at `40b43e1`)

| gate | result |
|---|---|
| pytest | **3947 passed, 0 failed**, 2 warnings (the #25 websockets pair), 200 s |
| baseline | `main` = **3864** (measured on #387's identical test tree); +83 = 37 + 4 + 12 + 17 from the tasks, + 13 from the fix wave |
| mypy | Success, **159** files |
| ruff `src/` | **10** (the #285 baseline), none new |
| mutations (fix wave) | dropping `snippet_chars=` on `_continue_or_grow`'s `continue_page` call makes the new pin fail; same for `grow_pool` |
| F4 order pin | `sort="Date", snippet_chars=0` must raise the membership error; under the old order it raises the snippet error, so the pin fails |
| `gui/` touched | **no** |
| CI | #387 **green** (3.12 + 3.13). #388: **check `gh pr checks 388`; I stopped at the push** |

- **Largest touched files:** `searcher.py` 1769 lines (was 1719) and
  `api/search.py` 756 (was 726). Both were already over 500; all new logic is
  in the 83-line pure module.
- **There is still no ROADMAP.md** (fifteenth confirmation). **README was
  updated.**

## What's next

### 0. **Merge #387 and #388, then deploy** *(acceptance: `api_minor: 3`)*
- **You merge**, in either order: they share no file except `uv.lock`
  untouched by #388.
- **Mac:**
  ```
  git checkout main && git pull
  uv sync --frozen --extra mcp --extra extraction
  launchctl kickstart -k gui/$(id -u)/com.localmail.daemon
  launchctl kickstart -k gui/$(id -u)/com.localmail.serve
  ```
  After #387, `uv sync` **will** move anyio.
- **DGX:** `git pull --ff-only && ~/.local/bin/uv sync --frozen --extra
  extraction --extra mcp`, then restart both units.
- **Acceptance:**
  - `/v1/version` reads **`api_minor: 3`** on both hosts.
  - `uv pip show anyio` reports **≥ 4.14.2** on both.
  - Dependabot #79 and #80 are closed.
  - A live search with `{"query":"invoice","fields":["message_id","snippet"],"snippet_chars":80}`
    returns hits with exactly those two keys.
  - The same search with `"snippet_chars": 0` returns a 400.
- **Then tell kastellan** all three gates (see Deploy above).

### 1. **Kastellan slice C: `/problems/token-expired`** *(the last slice)*
- **What was agreed on 2026-09-14:**
  - Return 401 with `/problems/token-expired` and `WWW-Authenticate: Bearer
    error="invalid_token"`, **only** when the presented token's `expires_at`
    has passed.
  - A revoked, disabled or unknown token keeps the generic answer. The expiry
    check must not consult the account, so nothing leaks about whether it was
    disabled.
  - `whoami` gains `credential: {kind, expires_at}`.
- **Acceptance (refine at brainstorming):**
  - an expired login token gets the new problem type;
  - a revoked one does not;
  - an API key (`expires_at NULL`) never does;
  - `whoami` reports `kind` as `session` or `api_key`, and `expires_at` as a
    timestamp or null;
  - `api_minor` goes to 4.
- Lowest value, since API keys never expire. Consider asking kastellan whether
  it still wants it.

### 2. **Candidate issue (not filed): `snippet_source` mislabel**
- A whole-message lexical win (no chunk) builds its snippet from the **full
  `body_text`**, yet reports `snippet_source="header"`
  (`searcher._hydrate` → `_build_results`).
- It predates this branch and is not on the wire. It also shapes test
  fixtures: Task 3 had to seed "zebras" so the message-level arm would not
  win the #360 tie.

### 3. **Issues filed by the last review rounds**
- **#386:** blob-streaming file handle and mid-stream failure (from #385's
  review).
- **#380:** a lone surrogate in a header value.
- **#383:** `messages.headers` and its GIN index are now write-only.
- **#384:** the TOAST prefix read. Slice D's 3–8 ms-per-window measurement is
  evidence for it.
- **#377:** a NUL in `query` is a 500.
- **#378:** an empty operator value becomes free text.
- **#369:** last-token-wins on scalar operators.
- **#373:** `O'Brien` becomes `OBrien`. Seed `OBrien` in tests.
- **#372:** ghost hits.
- **#370:** a type error is a 422, not problem+json.
- **#371:** the Tauri hop drops `lang` and the dates.
- **#368:** MCP ignores unknown arguments.
- **#365:** embedded images count as attachments.

### 4. **Carried**
- **#375 (Mac build-hash probe):** acceptance is that a `TimeoutExpired` is not
  cached.
- **#374 + #305, the `cli.py` refactor (2177 lines):** acceptance is that
  `localmail --version` survives a blocked `sqlparse`.
- **Other issues:** #358/#357, #330/#327/#328, #319/#318/#320 (the next
  migration slot is `0037_*.sql`), #340, #363, #285 (ruff in CI: your call).
- **Admin GUI phase 5:** the Users panel.
- **Robustness:** #218, #226, #225/#227, #200/#211/#208, #206, #204, #25.

## Rulings made this session

The SDD workspace has been deleted. These are the decisions it recorded, in
order, each with what it costs if it was wrong.

1. **Work ran on the branch in the main tree, not a worktree.** Implementers
   ran one at a time. *Cost if wrong:* an uncommitted edit sits in the tree
   the launchd agents restart from; they only pick up code on a kickstart.
2. **`snippet_chars` was typed `int | bool | None` from the start.** The plan
   expected a 422 for `true`; pydantic lax mode actually yields 200. *Cost if
   wrong:* none, because it was superseded by ruling 5.
3. **`ce4abe4`'s "Claude Haiku 4.5" trailer was left unamended.** PRs are
   squash-merged, and the attribution is accurate. *Cost if wrong:* one branch
   commit carries a different trailer.
4. **The Task 5 implementer did docs only; the controller ran the full gate.**
   That is what surfaced the 3 failures in (2) at the top. *Cost if wrong:*
   none.
5. **Final-review I2, option (a): `snippet_chars: Any` with a
   `Field(description=…)`.** Every value now reaches the pure rule, which is
   what the spec states. *Cost if wrong:* the OpenAPI schema loses the integer
   type; the description says it.
6. **Three final-review minors were folded into the one fix wave:**
   - forwarding pins for `continue_page` and `grow_pool`;
   - the Searcher width check hoisted *below* sort membership, still pre-IO,
     so both layers order the guards alike (#348);
   - `_snippet_width` refuses a non-int.

   *Cost if wrong:* a slightly larger final diff.
7. **Left alone:** the Layout placement of `api/search_projection.py` under
   the top-level listing (cosmetic; there is no `api/` block), and the
   OpenAPI `integer | boolean` note, which (5) resolves.

**Parked** (none blocks merge):
- `project_hit` reads `snippet_html` even when `snippet` is not requested.
  The hit always carries it, and the precondition is documented.
- The `snippet_source` mislabel: candidate issue, see What's next 2.
- The default-unchanged pin checks the key set and the width, not literal
  value bytes. The no-`fields` branch is textually the old expression.

## Open decisions & risks

1. **#387 and #388 are open and yours to merge.** Open issues stay **37**,
   because #388 closes none. After #387, Dependabot drops to **1** (#71).
2. **`api_minor` moved again, 2 → 3.** An external consumer testing `== 2`
   breaks, as in D.
3. **`snippet_chars` is `Any` in the OpenAPI schema.** A generated client will
   not see "integer". The field's description states the contract.
4. **anyio 4.15's deprecation warning** comes from starlette's test client. If
   `filterwarnings` ever escalates `DeprecationWarning`, it will fail. A
   starlette release will fix it.
5. **A merge does NOT close issues its subject merely names.** Use
   `Closes #N`, and check with `gh issue list`.
6. **THIS FILE IS NOT THE AUTHORITY; `git` and `gh` are.**
7. **Scoped test runs cannot see mock callers of a changed signature.** See
   (2) at the top. Always finish with one full run.
8. **Never trust "pre-existing" or a test count from a subagent.** Reconcile
   by hand. This session's reconciliation came out exact at 3947.
9. **The stale NOTIFY queue** (three LISTEN/NOTIFY failures with `could not
   access status of transaction`) did not appear this session. It is still
   cluster state when it does.
10. **Never run two pytest sessions against one test database.** Every
    subagent was told to run pytest in the foreground.
11. **Restore a mutation from a file copy, never with `git checkout`.**
12. **The test DB is on port 5532**, the same cluster as the live archive.
13. **Always sync with `--extra mcp --extra extraction` on both hosts**, and
    use `~/.local/bin/uv` on the DGX.
14. **A MagicMock attribute reaches the wire as `{}`/`[]`.** Slice E's wire
    tests use a real Searcher.
15. **No ROADMAP.md.** That `/nextsession` step is a no-op.

## Exact commands to resume

```bash
cd /Users/hherb/src/localmail
git fetch --prune origin                 # ALWAYS first
git status && git branch --show-current
git log --oneline -1 origin/main         # THE authority

# Did the LAST session hand off, and are #387/#388 merged?
git log --oneline -1 -- NEXT_SESSION.md
ls -t docs/handoffs/ | head -3
gh pr view 387 --json state,mergeCommit --jq '"\(.state) \(.mergeCommit.oid // "-")"'
gh pr view 388 --json state,mergeCommit --jq '"\(.state) \(.mergeCommit.oid // "-")"'
gh pr checks 388
gh issue list --limit 100 --json number --jq length      # 37
gh api repos/hherb/localmail/dependabot/alerts \
  --jq '.[] | select(.state=="open") | "\(.number)\t\(.dependency.package.name)"'   # expect only 71 once #387 merges

# Tunnel + load before trusting any timing.
tail -3 ~/localmail-probe/tunnel-probe.log      # utun_rx=NA => THIS Mac's wg0 is down
uptime; ps -eo pid,ppid,etime,time,command | grep "[w]hile :; do :; done"

# AFTER MERGING — Mac (the tree IS the install):
#   git checkout main && git pull
#   unset VIRTUAL_ENV && uv sync --frozen --extra mcp --extra extraction   # anyio moves after #387
#   unset VIRTUAL_ENV && uv run localmail init-db      # expect "schema already up to date"
#   launchctl kickstart -k gui/$(id -u)/com.localmail.daemon
#   launchctl kickstart -k gui/$(id -u)/com.localmail.serve
#   curl -sk https://127.0.0.1:8443/v1/version        # api_minor MUST read 3
# AFTER MERGING — DGX:
#   ssh 10.0.0.3 'cd ~/src/localmail && git pull --ff-only && ~/.local/bin/uv sync --frozen --extra extraction --extra mcp'
#   ssh 10.0.0.3 'systemctl --user restart localmail-daemon localmail-serve'
#   ssh 10.0.0.3 'curl -sk https://10.0.0.3:8443/v1/version'   # api_minor == 3

# Python suite.
unset VIRTUAL_ENV && uv run pytest -q
#   macOS at 40b43e1: 3947 passed, 0 skipped, 2 warnings (the #25 websockets pair).
#   (#387 adds a third: anyio's BlockingPortal deprecation inside starlette's testclient.)
unset VIRTUAL_ENV && uv run mypy src/localmail                   # Success, 159 files
unset VIRTUAL_ENV && uv run ruff check src/localmail/ | tail -1  # 10 (#285)

# This slice directly:
T="tests/test_search_projection.py tests/test_searcher_snippet_width.py tests/test_serve_search_projection.py tests/test_api_search_pagination.py tests/test_api_search_cursor_mode.py tests/test_serve_version_route.py"
unset VIRTUAL_ENV && uv run pytest -q ${=T}   # ${=T}, NOT $T — zsh does not word-split
```

`main` was **`f9fa3b5`** at session start. This session left **two PRs**:
- **#387** on `fix/anyio-security-floor` (1 commit);
- **#388** on `fix/kastellan-e-search-projection`, commits `428a97a..40b43e1`
  plus the handoff commit.

The latest migration is still **`0036_api_keys.sql`**, and the next free slot
is `0037_*.sql`. **Open issues: 37. Dependabot: 3 open, 1 after #387.**
