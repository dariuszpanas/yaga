use serde_json::{Value, json};
use std::{
    fs,
    path::Path,
    process::{Command, Output},
};

fn invoke(dir: &Path, args: &[&str]) -> Output {
    Command::new(env!("CARGO_BIN_EXE_yaga"))
        .current_dir(dir)
        .env("PATH", dir.join("no-tools"))
        .env("NO_COLOR", "1")
        .env("COLUMNS", "100")
        .args(args)
        .output()
        .unwrap()
}

#[test]
fn inspection_needs_only_the_plan_and_preserves_one_versioned_contract() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("plan.toml");
    let args = ["repo", "inspect", "--plan", "plan.toml", "--format", "json"];
    fs::write(&path, "plan-version=1\nchecks=['workflow-lint','commit']").unwrap();
    let output = invoke(dir.path(), &args);
    assert!(output.status.success());
    assert!(output.stderr.is_empty());
    let document: Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(
        document,
        json!({
            "schema_version": 1, "kind": "repository_plan", "plan_version": 1,
            "providers": ["commit", "workflow-lint"], "required_options": [],
            "required_tools": ["git", "docker"], "workflow_security": null,
            "workflow_paths": [], "policy_paths": {}, "commit_source_default": "HEAD"
        })
    );

    fs::write(&path, "plan-version=2\nchecks=['tree','workflow-security','path']\nworkflow-paths=['ci']\nworkflow-security-rules=['secrets.inherit']\npath-policy='absent/p.toml'\ntree-policy='absent/t.toml'").unwrap();
    let output = invoke(dir.path(), &args);
    assert!(output.status.success());
    let document: Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(
        document["providers"],
        json!(["workflow-security", "path", "tree"])
    );
    assert_eq!(
        document["policy_paths"],
        json!({"tree":"absent/t.toml","path":"absent/p.toml"})
    );
    assert_eq!(document["workflow_paths"], json!(["ci"]));
    assert_eq!(
        document["workflow_security"],
        json!({"profile":"custom","rules":["secrets.inherit"]})
    );
    assert_eq!(document["required_options"], json!(["--revision"]));
    assert_eq!(document["required_tools"], json!(["git"]));
    assert!(document["commit_source_default"].is_null());

    for (source, expected) in [
        (
            "plan-version=3\nchecks=['workflow']",
            "newer schema version 3",
        ),
        ("plan-version=2\nchecks=['tree']", "tree-policy is required"),
        (
            "plan-version=2\nchecks=['workflow']\ntree-policy='t.toml'",
            "tree-policy requires",
        ),
    ] {
        fs::write(&path, source).unwrap();
        let output = invoke(dir.path(), &args);
        assert_eq!(output.status.code(), Some(2));
        assert!(output.stdout.is_empty());
        let error: Value = serde_json::from_slice(&output.stderr).unwrap();
        assert!(
            error["error"]["message"]
                .as_str()
                .unwrap()
                .contains(expected)
        );
    }
}

#[test]
fn native_metadata_drives_help_for_new_commands() {
    let dir = tempfile::tempdir().unwrap();
    let group = invoke(dir.path(), &["repo", "--help"]);
    assert!(group.status.success());
    assert!(String::from_utf8(group.stdout).unwrap().contains("inspect"));
    let help = invoke(dir.path(), &["repo", "inspect", "--help"]);
    assert!(help.status.success());
    let text = String::from_utf8(help.stdout).unwrap();
    for expected in [
        "Usage:",
        "yaga repo inspect",
        "Options",
        "--plan",
        "[required]",
        "--format",
        "text|json",
    ] {
        assert!(text.contains(expected), "{text}");
    }
    let missing = invoke(dir.path(), &["repo", "inspect"]);
    assert_eq!(missing.status.code(), Some(2));
    assert!(
        String::from_utf8(missing.stderr)
            .unwrap()
            .contains("Missing option")
    );
}
