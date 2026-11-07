//! Tests for [`super`] (split out to keep the module under ~500 lines).

use super::*;
use crate::http::body::test_support::{write_past, MAX_OVERSHOOT};
use crate::storage::keyring::{MemKeyring, Slot};

// 64 lowercase 'a' — passes validate_sha256.
const VALID_SHA: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";

fn fake_store() -> KeyringStore {
    KeyringStore::with_backend(MemKeyring::new())
}

// The capped read reports only the size; the command's own ceiling must
// be what reaches the wire as `max`, and a read error keep its text.
#[test]
fn body_errors_map_onto_this_commands_ceiling() {
    assert!(matches!(
        AttachmentError::from(BodyError::TooLarge { size: 7 }),
        AttachmentError::TooLarge {
            size: 7,
            max: MAX_ATTACHMENT_BYTES
        }
    ));
    assert!(matches!(
        AttachmentError::from(BodyError::Timeout),
        AttachmentError::Timeout {
            seconds: REQUEST_TIMEOUT_SECS
        }
    ));
    assert!(matches!(
        AttachmentError::from(BodyError::Read("reset".into())),
        AttachmentError::Read(m) if m == "reset"
    ));
}

// Pins the wiring, not just the helper: a command that went back to
// buffering the body and checking afterwards would report the full size.
async fn oversized_attachment_server() -> (mockito::ServerGuard, mockito::Mock, KeyringStore) {
    let mut server = mockito::Server::new_async().await;
    let mock = server
        .mock("GET", format!("/v1/attachments/{VALID_SHA}").as_str())
        .with_chunked_body(write_past(MAX_ATTACHMENT_BYTES))
        .create_async()
        .await;
    let s = fake_store();
    s.put(Slot::ServerUrl, &format!("{}/", server.url()))
        .unwrap();
    s.put(Slot::CertPin, "deadbeef").unwrap();
    s.put(Slot::BearerToken, "tok").unwrap();
    (server, mock, s)
}

fn assert_cut_off_at_the_cap(err: AttachmentError) {
    assert!(
        matches!(
            err,
            AttachmentError::TooLarge { size, max: MAX_ATTACHMENT_BYTES }
                if size > MAX_ATTACHMENT_BYTES && size <= MAX_ATTACHMENT_BYTES + MAX_OVERSHOOT
        ),
        "{err:?}"
    );
}

#[tokio::test]
async fn fetch_bytes_cuts_an_oversized_chunked_body_off_at_the_cap() {
    let (_server, _m, s) = oversized_attachment_server().await;
    let err = fetch_attachment_bytes(&s, VALID_SHA).await.unwrap_err();
    assert_cut_off_at_the_cap(err);
}

// No success test existed for the download. It checks the file is
// replaced whole with no temp left beside it; it cannot tell
// `write_replacing` from `fs::write`, which agree on success — the
// failure paths are pinned in `save_file`.
#[tokio::test]
async fn download_replaces_the_destination_and_leaves_no_temp() {
    let mut server = mockito::Server::new_async().await;
    let _m = server
        .mock("GET", format!("/v1/attachments/{VALID_SHA}").as_str())
        .with_body("new attachment bytes")
        .create_async()
        .await;
    let s = fake_store();
    s.put(Slot::ServerUrl, &format!("{}/", server.url()))
        .unwrap();
    s.put(Slot::CertPin, "deadbeef").unwrap();
    s.put(Slot::BearerToken, "tok").unwrap();
    let dir = std::env::temp_dir().join(format!("localmail-465-dl-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir(&dir).unwrap();
    let dest = dir.join("saved.bin");
    std::fs::write(&dest, b"an older, longer file the user chose to overwrite").unwrap();
    let result = download_attachment(&s, VALID_SHA, dest.clone()).await;
    let names: Vec<_> = std::fs::read_dir(&dir)
        .unwrap()
        .map(|e| e.unwrap().file_name())
        .collect();
    let contents = std::fs::read(&dest).unwrap();
    let _ = std::fs::remove_dir_all(&dir);
    assert_eq!(result.unwrap().bytes_written, 20);
    assert_eq!(contents, b"new attachment bytes");
    assert_eq!(names, ["saved.bin"]);
}

#[tokio::test]
async fn download_cuts_an_oversized_chunked_body_off_and_writes_nothing() {
    let (_server, _m, s) = oversized_attachment_server().await;
    let dest =
        std::env::temp_dir().join(format!("localmail-218-{}-{VALID_SHA}", std::process::id()));
    let _ = std::fs::remove_file(&dest);
    let err = download_attachment(&s, VALID_SHA, dest.clone())
        .await
        .unwrap_err();
    assert_cut_off_at_the_cap(err);
    assert!(!dest.exists(), "nothing may be written on refusal");
}

#[tokio::test]
async fn download_without_connection_returns_auth_not_connected() {
    let s = fake_store();
    let err = download_attachment(&s, VALID_SHA, PathBuf::from("/tmp/x"))
        .await
        .unwrap_err();
    assert!(matches!(
        err,
        AttachmentError::Auth(AuthError::NotConnected)
    ));
}

#[tokio::test]
async fn download_without_token_returns_auth_not_logged_in() {
    let s = fake_store();
    s.put(Slot::ServerUrl, "https://localhost:8443/").unwrap();
    s.put(Slot::CertPin, "deadbeef").unwrap();
    let err = download_attachment(&s, VALID_SHA, PathBuf::from("/tmp/x"))
        .await
        .unwrap_err();
    assert!(matches!(err, AttachmentError::Auth(AuthError::NotLoggedIn)));
}

#[tokio::test]
async fn fetch_bytes_without_connection_returns_auth_not_connected() {
    let s = fake_store();
    let err = fetch_attachment_bytes(&s, VALID_SHA).await.unwrap_err();
    assert!(matches!(
        err,
        AttachmentError::Auth(AuthError::NotConnected)
    ));
}

#[tokio::test]
async fn fetch_bytes_without_token_returns_auth_not_logged_in() {
    let s = fake_store();
    s.put(Slot::ServerUrl, "https://localhost:8443/").unwrap();
    s.put(Slot::CertPin, "deadbeef").unwrap();
    let err = fetch_attachment_bytes(&s, VALID_SHA).await.unwrap_err();
    assert!(matches!(err, AttachmentError::Auth(AuthError::NotLoggedIn)));
}

#[test]
fn validate_sha256_accepts_64_hex_chars() {
    assert!(validate_sha256(&"a".repeat(64)).is_ok());
    assert!(validate_sha256(&"0123456789abcdefABCDEF".repeat(3)[..64]).is_ok());
}

#[test]
fn validate_sha256_rejects_wrong_length() {
    assert!(matches!(
        validate_sha256(""),
        Err(AttachmentError::InvalidSha256(_))
    ));
    assert!(matches!(
        validate_sha256(&"a".repeat(63)),
        Err(AttachmentError::InvalidSha256(_))
    ));
    assert!(matches!(
        validate_sha256(&"a".repeat(65)),
        Err(AttachmentError::InvalidSha256(_))
    ));
}

#[test]
fn validate_sha256_rejects_non_hex() {
    let mut s = "a".repeat(63);
    s.push('!');
    assert!(matches!(
        validate_sha256(&s),
        Err(AttachmentError::InvalidSha256(_))
    ));
    s.pop();
    s.push('z');
    assert!(matches!(
        validate_sha256(&s),
        Err(AttachmentError::InvalidSha256(_))
    ));
}

#[test]
fn validate_sha256_rejects_path_traversal() {
    // The most obvious abuse — slashes — fails the hex check.
    assert!(matches!(
        validate_sha256("../../etc/passwd"),
        Err(AttachmentError::InvalidSha256(_))
    ));
    assert!(matches!(
        validate_sha256(&format!("{}{}{}", "a".repeat(30), "/", "a".repeat(33))),
        Err(AttachmentError::InvalidSha256(_))
    ));
}

#[test]
fn invalid_sha256_carries_the_offending_input() {
    // The diagnostic is the only signal a JS consumer or developer has —
    // make sure the bad value travels through so it shows up in formatError().
    let err = validate_sha256("oops").unwrap_err();
    match err {
        AttachmentError::InvalidSha256(s) => assert_eq!(s, "oops"),
        other => panic!("expected InvalidSha256, got {other:?}"),
    }
}

#[tokio::test]
async fn download_rejects_invalid_sha256_before_keyring_read() {
    let s = fake_store();
    let err = download_attachment(&s, "not-a-sha", PathBuf::from("/tmp/x"))
        .await
        .unwrap_err();
    assert!(matches!(err, AttachmentError::InvalidSha256(_)));
}

#[tokio::test]
async fn fetch_bytes_rejects_invalid_sha256_before_keyring_read() {
    let s = fake_store();
    let err = fetch_attachment_bytes(&s, "not-a-sha").await.unwrap_err();
    assert!(matches!(err, AttachmentError::InvalidSha256(_)));
}

#[test]
fn attachment_error_serializes_with_kind_and_detail_tags() {
    // The JS side's formatError() walks {kind, detail} chains. Lock the
    // wire shape in for each variant so a future refactor of the enum
    // can't silently change what the UI sees.
    let invalid = AttachmentError::InvalidSha256("oops".into());
    assert_eq!(
        serde_json::to_value(&invalid).unwrap(),
        serde_json::json!({"kind": "InvalidSha256", "detail": "oops"}),
    );

    let too_large = AttachmentError::TooLarge {
        size: 200 * 1024 * 1024,
        max: MAX_ATTACHMENT_BYTES,
    };
    assert_eq!(
        serde_json::to_value(&too_large).unwrap(),
        serde_json::json!({
            "kind": "TooLarge",
            "detail": {"size": 200 * 1024 * 1024_u64, "max": MAX_ATTACHMENT_BYTES},
        }),
    );

    let http = AttachmentError::Http(403);
    assert_eq!(
        serde_json::to_value(&http).unwrap(),
        serde_json::json!({"kind": "Http", "detail": 403}),
    );

    let network = AttachmentError::Network("connection refused".into());
    assert_eq!(
        serde_json::to_value(&network).unwrap(),
        serde_json::json!({"kind": "Network", "detail": "connection refused"}),
    );

    let timeout = AttachmentError::Timeout { seconds: 15 };
    assert_eq!(
        serde_json::to_value(&timeout).unwrap(),
        serde_json::json!({"kind": "Timeout", "detail": {"seconds": 15}}),
    );

    let read = AttachmentError::Read("stream closed".into());
    assert_eq!(
        serde_json::to_value(&read).unwrap(),
        serde_json::json!({"kind": "Read", "detail": "stream closed"}),
    );

    let write = AttachmentError::Write {
        path: "/tmp/x".into(),
        error: "permission denied".into(),
    };
    assert_eq!(
        serde_json::to_value(&write).unwrap(),
        serde_json::json!({
            "kind": "Write",
            "detail": {"path": "/tmp/x", "error": "permission denied"},
        }),
    );
}

#[test]
fn auth_pre_check_wraps_via_from_auth_error() {
    // The wire shape for an auth pre-check failure must remain a nested
    // {kind: "Auth", detail: {kind: "NotConnected"}} chain so the
    // existing formatError() walker handles it without change.
    let wrapped: AttachmentError = AuthError::NotConnected.into();
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

fn assert_network_with_cause(err: AttachmentError) {
    match err {
        // reqwest's own Display ends at "(<url>)"; the cause follows it.
        AttachmentError::Network(msg) => assert!(msg.contains("): "), "no cause in {msg:?}"),
        other => panic!("expected Network, got {other:?}"),
    }
}

#[tokio::test]
async fn download_reports_a_refused_send_with_its_cause() {
    let err = download_attachment(&refused_store(), VALID_SHA, PathBuf::from("/tmp/x"))
        .await
        .unwrap_err();
    assert_network_with_cause(err);
}

#[tokio::test]
async fn fetch_bytes_reports_a_refused_send_with_its_cause() {
    let err = fetch_attachment_bytes(&refused_store(), VALID_SHA)
        .await
        .unwrap_err();
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
        AttachmentError::send_failed(err),
        AttachmentError::Timeout {
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
    match AttachmentError::send_failed(err) {
        AttachmentError::Network(msg) => {
            assert!(msg.len() > plain.len(), "no cause in {msg:?}")
        }
        other => panic!("expected Network, got {other:?}"),
    }
}
