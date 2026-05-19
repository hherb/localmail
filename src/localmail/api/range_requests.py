"""HTTP Range header parsing for byte-stream endpoints (RFC 9110 §14.1).

Pure parsing utilities used by `/v1/attachments/{sha256}` to serve
`206 Partial Content` for `Range: bytes=…` requests. No IO; no FastAPI
dependencies. Used by `localmail.serve.routes.attachments`.

Design contract (matches what mainstream clients — browsers, video.js,
curl --range — expect):

* Absent header                          → ``None`` (caller serves full 200).
* Syntactically invalid bytes= header    → ``None`` (RFC 9110 §14.1.2 permits
                                            servers to ignore unparseable
                                            Range; we treat that as "serve 200
                                            full-response").
* Non-``bytes=`` range unit               → ``None``.
* Multi-range (``bytes=a-b,c-d``)         → ``None`` (we don't emit
                                            ``multipart/byteranges``; falling
                                            through to 200 is spec-compliant
                                            and matches what video / PDF
                                            clients actually use).
* Valid but **entirely** past EOF         → raises ``UnsatisfiableRange``
                                            (caller emits 416 with
                                            ``Content-Range: bytes */N``).
* Valid, partially-past-EOF end position  → end is clamped to ``size - 1``
                                            (RFC 9110 §14.1.2: "if the value
                                            is greater than or equal to the
                                            current length …, the byte range
                                            is interpreted as the remainder
                                            of the representation").
"""
from __future__ import annotations

from dataclasses import dataclass

RANGE_UNIT = "bytes"
RANGE_PREFIX = f"{RANGE_UNIT}="


@dataclass(frozen=True)
class ByteRange:
    """Inclusive ``[start, end]`` byte slice of a known-length resource.

    ``length`` is the number of bytes covered, i.e. ``end - start + 1``.
    Both endpoints are always within ``[0, size - 1]`` for the resource
    they were parsed against; callers can `.seek(start)` then read exactly
    ``length`` bytes without further bounds checks.
    """

    start: int
    end: int

    @property
    def length(self) -> int:
        return self.end - self.start + 1


class UnsatisfiableRange(Exception):
    """The Range header was parseable but no byte position overlaps the file.

    Distinct from ``ValueError`` so caller exception handlers can
    discriminate cleanly between "couldn't parse" (→ 200) and "parsed but
    invalid for this file" (→ 416).
    """


def parse_byte_range(header: str | None, total_size: int) -> ByteRange | None:
    """Parse a Range header against a resource of size ``total_size``.

    Args:
        header: Raw value of the ``Range`` HTTP header, or ``None``.
        total_size: Size of the resource in bytes; must be ``>= 0``.

    Returns:
        ``ByteRange`` if the header specifies a single, satisfiable
        byte range; ``None`` if the header is absent or syntactically
        invalid (caller serves the full resource with 200).

    Raises:
        UnsatisfiableRange: header parsed, but no byte position overlaps
            ``[0, total_size)``.
    """
    if header is None:
        return None
    if not header.startswith(RANGE_PREFIX):
        return None
    spec = header[len(RANGE_PREFIX):].strip()
    if not spec or "," in spec or "-" not in spec:
        return None
    start_str, end_str = (s.strip() for s in spec.split("-", 1))
    if start_str == "" and end_str == "":
        return None
    if total_size <= 0:
        raise UnsatisfiableRange("range request on empty resource")
    try:
        if start_str == "":
            return _suffix_range(end_str, total_size)
        if end_str == "":
            return _open_ended_range(start_str, total_size)
        return _closed_range(start_str, end_str, total_size)
    except ValueError:
        return None


def _suffix_range(end_str: str, total_size: int) -> ByteRange:
    """``bytes=-N`` — return the last ``N`` bytes (clamped to whole file)."""
    suffix = int(end_str)
    if suffix <= 0:
        raise UnsatisfiableRange(f"suffix length {suffix} is unsatisfiable")
    start = max(0, total_size - suffix)
    return ByteRange(start=start, end=total_size - 1)


def _open_ended_range(start_str: str, total_size: int) -> ByteRange:
    """``bytes=N-`` — return ``N`` to end of file."""
    start = int(start_str)
    if start < 0:
        raise ValueError("negative start")
    if start >= total_size:
        raise UnsatisfiableRange(f"start {start} >= size {total_size}")
    return ByteRange(start=start, end=total_size - 1)


def _closed_range(start_str: str, end_str: str, total_size: int) -> ByteRange:
    """``bytes=N-M`` — return ``N`` to ``M`` (``M`` clamped to size-1)."""
    start = int(start_str)
    end = int(end_str)
    if start < 0 or end < 0 or end < start:
        raise ValueError("malformed closed range")
    if start >= total_size:
        raise UnsatisfiableRange(f"start {start} >= size {total_size}")
    return ByteRange(start=start, end=min(end, total_size - 1))


def content_range_header(byte_range: ByteRange, total_size: int) -> str:
    """Build the ``Content-Range`` header value for a 206 response."""
    return f"{RANGE_UNIT} {byte_range.start}-{byte_range.end}/{total_size}"


def unsatisfiable_content_range(total_size: int) -> str:
    """Build the ``Content-Range`` header value for a 416 response."""
    return f"{RANGE_UNIT} */{total_size}"
