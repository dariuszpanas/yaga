use super::*;
use crate::agent_review::github::{
    CODEX_STATUS_CONTEXT,
    boundary::{ReviewBoundary, parse_boundary_description},
};
use std::collections::VecDeque;

const REPO: &str = "owner/repo";
const HEAD: &str = "381229bcd55aecb25ca7727966ee1e5d37ee9857";
const URL: &str = "https://example.org/owner/repo/actions/runs/123/attempts/1";
fn status(id: i64, state: &str, description: &str) -> Value {
    json!({"id":id,"state":state,"description":description,"target_url":URL,"context":CODEX_STATUS_CONTEXT,"creator":{"id":GITHUB_ACTIONS_USER_ID,"login":GITHUB_ACTIONS_LOGIN},"created_at":"2026-11-22T00:00:00Z"})
}
struct Script {
    pages: VecDeque<Value>,
    writes: Vec<Value>,
    bad_receipt: bool,
}
impl Script {
    fn pages(items: Vec<Value>) -> Self {
        Self {
            pages: items.into(),
            writes: vec![],
            bad_receipt: false,
        }
    }
}
impl RestApi for Script {
    fn get(&mut self, path: &str) -> Result<Value, String> {
        assert_eq!(
            path,
            format!("/repos/{REPO}/commits/{HEAD}/statuses?per_page=100&page=1")
        );
        self.pages.pop_front().ok_or("unexpected read".into())
    }
    fn post(&mut self, path: &str, payload: &Value) -> Result<Value, String> {
        assert_eq!(path, format!("/repos/{REPO}/statuses/{HEAD}"));
        self.writes.push(payload.clone());
        if self.bad_receipt && payload["state"] == "success" {
            return Err("lost response".into());
        }
        let mut response = status(
            1000 + self.writes.len() as i64,
            payload["state"].as_str().unwrap(),
            payload["description"].as_str().unwrap(),
        );
        response["target_url"] = payload["target_url"].clone();
        Ok(response)
    }
    fn require_success_tail(&self, _: usize, _: u64) -> Result<(), String> {
        Ok(())
    }
}
fn lease() -> StatusLease {
    StatusLease {
        status_id: 9,
        head_sha: HEAD.into(),
        target_url: URL.into(),
        description: "pending".into(),
        context: CODEX_STATUS_CONTEXT.into(),
    }
}
#[test]
fn terminal_does_not_displace_a_newer_owned_lease() {
    let mut api = Script::pages(vec![json!([
        status(10, "pending", "other"),
        status(9, "pending", "pending")
    ])]);
    assert!(
        publish_terminal(&mut api, REPO, &lease(), "success", "done")
            .unwrap()
            .is_none()
    );
    assert!(api.writes.is_empty());
}
#[test]
fn lost_terminal_receipt_repairs_pending_without_another_read() {
    let mut api = Script::pages(vec![json!([status(9, "pending", "pending")])]);
    api.bad_receipt = true;
    assert!(publish_terminal(&mut api, REPO, &lease(), "success", "done").is_err());
    assert_eq!(
        api.writes
            .iter()
            .map(|v| v["state"].as_str().unwrap())
            .collect::<Vec<_>>(),
        ["success", "pending"]
    );
}
#[test]
fn aggregate_page_preserves_terminal_and_cleanup_slots() {
    let mut records: Vec<_> = (100..198)
        .map(|i| {
            let mut s = status(i, "success", "unrelated");
            s["context"] = json!("other");
            s
        })
        .collect();
    records.insert(0, status(9, "pending", "pending"));
    let mut api = Script::pages(vec![json!(records)]);
    assert!(
        publish_terminal(&mut api, REPO, &lease(), "success", "done")
            .unwrap_err()
            .contains("repair slot")
    );
    assert!(api.writes.is_empty());
}
#[test]
fn boundary_encoding_carries_action_pr_base_timestamp_and_run_attempt() {
    let b = ReviewBoundary {
        run_id: 523,
        run_attempt: 2,
        action: "ready_for_review".into(),
        pull_request_number: 77,
        head_sha: HEAD.into(),
        base_sha: "a".repeat(40),
        occurred_at: 1_795_305_600_000_000,
    };
    let description = b.description("Codex pending").unwrap();
    let s = parse_status(&status(10, "pending", &description), "status").unwrap();
    assert_eq!(
        parse_boundary_description(&s, HEAD),
        Some((b.clone(), "Codex pending"))
    );
    assert!(parse_boundary_description(&s, "HEAD").is_none());
    let mut bad_action = b;
    bad_action.action = "unknown".into();
    assert_eq!(
        bad_action.description("Codex pending").unwrap_err(),
        "YAGA boundary action is invalid"
    );
}
