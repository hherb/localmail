# Handover — localmail hybrid search, Phase 1 done, ready for acceptance

Read this end-to-end before doing anything. Cross-reference with the spec, the
plan, and CLAUDE.md as you go. Don't skip the "Key reading" section.

## TL;DR — what's done, what's next

**Done.** Phase 1 of the hybrid search subsystem shipped on branch
`worktree-phase1-hybrid-search`, opened as
[PR #1](https://github.com/hherb/localmail/pull/1). 32 commits, **149**
unit + integration tests passing (`unset VIRTUAL_ENV && uv run pytest -q`,
slow test included now that EmbeddingGemma resolves). No known failing
tests.

**Phase 1 acceptance: PASS (2026-05-16).** All gated languages cleared the
gate with room to spare:

| lang | #q | recall@20 | MRR@20 | gate |
|------|----|-----------|--------|------|
| de   | 20 | 1.000     | 0.967  | PASS |
| en   | 20 | 1.000     | 1.000  | PASS |
| es   | 20 | 1.000     | 0.963  | PASS |
| ja   | 20 | 1.000     | 0.938  | PASS |
| no   | 10 | 1.000     | 0.950  | —    |

1. ~~Fix the embedding-model registry mismatch~~ **DONE in commit
   `128a398`**.
2. ~~Author the multilingual ground-truth query set~~ **DONE** — see
   `tests/fixtures/multilingual_queries.json` (90 queries: 20 de/en/es/ja
   + 10 no). Schema in Step 2.
3. ~~Run the Phase 1 acceptance harness~~ **DONE — PASS** (table above).

After acceptance: Phase 2 (attachment extraction + Arm 4) gets its own
brainstorm → spec → plan cycle. Don't start coding Phase 2 work yet.

## How to resume in this worktree

You're picking up a worktree the previous session created via the
`EnterWorktree` superpowers skill. Re-entering it:

```bash
# Worktree path:
cd /Users/hherb/src/localmail/.claude/worktrees/phase1-hybrid-search
# Branch:
git branch --show-current     # → worktree-phase1-hybrid-search
# Last commit:
git log --oneline -1          # most recent commit on the branch
```

If your harness has an `EnterWorktree` tool, prefer
`EnterWorktree(path="/Users/hherb/src/localmail/.claude/worktrees/phase1-hybrid-search")`
over a manual `cd`. Otherwise the manual `cd` is fine; everything in this
handover is run from the worktree root.

Sanity check that the worktree is clean and tests pass before doing anything:

```bash
git status                    # should be clean
unset VIRTUAL_ENV && uv sync  # idempotent
unset VIRTUAL_ENV && uv run pytest -q -m "not slow"
# expected: 148 passed, 1 deselected
```

Note the `unset VIRTUAL_ENV` prefix — shells in this environment often have
`VIRTUAL_ENV` pointing at the wrong venv, which makes `uv run` pick the wrong
interpreter. The prefix is mandatory for every pytest / localmail invocation.

## Step 1 — install fastembed from source so EmbeddingGemma resolves

> **Status: DONE in commit `128a398`.** fastembed pinned to upstream
> commit `87678dd...`, model verified to download (768d), full test suite
> now reports **149 passed** (the previously-failing slow test passes).
> The "Why" and "What to do" below are kept for reference / future
> rollback. Skip to Step 2 if you're picking up the work as-is.

### Why this needed doing

Phase 1's `SearchConfig.embedding_model` defaults to `"embeddinggemma"`,
which `src/localmail/search/embeddings.py::_build_fastembed_inner` remaps
to the HuggingFace id `"google/embeddinggemma-300m"`. The remap is
correct — but **`fastembed 0.8.0`** (the latest PyPI release as of this
handover) **does not include EmbeddingGemma in its supported-models
catalog**. fastembed's PR #592 ("new: add gemmaembedding-300m") was
merged into `main` on **2026-03-25** at commit
**`87678dd784272f8e5d9fba034d7852d4233e58fd`**, but no PyPI release has
been cut since. The slow opt-in test
`tests/test_embeddings.py::test_fastembed_backend_real_model_smoke`
therefore fails with `ValueError: Model name google/embeddinggemma-300m is
not supported`, and every real-search invocation would fail the same way.

**User decision (during the handover write-up): compile fastembed from
source.** EmbeddingGemma's MTEB-Multilingual lead and Matryoshka curve
are substantial enough to justify the install complexity. We pin to the
post-PR-592 merge commit for reproducibility.

### What to do

The fix is purely an install-side change. No source code in this repo
needs to change — `_build_fastembed_inner`'s `embeddinggemma →
google/embeddinggemma-300m` remap is already correct for the post-PR-592
fastembed.

**1. Verify the upstream still has it.** A new fastembed PyPI release may
have shipped between this handover being written and you reading it. If
PyPI has a release with the feature, prefer it over a source install.

```bash
unset VIRTUAL_ENV && uv run python -c "
import urllib.request, json
data = json.load(urllib.request.urlopen('https://pypi.org/pypi/fastembed/json'))
latest = data['info']['version']
print('Latest PyPI fastembed:', latest)
"
unset VIRTUAL_ENV && uv run python -c "
from fastembed import TextEmbedding
print('Has Gemma in catalog:', any('gemma' in m.get('model','').lower() for m in TextEmbedding.list_supported_models()))
"
```

If the second command prints `True`, just `uv add fastembed@latest` and
skip to "Verify and commit" below.

If `False`, continue with the source install.

**2. Pin fastembed to the post-PR-592 commit.**

```bash
unset VIRTUAL_ENV && uv add "fastembed @ git+https://github.com/qdrant/fastembed.git@87678dd784272f8e5d9fba034d7852d4233e58fd"
```

This rewrites the `fastembed` entry in `pyproject.toml` to a git URL and
freezes the resolved revision in `uv.lock`. fastembed is pure-Python +
ONNX Runtime (no Rust, no compiled C extensions to build); the install
should complete in under a minute, no toolchain dependency.

If `uv add` complains about the SHA being unreachable, the merge commit
SHA is the source of truth — re-fetch by checking the GitHub URL
directly:

```bash
unset VIRTUAL_ENV && uv run python -c "
import urllib.request, json
req = urllib.request.Request('https://api.github.com/repos/qdrant/fastembed/pulls/592',
                             headers={'Accept': 'application/vnd.github+json'})
data = json.load(urllib.request.urlopen(req))
print('Use SHA:', data['merge_commit_sha'])
"
```

**3. Verify the catalog now has EmbeddingGemma.**

```bash
unset VIRTUAL_ENV && uv sync
unset VIRTUAL_ENV && uv run python -c "
from fastembed import TextEmbedding
import json
models = TextEmbedding.list_supported_models()
gemma = [m for m in models if 'gemma' in m.get('model','').lower()]
print('Gemma models:', json.dumps([{'model': m.get('model'), 'dim': m.get('dim')} for m in gemma], indent=2))
"
```

Expected: at least one entry with `model: 'google/embeddinggemma-300m'`
and `dim: 768`.

**4. Download the model.** fastembed auto-downloads on first instantiation;
trigger it once so the user knows the path works and to warm the cache
before the test suite runs:

```bash
unset VIRTUAL_ENV && uv run python -c "
from fastembed import TextEmbedding
te = TextEmbedding(model_name='google/embeddinggemma-300m')
v = list(te.embed(['health check probe']))[0]
print('Dim:', len(v))
print('Sample vec[:5]:', [round(x, 4) for x in v[:5]])
"
```

Expected output: `Dim: 768`. The model files (ONNX + tokenizer + config)
land under `~/.cache/fastembed/` (or `SearchConfig.fastembed_cache_dir`
if set). First run downloads ~250–600 MB depending on which quantization
variant fastembed pulls; subsequent runs are instant.

**Note on EmbeddingGemma weights and the Gemma Terms of Use.** The
weights are distributed under the [Gemma Terms of Use](https://ai.google.dev/gemma/terms).
By running the download you accept those terms. They do not restrict
personal mail search use, and they do not propagate into the AGPL-3.0
code in this repo (the weights are not bundled, only downloaded at
runtime). README already documents this; nothing to change.

### Verify and commit

```bash
unset VIRTUAL_ENV && uv run pytest -q                      # full suite incl. slow
# expected: 149 passed (the previously-failing slow test now passes)
unset VIRTUAL_ENV && uv run pytest -q -m "not slow"        # excluding slow
# expected: 148 passed, 1 deselected

git add pyproject.toml uv.lock
git commit -m "fix(deps): pin fastembed to post-PR-592 commit for EmbeddingGemma support

fastembed 0.8.0 (latest PyPI) does not include EmbeddingGemma. PR #592
('add gemmaembedding-300m') merged into upstream main at
87678dd784272f8e5d9fba034d7852d4233e58fd on 2026-03-25 but no PyPI
release has shipped since. Pin to the merge commit so
SearchConfig.embedding_model='embeddinggemma' resolves correctly. Bump
back to PyPI fastembed once they cut a release containing PR #592.
"
git push origin worktree-phase1-hybrid-search
```

The PR auto-updates with the new commit.

### Future: bump back to PyPI when a release ships

Periodically check whether a newer fastembed PyPI release includes
EmbeddingGemma. When it does, swap the git URL back to a version
constraint:

```bash
unset VIRTUAL_ENV && uv remove fastembed
unset VIRTUAL_ENV && uv add "fastembed>=X.Y"   # whatever version contains PR #592
```

Re-run the suite to confirm no regression, then commit the
`pyproject.toml` + `uv.lock` swap.

## Step 2 — author the multilingual ground-truth query set

> **Status: DONE.** `tests/fixtures/multilingual_queries.json` shipped
> with 90 queries (20 de/en/es/ja + 10 no), authored against the
> synthetic corpus in `tests/_multilingual_corpus.py::_SEED`. Mix:
> subject-term lexical, body-only, and paraphrase/conceptual. Step 3 ran
> against this file and PASSed all gates.

The acceptance harness needs a `tests/fixtures/multilingual_queries.json`
file (NOT the `.example.json` one, which is a template). The user authors
this — they know which messages in their own archive should be findable
under which queries.

**For the handover-tested path, use the synthetic fixture corpus.** The
file `tests/_multilingual_corpus.py` ships 50 synthetic emails (10 per
language) with `subject` + `body` per row. Pick relevant subjects from
that list and author queries that ought to surface them. Targets:

- 20 queries per language for de, en, es, ja
- 10 queries for no (Norwegian is reported but not gated — user said
  vocabulary-frugal language is fine with simpler full-text matching)

Schema of each query entry (see `multilingual_queries.example.json`):

```json
{
  "lang": "de",
  "query": "Berlin Konferenz",
  "relevant_subjects": ["Konferenz Berlin", "Konferenzprogramm anbei"]
}
```

`relevant_subjects` is a list of message subjects (verbatim string match)
that the harness considers correct hits for the query. The harness
computes recall@K and MRR@K against that ground truth.

You can either ask the user for their query set, or generate a starter
set yourself from the synthetic corpus subjects in
`tests/_multilingual_corpus.py::_SEED` and offer it for the user to
review. Lean toward generating — the user is more likely to refine a
draft than to start from scratch.

The example file (`tests/fixtures/multilingual_queries.example.json`) has
7 starter queries demonstrating the shape — extend it, save as
`multilingual_queries.json` (no `.example` infix).

## Step 3 — run the Phase 1 acceptance harness

> **Status: DONE — PASS (2026-05-16).** Embed worker converged in 2
> passes (chunk pass, then embed pass). Per-language results in the TL;DR
> table at the top. MRR@20 ranged 0.938 (ja) to 1.000 (en); recall@20 was
> 1.000 across the board. The Japanese result confirms Gemma's vector arm
> fully carries CJK despite the deferred tsvector `'simple'` concern.

Once the queries file exists and Step 1 is committed:

```bash
PYTHONPATH=src:. unset VIRTUAL_ENV && uv run python \
    tests/acceptance/run_recall_eval.py \
    --queries tests/fixtures/multilingual_queries.json \
    --k 20
```

(Yes, the `PYTHONPATH=src:.` prefix is required — fixed in Step 1 of the
final-review fixes; see `tests/acceptance/run_recall_eval.py` docstring
for the rationale.)

The harness:
1. Applies migrations to the test DB at `LOCALMAIL_TEST_DSN`
   (default `postgresql://localmail:local%40%40mail@localhost:5532/localmail_test`).
2. TRUNCATEs and seeds the synthetic corpus via `build_corpus(conn)`.
3. Runs `embed_worker` in a loop until all chunks are embedded.
4. Runs each query through `Searcher(reranker=None)` (un-reranked for the
   baseline measurement — reranker would hide the lexical vs vector gap).
5. Prints a per-language table of recall@20 and MRR@20 with PASS/FAIL
   gating for de/en/es/ja (target: recall ≥ 80%, MRR ≥ 0.5).

### What to do with the result

**If all gated languages PASS:** congratulate yourself, post the harness
output as a PR comment, ping the user to merge the PR. Phase 1 is done.

**If any gated language FAILS:** do not silently accept (Golden Rule 6).
Document what failed, then options in order of preference:

1. **Most likely cause: the synthetic corpus is too small.** 10 messages
   per language is enough to test the pipeline but easily produces
   recall=0.0 when the per-query relevant set is a singleton and the
   reranker is off. Re-run with `--candidates-per-arm 50` instead of the
   default — sometimes that's the whole story. (The harness already
   passes `args.k * 3` as candidates per arm, so default behavior is
   reasonable; try `--k 10` if recall is high but MRR is low.)

2. **If recall is genuinely low**, the next lever is the embedding model
   choice. Check the alternatives in Step 1; switching to
   `intfloat/multilingual-e5-large` (1024d) plus a schema migration
   (`halfvec(768)` → `halfvec(1024)` + re-embed) is a known quality
   improvement, but it's a real chunk of work. Get user approval before
   doing this.

3. **If only Japanese fails**, that's expected — tsvector's `'simple'`
   config doesn't tokenize CJK well. Phase 5 has a pg_trgm fallback in
   the open-questions list; document the gap and defer to Phase 5.

**Don't move to Phase 2 until either the gate passes or the gap is
documented with the user's acknowledgement.**

## After acceptance — what's next

Two parallel tracks possible.

**Track A: PR feedback iteration.** The PR may receive review comments
from the user. Read them, address them, push more commits to the same
branch. Use the `requesting-code-review` / `receiving-code-review`
superpowers skills if the feedback warrants formal review-loop discipline.

**Track B: Phase 2 design.** Once acceptance passes and the PR merges,
Phase 2 (attachment extraction + Arm 4) starts its own
brainstorm → spec → plan → execute cycle. Do **not** start coding Phase 2
artifacts yet — the spec already has Phase 2's scope sketch, but it needs
its own brainstorm pass to confirm priorities and surface any new
constraints. Invoke `superpowers:brainstorming` for that.

## Deferred concerns to remember (don't re-discover these)

Documented in `CLAUDE.md` (read the "Search subsystem" section) and in the
PR body. Summarized here so you don't waste time:

- **`_split_statements` in `db.py` is naive.** Splits on every `;`. Safe
  for migrations 0006 and 0009 (and 0001-0003 which run transactionally),
  but breaks if a future non-transactional migration includes semicolons
  inside string literals or dollar-quoted blocks. Phase 2's migrations
  should be designed to avoid this; long-term, replace with `sqlparse` or
  a real tokenizer.

- **`messages.fts_v2` C-weight field includes `body_html`** alongside
  `body_text`. Plan said `body_text` only; implementer added `body_html`
  for HTML-only newsletter coverage. May dilute ranking via HTML markup
  tokens (`<p>`, `href`, …). If acceptance recall suffers and you trace
  it to HTML noise, strip HTML in chunking before storing, or revert to
  body_text only in migration 0010 (Phase 2 / 5).

- **`bm25_field_boosts` config values >1.0 get normalized by `max()`**
  before passing to `ts_rank_cd` (which requires weights in [0, 1]).
  Relative ratios preserved. Users should write boost values >1.0 as
  intended without surprise; the docs in CLAUDE.md note this.

- **MCP server is Phase 3, not Phase 1.** Don't add it now. Python API
  consumers can use `localmail.search.create_searcher` directly.

- **`--smart` query rewriter is Phase 4.** Calling
  `Searcher.search(..., smart=True)` raises `RuntimeError` in Phase 1.

## Where decisions changed during execution

Two design decisions changed during Phase 1 implementation. Both are
captured in the spec / plan (see commits `708c277` and `8351a75`), but
flagging here so you don't re-litigate them:

1. **BM25 backend: `pg_search` → PG built-in `tsvector`.** The original
   spec selected ParadeDB's `pg_search`, but its prebuilt binaries don't
   cover current macOS releases and the pgrx source build re-breaks on
   each PG upgrade. The user voted for install robustness; we switched.
   `pg_search` remains documented as a Phase 5+ upgrade path.

2. **Default embedding model: `EmbeddingGemma` → … TBD by you in Step 1.**
   This handover deals with the consequence: the spec's chosen model
   isn't in the installed fastembed catalog, so Step 1 instructs you to
   switch the default to a model that actually works.

## Key reading (in priority order)

1. **`CLAUDE.md`** — project conventions, current state of all subsystems,
   the "Search subsystem" section added by Task 24. Always re-read this
   when starting a session.
2. **`docs/llm/GOLDEN_RULES.md`** — 11 rules, especially #4 (no magic
   numbers — they live in `SearchConfig`) and #6 (no truncation — Phase 1
   pagination is built around the `grow_pool` escape hatch).
3. **`docs/superpowers/specs/2026-05-16-hybrid-search-design.md`** —
   full Phase 1 (and forward-looking Phase 2-5) design. Includes the
   BM25-backend revision note (search for "BM25-backend decision").
4. **`docs/superpowers/plans/2026-05-16-hybrid-search-phase1.md`** —
   the implementation plan, 24 tasks, every line of code traceable back
   to one of them. Read the header for the architecture summary.
5. **`README.md`** — user-facing "Search (Phase 1)" section. Update this
   if you change anything user-visible.
6. **PR #1** — review comments, if any, override everything in this
   handover. Read those first if there are any.

## Things you should NOT do

- Don't merge the PR yourself — wait for the user.
- Don't force-push without explicit user request.
- Don't start Phase 2 implementation work before acceptance passes and
  Phase 2 has its own brainstorm / spec / plan.
- Don't add a new dependency, extension, or external service without
  user approval (especially `pg_search` or `ollama` — both are
  deliberately deferred).
- Don't bypass `tsvector` for direct SQL on `body_text` — use the
  `messages.fts_v2` generated column. Same for `message_chunks.fts`.
- Don't add a model that requires `halfvec(N)` for `N != 768` without a
  migration plan and user approval (the existing schema is locked at
  768d).
- Don't change Searcher's public API without bumping a version or noting
  it in the PR — the Python API is the canonical surface for downstream
  agents.

## Quick reference — useful commands

```bash
# Run full tests (default — slow test excluded)
unset VIRTUAL_ENV && uv run pytest -q -m "not slow"

# Run including slow test (downloads ~470 MB model on first run)
unset VIRTUAL_ENV && uv run pytest -q

# Run only the acceptance suite (after Step 2 done)
PYTHONPATH=src:. unset VIRTUAL_ENV && uv run python \
    tests/acceptance/run_recall_eval.py \
    --queries tests/fixtures/multilingual_queries.json --k 20

# Smoke a real search end-to-end from the CLI (requires backfill first)
unset VIRTUAL_ENV && uv run localmail embed-backfill --no-progress
unset VIRTUAL_ENV && uv run localmail search "Berlin" --verbose --format json

# Inspect failed embeddings
unset VIRTUAL_ENV && uv run localmail list-failed-embeddings --format json

# Status check (counts: messages, chunks, embedded, failed)
unset VIRTUAL_ENV && uv run localmail search-status

# Find which fastembed models are in catalog (for Step 1 verification)
unset VIRTUAL_ENV && uv run python -c "
from fastembed import TextEmbedding
import json
print(json.dumps([m.get('model') for m in TextEmbedding.list_supported_models()], indent=2))
"
```

## End of handover

If anything in this document conflicts with newer PR comments, the PR
comments win. If anything conflicts with `CLAUDE.md`, `CLAUDE.md` wins
(it's the maintained source of truth; this handover is a snapshot).

Good luck.
