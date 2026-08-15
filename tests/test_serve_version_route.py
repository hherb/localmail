# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""The /v1/version wire contract (#278, #300).

`build_hash` alone cannot say why it is absent, and "why is this value absent"
is exactly #300's question about `server_version`. Both are answered by a
declared source string rather than by a bare null.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import localmail.serve.routes.version as version_route
from localmail.build_report import BuildInfo, BuildSource
from localmail.serve.app import create_app
from localmail.version_report import VersionSource


def _client(db_dsn: str) -> TestClient:
    # Same call as tests/test_serve_app_baseline.py — `create_app` is
    # keyword-only and takes `db_dsn`, not `dsn`.
    return TestClient(create_app(db_dsn=db_dsn, searcher=None))


def test_the_six_keys_are_present(db_dsn: str) -> None:
    body = _client(db_dsn).get("/v1/version").json()
    assert set(body) == {
        "api_major", "api_minor", "server_version",
        "build_hash", "build_source", "version_source",
    }


def test_the_source_fields_are_never_null(db_dsn: str) -> None:
    """Only `build_hash` is nullable. A client that cannot explain an absent
    hash is the state this design exists to end."""
    body = _client(db_dsn).get("/v1/version").json()
    assert isinstance(body["build_source"], str) and body["build_source"]
    assert isinstance(body["version_source"], str) and body["version_source"]


def test_an_identified_build_reports_its_hash(
    db_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        version_route, "resolve_build_info",
        lambda: BuildInfo(build_hash="eec8e09-dirty", source=BuildSource.GIT_CHECKOUT),
    )
    body = _client(db_dsn).get("/v1/version").json()
    assert body["build_hash"] == "eec8e09-dirty"
    assert body["build_source"] == "git_checkout"


def test_an_unidentified_build_names_the_reason_rather_than_only_nulling(
    db_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        version_route, "resolve_build_info",
        lambda: BuildInfo(build_hash=None, source=BuildSource.NOT_A_REPO),
    )
    body = _client(db_dsn).get("/v1/version").json()
    assert body["build_hash"] is None
    assert body["build_source"] == "not_a_repo"


def test_an_unresolvable_version_is_flagged_on_the_wire(
    db_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#300: the sentinel used to ship unflagged, and the GUI rendered it."""
    monkeypatch.setattr(
        version_route, "VERSION_SOURCE", VersionSource.METADATA_UNREADABLE
    )
    body = _client(db_dsn).get("/v1/version").json()
    assert body["version_source"] == "metadata_unreadable"


def test_the_route_never_reads_the_human_diagnostic(db_dsn: str) -> None:
    """The endpoint is unauthenticated and the diagnostic embeds rendered
    exception text — errno values and filesystem paths since #303.

    **An AST pin, because a value assertion here cannot work.** The obvious
    test — monkeypatch the source, assert `"cause:" not in body` — is vacuous:
    the route emits no diagnostic and `__version_diagnostic__` is `None` in any
    healthy test process, so there is no text to leak and the assertion cannot
    fail. Adding the real global to the body survived it. The AST is also what
    keeps this from breaking when the reason is written down beside the code:
    the docstring above necessarily names the thing it forbids, which is the
    #291 lesson `_mentions_version_option` already paid for.
    """
    import ast
    from pathlib import Path

    source = Path(version_route.__file__).read_text()
    referenced = {
        node.attr if isinstance(node, ast.Attribute) else node.id
        for node in ast.walk(ast.parse(source))
        if isinstance(node, (ast.Name, ast.Attribute))
    }

    assert "__version_diagnostic__" not in referenced
    # Positive control: the walk really does see this module's identifiers, so
    # a rename of the collector cannot make the assertion above pass vacuously.
    assert "resolve_build_info" in referenced
