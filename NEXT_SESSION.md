# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-10-21 (session 53).** `main` started this session at
> **`e91471e`**. The operator merged #397 between sessions, and the Mac was
> already deployed on it (both agents restarted 2026-10-14 18:32).
>
> This session:
> - **verified the #397 deploy on the Mac**:
>   - `/v1/version` → `build_hash: e91471e`;
>   - installed versions: pypdf 6.20.1, rapidocr 3.9.2 (the cap held),
>     opencv-python 4.14.0.94;
>   - extraction running since the restart, and `transient_extractions` = 0.
> - **found the DGX unreachable.** The tunnel probe on this Mac reports
>   `tunnel=FAIL lan=FAIL hub=ok`, so the DGX's #397 deploy and its
>   aarch64 OCR check are **still not done**.
> - **found 18 new Dependabot alerts (#115–#132)**, 15 of them docling, and
>   discovered while triaging them that **docling was converting non-PDF
>   attachments**: an HTML file labelled `invoice.pdf` reached docling's HTML
>   backend. Fixed in **PR #399**, together with the other floors.
>
> **Open issues: 40. Dependabot: 19 open.** #399 closes #115, #117 and
> #132. That leaves #71 (accelerate) and the 15 docling alerts this PR
> deliberately does not bump (#116, #118–#131). Those are to be
> **dismissed** after the merge (step 0), which takes it to **#71 alone**.
>
> **Three things to know before you touch anything.**
>
> **(1) The Mac tree is on the PR branch, and the venv is synced to it.** Any
> restart runs `fix/docling-pdf-only-and-floors`: mcp 1.30.0, fsspec
> 2026.9.0, and the PDF-only restriction. That is safe (full suite green),
> but if #399 is rejected, run `git checkout main && uv sync --frozen
> --extra mcp --extra extraction` before the next restart.
>
> **(2) "docling is PDF-only" was a claim about our routing, not about
> docling.** `DocumentConverter` admits every format by default and sniffs
> the bytes. The restriction now lives in
> `extractor._pdf_only_converter_kwargs`, and nothing else stands between a
> sender-labelled `.pdf` and docling's other backends. **Do not remove it
> during the docling bump.**
>
> **(3) The docling 2.132 bump is still owed, as its own PR.** It forces
> `docling-ibm-models` 3 → 4 (new layout models). Run a before/after OCR
> probe under **rapidocr and ocrmac**, against `main` in its own worktree
> venv. The last two sessions' probes were not kept, so write one again;
> the shape is in CLAUDE.md's October 2026 entries.

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

### PR #399: `fix/docling-pdf-only-and-floors` (opened on `main`)

| SHA | What |
|---|---|
| `7b94283` | **Restrict docling to PDF input**: `_pdf_only_converter_kwargs` on both converter paths + `tests/test_docling_input_formats.py` |
| `c4801dc` | `mcp>=1.30.0`, `fsspec>=2026.6.0`, `uv.lock`; `validate_token_resource=False` + its test |
| `c0dce76` | GUI: `rustls >= 0.23.45` in `Cargo.toml` (lock 0.23.46); `npm audit fix` (vite 8.0.16 → 8.3.4) |
| `beaf1fc` | README (the poison-pill bullet) and CLAUDE.md (the second October round) |

Plus this handoff, in the same PR.

**The finding that mattered.** The obvious triage was "every docling
advisory is in a non-PDF backend and `DoclingExtractor` is PDF-only, so all
15 are unreachable". The premise was false:

- **Routing:** `is_pdf` routes on a MIME type and a filename, both written
  by the sender.
- **Conversion:** `DocumentConverter` had no `allowed_formats`, and it
  chooses a backend by sniffing the bytes of the extension-less blob path.

Reproduced against the live install:

- **HTML labelled `invoice.pdf`** → pypdf fails → docling fallback →
  extracted by the **HTML backend**. That is #122, the `rowspan` DoS, on a
  mail-borne path.
- **ODT labelled `invoice.pdf`** → stopped only by `odfdo` being absent.
  It failed as `OCR engine 'auto' (search.extractor_ocr_engine) is not
  usable`, so a hostile file was
  held on the *transient* budget instead of being poison-pilled.

After the fix both are refused as plain `ExtractorError`s.

**Other alerts, all hygiene:**

| alert | floor | why it is hygiene |
|---|---|---|
| #132 mcp | `>=1.30.0` (lock held on 1.x; unconstrained → 2.3) | stateful-mode session leak; we run `stateless_http=True`, which the advisory exempts |
| #117 fsspec (HIGH) | `>=2026.6.0`, newly declared | needs `ReferenceFileSystem`; nothing opens it |
| #115 rustls | `>= 0.23.45` in `Cargo.toml` | GUI client TLS; a direct dependency |
| vite (npm audit only) | lock-only | launch-editor, dev server only |

**mcp 1.30 also announced a breaking default.** `validate_token_resource`
will default to `True` in 3.0, which would refuse every token: neither
verifier populates `AccessToken.resource`, OAuth ones included. Set
explicitly to `False`; on the AS path `access.load_access` enforces RFC 8707
itself. Against `main` (mcp 1.28.1) this prevents ~90 warnings the bump
would otherwise add.

## Verification (this Mac, all extras, at `beaf1fc`)

| gate | result |
|---|---|
| pytest | **4044 passed, 0 failed**, 18 warnings, 291 s |
| mypy | Success, **160** files |
| `cargo test --locked` | 109 passed |
| `cargo clippy --all-targets -D warnings` | clean |
| `gui/` vitest | **512 passed**, 49 files |
| `svelte-check` | 326 files, 0 errors |
| `npm run build` / `npm audit` | built / 0 vulnerabilities |
| CI | see `gh pr checks 399`; I stopped at the push |

- **+6 tests**, measured with `--collect-only`: `main` at `e91471e`
  collects **4038** and the branch **4044**. That is 5 in the new file plus
  the MCP build test. (#397's handoff quoted 4035 for its first commit; its
  review round added 3 before merge.)
- **The new docling tests skip in CI** (`importorskip("docling")`), except
  the wiring test, which fakes the docling modules. CI measured: `4037
  passed, 7 skipped`, against `main`'s `4034 passed, 4 skipped` — 3 new
  skips (html, odt, the positive control); the MCP test runs there.
- **Mutation:** making `_pdf_only_converter_kwargs` return `{}` fails 4
  tests (5 after the review round's fail-closed pin), and the positive
  control alone survives.
- **Review round:** the helper now fails closed instead of returning `{}`
  when `InputFormat` is unimportable (see CLAUDE.md); the 8 fake-docling
  tests stub it. Verified with docling blocked on `sys.meta_path`:
  `84 passed, 7 skipped` over the three docling test files. The MCP test was RED first
  (`None is False`).
- **18 warnings:** the known 3, two `hf_xet` from the fastembed smoke test,
  and ~13 torch/docling deprecations from the new real-docling tests. These
  were 106 before the `validate_token_resource` change.
- **Load** was 20–40 all session, with another repo's pytest running beside
  it. No timing is comparable.

## What's next

### 0. **Merge #399, deploy, dismiss** *(acceptance: Dependabot → #71 only)*
- **You merge**, once `gh pr checks 399` is green on both legs.
- **Mac:** `git checkout main && git pull`, then `uv sync --frozen --extra
  mcp --extra extraction` (a no-op: the venv is already synced), then
  `kickstart -k` both agents.
- **Then dismiss the 15 docling alerts.** Do it **only once #399 is on
  `main`**, or the reason is false.
  ```bash
  for n in 116 118 119 120 121 122 123 124 125 126 127 128 129 130 131; do
    gh api -X PATCH repos/hherb/localmail/dependabot/alerts/$n \
      -f state=dismissed -f dismissed_reason=not_used \
      -f dismissed_comment="DoclingExtractor restricts DocumentConverter to InputFormat.PDF (#399); this advisory is in a non-PDF backend or remote fetch. Bump to >=2.132 tracked separately (ibm-models 4)."
  done
  ```
  - #117 is fsspec and is closed by the floor, so it is not in the list.
  - #126/#127 and #128/#129 are docling/docling-slim pairs, all in the
    list.
- **Acceptance:**
  - `/v1/version` = the merge commit;
  - `gh api …/dependabot/alerts` lists **#71 only** a few minutes after the
    merge (alerts lag);
  - `/mcp` still accepts a login bearer token (the
    `validate_token_resource` change).

### 1. **The DGX: restore reachability, then deploy #397 + #399**
- **Triage per the `dgx-network-drops` memory:** `journalctl --list-boots`
  first, then the persistent probe logs. Its LAN IP moves (DHCP).
- **Acceptance:**
  - both units report `NRestarts=0` and `build_hash` = the `main` merge
    commit;
  - `uv pip show pypdf` → 6.20.1;
  - **a scanned PDF extracts with text under rapidocr on aarch64**
    (`extractor = 'docling'`, non-empty text). This is carried from #397 and
    is still the only check of the rapidocr/OpenCV caps on that wheel set.

### 2. **docling 2.132 + ibm-models 4, as its own PR**
- **Acceptance:**
  - a before/after OCR probe (scanned invoice, scanned table, native table)
    under rapidocr **and** ocrmac, `main` vs branch, each in its own
    worktree venv;
  - the differences are explained, or the bump is held;
  - `_pdf_only_converter_kwargs` is untouched and its tests still pass.

### 3. **kastellan** (carried, yours)
- Ask whether it still wants slice C (`/problems/token-expired`); its API
  key never expires.
- Tell it about `?headers=list` (api_minor ≥ 1), attachment index paging
  (≥ 2; never compute the offset from `text.length`), and `fields` /
  `snippet_chars` (≥ 3).

### 4. **Carried issues**
- **Most recent:**
  - #398 (explicit `ocrmac` engine fails on language codes);
  - #394 (`fields` unbounded; a wire-contract change);
  - #395 (a rule module forbidding new `{@html}` sinks).
- **Earlier:** #386, #389, #380, #383, #384, #377, #378, #369, #373, #372,
  #370, #371, #368, #365.
- **Not filed yet:** `snippet_source="header"` mislabel on a whole-message
  lexical win.
- **Larger work:**
  - #375 (Mac build-hash timeout cache);
  - #374 + #305 (the `cli.py` refactor, 2177 lines);
  - #358/#357, #330/#327/#328, #319/#318/#320 (the next migration slot is
    `0037_*.sql`), #340, #363, #285 (ruff in CI);
  - admin GUI phase 5, the Users panel;
  - robustness: #218, #226, #225/#227, #200/#211/#208, #206, #204, #25.

## Rulings made this session

1. **The new alerts came before the carried plan.** The DGX deploy was
   blocked anyway, and the triage found a live mail-borne path. *Cost if
   wrong:* none; nothing else was blocked.
2. **The docling bump was deferred and the alerts are to be dismissed**
   (operator's choice of three). The restriction makes them unreachable,
   while the bump changes extraction output. *Cost if wrong:* a non-PDF
   advisory that somehow reaches the PDF backend stays unfixed until the
   bump PR.
3. **The fix restricts formats rather than validating bytes ourselves.**
   docling already knows how to refuse a format, and a second sniffer would
   be a second authority. *Cost if wrong:* we trust docling's PDF format
   check.
4. **`_pdf_only_converter_kwargs` fails open (`{}`) when `InputFormat` is
   unimportable.** That happens only without docling, where no real
   converter exists; CI's fakes need it. *Cost if wrong:* a docling
   installed without `InputFormat`, which the `>=2.118.1` floor rules out.
5. **The dismissals wait for the merge**, so the dismissal reason is true
   when it is written.
6. **`validate_token_resource=False`, not `True`.** `True` would refuse every
   opaque login token. *Cost if wrong:* none in behaviour, since unset already
   meant False.
7. **Code and handoff are in one PR**, per the convention.

## Open decisions & risks

1. **#399 is open and yours to merge**, then dismiss (step 0).
2. **The Mac runs the branch on its next restart** (Things to know, (1)).
3. **The DGX has not received #397 or #399.** Its aarch64 OCR is unverified.
4. **oauthlib 4.0 and docling-parse 7 (from #397) are still lightly
   exercised.** If `oauth-login` or native-PDF fallback extraction
   misbehaves, suspect them first.
5. **A future docling or extra that installs `odfdo`** would have made #116
   live before this fix; with it, it is refused. Keep the restriction.
6. **`api_minor` stays 3.** Nothing on the wire changed.
7. **THIS FILE IS NOT THE AUTHORITY; `git` and `gh` are.**
8. **Never run two pytest sessions against one test database.** Another
   repo's pytest sharing the machine is fine; it uses its own database.
9. **Restore a mutation from a file copy, never with `git checkout`.**
10. **The test DB is on port 5532**, the same cluster as the live archive.
11. **Always sync with `--extra mcp --extra extraction` on both hosts**, and
    use `~/.local/bin/uv` on the DGX.
12. **`uv sync --dry-run` is not read-only; `uv lock --dry-run` is.**
13. **The scratchpad does not survive between sessions.** Last session's
    probe script was gone; keep anything reusable in the tree or in this
    file.
14. **No ROADMAP.md.** That `/nextsession` step is a no-op.

## Exact commands to resume

```bash
cd /Users/hherb/src/localmail
git fetch --prune origin                 # ALWAYS first
git status && git branch --show-current
git log --oneline -1 origin/main         # THE authority

# Is #399 merged?
gh pr view 399 --json state,mergeCommit --jq '"\(.state) \(.mergeCommit.oid // "-")"'
gh pr checks 399
git log --oneline main..origin/fix/docling-pdf-only-and-floors   # non-empty after merge = stranded
gh issue list --limit 100 --json number --jq length      # 40
gh api --paginate repos/hherb/localmail/dependabot/alerts \
  --jq '.[] | select(.state=="open") | "\(.number)\t\(.dependency.package.name)"'

# Deployment health
launchctl list | grep localmail
curl -sk https://127.0.0.1:8443/v1/version
tail -3 ~/localmail-probe/tunnel-probe.log     # tunnel/lan FAIL = DGX unreachable
ssh -o ConnectTimeout=8 10.0.0.3 'curl -sk https://10.0.0.3:8443/v1/version'

# Deploy after merge — Mac:
#   git checkout main && git pull
#   unset VIRTUAL_ENV && uv sync --frozen --extra mcp --extra extraction
#   unset VIRTUAL_ENV && uv run localmail init-db      # expect "schema already up to date"
#   launchctl kickstart -k gui/$(id -u)/com.localmail.daemon
#   launchctl kickstart -k gui/$(id -u)/com.localmail.serve
# DGX:
#   ssh 10.0.0.3 'cd ~/src/localmail && git pull --ff-only && ~/.local/bin/uv sync --frozen --extra extraction --extra mcp'
#   ssh 10.0.0.3 'systemctl --user restart localmail-daemon localmail-serve'

# Load before trusting any timing.
uptime; ps -A -o pid,pcpu,etime,comm -r | head -6

# Suites.
unset VIRTUAL_ENV && uv run pytest -q              # 4044 passed (macOS, all extras)
unset VIRTUAL_ENV && uv run mypy src/localmail     # Success, 160 files
cd gui && npm test -- --run && npm run check && npm audit   # 512 / 0 errors / 0 vulns
cd gui/src-tauri && cargo test --locked && cargo clippy --locked --all-targets -- -D warnings

# This change directly:
unset VIRTUAL_ENV && uv run pytest -q tests/test_docling_input_formats.py tests/test_mcp_server_build.py
```

`main` was **`e91471e`** at session start and is unchanged. This session left
**one PR**: **#399** on `fix/docling-pdf-only-and-floors`, commits
`7b94283..beaf1fc`, plus this handoff.

The latest migration is still **`0036_api_keys.sql`**; the next free slot is
`0037_*.sql`. **Open issues: 40. Dependabot: 19 open → #71 alone after #399
merges and the 15 dismissals land.**
