//! Thin async helpers around reqwest. All HTTPS calls go through here so the
//! TLS config (TOFU verifier) is set up exactly once per call.

use std::sync::Arc;
use std::time::Duration;

use reqwest::Client;
use rustls::ClientConfig;
use serde::de::DeserializeOwned;
use serde::Serialize;

use crate::http::errors::HttpError;
use crate::http::verifier::{TofuMode, TofuVerifier};

pub const REQUEST_TIMEOUT_SECS: u64 = 15;

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
        let body = resp.text().await.unwrap_or_default();
        return Err(HttpError::HttpStatus {
            status: status.as_u16(),
            body,
        });
    }
    resp.json::<T>()
        .await
        .map_err(|e| HttpError::from_reqwest(e, REQUEST_TIMEOUT_SECS))
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
        let body = resp.text().await.unwrap_or_default();
        return Err(HttpError::HttpStatus {
            status: status.as_u16(),
            body,
        });
    }
    resp.json::<T>()
        .await
        .map_err(|e| HttpError::from_reqwest(e, REQUEST_TIMEOUT_SECS))
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
        let body = resp.text().await.unwrap_or_default();
        return Err(HttpError::HttpStatus {
            status: status.as_u16(),
            body,
        });
    }
    Ok(())
}

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
        assert_eq!(got, Echo { message: "hi".into() });
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
        let err = http_get_json::<Echo>(&client, &url, None).await.unwrap_err();
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

    #[test]
    fn build_probe_client_returns_usable_client_and_verifier() {
        let p = build_probe_client().expect("probe client builds");
        assert!(p.verifier.captured_fingerprint().is_none());
    }
}
