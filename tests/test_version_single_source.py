# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""One version literal per ecosystem, and they must all agree.

`pyproject.toml` holds the only version literal in the Python tree —
`localmail.__version__` reads it back from the installed distribution metadata
and `/v1/version` reports that attribute, so neither can drift from it. Cargo
and npm each require a literal in their own manifest and no configuration
collapses those into one; `tauri.conf.json` is the one GUI duplicate that
*can* be dropped, and is. These tests are the guard on what remains.

The lockfiles need no guard here: CI runs `uv sync --frozen`, `npm ci`, and
`cargo --locked`, each of which already fails on a manifest it disagrees with.
"""
from __future__ import annotations

import json
import tomllib
from importlib.metadata import version as package_version
from pathlib import Path

import localmail
from localmail.serve.routes.version import SERVER_VERSION

REPO_ROOT = Path(__file__).resolve().parents[1]


def _pyproject_version() -> str:
    with (REPO_ROOT / "pyproject.toml").open("rb") as fh:
        version = tomllib.load(fh)["project"]["version"]
    assert isinstance(version, str)
    return version


def _json_at(relative: str) -> dict:
    return json.loads((REPO_ROOT / relative).read_text())


def test_package_version_matches_pyproject() -> None:
    assert localmail.__version__ == _pyproject_version()


def test_package_version_is_read_from_distribution_metadata() -> None:
    """Pins the derivation itself: reintroducing a literal in `__init__.py`
    fails here the moment it disagrees with what was installed."""
    assert localmail.__version__ == package_version("localmail")


def test_serve_reports_the_package_version() -> None:
    """`/v1/version` must go through the package attribute rather than
    repeating the `importlib.metadata` lookup it used to own."""
    assert SERVER_VERSION == localmail.__version__


def test_cargo_manifest_matches_pyproject() -> None:
    with (REPO_ROOT / "gui/src-tauri/Cargo.toml").open("rb") as fh:
        cargo = tomllib.load(fh)
    assert cargo["package"]["version"] == _pyproject_version()


def test_npm_manifest_matches_pyproject() -> None:
    assert _json_at("gui/package.json")["version"] == _pyproject_version()


def test_tauri_config_inherits_its_version_from_cargo() -> None:
    """Tauri uses `Cargo.toml`'s version when the config omits the key, so the
    key stays absent — setting it back reinstates a duplicate that nothing
    would catch, since it feeds only the bundled app's metadata."""
    assert "version" not in _json_at("gui/src-tauri/tauri.conf.json")
