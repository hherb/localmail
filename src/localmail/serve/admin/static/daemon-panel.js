// Daemon-control panel (#148): surface non-2xx responses from the mutating
// lifecycle / reload / restart-sync buttons as a transient toast.
//
// The buttons use hx-swap="none", so without this an error — a busy-guard 409
// or a CSRF 400 — leaves the button looking inert until the next status poll
// (and a 400 never changes the visible state, so it reads as "nothing
// happened"). We react to the POST result client-side; the /v1/admin/* routes
// stay pure machine-JSON (no HTML content negotiation).
//
// CSP for /admin is `script-src 'self'` (no unsafe-inline / unsafe-eval), so
// this lives in a served file and binds via addEventListener — an inline
// <script> or an htmx `hx-on::after-request` handler would be blocked.
(function () {
  "use strict";

  var TOAST_ID = "daemon-toast";
  var STATUS_BUSY = 409; // busy-guard: another lifecycle op already in flight
  var STATUS_CSRF = 400; // rejected / expired CSRF token
  var TOAST_TTL_MS = 6000;

  var MESSAGES = {};
  MESSAGES[STATUS_BUSY] =
    "Another daemon operation is already in progress. Try again in a moment.";
  MESSAGES[STATUS_CSRF] =
    "Request rejected (invalid or expired session). Reload the page and try again.";

  var hideTimer = null;

  function showToast(message, kind) {
    var el = document.getElementById(TOAST_ID);
    if (!el) {
      return;
    }
    el.textContent = message;
    el.dataset.kind = kind;
    el.hidden = false;
    if (hideTimer) {
      clearTimeout(hideTimer);
    }
    hideTimer = setTimeout(function () {
      el.hidden = true;
    }, TOAST_TTL_MS);
  }

  document.body.addEventListener("htmx:afterRequest", function (evt) {
    var cfg = evt.detail && evt.detail.requestConfig;
    // Only the mutating buttons POST; the periodic status refresh is a GET and
    // must not raise a toast on every tick.
    if (!cfg || cfg.verb !== "post") {
      return;
    }
    var status = evt.detail.xhr ? evt.detail.xhr.status : 0;
    if (status >= 200 && status < 300) {
      showToast("Request accepted.", "ok");
      return;
    }
    var message = MESSAGES[status] || "Request failed (HTTP " + status + ").";
    showToast(message, "error");
  });
})();
