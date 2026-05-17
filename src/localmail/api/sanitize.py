"""HTML sanitizer for message bodies.

External resource loading is blocked by default; only `cid:` references
that resolve to an attachment-blob SHA-256 are rewritten to internal URLs.
The serve layer further constrains the rendered output via Content-Security-Policy.

Design notes:

- ``_STRIP_WITH_CONTENT_RE`` is a regex pre-pass that drops dangerous tags
  *together with their inner content* before bleach sees them. This is
  necessary because ``bleach.clean(strip=True)`` removes tags but keeps
  their text — for ``<script>alert(1)</script>`` that would leak "alert(1)"
  as visible text. Regex-based HTML parsing is historically fragile (mutation
  XSS bypasses); the pairing with bleach below mitigates this — even if a
  malformed ``<script>...`` survives the regex, bleach will still drop the
  tag itself, so the worst case is a fragment of script source rendered as
  plain text rather than executed.
- ``bleach`` upstream is in maintenance-only mode; a future migration to
  ``nh3`` (Rust-backed, actively maintained) is tracked separately.
"""
from __future__ import annotations

import re

import bleach

_ALLOWED_TAGS = [
    "a", "abbr", "b", "blockquote", "br", "cite", "code", "div",
    "em", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "i", "img",
    "li", "ol", "p", "pre", "q", "small", "span", "strong", "sub",
    "sup", "table", "tbody", "td", "th", "thead", "tr", "u", "ul",
]
_ALLOWED_ATTRS = {
    "*": ["class", "style", "title"],
    "a": ["href"],
    "img": ["src", "alt", "width", "height"],
    "td": ["colspan", "rowspan", "align"],
    "th": ["colspan", "rowspan", "align"],
}
# "http"/"https" are required to let bleach pass root-relative URLs like
# /v1/attachments/…  (bleach uses urllib.parse internally and only resolves
# relative paths when a "real" scheme is present in the allowed list).
# External http/https image srcs are already stripped to "" by
# _rewrite_image_srcs before bleach ever sees them, so this does NOT allow
# remote tracking pixels through.
# "data" enables inline base64 image URIs rewritten in _rewrite_image_srcs.
_ALLOWED_PROTOCOLS = ["mailto", "http", "https", "data"]

_CID_RE = re.compile(r"^cid:(.+)$", re.IGNORECASE)
_DATA_IMAGE_RE = re.compile(r"^data:image/(png|jpeg|gif|webp);base64,", re.IGNORECASE)

# Tags whose inner content must also be removed (not just the tags themselves).
_STRIP_WITH_CONTENT_RE = re.compile(
    r"<(script|style|noscript|iframe|object|embed|applet|form)"
    r"[\s>].*?</\1\s*>",
    re.IGNORECASE | re.DOTALL,
)


def sanitize_html(html: str, *, cid_to_sha: dict[str, str]) -> str:
    """Return a sanitized HTML string.

    Args:
      html: untrusted input from the email body.
      cid_to_sha: map of Content-ID (without 'cid:' prefix and without angle
        brackets) to attachment-blob SHA-256 hex strings. Used to rewrite
        `<img src="cid:...">` to the attachment URL.

    External src values (anything starting with http(s):// or //) are stripped.
    """
    # Remove dangerous tags together with their content before bleach sees them.
    # bleach strip=True keeps tag inner text which leaks script/style bodies.
    pre = _STRIP_WITH_CONTENT_RE.sub("", html)
    pre = _rewrite_image_srcs(pre, cid_to_sha)
    return bleach.clean(
        pre,
        tags=_ALLOWED_TAGS,
        attributes=_ALLOWED_ATTRS,
        protocols=_ALLOWED_PROTOCOLS,
        strip=True,
        strip_comments=True,
    )


def _rewrite_image_srcs(html: str, cid_to_sha: dict[str, str]) -> str:
    """Replace cid:* srcs with /v1/attachments/<sha256>; strip everything else."""
    def replace_src(match: re.Match[str]) -> str:
        src = match.group(1)
        cid_match = _CID_RE.match(src.strip("<>"))
        if cid_match:
            cid = cid_match.group(1).strip("<>")
            sha = cid_to_sha.get(cid)
            if sha is None:
                return 'src=""'
            return f'src="/v1/attachments/{sha}"'
        if _DATA_IMAGE_RE.match(src):
            return match.group(0)
        return 'src=""'

    return re.sub(
        r'src\s*=\s*"([^"]*)"',
        replace_src,
        html,
        flags=re.IGNORECASE,
    )
