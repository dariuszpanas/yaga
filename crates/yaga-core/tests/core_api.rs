//! Public library integration tests verifying yaga-core works standalone from Rust without Python.

use std::collections::BTreeMap;
use std::path::Path;
#[cfg(feature = "process")]
use yaga_core::policies::audit_policies;
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
    policies::{Kind, audit_bytes, audit_entries, classify_bytes},
    repository::{Provider, parse_plan},
    test_planning::{PlanDocument, Source, TestPolicy, plan_tests},
    workflows::{ReferenceContext, check_uses, parse_workflow},
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

    #[cfg(feature = "process")]
    {
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
}

#[test]
fn pure_in_memory_helpers_and_json_serialization_cover_all_evaluators() {
    // Policy classification & byte-slice audit
    let commit_toml = b"config-version = 1\n[commit]\n";
    let branch_toml = b"branch-policy-version = 1\nallowed-patterns = ['main', 'feat/*']\n";
    assert_eq!(
        classify_bytes(Path::new(".yaga.toml"), commit_toml).unwrap(),
        vec![Kind::Commit]
    );
    let audited = audit_bytes(Path::new(".yaga/branch-policy.toml"), branch_toml);
    assert_eq!(audited.len(), 1);
    assert_eq!(audited[0].status, "current");
    let audit_report = audit_entries(
        &[
            (Path::new(".yaga.toml"), commit_toml),
            (Path::new(".yaga/branch-policy.toml"), branch_toml),
        ],
        true,
    )
    .unwrap();
    assert_eq!(audit_report.exit_code(), 0);
    let audit_json = serde_json::to_value(&audit_report).unwrap();
    assert_eq!(audit_json["entries"][0]["kind"], "commit");
    assert_eq!(audit_json["entries"][1]["kind"], "branch");

    // RepositoryPlan pure relative path helpers
    let plan = parse_plan(
        b"plan-version = 2\nchecks = ['workflow', 'mode', 'size']\nworkflow-paths = ['.github/workflows/ci.yml']\nmode-policy = '.yaga/mode-policy.toml'\nsize-policy = '.yaga/size-policy.toml'\n",
        Path::new(".yaga/checks/ci.toml"),
    )
    .unwrap();
    let rel_policies = plan.relative_policy_paths().unwrap();
    assert_eq!(
        rel_policies,
        BTreeMap::from([
            (Provider::Mode, ".yaga/mode-policy.toml".to_owned()),
            (Provider::Size, ".yaga/size-policy.toml".to_owned()),
        ])
    );
    assert_eq!(
        plan.relative_workflow_paths().unwrap(),
        vec![".github/workflows/ci.yml".to_owned()]
    );
    assert_eq!(
        serde_json::to_value(&rel_policies).unwrap()["mode"],
        ".yaga/mode-policy.toml"
    );

    // BranchReport serialization (valid & invalid)
    let branch_policy = BranchPolicy::new(vec!["feat/*".into()]).unwrap();
    let branch_ok = check_branch_name(&branch_policy, "feat/pure").unwrap();
    let branch_ok_json = serde_json::to_value(&branch_ok).unwrap();
    assert_eq!(branch_ok_json["branch"], "feat/pure");
    assert_eq!(branch_ok_json["valid"], true);
    assert_eq!(branch_ok_json["matched_pattern"], "feat/*");
    assert!(branch_ok_json["diagnostic"].is_null());

    let branch_bad = check_branch_name(&branch_policy, "fix/other").unwrap();
    let branch_bad_json = serde_json::to_value(&branch_bad).unwrap();
    assert_eq!(branch_bad_json["valid"], false);
    assert!(branch_bad_json["matched_pattern"].is_null());
    assert_eq!(branch_bad_json["diagnostic"]["code"], "branch.allowed");

    // Description report serialization
    let desc_policy =
        parse_description_policy(b"description-policy-version = 1\nmin-words = 3\n").unwrap();
    let desc_report = check_description("## Summary\n\nToo short.\n", &desc_policy, None).unwrap();
    let desc_json = serde_json::to_value(&desc_report).unwrap();
    assert_eq!(desc_json["word_count"], 2);
    assert!(desc_json["diagnostic_count"].as_u64().unwrap() > 0);

    // Path evaluator reports & diagnostics serialization
    let change_policy = ChangePolicy::from_toml(
        b"change-policy-version = 1\n[[rules]]\nname = 'code'\nwhen-any = ['src/**']\nrequire-any = ['tests/**']\n",
    )
    .unwrap();
    let change_results = change_policy.check(&["src/lib.rs"]).unwrap();
    assert_eq!(
        serde_json::to_value(change_results[0].status()).unwrap(),
        "failed"
    );
    assert_eq!(
        serde_json::to_value(&change_results).unwrap()[0]["name"],
        "code"
    );

    let mode_policy =
        ModePolicy::from_toml(b"mode-policy-version = 1\ndefault-allowed-modes = ['regular']\n")
            .unwrap();
    let mode_report = mode_policy
        .check_entries(
            &[("run.sh", ModeKind::Executable)],
            &mut WorkBudget::default(),
        )
        .unwrap();
    let mode_json = serde_json::to_value(&mode_report).unwrap();
    assert_eq!(mode_json["finding_count"], 1);
    assert_eq!(mode_json["diagnostics"][0]["actual_kind"], "executable");

    let path_report = PathPolicy::windows_compatible_v1()
        .check(&["README.md", "readme.md"])
        .unwrap();
    let path_json = serde_json::to_value(&path_report).unwrap();
    assert_eq!(
        path_json["diagnostics"][0]["code"],
        "path.ascii-case-collision"
    );
    assert_eq!(path_json["rule_counts"][3][0], "ascii-case-collision");

    let size_policy =
        SizePolicy::from_toml(b"size-policy-version = 1\ndefault-max-blob-bytes = 10\n").unwrap();
    let size_report = size_policy
        .check_blobs(&[("big.bin", 25)], &mut WorkBudget::default())
        .unwrap();
    let size_json = serde_json::to_value(&size_report).unwrap();
    assert_eq!(size_json["total_bytes"], 25);
    assert_eq!(size_json["diagnostics"][0]["code"], "size.blob");

    let tree_policy = TreePolicy::from_toml(
        b"tree-policy-version = 1\nrequired-paths = ['LICENSE']\nforbidden-patterns = []\n",
    )
    .unwrap();
    let tree_findings = tree_policy.check(&["README.md"]).unwrap();
    let tree_json = serde_json::to_value(&tree_findings).unwrap();
    assert_eq!(tree_json[0]["code"], "tree.required");

    // Workflow diagnostic & reference serialization
    let workflow = parse_workflow(
        b"jobs:\n  ci:\n    steps:\n      - uses: actions/checkout@v4\n",
        ".github/workflows/ci.yml",
    )
    .unwrap();
    let wf_findings = workflow.reference_findings();
    let wf_json = serde_json::to_value(&wf_findings).unwrap();
    assert_eq!(wf_json[0]["code"], "uses.pin");
    let ref_json = serde_json::to_value(workflow.references()).unwrap();
    assert_eq!(ref_json[0]["context"], "step");
    assert_eq!(ref_json[0]["value"], "actions/checkout@v4");
}
