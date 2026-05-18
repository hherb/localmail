# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-05-18 (very late session).** HTML sanitiser
> migrated from `bleach` to `nh3` on branch
> `feat/sanitize-via-nh3` (commit `b6d89be`); PR **#42** open against
> `main` ("feat(api): migrate HTML sanitiser from bleach to nh3 (#13)").
> Tests green: 477 passed (was 472, +5 new nh3-specific regressions).
> Per-user account ACL (previous session) merged as PR #41 on `main`
> at commit `4e2e2f1`.

## Project context (1-minute version)

`localmail` mirrors IMAP accounts (Gmail OAuth, password) into Postgres.
**Strictly read-only with respect to IMAP**. Downstream consumers read
DB + attachment tree directly or via the `localmail serve` HTTPS API.
See [CLAUDE.md](CLAUDE.md), [README.md](README.md), and
[docs/superpowers/specs/2026-05-17-localmail-gui-design.md](docs/superpowers/specs/2026-05-17-localmail-gui-design.md).

## What this session shipped

| SHA | What |
|---|---|
| `b6d89be` | `feat(api): migrate HTML sanitiser from bleach to nh3 (closes #13)` — branch `feat/sanitize-via-nh3`, PR #42. |

Concrete deliverables in `b6d89be`:

- `src/localmail/api/sanitize.py` — rewritten against `nh3`. Uses
  `clean_content_tags={script, style, noscript, iframe, object, embed,
  applet, form}` (replaces the old regex pre-pass) and
  `filter_style_properties=...` (replaces `bleach.css_sanitizer`).
  All numeric/string tunables centralised at module top: `_ALLOWED_TAGS`,
  `_ALLOWED_ATTRS`, `_ALLOWED_URL_SCHEMES`, `_CLEAN_CONTENT_TAGS`,
  `_ALLOWED_STYLE_PROPERTIES`.
- `pyproject.toml` — dropped `bleach[css]>=6.2`; added `nh3>=0.2.18`.
- `tests/test_api_sanitize.py` — loosened two whitespace-pinned
  assertions (bleach emitted `color: red`; nh3 emits `color:red` — both
  preserve the security invariant). Added 5 new regression tests:
    - `test_anchor_gains_rel_noopener_noreferrer` — pins nh3's auto
      `rel` injection (tabnabbing protection).
    - `test_script_src_cid_does_not_leak_attachment_url` — confirms
      that `_rewrite_image_srcs` → sanitise ordering doesn't surface
      a rewritten `/v1/attachments/<sha>` URL when the dangerous tag
      sits in `clean_content_tags`.
    - `test_iframe_content_fully_stripped`.
    - `test_unclosed_script_tag_content_stripped` — `html5ever`
      handles unclosed `<script>` at EOF where the old regex pre-pass
      required a matching closer.
    - `test_comments_stripped` — pins `strip_comments=True`.
- `docs/superpowers/specs/2026-05-17-localmail-gui-design.md` — design
  table now references `nh3` and notes the CSS-property allowlist
  affordance.
- `src/localmail/api/__init__.py` — one-line docstring updated.

Test surface: 14 → 19 sanitize tests; suite total 472 → 477. mypy clean.

## What's next — concrete acceptance criteria

PR #42 needs to merge first. Once it does:

### 1. Merge PR #42 and clean up

```bash
gh pr view 42                       # confirm CI green
gh pr merge 42 --squash --delete-branch
git checkout main && git pull
```

### 2. Distinguish transient vs poison-pill in `extract_worker` (#36)

Mirrors the policy `embed_worker` already enforces. Today a network blip
during a `docling` model download can mark a perfectly fine PDF as
permanently failed.

Acceptance: scope "permanent failure" classes narrowly (parser raises,
MIME mismatch, byte-stream truncation that survives a retry, etc.) and
let everything else back off and retry without incrementing the failure
counter past `extract_worker_max_retries`. New test covering each
classification. `failed_extractions.error_class` continues to be the
canonical poison-pill marker.

### 3. RRF `k` re-tuning (#35)

Acceptance: with the multilingual acceptance harness
(`tests/acceptance/run_recall_eval.py`), sweep `rrf_k ∈ {30, 45, 60, 90}`
and pick the value that maximises mean recall@20 across de/en/es/ja
without dropping any language below the existing gate (recall@20 ≥ 80%
+ MRR@20 ≥ 0.5). Land the winner as the new default in `SearchConfig`
and add a one-line note in CLAUDE.md.

### Other open issues (unchanged from previous handoff)

- **#40** Partial index `WHERE body_lang IS NULL` for the lang-detect
  worker — not blocking until archives exceed ~100k messages.
- **#37** Unify ConnectionPool ceiling across IDLE/poll/embed/extract
  workers (+ closes #9).
- **#38** `/v1/changes` semantics — initial backfill window decision.
- **#32** Attachment streaming — Range support, Content-Disposition,
  MIME hardening.
- **#33** Unify ID typing across endpoints (strings on the wire, ints
  in DB).
- **#27 / #28 / #22 / #24 / #18 / #17** GUI client polish & CI.
- **#25** uvicorn / `websockets.legacy` deprecation (still firing
  during `test_e2e_serve.py`).
- **#10 / #12** Persist Content-ID on attachments (inline `cid:`
  rendering).
- **#7** IP-based / global login rate limiter.
- **#5** Batch INSERT for chunking loop.
- **#4** Embedding backend — explicit model paths + verified
  query/document task prefixes.
- **#3** `db._split_statements` is `sqlparse`-backed; verify before
  closing.
- **#2** Migration 0006 — make GIN index builds CONCURRENT for
  production upgrades.

## Open decisions & risks

1. **CSS allowlist drift from bleach.** The new
   `_ALLOWED_STYLE_PROPERTIES` includes `margin*`/`padding*`/`max-*` /
   `min-*` (bleach's default excluded them). These are completely safe
   for email layout and dramatically improve rendering of marketing
   mail. If a future fuzz pass surfaces a CSS-property-based attack
   we missed, narrowing this list is one constant-edit away.

2. **`<a>` always gets `rel="noopener noreferrer"`.** New behaviour
   (cheap tabnabbing protection). If the GUI client opens links inline
   it doesn't matter; if it ever shells out to a system browser the
   `rel` doesn't hurt either. Worth flagging if a downstream consumer
   ever needs to parse the rel attribute back out.

3. **`nh3` is a compiled (Rust) dependency.** Adds a `cp312-cp312`
   wheel to the lock; macOS/Linux wheels exist; alpine musl wheels
   exist too. CI should be fine — confirm in the PR.

4. **Dependabot — 12 vulnerabilities** (1 high / 9 mod / 2 low) on
   `main` still need triage. `bleach` was not one of them; the count
   should drop or stay the same after #42 merges (run
   `gh api repos/hherb/localmail/vulnerability-alerts` after merge to
   confirm).

5. **`websockets.legacy` DeprecationWarning** (issue #25) still fires
   during `test_e2e_serve.py`. Pre-existing; this session didn't
   change it. Tracked.

6. **Existing pre-#41 single-operator upgrade.** Anyone running a
   pre-`4e2e2f1` build with a created API user will see empty
   `/v1/accounts` until they run
   `localmail grant-account USERNAME <each-account>`. README upgrade
   note is in place; flag loudly before any release.

## Exact commands to resume

```bash
# Where you are.
cd /Users/hherb/src/localmail
git status                                 # feat/sanitize-via-nh3 if not yet merged

# Quick sanity check.
unset VIRTUAL_ENV && uv sync
unset VIRTUAL_ENV && uv run pytest -q       # expect 477 passed

# Merge the nh3 PR.
gh pr view 42                               # confirm CI green
gh pr merge 42 --squash --delete-branch
git checkout main && git pull

# Triage the next issue.
gh issue list --state open --limit 40

# If you pick #36 (extract_worker transient vs poison):
git checkout -b fix/extract-worker-transient-vs-poison

# If you pick #35 (RRF k re-tuning):
git checkout -b chore/rrf-k-retune
# Then run the acceptance harness:
unset VIRTUAL_ENV && uv run python tests/acceptance/run_recall_eval.py
```

## Known gotchas (still load-bearing)

- **`unset VIRTUAL_ENV && uv run …`** — shells frequently have a stale
  `VIRTUAL_ENV` pointing at some other pyenv venv; `uv run --active`
  picks the wrong interpreter without this.
- **`LOCALMAIL_TEST_DSN` defaults to `postgresql:///localmail_test`** —
  tests must not touch the live `localmail` DB.
- **Migrations 0011–0016 are additive.** Re-running `init-db` on an
  older archive is safe (idempotent), but back up first if the archive
  is non-trivial.
- **`docling` is the Phase 2 extractor.** Install with
  `uv sync --extra extraction` if you need attachment-text extraction
  locally.
- **TLS for `localmail serve`** — `--bind 0.0.0.0` requires `--tls-cert`
  + `--tls-key`. `--no-tls` is only honoured on `127.0.0.1`. Use
  `localmail rotate-tls` to generate a self-signed cert.
- **First-time `body_lang` install** — run `localmail lang-backfill`
  (or `embed-backfill`, which drains both queues) once after upgrade so
  `lang:` queries return rows.
- **ACL upgrade**: post-0016, new API users have **no grants**. Run
  `localmail grant-account USERNAME ACCOUNT_NAME` once per pair.
- **nh3 style-attribute whitespace**: nh3 emits compact
  `color:red` (no space after `:`); never assert exact whitespace in
  sanitiser tests — assert that the property and value both survived.

## File map (post-nh3, on branch `feat/sanitize-via-nh3`)

```
src/localmail/
  api/                               # transport-free service library
    accounts.py acl.py attachments.py auth.py errors.py messages.py
    sanitize.py                      # NEW: nh3-based (was bleach)
    search.py
  serve/                             # FastAPI wrapper + TLS + middleware
    app.py middleware.py tls.py
    routes/                          # auth, accounts, messages, attachments,
                                     # changes, search, version  (all ACL-aware)
  search/
    arms.py chunking.py embed_worker.py embeddings.py extractor.py
    extract_worker.py lang_detect.py page_cache.py query.py reranker.py
    searcher.py
gui/                                 # Tauri 2 + Svelte 5 client
migrations/                          # 0001 … 0016_user_accounts.sql
docs/superpowers/                    # specs + plans
docs/handoffs/                       # frozen NEXT_SESSION snapshots
NEXT_SESSION.md                      # this file
```

End of nh3-migration session. One commit (`b6d89be`) on
`feat/sanitize-via-nh3`; PR #42 open. Merge it, then pick the next
issue.
