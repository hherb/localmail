# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""The rules behind the autouse pool-closing fixture (#321).

Every test here is pure except the three that name `db_dsn`, so the file runs
without Postgres apart from those.
"""
from __future__ import annotations

import ast
import importlib
import sys
from pathlib import Path

import pytest

# Module scope, and load-bearing for this file's own end-to-end pins: the
# autouse fixture patches a seam only if its module is already in
# `sys.modules`. Without this line the three `db_dsn` tests below passed only
# because an earlier test in the file happened to `importlib.import_module` it
# first, so running one of them alone — `pytest tests/test_pool_leaks.py::…` —
# failed, and leaked the very pool it was asserting about.
import localmail.serve.app  # noqa: F401

from tests._pool_leaks import (
    DB_MODULE,
    POOL_SEAM_ATTR,
    POOL_SEAMS,
    SERVE_APP_MODULE,
    close_pools,
    function_local_serve_app_imports,
    late_seam_error,
    loaded_seams,
    missing_seam_error,
    pool_constructor_calls,
    recording_factory,
    unclosed,
)


def _scanned_python_files() -> list[Path]:
    """Every Python file under `tests/`, recursively.

    Not `test_*.py`: `conftest.py`, the `_*.py` helpers and `acceptance/` can
    import `localmail.serve.app` too, and a function-local import in any of
    them defeats the fixture's `sys.modules` gate exactly as one in a test
    file does.
    """
    return sorted(Path(__file__).parent.rglob("*.py"))


def _repo_source_files() -> list[Path]:
    """Every Python file under `src/localmail/`, recursively."""
    root = Path(__file__).resolve().parent.parent / "src" / "localmail"
    return sorted(root.rglob("*.py"))


class FakePool:
    """Stands in for `psycopg_pool.ConnectionPool` — same two members we use."""

    def __init__(self, *args, **kwargs) -> None:
        self.args = args
        self.kwargs = kwargs
        self.closed = False
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1
        self.closed = True


# --------------------------------------------------------------------------
# recording_factory
# --------------------------------------------------------------------------


def test_recording_factory_records_every_pool_it_builds() -> None:
    sink: list[FakePool] = []
    factory = recording_factory(FakePool, sink)

    first = factory("dsn-a")
    second = factory("dsn-b")

    assert sink == [first, second]


def test_recording_factory_returns_the_real_factorys_object() -> None:
    """The wrapper must be transparent: `create_app` keeps the pool it built."""
    sink: list[FakePool] = []
    pool = recording_factory(FakePool, sink)("dsn")

    assert isinstance(pool, FakePool)
    assert sink[0] is pool


def test_recording_factory_passes_arguments_through_unchanged() -> None:
    sink: list[FakePool] = []

    pool = recording_factory(FakePool, sink)("dsn", min_size=1, max_size=4)

    assert pool.args == ("dsn",)
    assert pool.kwargs == {"min_size": 1, "max_size": 4}


def test_recording_factory_records_nothing_when_the_real_factory_raises() -> None:
    """A pool that was never built cannot be closed, and must not be listed."""
    def explode(*_args, **_kwargs):
        raise RuntimeError("bad dsn")

    sink: list[object] = []
    with pytest.raises(RuntimeError):
        recording_factory(explode, sink)("dsn")

    assert sink == []


# --------------------------------------------------------------------------
# unclosed / close_pools
# --------------------------------------------------------------------------


def test_unclosed_skips_a_pool_that_already_closed_itself() -> None:
    """A `with TestClient(app)` runs the lifespan, which closes the pool."""
    lifespan_closed, leaked = FakePool(), FakePool()
    lifespan_closed.close()

    assert unclosed([lifespan_closed, leaked]) == [leaked]


def test_close_pools_closes_the_leaked_ones_and_reports_how_many() -> None:
    already, leaked_a, leaked_b = FakePool(), FakePool(), FakePool()
    already.close()

    assert close_pools([already, leaked_a, leaked_b]) == 2
    assert leaked_a.closed and leaked_b.closed


def test_close_pools_does_not_reclose_a_pool_the_lifespan_closed() -> None:
    """`close()` is idempotent, but a second call is still a claim we did work."""
    already = FakePool()
    already.close()

    assert close_pools([already]) == 0
    assert already.close_calls == 1


def test_close_pools_with_nothing_recorded_is_a_no_op() -> None:
    assert close_pools([]) == 0


def test_close_pools_closes_every_pool_even_when_one_raises() -> None:
    """Aborting on the first failure would leak the rest — and their
    finalisers then raise `cannot join current thread` against some later,
    unrelated test, which is the misattribution this module exists to end.
    """
    class Boom(FakePool):
        def close(self) -> None:
            raise RuntimeError("close failed")

    boom, after = Boom(), FakePool()

    with pytest.raises(RuntimeError, match="close failed"):
        close_pools([boom, after])

    assert after.closed, "a pool after the failing one was left open"


def test_close_pools_reports_a_second_failure_rather_than_dropping_it() -> None:
    """Raising only the first would make the broad catch silent for the rest."""
    class Boom(FakePool):
        def __init__(self, label: str) -> None:
            super().__init__()
            self.label = label

        def close(self) -> None:
            raise RuntimeError(f"close failed: {self.label}")

    with pytest.raises(RuntimeError) as excinfo:
        close_pools([Boom("first"), Boom("second")])

    assert "first" in str(excinfo.value)
    assert any("second" in note for note in excinfo.value.__notes__)


# --------------------------------------------------------------------------
# the seam
# --------------------------------------------------------------------------


@pytest.mark.parametrize("module_name", [SERVE_APP_MODULE, DB_MODULE])
def test_every_declared_seam_really_exposes_its_attribute(module_name: str) -> None:
    """Each module resolves this name from its own globals on every call."""
    module = importlib.import_module(module_name)
    attr = dict((name, a) for name, a in POOL_SEAMS)[module_name]

    assert missing_seam_error(module, module_name, attr) is None


def test_missing_seam_error_names_the_module_and_the_attribute() -> None:
    """An aliased import (`... as Pool`) makes the fixture silently inert."""
    class Renamed:
        pass

    message = missing_seam_error(Renamed, SERVE_APP_MODULE, POOL_SEAM_ATTR)

    assert message is not None
    assert SERVE_APP_MODULE in message
    assert POOL_SEAM_ATTR in message


def test_loaded_seams_skips_a_module_that_is_not_imported() -> None:
    """Nothing can have built a pool through a module that does not exist yet."""
    modules = {"present": object()}

    found = loaded_seams(modules, [("present", "X"), ("absent", "X")])

    assert [name for name, _module, _attr in found] == ["present"]


def test_loaded_seams_defaults_to_every_declared_seam() -> None:
    fake = {name: object() for name, _attr in POOL_SEAMS}

    assert len(loaded_seams(fake)) == len(POOL_SEAMS)


def test_the_db_seam_is_always_loaded_because_conftest_imports_it() -> None:
    """`conftest` imports `localmail.db` for `apply_migrations`, so that seam
    never depends on the module-scope import rule below — only serve.app does.
    """
    assert DB_MODULE in sys.modules


# --------------------------------------------------------------------------
# the module-scope import rule that makes the sys.modules gate sound
# --------------------------------------------------------------------------


def test_the_scanner_reports_the_sibling_from_localmail_serve_import_app() -> None:
    """`from localmail.serve import app` loads the same module.

    Matching only `from localmail.serve.app import …` let a function-local
    `from localmail.serve import app as app_mod` sit in
    `test_daemon_supervisor_lifecycle.py` while this scan called the suite
    compliant.
    """
    source = "def test_x():\n    from localmail.serve import app as app_mod\n"

    assert function_local_serve_app_imports(source, "t.py") == [2]


def test_the_scanner_ignores_an_unrelated_name_from_the_parent_package() -> None:
    """The parent package has other members; only `app` is a seam."""
    source = "def test_x():\n    from localmail.serve import admin\n"

    assert function_local_serve_app_imports(source, "t.py") == []


def test_the_scanner_reports_a_function_local_import() -> None:
    source = (
        "def test_x():\n"
        "    from localmail.serve.app import create_app\n"
        "    assert create_app\n"
    )

    assert function_local_serve_app_imports(source, "t.py") == [2]


def test_the_scanner_reports_a_function_local_plain_import() -> None:
    source = "def test_x():\n    import localmail.serve.app\n"

    assert function_local_serve_app_imports(source, "t.py") == [2]


def test_the_scanner_ignores_a_module_level_import() -> None:
    source = "from localmail.serve.app import create_app\n\ndef test_x():\n    pass\n"

    assert function_local_serve_app_imports(source, "t.py") == []


def test_the_scanner_ignores_an_unrelated_function_local_import() -> None:
    source = "def test_x():\n    from localmail.search.embed_worker import reset_failure_log\n"

    assert function_local_serve_app_imports(source, "t.py") == []


def test_no_collected_test_module_imports_serve_app_below_module_scope() -> None:
    """The fixture patches the seam at test-setup time, which is after pytest
    has imported every collected module — so a module-level import is always
    in place in time and a function-local one never is. A function-local
    import therefore leaks that file's pools, silently.
    """
    offenders: list[str] = []
    for path in _scanned_python_files():
        lines = function_local_serve_app_imports(path.read_text(), path.name)
        offenders.extend(f"{path.name}:{n}" for n in lines)

    assert offenders == [], (
        f"import {SERVE_APP_MODULE} at module scope in: {', '.join(offenders)}"
    )


# --------------------------------------------------------------------------
# the fixture itself, driven end to end
# --------------------------------------------------------------------------


def test_the_autouse_fixture_closes_a_pool_opened_during_the_test(monkeypatch) -> None:
    """Drive the real fixture body: setup patches the seam, teardown closes.

    The seam is swapped for `FakePool` first, so the fixture wraps that rather
    than opening real connections.
    """
    from tests.conftest import close_leaked_pools

    app_module = importlib.import_module(SERVE_APP_MODULE)
    monkeypatch.setattr(app_module, POOL_SEAM_ATTR, FakePool)

    gen = close_leaked_pools.__wrapped__()
    recorded = next(gen)

    pool = getattr(app_module, POOL_SEAM_ATTR)("dsn")
    assert recorded == [pool]
    assert not pool.closed

    with pytest.raises(StopIteration):
        next(gen)
    assert pool.closed


def test_the_fixture_records_nothing_when_serve_app_was_never_imported(
    monkeypatch,
) -> None:
    """A run collecting no serve test must not pay for importing the module.

    The assertion is that the module is *still absent* afterwards, not that no
    pool was recorded: no pool is built here, so the recorded list is empty
    under every implementation — including one that imports the module rather
    than skipping it. That is what this test used to assert, and it passed
    against exactly the mutation it was written to catch.
    """
    from tests.conftest import close_leaked_pools

    monkeypatch.delitem(sys.modules, SERVE_APP_MODULE, raising=False)

    gen = close_leaked_pools.__wrapped__()
    assert next(gen) == []
    assert SERVE_APP_MODULE not in sys.modules, (
        "the fixture imported the module it is supposed to skip"
    )
    with pytest.raises(StopIteration):
        next(gen)


def test_create_app_registers_its_pool_with_the_running_fixture(
    db_dsn: str, close_leaked_pools: list
) -> None:
    """End to end against the real `create_app`: the seam is live during a test."""
    app_module = importlib.import_module(SERVE_APP_MODULE)
    app = app_module.create_app(db_dsn=db_dsn, searcher=None)

    assert app.state.pool in close_leaked_pools


def test_a_db_pool_is_recorded_too(db_dsn: str, close_leaked_pools: list) -> None:
    """`localmail.db.open_pool` is the other seam: Daemon and Searcher pools.

    `Daemon.stop()`/`join()` do not close `self.pool`, so 13 daemon tests plus
    one searcher test leaked one each — invisible on macOS and reported on
    Linux/3.13, because the warning depends on when the GC runs.
    """
    from localmail.db import open_pool

    pool = open_pool(db_dsn)

    assert pool in close_leaked_pools


def test_a_lifespan_run_closes_the_pool_before_the_fixture_sees_it(
    db_dsn: str, close_leaked_pools: list
) -> None:
    """`with TestClient(app)` is already correct; the fixture must find nothing."""
    from fastapi.testclient import TestClient

    app_module = importlib.import_module(SERVE_APP_MODULE)
    app = app_module.create_app(db_dsn=db_dsn, searcher=None)
    with TestClient(app):
        pass

    assert app.state.pool in close_leaked_pools
    assert unclosed(close_leaked_pools) == []


def test_every_test_module_is_scanned_by_the_import_rule() -> None:
    """Guard the guard: the glob above must actually match this suite.

    A typo'd pattern makes the offender scan pass over an empty set, which
    reports every future function-local import as compliant. Named files
    rather than a count threshold, so the pin says what it depends on.
    """
    scanned = {path.name for path in _scanned_python_files()}

    assert Path(__file__).name in scanned
    # A file that really does import the module the scan is about.
    assert "test_serve_app_baseline.py" in scanned
    # Not just `test_*.py`: conftest and the `_*.py` helpers can import it too,
    # and a helper's function-local import leaks exactly as a test file's does.
    assert "conftest.py" in scanned
    assert "_pool_leaks.py" in scanned


def test_the_scanner_parses_every_test_module_it_claims_to_scan() -> None:
    """`ast.parse` must not raise on any collected module, or the scan is blind."""
    for path in _scanned_python_files():
        ast.parse(path.read_text(), filename=path.name)


# --------------------------------------------------------------------------
# the teardown re-check: the gate's inference verified rather than trusted
# --------------------------------------------------------------------------


def test_late_seam_error_names_a_seam_that_arrived_after_setup() -> None:
    message = late_seam_error({SERVE_APP_MODULE: object()}, patched=[])

    assert message is not None
    assert SERVE_APP_MODULE in message


def test_late_seam_error_is_quiet_when_the_seam_was_patched() -> None:
    assert (
        late_seam_error({SERVE_APP_MODULE: object()}, patched=[SERVE_APP_MODULE])
        is None
    )


def test_late_seam_error_is_quiet_when_the_seam_never_loaded() -> None:
    """The legitimate skip: a unit-only run never imports serve.app at all."""
    assert late_seam_error({}, patched=[]) is None


def test_the_fixture_reports_a_module_that_arrived_during_the_test(
    monkeypatch,
) -> None:
    """The route no scanner can enumerate — a lazy import inside `src/`, an
    `importlib.import_module`, a `__import__` — is caught by its outcome.
    """
    from tests.conftest import close_leaked_pools

    monkeypatch.delitem(sys.modules, SERVE_APP_MODULE, raising=False)

    gen = close_leaked_pools.__wrapped__()
    next(gen)
    # Stand in for whatever imported it mid-test.
    monkeypatch.setitem(sys.modules, SERVE_APP_MODULE, object())

    with pytest.raises(RuntimeError, match="after the pool seam was patched"):
        next(gen)


# --------------------------------------------------------------------------
# a broken seam stops the run rather than going quietly inert
# --------------------------------------------------------------------------


def test_the_fixture_stops_the_run_when_a_seam_lost_its_attribute(
    monkeypatch,
) -> None:
    """An aliased import (`... as Pool`) makes the fixture unable to record.

    Driven through the fixture, not just through `missing_seam_error`: the
    pure rule was pinned and the fixture's *use* of it was not, so replacing
    the report with a `continue` left the whole suite green — a guard going
    quietly inert, which is the shape this module exists to remove.

    `pytest.exit` raises `_pytest.outcomes.Exit`, which is private; the type
    is asserted by name so the one-line-not-3000-tracebacks choice is pinned
    without importing it.
    """
    from tests.conftest import close_leaked_pools

    db_module = importlib.import_module(DB_MODULE)
    monkeypatch.delattr(db_module, POOL_SEAM_ATTR)

    gen = close_leaked_pools.__wrapped__()
    with pytest.raises(BaseException) as excinfo:
        next(gen)

    assert type(excinfo.value).__name__ == "Exit"
    assert POOL_SEAM_ATTR in str(excinfo.value)
    assert DB_MODULE in str(excinfo.value)


# --------------------------------------------------------------------------
# POOL_SEAMS is the complete set of modules that build a pool
# --------------------------------------------------------------------------


def test_pool_constructor_calls_finds_every_spelling_of_a_pool_build() -> None:
    source = (
        "import psycopg_pool\n"
        "a = ConnectionPool(dsn)\n"
        "b = psycopg_pool.ConnectionPool(dsn)\n"
        "c = AsyncConnectionPool(dsn)\n"
        "d = something_else(dsn)\n"
    )

    assert [name for name, _line in pool_constructor_calls(source, "t.py")] == [
        "ConnectionPool",
        "ConnectionPool",
        "AsyncConnectionPool",
    ]


def test_only_the_declared_seams_construct_a_pool_anywhere_in_src() -> None:
    """`missing_seam_error` asks whether the *name* is present, so it cannot
    see a third module growing a pool, an existing one switching to a
    fully-qualified `psycopg_pool.ConnectionPool(...)` call, or a move to
    `AsyncConnectionPool`. All three leave `POOL_SEAMS` intact, nothing
    patched, and no test failing — so the construction sites are read from
    `src/` directly and compared against what the fixture patches.
    """
    building: dict[str, list[tuple[str, int]]] = {}
    root = Path(__file__).resolve().parent.parent / "src"
    for path in _repo_source_files():
        calls = pool_constructor_calls(path.read_text(), path.name)
        if calls:
            module = path.relative_to(root).with_suffix("")
            building[".".join(module.parts)] = calls

    assert set(building) == {name for name, _attr in POOL_SEAMS}, (
        f"pools are built in {sorted(building)}; POOL_SEAMS declares "
        f"{sorted(name for name, _attr in POOL_SEAMS)}. Add the new module to "
        f"POOL_SEAMS or its pools leak unrecorded (#321)."
    )
