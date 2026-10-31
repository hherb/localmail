# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Every IMAP connection carries TCP keepalive shorter than the path's drop (#416).

Measured against Gmail with a silent IDLE: from the Mac 420 s survived and
540 s was reset at DONE; from the DGX 420 s was already reset; from both,
540 s with TCP keepalive survived. The daemon's
renewal DONE was the first byte after the silence, so every renewal crashed
the session with a traceback.
"""

from __future__ import annotations

import socket

from localmail import imap_client

from .test_sync import make_account

# The longest silence the probe saw survive (Mac only; the DGX's is shorter);
# keepalive must speak well before it.
_LONGEST_SURVIVING_SILENCE_S = 420


class _SocketClient:
    def __init__(self, **kw: object) -> None:
        self.normalise_times = True
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

    def socket(self) -> socket.socket:
        return self._sock

    def login(self, *a: object) -> None:
        pass

    def logout(self) -> None:
        self._sock.close()


def _idle_option() -> int:
    return getattr(socket, "TCP_KEEPIDLE", None) or socket.TCP_KEEPALIVE


def test_open_connection_turns_on_tcp_keepalive(monkeypatch, memory_keyring) -> None:
    from localmail import secrets

    secrets.set_password("acct", "pw")
    monkeypatch.setattr(imap_client, "IMAPClient", _SocketClient)
    with imap_client.open_connection(make_account()) as client:
        sock = client.socket()
        assert sock.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE) != 0
        idle = sock.getsockopt(socket.IPPROTO_TCP, _idle_option())
        interval = sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL)
    assert idle == imap_client.TCP_KEEPALIVE_IDLE_S
    assert interval == imap_client.TCP_KEEPALIVE_INTERVAL_S


def test_keepalive_speaks_well_inside_the_measured_drop() -> None:
    first_probe = imap_client.TCP_KEEPALIVE_IDLE_S
    assert first_probe + imap_client.TCP_KEEPALIVE_INTERVAL_S < _LONGEST_SURVIVING_SILENCE_S
