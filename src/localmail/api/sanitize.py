# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

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
- Image-source rewriting is done inside nh3's ``attribute_filter``
  callback (parser-aware, runs after the attribute allowlist and before
  the URL-scheme check). The earlier regex pre-pass — which matched
  ``src=…`` anywhere in the raw HTML, including inside ``<a href>``
  query strings — has been removed; that pre-pass silently corrupted
  links like ``?src=foo&x=1`` (issue #43). The filter only sees
  attribute values in their proper tag context, so an ``href`` query
  string is never confused with an ``<img src>`` attribute.
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
from typing import Callable

import nh3

_ALLOWED_TAGS: frozenset[str] = frozenset({
    "a", "abbr", "b", "blockquote", "br", "cite", "code", "div",
    "em", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "i", "img",
    "li", "ol", "p", "pre", "q", "small", "span", "strong", "sub",
    "sup", "table", "tbody", "td", "th", "thead", "tr", "u", "ul",
})

# SECURITY-CRITICAL: any new URL-interpreted attribute added here (e.g.
# ``srcset`` on ``<source>``, ``poster`` on ``<video>``, ``formaction`` on
# ``<button>``, ``data`` on ``<object>``) MUST get a parallel branch in
# ``_make_attribute_filter`` — otherwise ``cid:`` values would survive on
# a browser-dereferenced attribute, and ``http(s)://`` trackers would
# survive on any new image-like attribute. Today the filter handles
# exactly ``img/src`` (rewrite/strip) and ``a/href`` (drop ``cid:``);
# that matches this allowlist.
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
# does NOT strip ``<img src="http://tracker/…">``. The ``attribute_filter``
# below is the *sole* defense against image trackers — it returns
# ``None`` for every ``img/src`` whose value is not a known-good ``cid:``
# or validated ``data:image/…;base64,…`` URI.
#
# ``cid`` is allowed here so that ``<img src="cid:…">`` values reach
# the ``attribute_filter`` (when the scheme is rejected up front nh3
# strips the attribute before the filter ever runs). The filter rewrites
# img/src cid: URLs to scheme-relative ``/v1/attachments/<sha>`` paths
# and drops cid: on every other URL attribute (currently only
# ``<a href>`` via ``_HREF_DENY_SCHEMES``), so an attacker cannot turn a
# clicked link into an internal attachment fetch.
#
# ``data`` enables inline base64 image URIs validated end-to-end by
# ``_DATA_IMAGE_RE`` (full match, base64 alphabet only — no embedded
# quote/lt/gt that could confuse the parser). The filter drops data: on
# ``<a href>`` too — ``data:text/html,...`` is a ``javascript:``-equivalent
# payload in renderers that don't sandbox top-level navigation to
# data URLs.
_ALLOWED_URL_SCHEMES: frozenset[str] = frozenset({
    "mailto", "http", "https", "data", "cid",
})

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

# SECURITY-CRITICAL: URL schemes that must be dropped from ``<a href>``
# even though they appear in ``_ALLOWED_URL_SCHEMES``. ``cid`` and ``data``
# are in the scheme allowlist so that the ``attribute_filter`` reaches
# ``img/src`` values (where ``cid:`` is rewritten to attachment URLs and
# ``data:image/...`` is validated by ``_DATA_IMAGE_RE``). Surfacing either
# scheme on ``<a href>`` re-introduces an XSS / exfil vector:
#   - ``cid:`` would let a clicked link fetch an internal attachment;
#   - ``data:text/html,...`` is a ``javascript:``-equivalent payload in
#     any renderer that doesn't sandbox top-level navigation to data URLs
#     (closes #45).
# Schemes are compared lowercased; the tuple is the prefix set for
# ``str.startswith``. Leading C0 controls + ASCII whitespace are stripped
# first (see ``_LEADING_URL_TRIM_RE``).
_HREF_DENY_SCHEMES: tuple[str, ...] = ("cid:", "data:")

# WHATWG URL parser (and ammonia's scheme allowlist check, by extension)
# strips leading C0 controls + ASCII whitespace before identifying the
# scheme. nh3 then hands the raw, *un*stripped value to the
# ``attribute_filter``, which means a literal-leading-whitespace payload
# like ``<a href=" data:text/html,...">`` would pass the allowlist (scheme
# parses as ``data``) yet bypass a naive ``startswith("data:")`` prefix
# check. HTML entities decoded by html5ever (e.g. ``&#x20;data:...``) hit
# the filter the same way. Mirror the URL parser's leading-trim here.
# Range ``\x00-\x20`` covers NULL, every C0 control (tab/LF/FF/CR included),
# and the space character — exactly what a browser will discard before
# navigating.
_LEADING_URL_TRIM_RE = re.compile(r"^[\x00-\x20]+")

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

    Image-source rewriting and external-tracker stripping are performed
    inside nh3's ``attribute_filter`` callback so they see attribute
    values in proper tag context — an ``href`` URL with a ``?src=…``
    query string is never confused with an ``<img src=…>`` attribute.
    Dangerous tags (``script``/``style``/``iframe``/…) are removed
    *together with their content* by nh3's ``clean_content_tags``.
    Inline ``style`` attributes are kept but filtered to
    ``_ALLOWED_STYLE_PROPERTIES``.
    """
    return nh3.clean(
        html,
        tags=set(_ALLOWED_TAGS),
        attributes=_ALLOWED_ATTRS,
        attribute_filter=_make_attribute_filter(cid_to_sha),
        url_schemes=set(_ALLOWED_URL_SCHEMES),
        clean_content_tags=set(_CLEAN_CONTENT_TAGS),
        filter_style_properties=set(_ALLOWED_STYLE_PROPERTIES),
        strip_comments=True,
    )


def _make_attribute_filter(
    cid_to_sha: dict[str, str],
) -> Callable[[str, str, str], str | None]:
    """Build the per-call ``attribute_filter`` for ``nh3.clean``.

    The returned callable is invoked by nh3 for every attribute that
    survives the tag/attr allowlist check, *before* the URL-scheme check.
    Returning ``None`` drops the attribute; returning a string replaces
    its value. Two responsibilities:

    1. **Rewrite ``<img src="cid:…">``** to ``/v1/attachments/<sha>`` if
       the cid resolves to a known blob, otherwise drop the src.
       Pass-through validated ``data:image/…;base64,…`` URIs. Drop
       everything else on ``img/src`` (this is the sole defence against
       image trackers — ``http``/``https`` are in ``_ALLOWED_URL_SCHEMES``
       so nh3 itself would let them through).
    2. **Block ``cid:`` and ``data:`` on non-img URL attributes**
       (currently just ``<a href>``). Both schemes are in
       ``_ALLOWED_URL_SCHEMES`` so that their values reach this filter for
       img/src handling (cid rewrite, data:image validation). Surfacing
       them on ``<a href>`` would re-expose an attachment fetch on click
       (cid) or a ``javascript:``-equivalent payload in renderers that
       don't sandbox top-level navigation to data URLs (data). The deny
       list is stored in ``_HREF_DENY_SCHEMES``. Leading C0 controls +
       ASCII whitespace are stripped before the prefix match because the
       WHATWG URL parser does the same before scheme detection — without
       this normalisation a payload like ``<a href=" data:...">`` or its
       entity-decoded equivalent (``&#x20;data:...``) would pass the
       scheme allowlist yet bypass a naive prefix check.
    """
    def _filter(tag: str, attr: str, value: str) -> str | None:
        if tag == "img" and attr == "src":
            return _rewrite_img_src(value, cid_to_sha)
        if attr == "href":
            normalised = _LEADING_URL_TRIM_RE.sub("", value).lower()
            if normalised.startswith(_HREF_DENY_SCHEMES):
                return None
        return value

    return _filter


def _rewrite_img_src(value: str, cid_to_sha: dict[str, str]) -> str | None:
    """Decide what to do with the ``src`` of an ``<img>`` element.

    Returns the rewritten value (or ``None`` to drop the attribute):

    - ``cid:<id>`` resolves to ``/v1/attachments/<sha>`` if the id is
      known, otherwise ``None`` (broken cid, drop). Angle brackets
      sometimes appearing around the id (``cid:<image1@host>``) are
      stripped before lookup.
    - ``data:image/<mime>;base64,<data>`` passes through iff it matches
      ``_DATA_IMAGE_RE`` end-to-end (no embedded quote/lt/gt).
    - Anything else (``http(s)://…``, ``//tracker/…``, relative paths,
      ``javascript:…``, …) returns ``None`` — image trackers are blocked
      regardless of how the attacker tries to dress them up.
    """
    cid_match = _CID_RE.match(value.strip("<>"))
    if cid_match:
        cid = cid_match.group(1).strip("<>")
        sha = cid_to_sha.get(cid)
        if sha is None:
            return None
        return f"/v1/attachments/{sha}"
    if _DATA_IMAGE_RE.match(value):
        return value
    return None
