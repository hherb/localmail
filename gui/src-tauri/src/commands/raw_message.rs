//! GET /v1/messages/{id}/raw — returns the raw RFC822 bytes as a Vec<u8>.
//!
//! Used by the GUI's "view raw source" path. The body can be megabytes for
//! messages with large inline attachments, so we cap it the same way
//! `attachments.rs` does to avoid an unbounded buffer / IPC payload.
//!
//! Errors surface via the dedicated [`RawMessageError`] enum — like the
//! attachment commands (issue #22), raw-message I/O failures must not
//! masquerade as auth-domain errors. Auth pre-check failures still compose
//! by bubbling through `RawMessageError::Auth(#[from] AuthError)`.

use serde::Serialize;
use thiserror::Error;

use crate::commands::auth::AuthError;
use crate::commands::session::read_authenticated;
use crate::http::body::{error_chain, read_capped, BodyError};
use crate::http::client::build_pinned_client;
use crate::http::client::REQUEST_TIMEOUT_SECS;
use crate::http::errors::HttpError;
use crate::storage::keyring::KeyringStore;

// Same ceiling as `attachments::MAX_ATTACHMENT_BYTES`: raw RFC822 includes
// any base64-encoded attachments inline, so the bound has to match.
const MAX_RAW_BYTES: u64 = 100 * 1024 * 1024;

/// Errors raised by the raw-message download command.
///
/// See [`super::attachments::AttachmentError`] for the rationale on `Setup`
/// vs `Auth(Http)` — the same routing applies here: `?` on
/// `Result<_, HttpError>` resolves to `Setup` (pinned-client construction),
/// `?` on `Result<_, AuthError>` resolves to `Auth` (auth pre-checks).
#[derive(Debug, Error, Serialize)]
#[serde(tag = "kind", content = "detail")]
pub enum RawMessageError {
    #[error("{0}")]
    Auth(#[from] AuthError),

    #[error("{0}")]
    Setup(#[from] HttpError),

    /// `size` is the declared `Content-Length`, or without one the bytes
    /// received when the read stopped (a lower bound).
    #[error("raw message too large: {size} bytes (max {max})")]
    TooLarge { size: u64, max: u64 },

    #[error("network error: {0}")]
    Network(String),

    /// The pinned client's total timeout, which includes the body (#464).
    #[error("request timed out after {seconds}s")]
    Timeout { seconds: u64 },

    #[error("server returned HTTP {0}")]
    Http(u16),

    #[error("read body: {0}")]
    Read(String),
}

impl From<BodyError> for RawMessageError {
    fn from(e: BodyError) -> Self {
        match e {
            BodyError::TooLarge { size } => RawMessageError::TooLarge {
                size,
                max: MAX_RAW_BYTES,
            },
            BodyError::Timeout => RawMessageError::Timeout {
                seconds: REQUEST_TIMEOUT_SECS,
            },
            BodyError::Read(msg) => RawMessageError::Read(msg),
        }
    }
}

impl RawMessageError {
    /// A failed `send()`: a timeout says so; anything else keeps its causes.
    fn send_failed(e: reqwest::Error) -> Self {
        if e.is_timeout() {
            RawMessageError::Timeout {
                seconds: REQUEST_TIMEOUT_SECS,
            }
        } else {
            RawMessageError::Network(error_chain(&e))
        }
    }
}

pub async fn get_message_raw(
    store: &KeyringStore,
    message_id: &str,
) -> Result<Vec<u8>, RawMessageError> {
    let (url, pin, token) = read_authenticated(store)?;
    let client = build_pinned_client(&pin)?;
    let encoded_id =
        ::url::form_urlencoded::byte_serialize(message_id.as_bytes()).collect::<String>();
    let endpoint = format!("{url}v1/messages/{encoded_id}/raw");
    let resp = client
        .get(&endpoint)
        .bearer_auth(&token)
        .send()
        .await
        .map_err(RawMessageError::send_failed)?;
    if !resp.status().is_success() {
        return Err(RawMessageError::Http(resp.status().as_u16()));
    }
    Ok(read_capped(resp, MAX_RAW_BYTES).await?)
}

#[tauri::command]
pub async fn get_message_raw_cmd(message_id: String) -> Result<Vec<u8>, RawMessageError> {
    let store = KeyringStore::new();
    get_message_raw(&store, &message_id).await
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::http::body::test_support::{write_past, MAX_OVERSHOOT};
    use crate::storage::keyring::{MemKeyring, Slot};

    fn fake_store() -> KeyringStore {
        KeyringStore::with_backend(MemKeyring::new())
    }

    // The capped read reports only the size; the command's own ceiling must
    // be what reaches the wire as `max`, and a read error keep its text.
    #[test]
    fn body_errors_map_onto_this_commands_ceiling() {
        assert!(matches!(
            RawMessageError::from(BodyError::TooLarge { size: 7 }),
            RawMessageError::TooLarge {
                size: 7,
                max: MAX_RAW_BYTES
            }
        ));
        assert!(matches!(
            RawMessageError::from(BodyError::Timeout),
            RawMessageError::Timeout {
                seconds: REQUEST_TIMEOUT_SECS
            }
        ));
        assert!(matches!(
            RawMessageError::from(BodyError::Read("reset".into())),
            RawMessageError::Read(m) if m == "reset"
        ));
    }

    // Pins the wiring, not just the helper: a command that went back to
    // buffering the body and checking afterwards would report the full size.
    #[tokio::test]
    async fn get_message_raw_cuts_an_oversized_chunked_body_off_at_the_cap() {
        let mut server = mockito::Server::new_async().await;
        let _m = server
            .mock("GET", "/v1/messages/7/raw")
            .with_chunked_body(write_past(MAX_RAW_BYTES))
            .create_async()
            .await;
        let store = fake_store();
        store
            .put(Slot::ServerUrl, &format!("{}/", server.url()))
            .unwrap();
        store.put(Slot::CertPin, "deadbeef").unwrap();
        store.put(Slot::BearerToken, "tok").unwrap();
        let err = get_message_raw(&store, "7").await.unwrap_err();
        assert!(
            matches!(
                err,
                RawMessageError::TooLarge { size, max: MAX_RAW_BYTES }
                    if size > MAX_RAW_BYTES && size <= MAX_RAW_BYTES + MAX_OVERSHOOT
            ),
            "{err:?}"
        );
    }

    #[tokio::test]
    async fn get_message_raw_without_connection_returns_auth_not_connected() {
        let store = fake_store();
        let err = get_message_raw(&store, "1").await.unwrap_err();
        assert!(matches!(
            err,
            RawMessageError::Auth(AuthError::NotConnected)
        ));
    }

    #[tokio::test]
    async fn get_message_raw_without_token_returns_auth_not_logged_in() {
        let store = fake_store();
        store
            .put(Slot::ServerUrl, "https://localhost:8443/")
            .unwrap();
        store.put(Slot::CertPin, "deadbeef").unwrap();
        let err = get_message_raw(&store, "1").await.unwrap_err();
        assert!(matches!(err, RawMessageError::Auth(AuthError::NotLoggedIn)));
    }

    #[tokio::test]
    async fn get_message_raw_network_failure_maps_to_network_variant() {
        // Unreachable port → reqwest send() fails → typed RawMessageError::Network.
        // Exercises URL building (host:port:scheme passes through) and the
        // typed transport-error mapping without standing up a real TLS server.
        let store = fake_store();
        store.put(Slot::ServerUrl, "https://127.0.0.1:1/").unwrap();
        store.put(Slot::CertPin, "deadbeef").unwrap();
        store.put(Slot::BearerToken, "tok").unwrap();
        let err = get_message_raw(&store, "msg-42").await.unwrap_err();
        assert!(
            matches!(err, RawMessageError::Network(_)),
            "expected RawMessageError::Network, got {err:?}"
        );
    }

    #[test]
    fn raw_message_error_serializes_with_kind_and_detail_tags() {
        // Lock the wire shape so the JS formatError() walker keeps working.
        let too_large = RawMessageError::TooLarge {
            size: 200 * 1024 * 1024,
            max: MAX_RAW_BYTES,
        };
        assert_eq!(
            serde_json::to_value(&too_large).unwrap(),
            serde_json::json!({
                "kind": "TooLarge",
                "detail": {"size": 200 * 1024 * 1024_u64, "max": MAX_RAW_BYTES},
            }),
        );

        let http = RawMessageError::Http(404);
        assert_eq!(
            serde_json::to_value(&http).unwrap(),
            serde_json::json!({"kind": "Http", "detail": 404}),
        );

        let network = RawMessageError::Network("connection refused".into());
        assert_eq!(
            serde_json::to_value(&network).unwrap(),
            serde_json::json!({"kind": "Network", "detail": "connection refused"}),
        );

        let timeout = RawMessageError::Timeout { seconds: 15 };
        assert_eq!(
            serde_json::to_value(&timeout).unwrap(),
            serde_json::json!({"kind": "Timeout", "detail": {"seconds": 15}}),
        );

        let read = RawMessageError::Read("stream closed".into());
        assert_eq!(
            serde_json::to_value(&read).unwrap(),
            serde_json::json!({"kind": "Read", "detail": "stream closed"}),
        );

        let setup: RawMessageError = HttpError::BadUrl("https://".into()).into();
        assert_eq!(
            serde_json::to_value(&setup).unwrap(),
            serde_json::json!({"kind": "Setup", "detail": {"kind": "BadUrl", "detail": "https://"}}),
        );

        let wrapped: RawMessageError = AuthError::NotConnected.into();
        assert_eq!(
            serde_json::to_value(&wrapped).unwrap(),
            serde_json::json!({"kind": "Auth", "detail": {"kind": "NotConnected"}}),
        );
    }

    // A store pointing at a port nothing listens on, so every command's
    // send() fails with a refusal: pins that the call site goes through
    // `send_failed` rather than its old `Network(e.to_string())`.
    fn refused_store() -> KeyringStore {
        let addr = std::net::TcpListener::bind("127.0.0.1:0")
            .unwrap()
            .local_addr()
            .unwrap();
        let s = fake_store();
        s.put(Slot::ServerUrl, &format!("http://{addr}/")).unwrap();
        s.put(Slot::CertPin, "deadbeef").unwrap();
        s.put(Slot::BearerToken, "tok").unwrap();
        s
    }

    fn assert_network_with_cause(err: RawMessageError) {
        match err {
            // reqwest's own Display ends at "(<url>)"; the cause follows it.
            RawMessageError::Network(msg) => assert!(msg.contains("): "), "no cause in {msg:?}"),
            other => panic!("expected Network, got {other:?}"),
        }
    }

    #[tokio::test]
    async fn get_raw_reports_a_refused_send_with_its_cause() {
        let err = get_message_raw(&refused_store(), "1").await.unwrap_err();
        assert_network_with_cause(err);
    }

    // #464: a send() that times out names the timeout. A listener that
    // accepts and never answers stalls the request without a server.
    #[tokio::test]
    async fn a_send_timeout_is_reported_as_a_timeout() {
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let err = reqwest::Client::builder()
            .timeout(std::time::Duration::from_millis(200))
            .build()
            .unwrap()
            .get(format!("http://{}/", listener.local_addr().unwrap()))
            .send()
            .await
            .unwrap_err();
        assert!(matches!(
            RawMessageError::send_failed(err),
            RawMessageError::Timeout {
                seconds: REQUEST_TIMEOUT_SECS
            }
        ));
    }

    #[tokio::test]
    async fn a_refused_send_is_a_network_error_with_its_cause() {
        let addr = std::net::TcpListener::bind("127.0.0.1:0")
            .unwrap()
            .local_addr()
            .unwrap(); // dropped: nothing listens there now
        let err = reqwest::Client::new()
            .get(format!("http://{addr}/"))
            .send()
            .await
            .unwrap_err();
        let plain = err.to_string();
        match RawMessageError::send_failed(err) {
            RawMessageError::Network(msg) => {
                assert!(msg.len() > plain.len(), "no cause in {msg:?}")
            }
            other => panic!("expected Network, got {other:?}"),
        }
    }
}
