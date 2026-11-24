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
