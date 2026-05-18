//! POST /v1/search wrapper.
//!
//! Body: {"query": "...", "filters": {...}, "limit": N, "cursor": null|"..."}
//! Response: {"results": [SearchResultRow], "next_cursor": str|null,
//!            "total_estimate": int|null, "took_ms": float}.

use serde::{Deserialize, Serialize};

use crate::commands::auth::AuthError;
use crate::commands::session::read_authenticated;
use crate::http::client::{build_pinned_client, http_post_json};
use crate::storage::keyring::KeyringStore;

#[derive(Debug, Deserialize, Serialize)]
pub struct SearchFiltersWire {
    #[serde(skip_serializing_if = "Vec::is_empty")]
    pub account_ids: Vec<String>,
    #[serde(skip_serializing_if = "Vec::is_empty")]
    pub folder_ids: Vec<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub from: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub to: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub subject: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub after: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub before: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub has_attachment: Option<bool>,
}

#[derive(Debug, Deserialize, Serialize)]
pub struct SearchRequest {
    pub query: String,
    pub filters: SearchFiltersWire,
    pub limit: u32,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub cursor: Option<String>,
}

#[derive(Debug, Deserialize, Serialize)]
pub struct SearchAddress {
    pub address: Option<String>,
    pub name: Option<String>,
}

#[derive(Debug, Deserialize, Serialize)]
pub struct SearchAccount {
    pub id: String,
    pub name: Option<String>,
}

#[derive(Debug, Deserialize, Serialize)]
pub struct SearchFolder {
    pub id: String,
    pub full_path: String,
}

#[derive(Debug, Deserialize, Serialize)]
pub struct SearchResultRow {
    pub message_id: String,
    pub account: SearchAccount,
    pub folder: Option<SearchFolder>,
    pub subject: Option<String>,
    pub from: SearchAddress,
    pub to: Vec<SearchAddress>,
    pub date: Option<String>,
    pub snippet_html: Option<String>,
    pub has_attachments: bool,
    pub score: f64,
    pub matched_arms: Vec<String>,
}

#[derive(Debug, Deserialize, Serialize)]
pub struct SearchResponse {
    pub results: Vec<SearchResultRow>,
    pub next_cursor: Option<String>,
    pub total_estimate: Option<i64>,
    pub took_ms: f64,
}

pub async fn run_search(
    store: &KeyringStore,
    req: SearchRequest,
) -> Result<SearchResponse, AuthError> {
    let (url, pin, token) = read_authenticated(store)?;
    let client = build_pinned_client(&pin)?;
    let endpoint = format!("{url}v1/search");
    let resp: SearchResponse = http_post_json(&client, &endpoint, &req, Some(&token)).await?;
    Ok(resp)
}

#[tauri::command]
pub async fn run_search_cmd(req: SearchRequest) -> Result<SearchResponse, AuthError> {
    let store = KeyringStore::new();
    run_search(&store, req).await
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::storage::keyring::{MemKeyring, Slot};

    fn fake_store() -> KeyringStore {
        KeyringStore::with_backend(MemKeyring::new())
    }

    fn req() -> SearchRequest {
        SearchRequest {
            query: "hello".into(),
            filters: SearchFiltersWire {
                account_ids: vec![],
                folder_ids: vec![],
                from: None,
                to: None,
                subject: None,
                after: None,
                before: None,
                has_attachment: None,
            },
            limit: 50,
            cursor: None,
        }
    }

    #[tokio::test]
    async fn search_without_connection_returns_not_connected() {
        let s = fake_store();
        let err = run_search(&s, req()).await.unwrap_err();
        assert!(matches!(err, AuthError::NotConnected));
    }

    #[tokio::test]
    async fn search_without_token_returns_not_logged_in() {
        let s = fake_store();
        s.put(Slot::ServerUrl, "https://localhost:8443/").unwrap();
        s.put(Slot::CertPin, "deadbeef").unwrap();
        let err = run_search(&s, req()).await.unwrap_err();
        assert!(matches!(err, AuthError::NotLoggedIn));
    }

    #[test]
    fn empty_filter_lists_omitted_from_wire_json() {
        let body = serde_json::to_string(&req()).unwrap();
        assert!(!body.contains("account_ids"));
        assert!(!body.contains("folder_ids"));
        assert!(!body.contains("\"from\":"));
    }

    #[test]
    fn populated_filter_lists_serialize_as_string_arrays() {
        let mut r = req();
        r.filters.account_ids = vec!["1".into(), "3".into()];
        r.filters.folder_ids = vec!["42".into()];
        let body = serde_json::to_string(&r).unwrap();
        assert!(body.contains("\"account_ids\":[\"1\",\"3\"]"));
        assert!(body.contains("\"folder_ids\":[\"42\"]"));
    }

    #[test]
    fn search_response_deserialises_with_optional_next_cursor() {
        let json = r#"{"results":[],"next_cursor":null,"total_estimate":null,"took_ms":3.14}"#;
        let resp: SearchResponse = serde_json::from_str(json).unwrap();
        assert!(resp.results.is_empty());
        assert!(resp.next_cursor.is_none());
        assert_eq!(resp.took_ms, 3.14);
    }
}
