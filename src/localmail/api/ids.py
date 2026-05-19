"""Wire-format ID parsing for the localmail.api boundary.

External callers (HTTP, MCP) send entity IDs as **strings** — BIGSERIAL values
can exceed JS Number's 2^53 safe-integer range, and a JSON-strict client
shouldn't have to guess whether the same field is sometimes int, sometimes
string. Internally, the SQL layer takes ints. This module owns the boundary
cast with a uniform `ValidationFailed` shape so every transport (FastAPI
route handler, MCP tool, future gRPC stub) emits the same problem+json on
malformed input.
"""
from __future__ import annotations

from localmail.api.errors import ValidationFailed


def parse_int_id(value: str, *, field: str) -> int:
    """Parse a wire-format string ID into an int.

    Strict: only non-empty strings of ASCII digits ``0``–``9`` are accepted.
    Leading ``+``/``-``, surrounding whitespace, decimal points, hex
    prefixes, and Unicode digit characters are all rejected — clients
    must encode IDs as plain base-10 strings, matching what the server
    emits in response bodies (``str(id)``).

    Leading zeros are accepted (``"01"`` resolves to ``1``), so two stable
    encodings of the same int are both valid path params. Harmless for SQL,
    but callers treating the wire form as an opaque key (cache, audit log,
    ETag) should canonicalise via ``str(parse_int_id(value, field=...))``.

    Args:
        value: The string ID as it arrived on the wire.
        field: Name of the field for the error message
            (e.g. ``"account_id"``, ``"message_id"``).

    Returns:
        The parsed positive (or zero) int.

    Raises:
        ValidationFailed: When `value` is not a strict base-10 integer.
            HTTP transports map this to ``400 application/problem+json``
            with ``type=/problems/validation-failed``.

    """
    if not value or not value.isascii() or not value.isdigit():
        raise ValidationFailed(
            f"{field} must be a base-10 integer, got {value!r}"
        )
    return int(value)
