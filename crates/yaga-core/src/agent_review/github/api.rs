//! Narrow REST interface for the privileged gate. Pagination uses one complete page.
use serde_json::Value;

pub trait RestApi {
    fn get(&mut self, path: &str) -> Result<Value, String>;
    fn post(&mut self, path: &str, payload: &Value) -> Result<Value, String>;
    fn paginate(&mut self, path: &str) -> Result<Vec<Value>, String> {
        let separator = if path.contains('?') { '&' } else { '?' };
        let response = self.get(&format!("{path}{separator}per_page=100&page=1"))?;
        let Value::Array(page) = response else {
            return Err("GitHub API pagination returned an invalid page".into());
        };
        if !page.iter().all(Value::is_object) {
            return Err("GitHub API pagination returned an invalid page".into());
        }
        if page.len() > 100 {
            return Err("GitHub API pagination exceeded the record limit".into());
        }
        if page.len() == 100 {
            return Err("GitHub API pagination exceeded the page limit".into());
        }
        Ok(page)
    }
    fn require_success_tail(&self, count: usize, cleanup_margin_seconds: u64)
    -> Result<(), String>;
}

#[cfg(feature = "github-http")]
pub use http::GitHubRestApi;
#[cfg(feature = "github-http")]
mod http {
    use super::*;
    use std::{
        io::Read,
        time::{Duration, Instant},
    };
    use ureq::{
        Agent,
        http::{HeaderValue, Uri},
        tls::{RootCerts, TlsConfig},
    };
    pub struct GitHubRestApi {
        agent: Agent,
        authorization: HeaderValue,
        base: String,
        timeout: Duration,
        max_requests: usize,
        requests: usize,
        request_deadline: Option<Instant>,
        success_deadline: Option<Instant>,
    }
    impl GitHubRestApi {
        pub fn new(
            token: &str,
            base_url: &str,
            timeout: Duration,
            max_requests: usize,
            request_deadline: Option<Instant>,
            success_deadline: Option<Instant>,
        ) -> Result<Self, String> {
            if !(1..=4096).contains(&token.len()) || !token.bytes().all(|b| matches!(b, 33..=126)) {
                return Err("GITHUB_TOKEN is invalid".into());
            }
            let invalid = "GITHUB_API_URL must be an HTTPS origin or base path";
            if base_url.len() > 2048
                || base_url.contains(['\\', '#', '?'])
                || !base_url.bytes().all(|b| matches!(b, 33..=126))
            {
                return Err(invalid.into());
            }
            let uri: Uri = base_url.parse().map_err(|_| invalid)?;
            let authority = uri
                .authority()
                .filter(|a| !a.host().is_empty() && !a.as_str().contains('@'))
                .ok_or(invalid)?;
            let suffix = authority
                .as_str()
                .strip_prefix(authority.host())
                .ok_or(invalid)?;
            if uri.scheme_str() != Some("https")
                || (!suffix.is_empty() && !authority.port_u16().is_some_and(|port| port > 0))
            {
                return Err(invalid.into());
            }
            if !(Duration::from_secs(1)..=Duration::from_secs(30)).contains(&timeout) {
                return Err("request timeout must be between 1 and 30 seconds".into());
            }
            if !(1..=300).contains(&max_requests) {
                return Err("GitHub API request budget is invalid".into());
            }
            let config = Agent::config_builder()
                .https_only(true)
                .max_redirects(0)
                .max_redirects_will_error(false)
                .tls_config(
                    TlsConfig::builder()
                        .root_certs(RootCerts::PlatformVerifier)
                        .build(),
                )
                .timeout_global(Some(timeout))
                .http_status_as_error(false)
                .user_agent("yaga-action")
                .build();
            let mut authorization = HeaderValue::from_str(&format!("Bearer {token}"))
                .map_err(|_| "GITHUB_TOKEN is invalid")?;
            authorization.set_sensitive(true);
            Ok(Self {
                agent: config.into(),
                authorization,
                base: base_url.trim_end_matches('/').into(),
                timeout,
                max_requests,
                requests: 0,
                request_deadline,
                success_deadline,
            })
        }
        fn request(&mut self, path: &str, payload: Option<&Value>) -> Result<Value, String> {
            if !path.starts_with('/')
                || path.starts_with("//")
                || path.contains(['\\', '#'])
                || path.len() > 4096
                || !path.bytes().all(|b| matches!(b, 33..=126))
            {
                return Err("GitHub API path is invalid".into());
            }
            let body = payload.map(|p| crate::triage::ascii_json(&p.to_string()));
            if body.as_ref().is_some_and(|s| s.len() > 4096) {
                return Err("GitHub API request exceeded the byte limit".into());
            }
            if self.requests >= self.max_requests {
                return Err("GitHub API request budget exhausted".into());
            }
            if self
                .request_deadline
                .is_some_and(|deadline| Instant::now() + self.timeout > deadline)
            {
                return Err("GitHub API request no longer fits the polling deadline".into());
            }
            self.requests += 1;
            let url = format!("{}{path}", self.base);
            let method = if body.is_some() { "POST" } else { "GET" };
            // This is the sole send site. Never include credential headers or remote bodies in errors.
            let headers = |builder: &mut ureq::http::HeaderMap| {
                builder.insert("authorization", self.authorization.clone());
                builder.insert(
                    "accept",
                    HeaderValue::from_static("application/vnd.github+json"),
                );
                builder.insert(
                    "x-github-api-version",
                    HeaderValue::from_static("2022-11-28"),
                );
            };
            let response = if let Some(body) = body {
                let mut req = self.agent.post(&url);
                headers(
                    req.headers_mut()
                        .ok_or("GitHub API request could not be constructed")?,
                );
                req.header("content-type", "application/json")
                    .send(body.as_bytes())
            } else {
                let mut req = self.agent.get(&url);
                headers(
                    req.headers_mut()
                        .ok_or("GitHub API request could not be constructed")?,
                );
                req.call()
            };
            let mut response = response
                .map_err(|_| format!("GitHub API {method} failed before a complete response"))?;
            if !response.status().is_success() {
                return Err(format!(
                    "GitHub API {method} failed with HTTP {}",
                    response.status().as_u16()
                ));
            }
            let mut bytes = vec![];
            response
                .body_mut()
                .as_reader()
                .take(4 * 1024 * 1024 + 1)
                .read_to_end(&mut bytes)
                .map_err(|_| format!("GitHub API {method} failed while reading the response"))?;
            if bytes.len() > 4 * 1024 * 1024 {
                return Err("GitHub API response exceeded the byte limit".into());
            }
            crate::json::strict_value(&bytes).map_err(|_| "GitHub API returned invalid JSON".into())
        }
    }
    impl RestApi for GitHubRestApi {
        fn get(&mut self, path: &str) -> Result<Value, String> {
            self.request(path, None)
        }
        fn post(&mut self, path: &str, payload: &Value) -> Result<Value, String> {
            self.request(path, Some(payload))
        }
        fn require_success_tail(
            &self,
            count: usize,
            cleanup_margin_seconds: u64,
        ) -> Result<(), String> {
            if count == 0 {
                return Err("GitHub API request reserve is invalid".into());
            }
            if cleanup_margin_seconds == 0 {
                return Err("success cleanup margin is invalid".into());
            }
            if count > self.max_requests - self.requests {
                return Err("GitHub API request reserve is unavailable".into());
            }
            let deadline = self
                .success_deadline
                .ok_or("success deadline is unavailable")?;
            let needed = self
                .timeout
                .checked_mul(count as u32)
                .and_then(|d| d.checked_add(Duration::from_secs(cleanup_margin_seconds)));
            if !needed.is_some_and(|d| d <= deadline.saturating_duration_since(Instant::now())) {
                return Err("success validation tail no longer fits the job deadline".into());
            }
            Ok(())
        }
    }
}
