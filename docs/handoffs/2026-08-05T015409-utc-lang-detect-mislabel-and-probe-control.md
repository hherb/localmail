# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-08-05 (session 16).** This session closed **#255** — the
> defect #251 exposed, where **17% of all `body_lang` labels** named a language
> with no plausible presence in the archive — through PR **#258**, and
> re-labelled both deployments. It also restored the DGX probe's control column
> (PR **#260**), which had been reading `FAIL` permanently since Aug 4 and had
> quietly cost the WireGuard investigation its only "is the host alive"
> reference. Filed **#259**; closed **#90**. The DGX drops themselves were
> **not** investigated and remain unexplained.

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

### 1. #255 — language detection stops mislabelling English mail (PR #258 → `19be6a2`)

Unwedging detection in #251 took the Mac from 7,744 to 100,922 labels and
immediately exposed the next defect: **17,129 of those (17%)** named a language
with no plausible presence in the archive. Yoruba was the **second most common
language** at 7,593 rows; `fi` (910), `eo` (831), `et` (745), `cy` (605),
`la` (491) and `az` (476) are the same failure. Pre-existing detector quality,
invisible while only 7.6% of the archive was labelled.

The mail is English marketing/newsletter traffic whose bodies are largely
tracking URLs with high-entropy path segments. Lingua scores that soup above
`body_lang_min_confidence` and lands on a low-resource language. The errors are
**correlated**, so `lang:en` was excluding ~7,600 English newsletters — the
inverse of the filter's purpose.

**The decision was measured against the live archive, not reasoned about.**
300 randomly-sampled rows per bucket:

| accuracy | detector input | bad rows still implausible | `en` kept | throughput |
|---|---|---|---|---|
| low | raw *(before)* | 300 / 300 | 100% | 75 rows/s |
| low | URL-stripped | 94 | 89% | 108 rows/s |
| full | raw | 155 | 96% | 119 rows/s |
| **full** | **URL-stripped** | **3** | **96%** | **176 rows/s** |

**Neither change alone suffices** — 69% and 48% separately, **99%** together.
The issue framed them as alternatives; that was the central finding.

**Three assumptions written into the issue measured false.** All are now
recorded in CLAUDE.md so they are not re-tried:

1. **"Full accuracy is ~1 GB resident vs ~100 MB."** Measured peak RSS after
   800 detections in a fresh process: **227 MB full, 239 MB low.** Full is
   marginally *cheaper* and 2.3× faster — lingua 2.2.0 loads per-language
   models lazily, so the mode barely moves resident size. The config comment
   asserting the old figures was simply wrong and was replaced.
2. **"Raising `body_lang_min_confidence` is the cheapest lever."** 0.65 → 0.90
   moved implausible labels **64 → 62** of 500. Low-accuracy lingua is
   *confidently* wrong, so a confidence floor cannot discriminate. Rejected.
3. **"The U+034F preheader padding is a primary cause."** An ablation shows
   invisible-character, email, HTML-tag and separator-rule stripping each add
   **zero** once URLs are gone. None were implemented. **Do not add
   normalisation steps here without a measurement.**

**The one apparent regression was verified by hand.** Only 64% of currently-`de`
rows keep that label. Inspecting 20 of the 81 `de → en` flips: **19 are
unambiguously English** ("Dear Dr Horst Herb, Enclosed you will find your
current invoice…", "Please be advised of the following vacancy…"). One is a
genuine German thread carrying a large English quoted-reply block. The drop is
correcting false positives. Rows currently labelled `en` are the control: 96%
keep the label and **none** move to another language.

What shipped:

- **`search/lang_text.py`** — a new pure module, `normalize_for_detection`, the
  one rule for what the detector sees. Applied *only* inside
  `LinguaDetector.detect`; `body_text`, FTS, chunking and embeddings all still
  see the original body.
- **The length floor measures the normalised text**, and that ordering is
  load-bearing: a body of pure tracking URLs clears the 20-char floor raw and
  earns a confident wrong label; normalised it is empty and correctly declines.
- **`body_lang_low_accuracy` defaults `False`**, with `LinguaDetector`'s own
  constructor default aligned so the two cannot drift.
- **`reopen_all` / `lang-backfill --relabel [--yes]`** — the escape hatch
  `--retry-declined` cannot be, since a row carrying a *wrong* label is neither
  claimable nor declined. Destructive, so it prompts. `RELABELABLE_WHERE_SQL`
  joins the existing pair as one authority per predicate.

**No migration** — latest stays `0035`. Design:
[docs/superpowers/specs/2026-08-05-lang-detect-mislabel-design.md](docs/superpowers/specs/2026-08-05-lang-detect-mislabel-design.md);
plan: [docs/superpowers/plans/2026-08-05-lang-detect-mislabel.md](docs/superpowers/plans/2026-08-05-lang-detect-mislabel.md).

### 2. Probe control column restored (PR #260 → `3063db8`)

The probe's `lan` column is its **control**: it reaches the DGX *off* the
tunnel, so `tunnel=FAIL` with `lan=ok` means the tunnel broke, while
`lan=FAIL` means the window says nothing. It hardcoded `192.168.68.76`, an
address the DGX gave up at the Aug 4 reboot — so it had been reading `FAIL`
permanently and **the probe has been running without a working control for the
entire period the drops remain unexplained**.

Hardcoding the current address would only reset the clock: it is a DHCP lease
that has moved three times across two subnets. `LAN_CANDIDATES` is now tried in
order per sample and **the answering address is recorded in the log line**, so
a future move is visible rather than silent. The live log shows the fix
landing:

```
2026-08-05T01:41:25Z tunnel=ok(3/3) lan=FAIL(0/3) hub=ok(3/3) …
2026-08-05T01:42:01Z tunnel=ok(3/3) lan=ok(3/3)@192.168.1.99 hub=ok(3/3) …
```

**Log lines without an `@addr` suffix are unreliable on the `lan` column** —
documented in the measurement doc so the existing history is not misread.

Also recorded: the two hosts are now on **different subnets** and the "LAN"
path measures ~35–120 ms. Still a valid liveness control (it bypasses the
tunnel) but **not** a LAN latency figure.

### 3. Issue hygiene

- **#259 filed** — the embed worker backs off on `wrote == 0`, which counts
  only embedded chunks, so a sweep that labelled 200 messages reads as empty
  and sleeps up to 35 s. ~340 rows/min against `lang-backfill`'s far higher
  rate. Harmless in steady state; filed rather than drive-by fixed because the
  current behaviour is documented as deliberate.
- **#90 closed** — its premise is gone. Dependabot alert #3 is **dismissed**
  (`not_used`) and the repo has **0 open alerts**, agreeing with the issue's own
  finding of no `glib::VariantStrIter` call site. Deliberately *not* repurposed
  as "bump Tauri for its own sake" — that would be an issue with no acceptance
  criterion.

### 4. Verification

- `uv run pytest -q --deselect tests/test_daemon_control_socket.py` →
  **2205 passed, 0 failed** (was 2186; **+19** — `test_lang_text.py` plus
  additions to `test_lang_detect.py` and `test_cli_lang_backfill.py`)
- `uv run mypy src/localmail` → clean, **135** source files (was 134)
- `uv run ruff check src/localmail` → the pre-existing **10**, none in touched
  files
- CI green on PR #258 (Linux, PG pg18, Python 3.12). PR #260 is ops/docs only
  and correctly reports no checks.
- **Re-verified through the shipped code path** against the live archive before
  merge: 99% of implausible rows resolved, 96% of `en` rows retained with the
  remainder declining — none re-labelled to another language.

### 5. Deployment

Both hosts pulled `19be6a2`, re-synced (`--all-extras` on the Mac,
`--extra mcp --extra extraction` on the DGX), restarted, and ran
`lang-backfill --relabel --yes`.

**Both archives re-labelled and fully drained** (`claimable = 0` on each):

| | Mac before | Mac after | DGX before | DGX after |
|---|---|---|---|---|
| **implausible labels** | 17,130 | **350** | 4,953 | **120** |
| `yo` | 7,594 | **57** | 2,451 | **29** |
| `en` | 73,906 | **91,581** | 21,194 | **26,335** |
| populated | 100,929 | 95,622 | 28,621 | 27,524 |
| declined | 6,858 | 12,166 | 866 | 1,965 |
| claimable | 0 | **0** | 0 | **0** |

**98.0% and 97.6% of the implausible labels are gone**, against the design's
99% projection — close, and the shortfall is the residual `ja` (229 Mac / 57
DGX) discussed under What's next.

Backfill output: Mac `re-opened 107787 … done: 102988 messages processed,
91076 labelled`; DGX `re-opened 29488 … done: 27289 processed, 25424 labelled`.

**`declined` roughly doubled on both hosts, and that is the fix working, not a
regression.** ~5,300 Mac rows moved from "confidently mislabelled" to
"declined": they are bodies with no linguistic content once tracking URLs are
removed, which is exactly what the new length-floor ordering is for. `body_lang`
now means what it says — a language, or NULL for unknown.

Both daemons healthy after restart (Mac 7 heartbeat rows across both accounts,
DGX 5 across its one; all three DGX units `active`).

## What's next

### 0. Confirm the deployments held

```bash
launchctl print gui/$UID/com.localmail.daemon | grep -E '^\s*(state|pid) '
psql -h localhost -p 5532 -U localmail -d localmail -c \
  "SELECT worker_kind, account_id, state, now()-last_heartbeat_at AS age
     FROM daemon_heartbeats ORDER BY worker_kind, account_id"
ssh 192.168.1.99 'systemctl --user is-active localmail-daemon localmail-serve localmail-wgprobe'
```

**Acceptance:** 7 Mac heartbeat rows, ages under ~60 s, both accounts present;
three DGX units `active`; `claimable` at or near 0 on both.

### 1. **#259 — the embed worker's backoff ignores language progress** *(filed this session)*
   **Acceptance:** either the backoff resets on language progress
   (`LangDetectPass.visited` already exists and is the honest signal), or the
   docstring explains why a sweep that labelled 200 messages counts as empty
   and points at `lang-backfill` for backlogs. The current state justifies the
   return *value* but not the backoff *decision* that reads it.

### 2. **Remaining robustness issues** *(carried)*
   - **#218** — GUI download commands buffer the full body before enforcing the
     size ceiling.
   - **#235** — `search --smart` reports "could not reach the rewriter service"
     forever on a malformed `rewriter_base_url`.
   - **#226** — self-signed cert misses the reachable IP when `--bind 0.0.0.0`.
   - **#225 / #227** — `/v1/changes` subscription lifecycle gaps.
   - **#200 / #211 / #208** — admin panels silently swallow 4xx; surface as a toast.
   - **#206** — GUI AccountForm: folder filters not editable.

### 3. **Admin GUI phase 5 — Users & ACL panel** *(carried, still the design's next slice)*
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
   `AdminView.test.ts` and `MainView.test.ts`** (see risk 14).

### 4. **Smaller, deliberately not done**
   - **`cli.py` is 1901 lines** and `daemon.py` is 567, both over the 500-line
     guideline. `cli.py` grew ~37 lines this session. Real refactors, out of
     #255's scope — but `cli.py` is now the largest file in the tree and each
     session adds to it.
   - **165 `docling: File format not allowed` failures on the Mac** (of 182
     total). Legitimate content-level failures, but each burns `retry_count`
     three times for a format docling will never accept.
   - **The residual implausible labels are dominated by `ja`** — 229 of the
     Mac's 350 and 57 of the DGX's 120, with `yo` down to 57/29 and a long thin
     tail (`tl`, `la`, `id`). That is 0.24% of Mac labels, against 17% before.
     **Deliberately left alone**: it is below the noise floor of the original
     complaint, and the obvious lever (raising the confidence floor) was
     measured useless. If it is ever worth chasing, sample the `ja` rows first
     — do not assume they are the same failure mode as `yo` was.

## Open decisions & risks

1. **`--relabel` is the only destructive verb in the lang path** *(new)*. It
   discards **every** label, so the archive is unsearchable by `lang:` until the
   drain completes. It prompts unless `--yes`. Reach for `--retry-declined`
   first — it only re-opens rows the detector turned away and is non-destructive.
2. **A re-label drains at ~35 rows/s, not the 176 rows/s the detector
   benchmarks at** *(new)*. The measured figure is detection only; the real
   pass pays a per-row UPDATE round trip and competes with the daemon's own
   sweeps. Budget **~45 minutes for a 100k-row archive**, not the ~10 minutes
   the design estimated. The design's estimate was wrong in the optimistic
   direction — correct it there if it is ever re-quoted.
3. **`reopen_all`'s bulk UPDATE does not appear in `pg_stat_activity` as
   progress** *(new)*. It is one statement over every bodied row; until it
   commits, every count query returns the *pre-* snapshot. Reading "nothing has
   changed" in the first minutes is expected and is **not** a stall — check for
   a live `lang-backfill` process before concluding otherwise.
4. **Do not add normalisation steps to `lang_text.py` without a measurement**
   *(new)*. Invisible-character, email, HTML-tag and separator-rule stripping
   were each measured at **zero** benefit. The module does one thing on purpose.
5. **`body_lang_low_accuracy` is retained but measured strictly worse** *(new)*.
   The knob exists only as an escape hatch for a memory-constrained host, and
   the measurement was taken on the Mac. If the DGX ever shows different memory
   behaviour, the fix is a config edit, not a release.
6. **The DGX drops are STILL UNEXPLAINED — five theories refuted, four of them
   mine** *(carried; not investigated this session)*. **Do not propose a sixth
   without a captured outage in which the host was demonstrably up throughout.**
   Triage with `journalctl --list-boots` first. **Power is not a candidate**:
   the DGX is on a ~5-day UPS and the journal holds exactly one unclean stop, a
   scheduled PSU install. Reach it on the LAN, but **look the address up** — it
   is DHCP and has been `192.168.68.62`, `192.168.68.76`, and now
   `192.168.1.99`, on two different subnets. **Do not edit
   `/etc/wireguard/wg0.conf`.** Note the control column was broken for this
   entire period (see §2) — **any `lan=FAIL` reading in a log line without an
   `@addr` suffix proves nothing.**
7. **A single `tunnel=FAIL` probe sample is not an outage** *(carried)* — it is
   Starlink losing three packets on a ~900 ms path. Sustained means several
   consecutive samples. Both probes are still running.
8. **`body_lang_pending` means claimable work only** *(carried from #251)*; the
   turned-away remainder is `body_lang_declined`. A steady non-zero `declined`
   is **normal**. Only `--retry-declined` moves rows back to pending; only
   `--relabel` re-opens rows that already carry a label.
9. **`LangDetectPass` deliberately has no `__bool__`** *(carried)*. Callers must
   write `result.visited == 0`; an implicit reading of this value is what wedged
   the archive in #251.
10. **`ExtractorConfigurationError` subclasses `TransientExtractorError`, and
    that subclassing *is* the #248 fix — do not "clean it up"** *(carried)*.
11. **`type-skipped` is a one-way door for a widened allowlist** *(carried)*.
    Clear deliberately: `DELETE FROM attachment_text WHERE extractor =
    'type-skipped'`. #251 and #255 both deliberately avoided repeating this shape.
12. **Test trap: `_try_import_docling` must be monkeypatched in BOTH namespaces**
    *(carried)*.
13. **OCR is macOS-only by default** *(carried)*; installing the `[extraction]`
    extra is the whole fix on Linux. Budget ~5.5 GB of venv on aarch64.
14. **CI trap** *(carried)*: any GUI admin panel that fetches on mount MUST be
    stubbed in **both** `AdminView.test.ts` and `MainView.test.ts`.
15. **`uv sync` without extras silently downgrades a host** *(carried)*. Use
    `--all-extras` on the Mac and `--extra mcp --extra extraction` on the DGX.
    **`uv` is not on the DGX's default non-interactive PATH** — use
    `~/.local/bin/uv` over SSH.
16. **Do not run the test suite while a backfill is draining** *(carried)* —
    contention on the shared cluster produces dozens of false failures.
17. **macOS test noise** *(carried)* — `test_daemon_control_socket.py` fails
    locally (`AF_UNIX path too long`); deselect it, Linux CI is the real signal.
    Also carried: psycopg_pool teardown `ResourceWarning`s, the websockets
    `DeprecationWarning` (#25), Starlette TestClient `httpx` `DeprecationWarning`,
    jsdom canvas noise in gui vitest.
18. **The stale NOTIFY queue recurs** *(carried)* — if exactly the three
    LISTEN/NOTIFY tests fail with "could not access status of transaction N", it
    is Fault 1 in the postgres-maintenance runbook, not a code bug. Cycling the
    daemon clears it, but **give it a minute** before re-running.
19. **No ROADMAP.md** *(carried, re-confirmed)* — the `/nextsession` ROADMAP step
    is a no-op; slice status lives in NEXT_SESSION + `docs/handoffs/` + the specs.
    README **was** updated this session (`--relabel`, the corrected
    `body_lang_low_accuracy` default, the URL-stripping rationale).
20. **Secrets/ACL invariants unchanged** *(carried)*: `secrets.configure`'s pin
    is kept even though #245 is fixed; #246 warns rather than refuses on
    group-write; `InsecureSecretsFile` refuses rather than warns; #239's manual
    tombstone retention is deliberate; admin bearer tokens have no per-token
    scope (#204).
21. **Run vitest from `gui/`, not the repo root** *(carried)*.
22. **`cargo clippy --all-targets` is clean but ungated** *(carried)*.

## Exact commands to resume

```bash
cd /Users/hherb/src/localmail
git fetch --prune origin                 # ALWAYS first
git status                               # expect clean
git branch --show-current                # main
git log --oneline origin/main..main      # expect 0 — everything is pushed

# Python test suite (deselect the macOS-only socket failure — see risk 17).
# Do NOT run this while a backfill is draining — see risk 16.
unset VIRTUAL_ENV && uv run pytest -q --deselect tests/test_daemon_control_socket.py
#   expect: **2205 passed, 0 failed**
unset VIRTUAL_ENV && uv run mypy src/localmail
#   expect: Success, 135 source files

# #255 — confirm both hosts stayed correct. `implausible` is the signal:
psql -h localhost -p 5532 -U localmail -d localmail -c "
  SELECT count(*) FILTER (WHERE body_lang IS NOT NULL) AS populated,
         count(*) FILTER (WHERE body_lang IS NULL AND body_text IS NOT NULL
                            AND body_lang_attempted_at IS NULL) AS claimable,
         count(*) FILTER (WHERE body_lang IS NULL AND body_text IS NOT NULL
                            AND body_lang_attempted_at IS NOT NULL) AS declined,
         count(*) FILTER (WHERE body_lang IS NOT NULL AND NOT (body_lang = ANY(
           ARRAY['en','de','fr','es','it','nl','sv','da','no','pt']))) AS implausible
    FROM messages"

# The DGX — LAN only, and look the address up; it is DHCP (risk 6).
# Note uv is NOT on the non-interactive PATH there (risk 15):
ssh 192.168.1.99 'systemctl --user is-active localmail-daemon localmail-serve localmail-wgprobe'
ssh 192.168.1.99 'cd ~/src/localmail && ~/.local/bin/uv run localmail search-status'

# The probe's control column now records which address answered (PR #260):
tail -5 ~/localmail-probe/tunnel-probe.log   # expect lan=ok(3/3)@<addr>

# Frontend — only if you touch gui/ (MUST be run from gui/ — see risk 21):
cd gui && npm run check && npm test && npm run build && cd ..
cd gui/src-tauri && cargo test && cargo clippy --locked -- -D warnings \
  && cargo clippy --all-targets -- -D warnings && cd ../..
```

Session-16 code is **`19be6a2`** (PR #258, the #255 fix, spec + plan included)
and **`3063db8`** (PR #260, the probe control). Both deployments run
**`19be6a2`**; the probe fix is Mac-only by nature and is deployed to
`~/localmail-probe/tunnel-probe.sh`.

Latest migration **`0035_messages_body_lang_attempted_at.sql`**; next free slot
`0036_*.sql`. **Open issues: 12** — **#255 closed**, **#90 closed**,
**#259 filed**. Dependabot: **0** open alerts.
