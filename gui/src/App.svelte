<script lang="ts">
  let greeting: string = $state("");
  let name: string = $state("world");

  function setGreeting(): void {
    greeting = `Hello, ${name}!`;
  }
</script>

<main class="container">
  <h1>localmail</h1>
  <p class="tagline">read-only archive browser — scaffolding only</p>

  <form
    onsubmit={(e: Event) => {
      e.preventDefault();
      setGreeting();
    }}
  >
    <input id="greet-input" bind:value={name} placeholder="who?" />
    <button type="submit">Greet</button>
  </form>

  {#if greeting}
    <p class="greeting">{greeting}</p>
  {/if}
</main>
