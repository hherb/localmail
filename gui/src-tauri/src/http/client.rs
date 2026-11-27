//! Thin async helpers around reqwest. All HTTPS calls go through here so the
//! TLS config (TOFU verifier) is set up exactly once per call.

use std::sync::Arc;
use std::time::Duration;

use reqwest::Client;
use rustls::ClientConfig;
use serde::de::DeserializeOwned;
use serde::Serialize;

use crate::http::body::{read_capped, BodyError};
use crate::http::errors::HttpError;
use crate::http::verifier::{TofuMode, TofuVerifier};

pub const REQUEST_TIMEOUT_SECS: u64 = 15;

/// Ceiling on a JSON response body. The largest is a message detail (bodies,
/// sanitized HTML, headers, JSON-escaped), which can exceed its raw message,
/// so this is a policy matching the raw-message cap, not derived from it: a
/// larger detail fails with `TooLarge`.
pub const MAX_JSON_BYTES: u64 = 100 * 1024 * 1024;

/// Ceiling on an error response's body, which is only kept for the message
/// (a problem+json document is a few hundred bytes).
pub const MAX_ERROR_BODY_BYTES: u64 = 64 * 1024;

pub struct ProbeClient {
    pub client: Client,
    pub verifier: Arc<TofuVerifier>,
}

pub fn build_probe_client() -> Result<ProbeClient, HttpError> {
    let verifier = TofuVerifier::new(TofuMode::Probe);
    let client = build_reqwest_with_verifier(verifier.clone())?;
    Ok(ProbeClient { client, verifier })
}

pub fn build_pinned_client(expected_hex: &str) -> Result<Client, HttpError> {
    let verifier = TofuVerifier::new(TofuMode::Pinned {
        expected_hex: expected_hex.to_string(),
    });
    build_reqwest_with_verifier(verifier)
}

fn build_reqwest_with_verifier(verifier: Arc<TofuVerifier>) -> Result<Client, HttpError> {
    let crypto_provider = rustls::crypto::ring::default_provider();
    let tls_config = ClientConfig::builder_with_provider(crypto_provider.into())
        .with_safe_default_protocol_versions()
        .map_err(|e| HttpError::Network(format!("rustls config: {e}")))?
        .dangerous()
        .with_custom_certificate_verifier(verifier)
        .with_no_client_auth();

    Client::builder()
        .use_preconfigured_tls(tls_config)
        .timeout(Duration::from_secs(REQUEST_TIMEOUT_SECS))
        .build()
        .map_err(|e| HttpError::from_reqwest(e, REQUEST_TIMEOUT_SECS))
}

/// Decode a success body as JSON, reading at most `MAX_JSON_BYTES` of it
/// rather than buffering whatever the server sends (`resp.json()`).
async fn decode_json<T: DeserializeOwned>(resp: reqwest::Response) -> Result<T, HttpError> {
    let body = read_capped(resp, MAX_JSON_BYTES)
        .await
        .map_err(|e| match e {
            BodyError::TooLarge { size } => HttpError::TooLarge {
                size,
                max: MAX_JSON_BYTES,
            },
            BodyError::Timeout => HttpError::Timeout {
                seconds: REQUEST_TIMEOUT_SECS,
            },
            BodyError::Read(msg) => HttpError::Network(msg),
        })?;
    serde_json::from_slice(&body).map_err(|e| HttpError::Decode(e.to_string()))
}

/// A non-success response as `HttpStatus`. The status is what callers branch
/// on, so a body that is too large or unreadable is named in the text rather
/// than failing the call.
async fn status_error(resp: reqwest::Response) -> HttpError {
    let status = resp.status().as_u16();
    let body = match read_capped(resp, MAX_ERROR_BODY_BYTES).await {
        Ok(bytes) => String::from_utf8_lossy(&bytes).into_owned(),
        Err(BodyError::TooLarge { .. }) => {
            format!("<error body over {MAX_ERROR_BODY_BYTES} bytes>")
        }
        Err(BodyError::Timeout) => "<error body timed out>".to_string(),
        Err(BodyError::Read(msg)) => format!("<error body unreadable: {msg}>"),
    };
    HttpError::HttpStatus { status, body }
}

pub async fn http_get_json<T: DeserializeOwned>(
    client: &Client,
    url: &str,
    bearer: Option<&str>,
) -> Result<T, HttpError> {
    let mut req = client.get(url);
    if let Some(tok) = bearer {
        req = req.bearer_auth(tok);
    }
    let resp = req
        .send()
        .await
        .map_err(|e| HttpError::from_reqwest(e, REQUEST_TIMEOUT_SECS))?;
    let status = resp.status();
    if !status.is_success() {
        return Err(status_error(resp).await);
    }
    decode_json(resp).await
}

pub async fn http_post_json<B: Serialize, T: DeserializeOwned>(
    client: &Client,
    url: &str,
    body: &B,
    bearer: Option<&str>,
) -> Result<T, HttpError> {
    let mut req = client.post(url).json(body);
    if let Some(tok) = bearer {
        req = req.bearer_auth(tok);
    }
    let resp = req
        .send()
        .await
        .map_err(|e| HttpError::from_reqwest(e, REQUEST_TIMEOUT_SECS))?;
    let status = resp.status();
    if !status.is_success() {
        return Err(status_error(resp).await);
    }
    decode_json(resp).await
}

pub async fn http_post_empty(
    client: &Client,
    url: &str,
    bearer: Option<&str>,
) -> Result<(), HttpError> {
    let mut req = client.post(url);
    if let Some(tok) = bearer {
        req = req.bearer_auth(tok);
    }
    let resp = req
        .send()
        .await
        .map_err(|e| HttpError::from_reqwest(e, REQUEST_TIMEOUT_SECS))?;
    let status = resp.status();
    if !status.is_success() && status.as_u16() != 204 {
        return Err(status_error(resp).await);
    }
    Ok(())
}

// POST a JSON body and accept either 2xx with no response body or 204. Mirrors
// http_post_json's error classification (HttpStatus preserves the status code
// so callers can branch on 401 vs 500) while not requiring a JSON response.
pub async fn http_post_json_no_resp<B: Serialize>(
    client: &Client,
    url: &str,
    body: &B,
    bearer: Option<&str>,
) -> Result<(), HttpError> {
    let mut req = client.post(url).json(body);
    if let Some(tok) = bearer {
        req = req.bearer_auth(tok);
    }
    let resp = req
        .send()
        .await
        .map_err(|e| HttpError::from_reqwest(e, REQUEST_TIMEOUT_SECS))?;
    let status = resp.status();
    if !status.is_success() && status.as_u16() != 204 {
        return Err(status_error(resp).await);
    }
    Ok(())
}

pub async fn http_patch_json<B: Serialize, T: DeserializeOwned>(
    client: &Client,
    url: &str,
    body: &B,
    bearer: Option<&str>,
) -> Result<T, HttpError> {
    let mut req = client.patch(url).json(body);
    if let Some(tok) = bearer {
        req = req.bearer_auth(tok);
    }
    let resp = req
        .send()
        .await
        .map_err(|e| HttpError::from_reqwest(e, REQUEST_TIMEOUT_SECS))?;
    let status = resp.status();
    if !status.is_success() {
        return Err(status_error(resp).await);
    }
    decode_json(resp).await
}

pub async fn http_delete(
    client: &Client,
    url: &str,
    bearer: Option<&str>,
) -> Result<(), HttpError> {
    let mut req = client.delete(url);
    if let Some(tok) = bearer {
        req = req.bearer_auth(tok);
    }
    let resp = req
        .send()
        .await
        .map_err(|e| HttpError::from_reqwest(e, REQUEST_TIMEOUT_SECS))?;
    let status = resp.status();
    if !status.is_success() {
        return Err(status_error(resp).await);
    }
    Ok(())
}

#[cfg(test)]
#[path = "client_caps_tests.rs"]
mod caps_tests;

#[cfg(test)]
mod tests {
    use super::*;
    use serde::Deserialize;

    #[derive(Debug, Deserialize, PartialEq)]
    struct Echo {
        message: String,
    }

    #[tokio::test]
    async fn http_get_json_decodes_response() {
        let mut server = mockito::Server::new_async().await;
        let _m = server
            .mock("GET", "/echo")
            .with_status(200)
            .with_header("content-type", "application/json")
            .with_body(r#"{"message":"hi"}"#)
            .create_async()
            .await;

        // mockito is HTTP (not HTTPS) — use a plain reqwest::Client here.
        let client = Client::new();
        let url = format!("{}/echo", server.url());
        let got: Echo = http_get_json(&client, &url, None).await.unwrap();
        assert_eq!(
            got,
            Echo {
                message: "hi".into()
            }
        );
    }

    #[tokio::test]
    async fn http_get_json_maps_4xx_to_http_status() {
        let mut server = mockito::Server::new_async().await;
        let _m = server
            .mock("GET", "/forbidden")
            .with_status(403)
            .with_body("nope")
            .create_async()
            .await;
        let client = Client::new();
        let url = format!("{}/forbidden", server.url());
        let err = http_get_json::<Echo>(&client, &url, None)
            .await
            .unwrap_err();
        match err {
            HttpError::HttpStatus { status, body } => {
                assert_eq!(status, 403);
                assert_eq!(body, "nope");
            }
            other => panic!("expected HttpStatus, got {:?}", other),
        }
    }

    #[tokio::test]
    async fn http_get_json_attaches_bearer_header() {
        let mut server = mockito::Server::new_async().await;
        let _m = server
            .mock("GET", "/ping")
            .match_header("authorization", "Bearer abc123")
            .with_status(200)
            .with_body(r#"{"message":"ok"}"#)
            .create_async()
            .await;
        let client = Client::new();
        let url = format!("{}/ping", server.url());
        let got: Echo = http_get_json(&client, &url, Some("abc123")).await.unwrap();
        assert_eq!(got.message, "ok");
    }

    #[tokio::test]
    async fn http_post_empty_accepts_204() {
        let mut server = mockito::Server::new_async().await;
        let _m = server
            .mock("POST", "/logout")
            .with_status(204)
            .create_async()
            .await;
        let client = Client::new();
        let url = format!("{}/logout", server.url());
        http_post_empty(&client, &url, Some("tok")).await.unwrap();
    }

    #[tokio::test]
    async fn http_patch_json_sends_body_and_decodes_response() {
        let mut server = mockito::Server::new_async().await;
        let m = server
            .mock("PATCH", "/thing")
            .match_header("authorization", "Bearer tok")
            .match_body(mockito::Matcher::JsonString(
                r#"{"message":"set"}"#.to_string(),
            ))
            .with_status(200)
            .with_header("content-type", "application/json")
            .with_body(r#"{"message":"set"}"#)
            .create_async()
            .await;

        let client = Client::new();
        let url = format!("{}/thing", server.url());
        let body = serde_json::json!({ "message": "set" });
        let got: Echo = http_patch_json(&client, &url, &body, Some("tok"))
            .await
            .unwrap();
        assert_eq!(got.message, "set");
        m.assert_async().await;
    }

    #[tokio::test]
    async fn http_delete_accepts_204_and_maps_409() {
        let mut server = mockito::Server::new_async().await;
        let _ok = server
            .mock("DELETE", "/gone")
            .with_status(204)
            .create_async()
            .await;
        let client = Client::new();
        http_delete(&client, &format!("{}/gone", server.url()), Some("tok"))
            .await
            .expect("204 should succeed");

        let _conflict = server
            .mock("DELETE", "/busy")
            .with_status(409)
            .with_body("account has messages")
            .create_async()
            .await;
        let err = http_delete(&client, &format!("{}/busy", server.url()), Some("tok"))
            .await
            .unwrap_err();
        match err {
            HttpError::HttpStatus { status, body } => {
                assert_eq!(status, 409);
                assert_eq!(body, "account has messages");
            }
            other => panic!("expected HttpStatus 409, got {other:?}"),
        }
    }

    #[test]
    fn build_probe_client_returns_usable_client_and_verifier() {
        let p = build_probe_client().expect("probe client builds");
        assert!(p.verifier.captured_fingerprint().is_none());
    }
}
