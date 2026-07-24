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
use crate::http::client::{
    build_pinned_client, http_delete, http_get_json, http_patch_json, http_post_json,
    http_post_json_no_resp,
};
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

#[tauri::command]
pub async fn list_admin_accounts_cmd() -> Result<Vec<AdminAccountSummary>, AuthError> {
    let store = KeyringStore::new();
    list_admin_accounts(&store).await
}

#[tauri::command]
pub async fn get_admin_account_cmd(account_id: String) -> Result<AdminAccount, AuthError> {
    let store = KeyringStore::new();
    get_admin_account(&store, &account_id).await
}

#[cfg(test)]
#[path = "accounts_tests.rs"]
mod tests;
