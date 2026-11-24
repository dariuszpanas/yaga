use super::*;
use crate::fs::absolute_path;
use std::io::Read;
use std::path::PathBuf;

#[test]
fn strict_diagnostic_identity_shape_and_positions() {
    let result=diagnostics(br#"[{"message":"bad\u001bname","kind":"Expression Error","filepath":"a.yml","line":0,"column":3,"end_column":7,"snippet":""}]"#,"a.yml").unwrap();
    assert_eq!(result[0].line, 1);
    assert_eq!(result[0].column, 3);
    assert_eq!(result[0].code, "actionlint.expression-error");
    assert_eq!(result[0].message, "bad?name");
    for bad in [
        r#"[{"message":"x","kind":"a","filepath":"b.yml","line":1,"column":1}]"#,
        r#"[{"message":"x","kind":"a","filepath":"a.yml","line":1,"column":true}]"#,
        r#"[{"message":"x","message":"y","kind":"a","filepath":"a.yml","line":1,"column":1}]"#,
        r#"[{"message":"x","kind":"a","filepath":"a.yml","line":1,"column":1,"other":0}]"#,
        r#"[{"message":"x","kind":"a","filepath":"a.yml","line":10000000,"column":1}]"#,
    ] {
        assert!(diagnostics(bad.as_bytes(), "a.yml").is_err());
    }
}

fn file(repo: &Path, relative: &str, bytes: &[u8]) -> PathBuf {
    let path = repo.join(relative);
    std::fs::create_dir_all(path.parent().unwrap()).unwrap();
    std::fs::write(&path, bytes).unwrap();
    path
}

#[test]
fn snapshot_stops_on_yaml_limits_in_selected_and_reusable_workflows() {
    let temp = tempfile::tempdir().unwrap();
    let repo = absolute_path(temp.path()).unwrap();
    let selected = ".github/workflows/check.yml";
    let reusable = ".github/workflows/reuse.yml";
    let cases = [
        (
            format!("x: {}0{}", "[".repeat(70), "]".repeat(70)),
            "exceeds the hard YAML depth limit of 64",
        ),
        ("x: &cycle [*cycle]".to_owned(), "contains an alias cycle"),
    ];
    for (yaml, detail) in cases {
        for path in [selected, reusable] {
            let content = if path == reusable {
                file(&repo, reusable, yaml.as_bytes());
                format!("jobs:\n  reuse:\n    uses: ./{reusable}\n").into_bytes()
            } else {
                yaml.as_bytes().to_vec()
            };
            let input = WorkflowInput {
                path: file(&repo, selected, &content),
                relative_path: selected.into(),
                content,
            };
            assert_eq!(
                snapshot::Snapshot::build(&repo, &[input]).err(),
                Some(format!("{path} {detail}")),
            );
        }
    }
}

#[test]
fn snapshot_preserves_malformed_input_for_actionlint() {
    let temp = tempfile::tempdir().unwrap();
    let repo = absolute_path(temp.path()).unwrap();
    let name = ".github/workflows/check.yml";
    for raw in [b"jobs: [".as_slice(), b"---\n---\n", b"", &[0xff]] {
        let input = WorkflowInput {
            path: file(&repo, name, raw),
            relative_path: name.into(),
            content: raw.to_vec(),
        };
        let snapshot = snapshot::Snapshot::build(&repo, &[input]).unwrap();
        assert_eq!(snapshot.files[name], raw);
    }
}

#[test]
fn snapshot_preserves_yaml_marks_and_stages_only_runtime_presence() {
    let temp = tempfile::tempdir().unwrap();
    let repo = absolute_path(temp.path()).unwrap();
    let content=b"on: push\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: '$/local'\n        name: '$/local'\n".to_vec();
    let relative = ".github/workflows/check.yml";
    let inputs = vec![WorkflowInput {
        path: file(&repo, relative, &content),
        relative_path: relative.into(),
        content,
    }];
    file(
        &repo,
        "local/action.yaml",
        b"name: x\ndescription: y\nruns:\n  using: node20\n  main: entry.js\n",
    );
    file(&repo, "local/action.yml", b"not the selected manifest");
    file(&repo, "local/entry.js", b"private runtime contents");
    file(&repo, ".github/actionlint.yml", b"self-hosted-runner: {}\n");
    let snapshot = snapshot::Snapshot::build(&repo, &inputs).unwrap();
    assert_eq!(snapshot.config(), Some(".github/actionlint.yml"));
    assert!(snapshot.files["local/entry.js"].is_empty());
    assert!(!snapshot.files.contains_key("local/action.yml"));
    let changed = String::from_utf8_lossy(&snapshot.files[relative]);
    assert!(changed.contains("uses: './local'"));
    assert!(changed.contains("name: '$/local'"));
    let first = snapshot.archive().unwrap();
    assert_eq!(first, snapshot.archive().unwrap());
    let mut archive = tar::Archive::new(first.as_slice());
    let mut found = false;
    for record in archive.entries().unwrap() {
        let mut record = record.unwrap();
        if record.path().unwrap().as_ref() == Path::new("local/entry.js") {
            let mut data = Vec::new();
            record.read_to_end(&mut data).unwrap();
            assert!(data.is_empty());
            found = true;
        }
    }
    assert!(found);
}

#[test]
#[ignore = "requires a running local Linux-container Docker engine and the pinned actionlint image"]
fn real_docker_findings_and_cleanup() {
    let temp = tempfile::tempdir().unwrap();
    let repo = absolute_path(temp.path()).unwrap();
    let relative = ".github/workflows/lint.yml";
    let content=b"on: push\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo hi\n        unsupported-field: true\n".to_vec();
    let inputs = vec![WorkflowInput {
        relative_path: relative.into(),
        path: file(&repo, relative, &content),
        content,
    }];
    let results = lint_inputs(&repo, &inputs).unwrap();
    assert_eq!(results.len(), 1);
    assert!(
        results[0]
            .1
            .iter()
            .any(|d| d.message.contains("unsupported-field"))
    );
}
