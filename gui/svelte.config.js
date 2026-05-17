import { vitePreprocess } from "@sveltejs/vite-plugin-svelte";

// `style: false` works around a Vite 6 / vite-plugin-svelte vitest pipeline
// bug: enabling style preprocessing triggers a PartialEnvironment proxy crash
// inside preprocessCSS during vitest runs. We use only plain CSS in <style>
// blocks (no :global, no PostCSS directives), so Vite handles them natively
// and skipping Svelte's style pass is safe. Revert this option once the
// upstream issue ships a fix — see vite-plugin-svelte release notes.
export default {
  preprocess: vitePreprocess({ style: false }),
};
