use super::{ReferenceContext, parse_action_manifest, parse_workflow};
use crate::repository::normalize_security;

#[test]
fn missing_workflow_displays_the_platform_selector_spelling() {
    use std::path::{Path, PathBuf};
    let repo = tempfile::tempdir().unwrap();
    let error = super::discover_inputs(
        repo.path(),
        &[PathBuf::from("./.github/workflows/absent.yml")],
        super::DiscoveryLimits::default(),
    )
    .unwrap_err();
    let display = Path::new(".github").join("workflows").join("absent.yml");
    assert_eq!(
        error,
        format!("workflow input does not exist: {}", display.display())
    );
}

#[test]
fn duplicate_fields_keep_both_occurrences_and_original_locations() {
    let raw = b"jobs:\n  build:\n    steps:\n      - uses: first/action@v1\n        uses: second/action@v2\n";
    let parsed = parse_workflow(raw, "ci.yml").unwrap();
    assert_eq!(parsed.node_count, 12);
    assert_eq!(parsed.references_checked(), 2);
    assert_eq!(
        parsed
            .reference_findings()
            .iter()
            .map(|d| (d.code.as_str(), d.line, d.column))
            .collect::<Vec<_>>(),
        [
            ("uses.pin", 4, 15),
            ("yaml.duplicate_key", 5, 9),
            ("uses.pin", 5, 15)
        ]
    );
}

#[test]
fn parse_once_for_three_security_profiles_and_exact_immutable_references() {
    let raw = b"on: [pull_request, workflow_run]\npermissions: {statuses: write}\njobs:\n  run:\n    container: image\n    services:\n      off: {image: ''}\n    steps:\n      - uses: 'actions/checkout@v4'\n        with: {ref: '${{ github.head_ref }}'}\n      - uses: '$/local'\n  call:\n    uses: './.github/workflows/reuse.yml'\n    secrets: inherit\n";
    let parsed = parse_workflow(raw, "ci.yml").unwrap();
    assert_eq!(parsed.references_checked(), 5);
    let reference = &parsed.references[1];
    assert_eq!(reference.context, ReferenceContext::Step);
    assert_eq!(
        &raw[reference.source_start.unwrap()..reference.source_end.unwrap()],
        b"$/local"
    );
    assert_eq!(parsed.references[2].context, ReferenceContext::Job);
    assert_eq!(
        parsed
            .reference_findings()
            .iter()
            .map(|d| d.code.as_str())
            .collect::<Vec<_>>(),
        ["image.pin", "uses.pin"]
    );
    for (profile, expected_count) in [
        ("recommended-v1", 3),
        ("recommended-v2", 4),
        ("recommended-v3", 5),
    ] {
        let (_, rules) = normalize_security(Some(profile), &[]).unwrap();
        let diagnostics = parsed.security_findings(&rules).unwrap();
        assert_eq!(
            diagnostics.len(),
            expected_count,
            "{profile}: {diagnostics:?}"
        );
    }
}

#[test]
fn unicode_source_spans_keep_character_offsets_for_python_and_bytes_for_lint() {
    let source = "jobs: {雷: {steps: [{uses: '$/🦀'}]}}";
    let parsed = parse_workflow(source.as_bytes(), "ci.yml").unwrap();
    let item = &parsed.references()[0];
    assert_eq!(
        &source[item.source_start.unwrap()..item.source_end.unwrap()],
        "$/🦀"
    );
    let wire = serde_json::to_value(parsed.bundle()).unwrap();
    let reference = &wire["references"][0];
    let start = reference[4].as_u64().unwrap() as usize;
    let end = reference[5].as_u64().unwrap() as usize;
    assert_eq!(
        source
            .chars()
            .skip(start)
            .take(end - start)
            .collect::<String>(),
        "'$/🦀'"
    );
}

#[test]
fn implicit_mapping_and_empty_scalar_positions_match_their_yaml_source() {
    let raw = b"on: push\njobs:\n  build:\n    steps:\n      - uses:\n";
    let parsed = parse_workflow(raw, "ci.yml").unwrap();
    let (_, rules) = normalize_security(None, &[]).unwrap();
    let missing = &parsed.security_findings(&rules).unwrap()[0];
    assert_eq!(
        (missing.code.as_str(), missing.line, missing.column),
        ("security.permissions.explicit", 1, 1)
    );
    let missing = &parsed.reference_findings()[0];
    assert_eq!(
        (missing.code.as_str(), missing.line, missing.column),
        ("uses.syntax", 5, 14)
    );
}

#[test]
fn alias_cycles_document_count_and_resource_limits_are_operational_errors() {
    for (raw, message) in [
        ("jobs: &a [*a]", "contains an alias cycle"),
        (
            "---\n{}\n---\n{}",
            "must contain exactly one nonempty YAML document",
        ),
        (" ", "must contain exactly one nonempty YAML document"),
    ] {
        assert!(
            parse_workflow(raw.as_bytes(), "ci.yml")
                .unwrap_err()
                .ends_with(message)
        );
    }
    let many = format!(
        "jobs:\n  build:\n    steps:\n{}",
        "      - uses: local/action@v1\n".repeat(513)
    );
    assert!(
        parse_workflow(many.as_bytes(), "ci.yml")
            .unwrap_err()
            .ends_with("hard workflow reference limit of 512")
    );
    assert_eq!(
        parse_workflow(&[255], "ci.yml").unwrap_err(),
        "ci.yml is not valid UTF-8"
    );
}

#[test]
fn manifests_accept_syntax_errors_for_actionlint_and_select_only_runtime_fields() {
    let raw=b"runs:\n  using: node24\n  main: dist/bundle.js\n  pre: before.js\n  post: after.js\nother: {image: ignored}\n";
    let (count, dependencies) = parse_action_manifest(raw, "action.yml").unwrap();
    assert!(count > 3);
    assert_eq!(
        dependencies,
        [
            ("main".into(), "dist/bundle.js".into()),
            ("pre".into(), "before.js".into()),
            ("post".into(), "after.js".into())
        ]
    );
    assert_eq!(
        parse_action_manifest(b"{", "action.yml").unwrap(),
        (0, vec![])
    );
    assert!(parse_action_manifest(&[255], "action.yml").is_err());
}
