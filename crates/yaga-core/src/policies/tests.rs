use super::*;
#[cfg(feature = "process")]
use std::fs;
#[cfg(feature = "process")]
use tempfile::tempdir;

#[cfg(feature = "process")]
#[test]
fn prepare_starters_uses_existing_tree_policies_and_preserves_selection_order() {
    let dir = tempdir().unwrap();
    let selected = [Kind::Repo, Kind::Size, Kind::Mode];
    let selection = InitSelection::new(&selected).unwrap();
    assert_eq!(
        selection.inspection_kinds(),
        &[Kind::Repo, Kind::Size, Kind::Mode, Kind::Path, Kind::Tree]
    );
    let path = dir.path().join(".yaga/mode-policy.toml");
    let existing = [(Kind::Mode, path.clone())].into();
    let rows = selection
        .prepare(dir.path(), &existing, true, true)
        .collect::<Result<Vec<_>, _>>()
        .unwrap();
    assert_eq!(
        rows.iter().map(|row| row.item.kind).collect::<Vec<_>>(),
        selected
    );
    let repo = rows[0].starter.as_deref().unwrap();
    assert!(repo.contains("\nmode-policy =") && repo.contains("\nsize-policy ="));
    assert_eq!(rows[0].item.status, "dry-run");
    assert_eq!(rows[2].item.path, path);
    assert_eq!(rows[2].item.status, "skipped");
    assert!(rows[2].starter.is_none());
    assert_eq!(
        selection
            .prepare(dir.path(), &existing, true, false)
            .collect::<Result<Vec<_>, _>>()
            .unwrap_err()
            .target,
        path
    );
    assert!(dir.path().read_dir().unwrap().next().is_none());
}

#[cfg(unix)]
#[test]
fn undecodable_filename_preserves_plan_directory_hint() {
    use std::ffi::OsStr;
    use std::os::unix::ffi::OsStrExt;
    use std::path::Path;

    let path = Path::new(OsStr::from_bytes(b"checks/custom-\xff.toml"));
    assert_eq!(
        classify_policy(path, |_| false, std::iter::empty()),
        vec![Kind::Repo]
    );
    assert_eq!(
        classify_policy(path, |key| key == "test-policy-version", std::iter::empty()),
        vec![Kind::Test]
    );
}

#[cfg(feature = "process")]
#[test]
fn policy_audit_reports_strict_utf8_byte_offsets() {
    let dir = tempdir().unwrap();
    let path = dir.path().join("branch-policy.toml");
    for (input, detail) in [
        (&b"\xff"[..], "byte 0xff in position 0: invalid start byte"),
        (
            b"#\xc3\xa9\n\x80",
            "byte 0x80 in position 4: invalid start byte",
        ),
        (
            b"\xe0\x80",
            "byte 0xe0 in position 0: invalid continuation byte",
        ),
        (
            b"\xef\xbb\xbf# x\n\xe2\x82",
            "bytes in position 4-5: unexpected end of data",
        ),
        (
            b"\xf0\x9fX",
            "bytes in position 0-1: invalid continuation byte",
        ),
        (
            b"\xf4\x8f\xbf",
            "bytes in position 0-2: unexpected end of data",
        ),
    ] {
        fs::write(&path, input).unwrap();
        let report = audit_policies(dir.path(), std::slice::from_ref(&path), false).unwrap();
        assert_eq!(report.exit_code(), 2);
        assert_eq!(report.entries.len(), 1);
        assert_eq!(
            report.entries[0].message,
            format!("invalid UTF-8 TOML: 'utf-8' codec can't decode {detail}")
        );
    }
}

#[cfg(feature = "process")]
#[test]
fn initialize_all_and_audit_every_family() {
    let dir = tempdir().unwrap();
    let repo = dir.path();
    let preview = initialize_policies(repo, Kind::ALL, true, false).unwrap();
    assert_eq!(preview.items.len(), 12);
    assert_eq!(fs::read_dir(repo).unwrap().count(), 0);
    let created = initialize_policies(repo, Kind::ALL, false, false).unwrap();
    assert!(created.items.iter().all(|i| i.status == "created"));
    let skipped = initialize_policies(repo, Kind::ALL, false, true).unwrap();
    assert!(skipped.items.iter().all(|i| i.status == "skipped"));
    let report = audit_policies(repo, &[], true).unwrap();
    assert_eq!(report.exit_code(), 0, "{}", report.render(Format::Text));
    assert_eq!(report.entries.len(), 12);
    assert_eq!(report.entries[0].kind, Some(Kind::Commit));
    let fixture = "plan-version=1\nchecks=['commit']\n";
    let path = repo.join(Kind::Repo.default_path());
    fs::write(&path, fixture).unwrap();
    let strict = audit_policies(repo, &[path.clone(), path.clone()], true).unwrap();
    assert_eq!(strict.entries.len(), 1);
    assert_eq!(strict.exit_code(), 1);
    assert!(
        strict
            .render(Format::Github)
            .contains("::error title=YAGA policy")
    );
    fs::write(&path, "plan-version=3\nchecks=['commit']").unwrap();
    assert_eq!(audit_policies(repo, &[path], false).unwrap().exit_code(), 2);
}

#[cfg(feature = "process")]
#[test]
fn policy_initialization_preflights_whole_set() {
    let dir = tempdir().unwrap();
    fs::write(
        dir.path().join("pyproject.toml"),
        "[tool.yaga.commit]\nscope-policy='required'",
    )
    .unwrap();
    assert!(initialize_policies(dir.path(), &[Kind::Branch, Kind::Commit], false, false).is_err());
    assert!(!dir.path().join(".yaga").exists());
    let report =
        initialize_policies(dir.path(), &[Kind::Branch, Kind::Commit], false, true).unwrap();
    assert_eq!(report.items[0].status, "created");
    assert_eq!(
        report.items[1].path,
        crate::fs::absolute_path(&dir.path().join("pyproject.toml")).unwrap()
    );
}

#[test]
fn test_and_triage_policies_fail_on_ambiguous_identities() {
    let policy=b"test-policy-version=1\n[[suites]]\nid='one'\nbinding='one'\nneeds=['two']\n[[suites]]\nid='two'\nbinding='two'\nneeds=['one']\n[profiles.ci]\nmode='full'";
    assert!(
        testing::TestPolicy::parse(policy)
            .unwrap_err()
            .contains("cycle")
    );
    let policy=b"triage-policy-version=1\nhost='github.com'\nrepository-id=1\nmaintainer-user-ids=[2]\n[[trusted-bots]]\nuser-id=2\nlogin='bot[bot]'\nallowed-patterns=['deps/**']";
    assert!(
        triage::TriagePolicy::parse(policy)
            .unwrap_err()
            .contains("overlap")
    );
    let dated=b"test-policy-version=1\n[[suites]]\nid=2001-01-01\nbinding='ok'\n[profiles.ci]\nmode='full'";
    assert!(testing::TestPolicy::parse(dated).is_err());
    assert!(
        testing::TestPolicy::parse(b"test-policy-version = 2\n")
            .unwrap_err()
            .contains("found newer schema version 2; update YAGA using its installation manager")
    );
    assert!(
        triage::TriagePolicy::parse(b"triage-policy-version = 3\n")
            .unwrap_err()
            .contains("found newer schema version 3; update YAGA using its installation manager")
    );
}

#[cfg(feature = "process")]
#[test]
fn discovery_rejects_escape_and_cycles_are_finite() {
    #[cfg(unix)]
    {
        use std::os::unix::fs::symlink;
        let dir = tempdir().unwrap();
        let outside = tempdir().unwrap();
        let repo = dir.path();
        let yaga = repo.join(".yaga");
        fs::create_dir(&yaga).unwrap();
        symlink(&yaga, yaga.join("again")).unwrap();
        initialize_policies(repo, &[Kind::Change], false, false).unwrap();
        assert_eq!(audit_policies(repo, &[], false).unwrap().entries.len(), 1);
        symlink(outside.path(), yaga.join("outside")).unwrap();
        assert!(audit_policies(repo, &[], false).is_err());
    }
}

#[cfg(feature = "process")]
#[test]
fn nested_starter_paths_use_native_separators_on_both_dry_runs() {
    let directory = tempdir().unwrap();
    let repo = crate::fs::absolute_path(directory.path()).unwrap();
    let expected = repo.join(".yaga").join("checks").join("ci.toml");
    let kinds = [Kind::Repo];
    for dry_run in [true, false, true] {
        let report = initialize_policies(&repo, &kinds, dry_run, true).unwrap();
        assert_eq!(report.items[0].path.as_os_str(), expected.as_os_str());
    }
}

#[test]
fn byte_slice_classification_and_auditing_match_all_twelve_families() {
    let all_tree = TREE_KINDS.into_iter().collect::<HashSet<_>>();
    let rendered: Vec<(String, String)> = Kind::ALL
        .iter()
        .map(|&kind| {
            (
                kind.default_path().to_owned(),
                render_starter(kind, &all_tree),
            )
        })
        .collect();
    for (&kind, (path, body)) in Kind::ALL.iter().zip(&rendered) {
        let p = Path::new(path);
        assert_eq!(classify_bytes(p, body.as_bytes()).unwrap(), vec![kind]);
        let entries = audit_bytes(p, body.as_bytes());
        assert_eq!(entries.len(), 1, "{path}: {entries:?}");
        assert_eq!(entries[0].kind, Some(kind));
        assert_eq!(entries[0].status, "current");
        assert_eq!(entries[0].source_version, Some(kind.current()));
        assert_eq!(entries[0].current_version, Some(kind.current()));
    }
    let inputs: Vec<(&Path, &[u8])> = rendered
        .iter()
        .map(|(path, body)| (Path::new(path.as_str()), body.as_bytes()))
        .collect();
    let report = audit_entries(&inputs, true).unwrap();
    assert_eq!(report.exit_code(), 0);
    assert_eq!(report.entries.len(), Kind::ALL.len());
    let serialized = serde_json::to_value(&report).unwrap();
    assert_eq!(serialized["strict"], true);
    assert_eq!(
        serialized["entries"].as_array().unwrap().len(),
        Kind::ALL.len()
    );
}

#[test]
fn byte_slice_audit_handles_pyproject_bom_utf8_bounds_and_deprecations() {
    let pyproject = b"\xef\xbb\xbf[tool.yaga]\nconfig-version = 1\n[tool.yaga.commit]\n[tool.yaga.agent-review]\nversion = 1\nrequired = ['codex']\n";
    let path = Path::new("pyproject.toml");
    assert_eq!(
        classify_bytes(path, pyproject).unwrap(),
        vec![Kind::Commit, Kind::AgentReview]
    );
    let dual = audit_bytes(path, pyproject);
    assert_eq!(dual.len(), 2);
    assert!(dual.iter().all(|e| e.status == "current"));

    // Deprecated repo v1 plan
    let deprecated_entries = audit_entries(
        &[(
            Path::new(".yaga/checks/ci.toml"),
            b"plan-version = 1\nchecks = ['commit']\n",
        )],
        true,
    )
    .unwrap();
    assert_eq!(deprecated_entries.exit_code(), 1);
    assert_eq!(deprecated_entries.entries[0].status, "deprecated");
    assert_eq!(
        audit_entries(
            &[(
                Path::new(".yaga/checks/ci.toml"),
                b"plan-version = 1\nchecks = ['commit']\n",
            )],
            false,
        )
        .unwrap()
        .exit_code(),
        0
    );

    // Invalid UTF-8 preserves exact offset diagnostics without touching disk
    let bad_utf8 = audit_bytes(
        Path::new(".yaga/branch-policy.toml"),
        b"\xef\xbb\xbf# x\n\xe2\x82",
    );
    assert_eq!(bad_utf8.len(), 1);
    assert_eq!(bad_utf8[0].status, "invalid");
    assert_eq!(bad_utf8[0].kind, Some(Kind::Branch));
    assert_eq!(
        bad_utf8[0].message,
        "invalid UTF-8 TOML: 'utf-8' codec can't decode bytes in position 4-5: unexpected end of data"
    );
    assert_eq!(
        classify_bytes(Path::new(".yaga/branch-policy.toml"), b"\xff")
            .unwrap_err()
            .kind(),
        "input"
    );

    // Oversized input and over-limit entry counts are bounded fail-closed
    let oversized = vec![b' '; 1024 * 1024 + 1];
    assert_eq!(
        classify_bytes(Path::new(".yaga.toml"), &oversized)
            .unwrap_err()
            .kind(),
        "input"
    );
    let oversized_audit = audit_bytes(Path::new(".yaga.toml"), &oversized);
    assert_eq!(oversized_audit[0].status, "invalid");
    assert!(oversized_audit[0].message.contains("exceeds"));

    let too_many: Vec<(&Path, &[u8])> =
        vec![(Path::new(".yaga.toml"), b"config-version = 1\n"); 129];
    assert_eq!(audit_entries(&too_many, false).unwrap_err().kind(), "input");
    assert!(audit_entries(&[], false).unwrap().entries.is_empty());
}
