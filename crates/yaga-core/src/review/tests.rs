#[cfg(feature = "process")]
use super::remote;
use super::{Snapshot, evaluate, in_progress};
use crate::agent_review::{Outcome, Policy};
#[cfg(feature = "process")]
use crate::gh::PullRequest;
use serde_json::{Value, json};
use std::path::Path;

fn policy() -> Policy {
    Policy::parse(
        b"[agent-review]\nversion=1\nrequired=['codex']\n",
        Path::new("review.toml"),
    )
    .unwrap()
}
fn document(policy: &Policy) -> Value {
    json!({"version":1,"number":7,"head_sha":"a".repeat(40),"base_sha":"b".repeat(40),"plan_digest":policy.digest()})
}
fn summary(doc: &Value, policy: &Policy) -> (Outcome, String) {
    let snapshot = Snapshot::parse(doc, "snapshot").unwrap();
    let mut aggregate = evaluate(policy, &snapshot).unwrap();
    (
        aggregate.state,
        aggregate.results.remove(0).summary.unwrap(),
    )
}
const CODEX: &str = "chatgpt-codex-connector[bot]";

#[test]
fn head_bound_approval_survives_old_reviews_and_rejects_an_impostor() {
    let p = policy();
    let mut doc = document(&p);
    doc["reviews"] = json!([
        {"id":3,"author_login":CODEX,"author_id":199175422,"state":"APPROVED","submitted_at":"2026-11-01T03:00:00Z","commit_id":doc["head_sha"]},
        {"id":9,"author_login":CODEX,"author_id":199175422,"state":"CHANGES_REQUESTED","submitted_at":"2026-11-02T04:00:00Z","commit_id":doc["base_sha"]}
    ]);
    assert_eq!(
        summary(&doc, &p),
        (
            Outcome::Passed,
            format!("{CODEX} approved head aaaaaaaaaaaa")
        )
    );
    doc["reviews"][0]["author_id"] = json!(11);
    let error = evaluate(&p, &Snapshot::parse(&doc, "snapshot").unwrap()).unwrap_err();
    assert_eq!(
        error.to_string(),
        format!("review for {CODEX} has an unexpected author_id")
    );
}

#[test]
fn unbound_reactions_require_an_explicit_head_time() {
    let p = policy();
    let mut doc = document(&p);
    doc["reactions"] = json!([{"id":1,"author_login":CODEX,"author_id":199175422,"content":"+1","created_at":"2026-11-02T18:00:01Z"}]);
    assert_eq!(summary(&doc, &p).0, Outcome::Pending);
    // Week date with timezone offset and a basic timestamp represent the same second.
    doc["head_committed_at"] = json!("2026-W45-1T19:00:01+01");
    assert_eq!(summary(&doc, &p).0, Outcome::Pending);
    doc["head_committed_at"] = json!("20261102T180000Z");
    assert_eq!(summary(&doc, &p).0, Outcome::Passed);
}

#[test]
fn requested_reviewers_win_over_a_head_bound_completion_for_polling() {
    let p = policy();
    let mut doc = document(&p);
    doc["requested_reviewers"] = json!([{"author_login":CODEX,"author_id":199175422}]);
    doc["reviews"] = json!([{"id":3,"author_login":CODEX,"author_id":199175422,"state":"APPROVED","submitted_at":"2026-11-01T03:00:00Z","commit_id":doc["head_sha"]}]);
    let snapshot = Snapshot::parse(&doc, "snapshot").unwrap();
    let report = evaluate(&p, &snapshot).unwrap();
    assert!(in_progress(&snapshot, &report, &p));
    doc["draft"] = json!(true);
    let draft = Snapshot::parse(&doc, "snapshot").unwrap();
    assert!(!in_progress(&draft, &report, &p));
}

#[test]
fn summaries_fail_closed_on_ambiguous_markers_and_contradictory_flags() {
    let p = policy();
    let mut doc = document(&p);
    let marker = format!(
        "<!-- codex-review-state:v1 {} -->",
        json!({"code_review":{"commit_sha":doc["head_sha"],"status":"completed","has_any_findings":false,"has_unresolved_findings":true}})
    );
    doc["issue_comments"] = json!([{"id":1,"author_login":CODEX,"author_id":199175422,"app_id":1144995,"created_at":"2026-11-02T18:00:00Z","body":marker}]);
    let check = |d: &Value| {
        evaluate(&p, &Snapshot::parse(d, "fixture").unwrap())
            .unwrap_err()
            .to_string()
    };
    assert_eq!(
        check(&doc),
        "issue comment 1 code_review finding flags are contradictory"
    );
    doc["issue_comments"][0]["body"] = json!(marker.repeat(2));
    assert_eq!(
        check(&doc),
        "issue comment 1 contains multiple codex-review-state markers"
    );
}

#[test]
fn rejects_duplicate_json_incomplete_pages_and_plan_changes() {
    assert!(
        Snapshot::from_bytes(br#"{"version":1,"version":1}"#, "input")
            .unwrap_err()
            .contains("not valid JSON")
    );
    let p = policy();
    let mut doc = document(&p);
    doc["review_comments"] = json!(vec![Value::Null; 100]);
    assert!(
        Snapshot::parse(&doc, "input")
            .unwrap_err()
            .contains("page is incomplete")
    );
    let mut doc = document(&p);
    doc["plan_digest"] = json!("0".repeat(64));
    assert_eq!(
        evaluate(&p, &Snapshot::parse(&doc, "input").unwrap())
            .unwrap_err()
            .kind(),
        "configuration"
    );
}

#[cfg(feature = "process")]
#[test]
fn remote_collection_authenticates_identity_before_reading_evidence() {
    let p = policy();
    let reference = PullRequest::parse("7", Some("owner/repo")).unwrap();
    let mut reads = Vec::new();
    let result = remote::collect(&reference, &p.digest(), false, &mut |endpoint| {
        reads.push(endpoint.to_owned());
        Ok(json!({"number":7,"html_url":"https://github.com/other/repo/pull/7"}))
    });
    assert_eq!(
        result.unwrap_err(),
        "gh returned a different pull-request identity"
    );
    assert_eq!(reads, ["repos/owner/repo/pulls/7"]);
}

#[cfg(feature = "process")]
#[test]
fn snapshot_load_resolves_symlinks_and_rejects_directories() {
    let dir = tempfile::tempdir().unwrap();
    let p = policy();
    let snap = dir.path().join("snap.json");
    std::fs::write(&snap, document(&p).to_string()).unwrap();
    assert_eq!(Snapshot::load(&snap).unwrap().number(), 7);
    assert!(Snapshot::load(dir.path()).is_err());
    #[cfg(unix)]
    {
        let link = dir.path().join("link.json");
        std::os::unix::fs::symlink(&snap, &link).unwrap();
        assert_eq!(Snapshot::load(&link).unwrap().number(), 7);
    }
}
