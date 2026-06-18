# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Invariant: no /v1/* machine route reads the admin session cookie (#121).

PR #118 widened the admin session cookie's Path from /admin to / so it
reaches the /v1/admin/* JSON routes. That is safe only as long as no
*machine* /v1/* route (i.e. /v1/* that is NOT /v1/admin/*) ever reads
the cookie — machine clients authenticate with `Authorization: Bearer …`.
This test walks the live FastAPI dependant tree and fails loudly if a
future patch adds cookie-reading to a machine endpoint, silently widening
the cookie-smuggling surface.
"""
from __future__ import annotations

import pytest
from fastapi.routing import APIRoute

from localmail.config import ServeConfig
from localmail.serve.admin.dependencies import SESSION_COOKIE_NAME
from localmail.serve.app import create_app


# Qualname of the closure returned by require_admin_session(); shared across
# every call because closures of one `def` share __qualname__.
# MAINTENANCE: this is a string match against an internal closure name. If the
# inner `_dep` function in serve/admin/dependencies.py:require_admin_session is
# ever renamed, update this constant — otherwise the dependency-detection half
# of the invariant goes silently vacuous (the cookie-name half still fires).
_ADMIN_SESSION_DEP_QUALNAME = "require_admin_session.<locals>._dep"


@pytest.fixture
def app(db_dsn):
    cfg = ServeConfig(
        session_signing_key="s" * 43,
        state_signing_key="t" * 43,
        oauth_callback_url="https://example.test/admin/oauth/callback",
        cookie_secure=False,
    )
    return create_app(db_dsn=db_dsn, serve_config=cfg)


def _walk_dependant(dependant):
    """Yield (cookie_param_names, dependency_qualnames) for the whole tree."""
    cookie_names = {p.alias or p.name for p in dependant.cookie_params}
    dep_qualnames = set()
    for sub in dependant.dependencies:
        if sub.call is not None:
            dep_qualnames.add(getattr(sub.call, "__qualname__", ""))
        sub_cookies, sub_deps = _walk_dependant(sub)
        cookie_names |= sub_cookies
        dep_qualnames |= sub_deps
    return cookie_names, dep_qualnames


def _machine_v1_routes(app):
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        path = route.path
        if path.startswith("/v1/") and not path.startswith("/v1/admin/"):
            yield route


def _admin_v1_routes(app):
    for route in app.routes:
        if isinstance(route, APIRoute) and route.path.startswith("/v1/admin/"):
            yield route


def test_machine_v1_routes_exist(app):
    """Guard the guard: if the walk finds nothing, the invariant is vacuous."""
    assert list(_machine_v1_routes(app)), "no /v1/ machine routes discovered"


def test_dep_detector_fires_on_admin_routes(app):
    """Guard the guard: prove the fragile detector string actually matches a
    live /v1/admin/* route. `_ADMIN_SESSION_DEP_QUALNAME` is a hand-written
    match against an internal closure name, so if the inner `_dep` in
    require_admin_session is renamed this fails loudly — otherwise the
    machine-route assertion's dependency half would pass vacuously.

    (The cookie-name half needs no such guard: admin routes read the cookie
    imperatively inside `_dep` via `request.cookies.get(...)`, not as a
    declared FastAPI Cookie param, so it never appears in `cookie_params`;
    and `SESSION_COOKIE_NAME` is imported, so it cannot drift out of sync.)"""
    dep_seen = any(
        _ADMIN_SESSION_DEP_QUALNAME in _walk_dependant(route.dependant)[1]
        for route in _admin_v1_routes(app)
    )
    assert dep_seen, (
        f"no /v1/admin/* route depends on {_ADMIN_SESSION_DEP_QUALNAME!r} — "
        "require_admin_session's closure was likely renamed"
    )


def test_no_machine_v1_route_reads_admin_session_cookie(app):
    offenders = []
    for route in _machine_v1_routes(app):
        cookies, deps = _walk_dependant(route.dependant)
        if SESSION_COOKIE_NAME in cookies:
            offenders.append(f"{route.path} reads cookie {SESSION_COOKIE_NAME!r}")
        if _ADMIN_SESSION_DEP_QUALNAME in deps:
            offenders.append(f"{route.path} depends on require_admin_session")
    assert not offenders, "machine /v1 routes must not read the admin cookie: " + \
        "; ".join(offenders)
