use super::*;

#[test]
fn checks_recorded_consumer_corpus() {
    let policy: CommitPolicy = serde_json::from_value(serde_json::json!({
        "allowed_types": ["build","chore","ci","docs","feat","fix","perf","refactor","revert","style","test"],
        "type_case": "lower", "header_max_length": 72, "description_min_length": 10,
        "description_case": "forbid-initial-uppercase", "length_unit":"utf16",
        "footer_syntax": "colon-whitespace", "description_ending":"forbid-period",
        "body_policy":"required", "body_min_length":100, "body_max_line_length":72,
        "footer_max_line_length":100, "line_length_urls":"exempt","required_colon_footer_tokens":["Validation"]
    })).unwrap();
    let corpus: serde_json::Value = serde_json::from_str(include_str!(
        "../../tests/fixtures/commit_consumer_parity.json"
    ))
    .unwrap();
    for case in corpus["cases"].as_array().unwrap() {
        let message = case["message"].as_str().unwrap();
        let result = check_message(message, &policy, false, false);
        assert_eq!(
            result.valid(),
            case["valid"].as_bool().unwrap(),
            "{message}: {:?}",
            result.diagnostics
        );
    }
}

#[test]
fn retains_footer_boundaries_and_source_lines() {
    let message = "fix(api)!: preserve metadata\r\n\r\nExplain why.\r\nRefs #12\r\n\r\nBREAKING CHANGE: first\r\n\r\ncontinued\r\nReviewed-by #123\r\n";
    let p = parse_message(
        message,
        MessageFormat::Conventional,
        FooterSyntax::Conventional,
    )
    .unwrap();
    assert_eq!(p.scope.as_deref(), Some("api"));
    assert_eq!(p.body, "Explain why.\nRefs #12");
    assert_eq!(p.body_start_line, 3);
    assert_eq!(p.footer_start_line, Some(6));
    assert_eq!(
        p.footer_lines,
        [
            "BREAKING CHANGE: first",
            "",
            "continued",
            "Reviewed-by #123"
        ]
    );
    assert!(p.breaking_header && p.breaking_footer && p.breaking);
}

#[test]
fn leading_body_blanks_and_multibyte_text_keep_their_source_positions() {
    let input = "fix: résumé\r\n\r\n\r\n雪\rsecond\r\n\r\n\r\nRefs #12\r\n\r\n";
    let parsed = parse_message(
        input,
        MessageFormat::Conventional,
        FooterSyntax::Conventional,
    )
    .unwrap();
    assert_eq!(parsed.body, "雪\nsecond");
    assert_eq!(parsed.body_lines, ["", "雪", "second"]);
    assert_eq!(parsed.body_start_line, 3);
    assert_eq!(parsed.footer_start_line, Some(8));
    assert_eq!(parsed.footer_lines, ["Refs #12"]);
    let policy = CommitPolicy {
        body_max_line_length: Some(1),
        ..CommitPolicy::default()
    };
    let findings = check_message(input, &policy, false, false).diagnostics;
    assert!(
        findings
            .iter()
            .any(|d| d.code == "body.line-length" && d.line == 5)
    );
}

#[test]
fn paragraph_policy_checks_original_body_line_positions_without_a_width_limit() {
    let policy = CommitPolicy {
        body_paragraph_splitting: ParagraphSplitting::Check,
        ..CommitPolicy::default()
    };
    let input = "fix: example\n\n\nContinue the sentence,\n\nthen finish it.\n";
    let findings = check_message(input, &policy, false, false).diagnostics;
    assert!(
        findings
            .iter()
            .any(|d| d.code == "body.paragraph-format" && d.line == 6)
    );
    assert!(
        check_message(input, &policy, false, true)
            .diagnostics
            .is_empty()
    );
}

#[test]
fn only_paragraph_starts_begin_footers_and_keep_exact_source_lines() {
    let input =
        "feat: sample\n\n\nA\nRefs: continuation in the body\n\n\nB\n\n\n\nRefs #123\n continued";
    let parsed = parse_message(
        input,
        MessageFormat::Conventional,
        FooterSyntax::Conventional,
    )
    .unwrap();
    assert_eq!(parsed.footer_start_line, Some(12));
    assert_eq!(parsed.footer_lines, ["Refs #123", " continued"]);
    assert_eq!(parsed.body, "A\nRefs: continuation in the body\n\n\nB");
}

#[test]
fn unicode_and_header_formats() {
    for title in [
        "",
        "fix(api client): x",
        "fix(): x",
        "fi\u{202e}x: x",
        "fix: x\u{001c}",
    ] {
        assert!(
            parse_message(
                title,
                MessageFormat::Conventional,
                FooterSyntax::Conventional
            )
            .is_none(),
            "{title:?}"
        );
    }
    for text in ["x\u{00ad}y", "x\u{2028}y", "bad\ttitle"] {
        assert!(parse_message(text, MessageFormat::Plain, FooterSyntax::Conventional).is_none());
    }
    let p = parse_message(
        "A 👩‍💻 change",
        MessageFormat::Plain,
        FooterSyntax::Conventional,
    )
    .unwrap();
    assert_eq!(p.description, p.header);
    assert_eq!(p.commit_type, None);
    let policy = CommitPolicy {
        allowed_types: Some(vec!["STRASSE".into(), "Ϊ́".into(), "Ꭰ".into()]),
        ..Default::default()
    };
    for kind in ["straße", "STRAẞE", "ΐ", "ꭰ"] {
        assert!(check_message(&format!("{kind}: x"), &policy, false, false).valid());
    }
}

#[test]
fn body_rules_and_warnings_preserve_order_and_lines() {
    let policy = CommitPolicy {
        required_footer_tokens: vec!["Signed-off-by".into(), "Refs".into()],
        forbidden_footer_tokens: vec!["WIP".into()],
        footer_values: vec![("Validation".into(), vec!["ok".into()])],
        required_issue_prefixes: vec!["#".into()],
        breaking_markers: BreakingMarkers::Paired,
        body_min_words: 3,
        warning_rules: vec!["body.word-count".into(), "syntax.separator".into()],
        ..Default::default()
    };
    let result = check_message(
        "fix!: x\nbody words\n\nWIP: x\nValidation: ok\nValidation: no\nValidation: bad",
        &policy,
        false,
        false,
    );
    let observed: Vec<_> = result
        .diagnostics
        .iter()
        .map(|d| (d.code, d.line, d.severity))
        .collect();
    assert_eq!(
        observed,
        [
            ("syntax.separator", 2, Severity::Error),
            ("breaking.marker-pair", 1, Severity::Error),
            ("body.word-count", 2, Severity::Warning),
            ("footer.required", 1, Severity::Error),
            ("footer.forbidden", 4, Severity::Error),
            ("reference.required", 1, Severity::Error),
            ("footer.value", 6, Severity::Error)
        ]
    );
    assert_eq!(
        result.diagnostics[3].message,
        "required footer token 'Signed-off-by' is missing; 1 additional required footer token missing"
    );
    assert!(check_message("fix!: x", &policy, false, true).valid());
}

#[test]
fn bounded_reference_grammar() {
    let prefixes = ["A", "AB", "AB1", "#", "PROJ-"].map(String::from);
    for text in ["AB12", "(#1).", "Refs: PROJ-98765432109876543210"] {
        assert!(has_issue_reference(text, &prefixes));
    }
    for text in [
        "#0",
        "#01",
        "#١",
        "#123456789012345678901",
        "https://x/#12",
        "# 1",
    ] {
        assert!(!has_issue_reference(text, &prefixes));
    }
}

#[test]
fn ignored_unicode_and_bracket_patterns() {
    for (pattern, text, yes) in [
        (r#"Revert "*""#, r#"Revert "feat: x""#, true),
        ("[!A-C]?*", "Ω💡", true),
        ("[!A-C]?*", "B💡", false),
        ("[]-]", "]", true),
        ("[!]", "[!]", true),
        ("[z-a]", "x", false),
        ("[!z-a]", "x", true),
        ("[c-b!c]", "]", true),
        ("[c-b!c]", "c", false),
        ("a**b", "abc", false),
        ("**", "", true),
        ("[a-c-e]", "-", true),
        ("[-]", "-", true),
    ] {
        assert_eq!(glob::matches(pattern, text), yes, "{pattern} {text}");
    }
}

#[test]
fn resource_and_merge_checks_precede_exemptions() {
    let policy = CommitPolicy {
        ignored_headers: vec!["*".into()],
        ..Default::default()
    };
    let huge = "x".repeat(MAX_MESSAGE_BYTES + 1);
    assert_eq!(
        check_message(&huge, &policy, true, false).diagnostics[0].code,
        "message.size"
    );
    assert_eq!(
        check_message("x", &policy, true, false).skipped_reason,
        Some("merge commit")
    );
    assert_eq!(
        check_message("x", &policy, false, false).skipped_reason,
        Some("ignored header")
    );
}

#[test]
fn existing_standalone_and_pyproject_policies_use_the_same_checker() {
    use std::path::Path;
    let pyproject = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../pyproject.toml");
    if pyproject.is_file() {
        let bytes = std::fs::read(&pyproject).expect("read pyproject.toml");
        let config = parse_config(&bytes, Path::new("pyproject.toml")).unwrap();
        assert!(
            config
                .policy
                .allowed_types
                .unwrap()
                .contains(&"feat".into())
        );
        assert_eq!(config.typos, TyposPolicy::Check);
    } else {
        assert!(
            !Path::new(env!("CARGO_MANIFEST_DIR"))
                .join("../../Cargo.toml")
                .is_file(),
            "missing pyproject.toml in workspace checkout"
        );
    }
    let parsed = parse_config(b"\xef\xbb\xbfconfig-version=1\nrequired-version='>=0.5.0'\n[commit]\nallowed-types=['fix']\nscope-policy-by-type={fix='required'}\nfooter-values={QA=['pass','fail']}\n", Path::new(".yaga.toml")).unwrap();
    let result = check_message("fix: x\n\nQA: pending", &parsed.policy, false, false);
    assert_eq!(
        result
            .diagnostics
            .iter()
            .map(|d| d.code)
            .collect::<Vec<_>>(),
        ["scope.required", "footer.value"]
    );
}

#[test]
fn rejects_wrong_types_unknown_keys_and_contradictions() {
    use std::path::Path;
    for doc in [
        "config-version=2",
        "config-version=true",
        "required-version='>999.9.0'",
        "commit=[]",
        "agent-review=false",
        "[commit]\nmax-commits=true",
        "[commit]\nallowed_types=['fix']",
        "[commit]\nallowed-types=[]",
        "[commit]\nbody-min-length=true",
        "[commit]\nheader-max-length=0",
        "[commit]\nallowed-types=['strasse','Straße']",
        "[commit]\nscope-policy-by-type=[]",
        "[commit]\nallowed-types=['fix']\nscope-policy-by-type={bad='required'}",
        "[commit]\nmessage-format='plain'\ntype-case='any'",
        "[commit]\nrequired-footer-tokens=['Wip']\nforbidden-footer-tokens=['wip']",
        "[commit]\nrequired-footer-tokens=['BREAKING-CHANGE']",
        "[commit]\nrequired-issue-prefixes=['#','X?']",
        "[commit]\nfooter-values={QA=['a','a']}",
        "[commit]\nwarning-rules=['syntax.header']",
        "[commit]\nbody-min-words=3\nbody-max-length=4",
        "[commit]\nbody-policy='forbidden'\nbody-min-words=1",
        "[commit]\nallowed-scopes=[]\nscope-policy='required'",
        "[commit.quality]\nthreshold=nan",
        "[commit.quality]\nprovider='bedrock'\ntask='seq2seq'",
        "[commit]\nignored-headers=[2001-01-01]",
    ] {
        assert!(
            parse_config(doc.as_bytes(), Path::new(".yaga.toml")).is_err(),
            "{doc}"
        );
    }
}

#[test]
fn quality_keeps_legacy_policy_display_and_requires_explicit_http_models() {
    let path = std::path::Path::new(".yaga.toml");
    let default = parse_config(b"config-version=1", path).unwrap();
    let rendered: serde_json::Value =
        serde_json::from_str(&render_config(&default, true, false, None).unwrap()).unwrap();
    assert_eq!(
        rendered["config"]["quality"],
        serde_json::json!({
            "provider":"huggingface","model_id":"saridormi/commit-message-quality-codebert",
            "revision":"30c7895b3eb0270a3246ef3db7b43c837d8e553a","task":"classification",
            "threshold":0.70,"region":null,"max_tokens":32,"max_input_tokens":512,"input_mode":"message"
        })
    );
    let parse = |fields: &str| {
        parse_config(format!("[commit.quality]\n{fields}").as_bytes(), path).map(|cfg| cfg.quality)
    };
    let legacy =
        parse("provider='huggingface'\ntask='seq2seq'\nmodel='local/model'\nregion='us-east-1'")
            .unwrap();
    assert_eq!(legacy.task, QualityTask::Seq2seq);
    assert_eq!(legacy.region.as_deref(), Some("us-east-1"));
    let bedrock = parse("provider='bedrock'").unwrap();
    assert_eq!(bedrock.model, "amazon.nova-micro-v1:0");
    assert_eq!(bedrock.revision, None);
    for name in ["openai", "anthropic", "gemini", "openai-compatible"] {
        let provider = format!("provider='{name}'");
        assert_eq!(
            parse(&provider).unwrap_err(),
            format!(
                "{} in .yaga.toml",
                EXPLICIT_QUALITY_MODEL.trim_end_matches('.')
            )
        );
        let fields = format!("{provider}\nmodel='fixture-v1'");
        let q = parse(&fields).unwrap();
        assert_eq!(q.provider.as_str(), name);
        assert_eq!(q.model, "fixture-v1");
        assert_eq!(q.revision, None);
        for invalid in ["region='us-east-1'", "task='seq2seq'", "revision='bad'"] {
            assert!(parse(&format!("{fields}\n{invalid}")).is_err());
        }
    }
}

#[test]
fn commit_configuration_errors_preserve_wire_keys_and_future_version_hint() {
    let path = std::path::Path::new(".yaga.toml");
    let range = "config-version must be an integer from 1 through 1 in .yaga.toml";
    let error = |source: &str| parse_config(source.as_bytes(), path).unwrap_err();
    for version in ["2", "100"] {
        assert_eq!(
            error(&format!("config-version={version}")),
            format!(
                "{range} (found newer schema version {version}; update YAGA using its installation manager)"
            )
        );
    }
    for value in ["0", "-2", "true", "'2'", "2.0"] {
        assert_eq!(error(&format!("config-version={value}")), range);
    }
    assert_eq!(
        error("z=0\na=0\nagent-review=false\nconfig-version=2"),
        "unknown YAGA configuration key(s) in .yaga.toml: a, z"
    );
    assert_eq!(
        error("agent-review=false\nconfig-version=2\ncommit=false"),
        "agent-review must be a table in .yaga.toml"
    );
    assert_eq!(
        error("[commit]\nnew-option=1"),
        "unknown commit configuration key(s) in .yaga.toml: new-option"
    );
    assert_eq!(
        error("[commit]\nz=1\nquality=false\nallowed_types=[]\nrequired-version='*'\nnew-option=1"),
        "unknown commit configuration key(s) in .yaga.toml: allowed_types, new-option, required-version, z"
    );
    assert_eq!(
        error("[commit]\nmax-commits=false\nheader-max-length=1\nquality={}"),
        "invalid value for max-commits in .yaga.toml"
    );
}

#[test]
fn nearest_config_stops_at_git_boundary_and_never_merges() {
    use std::fs;
    let temp = tempfile::tempdir().unwrap();
    let repo = temp.path().join("nested");
    let leaf = repo.join("leaf");
    fs::create_dir_all(&leaf).unwrap();
    fs::create_dir(repo.join(".git")).unwrap();
    fs::write(temp.path().join(".yaga.toml"), "bad=1").unwrap();
    fs::write(leaf.join("pyproject.toml"), "[project]\nname='ok'").unwrap();
    assert!(load_config(None, &leaf, None).unwrap().path.is_none());
    fs::write(
        repo.join("pyproject.toml"),
        "[tool.yaga.commit]\nheader-max-length=40",
    )
    .unwrap();
    assert_eq!(
        load_config(None, &leaf, None)
            .unwrap()
            .policy
            .header_max_length,
        Some(40)
    );
    fs::write(repo.join(".yaga.toml"), "[commit]\nbody-min-words=1").unwrap();
    let loaded = load_config(None, &leaf, None).unwrap();
    assert_eq!(loaded.policy.header_max_length, None);
    assert_eq!(loaded.policy.body_min_words, 1);
}

#[test]
fn starters_validate_and_publish_without_replacement() {
    use std::fs;
    for starter in [
        ConfigStarter::RecommendedV1,
        ConfigStarter::TitleV1,
        ConfigStarter::PlainV1,
        ConfigStarter::CompleteMessageV1,
    ] {
        let dir = tempfile::tempdir().unwrap();
        fs::create_dir(dir.path().join(".git")).unwrap();
        let target = dir.path().join(".yaga.toml");
        let preview = initialize_config(dir.path(), true, starter).unwrap();
        assert!(!target.exists());
        assert!(
            render_config(&preview, true, true, None)
                .unwrap()
                .contains("\"dry_run\": true")
        );
        let successes: usize = std::thread::scope(|scope| {
            let workers: Vec<_> = (0..4)
                .map(|_| scope.spawn(|| initialize_config(dir.path(), false, starter).is_ok()))
                .collect();
            workers
                .into_iter()
                .filter_map(|thread| thread.join().ok())
                .map(usize::from)
                .sum()
        });
        assert_eq!(successes, 1);
        assert_eq!(fs::read(&target).unwrap(), starter.bytes());
        assert_eq!(fs::read_dir(dir.path()).unwrap().count(), 2);
    }
}

#[test]
fn templates_use_effective_component_policy_and_leave_values_blank() {
    let p = CommitPolicy {
        allowed_types: Some(vec!["fix".into()]),
        ignored_headers: vec!["*".into()],
        scope_policy: Presence::Required,
        body_policy_by_type: vec![("FiX".into(), Presence::Required)],
        required_footer_tokens: vec!["QA".into()],
        required_colon_footer_tokens: vec!["Qa".into()],
        footer_values: vec![("QA".into(), vec!["pass".into(), "fail".into()])],
        ..Default::default()
    };
    assert!(message_template(&p, Some("docs"), Some("cli"), TemplateComment::Hash).is_err());
    assert!(message_template(&p, Some("fix"), None, TemplateComment::Hash).is_err());
    assert!(
        message_template(
            &p,
            Some("fix"),
            Some("x): y\n\nfix(z"),
            TemplateComment::Hash
        )
        .is_err()
    );
    let text = message_template(&p, Some("fix"), Some("cli"), TemplateComment::Semicolon).unwrap();
    assert!(text.starts_with("fix(cli): \n\n; "));
    assert!(text.contains("; Body policy: required."));
    assert!(text.ends_with("\n\nQa: \n"));
    assert!(!text.contains("\nQA: pass"));
    assert_eq!(
        explain_type(&p, "fix").unwrap().body_source,
        "body-policy-by-type.FiX"
    );
}
