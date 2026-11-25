use super::*;
use std::path::Path;

fn parse(text: &str) -> Result<RepositoryPlan, String> {
    parse_plan(text.as_bytes(), Path::new("plan.toml"))
}

#[test]
fn plans_have_closed_version_specific_surface() {
    let v1 = parse("plan-version=1\nchecks=['workflow-lint','commit','workflow']").unwrap();
    assert_eq!(
        v1.selection.providers.into_iter().collect::<Vec<_>>(),
        [Provider::Commit, Provider::Workflow, Provider::WorkflowLint]
    );
    for bad in [
        "plan-version=1\nchecks=['path']",
        "plan-version=1\nchecks=['commit']\nrequired-version='>=0.0.0'",
        "plan-version=2\nchecks=['path']",
        "plan-version=2\nchecks=['commit']\nmode-policy='m.toml'",
        "plan-version=2\nchecks=['tree','tree']\ntree-policy='t.toml'",
        "plan-version=2\nchecks=['commit']\nrevision='HEAD'",
        "plan-version=2\nchecks=['workflow','workflow-lint']\nworkflow-security-profile='recommended-v3'",
    ] {
        assert!(parse(bad).is_err(), "{bad}");
    }
}

#[test]
fn portable_paths_and_casefolded_duplicates() {
    let prefix = "plan-version=2\nchecks=['mode','path']\nmode-policy='.yaga/straße'\npath-policy=";
    assert!(parse(&format!("{prefix}'.yaga/STRASSE'")).is_err());
    for path in [
        "../x",
        "x//y",
        "x/./y",
        "x/COM¹.log",
        "~/settings",
        "x/a.toml.",
        "x/<>",
        "x/\u{200b}file",
        "/root",
        "C:root",
    ] {
        assert!(parse(&format!("{prefix}'{path}'")).is_err(), "{path}");
    }
    let result = parse(&format!("{prefix}'.yaga/paths.toml'")).unwrap();
    assert_eq!(result.selection.policy_paths.len(), 2);
}

#[test]
fn runtime_arguments_have_exclusive_owners() {
    let p = Selection {
        providers: normalize_providers(&["tree".into()]).unwrap(),
        policy_paths: BTreeMap::from([(Provider::Tree, PathBuf::from("tree.toml"))]),
        ..Default::default()
    };
    assert!(p.validate(false, false, false, true).is_ok());
    assert!(p.validate(false, false, false, false).is_err());
    assert!(p.validate(false, true, false, true).is_err());
    assert_eq!(
        normalize_security(
            None,
            &[
                "checkout.persist_credentials".into(),
                "permissions.explicit".into()
            ]
        )
        .unwrap()
        .1,
        ["permissions.explicit", "checkout.persist_credentials"]
    );
    assert_eq!(normalize_security(None, &[]).unwrap().1.len(), 5);
}

#[cfg(all(unix, feature = "process"))]
#[test]
fn missing_target_under_outside_symlink_is_still_an_escape() {
    let temp = tempfile::tempdir().unwrap();
    let repo = temp.path().join("repo");
    let outside = temp.path().join("outside");
    std::fs::create_dir_all(&repo).unwrap();
    std::fs::create_dir_all(&outside).unwrap();
    std::os::unix::fs::symlink(outside, repo.join("policy")).unwrap();
    let plan = parse("plan-version=2\nchecks=['size']\nsize-policy='policy/missing'").unwrap();
    assert!(plan.bind(&repo).is_err());
}

#[test]
fn relative_policy_and_workflow_paths_stay_posix_and_pure() {
    let plan = parse(
        "plan-version = 2\n\
         checks = ['workflow', 'mode', 'tree']\n\
         workflow-paths = ['.github/workflows/ci.yml', '.github/workflows/release.yml']\n\
         mode-policy = '.yaga/mode-policy.toml'\n\
         tree-policy = '.yaga/tree-policy.toml'\n",
    )
    .unwrap();
    assert_eq!(
        plan.relative_policy_paths().unwrap(),
        BTreeMap::from([
            (Provider::Mode, ".yaga/mode-policy.toml".to_owned()),
            (Provider::Tree, ".yaga/tree-policy.toml".to_owned()),
        ])
    );
    assert_eq!(
        plan.relative_workflow_paths().unwrap(),
        vec![
            ".github/workflows/ci.yml".to_owned(),
            ".github/workflows/release.yml".to_owned(),
        ]
    );

    // Manually tampered paths are still rejected fail-closed without filesystem access.
    for bad in [
        "../escape.toml",
        "/abs.toml",
        ".yaga\\mode.toml",
        "a/./b.toml",
    ] {
        let mut tampered = plan.clone();
        tampered
            .selection
            .policy_paths
            .insert(Provider::Mode, PathBuf::from(bad));
        assert!(tampered.relative_policy_paths().is_err(), "{bad}");

        let mut bad_wf = plan.clone();
        bad_wf.selection.workflow_paths = vec![PathBuf::from(bad)];
        assert!(bad_wf.relative_workflow_paths().is_err(), "{bad}");
    }

    // Casefolded duplicates are rejected consistently with plan parsing.
    let mut dup_policy = plan.clone();
    dup_policy
        .selection
        .policy_paths
        .insert(Provider::Tree, PathBuf::from(".YAGA/MODE-POLICY.TOML"));
    assert!(dup_policy.relative_policy_paths().is_err());

    let mut dup_workflow = plan;
    dup_workflow.selection.workflow_paths = vec![
        PathBuf::from(".github/workflows/ci.yml"),
        PathBuf::from(".github/workflows/CI.YML"),
    ];
    assert!(dup_workflow.relative_workflow_paths().is_err());
}
