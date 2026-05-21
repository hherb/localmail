# `trusted_proxies` — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Recover the originating client IP from `X-Forwarded-For` (when an operator-configured trusted-proxy CIDR set admits it) so the Postgres-backed login rate limiter's per-IP cap functions correctly behind a reverse proxy.

**Architecture:** New pure module `src/localmail/api/client_ip.py` exposing `resolve_client_ip(socket_peer, xff_header, *, trusted_proxies, max_hops)` — eight-step right-to-left peel, transport-free. Two new fields on `AuthConfig`. One call-site change in `serve/routes/auth.py`. No middleware, no migration, no schema change. Empty default preserves current behaviour exactly.

**Tech Stack:** Python ≥ 3.12, `ipaddress` (stdlib), `pydantic` v2 (already used), `fastapi.testclient` (tests).

**Spec:** [`docs/superpowers/specs/2026-05-21-trust-proxy-headers-design.md`](../specs/2026-05-21-trust-proxy-headers-design.md)

**Branch:** `feat/auth-trusted-proxies` (already created off `main` at `2d8debc`, with the spec commit).

---

## Pre-flight

- [ ] **Pre-1: Verify the working tree is on the right branch.**

```bash
cd /Users/hherb/src/localmail
git status -s -b
```

Expected output starts with `## feat/auth-trusted-proxies`. Working tree should be clean except for the untracked `.claude/settings.local.json`. If you are on `main`: `git checkout feat/auth-trusted-proxies`.

- [ ] **Pre-2: Verify baseline test suite is green before any changes.**

```bash
unset VIRTUAL_ENV && uv run pytest -q
```

Expected: `731 passed` (baseline at commit `2d8debc`). If anything fails, stop and investigate — do not start implementing on a broken baseline.

---

## File map

```
NEW   src/localmail/api/client_ip.py
NEW   tests/test_api_client_ip.py

MOD   src/localmail/config.py
MOD   tests/test_config.py

MOD   src/localmail/serve/routes/auth.py
MOD   tests/test_api_auth_rate_limiter.py

MOD   config.example.toml
MOD   README.md
MOD   CLAUDE.md
```

---

## Task 1: Pure resolver module (`client_ip.py`)

**Files:**
- Create: `src/localmail/api/client_ip.py`
- Test: `tests/test_api_client_ip.py`

Implements steps 1–8 of the spec algorithm. No FastAPI, no DB, no IO.

### Step 1.1: Write the failing test file

- [ ] **Step 1.1:** Create `tests/test_api_client_ip.py` with the 16 unit cases (T1–T16 in the spec).

```python
"""Unit tests for resolve_client_ip — pure-Python, no DB, no FastAPI.

Coverage matrix matches T1–T16 in
docs/superpowers/specs/2026-05-21-trust-proxy-headers-design.md.
"""
from __future__ import annotations

from ipaddress import IPv4Network, ip_network

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


def test_host_form_cidr_round_trip_is_parseable() -> None:
    """Sanity: host-form `/32` networks (the shape config.py emits via
    strict=False) accept their own host as a member — protects against
    accidentally storing the network address instead of the host."""
    n = ip_network("10.0.0.5", strict=False)
    assert IPv4Network("10.0.0.5/32") == n
```

- [ ] **Step 1.2: Run the failing test.**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_api_client_ip.py -v
```

Expected: collection error / `ModuleNotFoundError: No module named 'localmail.api.client_ip'`. This is the expected red — proceed.

### Step 1.3: Implement the resolver

- [ ] **Step 1.3:** Create `src/localmail/api/client_ip.py` with exactly this content:

```python
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
```

- [ ] **Step 1.4: Run the test to verify it passes.**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_api_client_ip.py -v
```

Expected: `17 passed` (16 T-cases + the host-form sanity test).

- [ ] **Step 1.5: Run mypy on the new module.**

```bash
unset VIRTUAL_ENV && uv run mypy src/localmail/api/client_ip.py
```

Expected: `Success: no issues found in 1 source file`. If mypy reports anything new, fix it before committing — the pure-helper modules in this repo are expected to be mypy-clean.

- [ ] **Step 1.6: Commit Task 1.**

```bash
git add src/localmail/api/client_ip.py tests/test_api_client_ip.py
git commit -m "$(cat <<'EOF'
feat(api): resolve_client_ip — XFF peel against trusted proxies

Pure module localmail.api.client_ip exposes resolve_client_ip(socket_peer,
xff_header, *, trusted_proxies, max_hops). Eight-step right-to-left peel
matching nginx set_real_ip_from / Caddy trusted_proxies semantics. No
FastAPI imports — transport-free so future MCP and other callers can
reuse it.

16 T-case unit tests cover: knob disabled, untrusted peer (quiet),
one/two/three-hop chains, all-trusted fallback, unparseable XFF entry
(logs + falls back), IPv4 with port suffix, bracketed/bare IPv6, zone
ID strip, max_hops exceeded (logs + falls back), mixed v4/v6 trusted
set.

Spec: docs/superpowers/specs/2026-05-21-trust-proxy-headers-design.md

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: AuthConfig fields + pydantic validators

**Files:**
- Modify: `src/localmail/config.py` (`AuthConfig` and its imports)
- Test: `tests/test_config.py` (extend with five new cases)

### Step 2.1: Write the failing config tests

- [ ] **Step 2.1:** Append five new tests to `tests/test_config.py`. Use the existing imports at the top of the file (`AuthConfig`, `ValidationError` from pydantic) and add the missing one (`IPv4Network`). Add this block to the END of `tests/test_config.py`:

```python
def test_auth_trusted_proxies_default_empty() -> None:
    """Default empty list preserves current behaviour exactly."""
    cfg = AuthConfig()
    assert cfg.trusted_proxies == []
    assert cfg.trusted_proxies_parsed == ()
    assert cfg.trusted_proxies_max_hops == 3


def test_auth_trusted_proxies_host_form_becomes_single_host_network() -> None:
    """strict=False means a bare IP becomes a /32 (or /128 for v6)."""
    from ipaddress import IPv4Network
    cfg = AuthConfig(trusted_proxies=["10.0.0.5"])
    assert IPv4Network("10.0.0.5/32") in cfg.trusted_proxies_parsed


def test_auth_trusted_proxies_cidr_form_parses() -> None:
    """Explicit CIDR is parsed as-is."""
    from ipaddress import IPv4Network
    cfg = AuthConfig(trusted_proxies=["127.0.0.0/8"])
    assert IPv4Network("127.0.0.0/8") in cfg.trusted_proxies_parsed


def test_auth_trusted_proxies_bad_cidr_raises() -> None:
    """Unparseable CIDR fails LOUD at config load."""
    with pytest.raises(ValidationError):
        AuthConfig(trusted_proxies=["not-a-cidr"])


def test_auth_trusted_proxies_max_hops_zero_raises() -> None:
    """max_hops=0 is a footgun (silently disables peel) — reject."""
    with pytest.raises(ValidationError):
        AuthConfig(trusted_proxies_max_hops=0)


def test_auth_trusted_proxies_max_hops_too_high_raises() -> None:
    """max_hops > 10 has no realistic use — reject as sanity bound."""
    with pytest.raises(ValidationError):
        AuthConfig(trusted_proxies_max_hops=11)
```

- [ ] **Step 2.2: Run the failing tests.**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_config.py -v -k trusted_proxies
```

Expected: each test fails with `AttributeError: 'AuthConfig' object has no attribute 'trusted_proxies'` or similar. This is the expected red — proceed.

### Step 2.3: Add the config fields

- [ ] **Step 2.3:** Modify `src/localmail/config.py`. Two changes:

**Change A — top-of-file imports** ([src/localmail/config.py:10](src/localmail/config.py#L10)). Replace the existing line:

```python
from pydantic import BaseModel, Field, field_validator
```

with:

```python
from ipaddress import IPv4Network, IPv6Network, ip_network

from pydantic import BaseModel, Field, PrivateAttr, field_validator

TrustedProxies = tuple[IPv4Network | IPv6Network, ...]
```

**Change B — extend `AuthConfig`** ([src/localmail/config.py:49-68](src/localmail/config.py#L49-L68)). After the existing `login_cleanup_interval_s: int = 300` line, before the next class, add these fields and methods inside `AuthConfig`:

```python
    # Reverse-proxy support for the login rate limiter. Empty (default) =
    # historic behaviour: client_ip is the socket peer (request.client.host).
    # When non-empty, an X-Forwarded-For header is peeled right-to-left,
    # skipping entries in trusted_proxies, to find the originating client.
    # The same list governs both:
    #   (a) admission: is the immediate socket peer a trusted proxy?
    #   (b) peeling:   which XFF entries are proxies vs the client?
    # See docs/superpowers/specs/2026-05-21-trust-proxy-headers-design.md
    trusted_proxies: list[str] = Field(default_factory=list)

    # Hard cap on XFF entries we walk before giving up — bounds CPU on an
    # attacker-supplied giant XFF header and bounds the chain depth we
    # claim to support. Three is enough for client → CDN → ALB → app.
    trusted_proxies_max_hops: int = Field(default=3, ge=1, le=10)

    _trusted_proxies_parsed: TrustedProxies = PrivateAttr(default=())

    @field_validator("trusted_proxies")
    @classmethod
    def _validate_trusted_proxies(cls, v: list[str]) -> list[str]:
        # Parse-and-discard: fail LOUD at config-load on a bad CIDR.
        # The real parse runs once in model_post_init and is read by
        # trusted_proxies_parsed.
        for s in v:
            ip_network(s, strict=False)
        return v

    def model_post_init(self, __context: object) -> None:
        # PrivateAttr assignment via object.__setattr__ works regardless
        # of any future model_config = ConfigDict(frozen=...) change.
        object.__setattr__(
            self,
            "_trusted_proxies_parsed",
            tuple(
                ip_network(s, strict=False) for s in self.trusted_proxies
            ),
        )

    @property
    def trusted_proxies_parsed(self) -> TrustedProxies:
        return self._trusted_proxies_parsed
```

- [ ] **Step 2.4: Run the config tests to verify they pass.**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_config.py -v -k trusted_proxies
```

Expected: `6 passed`.

- [ ] **Step 2.5: Run the full config test suite to check nothing else broke.**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_config.py -v
```

Expected: all existing tests still pass alongside the six new ones.

- [ ] **Step 2.6: Run mypy on the touched module.**

```bash
unset VIRTUAL_ENV && uv run mypy src/localmail/config.py
```

Expected: no new errors. (If mypy complains about `__context: object` in `model_post_init`, change to `__context: Any` and add `from typing import Any` — pydantic v2 docs use `Any`.)

- [ ] **Step 2.7: Commit Task 2.**

```bash
git add src/localmail/config.py tests/test_config.py
git commit -m "$(cat <<'EOF'
feat(config): AuthConfig.trusted_proxies + trusted_proxies_max_hops

Two new optional fields on AuthConfig. Empty defaults preserve current
behaviour exactly; the new fields are only meaningful when an operator
explicitly opts in. Pydantic validators fail LOUD at config load on a
bad CIDR or out-of-range max_hops (ge=1 to block the silent-disable
footgun, le=10 as a sanity bound).

CIDR parsing uses strict=False so operators can write either host form
("10.0.0.5") or explicit CIDR ("10.0.0.0/8"). The parsed
tuple[IPv4Network | IPv6Network, ...] lives on a PrivateAttr populated
in model_post_init so the per-request cost in the login route is a
single property read.

Spec: docs/superpowers/specs/2026-05-21-trust-proxy-headers-design.md

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Wire the resolver into the `/login` route

**Files:**
- Modify: `src/localmail/serve/routes/auth.py` (only the `login()` body)
- Test: `tests/test_api_auth_rate_limiter.py` (append one end-to-end test)

### Step 3.1: Write the failing end-to-end test

- [ ] **Step 3.1:** Append this test to the END of `tests/test_api_auth_rate_limiter.py`. It needs the existing `db_dsn` and `api_user` fixtures (already in scope) and the FastAPI TestClient.

```python
def test_per_ip_cap_uses_xff_when_trusted(db_dsn: str, api_user) -> None:
    """Behind a trusted proxy, the per-IP cap reads X-Forwarded-For.

    Drives 5 failures from 5 distinct public IPs — none trips the per-IP
    cap because each IP only fails once. Then drives 3 failures from one
    shared IP under the cap; the 4th from the same IP must trip 429.
    """
    from fastapi.testclient import TestClient

    from localmail.config import AuthConfig
    from localmail.serve.app import create_app

    cfg = AuthConfig(
        trusted_proxies=["127.0.0.0/8"],
        login_per_ip_max=3,
        login_per_ip_window_s=60,
        # Set the other caps high so they don't trip first.
        login_per_user_max=100,
        login_global_max=100,
    )
    app = create_app(db_dsn=db_dsn, searcher=None, auth_config=cfg)
    # TestClient default socket peer is ("testclient", 50000); override so
    # the resolver sees 127.0.0.1 as a trusted proxy.
    c = TestClient(app, client=("127.0.0.1", 50000))

    for i in range(5):
        r = c.post(
            "/v1/auth/login",
            json={"username": api_user.username, "password": "wrong"},
            headers={"X-Forwarded-For": f"203.0.113.{i + 1}"},
        )
        assert r.status_code == 401, (
            f"distinct-IP failure {i} unexpectedly returned {r.status_code}"
        )

    # 3 failures from a single shared XFF (under cap of 3).
    for i in range(3):
        r = c.post(
            "/v1/auth/login",
            json={"username": api_user.username, "password": "wrong"},
            headers={"X-Forwarded-For": "198.51.100.42"},
        )
        assert r.status_code == 401, (
            f"shared-IP failure {i + 1} unexpectedly returned {r.status_code}"
        )

    # 4th from the same shared XFF — trip per-IP cap.
    r = c.post(
        "/v1/auth/login",
        json={"username": api_user.username, "password": "wrong"},
        headers={"X-Forwarded-For": "198.51.100.42"},
    )
    assert r.status_code == 429, (
        f"expected 429 on 4th same-IP failure, got {r.status_code}"
    )
    assert r.json()["cap"] == "ip", r.json()
```

- [ ] **Step 3.2: Run the failing test.**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_api_auth_rate_limiter.py::test_per_ip_cap_uses_xff_when_trusted -v
```

Expected: `AssertionError: expected 429 on 4th same-IP failure, got 401`. (Because without the route change, every login sees `client_ip="127.0.0.1"` — the per-IP cap trips on attempt 4 of the FIRST loop already, so the test may also fail at "distinct-IP failure 3 unexpectedly returned 429". Either failure shape is the expected red.) Proceed.

### Step 3.3: Wire the resolver into `routes/auth.py`

- [ ] **Step 3.3:** Modify `src/localmail/serve/routes/auth.py`. Two changes:

**Change A — imports** at the top of the file. After the existing imports add:

```python
from localmail.api.client_ip import resolve_client_ip
```

**Change B — `login()` body** ([src/localmail/serve/routes/auth.py:33-43](src/localmail/serve/routes/auth.py#L33-L43)). Replace the existing function with:

```python
@router.post("/login", response_model=TokenResponse)
def login(req: LoginRequest, request: Request) -> TokenResponse:
    pool = request.app.state.pool
    cfg = request.app.state.auth_config
    client_ip = resolve_client_ip(
        socket_peer=request.client.host if request.client else None,
        xff_header=request.headers.get("X-Forwarded-For"),
        trusted_proxies=cfg.trusted_proxies_parsed,
        max_hops=cfg.trusted_proxies_max_hops,
    )
    with pool.connection() as conn:
        token, expires_at = auth_svc.login(
            conn, req.username, req.password, client_ip=client_ip, cfg=cfg
        )
        conn.commit()
    return TokenResponse(token=token, expires_at=expires_at.isoformat())
```

- [ ] **Step 3.4: Run the end-to-end test to verify it passes.**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_api_auth_rate_limiter.py::test_per_ip_cap_uses_xff_when_trusted -v
```

Expected: `1 passed`.

- [ ] **Step 3.5: Run the full rate-limiter suite to check nothing else broke.**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_api_auth_rate_limiter.py -v
```

Expected: all pre-existing rate-limiter tests still pass plus the new one. (Existing tests don't configure `trusted_proxies`, so the route still uses the socket peer — backward compatible.)

- [ ] **Step 3.6: Run the auth-routes suite (the existing happy-path tests use TestClient too).**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_serve_auth_routes.py -v
```

Expected: all pass.

- [ ] **Step 3.7: Commit Task 3.**

```bash
git add src/localmail/serve/routes/auth.py tests/test_api_auth_rate_limiter.py
git commit -m "$(cat <<'EOF'
feat(serve): /v1/auth/login resolves client IP via trusted_proxies

The login route now calls localmail.api.client_ip.resolve_client_ip
with the socket peer + the X-Forwarded-For header + the configured
trusted-proxy CIDR set before invoking auth_svc.login. With the default
empty trusted_proxies the resolver returns the socket peer unchanged,
so this is a pure no-op for any operator who has not opted in.

New end-to-end test in tests/test_api_auth_rate_limiter.py drives 5
failed logins from distinct XFF values (no per-IP cap trips) then 4
from a shared XFF (4th trips 429 with cap=ip).

Closes the gotcha documented in CLAUDE.md after PR #69.

Spec: docs/superpowers/specs/2026-05-21-trust-proxy-headers-design.md

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Documentation updates

**Files:**
- Modify: `config.example.toml`
- Modify: `README.md`
- Modify: `CLAUDE.md`

No new tests in this task — docs verification is by inspection plus the next task's smoke check that the example config still loads.

### Step 4.1: Update `config.example.toml`

- [ ] **Step 4.1:** Modify [`config.example.toml`](config.example.toml). Append these lines to the END of the existing `[auth]` block (after `login_cleanup_interval_s = 300     # 5m`):

```toml

# Reverse-proxy support. Empty (default) = use the socket peer.
# When set, X-Forwarded-For is peeled right-to-left, skipping entries
# in trusted_proxies, to find the originating client. See README.
# trusted_proxies = ["127.0.0.0/8"]                 # same-host proxy
# trusted_proxies = ["10.0.0.0/8", "127.0.0.0/8"]   # private-LAN proxy
# trusted_proxies = ["173.245.48.0/20", "103.21.244.0/22"]  # Cloudflare ranges
# trusted_proxies_max_hops = 3
```

### Step 4.2: Update `README.md`

- [ ] **Step 4.2:** Modify [`README.md`](README.md). Two changes:

**Change A — replace the stale "until `auth.trust_proxy_headers` lands" blockquote** at [`README.md:255-259`](README.md#L255-L259). Find:

```markdown
> The three login-rate-limit caps (global / per-IP / per-user) are
> Postgres-backed, so they survive `localmail serve` restarts and apply
> consistently across `uvicorn --workers N`. Behind a reverse proxy the
> per-IP cap is not effective until `auth.trust_proxy_headers` lands
> (see issue tracker) — bump `login_global_max` to compensate.
```

Replace with:

```markdown
> The three login-rate-limit caps (global / per-IP / per-user) are
> Postgres-backed, so they survive `localmail serve` restarts and apply
> consistently across `uvicorn --workers N`. Behind a reverse proxy,
> configure `auth.trusted_proxies` (see below) so the per-IP cap reads
> the real client from `X-Forwarded-For` instead of the proxy's IP.
```

**Change B — add a new "Behind a reverse proxy" subsection** immediately after the blockquote you just edited (before the existing `### Browse & search pagination` heading at `README.md:261`). Insert:

````markdown

### Behind a reverse proxy

The login rate limiter has separate global, per-IP, and per-user caps.
Behind a reverse proxy, `request.client.host` is the proxy's address —
not the real client — so every login appears to come from the proxy
and the per-IP cap collapses into a copy of the global cap.

Configure `auth.trusted_proxies` (a list of CIDRs) to recover the real
client IP from `X-Forwarded-For`. The list governs both admission ("is
this socket peer a trusted proxy?") and peeling ("which XFF entries
are proxies vs the client?"). Right-to-left peel of XFF — identical
to nginx's `set_real_ip_from` / Caddy's `trusted_proxies` semantics.

```toml
[auth]
# Same-host nginx/Caddy/Traefik fronting localmail serve on 127.0.0.1:
trusted_proxies = ["127.0.0.0/8"]

# Reverse proxy on a separate host in a private LAN:
# trusted_proxies = ["10.0.0.0/8", "127.0.0.0/8"]

# Behind Cloudflare (list every current Cloudflare range):
# trusted_proxies = ["173.245.48.0/20", "103.21.244.0/22"]

# Hard cap on entries we walk before giving up. Defaults to 3
# (client → CDN → ALB → app). Bump if your chain is longer.
# trusted_proxies_max_hops = 3
```

Default is `[]` — unchanged behaviour; the socket peer is used. Bad
CIDR values fail loud at config load.

**Do not combine this with `uvicorn --forwarded-allow-ips`.** That flag
rewrites `request.client.host` to the XFF-derived value before the
FastAPI handler runs, which defeats the admission check and lets any
direct client spoof the per-IP cap.
````

### Step 4.3: Update `CLAUDE.md`

- [ ] **Step 4.3:** Modify [`CLAUDE.md`](CLAUDE.md). Find the "**Proxy gotcha**" sentence at [`CLAUDE.md:358-362`](CLAUDE.md#L358-L362):

```markdown
DELETEs. **Proxy gotcha**: `request.client.host` is the socket peer,
  not the X-Forwarded-For client. Behind a reverse proxy every login
  appears to come from `127.0.0.1` and the per-IP cap is effectively
  global — bump `login_global_max` accordingly or wait for the planned
  `auth.trust_proxy_headers` config knob.
```

Replace with:

```markdown
DELETEs. **Reverse-proxy support**: `auth.trusted_proxies` (CIDR list)
  + `auth.trusted_proxies_max_hops` enable right-to-left peeling of
  `X-Forwarded-For` for the per-IP cap. Empty default = unchanged
  behaviour (the socket peer is used). The same CIDR list governs both
  admission (is the immediate peer a trusted proxy?) and peeling
  (which XFF entries to skip). Design + threat model in
  [docs/superpowers/specs/2026-05-21-trust-proxy-headers-design.md](docs/superpowers/specs/2026-05-21-trust-proxy-headers-design.md).
  Do NOT also set `uvicorn --forwarded-allow-ips`; it rewrites
  `request.client.host` before our admission check and collapses it.
```

### Step 4.4: Smoke-test the example config

- [ ] **Step 4.4:** Verify `config.example.toml` still parses by loading it through `load_config()`. Run:

```bash
unset VIRTUAL_ENV && uv run python -c "
from localmail.config import load_config
cfg = load_config('config.example.toml')
print('default:', cfg.auth.trusted_proxies, cfg.auth.trusted_proxies_parsed)
"
```

Expected output: `default: [] ()` — the recipes in the file are all commented out, so the default empty config wins.

### Step 4.5: Commit Task 4

- [ ] **Step 4.5:** Commit the docs updates.

```bash
git add config.example.toml README.md CLAUDE.md
git commit -m "$(cat <<'EOF'
docs(auth): document trusted_proxies + remove stale gotcha references

config.example.toml gets three commented recipes (same-host, private-LAN,
Cloudflare) and the max_hops knob. README replaces the "until
auth.trust_proxy_headers lands" blockquote with a new "Behind a reverse
proxy" subsection explaining the three deployment recipes and the
uvicorn --forwarded-allow-ips non-interaction. CLAUDE.md's Proxy-gotcha
bullet now describes the shipped knob instead of the planned one.

Spec: docs/superpowers/specs/2026-05-21-trust-proxy-headers-design.md

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: Final verification + PR

### Step 5.1: Run the full test suite

- [ ] **Step 5.1:** Run all tests.

```bash
unset VIRTUAL_ENV && uv run pytest -q
```

Expected: `755 passed` (baseline 731 + 17 client_ip + 6 config + 1 rate-limiter = 755). The exact number must equal `baseline + new`; if it's lower, a previously passing test regressed and you must investigate.

### Step 5.2: Run mypy on the touched source

- [ ] **Step 5.2:** Type-check the source tree.

```bash
unset VIRTUAL_ENV && uv run mypy src/localmail
```

Expected: the 4 pre-existing `parser.py` errors carry forward unchanged. No new errors. If mypy reports anything new in `api/client_ip.py`, `config.py`, or `serve/routes/auth.py`, fix it before opening the PR.

### Step 5.3: Inspect the branch contents

- [ ] **Step 5.3:** Sanity-check the four commits on the branch.

```bash
git log --oneline main..HEAD
```

Expected output (order top-down — newest first):

```
<sha> docs(auth): document trusted_proxies + remove stale gotcha references
<sha> feat(serve): /v1/auth/login resolves client IP via trusted_proxies
<sha> feat(config): AuthConfig.trusted_proxies + trusted_proxies_max_hops
<sha> feat(api): resolve_client_ip — XFF peel against trusted proxies
<sha> docs(auth): design for trusted_proxies + XFF client IP resolution
```

(The spec commit is the bottom one, from the pre-flight on `2d8debc`.) Five commits total — if you see fewer, you missed a commit step in an earlier task; rerun it.

### Step 5.4: Push the branch

- [ ] **Step 5.4:** Push.

```bash
git push -u origin feat/auth-trusted-proxies
```

### Step 5.5: Open the PR

- [ ] **Step 5.5:** Open a PR against `main`. Use this body verbatim (single HEREDOC):

```bash
gh pr create --title "feat(auth): trusted_proxies — XFF-aware login rate limiter" --body "$(cat <<'EOF'
## Summary

- New pure module \`localmail.api.client_ip\` exposes \`resolve_client_ip()\` —
  eight-step right-to-left peel of \`X-Forwarded-For\` against an operator-
  configured trusted-proxy CIDR set. Transport-free; reusable from MCP, etc.
- Two new \`AuthConfig\` fields: \`trusted_proxies: list[str]\` (CIDRs) +
  \`trusted_proxies_max_hops: int = 3\`. Empty default = current behaviour
  exactly; pydantic validators fail loud on bad CIDR / out-of-range hops.
- \`/v1/auth/login\` calls the resolver; everything downstream
  (\`_check_login_rate_limits\`, \`_record_login_attempt\`, the audit table)
  is unchanged. Closes the proxy gotcha documented in CLAUDE.md after PR #69.

Spec: \`docs/superpowers/specs/2026-05-21-trust-proxy-headers-design.md\`

## Test plan

- [x] \`uv run pytest -q\` — full suite green (baseline 731 + new).
- [x] \`uv run mypy src/localmail\` — no new errors (4 pre-existing \`parser.py\` ones carry forward).
- [x] \`uv run python -c \"from localmail.config import load_config; load_config('config.example.toml')\"\` — example config loads with the three commented recipes intact.
- [ ] Manual: stand up nginx on \`127.0.0.1\` fronting \`localmail serve --bind 127.0.0.1 --no-tls\`, set \`trusted_proxies = [\"127.0.0.0/8\"]\`, drive 4 failed logins via curl with distinct \`X-Forwarded-For\` values, confirm none trips \`/v1/auth/login\` 429; then 4 with the same XFF, confirm the 4th trips with \`cap = \"ip\"\`. (Smoke before merge.)

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

(The HEREDOC escapes backticks as `\`` and double-quotes as `\"` so the gh CLI receives them as literal characters.)

### Step 5.6: Record the PR number and confirm CI

- [ ] **Step 5.6:** Record the PR number and check CI starts.

```bash
gh pr view --json number,url
gh pr checks
```

Expected: CI workflows trigger. If CI doesn't trigger (e.g., on a fork with disabled workflows), note this in the PR comments — do not block the implementation review on missing CI.

---

## Self-review checklist

Run this after the final commit but before opening the PR — quick sanity check, not a full re-verification.

- [ ] **Spec coverage:** Each section in the spec maps to a task:
  - Spec § Module layout + API → Task 1.
  - Spec § Algorithm (eight steps) → Task 1 (implementation) + Task 1 tests T1–T16.
  - Spec § Sanitisation rules → Task 1 (`_normalise_xff_entry`) + tests T11–T14.
  - Spec § Config schema → Task 2.
  - Spec § Route integration → Task 3.
  - Spec § Non-interactions (no middleware, no `--forwarded-allow-ips`) → Task 4 README + CLAUDE.md.
  - Spec § Logging → Task 1 (WARNING calls) + tests T10, T15.
  - Spec § Testing → Tasks 1, 2, 3 tests.
  - Spec § Docs → Task 4.
  - Spec § Acceptance → Task 5.
- [ ] **No placeholders:** No "TBD", "TODO", or "implement later" anywhere in the diff.
- [ ] **Type consistency:** `TrustedProxies` alias defined identically in both `client_ip.py` and `config.py` (`tuple[IPv4Network | IPv6Network, ...]`). `trusted_proxies_parsed` is a property returning `TrustedProxies` in `config.py` and is named the same when the route reads it.
- [ ] **Commit hygiene:** Each task is one commit. No squash needed.

If anything fails the self-review, fix it before pushing.
