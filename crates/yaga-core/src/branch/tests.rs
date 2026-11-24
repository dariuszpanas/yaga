use super::*;

fn policy(patterns: &[&str]) -> BranchPolicy {
    BranchPolicy::new(patterns.iter().copied().map(str::to_owned).collect()).unwrap()
}

#[test]
fn validate_name_before_ordered_matching() {
    let policy = policy(&["Feat/*", "feat/**", "feat/*"]);
    let accepted = check_branch_name(&policy, "feat/topic").unwrap();
    assert_eq!(accepted.matched_pattern(), Some("feat/**"));
    assert_eq!(
        check_branch_name(&policy, "fix/one").unwrap().diagnostic(),
        Some(ALLOWED)
    );
    for invalid in [
        "café",
        "HeAd",
        "refs/heads/main",
        "a..b",
        "feat/x.LOCK",
        "1x",
        "a//b",
        "feat/_x",
        "feat/x_",
        "feat/\n",
        &"a".repeat(40),
        &"f".repeat(64),
        &["a"; MAX_BRANCH_COMPONENTS + 1].join("/"),
    ] {
        assert_eq!(
            check_branch_name(&policy, invalid).unwrap().diagnostic(),
            Some(SYNTAX),
            "{invalid:?}"
        );
    }
    for valid in [
        "a",
        "feat/HEAD",
        "HEAD/foo",
        "x/refs/y",
        "feat/07",
        "refs",
        &["a"; MAX_BRANCH_COMPONENTS].join("/"),
    ] {
        assert_ne!(
            check_branch_name(&policy, valid).unwrap().diagnostic(),
            Some(SYNTAX)
        );
    }
}

#[test]
fn inclusive_byte_bounds_and_transport_errors() {
    let policy = policy(&["**"]);
    assert!(
        check_branch_name(&policy, &"a".repeat(244))
            .unwrap()
            .valid()
    );
    assert_eq!(
        check_branch_name(&policy, &"é".repeat(122))
            .unwrap()
            .diagnostic(),
        Some(SYNTAX)
    );
    for invalid in ["", &"a".repeat(245), &"é".repeat(123)] {
        assert!(matches!(
            check_branch_name(&policy, invalid),
            Err(BranchError::Input(_))
        ));
    }
    assert!(validate_branch_pattern(&"a".repeat(244)).is_ok());
    assert!(validate_branch_pattern(&"a".repeat(245)).is_err());
}

#[test]
fn literal_edges_and_globstar() {
    for good in [
        "*", "**", "*-fix", "x-*", "*.x", "x.*", "x.lock*", "**/1*", "HEAD", "refs/**",
    ] {
        assert!(validate_branch_pattern(good).is_ok(), "{good}");
    }
    for bad in [
        "",
        "é",
        "/feat",
        "a/",
        "a//b",
        "feat/**x",
        "a***b",
        "-feat*",
        "1*",
        "feat/-x",
        "feat/*x.",
        "a..b",
        "feat/*.LOCK",
        "x/?x",
        "feat/$x",
    ] {
        assert!(validate_branch_pattern(bad).is_err(), "{bad}");
    }
    for (pattern, branch, expected) in [
        ("feat/**", "feat", true),
        ("feat/*", "feat", false),
        ("**/main", "main", true),
        ("**/main", "feat/Main", false),
        ("f*e*t", "feat", true),
        ("f*t*e", "feat", false),
        (
            "dependabot/*/**",
            "dependabot/npm_and_yarn/pkg/acorn-6.4.1",
            true,
        ),
        ("feat/*", "feat/a/b", false),
        ("**/1*", "feat/123", true),
    ] {
        assert_eq!(match_branch_pattern(pattern, branch).unwrap(), expected);
    }
}

#[test]
fn strict_policy_wire_boundaries() {
    let label = Path::new("branch.toml");
    let source = "\u{feff}branch-policy-version = 1\nallowed-patterns = ['main', 'feat/**']\n";
    let padded = format!(
        "{source}#{}",
        "x".repeat(MAX_BRANCH_POLICY_BYTES - source.len() - 1)
    );
    let policy = parse_branch_policy(padded.as_bytes(), label).unwrap();
    assert_eq!(
        policy.allowed_patterns().collect::<Vec<_>>(),
        ["main", "feat/**"]
    );
    assert!(parse_branch_policy(format!("{padded}x").as_bytes(), label).is_err());
    for (body, expected) in [
        ("", "missing required key(s)"),
        ("x = 2", "unknown branch policy key"),
        (
            "branch-policy-version = 2\nallowed-patterns = []",
            "newer schema",
        ),
        (
            "branch-policy-version = true\nallowed-patterns = []",
            "integer 1",
        ),
        (
            "branch-policy-version = 1\nallowed-patterns = [3]",
            "array of strings",
        ),
        (
            "branch-policy-version = 1\nallowed-patterns = []",
            "must not be empty",
        ),
        (
            "branch-policy-version = 1\nallowed-patterns = ['x','x']",
            "must be unique",
        ),
        (
            "branch-policy-version = 1\nallowed-patterns = ['x.']",
            "invalid branch policy",
        ),
        (
            "branch-policy-version = 1\nbranch-policy-version = 1",
            "invalid branch policy TOML",
        ),
        ("required-version = '>=999.0.0'\nx=1", "does not satisfy"),
        ("required-version = false", REQUIREMENT_ERROR),
    ] {
        assert!(
            parse_branch_policy(body.as_bytes(), label)
                .unwrap_err()
                .to_string()
                .contains(expected),
            "{body}"
        );
    }
    assert!(
        parse_branch_policy(&[0xff], label)
            .unwrap_err()
            .to_string()
            .contains("not valid UTF-8")
    );
}
const REQUIREMENT_ERROR: &str = crate::version::REQUIREMENT_TYPE_ERROR;

#[test]
fn only_display_sanitization_changes_reported_branch() {
    let policy = policy(&["main", "feat/*"]);
    let branch = "feat/bad%,:'\u{200b}\n";
    let checked = CheckedBranch {
        report: check_branch_name(&policy, branch).unwrap(),
        policy: LoadedBranchPolicy {
            policy,
            path: PathBuf::from("branch.toml"),
        },
    };
    assert_eq!(
        checked.render(OutputFormat::Text),
        "Policy: branch.toml\nBranch: feat/bad%,:'??\nFAILED  [branch.syntax] branch name does not use the portable YAGA branch syntax\nAllowed: main, feat/*\nBranch policy: failed."
    );
    assert_eq!(
        checked.render(OutputFormat::Github),
        "::error title=YAGA branch.syntax::branch \"feat/bad%25,:'??\": branch name does not use the portable YAGA branch syntax\nYAGA branch policy: failed for branch \"feat/bad%,:'??\"."
    );
    let json =
        serde_json::from_str::<serde_json::Value>(&checked.render(OutputFormat::Json)).unwrap();
    assert_eq!(json["branch"], "feat/bad%,:'??");
    assert_eq!(json["schema_version"], 1);
    assert_eq!(json["diagnostics"][0]["code"], "branch.syntax");
    assert!(json["matched_pattern"].is_null());
}
