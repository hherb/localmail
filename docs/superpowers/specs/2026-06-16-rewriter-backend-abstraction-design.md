# Pluggable `--smart` rewriter backends (OpenAI-compat + Anthropic)

> **Status:** design approved 2026-06-16. Closes the NEXT_SESSION §2b
> "rewriter-backend follow-up" — `rewriter_backend` is no longer hard-`"ollama"`.

## Problem

The opt-in `--smart` query rewriter (Phase 4) only speaks to a local Ollama
server. `SearchConfig.rewriter_backend` is already a `Literal["ollama"]` — a
half-built seam with exactly one legal value — and `create_searcher` hard-wires
`OllamaLLMRewriter(cfg.search)`. Operators who want a cloud (or other
non-Ollama) rewriter have no path.

This design makes `rewriter_backend` dispatch to one of three HTTP backends:
the existing **Ollama**, a new **OpenAI-compatible** backend (covers OpenAI,
Azure, OpenRouter, Together, Groq, vLLM, LM Studio, llama.cpp-server, and
Ollama's own OpenAI-compat endpoint), and a new **Anthropic** backend.

**Non-goals (YAGNI):** no SDK dependencies (both cloud backends use the existing
`httpx`, like `OllamaLLMRewriter`); no per-backend model matrix (one
`rewriter_model` field, set to match the chosen backend); no streaming; no
keyring/CLI for keys (env-var only); no new migration.

## Approach: template-method base + factory

A small `_HttpJsonRewriter` base implements the shared `rewrite()` flow, which
is identical across providers:

```
build_rewrite_prompt(free_text, today, max_expansion_terms)   # pure, unchanged
  -> raw = self._request(prompt)                              # the ONLY per-provider code
  -> parse_rewrite_response(raw)                              # pure, unchanged
```

Each concrete backend implements only `_request(prompt) -> str` — the
provider-specific URL, auth header, request body, and response-key path. A
`build_rewriter(cfg)` factory dispatches on `rewriter_backend`.

**Rejected alternatives:**
- *Independent classes, no shared base* — duplicates prompt-build + parse +
  error handling three times.
- *One class branching on a dialect enum* — each provider can't be tested in
  isolation; the branch grows with every provider.

## File structure

`search/rewriter.py` is currently 212 lines and documented as "pure helpers
plus a single IO class". Three IO classes would bloat it past that character, so
we split:

- **`search/rewriter.py`** — stays pure: `build_rewrite_prompt`,
  `parse_rewrite_response`, `apply_rewrite`, `_fill`, `RewriteResult`,
  `RewriteParseError`, the `QueryRewriter` Protocol, and the shared
  `_RewriteSchema` / `_OLLAMA_FORMAT_SCHEMA`. Re-exports the backend classes +
  `build_rewriter` for back-compat so `localmail.search.OllamaLLMRewriter` and
  `from localmail.search.rewriter import OllamaLLMRewriter` keep working.
- **`search/rewriter_backends.py`** (new) — `_HttpJsonRewriter` base,
  `OllamaLLMRewriter`, `OpenAICompatRewriter`, `AnthropicRewriter`,
  `build_rewriter(cfg)`, and the `MissingApiKey` error.

`search/__init__.py` re-export list is unchanged in spelling
(`OllamaLLMRewriter`, `QueryRewriter`, `RewriteParseError`, `RewriteResult`)
and gains `build_rewriter`, `OpenAICompatRewriter`, `AnthropicRewriter`,
`MissingApiKey`.

## Config (`SearchConfig`, `localmail.config`)

Widen the existing Literal and add per-backend knobs. All defaulted; the Ollama
path is byte-for-byte unchanged when `rewriter_backend == "ollama"`. Reuse the
existing `rewriter_model`, `rewriter_timeout_s`, `rewriter_max_expansion_terms`.

```python
rewriter_backend: Literal["ollama", "openai", "anthropic"] = "ollama"  # widened
rewriter_max_tokens: int = 1024            # shared; required by Anthropic, optional for OpenAI
rewriter_openai_base_url: str = "https://api.openai.com/v1"
rewriter_openai_api_key_env: str = "OPENAI_API_KEY"
rewriter_anthropic_base_url: str = "https://api.anthropic.com"
rewriter_anthropic_api_key_env: str = "ANTHROPIC_API_KEY"
rewriter_anthropic_version: str = "2023-06-01"   # anthropic-version header
```

No magic numbers leak into backend code: every URL, env-var name, version
string, timeout, and token cap is a config field.

## Backend `_request` shapes

All three set `temperature: 0` for determinism and reuse the long-lived
`httpx.Client` ownership model already in `OllamaLLMRewriter` (owns the client
when none is injected; `close()` releases it; an injected client is left for the
caller). Response-key extraction failures raise `RewriteParseError` (exactly
like Ollama's current missing-`response` handling).

### Ollama (unchanged behaviour, moved file)
```
POST {ollama_host}/api/generate
body: {model, prompt, stream: false, format: <_OLLAMA_FORMAT_SCHEMA>,
       options: {temperature: 0}}
extract: body["response"]
```

### OpenAI-compatible
```
POST {rewriter_openai_base_url}/chat/completions
headers: Authorization: Bearer <key>
body: {model, messages: [{role: "user", content: prompt}],
       temperature: 0, max_tokens: <rewriter_max_tokens>,
       response_format: {"type": "json_object"}}
extract: body["choices"][0]["message"]["content"]
```
`response_format: json_object` guarantees a valid JSON object on compliant
servers; non-compliant local servers ignore it harmlessly (the prompt still says
"Return ONLY JSON", and any stray output degrades gracefully — see below).

### Anthropic
```
POST {rewriter_anthropic_base_url}/v1/messages
headers: x-api-key: <key>, anthropic-version: <rewriter_anthropic_version>
body: {model, max_tokens: <rewriter_max_tokens>, temperature: 0,
       messages: [{role: "user", content: prompt},
                  {role: "assistant", content: "{"}]}
extract: "{" + body["content"][0]["text"]
```
The **assistant prefill of `"{"`** forces an immediate JSON object without
tool-use (dep-free); `_request` reconstructs the full document by prepending the
prefilled `"{"` before parsing.

## Credentials & failure policy

- The cloud backends read their API key **at construction** from the configured
  env var (`os.environ[cfg.rewriter_openai_api_key_env]` etc.). A missing or
  empty value raises **`MissingApiKey`** (a `RewriteParseError` subclass).
- `create_searcher` already wraps rewriter construction in
  `try/except Exception` → logs "rewriter init failed … continuing without
  --smart" and sets `rewriter = None`. So a misconfigured key degrades cleanly
  to `smart_available == False`; the wire/MCP layer reports the **existing**
  `unavailable` / `not_configured` status. No new Searcher or
  `rewrite_status` code path is introduced.
- The one-line change in `create_searcher`: `OllamaLLMRewriter(cfg.search)` →
  `build_rewriter(cfg.search)`, inside the existing guard. `CachingRewriter`
  still decorates the result.
- Per-request contract is unchanged: every backend raises `httpx.HTTPError`
  subclasses (timeout/connect/status) and `RewriteParseError`. `Searcher.search`
  already catches `(httpx.HTTPError, RewriteParseError)`, so its graceful
  degradation and `classify_rewrite_failure` mapping need no change.

## Testing (TDD), no migration / no new dependency

- **Factory:** `build_rewriter` returns the right class per backend; unknown
  backend is unreachable (pydantic Literal rejects it at config load); cloud
  backends raise `MissingApiKey` when the env var is unset/empty.
- **Per-backend `_request`** against `httpx.MockTransport` (injected client):
  asserts the request URL, auth header, and body shape, and that a canned
  provider response extracts into the expected `RewriteResult`. HTTP 500 →
  `httpx.HTTPStatusError`; a body missing the response key → `RewriteParseError`.
- **Anthropic prefill** reconstruction: a `content[0].text` of
  `"...}"` parses as the full `{...}` object.
- **Back-compat:** `localmail.search.OllamaLLMRewriter` and
  `from localmail.search.rewriter import OllamaLLMRewriter` still import; the
  Ollama request body is unchanged (regression-pinned).
- **Config:** defaults present, Literal widened to the three values.
- All API-key reads in tests go through `monkeypatch.setenv` — no real keys.

## Known limitation

Anthropic has no hard JSON-mode guarantee. Prefill + `temperature: 0` + the
"Return ONLY JSON" prompt is reliable in practice; any stray output fails
`parse_rewrite_response` and degrades gracefully via the existing
`rewrite_skipped` / `rewrite_status="failed"` path (`unparseable`). Documented,
not defended further.

## Docs touched on implementation

- `CLAUDE.md` rewriter paragraph — note the three backends + the new
  `rewriter_backends.py` module.
- `README.md` `--smart` / config section — list the backend options + env-var
  credential model.
- `config.example.toml` — show the new fields commented.
