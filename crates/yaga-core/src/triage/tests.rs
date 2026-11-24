use super::*;
use serde_json::json;
use sha2::{Digest, Sha256};

fn policy() -> TriagePolicy {
    TriagePolicy::parse(b"triage-policy-version=1\nhost='github.com'\nrepository-id=100\nmaintainer-user-ids=[10]\nduplicate-candidates=0").unwrap()
}
fn data() -> Value {
    json!({
        "version":1,"host":"github.com","repository":"example/project","repository_id":100,"number":23,
        "state":"open","draft":false,"head_sha":"a".repeat(40),"base_sha":"b".repeat(40),"base_ref":"main","head_tree":"c".repeat(40),
        "head_repository_id":101,"head_repository_fork":true,"author_id":30,"author_login":"external","author_type":"User",
        "changed_files":0,"files":[],"reviews":[],"candidates":[],"candidate_limit":0,"candidates_examined":0
    })
}
fn check(policy: &TriagePolicy, doc: &Value) -> Value {
    let snapshot = Snapshot::decode(&serde_json::to_vec(doc).unwrap()).unwrap();
    evaluate(policy, &snapshot).unwrap()
}
#[test]
fn latest_human_review_must_acknowledge_current_head_base_and_policy() {
    let p = policy();
    let mut doc = data();
    let initial = check(&p, &doc);
    let a = &initial["admission"];
    assert_eq!(a["decision"], "hold");
    let canonical = format!(
        r#"{{"base_ref":"main","base_sha":"{}","head_sha":"{}","host":"github.com","number":23,"policy_digest":"{}","repository_id":100}}"#,
        "b".repeat(40),
        "a".repeat(40),
        p.digest
    );
    assert_eq!(
        a["acknowledgment"],
        format!(
            "<!-- yaga-pr-triage-acceptance:v1 {:x} -->",
            Sha256::digest(canonical.as_bytes())
        )
    );
    doc["reviews"] = json!([{"id":7,"user_id":10,"user_type":"User","state":"APPROVED","commit_id":doc["head_sha"],"submitted_at":"2026-11-22T00:00:00Z","body":a["acknowledgment"]}]);
    assert_eq!(check(&p, &doc)["admission"]["decision"], "allow");
    doc["base_ref"] = "other".into();
    assert_eq!(check(&p, &doc)["admission"]["decision"], "hold");
    doc["base_ref"] = "main".into();
    doc["reviews"][0]["user_type"] = "Bot".into();
    assert_eq!(check(&p, &doc)["admission"]["decision"], "hold");
    doc["reviews"][0]["user_type"] = "User".into();
    let mut newer = doc["reviews"][0].clone();
    newer["id"] = 8.into();
    newer["state"] = "CHANGES_REQUESTED".into();
    doc["reviews"].as_array_mut().unwrap().push(newer);
    assert_eq!(
        check(&p, &doc)["admission"]["reason"],
        "maintainer-changes-requested"
    );
}
#[test]
fn rejects_incomplete_inventory_and_ambiguous_records() {
    let mut doc = data();
    doc["changed_files"] = 1.into();
    assert!(Snapshot::decode(&serde_json::to_vec(&doc).unwrap()).is_err());
    assert!(Snapshot::decode(br#"{"host":"a","host":"b"}"#).is_err());
}

#[test]
fn large_file_inventories_pass_up_to_raised_bound() {
    let p = policy();
    let mut doc = data();
    let sha = "d".repeat(40);
    let make_files = |count: usize| -> Vec<Value> {
        (0..count)
            .map(|i| {
                json!({
                    "filename": format!("crates/file_{i}.rs"),
                    "previous_filename": Value::Null,
                    "sha": sha,
                    "status": "added"
                })
            })
            .collect()
    };

    doc["changed_files"] = 650.into();
    doc["files"] = Value::Array(make_files(650));
    let report = check(&p, &doc);
    assert_eq!(report["inventory"]["changed_files"], 650);

    doc["changed_files"] = (MAX_FILES + 1).into();
    doc["files"] = Value::Array(make_files(MAX_FILES + 1));
    assert!(Snapshot::decode(&serde_json::to_vec(&doc).unwrap()).is_err());
}
