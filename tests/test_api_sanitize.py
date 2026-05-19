import pytest

from localmail.api.sanitize import sanitize_html


def test_script_tag_stripped() -> None:
    html = "<p>hi</p><script>alert(1)</script>"
    out = sanitize_html(html, cid_to_sha={})
    assert "<script>" not in out
    assert "alert" not in out
    assert "<p>hi</p>" in out


def test_event_handlers_stripped() -> None:
    html = '<a href="x" onclick="bad()">click</a>'
    out = sanitize_html(html, cid_to_sha={})
    assert "onclick" not in out
    assert ">click</a>" in out


def test_external_image_src_stripped() -> None:
    html = '<img src="https://tracker.example/pixel.gif">'
    out = sanitize_html(html, cid_to_sha={})
    assert "tracker.example" not in out
    assert "src=" not in out or "src=\"\"" in out


def test_cid_image_rewritten_to_attachment_url() -> None:
    cid_to_sha = {"image1@example": "deadbeef" * 8}
    html = '<img src="cid:image1@example">'
    out = sanitize_html(html, cid_to_sha=cid_to_sha)
    assert "/v1/attachments/" + ("deadbeef" * 8) in out
    assert "cid:" not in out


def test_unknown_cid_stripped() -> None:
    html = '<img src="cid:missing@example">'
    out = sanitize_html(html, cid_to_sha={})
    assert "cid:" not in out


def test_safe_styles_passed_through_inline() -> None:
    html = '<p style="color: red">x</p>'
    out = sanitize_html(html, cid_to_sha={})
    assert "<p" in out
    assert "x</p>" in out


def test_data_uri_image_allowed() -> None:
    html = '<img src="data:image/png;base64,AAAA">'
    out = sanitize_html(html, cid_to_sha={})
    assert "data:image/png" in out


def test_style_color_kept() -> None:
    """Safe CSS — colors, fonts, borders — must survive the CSS sanitizer.

    Whitespace inside a style declaration is implementation-defined (some
    sanitisers preserve ``color: red``, ``nh3`` normalises to ``color:red``).
    The security invariant is that the property and value survive together
    — the assertions accept either form.
    """
    html = '<p style="color: red; font-weight: bold">x</p>'
    out = sanitize_html(html, cid_to_sha={})
    assert "color" in out and "red" in out
    assert "font-weight" in out and "bold" in out


def test_style_background_image_url_dropped() -> None:
    """``background-image`` is excluded from ``_ALLOWED_STYLE_PROPERTIES``."""
    html = '<p style="background-image: url(https://tracker.example/p.png)">x</p>'
    out = sanitize_html(html, cid_to_sha={})
    assert "tracker.example" not in out
    assert "background-image" not in out


def test_style_position_fixed_dropped() -> None:
    """``position`` is omitted from the allowlist — blocks overlay/clickjacking via inline style.

    Surviving properties (``color: red``) should remain regardless of the
    sanitiser's whitespace normalisation.
    """
    html = '<div style="position: fixed; top: 0; left: 0; color: red">overlay</div>'
    out = sanitize_html(html, cid_to_sha={})
    assert "position" not in out
    assert "color" in out and "red" in out


def test_style_javascript_url_dropped() -> None:
    """A `javascript:` URL inside CSS (e.g. background) must not survive."""
    html = '<p style="background: url(javascript:alert(1))">x</p>'
    out = sanitize_html(html, cid_to_sha={})
    assert "javascript:" not in out
    assert "alert" not in out


def test_external_image_unquoted_src_stripped() -> None:
    """Unquoted srcs (`src=foo`) must also be neutralised."""
    html = "<img src=https://tracker.example/pixel.gif>"
    out = sanitize_html(html, cid_to_sha={})
    assert "tracker.example" not in out


def test_external_image_single_quoted_src_stripped() -> None:
    """Single-quoted srcs must also be neutralised."""
    html = "<img src='https://tracker.example/pixel.gif'>"
    out = sanitize_html(html, cid_to_sha={})
    assert "tracker.example" not in out


def test_cid_unquoted_src_rewritten() -> None:
    """cid: references work regardless of quoting style."""
    cid_to_sha = {"image1@example": "cafef00d" * 8}
    html = "<img src=cid:image1@example>"
    out = sanitize_html(html, cid_to_sha=cid_to_sha)
    assert "/v1/attachments/" + ("cafef00d" * 8) in out
    assert "cid:" not in out


def test_anchor_gains_rel_noopener_noreferrer() -> None:
    """nh3 auto-injects ``rel="noopener noreferrer"`` on every ``<a>`` tag.

    This is cheap tabnabbing protection — even if the message renderer
    later opens a sanitised link in a new context, the opener reference
    cannot be hijacked.
    """
    html = '<a href="https://example.com/foo">visit</a>'
    out = sanitize_html(html, cid_to_sha={})
    assert "noopener" in out
    assert "noreferrer" in out
    assert "https://example.com/foo" in out


def test_script_src_cid_does_not_leak_attachment_url() -> None:
    """``<script src="cid:...">`` must not surface a rewritten attachment URL.

    The ``attribute_filter`` only rewrites ``img/src`` (it returns the
    value unchanged for ``script/src``), but ``clean_content_tags``
    removes the whole ``<script>`` tag plus its content before any URL
    surfaces in the output. Both layers are pinned here so a future
    relaxation of either is forced to update this test.
    """
    cid_to_sha = {"evil@example": "deadbeef" * 8}
    html = '<p>before</p><script src="cid:evil@example">alert(1)</script><p>after</p>'
    out = sanitize_html(html, cid_to_sha=cid_to_sha)
    assert "deadbeef" not in out
    assert "/v1/attachments/" not in out
    assert "alert" not in out
    assert "<script" not in out
    assert "<p>before</p>" in out
    assert "<p>after</p>" in out


def test_iframe_content_fully_stripped() -> None:
    """``<iframe>`` content must be removed along with the tag itself."""
    html = '<p>ok</p><iframe src="javascript:alert(1)">fallback text</iframe><p>tail</p>'
    out = sanitize_html(html, cid_to_sha={})
    assert "<iframe" not in out
    assert "javascript:" not in out
    assert "alert" not in out
    assert "fallback text" not in out
    assert "<p>ok</p>" in out
    assert "<p>tail</p>" in out


def test_unclosed_script_tag_content_stripped() -> None:
    """An unclosed ``<script>`` at end-of-doc must still be neutralised.

    The previous regex pre-pass required a matching ``</script>``; nh3
    delegates to ``html5ever`` which treats everything after ``<script>``
    as script content until EOF.
    """
    html = "<p>visible</p><script>alert('escaped')"
    out = sanitize_html(html, cid_to_sha={})
    assert "<script" not in out
    assert "alert" not in out
    assert "<p>visible</p>" in out


def test_comments_stripped() -> None:
    """HTML comments must not survive (could leak server-side notes)."""
    html = "<p>shown</p><!-- secret server comment --><p>also shown</p>"
    out = sanitize_html(html, cid_to_sha={})
    assert "secret" not in out
    assert "<!--" not in out
    assert "<p>shown</p>" in out
    assert "<p>also shown</p>" in out


def test_data_uri_with_quote_injection_neutralised() -> None:
    """A crafted ``data:`` value containing ``"`` must not echo into the output.

    ``_DATA_IMAGE_RE`` is a full-match against the base64 alphabet, so any
    non-base64 character (here ``"``) fails validation and the src is
    stripped instead of forwarded. Without that, the rewriter would emit
    ``src="data:image/png;base64,AAA"onerror=alert(1)"`` and the parser
    would surface ``onerror`` as a separate attribute (nh3's attribute
    allowlist would then strip it — but we don't want to rely on that
    second line of defence for an injection the rewriter itself could
    have prevented).
    """
    html = '<img src=\'data:image/png;base64,AAA"onerror=alert(1)\'>'
    out = sanitize_html(html, cid_to_sha={})
    assert "onerror" not in out
    assert "alert" not in out
    assert 'data:image/png;base64,AAA"' not in out


def test_data_uri_with_angle_bracket_injection_neutralised() -> None:
    """``<`` / ``>`` inside a ``data:`` value must not echo into the output."""
    html = '<img src=\'data:image/png;base64,AAA<script>x</script>\'>'
    out = sanitize_html(html, cid_to_sha={})
    assert "<script" not in out
    assert "</script" not in out


def test_data_src_attribute_does_not_alias_real_src() -> None:
    """``data-src="…tracker…"`` must not survive even though the regex
    matches the embedded ``src=`` substring.

    The rewriter rewrites the inner ``src=…`` to ``src=""`` (corrupting
    the ``data-src`` value), and nh3 then drops ``data-src`` entirely
    because it is not in ``_ALLOWED_ATTRS``. Pins behaviour against
    accidental regression if anyone widens the attribute allowlist.
    """
    cid_to_sha = {"safe@example": "f" * 64}
    html = '<img data-src="https://tracker.example/p.png" src="cid:safe@example">'
    out = sanitize_html(html, cid_to_sha=cid_to_sha)
    assert "tracker.example" not in out
    assert "data-src" not in out
    assert "/v1/attachments/" + ("f" * 64) in out


def test_svg_tag_stripped() -> None:
    """``<svg>`` is not in ``_ALLOWED_TAGS``; its tag must be stripped.

    SVG is a known XSS vector (``<svg onload=…>``, embedded ``<script>``).
    Per nh3's default behaviour for unlisted tags, only the tag wrapping
    is removed and inner text survives — which is correct here: any
    ``<script>`` inside is removed by ``clean_content_tags``, any
    ``onload`` event handlers vanish with the parent ``<svg>``.
    """
    html = '<p>before</p><svg onload="alert(1)"><script>alert(2)</script>vis</svg><p>after</p>'
    out = sanitize_html(html, cid_to_sha={})
    assert "<svg" not in out
    assert "onload" not in out
    assert "alert" not in out
    assert "<script" not in out
    assert "<p>before</p>" in out
    assert "<p>after</p>" in out


def test_href_query_string_with_src_param_preserved() -> None:
    """``href`` URLs whose query string contains ``src=`` must survive intact.

    The previous regex pre-pass matched ``src=…`` anywhere in the raw
    HTML, including inside ``<a href>`` query strings — which silently
    corrupted legitimate ``?src=…``/``?utm_source=…&src=…`` tracking
    links. The new design delegates rewriting to nh3's
    ``attribute_filter``, which is parser-aware: it only sees attribute
    values in the proper tag context, so an ``href`` query-string
    ``src=foo`` is never confused with an ``<img src=foo>`` attribute.
    Closes issue #43.

    nh3 HTML-encodes ``&`` to ``&amp;`` in attribute values; assert the
    URL parts separately so the test is whitespace- and
    entity-encoding-agnostic.
    """
    html = '<a href="https://example.com/page?src=foo&x=1">link</a>'
    out = sanitize_html(html, cid_to_sha={})
    assert "https://example.com/page?src=foo" in out
    assert "x=1" in out
    assert ">link</a>" in out


def test_href_with_cid_scheme_stripped() -> None:
    """``<a href="cid:...">`` must have the href stripped.

    ``cid`` is in ``_ALLOWED_URL_SCHEMES`` so that the cid →
    attachment-URL rewrite in ``attribute_filter`` reaches img/src values
    (without ``cid`` in the scheme allowlist, nh3 drops the attribute
    before the filter ever runs). The same filter defensively returns
    ``None`` for ``cid:`` on ``<a href>`` so an attacker cannot turn a
    clicked link into an internal attachment fetch — even one that would
    resolve to a known blob.
    """
    cid_to_sha = {"exfil@example": "a" * 64}
    html = '<a href="cid:exfil@example">click</a>'
    out = sanitize_html(html, cid_to_sha=cid_to_sha)
    assert "cid:" not in out
    assert "/v1/attachments/" not in out
    assert ">click</a>" in out


def test_anchor_text_containing_img_substring_preserved() -> None:
    """Tag-shaped substrings inside attribute values must not be mistaken for tags.

    The previous regex pre-pass operated on raw HTML text, so a ``title``
    attribute containing the literal substring ``<img src=foo>`` would
    have its ``src=`` rewritten as if it were a real img tag. The
    parser-based filter only sees attribute values in their proper tag
    context, so substrings cannot be confused with tags.
    """
    html = '<a href="https://example.com/" title="see &lt;img src=foo&gt; below">x</a>'
    out = sanitize_html(html, cid_to_sha={})
    assert "https://example.com/" in out
    assert ">x</a>" in out
    assert "foo" in out


def test_href_with_uppercase_cid_scheme_stripped() -> None:
    """URL schemes are case-insensitive per RFC 3986; ``CID:`` must be dropped too.

    The ``attribute_filter`` lowercases the value before matching the
    ``cid:`` prefix, so case-variant attacks (``CID:``, ``Cid:``,
    ``cId:``) cannot bypass the href defence. Pins the ``.lower()``
    call against accidental removal.
    """
    cid_to_sha = {"exfil@example": "a" * 64}
    html = '<a href="CID:exfil@example">click</a>'
    out = sanitize_html(html, cid_to_sha=cid_to_sha)
    assert "CID:" not in out
    assert "cid:" not in out.lower()
    assert "/v1/attachments/" not in out
    assert ">click</a>" in out


def test_cid_substring_in_title_attribute_preserved() -> None:
    """The ``attribute_filter`` must not over-reach into non-URL attributes.

    A ``title`` attribute happens to allow free text including
    ``cid:`` substrings. The filter's ``href``-only ``cid:`` block must
    not also strip ``title`` values; the only attributes that can act on
    a URL are ``img/src`` (rewritten) and ``a/href`` (cid: dropped).
    """
    html = '<span title="see cid:foo for the inline image">x</span>'
    out = sanitize_html(html, cid_to_sha={})
    assert 'title="see cid:foo for the inline image"' in out
    assert ">x</span>" in out


def test_href_with_data_scheme_stripped() -> None:
    """``<a href="data:...">`` must have the href stripped.

    ``data`` is in ``_ALLOWED_URL_SCHEMES`` to support inline
    ``<img src="data:image/...;base64,...">`` images (validated end-to-end
    by ``_DATA_IMAGE_RE``). The same scheme on ``<a href>`` is a
    long-standing XSS vector — modern browsers block top-level navigation
    to ``data:`` URLs and the serve middleware applies CSP, but the
    sanitiser should not hand the renderer a ``javascript:``-equivalent
    payload and rely on downstream defences to neutralise it. The
    ``attribute_filter`` therefore drops ``data:`` on ``a/href``
    defensively. Closes issue #45.
    """
    html = '<a href="data:text/html,<script>alert(1)</script>">click</a>'
    out = sanitize_html(html, cid_to_sha={})
    assert "data:" not in out
    assert "<script" not in out
    assert "alert" not in out
    assert ">click</a>" in out


def test_href_with_uppercase_data_scheme_stripped() -> None:
    """URL schemes are case-insensitive per RFC 3986; ``DATA:`` must be dropped too.

    The ``attribute_filter`` lowercases the value before matching the
    ``data:`` prefix, so case-variant attacks (``DATA:``, ``Data:``,
    ``dAtA:``) cannot bypass the href defence.
    """
    html = '<a href="DATA:text/html,<script>alert(1)</script>">click</a>'
    out = sanitize_html(html, cid_to_sha={})
    assert "DATA:" not in out
    assert "data:" not in out.lower()
    assert "<script" not in out
    assert ">click</a>" in out


def test_data_substring_in_title_attribute_preserved() -> None:
    """The ``data:`` href block must not over-reach into non-URL attributes.

    A ``title`` attribute happens to allow free text including ``data:``
    substrings. The filter's ``href``-only ``data:`` block must not also
    strip ``title`` values; the only attributes that can act on a URL
    are ``img/src`` (where ``data:image/...`` images are validated by
    ``_DATA_IMAGE_RE``) and ``a/href`` (data: dropped).
    """
    html = '<span title="see data:image/png... discussion">x</span>'
    out = sanitize_html(html, cid_to_sha={})
    assert 'title="see data:image/png... discussion"' in out
    assert ">x</span>" in out


@pytest.mark.parametrize("leading", [
    " ",      # ASCII space
    "\t",     # tab
    "\n",     # LF
    "\r",     # CR
    "\x0c",   # form-feed
    "  \t\n", # mixed run
])
def test_href_with_leading_whitespace_data_scheme_stripped(leading: str) -> None:
    """Leading whitespace / C0 controls must not bypass the ``data:`` deny.

    The WHATWG URL parser strips leading C0 controls + ASCII whitespace
    before identifying the scheme, and so does ammonia's scheme allowlist
    check — but nh3 hands the raw, *un*stripped value to the
    ``attribute_filter``. A naive ``startswith("data:")`` therefore lets
    payloads like ``<a href=" data:text/html,...">`` (space) or
    ``<a href="\\tdata:...">`` (tab) survive, even though a browser would
    happily navigate to them. The filter normalises the value via
    ``_LEADING_URL_TRIM_RE`` before the prefix match to close this gap.
    """
    html = f'<a href="{leading}data:text/html,<script>alert(1)</script>">click</a>'
    out = sanitize_html(html, cid_to_sha={})
    assert "data:" not in out
    assert "<script" not in out
    assert "alert" not in out
    assert ">click</a>" in out


@pytest.mark.parametrize("leading", [" ", "\t", "\n", "\r", "\x0c"])
def test_href_with_leading_whitespace_cid_scheme_stripped(leading: str) -> None:
    """Same bypass surface for ``cid:`` — same normalisation must catch it.

    Pre-existing since the bleach→nh3 migration; surfaced while fixing #45.
    A ``<a href=" cid:foo">`` would otherwise let a clicked link fetch an
    internal attachment despite the explicit href deny.
    """
    html = f'<a href="{leading}cid:foo">click</a>'
    out = sanitize_html(html, cid_to_sha={"foo": "abc123"})
    assert "cid:" not in out.lower()
    assert "/v1/attachments/" not in out
    assert ">click</a>" in out


def test_href_with_html_entity_space_data_scheme_stripped() -> None:
    """``&#x20;data:...`` decodes to ``" data:..."`` in html5ever.

    The literal-whitespace bypass also reaches the filter via an HTML
    numeric character reference for U+0020. Same normalisation path
    (``_LEADING_URL_TRIM_RE``) closes it.
    """
    html = '<a href="&#x20;data:text/html,<script>alert(1)</script>">click</a>'
    out = sanitize_html(html, cid_to_sha={})
    assert "data:" not in out
    assert "<script" not in out
    assert "alert" not in out
    assert ">click</a>" in out


def test_href_with_leading_whitespace_legit_scheme_preserved() -> None:
    """Normalisation must not over-reach to schemes outside the deny list.

    Leading whitespace on a legit ``https:`` URL should not cause the
    filter to drop the href — only ``cid:`` and ``data:`` are denied.
    nh3 itself preserves the literal whitespace in the output (the WHATWG
    parser would strip it at navigation time), and the deny check is the
    only thing that uses the normalised value.
    """
    html = '<a href=" https://example.com">ok</a>'
    out = sanitize_html(html, cid_to_sha={})
    assert 'href=" https://example.com"' in out
    assert ">ok</a>" in out
