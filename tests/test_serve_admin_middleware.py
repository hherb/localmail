"""Access-log scrubber rewrites query string for the access log only."""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from localmail.serve.admin.middleware import (
    ScrubSensitiveQueryParamsMiddleware,
    get_unscrubbed_query_params,
)


def _build_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(
        ScrubSensitiveQueryParamsMiddleware,
        sensitive=("code", "state", "password"),
    )

    @app.get("/echo")
    async def echo(request: Request):
        return {
            "scrubbed_query": request.url.query,
            "unscrubbed": dict(get_unscrubbed_query_params(request)),
        }

    return app


def test_sensitive_params_scrubbed_in_url() -> None:
    client = TestClient(_build_app())
    r = client.get("/echo?code=secretcode&state=secretstate&keep=visible")
    j = r.json()
    assert "code=secretcode" not in j["scrubbed_query"]
    assert "state=secretstate" not in j["scrubbed_query"]
    assert "code=REDACTED" in j["scrubbed_query"]
    assert "state=REDACTED" in j["scrubbed_query"]
    assert "keep=visible" in j["scrubbed_query"]


def test_handler_still_sees_unscrubbed() -> None:
    client = TestClient(_build_app())
    r = client.get("/echo?code=secretcode&keep=visible")
    j = r.json()
    assert j["unscrubbed"] == {"code": "secretcode", "keep": "visible"}


def test_no_query_string_is_no_op() -> None:
    client = TestClient(_build_app())
    r = client.get("/echo")
    j = r.json()
    assert j["scrubbed_query"] == ""
    assert j["unscrubbed"] == {}


def test_non_sensitive_params_untouched() -> None:
    client = TestClient(_build_app())
    r = client.get("/echo?account=horst&limit=10")
    j = r.json()
    assert "account=horst" in j["scrubbed_query"]
    assert "limit=10" in j["scrubbed_query"]
