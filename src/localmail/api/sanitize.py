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
from bleach.css_sanitizer import CSSSanitizer

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
# "http"/"https" are required so bleach passes the root-relative
# /v1/attachments/… URLs produced by _rewrite_image_srcs (bleach uses
# urllib.parse internally and only resolves relative paths when a "real"
# scheme is in the allowed list). The src-rewrite pass below strips ALL
# external http/https srcs first — regardless of quoting style — so this
# entry does NOT permit remote tracker-pixels through. The data entry
# enables inline base64 image URIs validated by _DATA_IMAGE_RE.
_ALLOWED_PROTOCOLS = ["mailto", "http", "https", "data"]

_CID_RE = re.compile(r"^cid:(.+)$", re.IGNORECASE)
_DATA_IMAGE_RE = re.compile(r"^data:image/(png|jpeg|gif|webp);base64,", re.IGNORECASE)

# bleach defaults to a 46-property safe allowlist (no background-image, no
# position, no expression/behavior). We accept that allowlist verbatim so
# inline `style` attributes — which are very common in HTML email — still
# render fonts/colors/borders/etc. while CSS-based exfil and overlay
# attacks are dropped. Without a CSSSanitizer here, bleach allows style
# attributes through *unfiltered* and emits NoCssSanitizerWarning.
_CSS_SANITIZER = CSSSanitizer()

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
        css_sanitizer=_CSS_SANITIZER,
        strip=True,
        strip_comments=True,
    )


_SRC_ATTR_RE = re.compile(
    r"""src\s*=\s*(?:"(?P<dq>[^"]*)"|'(?P<sq>[^']*)'|(?P<bare>[^\s>]+))""",
    re.IGNORECASE,
)


def _rewrite_image_srcs(html: str, cid_to_sha: dict[str, str]) -> str:
    """Replace cid:* srcs with /v1/attachments/<sha256>; strip everything else.

    Matches double-quoted, single-quoted, and unquoted `src` attribute forms
    so that external trackers cannot slip through by choosing a quoting style
    the regex didn't anticipate. The rewritten output is always emitted as a
    double-quoted attribute regardless of the input quoting.
    """
    def replace_src(match: re.Match[str]) -> str:
        src = match.group("dq") or match.group("sq") or match.group("bare") or ""
        cid_match = _CID_RE.match(src.strip("<>"))
        if cid_match:
            cid = cid_match.group(1).strip("<>")
            sha = cid_to_sha.get(cid)
            if sha is None:
                return 'src=""'
            return f'src="/v1/attachments/{sha}"'
        if _DATA_IMAGE_RE.match(src):
            return f'src="{src}"'
        return 'src=""'

    return _SRC_ATTR_RE.sub(replace_src, html)
