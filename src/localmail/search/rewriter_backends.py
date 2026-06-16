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
from typing import Callable, assert_never

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


class OpenAICompatRewriter(_HttpJsonRewriter):
    """Rewriter backed by an OpenAI-compatible ``/chat/completions`` endpoint.

    Works against any server speaking the OpenAI Chat Completions API
    (OpenAI, OpenRouter, Together, Groq, vLLM, LM Studio,
    llama.cpp-server, Ollama's own ``/v1``). ``response_format`` requests a
    JSON object; non-compliant servers ignore it and the prompt's "Return
    ONLY JSON" instruction plus graceful degradation cover the gap.
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
    assert_never(backend)
