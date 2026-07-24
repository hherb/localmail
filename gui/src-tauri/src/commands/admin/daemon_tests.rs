//! Unit tests for `commands::admin::daemon`, split into their own file to keep
//! the implementation module under the size limit. Included via
//! `#[cfg(test)] #[path = ...] mod tests;`.

use super::*;
use crate::storage::keyring::{MemKeyring, Slot};

fn fake_store() -> KeyringStore {
    KeyringStore::with_backend(MemKeyring::new())
}

fn daemon_view_json() -> &'static str {
    r#"{
        "state": "external",
        "pid": null,
        "started_at": null,
        "supervise_daemon_externally": true,
        "heartbeats": [
            {"worker_kind": "idle", "account_id": "3", "state": "idle",
             "current_folder": "INBOX", "last_error_msg": null,
             "started_at": "2026-07-24T00:00:00+00:00",
             "last_heartbeat_at": "2026-07-24T00:00:05+00:00", "stale": false},
            {"worker_kind": "poll", "account_id": "3", "state": "polling",
             "current_folder": null, "last_error_msg": "boom",
             "started_at": "2026-07-24T00:00:00+00:00",
             "last_heartbeat_at": "2026-07-24T00:00:01+00:00", "stale": true}
        ],
        "recent_log": ["line one", "line two"]
    }"#
}

#[tokio::test]
async fn fetch_daemon_decodes_the_fused_view() {
    let mut server = mockito::Server::new_async().await;
    let m = server
        .mock("GET", "/v1/admin/daemon")
        .match_header("authorization", "Bearer tok")
        .with_status(200)
        .with_header("content-type", "application/json")
        .with_body(daemon_view_json())
        .create_async()
        .await;

    let client = Client::new();
    let base = format!("{}/", server.url());
    let view = fetch_daemon(&client, &base, "tok").await.unwrap();
    assert_eq!(view.state, "external");
    assert_eq!(view.pid, None);
    assert!(view.supervise_daemon_externally);
    assert_eq!(view.heartbeats.len(), 2);
    assert_eq!(view.heartbeats[0].account_id.as_deref(), Some("3"));
    assert_eq!(view.heartbeats[0].current_folder.as_deref(), Some("INBOX"));
    assert!(!view.heartbeats[0].stale);
    assert!(view.heartbeats[1].stale);
    assert_eq!(view.heartbeats[1].last_error_msg.as_deref(), Some("boom"));
    assert_eq!(view.recent_log, vec!["line one", "line two"]);
    m.assert_async().await;
}

#[tokio::test]
async fn fetch_daemon_maps_403_to_http_status() {
    let mut server = mockito::Server::new_async().await;
    let _m = server
        .mock("GET", "/v1/admin/daemon")
        .with_status(403)
        .with_body("admin privileges required")
        .create_async()
        .await;
    let client = Client::new();
    let base = format!("{}/", server.url());
    let err = fetch_daemon(&client, &base, "tok").await.unwrap_err();
    match err {
        AuthError::Http(crate::http::errors::HttpError::HttpStatus { status, .. }) => {
            assert_eq!(status, 403);
        }
        other => panic!("expected HttpStatus 403, got {other:?}"),
    }
}

#[tokio::test]
async fn post_lifecycle_start_decodes_the_transitional_status() {
    let mut server = mockito::Server::new_async().await;
    let m = server
        .mock("POST", "/v1/admin/daemon/start")
        .match_header("authorization", "Bearer tok")
        .with_status(202)
        .with_header("content-type", "application/json")
        .with_body(r#"{"state":"starting","pid":null,"started_at":null}"#)
        .create_async()
        .await;
    let client = Client::new();
    let base = format!("{}/", server.url());
    let status = post_lifecycle(&client, &base, "tok", "start").await.unwrap();
    assert_eq!(status.state, "starting");
    m.assert_async().await;
}

#[tokio::test]
async fn post_lifecycle_maps_409_busy_or_external() {
    let mut server = mockito::Server::new_async().await;
    let _m = server
        .mock("POST", "/v1/admin/daemon/restart")
        .with_status(409)
        .with_body(r#"{"detail":"a lifecycle operation is already in progress"}"#)
        .create_async()
        .await;
    let client = Client::new();
    let base = format!("{}/", server.url());
    let err = post_lifecycle(&client, &base, "tok", "restart")
        .await
        .unwrap_err();
    match err {
        AuthError::Http(crate::http::errors::HttpError::HttpStatus { status, .. }) => {
            assert_eq!(status, 409);
        }
        other => panic!("expected HttpStatus 409, got {other:?}"),
    }
}

#[tokio::test]
async fn post_reload_decodes_the_command_ack() {
    let mut server = mockito::Server::new_async().await;
    let m = server
        .mock("POST", "/v1/admin/daemon/reload")
        .match_header("authorization", "Bearer tok")
        .with_status(200)
        .with_header("content-type", "application/json")
        .with_body(r#"{"command_id":"42"}"#)
        .create_async()
        .await;
    let client = Client::new();
    let base = format!("{}/", server.url());
    let ack = post_reload(&client, &base, "tok").await.unwrap();
    assert_eq!(ack.command_id, "42");
    m.assert_async().await;
}

#[tokio::test]
async fn post_restart_sync_targets_the_account_route() {
    let mut server = mockito::Server::new_async().await;
    let m = server
        .mock("POST", "/v1/admin/accounts/7/restart-sync")
        .match_header("authorization", "Bearer tok")
        .with_status(200)
        .with_header("content-type", "application/json")
        .with_body(r#"{"command_id":"99"}"#)
        .create_async()
        .await;
    let client = Client::new();
    let base = format!("{}/", server.url());
    let ack = post_restart_sync(&client, &base, "tok", "7").await.unwrap();
    assert_eq!(ack.command_id, "99");
    m.assert_async().await;
}

#[tokio::test]
async fn post_restart_sync_maps_404_unknown_account() {
    let mut server = mockito::Server::new_async().await;
    let _m = server
        .mock("POST", "/v1/admin/accounts/7/restart-sync")
        .with_status(404)
        .with_body(r#"{"detail":"account not found"}"#)
        .create_async()
        .await;
    let client = Client::new();
    let base = format!("{}/", server.url());
    let err = post_restart_sync(&client, &base, "tok", "7").await.unwrap_err();
    match err {
        AuthError::Http(crate::http::errors::HttpError::HttpStatus { status, .. }) => {
            assert_eq!(status, 404);
        }
        other => panic!("expected HttpStatus 404, got {other:?}"),
    }
}

#[tokio::test]
async fn fetch_without_token_returns_not_logged_in() {
    let store = fake_store();
    store.put(Slot::ServerUrl, "https://localhost:8443/").unwrap();
    store.put(Slot::CertPin, "deadbeef").unwrap();
    let err = get_admin_daemon(&store).await.unwrap_err();
    assert!(matches!(err, AuthError::NotLoggedIn));
}

#[test]
fn lifecycle_op_accepts_the_three_ops_and_rejects_anything_else() {
    // Deserialised at the command boundary, so an unknown op can never reach
    // the URL path — a request for it is never built.
    assert!(matches!(
        serde_json::from_str::<LifecycleOp>("\"start\"").unwrap(),
        LifecycleOp::Start
    ));
    assert!(matches!(
        serde_json::from_str::<LifecycleOp>("\"stop\"").unwrap(),
        LifecycleOp::Stop
    ));
    assert!(matches!(
        serde_json::from_str::<LifecycleOp>("\"restart\"").unwrap(),
        LifecycleOp::Restart
    ));
    assert!(serde_json::from_str::<LifecycleOp>("\"reload\"").is_err());
    assert!(serde_json::from_str::<LifecycleOp>("\"START\"").is_err());
    assert!(serde_json::from_str::<LifecycleOp>("\"\"").is_err());
}
