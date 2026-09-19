# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-09-19 (session 49).** `main` was **`829b105`** at the
> start. The operator had merged #381 (kastellan slice B, `?headers=list`)
> between sessions, #379 had closed as intended, and the previous handoff
> described its own state accurately.
>
> This session **deployed `829b105` to both hosts** (`api_minor: 1` confirmed
> live on each) and built **kastellan slice D**: attachments by message index
> plus character paging of extracted text. It is **one PR, #385**, on
> `fix/kastellan-d-attachment-index`, containing the code, the spec, the plan
> and this handoff. **CI green is the stopping point; you merge.**
>
> **Four things worth knowing before you touch anything.**
>
> **(1) Open issues are 36, not the 34 the last handoff predicted.** The review
> round of #381 filed **#382, #383 and #384** on 2026-09-17. **#382 is already
> CLOSED as "completed"**, at 07:12Z that day, with no comment and no commit on
> `main` after `829b105`. Nothing in the tree fixes it:
> `header_block.entries_from_message` still discards the exception it catches.
> So #382 was closed by hand. If you meant "won't fix", that is fine; if you
> meant "fixed", it is not.
>
> **(2) My spec named the wrong Postgres error, and the review caught it.** It
> said an index past `int4` raises `operator does not exist: jsonb -> bigint`.
> That is true of the *uncast* SQL only. The shipped SQL casts with `%s::int`,
> so the real error is `integer out of range`. The conclusion (either way a
> 500, so the resolver refuses the index before the query) was right. The
> claim is now corrected in the code comment, CLAUDE.md, the spec and the plan,
> the last two annotated in place.
>
> **(3) The LISTEN/NOTIFY fault came and went by itself.** Task 1's full run
> hit exactly the three-test trio (`UndefinedFile: could not access status of
> transaction 2589113565`; queue usage 0; a live `LISTEN` worked). The next run,
> ~20 minutes later, passed all 3784 **with nobody cycling the daemon**. So
> "cycle the daemon" is sufficient, not necessary. Risk 7 below still applies.
>
> **(4) The haiku implementers were accurate on code and wrong on counts.**
> Tasks 1, 2, 3, 5 and 6 were pure transcription and passed review first time.
> One summary claimed 16 tests where there were 13, and one called the NOTIFY
> trio "pre-existing, verified". Both were caught only because every full-suite
> count was reconciled by hand against the previous task's total. Keep doing
> that.
>
> **Open issues: 36. Dependabot: 1** (#71 `accelerate`, no fix exists, left
> open on purpose). #385 closes no issue: slice D never had one.

## Project context (1-minute version)

`localmail` mirrors IMAP accounts (Gmail OAuth, password) into Postgres,
**strictly read-only w.r.t. IMAP**. The database is canonical for accounts.
Daemon: hot-reload account set, heartbeats, DB command queue, two-plane
supervision. Web admin UI (HTMX): account CRUD, user management, archive
imports, daemon control, API keys. Hybrid search (Phases 1+2), an HTTPS GUI
server, a remote MCP server (optionally a full OAuth 2.1 authorization server)
and the opt-in `--smart` LLM query rewriter are all shipped. A Tauri 2 + Svelte 5
GUI lives under `gui/`: a read-only viewer plus an admin mode (Accounts and
Daemon panels shipped; Users and Imports still placeholders). Version **0.3.0**, Python
**3.13** pinned (CI matrixes 3.12 + 3.13). Licensed AGPL-3.0-or-later (per-file
SPDX headers in `src/localmail/`; **not** in `gui/`). **kastellan** consumes
`/v1` over REST with an API key; its slice order is tracked in memory
(`kastellan-request-triage`): **0 → A → B → D → E → C**, and D is now in review.

## What we shipped this session

### Deploy — both hosts on `829b105` (#381, `?headers=list`)

- **Mac:** the tree was already at `829b105`. Ran `uv sync --frozen --extra mcp
  --extra extraction` (no changes), confirmed `init-db` reported the schema up
  to date, and kickstarted both agents. `/v1/version` →
  **`api_minor: 1`, `build_hash: 829b105`, `build_source: git_checkout`**
  (#375 did not recur).
- **DGX:** `git pull --ff-only` (`2ccfb1b` → `829b105`), then `uv sync`. It
  re-resolved one transitive package, `nvidia-cusparselt-cu13` 0.8.1; nothing
  else moved. Schema up to date; both units restarted with `NRestarts=0`;
  `/v1/version` → **`api_minor: 1`, `build_hash: 829b105`**.
- **Acceptance met.** On the live Mac archive, message 129707 returns 28
  header entries in wire order, including a 3-hop `Received` chain. `full` is
  still an object (24 keys), `compact` carries no `headers` key, and `bogus`
  is `ValidationFailed`.
- **Not done: telling kastellan.** Its `email-in` worker should move from
  `?headers=full` to `?headers=list`, gated on `api_minor >= 1`. That message
  has to come from you; I have no channel to that project.

### PR #385 — `fix/kastellan-d-attachment-index`, 13 commits on `829b105`

| SHA | What |
|---|---|
| `d035a5c` | Spec: [2026-09-17-message-attachment-index-design.md](docs/superpowers/specs/2026-09-17-message-attachment-index-design.md) |
| `a9c0ac3` | Plan (7 tasks) plus in-place spec corrections found while planning |
| `a071373` | `text_window.py`: pure character paging (`TextWindow`, `TextPage`, `MAX_TEXT_CHARS`) |
| `3f1f218` | `get_attachment_text_page` + `text_window_from_query`; `get_attachment_text` delegates |
| `5233f90` | `resolve_message_attachment` + `MAX_JSONB_INDEX` |
| `1503651` | Golden for `/v1/attachments/{sha256}`, committed **before** the move, green on unchanged code |
| `06ca213` | Streaming rules moved into `serve/routes/blob_response.py`; no test edited |
| `c14835e` | `/v1/attachments/{sha256}/text` paged |
| `a20eb8b` | `GET /v1/messages/{id}/attachments/{index}[/text]` |
| `d6b52c1` | `api_minor` 1 → 2; acceptance through `process_one_message`; README + CLAUDE.md |
| `8a91cdf` | Final-review fix wave (7 findings) |
| `e603643` | Plan's false error name annotated in place |
| `8e02a37` | Spec's false error name annotated in place (the prose bullet) |

Plus this handoff's commit.

**What the wire gains:**
- **`GET /v1/messages/{id}/attachments/{index}`** gives the same bytes, Range,
  ETag, 304 and force-download behaviour as the sha route. The difference is
  that `Content-Disposition` carries **that entry's own filename**.
- **`…/{index}/text` and `/v1/attachments/{sha256}/text`** now answer
  `{text, offset, limit, total, next_offset}`. Paging is by character, and the
  un-paged default is unchanged.
- **`api_minor` is 2.**

**Measured on the live archive** (129,668 messages; all figures in the spec):

| | |
|---|---|
| `(message, filename)` groups spanning >1 distinct blob | **200** (one message: `attachment` ×22, 22 blobs) |
| `(message, sha)` groups carrying >1 filename | **888** |
| blobs carrying >1 filename across messages | **1,109** |
| extracted text | p50 3.1 KB, p95 40 KB, **p99 129 KB, max 2.1 MB** |
| texts containing a character above U+FFFF | **8 of 9,303** (why `next_offset` is server-computed) |
| cost of one 20 k-char window on the 2.1 MB text | **3 ms first page, 8 ms last**, vs 2 ms whole (TOAST-compressed) |

## Verification (this Mac, all extras, controller-run at `8e02a37`)

| gate | result |
|---|---|
| pytest | **3826 passed, 0 failed**, 2 warnings (#25 websockets pair), 196 s |
| baseline | `main` = **3752**; +74 = 19 + 13 + 10 + 5 + 6 + 16 + 3 (tasks) + 2 (fix wave) |
| mypy | Success, **158** files |
| ruff `src/` | **10** (#285 baseline), none in branch-touched files |
| mutation 1 | delete the negative-index guard → 2 named tests fail |
| mutation 2 | `filename=None` in `message_attachment()` → disposition test + acceptance test fail |
| `gui/` touched | **no** |
| CI (#385) | **check `gh pr checks 385` — I stopped at the push** |

Both mutations were restored from scratchpad copies, never with `git checkout`,
and run with `python -B`. The largest touched file is `api/attachments.py` at
367 lines. **There is still no ROADMAP.md** (fourteenth confirmation). README
**was** updated.

## What's next

### 0. **Merge #385, then deploy it** *(`api_minor: 2` is the acceptance)*
- **You merge.** CI: `python-ci` on 3.12 + 3.13. No `gui/` file changed, so
  `gui-ci` should not run.
- **Mac:** `git checkout main && git pull`, then `uv sync --frozen --extra mcp
  --extra extraction`, then kickstart both agents.
- **DGX:** `git pull --ff-only && ~/.local/bin/uv sync --frozen --extra
  extraction --extra mcp`, then restart both units.
- **Acceptance:** `/v1/version` reads **`api_minor: 2`** on both hosts. Pick a
  live message that carries attachments (for example
  `SELECT id FROM messages WHERE jsonb_array_length(attachments) > 1 LIMIT 1`).
  Its `…/attachments/0` must serve the bytes of `attachments[0].sha256`, and
  `…/attachments/0/text?limit=100` must answer with `next_offset`.
- **Tell kastellan both things:** use `?headers=list` (gated on `api_minor >= 1`),
  and use index addressing plus paging (gated on `api_minor >= 2`). **Never let
  it compute the next offset from `text.length`.**

### 1. **Kastellan slice E: `fields=` projection and `snippet_chars`** *(next in 0→A→B→D→E→C)*
- From the triage memory: `fields=` projection on responses, and a
  `snippet_chars` length control. Note that **`snippet_html` is actually plain
  text**.
- **Acceptance** (to be refined at brainstorming): a caller can ask for a
  subset of search/browse fields and get exactly those keys. `snippet_chars`
  bounds the snippet. An unknown field name is a **400 problem+json**, never
  silently dropped (#364's rule).
- Flow: brainstorm → spec → plan → subagent-driven, as for B and D.

### 2. **The issues the last three review rounds filed**
- **#380** — a lone surrogate in a header value would 500 the renderer.
- **#383** — `messages.headers` and its GIN index are now write-only.
- **#384** — the header prefix read may not be cheap on a TOASTed `raw_bytes`.
  This session measured the same effect on `attachment_text`: a
  20 k-character window on the 2.1 MB text costs 3–8 ms against 2 ms for the
  whole value. That is evidence, not proof, for #384.
- **#377** — a NUL in `query` or a text filter is a psycopg 500.
- **#378** — operators with an empty value silently become free text.
- **#369** — scalar conflicts are resolved last-token-wins.
- **#373** — `O'Brien` becomes `OBrien`, so it never matches. Seed `OBrien` in
  tests until this is fixed.
- **#372** — ghost hits.
- **#370** — errors come back as 422 rather than problem+json.
- **#371** — the Tauri hop drops `lang` and the date filters.
- **#368** — MCP ignores unknown arguments.
- **#365** — embedded images count as attachments.

### 3. **#375 — the Mac build-hash probe** *(carried; did not recur again)*
**Acceptance:** a `TimeoutExpired` is not cached, and a later `/v1/version`
re-probes, rate-limited.

### 4. **#374 + #305 — the `cli.py` refactor** *(carried)*
**Acceptance for #305:** with `sqlparse` blocked on `sys.meta_path`,
`localmail --version` still exits 0. `cli.py` is **2177 lines**.

### 5. **Carried backlogs**
- #358 / #357 · #330 / #327 / #328 · #319 / #318 / #320 (the next free
  migration slot is `0037_*.sql`) · #340 · #363 · #285 (ruff in CI: your call).
- Admin GUI phase 5, the Users panel.
- Robustness: #218 · #226 · #225/#227 · #200/#211/#208 · #206 · #204 · #25.

## Rulings made this session

The SDD workspace has been deleted. These are the decisions it recorded, in
order, each with what it costs if it was wrong.

1. **Work ran on the branch in the main tree, not in a git worktree.**
   Implementers ran strictly one at a time, and this Mac's `.venv` *is* the
   production install. *Cost if wrong:* an uncommitted edit sits in the tree
   the launchd agents restart from. Mitigated: they only pick up code on an
   explicit kickstart.
2. **The LISTEN/NOTIFY trio was accepted by name as the cluster fault**, after
   I verified the error, the queue and a live `LISTEN` myself. The plan was to
   cycle the daemon before the final run; that wasn't needed, because the fault
   cleared on its own. *Cost if wrong:* a real NOTIFY regression hides until
   the final run. It didn't: the final run passes all three.
3. **Six final-review Minors were folded into the one required fix** (the WHY
   comment on the two-statement branch). They were: reject a non-hex stored
   sha as the shared 404, correct the error name, add `text_window.py` to
   CLAUDE.md's Layout, pin enumeration at the route, add the bytes-route
   ungranted-malformed-index test, and lead the README paragraph with the
   stronger motivation. *Cost if wrong:* a slightly larger final diff.
4. **Parked final Minor 3.** In both `stream_blob` and `message_attachment`,
   `fp` is closed only by garbage collection if the pool context raises on
   exit. This predates the branch, and fixing it restructures a route the
   golden pins. *Cost if wrong:* one file handle held until GC after a failed
   pool return. **Candidate issue: not filed.**
5. **The executed plan's false claims were annotated in place, not
   rewritten.** This follows CLAUDE.md's convention for refuted claims in
   plans. *Cost if wrong:* two italic notes in a historical document.

**Minors deferred and left by the final review's triage** (none blocks merge):
- The golden omits `accept-ranges` on 206 and `content-type` on 416, and its
  304 assertion checks only the absence of `content-disposition`.
- `etag_for_sha256` is computed twice per body-serving request.
- `TextWindow(limit=-5)` is never driven through the constructor, and
  `frozen=True` is not pinned.
- A test reuses sha `"e1"*32` within one file.
- The two new routes repeat the old routes' structure. That was mandated by
  the plan; the rules themselves are shared.

## Open decisions & risks

1. **#385 is open and yours to merge.** Open issues stay **36** on merge (it
   closes none). **Dependabot stays 1.**
2. **`api_minor` moved again (1 → 2).** Nothing in-tree pins it. An external
   consumer that tests `== 1` breaks.
3. **Both text routes' response body changed shape.** `{"text"}` became
   `{text, offset, limit, total, next_offset}`. This is additive, and `text`
   is unchanged, but a client doing exact-dict equality breaks. In-tree only
   the two HTTP tests did, and they are updated. The GUI does not read
   `/text`.
4. **#382 was closed with nothing fixed** (see (1) at the top).
5. **A merge does NOT close issues its subject merely names.** Use
   `Closes #N` in the PR body, and check with `gh issue list` after merging.
6. **THIS FILE IS NOT THE AUTHORITY; `git` and `gh` are.** Start with
   `git fetch --prune`, `git log --oneline -1 origin/main`,
   `git log --oneline -1 -- NEXT_SESSION.md`, `gh pr list` and
   `gh issue list`.
7. **The stale NOTIFY queue recurs, and can clear by itself.** Exactly three
   LISTEN/NOTIFY failures with `could not access status of transaction` are
   cluster state. Probe the queue; re-run before you cycle the daemon.
8. **Never trust "pre-existing" or a test count from a subagent.** Reconcile
   every full-suite total against the previous one. This session it caught a
   16-for-13 miscount.
9. **`utun_rx=NA` in the tunnel probe means the Mac's `wg0` is down**, not the
   DGX. The tunnel was up all this session.
10. **Never run two pytest sessions against one test database.** Every
    subagent was told to run pytest in the foreground.
11. **Restore a mutation from a file copy, never with `git checkout`.**
12. **The test DB is on port 5532**, the same cluster as the live archive.
13. **Dependency floors: read the range against `uv.lock`.** Always sync with
    `--extra mcp --extra extraction` on both hosts, and use `~/.local/bin/uv`
    on the DGX.
14. **A MagicMock attribute reaches the wire as `{}`/`[]`** (carried). This
    session's new keys are all asserted through the real transport.
15. **CI trap** (carried): a GUI admin panel that fetches on mount must be
    stubbed in both `AdminView.test.ts` and `MainView.test.ts`.
16. **No ROADMAP.md** (carried): that `/nextsession` step is a no-op.

## Exact commands to resume

```bash
cd /Users/hherb/src/localmail
git fetch --prune origin                 # ALWAYS first
git status && git branch --show-current
git log --oneline -1 origin/main         # THE authority, not this file's header

# Did the LAST session hand off, and is #385 merged?
git log --oneline -1 -- NEXT_SESSION.md
ls -t docs/handoffs/ | head -3
gh pr view 385 --json state,mergeCommit --jq '"\(.state) \(.mergeCommit.oid // "-")"'
gh pr checks 385
gh issue list --limit 100 --json number --jq length      # 36
gh api repos/hherb/localmail/dependabot/alerts \
  --jq '.[] | select(.state=="open") | "\(.number)\t\(.dependency.package.name)"'   # expect only 71

# Tunnel + load before trusting any timing.
tail -3 ~/localmail-probe/tunnel-probe.log      # utun_rx=NA => THIS Mac's wg0 is down
uptime; ps -eo pid,ppid,etime,time,command | grep "[w]hile :; do :; done"

# AFTER MERGING #385 — Mac (the tree IS the install):
#   git checkout main && git pull
#   unset VIRTUAL_ENV && uv sync --frozen --extra mcp --extra extraction
#   unset VIRTUAL_ENV && uv run localmail init-db      # expect "schema already up to date"
#   launchctl kickstart -k gui/$(id -u)/com.localmail.daemon
#   launchctl kickstart -k gui/$(id -u)/com.localmail.serve
#   curl -sk https://127.0.0.1:8443/v1/version        # api_minor MUST read 2
# AFTER MERGING — DGX:
#   ssh 10.0.0.3 'cd ~/src/localmail && git pull --ff-only && ~/.local/bin/uv sync --frozen --extra extraction --extra mcp'
#   ssh 10.0.0.3 'systemctl --user restart localmail-daemon localmail-serve'
#   ssh 10.0.0.3 'curl -sk https://10.0.0.3:8443/v1/version'   # api_minor == 2

# Python suite.
unset VIRTUAL_ENV && uv run pytest -q
#   macOS at 8e02a37: 3826 passed, 0 skipped, 2 warnings (the #25 websockets pair).
#   Exactly 3 LISTEN/NOTIFY failures = RISK 7, not your change:
psql -h localhost -p 5532 -U localmail -d postgres -tAc 'SELECT pg_notification_queue_usage()'
psql -h localhost -p 5532 -U localmail -d localmail_test -c 'LISTEN daemon_commands'
unset VIRTUAL_ENV && uv run mypy src/localmail                   # Success, 158 files
unset VIRTUAL_ENV && uv run ruff check src/localmail/ | tail -1  # 10 (#285)

# This slice directly:
T="tests/test_text_window.py tests/test_api_attachment_text_page.py tests/test_api_message_attachment.py tests/test_serve_attachments_golden.py tests/test_serve_message_attachment_routes.py tests/test_attachment_index_acceptance.py"
unset VIRTUAL_ENV && uv run pytest -q ${=T}   # ${=T}, NOT $T — zsh does not word-split

# Host health (Mac):
launchctl list | grep -i localmail
unset VIRTUAL_ENV && uv run localmail search-status    # under a second (#280)
```

`main` was **`829b105`** at session start. This session left **one PR, #385**,
on `fix/kastellan-d-attachment-index`: 13 commits `d035a5c..8e02a37`, plus the
handoff commit. The latest migration is still **`0036_api_keys.sql`**; the next
free slot is `0037_*.sql` (this slice needed none). **Open issues: 36.
Dependabot: 1.**
