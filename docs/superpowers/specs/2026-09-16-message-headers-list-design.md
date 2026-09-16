# Message headers are served per occurrence, in wire order

Date: 2026-09-16
Status: approved
Issue: #379 (slice B of the kastellan request triage; slice A was #364)

## Problem

`GET /v1/messages/{id}?headers=full` emits the `messages.headers` JSONB verbatim
(`api/messages.py:94-95`). The parser builds that column by grouping every
occurrence under the header name *as spelled on the wire*
(`parser.py::_headers_dict`), so the object is `name -> [value, …]`.

Three consequences, measured on the live Mac archive (129,590 messages):

1. **Wire order across names is lost.** A dict preserves first-insertion order of
   *names*; order within one name survives, order between names does not. A
   `Received` chain's position relative to `Authentication-Results`, `ARC-Seal`
   and `DKIM-Signature` is unrecoverable. **89.1%** of 2,746 sampled messages
   carry at least one repeated name (`received` 2448, `dkim-signature` 628,
   `return-path` 356, `authentication-results` 178).
2. **Case variants split into separate keys.** `Received` and `received` are
   different JSON keys, so their interleaving is gone. **140 of 2,746 (5.1%)**
   sampled messages carry one name under more than one spelling.
3. **The values are frozen at sync time.** The column is written once, by
   whatever parser ran then. #314 (`eec8e09`) moved `_headers_dict` to
   `raw_items()`, so older rows can carry a leading space or tab that today's
   parser strips. Over 900 sampled messages, **104 (11.6%)** disagree with a
   fresh parse of their own `raw_bytes`; all 309 differing values are
   whitespace-only — same key set, same occurrence counts, identical after
   `.strip()`. The oldest 300 rows differ in 16, the newest 300 in 0.

Beside these, an unrecognised `headers=` value is silently answered compact:
the route declares `headers: str = Query("compact")` and derives
`full_headers=(headers == "full")`, so a typo *and* a new client's
`?headers=list` against an old server both return 200 with no `headers` key.
`api_minor` has never moved, so there is nothing to feature-detect against.

hherb/kastellan's `email-in` worker reads this endpoint for its DMARC
`Authentication-Results` ordering check.

## Non-goals

- **A new column or migration.** The read-time parse of a bounded prefix costs
  0.33 ms median (measured, below) and `raw_bytes` is already the authority.
  A stored `header_list` would double ~4.7 KB per message, need a backfill over
  129,590 rows, and leave legacy rows NULL until it ran.
- **A raw (undecoded) value form.** Each entry's `value` is what the parser
  produces per occurrence, so `list` is a refinement of `full` rather than a
  second encoding of the same header. A `{name, value, raw}` entry roughly
  doubles the payload for a use nobody has asked for; RFC 2047 decoding is
  visible on `Subject`/`From`, not on the trace headers this slice exists for.
- **Refusing unknown query *parameter names*.** FastAPI ignores them (that is
  how hherb/kastellan#500's `?full_headers=true` failed silently). Refusing them
  is #370's territory and reaches every route.
- **Changing what is stored.** `messages.headers` keeps being written and
  GIN-indexed; it simply stops being what the read path serves.
- **A GUI change.** The Rust struct models `headers` as `Option<Value>` and
  `full_headers.rs` hardcodes `?headers=full`; the TS type is
  `Record<string, string | string[]>`. Both are unaffected.

## Design

### 1. One pure module owns the header block

New `src/localmail/header_block.py` — pure, no IO, beside `parser.py` and
`pgtext.py` for the reason `account_names.py` and `ocr_policy.py` live there:
`api/messages.py` needs it and `parser.py` needs it, so it cannot live in
either.

```python
#: The header block is read as a bounded prefix of `raw_bytes`, because that
#: column carries the attachments too: p50 44 KB, p99 2.6 MB, max 35 MB on the
#: live archive, against a p95 header block of 8.7 KB. Measured: 0 of 129,590
#: messages have a block that does not end within this ceiling.
HEADER_BLOCK_READ_BYTES = 64 * 1024

@dataclass(frozen=True)
class HeaderEntry:
    name: str          # the wire spelling, never normalised
    value: str         # one occurrence, through the parser's own rule
```

Four functions:

- `header_block_end(prefix: bytes, *, truncated: bool) -> int | None` — the
  offset of the first `\r\n\r\n` or `\n\n`, or `len(prefix)` when the prefix is
  a whole message that ends without one (RFC 5322 permits a message with no
  body), or `None` when `truncated` and no separator was found. `None` is the
  only case the caller has to do more work for.
- `entries_from_message(msg: EmailMessage) -> list[HeaderEntry]` — walks
  `raw_items()` and applies the per-occurrence value rule, NUL-stripped through
  `pgtext.strip_nuls`.
- `parse_header_block(block: bytes) -> list[HeaderEntry]` — parses with
  `email.policy.default` (the policy `parse_message` uses; a different policy
  yields different values) and delegates.
- `group_entries(entries) -> dict[str, list[str]]` — the `full` shape.

**`full` is the grouping of `list`, not a second parse.** That is what makes the
refinement invariant structural: there is one sequence of occurrences, and the
two modes are two projections of it. Stating it as a test over two independent
implementations would be the drift this codebase keeps filing issues about.

**`parser._headers_dict` is reimplemented as `group_entries(entries_from_message(msg))`**,
so the per-occurrence rule — including #314's broad catch, which reports a
header the stdlib cannot parse as its raw text rather than poisoning the whole
column — has exactly one implementation. `parse_message` and the read path
cannot disagree about what a header's value is. The import direction is
`parser -> header_block`; `header_block` imports only the stdlib and `pgtext`.

### 2. The read path reads a bounded prefix

`api/messages.py::get_message` takes `headers: HeaderMode` — a
`Literal["compact", "full", "list"]` — in place of `full_headers: bool`. There
are no external callers; the routes and the MCP tools are the only two.

- `compact`: reads nothing extra and emits **no** `headers` key, exactly as
  today. The `m.headers` column leaves the SELECT.
- `full` / `list`: the SELECT adds
  `substring(raw_bytes from 1 for HEADER_BLOCK_READ_BYTES + 1)`. The `+1` is
  how truncation is detected without a second query: a returned prefix longer
  than the ceiling means the message continues past it.

Measured on the live archive: the prefix read is **0.33 ms median, 1.14 ms max**
over 40 random messages, against 0.09 ms for the JSONB passthrough it replaces
and a 5.61 ms max for reading the whole column. A block-only parse is
byte-identical to a whole-message parse (verified directly, and the differential
test below keeps it that way).

**Truncation is never silent.** When `header_block_end` returns `None`, one
fallback statement reads the whole `raw_bytes` for that message — ACL-scoped,
the same shape `get_message_raw` already uses — and a WARNING names the id and
the ceiling. A short header list that looked complete is precisely the failure
this slice exists to end. It is unreachable on the live archive today, which is
why it is a fallback rather than a refusal.

### 3. An unusable mode is refused, not answered

The route validates `headers` and raises `ValidationFailed` (400 problem+json)
naming the accepted values. Not FastAPI's `Literal` alone: that answers 422 with
an array `detail`, which is #370's complaint and which kastellan's worker
renders as a 512-byte raw body. This follows slice A — an unknown filter key is
a 400 naming the key.

This is a behaviour change: `?headers=xyz` was a silent 200-compact.

### 4. `api_minor` becomes 1

An old server *cannot* refuse `?headers=list`; it answers 200 with no `headers`
key. So a client has to be able to ask before it asks, and the only feature
signal on the wire is `GET /v1/version`. `API_MINOR` goes 0 -> 1, documented as
`api_minor >= 1` iff `headers=list` is served. `test_serve_app_baseline.py`
asserts `>= 0` and passes unchanged; the six-key set test is untouched.

This is the first move of that number, and the reason slice A explicitly
deferred feature detection to this slice.

### 5. MCP takes the same vocabulary

`get_message(message_id, headers="compact"|"full"|"list")`. `full_headers` is
removed rather than kept beside it: two parameters that can contradict each
other on an agent-facing schema need a precedence rule nobody reads. Agents read
the schema each session, and localmail's own tests are the only pin.

Its description is corrected in passing. Today it promises that `false` "returns
the common subset (From/To/Subject/Date/…)", and `false` returns no `headers`
key at all.

## Wire changes

| Change | Who sees it |
|---|---|
| `headers=list` | new; nothing sends it yet |
| `headers=<unknown>` is 400, was 200-compact | a typo; no shipped client sends one |
| `full` loses leading whitespace on ~12% of rows | kastellan's DMARC check, the GUI unfold panel — whitespace-only, same keys, same counts |
| `api_minor` 0 -> 1 | nothing reads it in-tree; README documents it |
| MCP `full_headers` -> `headers` | MCP agents; the parameter never worked as described |

## Testing

- **Pure** (`tests/test_header_block.py`): `header_block_end` over CRLF, bare
  LF, a message with no body, and a truncated prefix; `parse_header_block` over
  a fixture with two `Received` headers and a case-variant duplicate, asserting
  the exact sequence; `group_entries` over the same fixture.
- **Differential**: `group_entries(entries_from_message(m))` equals the
  pre-change `_headers_dict(m)` over every `tests/_eml.py` fixture and over
  #314's `Message-Id: <>` poison fixture. This is what keeps the parser's stored
  output unchanged while its implementation moves.
- **Refinement invariant**: for every fixture, grouping the `list` payload by
  name reproduces the `full` payload exactly.
- **api**: `list` order; `full` shape; `compact` still has no `headers` key; the
  truncation fallback, driven by a fixture whose block exceeds a patched
  ceiling.
- **Route**: `headers=list` end to end; an unknown value is 400 problem+json
  naming the accepted values; `full`'s body is unchanged against a golden.
- **MCP**: the *published* `inputSchema` declares `headers` with the three
  values and no `full_headers` — read off the published tool list, since
  `server.py` is what an agent sees (#308's lesson).
- **Version**: `api_minor == 1`.
- **Acceptance** (from the handoff): a message with two `Received` headers and a
  case-variant duplicate returns every occurrence in wire order, and `full` is
  unchanged against a golden response.

## Consequences

- `api/messages.py` gains the mode and the fallback; `header_block.py` is new
  and pure. Neither approaches the 500-line guideline.
- `messages.headers` keeps its GIN index and its writer, and gains no reader.
  An operator's `headers ? 'Received'` query is unaffected.
- The stored column and the served value may now differ for pre-#314 rows. That
  is the point: the served value is the one today's parser produces.
