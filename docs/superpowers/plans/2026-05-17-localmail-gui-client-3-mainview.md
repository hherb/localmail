# localmail GUI Client — Sub-plan 3: Main view shell

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the Sub-plan 2 placeholder `AuthenticatedShell` with a real Layout-A 3-pane main view: account/folder tree on the left, message list in the middle, plain-text reading pane on the right. Done when you launch the app, log in, see your accounts + folders in the tree, the latest ~200 messages in the middle, and clicking a message renders its plain-text body + key headers in the reading pane.

**Architecture:** Four Rust commands (accounts, folders, changes, message detail) wrapping the existing `build_pinned_client` + `http_get_json` helpers; one new Svelte rune-backed singleton (`mail.svelte.ts`) holding selection + loaded messages + selected detail; one screen-level component (`MainView`) composing three children (`AccountTree`, `MessageList`, `ReadingPane`). HTML rendering, attachments, search, and server-side folder narrowing are **out of scope** — they land in Sub-plan 4.

**Tech Stack:** Same as Sub-plans 1 & 2 — reqwest 0.12 + rustls 0.23 (Rust), Svelte 5 runes + TypeScript, vitest, cargo test. No new dependencies in this sub-plan.

**Base branch:** `main`. After PR #19 merge (Sub-plan 2). Server lives on `worktree-phase2-hybrid-search`; nothing in this sub-plan requires server-side changes.

**Out of scope (Sub-plan 4 / 5):**
- HTML body rendering (Sub-plan 4) — this sub-plan only renders `body_text`.
- Search bar, filters, snippets, `matched_arms` UI (Sub-plan 4).
- Attachments strip, download, preview (Sub-plan 4).
- Resizable splitter / column widths (Sub-plan 5 polish).
- Server-side narrowing by account/folder (deferred — see "Known shortcut" below).
- Background polling for `/v1/changes` (Sub-plan 5).
- Token refresh background timer (already explicit out-of-scope from Sub-plan 2).

**Known shortcut (acknowledged tech debt):** The server has no `GET /v1/folders/{id}/messages` endpoint (spec mentions it; not implemented). The only listing endpoint today is `GET /v1/changes` which returns the most recent 200 messages across all accounts with no filter args. For Sub-plan 3 we accept that limitation: "All Mail" calls `/v1/changes`, and selecting an account or folder in the tree filters the **already-loaded** 200 messages client-side. This is honest about being a shell. When Sub-plan 4 wires `account_ids` / `folder_ids` through to the search backend (currently they raise `ValidationFailed`), tree clicks become server-side queries via `/v1/search`. **Do not add a new server-side endpoint in this sub-plan.**

---

## File structure

### Created

```
gui/src-tauri/src/
  commands/
    accounts.rs              # list_accounts + list_folders + tauri cmds
    messages.rs              # get_message + tauri cmd
    changes.rs               # list_recent (no-cursor /v1/changes call) + tauri cmd

gui/src/
  lib/
    api/
      types.ts               # shared TS types: AccountSummary, FolderSummary, MessageSummary, MessageDetail, Selection
    format.ts                # pure helpers: formatDate, addressLabel, truncate, isAllMail, selectionMatches
    format.test.ts           # vitest unit tests for format.ts
    stores/
      mail.svelte.ts         # singleton mail state machine
      mail.test.ts           # vitest unit tests for the store
  components/
    AccountTree.svelte       # left rail
    AccountTree.test.ts      # vitest component test
    MessageList.svelte       # middle pane
    MessageListRow.svelte    # single row (split out so MessageList stays small)
    MessageList.test.ts      # vitest component test
    ReadingPane.svelte       # right pane
    ReadingPane.test.ts      # vitest component test
  screens/
    MainView.svelte          # 3-pane layout + header (replaces AuthenticatedShell role)
```

### Modified

```
gui/src-tauri/src/
  commands/mod.rs            # add `pub mod accounts; pub mod messages; pub mod changes;`
  lib.rs                     # register new tauri commands

gui/src/
  lib/tauri.ts               # extend with listAccounts, listFolders, listRecentMessages, getMessage wrappers
  routes/Router.svelte       # render <MainView /> instead of <AuthenticatedShell /> for logged_in
```

### Removed

```
gui/src/screens/
  AuthenticatedShell.svelte  # superseded by MainView; its capabilities pills move into MainView header
```

---

## Task 0: Worktree + base verification

**Files:**
- Create worktree at: `.claude/worktrees/gui-client-3`

- [ ] **Step 1: Create worktree off main**

```bash
cd /Users/hherb/src/localmail
git fetch --all
git worktree add .claude/worktrees/gui-client-3 -b gui-client-3 main
cd .claude/worktrees/gui-client-3
git log --oneline -1
```

Expected: HEAD is the latest `main` tip (currently `857319a Merge pull request #19 from hherb/gui-client-2`). All subsequent steps run from inside this worktree.

If the worktree or branch already exists, STOP and report BLOCKED — do not delete or reuse.

- [ ] **Step 2: Verify previous sub-plan state survives**

```bash
cd /Users/hherb/src/localmail/.claude/worktrees/gui-client-3/gui/src-tauri && cargo test 2>&1 | tail -5
```

Expected: `test result: ok. N passed`. The exact N depends on the suite that came with Sub-plan 2; just confirm zero failures.

```bash
cd /Users/hherb/src/localmail/.claude/worktrees/gui-client-3/gui && npm install && npm test 2>&1 | tail -10
```

Expected: vitest reports all tests passing. If either run fails, STOP — the worktree base is wrong.

- [ ] **Step 3: No commit yet** (nothing changed). Move to Task 1.

---

## Task 1: Rust command — list accounts

**Files:**
- Create: `gui/src-tauri/src/commands/accounts.rs`
- Modify: `gui/src-tauri/src/commands/mod.rs`
- Modify: `gui/src-tauri/src/lib.rs`

The Rust side is a thin wrapper around `http_get_json` with the same keyring + pinned-client pattern as `capabilities.rs`. The endpoint is `GET /v1/accounts` and returns `Vec<AccountSummary>`.

- [ ] **Step 1: Write the failing test**

`gui/src-tauri/src/commands/accounts.rs`:

```rust
//! Account + folder listing.
//!
//! Two HTTP calls, both authenticated:
//!   GET /v1/accounts                      → list_accounts
//!   GET /v1/accounts/{account_id}/folders → list_folders
//!
//! Both return JSON arrays decoded into typed structs the Svelte side knows.

use serde::{Deserialize, Serialize};

use crate::commands::auth::AuthError;
use crate::http::client::{build_pinned_client, http_get_json};
use crate::storage::keyring::{KeyringStore, Slot};

#[derive(Debug, Deserialize, Serialize)]
pub struct AccountCapabilities {
    pub can_sync: bool,
    pub is_archive_only: bool,
    pub is_shared: bool,
}

#[derive(Debug, Deserialize, Serialize)]
pub struct AccountSummary {
    pub id: String,
    pub name: String,
    pub address: Option<String>,
    pub last_sync_at: Option<String>,
    pub message_count: i64,
    pub capabilities: AccountCapabilities,
}

#[derive(Debug, Deserialize, Serialize)]
pub struct FolderSummary {
    pub id: String,
    pub name: String,
    pub full_path: String,
    pub flags: Option<String>,
    pub last_uid: Option<i64>,
    pub message_count: i64,
}

fn read_connection(store: &KeyringStore) -> Result<(String, String, String), AuthError> {
    let url = store.get(Slot::ServerUrl)
        .map_err(|e| AuthError::Keyring(e.to_string()))?
        .ok_or(AuthError::NotConnected)?;
    let pin = store.get(Slot::CertPin)
        .map_err(|e| AuthError::Keyring(e.to_string()))?
        .ok_or(AuthError::NotConnected)?;
    let token = store.get(Slot::BearerToken)
        .map_err(|e| AuthError::Keyring(e.to_string()))?
        .ok_or(AuthError::NotLoggedIn)?;
    Ok((url, pin, token))
}

pub async fn list_accounts(store: &KeyringStore) -> Result<Vec<AccountSummary>, AuthError> {
    let (url, pin, token) = read_connection(store)?;
    let client = build_pinned_client(&pin)?;
    let endpoint = format!("{url}v1/accounts");
    let accounts: Vec<AccountSummary> = http_get_json(&client, &endpoint, Some(&token)).await?;
    Ok(accounts)
}

pub async fn list_folders(store: &KeyringStore, account_id: &str) -> Result<Vec<FolderSummary>, AuthError> {
    let (url, pin, token) = read_connection(store)?;
    let client = build_pinned_client(&pin)?;
    let endpoint = format!("{url}v1/accounts/{account_id}/folders");
    let folders: Vec<FolderSummary> = http_get_json(&client, &endpoint, Some(&token)).await?;
    Ok(folders)
}

#[tauri::command]
pub async fn list_accounts_cmd() -> Result<Vec<AccountSummary>, AuthError> {
    let store = KeyringStore::new();
    list_accounts(&store).await
}

#[tauri::command]
pub async fn list_folders_cmd(account_id: String) -> Result<Vec<FolderSummary>, AuthError> {
    let store = KeyringStore::new();
    list_folders(&store, &account_id).await
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::storage::keyring::MemKeyring;

    fn fake_store() -> KeyringStore {
        KeyringStore::with_backend(MemKeyring::new())
    }

    #[tokio::test]
    async fn list_accounts_without_connection_returns_not_connected() {
        let store = fake_store();
        let err = list_accounts(&store).await.unwrap_err();
        assert!(matches!(err, AuthError::NotConnected));
    }

    #[tokio::test]
    async fn list_accounts_without_token_returns_not_logged_in() {
        let store = fake_store();
        store.put(Slot::ServerUrl, "https://localhost:8443/").unwrap();
        store.put(Slot::CertPin, "deadbeef").unwrap();
        let err = list_accounts(&store).await.unwrap_err();
        assert!(matches!(err, AuthError::NotLoggedIn));
    }

    #[tokio::test]
    async fn list_folders_without_token_returns_not_logged_in() {
        let store = fake_store();
        store.put(Slot::ServerUrl, "https://localhost:8443/").unwrap();
        store.put(Slot::CertPin, "deadbeef").unwrap();
        let err = list_folders(&store, "1").await.unwrap_err();
        assert!(matches!(err, AuthError::NotLoggedIn));
    }
}
```

- [ ] **Step 2: Update commands/mod.rs**

Open `gui/src-tauri/src/commands/mod.rs`. Add the new module declaration alongside the existing ones:

```rust
pub mod accounts;
pub mod auth;
pub mod capabilities;
pub mod changes;
pub mod connect;
pub mod messages;
```

(Tasks 2 and 3 will create `changes.rs` and `messages.rs`. If those don't exist yet when this commit lands, comment them out and uncomment per task. Simpler: do this mod.rs edit incrementally — only add `pub mod accounts;` here, add the others in Tasks 2 and 3.)

- [ ] **Step 3: Register the tauri commands**

Open `gui/src-tauri/src/lib.rs`. In `tauri::generate_handler![...]`, append:

```rust
crate::commands::accounts::list_accounts_cmd,
crate::commands::accounts::list_folders_cmd,
```

- [ ] **Step 4: Run the new tests**

```bash
cd gui/src-tauri && cargo test commands::accounts 2>&1 | tail -10
```

Expected: 3 passes. If `cargo check` fails first, fix imports.

- [ ] **Step 5: Full suite still green**

```bash
cd gui/src-tauri && cargo test 2>&1 | tail -5
```

Expected: all previous tests + 3 new tests pass.

- [ ] **Step 6: Commit**

```bash
git add gui/src-tauri/src/commands/accounts.rs \
        gui/src-tauri/src/commands/mod.rs \
        gui/src-tauri/src/lib.rs
git commit -m "feat(gui-client): Rust commands for /v1/accounts and /v1/accounts/{id}/folders"
```

---

## Task 2: Rust command — list recent messages (`/v1/changes`)

**Files:**
- Create: `gui/src-tauri/src/commands/changes.rs`
- Modify: `gui/src-tauri/src/commands/mod.rs`
- Modify: `gui/src-tauri/src/lib.rs`

`/v1/changes` (no `since` cursor) returns up to 200 most recent messages across all accounts. We expose it as `list_recent_messages` — a name closer to how the Svelte side will use it. The future `since`-cursor variant (for polling) is Sub-plan 5, not now.

- [ ] **Step 1: Write `changes.rs`**

`gui/src-tauri/src/commands/changes.rs`:

```rust
//! GET /v1/changes — recent messages across all accounts.
//!
//! Sub-plan 3 uses the no-cursor form to seed the message list with the
//! latest ~200 messages. A `since` cursor for incremental polling lands in
//! Sub-plan 5 (background change polling).

use serde::{Deserialize, Serialize};

use crate::commands::auth::AuthError;
use crate::http::client::{build_pinned_client, http_get_json};
use crate::storage::keyring::{KeyringStore, Slot};

#[derive(Debug, Deserialize, Serialize)]
pub struct MessageAddress {
    pub address: Option<String>,
    pub name: Option<String>,
}

#[derive(Debug, Deserialize, Serialize)]
pub struct MessageAccount {
    pub id: String,
    pub name: Option<String>,
}

#[derive(Debug, Deserialize, Serialize)]
pub struct MessageSummary {
    pub message_id: String,
    pub subject: Option<String>,
    pub from: MessageAddress,
    pub date: Option<String>,
    pub account: MessageAccount,
}

#[derive(Debug, Deserialize, Serialize)]
pub struct ChangesResponse {
    pub new_messages: Vec<MessageSummary>,
    pub next_cursor: String,
}

fn read_connection(store: &KeyringStore) -> Result<(String, String, String), AuthError> {
    let url = store.get(Slot::ServerUrl)
        .map_err(|e| AuthError::Keyring(e.to_string()))?
        .ok_or(AuthError::NotConnected)?;
    let pin = store.get(Slot::CertPin)
        .map_err(|e| AuthError::Keyring(e.to_string()))?
        .ok_or(AuthError::NotConnected)?;
    let token = store.get(Slot::BearerToken)
        .map_err(|e| AuthError::Keyring(e.to_string()))?
        .ok_or(AuthError::NotLoggedIn)?;
    Ok((url, pin, token))
}

pub async fn list_recent_messages(store: &KeyringStore) -> Result<ChangesResponse, AuthError> {
    let (url, pin, token) = read_connection(store)?;
    let client = build_pinned_client(&pin)?;
    let endpoint = format!("{url}v1/changes");
    let resp: ChangesResponse = http_get_json(&client, &endpoint, Some(&token)).await?;
    Ok(resp)
}

#[tauri::command]
pub async fn list_recent_messages_cmd() -> Result<ChangesResponse, AuthError> {
    let store = KeyringStore::new();
    list_recent_messages(&store).await
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::storage::keyring::MemKeyring;

    fn fake_store() -> KeyringStore {
        KeyringStore::with_backend(MemKeyring::new())
    }

    #[tokio::test]
    async fn list_recent_without_connection_returns_not_connected() {
        let store = fake_store();
        let err = list_recent_messages(&store).await.unwrap_err();
        assert!(matches!(err, AuthError::NotConnected));
    }

    #[tokio::test]
    async fn list_recent_without_token_returns_not_logged_in() {
        let store = fake_store();
        store.put(Slot::ServerUrl, "https://localhost:8443/").unwrap();
        store.put(Slot::CertPin, "deadbeef").unwrap();
        let err = list_recent_messages(&store).await.unwrap_err();
        assert!(matches!(err, AuthError::NotLoggedIn));
    }
}
```

- [ ] **Step 2: Wire into mod.rs and lib.rs**

`gui/src-tauri/src/commands/mod.rs` — add (if not already present from Task 1):

```rust
pub mod changes;
```

`gui/src-tauri/src/lib.rs` — append inside `tauri::generate_handler![...]`:

```rust
crate::commands::changes::list_recent_messages_cmd,
```

- [ ] **Step 3: Run tests**

```bash
cd gui/src-tauri && cargo test commands::changes 2>&1 | tail -10
```

Expected: 2 passes.

```bash
cd gui/src-tauri && cargo test 2>&1 | tail -5
```

Expected: full suite green.

- [ ] **Step 4: Commit**

```bash
git add gui/src-tauri/src/commands/changes.rs \
        gui/src-tauri/src/commands/mod.rs \
        gui/src-tauri/src/lib.rs
git commit -m "feat(gui-client): Rust command for /v1/changes (recent messages)"
```

---

## Task 3: Rust command — get message detail

**Files:**
- Create: `gui/src-tauri/src/commands/messages.rs`
- Modify: `gui/src-tauri/src/commands/mod.rs`
- Modify: `gui/src-tauri/src/lib.rs`

`GET /v1/messages/{id}` returns the full message: subject, addresses, body_text, sanitized body_html, attachments, account/folder breadcrumb. Sub-plan 3 only reads `body_text` and headers from this response; `body_html` and `attachments` deserialize and pass through but the UI doesn't render them yet (Sub-plan 4).

- [ ] **Step 1: Write `messages.rs`**

`gui/src-tauri/src/commands/messages.rs`:

```rust
//! GET /v1/messages/{id} — full message detail.
//!
//! Sub-plan 3 consumes `body_text`, key headers, `from`/`to`, `date`, and the
//! account/folder breadcrumb. `body_html` and `attachments` deserialize for
//! future use (Sub-plan 4) but are not surfaced in the current UI.

use serde::{Deserialize, Serialize};

use crate::commands::auth::AuthError;
use crate::commands::changes::MessageAddress;
use crate::http::client::{build_pinned_client, http_get_json};
use crate::storage::keyring::{KeyringStore, Slot};

#[derive(Debug, Deserialize, Serialize)]
pub struct MessageFolder {
    pub id: String,
    pub name: String,
}

#[derive(Debug, Deserialize, Serialize)]
pub struct MessageDetailAccount {
    pub id: String,
    pub name: Option<String>,
    pub address: Option<String>,
}

#[derive(Debug, Deserialize, Serialize)]
pub struct MessageAttachment {
    pub filename: Option<String>,
    pub sha256: Option<String>,
}

#[derive(Debug, Deserialize, Serialize)]
pub struct MessageDetail {
    pub id: String,
    pub subject: Option<String>,
    pub from: MessageAddress,
    pub to: Vec<MessageAddress>,
    pub cc: Vec<MessageAddress>,
    pub bcc: Vec<MessageAddress>,
    pub date: Option<String>,
    pub body_text: Option<String>,
    pub body_html: Option<String>,
    pub attachments: Vec<MessageAttachment>,
    pub account: MessageDetailAccount,
    pub folders: Vec<MessageFolder>,
}

fn read_connection(store: &KeyringStore) -> Result<(String, String, String), AuthError> {
    let url = store.get(Slot::ServerUrl)
        .map_err(|e| AuthError::Keyring(e.to_string()))?
        .ok_or(AuthError::NotConnected)?;
    let pin = store.get(Slot::CertPin)
        .map_err(|e| AuthError::Keyring(e.to_string()))?
        .ok_or(AuthError::NotConnected)?;
    let token = store.get(Slot::BearerToken)
        .map_err(|e| AuthError::Keyring(e.to_string()))?
        .ok_or(AuthError::NotLoggedIn)?;
    Ok((url, pin, token))
}

pub async fn get_message(store: &KeyringStore, message_id: &str) -> Result<MessageDetail, AuthError> {
    let (url, pin, token) = read_connection(store)?;
    let client = build_pinned_client(&pin)?;
    let endpoint = format!("{url}v1/messages/{message_id}");
    let detail: MessageDetail = http_get_json(&client, &endpoint, Some(&token)).await?;
    Ok(detail)
}

#[tauri::command]
pub async fn get_message_cmd(message_id: String) -> Result<MessageDetail, AuthError> {
    let store = KeyringStore::new();
    get_message(&store, &message_id).await
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::storage::keyring::MemKeyring;

    fn fake_store() -> KeyringStore {
        KeyringStore::with_backend(MemKeyring::new())
    }

    #[tokio::test]
    async fn get_message_without_connection_returns_not_connected() {
        let store = fake_store();
        let err = get_message(&store, "1").await.unwrap_err();
        assert!(matches!(err, AuthError::NotConnected));
    }

    #[tokio::test]
    async fn get_message_without_token_returns_not_logged_in() {
        let store = fake_store();
        store.put(Slot::ServerUrl, "https://localhost:8443/").unwrap();
        store.put(Slot::CertPin, "deadbeef").unwrap();
        let err = get_message(&store, "1").await.unwrap_err();
        assert!(matches!(err, AuthError::NotLoggedIn));
    }
}
```

- [ ] **Step 2: Wire mod.rs + lib.rs**

`gui/src-tauri/src/commands/mod.rs`:

```rust
pub mod messages;
```

`gui/src-tauri/src/lib.rs` — inside `tauri::generate_handler![...]`:

```rust
crate::commands::messages::get_message_cmd,
```

- [ ] **Step 3: Run tests**

```bash
cd gui/src-tauri && cargo test commands::messages 2>&1 | tail -10
```

Expected: 2 passes.

```bash
cd gui/src-tauri && cargo test 2>&1 | tail -5
```

Expected: full suite green. Also do a `cargo check` to surface unused-import warnings.

- [ ] **Step 4: Commit**

```bash
git add gui/src-tauri/src/commands/messages.rs \
        gui/src-tauri/src/commands/mod.rs \
        gui/src-tauri/src/lib.rs
git commit -m "feat(gui-client): Rust command for /v1/messages/{id} (full detail)"
```

---

## Task 4: TypeScript types + invoke wrappers

**Files:**
- Create: `gui/src/lib/api/types.ts`
- Modify: `gui/src/lib/tauri.ts`

Shared types are factored into `lib/api/types.ts` so multiple stores and components import the same shape. Invoke wrappers stay in `tauri.ts` next to the existing ones for consistency.

- [ ] **Step 1: Create `lib/api/types.ts`**

```typescript
/**
 * Shared API response types. Mirrors the Rust structs in
 * src-tauri/src/commands/{accounts,changes,messages}.rs, which themselves
 * mirror the JSON returned by the server.
 *
 * Keep this file dependency-free — pure type declarations. Stores and
 * components import from here; the invoke wrappers live in tauri.ts.
 */

export interface AccountCapabilities {
  can_sync: boolean;
  is_archive_only: boolean;
  is_shared: boolean;
}

export interface AccountSummary {
  id: string;
  name: string;
  address: string | null;
  last_sync_at: string | null;
  message_count: number;
  capabilities: AccountCapabilities;
}

export interface FolderSummary {
  id: string;
  name: string;
  full_path: string;
  flags: string | null;
  last_uid: number | null;
  message_count: number;
}

export interface MessageAddress {
  address: string | null;
  name: string | null;
}

export interface MessageAccount {
  id: string;
  name: string | null;
}

export interface MessageSummary {
  message_id: string;
  subject: string | null;
  from: MessageAddress;
  date: string | null;
  account: MessageAccount;
}

export interface ChangesResponse {
  new_messages: MessageSummary[];
  next_cursor: string;
}

export interface MessageFolder {
  id: string;
  name: string;
}

export interface MessageDetailAccount {
  id: string;
  name: string | null;
  address: string | null;
}

export interface MessageAttachment {
  filename: string | null;
  sha256: string | null;
}

export interface MessageDetail {
  id: string;
  subject: string | null;
  from: MessageAddress;
  to: MessageAddress[];
  cc: MessageAddress[];
  bcc: MessageAddress[];
  date: string | null;
  body_text: string | null;
  body_html: string | null;
  attachments: MessageAttachment[];
  account: MessageDetailAccount;
  folders: MessageFolder[];
}

/**
 * What the user has selected in the left rail. Drives which subset of the
 * loaded message list the middle pane shows.
 *
 * - `all`     — "All Mail" pinned entry. Shows everything in the loaded set.
 * - `account` — narrow to one account (filters loaded messages by account.id).
 * - `folder`  — narrow to one folder of one account. Folder filtering is
 *               client-side until Sub-plan 4 wires server-side folder_ids.
 */
export type Selection =
  | { kind: "all" }
  | { kind: "account"; accountId: string }
  | { kind: "folder"; accountId: string; folderId: string };
```

- [ ] **Step 2: Extend `lib/tauri.ts`**

At the bottom of `gui/src/lib/tauri.ts`, append the new wrappers. Re-export types from `./api/types` so consumers can import everything from `../lib/tauri`:

```typescript
import type {
  AccountSummary,
  ChangesResponse,
  FolderSummary,
  MessageDetail,
} from "./api/types";

export type {
  AccountCapabilities,
  AccountSummary,
  ChangesResponse,
  FolderSummary,
  MessageAccount,
  MessageAddress,
  MessageAttachment,
  MessageDetail,
  MessageDetailAccount,
  MessageFolder,
  MessageSummary,
  Selection,
} from "./api/types";

export async function listAccounts(): Promise<AccountSummary[]> {
  return invoke<AccountSummary[]>("list_accounts_cmd");
}

export async function listFolders(accountId: string): Promise<FolderSummary[]> {
  return invoke<FolderSummary[]>("list_folders_cmd", { accountId });
}

export async function listRecentMessages(): Promise<ChangesResponse> {
  return invoke<ChangesResponse>("list_recent_messages_cmd");
}

export async function getMessage(messageId: string): Promise<MessageDetail> {
  return invoke<MessageDetail>("get_message_cmd", { messageId });
}
```

**Note on snake_case:** Tauri's invoke arguments must match the Rust parameter names. `list_folders_cmd(account_id: String)` Tauri-side becomes `{ accountId }` JS-side because Tauri 2 auto-converts snake_case ↔ camelCase. Verified by Sub-plan 2's `confirmTrust(url, certSha256)` → `confirm_trust_cmd(url: String, cert_sha256: String)`.

- [ ] **Step 3: Type-check**

```bash
cd gui && npm run check 2>&1 | tail -20
```

Expected: zero errors. The added imports/exports compile cleanly.

- [ ] **Step 4: Commit**

```bash
git add gui/src/lib/api/types.ts gui/src/lib/tauri.ts
git commit -m "feat(gui-client): TS types + invoke wrappers for accounts/folders/messages"
```

---

## Task 5: Pure format helpers + tests

**Files:**
- Create: `gui/src/lib/format.ts`
- Create: `gui/src/lib/format.test.ts`

Pure functions used by the list and tree components. Tested in isolation so component tests don't need to assert on date strings.

- [ ] **Step 1: Write `format.test.ts` first (TDD)**

```typescript
import { describe, expect, it } from "vitest";
import {
  addressLabel,
  formatRelativeDate,
  selectionMatches,
  truncate,
} from "./format";
import type { MessageAddress, MessageSummary, Selection } from "./tauri";

describe("addressLabel", () => {
  it("prefers name over address", () => {
    const a: MessageAddress = { name: "Anna H.", address: "anna@example.com" };
    expect(addressLabel(a)).toBe("Anna H.");
  });

  it("falls back to address when name is null", () => {
    const a: MessageAddress = { name: null, address: "anna@example.com" };
    expect(addressLabel(a)).toBe("anna@example.com");
  });

  it("returns placeholder when both are null", () => {
    const a: MessageAddress = { name: null, address: null };
    expect(addressLabel(a)).toBe("(unknown sender)");
  });
});

describe("truncate", () => {
  it("returns input unchanged if shorter than limit", () => {
    expect(truncate("hello", 10)).toBe("hello");
  });

  it("truncates and appends ellipsis when longer", () => {
    expect(truncate("hello world", 5)).toBe("hello…");
  });

  it("handles null/undefined as empty string", () => {
    expect(truncate(null, 10)).toBe("");
    expect(truncate(undefined, 10)).toBe("");
  });
});

describe("formatRelativeDate", () => {
  it("returns empty string for null", () => {
    expect(formatRelativeDate(null, new Date("2026-05-17T12:00:00Z"))).toBe("");
  });

  it("returns time only for same day", () => {
    const out = formatRelativeDate(
      "2026-05-17T09:30:00Z",
      new Date("2026-05-17T15:00:00Z"),
    );
    // Time format is locale-dependent; just assert it contains a digit and a colon.
    expect(out).toMatch(/\d+:\d+/);
  });

  it("returns short date for earlier in the same year", () => {
    const out = formatRelativeDate(
      "2026-03-03T08:14:00Z",
      new Date("2026-05-17T12:00:00Z"),
    );
    // Format like "Mar 3"; assert month abbreviation present.
    expect(out.toLowerCase()).toMatch(/[a-z]{3}\s+\d+/);
  });

  it("returns full date for older messages", () => {
    const out = formatRelativeDate(
      "2024-12-25T08:14:00Z",
      new Date("2026-05-17T12:00:00Z"),
    );
    expect(out).toMatch(/2024/);
  });
});

describe("selectionMatches", () => {
  const mkMsg = (accountId: string): MessageSummary => ({
    message_id: "1",
    subject: "x",
    from: { name: null, address: null },
    date: null,
    account: { id: accountId, name: null },
  });

  it('"all" matches every message', () => {
    const sel: Selection = { kind: "all" };
    expect(selectionMatches(sel, mkMsg("1"))).toBe(true);
    expect(selectionMatches(sel, mkMsg("2"))).toBe(true);
  });

  it('"account" matches messages of that account', () => {
    const sel: Selection = { kind: "account", accountId: "1" };
    expect(selectionMatches(sel, mkMsg("1"))).toBe(true);
    expect(selectionMatches(sel, mkMsg("2"))).toBe(false);
  });

  it('"folder" filters by account (folder narrowing is server-side, deferred)', () => {
    const sel: Selection = { kind: "folder", accountId: "1", folderId: "5" };
    expect(selectionMatches(sel, mkMsg("1"))).toBe(true);
    expect(selectionMatches(sel, mkMsg("2"))).toBe(false);
  });
});
```

- [ ] **Step 2: Run to confirm failures**

```bash
cd gui && npm test -- format.test 2>&1 | tail -10
```

Expected: fails because `format.ts` doesn't exist yet.

- [ ] **Step 3: Implement `format.ts`**

```typescript
/**
 * Pure helpers shared by the list / tree / reading-pane components.
 *
 * No DOM, no Tauri invokes, no $state. Test as pure functions only.
 */

import type { MessageAddress, MessageSummary, Selection } from "./tauri";

const FALLBACK_SENDER = "(unknown sender)";
const ELLIPSIS = "…";

/**
 * Display label for a sender / recipient: `name` if present, else `address`,
 * else a placeholder. The MessageAddress shape allows both fields nullable —
 * real mail occasionally has neither.
 */
export function addressLabel(addr: MessageAddress): string {
  if (addr.name && addr.name.trim()) return addr.name.trim();
  if (addr.address && addr.address.trim()) return addr.address.trim();
  return FALLBACK_SENDER;
}

/**
 * Truncate `s` to `maxChars` characters, appending an ellipsis if the input
 * was longer. `null`/`undefined` → empty string. The trimmed character count
 * does NOT include the appended ellipsis.
 */
export function truncate(s: string | null | undefined, maxChars: number): string {
  if (s == null) return "";
  if (s.length <= maxChars) return s;
  return s.slice(0, maxChars) + ELLIPSIS;
}

/**
 * Format a message date relative to `now`:
 *   - same calendar day  → time only (e.g. "9:30 AM" / "09:30")
 *   - same calendar year → short date (e.g. "Mar 3")
 *   - older              → year-qualified date (e.g. "Dec 25, 2024")
 * Returns "" for null / unparseable input.
 *
 * Format is locale-driven via Intl; we don't pin a locale because the user's
 * OS locale is the right default for a desktop client.
 */
export function formatRelativeDate(iso: string | null, now: Date = new Date()): string {
  if (!iso) return "";
  const dt = new Date(iso);
  if (Number.isNaN(dt.getTime())) return "";
  if (sameDay(dt, now)) {
    return dt.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
  }
  if (dt.getFullYear() === now.getFullYear()) {
    return dt.toLocaleDateString(undefined, { month: "short", day: "numeric" });
  }
  return dt.toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
    year: "numeric",
  });
}

function sameDay(a: Date, b: Date): boolean {
  return (
    a.getFullYear() === b.getFullYear() &&
    a.getMonth() === b.getMonth() &&
    a.getDate() === b.getDate()
  );
}

/**
 * True if `msg` should be visible under the current selection.
 *
 * `folder` selection narrows by account only (folder narrowing is server-side
 * and deferred to Sub-plan 4 — the loaded `/v1/changes` response does not
 * tell us which folder each message belongs to). Treating "folder"
 * functionally like "account" keeps the UI honest while we wait.
 */
export function selectionMatches(sel: Selection, msg: MessageSummary): boolean {
  switch (sel.kind) {
    case "all":
      return true;
    case "account":
      return msg.account.id === sel.accountId;
    case "folder":
      return msg.account.id === sel.accountId;
  }
}
```

- [ ] **Step 4: Tests pass**

```bash
cd gui && npm test -- format.test 2>&1 | tail -10
```

Expected: all describe blocks pass.

- [ ] **Step 5: Commit**

```bash
git add gui/src/lib/format.ts gui/src/lib/format.test.ts
git commit -m "feat(gui-client): pure format helpers (addressLabel, truncate, formatRelativeDate, selectionMatches)"
```

---

## Task 6: `mail.svelte.ts` store + tests

**Files:**
- Create: `gui/src/lib/stores/mail.svelte.ts`
- Create: `gui/src/lib/stores/mail.test.ts`

The mail store mirrors `auth.svelte.ts`: rune-backed singleton, exposes a `snapshot` getter for reactive reads, and actions that mutate `#state`. State is the loaded account list, the loaded folder map (per account, lazy), the loaded message list (from `/v1/changes`), the current `Selection`, and the currently-opened `MessageDetail`.

- [ ] **Step 1: Write `mail.test.ts` first (TDD)**

```typescript
import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  listAccounts: vi.fn(),
  listFolders: vi.fn(),
  listRecentMessages: vi.fn(),
  getMessage: vi.fn(),
}));

vi.mock("../tauri", () => mocks);

import { mail } from "./mail.svelte";
import type { AccountSummary, FolderSummary, MessageDetail, MessageSummary } from "../tauri";

const acct = (id: string, name: string): AccountSummary => ({
  id,
  name,
  address: `${name}@example.com`,
  last_sync_at: null,
  message_count: 0,
  capabilities: { can_sync: true, is_archive_only: false, is_shared: false },
});

const folder = (id: string, name: string): FolderSummary => ({
  id,
  name,
  full_path: name,
  flags: null,
  last_uid: null,
  message_count: 0,
});

const msg = (id: string, accountId: string, subject = "hi"): MessageSummary => ({
  message_id: id,
  subject,
  from: { name: null, address: "x@example.com" },
  date: null,
  account: { id: accountId, name: null },
});

const detail = (id: string, body: string): MessageDetail => ({
  id,
  subject: "s",
  from: { name: null, address: null },
  to: [],
  cc: [],
  bcc: [],
  date: null,
  body_text: body,
  body_html: null,
  attachments: [],
  account: { id: "1", name: null, address: null },
  folders: [],
});

beforeEach(() => {
  mail.reset();
  vi.clearAllMocks();
});

describe("mail store", () => {
  it("starts empty with selection=all and no loaded data", () => {
    expect(mail.snapshot.accounts).toEqual([]);
    expect(mail.snapshot.messages).toEqual([]);
    expect(mail.snapshot.selection).toEqual({ kind: "all" });
    expect(mail.snapshot.selectedMessage).toBeNull();
    expect(mail.snapshot.loadingMessages).toBe(false);
  });

  it("loads accounts via listAccounts()", async () => {
    mocks.listAccounts.mockResolvedValue([acct("1", "alice"), acct("2", "bob")]);
    await mail.loadAccounts();
    expect(mail.snapshot.accounts).toHaveLength(2);
    expect(mail.snapshot.accounts[0].name).toBe("alice");
  });

  it("loads folders into per-account map", async () => {
    mocks.listFolders.mockResolvedValue([folder("10", "INBOX"), folder("11", "Sent")]);
    await mail.loadFoldersFor("1");
    expect(mail.snapshot.folders.get("1")).toHaveLength(2);
  });

  it("loadFoldersFor is idempotent — does not re-fetch if already loaded", async () => {
    mocks.listFolders.mockResolvedValue([folder("10", "INBOX")]);
    await mail.loadFoldersFor("1");
    await mail.loadFoldersFor("1");
    expect(mocks.listFolders).toHaveBeenCalledTimes(1);
  });

  it("loads recent messages and exposes them via snapshot.messages", async () => {
    mocks.listRecentMessages.mockResolvedValue({
      new_messages: [msg("1", "1"), msg("2", "2")],
      next_cursor: "2",
    });
    await mail.loadRecentMessages();
    expect(mail.snapshot.messages).toHaveLength(2);
  });

  it("setSelection updates current selection", () => {
    mail.setSelection({ kind: "account", accountId: "1" });
    expect(mail.snapshot.selection).toEqual({ kind: "account", accountId: "1" });
  });

  it("openMessage loads detail and stores it", async () => {
    mocks.getMessage.mockResolvedValue(detail("42", "plain body"));
    await mail.openMessage("42");
    expect(mail.snapshot.selectedMessage?.id).toBe("42");
    expect(mail.snapshot.selectedMessage?.body_text).toBe("plain body");
  });

  it("openMessage with same id is a no-op (no extra fetch)", async () => {
    mocks.getMessage.mockResolvedValue(detail("42", "x"));
    await mail.openMessage("42");
    await mail.openMessage("42");
    expect(mocks.getMessage).toHaveBeenCalledTimes(1);
  });

  it("loadingMessages is true during fetch, false after", async () => {
    let resolveFn!: (v: { new_messages: MessageSummary[]; next_cursor: string }) => void;
    mocks.listRecentMessages.mockReturnValue(
      new Promise((r) => {
        resolveFn = r;
      }),
    );
    const pending = mail.loadRecentMessages();
    expect(mail.snapshot.loadingMessages).toBe(true);
    resolveFn({ new_messages: [], next_cursor: "0" });
    await pending;
    expect(mail.snapshot.loadingMessages).toBe(false);
  });

  it("captures errorMessage on load failure", async () => {
    mocks.listRecentMessages.mockRejectedValue({ kind: "Auth", detail: "NotLoggedIn" });
    await mail.loadRecentMessages();
    expect(mail.snapshot.errorMessage).toContain("Auth");
  });

  it("reset clears everything", async () => {
    mocks.listAccounts.mockResolvedValue([acct("1", "alice")]);
    await mail.loadAccounts();
    mail.reset();
    expect(mail.snapshot.accounts).toEqual([]);
    expect(mail.snapshot.selection).toEqual({ kind: "all" });
  });
});
```

- [ ] **Step 2: Run to confirm failures**

```bash
cd gui && npm test -- mail.test 2>&1 | tail -15
```

Expected: every test fails because `mail.svelte.ts` doesn't exist.

- [ ] **Step 3: Implement `mail.svelte.ts`**

```typescript
/**
 * Single source of truth for the GUI's mail browsing state. Rune-backed
 * singleton; mirrors the pattern of `auth.svelte.ts`.
 *
 * State:
 *   accounts          AccountSummary[]                  loaded once after login
 *   folders           Map<accountId, FolderSummary[]>   loaded lazily on expansion
 *   messages          MessageSummary[]                  most-recent 200 (via /v1/changes)
 *   selection         Selection                         what the tree currently selects
 *   selectedMessage   MessageDetail | null              detail for the currently-open message
 *   loadingMessages   boolean                           true during list fetch
 *   loadingDetail     boolean                           true during detail fetch
 *   errorMessage      string | null                     last error surfaced from a load
 *
 * Actions:
 *   loadAccounts()                                       fetch /v1/accounts
 *   loadFoldersFor(accountId)                            fetch folders, idempotent
 *   loadRecentMessages()                                 fetch /v1/changes
 *   setSelection(sel)                                    update the left-rail selection
 *   openMessage(id)                                      fetch + store detail; no-op if same id
 *   reset()                                              clear all state (used on logout)
 */
import {
  getMessage,
  listAccounts,
  listFolders,
  listRecentMessages,
  type AccountSummary,
  type FolderSummary,
  type MessageDetail,
  type MessageSummary,
  type Selection,
} from "../tauri";

export interface MailState {
  accounts: AccountSummary[];
  folders: Map<string, FolderSummary[]>;
  messages: MessageSummary[];
  selection: Selection;
  selectedMessage: MessageDetail | null;
  loadingMessages: boolean;
  loadingDetail: boolean;
  errorMessage: string | null;
}

function initialState(): MailState {
  return {
    accounts: [],
    folders: new Map(),
    messages: [],
    selection: { kind: "all" },
    selectedMessage: null,
    loadingMessages: false,
    loadingDetail: false,
    errorMessage: null,
  };
}

class MailStore {
  #state: MailState = $state(initialState());

  get snapshot(): MailState {
    return this.#state;
  }

  reset(): void {
    this.#state = initialState();
  }

  async loadAccounts(): Promise<void> {
    this.#state.errorMessage = null;
    try {
      const list = await listAccounts();
      this.#state.accounts = list;
    } catch (err: unknown) {
      this.#state.errorMessage = formatError(err);
    }
  }

  async loadFoldersFor(accountId: string): Promise<void> {
    if (this.#state.folders.has(accountId)) return;
    try {
      const list = await listFolders(accountId);
      const next = new Map(this.#state.folders);
      next.set(accountId, list);
      this.#state.folders = next;
    } catch (err: unknown) {
      this.#state.errorMessage = formatError(err);
    }
  }

  async loadRecentMessages(): Promise<void> {
    this.#state.loadingMessages = true;
    this.#state.errorMessage = null;
    try {
      const resp = await listRecentMessages();
      this.#state.messages = resp.new_messages;
    } catch (err: unknown) {
      this.#state.errorMessage = formatError(err);
    } finally {
      this.#state.loadingMessages = false;
    }
  }

  setSelection(sel: Selection): void {
    this.#state.selection = sel;
  }

  async openMessage(messageId: string): Promise<void> {
    if (this.#state.selectedMessage?.id === messageId) return;
    this.#state.loadingDetail = true;
    this.#state.errorMessage = null;
    try {
      const detail = await getMessage(messageId);
      this.#state.selectedMessage = detail;
    } catch (err: unknown) {
      this.#state.errorMessage = formatError(err);
    } finally {
      this.#state.loadingDetail = false;
    }
  }
}

function formatError(err: unknown): string {
  if (err && typeof err === "object") {
    const o = err as { kind?: string; detail?: unknown };
    if (o.kind && o.detail !== undefined) {
      const detailStr =
        typeof o.detail === "object" && o.detail !== null
          ? formatError(o.detail)
          : String(o.detail);
      return `${o.kind}: ${detailStr}`;
    }
    if (o.kind) return String(o.kind);
  }
  return String(err);
}

export const mail = new MailStore();
```

- [ ] **Step 4: Tests pass**

```bash
cd gui && npm test -- mail.test 2>&1 | tail -15
```

Expected: all describe blocks pass.

- [ ] **Step 5: Commit**

```bash
git add gui/src/lib/stores/mail.svelte.ts gui/src/lib/stores/mail.test.ts
git commit -m "feat(gui-client): mail.svelte.ts singleton store + unit tests"
```

---

## Task 7: AccountTree component + tests

**Files:**
- Create: `gui/src/components/AccountTree.svelte`
- Create: `gui/src/components/AccountTree.test.ts`

Left rail. Renders "All Mail" at the top, then each account; clicking an account toggles its folder list and selects the account; clicking a folder selects that folder. Disclosure state lives in a local `$state` set; selection lives in the `mail` store.

- [ ] **Step 1: Write `AccountTree.test.ts` first (TDD)**

```typescript
import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, fireEvent } from "@testing-library/svelte";

const mocks = vi.hoisted(() => ({
  listAccounts: vi.fn(),
  listFolders: vi.fn(),
  listRecentMessages: vi.fn(),
  getMessage: vi.fn(),
}));

vi.mock("../lib/tauri", () => mocks);

import AccountTree from "./AccountTree.svelte";
import { mail } from "../lib/stores/mail.svelte";

beforeEach(() => {
  mail.reset();
  vi.clearAllMocks();
});

describe("AccountTree", () => {
  it('renders "All Mail" pinned entry', () => {
    const { getByText } = render(AccountTree);
    expect(getByText(/all mail/i)).toBeTruthy();
  });

  it("renders account names from the store", async () => {
    mocks.listAccounts.mockResolvedValue([
      {
        id: "1",
        name: "personal",
        address: null,
        last_sync_at: null,
        message_count: 0,
        capabilities: { can_sync: true, is_archive_only: false, is_shared: false },
      },
    ]);
    await mail.loadAccounts();
    const { getByText } = render(AccountTree);
    expect(getByText("personal")).toBeTruthy();
  });

  it("clicking an account toggles folder list and calls loadFoldersFor", async () => {
    mocks.listAccounts.mockResolvedValue([
      {
        id: "1",
        name: "personal",
        address: null,
        last_sync_at: null,
        message_count: 0,
        capabilities: { can_sync: true, is_archive_only: false, is_shared: false },
      },
    ]);
    mocks.listFolders.mockResolvedValue([
      {
        id: "10",
        name: "INBOX",
        full_path: "INBOX",
        flags: null,
        last_uid: null,
        message_count: 0,
      },
    ]);
    await mail.loadAccounts();
    const { getByText, findByText } = render(AccountTree);
    await fireEvent.click(getByText("personal"));
    expect(mocks.listFolders).toHaveBeenCalledWith("1");
    expect(await findByText("INBOX")).toBeTruthy();
  });

  it("clicking a folder sets selection.kind = folder", async () => {
    mocks.listAccounts.mockResolvedValue([
      {
        id: "1",
        name: "personal",
        address: null,
        last_sync_at: null,
        message_count: 0,
        capabilities: { can_sync: true, is_archive_only: false, is_shared: false },
      },
    ]);
    mocks.listFolders.mockResolvedValue([
      {
        id: "10",
        name: "INBOX",
        full_path: "INBOX",
        flags: null,
        last_uid: null,
        message_count: 0,
      },
    ]);
    await mail.loadAccounts();
    const { getByText, findByText } = render(AccountTree);
    await fireEvent.click(getByText("personal"));
    await fireEvent.click(await findByText("INBOX"));
    expect(mail.snapshot.selection).toEqual({
      kind: "folder",
      accountId: "1",
      folderId: "10",
    });
  });
});
```

If `@testing-library/svelte` isn't already installed, add it as a dev dep first:

```bash
cd gui && npm install --save-dev @testing-library/svelte @testing-library/jest-dom jsdom
```

And ensure `vite.config.ts` has `test.environment = "jsdom"` — Sub-plan 2 likely already set this. Verify:

```bash
grep -n "environment" gui/vite.config.ts
```

If absent, append to the `defineConfig({ test: {...} })` block: `environment: "jsdom"`. (If the test config doesn't exist yet, add a minimal `test: { environment: "jsdom", globals: true }`. Run tests after.)

- [ ] **Step 2: Run to confirm failures**

```bash
cd gui && npm test -- AccountTree 2>&1 | tail -10
```

Expected: fails (component file does not exist).

- [ ] **Step 3: Implement `AccountTree.svelte`**

```svelte
<script lang="ts">
  /**
   * Left rail. Renders "All Mail" + accounts (clickable, expandable to folders).
   *
   * Disclosure state (which accounts are expanded) lives in a local $state
   * Set; selection lives in the `mail` store so changes drive the message list.
   */
  import { mail } from "../lib/stores/mail.svelte";

  let expanded: Set<string> = $state(new Set());

  function selectAll(): void {
    mail.setSelection({ kind: "all" });
  }

  async function selectAccount(accountId: string): Promise<void> {
    mail.setSelection({ kind: "account", accountId });
    if (expanded.has(accountId)) {
      const next = new Set(expanded);
      next.delete(accountId);
      expanded = next;
      return;
    }
    const next = new Set(expanded);
    next.add(accountId);
    expanded = next;
    await mail.loadFoldersFor(accountId);
  }

  function selectFolder(accountId: string, folderId: string): void {
    mail.setSelection({ kind: "folder", accountId, folderId });
  }

  function isSelected(accountId: string | null, folderId: string | null): boolean {
    const sel = mail.snapshot.selection;
    if (accountId === null && folderId === null) return sel.kind === "all";
    if (folderId === null) {
      return sel.kind === "account" && sel.accountId === accountId;
    }
    return (
      sel.kind === "folder" &&
      sel.accountId === accountId &&
      sel.folderId === folderId
    );
  }
</script>

<aside class="tree">
  <ul>
    <li>
      <button
        type="button"
        class="row root"
        class:active={isSelected(null, null)}
        onclick={selectAll}
      >
        📥 All Mail
      </button>
    </li>
    {#each mail.snapshot.accounts as account (account.id)}
      <li>
        <button
          type="button"
          class="row account"
          class:active={isSelected(account.id, null)}
          onclick={() => selectAccount(account.id)}
        >
          {expanded.has(account.id) ? "▾" : "▸"}
          {account.capabilities.is_archive_only ? "📦" : "✉️"}
          {account.name}
        </button>
        {#if expanded.has(account.id)}
          {@const folders = mail.snapshot.folders.get(account.id) ?? []}
          <ul class="folders">
            {#each folders as folder (folder.id)}
              <li>
                <button
                  type="button"
                  class="row folder"
                  class:active={isSelected(account.id, folder.id)}
                  onclick={() => selectFolder(account.id, folder.id)}
                >
                  {folder.name}
                </button>
              </li>
            {/each}
          </ul>
        {/if}
      </li>
    {/each}
  </ul>
</aside>

<style>
  .tree {
    height: 100%;
    overflow-y: auto;
    background: #fafafa;
    border-right: 1px solid #e5e5e5;
    padding: 8px 0;
    font-size: 13px;
  }
  ul {
    list-style: none;
    padding: 0;
    margin: 0;
  }
  .row {
    display: block;
    width: 100%;
    text-align: left;
    padding: 6px 12px;
    background: none;
    border: none;
    color: #222;
    font: inherit;
    cursor: pointer;
  }
  .row:hover {
    background: #eef0f3;
  }
  .row.active {
    background: #d8e6ff;
    color: #1a4fc7;
    font-weight: 600;
  }
  .row.root {
    font-weight: 600;
  }
  .folders {
    padding-left: 14px;
  }
  .folder {
    padding-left: 24px;
    font-size: 12px;
  }
</style>
```

- [ ] **Step 4: Tests pass**

```bash
cd gui && npm test -- AccountTree 2>&1 | tail -10
```

Expected: 4 passes.

- [ ] **Step 5: Commit**

```bash
git add gui/src/components/AccountTree.svelte \
        gui/src/components/AccountTree.test.ts \
        gui/package.json gui/package-lock.json gui/vite.config.ts
git commit -m "feat(gui-client): AccountTree component + tests"
```

(Include the package files and vite config only if they actually changed in this task.)

---

## Task 8: MessageList + MessageListRow components + tests

**Files:**
- Create: `gui/src/components/MessageList.svelte`
- Create: `gui/src/components/MessageListRow.svelte`
- Create: `gui/src/components/MessageList.test.ts`

`MessageList` is the middle pane. It reads `mail.snapshot.messages` + `mail.snapshot.selection`, filters via `selectionMatches`, and renders one `MessageListRow` per visible message. Clicking a row calls `mail.openMessage(id)`.

`MessageListRow` is split out so each file stays focused.

- [ ] **Step 1: Write `MessageList.test.ts`**

```typescript
import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, fireEvent } from "@testing-library/svelte";

const mocks = vi.hoisted(() => ({
  listAccounts: vi.fn(),
  listFolders: vi.fn(),
  listRecentMessages: vi.fn(),
  getMessage: vi.fn(),
}));

vi.mock("../lib/tauri", () => mocks);

import MessageList from "./MessageList.svelte";
import { mail } from "../lib/stores/mail.svelte";

beforeEach(() => {
  mail.reset();
  vi.clearAllMocks();
});

describe("MessageList", () => {
  it("shows an empty hint when no messages loaded", () => {
    const { getByText } = render(MessageList);
    expect(getByText(/no messages/i)).toBeTruthy();
  });

  it("renders a row per loaded message under selection=all", async () => {
    mocks.listRecentMessages.mockResolvedValue({
      new_messages: [
        {
          message_id: "1",
          subject: "hi anna",
          from: { name: "Anna", address: "anna@x" },
          date: null,
          account: { id: "1", name: "personal" },
        },
        {
          message_id: "2",
          subject: "second",
          from: { name: null, address: "bob@x" },
          date: null,
          account: { id: "2", name: "work" },
        },
      ],
      next_cursor: "2",
    });
    await mail.loadRecentMessages();
    const { getByText } = render(MessageList);
    expect(getByText("hi anna")).toBeTruthy();
    expect(getByText("second")).toBeTruthy();
  });

  it("narrows to one account when selection=account", async () => {
    mocks.listRecentMessages.mockResolvedValue({
      new_messages: [
        {
          message_id: "1",
          subject: "hi anna",
          from: { name: "Anna", address: "anna@x" },
          date: null,
          account: { id: "1", name: "personal" },
        },
        {
          message_id: "2",
          subject: "second",
          from: { name: null, address: "bob@x" },
          date: null,
          account: { id: "2", name: "work" },
        },
      ],
      next_cursor: "2",
    });
    await mail.loadRecentMessages();
    mail.setSelection({ kind: "account", accountId: "1" });
    const { getByText, queryByText } = render(MessageList);
    expect(getByText("hi anna")).toBeTruthy();
    expect(queryByText("second")).toBeNull();
  });

  it("clicking a row calls openMessage with its id", async () => {
    mocks.listRecentMessages.mockResolvedValue({
      new_messages: [
        {
          message_id: "42",
          subject: "click me",
          from: { name: "X", address: null },
          date: null,
          account: { id: "1", name: "p" },
        },
      ],
      next_cursor: "42",
    });
    mocks.getMessage.mockResolvedValue({
      id: "42",
      subject: "click me",
      from: { name: null, address: null },
      to: [],
      cc: [],
      bcc: [],
      date: null,
      body_text: "body",
      body_html: null,
      attachments: [],
      account: { id: "1", name: null, address: null },
      folders: [],
    });
    await mail.loadRecentMessages();
    const { getByText } = render(MessageList);
    await fireEvent.click(getByText("click me"));
    expect(mocks.getMessage).toHaveBeenCalledWith("42");
  });
});
```

- [ ] **Step 2: Confirm failures**

```bash
cd gui && npm test -- MessageList 2>&1 | tail -10
```

Expected: fails (components missing).

- [ ] **Step 3: Implement `MessageListRow.svelte`**

```svelte
<script lang="ts">
  /**
   * Single row in the message list.
   *
   * Props: `message` (the loaded summary), `selected` (highlight), `onClick`.
   * Kept tiny — the list owns the data, the row just renders one item.
   */
  import { addressLabel, formatRelativeDate, truncate } from "../lib/format";
  import type { MessageSummary } from "../lib/tauri";

  let {
    message,
    selected,
    onClick,
  }: {
    message: MessageSummary;
    selected: boolean;
    onClick: () => void;
  } = $props();

  const SUBJECT_TRUNCATE_CHARS = 64;
</script>

<button type="button" class="row" class:selected onclick={onClick}>
  <div class="top">
    <span class="from">{addressLabel(message.from)}</span>
    <span class="date">{formatRelativeDate(message.date)}</span>
  </div>
  <div class="subject">{truncate(message.subject, SUBJECT_TRUNCATE_CHARS) || "(no subject)"}</div>
  <div class="meta">
    {message.account.name ?? message.account.id}
  </div>
</button>

<style>
  .row {
    display: block;
    width: 100%;
    text-align: left;
    background: none;
    border: none;
    border-bottom: 1px solid #ececec;
    padding: 8px 12px;
    font: inherit;
    cursor: pointer;
  }
  .row:hover {
    background: #f4f6f9;
  }
  .row.selected {
    background: #d8e6ff;
  }
  .top {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    font-size: 12px;
    color: #555;
  }
  .from {
    font-weight: 600;
    color: #222;
  }
  .date {
    flex-shrink: 0;
    margin-left: 8px;
  }
  .subject {
    margin-top: 2px;
    font-size: 13px;
    color: #222;
  }
  .meta {
    margin-top: 2px;
    font-size: 11px;
    color: #888;
  }
</style>
```

- [ ] **Step 4: Implement `MessageList.svelte`**

```svelte
<script lang="ts">
  /**
   * Middle pane. Filters the loaded message list by current selection,
   * renders one MessageListRow per visible message, dispatches clicks to
   * the mail store.
   */
  import MessageListRow from "./MessageListRow.svelte";
  import { selectionMatches } from "../lib/format";
  import { mail } from "../lib/stores/mail.svelte";

  function visibleMessages() {
    return mail.snapshot.messages.filter((m) =>
      selectionMatches(mail.snapshot.selection, m),
    );
  }

  async function openMessage(id: string): Promise<void> {
    await mail.openMessage(id);
  }
</script>

<section class="list">
  {#if mail.snapshot.loadingMessages}
    <div class="hint">Loading…</div>
  {:else}
    {@const items = visibleMessages()}
    {#if items.length === 0}
      <div class="hint">No messages.</div>
    {:else}
      {#each items as msg (msg.message_id)}
        <MessageListRow
          message={msg}
          selected={mail.snapshot.selectedMessage?.id === msg.message_id}
          onClick={() => openMessage(msg.message_id)}
        />
      {/each}
    {/if}
  {/if}
  {#if mail.snapshot.errorMessage}
    <div class="error">{mail.snapshot.errorMessage}</div>
  {/if}
</section>

<style>
  .list {
    height: 100%;
    overflow-y: auto;
    background: #fff;
    border-right: 1px solid #e5e5e5;
  }
  .hint {
    padding: 24px;
    text-align: center;
    color: #888;
    font-size: 13px;
  }
  .error {
    margin: 12px;
    padding: 8px 12px;
    background: #fdecec;
    border: 1px solid #f5c6c6;
    border-radius: 4px;
    color: #a02020;
    font-size: 12px;
    font-family: ui-monospace, SFMono-Regular, monospace;
  }
</style>
```

- [ ] **Step 5: Tests pass**

```bash
cd gui && npm test -- MessageList 2>&1 | tail -10
```

Expected: 4 passes.

- [ ] **Step 6: Commit**

```bash
git add gui/src/components/MessageList.svelte \
        gui/src/components/MessageListRow.svelte \
        gui/src/components/MessageList.test.ts
git commit -m "feat(gui-client): MessageList + MessageListRow + tests"
```

---

## Task 9: ReadingPane component + tests

**Files:**
- Create: `gui/src/components/ReadingPane.svelte`
- Create: `gui/src/components/ReadingPane.test.ts`

Right pane. Shows the currently-opened message's headers (From, To, Date, Account, Folders) and its plain-text body in a `<pre>` block. Empty state when no message is open. **HTML body is NOT rendered in this sub-plan** — there's a one-line note in the pane explaining that HTML rendering lands in Sub-plan 4.

- [ ] **Step 1: Write `ReadingPane.test.ts`**

```typescript
import { describe, expect, it, beforeEach } from "vitest";
import { render } from "@testing-library/svelte";

import ReadingPane from "./ReadingPane.svelte";
import { mail } from "../lib/stores/mail.svelte";

beforeEach(() => {
  mail.reset();
});

describe("ReadingPane", () => {
  it("shows empty state when no message is selected", () => {
    const { getByText } = render(ReadingPane);
    expect(getByText(/select a message/i)).toBeTruthy();
  });

  it("renders subject, from, plain-text body when a message is open", () => {
    mail.snapshot.selectedMessage = {
      id: "1",
      subject: "School excursion",
      from: { name: "Anna H.", address: "anna@example.com" },
      to: [{ name: null, address: "horst@example.com" }],
      cc: [],
      bcc: [],
      date: "2026-05-17T09:00:00Z",
      body_text: "Bus leaves at 7:30",
      body_html: null,
      attachments: [],
      account: { id: "1", name: "personal", address: "horst@example.com" },
      folders: [{ id: "10", name: "INBOX" }],
    };
    const { getByText } = render(ReadingPane);
    expect(getByText("School excursion")).toBeTruthy();
    expect(getByText(/anna h\./i)).toBeTruthy();
    expect(getByText("Bus leaves at 7:30")).toBeTruthy();
  });

  it("shows a placeholder when body_text is null", () => {
    mail.snapshot.selectedMessage = {
      id: "1",
      subject: "Empty body",
      from: { name: null, address: null },
      to: [],
      cc: [],
      bcc: [],
      date: null,
      body_text: null,
      body_html: null,
      attachments: [],
      account: { id: "1", name: null, address: null },
      folders: [],
    };
    const { getByText } = render(ReadingPane);
    expect(getByText(/no plain-text body/i)).toBeTruthy();
  });
});
```

- [ ] **Step 2: Implement `ReadingPane.svelte`**

```svelte
<script lang="ts">
  /**
   * Right pane. Renders headers + plain-text body for the currently-open
   * message. HTML rendering and attachments land in Sub-plan 4.
   */
  import { addressLabel, formatRelativeDate } from "../lib/format";
  import { mail } from "../lib/stores/mail.svelte";
</script>

<article class="pane">
  {#if mail.snapshot.loadingDetail}
    <div class="hint">Loading…</div>
  {:else if mail.snapshot.selectedMessage}
    {@const m = mail.snapshot.selectedMessage}
    <header>
      <h2>{m.subject ?? "(no subject)"}</h2>
      <dl class="headers">
        <dt>From</dt><dd>{addressLabel(m.from)}</dd>
        {#if m.to.length}
          <dt>To</dt><dd>{m.to.map(addressLabel).join(", ")}</dd>
        {/if}
        {#if m.cc.length}
          <dt>Cc</dt><dd>{m.cc.map(addressLabel).join(", ")}</dd>
        {/if}
        <dt>Date</dt><dd>{formatRelativeDate(m.date)}</dd>
        <dt>Account</dt>
        <dd>
          {m.account.name ?? m.account.id}
          {#if m.folders.length}
            <span class="folders"> · {m.folders.map((f) => f.name).join(", ")}</span>
          {/if}
        </dd>
      </dl>
    </header>

    {#if m.body_text}
      <pre class="body">{m.body_text}</pre>
    {:else}
      <div class="hint">No plain-text body. (HTML rendering arrives in Sub-plan 4.)</div>
    {/if}
  {:else}
    <div class="hint">Select a message to read it.</div>
  {/if}
</article>

<style>
  .pane {
    height: 100%;
    overflow-y: auto;
    padding: 16px 20px;
    background: #fff;
  }
  .hint {
    margin: 48px auto;
    text-align: center;
    color: #888;
    font-size: 13px;
  }
  header {
    border-bottom: 1px solid #eee;
    padding-bottom: 12px;
    margin-bottom: 16px;
  }
  h2 {
    margin: 0 0 8px 0;
    font-size: 18px;
  }
  .headers {
    display: grid;
    grid-template-columns: auto 1fr;
    column-gap: 12px;
    row-gap: 2px;
    margin: 0;
    font-size: 12px;
  }
  .headers dt {
    color: #888;
  }
  .headers dd {
    margin: 0;
    color: #222;
  }
  .folders {
    color: #888;
  }
  .body {
    white-space: pre-wrap;
    word-break: break-word;
    font-family: ui-monospace, SFMono-Regular, monospace;
    font-size: 13px;
    margin: 0;
    color: #222;
  }
</style>
```

- [ ] **Step 3: Tests pass**

```bash
cd gui && npm test -- ReadingPane 2>&1 | tail -10
```

Expected: 3 passes.

- [ ] **Step 4: Commit**

```bash
git add gui/src/components/ReadingPane.svelte \
        gui/src/components/ReadingPane.test.ts
git commit -m "feat(gui-client): ReadingPane component (plain-text only) + tests"
```

---

## Task 10: MainView screen + Router wiring + retire AuthenticatedShell

**Files:**
- Create: `gui/src/screens/MainView.svelte`
- Modify: `gui/src/routes/Router.svelte`
- Delete: `gui/src/screens/AuthenticatedShell.svelte`

`MainView` is the top-level screen for the `logged_in` phase. On mount it loads accounts and recent messages. It renders a small header (username, capabilities pills, log out, refresh token) above a 3-column grid (AccountTree | MessageList | ReadingPane).

- [ ] **Step 1: Implement `MainView.svelte`**

```svelte
<script lang="ts">
  /**
   * Top-level screen for the logged-in phase. Three-pane Layout-A:
   * [AccountTree | MessageList | ReadingPane] with a small header bar.
   *
   * On mount we kick off two parallel loads: the account list (drives the
   * tree) and the recent messages list (seeds the middle pane). Both go
   * through the `mail` store so other components observe the same state.
   */
  import { onMount } from "svelte";
  import AccountTree from "../components/AccountTree.svelte";
  import MessageList from "../components/MessageList.svelte";
  import ReadingPane from "../components/ReadingPane.svelte";
  import { auth } from "../lib/stores/auth.svelte";
  import { mail } from "../lib/stores/mail.svelte";

  let pending: boolean = $state(false);

  onMount(async () => {
    await Promise.all([mail.loadAccounts(), mail.loadRecentMessages()]);
  });

  async function onLogout(): Promise<void> {
    pending = true;
    try {
      mail.reset();
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
  <div class="app">
    <header class="bar">
      <div class="left">
        <strong>localmail</strong>
        <span class="username">{snap.username}</span>
      </div>
      <div class="right">
        <ul class="caps">
          <li class="cap" class:on={snap.capabilities.search}>search</li>
          <li class="cap" class:on={snap.capabilities.attachments}>attachments</li>
          <li class="cap" class:on={snap.capabilities.attachment_text}>attachment_text</li>
          <li class="cap" class:on={snap.capabilities.threading}>threading</li>
          <li class="cap" class:on={snap.capabilities.send}>send</li>
        </ul>
        <button onclick={onRefresh} disabled={pending}>Refresh token</button>
        <button onclick={onLogout} disabled={pending}>Log out</button>
      </div>
    </header>
    <main class="panes">
      <AccountTree />
      <MessageList />
      <ReadingPane />
    </main>
  </div>
{/if}

<style>
  .app {
    height: 100vh;
    display: grid;
    grid-template-rows: auto 1fr;
  }
  .bar {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 6px 12px;
    background: #f4f6f9;
    border-bottom: 1px solid #e0e3e8;
    font-size: 12px;
  }
  .left {
    display: flex;
    align-items: baseline;
    gap: 12px;
  }
  .username {
    color: #1a4fc7;
    font-weight: 600;
  }
  .right {
    display: flex;
    align-items: center;
    gap: 8px;
  }
  .caps {
    list-style: none;
    padding: 0;
    margin: 0 8px 0 0;
    display: flex;
    gap: 4px;
  }
  .cap {
    padding: 2px 8px;
    border-radius: 10px;
    background: #ececec;
    color: #888;
    font-size: 11px;
    font-family: ui-monospace, SFMono-Regular, monospace;
    text-decoration: line-through;
  }
  .cap.on {
    background: #e6f5dd;
    color: #2d6a1a;
    text-decoration: none;
  }
  button {
    padding: 3px 10px;
    font-size: 12px;
    background: #fff;
    border: 1px solid #ccc;
    border-radius: 4px;
    cursor: pointer;
  }
  button:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
  .panes {
    display: grid;
    grid-template-columns: 220px 340px 1fr;
    height: 100%;
    min-height: 0;
  }
</style>
```

- [ ] **Step 2: Update `Router.svelte`**

```svelte
<script lang="ts">
  import { onMount } from "svelte";
  import { auth } from "../lib/stores/auth.svelte";
  import ConnectScreen from "../screens/ConnectScreen.svelte";
  import LoginScreen from "../screens/LoginScreen.svelte";
  import MainView from "../screens/MainView.svelte";

  onMount(async () => {
    await auth.refreshState();
  });
</script>

{#if auth.snapshot.phase === "connecting" || auth.snapshot.phase === "needs_trust"}
  <ConnectScreen />
{:else if auth.snapshot.phase === "logged_out"}
  <LoginScreen />
{:else if auth.snapshot.phase === "logged_in"}
  <MainView />
{/if}
```

- [ ] **Step 3: Delete `AuthenticatedShell.svelte`**

```bash
rm gui/src/screens/AuthenticatedShell.svelte
```

- [ ] **Step 4: Type-check + tests**

```bash
cd gui && npm run check 2>&1 | tail -15
```

Expected: zero errors. (The deleted file's references are all gone since Router no longer imports it.)

```bash
cd gui && npm test 2>&1 | tail -15
```

Expected: full suite green.

```bash
cd gui/src-tauri && cargo test 2>&1 | tail -5
```

Expected: full Rust suite green.

- [ ] **Step 5: Commit**

```bash
git add gui/src/screens/MainView.svelte \
        gui/src/routes/Router.svelte
git rm gui/src/screens/AuthenticatedShell.svelte
git commit -m "feat(gui-client): MainView screen replaces AuthenticatedShell placeholder"
```

---

## Task 11: Manual smoke test docs + final commit

**Files:**
- Modify: `gui/README.md`

Document the Sub-plan 3 acceptance steps so the user can verify the build by hand. Mirror the format of the existing Sub-plan 2 section.

- [ ] **Step 1: Edit `gui/README.md`**

Find the "Manual smoke (Sub-plan 2 acceptance)" section. Append a new section after it:

```markdown
## Manual smoke (Sub-plan 3 acceptance)

Same server prereqs as Sub-plan 2. Run `localmail serve` and have at least
one account synced with some messages (otherwise the message list will be
empty — not a bug).

```bash
cd gui
npm run tauri dev
```

Acceptance steps:

1. Log in as before (Sub-plan 2 flow).
2. App lands on the new **Main view** — three columns:
   - Left rail: "📥 All Mail" pinned at top, then your configured accounts.
   - Middle column: a list of the most recent ~200 messages across all
     accounts, sorted newest first. Each row shows sender, subject, account,
     and a relative date.
   - Right pane: "Select a message to read it." placeholder.
3. Click an account in the left rail. The account expands to show its
   folders (loaded from `/v1/accounts/{id}/folders`). The middle column
   filters to messages from that account (**client-side filter on the
   already-loaded 200 — server-side narrowing arrives in Sub-plan 4**).
4. Click a folder. Selection narrows further but the same client-side
   account filter is what's actually applied (folder filtering is also
   server-side and deferred).
5. Click "📥 All Mail" to reset to the full loaded set.
6. Click any message row. The right pane loads its plain-text body and
   key headers (From / To / Date / Account · Folders). HTML-only messages
   show "No plain-text body. (HTML rendering arrives in Sub-plan 4.)" —
   that is expected behaviour for this sub-plan.
7. Click another message; the right pane updates without flicker.
8. Click the same message twice — no redundant network request fires.
9. "Refresh token" and "Log out" buttons in the top header still work.
10. After log out, log back in. The main view loads accounts + messages
    again with no stale data.

If any step fails, capture the DevTools console output AND the `npm run
tauri dev` terminal output, then report.
```

- [ ] **Step 2: Commit**

```bash
git add gui/README.md
git commit -m "docs(gui-client): Sub-plan 3 manual smoke acceptance steps"
```

- [ ] **Step 3: Push and open PR (when ready)**

```bash
git push -u origin gui-client-3
gh pr create --base main --head gui-client-3 \
  --title "feat(gui-client): Sub-plan 3 — Main view shell (3-pane)" \
  --body "$(cat <<'EOF'
## Summary
- Replaces the Sub-plan 2 AuthenticatedShell placeholder with a real 3-pane main view.
- Adds Rust commands for /v1/accounts, /v1/accounts/{id}/folders, /v1/changes, /v1/messages/{id}.
- Adds Svelte components: AccountTree (left), MessageList + MessageListRow (middle), ReadingPane (right).
- Adds the singleton `mail` store mirroring the `auth` store pattern.
- Adds pure format helpers (addressLabel, formatRelativeDate, truncate, selectionMatches) with unit tests.

## Out of scope (Sub-plan 4)
- HTML body rendering and external-image policy.
- Search bar, filter popover, snippets.
- Attachments strip + preview.
- Server-side folder/account narrowing (account/folder filters are
  client-side in this sub-plan; spec acknowledges as tech debt).

## Test plan
- [ ] `cargo test` green in `gui/src-tauri/`
- [ ] `npm test` green in `gui/`
- [ ] Manual smoke per `gui/README.md` Sub-plan 3 section

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

The push and PR step is only when ready (after a clean local smoke test); a subagent should not auto-push.

---

## End-of-plan acceptance

When all tasks are done and the PR is open / mergeable:

- `cargo test` in `gui/src-tauri/` shows the new commands' tests (3 + 2 + 2 = 7 new) all green plus the previous suite.
- `npm test` in `gui/` shows the new vitest suites green: format.test, mail.test, AccountTree.test, MessageList.test, ReadingPane.test.
- `npm run check` shows zero TypeScript errors.
- `npm run tauri dev` produces a working app that walks through the manual smoke without errors.
- No file in this sub-plan exceeds ~500 lines (per project convention).

## Notes for the executing engineer

- **Pure functions before stateful code.** Build `format.ts` (Task 5) before `mail.svelte.ts` (Task 6) before components (Tasks 7–9) before composition (Task 10). Each layer's tests run without the layer above.
- **Don't loosen CSP.** No new external `script-src` or `connect-src` directives. The Rust side does all network I/O.
- **Don't add a new server endpoint.** If you find yourself wanting one, that's Sub-plan 4.
- **`<svelte:component>` is deprecated.** Use `{@const C = component}<C />` if you ever need dynamic component selection in this sub-plan (you shouldn't).
- **Auth store has the receipts.** When in doubt about error formatting or the singleton pattern, copy from `auth.svelte.ts` and `auth.test.ts` — both shipped in Sub-plan 2 and work.
