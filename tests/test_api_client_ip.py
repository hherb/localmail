# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Unit tests for resolve_client_ip — pure-Python, no DB, no FastAPI.

Coverage matrix matches T1–T16 in
docs/superpowers/specs/2026-05-21-trust-proxy-headers-design.md.
"""
from __future__ import annotations

from ipaddress import ip_network

import pytest

from localmail.api.client_ip import resolve_client_ip

LOOPBACK = (ip_network("127.0.0.0/8"),)
PRIVATE_LAN = (ip_network("10.0.0.0/8"), ip_network("127.0.0.0/8"))


def test_t1_socket_peer_none_returns_none() -> None:
    """T1: missing socket peer (e.g. unix socket) → None."""
    assert (
        resolve_client_ip(
            socket_peer=None,
            xff_header="1.2.3.4",
            trusted_proxies=LOOPBACK,
            max_hops=3,
        )
        is None
    )


def test_t2_empty_trusted_proxies_returns_socket_peer() -> None:
    """T2: knob disabled — always return socket peer, never look at XFF."""
    assert (
        resolve_client_ip(
            socket_peer="9.9.9.9",
            xff_header="1.2.3.4, 5.6.7.8",
            trusted_proxies=(),
            max_hops=3,
        )
        == "9.9.9.9"
    )


def test_t3_untrusted_peer_with_xff_is_quiet(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """T3: spoofed XFF on a direct connection → socket peer + NO log."""
    caplog.set_level("WARNING", logger="localmail.serve")
    result = resolve_client_ip(
        socket_peer="9.9.9.9",
        xff_header="1.2.3.4",
        trusted_proxies=LOOPBACK,
        max_hops=3,
    )
    assert result == "9.9.9.9"
    assert caplog.records == []


def test_t4_trusted_peer_no_xff_header() -> None:
    """T4: trusted proxy, no XFF header — nothing to peel."""
    assert (
        resolve_client_ip(
            socket_peer="127.0.0.1",
            xff_header=None,
            trusted_proxies=LOOPBACK,
            max_hops=3,
        )
        == "127.0.0.1"
    )


def test_t5_trusted_peer_empty_xff_header() -> None:
    """T5: header present but empty string."""
    assert (
        resolve_client_ip(
            socket_peer="127.0.0.1",
            xff_header="",
            trusted_proxies=LOOPBACK,
            max_hops=3,
        )
        == "127.0.0.1"
    )


def test_t6_one_hop_chain_returns_client() -> None:
    """T6: single-hop chain — XFF is just the client IP."""
    assert (
        resolve_client_ip(
            socket_peer="10.0.0.1",
            xff_header="203.0.113.7",
            trusted_proxies=PRIVATE_LAN,
            max_hops=3,
        )
        == "203.0.113.7"
    )


def test_t7_two_hop_peels_one_trusted() -> None:
    """T7: right-to-left peel — strip rightmost trusted, return client."""
    assert (
        resolve_client_ip(
            socket_peer="10.0.0.5",
            xff_header="203.0.113.7, 10.0.0.5",
            trusted_proxies=PRIVATE_LAN,
            max_hops=3,
        )
        == "203.0.113.7"
    )


def test_t8_three_hop_peels_two_trusted() -> None:
    """T8: client → p1 → p2 → app. Both p1 and p2 trusted, peel both."""
    assert (
        resolve_client_ip(
            socket_peer="127.0.0.1",
            xff_header="203.0.113.7, 10.0.0.5, 127.0.0.1",
            trusted_proxies=PRIVATE_LAN,
            max_hops=3,
        )
        == "203.0.113.7"
    )


def test_t9_all_entries_trusted_returns_socket_peer() -> None:
    """T9: every XFF entry is itself a trusted proxy — fall back."""
    assert (
        resolve_client_ip(
            socket_peer="127.0.0.1",
            xff_header="10.0.0.5, 127.0.0.1",
            trusted_proxies=PRIVATE_LAN,
            max_hops=3,
        )
        == "127.0.0.1"
    )


def test_t10_unparseable_entry_logs_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """T10: garbled entry encountered during peel → WARNING + socket peer."""
    caplog.set_level("WARNING", logger="localmail.serve")
    result = resolve_client_ip(
        socket_peer="127.0.0.1",
        xff_header="203.0.113.7, garbage, 127.0.0.1",
        trusted_proxies=LOOPBACK,
        max_hops=3,
    )
    assert result == "127.0.0.1"
    assert any("unparseable" in r.getMessage().lower() for r in caplog.records)


def test_t11_ipv4_with_port_suffix_stripped() -> None:
    """T11: 'IP:port' form — strip trailing :port for IPv4."""
    assert (
        resolve_client_ip(
            socket_peer="127.0.0.1",
            xff_header="203.0.113.7:54321",
            trusted_proxies=LOOPBACK,
            max_hops=3,
        )
        == "203.0.113.7"
    )


def test_t12_ipv6_bracketed_with_port() -> None:
    """T12: bracketed v6 + port → unwrap brackets, drop port."""
    assert (
        resolve_client_ip(
            socket_peer="127.0.0.1",
            xff_header="[2001:db8::1]:443",
            trusted_proxies=LOOPBACK,
            max_hops=3,
        )
        == "2001:db8::1"
    )


def test_t13_ipv6_zone_id_stripped() -> None:
    """T13: link-local zone ID — strip %zone."""
    assert (
        resolve_client_ip(
            socket_peer="127.0.0.1",
            xff_header="fe80::1%eth0",
            trusted_proxies=LOOPBACK,
            max_hops=3,
        )
        == "fe80::1"
    )


def test_t14_bare_ipv6_not_port_stripped() -> None:
    """T14: unbracketed IPv6 — colons must NOT trigger port-strip."""
    assert (
        resolve_client_ip(
            socket_peer="127.0.0.1",
            xff_header="2001:db8::1",
            trusted_proxies=LOOPBACK,
            max_hops=3,
        )
        == "2001:db8::1"
    )


def test_t15_max_hops_exceeded_logs_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """T15: max_hops=1, both XFF entries trusted → cap WARNING + socket peer."""
    caplog.set_level("WARNING", logger="localmail.serve")
    result = resolve_client_ip(
        socket_peer="10.0.0.5",
        xff_header="10.0.0.1, 10.0.0.5",
        trusted_proxies=PRIVATE_LAN,
        max_hops=1,
    )
    assert result == "10.0.0.5"
    assert any("max_hops" in r.getMessage().lower() for r in caplog.records)


def test_t16_mixed_v4_v6_trusted_set() -> None:
    """T16: mixed-family trusted set; resolves both v4 and v6 chains."""
    trusted = (ip_network("10.0.0.0/8"), ip_network("fd00::/8"))
    assert (
        resolve_client_ip(
            socket_peer="10.0.0.1",
            xff_header="203.0.113.7",
            trusted_proxies=trusted,
            max_hops=3,
        )
        == "203.0.113.7"
    )
    assert (
        resolve_client_ip(
            socket_peer="fd00::1",
            xff_header="2001:db8::abcd",
            trusted_proxies=trusted,
            max_hops=3,
        )
        == "2001:db8::abcd"
    )
