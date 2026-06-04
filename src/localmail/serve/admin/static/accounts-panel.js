// Account form: show/hide field groups by the selected auth method.
// CSP for /admin is `script-src 'self'` (no inline handlers), so all behaviour
// is bound here from a served file (mirrors daemon-panel.js).
(function () {
  "use strict";

  function applyAuthVisibility(root) {
    var select = root.querySelector("[data-auth-select]");
    if (!select) return;
    var method = select.value;
    var groups = root.querySelectorAll("[data-auth-group]");
    groups.forEach(function (el) {
      var applies = el.getAttribute("data-auth-group").split(/\s+/);
      el.hidden = applies.indexOf(method) === -1;
    });
  }

  function wire(root) {
    var select = root.querySelector("[data-auth-select]");
    if (!select) return;
    select.addEventListener("change", function () { applyAuthVisibility(root); });
    applyAuthVisibility(root);
  }

  document.addEventListener("DOMContentLoaded", function () {
    var fields = document.getElementById("account-form-fields");
    if (fields) wire(fields);
  });

  // Re-wire after an HTMX swap replaces #account-form-fields (validation error).
  document.body.addEventListener("htmx:afterSwap", function (evt) {
    var fields = evt.target.querySelector
      ? (evt.target.id === "account-form-fields"
          ? evt.target
          : evt.target.querySelector("#account-form-fields"))
      : null;
    if (fields) wire(fields);
  });
})();
