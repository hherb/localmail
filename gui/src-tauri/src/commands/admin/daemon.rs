//! Proxies for `/v1/admin/daemon*` (daemon status + lifecycle) and
//! `/v1/admin/accounts/{id}/restart-sync` (per-account sync restart).
//!
//! Mirrors `commands::admin::accounts`: each endpoint has a `fetch_*` / `post_*`
//! helper taking an explicit `reqwest::Client` + base URL (mockito-testable),
//! plus a keyring-reading wrapper and a thin `#[tauri::command]`.
//!
//! Bearer-authenticated admin requests carry no ambient cookie, so the server
//! skips CSRF for them (see `serve/admin/csrf.py::check_csrf`) — none of these
//! proxies send an `X-CSRF-Token` header, unlike the HTML admin panel.
//!
//! IDs are strings on the wire in both directions (see CLAUDE.md, "ID typing");
//! nothing here parses them into integers.

use reqwest::Client;
use serde::{Deserialize, Serialize};

use crate::commands::auth::AuthError;
use crate::commands::session::read_authenticated;
use crate::http::client::{build_pinned_client, http_get_json, http_post_json};
use crate::storage::keyring::KeyringStore;

/// One sync worker's liveness row, as fused by `build_daemon_view` server-side.
/// `stale` is computed by the server against `heartbeat_stale_seconds`; the UI
/// renders red on this flag alone — never a client clock (design + #148).
#[derive(Debug, Deserialize, Serialize)]
pub struct DaemonHeartbeat {
    pub worker_kind: String,
    pub account_id: Option<String>,
    pub state: String,
    pub current_folder: Option<String>,
    pub last_error_msg: Option<String>,
    pub started_at: String,
    pub last_heartbeat_at: String,
    pub stale: bool,
}

/// The `GET /v1/admin/daemon` fused view: supervisor process state +
/// per-worker heartbeats + captured recent log lines.
#[derive(Debug, Deserialize, Serialize)]
pub struct DaemonView {
    pub state: String,
    pub pid: Option<i64>,
    pub started_at: Option<String>,
    pub supervise_daemon_externally: bool,
    pub heartbeats: Vec<DaemonHeartbeat>,
    pub recent_log: Vec<String>,
}

/// The transitional supervisor status returned (202) by a lifecycle POST.
#[derive(Debug, Deserialize, Serialize)]
pub struct DaemonStatus {
    pub state: String,
    pub pid: Option<i64>,
    pub started_at: Option<String>,
}

/// Acknowledgement returned by the DB-mediated (Plane A) controls — reload and
/// per-account restart-sync — carrying the enqueued `daemon_commands` row id.
#[derive(Debug, Deserialize, Serialize)]
pub struct CommandAck {
    pub command_id: String,
}

async fn fetch_daemon(
    client: &Client,
    base_url: &str,
    token: &str,
) -> Result<DaemonView, AuthError> {
    let endpoint = format!("{base_url}v1/admin/daemon");
    Ok(http_get_json(client, &endpoint, Some(token)).await?)
}

async fn post_lifecycle(
    client: &Client,
    base_url: &str,
    token: &str,
    op: &str,
) -> Result<DaemonStatus, AuthError> {
    // The routes take no request body; an empty JSON object is ignored by
    // FastAPI and keeps a uniform content-type. The busy-guard / external stub
    // surfaces as a 409, mapped to HttpError::HttpStatus by http_post_json.
    let endpoint = format!("{base_url}v1/admin/daemon/{op}");
    Ok(http_post_json(client, &endpoint, &serde_json::json!({}), Some(token)).await?)
}

async fn post_reload(
    client: &Client,
    base_url: &str,
    token: &str,
) -> Result<CommandAck, AuthError> {
    let endpoint = format!("{base_url}v1/admin/daemon/reload");
    Ok(http_post_json(client, &endpoint, &serde_json::json!({}), Some(token)).await?)
}

async fn post_restart_sync(
    client: &Client,
    base_url: &str,
    token: &str,
    account_id: &str,
) -> Result<CommandAck, AuthError> {
    let endpoint = format!("{base_url}v1/admin/accounts/{account_id}/restart-sync");
    Ok(http_post_json(client, &endpoint, &serde_json::json!({}), Some(token)).await?)
}

pub async fn get_admin_daemon(store: &KeyringStore) -> Result<DaemonView, AuthError> {
    let (url, pin, token) = read_authenticated(store)?;
    let client = build_pinned_client(&pin)?;
    fetch_daemon(&client, &url, &token).await
}

pub async fn lifecycle_admin_daemon(
    store: &KeyringStore,
    op: &str,
) -> Result<DaemonStatus, AuthError> {
    let (url, pin, token) = read_authenticated(store)?;
    let client = build_pinned_client(&pin)?;
    post_lifecycle(&client, &url, &token, op).await
}

pub async fn reload_admin_daemon(store: &KeyringStore) -> Result<CommandAck, AuthError> {
    let (url, pin, token) = read_authenticated(store)?;
    let client = build_pinned_client(&pin)?;
    post_reload(&client, &url, &token).await
}

pub async fn restart_account_sync(
    store: &KeyringStore,
    account_id: &str,
) -> Result<CommandAck, AuthError> {
    let (url, pin, token) = read_authenticated(store)?;
    let client = build_pinned_client(&pin)?;
    post_restart_sync(&client, &url, &token, account_id).await
}

#[tauri::command]
pub async fn get_admin_daemon_cmd() -> Result<DaemonView, AuthError> {
    let store = KeyringStore::new();
    get_admin_daemon(&store).await
}

#[tauri::command]
pub async fn lifecycle_admin_daemon_cmd(op: String) -> Result<DaemonStatus, AuthError> {
    let store = KeyringStore::new();
    lifecycle_admin_daemon(&store, &op).await
}

#[tauri::command]
pub async fn reload_admin_daemon_cmd() -> Result<CommandAck, AuthError> {
    let store = KeyringStore::new();
    reload_admin_daemon(&store).await
}

#[tauri::command]
pub async fn restart_account_sync_cmd(account_id: String) -> Result<CommandAck, AuthError> {
    let store = KeyringStore::new();
    restart_account_sync(&store, &account_id).await
}

#[cfg(test)]
#[path = "daemon_tests.rs"]
mod tests;
