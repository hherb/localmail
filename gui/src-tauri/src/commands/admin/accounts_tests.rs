//! Unit tests for `commands::admin::accounts`, split into their own file
//! to keep the implementation module comfortably under the size limit.
//! Included via `#[cfg(test)] #[path = ...] mod tests;`.

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

fn full_account_json() -> &'static str {
    r#"{"id":"9","name":"new","email_address":"n@e.w","auth_method":"password",
        "oauth_provider":null,"imap_host":"h","imap_port":993,
        "folder_allow":null,"folder_deny":null,"folder_deny_flags":null,
        "sync_enabled":true,"created_at":"2026-01-01T00:00:00+00:00",
        "updated_at":"2026-01-01T00:00:00+00:00"}"#
}

#[tokio::test]
async fn post_create_sends_the_input_and_decodes_201() {
    let mut server = mockito::Server::new_async().await;
    let m = server
        .mock("POST", "/v1/admin/accounts")
        .match_header("authorization", "Bearer tok")
        .match_body(mockito::Matcher::JsonString(
            r#"{"name":"new","email_address":"n@e.w","auth_method":"password","imap_host":"h","imap_port":993}"#
                .to_string(),
        ))
        .with_status(201)
        .with_header("content-type", "application/json")
        .with_body(full_account_json())
        .create_async()
        .await;

    let client = Client::new();
    let base = format!("{}/", server.url());
    let input = AdminAccountInput {
        name: "new".into(),
        email_address: "n@e.w".into(),
        auth_method: "password".into(),
        imap_host: Some("h".into()),
        imap_port: Some(993),
        oauth_provider: None,
        folder_allow: None,
        folder_deny: None,
        folder_deny_flags: None,
    };
    let got = post_create(&client, &base, "tok", &input).await.unwrap();
    assert_eq!(got.id, "9");
    m.assert_async().await;
}

#[tokio::test]
async fn patch_update_omits_unset_fields_entirely() {
    // update_account writes EVERY key present in the body, so a
    // serialized null would blank the column. Only sync_enabled is set
    // here, so the wire body must contain exactly that one key.
    let mut server = mockito::Server::new_async().await;
    let m = server
        .mock("PATCH", "/v1/admin/accounts/9")
        .match_body(mockito::Matcher::JsonString(
            r#"{"sync_enabled":false}"#.to_string(),
        ))
        .with_status(200)
        .with_header("content-type", "application/json")
        .with_body(full_account_json())
        .create_async()
        .await;

    let client = Client::new();
    let base = format!("{}/", server.url());
    let patch = AdminAccountPatch {
        sync_enabled: Some(false),
        ..AdminAccountPatch::default()
    };
    patch_update(&client, &base, "tok", "9", &patch).await.unwrap();
    m.assert_async().await;
}

#[tokio::test]
async fn patch_update_maps_400_validation_to_http_status() {
    let mut server = mockito::Server::new_async().await;
    let _m = server
        .mock("PATCH", "/v1/admin/accounts/9")
        .with_status(400)
        .with_body(r#"{"detail":"imap_host is required for live accounts"}"#)
        .create_async()
        .await;
    let client = Client::new();
    let base = format!("{}/", server.url());
    let patch = AdminAccountPatch {
        imap_host: Some(String::new()),
        ..AdminAccountPatch::default()
    };
    let err = patch_update(&client, &base, "tok", "9", &patch)
        .await
        .unwrap_err();
    match err {
        AuthError::Http(crate::http::errors::HttpError::HttpStatus { status, body }) => {
            assert_eq!(status, 400);
            assert!(body.contains("imap_host is required"));
        }
        other => panic!("expected HttpStatus 400, got {other:?}"),
    }
}

#[tokio::test]
async fn delete_appends_force_query_only_when_forcing() {
    let mut server = mockito::Server::new_async().await;
    let plain = server
        .mock("DELETE", "/v1/admin/accounts/9")
        .with_status(204)
        .create_async()
        .await;
    let forced = server
        .mock("DELETE", "/v1/admin/accounts/9?force=true")
        .with_status(204)
        .create_async()
        .await;

    let client = Client::new();
    let base = format!("{}/", server.url());
    delete_one(&client, &base, "tok", "9", false).await.unwrap();
    delete_one(&client, &base, "tok", "9", true).await.unwrap();
    plain.assert_async().await;
    forced.assert_async().await;
}

#[tokio::test]
async fn delete_maps_409_cascade_refusal() {
    let mut server = mockito::Server::new_async().await;
    let _m = server
        .mock("DELETE", "/v1/admin/accounts/9")
        .with_status(409)
        .with_body(r#"{"detail":"account 9 has 1200 messages"}"#)
        .create_async()
        .await;
    let client = Client::new();
    let base = format!("{}/", server.url());
    let err = delete_one(&client, &base, "tok", "9", false).await.unwrap_err();
    match err {
        AuthError::Http(crate::http::errors::HttpError::HttpStatus { status, .. }) => {
            assert_eq!(status, 409);
        }
        other => panic!("expected HttpStatus 409, got {other:?}"),
    }
}

#[tokio::test]
async fn post_password_sends_body_and_accepts_204() {
    let mut server = mockito::Server::new_async().await;
    let m = server
        .mock("POST", "/v1/admin/accounts/9/password")
        .match_header("authorization", "Bearer tok")
        .match_body(mockito::Matcher::JsonString(
            r#"{"password":"hunter2"}"#.to_string(),
        ))
        .with_status(204)
        .create_async()
        .await;
    let client = Client::new();
    let base = format!("{}/", server.url());
    post_password(&client, &base, "tok", "9", "hunter2").await.unwrap();
    m.assert_async().await;
}

#[tokio::test]
async fn post_test_connection_decodes_probed_folders() {
    let mut server = mockito::Server::new_async().await;
    let _m = server
        .mock("POST", "/v1/admin/accounts/9/test-connection")
        .with_status(200)
        .with_header("content-type", "application/json")
        .with_body(r#"{"folders":[{"name":"INBOX","flags":["\\HasNoChildren"]}]}"#)
        .create_async()
        .await;
    let client = Client::new();
    let base = format!("{}/", server.url());
    let got = post_test_connection(&client, &base, "tok", "9").await.unwrap();
    assert_eq!(got.folders.len(), 1);
    assert_eq!(got.folders[0].name, "INBOX");
    assert_eq!(got.folders[0].flags, vec!["\\HasNoChildren".to_string()]);
}

#[tokio::test]
async fn post_test_connection_maps_400_connect_failure() {
    let mut server = mockito::Server::new_async().await;
    let _m = server
        .mock("POST", "/v1/admin/accounts/9/test-connection")
        .with_status(400)
        .with_body(r#"{"detail":"[Errno 8] nodename nor servname provided"}"#)
        .create_async()
        .await;
    let client = Client::new();
    let base = format!("{}/", server.url());
    let err = post_test_connection(&client, &base, "tok", "9").await.unwrap_err();
    match err {
        AuthError::Http(crate::http::errors::HttpError::HttpStatus { status, .. }) => {
            assert_eq!(status, 400);
        }
        other => panic!("expected HttpStatus 400, got {other:?}"),
    }
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
