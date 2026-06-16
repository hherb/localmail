"""Tests for the pluggable rewriter backends + factory."""
from __future__ import annotations

import json
from datetime import date as _date

import httpx
import pytest

from localmail.config import SearchConfig
from localmail.search.rewriter import RewriteParseError
from localmail.search.rewriter_backends import (
    AnthropicRewriter,
    MissingApiKey,
    OllamaLLMRewriter,
    OpenAICompatRewriter,
    build_rewriter,
)


def test_ollama_backcompat_import_path_still_works():
    # External callers may import the deep path; PEP 562 __getattr__ keeps it.
    from localmail.search.rewriter import OllamaLLMRewriter as FromRewriter
    from localmail.search.rewriter_backends import OllamaLLMRewriter as FromBackends

    assert FromRewriter is FromBackends


def test_package_level_export_still_works():
    from localmail.search import OllamaLLMRewriter  # noqa: F401


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
    assert seen["body"]["messages"][0]["role"] == "user"
    assert len(seen["body"]["messages"]) == 1
    assert seen["body"]["model"] == cfg.rewriter_model


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


def test_anthropic_happy_path(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    seen = {}

    def handler(request):
        seen["path"] = request.url.path
        seen["x_api_key"] = request.headers.get("x-api-key")
        seen["version"] = request.headers.get("anthropic-version")
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
    assert seen["body"]["model"] == cfg.rewriter_model
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


def test_all_backends_reexported_via_rewriter_module():
    # PEP 562 __getattr__ keeps the deep import path working for every backend.
    import localmail.search.rewriter as r
    import localmail.search.rewriter_backends as b
    for name in ("OllamaLLMRewriter", "OpenAICompatRewriter", "AnthropicRewriter", "build_rewriter", "MissingApiKey"):
        assert getattr(r, name) is getattr(b, name)
