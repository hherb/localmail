# Pluggable rewriter backends Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `SearchConfig.rewriter_backend` dispatch the `--smart` query rewriter to one of three HTTP backends — the existing Ollama, a new OpenAI-compatible one, and a new Anthropic one — without new SDK dependencies or a migration.

**Architecture:** A template-method base `_HttpJsonRewriter` owns the shared `rewrite()` flow (`build_rewrite_prompt` → `_request(prompt)` → `parse_rewrite_response`); each concrete backend implements only `_request`. A `build_rewriter(cfg)` factory dispatches on `rewriter_backend`. Pure helpers stay in `search/rewriter.py`; the IO backends + factory live in a new `search/rewriter_backends.py`. Cloud backends read their API key from a configurable env var at construction; a missing key raises `MissingApiKey`, which `create_searcher`'s existing guard turns into graceful "no `--smart`".

**Tech Stack:** Python 3.12, `httpx` (already a dep), `pydantic` v2, `pytest` with `httpx.MockTransport`.

Spec: `docs/superpowers/specs/2026-06-16-rewriter-backend-abstraction-design.md`

---

## File structure

- **Modify `src/localmail/config.py`** (`SearchConfig`, lines 350-360) — widen the `rewriter_backend` Literal + add per-backend fields.
- **Modify `src/localmail/search/rewriter.py`** — stays pure: rename `_OLLAMA_FORMAT_SCHEMA` → public `OLLAMA_FORMAT_SCHEMA`, delete the `OllamaLLMRewriter` class (moves to the new module), add a PEP 562 module `__getattr__` for back-compat of the deep import path.
- **Create `src/localmail/search/rewriter_backends.py`** — `_HttpJsonRewriter` base, `OllamaLLMRewriter`, `OpenAICompatRewriter`, `AnthropicRewriter`, `build_rewriter`, `MissingApiKey`, `_require_env`.
- **Modify `src/localmail/search/__init__.py`** — import the backend classes from `rewriter_backends`; export `build_rewriter`, `OpenAICompatRewriter`, `AnthropicRewriter`, `MissingApiKey`; wire `build_rewriter` into `create_searcher`.
- **Modify `tests/test_rewriter.py`** — update the `OllamaLLMRewriter` import source; keep the existing Ollama regression tests.
- **Create `tests/test_rewriter_backends.py`** — new-backend + factory + back-compat tests.
- **Docs:** `CLAUDE.md`, `README.md`, `config.example.toml`.

---

## Task 1: Config — widen backend Literal + add per-backend fields

**Files:**
- Modify: `src/localmail/config.py:352` and insert after `:360`
- Test: `tests/test_rewriter.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_rewriter.py` (near the existing `test_default_rewriter_model`, ~line 158):

```python
def test_rewriter_backend_literal_accepts_three_values():
    for backend in ("ollama", "openai", "anthropic"):
        assert SearchConfig(rewriter_backend=backend).rewriter_backend == backend


def test_rewriter_backend_rejects_unknown():
    import pytest
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        SearchConfig(rewriter_backend="cohere")


def test_rewriter_backend_defaults():
    cfg = SearchConfig()
    assert cfg.rewriter_backend == "ollama"
    assert cfg.rewriter_max_tokens == 1024
    assert cfg.rewriter_openai_base_url == "https://api.openai.com/v1"
    assert cfg.rewriter_openai_api_key_env == "OPENAI_API_KEY"
    assert cfg.rewriter_anthropic_base_url == "https://api.anthropic.com"
    assert cfg.rewriter_anthropic_api_key_env == "ANTHROPIC_API_KEY"
    assert cfg.rewriter_anthropic_version == "2023-06-01"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_rewriter.py -k "rewriter_backend" -q`
Expected: FAIL — `openai`/`anthropic` rejected by the current `Literal["ollama"]`; new attributes missing.

- [ ] **Step 3: Edit the config**

In `src/localmail/config.py`, replace line 352:

```python
    rewriter_backend: Literal["ollama"] = "ollama"
```

with:

```python
    rewriter_backend: Literal["ollama", "openai", "anthropic"] = "ollama"
```

Then, immediately after the `rewriter_max_expansion_terms` line (currently `:355`), insert:

```python
    # Shared cap on the rewriter's generated tokens. Anthropic's Messages API
    # requires max_tokens; OpenAI treats it as optional. The rewrite output is
    # a small JSON object, so the default is generous but bounded.
    rewriter_max_tokens: int = 1024
    # OpenAI-compatible backend (rewriter_backend = "openai"). base_url covers
    # OpenAI, Azure, OpenRouter, Together, Groq, vLLM, LM Studio,
    # llama.cpp-server, and Ollama's own /v1 endpoint. The API key is read at
    # construction from the named environment variable (never config/DB).
    rewriter_openai_base_url: str = "https://api.openai.com/v1"
    rewriter_openai_api_key_env: str = "OPENAI_API_KEY"
    # Anthropic backend (rewriter_backend = "anthropic"). The version string is
    # the anthropic-version request header.
    rewriter_anthropic_base_url: str = "https://api.anthropic.com"
    rewriter_anthropic_api_key_env: str = "ANTHROPIC_API_KEY"
    rewriter_anthropic_version: str = "2023-06-01"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_rewriter.py -k "rewriter_backend" -q`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add src/localmail/config.py tests/test_rewriter.py
git commit -m "feat(search): widen rewriter_backend + add OpenAI/Anthropic config fields"
```

---

## Task 2: Extract `_HttpJsonRewriter` base; move Ollama into `rewriter_backends.py`

This is a refactor: behaviour of the Ollama backend must not change. The existing `tests/test_rewriter.py` Ollama tests are the regression guard.

**Files:**
- Create: `src/localmail/search/rewriter_backends.py`
- Modify: `src/localmail/search/rewriter.py` (rename schema, delete `OllamaLLMRewriter`, add `__getattr__`)
- Modify: `src/localmail/search/__init__.py:14`
- Modify: `tests/test_rewriter.py:9-10` (import source)
- Test: `tests/test_rewriter_backends.py` (new — back-compat assertion)

- [ ] **Step 1: Write the failing back-compat test**

Create `tests/test_rewriter_backends.py`:

```python
"""Tests for the pluggable rewriter backends + factory."""
from __future__ import annotations

from datetime import date as _date

import httpx
import pytest

from localmail.config import SearchConfig


def test_ollama_backcompat_import_path_still_works():
    # External callers may import the deep path; PEP 562 __getattr__ keeps it.
    from localmail.search.rewriter import OllamaLLMRewriter as FromRewriter
    from localmail.search.rewriter_backends import OllamaLLMRewriter as FromBackends

    assert FromRewriter is FromBackends


def test_package_level_export_still_works():
    from localmail.search import OllamaLLMRewriter  # noqa: F401
```

- [ ] **Step 2: Run test to verify it fails**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_rewriter_backends.py -q`
Expected: FAIL — `rewriter_backends` module does not exist yet.

- [ ] **Step 3: Create `rewriter_backends.py` with the base + Ollama**

Create `src/localmail/search/rewriter_backends.py`:

```python
"""HTTP rewriter backends for the ``--smart`` query rewriter.

A template-method base (:class:`_HttpJsonRewriter`) owns the shared
``rewrite()`` flow; each concrete backend implements only :meth:`_request`,
the provider-specific HTTP call. :func:`build_rewriter` dispatches on
``SearchConfig.rewriter_backend``. No third-party SDKs — every backend uses
the already-present ``httpx``.
"""

from __future__ import annotations

import os
from datetime import date
from typing import Callable

import httpx

from localmail.config import SearchConfig
from localmail.search.rewriter import (
    OLLAMA_FORMAT_SCHEMA,
    RewriteParseError,
    RewriteResult,
    build_rewrite_prompt,
    parse_rewrite_response,
)


class MissingApiKey(RewriteParseError):
    """A cloud backend's API-key environment variable is unset or empty.

    Subclasses :class:`RewriteParseError` so that, raised at construction, it
    is caught by ``create_searcher``'s rewriter guard and degrades to "no
    ``--smart``" rather than crashing startup.
    """


def _require_env(name: str) -> str:
    """Return a non-empty environment variable or raise :class:`MissingApiKey`."""
    value = os.environ.get(name, "")
    if not value:
        raise MissingApiKey(f"environment variable {name!r} is unset or empty")
    return value


class _HttpJsonRewriter:
    """Shared ``rewrite()`` orchestration for an HTTP JSON rewriter backend.

    Subclasses set the class attribute ``name`` and implement
    :meth:`_request`, which performs the provider call and returns the model's
    raw JSON text. When no ``client`` is injected the instance owns a
    long-lived :class:`httpx.Client`; :meth:`close` releases it.
    """

    name: str = ""

    def __init__(
        self,
        cfg: SearchConfig,
        *,
        client: httpx.Client | None = None,
        today_provider: Callable[[], date] = date.today,
    ) -> None:
        self._cfg = cfg
        self.model = cfg.rewriter_model
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=cfg.rewriter_timeout_s)
        self._today = today_provider

    def close(self) -> None:
        """Close the owned httpx client; a no-op for an injected client."""
        if self._owns_client:
            self._client.close()

    def rewrite(self, free_text: str) -> RewriteResult:
        prompt = build_rewrite_prompt(
            free_text,
            today=self._today(),
            max_expansion_terms=self._cfg.rewriter_max_expansion_terms,
        )
        raw = self._request(prompt)
        return parse_rewrite_response(raw)

    def _request(self, prompt: str) -> str:
        raise NotImplementedError


class OllamaLLMRewriter(_HttpJsonRewriter):
    """Rewriter backed by a local Ollama ``/api/generate`` call."""

    name = "ollama"

    def _request(self, prompt: str) -> str:
        resp = self._client.post(
            f"{self._cfg.ollama_host}/api/generate",
            json={
                "model": self.model,
                "prompt": prompt,
                "stream": False,
                "format": OLLAMA_FORMAT_SCHEMA,
                "options": {"temperature": 0},
            },
        )
        resp.raise_for_status()
        body = resp.json()
        try:
            return body["response"]
        except (KeyError, TypeError) as exc:
            raise RewriteParseError(
                f"missing 'response' key in Ollama reply: {body!r}"
            ) from exc
```

- [ ] **Step 4: Update `rewriter.py` — rename schema, delete Ollama class, add `__getattr__`**

In `src/localmail/search/rewriter.py`:

(a) Replace line 151:

```python
_OLLAMA_FORMAT_SCHEMA = _RewriteSchema.model_json_schema()
```

with:

```python
OLLAMA_FORMAT_SCHEMA = _RewriteSchema.model_json_schema()
"""JSON schema passed as Ollama's ``format`` constraint. Public so the
Ollama backend in ``rewriter_backends`` can import it."""
```

(b) Delete the entire `OllamaLLMRewriter` class (currently lines 154-211, from `class OllamaLLMRewriter:` to the end of its `rewrite` method).

(c) Append at the end of the file:

```python
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - import for type-checkers only
    from localmail.search.rewriter_backends import (  # noqa: F401
        AnthropicRewriter,
        MissingApiKey,
        OllamaLLMRewriter,
        OpenAICompatRewriter,
        build_rewriter,
    )

_BACKEND_REEXPORTS = frozenset(
    {
        "OllamaLLMRewriter",
        "OpenAICompatRewriter",
        "AnthropicRewriter",
        "build_rewriter",
        "MissingApiKey",
    }
)


def __getattr__(name: str):
    """Back-compat: re-export the backend classes from their new module.

    Lazy (PEP 562) so importing ``rewriter`` never triggers an import-time
    cycle with ``rewriter_backends`` (which imports the pure helpers above).
    """
    if name in _BACKEND_REEXPORTS:
        from localmail.search import rewriter_backends

        return getattr(rewriter_backends, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
```

- [ ] **Step 5: Update the two internal import sites**

In `src/localmail/search/__init__.py`, replace line 14:

```python
from localmail.search.rewriter import OllamaLLMRewriter, QueryRewriter, RewriteParseError, RewriteResult
```

with:

```python
from localmail.search.rewriter import QueryRewriter, RewriteParseError, RewriteResult
from localmail.search.rewriter_backends import (
    AnthropicRewriter,
    MissingApiKey,
    OllamaLLMRewriter,
    OpenAICompatRewriter,
    build_rewriter,
)
```

In `tests/test_rewriter.py`, change the import block (line 9-10) so `OllamaLLMRewriter` comes from `rewriter_backends`. The current block is:

```python
from localmail.search.rewriter import (
    OllamaLLMRewriter,
    ...
```

Move `OllamaLLMRewriter` out of that block into a new line:

```python
from localmail.search.rewriter_backends import OllamaLLMRewriter
```

(Leave the other names — `RewriteParseError`, `apply_rewrite`, etc. — importing from `rewriter`.)

- [ ] **Step 6: Run the back-compat + Ollama regression tests**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_rewriter.py tests/test_rewriter_backends.py -q`
Expected: PASS — all existing Ollama tests (`test_ollama_*`) green (behaviour unchanged) plus the 2 back-compat tests.

- [ ] **Step 7: Type-check**

Run: `unset VIRTUAL_ENV && uv run mypy src/localmail/search/rewriter.py src/localmail/search/rewriter_backends.py src/localmail/search/__init__.py`
Expected: no issues.

- [ ] **Step 8: Commit**

```bash
git add src/localmail/search/rewriter.py src/localmail/search/rewriter_backends.py \
        src/localmail/search/__init__.py tests/test_rewriter.py tests/test_rewriter_backends.py
git commit -m "refactor(search): extract _HttpJsonRewriter base; move Ollama to rewriter_backends"
```

---

## Task 3: OpenAI-compatible backend

**Files:**
- Modify: `src/localmail/search/rewriter_backends.py`
- Test: `tests/test_rewriter_backends.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_rewriter_backends.py`:

```python
from localmail.search.rewriter_backends import (
    AnthropicRewriter,
    MissingApiKey,
    OpenAICompatRewriter,
    build_rewriter,
)

_OPENAI_OK = {
    "choices": [
        {"message": {"content": '{"rewritten_text": "x", "expansion_terms": ["y"]}'}}
    ]
}


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_openai_happy_path(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    seen = {}

    def handler(request):
        seen["path"] = request.url.path
        seen["auth"] = request.headers.get("authorization")
        import json
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=_OPENAI_OK)

    cfg = SearchConfig(rewriter_backend="openai")
    r = OpenAICompatRewriter(cfg, client=_client(handler), today_provider=lambda: _date(2026, 6, 7))
    out = r.rewrite("orig")

    assert out.rewritten_text == "x"
    assert out.expansion_terms == ["y"]
    assert seen["path"] == "/v1/chat/completions"
    assert seen["auth"] == "Bearer sk-test"
    assert seen["body"]["temperature"] == 0
    assert seen["body"]["max_tokens"] == 1024
    assert seen["body"]["response_format"] == {"type": "json_object"}
    assert seen["body"]["messages"] == [{"role": "user", "content": seen["body"]["messages"][0]["content"]}]


def test_openai_missing_key_raises_at_construction(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(MissingApiKey):
        OpenAICompatRewriter(SearchConfig(rewriter_backend="openai"))


def test_openai_5xx_raises_http_error(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    def handler(request):
        return httpx.Response(500, json={"error": "boom"})

    cfg = SearchConfig(rewriter_backend="openai")
    r = OpenAICompatRewriter(cfg, client=_client(handler))
    with pytest.raises(httpx.HTTPStatusError):
        r.rewrite("orig")


def test_openai_malformed_body_raises_parse_error(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    def handler(request):
        return httpx.Response(200, json={"unexpected": True})

    cfg = SearchConfig(rewriter_backend="openai")
    r = OpenAICompatRewriter(cfg, client=_client(handler))
    with pytest.raises(RewriteParseError):
        r.rewrite("orig")
```

Add `from localmail.search.rewriter import RewriteParseError` to the test file's imports.

- [ ] **Step 2: Run tests to verify they fail**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_rewriter_backends.py -k openai -q`
Expected: FAIL — `OpenAICompatRewriter` not defined.

- [ ] **Step 3: Implement the backend**

Append to `src/localmail/search/rewriter_backends.py`:

```python
class OpenAICompatRewriter(_HttpJsonRewriter):
    """Rewriter backed by an OpenAI-compatible ``/chat/completions`` endpoint.

    Works against OpenAI, Azure, OpenRouter, Together, Groq, vLLM, LM Studio,
    llama.cpp-server, and Ollama's own ``/v1`` endpoint. ``response_format``
    requests a JSON object; non-compliant servers ignore it and the prompt's
    "Return ONLY JSON" instruction plus graceful degradation cover the gap.
    """

    name = "openai"

    def __init__(
        self,
        cfg: SearchConfig,
        *,
        client: httpx.Client | None = None,
        today_provider: Callable[[], date] = date.today,
    ) -> None:
        super().__init__(cfg, client=client, today_provider=today_provider)
        self._api_key = _require_env(cfg.rewriter_openai_api_key_env)

    def _request(self, prompt: str) -> str:
        resp = self._client.post(
            f"{self._cfg.rewriter_openai_base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self._api_key}"},
            json={
                "model": self.model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0,
                "max_tokens": self._cfg.rewriter_max_tokens,
                "response_format": {"type": "json_object"},
            },
        )
        resp.raise_for_status()
        body = resp.json()
        try:
            return body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RewriteParseError(
                f"unexpected OpenAI reply: {body!r}"
            ) from exc
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_rewriter_backends.py -k openai -q`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add src/localmail/search/rewriter_backends.py tests/test_rewriter_backends.py
git commit -m "feat(search): OpenAI-compatible rewriter backend"
```

---

## Task 4: Anthropic backend

**Files:**
- Modify: `src/localmail/search/rewriter_backends.py`
- Test: `tests/test_rewriter_backends.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_rewriter_backends.py`:

```python
def test_anthropic_happy_path(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    seen = {}

    def handler(request):
        seen["path"] = request.url.path
        seen["x_api_key"] = request.headers.get("x-api-key")
        seen["version"] = request.headers.get("anthropic-version")
        import json
        seen["body"] = json.loads(request.content)
        # The model continues from the prefilled "{"; the leading brace is the
        # assistant prefill, so the API returns the remainder of the object.
        return httpx.Response(
            200,
            json={"content": [{"type": "text", "text": '"rewritten_text": "x", "expansion_terms": ["y"]}'}]},
        )

    cfg = SearchConfig(rewriter_backend="anthropic")
    r = AnthropicRewriter(cfg, client=_client(handler), today_provider=lambda: _date(2026, 6, 7))
    out = r.rewrite("orig")

    assert out.rewritten_text == "x"
    assert out.expansion_terms == ["y"]
    assert seen["path"] == "/v1/messages"
    assert seen["x_api_key"] == "sk-ant-test"
    assert seen["version"] == "2023-06-01"
    assert seen["body"]["max_tokens"] == 1024
    assert seen["body"]["temperature"] == 0
    assert seen["body"]["messages"][-1] == {"role": "assistant", "content": "{"}


def test_anthropic_missing_key_raises_at_construction(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(MissingApiKey):
        AnthropicRewriter(SearchConfig(rewriter_backend="anthropic"))


def test_anthropic_5xx_raises_http_error(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")

    def handler(request):
        return httpx.Response(529, json={"error": "overloaded"})

    cfg = SearchConfig(rewriter_backend="anthropic")
    r = AnthropicRewriter(cfg, client=_client(handler))
    with pytest.raises(httpx.HTTPStatusError):
        r.rewrite("orig")


def test_anthropic_malformed_body_raises_parse_error(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")

    def handler(request):
        return httpx.Response(200, json={"unexpected": True})

    cfg = SearchConfig(rewriter_backend="anthropic")
    r = AnthropicRewriter(cfg, client=_client(handler))
    with pytest.raises(RewriteParseError):
        r.rewrite("orig")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_rewriter_backends.py -k anthropic -q`
Expected: FAIL — `AnthropicRewriter` not defined.

- [ ] **Step 3: Implement the backend**

Append to `src/localmail/search/rewriter_backends.py`:

```python
class AnthropicRewriter(_HttpJsonRewriter):
    """Rewriter backed by the Anthropic ``/v1/messages`` API.

    Anthropic has no JSON mode; an assistant turn prefilled with ``"{"`` forces
    the model to emit a JSON object immediately (no tool-use, no SDK). The
    prefilled brace is prepended back onto the response before parsing.
    """

    name = "anthropic"

    def __init__(
        self,
        cfg: SearchConfig,
        *,
        client: httpx.Client | None = None,
        today_provider: Callable[[], date] = date.today,
    ) -> None:
        super().__init__(cfg, client=client, today_provider=today_provider)
        self._api_key = _require_env(cfg.rewriter_anthropic_api_key_env)

    def _request(self, prompt: str) -> str:
        resp = self._client.post(
            f"{self._cfg.rewriter_anthropic_base_url}/v1/messages",
            headers={
                "x-api-key": self._api_key,
                "anthropic-version": self._cfg.rewriter_anthropic_version,
            },
            json={
                "model": self.model,
                "max_tokens": self._cfg.rewriter_max_tokens,
                "temperature": 0,
                "messages": [
                    {"role": "user", "content": prompt},
                    {"role": "assistant", "content": "{"},
                ],
            },
        )
        resp.raise_for_status()
        body = resp.json()
        try:
            text = body["content"][0]["text"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RewriteParseError(
                f"unexpected Anthropic reply: {body!r}"
            ) from exc
        return "{" + text
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_rewriter_backends.py -k anthropic -q`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add src/localmail/search/rewriter_backends.py tests/test_rewriter_backends.py
git commit -m "feat(search): Anthropic rewriter backend (prefill-forced JSON)"
```

---

## Task 5: `build_rewriter` factory + wire into `create_searcher`

**Files:**
- Modify: `src/localmail/search/rewriter_backends.py`
- Modify: `src/localmail/search/__init__.py` (`create_searcher`, line ~94; `__all__`)
- Test: `tests/test_rewriter_backends.py`

- [ ] **Step 1: Write the failing factory tests**

Add to `tests/test_rewriter_backends.py`:

```python
def test_build_rewriter_dispatch(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    assert isinstance(build_rewriter(SearchConfig(rewriter_backend="ollama")), OllamaLLMRewriter)
    assert isinstance(build_rewriter(SearchConfig(rewriter_backend="openai")), OpenAICompatRewriter)
    assert isinstance(build_rewriter(SearchConfig(rewriter_backend="anthropic")), AnthropicRewriter)


def test_build_rewriter_openai_missing_key_propagates(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(MissingApiKey):
        build_rewriter(SearchConfig(rewriter_backend="openai"))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_rewriter_backends.py -k build_rewriter -q`
Expected: FAIL — `build_rewriter` not defined.

- [ ] **Step 3: Implement the factory**

Append to `src/localmail/search/rewriter_backends.py`:

```python
def build_rewriter(
    cfg: SearchConfig,
    *,
    client: httpx.Client | None = None,
    today_provider: Callable[[], date] = date.today,
) -> _HttpJsonRewriter:
    """Construct the rewriter backend named by ``cfg.rewriter_backend``.

    The cloud backends read their API key at construction and raise
    :class:`MissingApiKey` when it is unset — ``create_searcher`` catches that
    and degrades to "no ``--smart``".
    """
    backend = cfg.rewriter_backend
    if backend == "ollama":
        return OllamaLLMRewriter(cfg, client=client, today_provider=today_provider)
    if backend == "openai":
        return OpenAICompatRewriter(cfg, client=client, today_provider=today_provider)
    if backend == "anthropic":
        return AnthropicRewriter(cfg, client=client, today_provider=today_provider)
    raise ValueError(f"unknown rewriter_backend: {backend!r}")  # Literal blocks this
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_rewriter_backends.py -k build_rewriter -q`
Expected: PASS (2 tests).

- [ ] **Step 5: Wire into `create_searcher`**

In `src/localmail/search/__init__.py`, inside `create_searcher`, replace line ~94:

```python
                rewriter = OllamaLLMRewriter(cfg.search)
```

with:

```python
                rewriter = build_rewriter(cfg.search)
```

And update the warning just below it (currently logs only `rewriter_model`) to name the backend too — replace:

```python
                logging.getLogger("localmail.search").warning(
                    "rewriter init failed (%s=%r): %s — continuing without --smart",
                    "rewriter_model", cfg.search.rewriter_model, exc,
                )
```

with:

```python
                logging.getLogger("localmail.search").warning(
                    "rewriter init failed (backend=%r model=%r): %s — continuing without --smart",
                    cfg.search.rewriter_backend, cfg.search.rewriter_model, exc,
                )
```

Then add the new public names to `__all__` (the list ending at line ~28). Insert after `"OllamaLLMRewriter",`:

```python
    "OpenAICompatRewriter",
    "AnthropicRewriter",
    "build_rewriter",
    "MissingApiKey",
```

- [ ] **Step 6: Verify the whole rewriter suite + types**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_rewriter.py tests/test_rewriter_backends.py tests/test_searcher_smart.py tests/test_rewrite_cache.py -q`
Expected: PASS.

Run: `unset VIRTUAL_ENV && uv run mypy src/localmail`
Expected: `Success: no issues found`.

- [ ] **Step 7: Commit**

```bash
git add src/localmail/search/rewriter_backends.py src/localmail/search/__init__.py tests/test_rewriter_backends.py
git commit -m "feat(search): build_rewriter factory + dispatch in create_searcher"
```

---

## Task 6: Documentation

**Files:**
- Modify: `CLAUDE.md`, `README.md`, `config.example.toml`

- [ ] **Step 1: `config.example.toml`**

After the `rewriter_cache_*` lines (currently `:147-149`), add a commented block:

```toml
# Rewriter backend. "ollama" (default, local) | "openai" | "anthropic".
# The cloud backends read their API key from an environment variable named
# below (never stored in this file or the DB) and use httpx directly — no SDK.
# rewriter_backend = "ollama"
# rewriter_model = "granite4.1:3b-q8_0"   # set to match the chosen backend
# rewriter_max_tokens = 1024
# OpenAI-compatible (OpenAI, Azure, OpenRouter, Together, Groq, vLLM, LM Studio,
# llama.cpp-server, Ollama /v1):
# rewriter_openai_base_url = "https://api.openai.com/v1"
# rewriter_openai_api_key_env = "OPENAI_API_KEY"
# Anthropic:
# rewriter_anthropic_base_url = "https://api.anthropic.com"
# rewriter_anthropic_api_key_env = "ANTHROPIC_API_KEY"
# rewriter_anthropic_version = "2023-06-01"
```

- [ ] **Step 2: `CLAUDE.md`**

In the Phase 4 `--smart` paragraph, change the description of the rewriter IO so it reads (replace the "plus one IO class `OllamaLLMRewriter`" clause):

> [search/rewriter.py](src/localmail/search/rewriter.py) is pure helpers
> (`build_rewrite_prompt`, `parse_rewrite_response`, `apply_rewrite`); the IO
> backends live in [search/rewriter_backends.py](src/localmail/search/rewriter_backends.py)
> — a template-method base `_HttpJsonRewriter` + three `httpx`-only backends
> (`OllamaLLMRewriter`, `OpenAICompatRewriter`, `AnthropicRewriter`) selected by
> `search.rewriter_backend` (`ollama` default | `openai` | `anthropic`) via
> `build_rewriter(cfg)`. Cloud backends read their API key from a configurable
> env var at construction; a missing key raises `MissingApiKey` and
> `create_searcher` degrades to "no `--smart`". No new uv extra (`httpx` is
> already a dep).

Update the `migrations/` Layout note only if needed (no migration here — leave as-is).

- [ ] **Step 3: `README.md`**

Find the `--smart` / rewriter section (search for "smart" or "Ollama"). Add a sentence listing the three backends and the env-var credential model, e.g.:

> The `--smart` rewriter defaults to a local Ollama model. Set
> `[search] rewriter_backend = "openai"` (any OpenAI-compatible endpoint) or
> `"anthropic"` to use a cloud model instead; the API key is read from the
> environment variable named by `rewriter_openai_api_key_env` /
> `rewriter_anthropic_api_key_env` (default `OPENAI_API_KEY` /
> `ANTHROPIC_API_KEY`) and is never written to config or the database.

- [ ] **Step 4: Verify docs reference real symbols**

Run: `grep -n "rewriter_backends\|build_rewriter\|OpenAICompatRewriter\|AnthropicRewriter" CLAUDE.md README.md config.example.toml`
Expected: matches in each modified file.

- [ ] **Step 5: Commit**

```bash
git add CLAUDE.md README.md config.example.toml
git commit -m "docs: pluggable rewriter backends (OpenAI-compat + Anthropic)"
```

---

## Final verification

- [ ] **Full suite + types**

Run:
```bash
unset VIRTUAL_ENV && uv run --extra mcp pytest -q tests/ --deselect tests/test_daemon_control_socket.py
unset VIRTUAL_ENV && uv run mypy src/localmail
```
Expected: full suite passes (baseline 1638 + the new backend/config/back-compat tests, ~+18); mypy clean.

- [ ] **No leftover deep-path imports of the moved class**

Run: `grep -rn "from localmail.search.rewriter import" src/ tests/ | grep OllamaLLMRewriter`
Expected: no matches (all moved to `rewriter_backends`, except the back-compat test which uses `import ... as`).
```
```
```

---

## Self-review notes

- **Spec coverage:** config widening (T1) ✓; base + factory (T2, T5) ✓; OpenAI backend (T3) ✓; Anthropic prefill (T4) ✓; env-var creds + `MissingApiKey` at construction (T3/T4) ✓; `create_searcher` one-line swap (T5) ✓; file split + back-compat re-export (T2) ✓; docs (T6) ✓; no migration / no dep ✓.
- **Known-limitation** (Anthropic JSON) is covered by the malformed-body → `RewriteParseError` test (T4) which proves graceful degradation.
- **Type consistency:** `build_rewriter`, `_HttpJsonRewriter`, `_require_env`, `MissingApiKey`, `OLLAMA_FORMAT_SCHEMA` are spelled identically across tasks.
