# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""`localmail --version` survives a missing dependency (#305).

`cli.py` imports the daemon — and so `sqlparse`, `psycopg`, `keyring` — at
module scope, so a partial `uv sync` killed every command, `--version`
included, with a bare traceback before click parsed anything. The entry point
is now `localmail.cli_entry:main`, which imports the CLI inside a guard.
"""

from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from localmail.cli_entry import (
    DEPENDENCY_EXIT_CODE,
    REPAIR_COMMAND,
    startup_failure_report,
)

_REPO = Path(__file__).resolve().parents[1]

# A fresh interpreter in which one third-party module cannot be imported —
# the shape a partial `uv sync` leaves behind.
_BLOCKER = """
import sys
class _Blocker:
    def find_spec(self, name, path=None, target=None):
        if name == "sqlparse" or name.startswith("sqlparse."):
            raise ModuleNotFoundError(f"No module named {name!r}", name=name)
        return None
sys.meta_path.insert(0, _Blocker())
"""


def _missing(name: str = "sqlparse") -> ModuleNotFoundError:
    return ModuleNotFoundError(f"No module named {name!r}", name=name)


def _report(exc: ImportError, argv: list[str], diagnostic: str | None = None):
    return startup_failure_report(
        exc, argv=argv, version="1.2.3", version_diagnostic=diagnostic
    )


# --- pure rule ---------------------------------------------------------------


def test_version_still_prints_its_line_and_exits_zero() -> None:
    report = _report(_missing(), ["--version"])
    assert report is not None
    assert report.stdout == "localmail, version 1.2.3"
    assert report.exit_code == 0, "exit 0 is the --version liveness contract"


def test_version_names_the_missing_module_and_the_repair_on_stderr() -> None:
    report = _report(_missing(), ["--config", "x.toml", "--version"])
    assert report is not None
    assert "sqlparse" in report.stderr
    assert REPAIR_COMMAND in report.stderr
    assert "1.2.3" not in report.stderr


def test_version_keeps_an_unresolvable_version_diagnostic_too() -> None:
    report = _report(_missing(), ["--version"], diagnostic="error: no METADATA")
    assert report is not None
    assert "error: no METADATA" in report.stderr
    assert "sqlparse" in report.stderr


def test_any_other_command_gets_one_line_and_a_nonzero_exit() -> None:
    report = _report(_missing(), ["sync", "--account", "gmail"])
    assert report is not None
    assert report.stdout is None
    assert report.exit_code == DEPENDENCY_EXIT_CODE != 0
    assert len(report.stderr.splitlines()) == 1
    assert "sqlparse" in report.stderr and REPAIR_COMMAND in report.stderr


def test_a_broken_third_party_name_import_counts_as_a_dependency() -> None:
    """`from psycopg import x` failing names the package, not localmail."""
    exc = ImportError("cannot import name 'x' from 'psycopg'", name="psycopg")
    report = _report(exc, ["sync"])
    assert report is not None and report.exit_code == DEPENDENCY_EXIT_CODE


@pytest.mark.parametrize(
    "exc",
    [
        ImportError("cannot import name 'x' from 'localmail.db'", name="localmail.db"),
        ModuleNotFoundError("No module named 'localmail.gone'", name="localmail.gone"),
        ImportError("no name at all"),
    ],
    ids=["own-name", "own-module", "nameless"],
)
def test_our_own_import_bug_is_not_blamed_on_a_dependency(exc: ImportError) -> None:
    """A localmail bug must keep its traceback: `uv sync` would not fix it."""
    assert _report(exc, ["sync"]) is None


def test_version_survives_even_our_own_import_bug() -> None:
    exc = ImportError("cannot import name 'x' from 'localmail.db'", name="localmail.db")
    report = _report(exc, ["--version"])
    assert report is not None
    assert report.stdout == "localmail, version 1.2.3"
    assert "localmail.db" in report.stderr
    assert REPAIR_COMMAND not in report.stderr, "uv sync would not fix our bug"


def test_a_version_token_after_the_separator_is_not_the_flag() -> None:
    report = _report(_missing(), ["search", "--", "--version"])
    assert report is not None and report.stdout is None


# --- wiring ------------------------------------------------------------------


def test_the_console_script_is_the_guarded_entry_point() -> None:
    project = tomllib.loads((_REPO / "pyproject.toml").read_text())["project"]
    assert project["scripts"]["localmail"] == "localmail.cli_entry:main"


def _run_blocked(entry: str, *args: str) -> subprocess.CompletedProcess[str]:
    code = _BLOCKER + f"sys.argv = ['localmail', *{list(args)!r}]\n" + entry
    return subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=120
    )


_VIA_ENTRY = "from localmail.cli_entry import main\nmain()\n"
_VIA_DASH_M = "import runpy\nrunpy.run_module('localmail', run_name='__main__')\n"


@pytest.mark.parametrize("entry", [_VIA_ENTRY, _VIA_DASH_M], ids=["script", "dash-m"])
def test_version_survives_a_missing_dependency_end_to_end(entry: str) -> None:
    result = _run_blocked(entry, "--version")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().startswith("localmail, version ")
    assert "Traceback" not in result.stderr
    assert "sqlparse" in result.stderr


@pytest.mark.parametrize("entry", [_VIA_ENTRY, _VIA_DASH_M], ids=["script", "dash-m"])
def test_a_command_fails_in_one_line_on_a_missing_dependency(entry: str) -> None:
    result = _run_blocked(entry, "list-accounts")
    assert result.returncode == DEPENDENCY_EXIT_CODE
    assert "Traceback" not in result.stderr
    assert REPAIR_COMMAND in result.stderr


def test_the_blocker_really_breaks_the_cli_import() -> None:
    """Positive control: without it the end-to-end tests prove nothing."""
    result = _run_blocked("import localmail.cli\n")
    assert result.returncode != 0
    assert "sqlparse" in result.stderr
