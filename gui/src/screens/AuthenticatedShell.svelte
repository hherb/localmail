<script lang="ts">
  import { auth } from "../lib/stores/auth.svelte";

  let pending: boolean = $state(false);

  async function onLogout(): Promise<void> {
    pending = true;
    try {
      await auth.logout();
    } finally {
      pending = false;
    }
  }

  async function onRefresh(): Promise<void> {
    pending = true;
    try {
      await auth.refreshToken();
    } finally {
      pending = false;
    }
  }
</script>

{#if auth.snapshot.phase === "logged_in"}
  {@const snap = auth.snapshot}
  <main class="container">
    <header>
      <h1>localmail</h1>
      <div class="user">
        <span class="username">{snap.username}</span>
        <button onclick={onRefresh} disabled={pending} class="secondary">Refresh token</button>
        <button onclick={onLogout} disabled={pending} class="secondary">Log out</button>
      </div>
    </header>

    <section>
      <h2>Server capabilities</h2>
      <ul>
        <li><span class="cap" class:on={snap.capabilities.search}>search</span></li>
        <li><span class="cap" class:on={snap.capabilities.attachments}>attachments</span></li>
        <li><span class="cap" class:on={snap.capabilities.attachment_text}>attachment_text</span></li>
        <li><span class="cap" class:on={snap.capabilities.threading}>threading</span></li>
        <li><span class="cap" class:on={snap.capabilities.send}>send</span></li>
      </ul>
    </section>

    <section class="placeholder">
      <p>Sub-plan 2 acceptance shell — the real 3-pane main view lands in Sub-plan 3.</p>
    </section>
  </main>
{/if}

<style>
  .container {
    max-width: 720px;
    margin: 48px auto;
    padding: 24px;
    background: #fff;
    border-radius: 8px;
    box-shadow: 0 1px 3px rgba(0, 0, 0, 0.08);
  }
  header {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    border-bottom: 1px solid #eee;
    padding-bottom: 12px;
    margin-bottom: 24px;
  }
  .user {
    display: flex;
    align-items: center;
    gap: 8px;
  }
  .username {
    font-weight: 600;
    color: #1a4fc7;
  }
  .secondary {
    padding: 4px 10px;
    background: #f4f4f4;
    color: #555;
    border-color: #ccc;
    font-size: 12px;
  }
  ul {
    list-style: none;
    padding: 0;
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
  }
  .cap {
    display: inline-block;
    padding: 4px 10px;
    border-radius: 12px;
    background: #f4f4f4;
    color: #888;
    font-size: 12px;
    font-family: ui-monospace, SFMono-Regular, monospace;
    text-decoration: line-through;
  }
  .cap.on {
    background: #eef9e5;
    color: #2d6a1a;
    text-decoration: none;
  }
  .placeholder {
    margin-top: 32px;
    padding: 16px;
    background: #fafafa;
    border: 1px dashed #ccc;
    border-radius: 4px;
    text-align: center;
    color: #888;
    font-size: 12px;
  }
</style>
