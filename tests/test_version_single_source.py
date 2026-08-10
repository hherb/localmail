# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""One version literal per ecosystem, and they must all agree.

`pyproject.toml` holds the only version literal in the Python tree —
`localmail.__version__` reads it back from the installed distribution metadata
and `/v1/version` reports that attribute. Cargo and npm each need their own
literal in practice (neither tool can read the other's manifest, and a Tauri
bundle wants a real version), so those two are pinned here. The two duplicates
that *can* be derived are: `tauri.conf.json`'s copy, dropped in favour of
Cargo's, and the GUI About tab's client version, now injected by
`vite.config.ts` from `gui/package.json`.

What guards the lockfiles is not uniform, and the difference matters:

- `Cargo.lock` — CI's `cargo --locked` fails on a `Cargo.toml` it disagrees
  with. Free.
- `uv.lock` — guarded by CI's `uv sync --locked`. It is deliberately *not*
  pinned by a test here: `uv run` silently re-locks before pytest collects, so
  such a test would be healed before it could ever fail. (`--frozen`, which CI
  used to run, only skips the up-to-date check — it does not assert anything.)
- `gui/package-lock.json` — `npm ci` does **not** catch this: its
  package.json↔lock check covers the dependency tree, not the root `version`.
  Nothing heals it either, so it is pinned by hand below.

One consequence worth stating: a version bump means bumping `pyproject.toml`,
`gui/package.json` and `gui/src-tauri/Cargo.toml` together. A GUI-only release
is not expressible, by design.
"""
from __future__ import annotations

import importlib
import importlib.metadata
import json
import re
import tomllib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

import localmail
from localmail.cli import main
from localmail.serve.routes.version import SERVER_VERSION

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Accepts the positional and the `version=` spelling, and nothing else — a
#: bare `@click.version_option()` (which makes click look the version up
#: itself) has to fail this as surely as a hardcoded literal does. See
#: `test_cli_version_flag_is_derived_not_a_literal`.
_CLI_VERSION_OPTION_RE = re.compile(
    r"@click\.version_option\(\s*(?:version=)?__version__\b"
)


def _pyproject_version() -> str:
    with (REPO_ROOT / "pyproject.toml").open("rb") as fh:
        version = tomllib.load(fh)["project"]["version"]
    assert isinstance(version, str)
    return version


def _json_at(relative: str) -> dict:
    return json.loads((REPO_ROOT / relative).read_text())


@pytest.fixture
def reimported_localmail(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """Re-execute `localmail/__init__.py` with `importlib.metadata.version`
    stubbed, then restore it.

    `__init__.py` binds `version` with a `from … import`, so reloading the
    package re-reads whatever the stub left in place — which is what lets these
    tests observe the *derivation* rather than just comparing two values.
    """

    def _reload(fake: Any) -> Any:
        monkeypatch.setattr(importlib.metadata, "version", fake)
        return importlib.reload(localmail)

    try:
        yield _reload
    finally:
        monkeypatch.undo()
        importlib.reload(localmail)


def test_package_version_matches_pyproject() -> None:
    assert localmail.__version__ == _pyproject_version(), (
        "installed distribution metadata disagrees with pyproject.toml — if the "
        "version was just bumped, re-run `uv sync` (metadata is stamped at "
        "install time, so a stale editable install reports the old value)"
    )


def test_version_is_derived_not_a_literal(reimported_localmail: Any) -> None:
    """Pins the derivation, not the value.

    A hardcoded literal that happens to match the installed distribution — the
    normal state right after a release commit — passes a plain
    `__version__ == package_version(...)` comparison, so that assertion cannot
    tell a derivation from a reintroduced literal. Feeding the lookup a
    sentinel can.
    """
    reloaded = reimported_localmail(lambda _name: "1.2.3+sentinel")
    assert reloaded.__version__ == "1.2.3+sentinel"


def test_absent_distribution_falls_back(reimported_localmail: Any) -> None:
    """A source tree that was never installed still imports."""

    def _raise(_name: str) -> str:
        raise importlib.metadata.PackageNotFoundError(_name)

    assert reimported_localmail(_raise).__version__ == "0.0.0+unknown"


def test_version_less_metadata_falls_back(reimported_localmail: Any) -> None:
    """`version()` *returns None* — it does not raise — when a dist-info exists
    but its METADATA carries no `Version:` header (a truncated or hand-edited
    install). typeshed declares it `-> str`, so mypy cannot catch this; without
    the `or` guard `__version__` becomes None, `/v1/version` emits
    `"server_version": null`, and the GUI's connect probe — which decodes that
    field as a non-optional String — fails the entire trust flow with an error
    naming no field.
    """
    assert reimported_localmail(lambda _name: None).__version__ == "0.0.0+unknown"


def test_serve_reports_the_package_version() -> None:
    """Note this compares *values*: `SERVER_VERSION` is an import alias of
    `localmail.__version__`, so a route that re-derived the version identically
    would also pass. It catches a divergent literal, nothing subtler — the
    end-to-end pin is in `test_serve_app_baseline.py::test_version_unauth`.
    """
    assert SERVER_VERSION == localmail.__version__


def test_cli_version_flag_reports_the_package_version() -> None:
    """`localmail --version` is the manual's install-verification step (#279).

    Before this option existed it printed a usage error, i.e. it failed at the
    one point where a user has no way to tell a broken install from a missing
    flag. It is also the only way to read the version on a host running just
    the sync daemon — `/v1/version` needs `serve`.
    """
    result = CliRunner().invoke(main, ["--version"])
    assert result.exit_code == 0, result.output
    assert localmail.__version__ in result.output


def test_cli_version_flag_needs_no_config_or_database() -> None:
    """The flag answers "did my deploy land?", so it must answer it on a host
    where nothing else works yet — no config file written, no Postgres running.

    That rules out folding anything config- or DB-derived (a DSN, the applied
    migration revision) into this output: the one moment an operator most needs
    a version is the moment those lookups fail.

    `list-accounts` is the negative control, and it is what stops this from
    being the previous test with extra steps: it proves the pointed-nowhere
    `LOCALMAIL_CONFIG` actually bites, so `--version` surviving it is a real
    property of the flag rather than of an env var nothing reads.
    """
    env = {"LOCALMAIL_CONFIG": "./nonexistent/config.toml"}
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(main, ["--version"], env=env)
        control = runner.invoke(main, ["list-accounts"], env=env)

    assert result.exit_code == 0, result.output
    assert localmail.__version__ in result.output
    assert isinstance(control.exception, FileNotFoundError)


def test_cli_version_flag_is_derived_not_a_literal() -> None:
    """Pins the derivation at source level, for the reason
    `test_gui_client_version_is_injected_not_a_literal` does: the value test
    above cannot tell a derivation from a literal that happens to match the
    installed distribution, which is the normal state right after a release.

    Two spellings are rejected, not one. A hardcoded string is the obvious
    regression. The subtler one is a bare `@click.version_option()`, which
    looks equivalent — click then reads the distribution metadata itself — but
    adds a second, independent lookup that disagrees with `__version__` in
    exactly the case the `or`/`except` guards in `localmail/__init__.py` exist
    for: on a tree that was never installed click raises `RuntimeError` where
    every other reader degrades to `0.0.0+unknown`.
    """
    src = (REPO_ROOT / "src/localmail/cli.py").read_text()
    assert _CLI_VERSION_OPTION_RE.search(src)


def test_cargo_manifest_matches_pyproject() -> None:
    with (REPO_ROOT / "gui/src-tauri/Cargo.toml").open("rb") as fh:
        cargo = tomllib.load(fh)
    assert cargo["package"]["version"] == _pyproject_version()


def test_npm_manifest_matches_pyproject() -> None:
    assert _json_at("gui/package.json")["version"] == _pyproject_version()


def test_npm_lockfile_matches_pyproject() -> None:
    """`npm ci` checks the dependency tree against the lock, not the root
    `version`, so nothing else compares these two."""
    lock = _json_at("gui/package-lock.json")
    assert lock["version"] == _pyproject_version()
    assert lock["packages"][""]["version"] == _pyproject_version()


def test_tauri_config_inherits_its_version_from_cargo() -> None:
    """Tauri falls back to `Cargo.toml`'s version when the config omits the key
    — documented in the CLI's own config schema ("If removed the version number
    from `Cargo.toml` is used"), and observed in `tauri-codegen` 2.6.2, which
    emits `env!("CARGO_PKG_VERSION")` for the absent case. So the key stays
    absent: restoring it reinstates a third literal whose value nothing
    compares, since it feeds only the bundled app's metadata.
    """
    assert "version" not in _json_at("gui/src-tauri/tauri.conf.json")


def test_gui_client_version_is_injected_not_a_literal() -> None:
    """The About tab's client version comes from vite's `define`, fed by
    `gui/package.json`. It was a hand-kept literal, and had drifted three minors
    ahead of both GUI manifests — rendering "Client: 0.5.0" on a 0.3.0 build —
    while the component's own comment claimed it was kept in sync and its vitest
    assertion hardcoded the wrong value.
    """
    src = (REPO_ROOT / "gui/src/screens/settings/SettingsAbout.svelte").read_text()
    assert "__APP_VERSION__" in src
    assert not re.search(r"""CLIENT_VERSION\s*=\s*["']""", src)
