//! Public library integration tests verifying yaga-core works standalone from Rust without Python.

use std::path::Path;
use yaga_core::{
    branch::{BranchPolicy, check_branch_name},
    commits::{CommitPolicy, check_message, parse_config},
    descriptions::{check_description, policy::parse_description_policy},
    paths::{
        WorkBudget,
        changes::{ChangePolicy, Status},
        modes::ModePolicy,
        portability::PathPolicy,
        records::ModeKind,
        sizes::SizePolicy,
        tree::TreePolicy,
    },
    policies::audit_policies,
    repository::parse_plan,
    test_planning::{PlanDocument, Source, TestPolicy, plan_tests},
    workflows::{ReferenceContext, check_uses},
};

#[test]
fn commits_library_api_validates_messages_and_configs() {
    let loaded = parse_config(
        br#"config-version = 1
[commit]
allowed-types = ["feat", "fix"]
scope-policy = "required"
body-policy = "required"
body-min-words = 4
breaking-markers = "paired"
"#,
        Path::new(".yaga.toml"),
    )
    .unwrap();

    let ok = check_message(
        "feat(core): ship native library\n\nExpose all checks cleanly in Rust.",
        &loaded.policy,
        false,
        false,
    );
    assert!(ok.valid(), "{:?}", ok.diagnostics);

    let fail = check_message(
        "feat!: missing scope and body",
        &loaded.policy,
        false,
        false,
    );
    assert!(!fail.valid());
    let codes: Vec<&str> = fail.diagnostics.iter().map(|d| d.code).collect();
    assert!(codes.contains(&"scope.required"), "{codes:?}");
    assert!(codes.contains(&"body.required"), "{codes:?}");
    assert!(codes.contains(&"breaking.marker-pair"), "{codes:?}");

    // Default policy allows standard conventional commits
    let default_ok = check_message(
        "fix: handle edge case",
        &CommitPolicy::default(),
        false,
        false,
    );
    assert!(default_ok.valid());
}

#[test]
fn branch_and_description_library_apis_validate_cleanly() {
    let policy = BranchPolicy::new(vec!["main".into(), "feat/*".into()]).unwrap();
    assert!(check_branch_name(&policy, "feat/rust").unwrap().valid());
    assert!(!check_branch_name(&policy, "chore/other").unwrap().valid());
    assert!(
        !check_branch_name(&policy, "feat/bad..branch")
            .unwrap()
            .valid()
    );

    let desc_policy = parse_description_policy(
        b"description-policy-version = 1\nmin-words = 4\nplaceholders = [\"TODO\"]\n",
    )
    .unwrap();
    let ok = check_description(
        "## Summary\n\nPure Rust description check works cleanly.\n",
        &desc_policy,
        None,
    )
    .unwrap();
    assert_eq!(ok.diagnostic_count, 0);

    let bad = check_description("## Summary\n\nTODO\n", &desc_policy, None).unwrap();
    assert!(bad.diagnostic_count > 0);
}

#[test]
fn path_change_mode_size_and_tree_library_apis_are_pure() {
    let change_policy = ChangePolicy::from_toml(
        b"change-policy-version = 1\n[[rules]]\nname = 'code'\nwhen-any = ['src/**']\nrequire-any = ['tests/**']\n",
    )
    .unwrap();
    let failed = change_policy.check(&["src/lib.rs"]).unwrap();
    assert_eq!(failed[0].status(), Status::Failed);
    let passed = change_policy
        .check(&["src/lib.rs", "tests/lib.rs"])
        .unwrap();
    assert_eq!(passed[0].status(), Status::Passed);

    let mode_policy =
        ModePolicy::from_toml(b"mode-policy-version = 1\ndefault-allowed-modes = ['regular']\n")
            .unwrap();
    let entries = [
        ("README.md", ModeKind::Regular),
        ("run.sh", ModeKind::Executable),
    ];
    let mode_report = mode_policy
        .check_entries(&entries, &mut WorkBudget::default())
        .unwrap();
    assert_eq!(mode_report.finding_count, 1);

    let path_policy = PathPolicy::windows_compatible_v1();
    let path_report = path_policy.check(&["README.md", "readme.md"]).unwrap();
    assert!(path_report.finding_count() > 0);

    let size_policy = SizePolicy::from_toml(
        b"size-policy-version = 1\ndefault-max-blob-bytes = 100\nmax-total-blob-bytes = 150\n",
    )
    .unwrap();
    let size_entries = [("a.txt", 80_u64), ("b.txt", 80_u64)];
    let size_report = size_policy
        .check_blobs(&size_entries, &mut WorkBudget::default())
        .unwrap();
    assert!(!size_report.diagnostics.is_empty());

    let tree_policy = TreePolicy::from_toml(
        b"tree-policy-version = 1\nrequired-paths = ['LICENSE']\nforbidden-patterns = ['**/*.env']\n",
    )
    .unwrap();
    let findings = tree_policy.check(&["config/.env"]).unwrap();
    assert_eq!(findings.len(), 2);
}

#[test]
fn workflow_plan_test_planning_and_policy_audit_apis_work_standalone() {
    assert!(
        check_uses(
            "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
            ReferenceContext::Step
        )
        .is_none()
    );
    assert!(check_uses("actions/checkout@v4", ReferenceContext::Step).is_some());

    let plan = parse_plan(
        b"plan-version = 1\nchecks = ['commit', 'workflow']\n",
        Path::new("plan.toml"),
    )
    .unwrap();
    assert_eq!(plan.version, 1);

    let tp = TestPolicy::parse(
        b"test-policy-version = 1\n[[suites]]\nid = 'unit'\nbinding = 'cargo'\n[profiles.full]\nmode = 'full'\n",
    )
    .unwrap();
    let selection = plan_tests(&tp, "full", None).unwrap();
    let doc = PlanDocument::new(
        &tp.sha256,
        None,
        Source {
            base_commit: None,
            comparison_commit: None,
            head_commit: None,
            execution_commit: "1".repeat(40),
            execution_tree: "2".repeat(40),
        },
        selection,
    )
    .unwrap();
    assert_eq!(doc.plan_digest.len(), 64);

    // Empty repo reports no policies; adding .yaga.toml audits cleanly
    let dir = tempfile::tempdir().unwrap();
    assert!(audit_policies(dir.path(), &[], false).is_err());
    std::fs::write(
        dir.path().join(".yaga.toml"),
        "config-version = 1\n[commit]\n",
    )
    .unwrap();
    let audit = audit_policies(dir.path(), &[], false).unwrap();
    assert_eq!(audit.exit_code(), 0);
}
