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


def test_the_diagnostic_text_never_reaches_the_unauthenticated_body(
    db_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The endpoint is unauthenticated and the diagnostic embeds rendered
    exception text — errno values and filesystem paths since #303.

    Asserted against the module's own constants with a positive control beside
    them: `"cause:" not in body` cannot fail once the prefix is renamed.
    """
    from localmail.version_report import _CAUSE_PREFIX, _SEVERITY_PREFIX

    monkeypatch.setattr(
        version_route, "VERSION_SOURCE", VersionSource.METADATA_UNREADABLE
    )
    raw = _client(db_dsn).get("/v1/version").text

    assert _CAUSE_PREFIX not in raw
    assert _SEVERITY_PREFIX not in raw
    # Positive control: the source that would carry them IS what we are reporting.
    assert "metadata_unreadable" in raw
