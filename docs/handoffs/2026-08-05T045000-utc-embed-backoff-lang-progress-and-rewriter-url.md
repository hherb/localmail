# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-08-05 (session 17).** This session closed **#259** (the
> embed worker's backoff ignored language-detection progress) and **#235** (a
> malformed rewriter base URL reported "could not reach the rewriter service"
> forever), through PRs **#262** and **#263**. **Both are open, CI-green, and
> awaiting your merge** — nothing from this session is on `main` yet, and
> neither host has been redeployed. Session 16's deployments were re-verified
> intact first: Mac `claimable = 0 / implausible = 350`, all three DGX units
> `active`, probe control reading `lan=ok@192.168.1.99`.

## Project context (1-minute version)

`localmail` mirrors IMAP accounts (Gmail OAuth, password) into Postgres,
**strictly read-only w.r.t. IMAP**. The database is canonical for accounts.
Daemon: hot-reload account set, heartbeats, DB command queue, two-plane
supervision. Web admin UI (HTMX): account CRUD, user management, archive
imports, daemon control. Hybrid search (Phases 1+2) + an HTTPS GUI server + a
remote MCP server (optionally a full OAuth 2.1 authorization server) + the
opt-in `--smart` LLM query rewriter are all shipped. A Tauri 2 + Svelte 5 GUI
lives under `gui/` — read-only viewer plus an admin mode (Accounts + Daemon
panels shipped; Users + Imports still placeholders). Licensed AGPL-3.0-or-later
(per-file SPDX headers in `src/localmail/`; **not** in `gui/`).
See [CLAUDE.md](CLAUDE.md), [README.md](README.md).

## What we shipped this session

### 1. #259 — the embed worker's backoff sees language progress (PR #262 → `55aa287`)

`run_embed_worker_once` returned a bare count of embedded chunks, which
`run_embed_worker` read as "did this sweep do work". But the sweep also runs one
`body_lang_detect_batch_size` slice (default 200) of language detection, so a
sweep that laboured through 200 rows reported `0`, the loop concluded the queue
was empty, and it slept the full backoff (up to 5 s × 7 = 35 s).

That is **~340 rows/min**. Harmless in steady state — new mail arrives far below
one lang batch per sweep, so the queue really is empty on essentially every
sweep — but on the 100k-row backlog #251 unwedged it was the difference between
~5 hours and the ~25 minutes `lang-backfill` took on the same queue. That
asymmetry is how the issue was noticed at all.

The issue offered two resolutions (fix the backoff, or document why it doesn't
reset). **You chose the fix.** The sweep now returns
`SweepOutcome(embedded, lang_visited)` and the loop backs off only when neither
queue advanced. Expected effect on a backlog: ~5.7 rows/s → ~33 rows/s, i.e.
roughly what `lang-backfill` itself manages.

- **New pure [`search/sweep_pacing.py`](src/localmail/search/sweep_pacing.py)**
  owns *both* halves of the decision — `SweepOutcome.made_progress` (what counts
  as work) and `next_idle_streak` / `sweep_sleep_seconds` (how long to sleep on
  it). Writing them apart is precisely what produced the defect; co-locating
  them mirrors `blob_temps.py` and `shutdown_budget.py`.
- **`SweepOutcome.__bool__` raises `TypeError`.** `LangDetectPass` merely
  declines to define one, which leaves `if not result:` silently always-False.
  Raising makes the implicit read that caused #251 *and* #259 impossible rather
  than just discouraged. `lang_visited` counts rows **visited**, not labelled.
- **The hardcoded backoff cap of `6` becomes
  `search.embed_worker_idle_backoff_max_steps`** (CLAUDE.md: no magic numbers in
  search code). `next_idle_streak` takes `max_steps` keyword-only **with no
  default**, so config stays the one authority. `0` disables the backoff.
- **`embed-backfill` still breaks its first loop on `sweep.embedded`** — it has
  a second tight loop for the language queue. The three acceptance harnesses
  break on `made_progress` (they want a fully-populated corpus and cannot spin,
  since every visited row is stamped attempted).

**The key regression test was verified to discriminate**: breaking
`made_progress` back to `self.embedded > 0` fails it with `10.0 != 5.0` — the
backoff kicking in. Worth noting because the same test *passed accidentally*
against the pre-fix code (a dataclass is never `== 0`, so the old `wrote == 0`
check fell through); without deliberately breaking the new code it would have
looked like a valid RED.

### 2. #235 — a malformed rewriter base URL fails loud at construction (PR #263 → `7319f9c`)

A bad `ollama_host` / `rewriter_openai_base_url` / `rewriter_anthropic_base_url`
surfaced per request as `rewrite_note_code: unreachable`, "could not reach the
rewriter service" — a permanent `config.toml` typo in transient wording, on
every search forever.

`InvalidRewriterUrl` is now raised at construction, as a sibling of
`MissingApiKey`: same `RewriteParseError` base, same `create_searcher` guard,
same degradation to "no `--smart`". **No wire contract changed** — option 2 in
the issue (a new `invalid_config` note code) would have added a value to an enum
documented across CLAUDE.md, the MCP tool docstrings, and the HTTP schema.

**`httpx.URL` alone is not a validator, and that is the finding.** Measured on
httpx 0.28.1:

| value | `httpx.URL(...)` |
|---|---|
| `http://localhost:notaport` | **raises `InvalidURL`** |
| `localhost:11434` | OK — `scheme='localhost'`, `host=''` |
| `not a url` | OK — `scheme=''`, `host=''` |
| `ftp://x` | OK — `scheme='ftp'` |
| `http://` | OK — `host=''` |

Only the unparseable port raises. The **far more common** mistake — omitting the
scheme — parses happily and then fails at request time as an `HTTPError`, i.e.
the same misleading "unreachable" through a different door. So the rule (the
pure [`search/rewriter_url.py`](src/localmail/search/rewriter_url.py)`::base_url_error`,
shaped like `account_names.py::account_name_error`) is scheme-in-`{http,https}`
**and** non-empty host, *plus* the httpx parse so nothing passing it can raise
later. The `httpx.InvalidURL` catch in `Searcher.search` from #229 stays as the
backstop, per the issue's Notes.

Each backend declares `base_url_setting`. Stringly-typed on purpose: the name is
what lets the error tell the operator which key to edit, and a subclass omitting
it trips an assert rather than silently skipping the check. The existing startup
WARNING already interpolates the exception, so it now reads:

```
rewriter init failed (backend='ollama' model='...'): [search] ollama_host =
'localhost:11434' has scheme 'localhost'; it must start with http:// or
https:// — continuing without --smart
```

### 3. Verification

- `uv run pytest -q --deselect tests/test_daemon_control_socket.py` →
  **2244 passed, 0 failed** (was 2205; **+39** — `test_sweep_pacing.py` (12),
  `test_rewriter_url.py` (14), plus additions to `test_embed_worker.py` and
  `test_rewriter_backends.py`)
- `uv run mypy src/localmail` → clean, **137** source files (was 135)
- `uv run ruff check src/localmail` → the pre-existing **10**, none in touched
  files (verified against `HEAD` by stashing)
- CI green on **both** PRs (Linux, PG pg18, Python 3.12)

### 4. Not deployed

Neither host was touched. Both still run `19be6a2` from session 16, which is
correct — the two fixes are on unmerged branches. **Deploy after merging**, per
the resume commands below.

## What's next

### 0. Merge the two open PRs, then deploy

```bash
gh pr checks 262 && gh pr merge 262 --squash   # #259
gh pr checks 263 && gh pr merge 263 --squash   # #235
```

**Acceptance:** both merged, `main` green. Then redeploy both hosts (risk 15 —
`--all-extras` on the Mac, `--extra mcp --extra extraction` on the DGX) and
confirm heartbeats. Neither fix needs a migration or a backfill; #259's effect
is only visible when a lang backlog exists, which on both hosts is currently
zero, so **expect no observable change** — that is correct, not a failed deploy.

### 1. **Remaining robustness issues** *(carried)*
   - **#218** — GUI download commands buffer the full body before enforcing the
     size ceiling.
   - **#226** — self-signed cert misses the reachable IP when `--bind 0.0.0.0`.
   - **#225 / #227** — `/v1/changes` subscription lifecycle gaps.
   - **#200 / #211 / #208** — admin panels silently swallow 4xx; surface as a toast.
   - **#206** — GUI AccountForm: folder filters not editable.

### 2. **Admin GUI phase 5 — Users & ACL panel** *(carried, still the design's next slice)*
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
   `AdminView.test.ts` and `MainView.test.ts`** (see risk 13).

### 3. **Smaller, deliberately not done**
   - **`cli.py` is 1904 lines** and `daemon.py` is 567, both over the 500-line
     guideline. `cli.py` grew 3 lines this session. A real refactor, and each
     session adds to it.
   - **165 `docling: File format not allowed` failures on the Mac** (of 182
     total). Legitimate content-level failures, but each burns `retry_count`
     three times for a format docling will never accept.
   - **The residual implausible labels are dominated by `ja`** — 229 of the
     Mac's 350 and 57 of the DGX's 120. That is 0.24% of Mac labels, against 17%
     before #255. **Deliberately left alone**: below the noise floor of the
     original complaint, and the obvious lever (raising the confidence floor)
     was measured useless. If ever worth chasing, **sample the `ja` rows first**
     — do not assume they are the same failure mode `yo` was.
   - **The DGX drops remain uninvestigated and unexplained** (risk 5).

## Open decisions & risks

1. **Both PRs are unmerged; `main` is unchanged** *(new)*. `git log
   origin/main..` on either branch shows the single commit. If you resume before
   merging, **branch from the relevant PR branch, not `main`**, or you will
   rebuild what is already there.
2. **`SweepOutcome.__bool__` raises, unlike `LangDetectPass`** *(new)*. That
   asymmetry is deliberate — see the #259 notes in CLAUDE.md. If you ever
   "harmonise" the two, harmonise *upward* (make `LangDetectPass` raise too);
   making `SweepOutcome` merely undefined re-opens the silent-always-False hole.
   Not done here only because it is #251's code and out of #259's scope.
3. **A test that passes against the pre-fix code is not a RED** *(new)*. #259's
   central loop test did exactly that (a dataclass is never `== 0`). When the
   API itself is changing, deliberately break the *new* implementation and watch
   the test fail — that is the only proof it discriminates.
4. **`httpx.URL` is permissive; do not use it as a URL validator** *(new)*. Only
   an unparseable port raises. Everything else — missing scheme, no host, bare
   path — parses into an unusable URL. See the table under §2.
5. **The DGX drops are STILL UNEXPLAINED — five theories refuted, four of them
   mine** *(carried; not investigated this session)*. **Do not propose a sixth
   without a captured outage in which the host was demonstrably up throughout.**
   Triage with `journalctl --list-boots` first. **Power is not a candidate**:
   the DGX is on a ~5-day UPS and the journal holds exactly one unclean stop, a
   scheduled PSU install. Reach it on the LAN, but **look the address up** — it
   is DHCP and has been `192.168.68.62`, `192.168.68.76`, and now
   `192.168.1.99`, on two different subnets. **Do not edit
   `/etc/wireguard/wg0.conf`.** The probe's control column was broken from Aug 4
   until PR #260 — **any `lan=FAIL` in a log line without an `@addr` suffix
   proves nothing.**
6. **A single `tunnel=FAIL` probe sample is not an outage** *(carried)* — it is
   Starlink losing three packets on a ~900 ms path. Sustained means several
   consecutive samples. Both probes are still running.
7. **`--relabel` is the only destructive verb in the lang path** *(carried)*. It
   discards **every** label, so the archive is unsearchable by `lang:` until the
   drain completes. Prompts unless `--yes`. Reach for `--retry-declined` first.
   Budget **~45 minutes for a 100k-row archive** (the ~176 rows/s figure is
   detection only; the real pass pays a per-row UPDATE and competes with the
   daemon). `reopen_all`'s bulk UPDATE shows **no** progress in
   `pg_stat_activity` until it commits — reading "nothing has changed" in the
   first minutes is expected, not a stall.
8. **`body_lang_pending` means claimable work only** *(carried)*; the turned-away
   remainder is `body_lang_declined`. A steady non-zero `declined` is **normal**.
9. **Do not add normalisation steps to `lang_text.py` without a measurement**
   *(carried)*. Invisible-character, email, HTML-tag and separator-rule
   stripping were each measured at **zero** benefit once URLs are gone.
   `body_lang_low_accuracy` is retained but measured strictly worse (239 MB vs
   227 MB, 2.3× slower, far more mislabels) — an escape hatch only.
10. **`ExtractorConfigurationError` subclasses `TransientExtractorError`, and
    that subclassing *is* the #248 fix — do not "clean it up"** *(carried)*.
11. **`type-skipped` is a one-way door for a widened allowlist** *(carried)*.
    Clear deliberately: `DELETE FROM attachment_text WHERE extractor =
    'type-skipped'`. #251, #255 and #235 all deliberately avoided this shape.
12. **Test trap: `_try_import_docling` must be monkeypatched in BOTH namespaces**
    *(carried)*. **OCR is macOS-only by default**; installing the `[extraction]`
    extra is the whole fix on Linux (~5.5 GB of venv on aarch64).
13. **CI trap** *(carried)*: any GUI admin panel that fetches on mount MUST be
    stubbed in **both** `AdminView.test.ts` and `MainView.test.ts`.
14. **`uv sync` without extras silently downgrades a host** *(carried)*. Use
    `--all-extras` on the Mac and `--extra mcp --extra extraction` on the DGX.
    **`uv` is not on the DGX's default non-interactive PATH** — use
    `~/.local/bin/uv` over SSH.
15. **Do not run the test suite while a backfill is draining** *(carried)* —
    contention on the shared cluster produces dozens of false failures.
16. **macOS test noise** *(carried)* — `test_daemon_control_socket.py` fails
    locally (`AF_UNIX path too long`); deselect it, Linux CI is the real signal.
    The warning count flickers 3↔4 between runs (a psycopg_pool teardown
    `ResourceWarning`); also carried are the websockets `DeprecationWarning`
    (#25), the Starlette TestClient `httpx` one, and jsdom canvas noise.
17. **The stale NOTIFY queue recurs** *(carried)* — if exactly the three
    LISTEN/NOTIFY tests fail with "could not access status of transaction N", it
    is Fault 1 in the postgres-maintenance runbook, not a code bug. Cycling the
    daemon clears it, but **give it a minute** before re-running.
18. **No ROADMAP.md** *(carried, re-confirmed)* — the `/nextsession` ROADMAP step
    is a no-op; slice status lives in NEXT_SESSION + `docs/handoffs/` + the specs.
    README **was** updated this session (the new backoff knob, the daemon's own
    lang drain, and the base-URL scheme requirement).
19. **Secrets/ACL invariants unchanged** *(carried)*: `secrets.configure`'s pin
    is kept even though #245 is fixed; #246 warns rather than refuses on
    group-write; `InsecureSecretsFile` refuses rather than warns; #239's manual
    tombstone retention is deliberate; admin bearer tokens have no per-token
    scope (#204).
20. **Run vitest from `gui/`, not the repo root** *(carried)*.
    **`cargo clippy --all-targets` is clean but ungated** *(carried)*.

## Exact commands to resume

```bash
cd /Users/hherb/src/localmail
git fetch --prune origin                 # ALWAYS first
git status                               # expect clean
git branch --show-current                # main (after merging both PRs)
git log --oneline origin/main..main      # expect 0

# If the PRs are still open, MERGE FIRST (risk 1) — do not rebuild them:
gh pr checks 262 && gh pr merge 262 --squash   # #259 embed-worker backoff
gh pr checks 263 && gh pr merge 263 --squash   # #235 rewriter base URL

# Python test suite (deselect the macOS-only socket failure — see risk 16).
# Do NOT run this while a backfill is draining — see risk 15.
unset VIRTUAL_ENV && uv run pytest -q --deselect tests/test_daemon_control_socket.py
#   expect: **2244 passed, 0 failed**
unset VIRTUAL_ENV && uv run mypy src/localmail
#   expect: Success, 137 source files

# Deploy after merging (risk 14 — extras are NOT optional):
git pull && uv sync --all-extras && launchctl kickstart -k gui/$UID/com.localmail.daemon
launchctl print gui/$UID/com.localmail.daemon | grep -E '^\s*(state|pid) '
psql -h localhost -p 5532 -U localmail -d localmail -c \
  "SELECT worker_kind, account_id, state, now()-last_heartbeat_at AS age
     FROM daemon_heartbeats ORDER BY worker_kind, account_id"
#   expect: 7 rows, ages under ~60 s, both accounts present

# The lang state should be UNCHANGED by this session's fixes (see What's next §0):
psql -h localhost -p 5532 -U localmail -d localmail -c "
  SELECT count(*) FILTER (WHERE body_lang IS NOT NULL) AS populated,
         count(*) FILTER (WHERE body_lang IS NULL AND body_text IS NOT NULL
                            AND body_lang_attempted_at IS NULL) AS claimable,
         count(*) FILTER (WHERE body_lang IS NULL AND body_text IS NOT NULL
                            AND body_lang_attempted_at IS NOT NULL) AS declined,
         count(*) FILTER (WHERE body_lang IS NOT NULL AND NOT (body_lang = ANY(
           ARRAY['en','de','fr','es','it','nl','sv','da','no','pt']))) AS implausible
    FROM messages"
#   expect (Mac): populated 95627, claimable 0, declined 12166, implausible 350

# The DGX — LAN only, and look the address up; it is DHCP (risk 5).
# Note uv is NOT on the non-interactive PATH there (risk 14):
ssh 192.168.1.99 'systemctl --user is-active localmail-daemon localmail-serve localmail-wgprobe'
ssh 192.168.1.99 'cd ~/src/localmail && ~/.local/bin/uv run localmail search-status'

# The probe's control column records which address answered (PR #260):
tail -5 ~/localmail-probe/tunnel-probe.log   # expect lan=ok(3/3)@<addr>

# Frontend — only if you touch gui/ (MUST be run from gui/ — see risk 20):
cd gui && npm run check && npm test && npm run build && cd ..
cd gui/src-tauri && cargo test && cargo clippy --locked -- -D warnings \
  && cargo clippy --all-targets -- -D warnings && cd ../..
```

Session-17 code is **`55aa287`** (PR #262, #259) and **`7319f9c`** (PR #263,
#235) — **both unmerged**. Both deployments still run **`19be6a2`** from session
16, which is correct until the PRs land.

Latest migration **`0035_messages_body_lang_attempted_at.sql`**; next free slot
`0036_*.sql`. **Open issues: 12** — #259 and #235 close on merge, taking it to
**10**. Dependabot: **0** open alerts.
