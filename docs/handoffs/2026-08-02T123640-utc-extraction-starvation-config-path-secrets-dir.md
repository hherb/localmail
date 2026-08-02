# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-08-02 (session 12).** PR #244 merged before this session
> as `4f3285f`. This session closed **#216, #245, #246** in
> **[PR #247](https://github.com/hherb/localmail/pull/247)**, merged as
> **`7fb6c71`** (CI green). #216 turned out to be **much larger than filed**:
> attachment extraction was starved archive-wide, not merely incomplete for
> mis-typed attachments — the Mac had **16,542 unprocessed blobs and 19
> extracted**. It is now draining. Two things are outstanding and neither is
> code: the **DGX is unreachable** (dropped off the network mid-session, still
> on `4f3285f`), and the **headless-secrets cold-boot proof still has not
> happened**. See §0.

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

`origin/main` was at `4f3285f`. Everything below is in **`7fb6c71`** (PR #247).

### 1. #216 — the extract queue was starved for the whole archive

Filed as "MIME-mistyped attachments silently unindexed". That was true, and it
was the smaller half.

**The filed bug.** The allowlist's extension branch read `Path(path).suffix`
off `attachment_blobs.path` — content-addressable `blobs/<aa>/<bb>/<sha256hex>`,
**no extension by construction**. Every comparison was against `""`, so
`extractor_extension_allowlist` was dead code and a real PDF sent as
`application/octet-stream` could never be indexed.

**The severe half, found while fixing it.** A turned-away blob was `continue`d
with no row and no log, so it never gained an `attachment_text` row, stayed
eligible, and was re-claimed every sweep. `_claim_batch` is
`ORDER BY first_seen_at LIMIT extract_worker_batch_size` (default 20), so one
full batch of images ahead of everything else made **every sweep return
`touched=0`** — which the CLI backfill loop and the daemon worker both read as
"queue drained". Measured on the live Mac archive before the fix:

```
blobs total          : 16561
attachment_text rows : 19
unprocessed          : 16542   (6820 of them allowlisted PDFs)
the exact next claim : 0/20 allowlisted  ->  touched=0, forever
```

Two changes:

- Extensions come from the **original filename** in `messages.attachments`, via
  the new pure [src/localmail/search/attachment_kind.py](src/localmail/search/attachment_kind.py)
  (`extension_of` / `is_allowlisted` / `preferred_filename` / `is_pdf`), shared
  by the worker gate, the docling-fallback decision, and both extractors'
  `extract`/`supports`. `extract` gained a keyword-only `filename=`. Lookup is
  `extract_worker._blob_filenames`, whose containment predicate is served by the
  existing `messages_attachments_gin`. A blob is global, so it may carry several
  names — **any** one with an allowlisted extension admits it.
- A skipped blob records a `type-skipped` sentinel (sibling of `size-skipped`).
  That makes the decision queryable **and** makes the blob ineligible for the
  next claim, which is what unstarves the queue.

`search-status`'s eligibility count carried the identical dead substring match
and is fixed with it.

### 2. #245 — nine CLI commands ignored `--config`

`cli._dsn()` called `load_config()` with no path, so
`--config /etc/prod.toml embed-backfill` ran against
`~/.config/localmail/config.toml`: different database, different attachment
root. It now takes the click context, so a new command cannot omit it without a
`TypeError`. **The issue named five**; the four that only route through `_dsn`
(`list-failed-embeddings`, `retry-failed-embeddings`, `list-failed-extractions`,
`retry-failed-extractions`) were equally affected.
`tests/test_cli_config_path.py` pins all nine.

### 3. #246 — a writable parent directory defeats the 0600 secrets check

Directory write access permits `unlink`/`rename` of entries **regardless of
their own modes**. New pure `secrets_store.directory_exposure(mode)`, a sibling
of `mode_is_private` (option 2 from the issue): world-writable **refuses**
(`chmod o-w`), group-writable **warns once per process** (umask-002 +
private-group puts a stock install at 0775 — the DGX's own config dir is 0775
today), read/execute bits ignored.

**Verification (all run this session):**
- `uv run pytest --deselect tests/test_daemon_control_socket.py` →
  **2090 passed** (was 2008; **+82**)
- `uv run mypy src/localmail` → clean, **131** source files (was 130)
- `ruff check src/localmail` → back to the pre-existing 10, none new
- **No migration. No `gui/` changes.**

Built TDD throughout — every test watched failing first, including the one that
reproduces the queue starvation and the nine that reproduce the `--config` bug.

### 4. Ops

- **DGX** put back on `main` (it was stranded on the deleted #244 branch),
  `uv sync --extra mcp`, services restarted, **0 `KeyringLocked` since
  restart**, INBOX sync live. It is now at `4f3285f` — **not** `7fb6c71`.
- **Mac** deployed to `7fb6c71`; both launchd agents kickstarted. Extraction is
  draining (see §0).
- Filed **#248** (see §0.3).

## What's next

### 0. Finish what this session could not

#### 0.1 The DGX is unreachable and behind

It dropped off the network mid-session — 100% packet loss to `10.0.0.3` while
general internet was fine, so it is the host or the WireGuard peer, not a local
outage. Nothing I ran can explain it (only `git checkout`, `uv sync`, and
`systemctl --user restart` of the two localmail units, all of which succeeded
and were verified healthy first). It is still on `4f3285f`.

```bash
ping -c 3 10.0.0.3
ssh hherb@10.0.0.3 'export PATH="$HOME/.local/bin:$PATH"; cd ~/src/localmail \
  && git pull --ff-only && uv sync --extra mcp \
  && systemctl --user restart localmail-daemon localmail-serve'
```
**Acceptance:** DGX at `7fb6c71`, both units `active`.
Note `uv` is **not** on the non-interactive ssh PATH — export it, or the sync
silently no-ops (`uv: command not found`) while the restart still reports fine.

#### 0.2 The headless-secrets cold-boot proof *(carried from session 11)*

Still unproven. The keyring on the DGX is *currently unlocked*, so a restart
demonstrates nothing; only a cold boot exercises the failure the `file` backend
exists to remove. Deliberately not done this session — you asked for "back to
main only, no reboot".

```bash
ssh hherb@10.0.0.3 'sudo reboot'
# once back:
ssh hherb@10.0.0.3 'systemctl --user is-active localmail-daemon localmail-serve;
  journalctl --user -u localmail-daemon -b | grep -c KeyringLocked'   # expect 0
```
**Acceptance:** both units `active` after a cold boot with **zero**
`KeyringLocked` lines that boot.

#### 0.3 Watch the Mac drain, and decide #248

The queue is moving on its own at daemon cadence. Progress this session:

| | before | at handoff |
|---|---|---|
| `lightweight@1.0` | 19 | 567 |
| `type-skipped` | — | 339 |
| `lightweight-empty` | — | 29 |
| `failed_extractions` | 0 | 48 |
| unprocessed | 16542 | 15626 |

```bash
unset VIRTUAL_ENV && uv run localmail search-status
```
**Acceptance:** `unprocessed` trends to ~0. It will not reach 0 while #248 is
open — see below. If you want it faster than daemon cadence,
`uv run localmail extract-backfill` drains it in the foreground; expect it to
sit silently for a minute or two on the **first** docling pipeline init (that is
not a hang — it cost a 240 s timeout before I instrumented it).

**#248 (filed this session)** — every scanned PDF now fails with
`EasyOCR is not installed`, on the **poison-pill** path, so each burns
`retry_count` and is given up on after 3. 48 rows already. Scanned PDFs are
precisely what the docling fallback is *for*, so this is worth deciding before
the counter is spent archive-wide. My inclination is in the issue: default OCR
off (a scanned PDF becomes an honest empty sentinel, not a failure) **plus**
classifying a missing-OCR-engine as a config error that never burns
`retry_count`, with an OCR engine available opt-in. Recovery for rows already
recorded is `localmail retry-failed-extractions`.

### 1. **Remaining robustness issues** *(carried)*
   - **#221** — daemon supervisor lifecycle robustness (grace mismatch,
     event-loop block/orphan, STARTING-stuck, socket-timeout, chmod TOCTOU).
     Largest of the carried set.
   - **#218** — GUI download commands buffer the full body before enforcing the
     size ceiling.
   - **#235** — `search --smart` reports "could not reach the rewriter service"
     forever on a malformed `rewriter_base_url`.
   - **#226** — self-signed cert misses the reachable IP when `--bind 0.0.0.0`.
   - **#225 / #227** — `/v1/changes` subscription lifecycle gaps.
   - **#200 / #211** — admin panels silently swallow 4xx; surface as a toast.

### 2. **Admin GUI phase 5 — Users & ACL panel** *(carried, still the design's next slice)*
   `/v1/admin/users` is already `require_admin()` (bearer-capable) — **no backend
   work needed.** Service layer:
   [src/localmail/api/admin/users.py](src/localmail/api/admin/users.py).
   **Acceptance:** a `UsersPanel.svelte` replacing the placeholder tab: list,
   create, delete, per-account ACL grant/revoke (a checklist over every account),
   `is_admin` toggle, password reset, enable/disable. Surface the **two lock-out
   guards as 409s** — the count-based last-admin rule (`LastAdminError`) and the
   identity-based self-action rule (no self-demote/self-delete). Mirror
   [serve/admin/users_panel_router.py](src/localmail/serve/admin/users_panel_router.py).
   Follow the Daemon-panel shape, and **stub the new API module in both
   `AdminView.test.ts` and `MainView.test.ts`** (see risk 9).

### 3. **Close #90?** *(carried, still unanswered)*
   Its premise (the `glib` Dependabot alert) is dismissed as `not_used` and no
   longer appears among open alerts. Either close it, or repurpose it explicitly
   as "bump the Tauri stack for its own sake" with a real acceptance criterion.

## Open decisions & risks

1. **The DGX is offline and one commit behind.** Both facts are §0.1. Until it
   is reachable there is no way to confirm the #246 group-writable warning
   behaves as intended on the one host that actually has a 0775 config dir.
2. **The headless-secrets fix stays unproven until a cold boot** *(carried)*.
   The daemon is healthy on the file backend, but the keyring is unlocked, so
   only a reboot tests the thing the backend exists for. Do not close the loop
   without §0.2.
3. **`type-skipped` is a one-way door for a widened allowlist.** The sentinel is
   what excludes a blob from re-claim, so widening `extractor_mime_allowlist` /
   `extractor_extension_allowlist` afterwards does **not** re-open the skipped
   blobs. Clear them deliberately —
   `DELETE FROM attachment_text WHERE extractor = 'type-skipped'`.
   `retry-failed-extractions` deliberately does not do this: it is about
   *failures*, and a type-skip is a decision, not a failure. Documented in
   README and on the constant.
4. **#248's `retry_count` burn is running right now.** Every sweep that reaches
   a scanned PDF adds a `failed_extractions` row at `retry_count += 1`, and at 3
   the blob is given up on. Nothing is lost (bytes intact,
   `retry-failed-extractions` clears it), but the longer #248 sits, the more
   blobs need that clearing. This is the one item with a clock on it.
5. **`secrets.configure`'s pin is kept even though #245 is fixed.** Its
   docstring is corrected to say why: `search.create_searcher(cfg=None)` still
   falls back to a no-path `load_config()` for library callers, and the pin makes
   *a default-config read cannot undo a named config's backend* hold by
   construction. Pinned by
   `test_a_default_config_read_cannot_undo_a_named_configs_backend`.
6. **#246 warns rather than refuses on group-write — deliberate.** Refusing
   would wedge any stock umask-002 + private-group install, including the DGX.
   `mode_is_private` (file) and `directory_exposure` (directory) are deliberately
   **siblings, not one shared rule** — they read different bits and carry
   different costs. Do not merge them.
7. **`InsecureSecretsFile` refuses rather than warns — deliberate** *(carried)*.
   The daemon crash-loops on a group/other-*readable* secrets file. Right call
   for a leaked credential: the error names the `chmod`, one command to clear.
   Self-healing with a `chmod` was rejected — it rewrites permissions the
   operator may have set on purpose and cannot undo the exposure.
8. **#239's manual tombstone retention is still a deliberate call** *(carried)*.
   If an issue asks for an automatic sweep of `gave_up_at` rows, the trade is
   silently deleting the only record of permanently lost mail.
9. **CI trap** *(carried)*: any GUI admin panel that fetches on mount MUST be
   stubbed in **both** `AdminView.test.ts` and `MainView.test.ts`, or vitest
   leaks an unhandled rejection while still printing "passed".
10. **Admin bearer blast radius** *(carried, #204)*: a token issued to an
    `is_admin` user is an admin credential — no per-token scope.
11. **`uv sync` without `--all-extras` silently downgrades the Mac.** A bare
    `uv sync` **removed** the `extraction` extra's dependency tree (docling and
    its tree-sitter transitives) this session; `uv sync --all-extras` restored
    it. Use `--all-extras` on the Mac and `--extra mcp` on the DGX.
12. **macOS test noise** *(carried)* — `test_daemon_control_socket.py` fails
    locally (`AF_UNIX path too long`); deselect it, Linux CI is the real signal.
    Also carried: psycopg_pool teardown `ResourceWarning`s, the websockets
    `DeprecationWarning` (#25), Starlette TestClient `httpx` `DeprecationWarning`,
    and jsdom `HTMLCanvasElement.getContext` noise in the gui vitest run.
13. **No ROADMAP.md** *(carried, re-confirmed)* — the `/nextsession` ROADMAP step
    is a no-op; slice status lives in NEXT_SESSION + `docs/handoffs/` + the specs.
    README **was** updated this session (new "Which attachments get extracted"
    section; parent-directory paragraph in "Headless secret storage").
14. **Run vitest from `gui/`, not the repo root** *(carried)* — from the root it
    silently runs without gui's vite config and fails every `.svelte` import with
    a confusing parse error.
15. **`cargo clippy --all-targets` is clean but ungated** *(carried)*. CI lints
    without `--all-targets`, so test-module regressions still won't turn `main`
    red. Run it locally when touching Rust tests.

## Exact commands to resume

```bash
cd /Users/hherb/src/localmail
git fetch --prune origin                 # ALWAYS first
git status                               # expect clean
git branch --show-current                # main
git log --oneline -1                     # 7fb6c71

# Python test suite (deselect the macOS-only socket failure — see risk 12):
unset VIRTUAL_ENV && uv run pytest -q --deselect tests/test_daemon_control_socket.py
#   expect: 2090 passed
unset VIRTUAL_ENV && uv run mypy src/localmail
#   expect: Success, 131 source files

# §0.3 — watch the extraction backlog drain:
unset VIRTUAL_ENV && uv run localmail search-status

# §0.1 — DGX (check it is even up first; uv is NOT on the non-interactive PATH):
ping -c 3 10.0.0.3
ssh hherb@10.0.0.3 'export PATH="$HOME/.local/bin:$PATH"; cd ~/src/localmail \
  && git pull --ff-only && uv sync --extra mcp \
  && systemctl --user restart localmail-daemon localmail-serve'
ssh hherb@10.0.0.3 'systemctl --user is-active localmail-daemon localmail-serve'

# §0.2 — the cold-boot proof:
ssh hherb@10.0.0.3 'sudo reboot'
ssh hherb@10.0.0.3 'journalctl --user -u localmail-daemon -b | grep -c KeyringLocked'

# Frontend — only if you touch gui/ (MUST be run from gui/ — see risk 14):
cd gui && npm run check && npm test && npm run build && cd ..
cd gui/src-tauri && cargo test && cargo clippy --locked -- -D warnings \
  && cargo clippy --all-targets -- -D warnings && cd ../..
```

`origin/main` at **`7fb6c71`**. Latest migration
**`0034_transient_fetches_gave_up.sql`** (applied to both deployments in session
11); next free slot `0035_*.sql`. **Open issues: 14** — 13 carried plus the new
**#248**; #216, #245, #246 closed with #247. Dependabot: **0** open alerts.
