<script lang="ts">
  import { onMount } from "svelte";
  import { auth } from "../lib/stores/auth.svelte";
  import ConnectScreen from "../screens/ConnectScreen.svelte";

  // LoginScreen and AuthenticatedShell are loaded lazily (Tasks 9 & 10).
  // Dynamic imports use @vite-ignore so svelte-check does not error on
  // modules that don't exist yet; types are `any` for the same reason.
  let LoginScreen: any = $state(null);
  let AuthenticatedShell: any = $state(null);

  onMount(async () => {
    // @ts-ignore — module created in Task 9
    LoginScreen = (await import(/* @vite-ignore */ "../screens/LoginScreen.svelte")).default;
    // @ts-ignore — module created in Task 10
    AuthenticatedShell = (await import(/* @vite-ignore */ "../screens/AuthenticatedShell.svelte")).default;
    await auth.refreshState();
  });
</script>

{#if auth.snapshot.phase === "connecting" || auth.snapshot.phase === "needs_trust"}
  <ConnectScreen />
{:else if auth.snapshot.phase === "logged_out"}
  {#if LoginScreen}
    {@const LC = LoginScreen}
    <LC />
  {:else}
    <p style="text-align:center; margin-top:64px;">Loading login…</p>
  {/if}
{:else if auth.snapshot.phase === "logged_in"}
  {#if AuthenticatedShell}
    {@const AS = AuthenticatedShell}
    <AS />
  {:else}
    <p style="text-align:center; margin-top:64px;">Loading…</p>
  {/if}
{/if}
