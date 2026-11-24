use super::*;

#[test]
fn committed_records_reject_ambiguous_metadata_and_preserve_legal_names() {
    use records::{ModeSelection, NameSelection, SizeSelection, parse_names};
    let id = "a".repeat(40);
    let valid = format!("160000 commit {id}\tsub\0");
    let mode = ModeSelection::parse(valid.as_bytes(), 40).unwrap();
    assert_eq!(mode.entries()[0].kind, records::ModeKind::Gitlink);
    let legal = format!("100755 blob {id}\tline\nbreak\\tab\t.txt\0");
    assert!(ModeSelection::parse(legal.as_bytes(), 40).is_ok());
    assert!(parse_names(b"a\0a-/b\0a/b\0", NameSelection::Portability).is_err());
    assert!(parse_names(b"a\0a\0", NameSelection::Tree).is_err());
    assert!(parse_names(b"a\0b", NameSelection::Tree).is_err());
    assert!(parse_names(b"\xff\0", NameSelection::Tree).is_err());
    assert!(parse_names(b"", NameSelection::Tree).unwrap().is_empty());
    for record in [
        format!("160000 blob {id} -\tsub\0"),
        format!("100644 blob {id} 00\ta\0"),
        format!("100644 blob {id} -\ta\0"),
        format!("100644 blob {id} +4\ta\0"),
        format!("100644 blob {id} 9007199254740992\ta\0"),
        format!("100644 blob {id} 9\ta"),
        format!("100644 blob {id} 1\ta\0100644 blob {id} 2\tb\0"),
    ] {
        assert!(
            SizeSelection::parse(record.as_bytes(), 40).is_err(),
            "{record:?}"
        );
    }
    assert!(ModeSelection::parse(valid.as_bytes(), 64).is_err());
    assert!(ModeSelection::parse(b"", 20).is_err());
}

#[test]
fn size_reports_count_duplicate_blobs_per_path_and_put_total_last() {
    let policy = sizes::SizePolicy::new(1, Some(9), &[("src/**", 2), ("**", 4)]).unwrap();
    let id = "1".repeat(40);
    let input =
        format!("160000 commit {id} -\tsub\0120000 blob {id} 5\tsrc/x\0100755 blob {id} 5\tbin\0");
    let selection = records::SizeSelection::parse(input.as_bytes(), 40).unwrap();
    let report = policy.check(&selection).unwrap();
    assert_eq!(selection.gitlinks(), ["sub"]);
    assert_eq!(report.total_bytes, 10);
    assert_eq!(
        report
            .diagnostics
            .iter()
            .map(|f| (f.code, f.path, f.pattern, f.limit))
            .collect::<Vec<_>>(),
        [
            ("size.blob", Some("bin"), Some("**"), 4),
            ("size.blob", Some("src/x"), Some("src/**"), 2),
            ("size.total", None, None, 9),
        ]
    );
    let overflow = format!(
        "100644 blob {id} 9007199254740991\ta\0100644 blob {} 1\tb\0",
        "2".repeat(40)
    );
    let selection = records::SizeSelection::parse(overflow.as_bytes(), 40).unwrap();
    assert!(policy.check(&selection).is_err());
}

#[test]
fn mode_findings_preserve_bounded_prefix_and_first_override() {
    use records::ModeKind::*;
    let policy = modes::ModePolicy::new(
        &[Regular],
        &[
            ("**/run", &[Executable]),
            ("scripts/**", &[Symlink, Regular]),
        ],
    )
    .unwrap();
    let id = "a".repeat(40);
    let mut input = format!("100755 blob {id}\tscripts/run\0120000 blob {id}\tscripts/test\0");
    for i in 0..300 {
        input.push_str(&format!("120000 blob {id}\tx{i:03}\0"));
    }
    let selection = records::ModeSelection::parse(input.as_bytes(), 40).unwrap();
    let report = policy.check(&selection).unwrap();
    assert_eq!(report.finding_count, 300);
    assert_eq!(report.diagnostics.len(), 256);
    assert_eq!(report.diagnostics[255].path, "x255");
    assert_eq!(report.decisions[0].pattern, Some("**/run"));
    assert_eq!(report.decisions[1].allowed_modes, [Regular, Symlink]);
}

#[test]
fn portability_handles_collision_partners_and_preserves_exact_counts() {
    let policy = portability::PathPolicy::windows_compatible_v1();
    let report = policy
        .check(&[
            "DIR",
            "Dir",
            "NUL.txt",
            "bad?/dir.",
            "dir/child",
            "lPt¹.job",
            "É",
            "é",
        ])
        .unwrap();
    assert_eq!(report.finding_count(), 7);
    assert_eq!(
        report
            .diagnostics
            .iter()
            .filter_map(|f| f.related_path.map(|p| (f.path, p)))
            .collect::<Vec<_>>(),
        [("DIR", "Dir"), ("Dir", "DIR"), ("dir/child", "DIR"),]
    );
    assert_eq!(
        report
            .diagnostics
            .iter()
            .filter(|f| f.path == "bad?/dir.")
            .map(|f| (f.code, f.component))
            .collect::<Vec<_>>(),
        [
            ("path.windows-character", Some(1)),
            ("path.windows-trailing", Some(2)),
        ]
    );
    let names: Vec<_> = (0..300).map(|i| format!("{i:03}.")).collect();
    let names: Vec<_> = names.iter().map(String::as_str).collect();
    let report = policy.check(&names).unwrap();
    assert_eq!(report.finding_count(), 300);
    assert_eq!(report.diagnostics.len(), 256);
    assert_eq!(report.diagnostics[255].path, "255.");
}

#[test]
fn closed_schema_loaders_accept_repository_policies_and_reject_invalid_types() {
    let repo_root = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    if repo_root.join(".yaga").is_dir() {
        let read = |rel: &str| std::fs::read(repo_root.join(rel)).expect(rel);
        tree::TreePolicy::from_toml(&read(".yaga/tree-policy.toml")).unwrap();
        modes::ModePolicy::from_toml(&read(".yaga/mode-policy.toml")).unwrap();
        sizes::SizePolicy::from_toml(&read(".yaga/size-policy.toml")).unwrap();
        changes::ChangePolicy::from_toml(&read(".yaga/change-policy.toml")).unwrap();
        portability::PathPolicy::from_toml(&read(".yaga/path-policy.toml")).unwrap();
    } else {
        assert!(
            !repo_root.join("Cargo.toml").is_file(),
            "missing .yaga directory in workspace checkout"
        );
    }
    let bom = b"\xef\xbb\xbfmode-policy-version = 1\nrequired-version = '>=0.1.0'\ndefault-allowed-modes=['regular']\n";
    assert!(modes::ModePolicy::from_toml(bom).is_ok());
    for document in [
        "path-policy-version=1\nprofile='windows-compatible-v1'\nrules=['windows-trailing']",
        "path-policy-version=1\nrules=['windows-characters','windows-characters']",
        "path-policy-version=1\nrequired-version='>999.0.0'\nrules=['windows-trailing']",
        "path-policy-version=true\nrules=['windows-trailing']",
        "path-policy-version=1\nunknown=true\nrules=['windows-trailing']",
        "path-policy-version=1\npath-policy-version=1\nrules=['windows-trailing']",
    ] {
        assert!(portability::PathPolicy::from_toml(document.as_bytes()).is_err());
    }
    assert!(
        sizes::SizePolicy::from_toml(b"size-policy-version=1\ndefault-max-blob-bytes=true")
            .is_err()
    );
    assert!(modes::ModePolicy::from_toml(b"mode-policy-version=1\ndefault-allowed-modes=['regular']\n[[path-overrides]]\npattern='**'\nallowed-modes=['symlink']\nextra=true").is_err());
    assert!(changes::ChangePolicy::from_toml(&vec![b' '; 65537]).is_err());
}

#[test]
fn anchored_unicode_globs_and_literal_metacharacters() {
    for (pattern, path, matched) in [
        ("src/*.py", "src/.py", true),
        ("src/*.py", "other/src/a.py", false),
        ("src/*.py", "src/pkg/a.py", false),
        ("src/*.py", "SRC/a.py", false),
        ("a/**/b/**/c", "a/b/c", true),
        ("a/**/b/**/c", "a/x/y/b/z/c", true),
        ("docs/**", "docs", true),
        ("**", "any/path", true),
        (
            "$HOME/%literal%/[guide]?.md",
            "$HOME/%literal%/[guide]?.md",
            true,
        ),
        ("é.txt", "e\u{301}.txt", false),
        ("a*a*b", "aaab", true),
        ("*α*文*", "文abcαxyz", false),
        ("*α*文*", "abcαé文！", true),
        ("aba*aba", "ababa", false),
        ("é*é", "é", false),
    ] {
        assert_eq!(
            match_pattern(pattern, path),
            Ok(matched),
            "{pattern} {path}"
        );
    }
}

#[test]
fn validation_and_byte_limits() {
    for path in [
        "",
        ".",
        "/a",
        "a//b",
        "a/../b",
        "./a",
        "a/",
        "A:b",
        "1:x",
        "a\\b",
        "a\u{200b}b",
        "a\u{1343f}b",
        "a\u{2028}b",
    ] {
        assert!(PathComponents::parse(path).is_err(), "{path:?}");
        assert!(Pattern::compile(path).is_err());
    }
    assert!(Pattern::compile("src/a**b").is_err());
    assert!(PathComponents::parse("src/a**b").is_ok());
    assert!(Pattern::compile(&"é".repeat(256)).is_ok());
    assert!(Pattern::compile(&"é".repeat(257)).is_err());
    assert!(PathComponents::parse(&"é".repeat(2048)).is_ok());
    assert!(PathComponents::parse(&"é".repeat(2049)).is_err());
    assert!(PathComponents::parse(&vec!["x"; 65].join("/")).is_err());
    assert!(PathComponents::committed("name/line\nbreak\\CON").is_ok());
}

#[test]
fn work_counts_are_unicode_scalar_or_component_based_and_shared() {
    let pattern = Pattern::compile(&vec!["**"; 64].join("/")).unwrap();
    let value = vec!["é"; 64].join("/");
    let path = PathComponents::parse(&value).unwrap();
    let mut work = WorkBudget::new(8192);
    assert_eq!(pattern.matches(&path, &mut work), Ok(true));
    assert_eq!(work.remaining(), 4096);
    assert_eq!(pattern.matches(&path, &mut work), Ok(true));
    assert_eq!(work.remaining(), 0);
    assert_eq!(pattern.matches(&path, &mut work), Err(PathError::WorkLimit));
    for (pattern, value, expected_cost, expected_result) in [
        ("é", "文", 3, false),
        ("é", "é", 3, true),
        ("*é", "aβ", 3, false),
        ("*é*", "文aé文", 4, true),
        ("*é*", "文ab", 4, false),
        ("é*é", "é", 3, false),
        ("é*", "文", 3, false),
    ] {
        let mut work = WorkBudget::new(expected_cost);
        assert_eq!(
            Pattern::compile(pattern)
                .unwrap()
                .matches(&PathComponents::parse(value).unwrap(), &mut work),
            Ok(expected_result)
        );
        assert_eq!(work.remaining(), 0, "{pattern} {value}");
    }
}

#[test]
fn rust_policy_loader_accepts_toml_10_unicode_and_11_byte_escapes() {
    for escaped in [r"\u0061", r"\x61"] {
        let policy = format!(
            "tree-policy-version=1\nrequired-paths=[\"{escaped}\"]\nforbidden-patterns=[]\n"
        );
        let parsed = tree::TreePolicy::from_toml(policy.as_bytes()).unwrap();
        assert_eq!(parsed.required_paths(), ["a"]);
    }
}

#[test]
fn tree_requires_policy_order_and_attributes_first_forbidden_match() {
    assert!(tree::TreePolicy::new(&["README.md"], &["*.md"]).is_err());
    let policy = tree::TreePolicy::new(&["z", "a"], &["**/log.*", "tmp/**"]).unwrap();
    let found = policy.check(&["b", "tmp/file", "tmp/log.txt"]).unwrap();
    assert_eq!(
        found
            .iter()
            .map(|f| (f.path, f.code, f.pattern))
            .collect::<Vec<_>>(),
        [
            ("z", "tree.required", None),
            ("a", "tree.required", None),
            ("tmp/file", "tree.forbidden", Some("tmp/**")),
            ("tmp/log.txt", "tree.forbidden", Some("**/log.*")),
        ]
    );
    assert!(policy.check(&["b", "a"]).is_err());
}

#[test]
fn change_rules_keep_sorted_union_and_only_evaluate_triggered_requirements() {
    use changes::{ChangePolicy, Rule, Status};
    let policy = ChangePolicy::new(&[
        Rule {
            name: "src-docs",
            when_any: &["**/*.rs", "src/**"],
            require_any: &["docs/**"],
        },
        Rule {
            name: "docs-tests",
            when_any: &["docs/**"],
            require_any: &["tests/**"],
        },
        Rule {
            name: "tests-src",
            when_any: &["tests/**"],
            require_any: &["src/**"],
        },
    ])
    .unwrap();
    let report = policy.check(&["docs/x", "lib.rs", "src/x.rs"]).unwrap();
    assert_eq!(report[0].triggered_paths, ["lib.rs", "src/x.rs"]);
    assert_eq!(report[0].required_paths, ["docs/x"]);
    assert_eq!(report[0].status(), Status::Passed);
    assert_eq!(report[1].status(), Status::Failed);
    assert_eq!(report[2].status(), Status::Skipped);
    assert!(report[2].required_paths.is_empty());
}
