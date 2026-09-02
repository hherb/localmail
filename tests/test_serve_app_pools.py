# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""The rules behind the autouse pool-closing fixture (#321).

Every test here is pure except the two that name `db_dsn`, so the file runs
without Postgres apart from those.
"""
from __future__ import annotations

import ast
import importlib
import sys
from pathlib import Path

import pytest

from tests._serve_app_pools import (
    POOL_SEAM_ATTR,
    SERVE_APP_MODULE,
    close_pools,
    function_local_serve_app_imports,
    missing_seam_error,
    recording_factory,
    unclosed,
)


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


# --------------------------------------------------------------------------
# the seam
# --------------------------------------------------------------------------


def test_the_real_serve_app_module_exposes_the_seam() -> None:
    """`create_app` resolves this name from module globals on every call."""
    app_module = importlib.import_module(SERVE_APP_MODULE)

    assert missing_seam_error(app_module) is None


def test_missing_seam_error_names_the_module_and_the_attribute() -> None:
    """An aliased import (`... as Pool`) makes the fixture silently inert."""
    class Renamed:
        pass

    message = missing_seam_error(Renamed)

    assert message is not None
    assert SERVE_APP_MODULE in message
    assert POOL_SEAM_ATTR in message


# --------------------------------------------------------------------------
# the module-scope import rule that makes the sys.modules gate sound
# --------------------------------------------------------------------------


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
    for path in sorted(Path(__file__).parent.glob("test_*.py")):
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
    from tests.conftest import close_serve_app_pools

    app_module = importlib.import_module(SERVE_APP_MODULE)
    monkeypatch.setattr(app_module, POOL_SEAM_ATTR, FakePool)

    gen = close_serve_app_pools.__wrapped__(monkeypatch)
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
    """A run collecting no serve test must not pay for importing the module."""
    from tests.conftest import close_serve_app_pools

    monkeypatch.delitem(sys.modules, SERVE_APP_MODULE, raising=False)

    gen = close_serve_app_pools.__wrapped__(monkeypatch)
    assert next(gen) == []
    with pytest.raises(StopIteration):
        next(gen)


def test_create_app_registers_its_pool_with_the_running_fixture(
    db_dsn: str, close_serve_app_pools: list
) -> None:
    """End to end against the real `create_app`: the seam is live during a test."""
    app_module = importlib.import_module(SERVE_APP_MODULE)
    app = app_module.create_app(db_dsn=db_dsn, searcher=None)

    assert app.state.pool in close_serve_app_pools


def test_a_lifespan_run_closes_the_pool_before_the_fixture_sees_it(
    db_dsn: str, close_serve_app_pools: list
) -> None:
    """`with TestClient(app)` is already correct; the fixture must find nothing."""
    from fastapi.testclient import TestClient

    app_module = importlib.import_module(SERVE_APP_MODULE)
    app = app_module.create_app(db_dsn=db_dsn, searcher=None)
    with TestClient(app):
        pass

    assert app.state.pool in close_serve_app_pools
    assert unclosed(close_serve_app_pools) == []


def test_every_test_module_is_scanned_by_the_import_rule() -> None:
    """Guard the guard: the glob above must actually match this suite.

    A typo'd pattern makes the offender scan pass over an empty set, which
    reports every future function-local import as compliant. Named files
    rather than a count threshold, so the pin says what it depends on.
    """
    scanned = {path.name for path in Path(__file__).parent.glob("test_*.py")}

    assert Path(__file__).name in scanned
    # A file that really does import the module the scan is about.
    assert "test_serve_app_baseline.py" in scanned


def test_the_scanner_parses_every_test_module_it_claims_to_scan() -> None:
    """`ast.parse` must not raise on any collected module, or the scan is blind."""
    for path in sorted(Path(__file__).parent.glob("test_*.py")):
        ast.parse(path.read_text(), filename=path.name)
