use super::*;
use serde_json::{Value, json};

fn payload() -> Value {
    let repo = json!({"full_name":"owner/repo","id":1});
    json!({"action":"synchronize","number":2,"repository":repo,
        "pull_request":{"state":"open","draft":false,"number":2,"title":"feat: change","body":null,
            "base":{"repo":repo,"sha":"1".repeat(40),"ref":"main"},
            "head":{"repo":repo,"sha":"2".repeat(40),"ref":"feature"},
            "user":{"login":"dependabot[bot]","type":"Bot","id":3}}})
}

#[test]
fn parses_event_and_requires_exact_runner_binding() {
    let event = parse_event(&serde_json::to_vec(&payload()).unwrap()).unwrap();
    let mut ctx = RunnerContext {
        event_name: "pull_request",
        repository: "owner/repo",
        repository_id: 1,
        base_ref: "main",
        head_ref: "feature",
    };
    assert!(ctx.validate(&event, false).is_ok());
    assert!(ctx.validate(&event, true).is_err());
    ctx.repository_id = 2;
    assert!(ctx.validate(&event, false).is_err());
}
#[test]
fn proposed_message_limit_counts_utf8_and_separators_and_cannot_exceed_the_hard_limit() {
    use crate::commits::MAX_MESSAGE_BYTES;
    let title = "fix: é";
    let remaining = MAX_MESSAGE_BYTES - title.len() - 2;
    let mut body = "é".repeat(remaining / 2) + &"x".repeat(remaining % 2);
    assert_eq!(title.len() + 2 + body.len(), MAX_MESSAGE_BYTES);
    assert!(event::validate_proposed_message_size(title, &body, MAX_MESSAGE_BYTES).is_ok());
    body.push('x');
    assert!(event::validate_proposed_message_size(title, &body, usize::MAX).is_err());

    let mut document = payload();
    document["pull_request"]["title"] = json!("fix: x");
    document["pull_request"]["body"] = json!("é".repeat(10));
    let raw = serde_json::to_vec(&document).unwrap();
    assert!(parse_event_with_message_limit(&raw, 28).is_ok());
    assert!(parse_event_with_message_limit(&raw, 27).is_err());
}

#[test]
fn rejects_ambiguous_or_inconsistent_inputs() {
    let original = payload().to_string();
    assert!(parse_event(original.replace("\"id\":3", "\"id\":3,\"id\":4").as_bytes()).is_err());
    for (path, value) in [
        ("/number", json!(true)),
        ("/pull_request/user/login", json!("dependabot[Bot]")),
        ("/pull_request/user/id", json!(-1)),
        ("/pull_request/head/repo/id", json!(2)),
        ("/pull_request/base/sha", json!("A".repeat(40))),
        ("/pull_request/head/sha", json!("1".repeat(40))),
        ("/pull_request/title", json!("two\nlines")),
        ("/pull_request/head/ref", json!("head\u{202e}ref")),
    ] {
        let mut payload = payload();
        *payload.pointer_mut(path).unwrap() = value;
        assert!(
            parse_event(&serde_json::to_vec(&payload).unwrap()).is_err(),
            "{path}"
        );
    }
}

#[test]
fn trusted_event_reads_selected_policy_blob_and_checks_fetched_head() {
    use std::process::Command;
    let root = tempfile::tempdir().unwrap();
    let directory = root.path();
    let git = |args: &[&str]| {
        let output = Command::new("git")
            .arg("-C")
            .arg(directory)
            .args(args)
            .env("GIT_CONFIG_NOSYSTEM", "1")
            .env(
                "GIT_CONFIG_GLOBAL",
                if cfg!(windows) { "nul" } else { "/dev/null" },
            )
            .output()
            .unwrap();
        assert!(output.status.success(), "fixture setup failed for {args:?}");
        String::from_utf8(output.stdout).unwrap().trim().to_string()
    };
    if !Command::new("git")
        .args(["--no-lazy-fetch", "version"])
        .output()
        .unwrap()
        .status
        .success()
    {
        return;
    }
    git(&["init", "-q"]);
    git(&["config", "user.name", "Test"]);
    git(&["config", "user.email", "test@example.invalid"]);
    std::fs::write(
        directory.join(".yaga.toml"),
        "config-version=1\n[commit]\nallowed-types=['feat']\n",
    )
    .unwrap();
    git(&["add", ".yaga.toml"]);
    git(&["commit", "-qm", "feat: baseline"]);
    let base = git(&["rev-parse", "HEAD"]);
    git(&["commit", "--allow-empty", "-qm", "fix: change"]);
    let head = git(&["rev-parse", "HEAD"]);
    git(&["update-ref", "refs/remotes/pull/2/head", &head]);
    git(&["checkout", "-q", "--detach", &base]);
    std::fs::write(directory.join(".yaga.toml"), "invalid working tree").unwrap();
    let mut doc = payload();
    doc["repository"]["default_branch"] = json!("main");
    doc["pull_request"]["base"]["sha"] = json!(base);
    doc["pull_request"]["head"]["sha"] = json!(head);
    let path = directory.join("event.json");
    std::fs::write(&path, doc.to_string()).unwrap();
    let runner = RunnerContext {
        event_name: "pull_request_target",
        repository: "owner/repo",
        repository_id: 1,
        base_ref: "main",
        head_ref: "feature",
    };
    let source = TrustedPolicy {
        path: ".yaga.toml",
        revision: &base,
        runner_ref: "refs/heads/main",
    };
    let run = || check(&path, directory, None, Some(&runner), Some(&source));
    let report = run().unwrap();
    assert!(!report.valid());
    assert_eq!(report.results.len(), 2);
    git(&["update-ref", "refs/remotes/pull/2/head", &base]);
    assert!(
        matches!(run(),Err(("git",message)) if message=="fetched pull request head does not match the event head SHA")
    );
}
