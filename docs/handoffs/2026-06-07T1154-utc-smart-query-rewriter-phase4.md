# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-06-07 (Search Phase 4 `--smart` query rewriter — PR #171 open).**
> Prior task confirmed done: **PR #170 (MCP filter-semantics coupling test) merged**
> into `main` at `ed8dfec` (squash); local branch pruned. This session **designed,
> planned, and implemented the opt-in `--smart` LLM query rewriter (Search Phase 4)**
> end-to-end via subagent-driven development. Work is on branch
> `smart-query-rewriter`, pushed and open as **PR #171**
> (https://github.com/hherb/localmail/pull/171). **Local: full suite 1491 passed**
> (only the pre-existing macOS `test_daemon_control_socket` AF_UNIX-path-too-long
> failures deselected), **mypy clean (105 files)**. `main` is at `ed8dfec`.

## Project context (1-minute version)

`localmail` mirrors IMAP accounts (Gmail OAuth, password) into Postgres,
**strictly read-only w.r.t. IMAP**. The **database is canonical for accounts**
end-to-end. Daemon: hot-reload account set, heartbeats, DB command queue,
two-plane supervision, non-blocking lifecycle + admin panel (2B.1–2B.5). Admin
UI: account CRUD (2A.3), user management (2A.4), archive imports (2A.5). Hybrid
search (Phases 1+2) + an HTTPS GUI server + a remote MCP server (Phase 3) are
shipped; **Phase 4 (`--smart`) is this PR**. A Tauri + Svelte GUI lives under
`gui/`. See [CLAUDE.md](CLAUDE.md), [README.md](README.md).

## What we did this session — `--smart` LLM query rewriter (Phase 4)

**Why.** Phases 1–3 shipped hybrid search but the `--smart` flag was a stub:
`Searcher.search` raised `RuntimeError("--smart requires a configured rewriter
(Phase 4)")` and nothing ever populated `ParsedQuery.rewritten_text` /
`expansion_terms`. This session built the real rewriter.

**What.** A local-only (Ollama) LLM rewrites the free-text part of a `--smart`
query into three outputs, merged into the parsed query before retrieval:
- **`rewritten_text`** → vector arm + reranker (they already read
  `rewritten_text or free_text`).
- **`expansion_terms`** → OR-ed into the lexical (tsvector) arms via the new
  pure `arms.build_lexical_tsquery`. **Identity-preserving**: with no expansion
  terms it returns the bare single-`plainto_tsquery` form, so the non-smart path
  is byte-identical. Multi-term fragment is **parenthesised** (`@@` binds tighter
  than `||` in Postgres). Applied on **both** the hybrid arms and the `sort=date`
  lexical-keyset branch (`_lexical_date_search`).
- **`extracted_filters`** → NL → structured (e.g. "last summer" → `after:`/
  `before:`). **Explicit operators win**: the pure `rewriter.apply_rewrite`
  fills only the scalar slots (`after`/`before`/`from`/`to`/`subject`/
  `has_attachment`) the user left `None`; it never sets account/folder/lang/
  **label** (environment-specific; `_FiltersSchema` omits `label`).

**Failure policy = no silent failure, owned by the Searcher.**
`OllamaLLMRewriter` (the only IO; httpx → `/api/generate`, `format`-constrained
JSON, `temperature=0`) **raises** on every failure mode — timeout, connect,
4xx/5xx, malformed JSON, **and a 200-with-missing-`response`-key**.
`Searcher.search` catches exactly `(httpx.HTTPError, RewriteParseError)`, keeps
the un-rewritten query, logs `smart rewrite skipped: …`, and surfaces
`SearchPage.rewrite_skipped` (CLI prints a `note:`). Relative dates are resolved
LLM-side via an injected `today` (deterministic, testable prompt).

**No new migration. No new uv extra** (`httpx` already a dep; Ollama is external
HTTP). Opt-in via `[search] rewriter_enabled_by_default = true` + the per-call
`--smart` flag.

### Files
- **New** [src/localmail/search/rewriter.py](src/localmail/search/rewriter.py)
  (~200 lines): `RewriteResult`, `QueryRewriter` Protocol, `RewriteParseError`,
  pure `build_rewrite_prompt` / `parse_rewrite_response` / `apply_rewrite`,
  `OllamaLLMRewriter`.
- **New** [tests/test_rewriter.py](tests/test_rewriter.py) (17 tests),
  [tests/test_searcher_smart.py](tests/test_searcher_smart.py) (3 tests).
- **Modified**: `config.py` (`rewriter_max_expansion_terms: int = 8`),
  `search/arms.py` (`build_lexical_tsquery` + both BM25 arms),
  `search/searcher.py` (rewrite call + `rewrite_skipped` field + 3 return sites +
  `_lexical_date_search` expansion), `search/__init__.py` (factory + exports),
  `cli.py` (skip notice). Tests extended in `test_arms.py`,
  `test_search_public_api.py`, `test_cli_search.py`, `test_config.py`.
- **Docs**: README `--smart` subsection; CLAUDE.md module list + Phase-4 note;
  spec [docs/superpowers/specs/2026-06-07-smart-query-rewriter-design.md](docs/superpowers/specs/2026-06-07-smart-query-rewriter-design.md);
  plan [docs/superpowers/plans/2026-06-07-smart-query-rewriter.md](docs/superpowers/plans/2026-06-07-smart-query-rewriter.md).

### Commits (branch `smart-query-rewriter`, base `main` @ `ed8dfec`)
```
41fcd86 fix(search): commit the build_lexical_tsquery parenthesisation
c2fea28 fix(search): apply --smart expansion terms on the sort=date lexical path
b554791 docs(search): document --smart query rewriter (Phase 4)
42b0093 feat(cli): notice when --smart rewrite is skipped
c9972cb feat(search): create_searcher builds OllamaLLMRewriter when enabled; export rewriter API
1ccba78 feat(search): call rewriter on --smart with surfaced graceful fall-through
317c15f test(search): expansion term retrieves synonym-only message
47eb7be feat(search): OR-in expansion terms via build_lexical_tsquery (identity when empty)
329ec4d fix(search): rewriter raises RewriteParseError on missing response key; review polish
e6a12e9 feat(search): OllamaLLMRewriter HTTP backend (httpx, format-constrained JSON)
433bea5 feat(search): apply_rewrite precedence merge (explicit operators win)
ad703d6 feat(search): parse_rewrite_response with pydantic schema validation
3af9fb0 feat(search): rewriter module skeleton + deterministic prompt builder
8ea13b9 feat(search): add rewriter_max_expansion_terms config field
1eb352d docs(search): implementation plan for --smart query rewriter (Phase 4)
e9952f5 docs(search): design for --smart LLM query rewriter (Phase 4)
```
(HEAD `41fcd86`. `e9952f5`/`1eb352d` are the spec + plan.)

### Verification (this session)
```bash
unset VIRTUAL_ENV && uv run pytest -q tests/ --deselect tests/test_daemon_control_socket.py
                                                 # 1491 passed, 14 deselected
unset VIRTUAL_ENV && uv run mypy src/localmail   # clean, 105 files
```

## What's next

### 0. **Merge PR #171** *(immediate)*
```bash
gh pr checks 171                          # CI was pending at handoff time
gh pr merge 171 --squash --delete-branch
git checkout main && git pull
```
**Acceptance:** CI green on Linux (PG pg18, Python 3.12). The macOS-only
`test_daemon_control_socket` AF_UNIX failures are LOCAL env noise and won't
appear in CI. **Watch specifically** that `tests/test_arms.py` (the
`build_lexical_tsquery` unit + DB tests) and `tests/test_rewriter.py` are green —
commit `41fcd86` fixed a late catch where the committed `arms.py` shipped the
non-parenthesised tsquery while the committed test expected the parenthesised
form (the fix had been left in the working tree only; `gh pr create`'s
"uncommitted changes" warning surfaced it).

### 1. **`--smart` manual smoke (recommended before/after merge)**
Not run this session (no live Ollama in the loop). Acceptance:
```bash
ollama pull qwen2.5:3b
# config.toml: [search] rewriter_enabled_by_default = true
unset VIRTUAL_ENV && uv run localmail search "tax stuff from the accountant last summer" --smart --verbose
```
Expect: `timing(ms)` includes a `rewrite` key; results reflect inferred date
filters; killing Ollama mid-flight prints `note: --smart rewrite skipped …` and
still returns results.

### 2. **Phase-4 follow-ups (filed-as-notes, low priority; non-blocking)**
   - **Rewrite-result caching** — each `--smart` query hits Ollama fresh
     (deliberately out of scope this slice). Add an LRU keyed on free-text if it
     proves hot.
   - **Cloud/other rewriter backends** — `rewriter_backend` Literal stays
     `"ollama"`; widen only if a non-local backend is wanted (privacy tradeoff).
   - **MCP `search` tool `smart=` param** — the rewriter is wired into the
     Python `Searcher` + CLI; exposing it over MCP/HTTP is a separate small slice
     (thread a `smart` bool + surface `rewrite_skipped` on the wire).
   - Each non-trivial item needs its own brainstorm → spec → plan.

### 3. **Remaining MCP follow-ups (carried, low priority)**
   - Full OAuth 2.1 discovery (Approach B); `streamablehttp_client` rename
     (deprecated in `tests/test_mcp_integration.py`); `--smart` for MCP (see §2).

### 4. **Upstream-blocked (not actionable)**
   - **#90** (glib via Tauri stack bump; Dependabot alert) and **#25**
     (websockets/uvicorn depwarn) — both upstream-blocked.

## Open decisions & risks
1. **PR #171 open** — `main` at `ed8dfec`. Branch HEAD `41fcd86`. First action
   next session: confirm CI green + merge (§0).
2. **Watch the `arms.py` parenthesisation in CI** *(this session's near-miss)* —
   `41fcd86` is the fix; if CI is red on `test_arms.py`, re-check that the
   committed `build_lexical_tsquery` has the `len(terms) == 1` early-return
   (bare form) and the `f"({inner})"` multi-term form.
3. **macOS test noise** *(carried)* — `test_daemon_control_socket.py` fails
   locally on macOS (`AF_UNIX path too long`); pre-existing, present on `main`,
   env-specific. Deselected from the local gate; CI on Linux is the real signal.
4. **No ROADMAP.md** in this repo *(carried)* — slice status lives in
   NEXT_SESSION/handoffs + the specs. The `/nextsession` ROADMAP step is a no-op.
5. **`.claude/` local files** stay untracked, by design (incl.
   `.claude/scheduled_tasks.lock` — shows as the lone uncommitted change).
6. **Process note** *(this session)* — built via subagent-driven development; one
   implementer front-loaded production code into a single commit (RED step not
   genuine for a few sub-tasks) and another left the parens fix uncommitted. Both
   caught by review + the `gh` uncommitted-changes warning. End state is correct,
   but **always diff committed-vs-working-tree before declaring done**.

## Exact commands to resume
```bash
cd /Users/hherb/src/localmail
git fetch --prune origin                 # ALWAYS first

git status                               # clean apart from .claude/*.lock
git branch -vv                           # main (ed8dfec) + smart-query-rewriter (41fcd86)
git --no-pager log --oneline -8
gh pr list --state open                  # #171 (--smart query rewriter)
gh pr checks 171                          # CI status before merging
gh issue list --state open --limit 40    # #90, #25 (both upstream-blocked)

# Confirm the suite (the live, canonical signal this session):
unset VIRTUAL_ENV && uv run pytest -q tests/ --deselect tests/test_daemon_control_socket.py
                                                                 # expect 1491 passed
unset VIRTUAL_ENV && uv run mypy src/localmail                   # expect clean, 105 files
```

After PR #171 merges:
```bash
git checkout main && git pull
ls migrations/    # latest is 0027_import_jobs_owner.sql; next free slot 0028_*.sql
```

`main` at `ed8dfec` (== `origin/main`). Branch `smart-query-rewriter` pushed
(HEAD `41fcd86`), open as **PR #171**. Working tree clean apart from the
untracked `.claude/scheduled_tasks.lock`. **No migration this session.**
