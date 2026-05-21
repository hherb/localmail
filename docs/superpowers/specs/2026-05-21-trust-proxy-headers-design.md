# `trusted_proxies` — client IP resolution behind a reverse proxy — design

> **Status:** Draft 2026-05-21. Carry-forward from PR #69 (Postgres-backed
> login rate limiter). No tracking issue yet — file at implementation start.

## Motivation

`localmail`'s login rate limiter (PR #69, [Postgres-backed limiter
spec](2026-05-20-login-rate-limiter-postgres-design.md)) has three
sliding-window caps: global, per-IP, per-user. The per-IP cap is the most
useful guard against credential discovery — it bounds an attacker
rotating usernames against a single source IP without throttling
legitimate concurrent users sharing the global cap.

The `client_ip` value the limiter sees is currently
`request.client.host` (see
[`src/localmail/serve/routes/auth.py:36`](../../../src/localmail/serve/routes/auth.py#L36)),
which is the **socket peer** — the TCP address the FastAPI worker
accepted the connection from. Behind any reverse proxy (the common
deployment: nginx/Caddy/Traefik on the same host fronting `localmail
serve --bind 127.0.0.1 --no-tls`) every login attempt appears to come
from `127.0.0.1`, and the per-IP cap collapses into a second copy of
the global cap. Two of the three sliding windows are no longer
independent.

The PR #69 handoff documents this as a known gap (CLAUDE.md "Proxy
gotcha", README.md tuning notes) and recommended a small follow-up:
a config knob that, when enabled, parses an `X-Forwarded-For` header
to recover the originating client IP.

This spec turns that recommendation into a concrete, secure-by-default
design.

## Scope

In:

- New pure module `src/localmail/api/client_ip.py` exposing
  `resolve_client_ip(socket_peer, xff_header, *, trusted_proxies,
  max_hops) -> str | None`. Transport-free; no FastAPI imports.
  Mirrors `range_requests.py` / `conditional.py` / `browse_cursor.py`
  conventions.
- Two new fields on `LocalmailConfig.auth` (`AuthConfig`):
  - `trusted_proxies: list[str]` — CIDR strings; default `[]`.
  - `trusted_proxies_max_hops: int = 3` — hard cap on XFF entries to
    peel.
- One call-site change in
  [`src/localmail/serve/routes/auth.py`](../../../src/localmail/serve/routes/auth.py)
  to compute `client_ip` via the resolver before invoking
  `auth_svc.login(...)`.
- Pre-parsed `tuple[IPv4Network | IPv6Network, ...]` cached on the
  config model so per-request cost is one tuple read.
- Pydantic validators that fail config-load on a bad CIDR or
  out-of-range `max_hops`.
- New test file `tests/test_api_client_ip.py` (pure-unit tests of the
  resolver). Extensions to `tests/test_api_auth_rate_limiter.py` and
  `tests/test_config.py`.
- README + CLAUDE.md updates documenting the three canonical recipes
  (same-host / private-LAN / Cloudflare) and the
  `uvicorn --forwarded-allow-ips` non-interaction warning.

Out:

- `X-Real-IP` fallback. Picked against in brainstorming Q2.
- `Forwarded:` (RFC 7239) parsing. Same.
- Middleware that mutates `request.client`. Section 4.
- Similar resolver wiring on `/refresh` or `/change-password` —
  bearer-token routes already require proof-of-possession; per-IP
  brute-force is not the threat model there.
- GeoIP / ASN / country detection. The audit row stores the raw IP
  string only.
- Any change to migrations, the `api_login_attempts` table shape, or
  the `_check_login_rate_limits` SQL. Client IP already flows through
  unchanged.

## Threat model

The configurations we want to defend:

1. **Direct exposure.** `localmail serve` is bound to a public IP/port
   with TLS. No proxy. Default behaviour MUST be unchanged: per-IP cap
   sees the real peer; no new attack surface.
2. **Same-host reverse proxy.** nginx/Caddy on `127.0.0.1` fronts
   `localmail serve --bind 127.0.0.1 --no-tls`. Operator sets
   `trusted_proxies = ["127.0.0.0/8"]`. Per-IP cap must see the real
   public client.
3. **Private-LAN reverse proxy.** Proxy on a separate host inside RFC
   1918. Operator sets `trusted_proxies = ["10.0.0.0/8",
   "127.0.0.0/8"]`. Same recovery guarantee.
4. **Multi-hop / CDN.** Cloudflare → home proxy → app, etc. Operator
   lists every hop's CIDR. The right-to-left peel finds the first
   non-trusted entry — the actual client.

Attacker shapes we must NOT regress against:

- **A1: spoofed XFF on a direct connection.** Attacker reaches the app
  port directly and sets `X-Forwarded-For: 8.8.8.8`. The socket peer
  is their real IP, NOT in any configured CIDR → algorithm step 3
  fires, XFF is ignored, per-IP cap is enforced against the attacker.
- **A2: spoofed XFF tail.** Attacker is behind a trusted proxy (a
  customer on the same LAN, say) and prepends extra entries:
  `X-Forwarded-For: 1.2.3.4, attacker.lan.ip`. The trusted proxy
  appends its own IP at the right. Walking from the right peels the
  proxy → reaches `attacker.lan.ip` (NOT in trusted CIDR) → returns
  that as the client. The attacker's prepended `1.2.3.4` is never
  consulted.
- **A3: oversized XFF.** Attacker sends a 1000-entry XFF, hoping the
  resolver burns CPU or peels past a planted entry. `max_hops`
  bounds the walk; on overrun we fall back to the socket peer (not
  to a partially-peeled value). WARNING is logged for ops.
- **A4: malformed XFF.** Garbled entries (extra commas, port suffixes
  on unbracketed IPv6, non-IP strings). On the first unparseable
  entry from the right we fall back to the socket peer and log
  WARNING — we never trust anything past a value we couldn't parse.

The threat model explicitly does NOT cover an attacker who controls a
trusted proxy. That's a deployment compromise, not a header-parsing
flaw; no client-IP heuristic can recover from it.

## Design

### Public API

```python
# src/localmail/api/client_ip.py
from __future__ import annotations

import ipaddress
import logging
from ipaddress import IPv4Network, IPv6Network

logger = logging.getLogger("localmail.serve")

TrustedProxies = tuple[IPv4Network | IPv6Network, ...]


def resolve_client_ip(
    socket_peer: str | None,
    xff_header: str | None,
    *,
    trusted_proxies: TrustedProxies,
    max_hops: int,
) -> str | None:
    """Return the originating client IP, honouring X-Forwarded-For only
    when the immediate socket peer is itself a trusted proxy.

    See the spec at
    docs/superpowers/specs/2026-05-21-trust-proxy-headers-design.md
    for the threat model and the eight-step algorithm.
    """
```

Inputs are already-parsed primitives (a string, a header value, the
pre-parsed CIDR tuple, an int) so the function is unit-testable
without FastAPI. The signature also makes the resolver reusable from
any future transport (MCP, etc.) that obtains its client-address
context differently.

### Algorithm (exactly the canonical right-to-left peel)

```text
1. If socket_peer is None → return None.
2. If trusted_proxies is empty → return socket_peer.            (knob disabled)
3. If socket_peer is not in any trusted CIDR → return socket_peer.  (untrusted; quiet)
4. If xff_header is None or "" → return socket_peer.
5. Split xff_header on ",". For each entry: strip whitespace,
   IPv6 brackets, %zone ID, and a single trailing :port if the
   remainder parses as an IPv4 (preserves unbracketed IPv6).
6. Iterate the entries from the right, up to max_hops iterations:
   - Parse the entry as ipaddress.ip_address().
     - Unparseable → log WARNING, return socket_peer.
   - If the IP is in any trusted CIDR → continue (peel it).
   - Else → return str(IP).                                     (first non-trusted from the right)
7. Loop fell off the left (every entry was trusted) → return socket_peer.
8. Hit max_hops before finding an untrusted entry → log WARNING, return socket_peer.
```

Step 3 is the security-load-bearing step. **Quiet ignore** (per
brainstorming Q3) — no log, because (a) scanners blast XFF at every
endpoint and log noise is expensive, and (b) a missing log doesn't
give attackers a feedback signal that a parser is even present.

Steps 5-6 use `ipaddress.ip_address()` (not `ip_network()`) for
entries — XFF carries hosts, not ranges.

### Sanitisation rules (step 5)

Single function, exported for tests:

```python
def _normalise_xff_entry(raw: str) -> str | None:
    """Return a parseable IP string, or None if the entry is too
    mangled to use safely.

    Handles common-in-the-wild forms:
      "192.0.2.1"          → "192.0.2.1"
      "192.0.2.1:54321"    → "192.0.2.1"           (port-strip on last ':' if rest digits)
      "[2001:db8::1]:443"  → "2001:db8::1"         (bracketed v6 with port)
      "2001:db8::1"        → "2001:db8::1"         (bare v6)
      "fe80::1%eth0"       → "fe80::1"             (zone-ID strip)
      ""                   → None
      "  "                 → None
      "garbage"            → None  (delegated to ip_address() in caller)
    """
```

The bracket/port logic intentionally only strips a trailing `:port`
when the rest parses as IPv4 — bare IPv6 contains colons, so a naive
`rsplit(":", 1)` would mangle `2001:db8::1`. Test cases cover both.

### Config schema

```python
# src/localmail/config.py — additions to AuthConfig
from ipaddress import ip_network
from pydantic import Field, field_validator, PrivateAttr

class AuthConfig(BaseModel):
    # ... existing fields unchanged ...

    trusted_proxies: list[str] = Field(default_factory=list)
    trusted_proxies_max_hops: int = Field(default=3, ge=1, le=10)

    _trusted_proxies_parsed: TrustedProxies = PrivateAttr(default=())

    @field_validator("trusted_proxies")
    @classmethod
    def _validate_cidrs(cls, v: list[str]) -> list[str]:
        for s in v:
            ip_network(s, strict=False)   # raises ValueError on bad input
        return v

    def model_post_init(self, __context: object) -> None:
        object.__setattr__(
            self,
            "_trusted_proxies_parsed",
            tuple(ip_network(s, strict=False) for s in self.trusted_proxies),
        )

    @property
    def trusted_proxies_parsed(self) -> TrustedProxies:
        return self._trusted_proxies_parsed
```

Notes:

- `strict=False` accepts host-form (`"10.0.0.5"` → `10.0.0.5/32`)
  because operators don't reliably write the suffix.
- The validator parses-and-discards purely to fail loud at config-load
  on a bad CIDR. The real parse happens once in `model_post_init` and
  the result lives on a `PrivateAttr` so it survives `model_dump()`
  cleanly and doesn't surprise pydantic's introspection. (Reason for
  not using `functools.cached_property`: pydantic v2 treats
  unannotated descriptors as fields unless configured via
  `ConfigDict(ignored_types=...)`, and there's no precedent in this
  codebase. `model_post_init` is the documented v2 pattern.)
- `max_hops` bounds: `ge=1` stops a `0` from silently disabling the
  walk (a footgun — operators would expect `0` to mean "no peel" but
  the natural reading is "no entries trusted"). `le=10` is sanity;
  the longest realistic chain is 3-4.

### Route integration

Exactly one call site:

```python
# src/localmail/serve/routes/auth.py — login()
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

`auth_svc.login`, `_check_login_rate_limits`, `_record_login_attempt`
are **unchanged**. The `api_login_attempts` schema is unchanged.

### Explicit non-interactions

- **No FastAPI middleware** that rewrites `request.client`. Tempting
  (one line, fixes the whole app) but spreads the trust decision to
  every route and hides it from review. Only `/login` needs this
  today; keep the call explicit.
- **No `uvicorn --forwarded-allow-ips`.** That flag rewrites
  `request.client.host` to the XFF-derived IP before our code runs,
  which collapses step 3 of the algorithm (we'd never see the actual
  socket peer and could not enforce the admission check). README
  must explicitly tell operators NOT to set this flag.
- **No `X-Real-IP`.** Picked against in brainstorming Q2 — it would
  require a second admission path and the operators we expect to
  serve (nginx, Caddy, Cloudflare) all emit XFF anyway.

## Logging

Two WARNING events on the `localmail.serve` logger, both single-line,
both include a redacted XFF preview (length + first + last entry) so
ops can debug without dumping arbitrary attacker-supplied bytes into
logs:

```
client IP resolver: unparseable XFF entry, falling back to socket peer
  peer=192.0.2.10 xff_len=4 xff_first=1.2.3.4 xff_last=<unparseable>

client IP resolver: max_hops=3 exceeded, falling back to socket peer
  peer=192.0.2.10 xff_len=100 xff_first=1.2.3.4 xff_last=10.0.0.5
```

Both fire only when the socket peer IS trusted (step 3 already
returned otherwise). The "untrusted socket peer with spoofed XFF"
path is silent, per Q3.

## Testing

### `tests/test_api_client_ip.py` (new) — pure unit tests

| # | Case | Assertion |
|---|---|---|
| T1 | `socket_peer=None` | returns `None` |
| T2 | `trusted_proxies=()` (knob disabled) + any XFF | returns socket peer |
| T3 | Socket peer NOT in trusted CIDR + XFF present | returns socket peer; **caplog empty** |
| T4 | Socket peer trusted, `xff_header=None` | returns socket peer |
| T5 | Socket peer trusted, `xff_header=""` | returns socket peer |
| T6 | Socket peer = `10.0.0.1` (trusted), XFF = `"203.0.113.7"` | returns `"203.0.113.7"` |
| T7 | `trusted_proxies = ["10.0.0.0/8"]`, socket peer = `10.0.0.5`, XFF = `"203.0.113.7, 10.0.0.5"` | returns `"203.0.113.7"` (verifies right-to-left peel: peel trusted `10.0.0.5`, return untrusted `203.0.113.7`) |
| T8 | Three-hop chain where p1 + p2 trusted | returns the leftmost non-trusted entry |
| T9 | All XFF entries in trusted CIDR | returns socket peer |
| T10 | Socket peer trusted, XFF = `"garbage, 10.0.0.5"` from the right (i.e. unparseable at position [-2]) | returns socket peer + WARNING logged |
| T11 | Socket peer trusted, XFF = `"203.0.113.7:54321"` (IPv4 + port) | returns `"203.0.113.7"` |
| T12 | Socket peer trusted, XFF = `"[2001:db8::1]:443"` (bracketed v6 + port) | returns `"2001:db8::1"` |
| T13 | Socket peer trusted, XFF = `"fe80::1%eth0"` | returns `"fe80::1"` (zone-ID stripped) |
| T14 | Socket peer trusted, XFF = `"2001:db8::1"` bare v6 (note the colons) | parses correctly, NOT mangled by port-strip |
| T15 | `max_hops=1`, XFF = `"203.0.113.7, 10.0.0.5"` where peer is `10.0.0.5` | returns socket peer + WARNING (cap hit) |
| T16 | Mixed v4/v6 trusted set, mixed-family XFF entries | both addresses resolve correctly |

### `tests/test_api_auth_rate_limiter.py` (extend)

One new test, end-to-end against the live FastAPI app via TestClient:

- Configure `AuthConfig.trusted_proxies = ["127.0.0.0/8"]`.
- Drive `login_per_ip_max + 1` failed `/v1/auth/login` calls, each
  with a different `X-Forwarded-For` header value (one public IP per
  call), socket peer being `127.0.0.1` (TestClient default).
- Assert all return 401 (none trip the per-IP cap — distinct client
  IPs each).
- Then drive `login_per_ip_max + 1` calls with the SAME XFF value.
- Assert the last is 429 with `cap = "ip"`.

This proves the resolver wires into the limiter without re-testing
the limiter's internals.

### `tests/test_config.py` (extend)

- Empty `trusted_proxies` default round-trips: `AuthConfig()` →
  `.trusted_proxies == []`, `.trusted_proxies_parsed == ()`.
- Bad CIDR string → `pydantic.ValidationError` at construction time.
- `trusted_proxies_max_hops = 0` → `ValidationError`.
- `trusted_proxies_max_hops = 11` → `ValidationError`.
- Host-form entry `"10.0.0.5"` → parsed as `/32`,
  `IPv4Network("10.0.0.5/32") in .trusted_proxies_parsed`.

## Docs

### README.md

Append a "Behind a reverse proxy" subsection under the existing GUI
server / `localmail serve` section:

````markdown
### Behind a reverse proxy

The login rate limiter has separate global, per-IP, and per-user caps.
Behind a reverse proxy `request.client.host` is the proxy's address,
not the real client — every login attempt looks like it came from the
proxy and the per-IP cap collapses into a copy of the global cap.

Configure `auth.trusted_proxies` to recover the real client IP from
`X-Forwarded-For`. The list governs both admission (is this socket
peer a trusted proxy?) and peeling (which XFF entries are proxies vs
the client). Right-to-left peel of XFF, identical to nginx's
`set_real_ip_from` / Caddy's `trusted_proxies` semantics.

```toml
[auth]
# Same-host nginx/Caddy/Traefik fronting localmail serve on 127.0.0.1:
trusted_proxies = ["127.0.0.0/8"]

# Reverse proxy on a separate host in a private LAN:
# trusted_proxies = ["10.0.0.0/8", "127.0.0.0/8"]

# For a CDN/edge proxy (Cloudflare, Fastly, etc.) fetch the
# operator's current published IP ranges — they change over time.
# Cloudflare: https://www.cloudflare.com/ips/

# Hard cap on how many entries we'll walk before giving up. Defaults
# to 3 (client → CDN → ALB → app). Bump if your chain is longer.
# trusted_proxies_max_hops = 3
```

Do **not** combine this with `uvicorn --forwarded-allow-ips`. That
flag rewrites `request.client.host` to the XFF-derived value before
the FastAPI handler runs, which defeats the admission check and lets
any client spoof the per-IP cap by setting `X-Forwarded-For`
themselves.
````

### CLAUDE.md

Update the existing "Proxy gotcha" bullet under the login rate-limiter
section. Currently reads:

> **Proxy gotcha**: `request.client.host` is the socket peer, not the
> X-Forwarded-For client. Behind a reverse proxy every login appears
> to come from `127.0.0.1` and the per-IP cap is effectively global —
> bump `login_global_max` accordingly or wait for the planned
> `auth.trust_proxy_headers` config knob.

Replace with:

> **Reverse-proxy support**: `auth.trusted_proxies` (CIDR list) +
> `auth.trusted_proxies_max_hops` enable right-to-left peeling of
> `X-Forwarded-For`. Empty default = unchanged behaviour (socket
> peer). The same CIDR list governs both admission (is the immediate
> peer a trusted proxy?) and peeling (which XFF entries to skip).
> Design + threat model in
> [docs/superpowers/specs/2026-05-21-trust-proxy-headers-design.md].
> Do not set `uvicorn --forwarded-allow-ips` — it collapses the
> admission check.

## Acceptance

- All 16 unit tests in `tests/test_api_client_ip.py` pass.
- The two new tests in `tests/test_api_auth_rate_limiter.py` pass
  (one happy path, one cap-trip with shared XFF).
- The five new tests in `tests/test_config.py` pass.
- `uv run pytest -q` baseline (`731 passed` as of `f803ab3`) advances
  by exactly the count of new tests (+23 nominal, exact count
  decided in the plan).
- `uv run mypy src/localmail` shows no new errors (the existing 4
  `parser.py` errors carry forward unchanged).
- `config.example.toml` parses through `load_config()` with the
  three commented recipes uncommented one at a time.
- README + CLAUDE.md updates committed in the same PR.

## Open questions

None. The design covers the four attacker shapes (A1-A4), the four
deployment shapes (direct + three proxy), and the two explicit
non-interactions (middleware, uvicorn flag) without further
ambiguity. The CIDR validator chooses `strict=False` (host-form OK);
the port-strip is IPv4-only with a documented carve-out for
unbracketed IPv6.

## File map

```
src/localmail/
  api/
    client_ip.py            # NEW — resolve_client_ip + _normalise_xff_entry
  config.py                 # ADD trusted_proxies + trusted_proxies_max_hops + validator + cached property
  serve/
    routes/
      auth.py               # MODIFY login() — extract client_ip via resolver

config.example.toml         # ADD three recipes (commented) + max_hops example

tests/
  test_api_client_ip.py     # NEW — T1-T16 unit tests
  test_api_auth_rate_limiter.py   # EXTEND — 2 new end-to-end cases
  test_config.py            # EXTEND — 5 new validator cases

docs/superpowers/specs/
  2026-05-21-trust-proxy-headers-design.md   # this file

CLAUDE.md                   # MODIFY proxy-gotcha bullet
README.md                   # ADD "Behind a reverse proxy" subsection
```

No migrations. No schema changes. No new dependencies (`ipaddress`,
`pydantic` already in use).
