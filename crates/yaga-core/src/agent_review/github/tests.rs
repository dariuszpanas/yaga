use super::{api::RestApi, requests::*, *};
use serde_json::{Value, json};
use std::collections::VecDeque;
#[derive(Default)]
struct Mock {
    pages: VecDeque<Value>,
    posts: usize,
}
impl RestApi for Mock {
    fn get(&mut self, _: &str) -> Result<Value, String> {
        self.pages.pop_front().ok_or("unexpected read".into())
    }
    fn post(&mut self, _: &str, _: &Value) -> Result<Value, String> {
        self.posts += 1;
        Err("response lost after write".into())
    }
    fn require_success_tail(&self, _: usize, _: u64) -> Result<(), String> {
        Ok(())
    }
}
fn key() -> RequestKey {
    RequestKey {
        repository: "ex/project".into(),
        pull_request_number: 9,
        head_sha: "a".repeat(40),
        base_sha: "b".repeat(40),
        boundary_run_id: 8,
        source_run_id: 12,
        source_run_attempt: 1,
    }
}
fn comment(k: &RequestKey) -> Value {
    json!({"id":183,"user":{"login":GITHUB_ACTIONS_LOGIN,"id":GITHUB_ACTIONS_USER_ID},"performed_via_github_app":{"id":15368,"slug":"github-actions"},
    "created_at":"2026-11-01T12:00:00Z","updated_at":"2026-11-01T12:00:00Z","body":k.body_for("request").unwrap()})
}
#[test]
fn losing_a_request_response_never_duplicates_the_write() {
    let k = key();
    let mut api = Mock {
        pages: [json!([]), json!([]), json!([comment(&k)])].into(),
        posts: 0,
    };
    let request = ensure_authorization(&mut api, &k, "request", &mut |_| Ok(true)).unwrap();
    assert_eq!(request.comment_id, 183);
    assert_eq!(api.posts, 1);
}
#[test]
fn last_moment_boundary_change_prevents_request() {
    let mut api = Mock {
        pages: [json!([]), json!([])].into(),
        posts: 0,
    };
    assert!(ensure_authorization(&mut api, &key(), "request", &mut |_| Ok(false)).is_err());
    assert_eq!(api.posts, 0);
}
#[test]
fn ignores_other_authors_and_rejects_incomplete_histories() {
    let k = key();
    let mut forged = comment(&k);
    forged["user"]["id"] = 23.into();
    let mut api = Mock {
        pages: [json!([forged]), json!(vec![json!({}); 100])].into(),
        posts: 0,
    };
    assert!(find_authorization(&mut api, &k).unwrap().request.is_none());
    assert!(find_authorization(&mut api, &k).is_err());
}
#[cfg(feature = "github-http")]
#[test]
fn client_rejects_redirect_targets_and_exhausted_budgets_before_transport() {
    use api::GitHubRestApi;
    use std::time::{Duration, Instant};
    for base in [
        "http://example.org",
        "https://user@example.org",
        "https://example.org?token=x",
        "https://example.org/#frag",
        "https://example.org:invalid",
        "https://example.org:65536",
        "https://example.org:0",
    ] {
        assert!(GitHubRestApi::new("test", base, Duration::from_secs(15), 1, None, None).is_err());
    }
    let mut api = GitHubRestApi::new(
        "dummy",
        "https://example.invalid",
        Duration::from_secs(15),
        1,
        Some(Instant::now()),
        None,
    )
    .unwrap();
    assert!(
        api.get("//other.example/path")
            .unwrap_err()
            .contains("path")
    );
    assert!(
        api.get("/repos/ex/project")
            .unwrap_err()
            .contains("deadline")
    );
    assert!(api.require_success_tail(48, 60).is_err());
}

#[test]
fn authorization_compares_iso_timestamps_at_microsecond_precision() {
    let expected = timestamp(&json!("2026-11-22T08:04:05.123456Z"), "time").unwrap();
    for equivalent in [
        "20261122T080405,1234567Z",
        "2026-W47-7T09:04:05.123456+01:00",
    ] {
        assert_eq!(timestamp(&json!(equivalent), "time").unwrap(), expected);
    }
    assert_eq!(
        timestamp(&json!("2026-11-22T08:04:05"), "time").unwrap_err(),
        "time must include a timezone"
    );
}
