# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-06-07 (Phase 4 default-model UX follow-up).** The prior
> handoff's §0 (push + PR the CLI `--config` fix) is **done** — it merged as
> **PR #172** at `d4d328a` (squash-merge; stale local branch deleted this
> session). This session shipped a one-value UX follow-up: **default
> `rewriter_model` changed `qwen2.5:3b` → `granite4.1:3b-q8_0`** on branch
> `change-default-rewriter-model` (commit `09bd216`), opened as **PR #173**,
> **CI running at handoff** — merge once green. Suite green locally
> (**1495 passed, 14 deselected**), mypy clean (105 files). `main` ==
> `origin/main` == `d4d328a`; #173 not yet merged.

## Project context (1-minute version)

`localmail` mirrors IMAP accounts (Gmail OAuth, password) into Postgres,
**strictly read-only w.r.t. IMAP**. The **database is canonical for accounts**
end-to-end. Daemon: hot-reload account set, heartbeats, DB command queue,
two-plane supervision, non-blocking lifecycle + admin panel (2B.1–2B.5). Admin
UI: account CRUD (2A.3), user management (2A.4), archive imports (2A.5). Hybrid
search (Phases 1+2) + an HTTPS GUI server + a remote MCP server (Phase 3) +
the opt-in `--smart` LLM query rewriter (Phase 4) are all shipped. A Tauri +
Svelte GUI lives under `gui/`. See [CLAUDE.md](CLAUDE.md), [README.md](README.md).

## What we did this session

### A. Cleaned up the prior handoff's §0 (already merged)

PR #172 (`fix(cli): localmail search honours --config`) had merged into `main`
at `d4d328a` between sessions. Verified the squash-merged branch
`fix-cli-search-config-flag` content is fully in `main` (empty diff), then
force-deleted the stale local branch. **§0 is closed — no action carried.**

### B. `fix(search): default rewriter_model to granite4.1:3b-q8_0` (`09bd216`, PR #173)

Resolves Open Decision #2 from the prior handoff (the unpulled-default UX edge).
`qwen2.5:3b` is rarely already pulled on a fresh Ollama, so enabling `--smart`
without a manual `ollama pull` made the rewriter silently fall through to the
un-rewritten query (correctly logged + noted, but a sharp first-run edge).
Switched the default to **`granite4.1:3b-q8_0`** — the model verified working
for the structured rewrite in last session's Phase 4 live smoke.

**Decision rationale:** the README already documented the `ollama pull` pre-req,
and there is **no chat model the project otherwise standardises on** (embeddings
+ reranker use fastembed/auto-download; the rewriter is the only Ollama chat
model). So "document more" was already satisfied and "default to an existing
standard model" had no target — changing the default to a maintainer-verified
model was the proportionate fix. Behaviour is unchanged beyond the default
string: any Ollama chat model still works via `rewriter_model`, and the graceful
fall-through is intact.

**TDD:** flipped `test_ollama_happy_path`'s request-body assertion + added a new
explicit `test_default_rewriter_model` → ran **red** against `qwen2.5:3b` →
changed `src/localmail/config.py:353` → **green**. README `--smart` setup section
updated (`ollama pull …` + example `rewriter_model`, comment alignment).
**No migration.** Historical `docs/superpowers/specs|plans/*` and
`docs/handoffs/*` deliberately untouched (point-in-time records).

## What's next

### 0. **Merge PR #173** *(immediate)*
```bash
gh pr checks 173                 # wait for green (Linux CI is the real signal)
gh pr merge 173 --squash         # then: git checkout main && git pull --prune
```
**Acceptance:** CI green on Linux; squash-merge; `main` advances past `d4d328a`.

### 1. **Phase-4 follow-ups (low priority; non-blocking)**
   - **Rewrite-result caching** — each `--smart` query hits Ollama fresh
     (deliberately out of scope). Add a bounded per-process LRU keyed on
     free-text if it proves hot. **Acceptance:** repeated identical `--smart`
     query shows a near-zero `rewrite` timing on the 2nd call; cache bounded.
   - **MCP / HTTP `smart=` param** — the rewriter is wired into the Python
     `Searcher` + CLI only; exposing it over MCP `search` + HTTP `/v1/search`
     is a separate small slice (thread a `smart` bool + surface
     `rewrite_skipped` on the wire). **Acceptance:** MCP `search(smart=true)`
     runs the rewriter and a response field reflects `rewrite_skipped`.
   - **Actionable rewrite-failure message / pre-flight probe** *(considered,
     not taken this session)* — when `--smart` falls through because the model
     isn't pulled, the CLI prints a **generic** `note: --smart rewrite skipped
     (rewriter unavailable)`; the actionable Ollama 404 detail (`model '…' not
     found, try pulling it first`) is logged but not surfaced to the note.
     Changing the default reduces the odds of hitting this, but the opaque note
     remains. Optional future polish: surface the model name + `ollama pull`
     hint in the note, or pre-flight-probe at enable time. **Acceptance:** a
     missing-model fall-through tells the user which `ollama pull` fixes it.
   - **Cloud/other rewriter backends** — `rewriter_backend` Literal stays
     `"ollama"`; widen only if a non-local backend is wanted (privacy tradeoff).
   - Each non-trivial item needs its own brainstorm → spec → plan.

### 2. **Remaining MCP follow-ups (carried, low priority)**
   - Full OAuth 2.1 discovery (Approach B); `streamablehttp_client` →
     `streamable_http_client` rename (DeprecationWarning in
     `tests/test_mcp_integration.py`); `--smart` for MCP (see §1).

### 3. **Upstream-blocked (not actionable)**
   - **#90** (glib via Tauri stack bump; Dependabot alert) and **#25**
     (websockets/uvicorn depwarn) — both upstream-blocked.

## Open decisions & risks
1. **PR #173 is open, not merged** — branch `change-default-rewriter-model` @
   `09bd216`, base `main` @ `d4d328a`. First action next session is §0
   (confirm CI green + squash-merge).
2. **Opaque rewrite-failure note** *(new, low priority)* — see §1 bullet 3. The
   no-silent-failure contract is honoured (search still runs), but the user
   isn't told the root cause is a missing model. Optional polish.
3. **macOS test noise** *(carried)* — `test_daemon_control_socket.py` fails
   locally on macOS (`AF_UNIX path too long`); pre-existing, present on `main`,
   env-specific. Deselect from the local gate; Linux CI is the real signal.
   (Also: pre-existing psycopg_pool teardown `ResourceWarning`s in the suite
   tail — unrelated, harmless.)
4. **No ROADMAP.md** in this repo *(carried)* — slice status lives in
   NEXT_SESSION/handoffs + the specs. The `/nextsession` ROADMAP step is a no-op.
5. **`.claude/` local files** stay untracked, by design (incl.
   `.claude/scheduled_tasks.lock` — the lone uncommitted entry).

## Exact commands to resume
```bash
cd /Users/hherb/src/localmail
git fetch --prune origin                 # ALWAYS first

git status                               # clean apart from .claude/*.lock
git branch -vv                           # main (d4d328a) + change-default-rewriter-model (09bd216)
git --no-pager log --oneline -6
gh pr list --state open                  # expect #173 until merged
gh issue list --state open --limit 40    # #90, #25 (both upstream-blocked)

# §0 — merge the default-model PR once CI is green:
gh pr checks 173
gh pr merge 173 --squash
git checkout main && git pull --prune

# Suite + types:
unset VIRTUAL_ENV && uv run pytest -q tests/ --deselect tests/test_daemon_control_socket.py
                                                                 # expect 1495 passed
unset VIRTUAL_ENV && uv run mypy src/localmail                   # expect clean, 105 files
```

### Reproduce the `--smart` live smoke (needs a running Ollama)
```bash
# The default rewriter_model is now granite4.1:3b-q8_0 (this session). Pull it
# once, or override to any installed chat model via a TEMP config so the real
# config stays untouched:
ollama pull granite4.1:3b-q8_0
cp ~/.config/localmail/config.toml /tmp/lm_smoke.toml
cat >> /tmp/lm_smoke.toml <<'TOML'

[search]
rewriter_enabled_by_default = true
rewriter_model = "granite4.1:3b-q8_0"
rewriter_timeout_s = 30.0
TOML
# --config works for search (fixed last session — #172):
unset VIRTUAL_ENV && uv run localmail --config /tmp/lm_smoke.toml \
  search "tax stuff from the accountant last summer" --smart --verbose
# Expect: timing(ms) includes a 'rewrite' key; on-intent results. Point
# ollama_host at a dead port to see the 'note: --smart rewrite skipped …'
# fall-through still return results.  Then: rm /tmp/lm_smoke.toml
```

`main` at `d4d328a` (== `origin/main`). Branch `change-default-rewriter-model` @
`09bd216` (pushed, PR #173 open). Latest migration `0027_import_jobs_owner.sql`;
next free slot `0028_*.sql`. **No migration this session.**
