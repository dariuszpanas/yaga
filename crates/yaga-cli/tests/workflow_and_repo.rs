mod common;

use common::{commit_all, init_git_repo, run, stderr_str, stdout_json, stdout_str, write_file};

const PINNED_WORKFLOW: &str = r#"name: CI
on:
  pull_request:
permissions:
  contents: read
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1
        with:
          persist-credentials: false
      - run: echo ok
"#;

const UNPINNED_WORKFLOW: &str = r#"name: CI
on:
  pull_request:
permissions:
  contents: read
  pull-requests: write
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: echo ok
"#;

#[test]
fn workflow_check_enforces_immutable_sha_and_container_digests() {
    let dir = tempfile::tempdir().unwrap();
    write_file(
        dir.path(),
        ".github/workflows/ok.yml",
        PINNED_WORKFLOW.as_bytes(),
    );
    let ok = run(
        dir.path(),
        &["workflow", "check", "--repo", ".", "--format", "json"],
    );
    assert_eq!(ok.status.code(), Some(0), "stderr: {}", stderr_str(&ok));
    assert_eq!(stdout_json(&ok)["valid"], true);

    write_file(
        dir.path(),
        ".github/workflows/bad.yml",
        UNPINNED_WORKFLOW.as_bytes(),
    );
    let fail = run(
        dir.path(),
        &["workflow", "check", "--repo", ".", "--format", "json"],
    );
    assert_eq!(fail.status.code(), Some(1));
    let doc = stdout_json(&fail);
    assert_eq!(doc["valid"], false);
    assert!(doc["failed"].as_u64().unwrap() >= 1);

    // Container image without sha256 digest fails; disabled empty service image passes
    let container_wf = r#"name: Services
on: [push]
permissions:
  contents: read
jobs:
  test:
    runs-on: ubuntu-latest
    container:
      image: python:3.12
    services:
      disabled:
        image: ""
    steps:
      - run: echo hi
"#;
    write_file(dir.path(), "wf/container.yml", container_wf.as_bytes());
    let cfail = run(
        dir.path(),
        &[
            "workflow",
            "check",
            "--repo",
            ".",
            "wf/container.yml",
            "--format",
            "json",
        ],
    );
    assert_eq!(cfail.status.code(), Some(1));
}

#[test]
fn workflow_security_respects_v1_v2_v3_profiles_and_privileged_checkout() {
    let dir = tempfile::tempdir().unwrap();

    // Missing persist-credentials: false passes v1, fails v2
    let no_persist_false = r#"name: CI
on:
  pull_request:
permissions:
  contents: read
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1
      - run: echo ok
"#;
    write_file(
        dir.path(),
        ".github/workflows/v1.yml",
        no_persist_false.as_bytes(),
    );
    let v1_ok = run(
        dir.path(),
        &[
            "workflow",
            "security",
            "--repo",
            ".",
            "--profile",
            "recommended-v1",
            ".github/workflows/v1.yml",
        ],
    );
    assert_eq!(
        v1_ok.status.code(),
        Some(0),
        "stderr: {}",
        stderr_str(&v1_ok)
    );

    let v2_fail = run(
        dir.path(),
        &[
            "workflow",
            "security",
            "--repo",
            ".",
            "--profile",
            "recommended-v2",
            ".github/workflows/v1.yml",
            "--format",
            "json",
        ],
    );
    assert_eq!(v2_fail.status.code(), Some(1));

    // Job-level pull-requests: write on pull_request passes v2, fails v3
    let pr_write = r#"name: Writer
on:
  pull_request:
permissions:
  contents: read
jobs:
  write:
    runs-on: ubuntu-latest
    permissions:
      contents: read
      pull-requests: write
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1
        with:
          persist-credentials: false
      - run: echo ok
"#;
    write_file(
        dir.path(),
        ".github/workflows/pr-write.yml",
        pr_write.as_bytes(),
    );
    let v2_pass = run(
        dir.path(),
        &[
            "workflow",
            "security",
            "--repo",
            ".",
            "--profile",
            "recommended-v2",
            ".github/workflows/pr-write.yml",
        ],
    );
    assert_eq!(
        v2_pass.status.code(),
        Some(0),
        "stderr: {}",
        stderr_str(&v2_pass)
    );

    let v3_fail = run(
        dir.path(),
        &[
            "workflow",
            "security",
            "--repo",
            ".",
            "--profile",
            "recommended-v3",
            ".github/workflows/pr-write.yml",
            "--format",
            "json",
        ],
    );
    assert_eq!(v3_fail.status.code(), Some(1));
    assert!(stdout_str(&v3_fail).contains("permissions.pull_request_write"));

    // Privileged pull_request_target checkout with dynamic ref fails closed
    let priv_checkout = r#"name: Priv
on:
  pull_request_target:
permissions:
  contents: read
jobs:
  bad:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1
        with:
          ref: ${{ github.event.pull_request.head.sha }}
          persist-credentials: false
"#;
    write_file(
        dir.path(),
        ".github/workflows/priv.yml",
        priv_checkout.as_bytes(),
    );
    let priv_fail = run(
        dir.path(),
        &[
            "workflow",
            "security",
            "--repo",
            ".",
            ".github/workflows/priv.yml",
        ],
    );
    assert_eq!(priv_fail.status.code(), Some(1));
}

#[test]
fn repo_check_runs_plan_v2_providers_in_canonical_order_and_rejects_flag_mixing() {
    let dir = tempfile::tempdir().unwrap();
    init_git_repo(dir.path());

    write_file(
        dir.path(),
        ".yaga.toml",
        b"config-version = 1\n[commit]\nallowed-types = [\"feat\", \"fix\", \"chore\"]\n",
    );
    write_file(
        dir.path(),
        ".github/workflows/ci.yml",
        PINNED_WORKFLOW.as_bytes(),
    );
    write_file(
        dir.path(),
        ".yaga/mode-policy.toml",
        b"mode-policy-version = 1\ndefault-allowed-modes = [\"regular\"]\n",
    );
    write_file(
        dir.path(),
        ".yaga/path-policy.toml",
        b"path-policy-version = 1\nprofile = \"windows-compatible-v1\"\n",
    );
    write_file(
        dir.path(),
        ".yaga/size-policy.toml",
        b"size-policy-version = 1\ndefault-max-blob-bytes = 65536\n",
    );
    write_file(
        dir.path(),
        ".yaga/tree-policy.toml",
        b"tree-policy-version = 1\nrequired-paths = [\".yaga.toml\"]\nforbidden-patterns = [\"**/*.env\"]\n",
    );
    write_file(
        dir.path(),
        ".yaga/checks/ci.toml",
        br#"plan-version = 2
checks = ["tree", "workflow-security", "commit", "mode", "path", "size", "workflow"]
workflow-paths = [".github/workflows"]
workflow-security-profile = "recommended-v3"
mode-policy = ".yaga/mode-policy.toml"
path-policy = ".yaga/path-policy.toml"
size-policy = ".yaga/size-policy.toml"
tree-policy = ".yaga/tree-policy.toml"
"#,
    );
    let rev = commit_all(dir.path(), "chore: add valid repository plan");

    let ok = run(
        dir.path(),
        &[
            "repo",
            "check",
            "--plan",
            ".yaga/checks/ci.toml",
            "--commit",
            &rev,
            "--revision",
            &rev,
            "--format",
            "json",
        ],
    );
    assert_eq!(ok.status.code(), Some(0), "stderr: {}", stderr_str(&ok));
    let doc = stdout_json(&ok);
    assert_eq!(doc["valid"], true);
    let providers: Vec<&str> = doc["checks"]
        .as_array()
        .unwrap()
        .iter()
        .map(|p| p["provider"].as_str().unwrap())
        .collect();
    assert_eq!(
        providers,
        vec![
            "commit",
            "workflow",
            "workflow-security",
            "mode",
            "path",
            "size",
            "tree"
        ]
    );

    // Omitting --revision when committed-tree providers are selected -> exit 2
    let no_rev = run(
        dir.path(),
        &["repo", "check", "--plan", ".yaga/checks/ci.toml"],
    );
    assert_eq!(no_rev.status.code(), Some(2));

    // Mixing --plan with --check -> exit 2
    let mixed = run(
        dir.path(),
        &[
            "repo",
            "check",
            "--plan",
            ".yaga/checks/ci.toml",
            "--check",
            "commit",
            "--revision",
            &rev,
        ],
    );
    assert_eq!(mixed.status.code(), Some(2));
}

#[test]
fn policy_init_and_policy_check_cover_all_families() {
    let dir = tempfile::tempdir().unwrap();

    // --dry-run reports planned creations without writing
    let dry = run(
        dir.path(),
        &[
            "policy",
            "init",
            "--all",
            "--repo",
            ".",
            "--dry-run",
            "--format",
            "json",
        ],
    );
    assert_eq!(dry.status.code(), Some(0), "stderr: {}", stderr_str(&dry));
    assert!(!dir.path().join(".yaga").exists());

    // Real init --all creates starter policies
    let init = run(
        dir.path(),
        &["policy", "init", "--all", "--repo", ".", "--format", "json"],
    );
    assert_eq!(init.status.code(), Some(0), "stderr: {}", stderr_str(&init));
    assert!(dir.path().join(".yaga.toml").is_file());
    assert!(dir.path().join(".yaga/branch-policy.toml").is_file());

    // Re-running with --skip-existing succeeds without error
    let skip = run(
        dir.path(),
        &["policy", "init", "--all", "--skip-existing", "--repo", "."],
    );
    assert_eq!(skip.status.code(), Some(0), "stderr: {}", stderr_str(&skip));

    // policy check --strict validates all generated policies
    let audit = run(
        dir.path(),
        &[
            "policy", "check", "--repo", ".", "--strict", "--format", "json",
        ],
    );
    assert_eq!(
        audit.status.code(),
        Some(0),
        "stderr: {}",
        stderr_str(&audit)
    );
    let doc = stdout_json(&audit);
    assert_eq!(doc["valid"], true);
}
