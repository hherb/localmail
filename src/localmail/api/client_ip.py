# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Pure helper: derive the originating client IP from a socket peer plus
an X-Forwarded-For header, applying right-to-left peeling against an
operator-configured trusted-proxy CIDR set.

Transport-free — no FastAPI, no HTTP imports. Reusable from any future
caller (MCP, etc.) that obtains its client-address context differently.

Threat model + eight-step algorithm:
docs/superpowers/specs/2026-05-21-trust-proxy-headers-design.md
"""
from __future__ import annotations

import ipaddress
import logging
from ipaddress import IPv4Address, IPv4Network, IPv6Address, IPv6Network

logger = logging.getLogger("localmail.serve")

TrustedProxies = tuple[IPv4Network | IPv6Network, ...]


def _normalise_xff_entry(raw: str) -> str | None:
    """Return a parseable IP string, or None if the entry is too mangled
    to use safely.

    Handles common in-the-wild forms:
      "192.0.2.1"          → "192.0.2.1"
      "192.0.2.1:54321"    → "192.0.2.1"          (port-strip on IPv4)
      "[2001:db8::1]:443"  → "2001:db8::1"        (bracketed v6 + port)
      "2001:db8::1"        → "2001:db8::1"        (bare v6 — colons preserved)
      "fe80::1%eth0"       → "fe80::1"            (zone-ID strip)
      ""                   → None
    """
    s = raw.strip()
    if not s:
        return None
    if s.startswith("["):
        end = s.find("]")
        if end == -1:
            return None
        s = s[1:end]
    elif s.count(":") == 1:
        host, _, port = s.rpartition(":")
        if port.isdigit() and host:
            s = host
    if "%" in s:
        s = s.split("%", 1)[0]
    return s or None


def _in_trusted(
    ip: IPv4Address | IPv6Address, cidrs: TrustedProxies
) -> bool:
    return any(ip in net for net in cidrs)


def _redact_xff_preview(entries: list[str]) -> tuple[str, str]:
    if not entries:
        return ("", "")
    return (entries[0], entries[-1])


def resolve_client_ip(
    socket_peer: str | None,
    xff_header: str | None,
    *,
    trusted_proxies: TrustedProxies,
    max_hops: int,
) -> str | None:
    """Return the originating client IP, honouring X-Forwarded-For only
    when the immediate socket peer is itself a trusted proxy.

    See docs/superpowers/specs/2026-05-21-trust-proxy-headers-design.md
    for the eight-step algorithm and threat model.
    """
    if socket_peer is None:
        return None
    if not trusted_proxies:
        return socket_peer
    try:
        peer_ip = ipaddress.ip_address(socket_peer)
    except ValueError:
        return socket_peer
    if not _in_trusted(peer_ip, trusted_proxies):
        return socket_peer
    if not xff_header:
        return socket_peer
    entries = [
        e for e in (s.strip() for s in xff_header.split(",")) if e
    ]
    if not entries:
        return socket_peer

    walked = 0
    for raw in reversed(entries):
        if walked >= max_hops:
            first, last = _redact_xff_preview(entries)
            logger.warning(
                "client IP resolver: max_hops=%d exceeded, falling back "
                "to socket peer (peer=%s, xff_len=%d, xff_first=%s, "
                "xff_last=%s)",
                max_hops, socket_peer, len(entries), first, last,
            )
            return socket_peer
        normalised = _normalise_xff_entry(raw)
        if normalised is None:
            first, _last = _redact_xff_preview(entries)
            logger.warning(
                "client IP resolver: unparseable XFF entry, falling back "
                "to socket peer (peer=%s, xff_len=%d, xff_first=%s, "
                "xff_last=<unparseable>)",
                socket_peer, len(entries), first,
            )
            return socket_peer
        try:
            entry_ip = ipaddress.ip_address(normalised)
        except ValueError:
            first, _last = _redact_xff_preview(entries)
            logger.warning(
                "client IP resolver: unparseable XFF entry, falling back "
                "to socket peer (peer=%s, xff_len=%d, xff_first=%s, "
                "xff_last=<unparseable>)",
                socket_peer, len(entries), first,
            )
            return socket_peer
        walked += 1
        if _in_trusted(entry_ip, trusted_proxies):
            continue
        return str(entry_ip)
    return socket_peer
