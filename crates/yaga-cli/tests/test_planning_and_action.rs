mod common;

use common::{
    commit_all, git, init_git_repo, run, stderr_str, stdout_json, stdout_str, write_file, yaga_cmd,
};
use serde_json::json;

const TEST_POLICY: &[u8] = br#"test-policy-version = 1

[[suites]]
id = "build"
binding = "cargo"

[[suites]]
id = "unit"
binding = "cargo-test"
needs = ["build"]
variants = ["linux", "windows"]

[profiles.full]
mode = "full"

[profiles.pr]
mode = "selective"

[[rules]]
id = "rust-sources"
when-any = ["src/**/*.rs"]
select = ["unit"]
"#;

#[test]
fn test_policy_plan_and_evaluate_end_to_end() {
    let dir = tempfile::tempdir().unwrap();
    init_git_repo(dir.path());
    write_file(dir.path(), ".yaga/test-policy.toml", TEST_POLICY);
    write_file(dir.path(), "README.md", b"# demo\n");
    let base = commit_all(dir.path(), "chore: initial");

    // 1. test policy check
    let pcheck = run(
        dir.path(),
        &[
            "test",
            "policy",
            "check",
            "--file",
            ".yaga/test-policy.toml",
            "--format",
            "json",
        ],
    );
    assert_eq!(
        pcheck.status.code(),
        Some(0),
        "stderr: {}",
        stderr_str(&pcheck)
    );
    let pdoc = stdout_json(&pcheck);
    assert_eq!(pdoc["valid"], true);

    // Invalid policy -> exit 2
    write_file(dir.path(), "bad-test.toml", b"test-policy-version = 99\n");
    let pbad = run(
        dir.path(),
        &["test", "policy", "check", "--file", "bad-test.toml"],
    );
    assert_eq!(pbad.status.code(), Some(2));

    // 2. test plan (full profile with --revision)
    let bindings = "b".repeat(64);
    let full = run(
        dir.path(),
        &[
            "test",
            "plan",
            "--policy",
            ".yaga/test-policy.toml",
            "--profile",
            "full",
            "--revision",
            &base,
            "--bindings-sha256",
            &bindings,
            "--format",
            "json",
        ],
    );
    assert_eq!(full.status.code(), Some(0), "stderr: {}", stderr_str(&full));
    let full_doc = stdout_json(&full);
    assert_eq!(full_doc["test-plan-version"], 1);
    assert!(full_doc["plan_digest"].is_string());

    // 3. test plan (selective profile with --range)
    write_file(dir.path(), "src/main.rs", b"fn main() {}\n");
    let head = commit_all(dir.path(), "feat(app): add main");
    let range = format!("{base}..{head}");
    let plan_out = run(
        dir.path(),
        &[
            "test",
            "plan",
            "--policy",
            ".yaga/test-policy.toml",
            "--profile",
            "pr",
            "--range",
            &range,
            "--bindings-sha256",
            &bindings,
            "--format",
            "json",
        ],
    );
    assert_eq!(
        plan_out.status.code(),
        Some(0),
        "stderr: {}",
        stderr_str(&plan_out)
    );
    let plan_doc = stdout_json(&plan_out);
    write_file(dir.path(), "plan.json", &plan_out.stdout);

    let plan_digest = plan_doc["plan_digest"].as_str().unwrap();
    let policy_sha = plan_doc["plan"]["policy_sha256"].as_str().unwrap();
    let exec_commit = plan_doc["plan"]["source"]["execution_commit"]
        .as_str()
        .unwrap();
    let exec_tree = plan_doc["plan"]["source"]["execution_tree"]
        .as_str()
        .unwrap();

    // 4. test evaluate with complete passing results -> exit 0
    let passing_results = json!({
        "test-results-version": 1,
        "plan_digest": plan_digest,
        "policy_sha256": policy_sha,
        "bindings_sha256": bindings,
        "execution_commit": exec_commit,
        "execution_tree": exec_tree,
        "run_id": "run-100",
        "attempt": 1,
        "results": [
            {"suite": "build", "variant": "default", "binding": "cargo", "outcome": "passed"},
            {"suite": "unit", "variant": "linux", "binding": "cargo-test", "outcome": "passed"},
            {"suite": "unit", "variant": "windows", "binding": "cargo-test", "outcome": "passed"}
        ]
    });
    write_file(
        dir.path(),
        "results-pass.json",
        passing_results.to_string().as_bytes(),
    );
    let eval_ok = run(
        dir.path(),
        &[
            "test",
            "evaluate",
            "--plan",
            "plan.json",
            "--results",
            "results-pass.json",
            "--policy-sha256",
            policy_sha,
            "--bindings-sha256",
            &bindings,
            "--execution-commit",
            exec_commit,
            "--execution-tree",
            exec_tree,
            "--run-id",
            "run-100",
            "--attempt",
            "1",
            "--format",
            "json",
        ],
    );
    assert_eq!(
        eval_ok.status.code(),
        Some(0),
        "stderr: {}",
        stderr_str(&eval_ok)
    );
    assert_eq!(stdout_json(&eval_ok)["passed"], true);

    // 5. test evaluate with failing suite -> exit 1
    let mut failing_results = passing_results.clone();
    failing_results["results"][1]["outcome"] = json!("failed");
    write_file(
        dir.path(),
        "results-fail.json",
        failing_results.to_string().as_bytes(),
    );
    let eval_fail = run(
        dir.path(),
        &[
            "test",
            "evaluate",
            "--plan",
            "plan.json",
            "--results",
            "results-fail.json",
            "--policy-sha256",
            policy_sha,
            "--bindings-sha256",
            &bindings,
            "--execution-commit",
            exec_commit,
            "--execution-tree",
            exec_tree,
            "--run-id",
            "run-100",
            "--attempt",
            "1",
        ],
    );
    assert_eq!(eval_fail.status.code(), Some(1));

    // 6. test evaluate with mismatched attempt -> exit 2
    let eval_err = run(
        dir.path(),
        &[
            "test",
            "evaluate",
            "--plan",
            "plan.json",
            "--results",
            "results-pass.json",
            "--policy-sha256",
            policy_sha,
            "--bindings-sha256",
            &bindings,
            "--execution-commit",
            exec_commit,
            "--execution-tree",
            exec_tree,
            "--run-id",
            "run-100",
            "--attempt",
            "2",
        ],
    );
    assert_eq!(eval_err.status.code(), Some(2));
}

#[test]
fn native_action_commit_enforces_trusted_default_branch_policy() {
    let dir = tempfile::tempdir().unwrap();
    init_git_repo(dir.path());

    // Default branch policy requires a commit body
    write_file(
        dir.path(),
        ".yaga.toml",
        b"config-version = 1\n[commit]\nbody-policy = \"required\"\n",
    );
    let base = commit_all(dir.path(), "chore: establish trusted policy");

    // PR branch attempts to weaken .yaga.toml and omits commit body
    write_file(
        dir.path(),
        ".yaga.toml",
        b"config-version = 1\n[commit]\nbody-policy = \"optional\"\n",
    );
    let bad_head = commit_all(dir.path(), "fix(core): weaken policy without body");
    git(
        dir.path(),
        &["update-ref", "refs/remotes/pull/17/head", &bad_head],
    );
    git(dir.path(), &["checkout", "--detach", &base]);

    let event = json!({
        "action": "synchronize",
        "number": 17,
        "repository": {"id": 100, "full_name": "owner/repo", "default_branch": "main"},
        "pull_request": {
            "number": 17,
            "title": "fix(core): weaken policy without body",
            "state": "open",
            "draft": false,
            "user": {"login": "octocat", "id": 1, "type": "User"},
            "base": {"sha": base, "ref": "main", "repo": {"id": 100, "full_name": "owner/repo"}},
            "head": {"sha": bad_head, "ref": "feat/test", "repo": {"id": 100, "full_name": "owner/repo"}}
        }
    });
    let event_path = dir.path().join("event.json");
    std::fs::write(&event_path, event.to_string()).unwrap();

    // Even if working tree is dirty with weakened policy, trusted-config reads committed base!
    write_file(
        dir.path(),
        ".yaga.toml",
        b"config-version = 1\n[commit]\nbody-policy = \"optional\"\n",
    );

    let out = yaga_cmd(dir.path())
        .args(["__action", "commit"])
        .env("GITHUB_EVENT_NAME", "pull_request_target")
        .env("GITHUB_REPOSITORY", "owner/repo")
        .env("GITHUB_REPOSITORY_ID", "100")
        .env("GITHUB_BASE_REF", "main")
        .env("GITHUB_HEAD_REF", "feat/test")
        .env("GITHUB_SHA", &base)
        .env("GITHUB_REF", "refs/heads/main")
        .env("GITHUB_EVENT_PATH", &event_path)
        .env("GITHUB_WORKSPACE", dir.path())
        .env("YAGA_COMMIT_TRUSTED_CONFIG", ".yaga.toml")
        .output()
        .unwrap();
    assert_eq!(out.status.code(), Some(1), "stderr: {}", stderr_str(&out));
    assert!(stdout_str(&out).contains("body.required"));
}

#[test]
fn native_action_cli_and_summary_dispatch_cleanly() {
    let dir = tempfile::tempdir().unwrap();
    write_file(
        dir.path(),
        "branch.toml",
        b"branch-policy-version = 1\nallowed-patterns = [\"feat/*\"]\n",
    );

    let ok = yaga_cmd(dir.path())
        .args(["__action", "cli"])
        .env("GITHUB_WORKSPACE", dir.path())
        .env("YAGA_INPUT_CHECK", "branch")
        .env("YAGA_INPUT_POLICY", "branch.toml")
        .env("YAGA_INPUT_BRANCH", "feat/native")
        .env("YAGA_NON_CLI_INPUTS", "0")
        .env("YAGA_HAS_GITHUB_TOKEN", "0")
        .output()
        .unwrap();
    assert_eq!(ok.status.code(), Some(0), "stderr: {}", stderr_str(&ok));

    // Reject offline check receiving github-token
    let bad_token = yaga_cmd(dir.path())
        .args(["__action", "cli"])
        .env("GITHUB_WORKSPACE", dir.path())
        .env("YAGA_INPUT_CHECK", "branch")
        .env("YAGA_INPUT_POLICY", "branch.toml")
        .env("YAGA_INPUT_BRANCH", "feat/native")
        .env("YAGA_NON_CLI_INPUTS", "0")
        .env("YAGA_HAS_GITHUB_TOKEN", "1")
        .output()
        .unwrap();
    assert_eq!(bad_token.status.code(), Some(2));

    // Render step summary Markdown
    let report = dir.path().join("report.txt");
    let err = dir.path().join("err.txt");
    std::fs::write(&report, &ok.stdout).unwrap();
    std::fs::write(&err, b"").unwrap();
    let summary = run(
        dir.path(),
        &[
            "__action",
            "summary",
            "--mode",
            "branch",
            "--status",
            "0",
            "--report",
            report.to_str().unwrap(),
            "--error",
            err.to_str().unwrap(),
        ],
    );
    assert_eq!(
        summary.status.code(),
        Some(0),
        "stderr: {}",
        stderr_str(&summary)
    );
    let md = stdout_str(&summary);
    assert!(md.contains("YAGA Checks"), "{md}");
    assert!(md.contains("Passed"), "{md}");
}
