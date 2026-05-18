//! GET /v1/messages/{id}?headers=full — MessageDetail with the optional
//! `headers` field populated (flat JSON object of raw header name → value).
//!
//! Reuses the `MessageDetail` struct from `commands::messages` so callers
//! get one shape regardless of which endpoint they hit; the `headers` field
//! is `Option` and absent when ?headers=full was not requested.

use reqwest::Client;

use crate::commands::auth::AuthError;
use crate::commands::messages::MessageDetail;
use crate::commands::session::read_authenticated;
use crate::http::client::{build_pinned_client, http_get_json};
use crate::storage::keyring::KeyringStore;

// Split out so tests can drive the URL/parse path against a mockito server
// (HTTP, no TLS pinning) without rebuilding the keyring + pinned-client wiring.
async fn fetch_full_headers(
    client: &Client,
    base_url: &str,
    message_id: &str,
    token: Option<&str>,
) -> Result<MessageDetail, AuthError> {
    // Percent-encode the path segment so message IDs containing `?`, `#`, `/`
    // (RFC 5322 permits all of these inside the dot-atom-text local part) do
    // not corrupt the URL. NON_ALPHANUMERIC is the safe default for opaque
    // path segments.
    let encoded_id =
        url::form_urlencoded::byte_serialize(message_id.as_bytes()).collect::<String>();
    let endpoint = format!("{base_url}v1/messages/{encoded_id}?headers=full");
    let detail: MessageDetail = http_get_json(client, &endpoint, token).await?;
    Ok(detail)
}

pub async fn get_message_full_headers(
    store: &KeyringStore,
    message_id: &str,
) -> Result<MessageDetail, AuthError> {
    let (url, pin, token) = read_authenticated(store)?;
    let client = build_pinned_client(&pin)?;
    fetch_full_headers(&client, &url, message_id, Some(&token)).await
}

#[tauri::command]
pub async fn get_message_full_headers_cmd(
    message_id: String,
) -> Result<MessageDetail, AuthError> {
    let store = KeyringStore::new();
    get_message_full_headers(&store, &message_id).await
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::storage::keyring::{KeyringStore, MemKeyring, Slot};

    fn fake_store() -> KeyringStore {
        KeyringStore::with_backend(MemKeyring::new())
    }

    #[tokio::test]
    async fn get_message_full_headers_without_connection_returns_not_connected() {
        let store = fake_store();
        let err = get_message_full_headers(&store, "1").await.unwrap_err();
        assert!(matches!(err, AuthError::NotConnected));
    }

    #[tokio::test]
    async fn get_message_full_headers_without_token_returns_not_logged_in() {
        let store = fake_store();
        store.put(Slot::ServerUrl, "https://localhost:8443/").unwrap();
        store.put(Slot::CertPin, "deadbeef").unwrap();
        let err = get_message_full_headers(&store, "1").await.unwrap_err();
        assert!(matches!(err, AuthError::NotLoggedIn));
    }

    #[tokio::test]
    async fn fetch_full_headers_hits_headers_full_url_and_parses_response() {
        let mut server = mockito::Server::new_async().await;
        let body = r#"{
            "id": "msg-42",
            "subject": "hi",
            "from": {"address": "a@example.com", "name": null},
            "to": [],
            "cc": [],
            "bcc": [],
            "date": null,
            "body_text": "hello",
            "body_html": null,
            "attachments": [],
            "account": {"id": "acct-1", "name": null, "address": null},
            "folders": [],
            "headers": {
                "Message-ID": "<x@y>",
                "Received": ["from a", "from b"]
            }
        }"#;
        let _m = server
            .mock("GET", "/v1/messages/msg-42")
            .match_query(mockito::Matcher::UrlEncoded("headers".into(), "full".into()))
            .match_header("authorization", "Bearer tok")
            .with_status(200)
            .with_header("content-type", "application/json")
            .with_body(body)
            .create_async()
            .await;

        let client = Client::new();
        // http_get_json builds the endpoint as `{base}v1/...` — base must end in `/`.
        let base = format!("{}/", server.url());
        let detail = fetch_full_headers(&client, &base, "msg-42", Some("tok"))
            .await
            .unwrap();
        assert_eq!(detail.id, "msg-42");
        assert_eq!(detail.subject.as_deref(), Some("hi"));
        let headers = detail.headers.expect("headers populated");
        assert_eq!(headers["Message-ID"], "<x@y>");
        assert_eq!(headers["Received"][0], "from a");
        assert_eq!(headers["Received"][1], "from b");
    }

    #[tokio::test]
    async fn fetch_full_headers_propagates_http_status_errors() {
        let mut server = mockito::Server::new_async().await;
        let _m = server
            .mock("GET", "/v1/messages/missing")
            .match_query(mockito::Matcher::UrlEncoded("headers".into(), "full".into()))
            .with_status(404)
            .with_body("not found")
            .create_async()
            .await;
        let client = Client::new();
        let base = format!("{}/", server.url());
        let err = fetch_full_headers(&client, &base, "missing", None)
            .await
            .unwrap_err();
        assert!(matches!(err, AuthError::Http(_)));
    }
}
