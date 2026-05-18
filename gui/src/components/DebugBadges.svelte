<script lang="ts">
  interface Props {
    score: number;
    matchedArms: readonly string[];
  }
  let { score, matchedArms }: Props = $props();
</script>

<span class="debug">
  <span class="score" title="Fused score">{score.toFixed(3)}</span>
  {#each matchedArms as arm}
    <span class="arm">{arm}</span>
  {/each}
</span>

<style>
  .debug {
    font-family: ui-monospace, monospace;
    font-size: 11px;
    opacity: 0.75;
  }
  .score {
    background: #eef;
    padding: 0 4px;
    border-radius: 3px;
    margin-right: 4px;
  }
  .arm {
    background: #efe;
    padding: 0 4px;
    border-radius: 3px;
    margin-right: 2px;
  }
</style>
