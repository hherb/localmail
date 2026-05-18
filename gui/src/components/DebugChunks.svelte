<script lang="ts">
  interface MatchedChunk {
    kind: string;
    text: string;
    score?: number;
  }
  interface Props {
    matchedChunks?: ReadonlyArray<MatchedChunk>;
  }
  let { matchedChunks }: Props = $props();
</script>

{#if !matchedChunks || matchedChunks.length === 0}
  <p class="debug-chunks-empty">no matched chunks</p>
{:else}
  <details class="debug-chunks" open>
    <summary>{matchedChunks.length} matched chunk{matchedChunks.length === 1 ? "" : "s"}</summary>
    <ol>
      {#each matchedChunks as c}
        <li>
          <span class="kind">{c.kind}</span>
          {#if c.score !== undefined}
            <span class="score">{c.score.toFixed(3)}</span>
          {/if}
          <pre>{c.text}</pre>
        </li>
      {/each}
    </ol>
  </details>
{/if}

<style>
  .debug-chunks {
    font-family: ui-monospace, monospace;
    font-size: 12px;
    margin-top: 0.5rem;
  }
  .debug-chunks-empty {
    font-family: ui-monospace, monospace;
    font-size: 12px;
    color: #777;
    margin: 0.5rem 0;
  }
  summary {
    cursor: pointer;
    color: #555;
  }
  ol {
    margin: 0.25rem 0 0 1rem;
    padding: 0;
  }
  li {
    margin-bottom: 0.5rem;
  }
  .kind {
    background: #efe;
    padding: 0 4px;
    border-radius: 3px;
    margin-right: 4px;
  }
  .score {
    background: #eef;
    padding: 0 4px;
    border-radius: 3px;
    color: #555;
  }
  pre {
    white-space: pre-wrap;
    font-size: 12px;
    background: #fafafa;
    padding: 0.25rem;
    margin: 0.25rem 0 0 0;
    border-radius: 3px;
  }
</style>
