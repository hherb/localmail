# Smart query rewriter (`--smart`) — design

> **Search Phase 4.** Adds an opt-in LLM query rewriter behind the existing
> `--smart` flag. Local-only (Ollama HTTP); personal mail never leaves the
> host. No new migration, no new uv extra (`httpx` is already a dependency,
> Ollama is an external service rather than a Python package).

## Goal

When a user passes `--smart`, run the free-text portion of their query through
a local LLM that produces three things:

1. **`rewritten_text`** — a cleaned, semantically richer query string that
   feeds the vector arm and the cross-encoder reranker.
2. **`expansion_terms`** — a short list of synonym/related terms OR-ed into the
   lexical (tsvector) arms to broaden recall.
3. **`extracted_filters`** — structured filters inferred from natural language
   (e.g. "last summer" → `after:`/`before:`), filling only the filter slots the
   user did not type explicitly.

Non-`--smart` search behaviour is **byte-for-byte unchanged**.

## Non-goals (this slice)

- No remote/cloud LLM. Ollama on `localhost` only (consistent with the
  privacy constraint in the hybrid-search design).
- No new Python dependency and no `[smart]` uv extra — `httpx` already ships;
  Ollama is reached over HTTP.
- The LLM does **not** infer `account`, `folder`, or `lang` filters — those are
  environment-specific identifiers the model cannot reliably know. Only
  date / substring / `has_attachment` slots are LLM-fillable.
- No schema change, no migration.

## Decisions (from brainstorming)

1. **Full three-output rewrite** (`rewritten_text` + `expansion_terms` +
   `extracted_filters`).
2. **Explicit operators win.** A user-typed operator (`after:`/`before:`/
   `from:`/`to:`/`subject:`/`has:`/`label:`) is authoritative; the LLM only
   fills filter slots the user left empty. A precise typed date is never
   overwritten by an inferred one.
3. **Graceful runtime fall-through, surfaced (no silent failure).** If the
   rewriter cannot produce a result at runtime (Ollama unreachable, timeout
   past `rewriter_timeout_s`, unparseable JSON), the search runs with the
   un-rewritten query, logs a WARNING, **and** sets a caller-visible
   `rewrite_skipped` flag on `SearchPage` so the CLI/MCP/GUI can tell the user
   the rewrite didn't take.

## Architecture

### New module: `src/localmail/search/rewriter.py`

Pure helpers plus one IO class. Target < 350 lines.

```python
@dataclass(frozen=True)
class RewriteResult:
    rewritten_text: str
    expansion_terms: list[str]
    extracted_filters: SearchFilters   # only the slots the LLM inferred

class QueryRewriter(Protocol):
    name: str
    model: str
    def rewrite(self, free_text: str) -> RewriteResult: ...

class RewriteParseError(ValueError):
    """LLM response was not valid JSON / did not match the schema."""

# --- pure (unit-tested in isolation, no IO) ---
def build_rewrite_prompt(free_text: str, *, today: date,
                         max_expansion_terms: int) -> str: ...
def parse_rewrite_response(raw: str) -> RewriteResult: ...
def apply_rewrite(parsed: ParsedQuery, result: RewriteResult) -> ParsedQuery: ...

# --- the only IO ---
class OllamaLLMRewriter:
    name = "ollama"
    def __init__(self, cfg: SearchConfig, client: httpx.Client | None = None): ...
    def rewrite(self, free_text: str) -> RewriteResult: ...
```

### Data flow

```
search(query, smart=True)
  └─ parse_query(query)                 → ParsedQuery (user operators, free_text)
  └─ if free_text.strip():              # nothing to rewrite for empty queries
       try:
         result = rewriter.rewrite(free_text)        # bounded by rewriter_timeout_s
         parsed = apply_rewrite(parsed, result)      # pure precedence merge
       except (Timeout/Connect/HTTPStatus/RewriteParseError):
         log.warning("smart rewrite skipped: …"); rewrite_skipped = True
       timing_ms["rewrite"] = elapsed                # recorded even on failure
  └─ existing branches (date-sort / empty / hybrid) consume the enriched parsed
```

The rewrite happens once, right after `parse_query`, before the
date-sort / empty-query / hybrid branches, so every downstream path sees the
enriched `parsed`. `free_text` is preserved unchanged (lexical exact-recall);
`rewritten_text` is added (vector arm + reranker already prefer
`rewritten_text or free_text`).

## The pure merge: `apply_rewrite`

Correctness-critical. Returns a **new** frozen `ParsedQuery`.

- **`rewritten_text`** ← `result.rewritten_text`. `free_text` untouched.
- **`expansion_terms`** ← `result.expansion_terms[:max_expansion_terms]`.
  These OR into the lexical arms only; they never touch `free_text`.
- **`extracted_filters`** — fill empty slots only (explicit operators win):
  - Scalar slots `after`, `before`, `from_substr`, `to_substr`,
    `subject_substr`, `has_attachment`, `label`: take the LLM value **iff** the
    user's slot is `None`/empty.
  - List slots `account_names`, `folders`, `languages`: **never** populated by
    the LLM in v1 (see non-goals).

Exhaustively unit-tested per slot: empty→filled, occupied→preserved,
LLM-empty→unchanged.

## `OllamaLLMRewriter` (the only IO)

- `POST {cfg.ollama_host}/api/generate`, body:
  `{model: cfg.rewriter_model, prompt, stream: false, format: <JSON schema>,
    options: {temperature: 0}}`.
  Ollama's `format` (JSON schema) constrains the model to valid JSON;
  `parse_rewrite_response` still pydantic-validates defensively.
- `temperature: 0` → deterministic / reproducible.
- Per-request timeout = `cfg.rewriter_timeout_s`.
- `raise_for_status()` — a 4xx (model not pulled, bad request) or 5xx raises;
  the searcher treats it as a fall-through.
- The class **raises typed exceptions**; it does not swallow errors. The
  searcher owns the degradation policy in one place (testable both directions).

### Prompt

`build_rewrite_prompt(free_text, *, today, max_expansion_terms)` — pure. A fixed
instruction block + the injected `today` date (so "last summer" grounds
correctly) + the user's free text, requesting:

```json
{"rewritten_text": "...",
 "expansion_terms": ["...", "..."],
 "filters": {"after": "YYYY-MM-DD"|null, "before": "YYYY-MM-DD"|null,
             "from": "..."|null, "to": "..."|null, "subject": "..."|null,
             "has_attachment": true|false|null}}
```

`today` is **injected** (not `date.today()` inside the function) so the prompt
and any date grounding are deterministically testable. The LLM does the
relative-date arithmetic; Python only parses the resulting `YYYY-MM-DD`.

## Lexical arm expansion

`arm_bm25_messages` and `arm_bm25_chunks` build the tsquery via a shared pure
helper:

```python
def build_lexical_tsquery(free_text: str,
                          expansion_terms: list[str]) -> tuple[str, list[str]]:
    """Return (sql_fragment, params) for
    plainto_tsquery('simple', free_text) [ || plainto_tsquery('simple', term) ]*
    With no expansion terms, returns exactly the current single-tsquery form."""
```

- Zero expansion terms ⇒ byte-identical to today's
  `plainto_tsquery('simple', %s)` SQL + params. The non-smart path is provably
  unchanged (pinned by a test).
- Each expansion term adds one `|| plainto_tsquery('simple', %s)` OR-clause,
  broadening recall while preserving the original AND-ed phrase match.

## Factory & wire surface

- **`create_searcher`** gains `rewriter=_UNSET` (mirrors `embeddings`/
  `reranker`): `_UNSET` + `cfg.search.rewriter_enabled_by_default` →
  `OllamaLLMRewriter(cfg.search)`; `_UNSET` + disabled → `None`; explicit value
  overrides (tests inject a fake).
- **`SearchPage`** gains `rewrite_skipped: bool = False` (defaulted →
  every existing construction stays valid). `timing_ms["rewrite"]` is recorded
  whenever the smart path runs (success or fall-through).
- **`search/__init__.py`** exports `QueryRewriter`, `RewriteResult`,
  `OllamaLLMRewriter`.
- The existing config-time `RuntimeError("--smart requires a configured
  rewriter")` stays: asking for `--smart` with no rewriter configured is a
  config error, distinct from a runtime Ollama blip.
- **CLI**: the `--smart` path prints a one-line notice when `rewrite_skipped`.

## Config (`SearchConfig`)

Existing fields reused: `rewriter_enabled_by_default`, `rewriter_backend`
(`"ollama"`), `rewriter_model` (`"qwen2.5:3b"`), `rewriter_timeout_s` (10.0),
`ollama_host`. One new field:

- `rewriter_max_expansion_terms: int` — cap on expansion terms merged into the
  lexical arms (no magic number at the call site).

## Testing

- **Pure unit** (no network, no DB):
  - `build_rewrite_prompt` — injected `today` appears; deterministic.
  - `parse_rewrite_response` — valid / invalid / partial JSON; `RewriteParseError`.
  - `apply_rewrite` — every slot empty→filled, occupied→preserved,
    LLM-empty→unchanged; expansion-term cap.
  - `build_lexical_tsquery` — 0 / 1 / N expansions; **0 ⇒ identity** with the
    current arm SQL/params.
- **`OllamaLLMRewriter` with `httpx.MockTransport`** (no real Ollama): happy
  path, timeout→raises, ConnectError→raises, 4xx→raises, bad-JSON→
  `RewriteParseError`.
- **Searcher integration** with a `FakeRewriter`: `smart=True` enriches
  `parsed` (rewritten_text + expansion + merged filters), `timing_ms["rewrite"]`
  present; a `RaisingFakeRewriter` ⇒ `rewrite_skipped=True` + original results +
  WARNING logged.
- **Arms DB test**: expansion terms retrieve a message matching only a synonym;
  `expansion_terms=[]` returns the same rows as the non-smart query.

## Out of scope / deferred

- Cloud LLM backends (the `rewriter_backend` Literal stays `"ollama"`).
- LLM-inferred account/folder/lang filters.
- Caching rewrite results across queries.
- Streaming the LLM response (single non-streamed call is bounded by the
  timeout and small).
