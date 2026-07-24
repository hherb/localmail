# Admin mode GUI — Phase 2 (frontend shell) + Phase 3 (Accounts panel) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reveal an Admin mode in the `gui/` Tauri desktop app for `is_admin` users and ship the first working panel — full account CRUD, sync enable/disable, password storage, and IMAP test-connection — driven over the bearer-authed `/v1/admin/accounts` JSON API that PR #203 unlocked.

**Architecture:** Three layers, bottom-up. (1) Rust `commands/admin/accounts.rs` proxies the seven `/v1/admin/accounts*` endpoints through the existing pinned-TLS client + keyring bearer token, reusing `AuthError`/`HttpError` so status codes survive to JS. (2) A thin typed TS wrapper `lib/api/admin_accounts.ts` plus a pure `lib/admin_error.ts` for status-code branching. (3) Svelte 5 components under `components/admin/` hosted by a new `screens/AdminView.svelte` overlay, opened from a MainView header button that only renders when `whoami` reported `is_admin`.

**Tech Stack:** Rust (tauri 2, reqwest, serde, thiserror, mockito, tokio-test), TypeScript, Svelte 5 runes, vitest + @testing-library/svelte, jsdom.

## Global Constraints

- **Backend is already done.** PR #203 (`0b1a98b`) shipped `is_admin` on `GET /v1/auth/whoami` and swapped `require_admin()` (bearer OR cookie) into `/v1/admin/accounts`. **This plan changes no Python.** If a step seems to need a Python edit, stop and report it.
- **Every entity ID is a string on the wire**, both directions (CLAUDE.md "ID typing (#33)"). Rust structs use `String` for `id`, never `i64`.
- **PATCH bodies MUST omit unset fields.** `_AccountPatch` in the router uses `model_dump(exclude_unset=True)`, and `api.admin.accounts.update_account` writes *every key present in `fields`*. A serialized `"imap_host": null` therefore **blanks the column**. Every `Option` field on the patch struct carries `#[serde(skip_serializing_if = "Option::is_none")]`.
- **`is_admin` must deserialize from an older server.** Use `#[serde(default)]` so a `whoami` response predating #203 yields `false` rather than a decode error.
- **Svelte 5 runes only** — `$props()`, `$state()`, `$derived()`, `onclick={...}`. No Svelte 4 stores, no `on:click`.
- **No magic numbers.** Named consts for anything numeric that isn't self-evident.
- **Keep files under 500 lines.** Split a component rather than let it grow past that.
- **No comments unless the WHY is non-obvious** (CLAUDE.md). Module-level `//!` / `/** */` doc headers are expected and follow the neighbouring files' style.
- **gui/ files carry no SPDX headers** (unlike `src/localmail/`). Do not add them.
- Verification commands (run from `/Users/hherb/src/localmail`):
  - `cd gui && npm run check && npm test` — svelte-check + vitest
  - `cd gui/src-tauri && cargo test` — Rust unit tests
  - `cd gui/src-tauri && cargo clippy --all-targets -- -D warnings`

## File Structure

**Rust (`gui/src-tauri/src/`)**
| File | Responsibility |
|---|---|
| `commands/auth.rs` (modify) | `WhoamiResponse` gains `is_admin`; add mockito-testable `fetch_whoami` split helper |
| `http/client.rs` (modify) | add `http_patch_json` + `http_delete` verb helpers |
| `commands/admin/mod.rs` (create) | declares the `accounts` submodule |
| `commands/admin/accounts.rs` (create) | the seven `/v1/admin/accounts*` proxies + their types |
| `commands/mod.rs` (modify) | `pub mod admin;` |
| `lib.rs` (modify) | register the seven new `#[tauri::command]`s |

**TypeScript / Svelte (`gui/src/`)**
| File | Responsibility |
|---|---|
| `lib/tauri.ts` (modify) | `WhoamiResponse.is_admin`; re-export admin account types |
| `lib/stores/auth.svelte.ts` (modify) | `logged_in` state carries `isAdmin` |
| `lib/admin_error.ts` (create) | **pure** — dig an HTTP status code out of a nested Tauri error |
| `lib/api/admin_accounts.ts` (create) | typed `invoke()` wrappers + the admin account types |
| `screens/AdminView.svelte` (create) | tabbed overlay shell hosting the panels |
| `screens/MainView.svelte` (modify) | conditional "Admin" header button + overlay mount |
| `components/admin/AccountsPanel.svelte` (create) | list, refresh, sync toggle, delete (with 409 → force) |
| `components/admin/AccountForm.svelte` (create) | create/edit form with inline per-field errors |
| `components/admin/AccountSecrets.svelte` (create) | store-password + test-connection for one account |

---

### Task 1: `is_admin` end-to-end (Rust → store → header button)

**Files:**
- Modify: `gui/src-tauri/src/commands/auth.rs:51-55` (`WhoamiResponse`), `:111-121` (`whoami`)
- Modify: `gui/src/lib/tauri.ts:63-66`
- Modify: `gui/src/lib/stores/auth.svelte.ts:40-45`, `:74-87`
- Modify: `gui/src/screens/MainView.svelte:38`, `:144-155`
- Test: `gui/src-tauri/src/commands/auth.rs` (inline `mod tests`), `gui/src/lib/stores/auth.test.ts`, `gui/src/screens/MainView.test.ts`

**Interfaces:**
- Consumes: nothing (first task).
- Produces:
  - Rust `WhoamiResponse { username: String, user_id: String, is_admin: bool }`
  - Rust `async fn fetch_whoami(client: &Client, base_url: &str, token: &str) -> Result<WhoamiResponse, AuthError>`
  - TS `interface WhoamiResponse { username: string; user_id: string; is_admin: boolean }`
  - TS `AuthState` variant `{ phase: "logged_in"; username: string; isAdmin: boolean; capabilities: Capabilities; expiresAt?: string }`
  - Svelte: MainView renders `[data-testid="open-admin"]` iff `isAdmin`

- [ ] **Step 1: Write the failing Rust tests**

Add to the existing `mod tests` block at the bottom of `gui/src-tauri/src/commands/auth.rs`:

```rust
    #[tokio::test]
    async fn fetch_whoami_parses_is_admin_true() {
        let mut server = mockito::Server::new_async().await;
        let m = server
            .mock("GET", "/v1/auth/whoami")
            .match_header("authorization", "Bearer tok-admin")
            .with_status(200)
            .with_header("content-type", "application/json")
            .with_body(r#"{"username":"root","user_id":"1","is_admin":true}"#)
            .create_async()
            .await;

        let client = reqwest::Client::new();
        let base = format!("{}/", server.url());
        let me = fetch_whoami(&client, &base, "tok-admin").await.unwrap();
        assert_eq!(me.username, "root");
        assert!(me.is_admin);
        m.assert_async().await;
    }

    #[tokio::test]
    async fn fetch_whoami_defaults_is_admin_false_on_older_server() {
        // A serve predating PR #203 omits is_admin entirely. Decoding must
        // succeed and fall back to false, not error the whole login.
        let mut server = mockito::Server::new_async().await;
        let _m = server
            .mock("GET", "/v1/auth/whoami")
            .with_status(200)
            .with_header("content-type", "application/json")
            .with_body(r#"{"username":"viewer","user_id":"7"}"#)
            .create_async()
            .await;

        let client = reqwest::Client::new();
        let base = format!("{}/", server.url());
        let me = fetch_whoami(&client, &base, "tok").await.unwrap();
        assert!(!me.is_admin);
    }
```

- [ ] **Step 2: Run the Rust tests to verify they fail**

```bash
cd gui/src-tauri && cargo test fetch_whoami
```

Expected: FAIL — `cannot find function 'fetch_whoami' in this scope`.

- [ ] **Step 3: Add `is_admin` + the split helper**

In `gui/src-tauri/src/commands/auth.rs`, replace the `WhoamiResponse` struct:

```rust
#[derive(Debug, Deserialize, Serialize)]
pub struct WhoamiResponse {
    pub username: String,
    pub user_id: String,
    // Absent on a serve older than the bearer-admin release; a viewer-only
    // client must still log in rather than fail to decode.
    #[serde(default)]
    pub is_admin: bool,
}
```

Add `use reqwest::Client;` to the imports, then split the HTTP call out of `whoami` (mirrors `auth_change_password::post_change_password`):

```rust
async fn fetch_whoami(
    client: &Client,
    base_url: &str,
    token: &str,
) -> Result<WhoamiResponse, AuthError> {
    let endpoint = format!("{base_url}v1/auth/whoami");
    Ok(http_get_json(client, &endpoint, Some(token)).await?)
}

pub async fn whoami(store: &KeyringStore) -> Result<WhoamiResponse, AuthError> {
    let (url, pin) = read_endpoint(store)?;
    let token = store
        .get(Slot::BearerToken)
        .map_err(|e| AuthError::Keyring(e.to_string()))?
        .ok_or(AuthError::NotLoggedIn)?;
    let client = build_pinned_client(&pin)?;
    fetch_whoami(&client, &url, &token).await
}
```

- [ ] **Step 4: Run the Rust tests to verify they pass**

```bash
cd gui/src-tauri && cargo test
```

Expected: PASS, all tests green.

- [ ] **Step 5: Write the failing TS store test**

Append to `gui/src/lib/stores/auth.test.ts` (inside the existing top-level `describe`; reuse that file's existing `whoami`/`getCapabilities` mocks):

```ts
  it("carries is_admin from whoami into the logged_in snapshot", async () => {
    whoamiMock.mockResolvedValueOnce({
      username: "root",
      user_id: "1",
      is_admin: true,
    });
    getCapabilitiesMock.mockResolvedValueOnce({
      search: true,
      attachments: true,
      attachment_text: true,
      threading: false,
      send: false,
    });
    await auth.refreshState();
    const snap = auth.snapshot;
    expect(snap.phase).toBe("logged_in");
    expect(snap.phase === "logged_in" && snap.isAdmin).toBe(true);
  });

  it("defaults isAdmin to false when whoami omits it", async () => {
    whoamiMock.mockResolvedValueOnce({ username: "viewer", user_id: "7" });
    getCapabilitiesMock.mockResolvedValueOnce({
      search: true,
      attachments: true,
      attachment_text: true,
      threading: false,
      send: false,
    });
    await auth.refreshState();
    const snap = auth.snapshot;
    expect(snap.phase === "logged_in" && snap.isAdmin).toBe(false);
  });
```

If `auth.test.ts` names its mocks differently, use that file's existing names — do not rename them.

- [ ] **Step 6: Run the store test to verify it fails**

```bash
cd gui && npx vitest run src/lib/stores/auth.test.ts
```

Expected: FAIL — `snap.isAdmin` is `undefined`, not `true`.

- [ ] **Step 7: Thread `isAdmin` through the TS types and store**

In `gui/src/lib/tauri.ts`:

```ts
export interface WhoamiResponse {
  username: string;
  user_id: string;
  is_admin: boolean;
}
```

In `gui/src/lib/stores/auth.svelte.ts`, extend the `logged_in` variant of `AuthState`:

```ts
  | {
      phase: "logged_in";
      username: string;
      isAdmin: boolean;
      capabilities: Capabilities;
      expiresAt?: string;
    };
```

and populate it in `refreshState`:

```ts
      const me = await whoami();
      const caps = await getCapabilities();
      this.#state = {
        phase: "logged_in",
        username: me.username,
        isAdmin: me.is_admin === true,
        capabilities: caps,
      };
```

`=== true` is deliberate: it normalises an `undefined` from an older server to `false` on the JS side too.

- [ ] **Step 8: Run the store test to verify it passes**

```bash
cd gui && npx vitest run src/lib/stores/auth.test.ts
```

Expected: PASS.

- [ ] **Step 9: Write the failing MainView test**

In `gui/src/screens/MainView.test.ts`, add `isAdmin: false` to the existing `forceLoggedIn()` helper's `Object.assign` payload, add an admin variant beside it, and add two cases inside `describe("MainView", ...)`:

```ts
function forceLoggedInAdmin(): void {
  forceLoggedIn();
  Object.assign(auth.snapshot, { isAdmin: true });
}
```

```ts
  it("hides the Admin button for a non-admin user", () => {
    forceLoggedIn();
    const { container } = render(MainView);
    expect(container.querySelector('[data-testid="open-admin"]')).toBeFalsy();
  });

  it("shows the Admin button for an admin user", () => {
    forceLoggedInAdmin();
    const { container } = render(MainView);
    expect(container.querySelector('[data-testid="open-admin"]')).toBeTruthy();
  });
```

- [ ] **Step 10: Run the MainView test to verify it fails**

```bash
cd gui && npx vitest run src/screens/MainView.test.ts
```

Expected: FAIL on "shows the Admin button" — the element does not exist.

- [ ] **Step 11: Render the conditional Admin button**

In `gui/src/screens/MainView.svelte`, add the button to the `.right` block, immediately before the existing Settings `⚙` button:

```svelte
        {#if snap.isAdmin}
          <button
            aria-label="Admin"
            title="Admin"
            data-testid="open-admin"
            onclick={() => (adminOpen = true)}
            disabled={pending}
          >Admin</button>
        {/if}
```

and declare the state beside `settingsOpen` (line ~38):

```svelte
  let adminOpen: boolean = $state(false);
```

`adminOpen` is unused until Task 2 mounts `AdminView`; that is expected and `svelte-check` does not flag an assigned-but-unread `$state` local.

- [ ] **Step 12: Run the full frontend + Rust checks**

```bash
cd gui && npm run check && npm test
cd src-tauri && cargo test
```

Expected: all PASS.

- [ ] **Step 13: Commit**

```bash
git add gui/src-tauri/src/commands/auth.rs gui/src/lib/tauri.ts \
        gui/src/lib/stores/auth.svelte.ts gui/src/lib/stores/auth.test.ts \
        gui/src/screens/MainView.svelte gui/src/screens/MainView.test.ts
git commit -m "feat(gui): surface is_admin from whoami and gate an Admin header button"
```

---

### Task 2: `AdminView` tabbed shell

**Files:**
- Create: `gui/src/screens/AdminView.svelte`
- Create: `gui/src/screens/AdminView.test.ts`
- Modify: `gui/src/screens/MainView.svelte` (mount the overlay)

**Interfaces:**
- Consumes: `adminOpen` state from Task 1.
- Produces: `AdminView` component with props `{ open: boolean; onClose: () => void }`; tab test ids `admin-tab-accounts` / `admin-tab-daemon` / `admin-tab-users` / `admin-tab-imports`; panel slot test id `admin-panel-body`.

- [ ] **Step 1: Write the failing test**

Create `gui/src/screens/AdminView.test.ts`:

```ts
import { fireEvent, render } from "@testing-library/svelte";
import { describe, expect, it, vi } from "vitest";

import AdminView from "./AdminView.svelte";

describe("AdminView", () => {
  it("renders nothing when closed", () => {
    const { container } = render(AdminView, {
      props: { open: false, onClose: vi.fn() },
    });
    expect(container.querySelector('[role="dialog"]')).toBeFalsy();
  });

  it("renders four tabs when open, accounts selected first", () => {
    const { container } = render(AdminView, {
      props: { open: true, onClose: vi.fn() },
    });
    expect(container.querySelectorAll('[role="tab"]').length).toBe(4);
    const accounts = container.querySelector('[data-testid="admin-tab-accounts"]');
    expect(accounts?.getAttribute("aria-selected")).toBe("true");
  });

  it("switches the active tab on click", async () => {
    const { container } = render(AdminView, {
      props: { open: true, onClose: vi.fn() },
    });
    const daemon = container.querySelector(
      '[data-testid="admin-tab-daemon"]',
    ) as HTMLButtonElement;
    await fireEvent.click(daemon);
    expect(daemon.getAttribute("aria-selected")).toBe("true");
    expect(
      container
        .querySelector('[data-testid="admin-tab-accounts"]')
        ?.getAttribute("aria-selected"),
    ).toBe("false");
  });

  it("calls onClose when the close button is clicked", async () => {
    const onClose = vi.fn();
    const { container } = render(AdminView, { props: { open: true, onClose } });
    const close = container.querySelector(".close") as HTMLButtonElement;
    await fireEvent.click(close);
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
cd gui && npx vitest run src/screens/AdminView.test.ts
```

Expected: FAIL — cannot resolve `./AdminView.svelte`.

- [ ] **Step 3: Create the shell**

Create `gui/src/screens/AdminView.svelte`:

```svelte
<script lang="ts">
  /**
   * Full-pane admin overlay, mounted over MainView and revealed only for
   * is_admin users. Mirrors SettingsScreen's modal + tab structure so the
   * two overlays behave identically. Each tab body renders only when
   * active, keeping the DOM small and tab assertions trivial.
   *
   * Only the Accounts panel is implemented; the remaining three are
   * placeholders until their own phases land.
   */
  type Tab = "accounts" | "daemon" | "users" | "imports";

  interface Props {
    open: boolean;
    onClose: () => void;
  }
  let { open, onClose }: Props = $props();

  let tab: Tab = $state("accounts");
</script>

{#if open}
  <div class="overlay" role="dialog" aria-modal="true" aria-labelledby="admin-title">
    <div class="modal">
      <header>
        <h2 id="admin-title">Admin</h2>
        <button class="close" onclick={onClose} aria-label="Close">×</button>
      </header>
      <div class="tabs" role="tablist">
        <button
          role="tab"
          aria-selected={tab === "accounts"}
          class:active={tab === "accounts"}
          data-testid="admin-tab-accounts"
          onclick={() => (tab = "accounts")}
        >Accounts</button>
        <button
          role="tab"
          aria-selected={tab === "daemon"}
          class:active={tab === "daemon"}
          data-testid="admin-tab-daemon"
          onclick={() => (tab = "daemon")}
        >Daemon</button>
        <button
          role="tab"
          aria-selected={tab === "users"}
          class:active={tab === "users"}
          data-testid="admin-tab-users"
          onclick={() => (tab = "users")}
        >Users</button>
        <button
          role="tab"
          aria-selected={tab === "imports"}
          class:active={tab === "imports"}
          data-testid="admin-tab-imports"
          onclick={() => (tab = "imports")}
        >Imports</button>
      </div>
      <section class="body" role="tabpanel" data-testid="admin-panel-body">
        {#if tab === "accounts"}
          <p class="placeholder">Accounts panel</p>
        {/if}
        {#if tab === "daemon"}
          <p class="placeholder">Daemon control is not available in this build yet.</p>
        {/if}
        {#if tab === "users"}
          <p class="placeholder">User management is not available in this build yet.</p>
        {/if}
        {#if tab === "imports"}
          <p class="placeholder">Archive imports are not available in this build yet.</p>
        {/if}
      </section>
    </div>
  </div>
{/if}

<style>
  .overlay {
    position: fixed;
    inset: 0;
    background: rgba(0, 0, 0, 0.5);
    display: grid;
    place-items: center;
    z-index: 200;
  }
  .modal {
    background: white;
    width: min(960px, 94vw);
    height: min(680px, 92vh);
    display: flex;
    flex-direction: column;
    border-radius: 6px;
    overflow: hidden;
  }
  header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 0.75rem 1rem;
    border-bottom: 1px solid #ddd;
  }
  header h2 {
    margin: 0;
    font-size: 1.05rem;
  }
  .close {
    font-size: 1.25rem;
    background: none;
    border: none;
    cursor: pointer;
    line-height: 1;
    padding: 0 0.5rem;
  }
  .tabs {
    display: flex;
    gap: 0.25rem;
    padding: 0 1rem;
    border-bottom: 1px solid #ddd;
  }
  .tabs button {
    padding: 0.5rem 0.75rem;
    background: none;
    border: none;
    cursor: pointer;
    font-size: 0.9rem;
    border-bottom: 2px solid transparent;
  }
  .tabs button.active {
    font-weight: 600;
    border-bottom-color: #1a73e8;
    color: #1a73e8;
  }
  .body {
    flex: 1;
    padding: 1rem;
    overflow: auto;
  }
  .placeholder {
    color: #666;
    font-size: 0.9rem;
  }
</style>
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
cd gui && npx vitest run src/screens/AdminView.test.ts
```

Expected: PASS, 4 tests.

- [ ] **Step 5: Mount the overlay in MainView**

In `gui/src/screens/MainView.svelte`, add the import beside `SettingsScreen`:

```svelte
  import AdminView from "./AdminView.svelte";
```

and mount it immediately after the existing `<SettingsScreen ... />` line:

```svelte
    <AdminView open={adminOpen} onClose={() => (adminOpen = false)} />
```

- [ ] **Step 6: Add the MainView open-overlay test**

Append inside `describe("MainView", ...)` in `gui/src/screens/MainView.test.ts`:

```ts
  it("opens the admin overlay when the Admin button is clicked", async () => {
    forceLoggedInAdmin();
    const { container } = render(MainView);
    const btn = container.querySelector(
      '[data-testid="open-admin"]',
    ) as HTMLButtonElement;
    await fireEvent.click(btn);
    expect(container.querySelector('[data-testid="admin-panel-body"]')).toBeTruthy();
  });
```

- [ ] **Step 7: Run the frontend checks**

```bash
cd gui && npm run check && npm test
```

Expected: all PASS.

- [ ] **Step 8: Commit**

```bash
git add gui/src/screens/AdminView.svelte gui/src/screens/AdminView.test.ts \
        gui/src/screens/MainView.svelte gui/src/screens/MainView.test.ts
git commit -m "feat(gui): add tabbed AdminView overlay shell behind the Admin button"
```

---

### Task 3: PATCH + DELETE HTTP verb helpers

**Files:**
- Modify: `gui/src-tauri/src/http/client.rs` (add two helpers + two tests)

**Interfaces:**
- Consumes: existing `HttpError`, `REQUEST_TIMEOUT_SECS`.
- Produces:
  - `pub async fn http_patch_json<B: Serialize, T: DeserializeOwned>(client: &Client, url: &str, body: &B, bearer: Option<&str>) -> Result<T, HttpError>`
  - `pub async fn http_delete(client: &Client, url: &str, bearer: Option<&str>) -> Result<(), HttpError>`

- [ ] **Step 1: Write the failing tests**

Append inside the existing `mod tests` block of `gui/src-tauri/src/http/client.rs`:

```rust
    #[tokio::test]
    async fn http_patch_json_sends_body_and_decodes_response() {
        let mut server = mockito::Server::new_async().await;
        let m = server
            .mock("PATCH", "/thing")
            .match_header("authorization", "Bearer tok")
            .match_body(mockito::Matcher::JsonString(r#"{"message":"set"}"#.to_string()))
            .with_status(200)
            .with_header("content-type", "application/json")
            .with_body(r#"{"message":"set"}"#)
            .create_async()
            .await;

        let client = Client::new();
        let url = format!("{}/thing", server.url());
        let body = serde_json::json!({ "message": "set" });
        let got: Echo = http_patch_json(&client, &url, &body, Some("tok")).await.unwrap();
        assert_eq!(got.message, "set");
        m.assert_async().await;
    }

    #[tokio::test]
    async fn http_delete_accepts_204_and_maps_409() {
        let mut server = mockito::Server::new_async().await;
        let _ok = server
            .mock("DELETE", "/gone")
            .with_status(204)
            .create_async()
            .await;
        let client = Client::new();
        http_delete(&client, &format!("{}/gone", server.url()), Some("tok"))
            .await
            .expect("204 should succeed");

        let _conflict = server
            .mock("DELETE", "/busy")
            .with_status(409)
            .with_body("account has messages")
            .create_async()
            .await;
        let err = http_delete(&client, &format!("{}/busy", server.url()), Some("tok"))
            .await
            .unwrap_err();
        match err {
            HttpError::HttpStatus { status, body } => {
                assert_eq!(status, 409);
                assert_eq!(body, "account has messages");
            }
            other => panic!("expected HttpStatus 409, got {other:?}"),
        }
    }
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
cd gui/src-tauri && cargo test http_patch_json http_delete
```

Expected: FAIL — `cannot find function 'http_patch_json'` / `'http_delete'`.

- [ ] **Step 3: Add the helpers**

Append to `gui/src-tauri/src/http/client.rs`, after `http_post_json_no_resp`:

```rust
pub async fn http_patch_json<B: Serialize, T: DeserializeOwned>(
    client: &Client,
    url: &str,
    body: &B,
    bearer: Option<&str>,
) -> Result<T, HttpError> {
    let mut req = client.patch(url).json(body);
    if let Some(tok) = bearer {
        req = req.bearer_auth(tok);
    }
    let resp = req
        .send()
        .await
        .map_err(|e| HttpError::from_reqwest(e, REQUEST_TIMEOUT_SECS))?;
    let status = resp.status();
    if !status.is_success() {
        let body = resp.text().await.unwrap_or_default();
        return Err(HttpError::HttpStatus {
            status: status.as_u16(),
            body,
        });
    }
    resp.json::<T>()
        .await
        .map_err(|e| HttpError::from_reqwest(e, REQUEST_TIMEOUT_SECS))
}

pub async fn http_delete(
    client: &Client,
    url: &str,
    bearer: Option<&str>,
) -> Result<(), HttpError> {
    let mut req = client.delete(url);
    if let Some(tok) = bearer {
        req = req.bearer_auth(tok);
    }
    let resp = req
        .send()
        .await
        .map_err(|e| HttpError::from_reqwest(e, REQUEST_TIMEOUT_SECS))?;
    let status = resp.status();
    if !status.is_success() {
        let body = resp.text().await.unwrap_or_default();
        return Err(HttpError::HttpStatus {
            status: status.as_u16(),
            body,
        });
    }
    Ok(())
}
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
cd gui/src-tauri && cargo test
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add gui/src-tauri/src/http/client.rs
git commit -m "feat(gui): add http_patch_json and http_delete verb helpers"
```

---

### Task 4: Rust admin accounts — read side (list + get)

**Files:**
- Create: `gui/src-tauri/src/commands/admin/mod.rs`
- Create: `gui/src-tauri/src/commands/admin/accounts.rs`
- Modify: `gui/src-tauri/src/commands/mod.rs`
- Modify: `gui/src-tauri/src/lib.rs`

**Interfaces:**
- Consumes: `AuthError`, `read_authenticated`, `build_pinned_client`, `http_get_json`.
- Produces (used by Tasks 5-7):
  - `pub struct AdminAccountSummary { pub id: String, pub name: String, pub email_address: String, pub auth_method: String, pub sync_enabled: bool }`
  - `pub struct AdminAccount { pub id: String, pub name: String, pub email_address: String, pub auth_method: String, pub oauth_provider: Option<String>, pub imap_host: Option<String>, pub imap_port: Option<i64>, pub folder_allow: Option<Vec<String>>, pub folder_deny: Option<Vec<String>>, pub folder_deny_flags: Option<Vec<String>>, pub sync_enabled: bool, pub created_at: String, pub updated_at: String }`
  - `pub async fn list_admin_accounts(store: &KeyringStore) -> Result<Vec<AdminAccountSummary>, AuthError>`
  - `pub async fn get_admin_account(store: &KeyringStore, account_id: &str) -> Result<AdminAccount, AuthError>`
  - Tauri commands `list_admin_accounts_cmd`, `get_admin_account_cmd`

- [ ] **Step 1: Write the failing tests**

Create `gui/src-tauri/src/commands/admin/accounts.rs` containing **only** the test module for now (the implementation lands in Step 3):

```rust
#[cfg(test)]
mod tests {
    use super::*;
    use crate::storage::keyring::{MemKeyring, Slot};

    fn fake_store() -> KeyringStore {
        KeyringStore::with_backend(MemKeyring::new())
    }

    #[tokio::test]
    async fn list_without_token_returns_not_logged_in() {
        let store = fake_store();
        store.put(Slot::ServerUrl, "https://localhost:8443/").unwrap();
        store.put(Slot::CertPin, "deadbeef").unwrap();
        let err = list_admin_accounts(&store).await.unwrap_err();
        assert!(matches!(err, AuthError::NotLoggedIn));
    }

    #[tokio::test]
    async fn fetch_list_unwraps_the_accounts_envelope() {
        let mut server = mockito::Server::new_async().await;
        let m = server
            .mock("GET", "/v1/admin/accounts")
            .match_header("authorization", "Bearer tok")
            .with_status(200)
            .with_header("content-type", "application/json")
            .with_body(
                r#"{"accounts":[{"id":"3","name":"gmail","email_address":"a@b.c","auth_method":"oauth2","sync_enabled":true}]}"#,
            )
            .create_async()
            .await;

        let client = Client::new();
        let base = format!("{}/", server.url());
        let got = fetch_list(&client, &base, "tok").await.unwrap();
        assert_eq!(got.len(), 1);
        assert_eq!(got[0].id, "3");
        assert_eq!(got[0].auth_method, "oauth2");
        assert!(got[0].sync_enabled);
        m.assert_async().await;
    }

    #[tokio::test]
    async fn fetch_one_decodes_the_full_account() {
        let mut server = mockito::Server::new_async().await;
        let _m = server
            .mock("GET", "/v1/admin/accounts/3")
            .with_status(200)
            .with_header("content-type", "application/json")
            .with_body(
                r#"{"id":"3","name":"gmail","email_address":"a@b.c","auth_method":"password",
                     "oauth_provider":null,"imap_host":"imap.example.com","imap_port":993,
                     "folder_allow":null,"folder_deny":["Spam"],"folder_deny_flags":["\\Trash"],
                     "sync_enabled":false,"created_at":"2026-01-01T00:00:00+00:00",
                     "updated_at":"2026-01-02T00:00:00+00:00"}"#,
            )
            .create_async()
            .await;

        let client = Client::new();
        let base = format!("{}/", server.url());
        let got = fetch_one(&client, &base, "tok", "3").await.unwrap();
        assert_eq!(got.imap_port, Some(993));
        assert_eq!(got.folder_deny.as_deref(), Some(&["Spam".to_string()][..]));
        assert_eq!(got.folder_deny_flags.as_deref(), Some(&["\\Trash".to_string()][..]));
        assert!(!got.sync_enabled);
    }

    #[tokio::test]
    async fn fetch_one_maps_403_to_http_status() {
        let mut server = mockito::Server::new_async().await;
        let _m = server
            .mock("GET", "/v1/admin/accounts/3")
            .with_status(403)
            .with_body("admin privileges required")
            .create_async()
            .await;
        let client = Client::new();
        let base = format!("{}/", server.url());
        let err = fetch_one(&client, &base, "tok", "3").await.unwrap_err();
        match err {
            AuthError::Http(crate::http::errors::HttpError::HttpStatus { status, .. }) => {
                assert_eq!(status, 403);
            }
            other => panic!("expected HttpStatus 403, got {other:?}"),
        }
    }
}
```

Create `gui/src-tauri/src/commands/admin/mod.rs`:

```rust
//! Admin-mode command handlers. Every call here drives a `/v1/admin/*`
//! JSON endpoint with the stored bearer token; the server gates them on
//! the token user's `is_admin` flag (403 otherwise).

pub mod accounts;
```

Add to `gui/src-tauri/src/commands/mod.rs`, keeping the list alphabetical (before `attachments`):

```rust
pub mod admin;
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
cd gui/src-tauri && cargo test admin::accounts
```

Expected: FAIL to compile — `cannot find function 'fetch_list'`, `'fetch_one'`, `'list_admin_accounts'`.

- [ ] **Step 3: Write the implementation**

Prepend to `gui/src-tauri/src/commands/admin/accounts.rs`, above the test module:

```rust
//! Proxies for `/v1/admin/accounts*` (account CRUD, secrets, test-connection).
//!
//! Each endpoint gets a `fetch_*` / `post_*` helper taking an explicit
//! `reqwest::Client` + base URL so it is mockito-testable, plus a
//! keyring-reading wrapper and a thin `#[tauri::command]`. Mirrors
//! `commands::auth_change_password`.
//!
//! IDs are strings on the wire in both directions (see CLAUDE.md, "ID
//! typing"); nothing here parses them into integers.

use reqwest::Client;
use serde::{Deserialize, Serialize};

use crate::commands::auth::AuthError;
use crate::commands::session::read_authenticated;
use crate::http::client::{build_pinned_client, http_get_json};
use crate::storage::keyring::KeyringStore;

#[derive(Debug, Deserialize, Serialize)]
pub struct AdminAccountSummary {
    pub id: String,
    pub name: String,
    pub email_address: String,
    pub auth_method: String,
    pub sync_enabled: bool,
}

#[derive(Debug, Deserialize, Serialize)]
pub struct AdminAccount {
    pub id: String,
    pub name: String,
    pub email_address: String,
    pub auth_method: String,
    pub oauth_provider: Option<String>,
    pub imap_host: Option<String>,
    pub imap_port: Option<i64>,
    pub folder_allow: Option<Vec<String>>,
    pub folder_deny: Option<Vec<String>>,
    pub folder_deny_flags: Option<Vec<String>>,
    pub sync_enabled: bool,
    pub created_at: String,
    pub updated_at: String,
}

#[derive(Debug, Deserialize)]
struct AccountListEnvelope {
    accounts: Vec<AdminAccountSummary>,
}

async fn fetch_list(
    client: &Client,
    base_url: &str,
    token: &str,
) -> Result<Vec<AdminAccountSummary>, AuthError> {
    let endpoint = format!("{base_url}v1/admin/accounts");
    let env: AccountListEnvelope = http_get_json(client, &endpoint, Some(token)).await?;
    Ok(env.accounts)
}

async fn fetch_one(
    client: &Client,
    base_url: &str,
    token: &str,
    account_id: &str,
) -> Result<AdminAccount, AuthError> {
    let endpoint = format!("{base_url}v1/admin/accounts/{account_id}");
    Ok(http_get_json(client, &endpoint, Some(token)).await?)
}

pub async fn list_admin_accounts(
    store: &KeyringStore,
) -> Result<Vec<AdminAccountSummary>, AuthError> {
    let (url, pin, token) = read_authenticated(store)?;
    let client = build_pinned_client(&pin)?;
    fetch_list(&client, &url, &token).await
}

pub async fn get_admin_account(
    store: &KeyringStore,
    account_id: &str,
) -> Result<AdminAccount, AuthError> {
    let (url, pin, token) = read_authenticated(store)?;
    let client = build_pinned_client(&pin)?;
    fetch_one(&client, &url, &token, account_id).await
}

#[tauri::command]
pub async fn list_admin_accounts_cmd() -> Result<Vec<AdminAccountSummary>, AuthError> {
    let store = KeyringStore::new();
    list_admin_accounts(&store).await
}

#[tauri::command]
pub async fn get_admin_account_cmd(
    account_id: String,
) -> Result<AdminAccount, AuthError> {
    let store = KeyringStore::new();
    get_admin_account(&store, &account_id).await
}
```

- [ ] **Step 4: Register the commands**

In `gui/src-tauri/src/lib.rs`, add to the `tauri::generate_handler![...]` list, after `crate::commands::accounts::list_folders_cmd,`:

```rust
            crate::commands::admin::accounts::list_admin_accounts_cmd,
            crate::commands::admin::accounts::get_admin_account_cmd,
```

- [ ] **Step 5: Run the tests to verify they pass**

```bash
cd gui/src-tauri && cargo test && cargo clippy --all-targets -- -D warnings
```

Expected: PASS, no clippy warnings.

- [ ] **Step 6: Commit**

```bash
git add gui/src-tauri/src/commands/admin gui/src-tauri/src/commands/mod.rs gui/src-tauri/src/lib.rs
git commit -m "feat(gui): add admin account list/get Tauri commands"
```

---

### Task 5: Rust admin accounts — create + update

**Files:**
- Modify: `gui/src-tauri/src/commands/admin/accounts.rs`
- Modify: `gui/src-tauri/src/lib.rs`

**Interfaces:**
- Consumes: `AdminAccount` from Task 4; `http_patch_json` from Task 3.
- Produces:
  - `pub struct AdminAccountInput { pub name: String, pub email_address: String, pub auth_method: String, pub imap_host: Option<String>, pub imap_port: Option<i64>, pub oauth_provider: Option<String>, pub folder_allow: Option<Vec<String>>, pub folder_deny: Option<Vec<String>>, pub folder_deny_flags: Option<Vec<String>> }`
  - `pub struct AdminAccountPatch { pub email_address: Option<String>, pub auth_method: Option<String>, pub imap_host: Option<String>, pub imap_port: Option<i64>, pub oauth_provider: Option<String>, pub folder_allow: Option<Vec<String>>, pub folder_deny: Option<Vec<String>>, pub folder_deny_flags: Option<Vec<String>>, pub sync_enabled: Option<bool> }`
  - `pub async fn create_admin_account(store, input: AdminAccountInput) -> Result<AdminAccount, AuthError>`
  - `pub async fn update_admin_account(store, account_id: &str, patch: AdminAccountPatch) -> Result<AdminAccount, AuthError>`
  - Tauri commands `create_admin_account_cmd`, `update_admin_account_cmd`

- [ ] **Step 1: Write the failing tests**

Append inside the existing `mod tests` block of `gui/src-tauri/src/commands/admin/accounts.rs`:

```rust
    fn full_account_json() -> &'static str {
        r#"{"id":"9","name":"new","email_address":"n@e.w","auth_method":"password",
            "oauth_provider":null,"imap_host":"h","imap_port":993,
            "folder_allow":null,"folder_deny":null,"folder_deny_flags":null,
            "sync_enabled":true,"created_at":"2026-01-01T00:00:00+00:00",
            "updated_at":"2026-01-01T00:00:00+00:00"}"#
    }

    #[tokio::test]
    async fn post_create_sends_the_input_and_decodes_201() {
        let mut server = mockito::Server::new_async().await;
        let m = server
            .mock("POST", "/v1/admin/accounts")
            .match_header("authorization", "Bearer tok")
            .match_body(mockito::Matcher::JsonString(
                r#"{"name":"new","email_address":"n@e.w","auth_method":"password","imap_host":"h","imap_port":993}"#
                    .to_string(),
            ))
            .with_status(201)
            .with_header("content-type", "application/json")
            .with_body(full_account_json())
            .create_async()
            .await;

        let client = Client::new();
        let base = format!("{}/", server.url());
        let input = AdminAccountInput {
            name: "new".into(),
            email_address: "n@e.w".into(),
            auth_method: "password".into(),
            imap_host: Some("h".into()),
            imap_port: Some(993),
            oauth_provider: None,
            folder_allow: None,
            folder_deny: None,
            folder_deny_flags: None,
        };
        let got = post_create(&client, &base, "tok", &input).await.unwrap();
        assert_eq!(got.id, "9");
        m.assert_async().await;
    }

    #[tokio::test]
    async fn patch_update_omits_unset_fields_entirely() {
        // update_account writes EVERY key present in the body, so a
        // serialized null would blank the column. Only sync_enabled is set
        // here, so the wire body must contain exactly that one key.
        let mut server = mockito::Server::new_async().await;
        let m = server
            .mock("PATCH", "/v1/admin/accounts/9")
            .match_body(mockito::Matcher::JsonString(
                r#"{"sync_enabled":false}"#.to_string(),
            ))
            .with_status(200)
            .with_header("content-type", "application/json")
            .with_body(full_account_json())
            .create_async()
            .await;

        let client = Client::new();
        let base = format!("{}/", server.url());
        let patch = AdminAccountPatch {
            sync_enabled: Some(false),
            ..AdminAccountPatch::default()
        };
        patch_update(&client, &base, "tok", "9", &patch).await.unwrap();
        m.assert_async().await;
    }

    #[tokio::test]
    async fn patch_update_maps_400_validation_to_http_status() {
        let mut server = mockito::Server::new_async().await;
        let _m = server
            .mock("PATCH", "/v1/admin/accounts/9")
            .with_status(400)
            .with_body(r#"{"detail":"imap_host is required for live accounts"}"#)
            .create_async()
            .await;
        let client = Client::new();
        let base = format!("{}/", server.url());
        let patch = AdminAccountPatch {
            imap_host: Some(String::new()),
            ..AdminAccountPatch::default()
        };
        let err = patch_update(&client, &base, "tok", "9", &patch)
            .await
            .unwrap_err();
        match err {
            AuthError::Http(crate::http::errors::HttpError::HttpStatus { status, body }) => {
                assert_eq!(status, 400);
                assert!(body.contains("imap_host is required"));
            }
            other => panic!("expected HttpStatus 400, got {other:?}"),
        }
    }
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
cd gui/src-tauri && cargo test admin::accounts
```

Expected: FAIL to compile — `cannot find type 'AdminAccountInput'` / function `post_create` / `patch_update`.

- [ ] **Step 3: Write the implementation**

Extend the imports at the top of `gui/src-tauri/src/commands/admin/accounts.rs`:

```rust
use crate::http::client::{build_pinned_client, http_get_json, http_patch_json, http_post_json};
```

Add the types and helpers (place them after `AdminAccount`, before `AccountListEnvelope`):

```rust
#[derive(Debug, Deserialize, Serialize)]
pub struct AdminAccountInput {
    pub name: String,
    pub email_address: String,
    pub auth_method: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub imap_host: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub imap_port: Option<i64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub oauth_provider: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub folder_allow: Option<Vec<String>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub folder_deny: Option<Vec<String>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub folder_deny_flags: Option<Vec<String>>,
}

// Every field is skipped when None: the server's update_account writes each
// key present in the body, so an explicit null would blank the column
// rather than leave it alone.
#[derive(Debug, Default, Deserialize, Serialize)]
pub struct AdminAccountPatch {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub email_address: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub auth_method: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub imap_host: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub imap_port: Option<i64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub oauth_provider: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub folder_allow: Option<Vec<String>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub folder_deny: Option<Vec<String>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub folder_deny_flags: Option<Vec<String>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub sync_enabled: Option<bool>,
}
```

and the request helpers + wrappers + commands (after `fetch_one`):

```rust
async fn post_create(
    client: &Client,
    base_url: &str,
    token: &str,
    input: &AdminAccountInput,
) -> Result<AdminAccount, AuthError> {
    let endpoint = format!("{base_url}v1/admin/accounts");
    Ok(http_post_json(client, &endpoint, input, Some(token)).await?)
}

async fn patch_update(
    client: &Client,
    base_url: &str,
    token: &str,
    account_id: &str,
    patch: &AdminAccountPatch,
) -> Result<AdminAccount, AuthError> {
    let endpoint = format!("{base_url}v1/admin/accounts/{account_id}");
    Ok(http_patch_json(client, &endpoint, patch, Some(token)).await?)
}

pub async fn create_admin_account(
    store: &KeyringStore,
    input: AdminAccountInput,
) -> Result<AdminAccount, AuthError> {
    let (url, pin, token) = read_authenticated(store)?;
    let client = build_pinned_client(&pin)?;
    post_create(&client, &url, &token, &input).await
}

pub async fn update_admin_account(
    store: &KeyringStore,
    account_id: &str,
    patch: AdminAccountPatch,
) -> Result<AdminAccount, AuthError> {
    let (url, pin, token) = read_authenticated(store)?;
    let client = build_pinned_client(&pin)?;
    patch_update(&client, &url, &token, account_id, &patch).await
}

#[tauri::command]
pub async fn create_admin_account_cmd(
    input: AdminAccountInput,
) -> Result<AdminAccount, AuthError> {
    let store = KeyringStore::new();
    create_admin_account(&store, input).await
}

#[tauri::command]
pub async fn update_admin_account_cmd(
    account_id: String,
    patch: AdminAccountPatch,
) -> Result<AdminAccount, AuthError> {
    let store = KeyringStore::new();
    update_admin_account(&store, &account_id, patch).await
}
```

- [ ] **Step 4: Register the commands**

In `gui/src-tauri/src/lib.rs`, after `get_admin_account_cmd,`:

```rust
            crate::commands::admin::accounts::create_admin_account_cmd,
            crate::commands::admin::accounts::update_admin_account_cmd,
```

- [ ] **Step 5: Run the tests to verify they pass**

```bash
cd gui/src-tauri && cargo test && cargo clippy --all-targets -- -D warnings
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add gui/src-tauri/src/commands/admin/accounts.rs gui/src-tauri/src/lib.rs
git commit -m "feat(gui): add admin account create/update commands with omit-unset patch bodies"
```

---

### Task 6: Rust admin accounts — delete, store password, test connection

**Files:**
- Modify: `gui/src-tauri/src/commands/admin/accounts.rs`
- Modify: `gui/src-tauri/src/lib.rs`

**Interfaces:**
- Consumes: `http_delete` (Task 3), `http_post_json_no_resp`, `http_post_json`.
- Produces:
  - `pub struct ProbedFolder { pub name: String, pub flags: Vec<String> }`
  - `pub struct TestConnectionResult { pub folders: Vec<ProbedFolder> }`
  - `pub async fn delete_admin_account(store, account_id: &str, force: bool) -> Result<(), AuthError>`
  - `pub async fn store_admin_account_password(store, account_id: &str, password: &str) -> Result<(), AuthError>`
  - `pub async fn test_admin_account_connection(store, account_id: &str) -> Result<TestConnectionResult, AuthError>`
  - Tauri commands `delete_admin_account_cmd`, `store_admin_account_password_cmd`, `test_admin_account_connection_cmd`

- [ ] **Step 1: Write the failing tests**

Append inside the existing `mod tests` block of `gui/src-tauri/src/commands/admin/accounts.rs`:

```rust
    #[tokio::test]
    async fn delete_appends_force_query_only_when_forcing() {
        let mut server = mockito::Server::new_async().await;
        let plain = server
            .mock("DELETE", "/v1/admin/accounts/9")
            .with_status(204)
            .create_async()
            .await;
        let forced = server
            .mock("DELETE", "/v1/admin/accounts/9?force=true")
            .with_status(204)
            .create_async()
            .await;

        let client = Client::new();
        let base = format!("{}/", server.url());
        delete_one(&client, &base, "tok", "9", false).await.unwrap();
        delete_one(&client, &base, "tok", "9", true).await.unwrap();
        plain.assert_async().await;
        forced.assert_async().await;
    }

    #[tokio::test]
    async fn delete_maps_409_cascade_refusal() {
        let mut server = mockito::Server::new_async().await;
        let _m = server
            .mock("DELETE", "/v1/admin/accounts/9")
            .with_status(409)
            .with_body(r#"{"detail":"account 9 has 1200 messages"}"#)
            .create_async()
            .await;
        let client = Client::new();
        let base = format!("{}/", server.url());
        let err = delete_one(&client, &base, "tok", "9", false).await.unwrap_err();
        match err {
            AuthError::Http(crate::http::errors::HttpError::HttpStatus { status, .. }) => {
                assert_eq!(status, 409);
            }
            other => panic!("expected HttpStatus 409, got {other:?}"),
        }
    }

    #[tokio::test]
    async fn post_password_sends_body_and_accepts_204() {
        let mut server = mockito::Server::new_async().await;
        let m = server
            .mock("POST", "/v1/admin/accounts/9/password")
            .match_header("authorization", "Bearer tok")
            .match_body(mockito::Matcher::JsonString(
                r#"{"password":"hunter2"}"#.to_string(),
            ))
            .with_status(204)
            .create_async()
            .await;
        let client = Client::new();
        let base = format!("{}/", server.url());
        post_password(&client, &base, "tok", "9", "hunter2").await.unwrap();
        m.assert_async().await;
    }

    #[tokio::test]
    async fn post_test_connection_decodes_probed_folders() {
        let mut server = mockito::Server::new_async().await;
        let _m = server
            .mock("POST", "/v1/admin/accounts/9/test-connection")
            .with_status(200)
            .with_header("content-type", "application/json")
            .with_body(r#"{"folders":[{"name":"INBOX","flags":["\\HasNoChildren"]}]}"#)
            .create_async()
            .await;
        let client = Client::new();
        let base = format!("{}/", server.url());
        let got = post_test_connection(&client, &base, "tok", "9").await.unwrap();
        assert_eq!(got.folders.len(), 1);
        assert_eq!(got.folders[0].name, "INBOX");
        assert_eq!(got.folders[0].flags, vec!["\\HasNoChildren".to_string()]);
    }

    #[tokio::test]
    async fn post_test_connection_maps_400_connect_failure() {
        let mut server = mockito::Server::new_async().await;
        let _m = server
            .mock("POST", "/v1/admin/accounts/9/test-connection")
            .with_status(400)
            .with_body(r#"{"detail":"[Errno 8] nodename nor servname provided"}"#)
            .create_async()
            .await;
        let client = Client::new();
        let base = format!("{}/", server.url());
        let err = post_test_connection(&client, &base, "tok", "9").await.unwrap_err();
        match err {
            AuthError::Http(crate::http::errors::HttpError::HttpStatus { status, .. }) => {
                assert_eq!(status, 400);
            }
            other => panic!("expected HttpStatus 400, got {other:?}"),
        }
    }
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
cd gui/src-tauri && cargo test admin::accounts
```

Expected: FAIL to compile — `cannot find function 'delete_one'` / `'post_password'` / `'post_test_connection'`.

- [ ] **Step 3: Write the implementation**

Extend the imports:

```rust
use crate::http::client::{
    build_pinned_client, http_delete, http_get_json, http_patch_json, http_post_json,
    http_post_json_no_resp,
};
```

Add the types (after `AdminAccountPatch`):

```rust
#[derive(Debug, Deserialize, Serialize)]
pub struct ProbedFolder {
    pub name: String,
    pub flags: Vec<String>,
}

#[derive(Debug, Deserialize, Serialize)]
pub struct TestConnectionResult {
    pub folders: Vec<ProbedFolder>,
}

#[derive(Serialize)]
struct PasswordBody<'a> {
    password: &'a str,
}
```

Add the helpers + wrappers + commands (after `patch_update`):

```rust
async fn delete_one(
    client: &Client,
    base_url: &str,
    token: &str,
    account_id: &str,
    force: bool,
) -> Result<(), AuthError> {
    let query = if force { "?force=true" } else { "" };
    let endpoint = format!("{base_url}v1/admin/accounts/{account_id}{query}");
    Ok(http_delete(client, &endpoint, Some(token)).await?)
}

async fn post_password(
    client: &Client,
    base_url: &str,
    token: &str,
    account_id: &str,
    password: &str,
) -> Result<(), AuthError> {
    let endpoint = format!("{base_url}v1/admin/accounts/{account_id}/password");
    let body = PasswordBody { password };
    Ok(http_post_json_no_resp(client, &endpoint, &body, Some(token)).await?)
}

async fn post_test_connection(
    client: &Client,
    base_url: &str,
    token: &str,
    account_id: &str,
) -> Result<TestConnectionResult, AuthError> {
    let endpoint = format!("{base_url}v1/admin/accounts/{account_id}/test-connection");
    Ok(http_post_json(client, &endpoint, &serde_json::json!({}), Some(token)).await?)
}

pub async fn delete_admin_account(
    store: &KeyringStore,
    account_id: &str,
    force: bool,
) -> Result<(), AuthError> {
    let (url, pin, token) = read_authenticated(store)?;
    let client = build_pinned_client(&pin)?;
    delete_one(&client, &url, &token, account_id, force).await
}

pub async fn store_admin_account_password(
    store: &KeyringStore,
    account_id: &str,
    password: &str,
) -> Result<(), AuthError> {
    let (url, pin, token) = read_authenticated(store)?;
    let client = build_pinned_client(&pin)?;
    post_password(&client, &url, &token, account_id, password).await
}

pub async fn test_admin_account_connection(
    store: &KeyringStore,
    account_id: &str,
) -> Result<TestConnectionResult, AuthError> {
    let (url, pin, token) = read_authenticated(store)?;
    let client = build_pinned_client(&pin)?;
    post_test_connection(&client, &url, &token, account_id).await
}

#[tauri::command]
pub async fn delete_admin_account_cmd(
    account_id: String,
    force: bool,
) -> Result<(), AuthError> {
    let store = KeyringStore::new();
    delete_admin_account(&store, &account_id, force).await
}

#[tauri::command]
pub async fn store_admin_account_password_cmd(
    account_id: String,
    password: String,
) -> Result<(), AuthError> {
    let store = KeyringStore::new();
    store_admin_account_password(&store, &account_id, &password).await
}

#[tauri::command]
pub async fn test_admin_account_connection_cmd(
    account_id: String,
) -> Result<TestConnectionResult, AuthError> {
    let store = KeyringStore::new();
    test_admin_account_connection(&store, &account_id).await
}
```

- [ ] **Step 4: Register the commands**

In `gui/src-tauri/src/lib.rs`, after `update_admin_account_cmd,`:

```rust
            crate::commands::admin::accounts::delete_admin_account_cmd,
            crate::commands::admin::accounts::store_admin_account_password_cmd,
            crate::commands::admin::accounts::test_admin_account_connection_cmd,
```

- [ ] **Step 5: Run the tests to verify they pass**

```bash
cd gui/src-tauri && cargo test && cargo clippy --all-targets -- -D warnings
```

Expected: PASS.

- [ ] **Step 6: Check the file size**

```bash
wc -l gui/src-tauri/src/commands/admin/accounts.rs
```

Expected: under 500 lines. If it exceeds that, split the `#[cfg(test)] mod tests` block into `gui/src-tauri/src/commands/admin/accounts_tests.rs` and include it with `#[cfg(test)] #[path = "accounts_tests.rs"] mod tests;`.

- [ ] **Step 7: Commit**

```bash
git add gui/src-tauri/src/commands/admin gui/src-tauri/src/lib.rs
git commit -m "feat(gui): add admin account delete/password/test-connection commands"
```

---

### Task 7: TS admin API wrapper + pure HTTP-status helper

**Files:**
- Create: `gui/src/lib/admin_error.ts`
- Create: `gui/src/lib/admin_error.test.ts`
- Create: `gui/src/lib/api/admin_accounts.ts`
- Create: `gui/src/lib/api/admin_accounts.test.ts`

**Interfaces:**
- Consumes: the seven Tauri command names registered in Tasks 4-6.
- Produces:
  - `lib/admin_error.ts`: `export function httpStatusOf(err: unknown): number | null`, `export function isConflict(err: unknown): boolean`, `export function isForbidden(err: unknown): boolean`
  - `lib/api/admin_accounts.ts`: types `AdminAccountSummary`, `AdminAccount`, `AdminAccountInput`, `AdminAccountPatch`, `ProbedFolder`, `TestConnectionResult`; functions `listAdminAccounts()`, `getAdminAccount(id)`, `createAdminAccount(input)`, `updateAdminAccount(id, patch)`, `deleteAdminAccount(id, force)`, `storeAdminAccountPassword(id, password)`, `testAdminAccountConnection(id)`

- [ ] **Step 1: Write the failing tests**

Create `gui/src/lib/admin_error.test.ts`:

```ts
import { describe, expect, it } from "vitest";

import { httpStatusOf, isConflict, isForbidden } from "./admin_error";

describe("httpStatusOf", () => {
  it("reads a top-level HttpStatus", () => {
    expect(httpStatusOf({ kind: "HttpStatus", detail: { status: 404, body: "" } })).toBe(404);
  });

  it("reads an HttpStatus nested under AuthError::Http", () => {
    const err = {
      kind: "Http",
      detail: { kind: "HttpStatus", detail: { status: 409, body: "in use" } },
    };
    expect(httpStatusOf(err)).toBe(409);
  });

  it("returns null for a non-HTTP error", () => {
    expect(httpStatusOf({ kind: "NotLoggedIn" })).toBeNull();
  });

  it("returns null for junk input", () => {
    expect(httpStatusOf(null)).toBeNull();
    expect(httpStatusOf("boom")).toBeNull();
    expect(httpStatusOf(undefined)).toBeNull();
  });

  it("does not loop forever on a self-referential error", () => {
    const err: Record<string, unknown> = { kind: "Http" };
    err.detail = err;
    expect(httpStatusOf(err)).toBeNull();
  });
});

describe("isConflict / isForbidden", () => {
  it("detects 409", () => {
    expect(isConflict({ kind: "HttpStatus", detail: { status: 409 } })).toBe(true);
    expect(isConflict({ kind: "HttpStatus", detail: { status: 400 } })).toBe(false);
  });

  it("detects 403", () => {
    expect(isForbidden({ kind: "HttpStatus", detail: { status: 403 } })).toBe(true);
    expect(isForbidden({ kind: "NotLoggedIn" })).toBe(false);
  });
});
```

Create `gui/src/lib/api/admin_accounts.test.ts`:

```ts
import { beforeEach, describe, expect, it, vi } from "vitest";

const invokeMock = vi.hoisted(() => vi.fn());
vi.mock("@tauri-apps/api/core", () => ({ invoke: invokeMock }));

import {
  createAdminAccount,
  deleteAdminAccount,
  getAdminAccount,
  listAdminAccounts,
  storeAdminAccountPassword,
  testAdminAccountConnection,
  updateAdminAccount,
} from "./admin_accounts";

describe("admin_accounts", () => {
  beforeEach(() => {
    invokeMock.mockReset();
    invokeMock.mockResolvedValue(undefined);
  });

  it("listAdminAccounts invokes list_admin_accounts_cmd", async () => {
    invokeMock.mockResolvedValueOnce([]);
    await listAdminAccounts();
    expect(invokeMock).toHaveBeenCalledWith("list_admin_accounts_cmd");
  });

  it("getAdminAccount passes accountId", async () => {
    await getAdminAccount("3");
    expect(invokeMock).toHaveBeenCalledWith("get_admin_account_cmd", { accountId: "3" });
  });

  it("createAdminAccount passes the input under `input`", async () => {
    const input = {
      name: "n",
      email_address: "a@b.c",
      auth_method: "archive" as const,
    };
    await createAdminAccount(input);
    expect(invokeMock).toHaveBeenCalledWith("create_admin_account_cmd", { input });
  });

  it("updateAdminAccount passes accountId and patch", async () => {
    await updateAdminAccount("3", { sync_enabled: false });
    expect(invokeMock).toHaveBeenCalledWith("update_admin_account_cmd", {
      accountId: "3",
      patch: { sync_enabled: false },
    });
  });

  it("deleteAdminAccount defaults force to false", async () => {
    await deleteAdminAccount("3");
    expect(invokeMock).toHaveBeenCalledWith("delete_admin_account_cmd", {
      accountId: "3",
      force: false,
    });
  });

  it("deleteAdminAccount forwards force=true", async () => {
    await deleteAdminAccount("3", true);
    expect(invokeMock).toHaveBeenCalledWith("delete_admin_account_cmd", {
      accountId: "3",
      force: true,
    });
  });

  it("storeAdminAccountPassword passes the password", async () => {
    await storeAdminAccountPassword("3", "hunter2");
    expect(invokeMock).toHaveBeenCalledWith("store_admin_account_password_cmd", {
      accountId: "3",
      password: "hunter2",
    });
  });

  it("testAdminAccountConnection passes accountId", async () => {
    invokeMock.mockResolvedValueOnce({ folders: [] });
    await testAdminAccountConnection("3");
    expect(invokeMock).toHaveBeenCalledWith("test_admin_account_connection_cmd", {
      accountId: "3",
    });
  });
});
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
cd gui && npx vitest run src/lib/admin_error.test.ts src/lib/api/admin_accounts.test.ts
```

Expected: FAIL — cannot resolve `./admin_error` / `./admin_accounts`.

- [ ] **Step 3: Write `admin_error.ts`**

Create `gui/src/lib/admin_error.ts`:

```ts
/**
 * Pure helpers for branching on the HTTP status buried inside a Tauri
 * error value.
 *
 * The Rust side serialises tagged enums as `{ kind, detail }` and nests
 * them (`AuthError::Http(HttpError::HttpStatus { status, body })` arrives
 * as `{kind:"Http",detail:{kind:"HttpStatus",detail:{status,body}}}`).
 * `formatError` already renders these for display; this module exists for
 * the cases where the UI must *act* on the status — a 409 offering a
 * force-delete, a 403 explaining that admin rights were revoked.
 */

const CONFLICT = 409;
const FORBIDDEN = 403;

// Bounds the walk so a malformed (or self-referential) error object cannot
// spin. The real nesting is at most three levels deep.
const MAX_DEPTH = 8;

export function httpStatusOf(err: unknown): number | null {
  let node: unknown = err;
  for (let depth = 0; depth < MAX_DEPTH; depth += 1) {
    if (!node || typeof node !== "object") return null;
    const { kind, detail } = node as { kind?: unknown; detail?: unknown };
    if (kind === "HttpStatus" && detail && typeof detail === "object") {
      const status = (detail as { status?: unknown }).status;
      return typeof status === "number" ? status : null;
    }
    if (detail === node) return null;
    node = detail;
  }
  return null;
}

export function isConflict(err: unknown): boolean {
  return httpStatusOf(err) === CONFLICT;
}

export function isForbidden(err: unknown): boolean {
  return httpStatusOf(err) === FORBIDDEN;
}
```

- [ ] **Step 4: Write `admin_accounts.ts`**

Create `gui/src/lib/api/admin_accounts.ts`:

```ts
/**
 * Typed wrappers over the admin-account Tauri commands, which proxy
 * `/v1/admin/accounts*` with the stored bearer token.
 *
 * `AdminAccountPatch` fields are optional by design: the Rust layer omits
 * unset keys from the PATCH body, and the server writes every key it
 * receives — sending an explicit null would blank the column.
 */
import { invoke } from "@tauri-apps/api/core";

export type AdminAuthMethod = "password" | "oauth2" | "archive";

export interface AdminAccountSummary {
  id: string;
  name: string;
  email_address: string;
  auth_method: AdminAuthMethod;
  sync_enabled: boolean;
}

export interface AdminAccount {
  id: string;
  name: string;
  email_address: string;
  auth_method: AdminAuthMethod;
  oauth_provider: string | null;
  imap_host: string | null;
  imap_port: number | null;
  folder_allow: string[] | null;
  folder_deny: string[] | null;
  folder_deny_flags: string[] | null;
  sync_enabled: boolean;
  created_at: string;
  updated_at: string;
}

export interface AdminAccountInput {
  name: string;
  email_address: string;
  auth_method: AdminAuthMethod;
  imap_host?: string;
  imap_port?: number;
  oauth_provider?: string;
  folder_allow?: string[];
  folder_deny?: string[];
  folder_deny_flags?: string[];
}

export interface AdminAccountPatch {
  email_address?: string;
  auth_method?: AdminAuthMethod;
  imap_host?: string;
  imap_port?: number;
  oauth_provider?: string;
  folder_allow?: string[];
  folder_deny?: string[];
  folder_deny_flags?: string[];
  sync_enabled?: boolean;
}

export interface ProbedFolder {
  name: string;
  flags: string[];
}

export interface TestConnectionResult {
  folders: ProbedFolder[];
}

export async function listAdminAccounts(): Promise<AdminAccountSummary[]> {
  return invoke<AdminAccountSummary[]>("list_admin_accounts_cmd");
}

export async function getAdminAccount(accountId: string): Promise<AdminAccount> {
  return invoke<AdminAccount>("get_admin_account_cmd", { accountId });
}

export async function createAdminAccount(
  input: AdminAccountInput,
): Promise<AdminAccount> {
  return invoke<AdminAccount>("create_admin_account_cmd", { input });
}

export async function updateAdminAccount(
  accountId: string,
  patch: AdminAccountPatch,
): Promise<AdminAccount> {
  return invoke<AdminAccount>("update_admin_account_cmd", { accountId, patch });
}

export async function deleteAdminAccount(
  accountId: string,
  force: boolean = false,
): Promise<void> {
  return invoke<void>("delete_admin_account_cmd", { accountId, force });
}

export async function storeAdminAccountPassword(
  accountId: string,
  password: string,
): Promise<void> {
  return invoke<void>("store_admin_account_password_cmd", { accountId, password });
}

export async function testAdminAccountConnection(
  accountId: string,
): Promise<TestConnectionResult> {
  return invoke<TestConnectionResult>("test_admin_account_connection_cmd", {
    accountId,
  });
}
```

- [ ] **Step 5: Run the tests to verify they pass**

```bash
cd gui && npx vitest run src/lib/admin_error.test.ts src/lib/api/admin_accounts.test.ts
```

Expected: PASS.

- [ ] **Step 6: Run svelte-check**

```bash
cd gui && npm run check
```

Expected: 0 errors.

- [ ] **Step 7: Commit**

```bash
git add gui/src/lib/admin_error.ts gui/src/lib/admin_error.test.ts \
        gui/src/lib/api/admin_accounts.ts gui/src/lib/api/admin_accounts.test.ts
git commit -m "feat(gui): add admin accounts TS API wrapper and pure http-status helper"
```

---

### Task 8: `AccountsPanel` — list, sync toggle, delete

**Files:**
- Create: `gui/src/components/admin/AccountsPanel.svelte`
- Create: `gui/src/components/admin/AccountsPanel.test.ts`

**Interfaces:**
- Consumes: `listAdminAccounts`, `updateAdminAccount`, `deleteAdminAccount` (Task 7); `formatError` (`lib/format_error`); `isConflict` (Task 7).
- Produces: `AccountsPanel` component, no props. Test ids: `accounts-loading`, `accounts-error`, `accounts-empty`, `account-row-<id>`, `toggle-sync-<id>`, `delete-account-<id>`, `confirm-force-delete-<id>`, `new-account`, `accounts-refresh`. Emits no events; Task 11 mounts it in `AdminView`.

Selection/editing is deferred to Task 9, which adds the `selectedId` branch to this same component.

- [ ] **Step 1: Write the failing test**

Create `gui/src/components/admin/AccountsPanel.test.ts`:

```ts
import { fireEvent, render, waitFor } from "@testing-library/svelte";
import { beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({
  listAdminAccounts: vi.fn(),
  updateAdminAccount: vi.fn(),
  deleteAdminAccount: vi.fn(),
  getAdminAccount: vi.fn(),
  createAdminAccount: vi.fn(),
  storeAdminAccountPassword: vi.fn(),
  testAdminAccountConnection: vi.fn(),
}));
vi.mock("../../lib/api/admin_accounts", () => api);

import AccountsPanel from "./AccountsPanel.svelte";

const ROWS = [
  {
    id: "1",
    name: "gmail",
    email_address: "a@b.c",
    auth_method: "oauth2",
    sync_enabled: true,
  },
  {
    id: "2",
    name: "archive",
    email_address: "old@b.c",
    auth_method: "archive",
    sync_enabled: false,
  },
];

describe("AccountsPanel", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.listAdminAccounts.mockResolvedValue(ROWS);
    api.updateAdminAccount.mockResolvedValue({});
    api.deleteAdminAccount.mockResolvedValue(undefined);
  });

  it("lists accounts fetched on mount", async () => {
    const { container } = render(AccountsPanel);
    await waitFor(() => {
      expect(container.querySelector('[data-testid="account-row-1"]')).toBeTruthy();
    });
    expect(container.querySelector('[data-testid="account-row-2"]')).toBeTruthy();
    expect(api.listAdminAccounts).toHaveBeenCalledTimes(1);
  });

  it("renders an empty state when there are no accounts", async () => {
    api.listAdminAccounts.mockResolvedValueOnce([]);
    const { container } = render(AccountsPanel);
    await waitFor(() => {
      expect(container.querySelector('[data-testid="accounts-empty"]')).toBeTruthy();
    });
  });

  it("surfaces a load failure instead of failing silently", async () => {
    api.listAdminAccounts.mockRejectedValueOnce({
      kind: "Http",
      detail: { kind: "HttpStatus", detail: { status: 403, body: "nope" } },
    });
    const { container } = render(AccountsPanel);
    await waitFor(() => {
      const err = container.querySelector('[data-testid="accounts-error"]');
      expect(err).toBeTruthy();
      expect(err?.textContent).toContain("403");
    });
  });

  it("toggles sync_enabled through updateAdminAccount", async () => {
    const { container } = render(AccountsPanel);
    await waitFor(() => {
      expect(container.querySelector('[data-testid="toggle-sync-1"]')).toBeTruthy();
    });
    const btn = container.querySelector(
      '[data-testid="toggle-sync-1"]',
    ) as HTMLButtonElement;
    await fireEvent.click(btn);
    expect(api.updateAdminAccount).toHaveBeenCalledWith("1", { sync_enabled: false });
  });

  it("deletes without force on the first attempt", async () => {
    const { container } = render(AccountsPanel);
    await waitFor(() => {
      expect(container.querySelector('[data-testid="delete-account-2"]')).toBeTruthy();
    });
    await fireEvent.click(
      container.querySelector('[data-testid="delete-account-2"]') as HTMLButtonElement,
    );
    expect(api.deleteAdminAccount).toHaveBeenCalledWith("2", false);
  });

  it("offers a force-delete confirmation on 409 and retries with force", async () => {
    api.deleteAdminAccount.mockRejectedValueOnce({
      kind: "Http",
      detail: {
        kind: "HttpStatus",
        detail: { status: 409, body: '{"detail":"account 2 has 1200 messages"}' },
      },
    });
    const { container } = render(AccountsPanel);
    await waitFor(() => {
      expect(container.querySelector('[data-testid="delete-account-2"]')).toBeTruthy();
    });
    await fireEvent.click(
      container.querySelector('[data-testid="delete-account-2"]') as HTMLButtonElement,
    );
    await waitFor(() => {
      expect(
        container.querySelector('[data-testid="confirm-force-delete-2"]'),
      ).toBeTruthy();
    });
    await fireEvent.click(
      container.querySelector(
        '[data-testid="confirm-force-delete-2"]',
      ) as HTMLButtonElement,
    );
    expect(api.deleteAdminAccount).toHaveBeenLastCalledWith("2", true);
  });
});
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
cd gui && npx vitest run src/components/admin/AccountsPanel.test.ts
```

Expected: FAIL — cannot resolve `./AccountsPanel.svelte`.

- [ ] **Step 3: Write the component**

Create `gui/src/components/admin/AccountsPanel.svelte`:

```svelte
<script lang="ts">
  /**
   * Admin → Accounts. Lists every configured account and offers the
   * per-row operations the JSON admin API exposes: pause/resume sync and
   * delete. A delete the server refuses because messages reference the
   * account (409) escalates to an explicit force confirmation rather than
   * a silent no-op.
   */
  import { onMount } from "svelte";

  import {
    deleteAdminAccount,
    listAdminAccounts,
    updateAdminAccount,
    type AdminAccountSummary,
  } from "../../lib/api/admin_accounts";
  import { isConflict } from "../../lib/admin_error";
  import { formatError } from "../../lib/format_error";

  let rows: AdminAccountSummary[] = $state([]);
  let loading: boolean = $state(true);
  let errorMessage: string | null = $state(null);
  let busyId: string | null = $state(null);
  let forceDeleteId: string | null = $state(null);
  let forceDeleteReason: string | null = $state(null);

  onMount(load);

  async function load(): Promise<void> {
    loading = true;
    errorMessage = null;
    try {
      rows = await listAdminAccounts();
    } catch (err: unknown) {
      errorMessage = formatError(err);
    } finally {
      loading = false;
    }
  }

  async function onToggleSync(row: AdminAccountSummary): Promise<void> {
    busyId = row.id;
    errorMessage = null;
    try {
      await updateAdminAccount(row.id, { sync_enabled: !row.sync_enabled });
      await load();
    } catch (err: unknown) {
      errorMessage = formatError(err);
    } finally {
      busyId = null;
    }
  }

  async function onDelete(row: AdminAccountSummary, force: boolean): Promise<void> {
    busyId = row.id;
    errorMessage = null;
    try {
      await deleteAdminAccount(row.id, force);
      forceDeleteId = null;
      forceDeleteReason = null;
      await load();
    } catch (err: unknown) {
      if (!force && isConflict(err)) {
        forceDeleteId = row.id;
        forceDeleteReason = formatError(err);
      } else {
        forceDeleteId = null;
        errorMessage = formatError(err);
      }
    } finally {
      busyId = null;
    }
  }
</script>

<div class="panel">
  <div class="toolbar">
    <button data-testid="accounts-refresh" onclick={load} disabled={loading}>Refresh</button>
  </div>

  {#if errorMessage}
    <p class="error" data-testid="accounts-error" role="alert">{errorMessage}</p>
  {/if}

  {#if loading}
    <p data-testid="accounts-loading">Loading accounts…</p>
  {:else if rows.length === 0}
    <p data-testid="accounts-empty">No accounts configured yet.</p>
  {:else}
    <table>
      <thead>
        <tr>
          <th>Name</th>
          <th>Address</th>
          <th>Auth</th>
          <th>Sync</th>
          <th></th>
        </tr>
      </thead>
      <tbody>
        {#each rows as row (row.id)}
          <tr data-testid="account-row-{row.id}">
            <td>{row.name}</td>
            <td>{row.email_address}</td>
            <td>{row.auth_method}</td>
            <td>
              <button
                data-testid="toggle-sync-{row.id}"
                onclick={() => onToggleSync(row)}
                disabled={busyId === row.id}
              >{row.sync_enabled ? "Pause" : "Resume"}</button>
            </td>
            <td>
              <button
                class="danger"
                data-testid="delete-account-{row.id}"
                onclick={() => onDelete(row, false)}
                disabled={busyId === row.id}
              >Delete</button>
            </td>
          </tr>
          {#if forceDeleteId === row.id}
            <tr class="confirm-row">
              <td colspan="5">
                <span class="confirm-text">{forceDeleteReason}</span>
                <button
                  class="danger"
                  data-testid="confirm-force-delete-{row.id}"
                  onclick={() => onDelete(row, true)}
                  disabled={busyId === row.id}
                >Delete anyway (removes its messages)</button>
                <button onclick={() => (forceDeleteId = null)}>Cancel</button>
              </td>
            </tr>
          {/if}
        {/each}
      </tbody>
    </table>
  {/if}
</div>

<style>
  .panel {
    display: flex;
    flex-direction: column;
    gap: 0.75rem;
    font-size: 0.9rem;
  }
  .toolbar {
    display: flex;
    gap: 0.5rem;
  }
  table {
    width: 100%;
    border-collapse: collapse;
  }
  th,
  td {
    text-align: left;
    padding: 0.35rem 0.5rem;
    border-bottom: 1px solid #eee;
  }
  th {
    font-weight: 600;
    color: #555;
  }
  .danger {
    color: #b3261e;
  }
  .confirm-row td {
    background: #fff4f3;
  }
  .confirm-text {
    margin-right: 0.75rem;
  }
  .error {
    color: #b3261e;
    margin: 0;
  }
</style>
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
cd gui && npx vitest run src/components/admin/AccountsPanel.test.ts
```

Expected: PASS, 6 tests.

- [ ] **Step 5: Commit**

```bash
git add gui/src/components/admin/AccountsPanel.svelte gui/src/components/admin/AccountsPanel.test.ts
git commit -m "feat(gui): add admin AccountsPanel with sync toggle and force-delete escalation"
```

---

### Task 9: `AccountForm` — create and edit with inline field errors

**Files:**
- Create: `gui/src/components/admin/AccountForm.svelte`
- Create: `gui/src/components/admin/AccountForm.test.ts`
- Modify: `gui/src/components/admin/AccountsPanel.svelte` (open the form)
- Modify: `gui/src/components/admin/AccountsPanel.test.ts` (one wiring test)

**Interfaces:**
- Consumes: `createAdminAccount`, `updateAdminAccount`, `getAdminAccount` (Task 7).
- Produces: `AccountForm` with props `{ accountId: string | null; onSaved: () => void; onCancel: () => void }`. `accountId === null` means create. Test ids: `account-form`, `field-name`, `field-email`, `field-auth-method`, `field-imap-host`, `field-imap-port`, `account-form-submit`, `account-form-cancel`, `account-form-error`.

- [ ] **Step 1: Write the failing test**

Create `gui/src/components/admin/AccountForm.test.ts`:

```ts
import { fireEvent, render, waitFor } from "@testing-library/svelte";
import { beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({
  listAdminAccounts: vi.fn(),
  updateAdminAccount: vi.fn(),
  deleteAdminAccount: vi.fn(),
  getAdminAccount: vi.fn(),
  createAdminAccount: vi.fn(),
  storeAdminAccountPassword: vi.fn(),
  testAdminAccountConnection: vi.fn(),
}));
vi.mock("../../lib/api/admin_accounts", () => api);

import AccountForm from "./AccountForm.svelte";

const EXISTING = {
  id: "5",
  name: "gmail",
  email_address: "a@b.c",
  auth_method: "password",
  oauth_provider: null,
  imap_host: "imap.example.com",
  imap_port: 993,
  folder_allow: null,
  folder_deny: null,
  folder_deny_flags: null,
  sync_enabled: true,
  created_at: "2026-01-01T00:00:00+00:00",
  updated_at: "2026-01-01T00:00:00+00:00",
};

function field(container: Element, id: string): HTMLInputElement {
  return container.querySelector(`[data-testid="${id}"]`) as HTMLInputElement;
}

describe("AccountForm", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.createAdminAccount.mockResolvedValue(EXISTING);
    api.updateAdminAccount.mockResolvedValue(EXISTING);
    api.getAdminAccount.mockResolvedValue(EXISTING);
  });

  it("creates a new account from the entered fields", async () => {
    const onSaved = vi.fn();
    const { container } = render(AccountForm, {
      props: { accountId: null, onSaved, onCancel: vi.fn() },
    });
    await fireEvent.input(field(container, "field-name"), {
      target: { value: "work" },
    });
    await fireEvent.input(field(container, "field-email"), {
      target: { value: "w@e.rk" },
    });
    await fireEvent.change(field(container, "field-auth-method"), {
      target: { value: "password" },
    });
    await fireEvent.input(field(container, "field-imap-host"), {
      target: { value: "imap.e.rk" },
    });
    await fireEvent.input(field(container, "field-imap-port"), {
      target: { value: "993" },
    });
    await fireEvent.click(
      container.querySelector('[data-testid="account-form-submit"]') as HTMLButtonElement,
    );

    await waitFor(() => expect(api.createAdminAccount).toHaveBeenCalledTimes(1));
    expect(api.createAdminAccount).toHaveBeenCalledWith({
      name: "work",
      email_address: "w@e.rk",
      auth_method: "password",
      imap_host: "imap.e.rk",
      imap_port: 993,
    });
    expect(onSaved).toHaveBeenCalledTimes(1);
  });

  it("loads the existing account when editing and patches only changed fields", async () => {
    const onSaved = vi.fn();
    const { container } = render(AccountForm, {
      props: { accountId: "5", onSaved, onCancel: vi.fn() },
    });
    await waitFor(() => expect(field(container, "field-email").value).toBe("a@b.c"));

    await fireEvent.input(field(container, "field-email"), {
      target: { value: "new@b.c" },
    });
    await fireEvent.click(
      container.querySelector('[data-testid="account-form-submit"]') as HTMLButtonElement,
    );

    await waitFor(() => expect(api.updateAdminAccount).toHaveBeenCalledTimes(1));
    expect(api.updateAdminAccount).toHaveBeenCalledWith("5", {
      email_address: "new@b.c",
    });
  });

  it("renders a server validation error instead of failing silently", async () => {
    api.createAdminAccount.mockRejectedValueOnce({
      kind: "Http",
      detail: {
        kind: "HttpStatus",
        detail: {
          status: 400,
          body: '{"detail":"imap_host is required for live accounts"}',
        },
      },
    });
    const { container } = render(AccountForm, {
      props: { accountId: null, onSaved: vi.fn(), onCancel: vi.fn() },
    });
    await fireEvent.input(field(container, "field-name"), { target: { value: "x" } });
    await fireEvent.input(field(container, "field-email"), {
      target: { value: "x@y.z" },
    });
    await fireEvent.click(
      container.querySelector('[data-testid="account-form-submit"]') as HTMLButtonElement,
    );
    await waitFor(() => {
      const err = container.querySelector('[data-testid="account-form-error"]');
      expect(err?.textContent).toContain("imap_host is required");
    });
  });

  it("calls onCancel without touching the API", async () => {
    const onCancel = vi.fn();
    const { container } = render(AccountForm, {
      props: { accountId: null, onSaved: vi.fn(), onCancel },
    });
    await fireEvent.click(
      container.querySelector('[data-testid="account-form-cancel"]') as HTMLButtonElement,
    );
    expect(onCancel).toHaveBeenCalledTimes(1);
    expect(api.createAdminAccount).not.toHaveBeenCalled();
  });
});
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
cd gui && npx vitest run src/components/admin/AccountForm.test.ts
```

Expected: FAIL — cannot resolve `./AccountForm.svelte`.

- [ ] **Step 3: Write the component**

Create `gui/src/components/admin/AccountForm.svelte`:

```svelte
<script lang="ts">
  /**
   * Create / edit one account. On edit the form loads the current row and
   * submits only the fields the operator actually changed — the server
   * writes every key it receives, so sending an untouched field would
   * rewrite it (and sending null would blank it).
   *
   * `archive` accounts have no IMAP endpoint, so the host/port inputs are
   * hidden for that auth method; `oauth2` accounts get their refresh token
   * from the web OAuth flow, not from this form.
   */
  import { onMount } from "svelte";

  import {
    createAdminAccount,
    getAdminAccount,
    updateAdminAccount,
    type AdminAccount,
    type AdminAccountInput,
    type AdminAccountPatch,
    type AdminAuthMethod,
  } from "../../lib/api/admin_accounts";
  import { formatError } from "../../lib/format_error";

  interface Props {
    accountId: string | null;
    onSaved: () => void;
    onCancel: () => void;
  }
  let { accountId, onSaved, onCancel }: Props = $props();

  const DEFAULT_IMAP_PORT = 993;

  let name: string = $state("");
  let emailAddress: string = $state("");
  let authMethod: AdminAuthMethod = $state("password");
  let imapHost: string = $state("");
  let imapPort: string = $state("");
  let loaded: AdminAccount | null = $state(null);
  let errorMessage: string | null = $state(null);
  let saving: boolean = $state(false);

  const isEdit = $derived(accountId !== null);
  const needsImap = $derived(authMethod !== "archive");

  onMount(async () => {
    if (accountId === null) return;
    try {
      const acct = await getAdminAccount(accountId);
      loaded = acct;
      name = acct.name;
      emailAddress = acct.email_address;
      authMethod = acct.auth_method;
      imapHost = acct.imap_host ?? "";
      imapPort = acct.imap_port === null ? "" : String(acct.imap_port);
    } catch (err: unknown) {
      errorMessage = formatError(err);
    }
  });

  function parsePort(raw: string): number | undefined {
    const trimmed = raw.trim();
    if (trimmed === "") return undefined;
    const n = Number(trimmed);
    return Number.isInteger(n) ? n : undefined;
  }

  function buildCreateInput(): AdminAccountInput {
    const input: AdminAccountInput = {
      name: name.trim(),
      email_address: emailAddress.trim(),
      auth_method: authMethod,
    };
    if (needsImap) {
      const host = imapHost.trim();
      if (host !== "") input.imap_host = host;
      const port = parsePort(imapPort);
      if (port !== undefined) input.imap_port = port;
    }
    if (authMethod === "oauth2") input.oauth_provider = "gmail";
    return input;
  }

  function buildPatch(current: AdminAccount): AdminAccountPatch {
    const patch: AdminAccountPatch = {};
    if (emailAddress.trim() !== current.email_address) {
      patch.email_address = emailAddress.trim();
    }
    if (authMethod !== current.auth_method) patch.auth_method = authMethod;
    const host = imapHost.trim();
    if (host !== (current.imap_host ?? "")) patch.imap_host = host;
    const port = parsePort(imapPort);
    if (port !== (current.imap_port ?? undefined)) {
      if (port !== undefined) patch.imap_port = port;
    }
    return patch;
  }

  async function onSubmit(event: Event): Promise<void> {
    event.preventDefault();
    saving = true;
    errorMessage = null;
    try {
      if (loaded !== null) {
        await updateAdminAccount(loaded.id, buildPatch(loaded));
      } else {
        await createAdminAccount(buildCreateInput());
      }
      onSaved();
    } catch (err: unknown) {
      errorMessage = formatError(err);
    } finally {
      saving = false;
    }
  }
</script>

<form data-testid="account-form" onsubmit={onSubmit}>
  <h3>{isEdit ? "Edit account" : "New account"}</h3>

  {#if errorMessage}
    <p class="error" data-testid="account-form-error" role="alert">{errorMessage}</p>
  {/if}

  <label>
    Name
    <input data-testid="field-name" bind:value={name} disabled={isEdit} required />
  </label>

  <label>
    Email address
    <input data-testid="field-email" bind:value={emailAddress} required />
  </label>

  <label>
    Auth method
    <select data-testid="field-auth-method" bind:value={authMethod}>
      <option value="password">password</option>
      <option value="oauth2">oauth2 (Gmail)</option>
      <option value="archive">archive (no IMAP)</option>
    </select>
  </label>

  {#if needsImap}
    <label>
      IMAP host
      <input data-testid="field-imap-host" bind:value={imapHost} />
    </label>
    <label>
      IMAP port
      <input
        data-testid="field-imap-port"
        bind:value={imapPort}
        inputmode="numeric"
        placeholder={String(DEFAULT_IMAP_PORT)}
      />
    </label>
  {/if}

  <div class="actions">
    <button type="submit" data-testid="account-form-submit" disabled={saving}>
      {isEdit ? "Save" : "Create"}
    </button>
    <button type="button" data-testid="account-form-cancel" onclick={onCancel}>
      Cancel
    </button>
  </div>
</form>

<style>
  form {
    display: flex;
    flex-direction: column;
    gap: 0.6rem;
    max-width: 30rem;
    font-size: 0.9rem;
  }
  h3 {
    margin: 0;
    font-size: 1rem;
  }
  label {
    display: flex;
    flex-direction: column;
    gap: 0.2rem;
  }
  .actions {
    display: flex;
    gap: 0.5rem;
  }
  .error {
    color: #b3261e;
    margin: 0;
  }
</style>
```

Two deliberate constraints encoded above:

- The `name` field is **disabled when editing** because `_AccountPatch` has no `name` key — the server cannot rename an account through this endpoint.
- `buildPatch` can set a port but **cannot clear one**. Clearing the input yields `undefined`, and the omit-unset rule forbids sending `null` (which would blank the column via `update_account`). Switching the account to `archive` is the supported way to drop an IMAP endpoint. Do not "fix" this by sending null.

- [ ] **Step 4: Run the test to verify it passes**

```bash
cd gui && npx vitest run src/components/admin/AccountForm.test.ts
```

Expected: PASS, 4 tests.

If a test fails because jsdom did not dispatch `submit` from the button click, replace the click with an explicit form submit in that test — the component is correct either way:

```ts
    await fireEvent.submit(
      container.querySelector('[data-testid="account-form"]') as HTMLFormElement,
    );
```

- [ ] **Step 5: Wire the form into AccountsPanel**

In `gui/src/components/admin/AccountsPanel.svelte`, add the import:

```svelte
  import AccountForm from "./AccountForm.svelte";
```

add the state beside the others:

```svelte
  let formOpen: boolean = $state(false);
  let editingId: string | null = $state(null);
```

add a handler:

```svelte
  function openForm(id: string | null): void {
    editingId = id;
    formOpen = true;
  }

  async function onFormSaved(): Promise<void> {
    formOpen = false;
    await load();
  }
```

add a "New account" button to the toolbar, before Refresh:

```svelte
    <button data-testid="new-account" onclick={() => openForm(null)}>New account</button>
```

render the form above the table (immediately after the error paragraph):

```svelte
  {#if formOpen}
    <AccountForm
      accountId={editingId}
      onSaved={onFormSaved}
      onCancel={() => (formOpen = false)}
    />
  {/if}
```

and add an Edit button to each row, in the same `<td>` as Delete, before it:

```svelte
              <button
                data-testid="edit-account-{row.id}"
                onclick={() => openForm(row.id)}
                disabled={busyId === row.id}
              >Edit</button>
```

- [ ] **Step 6: Add the wiring test**

Append inside `describe("AccountsPanel", ...)` in `gui/src/components/admin/AccountsPanel.test.ts`:

```ts
  it("opens the create form from the toolbar", async () => {
    const { container } = render(AccountsPanel);
    await waitFor(() => {
      expect(container.querySelector('[data-testid="new-account"]')).toBeTruthy();
    });
    await fireEvent.click(
      container.querySelector('[data-testid="new-account"]') as HTMLButtonElement,
    );
    await waitFor(() => {
      expect(container.querySelector('[data-testid="account-form"]')).toBeTruthy();
    });
    expect(api.getAdminAccount).not.toHaveBeenCalled();
  });

  it("opens the edit form preloaded from the row", async () => {
    api.getAdminAccount.mockResolvedValueOnce({
      ...ROWS[0],
      oauth_provider: null,
      imap_host: null,
      imap_port: null,
      folder_allow: null,
      folder_deny: null,
      folder_deny_flags: null,
      created_at: "2026-01-01T00:00:00+00:00",
      updated_at: "2026-01-01T00:00:00+00:00",
    });
    const { container } = render(AccountsPanel);
    await waitFor(() => {
      expect(container.querySelector('[data-testid="edit-account-1"]')).toBeTruthy();
    });
    await fireEvent.click(
      container.querySelector('[data-testid="edit-account-1"]') as HTMLButtonElement,
    );
    await waitFor(() => expect(api.getAdminAccount).toHaveBeenCalledWith("1"));
  });
```

- [ ] **Step 7: Run the panel + form tests**

```bash
cd gui && npx vitest run src/components/admin
```

Expected: PASS, all tests in both files.

- [ ] **Step 8: Commit**

```bash
git add gui/src/components/admin
git commit -m "feat(gui): add admin AccountForm for create/edit with changed-fields-only patch"
```

---

### Task 10: `AccountSecrets` — store password + test connection

**Files:**
- Create: `gui/src/components/admin/AccountSecrets.svelte`
- Create: `gui/src/components/admin/AccountSecrets.test.ts`
- Modify: `gui/src/components/admin/AccountsPanel.svelte` (render per selected row)
- Modify: `gui/src/components/admin/AccountsPanel.test.ts` (one wiring test)

**Interfaces:**
- Consumes: `storeAdminAccountPassword`, `testAdminAccountConnection` (Task 7).
- Produces: `AccountSecrets` with props `{ accountId: string; authMethod: AdminAuthMethod }`. Test ids: `secrets-password`, `secrets-store-password`, `secrets-status`, `secrets-test-connection`, `secrets-folders`, `secrets-error`.

- [ ] **Step 1: Write the failing test**

Create `gui/src/components/admin/AccountSecrets.test.ts`:

```ts
import { fireEvent, render, waitFor } from "@testing-library/svelte";
import { beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({
  listAdminAccounts: vi.fn(),
  updateAdminAccount: vi.fn(),
  deleteAdminAccount: vi.fn(),
  getAdminAccount: vi.fn(),
  createAdminAccount: vi.fn(),
  storeAdminAccountPassword: vi.fn(),
  testAdminAccountConnection: vi.fn(),
}));
vi.mock("../../lib/api/admin_accounts", () => api);

import AccountSecrets from "./AccountSecrets.svelte";

describe("AccountSecrets", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.storeAdminAccountPassword.mockResolvedValue(undefined);
    api.testAdminAccountConnection.mockResolvedValue({
      folders: [{ name: "INBOX", flags: ["\\HasNoChildren"] }],
    });
  });

  it("stores a password and confirms", async () => {
    const { container } = render(AccountSecrets, {
      props: { accountId: "4", authMethod: "password" },
    });
    await fireEvent.input(
      container.querySelector('[data-testid="secrets-password"]') as HTMLInputElement,
      { target: { value: "hunter2" } },
    );
    await fireEvent.click(
      container.querySelector('[data-testid="secrets-store-password"]') as HTMLButtonElement,
    );
    await waitFor(() =>
      expect(api.storeAdminAccountPassword).toHaveBeenCalledWith("4", "hunter2"),
    );
    await waitFor(() => {
      expect(
        container.querySelector('[data-testid="secrets-status"]')?.textContent,
      ).toContain("Password stored");
    });
  });

  it("hides the password field for oauth2 accounts", () => {
    const { container } = render(AccountSecrets, {
      props: { accountId: "4", authMethod: "oauth2" },
    });
    expect(container.querySelector('[data-testid="secrets-password"]')).toBeFalsy();
  });

  it("lists the probed folders on a successful test connection", async () => {
    const { container } = render(AccountSecrets, {
      props: { accountId: "4", authMethod: "password" },
    });
    await fireEvent.click(
      container.querySelector('[data-testid="secrets-test-connection"]') as HTMLButtonElement,
    );
    await waitFor(() => {
      const list = container.querySelector('[data-testid="secrets-folders"]');
      expect(list?.textContent).toContain("INBOX");
    });
  });

  it("surfaces a connect failure as an inline error", async () => {
    api.testAdminAccountConnection.mockRejectedValueOnce({
      kind: "Http",
      detail: {
        kind: "HttpStatus",
        detail: {
          status: 400,
          body: '{"detail":"[Errno 8] nodename nor servname provided"}',
        },
      },
    });
    const { container } = render(AccountSecrets, {
      props: { accountId: "4", authMethod: "password" },
    });
    await fireEvent.click(
      container.querySelector('[data-testid="secrets-test-connection"]') as HTMLButtonElement,
    );
    await waitFor(() => {
      expect(
        container.querySelector('[data-testid="secrets-error"]')?.textContent,
      ).toContain("nodename nor servname");
    });
  });

  it("does not offer test-connection for archive accounts", () => {
    const { container } = render(AccountSecrets, {
      props: { accountId: "4", authMethod: "archive" },
    });
    expect(container.querySelector('[data-testid="secrets-test-connection"]')).toBeFalsy();
  });
});
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
cd gui && npx vitest run src/components/admin/AccountSecrets.test.ts
```

Expected: FAIL — cannot resolve `./AccountSecrets.svelte`.

- [ ] **Step 3: Write the component**

Create `gui/src/components/admin/AccountSecrets.svelte`:

```svelte
<script lang="ts">
  /**
   * Per-account credential + reachability controls.
   *
   * Password storage applies to `password` accounts only — an `oauth2`
   * account's refresh token comes from the web consent flow, and an
   * `archive` account has no IMAP endpoint at all (so neither control
   * applies to it).
   */
  import {
    storeAdminAccountPassword,
    testAdminAccountConnection,
    type AdminAuthMethod,
    type ProbedFolder,
  } from "../../lib/api/admin_accounts";
  import { formatError } from "../../lib/format_error";

  interface Props {
    accountId: string;
    authMethod: AdminAuthMethod;
  }
  let { accountId, authMethod }: Props = $props();

  let password: string = $state("");
  let status: string | null = $state(null);
  let errorMessage: string | null = $state(null);
  let folders: ProbedFolder[] | null = $state(null);
  let busy: boolean = $state(false);

  const canStorePassword = $derived(authMethod === "password");
  const canTestConnection = $derived(authMethod !== "archive");

  async function onStorePassword(): Promise<void> {
    busy = true;
    status = null;
    errorMessage = null;
    try {
      await storeAdminAccountPassword(accountId, password);
      password = "";
      status = "Password stored.";
    } catch (err: unknown) {
      errorMessage = formatError(err);
    } finally {
      busy = false;
    }
  }

  async function onTestConnection(): Promise<void> {
    busy = true;
    status = null;
    errorMessage = null;
    folders = null;
    try {
      const result = await testAdminAccountConnection(accountId);
      folders = result.folders;
      status = `Connected. ${result.folders.length} folder(s) visible.`;
    } catch (err: unknown) {
      errorMessage = formatError(err);
    } finally {
      busy = false;
    }
  }
</script>

<div class="secrets">
  {#if status}
    <p class="ok" data-testid="secrets-status">{status}</p>
  {/if}
  {#if errorMessage}
    <p class="error" data-testid="secrets-error" role="alert">{errorMessage}</p>
  {/if}

  {#if canStorePassword}
    <div class="row">
      <input
        type="password"
        data-testid="secrets-password"
        bind:value={password}
        placeholder="IMAP password"
        autocomplete="off"
      />
      <button
        data-testid="secrets-store-password"
        onclick={onStorePassword}
        disabled={busy || password === ""}
      >Store password</button>
    </div>
  {/if}

  {#if canTestConnection}
    <div class="row">
      <button
        data-testid="secrets-test-connection"
        onclick={onTestConnection}
        disabled={busy}
      >Test connection</button>
    </div>
  {/if}

  {#if folders !== null}
    <ul data-testid="secrets-folders">
      {#each folders as folder (folder.name)}
        <li>{folder.name} <span class="flags">{folder.flags.join(" ")}</span></li>
      {/each}
    </ul>
  {/if}
</div>

<style>
  .secrets {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
    font-size: 0.9rem;
  }
  .row {
    display: flex;
    gap: 0.5rem;
    align-items: center;
  }
  ul {
    margin: 0;
    padding-left: 1.1rem;
    max-height: 12rem;
    overflow: auto;
  }
  .flags {
    color: #777;
  }
  .ok {
    color: #146c2e;
    margin: 0;
  }
  .error {
    color: #b3261e;
    margin: 0;
  }
</style>
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
cd gui && npx vitest run src/components/admin/AccountSecrets.test.ts
```

Expected: PASS, 5 tests.

- [ ] **Step 5: Wire it into AccountsPanel**

In `gui/src/components/admin/AccountsPanel.svelte`, add the import:

```svelte
  import AccountSecrets from "./AccountSecrets.svelte";
```

add state:

```svelte
  let secretsId: string | null = $state(null);
```

add a "Credentials" button to each row's action `<td>`, before Edit:

```svelte
              <button
                data-testid="open-secrets-{row.id}"
                onclick={() => (secretsId = secretsId === row.id ? null : row.id)}
              >Credentials</button>
```

and render the expander row directly after the `{#if forceDeleteId === row.id}` block, still inside the `{#each}`:

```svelte
          {#if secretsId === row.id}
            <tr class="secrets-row">
              <td colspan="5">
                <AccountSecrets accountId={row.id} authMethod={row.auth_method} />
              </td>
            </tr>
          {/if}
```

Add the style rule beside `.confirm-row td`:

```css
  .secrets-row td {
    background: #f7f9fc;
  }
```

- [ ] **Step 6: Add the wiring test**

Append inside `describe("AccountsPanel", ...)` in `gui/src/components/admin/AccountsPanel.test.ts`:

```ts
  it("expands the credentials row for a password account", async () => {
    const { container } = render(AccountsPanel);
    await waitFor(() => {
      expect(container.querySelector('[data-testid="open-secrets-1"]')).toBeTruthy();
    });
    await fireEvent.click(
      container.querySelector('[data-testid="open-secrets-1"]') as HTMLButtonElement,
    );
    await waitFor(() => {
      expect(
        container.querySelector('[data-testid="secrets-test-connection"]'),
      ).toBeTruthy();
    });
  });
```

- [ ] **Step 7: Run all admin component tests**

```bash
cd gui && npx vitest run src/components/admin
```

Expected: PASS.

- [ ] **Step 8: Check the panel's size**

```bash
wc -l gui/src/components/admin/AccountsPanel.svelte
```

Expected: under 500 lines. If it exceeds that, extract the `<tr>` body into `components/admin/AccountRow.svelte`.

- [ ] **Step 9: Commit**

```bash
git add gui/src/components/admin
git commit -m "feat(gui): add per-account credential storage and IMAP test-connection controls"
```

---

### Task 11: Mount the panel, document, and verify the whole branch

**Files:**
- Modify: `gui/src/screens/AdminView.svelte` (replace the accounts placeholder)
- Modify: `gui/src/screens/AdminView.test.ts` (mock the api module)
- Modify: `CLAUDE.md`
- Modify: `README.md` (only if it documents GUI capabilities — check first)

**Interfaces:**
- Consumes: `AccountsPanel` (Tasks 8-10).
- Produces: a shipped Accounts panel reachable at MainView → Admin → Accounts.

- [ ] **Step 1: Mount `AccountsPanel` in `AdminView`**

In `gui/src/screens/AdminView.svelte`, add the import inside `<script>`:

```svelte
  import AccountsPanel from "../components/admin/AccountsPanel.svelte";
```

and replace the accounts placeholder:

```svelte
        {#if tab === "accounts"}
          <AccountsPanel />
        {/if}
```

- [ ] **Step 2: Mock the API in the AdminView test**

`AccountsPanel` now fetches on mount, so `AdminView.test.ts` must stub the module. Add at the top of `gui/src/screens/AdminView.test.ts`, before the `import AdminView` line:

```ts
const api = vi.hoisted(() => ({
  listAdminAccounts: vi.fn(async () => []),
  updateAdminAccount: vi.fn(),
  deleteAdminAccount: vi.fn(),
  getAdminAccount: vi.fn(),
  createAdminAccount: vi.fn(),
  storeAdminAccountPassword: vi.fn(),
  testAdminAccountConnection: vi.fn(),
}));
vi.mock("../lib/api/admin_accounts", () => api);
```

- [ ] **Step 3: Run the whole frontend + Rust suite**

```bash
cd gui && npm run check && npm test
cd src-tauri && cargo test && cargo clippy --all-targets -- -D warnings
```

Expected: svelte-check 0 errors; all vitest and cargo tests PASS; no clippy warnings.

- [ ] **Step 4: Confirm the Python suite is untouched**

```bash
cd /Users/hherb/src/localmail
unset VIRTUAL_ENV && uv run --extra mcp --extra extraction pytest -q tests/ \
  --deselect tests/test_daemon_control_socket.py
```

Expected: **1749 passed**, 14 deselected — identical to the pre-branch baseline. Any change here means Python was edited, which this plan forbids.

- [ ] **Step 5: Build the app to catch anything the unit tests miss**

```bash
cd gui && npm run build
```

Expected: build succeeds.

- [ ] **Step 6: Document in CLAUDE.md**

Add a bullet to the GUI section of `CLAUDE.md` recording: the `AdminView` overlay gated on `whoami.is_admin`; that `commands/admin/accounts.rs` proxies `/v1/admin/accounts*` with the bearer token; the **omit-unset PATCH invariant** and why (`update_account` writes every key present, so an explicit null blanks the column); and that Gmail OAuth "Connect" is deliberately absent because `oauth_start` is still `require_admin_session()` (cookie-only) and no endpoint reports secret status.

- [ ] **Step 7: Check whether README.md needs an update**

```bash
grep -n -i "gui\|desktop\|tauri" README.md | head -20
```

If README documents the desktop app's capabilities, add Admin mode (accounts panel) to that list. If it does not mention the GUI, leave it alone and say so.

- [ ] **Step 8: Commit**

```bash
git add gui/src/screens/AdminView.svelte gui/src/screens/AdminView.test.ts CLAUDE.md README.md
git commit -m "feat(gui): mount the Accounts panel in AdminView and document admin mode"
```

---

## Out of scope (record as follow-ups, do not build)

- **Gmail OAuth "Connect".** `POST /v1/admin/accounts/{id}/oauth/start` still uses `require_admin_session()` (cookie-only) — it was not one of the four routers #203 swapped — so a bearer client cannot start the flow. Additionally, the design's completion check ("poll the account's secret status until the refresh token is present") has no backing field: `_account_dict` exposes no secret status, and no `/v1/admin` endpoint reports one. Both are backend gaps that must be closed before the button can work.
- **Clear-secret.** `api.admin.accounts.clear_secret` exists but has no JSON route.
- **Daemon / Users / Imports panels.** Phases 4-6; their tabs render placeholders.
