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
    /// The client's total timeout ran out mid-body (#464). Kept apart from
    /// `Read` because the remedy differs: a slow link, not a broken server.
    Timeout,
    /// The transport failed mid-body; the text carries the cause chain.
    Read(String),
}

/// An error's message followed by each cause in its `source()` chain,
/// joined by `": "` (#464). reqwest's own `Display` writes only its kind and
/// URL, so a reset and a truncated body would otherwise read alike. A cause
/// whose text the message already contains is skipped.
pub fn error_chain(err: &(dyn std::error::Error + 'static)) -> String {
    let mut out = err.to_string();
    let mut cause = err.source();
    while let Some(e) = cause {
        let text = e.to_string();
        if !text.is_empty() && !out.contains(&text) {
            out.push_str(": ");
            out.push_str(&text);
        }
        cause = e.source();
    }
    out
}

/// How a failed reqwest call maps onto [`BodyError`]: a timeout, or the
/// failure with its causes.
fn transport_error(e: reqwest::Error) -> BodyError {
    if e.is_timeout() {
        BodyError::Timeout
    } else {
        BodyError::Read(error_chain(&e))
    }
}

/// Read the whole body of `resp`, refusing anything larger than `max` bytes.
/// The collected body never exceeds `max` bytes plus the one chunk in hand.
pub async fn read_capped(mut resp: reqwest::Response, max: u64) -> Result<Vec<u8>, BodyError> {
    let declared = resp.content_length();
    // Fail fast on an honest declaration, before reading a byte.
    if let Some(len) = declared {
        if len > max {
            return Err(BodyError::TooLarge { size: len });
        }
    }
    // shortcut: with no Content-Length the Vec grows by doubling, so its
    // capacity can reach ~2x `max`; fine at the GUI's 100 MiB caps.
    let mut body = Vec::with_capacity(declared.unwrap_or(0) as usize);
    while let Some(chunk) = resp.chunk().await.map_err(transport_error)? {
        let size = (body.len() + chunk.len()) as u64;
        // Checked before extending, so `body` never grows past `max`.
        if size > max {
            return Err(BodyError::TooLarge { size });
        }
        body.extend_from_slice(&chunk);
    }
    Ok(body)
}

/// Test support for the commands: a finite chunked body comfortably past
/// `max`, and how far past `max` a bounded read may report having got.
/// Finite so that a regression to buffer-then-check buffers a bounded amount
/// rather than running until the client timeout.
#[cfg(test)]
pub(crate) mod test_support {
    pub const MAX_OVERSHOOT: u64 = 1024 * 1024;

    pub fn write_past(max: u64) -> impl Fn(&mut dyn std::io::Write) -> std::io::Result<()> {
        move |w| {
            let chunk = [b'x'; 64 * 1024];
            let mut sent = 0;
            while sent <= max + 2 * MAX_OVERSHOOT {
                w.write_all(&chunk)?;
                sent += chunk.len() as u64;
            }
            Ok(())
        }
    }
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
    use super::test_support::MAX_OVERSHOOT;

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

    // Several chunks long, so a read that skipped the declared-length check
    // would report the first chunk's size instead of the declared one.
    #[tokio::test]
    async fn a_declared_length_over_the_cap_is_refused_with_that_length() {
        const DECLARED: usize = 4 * 1024 * 1024;
        let mut server = mockito::Server::new_async().await;
        let _m = server
            .mock("GET", "/big")
            .with_body(vec![b'x'; DECLARED])
            .create_async()
            .await;
        let body = read_capped(get(&server, "/big").await, 10).await;
        assert_eq!(
            body,
            Err(BodyError::TooLarge {
                size: DECLARED as u64
            })
        );
    }

    #[tokio::test]
    async fn a_declared_length_equal_to_the_cap_is_accepted() {
        let mut server = mockito::Server::new_async().await;
        let _m = server
            .mock("GET", "/declared-exact")
            .with_body(vec![b'x'; 100])
            .create_async()
            .await;
        let resp = get(&server, "/declared-exact").await;
        assert_eq!(resp.content_length(), Some(100));
        assert_eq!(read_capped(resp, 100).await.unwrap().len(), 100);
    }

    #[tokio::test]
    async fn a_transport_failure_mid_body_is_a_read_error() {
        // The body fails only once the client holds the response. Failing
        // at once races hyper's flush of the response head: on a fast host
        // the connection is aborted first and `send()` itself errors.
        let (headers_seen, wait) = std::sync::mpsc::channel::<()>();
        let wait = std::sync::Mutex::new(wait);
        let mut server = mockito::Server::new_async().await;
        let _m = server
            .mock("GET", "/broken")
            .with_chunked_body(move |w| {
                w.write_all(b"x")?;
                let _ = wait.lock().unwrap().recv();
                Err(std::io::Error::other("boom"))
            })
            .create_async()
            .await;
        let resp = get(&server, "/broken").await;
        headers_seen.send(()).unwrap();
        let body = read_capped(resp, SMALL_CAP).await;
        // The cause is what tells a reset from a timeout or a bad server;
        // reqwest's own Display stops at "error decoding response body".
        match body {
            Err(BodyError::Read(msg)) => assert!(msg.contains(": "), "no cause in {msg:?}"),
            other => panic!("expected Read, got {other:?}"),
        }
    }

    // #464: a body that stalls past the client's timeout is a Timeout, not
    // a generic read failure.
    #[tokio::test]
    async fn a_body_stalled_past_the_client_timeout_is_a_timeout() {
        const CLIENT_TIMEOUT: Duration = Duration::from_millis(300);
        const STALL: Duration = Duration::from_secs(5);
        let (done, wait) = std::sync::mpsc::channel::<()>();
        let wait = std::sync::Mutex::new(wait);
        let mut server = mockito::Server::new_async().await;
        let _m = server
            .mock("GET", "/slow")
            .with_chunked_body(move |w| {
                w.write_all(b"x")?;
                // Released by the test once it has its answer.
                let _ = wait.lock().unwrap().recv_timeout(STALL);
                Ok(())
            })
            .create_async()
            .await;
        let resp = reqwest::Client::builder()
            .timeout(CLIENT_TIMEOUT)
            .build()
            .unwrap()
            .get(format!("{}/slow", server.url()))
            .send()
            .await
            .unwrap();
        let body = read_capped(resp, SMALL_CAP).await;
        let _ = done.send(());
        assert_eq!(body, Err(BodyError::Timeout));
    }

    #[derive(Debug)]
    struct Layer(&'static str, Option<Box<Layer>>);
    impl std::fmt::Display for Layer {
        fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
            f.write_str(self.0)
        }
    }
    impl std::error::Error for Layer {
        fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
            self.1.as_deref().map(|e| e as _)
        }
    }

    #[test]
    fn error_chain_appends_every_cause() {
        let e = Layer(
            "outer",
            Some(Box::new(Layer("mid", Some(Box::new(Layer("root", None)))))),
        );
        assert_eq!(error_chain(&e), "outer: mid: root");
    }

    #[test]
    fn error_chain_skips_a_cause_the_message_already_says() {
        let e = Layer("read failed: reset", Some(Box::new(Layer("reset", None))));
        assert_eq!(error_chain(&e), "read failed: reset");
    }

    #[test]
    fn error_chain_of_a_causeless_error_is_its_message() {
        assert_eq!(error_chain(&Layer("alone", None)), "alone");
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
