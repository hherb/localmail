# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-10-29 (session 62).** `main` was **`b6fd32f`** at
> session start (#410 merged: Pillow 13, fastembed 0.9.1, docling 2.142.1).
>
> This session:
> - **Mac synced to #410 and restarted.** `/v1/version` → `build_hash:
>   b6fd32f`; venv holds Pillow 13.0.0, docling 2.142.1, fastembed 0.9.1.
>   Worktree `../localmail-pillow13` and its branch removed.
> - **DGX NOT synced.** The auto-mode classifier refused the remote
>   `git pull` + `uv sync` + restart over ssh. It is still on `9154672` with
>   Pillow 12. **Step 0 below is yours.**
> - **#395 fixed: PR #411 on `fix/395-forbid-html-sinks`.**
>
> **Open issues: 37** (36 once #411 merges).
>
> **Leftover worktree `.claude/worktrees/bottie-bearer-token-dbc203`**
> (detached at `5b85180`). Untouched; ask before removing it.

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

### PR #411: `fix/395-forbid-html-sinks` (opened on `main`)

| SHA | What |
|---|---|
| `aa38bd1` | `tests/_html_sink_rules.py` + `tests/test_html_sink_rules.py` (8 tests); CLAUDE.md #391 bullet and Layout |
| handoff | this file + `docs/handoffs/2026-10-29T0841Z-forbid-html-sinks.md` |

- **The rule:** `html_sink_error` reads every `.svelte` under `gui/src` and
  reports each `{@html}` sink as `path:line`. The allowlist `ALLOWED_SINKS`
  is empty. An empty scan is reported as an error, not passed.
- **The pattern is `\{\s*@html\b`.** Svelte 5.56's `tag.js:26` calls
  `allow_whitespace()` right after `{`, so `{ @html x}` compiles too.
- **It matches text, not a parse tree.** It fails closed: a `{@html` inside
  a markup comment would be reported. Today the tree has zero hits.
- **It runs in python-ci**, whose checkout includes `gui/`.

### Deployment (Mac)
- `uv sync --frozen --extra mcp --extra extraction`, then both agents
  kickstarted. The lock moves matched #410's list exactly.

## Verification

| check | result |
|---|---|
| mutation: `{@html snippet}` planted in `MessageListRow.svelte` | `test_the_gui_has_no_html_sink` fails, 7 pass |
| mutation: pattern without `\s*` | `test_whitespace_after_the_brace_is_still_a_sink` fails, 7 pass |
| pytest (macOS, all extras) | **4178 passed**, 6 warnings, 262 s (4170 + 8) |
| CI | see `gh pr checks 411`; I stopped at the push |

- Load was 9–14 (another suite on its own DB); timings are not comparable.
- The change is Python test-only, so mypy (which covers `src/` only),
  cargo and vitest were not re-run.

## What's next

### 0. **Sync the DGX to `b6fd32f`** (yours: the classifier blocks remote writes)
```bash
ssh 10.0.0.3 'cd ~/src/localmail && git pull --ff-only && ~/.local/bin/uv sync --frozen --extra mcp --extra extraction && systemctl --user restart localmail-daemon localmail-serve'
```
- **Acceptance:**
  - `curl -sk https://10.0.0.3:8443/v1/version` reports `b6fd32f`, or the
    #411 merge commit if you pull after merging.
  - `.venv/bin/python -c "import PIL; print(PIL.__version__)"` prints
    13.0.0 there.
  - Both units are `active`.
- Or allow it: add a Bash permission rule for `ssh 10.0.0.3 …`, and the
  next session runs it.

### 1. **Merge #411** (yours, once CI is green)
- `Closes #395` is in the PR body; check with `gh issue view 395` after
  the merge.
- Nothing to deploy: the change is test-only.

### 2. **Dependabot** — two open, both left open on purpose
- **#71 accelerate:** no fix exists; unchanged.
- **#185 deepmerge** (new, LOW, `>= 4.2.0, <= 4.3.1`, no patched release):
  dev-scope only, pulled in by `@sveltejs/vite-plugin-svelte` 7.1.2 at build
  time. It never ships in the GUI and never touches mail.
  - **Acceptance:** bump when a fixed deepmerge or vite-plugin-svelte
    release exists (`npm audit fix`, then `npm test -- --run && npm run
    check`).
- **#115 rustls** is closed now; nothing to do.

### 3. **Watch for docling > 2.144** (carried)
- Re-run the probe under rapidocr against any new release. If
  `native_table` passes, move the lock forward and lift the `docling<2.143`
  cap.
- **Acceptance:** the lock is on the newest release whose probe exits 0
  on both hosts.

### 3a. **emSign G3 in the OS trust stores** (carried)
- Re-check after each OS or `ca-certificates` update (commands below).

### 4. **kastellan** (carried, yours)
- Ask whether it still wants slice C (`/problems/token-expired`).
- Tell it about `?headers=list` (api_minor ≥ 1), attachment index paging
  (≥ 2), and `fields` / `snippet_chars` (≥ 3) with the 48-entry cap.

### 5. **Carried issues**
- **Next small slice:** #386, #389 or #380. Read each one first and pick
  the smallest.
  - **Acceptance:** whatever the issue states, a RED test first, and a
    mutation of the fix fails that test.
- **Then:** #383, #384, #378, #369, #373, #372, #370, #371, #368, #365.
- **Not filed yet:** the `snippet_source="header"` mislabel on a
  whole-message lexical win.
- **Larger work:**
  - #375; #374 + #305 (the `cli.py` refactor).
  - #358/#357, #330/#327/#328, #319/#318/#320 (next migration `0037_*.sql`).
  - #340, #363, #285.
  - Lifting the rapidocr cap; admin GUI phase 5 (Users panel).
  - Robustness: #218, #226, #225/#227, #200/#211/#208, #206, #204, #25.

## Rulings made this session

1. **The Mac deploy went ahead without a fresh confirmation**, per the last
   handoff's step 0. The DGX half was refused by the classifier and is left
   to you.
2. **The rule matches text, not a parse tree**, because parsing Svelte from
   Python needs a second parser. The documented cost is a false positive on
   a `{@html` inside a comment, which fails closed.
3. **pytest, not vitest.** This follows the house shape for pure rule
   modules. python-ci checks out `gui/` too, so it gates the change on CI.
4. **No stale-allowlist check.** The allowlist is empty; add the check
   with the first entry.

## Open decisions & risks

1. **The DGX is still on Pillow 12 / docling 2.141** until step 0 runs.
   The code on it is the older #409, so it is self-consistent, only out of
   date.
2. **The native-table regression in docling ≥ 2.143 is unexplained**, only
   localised.
3. **THIS FILE IS NOT THE AUTHORITY; `git` and `gh` are.**
4. **Never run two pytest sessions against one test database.**
5. **Restore a mutation from a file copy, never with `git checkout`.**
6. **The test DB is on port 5532**, the same cluster as the live archive.
7. **Always sync with `--extra mcp --extra extraction` on both hosts**, and
   use `~/.local/bin/uv` on the DGX.
8. **`uv sync --dry-run` is not read-only; `uv lock --dry-run` is.**
9. **No ROADMAP.md.** That `/nextsession` step is a no-op.

## Exact commands to resume

```bash
cd /Users/hherb/src/localmail
git fetch --prune origin                 # ALWAYS first
git switch main && git pull --ff-only
git log --oneline -1 origin/main         # THE authority
git worktree list

gh pr view 411 --json state,mergeCommit
gh issue list --limit 200 --json number --jq length       # 36 after #411
gh api --paginate repos/hherb/localmail/dependabot/alerts \
  --jq '.[] | select(.state=="open") | .dependency.package.name' | sort | uniq -c   # accelerate, deepmerge

# Deployment health
launchctl list | grep localmail
curl -sk https://127.0.0.1:8443/v1/version
tail -3 ~/localmail-probe/tunnel-probe.log
ssh -o ConnectTimeout=8 10.0.0.3 'curl -sk https://10.0.0.3:8443/v1/version'
grep -c "emSign ECC Root CA - G3" /etc/ssl/cert.pem
ssh 10.0.0.3 'ls /etc/ssl/certs | grep -ic emsign_ecc_root_ca_-_g3'

# Load before trusting any timing; no other localmail pytest may be running.
uptime; pgrep -fl pytest

# Suites.
unset VIRTUAL_ENV && uv run pytest -q              # 4178 passed (macOS, all extras)
uv run pytest -q tests/test_html_sink_rules.py     # 8 passed — this change
uvx mypy --python-executable .venv/bin/python src/localmail   # Success, 160 files
cd gui && npm test -- --run && npm run check && npm audit
cd gui/src-tauri && cargo test --locked && cargo clippy --locked --all-targets -- -D warnings
```

`main` was **`b6fd32f`** at session start and is unchanged. This session left
**one PR**, #411 on `fix/395-forbid-html-sinks`: commit `aa38bd1`, plus this
handoff.

The latest migration is still **`0036_api_keys.sql`**; the next free slot is
`0037_*.sql`.
