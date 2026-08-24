// The /admin CSP is script-src 'self' with no unsafe-inline, so the copy
// button cannot be wired with an inline handler or an htmx hx-on:: attribute.
document.addEventListener("click", (ev) => {
  const button = ev.target.closest("[data-copy-target]");
  if (!button) return;
  const field = document.getElementById(button.dataset.copyTarget);
  if (!field) return;
  field.select();
  navigator.clipboard.writeText(field.value).then(() => {
    button.textContent = "Copied";
  }).catch(() => {
    // No clipboard outside a secure context; the field is selected either way.
    button.textContent = "Press Ctrl/Cmd-C";
  });
});
