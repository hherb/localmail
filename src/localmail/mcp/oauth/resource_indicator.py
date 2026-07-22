# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Pure RFC 8707 resource-indicator canonicalization + accept/reject decision.

No IO, no SDK import. `canonicalize_resource` implements the RFC 8707 §2 rules
(absolute http(s) URI, no fragment, lowercase scheme/host, drop default port,
strip a trailing slash). `decide_resource` is the accept/bind/reject table the
provider applies at /authorize.
"""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

_DEFAULT_PORTS = {"http": 80, "https": 443}


def canonicalize_resource(raw: str) -> str | None:
    """Return the canonical resource identifier, or None if `raw` is invalid."""
    try:
        parts = urlsplit(raw.strip())
    except ValueError:
        return None
    if parts.scheme not in _DEFAULT_PORTS or not parts.hostname or parts.fragment:
        return None
    host = parts.hostname  # already lowercased by urlsplit
    port = parts.port
    netloc = host
    if port is not None and port != _DEFAULT_PORTS[parts.scheme]:
        netloc = f"{host}:{port}"
    path = parts.path[:-1] if parts.path.endswith("/") else parts.path
    return urlunsplit((parts.scheme, netloc, path, parts.query, ""))


def resolve_accepted_resources(
    configured: list[str] | None, derived: str
) -> list[str]:
    """The accepted resource set: `configured or [derived]`, each canonicalized.

    Malformed entries are dropped. A non-None `configured` that canonicalizes to
    an empty list is a hard operator misconfiguration -> raise ValueError (the
    caller resolves this once at construction, so it surfaces at startup).
    """
    if configured:
        out = [c for c in (canonicalize_resource(x) for x in configured) if c]
        if not out:
            raise ValueError("resource_indicators has no valid entries")
        return out
    canon = canonicalize_resource(derived)
    assert canon is not None  # derived comes from mcp_resource_url — always valid
    return [canon]


@dataclass(frozen=True)
class ResourceDecision:
    ok: bool
    bound: str | None
    error: str | None


def decide_resource(
    requested: str | None, accepted: list[str], *, require: bool
) -> ResourceDecision:
    """Accept/bind/reject a requested resource against the accepted set."""
    if requested is None:
        if require:
            return ResourceDecision(False, None, "resource indicator is required")
        return ResourceDecision(True, accepted[0], None)
    canon = canonicalize_resource(requested)
    if canon is not None and canon in accepted:
        return ResourceDecision(True, canon, None)
    return ResourceDecision(False, None, "invalid or unknown resource indicator")
