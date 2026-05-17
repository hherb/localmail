<script lang="ts">
  import { onMount } from "svelte";
  import { auth } from "../lib/stores/auth.svelte";
  import ConnectScreen from "../screens/ConnectScreen.svelte";
  import LoginScreen from "../screens/LoginScreen.svelte";
  import AuthenticatedShell from "../screens/AuthenticatedShell.svelte";

  onMount(async () => {
    await auth.refreshState();
  });
</script>

{#if auth.snapshot.phase === "connecting" || auth.snapshot.phase === "needs_trust"}
  <ConnectScreen />
{:else if auth.snapshot.phase === "logged_out"}
  <LoginScreen />
{:else if auth.snapshot.phase === "logged_in"}
  <AuthenticatedShell />
{/if}
