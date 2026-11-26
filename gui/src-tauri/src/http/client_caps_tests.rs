//! Body-size caps on the JSON helpers (#218 follow-up). Each helper is driven
//! by a server: a finite chunked body (no `Content-Length`) just past the cap,
//! so a regression to `resp.json()` / `resp.text()` buffers a bounded amount
//! and reports the wrong error instead of hanging. Plain HTTP via mockito.

use super::*;
use crate::http::body::test_support::{write_past, MAX_OVERSHOOT};
use serde::Deserialize;

#[derive(Debug, Deserialize)]
struct Any {}

#[derive(Clone, Copy, Debug)]
enum Helper {
    GetJson,
    PostJson,
    PatchJson,
    PostEmpty,
    PostJsonNoResp,
    Delete,
}

const ALL: [Helper; 6] = [
    Helper::GetJson,
    Helper::PostJson,
    Helper::PatchJson,
    Helper::PostEmpty,
    Helper::PostJsonNoResp,
    Helper::Delete,
];

impl Helper {
    fn method(self) -> &'static str {
        match self {
            Helper::GetJson => "GET",
            Helper::PatchJson => "PATCH",
            Helper::Delete => "DELETE",
            _ => "POST",
        }
    }

    async fn call(self, url: &str) -> Result<(), HttpError> {
        let c = Client::new();
        let b = serde_json::json!({});
        match self {
            Helper::GetJson => http_get_json::<Any>(&c, url, None).await.map(drop),
            Helper::PostJson => http_post_json::<_, Any>(&c, url, &b, None).await.map(drop),
            Helper::PatchJson => http_patch_json::<_, Any>(&c, url, &b, None).await.map(drop),
            Helper::PostEmpty => http_post_empty(&c, url, None).await,
            Helper::PostJsonNoResp => http_post_json_no_resp(&c, url, &b, None).await,
            Helper::Delete => http_delete(&c, url, None).await,
        }
    }
}

// Pinned as literals: every other assertion compares a constant with itself.
#[test]
fn the_caps_are_what_the_docs_say() {
    assert_eq!(MAX_JSON_BYTES, 100 * 1024 * 1024);
    assert_eq!(MAX_ERROR_BODY_BYTES, 64 * 1024);
}

#[tokio::test]
async fn a_json_body_past_the_cap_is_refused_near_the_cap() {
    for helper in [Helper::GetJson, Helper::PostJson, Helper::PatchJson] {
        let mut server = mockito::Server::new_async().await;
        let _m = server
            .mock(helper.method(), "/big")
            .with_status(200)
            .with_chunked_body(write_past(MAX_JSON_BYTES))
            .create_async()
            .await;
        let err = helper
            .call(&format!("{}/big", server.url()))
            .await
            .unwrap_err();
        match err {
            HttpError::TooLarge { size, max } => {
                assert_eq!(max, MAX_JSON_BYTES, "{helper:?}");
                assert!(size > MAX_JSON_BYTES, "{helper:?}: {size}");
                assert!(size <= MAX_JSON_BYTES + MAX_OVERSHOOT, "{helper:?}: {size}");
            }
            other => panic!("{helper:?}: expected TooLarge, got {other:?}"),
        }
    }
}

#[tokio::test]
async fn an_error_body_past_its_cap_keeps_the_status() {
    for helper in ALL {
        let mut server = mockito::Server::new_async().await;
        let _m = server
            .mock(helper.method(), "/err")
            .with_status(500)
            .with_chunked_body(write_past(MAX_ERROR_BODY_BYTES))
            .create_async()
            .await;
        let err = helper
            .call(&format!("{}/err", server.url()))
            .await
            .unwrap_err();
        match err {
            HttpError::HttpStatus { status, body } => {
                assert_eq!(status, 500, "{helper:?}");
                assert_eq!(
                    body,
                    format!("<error body over {MAX_ERROR_BODY_BYTES} bytes>"),
                    "{helper:?}"
                );
            }
            other => panic!("{helper:?}: expected HttpStatus, got {other:?}"),
        }
    }
}

#[tokio::test]
async fn a_small_error_body_is_kept_verbatim() {
    for helper in ALL {
        let mut server = mockito::Server::new_async().await;
        let _m = server
            .mock(helper.method(), "/err")
            .with_status(409)
            .with_body(r#"{"title":"busy"}"#)
            .create_async()
            .await;
        match helper.call(&format!("{}/err", server.url())).await {
            Err(HttpError::HttpStatus { status, body }) => {
                assert_eq!((status, body.as_str()), (409, r#"{"title":"busy"}"#));
            }
            other => panic!("{helper:?}: expected HttpStatus, got {other:?}"),
        }
    }
}

#[tokio::test]
async fn malformed_json_is_still_a_decode_error() {
    for helper in [Helper::GetJson, Helper::PostJson, Helper::PatchJson] {
        let mut server = mockito::Server::new_async().await;
        let _m = server
            .mock(helper.method(), "/bad")
            .with_status(200)
            .with_body("not json")
            .create_async()
            .await;
        let err = helper
            .call(&format!("{}/bad", server.url()))
            .await
            .unwrap_err();
        assert!(matches!(err, HttpError::Decode(_)), "{helper:?}: {err:?}");
    }
}
