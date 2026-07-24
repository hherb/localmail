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
pub async fn get_admin_account_cmd(account_id: String) -> Result<AdminAccount, AuthError> {
    let store = KeyringStore::new();
    get_admin_account(&store, &account_id).await
}

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
        assert_eq!(
            got.folder_deny_flags.as_deref(),
            Some(&["\\Trash".to_string()][..])
        );
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
