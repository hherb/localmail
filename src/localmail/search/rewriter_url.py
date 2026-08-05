# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Pure validation of a rewriter backend's base URL (#235).

The one rule for "can this config value be used as an HTTP base URL", shaped
like `account_names.py::account_name_error`: a message describing the problem,
or `None` when there is none. Callers own the wording around it — they know
which `[search]` setting the value came from.

Why it exists: a malformed base URL is a *permanent* mistake in `config.toml`,
but without this check it surfaces per-request as "could not reach the rewriter
service", which sends the operator to the network. `httpx.URL` is no help on
its own — it is permissive, and the common mistake of omitting the scheme
(`localhost:11434`) parses happily as `scheme='localhost'` rather than raising.
Only an unparseable port actually raises `InvalidURL`.
"""

from __future__ import annotations

import httpx

_SUPPORTED_SCHEMES = ("http", "https")


def base_url_error(value: str) -> str | None:
    """Return why `value` is unusable as an HTTP base URL, or None if it is fine.

    Checked in order: non-blank, parseable by httpx, an `http`/`https` scheme,
    and a non-empty host. The httpx parse is included so that a value passing
    this check cannot still raise `InvalidURL` at request time.
    """
    if not value.strip():
        return "must not be empty"
    try:
        url = httpx.URL(value)
    except httpx.InvalidURL as exc:
        return f"is not a valid URL ({exc})"
    if url.scheme not in _SUPPORTED_SCHEMES:
        return (
            f"has scheme {url.scheme!r}; it must start with http:// or https://"
        )
    if not url.host:
        return "has no host"
    return None
