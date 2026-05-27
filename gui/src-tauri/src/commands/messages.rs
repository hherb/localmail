//! GET /v1/messages/{id} — full message detail.
//!
//! Returns the full RFC822-derived view of a single message: `body_text`,
//! sanitized `body_html`, `attachments`, key headers, `from`/`to`/`cc`/`bcc`,
//! `date`, and the account/folder breadcrumb. `body_html` and `attachments`
//! drive the HTML reading pane + attachments strip; `bcc` is parsed but not
//! rendered from the typed field — Bcc only surfaces via the "Show full
//! headers" expander when the raw header is present.

use serde::{Deserialize, Serialize};
use serde_json::Value;

use crate::commands::auth::AuthError;
use crate::commands::changes::MessageAddress;
use crate::commands::session::read_authenticated;
use crate::http::client::{build_pinned_client, http_get_json};
use crate::storage::keyring::KeyringStore;

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
    // Populated only when the caller requested ?headers=full. The shape is a
    // flat object of raw header name → value (string | array of strings), kept
    // as serde_json::Value so the UI can iterate without us having to model
    // every RFC header here.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub headers: Option<Value>,
}

pub async fn get_message(store: &KeyringStore, message_id: &str) -> Result<MessageDetail, AuthError> {
    let (url, pin, token) = read_authenticated(store)?;
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
    use crate::storage::keyring::{MemKeyring, Slot};

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
