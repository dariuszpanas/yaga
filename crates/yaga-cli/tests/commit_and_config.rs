mod common;

use common::{
    commit_all, init_git_repo, run, run_with_stdin, stderr_json, stderr_str, stdout_json,
    stdout_str, write_file, yaga_cmd,
};
use serde_json::json;

#[test]
fn commit_check_supports_message_file_stdin_and_formats() {
    let dir = tempfile::tempdir().unwrap();
    write_file(
        dir.path(),
        ".yaga.toml",
        br#"config-version = 1
[commit]
allowed-types = ["feat", "fix", "chore"]
scope-policy = "optional"
scope-policy-by-type = { feat = "required" }
header-max-length = 72
description-min-length = 3
description-ending = "forbid"
breaking-markers = "paired"
body-policy = "required"
body-min-words = 5
required-footer-tokens = ["Signed-off-by"]
forbidden-footer-tokens = ["WIP"]
"#,
    );

    let valid = "feat(cli): add native command runner\n\nImplement the standalone command path cleanly in Rust.\n\nSigned-off-by: Dev <dev@example.com>\n";

    // --message with json output
    let ok = run(
        dir.path(),
        &[
            "commit",
            "check",
            "--config",
            ".yaga.toml",
            "--message",
            valid,
            "--format",
            "json",
        ],
    );
    assert_eq!(ok.status.code(), Some(0), "stderr: {}", stderr_str(&ok));
    let doc = stdout_json(&ok);
    assert_eq!(doc["valid"], true);
    assert_eq!(doc["passed"], 1);

    // --stdin with --quiet suppresses stdout
    let stdin_ok = run_with_stdin(
        dir.path(),
        &[
            "commit",
            "check",
            "--config",
            ".yaga.toml",
            "--stdin",
            "--quiet",
        ],
        valid.as_bytes(),
    );
    assert_eq!(stdin_ok.status.code(), Some(0));
    assert!(stdout_str(&stdin_ok).is_empty());

    // --file with multiple violations: missing scope, unpaired !, trailing dot, short body, missing/forbidden footer
    write_file(
        dir.path(),
        "msg.txt",
        b"feat!: bad.\n\ntoo short\n\nWIP: draft\n",
    );
    let fail = run(
        dir.path(),
        &[
            "commit",
            "check",
            "--config",
            ".yaga.toml",
            "--file",
            "msg.txt",
            "--format",
            "json",
        ],
    );
    assert_eq!(fail.status.code(), Some(1));
    let doc = stdout_json(&fail);
    assert_eq!(doc["valid"], false);
    let codes: Vec<&str> = doc["commits"][0]["diagnostics"]
        .as_array()
        .unwrap()
        .iter()
        .map(|d| d["code"].as_str().unwrap())
        .collect();
    for expected in [
        "scope.required",
        "description.ending",
        "breaking.marker-pair",
        "body.word-count",
        "footer.required",
        "footer.forbidden",
    ] {
        assert!(codes.contains(&expected), "missing {expected} in {codes:?}");
    }

    // github annotation format
    let gh = run(
        dir.path(),
        &[
            "commit",
            "check",
            "--config",
            ".yaga.toml",
            "--file",
            "msg.txt",
            "--format",
            "github",
        ],
    );
    assert_eq!(gh.status.code(), Some(1));
    assert!(stdout_str(&gh).contains("::error "));
}

#[test]
fn commit_check_rejects_mixed_sources_and_checks_git_ranges() {
    let dir = tempfile::tempdir().unwrap();
    init_git_repo(dir.path());
    write_file(
        dir.path(),
        ".yaga.toml",
        b"config-version = 1\n[commit]\nallowed-types = [\"feat\", \"fix\", \"chore\"]\n",
    );
    let base = commit_all(dir.path(), "chore: initial baseline");
    write_file(dir.path(), "a.txt", b"hello\n");
    let head = commit_all(dir.path(), "feat(core): add feature");

    // Mixed sources must fail with exit 2
    let mixed = run(
        dir.path(),
        &[
            "commit",
            "check",
            "--message",
            "feat: one",
            "--commit",
            "HEAD",
        ],
    );
    assert_eq!(mixed.status.code(), Some(2));

    // Exact range pass
    let range = format!("{base}..{head}");
    let ok = run(
        dir.path(),
        &["commit", "check", "--range", &range, "--format", "json"],
    );
    assert_eq!(ok.status.code(), Some(0), "stderr: {}", stderr_str(&ok));
    let doc = stdout_json(&ok);
    assert_eq!(doc["checked"], 1);
    assert_eq!(doc["valid"], true);

    // Invalid commit in range -> exit 1
    write_file(dir.path(), "b.txt", b"world\n");
    let bad_head = commit_all(dir.path(), "not conventional at all");
    let bad_range = format!("{base}..{bad_head}");
    let bad = run(dir.path(), &["commit", "check", "--range", &bad_range]);
    assert_eq!(bad.status.code(), Some(1));
    assert!(stdout_str(&bad).contains("syntax.header"));
}

#[test]
fn commit_title_mode_ignores_full_commit_breaking_pair_rule() {
    let dir = tempfile::tempdir().unwrap();
    write_file(
        dir.path(),
        ".yaga.toml",
        b"config-version = 1\n[commit]\nbreaking-markers = \"paired\"\nbody-policy = \"required\"\n",
    );
    let ok = run(
        dir.path(),
        &[
            "commit",
            "check",
            "--config",
            ".yaga.toml",
            "--title",
            "feat(api)!: change response format",
        ],
    );
    assert_eq!(ok.status.code(), Some(0), "stderr: {}", stderr_str(&ok));
}

#[test]
fn config_init_show_and_commit_template_work() {
    let dir = tempfile::tempdir().unwrap();

    // dry-run creates nothing
    let dry = run(
        dir.path(),
        &[
            "config",
            "init",
            "--repo",
            ".",
            "--dry-run",
            "--format",
            "json",
        ],
    );
    assert_eq!(dry.status.code(), Some(0), "stderr: {}", stderr_str(&dry));
    assert!(!dir.path().join(".yaga.toml").exists());

    // real init creates .yaga.toml
    let init = run(dir.path(), &["config", "init", "--repo", "."]);
    assert_eq!(init.status.code(), Some(0), "stderr: {}", stderr_str(&init));
    assert!(dir.path().join(".yaga.toml").exists());

    // second init fails rather than overwriting
    let again = run(dir.path(), &["config", "init", "--repo", "."]);
    assert_eq!(again.status.code(), Some(2));

    // config show returns JSON policy
    let show = run(
        dir.path(),
        &["config", "show", "--repo", ".", "--format", "json"],
    );
    assert_eq!(show.status.code(), Some(0), "stderr: {}", stderr_str(&show));
    let doc = stdout_json(&show);
    assert_eq!(doc["schema_version"], 1);

    // commit template prints commented guidance
    let tpl = run(
        dir.path(),
        &[
            "commit", "template", "--repo", ".", "--type", "feat", "--scope", "cli",
        ],
    );
    assert_eq!(tpl.status.code(), Some(0), "stderr: {}", stderr_str(&tpl));
    let text = stdout_str(&tpl);
    assert!(text.contains("feat(cli):"), "{text}");
    assert!(text.contains('#'), "{text}");
}

#[test]
fn github_pull_request_check_enforces_range_title_and_dependabot_rules() {
    let dir = tempfile::tempdir().unwrap();
    init_git_repo(dir.path());
    write_file(
        dir.path(),
        ".yaga.toml",
        b"config-version = 1\n[commit]\nallowed-types = [\"feat\", \"fix\", \"chore\"]\ndependabot-pull-requests = \"skip\"\ndependabot-require-github-signature = false\n",
    );
    let base = commit_all(dir.path(), "chore: init repo");
    write_file(dir.path(), "src.rs", b"fn main() {}\n");
    let head = commit_all(dir.path(), "feat(cli): implement check");

    let event = json!({
        "action": "synchronize",
        "number": 12,
        "repository": {"id": 100, "full_name": "owner/repo", "default_branch": "main"},
        "pull_request": {
            "number": 12,
            "title": "feat(cli): implement check",
            "state": "open",
            "draft": false,
            "user": {"login": "octocat", "id": 1, "type": "User"},
            "base": {"sha": base, "ref": "main", "repo": {"id": 100, "full_name": "owner/repo"}},
            "head": {"sha": head, "ref": "feat/cli", "repo": {"id": 100, "full_name": "owner/repo"}}
        }
    });
    write_file(dir.path(), "event.json", event.to_string().as_bytes());

    let ok = run(
        dir.path(),
        &[
            "github",
            "pull-request",
            "check",
            "--event-file",
            "event.json",
            "--repo",
            ".",
            "--format",
            "json",
        ],
    );
    assert_eq!(ok.status.code(), Some(0), "stderr: {}", stderr_str(&ok));
    let doc = stdout_json(&ok);
    assert_eq!(doc["valid"], true);

    // Invalid PR title -> exit 1
    let mut bad_event = event.clone();
    bad_event["pull_request"]["title"] = json!("bad title");
    write_file(
        dir.path(),
        "bad-event.json",
        bad_event.to_string().as_bytes(),
    );
    let fail = run(
        dir.path(),
        &[
            "github",
            "pull-request",
            "check",
            "--event-file",
            "bad-event.json",
            "--repo",
            ".",
        ],
    );
    assert_eq!(fail.status.code(), Some(1));

    // Missing base commit -> fail closed with exit 2
    let mut missing_base = event;
    missing_base["pull_request"]["base"]["sha"] = json!("0000000000000000000000000000000000000000");
    write_file(
        dir.path(),
        "missing-base.json",
        missing_base.to_string().as_bytes(),
    );
    let err = run(
        dir.path(),
        &[
            "github",
            "pull-request",
            "check",
            "--event-file",
            "missing-base.json",
            "--repo",
            ".",
            "--format",
            "json",
        ],
    );
    assert_eq!(err.status.code(), Some(2));
    assert!(stderr_json(&err)["error"]["message"].is_string());
}

#[test]
fn commit_quality_reports_migration_error_for_retired_local_backend() {
    let dir = tempfile::tempdir().unwrap();
    write_file(
        dir.path(),
        ".yaga.toml",
        b"config-version = 1\n[commit]\n[commit.quality]\nprovider = \"local\"\n",
    );
    let out = run(
        dir.path(),
        &[
            "commit",
            "quality",
            "--config",
            ".yaga.toml",
            "--message",
            "feat(cli): add quality command",
            "--format",
            "json",
        ],
    );
    assert_eq!(out.status.code(), Some(2));
    let err = stderr_str(&out);
    assert!(
        err.contains("provider") || err.contains("local") || err.contains("retired"),
        "{err}"
    );
}

#[test]
fn typos_discovery_rejects_in_repo_and_relative_path_executables() {
    let dir = tempfile::tempdir().unwrap();
    init_git_repo(dir.path());
    write_file(
        dir.path(),
        ".yaga.toml",
        b"config-version = 1\n[commit]\ntypos = \"check\"\n",
    );
    let bin_name = if cfg!(windows) { "typos.exe" } else { "typos" };
    let fake_bin = dir.path().join(bin_name);
    std::fs::write(&fake_bin, b"#!/bin/sh\nexit 0\n").unwrap();
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&fake_bin, std::fs::Permissions::from_mode(0o755)).unwrap();
    }

    // Repository directory on PATH must be rejected fail-closed (exit 2).
    let in_repo = yaga_cmd(dir.path())
        .env("PATH", dir.path())
        .args([
            "commit",
            "check",
            "--config",
            ".yaga.toml",
            "--message",
            "feat(cli): check boundary",
        ])
        .output()
        .unwrap();
    assert_eq!(in_repo.status.code(), Some(2));
    assert!(
        stderr_str(&in_repo).contains("must not be discovered inside the repository"),
        "stderr: {}",
        stderr_str(&in_repo)
    );

    // Relative `.` PATH entry must also be rejected fail-closed (exit 2).
    let relative = yaga_cmd(dir.path())
        .env("PATH", ".")
        .args([
            "commit",
            "check",
            "--config",
            ".yaga.toml",
            "--message",
            "feat(cli): check boundary",
        ])
        .output()
        .unwrap();
    assert_eq!(relative.status.code(), Some(2));
    assert!(
        stderr_str(&relative).contains("must be absolute"),
        "stderr: {}",
        stderr_str(&relative)
    );

    // Missing typos preserves the exact not-installed diagnostic.
    let missing = yaga_cmd(dir.path())
        .env("PATH", dir.path().join("no-tools"))
        .args([
            "commit",
            "check",
            "--config",
            ".yaga.toml",
            "--message",
            "feat(cli): check boundary",
        ])
        .output()
        .unwrap();
    assert_eq!(missing.status.code(), Some(2));
    assert!(
        stderr_str(&missing)
            .contains("commit policy requires the typos executable, but it is not installed"),
        "stderr: {}",
        stderr_str(&missing)
    );
}
