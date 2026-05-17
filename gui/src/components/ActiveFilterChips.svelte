<script lang="ts">
  import { search } from "../lib/stores/search.svelte";

  interface Chip { key: string; label: string; clear: () => void; }

  function chips(): Chip[] {
    const f = search.snapshot.filters;
    const out: Chip[] = [];
    if (f.from) out.push({ key: "from", label: `From: ${f.from}`, clear() {
      search.setFilters({ ...f, from: "" });
    }});
    if (f.to) out.push({ key: "to", label: `To: ${f.to}`, clear() {
      search.setFilters({ ...f, to: "" });
    }});
    if (f.subject) out.push({ key: "subject", label: `Subject: ${f.subject}`, clear() {
      search.setFilters({ ...f, subject: "" });
    }});
    if (f.after) out.push({ key: "after", label: `After: ${f.after}`, clear() {
      search.setFilters({ ...f, after: "" });
    }});
    if (f.before) out.push({ key: "before", label: `Before: ${f.before}`, clear() {
      search.setFilters({ ...f, before: "" });
    }});
    if (f.hasAttachment === true) out.push({ key: "has", label: "Has attachment", clear() {
      search.setFilters({ ...f, hasAttachment: null });
    }});
    return out;
  }

  async function remove(c: Chip) {
    c.clear();
    await search.submit();
  }
</script>

{#if chips().length > 0}
  <ul class="chips">
    {#each chips() as c (c.key)}
      <li class="chip">
        <span>{c.label}</span>
        <button
          type="button"
          aria-label="Remove {c.key}"
          onclick={() => remove(c)}
        >×</button>
      </li>
    {/each}
  </ul>
{/if}

<style>
  .chips { list-style: none; padding: 4px 12px; margin: 0; display: flex; gap: 6px; flex-wrap: wrap; }
  .chip { display: inline-flex; align-items: center; gap: 4px;
          background: #eef3fb; border: 1px solid #c8d6ec; padding: 2px 6px;
          border-radius: 12px; font-size: 12px; }
  button { background: transparent; border: none; cursor: pointer; font-size: 14px; line-height: 1; padding: 0 2px; color: #555; }
</style>
