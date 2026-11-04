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

import localmail
from localmail.cli_entry import (
    DEPENDENCY_EXIT_CODE,
    REPAIR_COMMAND,
    startup_failure_report,
)

_REPO = Path(__file__).resolve().parents[1]

# A fresh interpreter in which one module cannot be imported — the shape a
# partial `uv sync` leaves behind.
_BLOCKER = """
import sys
class _Blocker:
    def find_spec(self, name, path=None, target=None):
        if name == {blocked!r} or name.startswith({blocked!r} + "."):
            raise {exc_class}(f"No module named {{name!r}}", name=name)
        return None
sys.meta_path.insert(0, _Blocker())
"""


def _missing(name: str = "sqlparse") -> ModuleNotFoundError:
    return ModuleNotFoundError(f"No module named {name!r}", name=name)


def _raised_in(module: str, exc: Exception) -> Exception:
    """`exc`, raised from a frame whose module is `module`."""
    try:
        exec("raise exc", {"__name__": module, "exc": exc})
    except Exception as raised:
        return raised
    raise AssertionError("unreachable")


def _report(exc: Exception, argv: list[str], diagnostic: str | None = None):
    return startup_failure_report(
        exc, argv=argv, version="1.2.3", version_diagnostic=diagnostic
    )


# --- pure rule ---------------------------------------------------------------


def test_version_still_prints_its_line_and_exits_zero() -> None:
    report = _report(_missing(), ["--version"])
    assert report.stdout == "localmail, version 1.2.3"
    assert report.exit_code == 0, "exit 0 is the --version liveness contract"


def test_version_after_a_global_option_still_prints_its_line() -> None:
    report = _report(_missing(), ["--config", "x.toml", "--version"])
    assert report.stdout == "localmail, version 1.2.3"
    assert report.exit_code == 0
    assert "sqlparse" in report.stderr
    assert REPAIR_COMMAND in report.stderr
    assert "1.2.3" not in report.stderr


def test_version_puts_an_unresolvable_version_diagnostic_first() -> None:
    report = _report(_missing(), ["--version"], diagnostic="error: no METADATA")
    lines = report.stderr.splitlines()
    assert lines[0] == "error: no METADATA"
    assert "sqlparse" in lines[1]


def test_the_dependency_exit_code_is_one() -> None:
    # A literal, not the constant: 2 is click's usage error, and README says 1.
    assert DEPENDENCY_EXIT_CODE == 1


def test_any_other_command_gets_one_line_and_a_nonzero_exit() -> None:
    report = _report(_missing(), ["sync", "--account", "gmail"])
    assert report.stdout is None
    assert report.exit_code == 1
    assert len(report.stderr.splitlines()) == 1
    assert "sqlparse" in report.stderr and REPAIR_COMMAND in report.stderr


def test_any_other_command_still_reports_an_unresolvable_version() -> None:
    """#304: the group callback that would report it never runs here."""
    report = _report(_missing(), ["sync"], diagnostic="error: no METADATA")
    lines = report.stderr.splitlines()
    assert lines[0] == "error: no METADATA"
    assert REPAIR_COMMAND in lines[1]


def test_a_broken_third_party_name_import_counts_as_a_dependency() -> None:
    """`from psycopg import x` failing names the package, not localmail."""
    exc = ImportError("cannot import name 'x' from 'psycopg'", name="psycopg")
    assert _report(exc, ["sync"]).exit_code == 1


@pytest.mark.parametrize(
    "exc",
    [
        ImportError("no pq wrapper available."),
        SystemError("The installed pydantic-core version is incompatible"),
    ],
    ids=["nameless-import-error", "system-error"],
)
def test_a_nameless_failure_raised_inside_a_dependency_counts(exc: Exception) -> None:
    """psycopg reports a missing wheel or libpq with no `.name`."""
    report = _report(_raised_in("psycopg.pq", exc), ["sync"])
    assert report.exit_code == 1
    assert REPAIR_COMMAND in report.stderr


@pytest.mark.parametrize(
    "exc",
    [
        ImportError("cannot import name 'x' from 'localmail.db'", name="localmail.db"),
        ModuleNotFoundError("No module named 'localmail.gone'", name="localmail.gone"),
        ImportError("no name at all"),
        _raised_in("localmail.db", ImportError("nameless, raised by us")),
        # `.name` on a NameError is the undefined name, not a module.
        _raised_in(
            "localmail.cli",
            NameError("name 'psycopg' is not defined", name="psycopg"),
        ),
    ],
    ids=["own-name", "own-module", "no-traceback", "own-nameless", "own-non-import"],
)
def test_our_own_import_bug_is_not_blamed_on_a_dependency(exc: Exception) -> None:
    """A localmail bug must keep its traceback: `uv sync` would not fix it."""
    report = _report(exc, ["sync"])
    assert report.exit_code is None, "None means: re-raise"
    assert report.stdout is None and report.stderr == ""


def test_a_failure_in_the_import_machinery_is_charged_to_the_importer() -> None:
    """An unreadable or truncated file raises inside importlib, not the package.

    Charged to localmail, it keeps its traceback — the safe direction, since
    the frame cannot tell a broken dependency file from one of ours.
    """
    machinery = {"__name__": "importlib._bootstrap_external"}
    exec(
        "def read():\n"
        "    raise PermissionError(13, 'Permission denied', '/x/sqlparse/__init__.py')\n",
        machinery,
    )
    try:
        exec("read()", {"__name__": "localmail.db", "read": machinery["read"]})
    except PermissionError as exc:
        report = _report(exc, ["sync"])
    assert report.exit_code is None


def test_our_own_import_bug_still_reports_an_unresolvable_version() -> None:
    report = _report(_missing("localmail.gone"), ["sync"], diagnostic="error: x")
    assert report.exit_code is None
    assert report.stderr == "error: x"


def test_version_survives_even_our_own_import_bug() -> None:
    exc = ImportError("cannot import name 'x' from 'localmail.db'", name="localmail.db")
    report = _report(exc, ["--version"])
    assert report.stdout == "localmail, version 1.2.3"
    assert report.exit_code == 0
    assert "localmail.db" in report.stderr
    assert REPAIR_COMMAND not in report.stderr, "uv sync would not fix our bug"


def test_a_version_token_after_the_separator_is_not_the_flag() -> None:
    report = _report(_missing(), ["search", "--", "--version"])
    assert report.stdout is None


def test_the_whole_exception_chain_is_rendered() -> None:
    """#303: the errno and the path are on the inner exception."""
    exc = ImportError("cannot load psycopg", name="psycopg")
    exc.__cause__ = OSError(5, "Input/output error", "/x/_psycopg.so")
    assert "/x/_psycopg.so" in _report(exc, ["sync"]).stderr


def test_a_render_failure_degrades_to_the_type_name() -> None:
    class _Hostile(ImportError):
        @property
        def __cause__(self):  # type: ignore[override]
            raise RuntimeError("hostile")

    report = _report(_Hostile("x", name="psycopg"), ["--version"])
    assert report.exit_code == 0
    assert "_Hostile" in report.stderr


# --- wiring ------------------------------------------------------------------


def test_the_console_script_is_the_guarded_entry_point() -> None:
    project = tomllib.loads((_REPO / "pyproject.toml").read_text())["project"]
    assert project["scripts"]["localmail"] == "localmail.cli_entry:main"


_VIA_ENTRY = "from localmail.cli_entry import main\nmain()\n"
_VIA_DASH_M = "import runpy\nrunpy.run_module('localmail', run_name='__main__')\n"


def _run(
    entry: str,
    *args: str,
    setup: str | None = None,
    blocked: str = "sqlparse",
    exc_class: str = "ModuleNotFoundError",
) -> subprocess.CompletedProcess[str]:
    if setup is None:
        setup = _BLOCKER.format(blocked=blocked, exc_class=exc_class)
    code = setup + f"\nsys.argv = ['localmail', *{list(args)!r}]\n" + entry
    return subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=120
    )


def _shadowing(tmp_path: Path, package: str, body: str) -> str:
    """Setup code that puts a broken `package` ahead of the real one."""
    (tmp_path / package).mkdir()
    (tmp_path / package / "__init__.py").write_text(body)
    return f"import sys\nsys.path.insert(0, {str(tmp_path)!r})\n"


@pytest.mark.parametrize("entry", [_VIA_ENTRY, _VIA_DASH_M], ids=["script", "dash-m"])
def test_version_survives_a_missing_dependency_end_to_end(entry: str) -> None:
    result = _run(entry, "--version")
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"localmail, version {localmail.__version__}\n"
    assert "Traceback" not in result.stderr
    assert "sqlparse" in result.stderr


@pytest.mark.parametrize("entry", [_VIA_ENTRY, _VIA_DASH_M], ids=["script", "dash-m"])
def test_a_command_fails_in_one_line_on_a_missing_dependency(entry: str) -> None:
    result = _run(entry, "list-accounts")
    assert result.returncode == 1
    assert "Traceback" not in result.stderr
    assert REPAIR_COMMAND in result.stderr


def test_a_plain_import_error_from_a_dependency_is_caught_end_to_end() -> None:
    result = _run(_VIA_ENTRY, "list-accounts", exc_class="ImportError")
    assert result.returncode == 1
    assert "Traceback" not in result.stderr
    assert REPAIR_COMMAND in result.stderr


def test_the_version_diagnostic_comes_first_end_to_end() -> None:
    setup = (
        _BLOCKER.format(blocked="sqlparse", exc_class="ModuleNotFoundError")
        + "import localmail\nlocalmail.__version_diagnostic__ = 'DIAG-SENTINEL'\n"
    )
    for args in (["--version"], ["list-accounts"]):
        result = _run(_VIA_ENTRY, *args, setup=setup)
        assert result.stderr.splitlines()[0] == "DIAG-SENTINEL", args


def test_our_own_import_bug_keeps_its_traceback_end_to_end() -> None:
    result = _run(_VIA_ENTRY, "list-accounts", blocked="localmail.daemon")
    assert result.returncode == 1
    assert "Traceback" in result.stderr
    assert "localmail.daemon" in result.stderr
    assert REPAIR_COMMAND not in result.stderr


@pytest.mark.parametrize(
    "body",
    [
        'raise ImportError("no pq wrapper available.")\n',
        'raise SystemError("pydantic-core version is incompatible")\n',
    ],
    ids=["nameless-import-error", "system-error"],
)
def test_a_dependency_that_raises_at_import_is_reported_end_to_end(
    tmp_path: Path, body: str
) -> None:
    """The real shapes of a broken psycopg wheel and a half-upgraded pydantic."""
    setup = _shadowing(tmp_path, "sqlparse", body)
    version = _run(_VIA_ENTRY, "--version", setup=setup)
    assert version.returncode == 0, version.stderr
    assert version.stdout == f"localmail, version {localmail.__version__}\n"
    assert REPAIR_COMMAND in version.stderr
    command = _run(_VIA_ENTRY, "list-accounts", setup=setup)
    assert command.returncode == 1
    assert "Traceback" not in command.stderr
    assert REPAIR_COMMAND in command.stderr


def test_the_blocker_really_breaks_the_cli_import() -> None:
    """Positive control: without it the end-to-end tests prove nothing."""
    result = _run("import localmail.cli\n")
    assert result.returncode != 0
    assert "sqlparse" in result.stderr


def test_the_shadowing_package_really_breaks_the_cli_import(tmp_path: Path) -> None:
    setup = _shadowing(tmp_path, "sqlparse", 'raise SystemError("x")\n')
    result = _run("import localmail.cli\n", setup=setup)
    assert result.returncode != 0
    assert "SystemError" in result.stderr
