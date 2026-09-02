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
of the 38 affected files a closing fixture of its own.** Two reasons, and the
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
imported the function around it — which matters because every test module that
calls ``create_app`` binds it into its own namespace at import time, where a
patch cannot reach it.

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
#: :data:`DB_MODULE` is always already in ``sys.modules`` by the time a fixture
#: runs — both seams are always *importable*, which is not the property
#: :func:`loaded_seams` turns on — because ``tests/conftest.py`` imports
#: ``localmail.db`` at module scope for ``apply_migrations``.
#: :data:`SERVE_APP_MODULE` has no such guarantee, which is the whole reason
#: :func:`function_local_serve_app_imports` exists — and why the rule it
#: enforces names that module only.
POOL_SEAMS: tuple[tuple[str, str], ...] = (
    (SERVE_APP_MODULE, POOL_SEAM_ATTR),
    (DB_MODULE, POOL_SEAM_ATTR),
)


class Closable(Protocol):
    """The two members of ``ConnectionPool`` this module touches.

    ``closed`` is declared read-only because that is what ``ConnectionPool``
    actually exposes — a ``property``. Declared as a settable attribute the
    Protocol does not match the one class it exists to describe, and mypy says
    so ("expected settable variable, got read-only attribute"), which would
    make it wrong at the first call site that ever annotated a real pool.
    """

    @property
    def closed(self) -> bool: ...

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

    Every pool is closed even when one of them raises, and the first failure
    is re-raised afterwards. Aborting on the first would leave the rest open,
    and their finalisers then surface as a
    ``RuntimeError: cannot join current thread`` charged to some later,
    unrelated test — the exact misattribution this module exists to end,
    reintroduced by its own cleanup.

    A second and later failure is **attached to the first as a note**, not
    dropped: raising only the first would make this broad catch a silent one
    for every failure after it, and the note is what keeps the traceback an
    honest account of what the teardown actually hit.
    """
    leaked = unclosed(pools)
    failures: list[Exception] = []
    for pool in leaked:
        try:
            pool.close()
        except Exception as exc:  # noqa: BLE001 - re-raised below, never swallowed
            failures.append(exc)
    if failures:
        first, rest = failures[0], failures[1:]
        for other in rest:
            first.add_note(f"and closing a later pool also failed: {other!r}")
        raise first
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

    The rule is read from the AST rather than the text because
    ``tests/test_pool_leaks.py`` quotes the forbidden import verbatim, in the
    source strings it feeds this function as test cases. A substring scan
    flags those three lines; an AST walk correctly reads them as the string
    literals they are. (An earlier wording claimed *this* docstring quoted the
    import. It does not, and nothing else in this file does either — the
    reason is sound, the example was wrong.)

    **This is a belt to the fixture's braces, not the primary guard.** It
    cannot see ``importlib.import_module``, a ``__import__``, or a lazy import
    inside ``src/`` — `serve_cmd` has one — so the fixture re-checks at
    teardown that no declared seam arrived after it looked. See
    :func:`late_seam_error`.
    """
    tree = ast.parse(source, filename=filename)
    module_level = {id(node) for node in tree.body}
    return sorted(
        node.lineno
        for node in ast.walk(tree)
        if _imports_serve_app(node) and id(node) not in module_level
    )


def _imports_serve_app(node: ast.AST) -> TypeGuard[ast.Import | ast.ImportFrom]:
    """Whether ``node`` is an import of :data:`SERVE_APP_MODULE`.

    Both spellings count, because both put the module in ``sys.modules``:
    ``from localmail.serve.app import create_app`` and
    ``from localmail.serve import app``. Matching only the first is what let a
    function-local ``from localmail.serve import app as app_mod`` sit in
    ``test_daemon_supervisor_lifecycle.py`` while this scan reported the suite
    compliant.
    """
    if isinstance(node, ast.ImportFrom):
        if node.module == SERVE_APP_MODULE:
            return True
        parent, _, leaf = SERVE_APP_MODULE.rpartition(".")
        return node.module == parent and any(
            alias.name == leaf for alias in node.names
        )
    if isinstance(node, ast.Import):
        return any(alias.name == SERVE_APP_MODULE for alias in node.names)
    return False


def late_seam_error(
    modules: Mapping[str, Any],
    patched: Iterable[str],
    seams: Iterable[tuple[str, str]] = POOL_SEAMS,
) -> str | None:
    """Message naming a seam that arrived after the fixture looked, or ``None``.

    :func:`loaded_seams` skips a module absent from ``sys.modules`` at setup,
    on the inference that nothing can then build a pool through it. That
    inference is false in four distinct ways — a function-local
    ``from localmail.serve.app import …``, the sibling
    ``from localmail.serve import app``, an ``importlib.import_module``, and a
    lazy import inside ``src/`` (``cli.py``'s ``serve_cmd`` has one) — and only
    the first two are visible to
    :func:`function_local_serve_app_imports`, which cannot read production code
    at all.

    So the inference is **verified rather than trusted**: whatever route the
    module took, if a declared seam is loaded at teardown and was not patched
    at setup, every pool built through it during this test went unrecorded.
    Checking the outcome costs one set difference and covers the routes no
    scanner will ever enumerate — the standard this codebase applies to a
    guard whose failure is otherwise silent.
    """
    already = set(patched)
    late = sorted(name for name, _attr in seams if name in modules and name not in already)
    if not late:
        return None
    return (
        f"{', '.join(late)} was imported during this test, after the pool seam "
        f"was patched — every pool built through it leaked, unrecorded (#321). "
        f"Import it at module scope so the fixture can see it."
    )


def pool_constructor_calls(source: str, filename: str) -> list[tuple[str, int]]:
    """``(callee name, line)`` for every call in ``source`` that builds a pool.

    A callee whose name ends in ``ConnectionPool`` — so ``ConnectionPool(...)``,
    ``psycopg_pool.ConnectionPool(...)`` and ``AsyncConnectionPool(...)`` all
    count. Used to check :data:`POOL_SEAMS` against the modules that actually
    construct pools, which is the hole :func:`missing_seam_error` cannot see:
    it asks only whether the *name* is present, so a third module growing a
    pool, or an existing one switching to a fully-qualified call or to the
    async class, leaves the name intact, nothing patched, and no test failing.
    """
    tree = ast.parse(source, filename=filename)
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _callee_name(node.func)
        if name is not None and name.endswith(POOL_SEAM_ATTR):
            found.append((name, node.lineno))
    return sorted(found, key=lambda pair: pair[1])


def _callee_name(func: ast.expr) -> str | None:
    """The bare or dotted-leaf name a call expression resolves to."""
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None
