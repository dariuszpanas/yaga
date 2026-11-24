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

#[cfg(unix)]
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
