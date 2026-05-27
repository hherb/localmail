//! GET /v1/messages — keyset-paginated browse.
//!
//! Canonical browse / backfill endpoint (#38). Supports `account_ids`,
//! `folder_ids`, `limit`, and a `cursor` query parameter for paging into
//! older messages, ordered `COALESCE(internal_date, date_sent) DESC NULLS
//! LAST, id DESC`. The GUI's initial mail-list load and every `setSelection`
//! refetch go here.
//!
//! `/v1/changes` (see `changes.rs`) stays in place for forward incremental
//! polling only — it is tail-only by design and does NOT take a backwards
//! cursor.

use serde::{Deserialize, Serialize};

use crate::commands::auth::AuthError;
use crate::commands::changes::MessageSummary;
use crate::commands::session::read_authenticated;
use crate::http::client::{build_pinned_client, http_get_json};
use crate::storage::keyring::KeyringStore;

#[derive(Debug, Default, Deserialize, Serialize)]
pub struct ListMessagesRequest {
    #[serde(default)]
    pub account_ids: Vec<String>,
    #[serde(default)]
    pub folder_ids: Vec<String>,
    #[serde(default = "default_limit")]
    pub limit: u32,
    #[serde(default)]
    pub cursor: Option<String>,
}

fn default_limit() -> u32 { 50 }

#[derive(Debug, Deserialize, Serialize)]
pub struct ListMessagesResponse {
    pub messages: Vec<MessageSummary>,
    pub next_cursor: Option<String>,
}

pub async fn list_messages(
    store: &KeyringStore,
    req: ListMessagesRequest,
) -> Result<ListMessagesResponse, AuthError> {
    let (url, pin, token) = read_authenticated(store)?;
    let client = build_pinned_client(&pin)?;
    let mut qs: Vec<(String, String)> = Vec::new();
    for a in &req.account_ids { qs.push(("account_id".into(), a.clone())); }
    for f in &req.folder_ids { qs.push(("folder_id".into(), f.clone())); }
    qs.push(("limit".into(), req.limit.to_string()));
    if let Some(c) = &req.cursor { qs.push(("cursor".into(), c.clone())); }
    let mut endpoint = format!("{url}v1/messages?");
    endpoint.push_str(
        &qs.into_iter()
            .map(|(k, v)| format!("{}={}", k, urlencoding::encode(&v)))
            .collect::<Vec<_>>()
            .join("&"),
    );
    let resp: ListMessagesResponse = http_get_json(&client, &endpoint, Some(&token)).await?;
    Ok(resp)
}

#[tauri::command]
pub async fn list_messages_cmd(
    req: ListMessagesRequest,
) -> Result<ListMessagesResponse, AuthError> {
    let store = KeyringStore::new();
    list_messages(&store, req).await
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::storage::keyring::{MemKeyring, Slot};

    fn fake_store() -> KeyringStore {
        KeyringStore::with_backend(MemKeyring::new())
    }

    fn req() -> ListMessagesRequest {
        ListMessagesRequest {
            account_ids: vec![],
            folder_ids: vec![],
            limit: 50,
            cursor: None,
        }
    }

    #[tokio::test]
    async fn without_connection_returns_not_connected() {
        let s = fake_store();
        let err = list_messages(&s, req()).await.unwrap_err();
        assert!(matches!(err, AuthError::NotConnected));
    }

    #[tokio::test]
    async fn without_token_returns_not_logged_in() {
        let s = fake_store();
        s.put(Slot::ServerUrl, "https://localhost:8443/").unwrap();
        s.put(Slot::CertPin, "deadbeef").unwrap();
        let err = list_messages(&s, req()).await.unwrap_err();
        assert!(matches!(err, AuthError::NotLoggedIn));
    }

    #[test]
    fn response_deserialises_with_null_next_cursor() {
        let json = r#"{"messages": [], "next_cursor": null}"#;
        let resp: ListMessagesResponse = serde_json::from_str(json).unwrap();
        assert!(resp.messages.is_empty());
        assert!(resp.next_cursor.is_none());
    }

    #[test]
    fn response_deserialises_with_present_next_cursor() {
        let json = r#"{"messages": [], "next_cursor": "abcd"}"#;
        let resp: ListMessagesResponse = serde_json::from_str(json).unwrap();
        assert_eq!(resp.next_cursor.as_deref(), Some("abcd"));
    }
}
