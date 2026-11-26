mod common;

use common::{run, run_with_stdin, stderr_str, stdout_json, stdout_str, write_file};
use serde_json::json;
use sha2::{Digest, Sha256};

#[test]
fn pr_description_check_validates_structure_template_and_hygiene() {
    let dir = tempfile::tempdir().unwrap();
    write_file(
        dir.path(),
        "desc-policy.toml",
        br#"description-policy-version = 1
min-words = 6
max-length = 65536
placeholders = ["TODO", "TBD"]
require-checked-tasks = true
heading-min-level = 2
heading-max-length = 80
"#,
    );
    write_file(dir.path(), "template.md", b"## Summary\n\n## Testing\n");

    // Valid description matching template order & checked tasks
    let good = "## Summary\n\nImplement the standalone native description checker cleanly.\n\n## Testing\n\n- [x] Verified integration tests.\n";
    let ok = run_with_stdin(
        dir.path(),
        &[
            "pull-request",
            "description",
            "check",
            "--policy",
            "desc-policy.toml",
            "--template",
            "template.md",
            "--stdin",
            "--format",
            "json",
        ],
        good.as_bytes(),
    );
    assert_eq!(ok.status.code(), Some(0), "stderr: {}", stderr_str(&ok));
    assert_eq!(stdout_json(&ok)["valid"], true);

    // Violations: H1 heading, HTML heading, placeholder, unchecked task, missing template heading, prose tab
    let bad = "# Title\n\n## Summary\n\nTODO\n\nTab\there.\n\n<h3>Raw</h3>\n\n- [ ] Not done\n";
    write_file(dir.path(), "bad-body.md", bad.as_bytes());
    let fail = run(
        dir.path(),
        &[
            "pull-request",
            "description",
            "check",
            "--policy",
            "desc-policy.toml",
            "--template",
            "template.md",
            "--file",
            "bad-body.md",
            "--format",
            "json",
        ],
    );
    assert_eq!(fail.status.code(), Some(1));
    let doc = stdout_json(&fail);
    assert_eq!(doc["valid"], false);
    let codes: Vec<&str> = doc["diagnostics"]
        .as_array()
        .unwrap()
        .iter()
        .map(|d| d["code"].as_str().unwrap())
        .collect();
    for expected in [
        "description.heading-level",
        "description.html-heading",
        "description.placeholder",
        "description.unchecked-task",
    ] {
        assert!(codes.contains(&expected), "missing {expected} in {codes:?}");
    }

    // Event file source via github pull-request description check
    let event = json!({
        "action": "edited",
        "number": 9,
        "pull_request": {
            "number": 9,
            "state": "open",
            "draft": false,
            "body": good
        }
    });
    write_file(dir.path(), "pr-event.json", event.to_string().as_bytes());
    let ev_ok = run(
        dir.path(),
        &[
            "github",
            "pull-request",
            "description",
            "check",
            "--policy",
            "desc-policy.toml",
            "--template",
            "template.md",
            "--event-file",
            "pr-event.json",
        ],
    );
    assert_eq!(
        ev_ok.status.code(),
        Some(0),
        "stderr: {}",
        stderr_str(&ev_ok)
    );
}

#[test]
fn pr_review_check_evaluates_offline_snapshots() {
    let dir = tempfile::tempdir().unwrap();
    let policy_bytes = b"[agent-review]\nversion = 1\nrequired = [\"codex\"]\n";
    write_file(dir.path(), "review.toml", policy_bytes);

    let policy =
        yaga_core::agent_review::Policy::parse(policy_bytes, std::path::Path::new("review.toml"))
            .unwrap();
    let head_sha = "a".repeat(40);
    let base_sha = "b".repeat(40);
    let codex = "chatgpt-codex-connector[bot]";

    // Approved on current head -> exit 0
    let passing = json!({
        "version": 1,
        "number": 42,
        "head_sha": head_sha,
        "base_sha": base_sha,
        "plan_digest": policy.digest(),
        "reviews": [{
            "id": 10,
            "author_login": codex,
            "author_id": 199175422,
            "state": "APPROVED",
            "submitted_at": "2026-11-01T03:00:00Z",
            "commit_id": head_sha
        }]
    });
    write_file(dir.path(), "pass.json", passing.to_string().as_bytes());
    let ok = run(
        dir.path(),
        &[
            "pull-request",
            "review",
            "check",
            "--policy",
            "review.toml",
            "--file",
            "pass.json",
            "--format",
            "json",
        ],
    );
    assert_eq!(ok.status.code(), Some(0), "stderr: {}", stderr_str(&ok));
    assert_eq!(stdout_json(&ok)["status"], "passed");

    // Stale approval on base_sha only -> pending (exit 1)
    let stale = json!({
        "version": 1,
        "number": 42,
        "head_sha": head_sha,
        "base_sha": base_sha,
        "plan_digest": policy.digest(),
        "reviews": [{
            "id": 10,
            "author_login": codex,
            "author_id": 199175422,
            "state": "APPROVED",
            "submitted_at": "2026-11-01T03:00:00Z",
            "commit_id": base_sha
        }]
    });
    write_file(dir.path(), "stale.json", stale.to_string().as_bytes());
    let fail = run(
        dir.path(),
        &[
            "pull-request",
            "review",
            "check",
            "--policy",
            "review.toml",
            "--file",
            "stale.json",
            "--format",
            "json",
        ],
    );
    assert_eq!(fail.status.code(), Some(1));
    assert_eq!(stdout_json(&fail)["status"], "pending");
}

#[test]
fn pr_triage_check_enforces_maintainer_admission_on_external_prs() {
    let dir = tempfile::tempdir().unwrap();
    let policy_bytes = b"triage-policy-version = 1\nhost = \"github.com\"\nrepository-id = 100\nmaintainer-user-ids = [10]\nduplicate-candidates = 0\n";
    write_file(dir.path(), "triage.toml", policy_bytes);
    let policy = yaga_core::policies::triage::TriagePolicy::parse(policy_bytes).unwrap();

    let head = "a".repeat(40);
    let base = "b".repeat(40);
    let tree = "c".repeat(40);

    let mut snapshot = json!({
        "version": 1,
        "host": "github.com",
        "repository": "example/project",
        "repository_id": 100,
        "number": 23,
        "state": "open",
        "draft": false,
        "head_sha": head,
        "base_sha": base,
        "base_ref": "main",
        "head_tree": tree,
        "head_repository_id": 101,
        "head_repository_fork": true,
        "author_id": 30,
        "author_login": "external",
        "author_type": "User",
        "changed_files": 0,
        "files": [],
        "reviews": [],
        "candidates": [],
        "candidate_limit": 0,
        "candidates_examined": 0
    });
    write_file(dir.path(), "hold.json", snapshot.to_string().as_bytes());

    let hold = run(
        dir.path(),
        &[
            "pull-request",
            "triage",
            "check",
            "--policy",
            "triage.toml",
            "--file",
            "hold.json",
            "--format",
            "json",
        ],
    );
    assert_eq!(hold.status.code(), Some(1), "stderr: {}", stderr_str(&hold));
    let doc = stdout_json(&hold);
    assert_eq!(doc["admission"]["decision"], "hold");

    let canonical = format!(
        r#"{{"base_ref":"main","base_sha":"{base}","head_sha":"{head}","host":"github.com","number":23,"policy_digest":"{}","repository_id":100}}"#,
        policy.digest
    );
    let ack = format!(
        "<!-- yaga-pr-triage-acceptance:v1 {:x} -->",
        Sha256::digest(canonical.as_bytes())
    );
    snapshot["reviews"] = json!([{
        "id": 7,
        "user_id": 10,
        "user_type": "User",
        "state": "APPROVED",
        "commit_id": head,
        "submitted_at": "2026-11-22T00:00:00Z",
        "body": ack
    }]);
    write_file(dir.path(), "allow.json", snapshot.to_string().as_bytes());

    let allow = run(
        dir.path(),
        &[
            "pull-request",
            "triage",
            "check",
            "--policy",
            "triage.toml",
            "--file",
            "allow.json",
            "--format",
            "json",
        ],
    );
    assert_eq!(
        allow.status.code(),
        Some(0),
        "stderr: {}",
        stderr_str(&allow)
    );
    assert_eq!(stdout_json(&allow)["admission"]["decision"], "allow");

    // Passing a directory as --file fails cleanly with exit 2.
    let dir_err = run(
        dir.path(),
        &[
            "pull-request",
            "triage",
            "check",
            "--policy",
            "triage.toml",
            "--file",
            ".",
        ],
    );
    assert_eq!(dir_err.status.code(), Some(2));

    // Passing a symlinked snapshot works via resolve_user_path.
    #[cfg(unix)]
    {
        let link = dir.path().join("allow-link.json");
        std::os::unix::fs::symlink(dir.path().join("allow.json"), &link).unwrap();
        let sym = run(
            dir.path(),
            &[
                "pull-request",
                "triage",
                "check",
                "--policy",
                "triage.toml",
                "--file",
                "allow-link.json",
                "--format",
                "json",
            ],
        );
        assert_eq!(sym.status.code(), Some(0), "stderr: {}", stderr_str(&sym));
        assert_eq!(stdout_json(&sym)["admission"]["decision"], "allow");
    }
}

#[test]
fn gate_agent_review_policy_check_plan_template_and_evaluate() {
    let dir = tempfile::tempdir().unwrap();
    write_file(
        dir.path(),
        "gate-policy.toml",
        b"[agent-review]\nversion = 1\nrequired = [\"codex\"]\n",
    );

    let check = run(
        dir.path(),
        &[
            "gate",
            "agent-review",
            "policy",
            "check",
            "--file",
            "gate-policy.toml",
            "--format",
            "json",
        ],
    );
    assert_eq!(
        check.status.code(),
        Some(0),
        "stderr: {}",
        stderr_str(&check)
    );
    let plan = run(
        dir.path(),
        &[
            "gate",
            "agent-review",
            "policy",
            "plan",
            "--file",
            "gate-policy.toml",
            "--format",
            "json",
        ],
    );
    assert_eq!(plan.status.code(), Some(0), "stderr: {}", stderr_str(&plan));
    let plan_doc = stdout_json(&plan);
    assert!(plan_doc["plan_digest"].is_string());

    let tpl = run(
        dir.path(),
        &[
            "gate",
            "agent-review",
            "policy",
            "template",
            "--file",
            "gate-policy.toml",
        ],
    );
    assert_eq!(tpl.status.code(), Some(0), "stderr: {}", stderr_str(&tpl));
    let mut receipt = stdout_json(&tpl);

    // Pending receipt evaluates to exit 1
    write_file(dir.path(), "pending.json", receipt.to_string().as_bytes());
    let pending = run(
        dir.path(),
        &[
            "gate",
            "agent-review",
            "policy",
            "evaluate",
            "--file",
            "gate-policy.toml",
            "--results",
            "pending.json",
            "--format",
            "json",
        ],
    );
    assert_eq!(pending.status.code(), Some(1));

    // Mark lens passed -> exit 0
    receipt["results"][0]["outcome"] = json!("passed");
    receipt["results"][0]["summary"] = json!("No findings.");
    write_file(dir.path(), "passed.json", receipt.to_string().as_bytes());
    let passed = run(
        dir.path(),
        &[
            "gate",
            "agent-review",
            "policy",
            "evaluate",
            "--file",
            "gate-policy.toml",
            "--results",
            "passed.json",
            "--format",
            "json",
        ],
    );
    assert_eq!(
        passed.status.code(),
        Some(0),
        "stderr: {}",
        stderr_str(&passed)
    );
    assert_eq!(stdout_json(&passed)["state"], "passed");

    // Text output sanity
    let txt = run(
        dir.path(),
        &[
            "gate",
            "agent-review",
            "policy",
            "evaluate",
            "--file",
            "gate-policy.toml",
            "--results",
            "passed.json",
        ],
    );
    assert_eq!(txt.status.code(), Some(0));
    assert!(stdout_str(&txt).contains("passed"));
}
