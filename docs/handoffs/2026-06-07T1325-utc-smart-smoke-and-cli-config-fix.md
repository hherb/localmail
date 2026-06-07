# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-06-07 (Phase 4 `--smart` post-merge: live smoke + a CLI
> `--config` fix).** PR #171 (`--smart` LLM query rewriter) is **merged** into
> `main` at `809e724`. This session (a) ran the deferred **live `--smart`
> manual smoke** against a real Ollama LLM — all three acceptance signals
> passed — and (b) shipped a small **`fix(cli): search honours --config`** on
> branch `fix-cli-search-config-flag` (commit `fd62df0`), a pre-existing gap the
> smoke surfaced. Suite green (**1493 passed, 14 deselected**), mypy clean (105
> files). `main` == `origin/main` == `809e724`; the fix is on its branch, **not
> yet pushed / no PR** at handoff time.

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

### A. `--smart` live smoke validation (no code)

The prior handoff's only actionable, non-blocked item was the deferred manual
smoke (it had not run — no live Ollama in that loop). Ran it against the **live
archive (33,934 messages)**.

**Setup.** Live Ollama at `http://localhost:11434`. The config default
`rewriter_model = "qwen2.5:3b"` is **not** pulled, so the smoke used an already-
present model **`granite4.1:3b-q8_0`** via a *temporary* config copy (the real
`~/.config/localmail/config.toml` was **not** mutated). The rewriter is gated on
`[search] rewriter_enabled_by_default = true`; the temp config set that + the
model + a 30s timeout. Model warmed first (~2.5s).

**Query:** `"tax stuff from the accountant last summer"`.

**All three acceptance signals confirmed:**
1. ✅ **`timing(ms)` carries a `rewrite` key** — `rewrite≈1860ms`; top result a
   *"Monthly Tax Scoop"* from *Medical Accounting Services* (on-intent).
2. ✅ **NL → structured extraction.** Direct `OllamaLLMRewriter.rewrite()`
   (fixed `today=2026-06-07`): a richer `rewritten_text`; `expansion_terms` =
   8 terms (at the `rewriter_max_expansion_terms=8` cap); `extracted_filters` =
   `subject_substr='tax'`, `after=2026-03-01`, `before=2026-06-30`. (The exact
   "last summer" window is the 3B model's judgment, not a code defect — the
   relative-date-grounding + filter-extraction *mechanism* is proven.)
3. ✅ **Graceful, surfaced fall-through.** Pointing the rewriter at a dead port
   (same `(httpx.HTTPError, RewriteParseError)` catch) logged `smart rewrite
   skipped: [Errno 61] Connection refused`, printed `note: --smart rewrite
   skipped …`, and **still returned results** from the un-rewritten query.

### B. `fix(cli): localmail search honours --config` (`fd62df0`)

The smoke surfaced a **pre-existing** gap (NOT a Phase-4 regression): `localmail
search` called `create_searcher()` with no args, so it **ignored the global
`--config PATH` flag** and always re-read the default config for the searcher —
the only override was the `LOCALMAIL_CONFIG` env var.

**Fix (TDD):** `search` gains `@click.pass_context` and calls
`create_searcher(load_config(ctx.obj["config_path"]))`. The `serve` command's
`create_searcher()` is likewise passed `cfg` — `None` in the
`LOCALMAIL_DSN_OVERRIDE` branch (now explicitly bound), where `create_searcher`
loads the default config itself, so that branch's behaviour is unchanged. New
regression test `test_cli_search_honours_config_flag` asserts a `--config`
file's `[search]` block reaches the searcher; the existing
`test_search_prints_notice_when_rewrite_skipped` lambda was widened to accept
the now-passed `cfg` arg. Confirmed live: `localmail --config /tmp/x.toml search
… --smart` now enables the rewriter without `LOCALMAIL_CONFIG`.

**No migration. No README/ROADMAP change** (README already documents `--smart`;
ROADMAP.md doesn't exist in this repo). Temp smoke configs removed.

## What's next

### 0. **Push + PR the `--config` fix** *(immediate)*
```bash
git push -u origin fix-cli-search-config-flag
gh pr create --fill            # or with a title/body
```
**Acceptance:** CI green on Linux; squash-merge; `git checkout main && git pull`.
The macOS-only `test_daemon_control_socket` AF_UNIX failures are local env noise.

### 1. **Phase-4 follow-ups (filed-as-notes, low priority; non-blocking)**
   - **Default `rewriter_model`** — config default is `qwen2.5:3b`, which must be
     pulled separately. Either document the `ollama pull qwen2.5:3b` pre-req
     prominently in the README `--smart` section, or change the default to a
     model the project already standardises on. Decision needed (don't silently
     change a default users may rely on). **Acceptance:** a fresh-Ollama user
     either sees a clear pre-req note or `--smart` works out of the box.
   - **Rewrite-result caching** — each `--smart` query hits Ollama fresh
     (deliberately out of scope). Add an LRU keyed on free-text if it proves hot.
     **Acceptance:** repeated identical `--smart` query shows a near-zero
     `rewrite` timing on the 2nd call; cache bounded + per-process.
   - **MCP / HTTP `smart=` param** — the rewriter is wired into the Python
     `Searcher` + CLI; exposing it over MCP `search` + HTTP `/v1/search` is a
     separate small slice (thread a `smart` bool + surface `rewrite_skipped` on
     the wire). **Acceptance:** MCP `search(smart=true)` runs the rewriter and a
     response field reflects `rewrite_skipped`.
   - **Cloud/other rewriter backends** — `rewriter_backend` Literal stays
     `"ollama"`; widen only if a non-local backend is wanted (privacy tradeoff).
   - Each non-trivial item needs its own brainstorm → spec → plan.

### 2. **Remaining MCP follow-ups (carried, low priority)**
   - Full OAuth 2.1 discovery (Approach B); `streamablehttp_client` rename
     (deprecated in `tests/test_mcp_integration.py`); `--smart` for MCP (see §1).

### 3. **Upstream-blocked (not actionable)**
   - **#90** (glib via Tauri stack bump; Dependabot alert) and **#25**
     (websockets/uvicorn depwarn) — both upstream-blocked.

## Open decisions & risks
1. **`--config` fix is committed but unpushed** — branch
   `fix-cli-search-config-flag` @ `fd62df0`, base `main` @ `809e724`. First
   action next session is §0 (push + PR + merge).
2. **`rewriter_model` default is unpulled** *(carried into §1)* — `qwen2.5:3b`
   is the config default but absent from a fresh Ollama; `--smart` silently
   falls through (logged) until the operator pulls it or overrides the model.
   Correct *behaviour* (no-silent-failure honoured) but a sharp UX edge.
3. **macOS test noise** *(carried)* — `test_daemon_control_socket.py` fails
   locally on macOS (`AF_UNIX path too long`); pre-existing, present on `main`,
   env-specific. Deselect from the local gate; CI on Linux is the real signal.
   (Also: pre-existing psycopg_pool teardown `ResourceWarning`s in the suite
   tail — unrelated, harmless.)
4. **No ROADMAP.md** in this repo *(carried)* — slice status lives in
   NEXT_SESSION/handoffs + the specs. The `/nextsession` ROADMAP step is a no-op.
5. **`.claude/` local files** stay untracked, by design (incl.
   `.claude/scheduled_tasks.lock` — the lone uncommitted entry once docs land).

## Exact commands to resume
```bash
cd /Users/hherb/src/localmail
git fetch --prune origin                 # ALWAYS first

git status                               # clean apart from .claude/*.lock
git branch -vv                           # main (809e724) + fix-cli-search-config-flag (fd62df0)
git --no-pager log --oneline -6
gh pr list --state open                  # expect none until §0 pushes
gh issue list --state open --limit 40    # #90, #25 (both upstream-blocked)

# §0 — push + PR the --config fix:
git checkout fix-cli-search-config-flag
git push -u origin fix-cli-search-config-flag
gh pr create --fill

# Suite + types:
unset VIRTUAL_ENV && uv run pytest -q tests/ --deselect tests/test_daemon_control_socket.py
                                                                 # expect 1493 passed
unset VIRTUAL_ENV && uv run mypy src/localmail                   # expect clean, 105 files
```

### Reproduce the `--smart` live smoke (needs a running Ollama)
```bash
# Pick an installed instruct model (granite4.1:3b-q8_0 used this session) and
# enable the rewriter via a TEMP config so the real config stays untouched:
cp ~/.config/localmail/config.toml /tmp/lm_smoke.toml
cat >> /tmp/lm_smoke.toml <<'TOML'

[search]
rewriter_enabled_by_default = true
rewriter_model = "granite4.1:3b-q8_0"   # or `ollama pull qwen2.5:3b` for the default
rewriter_timeout_s = 30.0
TOML
# --config now works for search (fixed this session — fd62df0):
unset VIRTUAL_ENV && uv run localmail --config /tmp/lm_smoke.toml \
  search "tax stuff from the accountant last summer" --smart --verbose
# Expect: timing(ms) includes a 'rewrite' key; on-intent results. Point
# ollama_host at a dead port to see the 'note: --smart rewrite skipped …'
# fall-through still return results.  Then: rm /tmp/lm_smoke.toml
```

`main` at `809e724` (== `origin/main`). Branch `fix-cli-search-config-flag` @
`fd62df0` (unpushed). Latest migration `0027_import_jobs_owner.sql`; next free
slot `0028_*.sql`. **No migration this session.**
