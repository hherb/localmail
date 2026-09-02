# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Close the connection pools a test opens and never closes (#321).

``serve.app.create_app`` opens its pool eagerly (``open=True``) and closes it
only in the FastAPI lifespan's ``finally``. A test that never runs the
lifespan — a bare ``create_app(...)``, or ``TestClient(app)`` used without
``with`` — therefore leaks it. The pool holds its connections until the
garbage collector reaches it, and ``ConnectionPool.__del__`` then tries to
join the pool's own worker thread *from inside that thread*, raising
``RuntimeError: cannot join current thread``. Pytest reports that as a
``PytestUnraisableExceptionWarning`` against whichever unrelated test
happened to be running when the collection fired, which is why the warning
names a different set of files on every run and never names the leak site.

**The closing is done once, in an autouse fixture, rather than by giving each
of the 34 affected files a closing fixture of its own.** Two reasons, and the
first is the one this codebase keeps re-learning: a new inline
``create_app(...)`` written tomorrow silently reintroduces the leak, so a
per-file sweep buys discipline where the seam buys construction. The second
is that the sweep as issue #321 words it — wrap each call in
``with TestClient(...)`` — would break the tests that exist to assert
``create_app`` alone is side-effect-free, since running the lifespan is
exactly what binds the daemon control socket.

There are **two** seams, listed in :data:`POOL_SEAMS`, and both were needed:

* ``localmail.serve.app`` — the ``create_app`` pools above;
* ``localmail.db`` — the pools ``open_pool`` builds for ``Daemon`` and
  ``Searcher``. ``Daemon.stop()``/``join()`` do **not** close ``self.pool``, so
  13 daemon tests across four files plus one ``create_searcher`` test leaked one
  each. That half was invisible on macOS and reported on Linux/3.13 — the GC
  decides, so a platform can hide it entirely. Found by instrumenting
  ``open_pool``, not by reading the warning, which names the wrong file by
  construction.

Each seam is the name its own module resolves from its globals on **every**
call, so replacing it reaches every caller regardless of how that caller
imported the function around it — which matters because every test module binds
``create_app`` into its own namespace at import time, where a patch cannot
reach it.

Nothing here closes a pool that was already closed: that path is correct and
:func:`unclosed` filters it out, so the count :func:`close_pools` returns is
the number of pools that genuinely leaked.
"""
from __future__ import annotations

import ast
from collections.abc import Callable, Iterable, Mapping
from typing import Any, Protocol, TypeGuard, TypeVar

#: The module whose ``create_app`` opens a pool per app.
SERVE_APP_MODULE = "localmail.serve.app"

#: The module whose ``open_pool`` builds the ``Daemon`` and ``Searcher`` pools.
DB_MODULE = "localmail.db"

#: The name each module resolves from its own globals to build a pool.
#: Patching it is what makes every call site reachable.
POOL_SEAM_ATTR = "ConnectionPool"

#: Every ``(module name, attribute)`` a pool is built through.
#:
#: :data:`DB_MODULE` is always importable by the time a fixture runs, because
#: ``tests/conftest.py`` imports ``localmail.db`` at module scope for
#: ``apply_migrations``. :data:`SERVE_APP_MODULE` is not, which is the whole
#: reason :func:`function_local_serve_app_imports` exists — and why the rule it
#: enforces names that module only.
POOL_SEAMS: tuple[tuple[str, str], ...] = (
    (SERVE_APP_MODULE, POOL_SEAM_ATTR),
    (DB_MODULE, POOL_SEAM_ATTR),
)


class Closable(Protocol):
    """The two members of ``ConnectionPool`` this module touches."""

    closed: bool

    def close(self) -> None: ...


P = TypeVar("P")


def recording_factory(
    real_factory: Callable[..., P], sink: list[P]
) -> Callable[..., P]:
    """Wrap ``real_factory`` so each object it returns is appended to ``sink``.

    Transparent by construction: arguments pass through untouched and the
    caller receives the real object, so ``create_app`` is unaware it is being
    observed. A factory call that raises records nothing — a pool that was
    never built cannot leak, and listing it would make the teardown count lie.
    """
    def factory(*args: Any, **kwargs: Any) -> P:
        built = real_factory(*args, **kwargs)
        sink.append(built)
        return built

    return factory


def unclosed(pools: Iterable[Closable]) -> list[Closable]:
    """The pools that still need closing.

    A pool whose app ran its lifespan has already closed itself, and counting
    it would report leaks that do not exist.
    """
    return [pool for pool in pools if not pool.closed]


def close_pools(pools: Iterable[Closable]) -> int:
    """Close every still-open pool in ``pools``; return how many leaked.

    ``ConnectionPool.close`` is idempotent, so re-closing would be harmless —
    but the return value is a claim about what this fixture had to do, and
    filtering first is what keeps that claim true.
    """
    leaked = unclosed(pools)
    for pool in leaked:
        pool.close()
    return len(leaked)


def loaded_seams(
    modules: Mapping[str, Any], seams: Iterable[tuple[str, str]] = POOL_SEAMS
) -> list[tuple[str, Any, str]]:
    """The ``(name, module, attribute)`` seams already present in ``modules``.

    Pure over a ``sys.modules``-shaped mapping. A seam whose module has not
    been imported is skipped rather than imported: nothing can have built a
    pool through a module that does not exist yet, and importing
    ``localmail.serve.app`` speculatively costs ~0.5 s on every unit-only run.
    See :func:`function_local_serve_app_imports` for what keeps that inference
    true.
    """
    found = []
    for name, attr in seams:
        module = modules.get(name)
        if module is not None:
            found.append((name, module, attr))
    return found


def missing_seam_error(module: object, module_name: str, attr: str) -> str | None:
    """Message naming a broken pool seam, or ``None`` when it is intact.

    Shaped like ``account_names.account_name_error``: the rule answers, the
    caller decides what an error is.

    The failure this catches is an aliased import in the seam's module
    (``from psycopg_pool import ConnectionPool as Pool``). The attribute then
    does not exist, nothing patches, every pool leaks again — and no test
    fails, because closing a pool that was never recorded is a no-op. A guard
    that can go quietly inert is the shape this whole file exists to remove,
    so it reports rather than skips.

    It deliberately does **not** check the seam's identity. Swapping in a
    different pool class under the same name is a legitimate change, and the
    wrapper would keep recording and closing it correctly.
    """
    if not hasattr(module, attr):
        return (
            f"{module_name} has no attribute {attr!r}, so the autouse "
            f"pool-closing fixture cannot record the pools it builds and every "
            f"one of them leaks (#321). If the import was aliased, restore the "
            f"plain name or update POOL_SEAMS in tests/_pool_leaks.py."
        )
    return None


def function_local_serve_app_imports(source: str, filename: str) -> list[int]:
    """Line numbers where ``source`` imports :data:`SERVE_APP_MODULE` below
    module scope.

    The autouse fixture reads ``sys.modules`` at test-setup time and does
    nothing when the module is absent — which keeps a run that collects no
    serve test from paying half a second to import FastAPI. That gate is
    sound only because pytest imports every collected module before running
    any test, so a **module-level** import is always in place in time. A
    function-local one never is: the module appears mid-test, after the
    fixture has already decided there was nothing to patch, and that file's
    pools leak with nothing reporting it.

    The rule is read from the AST rather than the text because a docstring or
    a comment quoting the import — this one does — is prose, not code.
    """
    tree = ast.parse(source, filename=filename)
    module_level = {id(node) for node in tree.body}
    return sorted(
        node.lineno
        for node in ast.walk(tree)
        if _imports_serve_app(node) and id(node) not in module_level
    )


def _imports_serve_app(node: ast.AST) -> TypeGuard[ast.Import | ast.ImportFrom]:
    """Whether ``node`` is an import of :data:`SERVE_APP_MODULE`."""
    if isinstance(node, ast.ImportFrom):
        return node.module == SERVE_APP_MODULE
    if isinstance(node, ast.Import):
        return any(alias.name == SERVE_APP_MODULE for alias in node.names)
    return False
