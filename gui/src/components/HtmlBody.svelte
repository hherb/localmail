<script lang="ts">
  /**
   * Renders server-sanitised email HTML inside a sandboxed iframe with its
   * own CSP. The iframe carries `sandbox=""` (strictest — no scripts, forms,
   * popups, top-navigation, same-origin), so even a CSP-bypass payload can't
   * exfiltrate.
   *
   * The per-iframe <meta http-equiv="Content-Security-Policy"> blocks all
   * external resource loads by default; when `allowExternalImages` is true
   * we widen img-src to '*' for this message only.
   */
  let { html, allowExternalImages } = $props<{
    html: string;
    allowExternalImages: boolean;
  }>();

  function csp(): string {
    const imgSrc = allowExternalImages ? "*" : "'self' data:";
    return [
      "default-src 'none'",
      `img-src ${imgSrc}`,
      "style-src 'unsafe-inline'",
    ].join("; ");
  }

  function srcdoc(): string {
    // Inline CSP via meta http-equiv — browsers honor it for the embedded
    // document. <base target="_blank"> sends user-clicked links out to the
    // platform default browser (Tauri intercepts; Sub-plan 5 wires this).
    return `<!doctype html><html><head>` +
           `<meta http-equiv="Content-Security-Policy" content="${csp()}">` +
           `<base target="_blank">` +
           `<style>body{font:14px/1.4 system-ui,sans-serif;margin:8px;color:#222}</style>` +
           `</head><body>${html}</body></html>`;
  }
</script>

<iframe sandbox="" srcdoc={srcdoc()} title="message body"></iframe>

<style>
  iframe { width: 100%; height: 100%; border: none; background: #fff; }
</style>
