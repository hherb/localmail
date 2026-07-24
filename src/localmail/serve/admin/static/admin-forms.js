// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (C) 2026 Horst Herb

// Surface server-side validation errors on admin htmx forms.
//
// htmx does NOT swap responses with a non-2xx status by default, so the
// admin forms' inline field errors — rendered by `_rerender_*_error` as a
// 400 text/html fragment whose root id matches the form's hx-target — were
// silently dropped, making a rejected Save look like it did nothing.
//
// Allow the swap for 400/422 responses, but ONLY when the body is HTML: a
// JSON error (e.g. the 400 that check_csrf raises) must not be swapped into
// the form as raw `{"detail": ...}` text. CSP for /admin is `script-src
// 'self'`, so this lives in a served file, not inline.
(function () {
  "use strict";
  document.body.addEventListener("htmx:beforeSwap", function (evt) {
    var xhr = evt.detail && evt.detail.xhr;
    if (!xhr) return;
    if (xhr.status === 400 || xhr.status === 422) {
      var ct = xhr.getResponseHeader("content-type") || "";
      if (ct.indexOf("text/html") !== -1) {
        evt.detail.shouldSwap = true;
        evt.detail.isError = false;
      }
    }
  });
})();
