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

import ast
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
from localmail.version_report import (
    UNKNOWN_VERSION,
    VersionSource,
    unknown_version_diagnostic,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

def _mentions_version_option(source: str) -> bool:
    """True if the module's *code* references click's `version_option` at all.

    An AST walk rather than a regex, because the rationale for not using that
    decorator necessarily quotes its spelling — in the comment beside the
    replacement option and in `_print_version`'s docstring. #279's regex-over-
    stripped-comments approach handled the comment and not the docstring, so
    writing the reason down broke the pin that enforces it. Prose is not code,
    and the AST is where that distinction already lives.

    Catches the attribute form (`click.version_option`), a bare name from
    `from click import version_option`, and the import itself.
    """
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Attribute) and node.attr == "version_option":
            return True
        if isinstance(node, ast.Name) and node.id == "version_option":
            return True
        if isinstance(node, ast.ImportFrom) and any(
            alias.name == "version_option" for alias in node.names
        ):
            return True
    return False


def _printed_version(stdout: str) -> str:
    """The version token from the `%(prog)s, version %(v)s` line.

    Anchored on the tail rather than matched as a substring: `in` is satisfied
    by `0.3.0-dev` and `0.3.0+local` on a `0.3.0` install, i.e. by exactly the
    wrong answers the flag exists to rule out. The prog name is deliberately not
    asserted — it is `main` under `CliRunner` and `localmail` via the console
    script, and neither is what these tests are about.

    **Pass `result.stdout`, never `result.output`.** In click 8.4 `output` is
    stdout and stderr concatenated, so once #291 put a diagnostic on stderr the
    tail anchor started reading from whichever stream spoke last — and the
    diagnostic contains the word "version". Reading stdout is also the honest
    assertion: the machine-readable line is what stdout is *for* here.
    """
    return stdout.strip().rpartition("version ")[2]


@pytest.fixture
def forbid_db(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make any database connection attempt fail loudly.

    Both entry points, mirroring `test_cli_config_path.py::dsn_probe`:
    `open_pool` for the pooled commands, `psycopg.connect` for the direct ones.
    """

    def _forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("--version must not open a database connection")

    monkeypatch.setattr("localmail.db.open_pool", _forbidden)
    monkeypatch.setattr("psycopg.connect", _forbidden)


@pytest.fixture
def unknown_version(monkeypatch: pytest.MonkeyPatch):
    """Put the CLI in the state where the metadata could not be read.

    Rebinds what the callback actually reads — the two module attributes `cli`
    imported from the package — rather than stubbing `importlib.metadata` and
    reloading two modules. The resolution itself is pinned separately, on
    `resolve_version` (`test_version_report.py`) and on the package attributes
    (`test_the_fallback_records_which_failure_produced_it`); these tests are
    about what the *flag* does with the answer.
    """

    def _install(source: VersionSource) -> None:
        monkeypatch.setattr("localmail.cli.__version__", UNKNOWN_VERSION)
        monkeypatch.setattr("localmail.cli.__version_source__", source)

    return _install


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

    assert reimported_localmail(_raise).__version__ == UNKNOWN_VERSION


def test_a_readable_version_is_sourced_as_installed(reimported_localmail: Any) -> None:
    """`__version_source__` sits beside `__version__` so a reader can tell a
    real version from the sentinel without string-matching it (#291).

    Resolved **once**, at package import, and exported — not re-derived by each
    reader. A second independent lookup is the exact footgun a bare
    `@click.version_option()` carries: same question, different failure
    semantics, answers that diverge precisely where the guards earn their keep.
    """
    reloaded = reimported_localmail(lambda _name: "1.2.3+sentinel")
    assert reloaded.__version_source__ is VersionSource.INSTALLED


def test_the_fallback_records_which_failure_produced_it(
    reimported_localmail: Any,
) -> None:
    """The sentinel alone cannot say whether anything is installed, and the two
    remedies differ — so the cause travels with it."""

    def _raise(_name: str) -> str:
        raise importlib.metadata.PackageNotFoundError(_name)

    assert reimported_localmail(_raise).__version_source__ is (
        VersionSource.NOT_INSTALLED
    )
    assert reimported_localmail(lambda _name: None).__version_source__ is (
        VersionSource.METADATA_INCOMPLETE
    )


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
    flag. It is also the only `localmail` command that reports the version, so
    on a host running just the sync daemon reading it otherwise meant starting
    `serve` for `/v1/version`.
    """
    result = CliRunner().invoke(main, ["--version"])
    assert result.exit_code == 0, result.output
    assert _printed_version(result.stdout) == localmail.__version__


def test_cli_version_flag_needs_no_config_or_database(
    forbid_db: None,
) -> None:
    """The flag answers "did my deploy land?", so it must answer it on a host
    where nothing else works yet — no config file written, no Postgres running.

    That rules out folding anything config- or DB-derived (a DSN, the applied
    migration revision) into this output: the one moment an operator most needs
    a version is the moment those lookups fail.

    Both halves are asserted structurally, because neither holds by accident:

    - **The DB half needs `forbid_db`.** Asserting only `exit_code == 0` tests
      nothing while Postgres is reachable, which it is on CI and on both
      deployments — a version callback that read `schema_migrations` passed
      every assertion this test used to make. `forbid_db` makes *any* connection
      attempt fail regardless of whether a server is up.
    - **The config half needs the control's `filename`.** `list-accounts` raises
      `FileNotFoundError` from the *default* path too, so on a runner with no
      `~/.config/localmail/config.toml` the bare `isinstance` check passed
      whether or not `$LOCALMAIL_CONFIG` was read at all. Asserting the path it
      actually tried is what makes the control discriminating — it fails if
      `default_config_path` stops honouring the variable.
    """
    env = {"LOCALMAIL_CONFIG": "./nonexistent/config.toml"}
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(main, ["--version"], env=env)
        control = runner.invoke(main, ["list-accounts"], env=env)

    assert result.exit_code == 0, result.output
    assert _printed_version(result.stdout) == localmail.__version__

    assert isinstance(control.exception, FileNotFoundError)
    assert control.exception.filename is not None
    assert control.exception.filename.endswith("nonexistent/config.toml"), (
        control.exception.filename
    )


def test_cli_version_flag_is_derived_not_a_literal() -> None:
    """Pins the derivation *behaviourally*, which the old source regex could not.

    The value test above cannot tell a derivation from a literal that happens
    to match the installed distribution — the normal state right after a
    release — so #279 pinned the decorator's spelling with a regex instead.
    #291 replaced the decorator with a callback, and this is the stronger
    replacement rather than a looser one: rebinding the module attribute the
    callback reads proves the printed value *comes from* `localmail.__version__`
    at call time. A hardcoded string fails here; so does one assembled around
    the attribute (`__version__ + "-dev"`), which is the case the regex needed a
    trailing `[,)]` to catch and would still have missed inside an f-string.
    """
    monkeypatched = "9.9.9+sentinel"
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("localmail.cli.__version__", monkeypatched)
        result = CliRunner().invoke(main, ["--version"])

    assert result.exit_code == 0, result.output
    assert _printed_version(result.stdout) == monkeypatched


def test_cli_does_not_reintroduce_click_version_option() -> None:
    """`@click.version_option` must not come back, in any spelling.

    It is the one property only source can express, and it is now stricter than
    #279's version of this pin, which permitted the compliant
    `@click.version_option(__version__)`. Two independent reasons to forbid it
    outright:

    - **It bypasses the diagnostic.** click's own callback prints the line and
      exits; a version_option that happened to be passed `__version__` would
      print `0.0.0+unknown` and say nothing, i.e. reinstate #291 exactly.
    - **The bare form adds a second metadata reader.** click then looks the
      version up itself, independently of `__version__`, and the two disagree
      precisely where `version_report`'s guards earn their keep: on a tree that
      was never installed click raises `RuntimeError` where every other reader
      degrades to the sentinel.

    Asserted over the AST, not the text: the reason above is written down in
    `cli.py` too, and prose quoting a forbidden spelling must not be able to
    satisfy — or, as happened here first, to break — a source pin.
    """
    src = (REPO_ROOT / "src/localmail/cli.py").read_text()
    assert not _mentions_version_option(src)


def test_cli_version_flag_says_nothing_on_stderr_when_the_version_is_known() -> None:
    """The overwhelmingly common case must stay quiet, or the warning that
    matters gets filtered out by habit."""
    result = CliRunner().invoke(main, ["--version"])
    assert result.exit_code == 0, result.output
    assert result.stderr == ""


def test_cli_version_flag_reports_an_unknown_version_as_a_failure(
    unknown_version: Any,
) -> None:
    """#291: the sentinel used to print with exit 0 and nothing else — "the
    version could not be determined", in a format indistinguishable from a
    successful answer, at the one moment an operator is diagnosing a broken
    install. The stderr line is what carries the value.
    """
    unknown_version(VersionSource.NOT_INSTALLED)
    result = CliRunner().invoke(main, ["--version"])

    assert _printed_version(result.stdout) == UNKNOWN_VERSION
    assert result.stderr == (
        unknown_version_diagnostic(VersionSource.NOT_INSTALLED) or ""
    ) + "\n"


def test_cli_version_flag_keeps_the_diagnostic_off_stdout(
    unknown_version: Any,
) -> None:
    """stdout stays exactly one machine-readable line.

    Load-bearing, and the reason the diagnostic is not simply folded into
    `version_option(message=…)`: `localmail --version` is scripted (the
    manual's install-verification step), and a warning on stdout breaks every
    naive parser of it — including `_printed_version` here, whose tail anchor
    would read the diagnostic's own "version" instead.
    """
    unknown_version(VersionSource.METADATA_INCOMPLETE)
    result = CliRunner().invoke(main, ["--version"])

    assert len(result.stdout.splitlines()) == 1
    assert _printed_version(result.stdout) == UNKNOWN_VERSION
    assert result.stderr != ""


def test_cli_version_flag_still_exits_zero_when_the_version_is_unknown(
    unknown_version: Any,
) -> None:
    """An explicit decision, not an oversight (#291).

    A non-zero exit would break every script that runs `--version` as a
    liveness check, and it argues against the deliberate choice to degrade
    gracefully rather than raise the way click's own lookup does. The report is
    the remedy here; the exit status is not the channel for it.
    """
    unknown_version(VersionSource.NOT_INSTALLED)
    assert CliRunner().invoke(main, ["--version"]).exit_code == 0


def test_cli_version_flag_tells_the_two_unknown_causes_apart(
    unknown_version: Any,
) -> None:
    """Both print the same sentinel, so the stderr line is the only thing that
    can point at the right remedy — `uv sync` does not repair a dist-info that
    is already present."""
    unknown_version(VersionSource.NOT_INSTALLED)
    never_installed = CliRunner().invoke(main, ["--version"]).stderr

    unknown_version(VersionSource.METADATA_INCOMPLETE)
    damaged = CliRunner().invoke(main, ["--version"]).stderr

    assert never_installed != damaged
    assert "--reinstall" in damaged
    assert "--reinstall" not in never_installed


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

