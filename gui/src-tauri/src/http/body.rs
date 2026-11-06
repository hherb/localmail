//! Bounded response-body reads (#218).
//!
//! The download commands enforce a byte ceiling. Checking `Content-Length`
//! alone is not enough: a chunked response (or a proxy that strips the
//! header) has none, and `resp.bytes().await` then buffers the whole body
//! before any post-hoc length check runs — so an endless or huge response
//! could exhaust memory. [`read_capped`] reads chunk by chunk and stops as
//! soon as the running total passes the ceiling.

/// Why a capped read failed. Callers map this onto their own error enum.
#[derive(Debug, PartialEq, Eq)]
pub enum BodyError {
    /// The body exceeds `max`. `size` is the declared `Content-Length` when
    /// the server sent one, else the bytes received when the read stopped —
    /// a lower bound on the real size, which is unknown by construction.
    TooLarge { size: u64 },
    /// The transport failed mid-body.
    Read(String),
}

/// Read the whole body of `resp`, refusing anything larger than `max` bytes
/// without ever holding more than `max` plus one chunk in memory.
pub async fn read_capped(mut resp: reqwest::Response, max: u64) -> Result<Vec<u8>, BodyError> {
    // Fail fast on an honest declaration, before reading a byte.
    if let Some(len) = resp.content_length() {
        if len > max {
            return Err(BodyError::TooLarge { size: len });
        }
    }
    let mut body = Vec::new();
    while let Some(chunk) = resp
        .chunk()
        .await
        .map_err(|e| BodyError::Read(e.to_string()))?
    {
        let size = (body.len() + chunk.len()) as u64;
        // Checked before extending, so `body` never grows past `max`.
        if size > max {
            return Err(BodyError::TooLarge { size });
        }
        body.extend_from_slice(&chunk);
    }
    Ok(body)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::Duration;

    // Long enough for a real read on a loaded machine; short enough that a
    // regression to buffer-then-check (which never returns on an endless
    // body) fails the test instead of hanging the suite.
    const ENDLESS_BODY_TIMEOUT: Duration = Duration::from_secs(20);
    const SMALL_CAP: u64 = 64 * 1024;
    const CHUNK: [u8; 8192] = [b'x'; 8192];
    // hyper may hand back several written chunks as one read, so "stopped
    // near the cap" is asserted with slack rather than to the byte.
    const MAX_OVERSHOOT: u64 = 1024 * 1024;

    async fn get(server: &mockito::Server, path: &str) -> reqwest::Response {
        reqwest::Client::new()
            .get(format!("{}{path}", server.url()))
            .send()
            .await
            .unwrap()
    }

    #[tokio::test]
    async fn a_body_within_the_cap_is_returned_whole() {
        let mut server = mockito::Server::new_async().await;
        let _m = server
            .mock("GET", "/ok")
            .with_body("hello world")
            .create_async()
            .await;
        let body = read_capped(get(&server, "/ok").await, SMALL_CAP).await;
        assert_eq!(body.unwrap(), b"hello world");
    }

    #[tokio::test]
    async fn a_body_exactly_at_the_cap_is_accepted() {
        let mut server = mockito::Server::new_async().await;
        let _m = server
            .mock("GET", "/exact")
            .with_chunked_body(|w| w.write_all(&CHUNK))
            .create_async()
            .await;
        let body = read_capped(get(&server, "/exact").await, CHUNK.len() as u64).await;
        assert_eq!(body.unwrap().len(), CHUNK.len());
    }

    #[tokio::test]
    async fn a_declared_length_over_the_cap_is_refused_with_that_length() {
        let mut server = mockito::Server::new_async().await;
        let _m = server
            .mock("GET", "/big")
            .with_body(vec![b'x'; 100])
            .create_async()
            .await;
        let body = read_capped(get(&server, "/big").await, 10).await;
        assert_eq!(body, Err(BodyError::TooLarge { size: 100 }));
    }

    #[tokio::test]
    async fn a_chunked_body_one_byte_over_the_cap_is_refused() {
        let mut server = mockito::Server::new_async().await;
        let _m = server
            .mock("GET", "/over")
            .with_chunked_body(|w| {
                w.write_all(&CHUNK)?;
                w.write_all(b"!")
            })
            .create_async()
            .await;
        let body = read_capped(get(&server, "/over").await, CHUNK.len() as u64).await;
        assert!(matches!(body, Err(BodyError::TooLarge { size }) if size > CHUNK.len() as u64));
    }

    // The #218 defect: no Content-Length and a body that never ends. A read
    // that buffers before checking never returns; a bounded one stops just
    // past the cap.
    #[tokio::test]
    async fn an_endless_chunked_body_is_refused_without_buffering_it() {
        let mut server = mockito::Server::new_async().await;
        let _m = server
            .mock("GET", "/endless")
            .with_chunked_body(|w| loop {
                w.write_all(&CHUNK)?;
            })
            .create_async()
            .await;
        let resp = get(&server, "/endless").await;
        assert_eq!(
            resp.content_length(),
            None,
            "fixture must omit Content-Length"
        );
        let body = tokio::time::timeout(ENDLESS_BODY_TIMEOUT, read_capped(resp, SMALL_CAP))
            .await
            .expect("read_capped must stop at the cap, not buffer an endless body");
        match body {
            Err(BodyError::TooLarge { size }) => {
                assert!(size > SMALL_CAP);
                assert!(size <= SMALL_CAP + MAX_OVERSHOOT, "read {size} bytes");
            }
            other => panic!("expected TooLarge, got {other:?}"),
        }
    }
}
