mod common;

use common::{commit_all, init_git_repo, run, stderr_str, stdout_json, stdout_str, write_file};

#[test]
fn branch_check_enforces_patterns_syntax_and_formats() {
    let dir = tempfile::tempdir().unwrap();
    write_file(
        dir.path(),
        "branch.toml",
        b"branch-policy-version = 1\nallowed-patterns = [\"main\", \"feat/*\", \"fix/**\"]\n",
    );

    let ok = run(
        dir.path(),
        &[
            "branch",
            "check",
            "--policy",
            "branch.toml",
            "--name",
            "feat/rust-cli",
            "--format",
            "json",
        ],
    );
    assert_eq!(ok.status.code(), Some(0), "stderr: {}", stderr_str(&ok));
    let doc = stdout_json(&ok);
    assert_eq!(doc["valid"], true);
    assert_eq!(doc["matched_pattern"], "feat/*");

    let denied = run(
        dir.path(),
        &[
            "branch",
            "check",
            "--policy",
            "branch.toml",
            "--name",
            "wip/experiment",
            "--format",
            "json",
        ],
    );
    assert_eq!(denied.status.code(), Some(1));
    assert_eq!(
        stdout_json(&denied)["diagnostics"][0]["code"],
        "branch.allowed"
    );

    let syntax = run(
        dir.path(),
        &[
            "branch",
            "check",
            "--policy",
            "branch.toml",
            "--name",
            "feat/bad..name",
            "--format",
            "json",
        ],
    );
    assert_eq!(syntax.status.code(), Some(1));
    assert_eq!(
        stdout_json(&syntax)["diagnostics"][0]["code"],
        "branch.syntax"
    );

    let gh = run(
        dir.path(),
        &[
            "branch",
            "check",
            "--policy",
            "branch.toml",
            "--name",
            "other/name",
            "--format",
            "github",
        ],
    );
    assert_eq!(gh.status.code(), Some(1));
    assert!(stdout_str(&gh).contains("::error "));

    // Quiet mode produces empty stdout
    let quiet = run(
        dir.path(),
        &[
            "branch",
            "check",
            "--policy",
            "branch.toml",
            "--name",
            "main",
            "--quiet",
        ],
    );
    assert_eq!(quiet.status.code(), Some(0));
    assert!(stdout_str(&quiet).is_empty());

    // Invalid policy -> exit 2
    write_file(dir.path(), "bad.toml", b"branch-policy-version = 99\n");
    let err = run(
        dir.path(),
        &["branch", "check", "--policy", "bad.toml", "--name", "main"],
    );
    assert_eq!(err.status.code(), Some(2));
}

#[test]
fn change_check_evaluates_commit_ranges_without_reading_worktree() {
    let dir = tempfile::tempdir().unwrap();
    init_git_repo(dir.path());
    write_file(
        dir.path(),
        "change.toml",
        br#"change-policy-version = 1

[[rules]]
name = "src-needs-tests"
when-any = ["src/**/*.rs"]
require-any = ["tests/**/*.rs"]
"#,
    );
    write_file(dir.path(), "README.md", b"# demo\n");
    let base = commit_all(dir.path(), "chore: initial");

    // Touch src/ without tests/
    write_file(dir.path(), "src/lib.rs", b"pub fn a() {}\n");
    let missing_test_head = commit_all(dir.path(), "feat(core): add lib");

    // Even if an uncommitted test file sits in the worktree, change check must inspect ONLY git range!
    write_file(dir.path(), "tests/lib_test.rs", b"#[test] fn t() {}\n");
    let range1 = format!("{base}..{missing_test_head}");
    let fail = run(
        dir.path(),
        &[
            "change",
            "check",
            "--policy",
            "change.toml",
            "--range",
            &range1,
            "--format",
            "json",
        ],
    );
    assert_eq!(fail.status.code(), Some(1), "stderr: {}", stderr_str(&fail));
    let doc = stdout_json(&fail);
    assert_eq!(doc["valid"], false);
    assert_eq!(doc["rules"][0]["code"], "change.require_any");

    // Commit the test file and verify three-dot range passes
    let ok_head = commit_all(dir.path(), "test(core): add lib test");
    let range2 = format!("{base}...{ok_head}");
    let ok = run(
        dir.path(),
        &[
            "change",
            "check",
            "--policy",
            "change.toml",
            "--range",
            &range2,
            "--format",
            "json",
        ],
    );
    assert_eq!(ok.status.code(), Some(0), "stderr: {}", stderr_str(&ok));
    assert_eq!(stdout_json(&ok)["valid"], true);
}

#[test]
fn committed_tree_providers_check_mode_path_size_and_tree() {
    let dir = tempfile::tempdir().unwrap();
    init_git_repo(dir.path());

    write_file(dir.path(), "README.md", b"hello world\n");
    write_file(dir.path(), "src/app.rs", b"fn main() {}\n");
    write_file(dir.path(), "assets/big.bin", &[b'x'; 200]);
    write_file(dir.path(), "docs/secret.env", b"KEY=1\n");
    let rev = commit_all(dir.path(), "chore: seed tree");

    // Add an untracked oversized file in the worktree; committed checks must ignore it.
    write_file(dir.path(), "untracked_huge.bin", &[b'z'; 10_000]);

    // 1. mode check (all regular -> pass)
    write_file(
        dir.path(),
        "mode.toml",
        b"mode-policy-version = 1\ndefault-allowed-modes = [\"regular\"]\n",
    );
    let mode_ok = run(
        dir.path(),
        &[
            "mode",
            "check",
            "--policy",
            "mode.toml",
            "--revision",
            &rev,
            "--format",
            "json",
        ],
    );
    assert_eq!(
        mode_ok.status.code(),
        Some(0),
        "stderr: {}",
        stderr_str(&mode_ok)
    );
    assert_eq!(stdout_json(&mode_ok)["valid"], true);

    // 2. path check (windows-compatible-v1 -> pass on clean tree)
    write_file(
        dir.path(),
        "path.toml",
        b"path-policy-version = 1\nprofile = \"windows-compatible-v1\"\n",
    );
    let path_ok = run(
        dir.path(),
        &[
            "path",
            "check",
            "--policy",
            "path.toml",
            "--revision",
            &rev,
            "--format",
            "json",
        ],
    );
    assert_eq!(
        path_ok.status.code(),
        Some(0),
        "stderr: {}",
        stderr_str(&path_ok)
    );
    assert_eq!(stdout_json(&path_ok)["valid"], true);

    // 3. size check: assets/big.bin (200 bytes) fails 100-byte default limit
    write_file(
        dir.path(),
        "size.toml",
        b"size-policy-version = 1\ndefault-max-blob-bytes = 100\n",
    );
    let size_fail = run(
        dir.path(),
        &[
            "size",
            "check",
            "--policy",
            "size.toml",
            "--revision",
            &rev,
            "--format",
            "json",
        ],
    );
    assert_eq!(size_fail.status.code(), Some(1));
    let size_doc = stdout_json(&size_fail);
    assert_eq!(size_doc["diagnostics"][0]["code"], "size.blob");
    assert_eq!(size_doc["diagnostics"][0]["path"], "assets/big.bin");

    // Adding path-limits override makes size check pass (ignoring 10KB untracked file)
    write_file(
        dir.path(),
        "size-override.toml",
        br#"size-policy-version = 1
default-max-blob-bytes = 100
max-total-blob-bytes = 500

[[path-limits]]
pattern = "assets/*.bin"
max-blob-bytes = 256
"#,
    );
    let size_ok = run(
        dir.path(),
        &[
            "size",
            "check",
            "--policy",
            "size-override.toml",
            "--revision",
            &rev,
        ],
    );
    assert_eq!(
        size_ok.status.code(),
        Some(0),
        "stderr: {}",
        stderr_str(&size_ok)
    );

    // 4. tree check: missing LICENSE and forbidden *.env
    write_file(
        dir.path(),
        "tree.toml",
        b"tree-policy-version = 1\nrequired-paths = [\"README.md\", \"LICENSE\"]\nforbidden-patterns = [\"**/*.env\"]\n",
    );
    let tree_fail = run(
        dir.path(),
        &[
            "tree",
            "check",
            "--policy",
            "tree.toml",
            "--revision",
            &rev,
            "--format",
            "json",
        ],
    );
    assert_eq!(tree_fail.status.code(), Some(1));
    let tree_doc = stdout_json(&tree_fail);
    let codes: Vec<&str> = tree_doc["diagnostics"]
        .as_array()
        .unwrap()
        .iter()
        .map(|d| d["code"].as_str().unwrap())
        .collect();
    assert!(codes.contains(&"tree.required"), "{codes:?}");
    assert!(codes.contains(&"tree.forbidden"), "{codes:?}");

    // Overlapping required + forbidden pattern -> exit 2
    write_file(
        dir.path(),
        "tree-overlap.toml",
        b"tree-policy-version = 1\nrequired-paths = [\"README.md\"]\nforbidden-patterns = [\"*.md\"]\n",
    );
    let tree_err = run(
        dir.path(),
        &[
            "tree",
            "check",
            "--policy",
            "tree-overlap.toml",
            "--revision",
            &rev,
        ],
    );
    assert_eq!(tree_err.status.code(), Some(2));
}
