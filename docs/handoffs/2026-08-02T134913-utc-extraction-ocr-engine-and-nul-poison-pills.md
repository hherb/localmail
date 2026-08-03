# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-08-03 (session 13).** `origin/main` was at `c03099c`.
> This session closed **#248** and the newly-filed **#249** in
> **[PR #250](https://github.com/hherb/localmail/pull/250)**, merged as
> **`e620aa5`** (CI green). Both were deterministic failures burning
> `failed_extractions.retry_count` on blobs no retry could ever fix — together
> **1448 blobs** across the two hosts had already been given up on, and the count
> was still climbing during the session. **Both hosts are now deployed and
> recovered**; the Mac additionally does **real OCR** on scanned PDFs via ocrmac.
> The long-standing "DGX drops off the network" mystery is **diagnosed** (its
> upstream internet flaps; not WireGuard — §0.1), and the headless-secrets
> **cold-boot proof finally passed** (§0.2) — carried unproven since session 11.
> Nothing from §0 is left outstanding. See §0.

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

Everything below is in **`e620aa5`** (PR #250, two commits squashed: the fix
`58d2863` and the CI test fix `ba38b44`).

Both bugs were made *reachable* by #216 last session — before it, the extract
queue was starved and neither path ever ran. Neither was caused by it.

### 1. #248 — the OCR engine is configurable and defaults to `auto`

`DoclingExtractor` hardcoded `ocr_options=EasyOcrOptions(...)`. EasyOCR is
**not** a docling dependency, so on any install without it every scanned PDF
raised `ImportError` out of `convert()` — on the **poison-pill** path. Scanned
PDFs are precisely what the docling fallback exists for, so the archive's entire
scanned corpus was being written off, three failures at a time.

The hardcoding also **overrode a better default**. docling's own
`PdfPipelineOptions.ocr_options` is `OcrAutoOptions`, which probes ocrmac →
rapidocr → easyocr and, when none is installed, passes pages through **without
raising** — an honest `lightweight-empty` sentinel instead of a failure. So the
root cause is a hardcoded engine choice defeating a gracefully-degrading default.

- New pure [src/localmail/ocr_policy.py](src/localmail/ocr_policy.py)
  (`plan_ocr`, `unknown_engine_message`, `OCR_AUTO`, `OCR_DISABLED`). **Top-level,
  not under `search/`**: `config.py` imports `OCR_AUTO` as the field default and
  `localmail.search`'s `__init__` imports `config`, so `search/` would be a
  circular import — same reason `account_names.py` and `fetch_retry.py` live
  there. The docstring says so, to stop a future tidy-up moving it.
- `search.extractor_ocr_engine` **is docling's own registry kind**, resolved via
  `factory.create_options(kind=…)` — no mapping table to drift against a docling
  upgrade — and validated against the **live** `factory.registered_kind` rather
  than a `Literal` that would go stale (this build registers `tesserocr`, not
  `tesseract_cli`). `"none"` is the one value we own.
- A missing or unknown engine raises `ExtractorConfigurationError`, which
  **subclasses `TransientExtractorError`**. See risk 1 — that subclassing is the
  whole fix. Detection is `_exc_chain_has_import_error`, matching the exception
  **type**, never the message text (each engine words its own).
- `[extraction]` now installs `ocrmac ; sys_platform == 'darwin'` — a thin Apple
  Vision wrapper, **no torch and no model downloads**. Linux is unaffected and
  degrades via `auto`.

### 2. #249 — `ExtractedText` strips NUL bytes on construction *(filed this session)*

Found while measuring #248 on the live archive. Postgres `TEXT` rejects `\x00`
and `attachment_text.extracted_text` is the type's only consumer, so a NUL
surviving to the INSERT aborted it, escaped `_process_blob` into the worker's
outer safety net, and was recorded as a poison pill under the extractor name
**`'unexpected'`**. Deterministic — the same bytes always re-extract to the same
NUL — so every retry reproduced it. 128 blobs when found (112 PDFs, 10
`text/plain`, 5 `octet-stream`, 1 html), 189 by the time the fix landed.

Normalising in `__post_init__` rather than in each of the eleven `_extract_*`
methods means a twelfth cannot forget (same by-construction reasoning as #67's
unconditional ACL check). The rule is the new pure
[src/localmail/pgtext.py](src/localmail/pgtext.py)`::strip_nuls`, now the single
implementation shared by `parser.py`, `extract_worker.py`'s failure logging, and
this boundary — it had been copy-pasted into the first two and was simply
**missing** from the third.

### 3. Verification

- `uv run pytest -q --deselect tests/test_daemon_control_socket.py` →
  **2137 passed** (was 2090; **+47**)
- `uv run mypy src/localmail` → clean, **133** source files (was 131)
- `uv run ruff check src/localmail` → the pre-existing **10**, none new
- **No migration. No `gui/` changes.**

Built TDD; every test was watched failing first. Both end-to-end worker
reproductions were additionally re-run against *simulated pre-fix code* to prove
they catch the regression — the #249 one reproduces the production log line
verbatim. The #248 worker test was also verified under a `meta_path` blocker
that reproduces CI's docling-less environment (see risk 2).

### 4. Ops — the Mac

Deployed to `e620aa5`; `uv sync --all-extras` (installs ocrmac); both launchd
agents kickstarted. **1332 dead rows cleared** with `retry-failed-extractions`,
and they are re-extracting successfully:

| | before fix | after |
|---|---|---|
| `failed_extractions` (docling/EasyOCR) | 1126 | — |
| `failed_extractions` (`DataError`/`unexpected`) | 189 | — |
| `failed_extractions` (lightweight, genuine) | 17 | — |
| **total failed** | **1332** | **1** |
| `docling@2.97.0` extractions | 1 | 20 and climbing |

The single remaining failure is legitimate: `e3b0c442…b855` is the SHA-256 of
the **empty string** — a zero-byte attachment docling correctly refuses.

**Real OCR is verified on real production blobs**, not just fixtures. Against a
previously-poison-pilled scan:

```
docling.models.stages.ocr.auto_ocr_model: Auto OCR model selected ocrmac.
--- 8.1s: 253 chars, pages=1
'## *** MERCHANT COPY ***\n\nMerchant ID\n\n23512585 66997516 11 MAR 11 16:02 ...'
```

Warm throughput ~**1.7 s/page** — docling caches the pipeline internally, so the
per-blob `DocumentConverter()` construction is **not** worth caching ourselves
(measured, so don't "optimise" it on a hunch). A genuinely blank scan returns 0
chars and becomes an honest empty sentinel.

### 5. Ops — the DGX

Deployed to **`a7013c5`** over the LAN, 116 #249 victims recovered, and the
recurring "DGX is unreachable" mystery diagnosed (its upstream internet flaps —
**not** WireGuard). Full detail in §0.1, written up there rather than here
because the LAN address and the do-not-touch-wg0.conf note are things the next
session needs *before* it touches that host.

Its backlog is otherwise healthy, with two things worth knowing:

```
chunks_embedded 112911 / chunks_pending 0 / failed_embeddings 0
attachment_chunks_embedded 29366 / 29366
blobs_extracted 3773 / blobs_pending 566   (= 126 genuine failures + 440 lightweight-empty)
body_lang_populated 8324 / body_lang_pending 21078
```

- **440 `lightweight-empty` blobs.** On Linux there is no OCR engine (risk 3), so
  scanned PDFs land here as honest empty sentinels. Many would yield text if
  `easyocr` or `rapidocr` were installed — `auto` would pick either up with no
  config change, and clearing the sentinels
  (`DELETE FROM attachment_text WHERE extractor = 'lightweight-empty'`) would
  re-open them. Worth deciding deliberately; it is the DGX's remaining
  unindexed-content gap.
- **`body_lang_pending` 21078 vs 8324 populated.** Not a fault — the detector is
  working through the backlog at daemon cadence — but it is the largest pending
  queue on that host and worth a glance next session to confirm it is moving.

## What's next

### 0. Finish what this session could not

#### 0.1 ~~The DGX is unreachable and two releases behind~~ — **DONE**

Reached over the **LAN** (`ssh 192.168.68.76`) when `10.0.0.3` was still dead,
and deployed to **`a7013c5`**. Both units `active`, clean startup (the
onnxruntime `/sys/class/drm/card0` warning is the known-harmless one).

**#249 had bitten it, as predicted** — the NUL path is in the *lightweight*
extractor, which the DGX runs. (#248 could not: it carries only `--extra mcp`,
so docling is not installed there.) 116 blobs were sitting at `retry_count = 3`
with `DataError`/`unexpected`.

Recovery was a **targeted** DELETE, not the blanket `retry-failed-extractions`
used on the Mac: the other 126 rows there are *genuine* poison pills (85
encrypted PDFs, 29 truncated/corrupt PDFs, 9 malformed ICS, 1 bad docx) and
should keep their `retry_count` rather than re-fail three times each.

```sql
DELETE FROM failed_extractions
 WHERE extractor = 'unexpected' AND error_class = 'DataError'
   AND error_message LIKE '%NUL (0x00) bytes%';
```

Result: `lightweight@1.0` 3657 → **3773** (exactly +116), `failed_extractions`
steady at 126 with **zero** new `DataError` rows, queue otherwise drained.

**The WireGuard drop — the DGX side is now cleared by measurement.** It is not
a dead host, a dead tunnel, or a WireGuard misconfiguration: `wg0` never
restarted, `wg-quick@wg0` stayed `active`, and `PersistentKeepalive = 25` is
already set. The peer is a **VPS hub** (`vpn.consensus-ai.org:51820` =
135.181.95.235, `AllowedIPs = 10.0.0.0/24`), so Mac↔DGX hairpins through the
internet — hence ~700 ms RTT to a LAN-adjacent host.

The DGX is on **Starlink**, which cycles its public IP, and that looked like the
answer. **It is not.** A 10.6 h measurement spanning **18 IP cycles** recorded
**one** tunnel FAIL sample in 1188 — and that one reads `lan=FAIL` too, so both
paths died together and it was not tunnel-specific. Any re-convergence gap is
under 30 s. The clincher: the boot covering Aug 2 23:45 AEST, when an outage
*was* observed, has a longest WAN interruption of **4 s** anywhere in it and **no
transition at all** at that time — the DGX had continuous internet while its
tunnel was dead.

**Four explanations have now been refuted**, three of them mine (stale NAT +
add-keepalive; one-outbound-packet-restores-it; upstream-flapping; and now
IP-cycling). All are recorded in
[docs/operations/wireguard-drop-measurement.md](docs/operations/wireguard-drop-measurement.md)
so they are not re-proposed. **Do not edit `/etc/wireguard/wg0.conf`.**

**What remains:** the **VPS hub** and **the Mac's own tunnel session** — the two
things nothing was watching. Both probes are now **persistent** (launchd
`KeepAlive` on the Mac, systemd `Restart=always` + `Linger=yes` on the DGX;
verified to respawn after kill), because the fault recurs every day or two and
12-hour bursts kept missing it. The Mac probe now also pings the hub over the
public internet (not through the tunnel) and logs its own `utun8` counters, so
the next occurrence separates *hub gone* from *hub up but not relaying*.

**Next session: read the logs first** — the answer may already be sitting in
them.

```bash
grep 'tunnel=FAIL' ~/localmail-probe/tunnel-probe.log        # Mac
ssh 192.168.68.76 'tail -50 ~/localmail-probe/wg-probe.log'  # DGX
```

Note: `uv` is **not** on the non-interactive ssh PATH — export it, or `uv sync`
silently no-ops (`uv: command not found`) while the restart still reports fine.

#### 0.2 ~~The headless-secrets cold-boot proof~~ — **DONE, it passed**

Carried unproven since session 11; proven on 2026-08-03. A **real** cold boot
(`system boot 2026-08-03 19:06`, `up 1 minute` at check time), both units
`active`, and **0 `KeyringLocked` lines that boot**. The `file` backend does
what it was built for: no PAM session, no unlocked login keyring, no crash loop.

Two things came out of it for free:

- **#133's startup backoff worked in production.** Postgres (the Docker
  container) had not finished starting, so `_load_syncable_accounts` failed at
  19:07:04 and logged *"loading syncable accounts from the DB failed; retrying
  in 1.0s"* instead of crashing; the daemon converged at 19:07:11 and had all
  workers up by 19:07:18. That is precisely the scenario #133 was written for,
  now observed on real hardware rather than in a test.
- **Small ops bug, unfixed:** `~/.config/systemd/user/localmail-daemon.service`
  has `StartLimitIntervalSec=0` on line 11, inside `[Service]`. systemd only
  honours that key in `[Unit]`, so it logs *"Unknown key name
  'StartLimitIntervalSec' in section 'Service', ignoring"* and the restart-limit
  override is silently inert. Move it under `[Unit]`. Harmless while nothing
  crash-loops — which is the point of the fix above — but it would matter on the
  day something does.

```bash
# how it was verified (re-runnable if the secrets backend is ever touched):
ssh 192.168.68.76 'sudo reboot'
ssh 192.168.68.76 'uptime -p; who -b;
  systemctl --user is-active localmail-daemon localmail-serve;
  journalctl --user -u localmail-daemon -b | grep -c KeyringLocked'   # expect 0
```
**Acceptance (met):** both units `active` after a cold boot with **zero**
`KeyringLocked` lines that boot.

#### 0.3 Watch the Mac finish draining

1313 blobs unprocessed at handoff (16561 total, 15248 processed), moving on its
own at daemon cadence. Scanned PDFs are now slower per blob because OCR actually
runs (~1.7 s/page), which is the point.

```bash
unset VIRTUAL_ENV && uv run localmail search-status
```
**Acceptance:** `unprocessed` trends to ~0 with `failed_extractions` staying in
single digits. Unlike last session there is no known open bug holding it back.
`uv run localmail extract-backfill` drains it in the foreground if you want it
faster; expect ~100 s of silence on the **first** docling pipeline init (not a
hang).

#### 0.4 ~~Decide OCR on the DGX~~ — **DONE, OCR is live there**

Installed the `[extraction]` extra on the DGX (`uv sync --extra mcp --extra
extraction`) and re-opened its 440 `lightweight-empty` sentinels
(`DELETE FROM attachment_text WHERE extractor = 'lightweight-empty'`). Real OCR
text is landing — e.g. *"Enquiries to: Bec Smith, Phone: (08) 9194 1601 / Dr
Horst Herb / PO Box 161 Dorrigo 2453 / Medical Services Agreement"* off a scanned
letter that had produced nothing before.

**No config change was needed, and the earlier "you must pin it on Linux" advice
in this handoff was wrong.** docling's `auto` selector tries rapidocr on
**onnxruntime** *before* it tries easyocr or the torch backend; the DGX logs
`Auto OCR model selected rapidocr with onnxruntime`. The mistaken claim came from
reading only the tail of `auto_ocr_model.py`'s selector chain. Benchmarked on two
real DGX blobs, `auto` and a pinned `rapidocr` produce **byte-identical** output
(14 and 1202 chars) because they resolve to the same engine; ~3–5 s/page warm,
~77 s on the first call while the pipeline initialises.

Two things worth knowing for the next host:

- **rapidocr and onnxruntime were already present** — rapidocr via
  `docling[standard]`, onnxruntime as a *core* localmail dep via fastembed. A
  brief attempt to add them explicitly to the `[extraction]` extra was reverted
  as redundant. Only ocrmac (macOS) is ours.
- **docling pulls torch and CUDA on aarch64 Linux** — `torch` 407 MB,
  `nvidia-cudnn-cu13` 424 MB, `nvidia-cublas` 518 MB; the DGX venv went to
  **5.5 GB** (1.5 TB free, so not a problem there). OCR does **not** use it —
  `auto` picks the onnxruntime backend — but budget the disk on any new Linux
  host. `torch.cuda.is_available()` is `True` there (device `NVIDIA GB10`), so a
  GPU OCR path exists if throughput ever matters; at ~4 s/page for 440 documents
  it does not.

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
   - **#200 / #211 / #208** — admin panels silently swallow 4xx; surface as a toast.

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
   `AdminView.test.ts` and `MainView.test.ts`** (see risk 11).

### 3. **Close #90?** *(carried, still unanswered)*
   Its premise (the `glib` Dependabot alert) is dismissed as `not_used` and no
   longer appears among open alerts. Either close it, or repurpose it explicitly
   as "bump the Tauri stack for its own sake" with a real acceptance criterion.

## Open decisions & risks

1. **`ExtractorConfigurationError` subclasses `TransientExtractorError`, and that
   subclassing *is* the fix — do not "clean it up" into its own hierarchy.**
   `_is_transient` already recognises the base class, so `retry_count` is never
   burned and the bound becomes the #153 transient budget (default 5), which
   exists for exactly this shape: not-the-blob's-fault, possibly permanent. A
   dedicated `attachment_text` sentinel was **rejected**: it would make the blob
   ineligible for re-claim, so fixing the config would silently *not* re-open the
   very documents it was fixed for — the one-way door `type-skipped` documents
   (risk 4). Recovery is `retry-failed-extractions`, which clears both tables.
2. **Test trap, cost one red CI run: `_try_import_docling` must be monkeypatched
   in BOTH namespaces.** `extract_worker` imports it by name and holds its own
   reference, and *that* is the one gating `docling_avail`. Patching only
   `extractor`'s copy passes wherever docling is installed (locally) and asserts
   **nothing** where it is not (CI has no `[extraction]` extra) — the worker takes
   the "docling missing" branch, writes a `lightweight-empty` sentinel, and the
   assertion holds vacuously. The same applies to any future
   `from …extractor import X` the worker gates behaviour on. To reproduce CI
   locally, run pytest with a `meta_path` blocker for `docling`.
3. **OCR is macOS-only by default.** `[extraction]` installs ocrmac under a
   `sys_platform == 'darwin'` marker. A Linux host with the extraction extra gets
   docling but **no OCR engine**, so scanned PDFs become honest
   `lightweight-empty` sentinels — correct and quiet, but *no text*. **Installing
   the `[extraction]` extra is the whole fix; no pin is needed** — `auto` selects
   rapidocr on onnxruntime, verified on the DGX (§0.4). What it does cost on
   aarch64 Linux is disk: docling drags in torch + CUDA, ~5.5 GB of venv, none of
   which OCR actually uses. Re-open existing sentinels by hand afterwards;
   nothing does it automatically.
4. **`type-skipped` is a one-way door for a widened allowlist** *(carried)*. The
   sentinel is what excludes a blob from re-claim, so widening
   `extractor_mime_allowlist` / `extractor_extension_allowlist` afterwards does
   **not** re-open the skipped blobs. Clear them deliberately —
   `DELETE FROM attachment_text WHERE extractor = 'type-skipped'`.
   `retry-failed-extractions` deliberately does not: it is about *failures*, and
   a type-skip is a decision.
5. **"The DGX is unreachable" is NOT the DGX's fault — measured, not guessed.**
   Four explanations refuted, three of them mine; the DGX side is cleared (§0.1).
   Reach it at **`ssh 192.168.68.76`**, which works whenever the tunnel does not
   — deploys, systemd, and `docker exec … psql` all work normally over it.
   **Do not touch `/etc/wireguard/wg0.conf`**: `PersistentKeepalive = 25` is
   already set and 18 IP cycles passed without a tunnel outage. Persistent
   probes are now running on both hosts; read their logs before theorising.
6. **`secrets.configure`'s pin is kept even though #245 is fixed** *(carried)*.
   `search.create_searcher(cfg=None)` still falls back to a no-path
   `load_config()` for library callers, and the pin makes *a default-config read
   cannot undo a named config's backend* hold by construction. Pinned by
   `test_a_default_config_read_cannot_undo_a_named_configs_backend`.
7. **#246 warns rather than refuses on group-write — deliberate** *(carried)*.
   Refusing would wedge any stock umask-002 + private-group install, including
   the DGX (its config dir is 0775). `mode_is_private` (file) and
   `directory_exposure` (directory) are deliberately **siblings, not one shared
   rule**. Do not merge them.
8. **`InsecureSecretsFile` refuses rather than warns — deliberate** *(carried)*.
   The daemon crash-loops on a group/other-*readable* secrets file. Right call
   for a leaked credential: the error names the `chmod`, one command to clear.
9. **#239's manual tombstone retention is still a deliberate call** *(carried)*.
   If an issue asks for an automatic sweep of `gave_up_at` rows, the trade is
   silently deleting the only record of permanently lost mail.
10. **Admin bearer blast radius** *(carried, #204)*: a token issued to an
    `is_admin` user is an admin credential — no per-token scope.
11. **CI trap** *(carried)*: any GUI admin panel that fetches on mount MUST be
    stubbed in **both** `AdminView.test.ts` and `MainView.test.ts`, or vitest
    leaks an unhandled rejection while still printing "passed".
12. **`uv sync` without `--all-extras` silently downgrades the Mac** *(carried)*.
    A bare `uv sync` **removes** the `extraction` extra's tree (docling, and now
    ocrmac too). Use `--all-extras` on the Mac and `--extra mcp` on the DGX.
13. **macOS test noise** *(carried)* — `test_daemon_control_socket.py` fails
    locally (`AF_UNIX path too long`); deselect it, Linux CI is the real signal.
    Also carried: psycopg_pool teardown `ResourceWarning`s, the websockets
    `DeprecationWarning` (#25), Starlette TestClient `httpx` `DeprecationWarning`,
    and jsdom `HTMLCanvasElement.getContext` noise in the gui vitest run.
14. **No ROADMAP.md** *(carried, re-confirmed)* — the `/nextsession` ROADMAP step
    is a no-op; slice status lives in NEXT_SESSION + `docs/handoffs/` + the specs.
    README **was** updated this session (new "Scanned PDFs and OCR" section, a
    "How extraction failures are classified" heading, the configuration failure
    class, and the NUL-stripping note). `config.example.toml` gained a `[search]`
    section documenting `extractor_ocr_engine`.
15. **Run vitest from `gui/`, not the repo root** *(carried)* — from the root it
    silently runs without gui's vite config and fails every `.svelte` import.
16. **`cargo clippy --all-targets` is clean but ungated** *(carried)*. CI lints
    without `--all-targets`, so test-module regressions won't turn `main` red.

## Exact commands to resume

```bash
cd /Users/hherb/src/localmail
git fetch --prune origin                 # ALWAYS first
git status                               # expect clean
git branch --show-current                # main
git log --oneline -3                     # code in e620aa5, then the handoff commit

# Python test suite (deselect the macOS-only socket failure — see risk 13):
unset VIRTUAL_ENV && uv run pytest -q --deselect tests/test_daemon_control_socket.py
#   expect: 2137 passed
unset VIRTUAL_ENV && uv run mypy src/localmail
#   expect: Success, 133 source files

# §0.3 — watch the extraction backlog drain:
unset VIRTUAL_ENV && uv run localmail search-status

# The DGX — deployed at handoff. If 10.0.0.3 is dead its upstream internet is
# flapping (risk 5), NOT a dead host; the LAN address always works:
ssh 192.168.68.76 'systemctl --user is-active localmail-daemon localmail-serve'

# §0.2 — the cold-boot proof PASSED this session; re-run only if the secrets
# backend is touched again:
#   ssh 192.168.68.76 'sudo reboot'
#   ssh 192.168.68.76 'journalctl --user -u localmail-daemon -b | grep -c KeyringLocked'

# Reproduce CI's docling-less environment locally (risk 2) — a pytest plugin
# that blocks `import docling` via sys.meta_path; see the session transcript.

# Frontend — only if you touch gui/ (MUST be run from gui/ — see risk 15):
cd gui && npm run check && npm test && npm run build && cd ..
cd gui/src-tauri && cargo test && cargo clippy --locked -- -D warnings \
  && cargo clippy --all-targets -- -D warnings && cd ../..
```

All session-13 code is **`e620aa5`**; `origin/main` is that plus the handoff
commits. **Both hosts are deployed** (the Mac at `e620aa5`, the DGX at
`a7013c5`). Latest migration **`0034_transient_fetches_gave_up.sql`** (applied to
both deployments in session 11); next free slot `0035_*.sql`. **Open issues: 13**
— all carried; #248 and #249 closed with #250. Dependabot: **0** open alerts.
