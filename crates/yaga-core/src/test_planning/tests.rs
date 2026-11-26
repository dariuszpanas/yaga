use super::*;
use evaluation::{Expected, evaluate, parse_results};
use handoff::{Executor, Registry, create_handoff};
use serde_json::json;

const POLICY:&[u8]=b"test-policy-version=1\n[[suites]]\nid=\"unit\"\nbinding=\"pytest\"\nneeds=[\"build\"]\nvariants=[\"win\",\"linux\"]\n[[suites]]\nid=\"build\"\nbinding=\"cargo\"\n[profiles.dev]\nmode=\"selective\"\n[[rules]]\nid=\"source\"\nwhen-any=[\"src/**\"]\nselect=[\"unit\"]\n";
fn baseline() -> (PlanDocument, Registry) {
    let p = TestPolicy::parse(POLICY).unwrap();
    let selection = plan_tests(&p, "dev", Some(&["src/planner.rs".into()])).unwrap();
    let registry = Registry::new(vec![
        Executor {
            suite: "build".into(),
            binding: "cargo".into(),
            dispatch: "build".into(),
            variants: vec!["default".into()],
            capabilities: vec![],
            definition_sha256: "b".repeat(64),
        },
        Executor {
            suite: "unit".into(),
            binding: "pytest".into(),
            dispatch: "unit".into(),
            variants: vec!["win".into(), "linux".into()],
            capabilities: vec![],
            definition_sha256: "c".repeat(64),
        },
    ])
    .unwrap();
    let source = Source {
        base_commit: Some("1".repeat(40)),
        head_commit: Some("2".repeat(40)),
        comparison_commit: Some("1".repeat(40)),
        execution_commit: "2".repeat(40),
        execution_tree: "3".repeat(40),
    };
    let document =
        PlanDocument::new(&p.sha256, Some(registry.sha256()), source, selection).unwrap();
    (document, registry)
}
#[test]
fn original_canonical_digests_keep_prerequisite_and_matrix_dispatch() {
    let (doc, reg) = baseline();
    assert_eq!(
        reg.sha256(),
        "997b9d448fad441e85bd650c8e90f724c8516d06d4bf389ccb5ba4fbfef143d8"
    );
    assert_eq!(
        doc.plan_digest,
        "9bfbf366298e3eaec35e892594bf0d864208e7a5f91652f9ac9fcac6d3db671d"
    );
    let (digest, encoded) =
        encode_plan_semantics(serde_json::to_value(&doc.plan).unwrap()).unwrap();
    assert_eq!(digest, doc.plan_digest);
    assert_eq!(encoded, doc.json().unwrap());
    let plan = parse_plan(serde_json::from_str(&encoded).unwrap()).unwrap();
    let p = &plan.plan;
    let out = create_handoff(
        &plan,
        &reg,
        &p.policy_sha256,
        &p.source.execution_commit,
        &p.source.execution_tree,
    )
    .unwrap();
    assert_eq!(out["dispatch"], json!({"build":true,"unit":true}));
    assert_eq!(out["selected"][1]["needs"], json!(["build"]));
    assert_eq!(out["cells"].as_array().unwrap().len(), 3);
    let details = diagnostics::diagnose(&plan, Some(&["unit".into()])).unwrap();
    assert_eq!(details["comparison"]["additional"], json!(["build"]));
    assert_eq!(details["rules"][0]["paths"], json!(["src/planner.rs"]));
}
#[test]
fn diagnostics_and_handoff_keep_the_decoding_hosts_drive_grammar() {
    let (doc, registry) = baseline();
    let mut plan = doc.plan;
    let path = "\u{10000}:path";
    plan.selection.changed_paths = Some(vec![path.into()]);
    for reason in &mut plan.selection.reasons {
        if reason.path.is_some() {
            reason.path = Some(path.into());
        }
    }
    let (_, raw) = encode_plan_semantics(serde_json::to_value(&plan).unwrap()).unwrap();
    let wire: Value = serde_json::from_str(&raw).unwrap();
    assert!(parse_plan_with_drive_syntax(wire.clone(), DriveSyntax::UnicodeScalars).is_err());
    let decoded = parse_plan_with_drive_syntax(wire, DriveSyntax::Utf16Units).unwrap();
    let details = diagnostics::diagnose(&decoded, None).unwrap();
    assert_eq!(details["rules"][0]["paths"], json!([path]));
    let handoff = create_handoff(
        &decoded,
        &registry,
        &plan.policy_sha256,
        &plan.source.execution_commit,
        &plan.source.execution_tree,
    )
    .unwrap();
    assert_eq!(handoff["dispatch"], json!({"build":true,"unit":true}));
}

#[test]
fn plan_envelope_errors_precede_version_specific_contents() {
    let fields = "test document requires exactly its versioned object fields";
    let version = "unsupported test document version";
    let string = "test document string has an invalid type or exceeds its length limit";
    let digest = "test plan document does not match its canonical semantics and digest";
    let (doc, _) = baseline();
    let good_body = serde_json::to_value(doc.plan).unwrap();
    let mut future_body = good_body.clone();
    future_body["test-plan-version"] = json!(2);
    for (outer_version, plan_digest, body, expected) in [
        (json!(true), json!([]), json!({}), version),
        (json!(2), json!([]), json!({"future": true}), version),
        (json!(1), json!(false), json!({}), string),
        (json!(1), json!("é".repeat(65)), future_body.clone(), string),
        (json!(1), json!(""), json!({"test-plan-version": 2}), fields),
        (json!(1), json!(""), future_body, version),
        (json!(1), json!("é".repeat(64)), good_body, digest),
    ] {
        let envelope = json!({
            "test-plan-version": outer_version,
            "plan_digest": plan_digest,
            "plan": body,
        });
        assert!(matches!(parse_plan(envelope), Err(Error::Input(message)) if message == expected));
    }
    assert!(matches!(
        parse_plan(json!({"test-plan-version": 2})),
        Err(Error::Input(message)) if message == fields
    ));
}

#[test]
fn rehashed_invalid_plan_cannot_qualify() {
    let (doc, _) = baseline();
    let original = serde_json::to_value(&doc).unwrap();
    for i in 0..4 {
        let mut wire = original.clone();
        let p = &mut wire["plan"];
        match i {
            0 => p["selected"].as_array_mut().unwrap().swap(0, 1),
            1 => p["changed_paths"] = json!(["bogus", "bogus"]),
            2 => p["reasons"][0]["suite"] = json!("unknown"),
            _ => {
                p["source"].as_object_mut().unwrap().remove("base_commit");
            }
        }
        let (_, encoded) = encode_plan_semantics(p.take()).unwrap();
        assert!(parse_plan(serde_json::from_str(&encoded).unwrap()).is_err());
    }
}

#[test]
fn offline_plan_constructor_validates_independent_inputs() {
    let (document, _) = baseline();
    let plan = document.plan;
    let bind = |source, selection| {
        PlanDocument::new(
            &plan.policy_sha256,
            plan.bindings_sha256.as_deref(),
            source,
            selection,
        )
    };
    let mut wrong_source = plan.source.clone();
    wrong_source.head_commit = None;
    assert!(bind(wrong_source, plan.selection.clone()).is_err());
    let mut wrong_selection = plan.selection;
    wrong_selection.selected.clear();
    assert!(bind(plan.source, wrong_selection).is_err());
}
#[test]
fn receipts_need_bound_identities_and_every_required_cell() {
    let (doc, reg) = baseline();
    let p = &doc.plan;
    let expectations = || Expected {
        policy_sha256: &p.policy_sha256,
        bindings_sha256: reg.sha256(),
        execution_commit: &p.source.execution_commit,
        execution_tree: &p.source.execution_tree,
        run_id: "42:attempt",
        attempt: 1,
    };
    let mut receipts = json!({"test-results-version":1,"plan_digest":doc.plan_digest,"policy_sha256":p.policy_sha256,"bindings_sha256":reg.sha256(),"execution_commit":p.source.execution_commit,"execution_tree":p.source.execution_tree,"run_id":"42:attempt","attempt":1,"results":[]});
    let report = evaluate(
        &doc,
        &parse_results(receipts.clone()).unwrap(),
        expectations(),
    )
    .unwrap();
    assert!(!report.passed);
    assert_eq!(
        serde_json::from_str::<Value>(&report.json).unwrap()["counts"]["missing"],
        3
    );
    let mut wrong = expectations();
    wrong.attempt = 2;
    assert!(evaluate(&doc, &parse_results(receipts.clone()).unwrap(), wrong).is_err());
    let cell = json!({"suite":"build","variant":"default","binding":"cargo","outcome":"passed"});
    receipts["results"] = json!([cell, cell]);
    assert!(parse_results(receipts.clone()).is_err());
    receipts["results"] = json!([cell,{"suite":"unit","variant":"win","binding":"pytest","outcome":"passed"},{"suite":"unit","variant":"linux","binding":"pytest","outcome":"passed"}]);
    assert!(
        evaluate(
            &doc,
            &parse_results(receipts.clone()).unwrap(),
            expectations()
        )
        .unwrap()
        .passed
    );
    receipts["results"][1]["summary"] = Value::Null;
    assert!(parse_results(receipts).is_err());
}

#[test]
fn receipt_encoding_is_canonical_bounded_and_available_to_rust_producers() {
    use evaluation::{Cell, Outcome, Receipt, Results};
    let first = Cell {
        suite: "first".into(),
        variant: "win".into(),
        binding: "cargo".into(),
        outcome: Outcome::Passed,
        summary: Some("∑🦀\n\u{0000}\"".into()),
        evidence_sha256: None,
    };
    let mut second = first.clone();
    second.suite = "second".into();
    second.summary = None;
    let results = Results::try_from(Receipt {
        version: 1,
        plan_digest: "a".repeat(64),
        policy_sha256: "b".repeat(64),
        bindings_sha256: "c".repeat(64),
        execution_commit: "1".repeat(40),
        execution_tree: "2".repeat(40),
        run_id: "run".into(),
        attempt: 1,
        results: vec![second, first],
    })
    .unwrap();
    let document = results.document();
    assert_eq!(document["results"][0]["suite"], "first");
    assert!(document["results"][1].get("summary").is_none());
    let bytes = results.encode().unwrap();
    let text = std::str::from_utf8(&bytes).unwrap();
    assert!(text.starts_with(r#"{"attempt":1,"bindings_sha256":"#));
    assert!(text.contains(r#""summary":"∑🦀\n\u0000\"""#));
    assert!(text.ends_with("}\n"));
    assert_eq!(
        results.sha256(),
        format!("{:x}", Sha256::digest(text.trim_end_matches('\n')))
    );
    assert_eq!(results.encode_with_limit(bytes.len()).unwrap(), bytes);
    let error = results.encode_with_limit(bytes.len() - 1).unwrap_err();
    assert_eq!(error.kind(), "configuration");
    assert_eq!(parse_results(document).unwrap().encode().unwrap(), bytes);
}

#[test]
fn variant_targeting_supports_pruning_union_override_prerequisites_and_evaluation() {
    let policy_bytes = br#"test-policy-version = 1
full-on = ["Cargo.lock"]
no-impact = ["README.md"]

[[suites]]
id = "build"
binding = "cargo-build"
variants = ["linux", "windows"]

[[suites]]
id = "unit"
binding = "cargo-test"
needs = ["build"]
variants = ["linux", "macos", "windows"]

[profiles.pr]
mode = "selective"
always-variants = { unit = ["linux"] }

[profiles.pr-bare]
mode = "selective"

[profiles.release]
mode = "full"

[[rules]]
id = "win-only"
when-any = ["src/windows/**"]
select-variants = { unit = ["windows"] }

[[rules]]
id = "mac-only"
when-any = ["src/macos/**"]
select-variants = { unit = ["macos"] }

[[rules]]
id = "shared"
when-any = ["src/core/**"]
select = ["unit"]
"#;
    let policy = TestPolicy::parse(policy_bytes).unwrap();
    let registry = Registry::new(vec![
        Executor {
            suite: "build".into(),
            binding: "cargo-build".into(),
            dispatch: "build".into(),
            variants: vec!["linux".into(), "windows".into()],
            capabilities: vec![],
            definition_sha256: "b".repeat(64),
        },
        Executor {
            suite: "unit".into(),
            binding: "cargo-test".into(),
            dispatch: "unit".into(),
            variants: vec!["linux".into(), "macos".into(), "windows".into()],
            capabilities: vec![],
            definition_sha256: "c".repeat(64),
        },
    ])
    .unwrap();
    let make_doc = |selection| {
        PlanDocument::new(
            &policy.sha256,
            Some(registry.sha256()),
            Source {
                base_commit: Some("1".repeat(40)),
                head_commit: Some("2".repeat(40)),
                comparison_commit: Some("1".repeat(40)),
                execution_commit: "2".repeat(40),
                execution_tree: "3".repeat(40),
            },
            selection,
        )
        .unwrap()
    };

    // 1. Single-variant targeting keeps only `unit/windows` while expanding full prerequisite `build`.
    let win_sel = plan_tests(&policy, "pr-bare", Some(&["src/windows/job.rs".into()])).unwrap();
    assert_eq!(win_sel.effective_mode, Mode::Selective);
    assert_eq!(
        win_sel.selected,
        vec![
            SelectedSuite {
                suite: "build".into(),
                variants: vec!["linux".into(), "windows".into()],
            },
            SelectedSuite {
                suite: "unit".into(),
                variants: vec!["windows".into()],
            },
        ]
    );
    assert!(win_sel.reasons.iter().any(|r| {
        r.code == "test.rule"
            && r.suite.as_deref() == Some("unit")
            && r.rule.as_deref() == Some("win-only")
            && r.variants.as_deref() == Some(&["windows".to_owned()][..])
    }));
    let win_doc = make_doc(win_sel);
    let encoded = win_doc.json().unwrap();
    let decoded = parse_plan(serde_json::from_str(&encoded).unwrap()).unwrap();
    assert_eq!(decoded.plan_digest, win_doc.plan_digest);
    assert!(decoded.text().contains("variants=windows"));

    // Diagnostics report partial breadth when all suite IDs are selected but matrix cells are pruned.
    let diag = diagnostics::diagnose(&decoded, None).unwrap();
    assert_eq!(diag["selection_breadth"], "partial");
    assert_eq!(diag["counts"]["catalog_cells"], 5);
    assert_eq!(diag["counts"]["selected_cells"], 3);

    // Handoff and evaluation enforce exact selected cells only.
    let handoff = create_handoff(
        &decoded,
        &registry,
        &policy.sha256,
        &decoded.plan.source.execution_commit,
        &decoded.plan.source.execution_tree,
    )
    .unwrap();
    assert_eq!(handoff["cells"].as_array().unwrap().len(), 3);
    assert_eq!(handoff["selected"][1]["variants"], json!(["windows"]));

    let expected = Expected {
        policy_sha256: &policy.sha256,
        bindings_sha256: registry.sha256(),
        execution_commit: &decoded.plan.source.execution_commit,
        execution_tree: &decoded.plan.source.execution_tree,
        run_id: "run-win",
        attempt: 1,
    };
    let pass_receipt = parse_results(json!({
        "test-results-version": 1,
        "plan_digest": decoded.plan_digest,
        "policy_sha256": policy.sha256,
        "bindings_sha256": registry.sha256(),
        "execution_commit": decoded.plan.source.execution_commit,
        "execution_tree": decoded.plan.source.execution_tree,
        "run_id": "run-win",
        "attempt": 1,
        "results": [
            {"suite": "build", "variant": "linux", "binding": "cargo-build", "outcome": "passed"},
            {"suite": "build", "variant": "windows", "binding": "cargo-build", "outcome": "passed"},
            {"suite": "unit", "variant": "windows", "binding": "cargo-test", "outcome": "passed"}
        ]
    }))
    .unwrap();
    assert!(evaluate(&decoded, &pass_receipt, expected).unwrap().passed);

    // Unselected variant cell in receipt is rejected.
    let extra_receipt = parse_results(json!({
        "test-results-version": 1,
        "plan_digest": decoded.plan_digest,
        "policy_sha256": policy.sha256,
        "bindings_sha256": registry.sha256(),
        "execution_commit": decoded.plan.source.execution_commit,
        "execution_tree": decoded.plan.source.execution_tree,
        "run_id": "run-win",
        "attempt": 1,
        "results": [
            {"suite": "build", "variant": "linux", "binding": "cargo-build", "outcome": "passed"},
            {"suite": "build", "variant": "windows", "binding": "cargo-build", "outcome": "passed"},
            {"suite": "unit", "variant": "linux", "binding": "cargo-test", "outcome": "passed"},
            {"suite": "unit", "variant": "windows", "binding": "cargo-test", "outcome": "passed"}
        ]
    }))
    .unwrap();
    assert!(
        evaluate(
            &decoded,
            &extra_receipt,
            Expected {
                policy_sha256: &policy.sha256,
                bindings_sha256: registry.sha256(),
                execution_commit: &decoded.plan.source.execution_commit,
                execution_tree: &decoded.plan.source.execution_tree,
                run_id: "run-win",
                attempt: 1,
            },
        )
        .is_err()
    );

    // 2. Multi-rule union merges variants deterministically in sorted order.
    let union_sel = plan_tests(
        &policy,
        "pr-bare",
        Some(&["src/windows/job.rs".into(), "src/macos/job.rs".into()]),
    )
    .unwrap();
    assert_eq!(union_sel.selected[1].variants, vec!["macos", "windows"]);

    // 3. Profile `always-variants` unions with matching rules and applies to empty diffs.
    let always_sel = plan_tests(&policy, "pr", Some(&["src/windows/job.rs".into()])).unwrap();
    assert_eq!(always_sel.selected[1].variants, vec!["linux", "windows"]);
    assert!(always_sel.reasons.iter().any(|r| {
        r.code == "test.always"
            && r.suite.as_deref() == Some("unit")
            && r.variants.as_deref() == Some(&["linux".to_owned()][..])
    }));
    let empty_sel = plan_tests(&policy, "pr", Some(&[])).unwrap();
    assert_eq!(empty_sel.selected[1].variants, vec!["linux"]);

    // 4. Whole-suite selection overrides partial variant targeting.
    let whole_sel = plan_tests(
        &policy,
        "pr-bare",
        Some(&["src/windows/job.rs".into(), "src/core/lib.rs".into()]),
    )
    .unwrap();
    assert_eq!(
        whole_sel.selected[1].variants,
        vec!["linux", "macos", "windows"]
    );

    // 5. `full-on` fallback and `full` profile select all variants of all suites.
    let full_trigger = plan_tests(&policy, "pr-bare", Some(&["Cargo.lock".into()])).unwrap();
    assert_eq!(full_trigger.effective_mode, Mode::Full);
    assert_eq!(
        full_trigger.selected[1].variants,
        vec!["linux", "macos", "windows"]
    );
    let release_sel = plan_tests(&policy, "release", None).unwrap();
    assert_eq!(
        release_sel.selected[1].variants,
        vec!["linux", "macos", "windows"]
    );
}

#[test]
fn variant_targeting_rejects_invalid_policy_and_tampered_plan_variants() {
    let header = "test-policy-version = 1\n[[suites]]\nid = 'unit'\nbinding = 'cargo'\nvariants = ['linux', 'windows']\n";
    for bad in [
        format!(
            "{header}[profiles.pr]\nmode = 'selective'\n[[rules]]\nid = 'r'\nwhen-any = ['src/**']\nselect-variants = {{ unknown = ['linux'] }}\n"
        ),
        format!(
            "{header}[profiles.pr]\nmode = 'selective'\n[[rules]]\nid = 'r'\nwhen-any = ['src/**']\nselect-variants = {{ unit = ['macos'] }}\n"
        ),
        format!(
            "{header}[profiles.pr]\nmode = 'selective'\n[[rules]]\nid = 'r'\nwhen-any = ['src/**']\nselect-variants = {{ unit = [] }}\n"
        ),
        format!(
            "{header}[profiles.pr]\nmode = 'selective'\n[[rules]]\nid = 'r'\nwhen-any = ['src/**']\nselect-variants = {{ unit = ['linux', 'linux'] }}\n"
        ),
        format!(
            "{header}[profiles.pr]\nmode = 'selective'\n[[rules]]\nid = 'r'\nwhen-any = ['src/**']\nselect-variants = {{}}\n"
        ),
        format!(
            "{header}[profiles.pr]\nmode = 'selective'\nalways-variants = {{ unit = ['macos'] }}\n"
        ),
    ] {
        assert!(TestPolicy::parse(bad.as_bytes()).is_err(), "{bad}");
    }

    // Tampered plans dropping prerequisite variants, adding invalid reason variants, or dropping
    // variants in full mode are rejected fail-closed by `parse_plan`.
    let policy = TestPolicy::parse(
        br#"test-policy-version = 1
[[suites]]
id = "build"
binding = "cargo"
variants = ["linux", "windows"]
[[suites]]
id = "unit"
binding = "pytest"
needs = ["build"]
variants = ["linux", "windows"]
[profiles.pr]
mode = "selective"
[[rules]]
id = "win"
when-any = ["src/win/**"]
select-variants = { unit = ["windows"] }
"#,
    )
    .unwrap();
    let sel = plan_tests(&policy, "pr", Some(&["src/win/a.rs".into()])).unwrap();
    let doc = PlanDocument::new(
        &policy.sha256,
        None,
        Source {
            base_commit: Some("1".repeat(40)),
            head_commit: Some("2".repeat(40)),
            comparison_commit: Some("1".repeat(40)),
            execution_commit: "2".repeat(40),
            execution_tree: "3".repeat(40),
        },
        sel,
    )
    .unwrap();
    let original = serde_json::to_value(&doc).unwrap();
    for case in 0..5 {
        let mut wire = original.clone();
        let p = &mut wire["plan"];
        match case {
            // Dropping a prerequisite variant is forbidden
            0 => p["selected"][0]["variants"] = json!(["windows"]),
            // Empty selected variants is forbidden
            1 => p["selected"][1]["variants"] = json!([]),
            // Unselected variant in reason is forbidden
            2 => p["reasons"][1]["variants"] = json!(["linux"]),
            // Explicit null variants on reason is forbidden
            3 => p["reasons"][0]["variants"] = Value::Null,
            // Variants attached to test.prerequisite reason is forbidden
            _ => p["reasons"][0]["variants"] = json!(["linux"]),
        }
        let (_, encoded) = encode_plan_semantics(p.take()).unwrap();
        assert!(
            parse_plan(serde_json::from_str(&encoded).unwrap()).is_err(),
            "case {case} should fail"
        );
    }
}

#[test]
fn design_example_policies_parse_and_plan_deterministically() {
    let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let examples = root.join("docs/designs/examples/test-planning");
    if !examples.is_dir() {
        // Packaged crates.io tarballs do not bundle the repository docs tree.
        assert!(
            !root.join("Cargo.toml").is_file(),
            "missing docs/designs/examples/test-planning in workspace checkout"
        );
        return;
    }

    let polyglot_bytes = std::fs::read(examples.join("polyglot.toml")).expect("read polyglot.toml");
    let polyglot = TestPolicy::parse(&polyglot_bytes).expect("parse polyglot.toml");
    let dev_container = plan_tests(
        &polyglot,
        "development",
        Some(&["docker/app.Dockerfile".into()]),
    )
    .unwrap();
    assert_eq!(dev_container.effective_mode, Mode::Selective);
    let selected_ids: Vec<&str> = dev_container
        .selected
        .iter()
        .map(|s| s.suite.as_str())
        .collect();
    assert!(selected_ids.contains(&"repository.smoke"));
    assert!(selected_ids.contains(&"image.build"));
    assert!(selected_ids.contains(&"container.smoke"));
    assert!(selected_ids.contains(&"infra.chainsaw"));
    assert!(selected_ids.contains(&"qualification.ray"));
    assert!(!selected_ids.contains(&"frontend.unit"));

    let no_impact = plan_tests(&polyglot, "development", Some(&["LICENSE".into()])).unwrap();
    assert_eq!(no_impact.effective_mode, Mode::Selective);
    assert_eq!(
        no_impact
            .selected
            .iter()
            .map(|s| s.suite.as_str())
            .collect::<Vec<_>>(),
        vec!["repository.smoke"]
    );

    let poly_full = plan_tests(&polyglot, "release", None).unwrap();
    assert_eq!(poly_full.effective_mode, Mode::Full);
    assert_eq!(poly_full.selected.len(), polyglot.suites.len());

    let django_bytes =
        std::fs::read(examples.join("django-ray.toml")).expect("read django-ray.toml");
    let django = TestPolicy::parse(&django_bytes).expect("parse django-ray.toml");
    let latency = plan_tests(
        &django,
        "development",
        Some(&["qualification/latency/run.py".into()]),
    )
    .unwrap();
    assert_eq!(latency.effective_mode, Mode::Selective);
    assert_eq!(
        latency
            .selected
            .iter()
            .map(|s| s.suite.as_str())
            .collect::<Vec<_>>(),
        vec!["qualification.latency"]
    );

    let docs_only = plan_tests(&django, "development", Some(&["docs/index.md".into()])).unwrap();
    assert_eq!(docs_only.effective_mode, Mode::Selective);
    assert!(docs_only.selected.is_empty());

    let doc = PlanDocument::new(
        &django.sha256,
        None,
        Source {
            base_commit: Some("1".repeat(40)),
            head_commit: Some("2".repeat(40)),
            comparison_commit: Some("1".repeat(40)),
            execution_commit: "2".repeat(40),
            execution_tree: "3".repeat(40),
        },
        latency,
    )
    .unwrap();
    let encoded = doc.json().unwrap();
    let decoded = parse_plan(serde_json::from_str(&encoded).unwrap()).unwrap();
    assert_eq!(decoded.plan_digest, doc.plan_digest);
}
