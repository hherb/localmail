# Attachments are addressable by their position in the message

Date: 2026-09-17
Status: approved
Issue: slice D of the kastellan request triage (slice A was #364, B was #379)

## Problem

A client holding a message can reach one of its attachments only by content
hash: `GET /v1/messages/{id}` lists `{filename, sha256, content_type, size}` per
entry, and the bytes come from `GET /v1/attachments/{sha256}`. hherb/kastellan
matches the attachment it wants by filename, then follows the hash. Measured on
the live Mac archive (129,668 messages; 16,833 carry attachments, 35,980 entries,
average 2.14 per message, maximum 72):

1. **A filename does not identify an entry.** 1,153 `(message, filename)` groups
   hold more than one entry, and **200** of those span more than one distinct
   blob. One message carries `attachment` 22 times over 22 different blobs.
   Matching by name picks one of them and cannot know it picked wrong.
2. **A hash does not carry the entry's name.** **888** `(message, sha256)`
   groups carry the same bytes under two different filenames inside one
   message, and **1,109** blobs carry more than one filename across messages.
   `/v1/attachments/{sha256}` serves whichever name
   `get_attachment_filename` finds on the *earliest* carrying message, so the
   `Content-Disposition` it sends is wrong for every other carrier.
3. **Extracted text is served whole.** 9,303 blobs have text: p50 3.1 KB,
   p95 40 KB, **p99 129 KB, max 2.1 MB**, with 133 over 100 KB. A client that
   wants the first page of a long PDF receives the whole of it as one JSON
   string.

The entry's position in `messages.attachments` is the only per-message identity
that is both unique and already on the wire — it is the index of the array
`GET /v1/messages/{id}` returns.

## Non-goals

- **MCP.** Neither index addressing nor text paging reaches the MCP
  `get_attachment` tool. kastellan consumes `/v1` over REST only; an agent that
  needs a page can be served by a follow-up. `get_attachment_text`'s signature
  and return type are unchanged so the MCP tool is untouched.
- **`index` or `content_id` on `get_message`'s entries.** Position in the array
  already is the index. A new wire key lands with a consumer (#278's rule), and
  none has asked.
- **A cap on the un-paged default.** Omitting `offset` and `limit` returns the
  whole text, as today. Capping it would silently truncate the 133 texts over
  100 KB for every existing caller of `/v1/attachments/{sha256}/text`.
- **Excluding inline parts.** The index is the position in `messages.attachments`,
  which includes inline images: 12,658 of the 35,980 live entries carry a
  `content_id`, so `attachments/0` may well be a signature logo. That is the
  array kastellan already receives, so the indices line up. Whether inline
  parts count as attachments is #365.
- **Cheaper windows.** See "Cost" below: a window is not cheaper in the database
  than the whole text.

## Design

### 1. The index is resolved under the message's ACL

New `api/attachments.py::resolve_message_attachment`:

```python
@dataclass(frozen=True)
class MessageAttachment:
    sha256: str             # hex, as stored in messages.attachments
    filename: str | None    # this entry's own name, never another carrier's

def resolve_message_attachment(
    conn, message_id: int, index: int, *, allowed_account_ids: list[int],
) -> MessageAttachment: ...
```

One query:

```sql
SELECT m.attachments -> %(index)s
  FROM messages m
 WHERE m.id = %(mid)s AND m.account_id = ANY(%(accounts)s)
```

No row is a missing message or an ungranted one; a row whose element is SQL
`NULL` is an index past the end. Both raise `NotFound`, which is the shared 404
the rest of `/v1` uses so permission state cannot be enumerated. An entry with
no `sha256` also raises `NotFound`: there is nothing to serve (0 such entries
live, and 0 non-object entries).

**Two index values must never reach that query, and both were measured, not
reasoned:**

- **A negative index silently serves the last entry.** Postgres `->` indexes
  from the end on a negative operand: `'[{"a":1},{"b":2}]' -> -1` is `{"b":2}`.
  `parse_int_id` refuses a leading `-` on the wire, but the accessor is public
  API and a library caller reaches it directly, so it refuses `index < 0` itself
  with `ValidationFailed`. Refusing rather than resolving is the point: a
  negative index is not an address this API defines.
- **An index past `int4` is a 500.** `jsonb -> integer` has no `bigint` or
  `numeric` form, so `-> 2147483648` raises `UndefinedFunction` and escapes as
  an unhandled 500 (`serve.app` maps `APIError` only). Such an index is well
  formed and simply cannot exist, so it raises `NotFound` before the query:

```python
#: The largest operand `jsonb -> integer` accepts. The shipped SQL casts with
#: `%s::int`, so a larger index raises `NumericValueOutOfRange: integer out of
#: range` — a 500 — rather than the uncast form's `operator does not exist:
#: jsonb -> bigint`; either way it's a 500, and cannot address an entry
#: anyway, since no array has that many.
MAX_JSONB_INDEX = 2**31 - 1
```

*(Planning correction: an earlier wording of both this comment and the prose
bullet above it named `UndefinedFunction` / `operator does not exist: jsonb ->
bigint` as the error a larger index raises. Verified on the shipped SQL,
which casts the operand with `%s::int`: the actual error is
`NumericValueOutOfRange: integer out of range`. The uncast form does raise
the originally-named `UndefinedFunction`/`jsonb -> bigint` error, but that
isn't what ships. The conclusion — either way a 500, and no array has that
many entries — is unchanged.)*

The message id needs no such bound: `messages.id = %s` compares `bigint` with
`numeric` without error, measured up to `10**20`.

**Order of refusals:** a negative index (400) is refused before the empty-ACL
short-circuit, so a malformed request is never disguised as a 404 — the rule
`get_message`'s `headers` mode and `run_search`'s gates already follow. The
`MAX_JSONB_INDEX` refusal and the empty-ACL refusal are both 404s, so their
relative order is unobservable; both precede the query.

Both new routes live in `serve/routes/messages.py`, the router mounted at
`/v1/messages`. The route parses `message_id`, `index`, `offset` and `limit`
with `parse_int_id` before opening a connection.

### 2. The bytes route reuses the streaming code; only the name changes

`GET /v1/messages/{message_id}/attachments/{index}` resolves the entry, then
runs exactly what `/v1/attachments/{sha256}` runs: `get_attachment_blob_info`
(which re-checks the blob-level ACL — one GIN-indexed `EXISTS`, kept rather than
bypassed so the probe stays safe by default, #67), the `If-None-Match` 304
short-circuit, `_open_blob_file_at`, then `Range` / `If-Range` / 200 / 206 / 416
with the #32 force-download headers and MIME clamp.

The difference is `Content-Disposition`: it carries **the resolved entry's**
filename, falling back to the same sha-prefix name when that is null or blank.
This is the fix for problem 2, and it is available only here — a hash cannot
say which carrier it was reached through. The index route never runs
`get_attachment_filename`'s JSONB scan at all.

**The ETag stays `"<sha256>"`.** Two indices naming identical bytes share it,
which is correct: an ETag validates a representation *per URL*, a 304 carries no
`Content-Disposition` (RFC 9110 §15.4.5), and a client stores that header with
the URL it fetched. Nothing lets one URL's cached name leak onto another.

#### The streaming machinery moves to its own module

`serve/routes/attachments.py` holds the Range/ETag/disposition helpers and the
200/206/416 response construction. Both routers need them, and importing
underscore names across route modules is the wrong direction, so they move to a
new `serve/routes/blob_response.py`, leaving `attachments.py` a thin route:

```python
def blob_response(
    request: Request, *, sha256: str, mime: str, size: int,
    fp: BinaryIO, filename: str | None,
) -> Response:
    """200 / 206 / 416 for an already-opened, ACL-cleared blob."""
```

**`blob_response` takes an open file; it does not open one.** The 304
short-circuit and `_open_blob_file_at` stay at each call site, in that order.
That is load-bearing, not tidy: `test_304_does_not_call_open_attachment_bytes_or_filename`
spies on `localmail.serve.routes.attachments._open_blob_file_at`. Were the open
moved inside the helper, the spy would stop intercepting and its `== []`
assertion would pass whether or not the file was opened — a pin turned vacuous
by a refactor, with nothing failing. The new route gets the same spy test
against its own module.

The logger stays `logging.getLogger("localmail.serve")`, not `__name__`, because
the truncation test (#58) listens on that name.

### 3. Text is paged in characters

`GET /v1/messages/{message_id}/attachments/{index}/text` and the existing
`GET /v1/attachments/{sha256}/text` both accept `offset` and `limit` and both
answer:

```json
{"text": "…", "offset": 0, "limit": 20000, "total": 128875, "next_offset": 20000}
```

- `offset` and `limit` count **characters**. The column is `TEXT` and
  `substring()` is character-based, so no page boundary can split a code point.
- Omitting `limit` means "to the end"; omitting both returns the whole text, as
  today. `limit` is echoed (`null` when omitted).
- `total` is the whole text's length and `next_offset` is where the next page
  starts, `null` once this page reaches the end.
- The new keys are present on every response, paged or not. The existing
  `text` key is unchanged.

**`next_offset` is server-computed because a client cannot compute it.** In
JavaScript `text.length` counts UTF-16 code units, while Postgres counts code
points; the two differ by one for every character above U+FFFF. **8 of the
9,303** live extractions contain such characters (52 in all), so a client
paging with `offset += text.length` skips text on exactly those documents.
Python's `len(str)` counts code points, so the server's arithmetic agrees with
`substring()`.

#### One pure module owns the window

New `src/localmail/text_window.py`, top level beside `pgtext.py` because both
`api/attachments.py` and the routes need it:

```python
#: Postgres caps a single field value at 1 GB, and a character is at least one
#: byte, so no TEXT value is longer than this. `substring()` takes int4
#: positions — a larger one is `function substring(…, bigint, …) does not
#: exist`, a 500 — so offsets and limits are clamped here, which cannot change
#: an answer: every offset at or past this is past the end of every text.
MAX_TEXT_CHARS = 2**30

def text_window_error(offset: int, limit: int | None) -> str | None: ...

@dataclass(frozen=True)
class TextWindow:
    offset: int = 0
    limit: int | None = None
    # __post_init__ raises ValueError(text_window_error(...)) when not None.
    @property
    def sql_from(self) -> int: ...          # min(offset, MAX_TEXT_CHARS) + 1
    @property
    def sql_for(self) -> int | None: ...    # min(limit, MAX_TEXT_CHARS)
    def page(self, text: str, total: int) -> TextPage: ...

@dataclass(frozen=True)
class TextPage:
    text: str
    offset: int
    limit: int | None
    total: int
    next_offset: int | None
    def to_wire(self) -> dict[str, object]: ...
```

*(Review correction, in place: the `MAX_TEXT_CHARS` comment above names the
wrong error, the same slip as the `MAX_JSONB_INDEX` one. The shipped SQL casts
with `%s::int`, so an oversized position raises `NumericValueOutOfRange:
integer out of range`; `function substring(unknown, bigint) does not exist` is
the uncast form's error. The conclusion — a 500, hence the clamp — is
unchanged. The shipped comment in `text_window.py` carries both.)*


`text_window_error` refuses `offset < 0` and `limit < 1`. **`limit=0` is
refused, not served**, because it would answer `next_offset == offset` for any
text not yet exhausted, and a client looping on `next_offset` would never
advance.

**The `+ 1` lives on the object that validated `offset`**, because the
conversion to Postgres' 1-based position is only sound under that check. Written
apart, relaxing the check silently makes `substring(… from 0 …)` return one
character fewer than asked — the co-location argument `PREFIX_READ_BYTES` and
`prefix_is_truncated` make in `header_block.py`. The wire becomes a window in
exactly one place, `api.attachments.text_window_from_query(offset, limit)`,
which parses both with `parse_int_id` and raises `ValidationFailed` with
`text_window_error`'s wording; both text routes call it. `__post_init__` is the
by-construction backstop no reachable HTTP input hits (`ResolvedVersion`'s
shape); a library caller constructing a bad `TextWindow` directly gets its
`ValueError` before any IO. *(Planning correction: an earlier wording had the
accessor call `text_window_error` itself. The accessor takes a constructed
window, so it cannot see an invalid one.)*

#### The accessor

```python
def get_attachment_text_page(
    conn, sha256_hex: str, *, allowed_account_ids: list[int], window: TextWindow,
) -> TextPage: ...
```

One statement: `substring(extracted_text from %s [for %s])` beside
`length(extracted_text)`. The window is validated before the blob ACL check
runs, for the ordering reason in §1: an invalid window is a 400 even for a
caller granted nothing. `get_attachment_text` becomes
`get_attachment_text_page(…, window=TextWindow()).text`, so there is one SQL
read and the MCP tool's behaviour is unchanged. `window` is keyword-only with no
default, #234's shape: `TextWindow()` is the whole text, and a caller that meant
a page must say so.

#### Parameter errors are problem+json

Both routes take `offset` and `limit` as `str | None` and parse them with
`parse_int_id`, so malformed values are **400** `/problems/validation-failed`,
never FastAPI's 422 array `detail` (#370). A consequence worth stating:
`offset=-1` from the wire is refused by `parse_int_id`'s "must be a base-10
integer" before `text_window_error`'s "must be >= 0" can word it — the wording
every other integer parameter on `/v1` already has.

### 4. Cost

A window is **not** cheaper in the database than the whole text. The largest
extraction (2,116,946 characters) is stored TOAST-compressed at 387,898 bytes,
so every read decompresses all of it, and `substring()` on UTF-8 walks to the
character offset. Measured, median of 15:

| read | ms |
|---|---|
| the whole text (today) | 2.02 |
| first 20,000-character window + `length()` | 3.15 |
| last 20,000-character window + `length()` | 8.40 |

Walking that document in 20,000-character pages costs ~106 reads, each a full
decompression. What paging buys is the response: a 20,000-character body where
there was a 2.1 MB one, and a client that never holds the whole text. Storing a
second, uncompressed copy to make windows cheap is not warranted at p99 129 KB.
This is #384's question for `raw_bytes`, asked of a different column.

### 5. `api_minor` 1 → 2

An older server answers `/v1/messages/{id}/attachments/0` with 404 — the same
status a current server gives a missing message or an index past the end. A
client therefore cannot tell "not supported" from "not there", so the feature
signal is the version, exactly as for `?headers=list` (0 → 1). The text
routes' new keys are additive and need no signal of their own.

## Testing

- **`tests/test_text_window.py`** (pure): each refusal and its wording; the
  `limit=0` refusal; `sql_from`/`sql_for` including the clamp at
  `MAX_TEXT_CHARS`; `page()` for a first, middle, last, exact-fit, past-the-end
  and empty text; an astral-plane text whose `next_offset` differs from a
  UTF-16 count; `__post_init__` refusing what `text_window_error` refuses.
- **`tests/test_api_message_attachment.py`**: the resolver against a seeded
  message — each index, the entry's own filename, past-the-end 404, ungranted
  404 indistinguishable from missing, `index=-1` refused **and** not answered
  with the last entry, `MAX_JSONB_INDEX + 1` a 404 **without** a query (a
  connection that raises if touched), and `MAX_JSONB_INDEX` itself a 404 from
  the query, not a 500.
- **`tests/test_api_attachment_text_page.py`**: the accessor's SQL window against
  real rows — pages concatenate to the whole text, including an astral-plane
  one; the clamp reaches Postgres as an in-range `int4` for an offset past
  `2**31`; `get_attachment_text` still returns the whole string.
- **`tests/test_serve_message_attachment_routes.py`**: bytes — 200 with the
  entry's own filename, 206, 416, 304, ETag shared by two indices of one blob,
  404s, 400 problem+json for a non-digit index; the 304 spy on this module's
  `_open_blob_file_at`; text — paging, `next_offset`, the 400s.
- **`tests/test_serve_attachments_routes.py`** gains the text paging on the sha
  route. Its streaming tests stay unchanged and green, which is the pin that
  the extraction moved no behaviour. **Correction, found while planning:** an
  earlier wording said *all* its existing tests stay unchanged. Two do not —
  `test_attachment_text` there and `test_serve_acl_routes.py`'s text test
  assert the body with `==`, so they gain the four new keys. That is the
  additive wire change stated in §3, not a regression. The MCP pin
  (`test_mcp_tools.py`) is unchanged because MCP is.
- **Acceptance** (`tests/test_attachment_index_acceptance.py`): a message built
  by `_eml.two_attachments_same_name()` — two `note.txt` parts, different bytes
  — serves `file-one` at index 0 and `file-two` at index 1, each under its own
  name; the text route pages; and `/v1/attachments/{sha256}` answers
  byte-identically to a golden captured before the extraction.
- **`tests/test_serve_version_route.py`**: `api_minor >= 2`, the `>=` its
  `headers=list` sibling uses, so a later bump does not break it.

## Documentation

README's endpoint table gains the two routes and the text paging parameters;
CLAUDE.md's GUI-server section gains a bullet for this slice; `docs/mcp-usage.md`
is unchanged (MCP is out of scope).
