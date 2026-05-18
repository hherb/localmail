"""HTML sanitizer for message bodies.

External resource loading is blocked by default; only ``cid:`` references
that resolve to an attachment-blob SHA-256 are rewritten to internal URLs.
The serve layer further constrains the rendered output via
Content-Security-Policy.

Design notes:

- Sanitisation is delegated to ``nh3`` (Python bindings over Rust's
  ``ammonia``). It uses ``html5ever`` for parsing, which is robust against
  the mutation-XSS bypasses that plague regex- and DOM-based sanitisers.
- ``clean_content_tags`` drops ``<script>``/``<style>``/etc. *together
  with their inner content*, so an attacker cannot leak script source as
  visible text via strip-style behaviour.
- Inline ``style`` attributes are preserved (HTML email relies on them)
  but every CSS declaration is checked against
  ``_ALLOWED_STYLE_PROPERTIES``. URL-loading shorthands (``background``,
  ``background-image``, ``list-style``, ``cursor``…) and overlay
  primitives (``position``, ``z-index``, ``top``/``left``/…) are NOT in
  the allowlist, so CSS-based exfil and click-jacking are blocked.
  ``nh3``'s own normaliser additionally drops syntactically invalid
  declarations and ``@rules``.
- ``nh3`` adds ``rel="noopener noreferrer"`` to every ``<a>`` tag by
  default — kept on for cheap tabnabbing protection.
"""
from __future__ import annotations

import re

import nh3

_ALLOWED_TAGS: frozenset[str] = frozenset({
    "a", "abbr", "b", "blockquote", "br", "cite", "code", "div",
    "em", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "i", "img",
    "li", "ol", "p", "pre", "q", "small", "span", "strong", "sub",
    "sup", "table", "tbody", "td", "th", "thead", "tr", "u", "ul",
})

_ALLOWED_ATTRS: dict[str, set[str]] = {
    "*": {"class", "style", "title"},
    "a": {"href"},
    "img": {"src", "alt", "width", "height"},
    "td": {"colspan", "rowspan", "align"},
    "th": {"colspan", "rowspan", "align"},
}

# ``http``/``https`` are required so legitimate ``<a href="https://…">``
# links survive — nh3 drops the ``href`` entirely if the scheme is not in
# this set. The rewritten ``/v1/attachments/<sha>`` attachment URLs are
# scheme-relative and pass through regardless of this allowlist.
#
# SECURITY-CRITICAL: because ``http``/``https`` are allowed, nh3 itself
# does NOT strip ``<img src="http://tracker/…">``. ``_rewrite_image_srcs``
# below is the *sole* defense against image trackers — it strips every
# ``src=…`` whose value is not a known-good ``cid:`` or validated
# ``data:image/…;base64,…`` URI before nh3 sees the document. Anyone
# tightening that regex must keep it broad enough to catch every
# attribute-shaped ``src=…`` an attacker might emit.
#
# ``data`` enables inline base64 image URIs validated end-to-end by
# ``_DATA_IMAGE_RE`` (full match, base64 alphabet only — no embedded
# quote/lt/gt that could confuse the parser).
_ALLOWED_URL_SCHEMES: frozenset[str] = frozenset({"mailto", "http", "https", "data"})

# Tags whose inner *content* is removed along with the tag itself.
# ``html5ever`` handles malformed and self-closing variants correctly,
# so unclosed ``<script>`` at EOF is still neutralised.
_CLEAN_CONTENT_TAGS: frozenset[str] = frozenset({
    "script", "style", "noscript", "iframe", "object", "embed", "applet",
    "form",
})

# Safe CSS properties for inline ``style`` attributes — typography, box
# model, and table layout. Deliberately excluded:
#   - URL-loading shorthands: ``background``, ``background-image``,
#     ``border-image``, ``list-style``, ``list-style-image``, ``cursor``
#     (cursor URL form), ``content``.
#   - Overlay / click-jacking primitives: ``position``, ``top``, ``right``,
#     ``bottom``, ``left``, ``z-index``, ``clip``, ``clip-path``.
#   - Anything else that takes ``url()`` and could fetch a remote resource.
# ``nh3``'s own CSS normaliser additionally drops invalid declarations
# and ``@rules``, so this list is the *only* knob protecting render-time
# layout from inline-style attacks.
_ALLOWED_STYLE_PROPERTIES: frozenset[str] = frozenset({
    "azimuth", "background-color",
    "border", "border-bottom", "border-bottom-color", "border-bottom-style",
    "border-bottom-width", "border-collapse", "border-color",
    "border-left", "border-left-color", "border-left-style",
    "border-left-width",
    "border-right", "border-right-color", "border-right-style",
    "border-right-width",
    "border-spacing", "border-style",
    "border-top", "border-top-color", "border-top-style",
    "border-top-width",
    "border-width",
    "clear", "color", "direction", "display", "elevation",
    "float", "font", "font-family", "font-size", "font-style",
    "font-variant", "font-weight",
    "height", "letter-spacing", "line-height",
    "margin", "margin-bottom", "margin-left", "margin-right", "margin-top",
    "max-height", "max-width", "min-height", "min-width",
    "overflow",
    "padding", "padding-bottom", "padding-left", "padding-right",
    "padding-top",
    "pause", "pause-after", "pause-before", "pitch", "pitch-range",
    "richness",
    "speak", "speak-header", "speak-numeral", "speak-punctuation",
    "speech-rate", "stress",
    "table-layout",
    "text-align", "text-decoration", "text-indent",
    "unicode-bidi", "vertical-align", "voice-family", "volume",
    "white-space", "width",
})

_CID_RE = re.compile(r"^cid:(.+)$", re.IGNORECASE)
# Full-match (not prefix-match) so the rewriter never echoes an attacker-
# supplied ``"``/``<``/``>`` back into the document. The base64 alphabet
# is ``[A-Za-z0-9+/=]``; anything outside that — including the very chars
# that could break out of an attribute — fails the match and the src is
# stripped instead of forwarded.
_DATA_IMAGE_RE = re.compile(
    r"^data:image/(?:png|jpeg|gif|webp);base64,[A-Za-z0-9+/=]*$",
    re.IGNORECASE,
)


def sanitize_html(html: str, *, cid_to_sha: dict[str, str]) -> str:
    """Return a sanitized HTML string.

    Args:
      html: untrusted input from the email body.
      cid_to_sha: map of Content-ID (without 'cid:' prefix and without angle
        brackets) to attachment-blob SHA-256 hex strings. Used to rewrite
        ``<img src="cid:...">`` to the attachment URL.

    External src values (anything starting with ``http(s)://`` or ``//``)
    are stripped before nh3 sees them. Dangerous tags
    (``script``/``style``/``iframe``/…) are removed *together with their
    content* by nh3's ``clean_content_tags``. Inline ``style`` attributes
    are kept but filtered to ``_ALLOWED_STYLE_PROPERTIES``.
    """
    pre = _rewrite_image_srcs(html, cid_to_sha)
    return nh3.clean(
        pre,
        tags=set(_ALLOWED_TAGS),
        attributes=_ALLOWED_ATTRS,
        url_schemes=set(_ALLOWED_URL_SCHEMES),
        clean_content_tags=set(_CLEAN_CONTENT_TAGS),
        filter_style_properties=set(_ALLOWED_STYLE_PROPERTIES),
        strip_comments=True,
    )


_SRC_ATTR_RE = re.compile(
    r"""src\s*=\s*(?:"(?P<dq>[^"]*)"|'(?P<sq>[^']*)'|(?P<bare>[^\s>]+))""",
    re.IGNORECASE,
)


def _rewrite_image_srcs(html: str, cid_to_sha: dict[str, str]) -> str:
    """Replace cid:* srcs with /v1/attachments/<sha256>; strip everything else.

    Matches double-quoted, single-quoted, and unquoted ``src`` attribute
    forms so that external trackers cannot slip through by choosing a
    quoting style the regex didn't anticipate. The rewritten output is
    always emitted as a double-quoted attribute regardless of the input
    quoting.
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
